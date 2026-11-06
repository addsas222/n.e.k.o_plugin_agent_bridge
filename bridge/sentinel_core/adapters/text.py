"""兜底方言 —— 没有任何结构的日志也要产出事件。

为什么需要它
============

矩阵里的 9+ 个 Agent 大多只有人话输出：有的每行「正在读取 x.py」，有的干脆只
在最后打印一段总结。如果适配层只认那三家结构化协议，绝大多数派发就一条事件都
没有，时间线会空着——那和没有这个功能一样。

所以这里立一条最弱的约定：**非空行都是一条 ``text`` 事件**，另外补两件几乎所有
CLI 都成立的判断：

* **第一段非空行是目标**：用户在任务里写的意图通常就是输出的第一句（或 CLI 回显的
  prompt 首行），把它标成 ``kind="goal"``，时间线才知道「这一路是为什么跑的」；
* **含 error/错误/失败 的行是错误**：这是唯一一类跨方言都靠得住的关键词特征。

第一行如果本身就是错误行，**不**把它算作目标（否则一次「一启动就报错」的运行会被
记成「目标是报错」），目标留给后面第一段正常输出。
"""

from __future__ import annotations

import re
import time

from .base import KIND_ERROR, KIND_GOAL, KIND_TEXT, LineEvent, brief, clip

__all__ = ["TextAdapter"]

#: 跨方言通用的失败关键词。大小写不敏感；中文三个词覆盖「错误/失败/出错」的常见写法。
_ERROR_RE = re.compile(r"error|错误|失败", re.IGNORECASE)


class TextAdapter:
    """纯文本日志方言（兜底）。

    带一点状态：``goal`` 只认第一段非空行，所以要记住有没有认过。实例不要跨会话
    复用 —— ``get_adapter`` / ``detect_adapter`` 每次都返回新实例正是为此。
    """

    name = "text"

    def __init__(self) -> None:
        self._goal_seen = False

    def parse(self, line: str) -> LineEvent | None:
        """非空行一律产出一条事件；空行返回 ``None``。

        :param line: 原始行。
        :return: 事件，或 ``None``。
        """
        text = (line or "").strip()
        if not text:
            return None
        ts = time.time()
        failed = _ERROR_RE.search(text) is not None
        if failed:
            return LineEvent(ts=ts, kind=KIND_ERROR, text=brief(text, 200), raw=clip(line))
        if not self._goal_seen:
            self._goal_seen = True
            return LineEvent(ts=ts, kind=KIND_GOAL, text=brief(text, 160), raw=clip(line))
        return LineEvent(ts=ts, kind=KIND_TEXT, text=brief(text, 200), raw=clip(line))

    def sniff(self, sample: str) -> bool:
        """兜底方言对任何样本都说「像」。

        它永远排在检测顺序的最后，``detect_adapter`` 只在别的方言一条都命中不了时
        才退到这里，所以这里返回真是安全的。

        :param sample: 样本（不使用）。
        :return: 恒为真。
        """
        return True
