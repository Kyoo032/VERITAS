"""Small, side-effect-free helpers for reproducible runtime metadata."""

from __future__ import annotations

import platform
from importlib import metadata

from supgate import __version__

_DISTRIBUTIONS = {
    "httpx": "httpx",
    "pydantic": "pydantic",
    "PyYAML": "PyYAML",
    "tiktoken": "tiktoken",
    "typer": "typer",
}


def _distribution_version(distribution: str) -> str:
    """Return an exact installed version or an explicit degradation marker."""

    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return "unavailable (distribution metadata missing)"


def runtime_versions() -> dict[str, str]:
    """Return Python, supgate, and core dependency versions for a bundle."""

    return {
        "python": platform.python_version(),
        "supgate": __version__,
        **{name: _distribution_version(distribution) for name, distribution in _DISTRIBUTIONS.items()},
    }
