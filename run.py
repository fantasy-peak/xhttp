#!/usr/bin/env python3
"""PyInstaller / direct-launch entry point.

Running ``python -m xhttp`` goes through the package's ``__main__``;
PyInstaller executes its entry script as a top-level module, where relative
imports do not work, so it uses this thin absolute-import launcher instead.
"""

from xhttp.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
