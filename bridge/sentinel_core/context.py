"""会话登记表 —— 按指纹隔离每一路挂载的 Agent。

同一路会话的所有后续事件都靠指纹对上号，所以指纹必须**稳定**：同样的
``cwd + session_id + pid`` 每次都要算出同一个值，否则会分裂成两路、
各自只看到半个窗口。

字段与原版守护进程的快照保持一致，方便调用方无缝切换。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["FINGERPRINT_MAX_CHARS", "SessionContext", "SessionRegistry", "build_fingerprint"]

#: 指纹长度上限。
FINGERPRINT_MAX_CHARS = 512


def build_fingerprint(
    *,
    cwd: str = "",
    session_id: str = "",
    pid: int = 0,
) -> str:
    """拼出会话指纹。

    优先级从强到弱：``session_id`` → ``cwd`` → ``pid``。三者都没有时返回空串
    （这一路无法追踪，调用方应视为未挂载）。

    :param cwd: 工作目录。
    :param session_id: Agent 会话 ID。
    :param pid: 进程 ID。
    :return: 指纹字符串。
    """
    parts = [
        str(session_id or "").strip(),
        str(cwd or "").strip(),
        str(pid or "").strip(),
    ]
    parts = [part for part in parts if part]
    if not parts:
        return ""
    return "|".join(parts)[:FINGERPRINT_MAX_CHARS]


@dataclass
class SessionContext:
    """一路会话的上下文。

    :ivar fingerprint: 会话指纹。
    :ivar session_id: Agent 给的会话 ID。
    :ivar cwd: 工作目录。
    :ivar pid: 被监控进程的 PID。
    :ivar agent: Agent 类型。
    :ivar goal: 从日志首句拔出来的目标（拔不到就是空串）。
    :ivar log_path: 会话日志路径。
    :ivar events: 已处理的输出行数。
    :ivar alerts: 已触发的告警数。
    :ivar last_verdict: 最近一次判定。
    :ivar last_reason: 最近一次判定说明。
    :ivar created_at: 建立时间。
    :ivar updated_at: 最后更新时间。
    """

    fingerprint: str
    session_id: str = ""
    cwd: str = ""
    pid: int = 0
    agent: str = ""
    goal: str = ""
    log_path: str = ""
    events: int = 0
    alerts: int = 0
    last_verdict: str = ""
    last_reason: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    #: 保护计数字段的自增。
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def l2_sleeping(self) -> bool:
        """是否处于 L2 休眠。

        精简版没有模型级判定，这一路永远是醒着的；保留该字段只为对齐快照格式。
        """
        return False

    def touch(self) -> None:
        """刷新更新时间。"""
        self.updated_at = time.time()

    def record(self, *, verdict: str, reason: str, alert: bool) -> None:
        """记录一次判定结果。

        :param verdict: 判定值。
        :param reason: 说明。
        :param alert: 是否同时算一次告警。
        """
        with self.lock:
            self.events += 1
            if alert:
                self.alerts += 1
            self.last_verdict = str(verdict or "")
            self.last_reason = str(reason or "")
            self.updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 化的快照。

        :return: 快照字典。
        """
        return {
            "fingerprint": self.fingerprint,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "pid": self.pid,
            "agent": self.agent,
            "goal": self.goal,
            "log_path": self.log_path,
            "l2_sleeping": self.l2_sleeping,
            "events": self.events,
            "alerts": self.alerts,
            "last_verdict": self.last_verdict,
            "last_reason": self.last_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class SessionRegistry:
    """线程安全的会话登记表。"""

    def __init__(self) -> None:
        self._items: dict[str, SessionContext] = {}
        self._lock = threading.Lock()

    def get_or_create(self, fingerprint: str, **fields: Any) -> SessionContext:
        """取已有会话，没有就建一个。

        :param fingerprint: 会话指纹。
        :param fields: 建会话时用的字段。
        :return: 会话上下文。
        """
        with self._lock:
            context = self._items.get(fingerprint)
            if context is None:
                context = SessionContext(fingerprint=fingerprint, **fields)
                self._items[fingerprint] = context
            return context

    def get(self, fingerprint: str) -> SessionContext | None:
        """按指纹取会话。

        :param fingerprint: 会话指纹。
        :return: 会话上下文；不存在返回 ``None``。
        """
        with self._lock:
            return self._items.get(fingerprint)

    def drop(self, fingerprint: str) -> bool:
        """移除一路会话。

        :param fingerprint: 会话指纹。
        :return: 是否真的移除了。
        """
        with self._lock:
            return self._items.pop(fingerprint, None) is not None

    def snapshot(self) -> list[dict[str, Any]]:
        """全部会话的快照。

        :return: 快照列表。
        """
        with self._lock:
            contexts = list(self._items.values())
        return [item.to_dict() for item in contexts]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
