"""Reviewable post-run experience proposals for the JEV guidance layer.

Experience records are deliberately separate from the curated strategy RAG.
Model output starts as pending evidence, never as a hard rule. Only records
that pass explicit human review and a fixed-response replay can be activated;
active records are sent to JEV as soft context and never alter legal actions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlsplit

from spirebrain.brain.gpt_client import _extract_texts, _json_candidates
from spirebrain.redaction import redact_text, redact_value


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER_PATH = ROOT / "logs" / "strategy_experiences.jsonl"
MAX_REVIEW_EVENTS = 80
MAX_REVIEW_BYTES = 48 * 1024
MAX_EXPERIENCES_PER_QUERY = 3
_LEDGER_LOCK = RLock()
_FIXTURE_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


def _verify_fixed_response_fixture(path: str | Path, proposal: dict,
                                   expected_hash: str) -> dict:
    """Execute a fixed response fixture locally and return owned evidence.

    Replay validation is a trust boundary: callers may identify the fixture,
    but they cannot attest that it passed by supplying arbitrary counters.
    The fixture itself must be synthetic and must explicitly carry the
    proposal id that the replay harness injects into its planning context.
    Provider execution is the offline ``ReplayStrategicClient`` only.
    """
    fixture_path = Path(path)
    try:
        raw_bytes = fixture_path.read_bytes()
    except (OSError, TypeError) as exc:
        raise ValueError(f"fixture_path is not readable: {type(exc).__name__}") from None
    actual_hash = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
    if actual_hash != expected_hash:
        raise ValueError("fixture hash does not match fixture bytes")
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"fixed response fixture is invalid JSON: {type(exc).__name__}") from None
    if not isinstance(payload, dict):
        raise ValueError("fixed response fixture must be an object")
    if payload.get("fixture_type") != "synthetic":
        raise ValueError("fixed response fixture must be marked synthetic")
    if payload.get("kind") != "fixed_provider_responses":
        raise ValueError("replay validation requires fixed_provider_responses")
    proposal_id = str(proposal.get("proposal_id") or "")
    declared = payload.get("experience_ids", [])
    if not isinstance(declared, list) or not all(isinstance(item, str) for item in declared):
        raise ValueError("fixed response fixture experience_ids must be an array")
    responses = payload.get("responses")
    if not isinstance(responses, list) or not responses:
        raise ValueError("fixed response fixture requires responses")
    if not all(isinstance(item, dict) for item in responses):
        raise ValueError("fixed response fixture responses must be objects")

    # Import lazily so the ledger remains usable by startup and postmortem
    # paths without importing the replay command surface.
    from spirebrain.analysis.replay import ReplayStrategicClient

    client = ReplayStrategicClient(responses[:32])
    failures: list[str] = []
    applied = proposal_id in declared
    for index in range(min(len(responses), 32)):
        response = client.plan({
            "run_id": "experience-replay",
            "state_id": f"experience-replay-{index + 1}",
            "verified_experiences": [proposal_id] if applied else [],
        })
        if response.plan is None or response.fallback:
            failures.append(f"response_{index + 1}:provider_or_plan_invalid")
    # Exercise the exact JEV advisory adapter with the proposal shape that
    # activation will later publish. Its output must include the scoped soft
    # preference while preserving legal candidates unchanged.
    if applied and not failures:
        from spirebrain.guide.rules import GuideAwareClient

        validation_proof = {
            "passed": True, "fixture_type": "fixed_provider_responses",
            "fixture_hash": actual_hash, "experience_id": proposal_id,
            "experience_applied": True, "sample_count": len(responses),
            "illegal_action_count": 0, "candidate_set_change_count": 0,
            "hard_constraint_violation_count": 0,
        }

        class CaptureJev:
            def ask(self, state, questions):
                self.state = state
                return state

        jev = CaptureJev()
        guide = GuideAwareClient(jev)
        guide.experiences = [{
            **proposal, "experience_id": proposal_id, "status": "active",
            "policy_type": "soft_preference", "replay_validation": validation_proof,
        }]
        legal_candidates = [{"candidate_id": "candidate-1", "legal": True}]
        guide.ask({"legal_candidates": legal_candidates}, {})
        projected = jev.state.get("verified_experiences")
        if (not isinstance(projected, list) or len(projected) != 1
                or projected[0].get("experience_id") != proposal_id
                or projected[0].get("soft_preference") != proposal.get("claim")
                or jev.state.get("legal_candidates") != legal_candidates):
            failures.append("experience_context_not_applied_to_jev")
    return {
        "passed": not failures and applied,
        "fixture_type": "fixed_provider_responses",
        "fixture_hash": actual_hash,
        "sample_count": min(len(responses), 32),
        "experience_id": proposal_id,
        "experience_applied": applied,
        "illegal_action_count": 0,
        "candidate_set_change_count": 0,
        "hard_constraint_violation_count": 0,
        "failure_reasons": failures or ([] if applied else ["experience_not_declared"]),
    }


def verify_replay_fixture(path: str | Path, proposal: dict | str,
                          expected_hash: str) -> dict:
    """Public offline verifier used by tooling and review integrations."""
    bound = proposal if isinstance(proposal, dict) else {"proposal_id": str(proposal)}
    return _verify_fixed_response_fixture(path, bound, expected_hash)

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["proposals"],
    "properties": {
        "proposals": {
            "type": "array", "maxItems": 3,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["title", "claim", "character", "scenes", "acts",
                             "prerequisites", "exceptions", "evidence_event_ids"],
                "properties": {
                    "title": {"type": "string", "maxLength": 120},
                    "claim": {"type": "string", "maxLength": 500},
                    "character": {"type": "string"},
                    "scenes": {"type": "array", "items": {"type": "string"},
                               "maxItems": 6},
                    "acts": {"type": "array", "items": {"type": "integer"},
                             "maxItems": 3},
                    "prerequisites": {"type": "array", "items": {"type": "string"},
                                      "maxItems": 6},
                    "exceptions": {"type": "array", "items": {"type": "string"},
                                   "maxItems": 6},
                    "evidence_event_ids": {
                        "type": "array", "items": {"type": "string"},
                        "minItems": 1, "maxItems": 8,
                    },
                },
            },
        },
    },
}

REVIEW_PROMPT = """你负责在一局游戏结束后整理可供人工复核的策略经验候选。
只输出 JSON，不输出思维链。不要把相关性写成因果或胜率结论；没有清楚证据时返回空 proposals。
每条经验必须引用输入中的 evidence_event_ids，限定到铁甲战士、具体场景和幕数，写明前提与例外。
这些内容只是供 JEV 参考的软偏好，不是硬规则、命令或合法性约束。不得生成动作命令、索引、API 密钥或玩家身份信息。
"""


def _short_text(value: Any, limit: int) -> str:
    return redact_text(str(value or "")).strip()[:limit]


def _event_id(event: dict, index: int) -> str:
    sequence = event.get("trace_sequence")
    if isinstance(sequence, int) and sequence > 0:
        return f"trace:{sequence}"
    return f"legacy:{index + 1}"


_SENSITIVE_REVIEW_KEYS = {
    "api_key", "apikey", "authorization", "auth", "token", "secret",
    "password", "passwd", "path", "filepath", "file_path", "command_path",
    "command", "request", "request_body", "headers", "cwd", "env",
    "environment", "executable",
}


def _redact_review_value(value: Any, *, depth: int = 0) -> Any:
    """Redact nested model input and drop credential/path-bearing fields."""
    if depth > 5:
        return "[truncated]"
    if isinstance(value, dict):
        safe = {}
        for raw_key, raw_value in list(value.items())[:100]:
            key = str(raw_key)[:80]
            normalized = key.lower().replace("-", "_")
            if (normalized in _SENSITIVE_REVIEW_KEYS
                    or any(token in normalized for token in ("api_key", "authorization", "password"))):
                continue
            safe[key] = _redact_review_value(raw_value, depth=depth + 1)
        return safe
    if isinstance(value, (list, tuple, set)):
        return [_redact_review_value(item, depth=depth + 1) for item in list(value)[:100]]
    return redact_value(value, depth=depth, max_items=100, max_depth=5)


def _review_run_id(value: Any) -> str:
    """Use a stable pseudonym so model input cannot identify a local run."""
    return "run-" + hashlib.sha256(str(value or "unknown").encode("utf-8")).hexdigest()[:16]


def _compact_review(review: dict) -> dict:
    """Keep only bounded, decision-relevant facts; never forward raw blobs."""
    allowed = {
        "event_id", "event_type", "decision_id", "state_id", "screen", "point",
        "source_type", "source", "alternative", "reason", "legal",
        "legality_reason", "fallback", "fallback_reason", "error_kind", "latency_ms",
        "player_action", "verdict", "result", "act", "floor", "combat_facts",
        "rule_ids", "evidence", "terminal_screen", "result_source",
    }
    events = []
    for event in list(review.get("events") or [])[:MAX_REVIEW_EVENTS]:
        compact = {key: _redact_review_value(event[key]) for key in allowed if key in event}
        for key in ("reason", "legality_reason", "fallback_reason"):
            if key in compact:
                compact[key] = _short_text(compact[key], 240)
        events.append(compact)
    value = {
        "run_id": _review_run_id(review.get("run_id")),
        "run_epoch": review.get("run_epoch"),
        "source_type": _redact_review_value(review.get("source_type")),
        "character": _redact_review_value(review.get("character")),
        "terminal_result": _redact_review_value(review.get("terminal_result")),
        "sample_counts": _redact_review_value(review.get("sample_counts", {})),
        "events": events,
        "evidence_event_ids": _redact_review_value(review.get("evidence_event_ids", [])),
    }
    while len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_REVIEW_BYTES and events:
        events.pop()
        value["events"] = events
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_REVIEW_BYTES:
        raise ValueError("run review exceeds the bounded model input size")
    return value


class OpenAIExperienceReviewer:
    """One explicit post-run JSON request using the configured compatible route."""

    def __init__(self, *, endpoint: str, model: str, api_key: str,
                 backend: str = "openai", timeout_ms: int = 20_000,
                 opener=None) -> None:
        self.endpoint = str(endpoint or "").strip()
        self.model = str(model or "").strip()
        self.api_key = str(api_key or "").strip()
        self.backend = str(backend or "openai")
        self.timeout_ms = max(500, min(60_000, int(timeout_ms)))
        self.opener = opener or urllib.request.urlopen
        self.request_attempts = 0

    def review(self, run_review: dict) -> list[dict]:
        if not self.api_key:
            raise RuntimeError("missing_strategic_key")
        if (run_review.get("source_type") != "real_game"
                or run_review.get("lifecycle_complete") is not True
                or run_review.get("terminal_result") not in {
                    "victory", "death", "game_over", "win", "loss"}):
            raise ValueError("experience proposals require a verified real-game run")
        compact = _compact_review(run_review)
        endpoint_path = urlsplit(self.endpoint).path.rstrip("/").lower()
        if endpoint_path.endswith("/responses"):
            body = {
                "model": self.model,
                "instructions": REVIEW_PROMPT + "\nJSON Schema:\n" + json.dumps(
                    REVIEW_SCHEMA, ensure_ascii=False, separators=(",", ":")),
                "input": json.dumps(compact, ensure_ascii=False, separators=(",", ":")),
                "max_output_tokens": 1400,
                "text": {"format": {"type": "json_object"}},
            }
            if self.backend == "hiyo-codex" or self.model.lower().startswith(("gpt-5", "gpt-6")):
                body["reasoning"] = {"effort": "low"}
        else:
            body = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": REVIEW_PROMPT + "\nJSON Schema:\n"
                     + json.dumps(REVIEW_SCHEMA, ensure_ascii=False,
                                  separators=(",", ":"))},
                    {"role": "user", "content": json.dumps(
                        compact, ensure_ascii=False, separators=(",", ":"))},
                ],
                "max_tokens": 1400,
                "response_format": {"type": "json_object"},
            }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json",
                     "User-Agent": "jev-spire-brain/postmortem"},
            method="POST",
        )
        try:
            self.request_attempts += 1
            with self.opener(request, timeout=self.timeout_ms / 1000) as response:
                raw = response.read(128 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"postmortem_http_{int(exc.code)}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"postmortem_{type(exc).__name__}") from None
        if len(raw) > 128 * 1024:
            raise ValueError("postmortem response exceeds size limit")
        decoded = json.loads(raw.decode("utf-8", "replace"))
        texts = _extract_texts(decoded) if isinstance(decoded, dict) else []
        for text in texts:
            for candidate in _json_candidates(text):
                try:
                    value = json.loads(candidate)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict) and isinstance(value.get("proposals"), list):
                    return value["proposals"][:3]
        raise ValueError("postmortem response has no valid proposals object")


class ExperienceLedger:
    """Append-only proposal state; active records are opt-in and revocable."""

    def __init__(self, path: str | Path = DEFAULT_LEDGER_PATH) -> None:
        self.path = Path(path)

    def _events(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        events = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events

    def _append(self, event: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _LEDGER_LOCK:
            with self.path.open("a", encoding="utf-8", errors="replace") as stream:
                stream.write(line)

    def proposals(self) -> list[dict]:
        current: dict[str, dict] = {}
        for event in self._events():
            kind = event.get("event_type")
            if kind == "experience_proposed" and isinstance(event.get("proposal"), dict):
                proposal = dict(event["proposal"])
                current[str(proposal.get("proposal_id") or "")] = proposal
            elif kind == "experience_status":
                proposal_id = str(event.get("proposal_id") or "")
                if proposal_id in current:
                    current[proposal_id]["status"] = event.get("status")
                    if event.get("validation") is not None:
                        current[proposal_id]["replay_validation"] = event["validation"]
        return list(current.values())

    def add_pending(self, raw: dict, run_review: dict, *, provider: str,
                    model: str) -> dict:
        if (run_review.get("source_type") != "real_game"
                or run_review.get("lifecycle_complete") is not True
                or run_review.get("terminal_result") not in {
                    "victory", "death", "game_over", "win", "loss"}):
            raise ValueError("only verified real-game evidence can create proposals")
        allowed_evidence = set(str(item) for item in
                               (run_review.get("evidence_event_ids") or []))
        evidence = list(dict.fromkeys(_short_text(item, 80)
                                      for item in raw.get("evidence_event_ids", [])
                                      if _short_text(item, 80)))
        if not evidence or any(item not in allowed_evidence for item in evidence):
            raise ValueError("proposal evidence IDs must exist in this run review")
        character = str(raw.get("character") or "").strip().upper()
        if character != "IRONCLAD":
            raise ValueError("only the verified IRONCLAD profile may be proposed")
        scenes = [str(item).strip().upper()[:40] for item in raw.get("scenes", [])
                  if str(item).strip()][:6]
        if not scenes:
            raise ValueError("experience proposal needs a scoped scene")
        acts = []
        for item in raw.get("acts", [])[:3]:
            if type(item) is int and 1 <= item <= 3 and item not in acts:
                acts.append(item)
        if not acts:
            raise ValueError("experience proposal needs an act scope")
        observed_characters = {
            str(item).strip().upper() for item in
            (run_review.get("observed_characters") or []) if str(item).strip()
        }
        observed_scenes = {
            str(item).strip().upper()[:40] for item in
            (run_review.get("observed_scenes") or []) if str(item).strip()
        }
        observed_acts = {
            item for item in (run_review.get("observed_acts") or [])
            if type(item) is int and 1 <= item <= 3
        }
        if (character not in observed_characters
                or not set(scenes).issubset(observed_scenes)
                or not set(acts).issubset(observed_acts)):
            raise ValueError("experience proposal scope is outside observed run")
        title = _short_text(raw.get("title"), 120)
        claim = _short_text(raw.get("claim"), 500)
        if not title or not claim:
            raise ValueError("experience proposal needs a title and claim")
        prerequisites = [_short_text(item, 220) for item in raw.get("prerequisites", [])[:6]
                         if _short_text(item, 220)]
        exceptions = [_short_text(item, 220) for item in raw.get("exceptions", [])[:6]
                      if _short_text(item, 220)]
        content = {
            "title": title, "claim": claim, "character": character,
            "scenes": scenes, "acts": acts, "prerequisites": prerequisites,
            "exceptions": exceptions, "evidence_event_ids": evidence,
            "run_id": str(run_review.get("run_id") or "")[:120],
            "run_epoch": int(run_review.get("run_epoch") or 0),
            "source_type": "real_game", "policy_type": "soft_preference",
            "provider": _short_text(provider, 80), "model": _short_text(model, 120),
            "created_at": time.time(), "status": "pending_review",
        }
        if not content["run_id"]:
            raise ValueError("run review has no run identity")
        content_hash = hashlib.sha256(json.dumps(
            content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        proposal_id = "exp-" + content_hash[:20]
        content.update({"proposal_id": proposal_id, "content_hash": content_hash})
        existing = {item["proposal_id"] for item in self.proposals()}
        if proposal_id not in existing:
            self._append({"schema_version": 1, "event_type": "experience_proposed",
                          "timestamp": time.time(), "proposal": content})
        return content

    def _latest(self, proposal_id: str) -> dict:
        item = next((value for value in self.proposals()
                     if value.get("proposal_id") == str(proposal_id)), None)
        if item is None:
            raise ValueError("unknown experience proposal")
        return item

    def _transition(self, proposal_id: str, status: str, **fields) -> dict:
        current = self._latest(proposal_id)
        event = {"schema_version": 1, "event_type": "experience_status",
                 "timestamp": time.time(), "proposal_id": proposal_id,
                 "previous_status": current.get("status"), "status": status}
        event.update(fields)
        self._append(event)
        return {**current, "status": status, **fields}

    def approve(self, proposal_id: str, *, reviewer: str) -> dict:
        if self._latest(proposal_id).get("status") != "pending_review":
            raise ValueError("only pending proposals can be approved")
        return self._transition(proposal_id, "human_approved",
                                reviewer=_short_text(reviewer, 80) or "player")

    def record_replay_validation(self, proposal_id: str, report: dict) -> dict:
        status = self._latest(proposal_id).get("status")
        if status not in {"human_approved", "replay_failed"}:
            raise ValueError("human approval is required before replay validation")
        if not isinstance(report, dict):
            raise ValueError("replay report must be an object")
        fixture_path = report.get("fixture_path")
        if not isinstance(fixture_path, str) or not fixture_path.strip():
            raise ValueError("replay validation requires fixture_path")
        fixture_hash = str(report.get("fixture_hash") or "")
        if _FIXTURE_HASH.fullmatch(fixture_hash) is None:
            raise ValueError("replay validation requires a sha256 fixture hash")
        verified = verify_replay_fixture(
            fixture_path, self._latest(proposal_id), fixture_hash)
        valid = verified["passed"] is True
        compact = {
            "fixture_type": verified["fixture_type"],
            "fixture_hash": verified["fixture_hash"],
            "experience_id": verified["experience_id"],
            "experience_applied": verified["experience_applied"],
            "sample_count": verified["sample_count"],
            "illegal_action_count": verified["illegal_action_count"],
            "candidate_set_change_count": verified["candidate_set_change_count"],
            "hard_constraint_violation_count": verified["hard_constraint_violation_count"],
            "passed": bool(valid),
        }
        if verified.get("failure_reasons"):
            compact["failure_reasons"] = [
                _short_text(item, 120) for item in verified["failure_reasons"][:8]
            ]
        status = "replay_passed" if valid else "replay_failed"
        return self._transition(proposal_id, status, validation=compact)

    def activate(self, proposal_id: str, *, reviewer: str = "player") -> dict:
        if self._latest(proposal_id).get("status") != "replay_passed":
            raise ValueError("a passing fixed-response replay is required before activation")
        return self._transition(proposal_id, "active",
                                activated_by=_short_text(reviewer, 80) or "player")

    def reject(self, proposal_id: str, *, reviewer: str = "player") -> dict:
        if self._latest(proposal_id).get("status") != "pending_review":
            raise ValueError("only pending proposals can be rejected")
        return self._transition(proposal_id, "rejected",
                                reviewed_by=_short_text(reviewer, 80) or "player")

    def revoke(self, proposal_id: str, *, reviewer: str = "player") -> dict:
        if self._latest(proposal_id).get("status") != "active":
            raise ValueError("only active proposals can be revoked")
        return self._transition(proposal_id, "revoked",
                                revoked_by=_short_text(reviewer, 80) or "player")

    def active_for(self, character: str, scene: str, act: int | None = None) -> list[dict]:
        character = str(character or "").upper()
        scene = str(scene or "").upper()
        active = []
        for proposal in self.proposals():
            if proposal.get("status") != "active":
                continue
            validation = proposal.get("replay_validation")
            if (not isinstance(validation, dict)
                    or validation.get("passed") is not True
                    or validation.get("fixture_type") != "fixed_provider_responses"
                    or validation.get("experience_id") != proposal.get("proposal_id")
                    or validation.get("experience_applied") is not True
                    or _FIXTURE_HASH.fullmatch(str(validation.get("fixture_hash") or "")) is None):
                continue
            if proposal.get("character") != character or scene not in proposal.get("scenes", []):
                continue
            if act is not None and act not in proposal.get("acts", []):
                continue
            active.append({key: proposal[key] for key in (
                "proposal_id", "title", "claim", "character", "scenes", "acts",
                "prerequisites", "exceptions", "evidence_event_ids", "content_hash",
            ) if key in proposal} | {
                "experience_id": proposal["proposal_id"],
                "policy_type": "soft_preference",
                "status": "active",
                "replay_validation": validation,
            })
        return active[:MAX_EXPERIENCES_PER_QUERY]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review and activate post-run experiences")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER_PATH))
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--approve")
    parser.add_argument("--reject")
    parser.add_argument("--replay-report", help="JSON fixed-provider replay evidence")
    parser.add_argument("--proposal-id")
    parser.add_argument("--activate")
    parser.add_argument("--revoke")
    args = parser.parse_args(argv)
    ledger = ExperienceLedger(args.ledger)
    try:
        if args.list:
            output = [{key: item.get(key) for key in
                       ("proposal_id", "status", "title", "character", "scenes", "acts")}
                      for item in ledger.proposals()]
        elif args.approve:
            output = ledger.approve(args.approve, reviewer="player")
        elif args.reject:
            output = ledger.reject(args.reject, reviewer="player")
        elif args.replay_report:
            if not args.proposal_id:
                parser.error("--replay-report requires --proposal-id")
            report = json.loads(Path(args.replay_report).read_text(encoding="utf-8"))
            output = ledger.record_replay_validation(args.proposal_id, report)
        elif args.activate:
            output = ledger.activate(args.activate)
        elif args.revoke:
            output = ledger.revoke(args.revoke)
        else:
            parser.error("choose --list, --approve, --reject, --replay-report, --activate or --revoke")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": redact_text(str(exc))},
                         ensure_ascii=False))
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
