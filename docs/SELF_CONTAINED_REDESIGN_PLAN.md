# Self-Contained Claude2000 Redesign Plan

## Goal

Make Claude2000 fully self-contained within `~/.claude/`. No custom environment variables should be needed. Everything required should be installed/synced automatically.

---

## Current State Analysis

### Files to Review

#### Installer/Setup (`opc/scripts/setup/`)
- [ ] `wizard.py` - Main installer (too many questions)
- [ ] `update.py` - Incremental updater
- [ ] `embedded_postgres.py` - Database setup
- [ ] `personalization.py` - User preferences
- [ ] `math_features.py` - Math stack setup
- [ ] `claude_integration.py` - Claude Code integration

#### Scripts (`opc/scripts/core/`)
- [ ] `recall_learnings.py` - Memory recall
- [ ] `store_learning.py` - Memory storage
- [ ] `artifact_index.py` - Artifact indexing
- [ ] `memory_daemon.py` - Background memory service
- [ ] `db/` - Database modules

#### Shell Scripts (`.claude/scripts/`)
- [ ] `generate-reasoning.sh`
- [ ] `aggregate-reasoning.sh`
- [ ] `search-reasoning.sh`
- [ ] `status.sh`
- [ ] Others

#### Skills (`.claude/skills/`)
- [ ] All skills referencing external paths or env vars

#### Rules (`.claude/rules/`)
- [ ] `dynamic-recall.md` - Uses `$CLAUDE_2000_DIR`
- [ ] `cross-terminal-db.md` - Database access
- [ ] Others

#### Hooks (`.claude/hooks/`)
- [ ] All hooks that reference external paths

---

## Design Principles

### 1. Everything Lives in `~/.claude/`
```
~/.claude/
├── settings.json          # Claude Code settings
├── .env                   # Environment variables (DB URL, API keys)
├── scripts/               # All executable scripts
├── skills/                # Skill definitions
├── rules/                 # Rule definitions
├── agents/                # Agent definitions
├── hooks/                 # Hook scripts
├── pgdata/                # Embedded postgres data
├── pgserver-venv/         # Postgres binaries
└── cache/                 # Caches (embeddings, etc.)
```

### 2. No Custom Environment Variables Required
- Remove `CLAUDE_CC_DIR` (DONE)
- Remove `CLAUDE_2000_DIR` dependency from rules (DONE)
- Use `~/.claude/` paths directly everywhere (DONE)

### 3. Default-First Installation
- Installer uses sensible defaults
- Only asks critical questions:
  - Backup existing installation? (yes/no)
  - Installation mode? (fresh/update)
  - Reindex frequency? (for artifact indexing)
- Everything else uses defaults:
  - Embedded postgres: YES (default)
  - TLDR: YES (default)
  - Local embeddings: YES (default)
  - Default paths: `~/.claude/`

### 4. Idempotent Updates
- `update.py` can be run anytime safely
- Schema migrations are additive (IF NOT EXISTS)
- File syncs preserve user customizations

---

## Action Items

### Phase 1: Environment Variable Cleanup ✅ DONE
- [x] Remove `$CLAUDE_2000_DIR` from all rules
- [x] Update `dynamic-recall.md` to use `~/.claude/` paths
- [x] Update any remaining skills with env var references
- [x] Search for and fix any `$CLAUDE_` references

### Phase 2: Installer Simplification ✅ DONE
- [x] Refactor `wizard.py` to use defaults
- [x] Only prompt for: backup, mode, reindex frequency
- [x] Auto-install: embedded postgres, TLDR, local embeddings
- [x] Remove interactive prompts for feature toggles

### Phase 3: Path Standardization ✅ DONE
- [x] All scripts use `~/.claude/` base path
- [x] All database connections use `CLAUDE2000_DB_URL` (from `~/.claude/.env`)
- [x] All hooks reference `~/.claude/` paths (verified)
- [x] Shell scripts use `~/.claude/` or relative paths (verified)

### Phase 4: Documentation Update
- [ ] Update CLAUDE.md with new structure
- [ ] Update all docs referencing old paths
- [ ] Create migration guide for existing users

---

## Installer Redesign Specification

### Current Flow (Too Many Questions)
```
1. Check prerequisites
2. Ask: Docker or embedded postgres?
3. Ask: Enable math features?
4. Ask: Enable local embeddings?
5. Ask: Configure embedding provider?
6. Ask: Reindex frequency?
7. Ask: Custom paths?
... etc
```

### New Flow (Minimal Questions)
```
1. Check prerequisites (silent)
2. Detect existing installation
3. IF existing:
   - Ask: "Backup existing ~/.claude? [Y/n]"
   - Ask: "Fresh install or update? [update/fresh]"
4. Ask: "Artifact reindex frequency? [daily/weekly/manual]"
5. Auto-install everything with defaults:
   - Embedded postgres on port 5433
   - TLDR CLI
   - Local embeddings (Qwen3-Embedding-0.6B)
   - All skills, rules, agents, hooks
6. Show summary and done
```

### Default Values
| Setting | Default | Notes |
|---------|---------|-------|
| Database | Embedded postgres | Port 5433, pgdata in ~/.claude/ |
| Embeddings | Local (Qwen3) | 1024 dimensions |
| TLDR | Install | Via uv pip |
| Scripts | Sync all | .py and .sh |
| Schema | Auto-apply | IF NOT EXISTS safe |

---

## Files Requiring Changes

### High Priority ✅ DONE
1. ~~`opc/scripts/setup/wizard.py` - Simplify to minimal prompts~~
2. ~~`.claude/rules/dynamic-recall.md` - Remove `$CLAUDE_2000_DIR`~~
3. ~~`.claude/rules/agent-memory-recall.md` - Remove env var~~
4. ~~Any remaining `$CLAUDE_` env var references~~

### Medium Priority
1. `opc/scripts/setup/update.py` - Fix schema check (port 5432 vs 5433)
2. Documentation updates

### Low Priority
1. Legacy compatibility shims (if needed)

---

## Validation Checklist

After redesign, verify:
- [ ] Fresh install works with no questions beyond backup/mode/reindex
- [ ] `~/.claude/` contains everything needed
- [ ] No environment variables required (except PATH)
- [ ] Update script works on existing installation
- [ ] All skills work without env vars
- [ ] Database connection works
- [ ] Memory recall/store works
- [ ] Hooks fire correctly

---

## Next Session Prompt

```
Continue the self-contained redesign of Claude2000:

1. Review and update these files to remove $CLAUDE_2000_DIR:
   - .claude/rules/dynamic-recall.md
   - .claude/rules/agent-memory-recall.md

2. Simplify wizard.py installer:
   - Only ask: backup?, mode?, reindex frequency?
   - Use defaults for everything else
   - Auto-install: embedded postgres, TLDR, local embeddings

3. Fix update.py schema check (uses wrong port)

4. Test the simplified installation flow

Reference: docs/SELF_CONTAINED_REDESIGN_PLAN.md
```
