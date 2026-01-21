# Claude2000 Installation Guide

## Prerequisites

- **Python 3.11+** - Check with `python3 --version`
- **uv** - Install with: `curl -LsSf https://astral.sh/uv/install.sh | sh`

No Docker required! Claude2000 uses embedded PostgreSQL by default.

## Fresh Install

1. **Clone the repository**:
   ```bash
   git clone https://github.com/rurza/Claude2000.git
   cd Claude2000
   ```

2. **Run the installer**:
   ```bash
   uv run python install.py
   ```

3. **Follow the wizard** (12 steps):
   - Step 0: Backup existing ~/.claude
   - Step 1: Check system requirements
   - Step 2: Database configuration (embedded recommended)
   - Step 3: Embedding configuration
   - Step 4: API keys (optional)
   - Step 5: Generate configuration
   - Step 6: Database setup (migrations)
   - Step 7: Claude Code integration
   - Step 8: Math features (optional)
   - Step 9: TLDR code analysis tool
   - Step 10: Diagnostics tools
   - Step 11: Loogle (optional, for theorem proving)
   - Step 12: Install scripts to ~/.claude/claude2000/

4. **Start Claude Code**:
   ```bash
   claude
   ```

## Updating

```bash
cd Claude2000
uv run python update.py
```

## Uninstalling

```bash
cd Claude2000/opc
uv run python -m scripts.setup.wizard --uninstall
```

## Database Options

| Mode | Description | Use Case |
|------|-------------|----------|
| **embedded** | Embedded PostgreSQL (default) | Recommended for most users |
| **sqlite** | SQLite fallback | Simplest, limited cross-terminal features |

## Environment Variables

After installation, these are set in your shell config:

| Variable | Purpose |
|----------|---------|
| `CLAUDE_2000_DIR` | Path to ~/.claude/claude2000 (scripts location) |
| `CLAUDE_OPC_DIR` | Alias for backwards compatibility |

## Troubleshooting

### Embedded PostgreSQL fails to start

Try SQLite mode as fallback:
```bash
# During install, select "sqlite" when prompted for database mode
```

### Missing prerequisites

```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Verify Python
python3 --version  # Should be 3.11+
```

## What's Installed

- `~/.claude/hooks/` - Session hooks for memory, context, etc.
- `~/.claude/skills/` - 109+ skills for workflows
- `~/.claude/agents/` - 32 specialized agents
- `~/.claude/rules/` - Coding rules and patterns
- `~/.claude/claude2000/` - Core scripts and runtime
