"""Boot the hub once, disposably, for the clean tree ratchet's start smoke.

Run as the module ``scripts.smoke_hub`` with a port and a scratch data folder.

Serves on the port from a scratch models config written into the data folder:
the real ``hub:`` shape with no model rows, so the boot starts no backend and
adopts none, and never reads or binds the live hub's port. The scratch config
replaces ``host_profile.CONFIG_PATH`` before the server module imports, the same
seam the unit tests use, so no production code carries a smoke-only setting.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.runtime_files import checked_write_path  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: scripts.smoke_hub <port> <data_dir>\n")
        return 2
    port, data_dir = int(argv[0]), Path(argv[1])
    scratch = checked_write_path(data_dir / "models.yaml")
    scratch.write_text(
        yaml.safe_dump({
            "hub": {"port": port, "bind_host": "127.0.0.1"},
            "hosts": {"local": {"enabled": []}},
            "models": {},
        }),
        encoding="utf-8",
    )
    from src import host_profile

    host_profile.CONFIG_PATH = scratch
    from src import server

    server.main()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
