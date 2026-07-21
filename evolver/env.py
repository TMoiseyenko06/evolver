"""Minimal ``.env`` loader (no third-party dependency).

Reads ``KEY=VALUE`` lines from a ``.env`` file into ``os.environ`` before
:class:`~evolver.config.Config` is constructed (Config reads env in its field
defaults). Follows the usual dotenv conventions: blank lines and ``#`` comments
are ignored, an optional leading ``export`` is stripped, surrounding single/double
quotes are removed, and — by default — real environment variables already set
take precedence over the file (``override=False``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional, Union


def find_dotenv(start: Optional[Union[str, Path]] = None) -> Optional[Path]:
    """Search ``start`` (default cwd) and its parents for a ``.env`` file."""
    here = Path(start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


def load_dotenv(
    path: Optional[Union[str, Path]] = None, override: bool = False
) -> Dict[str, str]:
    """Load a ``.env`` file into ``os.environ``; return the keys it set.

    If ``path`` is None the file is discovered by walking up from the current
    directory. Missing files are a no-op (returns ``{}``).
    """
    dotenv_path = Path(path) if path is not None else find_dotenv()
    if not dotenv_path or not dotenv_path.is_file():
        return {}

    loaded: Dict[str, str] = {}
    for raw in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]  # strip matching surrounding quotes (keep contents literal)
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded
