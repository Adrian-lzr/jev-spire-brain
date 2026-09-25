"""Small, dependency-free secret redaction used at every observability edge."""

from __future__ import annotations

import re


_SECRET_PATTERNS = (
    # Authorization headers and prose such as ``Bearer sk-...``.
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+|bearer\s+)[^\s,}\]]+"),
    # ``API key: ...`` and provider errors such as ``API key provided: ...``.
    re.compile(
        r"(?i)((?:[\"']?(?:api[_ -]?key|access[_ -]?token|auth(?:entication)?[_ -]?token|secret)"
        r"[\"']?)(?:\s+provided)?\s*[:=]\s*[\"']?)[^\s,}\]\"']+"
    ),
)


def redact_text(value: object, *, limit: int = 1000) -> str:
    """Return bounded text with credential-like values replaced.

    This is intentionally conservative and is applied before errors reach the
    feed, provider log or trace.  It does not attempt to identify arbitrary
    high-entropy strings without a label, because doing that would corrupt
    card ids and request ids.
    """
    text = str(value or "")[:limit]
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)}[redacted]", text)
    return text


def redact_value(value: object, *, depth: int = 0, max_items: int = 100,
                 max_depth: int = 5) -> object:
    """Bound and redact a JSON-like value before it reaches observability.

    Provider responses and exception objects are not trusted log input. This
    preserves the shape needed by replay tools while preventing nested error
    bodies, plans, or usage metadata from becoming an unbounded log sink.
    """
    if depth > max_depth:
        return "[truncated]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            str(key)[:80]: redact_value(item, depth=depth + 1,
                                         max_items=max_items,
                                         max_depth=max_depth)
            for key, item in list(value.items())[:max_items]
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_value(item, depth=depth + 1,
                             max_items=max_items, max_depth=max_depth)
                for item in list(value)[:max_items]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value)
