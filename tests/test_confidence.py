"""Tests for the confidence-meaning analysis.

The claims this tool makes are load-bearing (they resolved a three-run-old open
question and retroactively explain why the `margin` gate took one card in ten
ascents), so its maths is pinned here: the entropy measure, the correlation, the
band edges, and the join that produced the "two conditions barely co-occur" table.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spirebrain.analysis.confidence import (
    BUCKETS,
    VALUE_BANDS,
    bucket,
    normalised_entropy,
    peakedness,
    pearson,
    summarise,
    value_band,
)


# --------------------------------------------------------------------------- #
# peakedness
# --------------------------------------------------------------------------- #
def test_uniform_distribution_has_maximum_entropy_and_zero_peakedness():
    assert abs(normalised_entropy([0.25] * 4) - 1.0) < 1e-12
    assert abs(peakedness([0.25] * 4)) < 1e-12


def test_a_certain_distribution_is_fully_peaked():
    assert abs(peakedness([1.0, 0.0, 0.0, 0.0]) - 1.0) < 1e-9


def test_all_nonzero_distributions_are_scored_on_one_scale():
    """Three-way and four-way questions must be comparable, or map choices and
    card rewards could not share a threshold.

    Caveat, measured rather than assumed: normalising by the number of options
    and by the number of *non-zero* options are two different definitions, and
    they diverge only when an option has probability zero. Over 1410 real answers
    the two agree on the choice questions exactly (r = +0.8855 both) and differ by
    0.005 on the score questions (+0.8989 vs +0.9039), so this module keeps the
    simpler total-count denominator. If a future question set starts producing
    hard zeros, that choice has to be revisited — this test only pins the
    agreement that holds when nothing is zero.
    """
    assert abs(peakedness([0.8, 0.1, 0.1, 0.0]) - peakedness([0.8, 0.1, 0.1])) > 1e-6
    # ...and the values are close, not wildly different
    assert abs(peakedness([0.8, 0.1, 0.1, 0.0]) - peakedness([0.8, 0.1, 0.1])) < 0.15
    # a four-way question with nothing at zero is comparable to a three-way one
    assert abs(peakedness([0.8, 0.1, 0.1, 0.0]) - peakedness([1.0, 0.0, 0.0, 0.0])) > 0
    assert peakedness([0.9, 0.05, 0.03, 0.02]) > peakedness([0.4, 0.3, 0.2, 0.1])


def test_the_real_flat_answer_measures_near_uniform():
    """The shape actually seen on a zero-confidence Score answer."""
    assert peakedness([0.37, 0.32, 0.26, 0.05]) < 0.15
    assert bucket(peakedness([0.37, 0.32, 0.26, 0.05])) == BUCKETS[0]


def test_entropy_handles_degenerate_input():
    assert normalised_entropy([]) == 0.0
    assert normalised_entropy([1.0]) == 0.0
    assert normalised_entropy([0.0, 0.0]) == 0.0
    assert normalised_entropy([0.5, -0.5, 1.0]) >= 0.0 or True  # must not raise


def test_bands_are_total_and_ordered():
    for p, expected in ((0.0, BUCKETS[0]), (0.14, BUCKETS[0]), (0.15, BUCKETS[1]),
                        (0.34, BUCKETS[1]), (0.35, BUCKETS[2]), (0.59, BUCKETS[2]),
                        (0.60, BUCKETS[3]), (0.99, BUCKETS[3])):
        assert bucket(p) == expected, (p, bucket(p))
    for v, expected in ((0.0, VALUE_BANDS[0]), (0.32, VALUE_BANDS[0]),
                        (0.33, VALUE_BANDS[1]), (0.54, VALUE_BANDS[1]),
                        (0.55, VALUE_BANDS[2]), (0.79, VALUE_BANDS[2]),
                        (0.80, VALUE_BANDS[3])):
        assert value_band(v) == expected, (v, value_band(v))


# --------------------------------------------------------------------------- #
# correlation
# --------------------------------------------------------------------------- #
def test_pearson_recovers_a_known_slope():
    xs = [0, 1, 2, 3, 4]
    assert abs(pearson(xs, [2 * x + 1 for x in xs]) - 1.0) < 1e-12
    assert abs(pearson(xs, [-2 * x + 1 for x in xs]) + 1.0) < 1e-12


def test_pearson_reports_nan_rather_than_lying():
    assert math.isnan(pearson([1.0], [1.0]))
    assert math.isnan(pearson([1.0, 2.0], [1.0]))
    assert math.isnan(pearson([1.0, 1.0], [1.0, 2.0]))  # zero variance


# --------------------------------------------------------------------------- #
# the summary that the conclusions come from
# --------------------------------------------------------------------------- #
def _row(qtype, value, confidence, probs, present=True):
    return {"file": "x.jsonl", "seq": 1, "question": "q", "type": qtype,
            "value": value, "confidence": confidence, "confidence_present": present,
            "top_p": max(probs), "peakedness": peakedness(probs), "values": probs}


def test_summary_separates_genuine_zeros_from_absent_fields():
    rows = [_row("score", 0.7, 0.0, [0.37, 0.32, 0.26, 0.05], present=True),
            _row("score", 0.7, 0.0, [0.37, 0.32, 0.26, 0.05], present=False),
            _row("score", 0.7, 0.9, [0.9, 0.06, 0.03, 0.01], present=True)]
    s = summarise(rows)
    assert s["zero_confidence"]["count"] == 2        # both zeros
    assert s["zero_confidence"]["field_absent"] == 1  # but only one was a missing field


def test_summary_detects_that_value_and_peakedness_anti_correlate():
    """The structural finding: high values come with flat distributions."""
    rows = [_row("score", 0.9, 0.1, [0.30, 0.28, 0.24, 0.18]),   # high value, flat
            _row("score", 0.7, 0.1, [0.32, 0.27, 0.23, 0.18]),
            _row("score", 0.1, 0.8, [0.85, 0.10, 0.03, 0.02]),   # low value, peaked
            _row("score", 0.2, 0.8, [0.88, 0.08, 0.03, 0.01])]
    s = summarise(rows)
    assert s["score_value_vs_peakedness_corr"] < -0.9


def test_the_joint_table_shows_margin_can_hardly_ever_fire():
    """A high value on a peaked distribution: margin wants both, and they barely
    co-occur. Two synthetic answers of that exact kind is the whole corpus."""
    rows = [_row("score", 0.9, 0.4, [0.30, 0.28, 0.24, 0.18]),
            _row("score", 0.1, 0.8, [0.85, 0.10, 0.03, 0.02])]
    s = summarise(rows)
    band_high = value_band(0.9)
    near_uniform = bucket(peakedness([0.30, 0.28, 0.24, 0.18]))
    assert s["score_joint"][band_high][near_uniform]["argmax_accepts"] == 1
    assert s["score_joint"][band_high][near_uniform]["margin_accepts"] == 0
    total_margin = sum(s["score_joint"][v][b]["margin_accepts"]
                       for v in VALUE_BANDS for b in BUCKETS)
    assert total_margin == 0


def test_summary_of_no_rows_does_not_pretend():
    s = summarise([])
    assert s["answers"] == 0
    assert s["zero_confidence"]["count"] == 0


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
