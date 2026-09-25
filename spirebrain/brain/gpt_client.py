"""Provider adapters for the strategic brain.

The OpenAI adapter uses only the Python standard library, matching the existing
JEV client.  It accepts both the Responses-shaped payload used by OpenAI and a
chat-completions compatible endpoint so local gateways can be tested without a
second implementation.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
import threading
from typing import Any

from .protocol import BrainResponse, PlanValidationError, StrategicPlan
from spirebrain.redaction import redact_text, redact_value
from spirebrain.runtime_budget import DecisionBudget, current_budget


PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "plan_id": {"type": "string"},
        "state_id": {"type": "string"},
        "run_id": {"type": "string"},
        "current_objective": {"type": "string"},
        "long_term_goal": {"type": "string"},
        "priority": {"type": "array", "items": {"type": "string"}},
        "preferred_candidates": {"type": "array", "items": {"type": "string"}},
        "avoid_candidates": {"type": "array", "items": {"type": "string"}},
        "resource_constraints": {"type": "object", "additionalProperties": True},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "replan_triggers": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
        "uncertainty": {"type": "string"},
        "expires_after": {"type": ["integer", "null"]},
    },
    "required": [
        "plan_id", "state_id", "run_id", "current_objective", "long_term_goal",
        "priority", "preferred_candidates", "avoid_candidates",
        "resource_constraints", "next_steps", "replan_triggers", "reason",
        "uncertainty", "expires_after",
    ],
}


SYSTEM_PROMPT = """你是 Slay the Spire 的战略规划大脑。
你只负责整局目标、资源约束和候选优先级，不直接生成游戏命令、鼠标坐标、卡牌索引或协议动词。
只能从候选列表中引用 candidate_id。所有动作最终由本地合法性层和 JEV 战术层确认。
当前状态会带有一个保守的打法 profile（力量成长、消耗引擎、格挡堆叠、自伤收益、异常牌转化或自适应端口）。
只有在卡组信号足够时才延续该打法；信号冲突或不足时选择自适应端口，优先解决输出、防御、运转三端中最弱的一端。
不要为了凑流派牺牲当前战斗生存、HP 预算、金币保留或路线可行性。
优先保护明确的生存约束，解释只写简短、可展示的理由，不输出隐藏思维链。
返回严格符合给定 JSON Schema 的短计划。"""


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
    else:
        text = json.dumps(value, ensure_ascii=False)
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


def _extract_text(body: dict) -> str:
    output_text = body.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    output = body.get("output") or []
    chunks: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content") or []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    if chunks:
        return "".join(chunks)
    choices = body.get("choices") or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        content = message.get("content", "")
        if isinstance(content, list):
            return "".join(
                str(part.get("text", "")) for part in content
                if isinstance(part, dict)
            )
        return str(content)
    raise ValueError("模型响应中没有文本内容")


def _unwrap_plan(data: Any) -> Any:
    """Accept common gateway wrappers while keeping the plan schema strict."""
    if not isinstance(data, dict):
        return data
    for key in ("plan", "strategic_plan", "result", "data"):
        nested = data.get(key)
        if isinstance(nested, dict):
            # Only unwrap when the nested object looks like our plan.  A model
            # cannot use this convenience to smuggle arbitrary commands past
            # StrategicPlan.from_dict's unknown-field check.
            if "plan_id" in nested or "current_objective" in nested or "steps" in nested:
                return nested
    return data


def _coerce_steps_plan(data: Any, *, state_id: str, run_id: str,
                       max_steps: int) -> Any:
    """Normalize the compact ``steps`` shape used by several Chinese gateways.

    The adapter copies only candidate IDs and display reasons.  It never copies
    a provider command, index, or free-form action into the execution layer.
    Full StrategicPlan responses continue through the strict schema unchanged.
    """
    if not isinstance(data, dict):
        return data
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list):
        # Some gateways reserve ``plan`` for a list of next actions.
        raw_steps = data.get("plan")
    if not isinstance(raw_steps, list):
        return data
    steps = [item for item in raw_steps if isinstance(item, dict)][:max_steps]
    preferred: list[str] = []
    reasons: list[str] = []
    for item in steps:
        candidate = item.get("candidate_id")
        if isinstance(candidate, str) and candidate.strip() and candidate.strip() not in preferred:
            preferred.append(candidate.strip()[:240])
        reason = item.get("reason")
        if isinstance(reason, str) and reason.strip():
            reasons.append(reason.strip()[:500])
    objective = data.get("current_objective") or data.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        objective = reasons[0] if reasons else "按当前合法候选推进"
    return {
        "plan_id": str(data.get("plan_id") or f"gateway-{state_id[:12]}"),
        "state_id": state_id,
        "run_id": run_id,
        "current_objective": objective[:500],
        "long_term_goal": str(data.get("long_term_goal") or "完成当前局面并保留资源")[:500],
        "priority": ["survive", "follow_strategy", "advance"],
        "preferred_candidates": preferred,
        "avoid_candidates": [],
        "resource_constraints": data.get("resource_constraints")
        if isinstance(data.get("resource_constraints"), dict) else {},
        "next_steps": preferred[:max_steps] or ["等待新的合法状态"],
        "replan_triggers": ["screen_change", "hp_change", "gold_change"],
        "reason": reasons[0] if reasons else "模型返回了当前候选步骤，已由本地合法性层校验。",
        "uncertainty": str(data.get("uncertainty") or "")[:300],
        "expires_after": 1,
    }


class UnavailableStrategicClient:
    # A disabled backend has no I/O.  Keeping this explicit lets the
    # orchestrator answer synchronously in tests and in the JEV-only mode.
    async_required = False

    def __init__(self, reason: str = "未配置 OPENAI_API_KEY",
                 backend_name: str = "unavailable") -> None:
        self.reason = reason
        self.backend_name = backend_name

    def plan(self, payload: dict) -> BrainResponse:
        return BrainResponse(backend=self.backend_name, error=self.reason, fallback=True)


class MockStrategicClient:
    """Deterministic planner used by tests and offline demos."""

    backend_name = "mock"
    async_required = False

    def __init__(self, preferred: list[str] | None = None,
                 objective: str = "先保证当前局面可控") -> None:
        self.preferred = list(preferred or [])
        self.objective = objective
        self.calls = 0
        self.payloads: list[dict] = []

    def plan(self, payload: dict) -> BrainResponse:
        self.calls += 1
        self.payloads.append(payload)
        candidates = payload.get("candidates") or []
        ids = [str(c.get("candidate_id")) for c in candidates if isinstance(c, dict)]
        preferred = [x for x in self.preferred if x in ids]
        if not preferred and ids:
            preferred = [ids[0]]
        state_id = str(payload.get("state_id", ""))
        run_id = str(payload.get("run_id", "local"))
        plan = StrategicPlan.from_dict({
            "plan_id": f"mock-{self.calls}",
            "state_id": state_id,
            "run_id": run_id,
            "current_objective": self.objective,
            "long_term_goal": "完成当前楼层并保留足够资源",
            "priority": ["survive", "preserve_resources", "advance"],
            "preferred_candidates": preferred,
            "avoid_candidates": [],
            "resource_constraints": {},
            "next_steps": ["执行当前合法候选", "状态变化后重新评估"],
            "replan_triggers": ["screen_change", "hp_change", "gold_change"],
            "reason": "离线战略大脑根据候选列表给出稳定的测试计划。",
            "uncertainty": "",
            "expires_after": 1,
        }, state_id=state_id, run_id=run_id)
        return BrainResponse(plan=plan, backend=self.backend_name, model="mock",
                             latency_ms=1, request_id=f"mock-{self.calls}")


class OpenAIStrategicClient:
    backend_name = "openai"
    # The live game must never wait on urllib/network I/O.  The orchestrator
    # uses this marker to run requests on its bounded background worker.
    async_required = True

    def __init__(self, *, api_key: str | None = None, model: str | None = None,
                 endpoint: str | None = None, timeout_ms: int = 6000,
                 max_output_tokens: int = 900, opener=None,
                 structured_output: bool | None = None,
        provider_name: str = "openai", max_calls: int = 1) -> None:
        self.provider_name = provider_name or "openai"
        self.backend_name = self.provider_name
        self.api_key = api_key or os.environ.get("BRAIN_API_KEY", "").strip() \
            or os.environ.get("OPENAI_API_KEY", "").strip()
        # With no key, plan() returns a local fallback immediately. Running that
        # path on a daemon thread only creates lifecycle races during shutdown
        # and temporary-directory cleanup; real network calls remain async.
        self.async_required = bool(self.api_key)
        self.model = model or os.environ.get("BRAIN_MODEL", "").strip() \
            or os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
        self.endpoint = endpoint or os.environ.get(
            "BRAIN_ENDPOINT", ""
        ).strip() or os.environ.get(
            "OPENAI_BRAIN_ENDPOINT", "https://api.openai.com/v1/responses"
        )
        self.timeout_ms = max(500, int(timeout_ms))
        self.max_calls = max(1, int(max_calls))
        self.max_output_tokens = max(128, int(max_output_tokens))
        self.opener = opener or urllib.request.urlopen
        # Compatible gateways often reject vendor-specific json_schema output;
        # local StrategicPlan validation remains strict and is the default.
        if structured_output is None:
            structured_output = os.environ.get("OPENAI_STRUCTURED_OUTPUT", "0").lower() in {
                "1", "true", "yes", "on"
            }
        self.structured_output = bool(structured_output)

    def plan(self, payload: dict) -> BrainResponse:
        request_id = uuid.uuid4().hex[:16]
        if not self.api_key:
            return BrainResponse(backend=self.backend_name, model=self.model,
                                 request_id=request_id,
                                 error="未配置战略大脑 API Key（BRAIN_API_KEY/OPENAI_API_KEY）",
                                 error_kind="missing_key",
                                 fallback=True)
        started = time.monotonic()
        budget = current_budget()
        reserved_ms = budget.reserve(self.timeout_ms, minimum_ms=50) if budget else self.timeout_ms
        if not reserved_ms:
            return BrainResponse(backend=self.backend_name, model=self.model,
                                 request_id=request_id, error="战略决策预算已耗尽",
                                 error_kind="budget_exhausted",
                                 fallback=True)
        try:
            body = self._request_body(payload)
            request = urllib.request.Request(
                self.endpoint,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "jev-spire-brain/strategic",
                },
                method="POST",
            )
            try:
                with self.opener(request, timeout=reserved_ms / 1000) as response:
                    raw = response.read()
            except urllib.error.HTTPError as exc:
                # Do not copy the response body into an error. Gateways can
                # echo request fragments or credentials, and the status code
                # is sufficient for retry/fallback classification.
                return self._failure(
                    request_id, started,
                    f"HTTP {exc.code} from strategic provider",
                    error_kind=("auth" if exc.code in {401, 403} else
                                "rate_limit" if exc.code == 429 else "http"),
                )
            decoded = json.loads(raw.decode("utf-8"))
            text = _extract_text(decoded)
            if len(text.encode("utf-8", "replace")) > 64 * 1024:
                raise PlanValidationError("模型响应超过 64KB 限制")
            data = _unwrap_plan(json.loads(_json_text(text)))
            data = _coerce_steps_plan(
                data,
                state_id=str(payload.get("state_id", "")),
                run_id=str(payload.get("run_id", "local")),
                max_steps=int(payload.get("max_plan_steps", 5) or 5),
            )
            plan = StrategicPlan.from_dict(
                data,
                state_id=str(payload.get("state_id", "")),
                run_id=str(payload.get("run_id", "local")),
                max_steps=int(payload.get("max_plan_steps", 5) or 5),
            )
            usage = decoded.get("usage") if isinstance(decoded, dict) else {}
            return BrainResponse(
                plan=plan,
                backend=self.backend_name,
                model=str(decoded.get("model", self.model)),
                latency_ms=int((time.monotonic() - started) * 1000),
                request_id=request_id,
                usage=usage if isinstance(usage, dict) else {},
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Exception text may include an endpoint with a query-string key.
            return self._failure(request_id, started,
                                 f"请求超时：{type(exc).__name__}" if isinstance(exc, TimeoutError)
                                 else f"网络错误：{type(exc).__name__}",
                                 error_kind="timeout" if isinstance(exc, TimeoutError) else "network")
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, PlanValidationError) as exc:
            return self._failure(request_id, started, redact_text(f"计划解析失败：{exc}"),
                                 error_kind="parse_or_validation")

    def _failure(self, request_id: str, started: float, error: str,
                 *, error_kind: str = "provider") -> BrainResponse:
        return BrainResponse(
            backend=self.backend_name,
            model=self.model,
            latency_ms=int((time.monotonic() - started) * 1000),
            request_id=request_id,
            error=error,
            error_kind=error_kind,
            fallback=True,
        )

    def _request_body(self, payload: dict) -> dict:
        user_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if "chat/completions" in self.endpoint:
            body = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": self.max_output_tokens,
            }
            if self.structured_output:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "strategic_plan", "strict": True,
                                     "schema": PLAN_SCHEMA},
                }
            return body
        return {
            "model": self.model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": SYSTEM_PROMPT}]},
                {"role": "user", "content": [{"type": "input_text", "text": user_text}]},
            ],
            "temperature": 0.1,
            "max_output_tokens": self.max_output_tokens,
            "text": {"format": {"type": "json_schema", "name": "strategic_plan",
                                  "strict": True, "schema": PLAN_SCHEMA}},
        }


class LoggingStrategicClient:
    """JSONL observability wrapper; credentials never enter the record."""

    _lock = threading.Lock()

    def __init__(self, inner, path: str | os.PathLike) -> None:
        self.inner = inner
        self.backend_name = getattr(inner, "backend_name", "unknown")
        self.async_required = bool(
            getattr(inner, "async_required", str(self.backend_name).lower() in {"openai", "gpt"})
        )
        self.path = Path(path)

    def plan(self, payload: dict) -> BrainResponse:
        started = time.monotonic()
        try:
            result = self.inner.plan(payload)
        except Exception as exc:  # keep the wrapper transparent to the caller
            result = BrainResponse(backend=self.backend_name,
                                   error=redact_text(f"{type(exc).__name__}: {exc}"), fallback=True)
        plan = result.plan.model_dict() if result.plan is not None else None
        record = redact_value({
            "ts": time.time(),
            "backend": result.backend or self.backend_name,
            "model": result.model,
            "request_id": result.request_id,
            "latency_ms": result.latency_ms or int((time.monotonic() - started) * 1000),
            "usage": result.usage,
            "error": result.error,
            "error_kind": result.error_kind,
            "fallback": bool(result.fallback),
            "state_id": payload.get("state_id", ""),
            "run_id": payload.get("run_id", ""),
            "trigger": payload.get("trigger", ""),
            "plan": plan,
        })
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
        return result


def get_strategic_brain(backend: str | None = None, **kwargs):
    """Build a provider without making a network request."""
    selected = (backend or os.environ.get("BRAIN_BACKEND") or "openai").lower()
    if selected in {"none", "disabled", "off", "jev", "legacy"}:
        return UnavailableStrategicClient("战略大脑已禁用",
                                          backend_name="jev" if selected in {"jev", "legacy"}
                                          else "disabled")
    if selected == "mock":
        return MockStrategicClient(**{k: v for k, v in kwargs.items() if k in {"preferred", "objective"}})
    compatible = {
        "openai", "gpt", "deepseek", "qwen", "tongyi", "zhipu", "glm",
        "moonshot", "kimi", "siliconflow", "doubao", "openai-compatible",
        "openai_compatible", "compatible", "local", "vllm", "ollama",
    }
    if selected in compatible:
        return OpenAIStrategicClient(
            provider_name=selected,
            **kwargs,
        )
    return UnavailableStrategicClient(f"未知战略大脑后端：{selected}", backend_name=selected)
