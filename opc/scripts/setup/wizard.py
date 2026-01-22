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

    console = Console()
except ImportError:
    import re
    rich_escape = lambda x: x  # No escaping needed without Rich

    def _strip_rich_markup(text: str) -> str:
        """Strip Rich markup tags like [bold], [green], [/bold], etc."""
        if not isinstance(text, str):
            return str(text)
        return re.sub(r'\[/?[a-z_ ]+\]', '', text, flags=re.IGNORECASE)

    # Fallback for minimal environments
    class Console:
        def print(self, *args, **kwargs):
            stripped = [_strip_rich_markup(str(a)) for a in args]
            print(*stripped)

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
            lines.append(f"CONTINUOUS_CLAUDE_DB_URL=postgresql://{user}:{password}@{host}:{port}/{database}")
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
                lines.append(f"CONTINUOUS_CLAUDE_DB_URL={uri}")
            else:
                # Fallback - will be updated after initialization (postgres user for portability)
                lines.append(f"CONTINUOUS_CLAUDE_DB_URL=postgresql://postgres:@/continuous_claude?host={pgdata}")
        else:  # sqlite
            lines.append("# SQLite mode - no connection string needed")
            lines.append("CONTINUOUS_CLAUDE_DB_URL=")
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
    """Run the interactive setup wizard.

    Orchestrates the full setup flow:
    1. Check prerequisites
    2. Prompt for database config
    3. Prompt for API keys
    4. Generate .env file
    5. Start Docker stack
    6. Run migrations
    7. Install Claude Code integration (hooks, skills, rules)
    """
    console.print(
        Panel.fit("[bold]CLAUDE2000 - SETUP WIZARD[/bold]", border_style="blue")
    )

    # Step 0: Backup global ~/.claude (safety first)
    console.print("\n[bold]Step 0/9: Backing up global Claude configuration...[/bold]")
    from scripts.setup.claude_integration import (
        backup_global_claude_dir,
        get_global_claude_dir,
    )

    global_claude = get_global_claude_dir()
    if global_claude.exists():
        backup_path = backup_global_claude_dir()
        if backup_path:
            console.print(f"  [green]OK[/green] Backed up ~/.claude to {backup_path.name}")
        else:
            console.print("  [yellow]WARN[/yellow] Could not create backup")
    else:
        console.print("  [dim]No existing ~/.claude found (clean install)[/dim]")

    # Step 1: Check prerequisites (with installation offers)
    console.print("\n[bold]Step 1/9: Checking system requirements...[/bold]")
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

    # Step 2: Database config
    console.print("\n[bold]Step 2/9: Database Configuration[/bold]")
    console.print("  Choose your database backend:")
    console.print("    [bold]embedded[/bold]  - Embedded PostgreSQL (recommended)")
    console.print("    [bold]sqlite[/bold]    - SQLite fallback (simplest, limited features)")
    db_mode = Prompt.ask("\n  Database mode", choices=["embedded", "sqlite"], default="embedded")

    if db_mode == "embedded":
        from scripts.setup.embedded_postgres import setup_embedded_environment
        console.print("  Setting up embedded postgres (creates Python 3.12 environment)...")
        embed_result = await setup_embedded_environment()
        if embed_result["success"]:
            console.print(f"  [green]OK[/green] Embedded environment ready at {embed_result['venv']}")
            db_config = {"mode": "embedded", "pgdata": str(embed_result["pgdata"]), "venv": str(embed_result["venv"])}
        else:
            console.print(f"  [red]ERROR[/red] {embed_result.get('error', 'Unknown')}")
            console.print("  Falling back to SQLite mode")
            db_mode = "sqlite"
            db_config = {"mode": "sqlite"}

    if db_mode == "sqlite":
        db_config = {"mode": "sqlite"}
        console.print("  [yellow]Note:[/yellow] Cross-terminal coordination disabled in SQLite mode")

    if False:  # Docker option removed
        console.print("  [dim]Customize host/port for containers (podman, nerdctl) or remote postgres.[/dim]")
        if Confirm.ask("Configure database connection?", default=True):
            db_config = await prompt_database_config()
            password = Prompt.ask("Database password", password=True, default="claude_dev")
            db_config["password"] = password
        else:
            db_config = {
                "host": "localhost",
                "port": 5432,
                "database": "continuous_claude",
                "user": "claude",
                "password": "claude_dev",
            }
        db_config["mode"] = "docker"

    # Step 3: Embedding configuration
    console.print("\n[bold]Step 3/9: Embedding Configuration[/bold]")
    if Confirm.ask("Configure embedding provider?", default=True):
        embeddings = await prompt_embedding_config()
    else:
        embeddings = {"provider": "local"}

    # Step 4: Generate .env
    console.print("\n[bold]Step 4/9: Generating configuration...[/bold]")
    config = {"database": db_config, "embeddings": embeddings}
    env_path = Path.cwd() / ".env"
    generate_env_file(config, env_path)
    console.print(f"  [green]OK[/green] Generated {env_path}")

    # Step 6: Database Setup (initialize and migrate)
    console.print("\n[bold]Step 5/9: Database Setup[/bold]")
    if db_mode == "embedded":
        console.print("  Initializing embedded PostgreSQL (this may take a moment)...")
        try:
            from scripts.setup.embedded_postgres import initialize_embedded_postgres

            pgdata = Path(db_config.get("pgdata", ""))
            venv = Path(db_config.get("venv", ""))
            # Path: wizard.py -> setup/ -> scripts/ -> opc/ -> Claude2000/docker/
            schema_path = Path(__file__).parent.parent.parent.parent / "docker" / "init-schema.sql"

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
            else:
                console.print(f"  [red]ERROR[/red] {result.get('error', 'Unknown error')}")
                console.print("  Falling back to SQLite mode")
                db_mode = "sqlite"
                db_config = {"mode": "sqlite"}
        except Exception as e:
            console.print(f"  [red]ERROR[/red] Could not initialize embedded postgres: {e}")
            console.print("  Falling back to SQLite mode")
            db_mode = "sqlite"
            db_config = {"mode": "sqlite"}
    elif db_mode == "sqlite":
        console.print("  [dim]SQLite mode - no migrations needed[/dim]")

    # Step 7: Claude Code Integration
    console.print("\n[bold]Step 6/9: Claude Code Integration[/bold]")
    from scripts.setup.claude_integration import (
        analyze_conflicts,
        backup_claude_dir,
        detect_existing_setup,
        generate_migration_guidance,
        get_global_claude_dir,
        get_opc_integration_source,
        install_opc_integration,
        install_opc_integration_symlink,
    )

    claude_dir = get_global_claude_dir()  # Use global ~/.claude, not project-local
    existing = detect_existing_setup(claude_dir)

    if existing.has_existing:
        console.print("  Found existing configuration:")
        console.print(f"    - Hooks: {len(existing.hooks)}")
        console.print(f"    - Skills: {len(existing.skills)}")
        console.print(f"    - Rules: {len(existing.rules)}")
        console.print(f"    - MCPs: {len(existing.mcps)}")

        opc_source = get_opc_integration_source()
        conflicts = analyze_conflicts(existing, opc_source)

        if conflicts.has_conflicts:
            console.print("\n  [yellow]Conflicts detected:[/yellow]")
            if conflicts.hook_conflicts:
                console.print(f"    - Hook conflicts: {', '.join(conflicts.hook_conflicts)}")
            if conflicts.skill_conflicts:
                console.print(f"    - Skill conflicts: {', '.join(conflicts.skill_conflicts)}")
            if conflicts.mcp_conflicts:
                console.print(f"    - MCP conflicts: {', '.join(conflicts.mcp_conflicts)}")

        # Show migration guidance
        guidance = generate_migration_guidance(existing, conflicts)
        console.print(f"\n{guidance}")

        # Offer choices
        console.print("\n[bold]Installation Options:[/bold]")
        console.print("  1. Full install (backup existing, copy OPC, merge non-conflicting)")
        console.print("  2. Fresh install (backup existing, copy OPC only)")
        console.print("  3. [cyan]Symlink install[/cyan] (link to repo - best for contributors)")
        console.print("  4. Skip (keep existing configuration)")
        console.print("")
        console.print("  [dim]Symlink mode links rules/skills/hooks/agents to the repo.[/dim]")
        console.print("  [dim]Changes sync automatically; great for contributing back.[/dim]")

        choice = Prompt.ask("Choose option", choices=["1", "2", "3", "4"], default="1")

        if choice in ("1", "2"):
            # Backup first
            backup_path = backup_claude_dir(claude_dir)
            if backup_path:
                console.print(f"  [green]OK[/green] Backup created: {backup_path.name}")

            # Install (copy mode)
            merge = choice == "1"
            result = install_opc_integration(
                claude_dir,
                opc_source,
                merge_user_items=merge,
                existing=existing if merge else None,
                conflicts=conflicts if merge else None,
            )

            if result["success"]:
                console.print(f"  [green]OK[/green] Installed {result['installed_hooks']} hooks")
                console.print(f"  [green]OK[/green] Installed {result['installed_skills']} skills")
                console.print(f"  [green]OK[/green] Installed {result['installed_rules']} rules")
                console.print(f"  [green]OK[/green] Installed {result['installed_agents']} agents")
                console.print(f"  [green]OK[/green] Installed {result['installed_servers']} MCP servers")
                if result["merged_items"]:
                    console.print(
                        f"  [green]OK[/green] Merged {len(result['merged_items'])} custom items"
                    )

                # Build TypeScript hooks
                console.print("  Building TypeScript hooks...")
                hooks_dir = claude_dir / "hooks"
                build_success, build_msg = build_typescript_hooks(hooks_dir)
                if build_success:
                    console.print(f"  [green]OK[/green] {build_msg}")
                else:
                    console.print(f"  [yellow]WARN[/yellow] {build_msg}")
                    console.print("  [dim]You can build manually: cd ~/.claude/hooks && npm install && npm run build[/dim]")
            else:
                console.print(f"  [red]ERROR[/red] {result.get('error', 'Unknown error')}")
        elif choice == "3":
            # Symlink mode
            result = install_opc_integration_symlink(claude_dir, opc_source)

            if result["success"]:
                console.print(f"  [green]OK[/green] Symlinked: {', '.join(result['symlinked_dirs'])}")
                if result["backed_up_dirs"]:
                    console.print(f"  [green]OK[/green] Backed up: {', '.join(result['backed_up_dirs'])}")
                console.print("  [dim]Changes in ~/.claude/ now sync to repo automatically[/dim]")

                # Build TypeScript hooks
                console.print("  Building TypeScript hooks...")
                hooks_dir = claude_dir / "hooks"
                build_success, build_msg = build_typescript_hooks(hooks_dir)
                if build_success:
                    console.print(f"  [green]OK[/green] {build_msg}")
                else:
                    console.print(f"  [yellow]WARN[/yellow] {build_msg}")
                    console.print("  [dim]You can build manually: cd ~/.claude/hooks && npm install && npm run build[/dim]")
            else:
                console.print(f"  [red]ERROR[/red] {result.get('error', 'Unknown error')}")
        else:
            console.print("  Skipped integration installation")
    else:
        # Clean install - offer copy vs symlink
        console.print("  No existing configuration found.")
        console.print("\n[bold]Installation Mode:[/bold]")
        console.print("  1. Copy install (default - copies files to ~/.claude/)")
        console.print("  2. [cyan]Symlink install[/cyan] (links to repo - best for contributors)")
        console.print("  3. Skip")
        console.print("")
        console.print("  [dim]Symlink mode links rules/skills/hooks/agents to the repo.[/dim]")
        console.print("  [dim]Changes sync automatically; great for contributing back.[/dim]")

        choice = Prompt.ask("Choose mode", choices=["1", "2", "3"], default="1")

        if choice == "1":
            opc_source = get_opc_integration_source()
            result = install_opc_integration(claude_dir, opc_source)

            if result["success"]:
                console.print(f"  [green]OK[/green] Installed {result['installed_hooks']} hooks")
                console.print(f"  [green]OK[/green] Installed {result['installed_skills']} skills")
                console.print(f"  [green]OK[/green] Installed {result['installed_rules']} rules")
                console.print(f"  [green]OK[/green] Installed {result['installed_agents']} agents")
                console.print(f"  [green]OK[/green] Installed {result['installed_servers']} MCP servers")

                # Build TypeScript hooks
                console.print("  Building TypeScript hooks...")
                hooks_dir = claude_dir / "hooks"
                build_success, build_msg = build_typescript_hooks(hooks_dir)
                if build_success:
                    console.print(f"  [green]OK[/green] {build_msg}")
                else:
                    console.print(f"  [yellow]WARN[/yellow] {build_msg}")
                    console.print("  [dim]You can build manually: cd ~/.claude/hooks && npm install && npm run build[/dim]")
            else:
                console.print(f"  [red]ERROR[/red] {result.get('error', 'Unknown error')}")
        elif choice == "2":
            opc_source = get_opc_integration_source()
            result = install_opc_integration_symlink(claude_dir, opc_source)

            if result["success"]:
                console.print(f"  [green]OK[/green] Symlinked: {', '.join(result['symlinked_dirs'])}")
                console.print("  [dim]Changes in ~/.claude/ now sync to repo automatically[/dim]")

                # Build TypeScript hooks
                console.print("  Building TypeScript hooks...")
                hooks_dir = claude_dir / "hooks"
                build_success, build_msg = build_typescript_hooks(hooks_dir)
                if build_success:
                    console.print(f"  [green]OK[/green] {build_msg}")
                else:
                    console.print(f"  [yellow]WARN[/yellow] {build_msg}")
                    console.print("  [dim]You can build manually: cd ~/.claude/hooks && npm install && npm run build[/dim]")
            else:
                console.print(f"  [red]ERROR[/red] {result.get('error', 'Unknown error')}")
        else:
            console.print("  Skipped integration installation")

    # Note: Environment variables are set in the final step when scripts are installed
    # to ~/.claude/claude2000/. This ensures CLAUDE_2000_DIR and CLAUDE_OPC_DIR
    # point to the installed location, not the repo.

    # Step 8: TLDR Code Analysis Tool
    console.print("\n[bold]Step 7/9: TLDR Code Analysis Tool[/bold]")
    console.print("  TLDR provides token-efficient code analysis for LLMs:")
    console.print("    - 95% token savings vs reading raw files")
    console.print("    - 155x faster queries with daemon mode")
    console.print("    - Semantic search, call graphs, program slicing")
    console.print("    - Works with Python, TypeScript, Go, Rust")
    console.print("")
    console.print("  [dim]Note: First semantic search downloads ~1.3GB embedding model.[/dim]")

    if Confirm.ask("\nInstall TLDR code analysis tool?", default=True):
        console.print("  Installing TLDR...")
        import subprocess

        try:
            # Install from PyPI using uv tool (puts tldr CLI in PATH)
            # Use 300s timeout - first install resolves many deps
            result = subprocess.run(
                ["uv", "tool", "install", "llm-tldr"],
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode == 0:
                console.print("  [green]OK[/green] TLDR installed")

                # Verify it works AND is the right tldr (not tldr-pages)
                console.print("  Verifying installation...")
                verify_result = subprocess.run(
                    ["tldr", "--help"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                # Check if this is llm-tldr (has 'tree', 'structure', 'daemon') not tldr-pages
                is_llm_tldr = any(cmd in verify_result.stdout for cmd in ["tree", "structure", "daemon"])
                if verify_result.returncode == 0 and is_llm_tldr:
                    console.print("  [green]OK[/green] TLDR CLI available")
                elif verify_result.returncode == 0 and not is_llm_tldr:
                    console.print("  [yellow]WARN[/yellow] Wrong tldr detected (tldr-pages, not llm-tldr)")
                    console.print("  [yellow]    [/yellow] The 'tldr' command is shadowed by tldr-pages.")
                    console.print("  [yellow]    [/yellow] Uninstall tldr-pages: pip uninstall tldr")
                    console.print("  [yellow]    [/yellow] Or use full path: ~/.local/bin/tldr")

                if is_llm_tldr:
                    console.print("")
                    console.print("  [dim]Quick start:[/dim]")
                    console.print("    tldr tree .              # See project structure")
                    console.print("    tldr structure . --lang python  # Code overview")
                    console.print("    tldr daemon start        # Start daemon (155x faster)")

                    # Configure semantic search
                    console.print("")
                    console.print("  [bold]Semantic Search Configuration[/bold]")
                    console.print("  Natural language code search using AI embeddings.")
                    console.print("  [dim]First run downloads ~1.3GB model and indexes your codebase.[/dim]")
                    console.print("  [dim]Auto-reindexes in background when files change.[/dim]")

                    if Confirm.ask("\n  Enable semantic search?", default=True):
                        # Get threshold
                        threshold_str = Prompt.ask(
                            "  Auto-reindex after how many file changes?",
                            default="20"
                        )
                        try:
                            threshold = int(threshold_str)
                        except ValueError:
                            threshold = 20

                        # Save config to global ~/.claude/settings.json
                        settings_path = get_global_claude_dir() / "settings.json"
                        settings = {}
                        if settings_path.exists():
                            try:
                                settings = json.loads(settings_path.read_text())
                            except Exception:
                                pass

                        # Detect GPU for model selection
                        # BGE-large (1.3GB) needs GPU, MiniLM (80MB) works on CPU
                        has_gpu = False
                        try:
                            import torch
                            has_gpu = torch.cuda.is_available() or torch.backends.mps.is_available()
                        except ImportError:
                            pass  # No torch = assume no GPU

                        if has_gpu:
                            model = "bge-large-en-v1.5"
                            timeout = 600  # 10 min with GPU
                        else:
                            model = "all-MiniLM-L6-v2"
                            timeout = 300  # 5 min for small model
                            console.print("  [dim]No GPU detected, using lightweight model[/dim]")

                        settings["semantic_search"] = {
                            "enabled": True,
                            "auto_reindex_threshold": threshold,
                            "model": model,
                        }

                        settings_path.parent.mkdir(parents=True, exist_ok=True)
                        settings_path.write_text(json.dumps(settings, indent=2))
                        console.print(f"  [green]OK[/green] Semantic search enabled (threshold: {threshold})")

                        # Offer to pre-download embedding model
                        # Note: We only download the model here, not index any directory.
                        # Indexing happens per-project when user runs `tldr semantic index .`
                        if Confirm.ask("\n  Pre-download embedding model now?", default=True):
                            console.print(f"  Downloading {model} embedding model...")
                            try:
                                # Just load the model to trigger download (no indexing)
                                download_result = subprocess.run(
                                    [sys.executable, "-c", f"from tldr.semantic import get_model; get_model('{model}')"],
                                    capture_output=True,
                                    text=True,
                                    timeout=timeout,
                                    env={**os.environ, "TLDR_AUTO_DOWNLOAD": "1"},
                                )
                                if download_result.returncode == 0:
                                    console.print("  [green]OK[/green] Embedding model downloaded")
                                else:
                                    console.print("  [yellow]WARN[/yellow] Download had issues")
                                    if download_result.stderr:
                                        console.print(f"       {download_result.stderr[:200]}")
                            except subprocess.TimeoutExpired:
                                console.print("  [yellow]WARN[/yellow] Download timed out")
                            except Exception as e:
                                console.print(f"  [yellow]WARN[/yellow] {e}")
                        else:
                            console.print("  [dim]Model downloads on first use of: tldr semantic index .[/dim]")
                    else:
                        console.print("  Semantic search disabled")
                        console.print("  [dim]Enable later in .claude/settings.json[/dim]")
                else:
                    console.print("  [yellow]WARN[/yellow] TLDR installed but not on PATH")
            else:
                console.print("  [red]ERROR[/red] Installation failed")
                console.print(f"       {result.stderr[:200]}")
                console.print("  You can install manually with: uv tool install llm-tldr")
        except subprocess.TimeoutExpired:
            console.print("  [yellow]WARN[/yellow] Installation timed out")
            console.print("  You can install manually with: uv tool install llm-tldr")
        except Exception as e:
            console.print(f"  [red]ERROR[/red] {e}")
            console.print("  You can install manually with: uv tool install llm-tldr")
    else:
        console.print("  Skipped TLDR installation")
        console.print("  [dim]Install later with: uv tool install llm-tldr[/dim]")

    # Step 10: Diagnostics Tools (Shift-Left Feedback)
    console.print("\n[bold]Step 8/9: Diagnostics Tools (Shift-Left Feedback)[/bold]")
    console.print("  Claude gets immediate type/lint feedback after editing files.")
    console.print("  This catches errors before tests run (shift-left).")
    console.print("")

    # Auto-detect what's installed
    diagnostics_tools = {
        "pyright": {"cmd": "pyright", "lang": "Python", "install": "pip install pyright"},
        "ruff": {"cmd": "ruff", "lang": "Python", "install": "pip install ruff"},
        "eslint": {"cmd": "eslint", "lang": "TypeScript/JS", "install": "npm install -g eslint"},
        "tsc": {"cmd": "tsc", "lang": "TypeScript", "install": "npm install -g typescript"},
        "go": {"cmd": "go", "lang": "Go", "install": "brew install go"},
        "clippy": {"cmd": "cargo", "lang": "Rust", "install": "rustup component add clippy"},
    }

    console.print("  [bold]Detected tools:[/bold]")
    missing_tools = []
    for name, info in diagnostics_tools.items():
        if shutil.which(info["cmd"]):
            console.print(f"    [green]✓[/green] {info['lang']}: {name}")
        else:
            console.print(f"    [red]✗[/red] {info['lang']}: {name}")
            missing_tools.append((name, info))

    if missing_tools:
        console.print("")
        console.print("  [bold]Install missing tools:[/bold]")
        for name, info in missing_tools:
            console.print(f"    {name}: [dim]{info['install']}[/dim]")
    else:
        console.print("")
        console.print("  [green]All diagnostics tools available![/green]")

    console.print("")
    console.print("  [dim]Note: Currently only Python diagnostics are wired up.[/dim]")
    console.print("  [dim]TypeScript, Go, Rust coming soon.[/dim]")

    # Step 9/9: Install scripts to ~/.claude/claude2000/
    console.print("\n[bold]Step 9/9: Installing Claude2000 scripts...[/bold]")
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
        console.print("  Creating Python virtual environment...")
        venv_path = install_dir / ".venv"
        try:
            import subprocess
            # Create venv using uv
            result = subprocess.run(
                ["uv", "venv", str(venv_path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                console.print(f"  [green]OK[/green] Created venv at {venv_path}")

                # Install minimal dependencies for memory scripts
                console.print("  Installing dependencies (this may take a minute)...")
                deps = [
                    "python-dotenv",
                    "asyncpg",
                    "numpy",
                    "sentence-transformers",
                    "httpx",  # For HTTP requests in embedding service
                ]
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

        # Set CLAUDE_2000_DIR environment variable in shell config
        shell_config = None
        shell = os.environ.get("SHELL", "")
        if "zsh" in shell:
            shell_config = Path.home() / ".zshrc"
        elif "bash" in shell:
            shell_config = Path.home() / ".bashrc"

        if shell_config and shell_config.exists():
            content = shell_config.read_text()
            export_line = f'export CLAUDE_2000_DIR="{install_dir}"'
            # Also add CLAUDE_OPC_DIR as alias for backwards compatibility
            alias_line = f'export CLAUDE_OPC_DIR="$CLAUDE_2000_DIR"'
            if "CLAUDE_2000_DIR" not in content:
                with open(shell_config, "a") as f:
                    f.write(f"\n# Claude2000 scripts location\n{export_line}\n{alias_line}\n")
                console.print(f"  [green]OK[/green] Added CLAUDE_2000_DIR to {shell_config.name}")
            else:
                console.print(f"  [dim]CLAUDE_2000_DIR already in {shell_config.name}[/dim]")
        else:
            console.print(f"  [yellow]NOTE[/yellow] Add to your shell config:")
            console.print(f'       export CLAUDE_2000_DIR="{install_dir}"')

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
        console.print(
            Panel.fit("[bold]CLAUDE2000[/bold]", border_style="blue")
        )
        console.print("\n[bold]Options:[/bold]")
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
        sys.exit(130)
    except Exception as e:
        console.print(f"\n[red]Error: {rich_escape(str(e))}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
