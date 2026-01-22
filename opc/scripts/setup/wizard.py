#!/usr/bin/env python3
"""Setup Wizard for Claude2000.

Interactive setup wizard for configuring the Claude2000.
Handles prerequisite checking, database configuration, API keys,
and environment file generation.

USAGE:
    python -m scripts.setup.wizard

Or run as a standalone script:
    python scripts/setup/wizard.py
"""

import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path for imports when run as a script
# This handles both `python -m scripts.setup.wizard` and `python scripts/setup/wizard.py`
_this_file = Path(__file__).resolve()
_project_root = _this_file.parent.parent.parent  # scripts/setup/wizard.py -> opc/
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

try:
    from rich.console import Console
    from rich.markup import escape as rich_escape
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.status import Status

    # Force terminal mode to enable spinners even when run via `uv run`
    console = Console(force_terminal=True)
except ImportError:
    import re
    rich_escape = lambda x: x  # No escaping needed without Rich

    def _strip_rich_markup(text: str) -> str:
        """Strip Rich markup tags like [bold], [green], [/bold], etc."""
        if not isinstance(text, str):
            return str(text)
        return re.sub(r'\[/?[a-z_ ]+\]', '', text, flags=re.IGNORECASE)

    # Fallback for minimal environments
    from contextlib import contextmanager

    class Console:
        def print(self, *args, **kwargs):
            stripped = [_strip_rich_markup(str(a)) for a in args]
            print(*stripped)

        @contextmanager
        def status(self, message, spinner=None):
            """Fallback status context manager - just prints the message."""
            print(_strip_rich_markup(message))
            try:
                yield
            finally:
                pass

    class Panel:
        @staticmethod
        def fit(text, **kwargs):
            return _strip_rich_markup(text)

    class Confirm:
        @staticmethod
        def ask(prompt, default=False):
            prompt = _strip_rich_markup(prompt)
            response = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
            if not response:
                return default
            return response in ('y', 'yes')

    class Prompt:
        @staticmethod
        def ask(prompt, choices=None, default=None, password=False):
            prompt = _strip_rich_markup(prompt)
            if choices:
                prompt = f"{prompt} ({'/'.join(choices)})"
            if default:
                prompt = f"{prompt} [{default}]"
            response = input(f"{prompt}: ").strip()
            return response if response else default

    console = Console()


# Module-level restore function, set by run_setup_wizard after backup is created
_restore_on_failure = None


# =============================================================================
# Container Runtime Detection (Docker/Podman)
# =============================================================================

# Platform-specific Docker installation commands
DOCKER_INSTALL_COMMANDS = {
    "darwin": "brew install --cask docker",
    "linux": "sudo apt-get install docker.io docker-compose",
    "win32": "winget install Docker.DockerDesktop",
}


async def check_runtime_installed(runtime: str = "docker") -> dict[str, Any]:
    """Check if a container runtime (docker or podman) is installed.

    Args:
        runtime: The runtime to check ("docker" or "podman")

    Returns:
        dict with keys:
            - installed: bool - True if runtime binary exists
            - runtime: str - The runtime name that was checked
            - version: str | None - Version string if installed
            - daemon_running: bool - True if daemon/service is responding
    """
    result = {
        "installed": False,
        "runtime": runtime,
        "version": None,
        "daemon_running": False,
    }

    try:
        proc = await asyncio.create_subprocess_exec(
            runtime,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode == 0:
            result["installed"] = True
            # Parse version from output like "Docker version 24.0.5" or "podman version 4.5.0"
            version_output = stdout.decode().strip()
            if "version" in version_output.lower():
                parts = version_output.split()
                for i, part in enumerate(parts):
                    if part.lower() == "version":
                        if i + 1 < len(parts):
                            result["version"] = parts[i + 1].rstrip(",")
                            break
            result["daemon_running"] = True
        elif proc.returncode == 1:
            # Binary exists but daemon not running
            stderr_text = stderr.decode().lower()
            if "cannot connect" in stderr_text or "daemon" in stderr_text:
                result["installed"] = True
                result["daemon_running"] = False

    except FileNotFoundError:
        pass
    except Exception:
        pass

    return result


async def check_container_runtime() -> dict[str, Any]:
    """Check for Docker or Podman, preferring Docker if both exist.

    Returns:
        dict with keys:
            - installed: bool - True if any runtime is available
            - runtime: str - "docker", "podman", or None
            - version: str | None - Version string
            - daemon_running: bool - True if service is responding
    """
    # Try Docker first (most common)
    result = await check_runtime_installed("docker")
    if result["installed"]:
        return result

    # Fall back to Podman (common on Fedora/RHEL)
    result = await check_runtime_installed("podman")
    return result


# Keep old function name for backwards compatibility
async def check_docker_installed() -> dict[str, Any]:
    """Check if Docker is installed. Deprecated: use check_container_runtime()."""
    return await check_container_runtime()


def get_docker_install_command() -> str:
    """Get platform-specific Docker installation command.

    Returns:
        str: Installation command for the current platform
    """
    platform = sys.platform

    if platform in DOCKER_INSTALL_COMMANDS:
        return DOCKER_INSTALL_COMMANDS[platform]

    # Unknown platform - provide generic guidance
    return "Visit https://docker.com/get-started to download Docker for your platform"


async def offer_docker_install() -> bool:
    """Offer to show Docker/Podman installation instructions.

    Returns:
        bool: True if user wants to proceed without container runtime
    """
    install_cmd = get_docker_install_command()
    console.print("\n  [yellow]Docker or Podman is required but not installed.[/yellow]")
    console.print(f"  Install Docker with: [bold]{install_cmd}[/bold]")
    console.print("  [dim]Or on Fedora/RHEL: sudo dnf install podman podman-compose[/dim]")

    return Confirm.ask("\n  Would you like to proceed without a container runtime?", default=False)


async def check_prerequisites_with_install_offers() -> dict[str, Any]:
    """Check prerequisites and offer installation help for missing items.

    Enhanced version of check_prerequisites that offers installation
    guidance when tools are missing.

    Returns:
        dict with keys: docker, container_runtime, python, uv, elan, all_present
    """
    result = {
        "docker": False,
        "container_runtime": None,  # "docker" or "podman"
        "python": shutil.which("python3") is not None,
        "uv": shutil.which("uv") is not None,
    }

    # Docker/Podman is now optional (only needed if using docker database mode)
    # Skip the Docker check entirely since we default to embedded postgres
    result["docker"] = False
    result["container_runtime"] = None

    # Docker and elan are optional, so exclude from all_present check
    result["all_present"] = all([result["python"], result["uv"]])
    return result


# =============================================================================
# Security: Sandbox Risk Acknowledgment
# =============================================================================


def acknowledge_sandbox_risk() -> bool:
    """Get user acknowledgment for running without sandbox.

    Requires user to type an exact phrase to acknowledge the security
    implications of running agent-written code without sandbox protection.

    Returns:
        bool: True if user typed the correct acknowledgment phrase
    """
    print("\n  SECURITY WARNING")
    print("  Running without sandbox means agent-written code executes with full system access.")
    print("  This is a security risk. Only proceed if you understand the implications.")
    response = input("\n  Type 'I understand the risks' to continue without sandbox: ")
    return response.strip().lower() == "i understand the risks"


# =============================================================================
# Feature Toggle Confirmation
# =============================================================================


def confirm_feature_toggle(feature: str, current: bool, new: bool) -> bool:
    """Confirm feature toggle change with user.

    Asks for explicit confirmation before changing a feature's enabled state.

    Args:
        feature: Name of the feature being toggled
        current: Current enabled state
        new: New enabled state being requested

    Returns:
        bool: True if user confirms the change
    """
    action = "enable" if new else "disable"
    response = input(f"  Are you sure you want to {action} {feature}? [y/N]: ")
    return response.strip().lower() == "y"


def build_typescript_hooks(hooks_dir: Path) -> tuple[bool, str]:
    """Build TypeScript hooks using npm.

    Args:
        hooks_dir: Path to hooks directory

    Returns:
        Tuple of (success, message)
    """
    # Check if hooks directory exists
    if not hooks_dir.exists():
        return True, "Hooks directory does not exist"

    # Check if package.json exists
    if not (hooks_dir / "package.json").exists():
        return True, "No package.json found - no npm build needed"

    # Find npm executable
    npm_cmd = shutil.which("npm")
    if npm_cmd is None:
        if platform.system() == "Windows":
            npm_cmd = shutil.which("npm.cmd")
        if npm_cmd is None:
            return False, "npm not found in PATH - TypeScript hooks will not be built"

    try:
        # Install dependencies
        console.print("  Running npm install...")
        result = subprocess.run(
            [npm_cmd, "install"],
            cwd=hooks_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            return False, f"npm install failed: {result.stderr[:200]}"

        # Build
        console.print("  Running npm run build...")
        result = subprocess.run(
            [npm_cmd, "run", "build"],
            cwd=hooks_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return False, f"npm build failed: {result.stderr[:200]}"

        return True, "TypeScript hooks built successfully"

    except subprocess.TimeoutExpired:
        return False, "npm command timed out"
    except OSError as e:
        return False, f"Failed to run npm: {e}"


async def check_prerequisites() -> dict[str, Any]:
    """Check if required tools are installed.

    Checks for:
    - Docker (optional, for docker database mode)
    - Python 3.11+ (already running if here)
    - uv package manager (required for deps)

    Returns:
        dict with keys: docker, python, uv, all_present
    """
    result = {
        "docker": shutil.which("docker") is not None,
        "python": shutil.which("python3") is not None,
        "uv": shutil.which("uv") is not None,
    }
    # docker is optional, so exclude from all_present check
    result["all_present"] = all([result["python"], result["uv"]])
    return result


async def prompt_database_config() -> dict[str, Any]:
    """Prompt user for database configuration.

    Returns:
        dict with keys: host, port, database, user
    """
    host = Prompt.ask("PostgreSQL host", default="localhost")
    port_str = Prompt.ask("PostgreSQL port", default="5432")
    database = Prompt.ask("Database name", default="continuous_claude")
    user = Prompt.ask("Database user", default="claude")

    return {
        "host": host,
        "port": int(port_str),
        "database": database,
        "user": user,
    }


async def prompt_embedding_config() -> dict[str, str]:
    """Prompt user for embedding provider configuration.

    Returns:
        dict with keys: provider, host (if ollama), model (if ollama)
    """
    console.print("  [dim]Embeddings power semantic search for learnings recall.[/dim]")
    console.print("  Options:")
    console.print("    1. local - sentence-transformers (downloads ~1.3GB model)")
    console.print("    2. ollama - Use Ollama server (fast, recommended if you have Ollama)")

    provider = Prompt.ask("Embedding provider", choices=["local", "ollama"], default="local")

    config = {"provider": provider}

    if provider == "ollama":
        host = Prompt.ask("Ollama host URL", default="http://localhost:11434")
        model = Prompt.ask("Ollama embedding model", default="nomic-embed-text")
        config["host"] = host
        config["model"] = model

    return config


def generate_env_file(config: dict[str, Any], env_path: Path) -> None:
    """Generate .env file from configuration.

    If env_path exists, creates a backup before overwriting.

    Args:
        config: Configuration dict with 'database' and 'embeddings' sections
        env_path: Path to write .env file
    """
    # Backup existing .env if present
    if env_path.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = env_path.parent / f".env.backup.{timestamp}"
        shutil.copy(env_path, backup_path)

    # Build env content
    lines = []

    # Database config
    db = config.get("database", {})
    if db:
        mode = db.get("mode", "docker")
        lines.append(f"# Database Mode: {mode}")

        if mode == "docker":
            host = db.get('host', 'localhost')
            port = db.get('port', 5432)
            database = db.get('database', 'continuous_claude')
            user = db.get('user', 'claude')
            password = db.get('password', '')
            lines.append(f"POSTGRES_HOST={host}")
            lines.append(f"POSTGRES_PORT={port}")
            lines.append(f"POSTGRES_DB={database}")
            lines.append(f"POSTGRES_USER={user}")
            if password:
                lines.append(f"POSTGRES_PASSWORD={password}")
            lines.append("")
            lines.append("# Connection string for scripts (canonical name)")
            lines.append(f"CLAUDE2000_DB_URL=postgresql://{user}:{password}@{host}:{port}/{database}")
        elif mode == "embedded":
            pgdata = db.get("pgdata", "")
            venv = db.get("venv", "")
            uri = db.get("uri", "")
            lines.append(f"PGSERVER_PGDATA={pgdata}")
            lines.append(f"PGSERVER_VENV={venv}")
            lines.append("")
            lines.append("# Connection string (Unix socket)")
            # Use provided URI if available, otherwise construct from pgdata
            if uri:
                lines.append(f"CLAUDE2000_DB_URL={uri}")
            else:
                # Fallback - will be updated after initialization (postgres user for portability)
                lines.append(f"CLAUDE2000_DB_URL=postgresql://postgres:@/continuous_claude?host={pgdata}")
        else:  # sqlite
            lines.append("# SQLite mode - no connection string needed")
            lines.append("CLAUDE2000_DB_URL=")
        lines.append("")

    # Embedding configuration
    embeddings = config.get("embeddings", {})
    if embeddings:
        provider = embeddings.get("provider", "local")
        lines.append("# Embedding provider (local, ollama)")
        lines.append(f"EMBEDDING_PROVIDER={provider}")
        if provider == "ollama":
            ollama_host = embeddings.get("host", "http://localhost:11434")
            ollama_model = embeddings.get("model", "nomic-embed-text")
            lines.append(f"OLLAMA_HOST={ollama_host}")
            lines.append(f"OLLAMA_EMBED_MODEL={ollama_model}")
        lines.append("")

    # Write file
    env_path.write_text("\n".join(lines))


async def run_setup_wizard() -> None:
    """Run the setup wizard with sensible defaults.

    Minimal prompts:
    1. Backup existing? (if exists)
    2. Installation mode? (copy/symlink)
    3. Reindex frequency? (for semantic search)

    Everything else uses defaults:
    - Embedded PostgreSQL (port 5433)
    - Local embeddings (Qwen3-Embedding-0.6B)
    - TLDR code analysis tool
    - Semantic search enabled
    """
    console.print("\n[bold]                              Setup Wizard[/bold]\n")

    from scripts.setup.claude_integration import (
        backup_global_claude_dir,
        get_global_claude_dir,
        get_opc_integration_source,
        install_opc_integration,
        install_opc_integration_symlink,
        detect_existing_setup,
    )

    global_claude = get_global_claude_dir()

    # === PROMPT 1: Fresh install or update? (if existing) ===
    install_type = "fresh"
    should_backup = False
    if global_claude.exists():
        console.print("\n[bold]Existing ~/.claude found[/bold]")
        install_type = Prompt.ask("Install type", choices=["update", "fresh"], default="update")
        if install_type == "fresh":
            console.print("  [dim]Fresh install will replace existing configuration[/dim]")
        should_backup = Confirm.ask("Create backup before proceeding?", default=True)
    else:
        console.print("\n[dim]No existing ~/.claude found (clean install)[/dim]")

    # === PROMPT 2: Installation mode? ===
    console.print("\n[bold]Installation Mode[/bold]")
    console.print("  1. [bold]copy[/bold]    - Copy files to ~/.claude/ (default)")
    console.print("  2. [bold]symlink[/bold] - Link to repo (best for contributors)")
    install_mode = Prompt.ask("Mode", choices=["copy", "symlink"], default="copy")

    # === PROMPT 3: Reindex frequency? ===
    console.print("\n[bold]Semantic Search[/bold]")
    console.print("  Auto-reindex after how many file changes?")
    threshold_str = Prompt.ask("Threshold", default="20")
    try:
        reindex_threshold = int(threshold_str)
    except ValueError:
        reindex_threshold = 20

    # === BACKUP: Create backup BEFORE any setup operations ===
    backup_path: Path | None = None
    if should_backup:
        console.print("\n[bold]Creating backup...[/bold]")
        backup_path = backup_global_claude_dir()
        if backup_path:
            console.print(f"  [green]OK[/green] Backed up to {backup_path.name}")
        else:
            console.print("  [yellow]WARN[/yellow] Could not create backup")

    # Define restore function early so it's available for all failure paths
    def restore_on_failure(error_msg: str) -> None:
        """On hard failure, rename .claude-failed-{date} and restore backup."""
        from datetime import datetime
        console.print(f"\n[red]FATAL ERROR:[/red] {error_msg}")

        if global_claude.exists():
            failed_name = f".claude-failed-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            failed_path = global_claude.parent / failed_name
            try:
                global_claude.rename(failed_path)
                console.print(f"  [yellow]Renamed failed installation to {failed_name}[/yellow]")
            except Exception as e:
                console.print(f"  [red]Could not rename failed installation: {e}[/red]")

        if backup_path and backup_path.exists():
            try:
                import shutil
                shutil.copytree(backup_path, global_claude)
                console.print(f"  [green]Restored backup from {backup_path.name}[/green]")
            except Exception as e:
                console.print(f"  [red]Could not restore backup: {e}[/red]")
                console.print(f"  [dim]Manual restore: cp -r {backup_path} {global_claude}[/dim]")

    # Store restore function in module scope so main() can access it on exception
    global _restore_on_failure
    _restore_on_failure = restore_on_failure

    # === FRESH INSTALL: Wipe ~/.claude entirely ===
    if install_type == "fresh" and global_claude.exists():
        console.print("\n[bold]Wiping existing ~/.claude for fresh install...[/bold]")

        # Kill all Claude Code processes first (they hold files open in ~/.claude)
        import subprocess
        try:
            # Find and kill all 'claude' processes
            result = subprocess.run(
                ["pgrep", "-x", "claude"],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                pids = result.stdout.strip().split('\n')
                console.print(f"  Killing {len(pids)} Claude process(es)...")
                for pid in pids:
                    if pid:
                        subprocess.run(["kill", "-9", pid], capture_output=True)
                console.print("  [green]OK[/green] Claude processes terminated")
                # Brief pause to let file handles release
                import time
                time.sleep(0.5)
        except Exception as e:
            console.print(f"  [dim]Could not kill Claude processes: {e}[/dim]")

        # Stop postgres if running
        pgdata = global_claude / "pgdata"
        pg_venv = global_claude / "pgserver-venv"
        if pgdata.exists() and pg_venv.exists():
            pg_ctl = pg_venv / "bin" / "pg_ctl"
            if pg_ctl.exists():
                console.print("  Stopping PostgreSQL...")
                import subprocess
                try:
                    subprocess.run(
                        [str(pg_ctl), "stop", "-D", str(pgdata), "-m", "fast"],
                        capture_output=True,
                        timeout=10
                    )
                    console.print("  [green]OK[/green] PostgreSQL stopped")
                except Exception as e:
                    console.print(f"  [dim]PostgreSQL stop: {e}[/dim]")

        # Remove ~/.claude entirely
        import shutil
        try:
            shutil.rmtree(global_claude)
            console.print("  [green]OK[/green] Removed ~/.claude")
        except Exception as e:
            restore_on_failure(f"Could not remove ~/.claude: {e}")
            sys.exit(1)

    # === AUTO: Check prerequisites ===
    console.print("\n[bold]Checking prerequisites...[/bold]")
    prereqs = await check_prerequisites_with_install_offers()

    if prereqs["python"]:
        console.print("  [green]OK[/green] Python 3.11+")
    else:
        console.print("  [red]MISSING[/red] Python 3.11+")

    if prereqs["uv"]:
        console.print("  [green]OK[/green] uv package manager")
    else:
        console.print(
            "  [red]MISSING[/red] uv - install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
        )

    if not prereqs["all_present"]:
        console.print("\n[red]Cannot continue without all prerequisites.[/red]")
        sys.exit(1)

    # === AUTO: Database (embedded postgres) ===
    console.print("\n[bold]Setting up embedded PostgreSQL...[/bold]")
    from scripts.setup.embedded_postgres import setup_embedded_environment
    with console.status("[cyan]Setting up PostgreSQL environment...[/cyan]", spinner="dots"):
        embed_result = await setup_embedded_environment()
    if embed_result["success"]:
        console.print(f"  [green]OK[/green] Environment ready at {embed_result['venv']}")
        db_config = {"mode": "embedded", "pgdata": str(embed_result["pgdata"]), "venv": str(embed_result["venv"])}
    else:
        restore_on_failure(f"PostgreSQL setup failed: {embed_result.get('error', 'Unknown')}")
        sys.exit(1)

    # === AUTO: Embeddings (local) ===
    embeddings = {"provider": "local"}

    # === AUTO: Generate .env ===
    console.print("\n[bold]Generating configuration...[/bold]")
    config = {"database": db_config, "embeddings": embeddings}
    env_path = Path.cwd() / ".env"
    generate_env_file(config, env_path)
    console.print(f"  [green]OK[/green] Generated {env_path}")

    # === AUTO: Database Setup ===
    console.print("\n[bold]Initializing database...[/bold]")
    try:
        from scripts.setup.embedded_postgres import initialize_embedded_postgres

        pgdata = Path(db_config.get("pgdata", ""))
        venv = Path(db_config.get("venv", ""))
        # Path: wizard.py -> setup/ -> scripts/ -> opc/ -> Claude2000/schema/
        schema_path = Path(__file__).parent.parent.parent.parent / "schema" / "init-schema.sql"

        with console.status("[cyan]Starting PostgreSQL and applying schema...[/cyan]", spinner="dots"):
            result = await initialize_embedded_postgres(pgdata, venv, schema_path)
        if result["success"]:
            console.print("  [green]OK[/green] Embedded PostgreSQL initialized")
            if result.get("warnings"):
                for warn in result["warnings"]:
                    console.print(f"  [dim]Note: {warn}[/dim]")

            # Verify schema was applied
            from scripts.setup.embedded_postgres import apply_schema_if_needed
            verify = apply_schema_if_needed(pgdata, schema_path)
            if verify["success"]:
                tables = verify.get("tables_after", 0)
                console.print(f"  [green]OK[/green] Schema verified ({tables} tables)")
            else:
                console.print(f"  [yellow]WARN[/yellow] Schema verification: {verify.get('error', 'unknown')}")

            # Update db_config with the actual URI and regenerate .env
            db_config["uri"] = result.get("uri", "")
            config["database"] = db_config
            generate_env_file(config, env_path)
            console.print(f"  [green]OK[/green] Updated {env_path} with correct connection URI")

            # Also update ~/.claude/settings.json with env var for hooks
            # Hooks read CLAUDE2000_DB_URL from settings.json env section
            settings_path = Path.home() / ".claude" / "settings.json"
            try:
                settings = {}
                if settings_path.exists():
                    settings = json.loads(settings_path.read_text())

                # Add/update env section with database URL
                if "env" not in settings:
                    settings["env"] = {}
                settings["env"]["CLAUDE2000_DB_URL"] = result.get("uri", "")

                settings_path.parent.mkdir(parents=True, exist_ok=True)
                settings_path.write_text(json.dumps(settings, indent=2))
                console.print(f"  [green]OK[/green] Updated ~/.claude/settings.json with database URL for hooks")
            except Exception as e:
                console.print(f"  [yellow]WARN[/yellow] Could not update settings.json: {e}")
                console.print(f"  [dim]Hooks may not connect to embedded postgres. Add manually:[/dim]")
                console.print(f'  [dim]"env": {{"CLAUDE2000_DB_URL": "{result.get("uri", "")}"}}"[/dim]')
        else:
            restore_on_failure(f"PostgreSQL init failed: {result.get('error', 'Unknown error')}")
            sys.exit(1)
    except Exception as e:
        restore_on_failure(f"PostgreSQL init exception: {e}")
        sys.exit(1)

    # === AUTO: Claude Code Integration (using install_mode from prompt) ===
    console.print("\n[bold]Installing Claude Code integration...[/bold]")
    claude_dir = global_claude
    opc_source = get_opc_integration_source()

    if install_mode == "symlink":
        with console.status("[cyan]Creating symlinks...[/cyan]", spinner="dots"):
            result = install_opc_integration_symlink(claude_dir, opc_source)
        if result["success"]:
            console.print(f"  [green]OK[/green] Symlinked: {', '.join(result['symlinked_dirs'])}")
        else:
            console.print(f"  [red]ERROR[/red] {result.get('error', 'Unknown error')}")
    else:  # copy mode
        with console.status("[cyan]Copying hooks, skills, rules, agents...[/cyan]", spinner="dots"):
            result = install_opc_integration(claude_dir, opc_source)
        if result["success"]:
            console.print(f"  [green]OK[/green] Installed {result['installed_hooks']} hooks")
            console.print(f"  [green]OK[/green] Installed {result['installed_skills']} skills")
            console.print(f"  [green]OK[/green] Installed {result['installed_rules']} rules")
            console.print(f"  [green]OK[/green] Installed {result['installed_agents']} agents")
        else:
            console.print(f"  [red]ERROR[/red] {result.get('error', 'Unknown error')}")

    # Build TypeScript hooks
    hooks_dir = claude_dir / "hooks"
    with console.status("[cyan]Building TypeScript hooks...[/cyan]", spinner="dots"):
        build_success, build_msg = build_typescript_hooks(hooks_dir)
    if build_success:
        console.print(f"  [green]OK[/green] {build_msg}")
    else:
        console.print(f"  [yellow]WARN[/yellow] {build_msg}")

    # === AUTO: TLDR Code Analysis Tool ===
    console.print("\n[bold]Installing TLDR code analysis...[/bold]")
    import subprocess

    try:
        with console.status("[cyan]Installing TLDR via uv...[/cyan]", spinner="dots"):
            result = subprocess.run(
                ["uv", "tool", "install", "llm-tldr"],
                capture_output=True,
                text=True,
                timeout=300,
            )
        if result.returncode == 0:
            console.print("  [green]OK[/green] TLDR installed")
        else:
            console.print(f"  [yellow]WARN[/yellow] TLDR install failed: {result.stderr[:100]}")
    except Exception as e:
        console.print(f"  [yellow]WARN[/yellow] TLDR install failed: {e}")

    # === AUTO: Semantic search config (using reindex_threshold from prompt) ===
    console.print("\n[bold]Configuring semantic search...[/bold]")
    settings_path = global_claude / "settings.json"
    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except Exception:
            pass

    # Detect GPU for model selection
    has_gpu = False
    try:
        import torch
        has_gpu = torch.cuda.is_available() or torch.backends.mps.is_available()
    except ImportError:
        pass  # No torch = assume no GPU

    if has_gpu:
        model = "bge-large-en-v1.5"
    else:
        model = "all-MiniLM-L6-v2"
        console.print("  [dim]No GPU detected, using lightweight model[/dim]")

    settings["semantic_search"] = {
        "enabled": True,
        "auto_reindex_threshold": reindex_threshold,
        "model": model,
    }

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2))
    console.print(f"  [green]OK[/green] Semantic search enabled (threshold: {reindex_threshold})")

    # === AUTO: Install scripts to ~/.claude/claude2000/ ===
    console.print("\n[bold]Installing Claude2000 scripts...[/bold]")
    install_dir = Path.home() / ".claude" / "claude2000"
    scripts_source = Path(__file__).parent.parent  # opc/scripts/
    scripts_dest = install_dir / "scripts"
    src_source = Path(__file__).parent.parent.parent / "src" / "runtime"
    src_dest = install_dir / "src" / "runtime"

    try:
        # Copy scripts directory
        install_dir.mkdir(parents=True, exist_ok=True)
        if scripts_dest.exists():
            shutil.rmtree(scripts_dest)
        shutil.copytree(scripts_source, scripts_dest)
        console.print(f"  [green]OK[/green] Copied scripts to {scripts_dest}")

        # Copy src/runtime if it exists
        if src_source.exists():
            src_dest.parent.mkdir(parents=True, exist_ok=True)
            if src_dest.exists():
                shutil.rmtree(src_dest)
            shutil.copytree(src_source, src_dest)
            console.print(f"  [green]OK[/green] Copied runtime to {src_dest}")

        # Copy .env to install directory for scripts that need it
        env_source = Path.cwd() / ".env"
        if env_source.exists():
            shutil.copy(env_source, install_dir / ".env")
            console.print(f"  [green]OK[/green] Copied .env configuration")

        # Create Python virtual environment with required dependencies
        venv_path = install_dir / ".venv"
        try:
            import subprocess
            # Create venv using uv
            with console.status("[cyan]Creating Python virtual environment...[/cyan]", spinner="dots"):
                result = subprocess.run(
                    ["uv", "venv", str(venv_path)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            if result.returncode == 0:
                console.print(f"  [green]OK[/green] Created venv at {venv_path}")

                # Install minimal dependencies for memory scripts
                deps = [
                    "python-dotenv",
                    "asyncpg",
                    "numpy",
                    "sentence-transformers",
                    "httpx",  # For HTTP requests in embedding service
                ]
                with console.status("[cyan]Installing dependencies (this may take a minute)...[/cyan]", spinner="dots"):
                    pip_result = subprocess.run(
                        ["uv", "pip", "install", "--python", str(venv_path / "bin" / "python")] + deps,
                        capture_output=True,
                        text=True,
                        timeout=300,  # 5 minutes for sentence-transformers
                    )
                if pip_result.returncode == 0:
                    console.print(f"  [green]OK[/green] Installed dependencies")
                else:
                    console.print(f"  [yellow]WARN[/yellow] Some dependencies failed: {pip_result.stderr[:200]}")
            else:
                console.print(f"  [yellow]WARN[/yellow] Could not create venv: {result.stderr[:200]}")
        except subprocess.TimeoutExpired:
            console.print("  [yellow]WARN[/yellow] Venv creation timed out")
        except Exception as e:
            console.print(f"  [yellow]WARN[/yellow] Venv creation failed: {e}")

        # Self-contained installation - no env vars needed
        # All scripts run from ~/.claude/ directory
        console.print(f"  Installed to: {install_dir}")
    except Exception as e:
        console.print(f"  [yellow]WARN[/yellow] Could not install scripts: {e}")
        console.print(f"  Scripts will run from repo location instead")

    # Done!
    console.print("\n" + "=" * 60)
    console.print("[bold green]Setup complete![/bold green]")
    console.print("\nNext steps:")
    console.print("  1. [bold]Restart your terminal[/bold] (to load CLAUDE_2000_DIR)")
    console.print("  2. Start Claude Code: [bold]claude[/bold]")
    console.print("")
    console.print("[dim]Note: TLDR daemon starts automatically when Claude Code runs.[/dim]")
    console.print("[dim]View docs: docs/QUICKSTART.md[/dim]")


async def run_uninstall_wizard() -> None:
    """Run the uninstall wizard to remove OPC and restore backup."""
    from scripts.setup.claude_integration import (
        find_latest_backup,
        get_global_claude_dir,
        uninstall_opc_integration,
        PRESERVE_FILES,
        PRESERVE_DIRS,
    )

    console.print(
        Panel.fit("[bold]CLAUDE2000 - UNINSTALL[/bold]", border_style="red")
    )

    global_claude = get_global_claude_dir()
    backup = find_latest_backup(global_claude) if global_claude.exists() else None

    console.print("\n[bold]Current state:[/bold]")
    if global_claude.exists():
        console.print(f"  ~/.claude exists at: {global_claude}")
    else:
        console.print("  [dim]No ~/.claude found[/dim]")

    if backup:
        console.print(f"  Backup available: {backup.name}")
    else:
        console.print("  [yellow]No backup found[/yellow] - uninstall will be clean (no restore)")

    # Show what user data will be preserved
    existing_preserve = []
    if global_claude.exists():
        for f in PRESERVE_FILES:
            if (global_claude / f).exists():
                existing_preserve.append(f)
        for d in PRESERVE_DIRS:
            if (global_claude / d).exists():
                existing_preserve.append(f"{d}/")

    console.print("\n[bold]This will:[/bold]")
    console.print("  1. Move current ~/.claude to ~/.claude-v3.archived.<timestamp>")
    if backup:
        console.print(f"  2. Restore from {backup.name}")
    else:
        console.print("  2. Create empty ~/.claude")
    if existing_preserve:
        console.print(f"  3. [green]Preserve your data:[/green] {', '.join(existing_preserve)}")

    if not Confirm.ask("\nProceed with uninstall?", default=False):
        console.print("[yellow]Uninstall cancelled.[/yellow]")
        return

    result = uninstall_opc_integration(is_global=True)

    if result["success"]:
        console.print(f"\n[green]SUCCESS[/green]\n{result['message']}")
    else:
        console.print(f"\n[red]FAILED[/red]\n{result['message']}")


async def main():
    """Entry point for the setup wizard."""
    # Check for --uninstall flag
    if len(sys.argv) > 1 and sys.argv[1] in ("--uninstall", "-u", "uninstall"):
        try:
            await run_uninstall_wizard()
        except KeyboardInterrupt:
            console.print("\n\n[yellow]Uninstall cancelled.[/yellow]")
            sys.exit(130)
        return

    # Show menu if no args
    if len(sys.argv) == 1:
        ascii_banner = r"""
 ██████╗██╗      █████╗ ██╗   ██╗██████╗ ███████╗██████╗  ██████╗  ██████╗  ██████╗
██╔════╝██║     ██╔══██╗██║   ██║██╔══██╗██╔════╝╚════██╗██╔═████╗██╔═████╗██╔═████╗
██║     ██║     ███████║██║   ██║██║  ██║█████╗   █████╔╝██║██╔██║██║██╔██║██║██╔██║
██║     ██║     ██╔══██║██║   ██║██║  ██║██╔══╝  ██╔═══╝ ████╔╝██║████╔╝██║████╔╝██║
╚██████╗███████╗██║  ██║╚██████╔╝██████╔╝███████╗███████╗╚██████╔╝╚██████╔╝╚██████╔╝
 ╚═════╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝╚══════╝ ╚═════╝  ╚═════╝  ╚═════╝
        """
        console.print(f"[bold cyan]{ascii_banner}[/bold cyan]")
        console.print("[bold]Options:[/bold]")
        console.print("  [bold]1[/bold] - Install / Update")
        console.print("  [bold]2[/bold] - Uninstall (restore backup)")
        console.print("  [bold]q[/bold] - Quit")

        choice = Prompt.ask("\nChoice", choices=["1", "2", "q"], default="1")

        if choice == "q":
            console.print("[dim]Goodbye![/dim]")
            return
        elif choice == "2":
            await run_uninstall_wizard()
            return
        # choice == "1" falls through to install

    try:
        await run_setup_wizard()
    except KeyboardInterrupt:
        console.print("\n\n[yellow]Setup cancelled.[/yellow]")
        if _restore_on_failure:
            _restore_on_failure("Setup cancelled by user")
        sys.exit(130)
    except Exception as e:
        console.print(f"\n[red]Error: {rich_escape(str(e))}[/red]")
        if _restore_on_failure:
            _restore_on_failure(str(e))
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
