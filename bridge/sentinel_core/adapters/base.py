"""方言适配层的地基 —— 把「一行日志」翻译成一条事件。

为什么有这个模块
================

``worker.py`` 只会把日志行原样尾随，于是上层只知道「有输出在流」，不知道
「Agent 正在干什么」。而不同 CLI 的输出形态差异极大：

* ``claude`` 每条是 Anthropic 消息对象，动作藏在 ``message.content[].tool_use`` 里；
* ``codex`` 每条是 rollout 事件，动作藏在 ``payload`` 的 ``function_call`` / ``custom_tool_call`` 里；
* ``mcode`` 每条是 headless 事件包，动作藏在 ``message`` / ``generic`` 里；
* 剩下的 CLI 什么都没有，只有一坨人类可读文本。

要让上层（时间线、晨报、账本）统一消费，必须先约定一个**最小公共结构**。
本模块就是这个结构：``LineEvent`` + ``Adapter`` 协议。

设计取舍
========

* **fail-open 到底**：``parse`` 只允许两种结果 —— 一条事件，或 ``None``。
  任何 JSON 语法错误、字段缺失、类型意外都退化成 ``None``，绝不抛异常。
  理由和 ``rules.py`` 一样：观察层不能成为可用性故障点，它跑在读取线程里，
  抛一次异常就可能带走整个输出收集（见 ``runner._PipeReader`` 的说明）。
* **只认结构化特征**：判定「这是哪家的日志」靠的是各家协议的字段名（
  ``tool_use`` / ``custom_tool_call`` / ``chunkIndex``），不是自然语言猜测。
* **摘要一律一句话**：``text`` 是给人看的，超过一行的内容压成一行并截断；
  完整原文留在 ``raw`` 里（截到 ``RAW_LIMIT``），需要深究时再去翻。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

__all__ = [
    "KIND_COMMAND",
    "KIND_ERROR",
    "KIND_FILE",
    "KIND_GOAL",
    "KIND_TEXT",
    "KIND_TOOL",
    "RAW_LIMIT",
    "Adapter",
    "LineEvent",
    "brief",
    "clip",
    "iso_ts",
    "loads_object",
]

#: 事件种类。上层按它分组（「它跑了哪些命令」「它改了哪些文件」）。
KIND_TOOL = "tool"
KIND_COMMAND = "command"
KIND_FILE = "file"
KIND_ERROR = "error"
KIND_TEXT = "text"
KIND_GOAL = "goal"

RAW_LIMIT = 2000
"""``raw`` 字段的字符上限。

账本是 SQLite，一条 rollout 事件可以是几十 KB 的补丁正文；原样入库会让
「查一下昨晚发生了什么」变成读整个磁盘。留 2000 字符足够认出这条是什么。
"""

_WS_RE = re.compile(r"\s+")


@dataclass
class LineEvent:
    """一条归一化后的输出事件。

    :ivar ts: 事件时间（秒，Unix 时间戳）。行里带时间就取行里的，没带就取解析时刻。
    :ivar kind: 种类，取值见 ``KIND_*``。
    :ivar text: 一句话摘要（时间线直接显示这一句）。
    :ivar tool: 工具名，``kind == "tool"`` 时有值。
    :ivar path: 涉及的文件，``kind == "file"`` 时有值。
    :ivar raw: 原始行（截断到 ``RAW_LIMIT``）。
    """

    ts: float
    kind: str
    text: str
    tool: str = ""
    path: str = ""
    raw: str = ""


class Adapter(Protocol):
    """一家 CLI 的输出方言。

    实现方只承诺两件事：``parse`` 永不抛异常、``sniff`` 判断这份日志像不像自己。
    """

    name: str

    def parse(self, line: str) -> LineEvent | None:
        """把一行日志翻译成事件；翻译不出来返回 ``None``。

        :param line: 原始行（不带换行符）。
        :return: 事件，或 ``None``。
        """
        ...

    def sniff(self, sample: str) -> bool:
        """判断一份日志样本像不像本方言。

        :param sample: 多行样本（通常会传日志开头若干行）。
        :return: 像则为真。
        """
        ...


def clip(text: Any, limit: int = RAW_LIMIT) -> str:
    """把任意值压成不超过 ``limit`` 字符的字符串。

    :param text: 原值（``None`` 转成空串）。
    :param limit: 上限。
    :return: 截断后的字符串。
    """
    raw = "" if text is None else str(text)
    return raw if len(raw) <= limit else raw[:limit]


def brief(text: Any, limit: int = 120) -> str:
    """压成一行摘要：换行/制表变空格，多余空白合并，超长截断。

    日志里常有整段补丁或 JSON 挂在同一行，直接塞进时间线会把界面撑坏。

    :param text: 原值。
    :param limit: 上限字符数。
    :return: 单行摘要。
    """
    flat = _WS_RE.sub(" ", "" if text is None else str(text)).strip()
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 1)] + "…"


def loads_object(line: str) -> dict[str, Any] | None:
    """把一行按 JSON 对象解析，**不是对象就返回 ``None``**。

    三家结构化方言都是「一行一个 JSON 对象」（JSONL / NDJSON），所以这是共用入口。
    数组、数字、字符串都当解析失败 —— 那些不是任何一家的行协议。

    :param line: 原始行。
    :return: 字典，或 ``None``。
    """
    text = (line or "").strip()
    if not text or text[0] != "{":
        return None
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def iso_ts(value: Any, default: float | None = None) -> float:
    """解析 ISO-8601 时间戳（各家都用 ``2026-10-30T12:00:00.123Z`` 这种）。

    解析不出来就退到 ``default``，再不行退到当前时刻 —— 时间戳坏了不该让整条
    事件丢掉，内容比时间重要。

    :param value: 行里的时间字段。
    :param default: 兜底时间戳。
    :return: Unix 时间戳（秒）。
    """
    fallback = time.time() if default is None else default
    if isinstance(value, (int, float)) and value > 0:
        # 有些方言给的是毫秒，按量级区分：超过 1e11 认为是毫秒
        return float(value) / 1000.0 if float(value) > 1e11 else float(value)
    text = str(value or "").strip()
    if not text:
        return fallback
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except (ValueError, OSError, OverflowError):
        return fallback
