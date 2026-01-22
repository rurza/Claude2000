# Session Findings: Database & Installer Review (2026-01-22)

## Overview

Reviewed scripts, hooks, and skills installed by the installer. Cross-referenced with database tables. Found and fixed several issues.

---

## 1. Database Tables

### Current Schema (5 tables)

| Table | Purpose | Status |
|-------|---------|--------|
| `sessions` | Cross-terminal awareness | ✓ In use |
| `file_claims` | Cross-terminal file locking | ✓ In use |
| `core_memory` | Key-value blocks (persona, task, context) | ✓ ADDED (was missing) |
| `archival_memory` | Long-term learnings with embeddings | ✓ In use (0 rows currently) |
| `handoffs` | Session handoffs with embeddings | ✓ In use |

### Finding: `archival_memory` IS Used

**Verified in use by:**
- `opc/scripts/core/recall_learnings.py` - semantic search queries
- `opc/scripts/core/store_learning.py` - storing learnings via memory service
- `opc/scripts/core/db/memory_service_pg.py` - CRUD operations
- Hooks: `session-start-recall.js`, `auto-learning.js`

The table exists but has 0 rows (nothing stored yet). Infrastructure is correctly wired.

### Bug Fixed: Missing `core_memory` Table

**Problem:** `memory_service_pg.py` uses `set_core()`, `get_core()`, etc. which query `core_memory` table, but this table was not defined in the schema.

**Fixed:**
1. Added `core_memory` table definition to `schema/init-schema.sql`
2. Created table directly in running database
3. Updated table count in schema comment (4 → 5 tables)

---

## 2. Folder Renamed: `docker/` → `schema/`

**Problem:** The `docker/` folder was misleading - no Docker is used. It only contained:
- `init-schema.sql` (used by embedded postgres)
- `docker-compose.yml` (unused)

**Fixed:**
1. Renamed folder to `schema/`
2. Deleted `docker-compose.yml` (unused)
3. Deleted `opc/scripts/setup/docker_setup.py` (unused)
4. Updated all references in:
   - `opc/scripts/setup/wizard.py`
   - `opc/scripts/setup/update.py`
   - `docs/ARCHITECTURE.md`
   - `.github/workflows/schema-validation.yml`

---

## 3. Docker References in Rules

**Problem:** `.claude/rules/cross-terminal-db.md` had Docker-based psql commands that don't work (not using Docker).

**Fixed:** Updated to use `psql "$CLAUDE2000_DB_URL"` directly. Fixed both:
- `~/.claude/rules/cross-terminal-db.md` (global)
- `.claude/rules/cross-terminal-db.md` (project)

---

## 4. Script Sync Gap (UNFIXED)

### Problem

The update script (`opc/scripts/setup/update.py`) syncs files to `~/.claude/` but has gaps:

**What gets synced:**
```python
checks = [
    ("hooks/src", ..., {".ts"}),           # TypeScript hooks
    ("skills", ..., {".md"}),              # Skill definitions
    ("rules", ..., {".md"}),               # Rules
    ("agents", ..., {".md", ".yaml"}),     # Agent definitions
    ("scripts/core", ..., {".py"}),        # Python scripts only
    ("scripts/mcp", ..., {".py"}),         # Python scripts only
]
```

**What does NOT get synced:**
- Shell scripts (`.sh` files) in `.claude/scripts/`
  - `generate-reasoning.sh`
  - `aggregate-reasoning.sh`
  - `search-reasoning.sh`
  - `status.sh`
  - `phase-progress-status.sh`
  - `get-resources.sh`
  - `agent-animation.sh`

### Consequence

Skills reference these scripts via `$CLAUDE_CC_DIR/.claude/scripts/generate-reasoning.sh` but:
1. `CLAUDE_CC_DIR` is not set
2. Even if set, the scripts don't exist at `~/.claude/scripts/`

**Affected skills:**
- `commit` - references `generate-reasoning.sh`
- `describe_pr` - references `aggregate-reasoning.sh`
- `git-commits` - references `generate-reasoning.sh`
- `recall-reasoning` - references `search-reasoning.sh`
- `tldr-stats` - references `tldr_stats.py`
- `skill-developer` - various references

### Recommended Fix

1. Add shell scripts to the sync list in `update.py`:
   ```python
   ("scripts", claude_dir / "scripts", {".sh"}),
   ```

2. Update skills to reference `~/.claude/scripts/` directly instead of `$CLAUDE_CC_DIR/.claude/scripts/`

3. Remove `CLAUDE_CC_DIR` from skills (non-standard variable)

---

## 5. Environment Variable Confusion

### Current State

| Variable | Value | Used By |
|----------|-------|---------|
| `CLAUDE_2000_DIR` | `~/.claude/claude2000` | Rules (dynamic-recall.md, etc.) |
| `CLAUDE_CC_DIR` | Not set | Skills (commit, describe_pr, etc.) |
| `CLAUDE2000_DB_URL` | Set correctly | Database connections |

### Problem

Two different naming conventions:
- Rules use `CLAUDE_2000_DIR` (set, works)
- Skills use `CLAUDE_CC_DIR` (not set, broken)

### Official Claude Code Structure

Per official docs, there is **no standard env var** for custom scripts. The convention is:
- User-scoped assets go in `~/.claude/`
- Project-scoped assets go in `.claude/`

### Recommended Fix

Standardize on file paths, not environment variables:
- Skills should use `~/.claude/scripts/script-name.sh`
- This requires the update script to sync shell scripts to that location

---

## 6. Directory Structure Summary

### What Exists

```
~/.claude/                          # Claude Code user directory
├── settings.json                   # User settings
├── rules/                          # User rules (synced)
├── skills/                         # User skills (synced)
├── agents/                         # User agents (synced)
├── hooks/                          # Hooks (synced, with dist/)
├── scripts/                        # Scripts (PARTIALLY synced - .py only)
│   ├── core/                       # Python scripts (synced)
│   └── mcp/                        # Python scripts (synced)
│   └── *.sh                        # Shell scripts (NOT synced!)
├── pgdata/                         # Embedded postgres data
├── pgserver-venv/                  # Postgres binaries
└── claude2000/                     # OPC runtime installation
    ├── scripts/                    # Full OPC scripts
    ├── .venv/                      # Python venv
    └── .env                        # Environment config
```

### Project Repo Structure

```
Claude2000/
├── .claude/
│   ├── scripts/                    # Source scripts (some not synced)
│   │   ├── core/                   # Python (synced)
│   │   ├── generate-reasoning.sh  # NOT synced
│   │   └── ...
│   ├── skills/
│   ├── rules/
│   ├── hooks/
│   └── agents/
├── schema/
│   └── init-schema.sql             # Database schema
└── opc/
    └── scripts/
        └── setup/
            ├── wizard.py           # Initial setup
            └── update.py           # Incremental updates
```

---

## 7. Commits Made

```
500c8ae refactor: rename docker/ to schema/, add missing core_memory table
```

**Changes:**
- Renamed `docker/` → `schema/`
- Added `core_memory` table to schema
- Removed unused `docker_setup.py` and `docker-compose.yml`
- Updated all path references
- Fixed `cross-terminal-db.md` Docker commands

---

## 8. Outstanding Issues

### Must Fix
1. **Shell scripts not synced** - Skills that reference `.sh` scripts are broken
2. **`CLAUDE_CC_DIR` not set** - Skills reference undefined variable

### Recommended Actions
1. Add `.sh` extension to update.py sync
2. Update skills to use `~/.claude/scripts/` paths directly
3. Remove `CLAUDE_CC_DIR` references from all skills
4. Consider consolidating `CLAUDE_2000_DIR` and script paths

---

## 9. Database Connection

**Working configuration:**
- Embedded PostgreSQL on port 5433
- Connection via `CLAUDE2000_DB_URL` environment variable
- No Docker required

**Query pattern:**
```bash
psql "$CLAUDE2000_DB_URL" -c "SELECT * FROM archival_memory"
```
