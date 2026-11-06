"""时间盒 —— 防单个子任务把整轮挂死。

为什么有这个模块
================

夜间无人值守时，最贵的故障不是「报错」，而是「不报错地卡住」：一个子任务编译卡在
交互提示上、一次 ```git clone`` 卡在网络上，整轮就一直挂到早上。派发方（``worker``）
本身只知道「进程还活着」，不知道「它已经没有意义了」。

时间盒给出一个统一的、极便宜的判据：**这一个子任务已经跑了多久**。超过预算就交给
上层决定——提醒、追加干预指令，或者直接终止。本模块只负责「测量与分级」，不做
任何终止动作（终止是干预层的事，避免越权）。

设计取舍
========

* **纯计算、可注入时间**：``check`` 接受 ``now``，因此测试不需要 ``sleep``，也不会
  因为机器慢而抖动。
* **warn 只打扰一次**：同一个 ``started_at`` 只提醒一次。重复提醒是噪声，而噪声
  会让人关掉告警——这是比不提醒更坏的结局。
* **``limit<=0`` 表示关闭**：退回「不设时间盒」的老行为，方便一键关掉。
* **fail-open**：内部记账出任何问题都只能导致「少提醒一次」，绝不能反过来让派发
  失败。
"""

from __future__ import annotations

import threading
import time
from typing import Any

__all__ = ["TimeBox"]

#: 提醒级别的字面量。
LEVEL_WARN = "warn"
LEVEL_EXPIRED = "expired"


class TimeBox:
    """单个子任务的时间预算。

    :ivar limit_seconds: 预算秒数；``<= 0`` 视为关闭。
    :ivar warn_ratio: 到达 ``limit * warn_ratio`` 时先给一次 ``warn``。
    """

    def __init__(self, limit_seconds: float, *, warn_ratio: float = 0.8) -> None:
        try:
            self.limit_seconds = float(limit_seconds)
        except (TypeError, ValueError):
            self.limit_seconds = 0.0
        try:
            self.warn_ratio = float(warn_ratio)
        except (TypeError, ValueError):
            self.warn_ratio = 0.8
        # 预算非正时直接关掉，避免下游到处判断。
        self.enabled = self.limit_seconds > 0
        self.warn_ratio = min(1.0, max(0.0, self.warn_ratio))
        # 同一起点的 warn 只发一次；用集合记住哪些起点已经提醒过。
        self._warned: set[float] = set()
        # 起点 → 标签。check 只看得到 started_at，标签得在这里存着。
        self._labels: dict[float, str] = {}
        # 派发线程 start、监控线程 check，两者并发是常态。
        self._lock = threading.Lock()

    # -- 生命周期 -----------------------------------------------------------
    def start(self, *, label: str = "") -> float:
        """开始计时。

        返回的时间戳就是后续 ``check`` / ``elapsed`` 的凭据，调用方自己保存
        （不放进实例属性，因为同一实例可能同时给多个子任务计时）。

        :param label: 人可读标签（Agent 名 / 任务摘要），会出现在告警里。
        :return: 起始时间戳。
        """
        now = time.time()
        try:
            with self._lock:
                self._labels[now] = str(label or "")
                self._prune(now)
        except Exception:  # noqa: BLE001 —— 记不住标签最多让告警少一行信息
            pass
        return now

    def elapsed(self, started_at: float, *, now: float | None = None) -> float:
        """已经过去多久。

        :param started_at: ``start`` 的返回值。
        :param now: 注入的当前时间戳（测试用）。
        :return: 秒数；``started_at`` 非法或时钟倒退时返回 0。
        """
        current = time.time() if now is None else float(now)
        try:
            delta = current - float(started_at)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, delta)

    def check(self, started_at: float, *, now: float | None = None) -> dict | None:
        """检查是否该提醒或已经超时。

        :param started_at: ``start`` 的返回值。
        :param now: 注入的当前时间戳（测试用）。
        :return: ``None`` 表示正常；否则为
            ``{"level", "elapsed", "limit", "label", "overrun"}``。
        """
        if not self.enabled:
            return None
        try:
            spent = self.elapsed(started_at, now=now)
            limit = self.limit_seconds
            label = self._label_for(started_at)
            if spent >= limit:
                return {
                    "level": LEVEL_EXPIRED,
                    "elapsed": spent,
                    "limit": limit,
                    "label": label,
                    "overrun": spent - limit,
                }
            if spent >= limit * self.warn_ratio:
                if not self._mark_warned(started_at):
                    # 提醒过就安静：重复打扰会训练人忽略告警。
                    return None
                return {
                    "level": LEVEL_WARN,
                    "elapsed": spent,
                    "limit": limit,
                    "label": label,
                    "overrun": 0.0,
                }
        except Exception:  # noqa: BLE001 —— 时间盒坏掉只能导致少提醒，不许影响派发
            return None
        return None

    # -- 内部 ---------------------------------------------------------------
    def _label_for(self, started_at: Any) -> str:
        """取起点对应的标签。

        :param started_at: 起始时间戳。
        :return: 标签；没有则为空串。
        """
        try:
            key = float(started_at)
        except (TypeError, ValueError):
            return ""
        with self._lock:
            return self._labels.get(key, "")

    def _mark_warned(self, started_at: Any) -> bool:
        """登记「这个起点已经提醒过」。

        :param started_at: 起始时间戳。
        :return: 首次登记为 ``True``，重复登记为 ``False``。
        """
        try:
            key = float(started_at)
        except (TypeError, ValueError):
            return True
        with self._lock:
            if key in self._warned:
                return False
            self._warned.add(key)
            return True

    def _prune(self, now: float) -> None:
        """清理过老的记账，避免长跑进程里集合无限增长。

        保留窗口取 ``max(limit*10, 10 分钟)``：远大于任何一次合理的子任务时长。

        :param now: 当前时间戳。
        """
        window = max(self.limit_seconds * 10, 600.0)
        cutoff = now - window
        self._warned = {key for key in self._warned if key >= cutoff}
        self._labels = {key: value for key, value in self._labels.items() if key >= cutoff}
