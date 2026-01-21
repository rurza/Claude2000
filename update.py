#!/usr/bin/env python3
"""Claude2000 updater - run with: python update.py

This is the main entry point for updating Claude2000.
It delegates to the update script in opc/scripts/setup/update.py.
"""
import sys
from pathlib import Path

# Add opc to path so we can import from scripts.setup
sys.path.insert(0, str(Path(__file__).parent / "opc"))

from scripts.setup.update import run_update


if __name__ == "__main__":
    run_update()
