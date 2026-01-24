#!/usr/bin/env python3
"""Mark handoff outcomes in PostgreSQL.

Uses asyncpg via postgres_pool.py for async database access.
NO SQLITE - PostgreSQL only.

Usage:
    python artifact_mark.py --handoff <id> --outcome SUCCEEDED
    python artifact_mark.py --latest --outcome PARTIAL_PLUS
    python artifact_mark.py --get-latest-id
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Ensure scripts package is importable regardless of cwd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Load environment from .env file
try:
    from dotenv import load_dotenv
    env_path = os.path.expanduser("~/.claude/claude2000/.env")
    if os.path.exists(env_path):
        load_dotenv(env_path)
    else:
        # Also try ~/.claude/.env
        env_path = os.path.expanduser("~/.claude/.env")
        if os.path.exists(env_path):
            load_dotenv(env_path)
except ImportError:
    pass  # dotenv not required if env vars already set


def get_postgres_url() -> str | None:
    """Get PostgreSQL URL from environment."""
    return os.environ.get("CLAUDE2000_DB_URL")


# =============================================================================
# ASYNC POSTGRESQL OPERATIONS
# =============================================================================

async def get_latest_id() -> str | None:
    """Get latest handoff ID from PostgreSQL."""
    from scripts.core.db.postgres_pool import get_connection

    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT id::text FROM handoffs ORDER BY indexed_at DESC LIMIT 1"
        )
        return row["id"] if row else None


async def get_handoff(handoff_id: str) -> tuple | None:
    """Get handoff by ID from PostgreSQL."""
    from scripts.core.db.postgres_pool import get_connection

    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT id::text, session_name, goal FROM handoffs WHERE id::text LIKE $1",
            f"{handoff_id}%",
        )
        return (row["id"], row["session_name"], row["goal"]) if row else None


async def update_outcome(handoff_id: str, outcome: str, notes: str) -> bool:
    """Update handoff outcome in PostgreSQL."""
    from scripts.core.db.postgres_pool import get_connection

    async with get_connection() as conn:
        result = await conn.execute(
            "UPDATE handoffs SET outcome = $1, outcome_notes = $2 WHERE id::text LIKE $3",
            outcome,
            notes,
            f"{handoff_id}%",
        )
        # asyncpg returns "UPDATE N" where N is rows affected
        return result != "UPDATE 0"


async def list_recent() -> list:
    """List recent handoffs from PostgreSQL."""
    from scripts.core.db.postgres_pool import get_connection

    async with get_connection() as conn:
        rows = await conn.fetch(
            "SELECT id::text, session_name, goal FROM handoffs ORDER BY indexed_at DESC LIMIT 10"
        )
        return [(row["id"], row["session_name"], row["goal"]) for row in rows]


# =============================================================================
# MAIN
# =============================================================================

async def async_main() -> int:
    """Async main function."""
    parser = argparse.ArgumentParser(
        description="Mark handoff outcome (PostgreSQL only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Mark specific handoff
  %(prog)s --handoff abc123 --outcome SUCCEEDED

  # Mark latest handoff
  %(prog)s --latest --outcome SUCCEEDED

  # Get latest handoff ID (for scripts)
  %(prog)s --get-latest-id
""",
    )
    parser.add_argument("--handoff", help="Handoff ID to mark")
    parser.add_argument("--latest", action="store_true", help="Mark the most recent handoff")
    parser.add_argument("--get-latest-id", action="store_true", help="Print latest handoff ID and exit")
    parser.add_argument(
        "--outcome",
        choices=["SUCCEEDED", "PARTIAL_PLUS", "PARTIAL_MINUS", "FAILED"],
        help="Outcome of the handoff",
    )
    parser.add_argument("--notes", default="", help="Optional notes about the outcome")

    args = parser.parse_args()

    # Check PostgreSQL URL is configured
    if not get_postgres_url():
        print("Error: CLAUDE2000_DB_URL not set", file=sys.stderr)
        print("Run the wizard: cd ~/.claude && uv run python -m scripts.setup.wizard", file=sys.stderr)
        return 1

    # Mode: get-latest-id
    if args.get_latest_id:
        latest = await get_latest_id()
        if latest:
            print(latest)
            return 0
        print("No handoffs found", file=sys.stderr)
        return 1

    # Mode: mark handoff
    if not args.outcome:
        parser.error("--outcome is required unless using --get-latest-id")

    # Determine which handoff to mark
    if args.latest:
        handoff_id = await get_latest_id()
        if not handoff_id:
            print("Error: No handoffs found in database")
            return 1
    elif args.handoff:
        handoff_id = args.handoff
    else:
        parser.error("Either --handoff ID or --latest is required")

    # Check if handoff exists
    handoff = await get_handoff(handoff_id)
    if not handoff:
        print(f"Error: Handoff not found: {handoff_id}")
        print("\nDatabase: PostgreSQL")
        print("\nAvailable handoffs:")
        for row in await list_recent():
            summary = row[2][:50] + "..." if row[2] and len(row[2]) > 50 else (row[2] or "(no summary)")
            print(f"  {row[0][:12]}: {row[1]} - {summary}")
        return 1

    # Update the handoff
    if not await update_outcome(handoff_id, args.outcome, args.notes):
        print(f"Error: Failed to update handoff: {handoff_id}")
        return 1

    # Show confirmation
    print(f"✓ Marked handoff as {args.outcome}")
    print("  Database: PostgreSQL")
    print(f"  ID: {handoff[0]}")
    print(f"  Session: {handoff[1]}")
    if handoff[2]:
        summary = handoff[2][:80] + "..." if len(handoff[2]) > 80 else handoff[2]
        print(f"  Summary: {summary}")
    if args.notes:
        print(f"  Notes: {args.notes}")

    return 0


def main() -> int:
    """Entry point with asyncio.run() wrapper."""
    return asyncio.run(async_main())


if __name__ == "__main__":
    sys.exit(main())
