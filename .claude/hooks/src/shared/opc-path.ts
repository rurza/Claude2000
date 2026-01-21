/**
 * Cross-platform Claude2000 directory resolution for hooks.
 *
 * Supports running Claude Code in any directory by:
 * 1. Checking CLAUDE_2000_DIR environment variable (primary)
 * 2. Checking CLAUDE_OPC_DIR environment variable (backwards compat)
 * 3. Checking ~/.claude/claude2000 (new install location)
 * 4. Falling back to ${CLAUDE_PROJECT_DIR}/opc (dev mode)
 * 5. Gracefully degrading if none exist
 */

import { existsSync } from 'fs';
import { join } from 'path';

/**
 * Get the Claude2000/OPC directory path, or null if not available.
 *
 * Resolution order:
 * 1. CLAUDE_2000_DIR env var (new primary location)
 * 2. CLAUDE_OPC_DIR env var (backwards compatibility)
 * 3. ~/.claude/claude2000 (new install location)
 * 4. ${CLAUDE_PROJECT_DIR}/opc (dev mode - running within CC project)
 * 5. ${CWD}/opc (fallback)
 *
 * @returns Path to scripts directory, or null if not found
 */
export function getOpcDir(): string | null {
  // 1. Try CLAUDE_2000_DIR env var (new primary)
  const env2000Dir = process.env.CLAUDE_2000_DIR;
  if (env2000Dir && existsSync(env2000Dir)) {
    return env2000Dir;
  }

  // 2. Try CLAUDE_OPC_DIR env var (backwards compatibility)
  const envOpcDir = process.env.CLAUDE_OPC_DIR;
  if (envOpcDir && existsSync(envOpcDir)) {
    return envOpcDir;
  }

  // 3. Try new install location ~/.claude/claude2000
  const homeDir = process.env.HOME || process.env.USERPROFILE || '';
  if (homeDir) {
    const claude2000Dir = join(homeDir, '.claude', 'claude2000');
    const claude2000Scripts = join(claude2000Dir, 'scripts', 'core');
    if (existsSync(claude2000Scripts)) {
      return claude2000Dir;
    }
  }

  // 4. Try project-relative path (dev mode)
  const projectDir = process.env.CLAUDE_PROJECT_DIR || process.cwd();
  const localOpc = join(projectDir, 'opc');
  if (existsSync(localOpc)) {
    return localOpc;
  }

  // 5. Not available
  return null;
}

/**
 * Get OPC directory or exit gracefully if not available.
 *
 * Use this in hooks that require OPC infrastructure.
 * If OPC is not available, outputs {"result": "continue"} and exits,
 * allowing the hook to be a no-op in non-CC projects.
 *
 * @returns Path to opc directory (never null - exits if not found)
 */
export function requireOpcDir(): string {
  const opcDir = getOpcDir();
  if (!opcDir) {
    // Graceful degradation - hook becomes no-op
    console.log(JSON.stringify({ result: "continue" }));
    process.exit(0);
  }
  return opcDir;
}

/**
 * Check if OPC infrastructure is available.
 *
 * Use this for optional OPC features that should silently skip
 * when running outside a Continuous-Claude environment.
 *
 * @returns true if OPC directory exists and is accessible
 */
export function hasOpcDir(): boolean {
  return getOpcDir() !== null;
}
