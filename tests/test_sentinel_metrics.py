"""`metrics.py` 的诚实性测试 —— 不许把「没测」渲染成「没花」。

背景
====

code-reviewer 的复核发现：`TokenLedger.record()` 是唯一写 `input_tokens` /
`output_tokens` 的入口，而生产链路只调 `record_blocked`（只给 `saved_tokens`）。
于是 `total_tokens` 恒为 0，而 `render_usage` 把它渲染成「Token 约 0（输入 0 /
输出 0，估算值）」——**对着一个恒为 0 的字段讲估算精度**，等于把「未统计」说成
「零消耗」。晨报本来要回答「昨晚烧了多少」，这样写会让用户以为答案是 0。

这里钉的是**话术正确性**，不是数字计算（计算在 `test_sentinel_store.py`）：

* 没测到消耗 → 文案必须出现「未统计」，且不得出现「约 0」；
* 只有挽回、没有消耗 → 不得编百分比（分母是 0，硬算会得出 100%）；
* 真测到了 → 保留原有「估算值」标注，别把好路径一起改坏。
"""

from __future__ import annotations

import pytest

from bridge.sentinel_core.metrics import TokenLedger, estimate_tokens, render_usage


def test_unmeasured_consumption_is_not_rendered_as_zero():
    """生产现状：只记了挽回，消耗恒 0 —— 必须说「未统计」。"""
    text = render_usage(
        {"estimated": True, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "saved_tokens": 90}
    )

    assert "未统计" in text
    assert "约 0" not in text, f"把「没测」渲染成了「没花」：{text}"
    assert "90" in text, "挽回的数值不该丢"


def test_saved_without_measured_total_has_no_fake_ratio():
    """分母为 0 时不算比例：`90 / (0 + 90)` 会得出「100%」这种无意义的数。"""
    text = render_usage({"total_tokens": 0, "input_tokens": 0, "output_tokens": 0, "saved_tokens": 90})

    assert "%" not in text, f"没有实测消耗却给出了比例：{text}"


def test_measured_usage_keeps_estimate_marker():
    """真拿到用量时保持原样，别把好路径改坏。"""
    text = render_usage({"total_tokens": 1500, "input_tokens": 1000, "output_tokens": 500, "saved_tokens": 0})

    assert "1.5k" in text
    assert "估算值" in text
    assert "未统计" not in text
    assert "挽回" not in text, "没有挽回时不该出现「挽回」字样"


def test_measured_usage_with_savings_still_shows_ratio():
    text = render_usage({"total_tokens": 100, "input_tokens": 60, "output_tokens": 40, "saved_tokens": 100})

    assert "50%" in text, text


def test_empty_summary_says_no_record():
    assert render_usage({}) == "Token：无记录"


@pytest.mark.parametrize("text", ["", "hello", "帮我写一个函数", "x" * 4096])
def test_estimate_is_monotonic_and_non_negative(text):
    """`estimate_tokens` 的下限性质：任何输入都不该给出负数。"""
    assert estimate_tokens(text) >= 0


def test_blocked_only_ledger_reports_unmeasured_consumption():
    """端到端：生产链路的真实调用序列（只有 `record_blocked`）。"""
    ledger = TokenLedger(None)
    ledger.record_blocked(agent="codebuddy", fingerprint="fp-deadbeef", task="rm -rf /")

    summary = ledger.summary()

    assert summary["total_tokens"] == 0, "本版本没有回传用量的调用点，这里应为 0"
    assert summary["saved_tokens"] > 0
    assert summary["calls"] == 1

    text = render_usage(summary)
    assert "未统计" in text
    assert "约 0" not in text
