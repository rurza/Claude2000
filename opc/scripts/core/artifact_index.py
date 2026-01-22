#!/usr/bin/env python3
"""
USAGE: artifact_index.py [--handoffs] [--plans] [--continuity] [--all] [--file PATH]

Index handoffs, plans, and continuity ledgers into PostgreSQL.
NO SQLITE - PostgreSQL only via asyncpg.

Examples:
    # Index all handoffs
    PYTHONPATH=~/.claude/claude2000 python scripts/core/artifact_index.py --handoffs

    # Index everything
    PYTHONPATH=~/.claude/claude2000 python scripts/core/artifact_index.py --all

    # Index a single handoff file (fast, for hooks)
    PYTHONPATH=~/.claude/claude2000 python scripts/core/artifact_index.py --file thoughts/shared/handoffs/session/task-01.md
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

# Load .env files for CLAUDE2000_DB_URL
try:
    from dotenv import load_dotenv
    # Try ~/.claude/claude2000/.env first (consolidated location)
    claude2000_env = Path.home() / ".claude" / "claude2000" / ".env"
    if claude2000_env.exists():
        load_dotenv(claude2000_env)
    else:
        # Fallback to ~/.claude/.env
        global_env = Path.home() / ".claude" / ".env"
        if global_env.exists():
            load_dotenv(global_env)
except ImportError:
    pass  # dotenv not required if env vars already set


def get_postgres_url() -> str | None:
    """Get PostgreSQL URL from environment."""
    return os.environ.get("CLAUDE2000_DB_URL")


# =============================================================================
# POSTGRESQL SCHEMA INITIALIZATION
# =============================================================================

async def init_postgres() -> None:
    """Ensure PostgreSQL schema exists for artifact tables."""
    from scripts.core.db.postgres_pool import get_connection

    async with get_connection() as conn:
        # Create handoffs table if not exists
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS handoffs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                session_name TEXT,
                task_number INTEGER,
                file_path TEXT UNIQUE,
                task_summary TEXT,
                what_worked TEXT,
                what_failed TEXT,
                key_decisions TEXT,
                files_modified TEXT,
                outcome TEXT DEFAULT 'UNKNOWN',
                outcome_notes TEXT,
                root_span_id TEXT,
                turn_span_id TEXT,
                session_id TEXT,
                braintrust_session_id TEXT,
                created_at TIMESTAMP,
                indexed_at TIMESTAMP DEFAULT NOW(),
                goal TEXT
            )
        """)

        # Create plans table if not exists
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS plans (
                id TEXT PRIMARY KEY,
                title TEXT,
                file_path TEXT,
                overview TEXT,
                approach TEXT,
                phases TEXT,
                constraints TEXT,
                indexed_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Create continuity table if not exists
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS continuity (
                id TEXT PRIMARY KEY,
                session_name TEXT,
                goal TEXT,
                state_done TEXT,
                state_now TEXT,
                state_next TEXT,
                key_learnings TEXT,
                key_decisions TEXT,
                snapshot_reason TEXT,
                indexed_at TIMESTAMP DEFAULT NOW()
            )
        """)


# =============================================================================
# PARSING FUNCTIONS (sync - no database access)
# =============================================================================

def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Extract YAML frontmatter from markdown content.

    Returns:
        Tuple of (frontmatter_dict, remaining_content)
    """
    if not content.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    frontmatter = {}
    for line in parts[1].strip().split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            frontmatter[key.strip()] = value.strip()

    return frontmatter, parts[2]


def extract_sections(content: str, level: int = 2) -> dict:
    """Extract markdown sections at the specified heading level.

    Args:
        content: Markdown content to parse
        level: Heading level (2 for ##, 3 for ###)

    Returns:
        Dict mapping normalized section names to content
    """
    if not content:
        return {}

    prefix = "#" * level + " "
    next_level_prefix = "#" * (level - 1) + " " if level > 1 else None

    sections = {}
    current_section = None
    current_content = []

    for line in content.split("\n"):
        if line.startswith(prefix):
            if current_section:
                sections[current_section] = "\n".join(current_content).strip()
            current_section = line[len(prefix):].strip().lower().replace(" ", "_")
            current_content = []
        elif next_level_prefix and line.startswith(next_level_prefix):
            # Stop at higher-level heading
            if current_section:
                sections[current_section] = "\n".join(current_content).strip()
            break
        elif current_section:
            current_content.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_content).strip()

    return sections


def extract_session_info(file_path: Path) -> tuple[str, str | None]:
    """Extract session name and optional UUID from handoff file path.

    Path format: thoughts/shared/handoffs/<session>/<filename>
    """
    parts = file_path.parts
    session_name = "unknown"

    # Find 'handoffs' in path and get next component
    for i, part in enumerate(parts):
        if part == "handoffs" and i + 1 < len(parts):
            session_name = parts[i + 1]
            break

    # Extract UUID from filename if present
    uuid_match = re.search(r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", file_path.stem)
    session_uuid = uuid_match.group(1) if uuid_match else None

    return session_name, session_uuid


def normalize_outcome(status: str) -> str:
    """Normalize status string to canonical outcome value."""
    if not status:
        return "UNKNOWN"

    status_lower = status.lower().strip()

    # Map common variations to canonical values
    if status_lower in ("succeeded", "success", "complete", "completed", "done"):
        return "SUCCEEDED"
    elif status_lower in ("partial_plus", "partial+", "partial-plus"):
        return "PARTIAL_PLUS"
    elif status_lower in ("partial_minus", "partial-", "partial-minus", "partial"):
        return "PARTIAL_MINUS"
    elif status_lower in ("failed", "failure", "error"):
        return "FAILED"
    elif status_lower in ("unknown", "pending", "in_progress"):
        return "UNKNOWN"
    else:
        return status.upper()


def parse_yaml_handoff(file_path: Path, raw_content: str) -> dict:
    """Parse a YAML handoff file into structured data."""
    # Parse YAML content (skip frontmatter markers if present)
    content = raw_content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 2:
            # If there's content after second ---, use first YAML block
            content = parts[1]

    try:
        data = yaml.safe_load(content) or {}
    except yaml.YAMLError:
        data = {}

    session_name, session_uuid = extract_session_info(file_path)
    file_id = hashlib.md5(str(file_path).encode()).hexdigest()[:12]

    # Handle nested structures
    context = data.get("context", {})
    if isinstance(context, str):
        context = {"raw": context}

    # Extract outcome from status field or outcome field
    outcome = data.get("outcome", data.get("status", "UNKNOWN"))
    outcome = normalize_outcome(outcome)

    # Handle learnings - can be string, list, or dict
    learnings = data.get("learnings", {})
    if isinstance(learnings, list):
        what_worked = "\n".join(str(item) for item in learnings)
        what_failed = ""
    elif isinstance(learnings, dict):
        what_worked = learnings.get("what_worked", "")
        what_failed = learnings.get("what_failed", "")
        if isinstance(what_worked, list):
            what_worked = "\n".join(str(item) for item in what_worked)
        if isinstance(what_failed, list):
            what_failed = "\n".join(str(item) for item in what_failed)
    else:
        what_worked = str(learnings) if learnings else ""
        what_failed = ""

    # Handle decisions
    decisions = data.get("decisions", {})
    if isinstance(decisions, dict):
        key_decisions = json.dumps(decisions)
    elif isinstance(decisions, list):
        key_decisions = "\n".join(str(item) for item in decisions)
    else:
        key_decisions = str(decisions) if decisions else ""

    # Handle files_modified
    files_modified = data.get("files_modified", data.get("files_to_modify", []))
    if isinstance(files_modified, list):
        files_modified = json.dumps(files_modified)
    else:
        files_modified = str(files_modified) if files_modified else ""

    # Extract goal/task summary
    goal = data.get("goal", data.get("now", data.get("task", "")))
    if isinstance(goal, dict):
        goal = goal.get("description", str(goal))

    return {
        "id": file_id,
        "session_name": data.get("session", session_name),
        "task_number": data.get("task_number", 0),
        "file_path": str(file_path),
        "task_summary": goal,
        "what_worked": what_worked,
        "what_failed": what_failed,
        "key_decisions": key_decisions,
        "files_modified": files_modified,
        "outcome": outcome,
        "root_span_id": data.get("root_span_id", ""),
        "turn_span_id": data.get("turn_span_id", ""),
        "session_id": session_uuid or data.get("session_id", ""),
        "braintrust_session_id": data.get("braintrust_session_id", ""),
        "created_at": data.get("date", datetime.now().isoformat()),
    }


def parse_markdown_handoff(file_path: Path, raw_content: str) -> dict:
    """Parse a markdown handoff file into structured data."""
    frontmatter, content = parse_frontmatter(raw_content)
    sections = extract_sections(content)

    session_name, session_uuid = extract_session_info(file_path)
    file_id = hashlib.md5(str(file_path).encode()).hexdigest()[:12]

    # Get outcome from frontmatter or sections
    outcome = frontmatter.get("status", frontmatter.get("outcome", "UNKNOWN"))
    outcome = normalize_outcome(outcome)

    return {
        "id": file_id,
        "session_name": frontmatter.get("session", session_name),
        "task_number": int(frontmatter.get("task_number", 0)),
        "file_path": str(file_path),
        "task_summary": sections.get("summary", sections.get("goal", "")),
        "what_worked": sections.get("what_worked", sections.get("learnings", "")),
        "what_failed": sections.get("what_failed", sections.get("blockers", "")),
        "key_decisions": sections.get("key_decisions", sections.get("decisions", "")),
        "files_modified": json.dumps(extract_files(content)),
        "outcome": outcome,
        "root_span_id": frontmatter.get("root_span_id", ""),
        "turn_span_id": frontmatter.get("turn_span_id", ""),
        "session_id": session_uuid or frontmatter.get("session_id", ""),
        "braintrust_session_id": frontmatter.get("braintrust_session_id", ""),
        "created_at": frontmatter.get("date", datetime.now().isoformat()),
    }


def parse_handoff(file_path: Path) -> dict:
    """Parse a handoff file (YAML or Markdown) into structured data."""
    raw_content = file_path.read_text()

    # Determine format based on extension and content
    if file_path.suffix in (".yaml", ".yml"):
        return parse_yaml_handoff(file_path, raw_content)
    elif file_path.suffix == ".md":
        # Check if it's actually YAML with .md extension (frontmatter-only)
        if raw_content.startswith("---"):
            parts = raw_content.split("---", 2)
            if len(parts) >= 3 and not parts[2].strip():
                # Only frontmatter, treat as YAML
                return parse_yaml_handoff(file_path, raw_content)
        return parse_markdown_handoff(file_path, raw_content)
    else:
        # Default to YAML parsing
        return parse_yaml_handoff(file_path, raw_content)


def extract_files(content: str) -> list:
    """Extract file paths from markdown content."""
    files = []
    for line in content.split("\n"):
        # Match backtick-quoted paths with extensions
        matches = re.findall(r"`([^`]+\.[a-z]+)(:[^`]*)?`", line)
        files.extend([m[0] for m in matches])
        # Match **File**: format
        matches = re.findall(r"\*\*File\*\*:\s*`?([^\s`]+)`?", line)
        files.extend(matches)
    return files


def parse_plan(file_path: Path) -> dict:
    """Parse a plan markdown file into structured data."""
    content = file_path.read_text()

    # Generate ID
    file_id = hashlib.md5(str(file_path).encode()).hexdigest()[:12]

    # Extract title from first H1
    title_match = re.search(r"^# (.+)$", content, re.MULTILINE)
    title = title_match.group(1) if title_match else file_path.stem

    # Extract sections
    sections = {}
    current_section = None
    current_content = []

    for line in content.split("\n"):
        if line.startswith("## "):
            if current_section:
                sections[current_section] = "\n".join(current_content).strip()
            current_section = line[3:].strip().lower().replace(" ", "_")
            current_content = []
        elif current_section:
            current_content.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_content).strip()

    # Extract phases
    phases = []
    for key in sections:
        if key.startswith("phase_"):
            phases.append({"name": key, "content": sections[key][:500]})

    return {
        "id": file_id,
        "title": title,
        "file_path": str(file_path),
        "overview": sections.get("overview", "")[:1000],
        "approach": sections.get("implementation_approach", sections.get("approach", ""))[:1000],
        "phases": json.dumps(phases),
        "constraints": sections.get("what_we're_not_doing", sections.get("constraints", "")),
    }


def parse_continuity(file_path: Path) -> dict:
    """Parse a continuity ledger into structured data."""
    content = file_path.read_text()

    # Generate ID
    file_id = hashlib.md5(str(file_path).encode()).hexdigest()[:12]

    # Extract session name from filename (CONTINUITY_CLAUDE-<session>.md)
    session_match = re.search(r"CONTINUITY_CLAUDE-(.+)\.md", file_path.name)
    session_name = session_match.group(1) if session_match else file_path.stem

    # Extract sections
    sections = {}
    current_section = None
    current_content = []

    for line in content.split("\n"):
        if line.startswith("## "):
            if current_section:
                sections[current_section] = "\n".join(current_content).strip()
            current_section = line[3:].strip().lower().replace(" ", "_")
            current_content = []
        elif current_section:
            current_content.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_content).strip()

    # Parse state section
    state = sections.get("state", "")
    state_done = []
    state_now = ""
    state_next = ""

    for line in state.split("\n"):
        if "[x]" in line.lower():
            state_done.append(line.strip())
        elif "[->]" in line or "now:" in line.lower():
            state_now = line.strip()
        elif "[ ]" in line or "next:" in line.lower():
            state_next = line.strip()

    return {
        "id": file_id,
        "session_name": session_name,
        "goal": sections.get("goal", "")[:500],
        "state_done": json.dumps(state_done),
        "state_now": state_now,
        "state_next": state_next,
        "key_learnings": sections.get(
            "key_learnings", sections.get("key_learnings_(this_session)", "")
        ),
        "key_decisions": sections.get("key_decisions", ""),
        "snapshot_reason": "manual",
    }


# =============================================================================
# ASYNC INDEX FUNCTIONS
# =============================================================================

async def index_handoffs(base_path: Path = Path("thoughts/shared/handoffs")) -> int:
    """Index all handoffs into PostgreSQL."""
    from scripts.core.db.postgres_pool import get_connection

    if not base_path.exists():
        print(f"Handoffs directory not found: {base_path}")
        return 0

    count = 0
    # Search for YAML files first (preferred), then Markdown
    handoff_files = list(itertools.chain(
        base_path.rglob("*.yaml"),
        base_path.rglob("*.yml"),
        base_path.rglob("*.md"),
    ))

    async with get_connection() as conn:
        for handoff_file in handoff_files:
            try:
                data = parse_handoff(handoff_file)
                await conn.execute(
                    """
                    INSERT INTO handoffs
                    (session_name, file_path, goal, what_worked, what_failed,
                     key_decisions, outcome, root_span_id, session_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (file_path) DO UPDATE SET
                        goal = EXCLUDED.goal,
                        what_worked = EXCLUDED.what_worked,
                        what_failed = EXCLUDED.what_failed,
                        key_decisions = EXCLUDED.key_decisions,
                        outcome = EXCLUDED.outcome,
                        root_span_id = EXCLUDED.root_span_id,
                        session_id = EXCLUDED.session_id,
                        indexed_at = NOW()
                    """,
                    data["session_name"],
                    data["file_path"],
                    data["task_summary"],  # -> goal
                    data["what_worked"],
                    data["what_failed"],
                    data["key_decisions"],
                    data["outcome"],
                    data["root_span_id"],
                    data["session_id"],
                )
                count += 1
            except Exception as e:
                print(f"Error indexing {handoff_file}: {e}")

    print(f"Indexed {count} handoffs")
    return count


async def index_plans(base_path: Path = Path("thoughts/shared/plans")) -> int:
    """Index all plans into PostgreSQL."""
    from scripts.core.db.postgres_pool import get_connection

    if not base_path.exists():
        print(f"Plans directory not found: {base_path}")
        return 0

    count = 0
    plan_files = list(base_path.glob("*.md"))

    async with get_connection() as conn:
        for plan_file in plan_files:
            try:
                data = parse_plan(plan_file)
                await conn.execute(
                    """
                    INSERT INTO plans (id, title, file_path, overview, approach, phases, constraints)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (id) DO UPDATE SET
                        title = EXCLUDED.title,
                        file_path = EXCLUDED.file_path,
                        overview = EXCLUDED.overview,
                        approach = EXCLUDED.approach,
                        phases = EXCLUDED.phases,
                        constraints = EXCLUDED.constraints,
                        indexed_at = NOW()
                    """,
                    data["id"],
                    data["title"],
                    data["file_path"],
                    data["overview"],
                    data["approach"],
                    data["phases"],
                    data["constraints"],
                )
                count += 1
            except Exception as e:
                print(f"Error indexing {plan_file}: {e}")

    print(f"Indexed {count} plans")
    return count


async def index_continuity(base_path: Path = Path(".")) -> int:
    """Index all continuity ledgers into PostgreSQL."""
    from scripts.core.db.postgres_pool import get_connection

    count = 0
    ledger_files = list(base_path.glob("CONTINUITY_CLAUDE-*.md"))

    async with get_connection() as conn:
        for ledger_file in ledger_files:
            try:
                data = parse_continuity(ledger_file)
                await conn.execute(
                    """
                    INSERT INTO continuity
                    (id, session_name, goal, state_done, state_now, state_next,
                     key_learnings, key_decisions, snapshot_reason)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (id) DO UPDATE SET
                        session_name = EXCLUDED.session_name,
                        goal = EXCLUDED.goal,
                        state_done = EXCLUDED.state_done,
                        state_now = EXCLUDED.state_now,
                        state_next = EXCLUDED.state_next,
                        key_learnings = EXCLUDED.key_learnings,
                        key_decisions = EXCLUDED.key_decisions,
                        snapshot_reason = EXCLUDED.snapshot_reason,
                        indexed_at = NOW()
                    """,
                    data["id"],
                    data["session_name"],
                    data["goal"],
                    data["state_done"],
                    data["state_now"],
                    data["state_next"],
                    data["key_learnings"],
                    data["key_decisions"],
                    data["snapshot_reason"],
                )
                count += 1
            except Exception as e:
                print(f"Error indexing {ledger_file}: {e}")

    print(f"Indexed {count} continuity ledgers")
    return count


async def index_single_file(file_path: Path) -> bool:
    """Index a single file based on its location/type.

    Returns True if indexed successfully, False otherwise.
    """
    from scripts.core.db.postgres_pool import get_connection

    file_path = Path(file_path).resolve()
    path_str = str(file_path)

    if "handoffs" in path_str and file_path.suffix in (".md", ".yaml", ".yml"):
        try:
            data = parse_handoff(file_path)
            async with get_connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO handoffs
                    (session_name, file_path, goal, what_worked, what_failed,
                     key_decisions, outcome, root_span_id, session_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (file_path) DO UPDATE SET
                        goal = EXCLUDED.goal,
                        what_worked = EXCLUDED.what_worked,
                        what_failed = EXCLUDED.what_failed,
                        key_decisions = EXCLUDED.key_decisions,
                        outcome = EXCLUDED.outcome,
                        root_span_id = EXCLUDED.root_span_id,
                        session_id = EXCLUDED.session_id,
                        indexed_at = NOW()
                    """,
                    data["session_name"],
                    data["file_path"],
                    data["task_summary"],
                    data["what_worked"],
                    data["what_failed"],
                    data["key_decisions"],
                    data["outcome"],
                    data["root_span_id"],
                    data["session_id"],
                )
            print(f"Indexed handoff: {file_path.name}")
            return True
        except Exception as e:
            print(f"Error indexing handoff {file_path}: {e}")
            return False

    elif "plans" in path_str and file_path.suffix == ".md":
        try:
            data = parse_plan(file_path)
            async with get_connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO plans (id, title, file_path, overview, approach, phases, constraints)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (id) DO UPDATE SET
                        title = EXCLUDED.title,
                        file_path = EXCLUDED.file_path,
                        overview = EXCLUDED.overview,
                        approach = EXCLUDED.approach,
                        phases = EXCLUDED.phases,
                        constraints = EXCLUDED.constraints,
                        indexed_at = NOW()
                    """,
                    data["id"],
                    data["title"],
                    data["file_path"],
                    data["overview"],
                    data["approach"],
                    data["phases"],
                    data["constraints"],
                )
            print(f"Indexed plan: {file_path.name}")
            return True
        except Exception as e:
            print(f"Error indexing plan {file_path}: {e}")
            return False

    elif file_path.name.startswith("CONTINUITY_CLAUDE-"):
        try:
            data = parse_continuity(file_path)
            async with get_connection() as conn:
                await conn.execute(
                    """
                    INSERT INTO continuity
                    (id, session_name, goal, state_done, state_now, state_next,
                     key_learnings, key_decisions, snapshot_reason)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (id) DO UPDATE SET
                        session_name = EXCLUDED.session_name,
                        goal = EXCLUDED.goal,
                        state_done = EXCLUDED.state_done,
                        state_now = EXCLUDED.state_now,
                        state_next = EXCLUDED.state_next,
                        key_learnings = EXCLUDED.key_learnings,
                        key_decisions = EXCLUDED.key_decisions,
                        snapshot_reason = EXCLUDED.snapshot_reason,
                        indexed_at = NOW()
                    """,
                    data["id"],
                    data["session_name"],
                    data["goal"],
                    data["state_done"],
                    data["state_now"],
                    data["state_next"],
                    data["key_learnings"],
                    data["key_decisions"],
                    data["snapshot_reason"],
                )
            print(f"Indexed continuity: {file_path.name}")
            return True
        except Exception as e:
            print(f"Error indexing continuity {file_path}: {e}")
            return False

    else:
        print(f"Unknown file type, skipping: {file_path}")
        return False


# =============================================================================
# MAIN
# =============================================================================

async def async_main() -> int:
    """Async main function."""
    parser = argparse.ArgumentParser(description="Index context graph artifacts (PostgreSQL only)")
    parser.add_argument("--handoffs", action="store_true", help="Index handoffs")
    parser.add_argument("--plans", action="store_true", help="Index plans")
    parser.add_argument("--continuity", action="store_true", help="Index continuity ledgers")
    parser.add_argument("--all", action="store_true", help="Index everything")
    parser.add_argument("--file", type=str, help="Index a single file (fast, for hooks)")

    args = parser.parse_args()

    # Check PostgreSQL URL is configured
    pg_url = get_postgres_url()
    if not pg_url:
        print("Error: CLAUDE2000_DB_URL not set", file=sys.stderr)
        print("Run the wizard: cd ~/.claude && uv run python -m scripts.setup.wizard", file=sys.stderr)
        return 1

    # Ensure schema exists
    await init_postgres()

    # Handle single file indexing (fast path for hooks)
    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"File not found: {file_path}")
            return 1
        success = await index_single_file(file_path)
        return 0 if success else 1

    if not any([args.handoffs, args.plans, args.continuity, args.all]):
        parser.print_help()
        return 0

    # Show database info
    print(f"Using database: PostgreSQL ({pg_url[:30]}...)")

    if args.all or args.handoffs:
        await index_handoffs()

    if args.all or args.plans:
        await index_plans()

    if args.all or args.continuity:
        await index_continuity()

    print("Done!")
    return 0


def main() -> int:
    """Entry point with asyncio.run() wrapper."""
    return asyncio.run(async_main())


if __name__ == "__main__":
    sys.exit(main())
