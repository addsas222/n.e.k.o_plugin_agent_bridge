"""挂载后的后台静默监控 —— 尾随日志，命中红线就记账。

与原版的差别
============

原版会去扫描磁盘猜「这一路 Agent 把会话日志写在哪」，那是一整套文件系统
雷达。精简版不做猜测：**给了 ``log_path`` 才监控**，没给就只登记会话。

这个取舍的理由是：猜路径依赖各 Agent 的目录布局，属于易变形的高维护成本
逻辑；而调用方（Agent Bridge）本来就知道自己的子进程在写什么日志，直接传
进来既准确又便宜。

监控是**只读**的：只读文件、只记账、绝不碰被监控的进程。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

from .context import SessionContext
from .rules import RuleEngine

__all__ = ["SessionWorker"]

logger = logging.getLogger(__name__)

#: 单次轮询间隔（秒）。
_POLL_INTERVAL = 0.5
#: 单次最多读多少字节 —— 防止一个巨大的日志把内存吃光。
_READ_LIMIT = 256 * 1024


class SessionWorker:
    """一路会话的监控线程。

    :param context: 会话上下文。
    :param rules: 规则引擎（每路独享，避免滑窗互相污染）。
    :param on_alert: 命中红线时的回调；缺省只记日志。
    """

    def __init__(
        self,
        context: SessionContext,
        rules: RuleEngine,
        *,
        on_alert: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.context = context
        self.rules = rules
        self.on_alert = on_alert
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"sentinel-watch-{context.fingerprint[:24]}", daemon=True
        )

    # -- 生命周期 --------------------------------------------------------
    def start(self) -> None:
        """启动监控线程。没有日志路径时直接不启动。"""
        if not self.context.log_path:
            logger.debug("会话 %s 没有 log_path，跳过监控", self.context.fingerprint)
            return
        self._thread.start()

    def stop(self) -> None:
        """请求停止并等待线程退出。"""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    @property
    def alive(self) -> bool:
        """线程是否还在跑。"""
        return self._thread.is_alive()

    # -- 主体 ------------------------------------------------------------
    def _run(self) -> None:
        """尾随日志文件，逐行判定。"""
        path = self.context.log_path
        offset = 0
        while not self._stop.is_set():
            try:
                offset = self._drain(path, offset)
            except OSError as exc:
                logger.debug("读取日志失败（继续重试）：%s: %s", type(exc).__name__, exc)
            except Exception as exc:  # noqa: BLE001 —— 监控线程不能因单次异常退出
                logger.debug("监控循环异常（继续）：%s: %s", type(exc).__name__, exc)
            self._stop.wait(_POLL_INTERVAL)

    def _drain(self, path: str, offset: int) -> int:
        """从偏移处读新增内容并逐行判定。

        :param path: 日志路径。
        :param offset: 当前已读偏移。
        :return: 新的偏移。
        """
        if not os.path.isfile(path):
            return offset
        size = os.path.getsize(path)
        if size < offset:
            offset = 0
        if size == offset:
            return offset

        with open(path, "rb") as handle:
            handle.seek(offset)
            chunk = handle.read(min(size - offset, _READ_LIMIT))
        offset += len(chunk)

        for line in chunk.decode("utf-8", "replace").splitlines():
            text = line.strip()
            if not text:
                continue
            verdict = self.rules.check_text(text)
            if verdict.dangerous:
                self.context.record(
                    verdict=verdict.verdict, reason=verdict.reason, alert=verdict.red_line
                )
                self._fire(verdict, text)
            else:
                self.context.record(verdict=verdict.verdict, reason="", alert=False)
        return offset

    def _fire(self, verdict: Any, line: str) -> None:
        """把一次命中抛给回调。

        :param verdict: 判定结果。
        :param line: 命中的日志行。
        """
        alert = {
            "fingerprint": self.context.fingerprint,
            "session_id": self.context.session_id,
            "agent": self.context.agent,
            "cwd": self.context.cwd,
            "pid": self.context.pid,
            "goal": self.context.goal,
            "rule_id": verdict.rule_id,
            "reason": verdict.reason,
            "severity": verdict.severity,
            "evidence": verdict.evidence or line[:200],
            "ts": time.time(),
        }
        logger.warning(
            "命中 %s（%s）：%s", verdict.rule_id, verdict.severity, verdict.reason
        )
        if self.on_alert is not None:
            try:
                self.on_alert(alert)
            except Exception as exc:  # noqa: BLE001 —— 回调失败不影响监控
                logger.debug("告警回调失败：%s: %s", type(exc).__name__, exc)
