"""L1 预检 —— 派发前问一句「这活儿能做吗」。

这一层要在 Agent 动手**之前**返回，所以只有纯规则、不调模型：红线级操作
（删根、抹盘、下载即执行）会被拦下，其余一律放行。

⚠️ **L1 通过不等于安全**。它只看字面，不看意图；真正的语义判定属于挂载后
的后台监控。这里的目标是「用零延迟挡掉那些一旦跑完就回不来的事」。

失败处理
========

规则引擎崩了、正则编译错了、输入是二进制垃圾 —— 一律放行并标记
``degraded``。安全层不能反过来成为可用性故障点。
"""

from __future__ import annotations

from typing import Any

from .rules import SEVERITY_NONE, SEVERITY_RED_LINE, SEVERITY_STANDARD, RuleEngine

__all__ = ["ACTION_ALLOW", "ACTION_BLOCK", "build_pre_check_event", "pre_check"]

#: 放行。
ACTION_ALLOW = "allow"
#: 拦下。
ACTION_BLOCK = "block"

#: 严重度排序，用来比较「门槛」。
_SEVERITY_RANK = {SEVERITY_NONE: 0, SEVERITY_STANDARD: 1, SEVERITY_RED_LINE: 2}


def build_pre_check_event(text: str) -> dict[str, Any]:
    """把任务文本包装成规则层能吃的工具调用事件。

    走 ``Bash`` + ``command`` 这条路 —— 高危规则本来就是按命令文本匹配的
    （``rm -rf /``、``mkfs``、``curl … | sh``）。

    :param text: 任务文本。
    :return: 统一事件字典。
    """
    return {
        "type": "tool_call",
        "tool": {"name": "Bash", "arguments": {"command": str(text or "")}},
    }


def _degraded(note: str) -> dict[str, Any]:
    """构造「降级放行」的结果。

    :param note: 原因说明。
    :return: 判定字典。
    """
    return {
        "action": ACTION_ALLOW,
        "verdict": "",
        "rule_id": "",
        "reason": note,
        "evidence": "",
        "degraded": True,
    }


def pre_check(
    text: str,
    *,
    rules: RuleEngine | None = None,
    config: dict[str, Any] | None = None,
    min_severity: str = SEVERITY_STANDARD,
) -> dict[str, Any]:
    """对一段任务文本做 L1 秒级判定。

    :param text: 任务文本（用户交代的那句话）。
    :param rules: 复用的规则引擎；不给就现造一个。
    :param config: 规则配置。
    :param min_severity: 达到这个严重度才真的拦。默认 ``standard``（与原版
        一致）；设成 ``red_line`` 就只拦不可逆的操作，``rm -rf ./dist``
        这类可恢复的删除会放行但仍带 ``rule_id`` 回来。
    :return: 判定字典，必定含 ``action``。
    """
    engine = rules
    if engine is None:
        try:
            engine = RuleEngine(config)
        except Exception as exc:  # noqa: BLE001 —— 预检层任何失败都必须放行
            return _degraded(f"规则引擎初始化失败（已放行）：{type(exc).__name__}")

    try:
        verdict = engine.check_text(text)
    except Exception as exc:  # noqa: BLE001
        return _degraded(f"预检判定失败（已放行）：{type(exc).__name__}")

    threshold = _SEVERITY_RANK.get(str(min_severity or SEVERITY_STANDARD).strip().lower(), 1)
    hit_rank = _SEVERITY_RANK.get(verdict.severity, 0)
    blocking = verdict.dangerous and hit_rank >= threshold

    result = {
        "action": ACTION_BLOCK if blocking else ACTION_ALLOW,
        "verdict": verdict.verdict,
        "rule_id": verdict.rule_id,
        "reason": verdict.reason,
        "evidence": verdict.evidence,
        "degraded": False,
        "severity": verdict.severity,
        "category": verdict.category,
    }
    if verdict.dangerous and not blocking:
        # 命中了但没到门槛 —— 明确告诉调用方「这是被放行的」，别让它误以为干净
        result["suppressed"] = True
    return result
