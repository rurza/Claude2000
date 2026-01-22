# Prefer TLDR Over Grep

**For code searches, use `tldr search` instead of the Grep tool.**

## Why

- `tldr search` provides 95% token savings
- Returns structured results with file:line references
- Integrates with call graph and code structure analysis

## Pattern

| Instead of | Use |
|------------|-----|
| `Grep` tool with pattern | `tldr search "pattern" .` via Bash |
| `Grep` for function names | `tldr search "function_name" .` |
| `Grep` for class definitions | `tldr search "ClassName" .` |

## When to Use Grep

Only use the Grep tool when:
- You need regex features tldr doesn't support
- tldr search returns no results and you need fuzzy matching
- Searching non-code files (logs, configs, etc.)

## Example

```bash
# Good - use tldr for code searches
tldr search "authenticate" .

# Only if tldr fails, fall back to Grep tool
```
