"""Cheap offline cross-language contract check.

This is intentionally static: Java's game dependencies are unavailable in CI,
but the JSON field names shared by Python, dashboard and overlay are still
checked without pretending a Java build succeeded.
"""

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
FEED = ROOT / "java/src/main/java/io/github/adrianlzr/spirebrain/FeedClient.java"
PY = ROOT / "spirebrain/overlay/feed.py"
MANIFEST = ROOT / "java/src/main/resources/ModTheSpire.json"

FIELDS = {
    "state_id": "stateId", "plan_id": "planId", "strategic_goal": "strategicGoal",
    "alternative_label": "alternativeLabel", "alternative_condition": "alternativeCondition",
    "uncertain": "uncertain", "source_type": "adviceSource", "confidence": "adviceConfidence",
}


def main() -> int:
    feed = FEED.read_text(encoding="utf-8")
    python = PY.read_text(encoding="utf-8")
    manifest = MANIFEST.read_text(encoding="utf-8")
    missing = []
    for py_name, java_name in FIELDS.items():
        if f'"{py_name}"' not in python:
            missing.append(f"python:{py_name}")
        if not re.search(rf"\b{re.escape(java_name)}\b", feed):
            missing.append(f"java:{java_name}")
    for required in ("name", "version", "modid"):
        if required not in manifest:
            missing.append(f"manifest:{required}")
    if missing:
        print("contract mismatch:", ", ".join(missing), file=sys.stderr)
        return 1
    print("cross-language contract: OK (static; Java build not attempted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
