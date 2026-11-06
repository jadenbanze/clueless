"""Entry point for the frozen server binary.

`server/api.py` uses relative imports, so it cannot be run as a script or frozen
directly — PyInstaller would fail to resolve `from . import brain`. This shim
imports the package properly.
"""

from server.api import main

if __name__ == "__main__":
    main()
