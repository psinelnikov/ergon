"""`python3 -m ergon`, and `python3 path/to/ergon/` (what the plugin hooks run)."""
import sys

if __package__ in (None, ""):  # run as a directory: put the folder holding the package on the path
    from pathlib import Path
    sys.path[0] = str(Path(__file__).resolve().parent.parent)

from ergon.cli import main

if __name__ == "__main__":
    main()
