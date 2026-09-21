"""Unit tests for the HP budget system."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spirebrain.tactical.hp_budget import HPBudget


def test_budget_act1():
    b = HPBudget(act=1, max_hp=80, current_hp=80)
    assert b.reserved_hp == 24          # 30% of 80
    assert b.remaining_budget == 56
    b.spend(20)
    assert b.remaining_budget == 36
    assert not b.exceeds_budget(30)
    assert b.exceeds_budget(40)


def test_restore_refills_budget():
    b = HPBudget(act=2, max_hp=70, current_hp=35)
    assert b.remaining_budget == 7      # 35 - 28 (40% of 70)
    b.restore(15)
    assert b.current_hp == 50
    assert b.remaining_budget == 22


def test_floor_never_negative():
    b = HPBudget(act=3, max_hp=60, current_hp=10)
    b.spend(99)
    assert b.current_hp == 0
    assert b.remaining_budget == 0
    assert b.exceeds_budget(1)


def test_act3_most_conservative():
    b1 = HPBudget(act=1, max_hp=100, current_hp=100)
    b3 = HPBudget(act=3, max_hp=100, current_hp=100)
    assert b3.reserved_hp > b1.reserved_hp


if __name__ == "__main__":
    test_budget_act1()
    test_restore_refills_budget()
    test_floor_never_negative()
    test_act3_most_conservative()
    print("all hp_budget tests passed")
