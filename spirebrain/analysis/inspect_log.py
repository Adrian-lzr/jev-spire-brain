"""Inspect the JEV call log — every question, answer, confidence and cost.

Why this exists: the log is the raw evidence behind every claim the project
makes, and it is append-only JSONL that is awkward to read by hand. When a
decision module starts falling back constantly, the first question is always
"did the model really answer that way, or did we parse it wrong?" — this answers
it from the recorded bytes.

    python -m spirebrain.analysis.inspect_log                 # last 5 calls
    python -m spirebrain.analysis.inspect_log --limit 24      # last 24
    python -m spirebrain.analysis.inspect_log --backend jev   # filter substring
    python -m spirebrain.analysis.inspect_log --summary       # roll-up only
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG = ROOT / "logs" / "jev_calls.jsonl"


def load(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"no log at {path} — run a simulation first")
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def describe_answer(name: str, a: dict) -> str:
    raw = a.get("raw") or {}
    kind = raw.get("type", "?")
    value = a.get("value")
    conf = a.get("confidence")
    extra = ""
    if kind == "choice":
        probs = raw.get("probabilities") or {}
        if probs:
            top = sorted(probs.items(), key=lambda kv: -kv[1])[:3]
            extra = "  probs=" + ", ".join(f"{k}:{v}" for k, v in top)
    elif kind == "score":
        extra = f"  level={raw.get('score', raw.get('level'))}"
    elif kind == "noul":
        extra = f"  p(yes)={raw.get('noul')}"
    v = f"{value:.4f}" if isinstance(value, float) else repr(value)
    c = f"{conf:.3f}" if isinstance(conf, float) else repr(conf)
    return f"      {name:<28} {kind:<7} value={v:<28} conf={c}{extra}"


def main(argv: list[str]) -> None:
    limit = 5
    backend_filter: str | None = None
    summary_only = False
    path = DEFAULT_LOG

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1]); i += 2; continue
        if arg.startswith("--limit="):
            limit = int(arg.split("=", 1)[1]); i += 1; continue
        if arg == "--backend" and i + 1 < len(argv):
            backend_filter = argv[i + 1]; i += 2; continue
        if arg.startswith("--backend="):
            backend_filter = arg.split("=", 1)[1]; i += 1; continue
        if arg == "--summary":
            summary_only = True; i += 1; continue
        path = Path(arg); i += 1

    calls = load(path)
    if backend_filter:
        calls = [c for c in calls if backend_filter.lower() in str(c.get("backend", "")).lower()]
    if not calls:
        raise SystemExit("no matching calls in the log")

    backends = Counter(str(c.get("backend")) for c in calls)
    total_cost = sum(float(c.get("cost_usd", 0.0)) for c in calls)
    latencies = [int(c.get("latency_ms", 0)) for c in calls]
    below = sum(1 for c in calls for a in c.get("answers", {}).values()
                if isinstance(a.get("confidence"), (int, float)) and a["confidence"] < 0.60)

    print(f"log      : {path}")
    print(f"calls    : {len(calls)}   backends: {dict(backends)}")
    print(f"cost     : ${total_cost:.8f}  (avg ${total_cost / len(calls):.8f}/call)")
    if latencies:
        lat_sorted = sorted(latencies)
        p50 = lat_sorted[len(lat_sorted) // 2]
        print(f"latency  : min={min(latencies)}ms  p50={p50}ms  max={max(latencies)}ms")
    print(f"answers  : {sum(len(c.get('answers', {})) for c in calls)} total, "
          f"{below} below the 0.60 floor")

    if summary_only:
        return

    print("\n--- questions asked (by type) ---")
    types = Counter()
    for c in calls:
        for spec in (c.get("questions") or {}).values():
            if isinstance(spec, dict):
                types[f"{spec.get('type')}"] += 1
    print("   " + ", ".join(f"{k}: {v}" for k, v in types.most_common()))

    for c in calls[-limit:]:
        print(f"\n#{c.get('seq')} {c.get('backend')} model={c.get('model')} "
              f"latency={c.get('latency_ms')}ms cost=${float(c.get('cost_usd', 0)):.8f} "
              f"usage={c.get('usage')}")
        state = str(c.get("state", ""))
        print(f"   state: {state[:220]}{'…' if len(state) > 220 else ''}")
        for name, a in (c.get("answers") or {}).items():
            print(describe_answer(name, a))


if __name__ == "__main__":
    main(sys.argv[1:])
