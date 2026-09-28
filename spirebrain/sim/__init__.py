"""Offline simulation package.

The benchmark module is intentionally loaded lazily.  Importing it eagerly
would make ``python -m spirebrain.sim.combat_benchmark`` emit a runpy warning
before its JSON report, which breaks consumers that expect machine-readable
stdout.
"""


def load_benchmark(*args, **kwargs):
    from .combat_benchmark import load_benchmark as _load_benchmark

    return _load_benchmark(*args, **kwargs)


def benchmark_fixture(*args, **kwargs):
    from .combat_benchmark import benchmark_fixture as _benchmark_fixture

    return _benchmark_fixture(*args, **kwargs)

from .ironclad import (  # noqa: F401
    IroncladCombatSimulator,
    SimulationResult,
    copy_game_data_manifest,
    extract_minimal_game_snapshot,
    load_fixture,
    sanitize_snapshot,
)

__all__ = [
    "benchmark_fixture",
    "load_benchmark",
    "IroncladCombatSimulator",
    "SimulationResult",
    "copy_game_data_manifest",
    "extract_minimal_game_snapshot",
    "load_fixture",
    "sanitize_snapshot",
]
