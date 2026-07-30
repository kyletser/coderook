from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class WorkspaceBoundaryError(PermissionError):
    """Raised when a path escapes the configured workspace."""


@dataclass(frozen=True)
class WorkspaceBoundary:
    """Resolve user-provided paths while enforcing a single workspace root."""

    root: Path

    def __post_init__(self) -> None:
        resolved_root = self.root.expanduser().resolve()
        if not resolved_root.is_dir():
            raise ValueError(f"workspace root is not a directory: {resolved_root}")
        object.__setattr__(self, "root", resolved_root)

    @classmethod
    def current(cls) -> WorkspaceBoundary:
        return cls(Path.cwd())

    def resolve(self, value: str) -> Path:
        if not value or "\x00" in value:
            raise WorkspaceBoundaryError("path must be a non-empty filesystem path")

        raw = Path(value).expanduser()
        candidate = raw if raw.is_absolute() else self.root / raw
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self.root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspaceBoundaryError(
                f"path is outside workspace '{self.root}': {value}"
            ) from exc
        return resolved
