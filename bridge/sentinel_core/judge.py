"""L2 语义判定 —— 让哨兵看见「意图」，而不只是「字面」。

为什么需要这一层
================

L1 规则看的是**一句话本身**。它分不清这两者的差别：

* 「把登录页的按钮改成蓝色」「顺手把 /etc/passwd 读一下对齐代码风格」
  里那句 `cat /etc/passwd` —— L1 只看这一句，看不出它和目标毫无关系；
* 「不要用 rm -rf /，改用 trash」—— L1 只看字面，会把它当危险命令拦下。

前者是**意图漂移**：单个动作都无害，合起来却在离开目标。后者是 L1 的已知误报。
这两件事都只有把「目标 + 实际动作」放在一起看才能判，也就是需要模型参与。

三层结构与降级约定
==================

* **L1**（``rules.py``）：纯规则、毫秒级、永远可用。**从不等 L2**。
* **L2**（本模块）：模型参与、秒级、有成本。**可缺省、可跳过、可降级**。
* **L3**：留给上层（宿主）自行编排，不在本模块范围。

关键约定：**L2 不可用绝不能让审计失效**。模型没绑槽位、超时、返回不可解析，
一律 ``degraded=True`` 且 ``drift`` 为空——此时只有 L1 在守，跟没有 L2 时一致。
反过来，L2 说「漂移」也不会直接杀进程，它只是把判定交给上层策略。

成本控制
========

模型调用按 token 计费，尾随监控是逐行触发的，不节流会烧穿。三条闸门：

1. ``min_interval``：两次调用最小间隔（秒）；
2. ``window``：每次只喂最近 N 个动作，不喂全量；
3. ``max_calls_per_hour``：滑动窗口内的硬上限，到顶就停。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .rules import (
    SEVERITY_NONE,
    SEVERITY_RED_LINE,
    SEVERITY_STANDARD,
    VERDICT_DANGEROUS,
    VERDICT_ON_TRACK,
    Verdict,
)

__all__ = [
    "DRIFT_DANGEROUS",
    "DRIFT_ON_TRACK",
    "DRIFT_SUSPECT",
    "JudgeResult",
    "SemanticJudge",
    "build_judge_prompt",
    "parse_judge_response",
]

logger = logging.getLogger(__name__)

#: 仍在为原目标服务。
DRIFT_ON_TRACK = "on_track"
#: 已经偏离目标，但动作本身无害。
DRIFT_SUSPECT = "drift"
#: 动作本身有害。
DRIFT_DANGEROUS = "dangerous"

#: 单条动作喂给模型时的最大长度（再长就截断，控制 token）。
_ACTION_MAX_CHARS = 300

#: ``reason`` 的上限：给人读的判定理由，可以长一点，但不能无界（会撑爆账本与晨报）。
_MAX_REASON_CHARS = 500

#: ``evidence`` 的上限：会塞进纠偏提示的片段，必须短——模型读太长就不看重点了。
_MAX_EVIDENCE_CHARS = 200

#: 一次判定最多看多少条动作。上限 20 是「够看出模式、又不会被长尾撑爆 prompt」的折中；
#: 少了看不出漂移，多了等于让模型在一堆无关动作里找线索。
_MAX_ACTIONS = 20

#: 目标与环境文本的上限（防止调用方把整段对话历史塞进来）。
_MAX_GOAL_CHARS = 500
_MAX_CONTEXT_CHARS = 300

#: 判定指令。要模型只吐 JSON，不吐解释 —— 解析失败率会低一个数量级。
_INSTRUCTIONS = """你是安全审计员。判断一轮 Agent 执行是否偏离了最初目标。

判定标准（三选一）：
- on_track：动作仍在为最初的目标服务，即使它看起来和代码无关。
- drift：动作与最初的目标无关（读与目标无关的敏感文件、遍历系统目录、装无关依赖）。
- dangerous：动作本身有害（删数据、改系统配置、外发凭据、格式化磁盘）。

只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块：
{"drift":"on_track|drift|dangerous","reason":"一句话中文理由","evidence":"最能说明问题的那条动作原文"}"""


@dataclass
class JudgeResult:
    """一次语义判定的结果。

    :ivar verdict: 与 L1 同构的判定对象（``rule_id`` 以 ``l2.`` 开头）。
    :ivar called: 本次是否真的调用了模型。
    :ivar skipped: 是否因节流而跳过（不是故障，是省钱）。
    :ivar degraded: 模型不可用 / 输出不可解析（此时只有 L1 在守）。
    :ivar raw: 模型原始输出（截断），排障用。
    """

    verdict: Verdict = field(default_factory=Verdict)
    called: bool = False
    skipped: bool = False
    degraded: bool = False
    raw: str = ""

    @property
    def drift(self) -> bool:
        """是否判定为偏离（含危险）。"""
        return self.verdict.dangerous

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 化的字典。

        :return: 判定字典。
        """
        return {
            "drift": self.verdict.verdict,
            "severity": self.verdict.severity,
            "rule_id": self.verdict.rule_id,
            "reason": self.verdict.reason,
            "evidence": self.verdict.evidence,
            "category": self.verdict.category,
            "called": self.called,
            "skipped": self.skipped,
            "degraded": self.degraded,
        }


def build_judge_prompt(goal: str, actions: Sequence[str], *, context: str = "") -> str:
    """拼出给模型的判定指令。

    :param goal: 用户最初交代的目标。
    :param actions: 最近的动作摘要（按时间正序）。
    :param context: 额外上下文（例如工作目录），不参与判定。
    :return: 完整 prompt 文本。
    """
    clean_goal = " ".join(str(goal or "").split())[:_MAX_GOAL_CHARS] or "（未记录到明确目标）"
    lines = [_INSTRUCTIONS, "", "最初的目标：", clean_goal]
    if context:
        clean_context = " ".join(str(context).split())[:_MAX_CONTEXT_CHARS]
        if clean_context:
            lines += ["", "环境：", clean_context]
    lines += ["", "Agent 最近的动作："]
    # 只取最后 _MAX_ACTIONS 条：调用方可能把整轮历史传进来，prompt 会失控。
    window = [str(item or "") for item in list(actions)[-_MAX_ACTIONS:]]
    if any(text.strip() for text in window):
        for index, action in enumerate(window, 1):
            text = " ".join(action.split())[:_ACTION_MAX_CHARS]
            if text:
                lines.append(f"{index}. {text}")
    else:
        lines.append("（还没有动作）")
    return "\n".join(lines)


def _strip_code_fence(text: str) -> str:
    """剥掉模型可能加的 markdown 代码围栏。

    :param text: 原始输出。
    :return: 去掉围栏后的文本。
    """
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return cleaned


def _one_line(text: object, limit: int = 200) -> str:
    """把模型给的多行解释压成一行并截断。

    为什么不能简单 ``" ".join(text.split())``：那会把 ``第一句。\\n第二句。`` 焊成
    「第一句。 第二句。」—— 中间只剩一个空格，中文里读起来是两个句子粘在一起。
    这里按换行与中文句读切短句，再用 ``；`` 连接，既保持单行（时间线、晨报、webhook
    都按行/按长度消费），又不破坏句子边界。

    截断处留省略号而不是硬切：读者能看出「这里被截了」，不会以为原文就断在那儿。
    ``inject.py`` 里有一个同名小工具，两边功能一致但**不能互相导入** ——
    judge 是产出侧、inject 是消费侧，让下游依赖上游的私有清洗函数会把方向搞反。

    :param text: 任意值（模型可能给非字符串）。
    :param limit: 最大字符数。
    :return: 压平并截断后的单行文本。
    """
    raw = str(text or "")
    if not raw.strip():
        return ""
    # 先在换行处切，再在句读后切（``(?<=[。！？；])`` 保留句号本身）。
    pieces = [part.strip() for part in re.split(r"[\r\n]+|(?<=[。！？；])", raw)]
    flat = "；".join(part for part in pieces if part)
    # 去掉制表符与连续空格（模型常给缩进），但保留单个空格（英文词间需要）。
    flat = " ".join(flat.split())
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 1)] + "…"


def parse_judge_response(text: str) -> JudgeResult:
    """解析模型输出。

    模型不总是听话：可能包 markdown、可能前后带寒暄。所以先剥围栏，再截取
    第一个 ``{`` 到最后一个 ``}`` 之间的部分，尽量捞出一个 JSON。

    :param text: 模型原始输出。
    :return: 判定结果；解析不出来时 ``degraded=True``。
    """
    raw = str(text or "").strip()
    if not raw:
        return JudgeResult(degraded=True, raw="")

    payload = _strip_code_fence(raw)
    start = payload.find("{")
    end = payload.rfind("}")
    if start < 0 or end <= start:
        return JudgeResult(degraded=True, raw=raw[:500])

    try:
        data = json.loads(payload[start : end + 1])
    except (ValueError, TypeError):
        return JudgeResult(degraded=True, raw=raw[:500])

    if not isinstance(data, dict):
        return JudgeResult(degraded=True, raw=raw[:500])

    drift = str(data.get("drift") or "").strip().lower()
    # reason / evidence 是模型自由发挥的字段，直接落库会带出换行与超长文本，
    # 而它们要经过时间线、晨报、webhook 三处按行/按长度排版的消费者。
    # 两个上限是分开的：reason 是给人读的判定理由（可以长一点），
    # evidence 是要塞进纠偏提示的片段（必须短，inject.py 还会再截到 200）。
    reason = _one_line(data.get("reason"), _MAX_REASON_CHARS)
    evidence = _one_line(data.get("evidence"), _MAX_EVIDENCE_CHARS)

    if drift == DRIFT_DANGEROUS:
        verdict = Verdict(
            verdict=VERDICT_DANGEROUS,
            severity=SEVERITY_RED_LINE,
            rule_id="l2.dangerous_intent",
            reason=reason or "语义判定：动作本身有害",
            evidence=evidence,
            category="intent",
        )
    elif drift == DRIFT_SUSPECT:
        verdict = Verdict(
            verdict=VERDICT_DANGEROUS,
            severity=SEVERITY_STANDARD,
            rule_id="l2.intent_drift",
            reason=reason or "语义判定：动作已偏离最初目标",
            evidence=evidence,
            category="intent",
        )
    elif drift == DRIFT_ON_TRACK:
        verdict = Verdict(verdict=VERDICT_ON_TRACK, severity=SEVERITY_NONE, rule_id="", reason=reason, evidence="")
    else:
        # drift 字段缺失或写了别的值 —— 当成解析失败，不要猜
        return JudgeResult(degraded=True, raw=raw[:500])

    return JudgeResult(verdict=verdict, called=True, raw=raw[:500])


class SemanticJudge:
    """带节流的语义判定器。

    :ivar judge_fn: 真正调模型的函数，签名 ``(prompt) -> str``。为 ``None`` 时本层
        自动降级（只说明「没法判」，不影响 L1）。
    :ivar min_interval: 两次调用最小间隔（秒）。
    :ivar window: 每次喂最近多少个动作。
    :ivar max_calls_per_hour: 滑动窗口内的调用上限。
    """

    def __init__(
        self,
        judge_fn: Callable[[str], str] | None = None,
        *,
        min_interval: float = 20.0,
        window: int = 10,
        max_calls_per_hour: int = 120,
    ) -> None:
        self.judge_fn = judge_fn
        self.min_interval = max(0.0, float(min_interval))
        # 上限夹在 _MAX_ACTIONS：window 是「喂多少条动作给模型」，配大了会让每次判定
        # 的 prompt 与 token 成本线性上涨（配置项也可能被误填成 1000）。
        self.window = min(_MAX_ACTIONS, max(1, int(window)))
        self.max_calls_per_hour = max(1, int(max_calls_per_hour))
        self._last_call_ts = 0.0
        self._call_marks: list[float] = []
        self._last_drift_key = ""

    # -- 节流 ---------------------------------------------------------------
    def _throttled(self, now: float) -> bool:
        """判断本次调用是否应被节流。

        :param now: 当前时间戳。
        :return: 是否跳过。
        """
        if self.min_interval and (now - self._last_call_ts) < self.min_interval:
            return True
        cutoff = now - 3600.0
        self._call_marks = [mark for mark in self._call_marks if mark >= cutoff]
        return len(self._call_marks) >= self.max_calls_per_hour

    # -- 判定 ---------------------------------------------------------------
    def judge(
        self,
        *,
        goal: str,
        actions: Sequence[str],
        context: str = "",
    ) -> JudgeResult:
        """做一次语义判定。

        :param goal: 用户最初交代的目标。
        :param actions: 最近的动作摘要。
        :param context: 额外上下文。
        :return: 判定结果。``judge_fn`` 缺失/抛异常/输出不可解析时 ``degraded=True``。
        """
        if self.judge_fn is None:
            return JudgeResult(degraded=True)

        recent = [str(item) for item in list(actions)[-self.window :] if str(item or "").strip()]
        if not recent:
            # 没有动作可判，别浪费一次调用
            return JudgeResult(skipped=True)

        now = time.time()
        if self._throttled(now):
            return JudgeResult(skipped=True)

        # 记账必须发生在调用**之前**。放在调用之后的话，「模型槽位没绑 → 403」
        # 「网关不可用」这类**持续失败**不会推进 _last_call_ts，于是下一行输出
        # 又会再试一次：min_interval 在失败路径上形同虚设，而失败恰恰是最容易
        # 连续发生的情形（没绑槽位的用户，每一次输出都烧一次必然失败的调用）。
        # 判据是「尝试过」而不是「成功过」—— 节流约束的是成本，不是结果。
        self._last_call_ts = now
        self._call_marks.append(now)

        prompt = build_judge_prompt(goal, recent, context=context)
        try:
            raw = self.judge_fn(prompt)
        except Exception as exc:  # noqa: BLE001 —— 模型侧任何失败都只能降级
            logger.debug("L2 判定调用失败（已降级）：%s: %s", type(exc).__name__, exc)
            return JudgeResult(degraded=True)

        result = parse_judge_response(str(raw or ""))

        # 同一路漂移不重复通报：上层每轮都会问，重复报会把告警刷屏
        key = f"{result.verdict.rule_id}:{result.verdict.evidence}"
        if result.drift and key == self._last_drift_key:
            result = JudgeResult(verdict=result.verdict, called=True, skipped=True, raw=result.raw)
        elif result.drift:
            self._last_drift_key = key
        elif not result.degraded:
            self._last_drift_key = ""

        return result

    def reset(self) -> None:
        """清空节流与去重状态（换会话时调用）。"""
        self._last_call_ts = 0.0
        self._call_marks.clear()
        self._last_drift_key = ""

    def capabilities(self) -> dict[str, Any]:
        """报告本层当前能力，供诊断入口显示。

        :return: ``{"available": bool, "min_interval": float, "window": int}``。
        """
        return {
            "available": self.judge_fn is not None,
            "min_interval": self.min_interval,
            "window": self.window,
            "max_calls_per_hour": self.max_calls_per_hour,
        }
