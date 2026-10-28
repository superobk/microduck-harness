#!/usr/bin/env python3
"""Launch the pinned calibration plugin dashboard on localhost:8768."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integrations.microduck_tools.bridge import main

if __name__ == "__main__":
    sys.exit(main())
