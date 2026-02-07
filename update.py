#!/usr/bin/env python3
"""Claude2000 updater - run with: uv run python update.py

This is the main entry point for updating Claude2000.
It delegates to the update script in opc/scripts/setup/update.py.

Bootstraps itself to Python 3.12+ via uv if the current interpreter is too old.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

MINIMUM_PYTHON = (3, 12)


def _bootstrap():
    """Re-exec this script under Python 3.12+ via uv."""
    uv = shutil.which("uv")
    if uv is None:
        print(
            "Error: 'uv' is not installed. Install it first:\n"
            "  curl -LsSf https://astral.sh/uv/install.sh | sh"
        )
        sys.exit(1)

    print(
        "Python %d.%d detected — project requires %d.%d+."
        % (sys.version_info[0], sys.version_info[1], MINIMUM_PYTHON[0], MINIMUM_PYTHON[1])
    )
    print("Bootstrapping via uv (will download Python %d.%d if needed)...\n" % MINIMUM_PYTHON)

    script = str(Path(__file__).resolve())
    env = dict(os.environ, _CLAUDE2000_BOOTSTRAPPED="1")
    result = subprocess.run(
        [uv, "run", "--project", str(Path(script).parent / "opc"),
         "--python", "%d.%d" % MINIMUM_PYTHON, "python", script]
        + sys.argv[1:],
        env=env,
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    if sys.version_info < MINIMUM_PYTHON and not os.environ.get("_CLAUDE2000_BOOTSTRAPPED"):
        _bootstrap()

    sys.path.insert(0, str(Path(__file__).parent / "opc"))
    from scripts.setup.update import run_update

    run_update()
