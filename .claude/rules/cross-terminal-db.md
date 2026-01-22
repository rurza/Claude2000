# Cross-Terminal Coordination Database

## Connection Details

```bash
# Connection via environment variable (embedded postgres on port 5433)
# CLAUDE2000_DB_URL is set in ~/.claude/.env

# Query directly via psql
psql "$CLAUDE2000_DB_URL" -c "SQL"
```

## Quick Queries

```bash
# Active sessions (last 5 min)
psql "$CLAUDE2000_DB_URL" -c \
  "SELECT id, project, working_on, last_heartbeat FROM sessions WHERE last_heartbeat > NOW() - INTERVAL '5 minutes';"

# All sessions
psql "$CLAUDE2000_DB_URL" -c \
  "SELECT id, project, working_on, last_heartbeat FROM sessions ORDER BY last_heartbeat DESC LIMIT 10;"

# File claims
psql "$CLAUDE2000_DB_URL" -c \
  "SELECT file_path, session_id, claimed_at FROM file_claims ORDER BY claimed_at DESC LIMIT 10;"
```

## Testing Cross-Terminal Coordination

1. **Terminal 1**: Run `claude` - registers session on start
2. **Terminal 2**: Run `claude` - should see Terminal 1 in peer sessions message
3. **Conflict test**: Have both edit the same file - Terminal 2 should get a warning

## Tables

| Table | Purpose |
|-------|---------|
| `sessions` | Cross-session awareness |
| `file_claims` | File locking/conflict detection |
| `core_memory` | Key-value blocks (persona, task, context) |
| `archival_memory` | Long-term learnings with embeddings |
| `handoffs` | Session handoffs and task completions |
