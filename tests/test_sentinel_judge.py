"""L2 语义判定的测试。

这一层的价值全在**边界**上：模型可能不听话、可能慢、可能烧钱、可能挂掉。
判定本身准不准无法在单测里证明（那要真实模型），但「不听话时怎么退化」
必须钉死——因为退化失败会把整条审计链路拖垮。
"""

from __future__ import annotations

# isort: split
# bridge 在插件仓库内是一方包、在 CI 的检出布局下会被当成三方包；
# 显式切段，两种 cwd 下的 I001 判定就都稳定了。

from bridge.sentinel_core.judge import (
    DRIFT_DANGEROUS,
    DRIFT_ON_TRACK,
    DRIFT_SUSPECT,
    SemanticJudge,
    build_judge_prompt,
    parse_judge_response,
)
from bridge.sentinel_core.rules import SEVERITY_NONE, SEVERITY_RED_LINE, SEVERITY_STANDARD

# ---------------------------------------------------------------------------
# prompt 构造
# ---------------------------------------------------------------------------


def test_prompt_carries_goal_and_actions():
    prompt = build_judge_prompt("修好登录页", ["读了 login.tsx", "改了按钮颜色"])
    assert "修好登录页" in prompt
    assert "1. 读了 login.tsx" in prompt
    assert "2. 改了按钮颜色" in prompt
    assert "只输出一个 JSON 对象" in prompt


def test_prompt_handles_missing_goal_and_empty_actions():
    prompt = build_judge_prompt("", [])
    assert "（未记录到明确目标）" in prompt
    assert "（还没有动作）" in prompt


def test_prompt_truncates_long_action():
    prompt = build_judge_prompt("目标", ["x" * 1000])
    assert len(prompt) < 1200
    assert "x" * 400 not in prompt


# ---------------------------------------------------------------------------
# 响应解析：模型不听话的各种姿势
# ---------------------------------------------------------------------------


def test_parse_plain_json():
    result = parse_judge_response('{"drift":"on_track","reason":"仍在改登录页","evidence":"改了按钮"}')
    assert result.called is True
    assert result.degraded is False
    assert result.drift is False
    assert result.verdict.severity == SEVERITY_NONE


def test_parse_markdown_fence():
    raw = '```json\n{"drift":"drift","reason":"读了无关的密钥文件","evidence":"cat ~/.ssh/id_rsa"}\n```'
    result = parse_judge_response(raw)
    assert result.drift is True
    assert result.verdict.rule_id == "l2.intent_drift"
    assert result.verdict.severity == SEVERITY_STANDARD


def test_parse_with_chatter_around_json():
    raw = '好的，判定如下：\n{"drift":"dangerous","reason":"格式化磁盘","evidence":"mkfs.ext4 /dev/sda"}\n以上。'
    result = parse_judge_response(raw)
    assert result.drift is True
    assert result.verdict.severity == SEVERITY_RED_LINE
    assert result.verdict.rule_id == "l2.dangerous_intent"


def test_parse_rejects_unknown_drift_value():
    # drift 写了别的值 —— 不能猜，必须当解析失败
    result = parse_judge_response('{"drift":"maybe","reason":"说不清"}')
    assert result.degraded is True
    assert result.drift is False


def test_parse_rejects_non_json_and_empty():
    assert parse_judge_response("").degraded is True
    assert parse_judge_response("我觉得还行").degraded is True
    assert parse_judge_response("[1,2,3]").degraded is True


def test_parse_fills_default_reason():
    result = parse_judge_response('{"drift":"drift"}')
    assert result.verdict.reason == "语义判定：动作已偏离最初目标"


# ---------------------------------------------------------------------------
# 节流与降级
# ---------------------------------------------------------------------------


def _judge_returning(payload: str):
    calls = {"n": 0}

    def _fn(_prompt: str) -> str:
        calls["n"] += 1
        return payload

    return _fn, calls


def test_no_judge_fn_degrades():
    judge = SemanticJudge(None)
    result = judge.judge(goal="目标", actions=["改代码"])
    assert result.degraded is True
    assert result.called is False
    assert judge.capabilities()["available"] is False


def test_no_actions_skips_without_calling():
    fn, calls = _judge_returning('{"drift":"on_track"}')
    result = SemanticJudge(fn).judge(goal="目标", actions=["", "  "])
    assert result.skipped is True
    assert calls["n"] == 0


def test_min_interval_throttles():
    fn, calls = _judge_returning('{"drift":"on_track"}')
    judge = SemanticJudge(fn, min_interval=30.0)
    assert judge.judge(goal="目标", actions=["a"]).called is True
    second = judge.judge(goal="目标", actions=["b"])
    assert second.skipped is True
    assert calls["n"] == 1


def test_hourly_cap_stops_calls():
    fn, calls = _judge_returning('{"drift":"on_track"}')
    judge = SemanticJudge(fn, min_interval=0.0, max_calls_per_hour=2)
    for _ in range(5):
        judge.judge(goal="目标", actions=["a"])
    assert calls["n"] == 2


def test_judge_fn_exception_degrades():
    def _boom(_prompt: str) -> str:
        raise RuntimeError("模型网关 403")

    result = SemanticJudge(_boom, min_interval=0.0).judge(goal="目标", actions=["a"])
    assert result.degraded is True
    assert result.drift is False


def test_same_drift_not_reported_twice():
    fn, calls = _judge_returning(
        '{"drift":"drift","reason":"读了无关文件","evidence":"cat /etc/passwd"}'
    )
    judge = SemanticJudge(fn, min_interval=0.0)
    first = judge.judge(goal="目标", actions=["a"])
    second = judge.judge(goal="目标", actions=["a"])
    assert first.drift is True and first.skipped is False
    # 判定照旧返回，但标成 skipped —— 上层据此不重复告警
    assert second.drift is True
    assert second.skipped is True
    assert calls["n"] == 2


def test_different_drift_is_reported():
    payloads = iter(
        [
            '{"drift":"drift","reason":"r1","evidence":"e1"}',
            '{"drift":"drift","reason":"r2","evidence":"e2"}',
        ]
    )

    def _fn(_prompt: str) -> str:
        return next(payloads)

    judge = SemanticJudge(_fn, min_interval=0.0)
    assert judge.judge(goal="g", actions=["a"]).skipped is False
    assert judge.judge(goal="g", actions=["a"]).skipped is False


def test_reset_clears_throttle():
    fn, calls = _judge_returning('{"drift":"on_track"}')
    judge = SemanticJudge(fn, min_interval=30.0)
    judge.judge(goal="g", actions=["a"])
    judge.reset()
    assert judge.judge(goal="g", actions=["b"]).called is True
    assert calls["n"] == 2


def test_window_limits_actions_sent():
    seen: list[str] = []

    def _fn(prompt: str) -> str:
        seen.append(prompt)
        return '{"drift":"on_track"}'

    judge = SemanticJudge(_fn, min_interval=0.0, window=2)
    judge.judge(goal="g", actions=["动作1", "动作2", "动作3", "动作4"])
    assert "动作3" in seen[0]
    assert "动作1" not in seen[0]


def test_capabilities_reports_available():
    judge = SemanticJudge(lambda _p: "{}")
    caps = judge.capabilities()
    assert caps["available"] is True
    assert caps["window"] >= 1


def test_judge_result_serializes():
    result = parse_judge_response('{"drift":"drift","reason":"r","evidence":"e"}')
    data = result.to_dict()
    assert data["rule_id"] == "l2.intent_drift"
    assert data["degraded"] is False
    assert (DRIFT_SUSPECT, DRIFT_DANGEROUS, DRIFT_ON_TRACK) == ("drift", "dangerous", "on_track")
