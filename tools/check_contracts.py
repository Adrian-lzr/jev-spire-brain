"""Validate the shared Python/dashboard/Java advice wire contract offline.

This is not a Java build: it checks the actual Python JSON projection and the
Java FeedClient's declared fields, JSON accessors, defaults, and enum branches.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FEED = ROOT / "java/src/main/java/io/github/adrianlzr/spirebrain/FeedClient.java"
PY_FEED = ROOT / "spirebrain/overlay/feed.py"
SERVER = ROOT / "spirebrain/overlay/server.py"
MANIFEST = ROOT / "java/src/main/resources/ModTheSpire.json"
FIXTURE = ROOT / "tests/fixtures/contracts/advice_event_v2.json"

# JSON key -> (Java field, Java type, JSON accessor, default, input object).
JAVA_FIELDS = {
    "state_id": ("stateId", "String", "optString", "", "last_advice"),
    "plan_id": ("planId", "String", "optString", "", "last_advice"),
    "strategic_goal": ("strategicGoal", "String", "optString", "", "last_advice"),
    "alternative_label": ("alternativeLabel", "String", "optString", "", "last_advice"),
    "alternative_condition": ("alternativeCondition", "String", "optString", "", "last_advice"),
    "uncertain": ("uncertain", "boolean", "optBoolean", False, "last_advice"),
    "source_type": ("adviceSource", "String", "optString", "", "last_advice"),
    "confidence": ("adviceConfidence", "double", "optDouble", 0.0, "last_advice"),
    "display_status": ("displayStatus", "String", "optString", "adviceStatus", "last_advice"),
    "mode": ("mode", "String", "optString", "advise", "run_state"),
    "config_id": ("configId", "String", "optString", "", "run_state"),
    "model_connection_status": ("modelConnectionStatus", "String", "optString", "", "run_state"),
}
SOURCE_ENUM = {"guide_rule", "jev", "jev_tactical", "gpt_strategy", "rule_constraint",
               "rule_fallback", "pending", "unavailable"}
DISPLAY_ENUM = {"local_advice", "model_analysis", "complete_advice", "fallback", "expired"}
STATUS_ENUM = {"ready", "fast_advice", "model_ready", "thinking", "unavailable", "expired"}
MODE_ENUM = {"advise", "play"}


def _java_fields(source: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for match in re.finditer(r"public\s+(String|boolean|double|int|long)\s+([^;]+);", source):
        kind, declaration = match.groups()
        for item in declaration.split(","):
            name = item.strip().split("=")[0].strip()
            if re.fullmatch(r"[\w$]+", name):
                fields[name] = kind
    return fields


def _java_accessor(source: str, object_name: str, key: str):
    pattern = (rf"snap\.(\w+)\s*=\s*{object_name}\.(opt\w+)\(\s*"
               rf"\"{re.escape(key)}\"(?:\s*,\s*([^)]*))?\s*\)")
    match = re.search(pattern, source)
    return match.groups() if match else None


def _valid_type(value: Any, java_type: str) -> bool:
    if java_type == "String":
        return isinstance(value, str)
    if java_type == "boolean":
        return isinstance(value, bool)
    if java_type == "double":
        return isinstance(value, (float, int)) and not isinstance(value, bool)
    return False


def check() -> list[str]:
    errors: list[str] = []
    try:
        java = FEED.read_text(encoding="utf-8")
        py_feed = PY_FEED.read_text(encoding="utf-8")
        server = SERVER.read_text(encoding="utf-8")
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot load contract input: {exc}"]

    if fixture.get("schema_version") != 2:
        errors.append("fixture schema_version must be integer 2")
    payload = fixture.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("last_advice"), dict):
        return errors + ["fixture payload.last_advice must be an object"]
    advice, run = payload["last_advice"], payload.get("run_state", {})
    if advice.get("status") not in STATUS_ENUM:
        errors.append(f"unknown advice status {advice.get('status')!r}")
    if advice.get("display_status") not in DISPLAY_ENUM:
        errors.append(f"unknown display status {advice.get('display_status')!r}")
    if advice.get("source_type") not in SOURCE_ENUM:
        errors.append(f"unknown source type {advice.get('source_type')!r}")
    if not isinstance(run, dict) or run.get("mode", "advise") not in MODE_ENUM:
        errors.append("run_state.mode must be a supported enum value")

    expected_java = fixture.get("expected_java", {})
    declarations = _java_fields(java)
    for key, (field, java_type, accessor, default, owner) in JAVA_FIELDS.items():
        if declarations.get(field) != java_type:
            errors.append(f"Java Snapshot.{field} must be declared {java_type}")
        read = _java_accessor(java, "advice" if owner == "last_advice" else "run", key)
        if key == "source_type":
            # Java intentionally parses source_type into a local sourceType,
            # then maps the enum to the player-facing adviceSource in a switch.
            if not re.search(r'advice\.optString\("source_type",\s*""\)', java):
                errors.append("Java does not parse last_advice.source_type")
            read = (field, accessor, '""')
        if not read:
            errors.append(f"Java does not parse {owner}.{key} into {field}")
            continue
        target_field, actual_accessor, raw_default = read
        if target_field != field:
            errors.append(f"Java {key} maps to {target_field}, expected {field}")
        if actual_accessor != accessor:
            errors.append(f"Java {field} must use {accessor}, got {actual_accessor}")
        if raw_default is not None:
            actual_default = raw_default.strip().strip('"')
            expected_default = ("snap.adviceStatus" if default == "adviceStatus"
                                else str(default).lower() if isinstance(default, bool)
                                else str(default))
            if actual_default != expected_default:
                errors.append(f"Java {field} default {actual_default!r}, expected {expected_default!r}")
        elif default not in ("", 0.0, False):
            errors.append(f"Java {field} must explicitly default to {default!r}")

        source = advice if owner == "last_advice" else run
        value = source.get(key) if isinstance(source, dict) else None
        # org.json opt accessors use their documented fallback for missing/null.
        effective = (advice.get("status", "ready") if default == "adviceStatus" and value is None
                     else default if value is None else value)
        if not _valid_type(effective, java_type):
            errors.append(f"fixture {owner}.{key} cannot be read as Java {java_type}")
        if field in expected_java:
            expected_value = expected_java[field]
            if key == "source_type":
                mapped = {
                    "guide_rule": "攻略规则", "jev": "JEV 判断", "jev_tactical": "JEV 战术层",
                    "gpt_strategy": "GPT 战略", "rule_constraint": "硬规则约束",
                    "rule_fallback": "规则兜底", "pending": "分析中", "unavailable": "暂不可用",
                }.get(effective, "")
                effective = mapped
            if expected_value != effective:
                errors.append(f"expected_java.{field} disagrees with fixture/default")

    switch = re.search(r"switch\s*\(sourceType\)\s*\{(.*?)\n\s*\}", java, re.S)
    switch_body = switch.group(1) if switch else ""
    for value in SOURCE_ENUM:
        if not re.search(rf'case\s+"{re.escape(value)}"\s*:', switch_body):
            errors.append(f"Java source_type enum {value!r} lacks a display mapping")

    for key in JAVA_FIELDS:
        if f'"{key}"' not in py_feed:
            errors.append(f"Python advice serializer omits {key}")
        if key not in {"mode", "config_id", "model_connection_status"} and f'"{key}"' not in server:
            errors.append(f"Dashboard /state omits {key}")
    for required in ("name", "version", "modid"):
        if required not in manifest:
            errors.append(f"ModTheSpire manifest missing {required}")
    return errors


def main() -> int:
    errors = check()
    if errors:
        print("contract mismatch:", "; ".join(errors), file=sys.stderr)
        return 1
    print("cross-language contract: OK (fixture type/null/default/enum checks; Java build not attempted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
