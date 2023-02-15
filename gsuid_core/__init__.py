import sys
from pathlib import Path

from .version import __version__ as __version__  # noqa: F401

sys.path.append(str(Path(__file__).parents[1]))
