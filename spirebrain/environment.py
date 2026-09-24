"""Injectable host-environment access for offline and live runs.

The game integration is Windows-specific, but decision and unit tests should not
need a particular drive, Steam installation, or the developer's AppData.  This
small adapter keeps the default behaviour (read the process environment) while
making every machine-dependent input explicit in tests and tools.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class EnvironmentAdapter:
    """Resolved host paths and environment variables used by the integration."""

    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    repo_root: Path | None = None
    steam_roots: tuple[Path, ...] = ()

    def get(self, key: str, default: str = "") -> str:
        value = self.environ.get(key)
        return default if value is None else str(value)

    @property
    def local_appdata(self) -> Path | None:
        value = self.get("LOCALAPPDATA") or self.get("APPDATA")
        return Path(value) if value else None

    @property
    def game_dir(self) -> Path | None:
        value = self.get("STS_GAME_DIR")
        return Path(value) if value else None

    def mod_config_dir(self, mod_name: str) -> Path:
        root = self.local_appdata
        if root is None:
            raise RuntimeError("%LOCALAPPDATA% is not set; cannot locate ModTheSpire's config dir")
        return root / "ModTheSpire" / mod_name

    @classmethod
    def system(cls, *, repo_root: str | Path | None = None) -> "EnvironmentAdapter":
        return cls(environ=dict(os.environ),
                   repo_root=Path(repo_root) if repo_root is not None else None)


def host_environment(environment: EnvironmentAdapter | Mapping[str, str] | None = None,
                     *, repo_root: str | Path | None = None) -> EnvironmentAdapter:
    """Normalize an adapter or plain environment mapping into one adapter."""
    if isinstance(environment, EnvironmentAdapter):
        return environment
    if environment is None:
        return EnvironmentAdapter.system(repo_root=repo_root)
    return EnvironmentAdapter(environ=dict(environment),
                              repo_root=Path(repo_root) if repo_root is not None else None)
