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

    # Configure postgres to use socket in pgdata directory and non-standard port
    # Port 5433 avoids conflicts with user's existing postgres on 5432
    postgresql_conf = pgdata / "postgresql.conf"
    if postgresql_conf.exists():
        conf_content = postgresql_conf.read_text()
        config_additions = []
        if "unix_socket_directories" not in conf_content or f"'{pgdata}'" not in conf_content:
            config_additions.append(f"unix_socket_directories = '{pgdata}'")
        if "port = 5433" not in conf_content:
            config_additions.append("port = 5433")
        if config_additions:
            with open(postgresql_conf, "a") as f:
                f.write(f"\n# Added by Claude2000 setup\n" + "\n".join(config_additions) + "\n")

    # Check if server is already running (port 5433)
    socket_file = pgdata / ".s.PGSQL.5433"
    postmaster_pid = pgdata / "postmaster.pid"

    # Clean up stale postmaster.pid if process is not running
    if postmaster_pid.exists() and not socket_file.exists():
        try:
            pid_content = postmaster_pid.read_text().strip().split('\n')
            if pid_content:
                old_pid = int(pid_content[0])
                # Check if process is actually running
                try:
                    os.kill(old_pid, 0)  # Signal 0 = check if process exists
                    # Process exists - postgres might be starting up, wait a bit
                    await asyncio.sleep(1)
                except OSError:
                    # Process doesn't exist - stale pid file
                    warnings.append(f"Removed stale postmaster.pid (PID {old_pid} not running)")
                    postmaster_pid.unlink()
        except (ValueError, IndexError):
            # Malformed pid file - remove it
            warnings.append("Removed malformed postmaster.pid")
            postmaster_pid.unlink()

    if not socket_file.exists():
        # Start postgres with retry logic for port-in-use scenarios
        logfile = pgdata / "logfile"
        max_retries = 5
        retry_delay = 1.0  # seconds

        for attempt in range(max_retries):
            proc = await asyncio.create_subprocess_exec(
                str(pg_ctl), "-D", str(pgdata), "-l", str(logfile), "start",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                break

            stderr_text = stderr.decode()
            # Check if it's a port-in-use error (transient, can retry)
            if "Address already in use" in stderr_text or "could not create any TCP/IP sockets" in stderr_text:
                if attempt < max_retries - 1:
                    warnings.append(f"Port 5433 in use, retrying in {retry_delay}s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 1.5  # Exponential backoff
                    # Clean up any partial state
                    if postmaster_pid.exists():
                        try:
                            postmaster_pid.unlink()
                        except OSError:
                            pass
                    continue
                else:
                    return {
                        "success": False,
                        "error": f"pg_ctl start failed after {max_retries} attempts: Port 5433 is in use. "
                                 "Another postgres may be running, or a recent process hasn't released the port yet. "
                                 "Try again in a few seconds or check: lsof -i :5433",
                    }
            else:
                return {"success": False, "error": f"pg_ctl start failed: {stderr_text}"}

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
            str(psql), "-h", str(pgdata), "-p", "5433", "-U", user, "-d", database, "-c", sql,
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
            str(psql), "-h", str(pgdata), "-p", "5433", "-U", current_user, "-d", "continuous_claude",
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
    final_uri = f"postgresql://postgres:@localhost:5433/continuous_claude"

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
    """Generate CLAUDE2000_DB_URL connection string for different modes.

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
    port = config.get("port", 5433)
    database = config.get("database", "continuous_claude")
    user = config.get("user", "claude")
    password = config.get("password", "")

    # URL-encode password in case it has special characters
    if password:
        password_encoded = quote_plus(password)
        return f"postgresql://{user}:{password_encoded}@{host}:{port}/{database}"
    else:
        return f"postgresql://{user}@{host}:{port}/{database}"


def check_embedded_postgres_status() -> dict[str, Any]:
    """Check if embedded postgres is running.

    Returns:
        dict with keys:
            - running: bool
            - pgdata: Path (if found)
            - socket: Path (if running)
            - reason: str (if not running)
    """
    import os

    pgdata = Path.home() / ".claude" / "pgdata"

    if not pgdata.exists():
        return {"running": False, "reason": "pgdata directory not found"}

    socket = pgdata / ".s.PGSQL.5433"
    if not socket.exists():
        return {"running": False, "reason": "socket file not found", "pgdata": pgdata}

    # Check postmaster.pid for "ready" status
    pid_file = pgdata / "postmaster.pid"
    if pid_file.exists():
        content = pid_file.read_text()
        if "ready" in content:
            return {"running": True, "pgdata": pgdata, "socket": socket}

    return {"running": False, "reason": "postgres not ready", "pgdata": pgdata}


def apply_schema_if_needed(pgdata: Path, schema_path: Path, venv_path: Path | None = None) -> dict[str, Any]:
    """Apply schema migration to embedded postgres if needed.

    Uses psql directly to apply the schema. Safe to run multiple times
    due to IF NOT EXISTS clauses in the schema.

    Args:
        pgdata: Path to postgres data directory (used as socket host)
        schema_path: Path to SQL schema file
        venv_path: Path to pgserver venv (contains postgres binaries).
                   If None, falls back to system psql.

    Returns:
        dict with keys:
            - success: bool
            - error: str (if failed)
            - tables_before: int
            - tables_after: int
            - warnings: list[str] (optional)
    """
    import glob
    import os
    import subprocess

    if not schema_path.exists():
        return {"success": False, "error": f"Schema file not found: {schema_path}"}

    # Find psql binary - prefer pgserver venv, fall back to system
    psql_cmd = "psql"
    if venv_path:
        pgserver_pattern = str(venv_path / "lib" / "python*" / "site-packages" / "pgserver" / "pginstall" / "bin" / "psql")
        matches = glob.glob(pgserver_pattern)
        if matches:
            psql_cmd = matches[0]

    # Get current user for psql connection
    current_user = os.environ.get("USER", os.environ.get("USERNAME", "postgres"))

    # Count tables before
    try:
        result = subprocess.run(
            [psql_cmd, "-h", str(pgdata), "-p", "5433", "-U", current_user, "-d", "continuous_claude",
             "-t", "-c", "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public';"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        tables_before = int(result.stdout.strip()) if result.returncode == 0 else 0
    except Exception:
        tables_before = 0

    # Apply schema
    result = subprocess.run(
        [psql_cmd, "-h", str(pgdata), "-p", "5433", "-U", current_user, "-d", "continuous_claude",
         "-f", str(schema_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    warnings: list[str] = []
    if result.returncode != 0:
        stderr = result.stderr
        # pg_trgm failure is expected and ok
        if "pg_trgm" in stderr and "is not available" in stderr:
            warnings.append("Optional extension pg_trgm not available (ok)")
        else:
            return {"success": False, "error": stderr[:200]}

    # Check for pg_trgm warning even on success (it might be in stderr)
    if "pg_trgm" in result.stderr and "is not available" in result.stderr:
        if not warnings:  # Avoid duplicates
            warnings.append("Optional extension pg_trgm not available (ok)")

    # Count tables after
    try:
        result = subprocess.run(
            [psql_cmd, "-h", str(pgdata), "-p", "5433", "-U", current_user, "-d", "continuous_claude",
             "-t", "-c", "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public';"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        tables_after = int(result.stdout.strip()) if result.returncode == 0 else 0
    except Exception:
        tables_after = 0

    result_dict: dict[str, Any] = {
        "success": True,
        "tables_before": tables_before,
        "tables_after": tables_after,
    }
    if warnings:
        result_dict["warnings"] = warnings

    return result_dict
