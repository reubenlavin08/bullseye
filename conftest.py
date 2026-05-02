"""Make src/ importable when running pytest from the repo root without
a full editable install. Keeps the test loop fast during development.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
