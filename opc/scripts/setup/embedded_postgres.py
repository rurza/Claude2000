"""Embedded PostgreSQL support via pgserver.

Provides zero-Docker postgres + pgvector for Claude2000.
Uses pgserver (pip install pgserver) which bundles postgres binaries.

USAGE:
    from scripts.setup.embedded_postgres import (
        start_embedded_postgres,
        stop_embedded_postgres,
        run_migrations_direct,
        generate_database_url,
    )
"""

from pathlib import Path
from typing import Any
from urllib.parse import quote_plus


def start_embedded_postgres(pgdata: Path) -> dict[str, Any]:
    """Start embedded postgres server using pgserver.

    Args:
        pgdata: Directory to store postgres data files.
                Will be created if it doesn't exist.

    Returns:
        dict with keys:
            - success: bool
            - uri: str (connection URI if success)
            - error: str (if failed)
            - server: PostgresServer instance (for cleanup)
    """
    try:
        import pgserver
    except ImportError:
        return {
            "success": False,
            "error": "pgserver not installed. Install with: pip install pgserver",
        }

    try:
        # Ensure pgdata directory exists
        pgdata.mkdir(parents=True, exist_ok=True)

        # Start server (pgserver handles init if needed)
        server = pgserver.get_server(str(pgdata))
        uri = server.get_uri()

        return {
            "success": True,
            "uri": uri,
            "server": server,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


async def initialize_embedded_postgres(pgdata: Path, venv_path: Path, schema_path: Path) -> dict[str, Any]:
    """Initialize and start embedded postgres with proper configuration.

    This function:
    1. Finds postgres binaries in the pgserver venv
    2. Runs initdb if database not initialized
    3. Configures postgres to use socket in pgdata directory
    4. Starts postgres
    5. Creates the postgres superuser role
    6. Creates the continuous_claude database
    7. Creates pgvector extension
    8. Applies the schema

    Args:
        pgdata: Directory for postgres data files
        venv_path: Path to pgserver venv (contains postgres binaries)
        schema_path: Path to init-schema.sql

    Returns:
        dict with keys:
            - success: bool
            - uri: str (connection URI if success)
            - error: str (if failed)
            - warnings: list[str] (optional)
    """
    import asyncio
    import glob
    import os
    import subprocess
    import sys

    warnings = []

    # Find postgres binaries in pgserver installation
    pgserver_pattern = str(venv_path / "lib" / "python*" / "site-packages" / "pgserver" / "pginstall" / "bin")
    bin_dirs = glob.glob(pgserver_pattern)
    if not bin_dirs:
        return {"success": False, "error": f"Could not find pgserver binaries at {pgserver_pattern}"}

    bin_dir = Path(bin_dirs[0])
    initdb = bin_dir / "initdb"
    pg_ctl = bin_dir / "pg_ctl"
    psql = bin_dir / "psql"

    if not all(p.exists() for p in [initdb, pg_ctl, psql]):
        return {"success": False, "error": f"Missing postgres binaries in {bin_dir}"}

    # Ensure pgdata directory exists
    pgdata.mkdir(parents=True, exist_ok=True)

    # Check if database is already initialized
    pg_version_file = pgdata / "PG_VERSION"
    if not pg_version_file.exists():
        # Run initdb
        proc = await asyncio.create_subprocess_exec(
            str(initdb), "-D", str(pgdata),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return {"success": False, "error": f"initdb failed: {stderr.decode()}"}

    # Configure postgres to use socket in pgdata directory
    postgresql_conf = pgdata / "postgresql.conf"
    if postgresql_conf.exists():
        conf_content = postgresql_conf.read_text()
        if "unix_socket_directories" not in conf_content or f"'{pgdata}'" not in conf_content:
            # Add or update unix_socket_directories
            with open(postgresql_conf, "a") as f:
                f.write(f"\n# Added by Claude2000 setup\nunix_socket_directories = '{pgdata}'\n")

    # Check if server is already running
    socket_file = pgdata / ".s.PGSQL.5432"
    if not socket_file.exists():
        # Start postgres
        logfile = pgdata / "logfile"
        proc = await asyncio.create_subprocess_exec(
            str(pg_ctl), "-D", str(pgdata), "-l", str(logfile), "start",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return {"success": False, "error": f"pg_ctl start failed: {stderr.decode()}"}

        # Wait for socket to appear
        for _ in range(30):  # 3 seconds max
            if socket_file.exists():
                break
            await asyncio.sleep(0.1)
        else:
            return {"success": False, "error": "Postgres started but socket not found"}

    # Get current OS user (used by initdb as superuser)
    current_user = os.environ.get("USER", os.environ.get("USERNAME", "postgres"))

    # Helper to run psql commands (initially as current OS user)
    async def run_psql(sql: str, database: str = "postgres", user: str = current_user) -> tuple[bool, str]:
        proc = await asyncio.create_subprocess_exec(
            str(psql), "-h", str(pgdata), "-U", user, "-d", database, "-c", sql,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode == 0, stderr.decode()

    # Create postgres superuser role (for cross-machine portability)
    success, err = await run_psql("CREATE ROLE postgres WITH SUPERUSER LOGIN;")
    if not success and "already exists" not in err:
        warnings.append(f"Could not create postgres role: {err}")

    # Create continuous_claude database
    success, err = await run_psql("CREATE DATABASE continuous_claude;")
    if not success and "already exists" not in err:
        return {"success": False, "error": f"Could not create database: {err}"}

    # Enable pgvector extension
    success, err = await run_psql("CREATE EXTENSION IF NOT EXISTS vector;", "continuous_claude")
    if not success:
        return {"success": False, "error": f"Could not create vector extension: {err}"}

    # Apply schema if provided
    if schema_path and schema_path.exists():
        proc = await asyncio.create_subprocess_exec(
            str(psql), "-h", str(pgdata), "-U", current_user, "-d", "continuous_claude",
            "-f", str(schema_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        stderr_str = stderr.decode()
        # pg_trgm failure is expected and ok
        if proc.returncode != 0 and "pg_trgm" not in stderr_str:
            return {"success": False, "error": f"Schema application failed: {stderr_str}"}
        if "pg_trgm" in stderr_str and "is not available" in stderr_str:
            warnings.append("Optional extension pg_trgm not available (ok)")

    # Final connection URI for continuous_claude database (use postgres user for portability)
    final_uri = f"postgresql://postgres:@/continuous_claude?host={pgdata}"

    result = {"success": True, "uri": final_uri}
    if warnings:
        result["warnings"] = warnings
    return result


def stop_embedded_postgres(pgdata: Path) -> dict[str, Any]:
    """Stop embedded postgres server.

    Args:
        pgdata: Directory containing postgres data files.

    Returns:
        dict with keys:
            - success: bool
            - error: str (if failed)
    """
    try:
        import pgserver
        from pgserver._commands import pg_ctl
    except ImportError:
        return {
            "success": False,
            "error": "pgserver not installed",
        }

    try:
        # Use pg_ctl to stop the server
        pg_ctl(["-m", "fast", "stop"], pgdata=pgdata)
        return {"success": True}
    except Exception as e:
        # Server might already be stopped
        error_msg = str(e).lower()
        if "not running" in error_msg or "no server running" in error_msg:
            return {"success": True}
        return {
            "success": False,
            "error": str(e),
        }


def run_migrations_direct(uri: str, schema_path: Path) -> dict[str, Any]:
    """Run migrations directly via psycopg2.

    Unlike docker_setup.run_migrations which uses docker exec,
    this connects directly to postgres and runs the SQL.

    Handles missing optional extensions (pg_trgm) gracefully.

    Args:
        uri: PostgreSQL connection URI
        schema_path: Path to SQL file with schema

    Returns:
        dict with keys:
            - success: bool
            - error: str (if failed)
            - warnings: list[str] (optional extensions that failed)
    """
    try:
        import psycopg2
    except ImportError:
        return {
            "success": False,
            "error": "psycopg2 not installed. Install with: pip install psycopg2-binary",
        }

    if not schema_path.exists():
        return {
            "success": False,
            "error": f"Schema file not found: {schema_path}",
        }

    # Extensions that are optional (don't fail if missing)
    optional_extensions = {"pg_trgm"}

    try:
        conn = psycopg2.connect(uri)
        conn.autocommit = True  # Required for CREATE EXTENSION
        cur = conn.cursor()

        # Read schema
        schema_sql = schema_path.read_text()

        # Split into statements and execute individually
        # This allows us to handle optional extension failures
        statements = _split_sql_statements(schema_sql)
        warnings = []

        for stmt in statements:
            stmt = stmt.strip()
            if not stmt:
                continue

            try:
                cur.execute(stmt)
            except Exception as e:
                error_msg = str(e)
                # Check if this is an optional extension failure
                is_optional = any(
                    ext in stmt.lower() and "create extension" in stmt.lower()
                    for ext in optional_extensions
                )
                if is_optional and "is not available" in error_msg:
                    warnings.append(f"Optional extension skipped: {error_msg.split(chr(10))[0]}")
                else:
                    # Re-raise for required statements
                    raise

        conn.close()
        result = {"success": True}
        if warnings:
            result["warnings"] = warnings
        return result
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def _split_sql_statements(sql: str) -> list[str]:
    """Split SQL into individual statements.

    Handles semicolons inside strings and comments.
    Simple implementation that works for our schema.
    """
    statements = []
    current = []
    in_string = False
    string_char = None
    i = 0

    while i < len(sql):
        char = sql[i]

        # Handle string literals
        if char in ("'", '"') and not in_string:
            in_string = True
            string_char = char
            current.append(char)
        elif char == string_char and in_string:
            # Check for escaped quote
            if i + 1 < len(sql) and sql[i + 1] == string_char:
                current.append(char)
                current.append(sql[i + 1])
                i += 1
            else:
                in_string = False
                string_char = None
                current.append(char)
        elif char == ";" and not in_string:
            # End of statement
            statements.append("".join(current))
            current = []
        elif char == "-" and i + 1 < len(sql) and sql[i + 1] == "-" and not in_string:
            # Line comment - skip to end of line
            while i < len(sql) and sql[i] != "\n":
                current.append(sql[i])
                i += 1
            if i < len(sql):
                current.append(sql[i])  # Include the newline
        else:
            current.append(char)

        i += 1

    # Don't forget the last statement
    if current:
        final = "".join(current).strip()
        if final:
            statements.append(final)

    return statements


async def setup_embedded_environment() -> dict[str, Any]:
    """Setup embedded postgres environment with Python 3.12 venv.

    Creates a dedicated venv with Python 3.12 (required by pgserver)
    and installs pgserver + psycopg2-binary.

    Returns:
        dict with keys:
            - success: bool
            - pgdata: Path (postgres data directory)
            - venv: Path (Python 3.12 venv)
            - error: str (if failed)
    """
    import asyncio
    import sys

    pgdata = Path.home() / ".claude" / "pgdata"
    venv_path = Path.home() / ".claude" / "pgserver-venv"

    try:
        # Create pgdata directory
        pgdata.mkdir(parents=True, exist_ok=True)

        # Check if venv already exists and has pgserver
        python_exe = venv_path / "bin" / "python"
        if sys.platform == "win32":
            python_exe = venv_path / "Scripts" / "python.exe"

        if venv_path.exists() and python_exe.exists():
            # Verify pgserver is installed
            proc = await asyncio.create_subprocess_exec(
                str(python_exe), "-c", "import pgserver",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode == 0:
                return {"success": True, "pgdata": pgdata, "venv": venv_path}

        # Create venv with Python 3.12
        proc = await asyncio.create_subprocess_exec(
            "uv", "venv", str(venv_path), "--python", "3.12",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            return {"success": False, "error": f"Failed to create venv: {stderr.decode()}"}

        # Install pgserver and psycopg2
        proc = await asyncio.create_subprocess_exec(
            "uv", "pip", "install", "pgserver", "psycopg2-binary",
            "--python", str(python_exe),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            return {"success": False, "error": f"Failed to install pgserver: {stderr.decode()}"}

        return {"success": True, "pgdata": pgdata, "venv": venv_path}

    except Exception as e:
        return {"success": False, "error": str(e)}


def generate_database_url(config: dict[str, Any]) -> str:
    """Generate DATABASE_URL for different modes.

    Args:
        config: dict with keys:
            - mode: "docker" | "embedded" | "sqlite"
            - For docker: host, port, database, user, password
            - For embedded: pgdata (path to data directory)
            - For sqlite: (no additional keys needed)

    Returns:
        Connection string or empty string for sqlite mode
    """
    mode = config.get("mode", "docker")

    if mode == "sqlite":
        # Empty string signals fallback to SQLite
        return ""

    if mode == "embedded":
        # If URI was set by initialize_embedded_postgres, use it
        if config.get("uri"):
            return config["uri"]
        # Embedded uses Unix socket via pgdata path (postgres user for portability)
        pgdata = config.get("pgdata", "")
        return f"postgresql://postgres:@/continuous_claude?host={pgdata}"

    # Docker mode (default)
    host = config.get("host", "localhost")
    port = config.get("port", 5432)
    database = config.get("database", "continuous_claude")
    user = config.get("user", "claude")
    password = config.get("password", "")

    # URL-encode password in case it has special characters
    if password:
        password_encoded = quote_plus(password)
        return f"postgresql://{user}:{password_encoded}@{host}:{port}/{database}"
    else:
        return f"postgresql://{user}@{host}:{port}/{database}"
