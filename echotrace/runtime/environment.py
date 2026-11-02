"""Load the repository's local ``.env`` without overriding real environment values.

Container deployments inject their environment directly.  This helper exists
only for direct Python entrypoints so the API and worker use the same local
configuration contract.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_local_environment(path: str | Path | None = None) -> None:
    """Load simple ``KEY=value`` entries from ``.env`` if it exists.

    The project intentionally keeps this dependency-free.  Existing process
    variables win, and unresolved Compose placeholders are ignored because
    they are not valid direct-run values.
    """

    env_path = Path(path) if path is not None else Path.cwd() / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ and not value.startswith("${"):
            os.environ[key] = value


__all__ = ["load_local_environment"]
