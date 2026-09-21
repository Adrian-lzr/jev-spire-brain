"""Tests for the simulation harness itself.

These exist because the harness quietly lied: every seed produced the identical
route and the identical HP trajectory, so ten "ascents" were one ascent repeated.
That made a ten-run experiment look like evidence while carrying the information
of a single run (docs/MEASUREMENTS.md, run 6). Nothing about the agent's behaviour
was wrong — the instrument was.

A harness is only worth measuring with if its seeds change its outcomes, so that
is asserted directly here rather than assumed.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spirebrain.jev_brain.state import path_damage_probes
from spirebrain.sim.run_offline import make_map, run_one_simulation


def _hps(result: dict) -> list[int]:
    return [act["hp_end"] for act in result["acts"]]


def test_different_seeds_produce_different_runs():
    """The regression guard for the defect that invalidated the first 10-run batch."""
    trajectories = {tuple(_hps(run_one_simulation(seed=s, backend="mock"))) for s in range(1, 9)}
    assert len(trajectories) > 1, (
        "seeds must change the outcome, or a multi-seed batch is one run repeated: "
        f"got {trajectories}"
    )


def test_routes_are_not_identical_across_seeds():
    routes = {tuple(p["choice"] for act in run_one_simulation(seed=s, backend="mock")["acts"]
                    for p in act["steps"] if p["kind"] == "map")
              for s in range(1, 9)}
    assert len(routes) > 1, "every seed walked the same map: the map is not seed-driven"


def test_maps_are_three_rows_of_three_nodes():
    from spirebrain.sim.run_offline import SYMBOL_POOLS

    rng = random.Random(7)
    for act in (1, 2, 3):
        rows = make_map(rng, act)
        assert len(rows) == 3
        for row in rows:
            assert len(row) == 3
            assert len({n["id"] for n in row}) == 3, "node ids must be distinct"
            # A row may hold two of the same type (sampling is by position, and a
            # real map row can show two monster nodes), but never an unknown type.
            assert {n["symbol"] for n in row} <= set(SYMBOL_POOLS[act])
            assert all(int(n["damage_hint"]) >= 0 for n in row)


def test_some_rows_offer_no_free_option():
    """If every row contained a rest node, 'safest' would always be 'free' and
    routing would never cost HP — which is how the old fixed map behaved."""
    rng = random.Random(11)
    free_rows = 0
    total = 0
    for act in (1, 2, 3):
        for _ in range(20):
            for row in make_map(rng, act):
                total += 1
                if any(int(n["damage_hint"]) == 0 for n in row):
                    free_rows += 1
    assert 0 < free_rows < total, "rest nodes should be possible but not guaranteed"


def test_probe_honours_a_node_damage_hint():
    nodes = [{"id": "x", "symbol": "R", "damage_hint": 9},
             {"id": "y", "symbol": "M"}]
    probes = path_damage_probes(nodes, act=1)
    assert probes["x"] == 9, "an explicit hint is an upper bound on that node"
    assert probes["y"] > 0, "without a hint we fall back to the symbol's worst case"


def test_a_spent_cost_is_at_most_the_probed_cost():
    """The probe is supposed to be a worst case, not a decoration. If the walk
    can cost more than the probe told the agent, the whole HP-budget layer is
    reasoning about a number that does not describe the run."""
    checked = 0
    for seed in range(1, 9):
        result = run_one_simulation(seed=seed, backend="mock")
        for act in result["acts"]:
            for step in act["steps"]:
                if step["kind"] != "map":
                    continue
                assert "probe" in step and "spent" in step
                assert step["spent"] <= step["probe"], (
                    f"seed {seed} act {act['act']}: spent {step['spent']} > "
                    f"probed {step['probe']}"
                )
                checked += 1
    assert checked >= 24, "expected one probe record per map row per act"


if __name__ == "__main__":
    import traceback

    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"ok   {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
