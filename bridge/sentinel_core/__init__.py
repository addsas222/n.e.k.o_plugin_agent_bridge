"""内置哨兵 —— agent_bridge 自带的安全审计内核。

为什么有这个模块
================

上游 Sentinel 是一个九万行的桌面审计产品（GUI、进程树扫描、多级模型路由、
打包 exe）。Agent Bridge 需要的不是那个外壳，而是它的**审计能力**：派发前
看一眼任务能不能做、派发后盯着它有没有跑偏、出事之后能查、能回滚、能通知。

所以这里按「能力」而不是「产品」重新实现了一份：纯标准库、无第三方依赖、
可进程内嵌，也可用 ``python -m bridge.sentinel_core`` 单独跑一个守护进程。

能力清单
========

============  ==========================================================
L1 规则       命令解析 + 红线/普通风险判定（派发前与运行中都走）
L2 语义       意图漂移检测，需注入模型回调；不可用时自动降级
账本          事件 / 会话 / 告警 / 安全点 / Token，SQLite 落盘
时间线        把事件串成「它怎么一步步走到这一步」，带噪声折叠
晨报          聚合昨夜事件成一份早上能读的摘要
Token 审计    只记「拦截挽回」；消耗字段无采集源，恒 0（别拿它做成本核算）
安全点        派发前打快照（git），支持回滚
干预          终止进程树（挂起/恢复保留为能力，策略层不选，理由见 README）
告警通道      本地日志 + webhook 投递
时间盒        单个任务吃掉整轮时预警
HTTP 端点     health / pre-check / attach / state（与上游契约一致）
============  ==========================================================

**没有**：GUI 桌面外壳、进程树全盘扫描、多级模型路由、单文件 exe 打包。
这些属于上游那套桌面产品，本模块不做。

三层结构与降级约定
==================

* **L1** 纯规则、毫秒级、永远可用，**从不等 L2**。
* **L2** 要调模型、有成本，可缺省、可跳过、可降级；它不可用时只有 L1 在守。
* **L3** 留给上层自行编排，不在本模块范围。

用法
====

进程内直接判定（最轻）::

    from bridge.sentinel_core import pre_check
    result = pre_check("rm -rf / --no-preserve-root")
    assert result["action"] == "block"

完整运行时（账本 + 会话 + 输出流 + 告警）::

    from bridge.sentinel_core import SentinelRuntime
    runtime = SentinelRuntime(config.sentinel, judge_fn=my_llm_call)
    runtime.pre_check("写个快速排序")

起一个本地服务::

    python -m bridge.sentinel_core --port 8787
"""

from __future__ import annotations

# 门面导出刻意做成**惰性**的，不是手滑。
#
# 这里的每个模块都只依赖标准库，但「只依赖标准库」不等于「加载免费」：
# sqlite3 + urllib.request + http.server + ctypes 在本机实测合计约 425ms。
# 而插件默认 `sentinel.enabled = false` —— 绝大多数用户根本不会用到这一整包。
#
# 顶层 eager import 会让「默认关闭」变成一句空话：README 与 config.example.toml
# 都写着「以下能力默认全关」，那加载期就不该先付这份开销。更糟的是插件根的
# `try: import SentinelRuntime / except ImportError: 无哨兵` 会把「导入炸了」
# 伪装成「这台机器不支持」—— 一个真实的语法错误会静默降级成功能缺失。
#
# 用 PEP 562 的模块级 __getattr__ 做惰性导出：`from .sentinel_core import X`
# 仍然照常可用（内部触发 __getattr__，只加载 X 所在的那一个模块），而
# `import bridge.sentinel_core` 本身什么都不加载。
_EXPORTS: dict[str, str] = {
    # 规则与预检
    "ACTION_ALLOW": "precheck",
    "ACTION_BLOCK": "precheck",
    "SEVERITY_NONE": "rules",
    "SEVERITY_RED_LINE": "rules",
    "SEVERITY_STANDARD": "rules",
    "VERDICT_DANGEROUS": "rules",
    "VERDICT_LOOP": "rules",
    "VERDICT_ON_TRACK": "rules",
    "LoopDetector": "rules",
    "RuleEngine": "rules",
    "Verdict": "rules",
    "build_pre_check_event": "precheck",
    "pre_check": "precheck",
    # 会话与上下文
    "SessionContext": "context",
    "SessionRegistry": "context",
    "build_fingerprint": "context",
    # 账本
    "EventStore": "store",
    "default_db_path": "store",
    "resolve_db_path": "store",
    # 语义判定与 Token 审计
    "JudgeResult": "judge",
    "SemanticJudge": "judge",
    "build_judge_prompt": "judge",
    "parse_judge_response": "judge",
    "TokenLedger": "metrics",
    "estimate_tokens": "metrics",
    "render_usage": "metrics",
    # 干预与回滚
    "ProcessGuard": "guard",
    "SafePointManager": "safepoint",
    # 告警与呈现
    "ChannelHub": "channels",
    "LocalChannel": "channels",
    "WebhookChannel": "channels",
    "TimeBox": "timebox",
    "build_briefing": "briefing",
    "build_correction_prompt": "inject",
    "build_resume_hint": "inject",
    "build_timeline": "timeline",
    "render_briefing": "briefing",
    "render_timeline": "timeline",
    # 编排层与独立守护进程
    "DaemonApp": "server",
    "LocalHTTPServer": "server",
    "SentinelRuntime": "runtime",
    "VERSION": "server",
    "build_app": "server",
    "serve": "server",
}

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
    "ChannelHub",
    "DaemonApp",
    "EventStore",
    "JudgeResult",
    "LocalChannel",
    "LocalHTTPServer",
    "LoopDetector",
    "ProcessGuard",
    "RuleEngine",
    "SafePointManager",
    "SemanticJudge",
    "SentinelRuntime",
    "SessionContext",
    "SessionRegistry",
    "TimeBox",
    "TokenLedger",
    "Verdict",
    "WebhookChannel",
    "build_app",
    "build_briefing",
    "build_correction_prompt",
    "build_fingerprint",
    "build_judge_prompt",
    "build_pre_check_event",
    "build_resume_hint",
    "build_timeline",
    "default_db_path",
    "estimate_tokens",
    "parse_judge_response",
    "pre_check",
    "render_briefing",
    "render_timeline",
    "render_usage",
    "resolve_db_path",
    "serve",
]


def __getattr__(name: str) -> object:
    """按需加载门面导出。

    只在属性缺失时被触发；命中后写回 ``globals()``，后续访问不再走这里。
    未声明的名字照常抛 ``AttributeError``，不要静默返回 None —— 打错名字要立刻暴露。
    """
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module = import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """让 ``dir()`` 与补全仍能看到全部导出名（惰性不影响可发现性）。"""
    return sorted(set(globals()) | set(__all__))
