#!/usr/bin/env python3
"""Launch the OSA Sound Designer GUI."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ui.designer import SoundDesigner

if __name__ == '__main__':
    app = SoundDesigner()
    app.run()
