"""Shared Windows console-suppression flag for subprocess spawns.

Every parent process that shells out from this hub (the tray running under
``pythonw``, the FastAPI hub itself when launched windowless, or a script
dispatched from either) has no console of its own — any bare
``subprocess.Popen``/``.run``/``.call``/``.check_output``/``.check_call``
under such a parent flashes a fresh console window per spawn unless the
child is told to suppress it.

Re-deriving this one-line ternary independently per call site (two different
spellings are possible — ``subprocess.CREATE_NO_WINDOW`` or the hardcoded
magic number ``0x08000000``) makes every new call site a fresh chance to
forget it. Import ``NO_WINDOW`` from here instead of re-deriving the ternary.

``NO_WINDOW`` is ``0`` on non-Windows platforms, so it is always safe to
pass as ``creationflags=NO_WINDOW`` unconditionally — ``subprocess.Popen``
only rejects a *non-zero* ``creationflags`` on POSIX.

``src/_respawn_watchdog.py`` is a deliberate exception — it is the detached
process that recovers *from* a broken deploy, so it can't assume any other
``src.*`` module (including this one) still imports cleanly, and re-derives
the ternary inline for that reason. ``scripts/_lib.py``'s own
``no_window_flags()`` similarly stays independent so the install/detection
scripts don't need this package's import machinery.
"""

from __future__ import annotations

import subprocess
import sys

NO_WINDOW: int = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
