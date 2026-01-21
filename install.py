#!/usr/bin/env python3
"""Claude2000 installer - run with: python install.py

This is the main entry point for installing Claude2000.
It delegates to the setup wizard in opc/scripts/setup/wizard.py.
"""
import asyncio
import sys
from pathlib import Path

# Add opc to path so we can import from scripts.setup
sys.path.insert(0, str(Path(__file__).parent / "opc"))

from scripts.setup.wizard import main


if __name__ == "__main__":
    asyncio.run(main())
