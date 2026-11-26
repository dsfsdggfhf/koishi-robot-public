"""Small, privacy-preserving runtime metrics helpers.

Metrics are deliberately emitted as counters and model metadata only.  They do
not contain message text, URLs, UIDs, credentials, prompts, or model replies.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable


def _usage_delta(before: dict[str, Any] | None, after: dict[str, Any] | None) -> tuple[int, int]:
    before = before or {}
    after = after or {}
    prompt = 0
    completion = 0
    for model, values in after.items():
        if not isinstance(values, dict):
            continue
        old = before.get(model) if isinstance(before.get(model), dict) else {}
        try:
            prompt += max(0, int(values.get("prompt", 0) or 0) - int(old.get("prompt", 0) or 0))
        except Exception:
            pass
        try:
            completion += max(0, int(values.get("completion", 0) or 0) - int(old.get("completion", 0) or 0))
        except Exception:
            pass
    return prompt, completion


def build_round_metrics(
    *,
    mode: str,
    started: float,
    ai: Any = None,
    usage_before: dict[str, Any] | None = None,
    candidate_count: int = 0,
    attempt_count: int = 0,
    sent_count: int = 0,
    success_count: int = 0,
    failure_count: int = 0,
    unknown_count: int = 0,
    skipped_count: int = 0,
    error_category: str | None = None,
    budget_limit_tokens: int | None = None,
    cache_hit: bool = False,
) -> dict[str, Any]:
    usage_after = ai.usage_summary() if ai is not None and hasattr(ai, "usage_summary") else {}
    prompt_tokens, output_tokens = _usage_delta(usage_before, usage_after)
    # 分模型增量：model_name 只是"本轮最后一次调用"的模型，token 却是全模型合计，
    # 只看 model_name 会归因错位。这里把每个模型各自的增量一起记下来。
    usage_by_model: dict[str, dict[str, int]] = {}
    try:
        for _m, _v in (usage_after or {}).items():
            if not isinstance(_v, dict):
                continue
            _old = (usage_before or {}).get(_m) if isinstance((usage_before or {}).get(_m), dict) else {}
            _p = max(0, int(_v.get("prompt", 0) or 0) - int(_old.get("prompt", 0) or 0))
            _c = max(0, int(_v.get("completion", 0) or 0) - int(_old.get("completion", 0) or 0))
            if _p or _c:
                usage_by_model[str(_m)] = {"prompt": _p, "completion": _c}
    except Exception:
        usage_by_model = {}
    last_call = getattr(ai, "last_call", {}) if ai is not None else {}
    if not isinstance(last_call, dict):
        last_call = {}
    model_name = str(last_call.get("usage_model") or last_call.get("model") or getattr(ai, "model", "") or "none")
    provider_name = str(last_call.get("provider") or "")
    try:
        model_call_count = max(0, int(getattr(ai, "call_count", 0) or 0))
    except Exception:
        model_call_count = 0
    try:
        before_calls = int(getattr(ai, "round_call_start", 0) or 0)
    except Exception:
        before_calls = 0
    model_call_count = max(0, model_call_count - before_calls)
    try:
        request_attempts = max(1, int(last_call.get("attempts", 1) or 1))
    except (TypeError, ValueError):
        request_attempts = 1
    text_channel = str(getattr(ai, "text_channel", "") or "")
    return {
        "kind": "single_round_metrics",
        "mode": str(mode),
        "candidate_count": max(0, int(candidate_count or 0)),
        "attempt_count": max(0, int(attempt_count or 0)),
        "sent_count": max(0, int(sent_count or 0)),
        "success_count": max(0, int(success_count or 0)),
        "failure_count": max(0, int(failure_count or 0)),
        "unknown_count": max(0, int(unknown_count or 0)),
        "skipped_count": max(0, int(skipped_count or 0)),
        "duration_ms": max(0, round((time.perf_counter() - started) * 1000)),
        "model_name": model_name,
        "provider": provider_name,
        "input_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "total_tokens": prompt_tokens + output_tokens,
        "usage_reported": bool(prompt_tokens or output_tokens),
        "usage_by_model": usage_by_model,
        "model_call_count": model_call_count,
        "request_attempts": request_attempts,
        "retry_count": max(0, model_call_count - 1 + request_attempts - 1),
        "fallback_used": text_channel in {"free", "fallback"},
        "cache_hit": bool(cache_hit),
        "budget_limit_tokens": budget_limit_tokens,
        "budget_rejected": False,
        "error_category": error_category,
    }


def emit_round_metrics(logger: Callable[..., Any], **kwargs: Any) -> dict[str, Any]:
    """Emit one JSON metrics line without exposing payload content."""
    payload = build_round_metrics(**kwargs)
    try:
        logger("ROUND_METRICS " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")), "INFO")
    except Exception:
        pass
    return payload
