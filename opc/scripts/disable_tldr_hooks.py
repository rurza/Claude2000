#!/usr/bin/env python3
"""Disable TLDR hooks from existing Claude Code installation.

This script removes TLDR hooks from your ~/.claude/settings.json file:
- PreToolUse:Read -> tldr-read-enforcer.mjs
- PreToolUse:Grep -> smart-search-router.mjs
- PreToolUse:Task -> tldr-context-inject.mjs
- SessionStart -> session-start-tldr-cache.mjs

USAGE:
    cd opc && python3 scripts/disable_tldr_hooks.py
"""

import json
import sys
from pathlib import Path


def get_global_claude_dir() -> Path:
    """Get the global .claude directory."""
    home = Path.home()
    return home / ".claude"


def strip_tldr_hooks_from_settings(settings_path: Path) -> bool:
    """Remove TLDR hooks from settings.json file.

    Args:
        settings_path: Path to settings.json file

    Returns:
        True if successfully modified, False otherwise
    """
    if not settings_path.exists():
        print(f"❌ Settings file not found: {settings_path}")
        return False

    try:
        settings = json.loads(settings_path.read_text())

        if "hooks" not in settings:
            print("✓ No hooks section found in settings.json")
            return True

        modified = False

        # Strip PreToolUse hooks
        if "PreToolUse" in settings["hooks"]:
            new_pretooluse = []
            for hook_group in settings["hooks"]["PreToolUse"]:
                matcher = hook_group.get("matcher")

                # Remove entire Read hook group (only has tldr-read-enforcer)
                if matcher == "Read":
                    hooks = hook_group.get("hooks", [])
                    if any("tldr-read-enforcer" in h.get("command", "") for h in hooks):
                        print("  - Removing Read hook: tldr-read-enforcer.mjs")
                        modified = True
                        continue

                # Remove entire Grep hook group (only has smart-search-router)
                elif matcher == "Grep":
                    hooks = hook_group.get("hooks", [])
                    if any("smart-search-router" in h.get("command", "") for h in hooks):
                        print("  - Removing Grep hook: smart-search-router.mjs")
                        modified = True
                        continue

                # For Task hooks, remove only tldr-context-inject, keep others
                elif matcher == "Task":
                    hooks = hook_group.get("hooks", [])
                    new_hooks = [
                        h for h in hooks if "tldr-context-inject" not in h.get("command", "")
                    ]
                    if len(new_hooks) != len(hooks):
                        print("  - Removing Task hook: tldr-context-inject.mjs")
                        modified = True
                        hook_group["hooks"] = new_hooks
                    if new_hooks:
                        new_pretooluse.append(hook_group)
                    elif len(hooks) > 0:
                        continue
                    else:
                        new_pretooluse.append(hook_group)
                else:
                    new_pretooluse.append(hook_group)

            settings["hooks"]["PreToolUse"] = new_pretooluse

        # Strip SessionStart hooks
        if "SessionStart" in settings["hooks"]:
            new_sessionstart = []
            for hook_group in settings["hooks"]["SessionStart"]:
                matcher = hook_group.get("matcher", "")

                # For startup|resume matcher, remove tldr-cache hook
                if "startup" in matcher or "resume" in matcher:
                    hooks = hook_group.get("hooks", [])
                    new_hooks = [
                        h
                        for h in hooks
                        if "session-start-tldr-cache" not in h.get("command", "")
                    ]
                    if len(new_hooks) != len(hooks):
                        print("  - Removing SessionStart hook: session-start-tldr-cache.mjs")
                        modified = True
                        hook_group["hooks"] = new_hooks
                    if new_hooks:
                        new_sessionstart.append(hook_group)
                    elif len(hooks) > 0:
                        continue
                    else:
                        new_sessionstart.append(hook_group)
                else:
                    new_sessionstart.append(hook_group)

            settings["hooks"]["SessionStart"] = new_sessionstart

        # Write back if modified
        if modified:
            settings_path.write_text(json.dumps(settings, indent=2))
            print(f"\n✓ TLDR hooks removed from {settings_path}")
        else:
            print("\n✓ No TLDR hooks found (already disabled)")

        return True

    except Exception as e:
        print(f"❌ Error: {e}")
        return False


def main():
    """Main entry point."""
    print("=" * 60)
    print("Disable TLDR Hooks Script")
    print("=" * 60)
    print()

    settings_path = get_global_claude_dir() / "settings.json"
    print(f"Settings file: {settings_path}")
    print()

    if not settings_path.exists():
        print("❌ Settings file not found!")
        print("   Have you run the setup wizard?")
        sys.exit(1)

    print("Removing TLDR hooks...")
    print()

    success = strip_tldr_hooks_from_settings(settings_path)

    if success:
        print()
        print("=" * 60)
        print("✓ Done! Restart your Claude Code session to apply changes.")
        print("=" * 60)
        sys.exit(0)
    else:
        print()
        print("=" * 60)
        print("❌ Failed to disable TLDR hooks")
        print("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()
