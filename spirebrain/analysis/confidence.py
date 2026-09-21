"""What does JEV's confidence number actually mean? Answer it from our own logs.

This module exists because the project twice wrote down a guess about confidence
and both times the guess was wrong:

* First it was treated as a correctness signal ("below 0.60 → don't act"),
  which made the brain never act at all.
* Then a leftover question sat in the docs for three runs: some Score answers
  reported `confidence=0.000` next to a sibling reporting 0.24 in the same call,
  and a bare `.get("confidence", 0.0)` cannot distinguish "the model said zero"
  from "the field was absent". The docs said, correctly, *do not read 0.000 as a
  calibrated zero until this is resolved*.

Both are answerable from data we already have. So this measures, over every real
call we have logged:

1. **Field integrity.** Is a 0.000 a genuine zero or a missing field?
2. **What confidence tracks.** Pearson correlation against our own peakedness
   measure (1 - normalised entropy of the answer distribution). If it is ~0.9,
   then confidence is a flatness statistic and can be computed by us, without
   asking the model to summarise its own distribution.
3. **What a zero means.** The peakedness ceiling of zero-confidence answers.
4. **What each gate would have done**, bucketed by peakedness — which is the
   quantitative version of run 9's finding that every rejection `margin` made was
   a *shape* rejection, never a value rejection.

    python -m spirebrain.analysis.confidence
    python -m spirebrain.analysis.confidence --glob "logs/jev_calls.arm*.jsonl"
    python -m spirebrain.analysis.confidence --json logs/confidence_report.json

Read-only: it never calls the API.
"""

from __future__ import annotations

import glob
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

REAL_BACKENDS = ("openrouter", "official", "cloudflare")


# --------------------------------------------------------------------------- #
# pure functions (tested)
# --------------------------------------------------------------------------- #
def normalised_entropy(values: list[float]) -> float:
    """Shannon entropy of a distribution, divided by log(n). 1.0 = uniform.

    Normalised so answers with different numbers of options are comparable: the
    Score rubric has four levels, a map choice may have three nodes.

    One definitional choice, made with data rather than by preference: divide by
    the total number of options, or by the number of *non-zero* ones? They diverge
    only when an option has probability zero. Over 1410 real answers the two agree
    exactly on choice questions (r = +0.8855 either way) and differ by 0.005 on
    score questions (+0.8989 vs +0.9039), so the simpler total-count denominator
    stays. Recorded here because a reader will ask, and because a question set that
    produces hard zeros would invalidate the choice.
    """
    total = sum(values)
    if total <= 0 or len(values) < 2:
        return 0.0
    p = [v / total for v in values if v > 0]
    if len(p) < 2:
        return 0.0
    return -sum(x * math.log(x) for x in p) / math.log(len(values))


def peakedness(values: list[float]) -> float:
    """1 - normalised entropy. 0.0 = every option equally likely.

    We compute this ourselves rather than trusting a reported confidence, because
    it is available for every answer that carries a distribution.
    """
    return 1.0 - normalised_entropy(values)


def pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 2 or len(xs) != len(ys):
        return float("nan")
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
    return num / den if den else float("nan")


def bucket(peaked: float) -> str:
    """Labelled bands, so a report can say 'near-uniform' rather than '0.03'."""
    if peaked < 0.15:
        return "near-uniform (<0.15)"
    if peaked < 0.35:
        return "weak (0.15-0.35)"
    if peaked < 0.60:
        return "clear (0.35-0.60)"
    return "strong (>=0.60)"


BUCKETS = ("near-uniform (<0.15)", "weak (0.15-0.35)", "clear (0.35-0.60)", "strong (>=0.60)")


# --------------------------------------------------------------------------- #
# reading the logs
# --------------------------------------------------------------------------- #
def load_answers(pattern: str | None = None) -> list[dict]:
    """Every real (non-mock) answer that carries a probability distribution."""
    paths = sorted(glob.glob(pattern or str(ROOT / "logs" / "jev_calls*.jsonl")))
    out: list[dict] = []
    for path in paths:
        if "mock" in Path(path).name:
            continue  # the mock invents its own numbers; it cannot say anything here
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if not str(rec.get("backend", "")).lower().startswith(REAL_BACKENDS):
                continue
            for question, answer in (rec.get("answers") or {}).items():
                raw = answer.get("raw") or {}
                probs = raw.get("probabilities") or {}
                try:
                    values = [float(v) for v in probs.values()]
                except (TypeError, ValueError):
                    continue
                if len(values) < 2:
                    continue
                out.append({
                    "file": Path(path).name,
                    "seq": rec.get("seq"),
                    "question": question,
                    "type": raw.get("type"),
                    "value": answer.get("value"),
                    "confidence": float(answer.get("confidence") or 0.0),
                    "confidence_present": raw.get("confidence_present"),
                    "top_p": max(values),
                    "peakedness": peakedness(values),
                    "values": sorted(values, reverse=True),
                })
    return out


def value_band(value: float) -> str:
    """The Score action floor sits at 0.55, on a rubric whose four levels are
    0.00 / 0.33 / 0.67 / 1.00. These bands straddle it."""
    if value < 0.33:
        return "below Filler (<0.33)"
    if value < 0.55:
        return "Filler..floor (0.33-0.55)"
    if value < 0.80:
        return "Solid..Excellent (0.55-0.80)"
    return "Excellent+ (>=0.80)"


VALUE_BANDS = ("below Filler (<0.33)", "Filler..floor (0.33-0.55)",
               "Solid..Excellent (0.55-0.80)", "Excellent+ (>=0.80)")


def summarise(rows: list[dict]) -> dict:
    by_type: dict[str, dict] = {}
    for qtype in sorted({r["type"] for r in rows if r["type"]}):
        sub = [r for r in rows if r["type"] == qtype]
        confs = [r["confidence"] for r in sub]
        peaks = [r["peakedness"] for r in sub]
        by_type[qtype] = {
            "n": len(sub),
            "confidence_min": round(min(confs), 4),
            "confidence_max": round(max(confs), 4),
            "top_p_min": round(min(r["top_p"] for r in sub), 4),
            "top_p_max": round(max(r["top_p"] for r in sub), 4),
            "peakedness_min": round(min(peaks), 4),
            "peakedness_max": round(max(peaks), 4),
            "corr_confidence_peakedness": round(pearson(confs, peaks), 4),
        }

    zeros = [r for r in rows if r["confidence"] == 0.0]
    nonzero = [r for r in rows if r["confidence"] > 0.0]
    absent = [r for r in rows if r["confidence_present"] is False]

    # What each gate would have done, by peakedness — the table that explains run 9.
    acceptance: dict[str, dict[str, int]] = {}
    for r in rows:
        if r["type"] != "score":
            continue
        band = bucket(r["peakedness"])
        value_ok = float(r["value"] or 0.0) >= 0.55          # SCORE_ACTION_FLOOR
        top_ok = r["top_p"] >= 0.50
        margin_ok = top_ok and r["peakedness"] >= 0.15        # the margin gate's extra demand
        cell = acceptance.setdefault(band, {"n": 0, "value_floor_ok": 0,
                                            "argmax_accepts": 0, "margin_accepts": 0})
        cell["n"] += 1
        cell["value_floor_ok"] += int(value_ok)
        cell["argmax_accepts"] += int(value_ok)
        cell["margin_accepts"] += int(value_ok and margin_ok)
    for band in BUCKETS:
        if band in acceptance:
            c = acceptance[band]
            c["margin_accept_rate"] = round(c["margin_accepts"] / c["n"], 3)
            c["argmax_accept_rate"] = round(c["argmax_accepts"] / c["n"], 3)

    scores = [r for r in rows if r["type"] == "score"]

    return {
        "answers": len(rows),
        "by_type": by_type,
        "zero_confidence": {
            "count": len(zeros),
            "field_absent": len(absent),
            "peakedness_max": round(max((r["peakedness"] for r in zeros), default=0.0), 4),
            "top_p_min": round(min((r["top_p"] for r in zeros), default=0.0), 4),
            "top_p_max": round(max((r["top_p"] for r in zeros), default=0.0), 4),
        },
        "nonzero_confidence_peakedness_max": round(
            max((r["peakedness"] for r in nonzero), default=0.0), 4),
        "score_acceptance_by_peakedness": acceptance,
        # The joint structure that explains why `margin` almost never fired: its
        # two conditions were nearly mutually exclusive.
        "score_value_vs_peakedness_corr": round(pearson(
            [float(r["value"] or 0.0) for r in scores],
            [r["peakedness"] for r in scores]), 4),
        "score_joint": _joint(scores),
    }


def _joint(scores: list[dict]) -> dict:
    """value band x peakedness band, with how many each gate would accept.

    This is the table that says *why* margin took one card in ten ascents: high
    values and peaked distributions barely ever occurred in the same answer.
    """
    table: dict[str, dict[str, dict]] = {}
    for band in VALUE_BANDS:
        table[band] = {b: {"n": 0, "margin_accepts": 0, "argmax_accepts": 0} for b in BUCKETS}
    for r in scores:
        value = float(r["value"] or 0.0)
        cell = table[value_band(value)][bucket(r["peakedness"])]
        value_ok = value >= 0.55
        cell["n"] += 1
        cell["argmax_accepts"] += int(value_ok)
        cell["margin_accepts"] += int(value_ok and r["top_p"] >= 0.50 and r["peakedness"] >= 0.15)
    return table


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def report(rows: list[dict], summary: dict) -> None:
    print(f"answers with a probability distribution: {summary['answers']}")
    print(f"types: { {k: v['n'] for k, v in summary['by_type'].items()} }\n")

    print("== 1. is a 0.000 a real zero, or a missing field? ==")
    z = summary["zero_confidence"]
    print(f"   confidence == 0.000 with the field PRESENT : {z['count'] - z['field_absent']}")
    print(f"   confidence == 0.000 with the field ABSENT  : {z['field_absent']}")
    if z["count"] and z["field_absent"] == 0:
        print("   -> every zero is genuine. The model does report 0.000 on purpose.")
    print()

    print("== 2. what does confidence track? ==")
    print(f"   {'type':8} {'n':>5}  {'conf range':<16} {'peakedness range':<18} corr")
    for qtype, st in summary["by_type"].items():
        print(f"   {qtype:8} {st['n']:>5}  "
              f"{st['confidence_min']:.3f}-{st['confidence_max']:.3f}      "
              f"{st['peakedness_min']:.3f}-{st['peakedness_max']:.3f}          "
              f"{st['corr_confidence_peakedness']:+.3f}")
    print("   peakedness = 1 - normalised entropy of the answer distribution")
    print("   -> a correlation near +0.9 means confidence IS a flatness statistic,")
    print("      and we can compute it ourselves instead of asking the model.")
    print()

    print("== 3. what does a zero mean? ==")
    print(f"   zero-confidence answers: {z['count']}")
    print(f"   their peakedness ceiling: {z['peakedness_max']:.4f}   "
          f"(a near-uniform distribution)")
    print(f"   their top option spans:   {z['top_p_min']:.3f}-{z['top_p_max']:.3f}")
    print(f"   non-zero answers reach peakedness {summary['nonzero_confidence_peakedness_max']:.4f}")
    print("   -> 0.000 does not mean 'certain it is bad'. It means 'no preference',")
    print("      and a top option of 0.37 against 0.32 is exactly that.")
    print()

    print("== 4. what each Score gate would have done, by peakedness ==")
    print(f"   {'peakedness band':22} {'n':>5} {'value floor ok':>15} "
          f"{'argmax accepts':>15} {'margin accepts':>15}")
    for band, c in summary["score_acceptance_by_peakedness"].items():
        print(f"   {band:22} {c['n']:>5} {c['value_floor_ok']:>15} "
              f"{c['argmax_accepts']:>15} {c['margin_accepts']:>15}")
    print("   -> margin's rejections live in the low-peakedness bands by construction.")
    print("      argmax differs only where the model had no preference to express.")
    print("      Card quality is still unmeasured: this table counts decisions, not outcomes.")
    print()

    print("== 5. why margin almost never fired: its two conditions barely co-occur ==")
    print(f"   corr(value, peakedness) over Score answers: "
          f"{summary['score_value_vs_peakedness_corr']:+.3f}")
    print("   (a negative number means: when the model rates an option high, its")
    print("    distribution is flat — so 'high value AND peaked' is rare by structure)")
    print()
    print(f"   {'value band':24}" + "".join(f"{b.split(' (')[0]:>16}" for b in BUCKETS))
    for band in VALUE_BANDS:
        row = summary["score_joint"][band]
        print(f"   {band:24}" + "".join(f"{row[b]['n']:>16}" for b in BUCKETS))
    print()
    print(f"   {'margin accepts':24}" + "".join(f"{b.split(' (')[0]:>16}" for b in BUCKETS))
    for band in VALUE_BANDS:
        row = summary["score_joint"][band]
        print(f"   {band:24}" + "".join(f"{row[b]['margin_accepts']:>16}" for b in BUCKETS))
    print()
    print(f"   {'argmax accepts':24}" + "".join(f"{b.split(' (')[0]:>16}" for b in BUCKETS))
    for band in VALUE_BANDS:
        row = summary["score_joint"][band]
        print(f"   {band:24}" + "".join(f"{row[b]['argmax_accepts']:>16}" for b in BUCKETS))


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    def value(name: str) -> str | None:
        prefix = f"--{name}="
        hit = next((a.split("=", 1)[1] for a in argv if a.startswith(prefix)), None)
        if hit is None and f"--{name}" in argv:
            hit = argv[argv.index(f"--{name}") + 1]
        return hit

    rows = load_answers(value("glob"))
    if not rows:
        print("no real answers with distributions found — run something against JEV first, "
              "or pass --glob", file=sys.stderr)
        return 2
    summary = summarise(rows)
    report(rows, summary)

    out = value("json")
    if out:
        Path(out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
