#!/usr/bin/env python3
"""Resolve repository-relative paths for the public package."""
from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKSTREAM_ROOT = REPOSITORY_ROOT


def resolve_project_path(value: str | Path, *, base: Path = WORKSTREAM_ROOT) -> Path:
    """Resolve relative paths from the public repository root."""
    path = Path(value)
    if path.is_absolute():
        return path
    return (base / path).resolve()
