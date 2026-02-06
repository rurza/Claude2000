#!/bin/bash
# Sync Claude2000 scripts from dev repo to self-contained location
#
# Usage: ./sync_to_self_contained.sh
#
# Syncs from: <repo>/opc/ (auto-detected from script location)
# Syncs to:   ~/.claude/claude2000/

set -e

# Resolve dev dir from this script's location (scripts/setup/ -> opc root)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEV_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SELF_CONTAINED="$HOME/.claude/claude2000"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${YELLOW}Claude2000 Sync Script${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Source: $DEV_DIR"
echo "Target: $SELF_CONTAINED"
echo ""

# Check source exists
if [[ ! -d "$DEV_DIR" ]]; then
    echo -e "${RED}Error: Dev directory not found: $DEV_DIR${NC}"
    exit 1
fi

# Create target if it doesn't exist
if [[ ! -d "$SELF_CONTAINED" ]]; then
    echo -e "${YELLOW}Creating self-contained directory...${NC}"
    mkdir -p "$SELF_CONTAINED"
fi

# Sync scripts directory (main scripts)
echo -e "${GREEN}Syncing scripts/...${NC}"
rsync -av --delete \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.DS_Store' \
    "$DEV_DIR/scripts/" "$SELF_CONTAINED/scripts/"

# Sync src directory (runtime, etc)
if [[ -d "$DEV_DIR/src" ]]; then
    echo -e "${GREEN}Syncing src/...${NC}"
    rsync -av --delete \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.DS_Store' \
        "$DEV_DIR/src/" "$SELF_CONTAINED/src/"
fi

# Sync pyproject.toml if changed
if [[ -f "$DEV_DIR/pyproject.toml" ]]; then
    if ! diff -q "$DEV_DIR/pyproject.toml" "$SELF_CONTAINED/pyproject.toml" > /dev/null 2>&1; then
        echo -e "${YELLOW}pyproject.toml changed - syncing...${NC}"
        cp "$DEV_DIR/pyproject.toml" "$SELF_CONTAINED/pyproject.toml"
        echo -e "${YELLOW}Run 'uv sync' in $SELF_CONTAINED to update venv${NC}"
    fi
fi

# Preserve .env (don't overwrite user config)
if [[ ! -f "$SELF_CONTAINED/.env" ]]; then
    if [[ -f "$DEV_DIR/.env.example" ]]; then
        echo -e "${YELLOW}Creating .env from example...${NC}"
        cp "$DEV_DIR/.env.example" "$SELF_CONTAINED/.env"
    fi
fi

# Create .venv if it doesn't exist
if [[ ! -d "$SELF_CONTAINED/.venv" ]]; then
    echo -e "${YELLOW}Creating virtual environment...${NC}"
    cd "$SELF_CONTAINED"
    uv venv
    echo -e "${GREEN}Installing dependencies...${NC}"
    uv sync
fi

echo ""
echo -e "${GREEN}Sync complete!${NC}"
echo ""
echo "Self-contained Claude2000 is now at: ~/.claude/claude2000"
echo ""
echo "Test with:"
echo "  cd ~/.claude/claude2000 && .venv/bin/python scripts/core/recall_learnings.py --query 'test' --k 3"
