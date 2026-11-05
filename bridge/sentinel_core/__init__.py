"""内置精简哨兵 —— 派发前的 L1 安全闸门。

为什么有这个模块
================

上游 Sentinel 是一个 9 万行的桌面审计产品，其中真正被 Agent Bridge 需要的
只有「派发前问一句能不能做」这一层。把整个上游拖进来代价太大（体积、
GPL 传染、依赖 pyyaml），所以这里按公开的 HTTP 契约和威胁类别重新实现了一份
只做这件事的版本。

能力边界
========

**有**：红线级命令识别（删根、抹盘、下载即执行）、普通高危命令告警、
挂载后的日志尾随监控（给了日志路径才生效）、四个端点与会话快照。

**没有**：GUI、进程树扫描、多模型语义判定（L2/L3）、自动猜日志路径。
这些属于上游那套桌面产品，本模块不打算复刻。

用法
====

进程内直接判定（最常用）::

    from bridge.sentinel_core import pre_check
    result = pre_check("rm -rf / --no-preserve-root")
    assert result["action"] == "block"

起一个本地服务::

    python -m bridge.sentinel_core --port 8787
"""

from __future__ import annotations

from .context import SessionContext, SessionRegistry, build_fingerprint
from .precheck import ACTION_ALLOW, ACTION_BLOCK, build_pre_check_event, pre_check
from .rules import (
    SEVERITY_NONE,
    SEVERITY_RED_LINE,
    SEVERITY_STANDARD,
    VERDICT_DANGEROUS,
    VERDICT_LOOP,
    VERDICT_ON_TRACK,
    LoopDetector,
    RuleEngine,
    Verdict,
)
from .server import VERSION, DaemonApp, LocalHTTPServer, build_app, serve

__all__ = [
    "ACTION_ALLOW",
    "ACTION_BLOCK",
    "SEVERITY_NONE",
    "SEVERITY_RED_LINE",
    "SEVERITY_STANDARD",
    "VERSION",
    "VERDICT_DANGEROUS",
    "VERDICT_LOOP",
    "VERDICT_ON_TRACK",
    "DaemonApp",
    "LocalHTTPServer",
    "LoopDetector",
    "RuleEngine",
    "SessionContext",
    "SessionRegistry",
    "Verdict",
    "build_app",
    "build_fingerprint",
    "build_pre_check_event",
    "pre_check",
    "serve",
]
