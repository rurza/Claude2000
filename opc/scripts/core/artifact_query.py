#!/usr/bin/env python3
"""
USAGE: artifact_query.py <query> [--type TYPE] [--outcome OUTCOME] [--limit N]

Search the Context Graph for relevant precedent using PostgreSQL full-text search.
NO SQLITE - PostgreSQL only via asyncpg.

Examples:
    # Search for authentication-related work
    PYTHONPATH=~/.claude/claude2000 python scripts/core/artifact_query.py "authentication OAuth JWT"

    # Search only successful handoffs
    PYTHONPATH=~/.claude/claude2000 python scripts/core/artifact_query.py "implement agent" --outcome SUCCEEDED

    # Search plans only
    PYTHONPATH=~/.claude/claude2000 python scripts/core/artifact_query.py "API design" --type plans
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

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


def build_tsquery(query: str) -> str:
    """Build a PostgreSQL tsquery from user input.

    Splits query into words and joins with | (OR) for flexible matching.
    Each word is sanitized to prevent syntax errors.
    """
    # Split on whitespace and filter empty strings
    words = [w.strip() for w in query.split() if w.strip()]
    if not words:
        return ""
    # Sanitize each word (remove special characters that break tsquery)
    sanitized = []
    for word in words:
        # Keep alphanumeric and basic punctuation
        clean = "".join(c for c in word if c.isalnum() or c in "-_")
        if clean:
            sanitized.append(clean)
    if not sanitized:
        return ""
    # Join with OR operator for flexible matching
    return " | ".join(sanitized)


# =============================================================================
# ASYNC DATABASE FUNCTIONS
# =============================================================================


async def get_handoff_by_span_id(conn: Any, root_span_id: str) -> dict | None:
    """Get a handoff by its Braintrust root_span_id."""
    sql = """
        SELECT id, session_name, goal, what_worked, what_failed,
               key_decisions, outcome, file_path, root_span_id, created_at
        FROM handoffs
        WHERE root_span_id = $1
        LIMIT 1
    """
    row = await conn.fetchrow(sql, root_span_id)
    if row:
        return dict(row)
    return None


async def get_ledger_for_session(conn: Any, session_name: str) -> dict | None:
    """Get continuity ledger by session name."""
    sql = """
        SELECT id, session_name, goal, key_learnings, key_decisions,
               state_done, state_now, state_next, indexed_at as created_at
        FROM continuity
        WHERE session_name = $1
        ORDER BY indexed_at DESC
        LIMIT 1
    """
    row = await conn.fetchrow(sql, session_name)
    if row:
        return dict(row)
    return None


async def search_handoffs(
    conn: Any, query: str, outcome: str | None = None, limit: int = 5
) -> list[dict]:
    """Search handoffs using PostgreSQL full-text search with ts_rank."""
    tsquery = build_tsquery(query)
    if not tsquery:
        return []

    if outcome:
        sql = """
            SELECT id, session_name, goal as task_summary,
                   what_worked, what_failed, key_decisions,
                   outcome, file_path, created_at,
                   ts_rank(search_vector, to_tsquery('english', $1)) as score
            FROM handoffs
            WHERE search_vector @@ to_tsquery('english', $1)
              AND outcome = $2
            ORDER BY score DESC
            LIMIT $3
        """
        rows = await conn.fetch(sql, tsquery, outcome, limit)
    else:
        sql = """
            SELECT id, session_name, goal as task_summary,
                   what_worked, what_failed, key_decisions,
                   outcome, file_path, created_at,
                   ts_rank(search_vector, to_tsquery('english', $1)) as score
            FROM handoffs
            WHERE search_vector @@ to_tsquery('english', $1)
            ORDER BY score DESC
            LIMIT $2
        """
        rows = await conn.fetch(sql, tsquery, limit)

    return [dict(row) for row in rows]


async def search_plans(conn: Any, query: str, limit: int = 3) -> list[dict]:
    """Search plans using PostgreSQL full-text search with ts_rank."""
    tsquery = build_tsquery(query)
    if not tsquery:
        return []

    sql = """
        SELECT id, title, overview, approach, file_path, indexed_at as created_at,
               ts_rank(search_vector, to_tsquery('english', $1)) as score
        FROM plans
        WHERE search_vector @@ to_tsquery('english', $1)
        ORDER BY score DESC
        LIMIT $2
    """
    rows = await conn.fetch(sql, tsquery, limit)
    return [dict(row) for row in rows]


async def search_continuity(conn: Any, query: str, limit: int = 3) -> list[dict]:
    """Search continuity ledgers using PostgreSQL full-text search with ts_rank."""
    tsquery = build_tsquery(query)
    if not tsquery:
        return []

    sql = """
        SELECT id, session_name, goal, key_learnings, key_decisions,
               state_now, indexed_at as created_at,
               ts_rank(search_vector, to_tsquery('english', $1)) as score
        FROM continuity
        WHERE search_vector @@ to_tsquery('english', $1)
        ORDER BY score DESC
        LIMIT $2
    """
    rows = await conn.fetch(sql, tsquery, limit)
    return [dict(row) for row in rows]


async def search_past_queries(conn: Any, query: str, limit: int = 2) -> list[dict]:
    """Check if similar questions have been asked before."""
    tsquery = build_tsquery(query)
    if not tsquery:
        return []

    sql = """
        SELECT id, question, answer, was_helpful, created_at,
               ts_rank(search_vector, to_tsquery('english', $1)) as score
        FROM queries
        WHERE search_vector @@ to_tsquery('english', $1)
        ORDER BY score DESC
        LIMIT $2
    """
    rows = await conn.fetch(sql, tsquery, limit)
    return [dict(row) for row in rows]


# =============================================================================
# FORMATTING HELPERS
# =============================================================================

STATUS_ICONS = {
    "SUCCEEDED": "v",
    "PARTIAL_PLUS": "~+",
    "PARTIAL_MINUS": "~-",
    "FAILED": "x",
}


def format_result_section(section_type: str, items: list) -> str:
    """Format a single result section using dispatch table."""
    if not items:
        return ""

    formatters = {
        "past_queries": _format_past_queries,
        "handoffs": _format_handoffs,
        "plans": _format_plans,
        "continuity": _format_continuity,
    }

    formatter = formatters.get(section_type)
    if formatter:
        return formatter(items)
    return ""


def _format_past_queries(items: list) -> str:
    """Format past queries section."""
    output = ["## Previously Asked"]
    for q in items:
        question = (q.get("question") or "")[:100]
        answer = (q.get("answer") or "")[:200]
        output.append(f"- **Q:** {question}...")
        output.append(f"  **A:** {answer}...")
    output.append("")
    return "\n".join(output)


def _format_handoffs(items: list) -> str:
    """Format handoffs section."""
    output = ["## Relevant Handoffs"]
    for h in items:
        status_icon = STATUS_ICONS.get(h.get("outcome"), "?")
        session = h.get("session_name", "unknown")
        output.append(f"### {status_icon} {session}")
        summary = (h.get("task_summary") or "")[:200]
        output.append(f"**Summary:** {summary}")
        what_worked = h.get("what_worked")
        if what_worked:
            output.append(f"**What worked:** {what_worked[:200]}")
        what_failed = h.get("what_failed")
        if what_failed:
            output.append(f"**What failed:** {what_failed[:200]}")
        output.append(f"**File:** `{h.get('file_path', '')}`")
        output.append("")
    return "\n".join(output)


def _format_plans(items: list) -> str:
    """Format plans section."""
    output = ["## Relevant Plans"]
    for p in items:
        title = p.get("title", "Untitled")
        output.append(f"### {title}")
        overview = (p.get("overview") or "")[:200]
        output.append(f"**Overview:** {overview}")
        output.append(f"**File:** `{p.get('file_path', '')}`")
        output.append("")
    return "\n".join(output)


def _format_continuity(items: list) -> str:
    """Format continuity section."""
    output = ["## Related Sessions"]
    for c in items:
        session = c.get("session_name", "unknown")
        output.append(f"### Session: {session}")
        goal = (c.get("goal") or "")[:200]
        output.append(f"**Goal:** {goal}")
        key_learnings = c.get("key_learnings")
        if key_learnings:
            output.append(f"**Key learnings:** {key_learnings[:200]}")
        output.append("")
    return "\n".join(output)


# =============================================================================
# SEARCH DISPATCH AND FORMATTING
# =============================================================================


async def handle_span_id_lookup(
    conn: Any, span_id: str, with_content: bool = False
) -> dict | None:
    """Handle --by-span-id lookup mode."""
    handoff = await get_handoff_by_span_id(conn, span_id)

    if not handoff:
        return None

    if with_content and handoff.get("file_path"):
        # Read full file content
        file_path = Path(handoff["file_path"])
        if file_path.exists():
            handoff["content"] = file_path.read_text()

        # Also get the ledger for this session
        session_name = handoff.get("session_name")
        if not session_name and handoff.get("file_path"):
            # Extract from path: thoughts/shared/handoffs/{session_name}/...
            parts = Path(handoff["file_path"]).parts
            if "handoffs" in parts:
                idx = parts.index("handoffs")
                if idx + 1 < len(parts):
                    session_name = parts[idx + 1]

        if session_name:
            # Try to find ledger file directly first
            ledger_path = Path(f"CONTINUITY_CLAUDE-{session_name}.md")
            if ledger_path.exists():
                ledger = {
                    "session_name": session_name,
                    "file_path": str(ledger_path),
                    "content": ledger_path.read_text(),
                }
                handoff["ledger"] = ledger
            else:
                # Fall back to DB lookup
                ledger = await get_ledger_for_session(conn, session_name)
                if ledger:
                    handoff["ledger"] = ledger

    return handoff


async def search_dispatch(
    conn: Any,
    query: str,
    search_type: str = "all",
    outcome: str | None = None,
    limit: int = 5,
) -> dict:
    """Dispatch search to appropriate handlers based on type."""
    results: dict[str, list] = {}

    # Always check past queries
    results["past_queries"] = await search_past_queries(conn, query)

    # Dispatch table for search types
    if search_type == "all" or search_type == "handoffs":
        results["handoffs"] = await search_handoffs(conn, query, outcome, limit)
    if search_type == "all" or search_type == "plans":
        results["plans"] = await search_plans(conn, query, limit)
    if search_type == "all" or search_type == "continuity":
        results["continuity"] = await search_continuity(conn, query, limit)

    return results


def format_results(results: dict, verbose: bool = False) -> str:
    """Format search results for display."""
    output = []

    # Past queries (compound learning)
    if results.get("past_queries"):
        output.append("## Previously Asked")
        for q in results["past_queries"]:
            question = (q.get("question") or "")[:100]
            answer = (q.get("answer") or "")[:200]
            output.append(f"- **Q:** {question}...")
            output.append(f"  **A:** {answer}...")
        output.append("")

    # Handoffs
    if results.get("handoffs"):
        output.append("## Relevant Handoffs")
        for h in results["handoffs"]:
            status_icon = {
                "SUCCEEDED": "v",
                "PARTIAL_PLUS": "~+",
                "PARTIAL_MINUS": "~-",
                "FAILED": "x",
            }.get(h.get("outcome"), "?")
            session = h.get("session_name", "unknown")
            output.append(f"### {status_icon} {session}")
            summary = (h.get("task_summary") or "")[:200]
            output.append(f"**Summary:** {summary}")
            what_worked = h.get("what_worked")
            if what_worked:
                output.append(f"**What worked:** {what_worked[:200]}")
            what_failed = h.get("what_failed")
            if what_failed:
                output.append(f"**What failed:** {what_failed[:200]}")
            output.append(f"**File:** `{h.get('file_path', '')}`")
            output.append("")

    # Plans
    if results.get("plans"):
        output.append("## Relevant Plans")
        for p in results["plans"]:
            title = p.get("title", "Untitled")
            output.append(f"### {title}")
            overview = (p.get("overview") or "")[:200]
            output.append(f"**Overview:** {overview}")
            output.append(f"**File:** `{p.get('file_path', '')}`")
            output.append("")

    # Continuity
    if results.get("continuity"):
        output.append("## Related Sessions")
        for c in results["continuity"]:
            session = c.get("session_name", "unknown")
            output.append(f"### Session: {session}")
            goal = (c.get("goal") or "")[:200]
            output.append(f"**Goal:** {goal}")
            key_learnings = c.get("key_learnings")
            if key_learnings:
                output.append(f"**Key learnings:** {key_learnings[:200]}")
            output.append("")

    if not any(results.values()):
        output.append("No relevant precedent found.")

    return "\n".join(output)


async def save_query(conn: Any, question: str, answer: str, matches: dict) -> None:
    """Save query for compound learning."""
    query_id = hashlib.md5(f"{question}{datetime.now().isoformat()}".encode()).hexdigest()[:12]

    # Build search vector from question
    tsquery_text = build_tsquery(question)

    await conn.execute(
        """
        INSERT INTO queries (id, question, answer, handoffs_matched, plans_matched,
                            continuity_matched, search_vector)
        VALUES ($1, $2, $3, $4, $5, $6, to_tsvector('english', $7))
        ON CONFLICT (id) DO NOTHING
        """,
        query_id,
        question,
        answer,
        json.dumps([str(h.get("id", "")) for h in matches.get("handoffs", [])]),
        json.dumps([str(p.get("id", "")) for p in matches.get("plans", [])]),
        json.dumps([str(c.get("id", "")) for c in matches.get("continuity", [])]),
        question,  # Use full question text for tsvector
    )


# =============================================================================
# ASYNC MAIN AND CLI ENTRY POINT
# =============================================================================


async def async_main() -> None:
    """Async main function."""
    from scripts.core.db.postgres_pool import get_connection

    parser = argparse.ArgumentParser(description="Search the Context Graph for relevant precedent")
    parser.add_argument("query", nargs="*", help="Search query")
    parser.add_argument("--type", choices=["handoffs", "plans", "continuity", "all"], default="all")
    parser.add_argument(
        "--outcome", choices=["SUCCEEDED", "PARTIAL_PLUS", "PARTIAL_MINUS", "FAILED"]
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--save", action="store_true", help="Save query for compound learning")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--by-span-id", type=str, help="Get handoff by Braintrust root_span_id")
    parser.add_argument("--with-content", action="store_true", help="Include full file content")

    args = parser.parse_args()

    # Check database URL is configured
    if not get_postgres_url():
        print("Error: CLAUDE2000_DB_URL not set")
        print("Run the wizard: cd ~/.claude && uv run python -m scripts.setup.wizard")
        return

    print("Using database: PostgreSQL", file=__import__("sys").stderr)

    async with get_connection() as conn:
        # Handle --by-span-id mode (direct lookup, no search)
        if args.by_span_id:
            handoff = await handle_span_id_lookup(conn, args.by_span_id, with_content=args.with_content)

            if args.json:
                print(json.dumps(handoff, indent=2, default=str))
            elif handoff:
                print(f"## Handoff: {handoff.get('session_name')}")
                print(f"**Outcome:** {handoff.get('outcome', 'UNKNOWN')}")
                print(f"**File:** {handoff.get('file_path')}")
                if handoff.get("content"):
                    print(f"\n{handoff['content']}")
            else:
                print(f"No handoff found for root_span_id: {args.by_span_id}")
            return

        # Regular search mode
        if not args.query:
            parser.print_help()
            return

        query = " ".join(args.query)

        # Use dispatch helper for search
        results = await search_dispatch(conn, query, args.type, args.outcome, args.limit)

        if args.json:
            # Convert UUID objects to strings for JSON serialization
            def serialize(obj: Any) -> Any:
                if hasattr(obj, "__str__") and not isinstance(obj, (str, int, float, bool, type(None), list, dict)):
                    return str(obj)
                return obj

            print(json.dumps(results, indent=2, default=serialize))
        else:
            formatted = format_results(results)
            print(formatted)

            if args.save:
                await save_query(conn, query, formatted, results)
                print("\n[Query saved for compound learning]")


def main() -> None:
    """Entry point with asyncio.run() wrapper."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
