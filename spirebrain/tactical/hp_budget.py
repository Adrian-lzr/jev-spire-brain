"""HP budget system — the soul of SpireBrain.

Quantifies the user's original intuition: "is the HP I'm about to lose
acceptable?" into a per-Act budget that every risk decision consults.

Pure functions only. Unit-testable. No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Conservative retention ratios by act (higher ascension -> raise these).
ACT_RETENTION = {1: 0.30, 2: 0.40, 3: 0.50}


@dataclass
class HPBudget:
    act: int
    max_hp: int
    current_hp: int
    spent: int = 0          # cumulative damage taken this act
    restored: int = 0       # cumulative healing this act
    retention_ratio: float = field(default=0.0)

    def __post_init__(self) -> None:
        self.retention_ratio = ACT_RETENTION.get(self.act, 0.50)

    @property
    def reserved_hp(self) -> int:
        """HP we must not dip below this act."""
        return int(self.max_hp * self.retention_ratio)

    @property
    def remaining_budget(self) -> int:
        """Spendable HP right now."""
        return max(0, self.current_hp - self.reserved_hp)

    def spend(self, damage: int) -> None:
        self.spent += damage
        self.current_hp = max(0, self.current_hp - damage)

    def restore(self, healing: int) -> None:
        self.restored += healing
        self.current_hp = min(self.max_hp, self.current_hp + healing)

    def exceeds_budget(self, predicted_damage: int) -> bool:
        """The question JEV risk probes ultimately consult."""
        return predicted_damage > self.remaining_budget
