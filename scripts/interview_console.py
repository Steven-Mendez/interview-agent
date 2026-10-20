#!/usr/bin/env python3
"""Interactive, real-model interview using the app's planning and decision system.

uv run python scripts/interview_console.py --resume cv.md --job-offer oferta.txt
uv run python scripts/interview_console.py --demo --length short
uv run python scripts/interview_console.py --from-interview <UUID>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.interview_text import cli

if __name__ == "__main__":
    cli(interactive_default=True)
