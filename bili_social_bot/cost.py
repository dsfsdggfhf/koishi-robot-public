from __future__ import annotations

from typing import Any

# 参考价格（元/百万 token）；非官方精确价，供估算。
# 用户提到的策略：空闲时段（非 9-12/14-18 点）价格为高峰一半。
MODEL_PRICES = {
    # 主模型：rightapi 中转站 gpt-5.4（实际计费口径，美元/百万 token，×7.2≈人民币）
    "gpt-5.4": {"input": 1.0, "output": 1.9},      # ≈ $0.14/$0.26 per 1M
    "gpt-5.5": {"input": 1.2, "output": 2.2},      # 估算
    "catspub:gpt-5.5": {"input": 2.0, "output": 12.0},  # Catspub Codex Pro 0.4x：约 p*5/c*30 再乘 0.4
    "rightapi:gpt-5.5": {"input": 1.2, "output": 2.2},  # rightapi 原中转站估算
    "rightapi:gpt-5.4": {"input": 1.0, "output": 1.9},  # rightapi gpt-5.4（前天主力配置）
    "gpt-5.6-luna": {"input": 1.4, "output": 2.6}, # 估算
    "gpt-5.6-sol": {"input": 1.4, "output": 2.6},
    "gpt-5.6-terra": {"input": 1.4, "output": 2.6},
    # opencode 订阅模型（估算；实际以 opencode 账单为准，×7.2≈人民币）
    "hy3": {"input": 1.0, "output": 4.2},            # $0.14/$0.58 per 1M ≈ ¥1.0/¥4.2
    "hy3-free": {"input": 0.0, "output": 0.0},       # 免费层
    "nemotron-3-ultra-free": {"input": 0.0, "output": 0.0},  # 免费层
    "minimax-m3": {"input": 2.2, "output": 8.6},     # 文本+图像
    "mimo-v2.5": {"input": 1.0, "output": 2.0},
    "qwen3.7-plus": {"input": 2.9, "output": 11.5},
    "qwen3.7-max": {"input": 18.0, "output": 54.0},
    "qwen3.8-max": {"input": 14.4, "output": 43.2},
    "minimax-m2.7": {"input": 0.5, "output": 1.5},
    "glm-5.1": {"input": 1.0, "output": 3.5},
    "glm-5.2": {"input": 1.2, "output": 4.0},
    "kimi-k2.6": {"input": 1.0, "output": 3.5},
    "kimi-k2.7-code": {"input": 1.0, "output": 3.5},
    # 备用/历史模型
    "deepseek-reasoner": {"input": 4.0, "output": 16.0},
    "deepseek-chat": {"input": 2.0, "output": 8.0},
    "qwen-vl-lite": {"input": 1.5, "output": 6.0},
    "qwen-vl-plus": {"input": 3.0, "output": 12.0},
    # Command Code Provider API：2026-10-01 公布的美元价 × 7.2，供额度消耗估算。
    # https://commandcode.ai/docs/resources/pricing-limits
    "tencent/hy3-paid": {"input": 0.14 * 7.2, "output": 0.58 * 7.2},
    "MiniMaxAI/MiniMax-M3": {"input": 0.30 * 7.2, "output": 1.20 * 7.2},
    "deepseek/deepseek-v4-flash-vision-exp": {"input": 0.30 * 7.2, "output": 1.20 * 7.2},
    "unknown": {"input": 2.0, "output": 8.0},
}

# 高峰时段（北京时间）
PEAK_RANGES = [(9, 12), (14, 18)]


def is_peak_hour_manual(dt: Any | None = None) -> bool:
    """判断当前是否高峰时段（委托 scheduler 单一实现，避免双实现漂移）。"""
    from .scheduler import is_peak_hour
    return is_peak_hour(dt)


def _commandcode_deepseek_peak(dt: Any | None = None) -> bool:
    """Command Code：周一至五 UTC 01–04、06–10 为高峰，周末全天低峰。"""
    from datetime import datetime, timezone
    current = datetime.now(timezone.utc) if dt is None else dt.astimezone(timezone.utc)
    return current.weekday() < 5 and (1 <= current.hour < 4 or 6 <= current.hour < 10)


def estimate_cost(prompt_tokens: int, completion_tokens: int, model: str = "unknown") -> float:
    """估算费用（元）。输入价格按半价规则：空闲时段为高峰一半。"""
    price = MODEL_PRICES.get(str(model), MODEL_PRICES["unknown"])
    if model in {"tencent/hy3-paid", "MiniMaxAI/MiniMax-M3"}:
        # 这两种模型没有 DeepSeek 的时段折扣。
        factor = 1.0
    elif model == "deepseek/deepseek-v4-flash-vision-exp":
        factor = 1.0 if _commandcode_deepseek_peak() else 0.5
    else:
        factor = 0.5 if not is_peak_hour_manual() else 1.0
    input_yuan = prompt_tokens / 1_000_000 * price["input"] * factor
    output_yuan = completion_tokens / 1_000_000 * price["output"] * factor
    return round(input_yuan + output_yuan, 6)


def cost_from_usage(usage: dict[str, dict[str, int]]) -> float:
    total = 0.0
    for model, u in usage.items():
        total += estimate_cost(u.get("prompt", 0), u.get("completion", 0), model)
    return round(total, 4)


def usage_text(usage: dict[str, dict[str, int]]) -> str:
    lines = []
    for model, u in usage.items():
        p = u.get("prompt", 0)
        c = u.get("completion", 0)
        cost = estimate_cost(p, c, model)
        lines.append(f"{model}: 输入 {p} tokens + 输出 {c} tokens ≈ ¥{cost:.4f}")
    return "\n".join(lines)
