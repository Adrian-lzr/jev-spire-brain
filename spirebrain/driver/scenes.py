"""Small screen-to-handler boundary for the Slay the Spire router."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class SceneRouter:
    def __init__(self, handlers: dict[str, Callable[[dict], Any]],
                 grid_screens: tuple[str, ...] = ()) -> None:
        self.handlers = {str(screen).upper(): handler
                         for screen, handler in handlers.items()}
        self.grid_screens = frozenset(str(screen).upper() for screen in grid_screens)

    def resolve(self, screen: str):
        return self.handlers.get(str(screen or "").upper())

    def is_grid(self, screen: str) -> bool:
        return str(screen or "").upper() in self.grid_screens
