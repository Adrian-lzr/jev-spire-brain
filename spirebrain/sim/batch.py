"""Batch ascent runner — the instrument the pre-registered experiment needs.

Until now the project could only run *one* ascent at a time, which is exactly why
every conclusion so far rests on n = 1 (docs/MEASUREMENTS.md, "Known threats to
validity"). This runs a seed group and aggregates the numbers the experiment
compares: cards taken, deck size, HP at act boundaries, fallback counts, cost.

    python -m spirebrain.sim.batch --seeds calibration --backend openrouter --acceptance margin
    python -m spirebrain.sim.batch --seeds calibration --backend openrouter --acceptance argmax
    python -m spirebrain.sim.batch --seeds test --backend openrouter --acceptance argmax --tag final

Results are appended to `logs/batch_<tag>.jsonl` **per ascent**, not at the end:
a 10-ascent run against a real API takes minutes, and a crash on ascent 9 must
not throw away ascents 1-8. The summary is printed and also written next to the
run log.

Cost is printed after every ascent so an experiment cannot silently run away.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from spirebrain.sim.run_offline import ROOT, load_seeds, run_one_simulation

LOG_DIR = ROOT / "logs"


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def summarise(records: list[dict]) -> dict:
    """Aggregate across ascents. Every field here is reported with its n."""
    n = len(records)
    if not n:
        return {"runs": 0}
    cards = [r["cards_taken"] for r in records]
    deck_end = [r["deck_end"] for r in records]
    hp_act1 = [r["acts"][0]["hp_end"] for r in records]
    hp_act2 = [r["acts"][1]["hp_end"] for r in records]
    hp_act3 = [r["acts"][2]["hp_end"] for r in records]
    fallbacks = [sum(1 for p in act["steps"] if p["fb"]) for r in records for act in r["acts"]]
    steps = [len(act["steps"]) for r in records for act in r["acts"]]
    return {
        "runs": n,
        "acceptance": records[0].get("acceptance"),
        "seeds": [r["seed"] for r in records],
        "cards_taken_total": sum(cards),
        "cards_taken_mean": _mean(cards),
        "runs_with_cards": sum(1 for c in cards if c > 0),
        "deck_end_mean": _mean(deck_end),
        "hp_act1_end_mean": _mean(hp_act1),
        "hp_act2_end_mean": _mean(hp_act2),
        "hp_act3_end_mean": _mean(hp_act3),
        "fallback_total": sum(fallbacks),
        "fallback_rate": (round(sum(fallbacks) / sum(steps), 3) if steps else None),
        "jev_calls": sum(r["jev_calls"] for r in records),
        "cost_usd": round(sum(r["jev_cost_usd"] for r in records), 8),
        "rejections": [
            {"seed": r["seed"], **rej} for r in records for rej in r.get("score_rejections", [])
        ],
    }


def run_batch(*, seeds: list[int], backend: str | None, acceptance: str | None,
              tag: str) -> dict:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out_path = LOG_DIR / f"batch_{tag}.jsonl"
    records: list[dict] = []
    spent = 0.0
    t0 = time.perf_counter()

    print(f"=== batch [{tag}] seeds={seeds} backend={backend or 'mock'} "
          f"acceptance={acceptance or 'margin'} -> {out_path.name} ===")
    for i, seed in enumerate(seeds, 1):
        result = run_one_simulation(seed=seed, backend=backend, acceptance=acceptance)
        records.append(result)
        spent += result["jev_cost_usd"]
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
        print(f"  [{i}/{len(seeds)}] seed={seed} cards={result['cards_taken']} "
              f"deck={result['deck_size_start']}->{result['deck_end']} "
              f"hp_ends={[a['hp_end'] for a in result['acts']]} "
              f"calls={result['jev_calls']} spent=${result['jev_cost_usd']:.8f} "
              f"| running total ${spent:.6f} | {time.perf_counter() - t0:.0f}s")

    summary = summarise(records)
    summary["tag"] = tag
    summary["wall_seconds"] = round(time.perf_counter() - t0, 1)
    with open(LOG_DIR / f"batch_{tag}_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n--- summary [{tag}] ---")
    for key in ("runs", "acceptance", "cards_taken_total", "cards_taken_mean",
                "runs_with_cards", "deck_end_mean", "hp_act1_end_mean",
                "hp_act2_end_mean", "hp_act3_end_mean", "fallback_total",
                "fallback_rate", "jev_calls", "cost_usd", "wall_seconds"):
        if key in summary:
            print(f"  {key:20} {summary[key]}")
    if summary.get("rejections"):
        print(f"  rejections ({len(summary['rejections'])}):")
        for rej in summary["rejections"]:
            print(f"    seed {rej['seed']} act {rej['act']}: value={rej['value']} - {rej['reason']}")
    return summary


def main(argv: list[str]) -> None:
    def value(name: str) -> str | None:
        prefix = f"--{name}="
        hit = next((a.split("=", 1)[1] for a in argv if a.startswith(prefix)), None)
        if hit is None and f"--{name}" in argv:
            hit = argv[argv.index(f"--{name}") + 1]
        return hit

    seeds_arg = value("seeds") or "calibration"
    if seeds_arg in ("calibration", "test"):
        seeds = load_seeds(seeds_arg)
    else:
        seeds = [int(s) for s in seeds_arg.split(",") if s.strip()]

    acceptance = value("acceptance")
    backend = value("backend")
    tag = value("tag") or f"{seeds_arg}-{acceptance or 'margin'}-{backend or 'mock'}"
    limit = value("limit")
    if limit:
        seeds = seeds[: int(limit)]

    run_batch(seeds=seeds, backend=backend, acceptance=acceptance, tag=tag)


if __name__ == "__main__":
    main(sys.argv[1:])
