# TLDR CLI - Code Analysis Tool

You have `tldr` available via the project wrapper at `~/.claude/claude2000/scripts/tldr-cli`.

**Always invoke as:** `~/.claude/claude2000/scripts/tldr-cli` (not bare `tldr`).

## Commands

```bash
# Core analysis
~/.claude/claude2000/scripts/tldr-cli tree [path]                    # File tree
~/.claude/claude2000/scripts/tldr-cli structure [path] --lang <lang> # Code structure (codemaps)
~/.claude/claude2000/scripts/tldr-cli search <pattern> [path]        # Search files
~/.claude/claude2000/scripts/tldr-cli extract <file>                 # Full file info
~/.claude/claude2000/scripts/tldr-cli context <entry> --project .    # LLM-ready context

# Flow analysis
~/.claude/claude2000/scripts/tldr-cli cfg <file> <function>          # Control flow graph
~/.claude/claude2000/scripts/tldr-cli dfg <file> <function>          # Data flow graph
~/.claude/claude2000/scripts/tldr-cli slice <file> <func> <line>     # Program slice
~/.claude/claude2000/scripts/tldr-cli calls [path]                   # Cross-file call graph

# Codebase analysis
~/.claude/claude2000/scripts/tldr-cli impact <func> [path]           # Who calls this function? (reverse call graph)
~/.claude/claude2000/scripts/tldr-cli dead [path]                    # Find unreachable/dead code
~/.claude/claude2000/scripts/tldr-cli arch [path]                    # Detect architectural layers

# Import analysis
~/.claude/claude2000/scripts/tldr-cli imports <file>                 # Parse imports from a file
~/.claude/claude2000/scripts/tldr-cli importers <module> [path]      # Find all files that import a module

# Quality & testing
~/.claude/claude2000/scripts/tldr-cli diagnostics <file|path>        # Type check + lint (pyright/ruff)
~/.claude/claude2000/scripts/tldr-cli change-impact [files...]       # Find tests affected by changes
```

## When to Use

- **Before reading files**: Run `~/.claude/claude2000/scripts/tldr-cli structure .` to see what exists
- **Finding code**: Use `~/.claude/claude2000/scripts/tldr-cli search "pattern"` instead of grep for structured results
- **Understanding functions**: Use `~/.claude/claude2000/scripts/tldr-cli cfg` for complexity, `dfg` for data flow
- **Debugging**: Use `~/.claude/claude2000/scripts/tldr-cli slice file.py func 42` to find what affects line 42
- **Context for tasks**: Use `~/.claude/claude2000/scripts/tldr-cli context entry_point` to get relevant code
- **Impact analysis**: Use `~/.claude/claude2000/scripts/tldr-cli impact func_name` before refactoring to see what would break
- **Dead code**: Use `~/.claude/claude2000/scripts/tldr-cli dead src/` to find unused functions for cleanup
- **Architecture**: Use `~/.claude/claude2000/scripts/tldr-cli arch src/` to understand layer structure
- **Import tracking**: Use `~/.claude/claude2000/scripts/tldr-cli imports file.py` to see what a file imports
- **Reverse imports**: Use `~/.claude/claude2000/scripts/tldr-cli importers module_name src/` to find who imports a module
- **Before tests**: Use `~/.claude/claude2000/scripts/tldr-cli diagnostics .` to catch type errors before running tests
- **Selective testing**: Use `~/.claude/claude2000/scripts/tldr-cli change-impact` to run only affected tests

## Languages

Supports: `python`, `typescript`, `go`, `rust`

## Example Workflow

```bash
# 1. See project structure
~/.claude/claude2000/scripts/tldr-cli tree src/ --ext .py

# 2. Find relevant code
~/.claude/claude2000/scripts/tldr-cli search "process_data" src/

# 3. Get context for a function
~/.claude/claude2000/scripts/tldr-cli context process_data --project src/ --depth 2

# 4. Understand control flow
~/.claude/claude2000/scripts/tldr-cli cfg src/processor.py process_data

# 5. Before refactoring - check impact
~/.claude/claude2000/scripts/tldr-cli impact process_data src/ --depth 3

# 6. Find dead code to clean up
~/.claude/claude2000/scripts/tldr-cli dead src/ --entry main cli
```

## Codebase Analysis Commands

### Impact Analysis
```bash
~/.claude/claude2000/scripts/tldr-cli impact <function> [path] --depth N --file <filter>
```
Shows reverse call graph - all functions that call the target. Useful before refactoring.

### Dead Code Detection
```bash
~/.claude/claude2000/scripts/tldr-cli dead [path] --entry <patterns>
```
Finds functions never called (excluding entry points like main, test_, etc.)

### Architecture Detection
```bash
~/.claude/claude2000/scripts/tldr-cli arch [path]
```
Analyzes call patterns to detect:
- Entry layer (controllers/handlers)
- Middle layer (services)
- Leaf layer (utilities)
- Circular dependencies

### Import Analysis
```bash
~/.claude/claude2000/scripts/tldr-cli imports <file> [--lang python]
```
Parses all import statements from a file. Returns JSON with module names, imported names, aliases.

### Reverse Import Lookup
```bash
~/.claude/claude2000/scripts/tldr-cli importers <module> [path] [--lang python]
```
Finds all files that import a given module. Complements `tldr impact` which tracks function *calls* - this tracks *imports*.

### Diagnostics (Type Check + Lint)
```bash
~/.claude/claude2000/scripts/tldr-cli diagnostics <file>              # Single file
~/.claude/claude2000/scripts/tldr-cli diagnostics . --project         # Whole project
~/.claude/claude2000/scripts/tldr-cli diagnostics src/ --format text  # Human-readable output
~/.claude/claude2000/scripts/tldr-cli diagnostics src/ --no-lint      # Type check only
```
Runs pyright (types) + ruff (lint) and returns structured errors. Use before tests to catch type errors early.

### Change Impact (Selective Testing)
```bash
~/.claude/claude2000/scripts/tldr-cli change-impact                   # Auto-detect (session/git)
~/.claude/claude2000/scripts/tldr-cli change-impact src/foo.py        # Explicit files
~/.claude/claude2000/scripts/tldr-cli change-impact --session         # Session-modified files
~/.claude/claude2000/scripts/tldr-cli change-impact --git             # Git diff files
~/.claude/claude2000/scripts/tldr-cli change-impact --run             # Actually run affected tests
```
Finds which tests to run based on changed code. Uses call graph + import analysis.

## Output

All commands output JSON (except `context` which outputs LLM-ready text, `diagnostics --format text` for human output).
