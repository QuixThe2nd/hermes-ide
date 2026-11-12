"""Shared path validation helpers for tool implementations (skills, cron, credential files)."""

from pathlib import Path
from typing import Optional


def validate_within_dir(path: Path, root: Path) -> Optional[str]:
    """Error message if *path* does not resolve inside *root* (symlinks and ``..`` followed)."""
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError) as exc:
        return f"Path escapes allowed directory: {exc}"
    return None


def has_traversal_component(path_str: str) -> bool:
    """Cheap pre-check for a literal ``..`` component before full resolution."""
    return ".." in Path(path_str).parts


