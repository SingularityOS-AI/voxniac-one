"""
conftest.py — ensures this directory (voxniac-zero-ONE, where server.py,
cascade.py, vz_config.py etc. live directly, with no package __init__.py) is
on sys.path for every test in tests/, regardless of how pytest is invoked.
"""

import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))
