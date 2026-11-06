"""面板数据聚合层 —— 把哨兵状态压成 Hosted 面板能直接渲染的局部 state。

为什么单独一层
==============

面板要把哨兵的「会话 / 告警 / 时间线」展示出来，聚合逻辑**不能**写在
``__init__.py`` 的 ``_ui_panel_state`` 里：本仓库的测试从不实例化插件类
（见 ``tests/_sdk_stub.py`` 与 ``tests/conftest.py``），写在那儿就永远测不到。
抽成纯函数后，面板的每一种状态都能被钉死 —— 尤其是「空表格」的三种成因
（没挂上 / 关了 / 没账本）必须彼此可区分，否则面板会把「没在看」渲染成
「一切正常」，那是审计工具最不该犯的错。

三种「空」的区分方式
====================

``sentinel.state`` 回答「为什么空」，列表字段一律给 ``[]`` 而不给 ``None``：

* ``runtime_missing`` —— 哨兵运行时根本没起来（``on_startup`` 抛错会停在这儿）；
* ``disabled`` —— 配置里关掉了；
* ``no_store`` —— 账本不可用（建表失败 / 路径不可写），一个字节都没落盘；
* ``ok`` —— 哨兵在跑、账本可用。此时列表为空才真的表示「这段时间没有记录」。

``None`` 只出现在 ``__init__.py`` 的外层兜底里（连本模块都整段炸了，state 是编的），
用 ``None`` 是为了让前端别说「没有记录」。**本模块永远不返回 ``None`` 列表。**

为什么入参是 ``store`` 而不是 ``runtime``
=========================================

``SentinelRuntime.timeline()`` 返回的是**渲染好的文本**（``runtime.py`` 里
``return render_timeline(items)``），面板要的是结构化数据。所以这里只依赖
``EventStore`` 的公开方法 + ``sentinel`` 的 ``enabled`` / ``doctor()`` 两样东西 ——
``tests/test_sentinel_panel.py`` 里的 ``_FakeSentinel`` 就只实现这两样，越界读私有
字段会当场红。

失败降级
========

跟同包 ``briefing.py`` 一个原则：**面板不该成为新的故障点**。任何数据源读不出来都
返回零值 / 空列表，不抛异常。审计数据看不到是次要的，面板白屏导致连「点按钮派活」
都做不了才是主要损失。

注意：``state`` 不承担「读失败」的语义。``state`` 只描述**哨兵/账本的停机态**，
``store.stats`` 抛错时 state 仍然是 ``ok`` —— 统计读不出来不代表哨兵没在跑。
"""

from __future__ import annotations

import time
from typing import Any

from .briefing import build_briefing
from .timeline import describe_event, fmt_clock, level_of

__all__ = ["build_panel_state", "degraded_state"]

# -- state 取值 -------------------------------------------------------------

STATE_RUNTIME_MISSING = "runtime_missing"
STATE_DISABLED = "disabled"
STATE_NO_STORE = "no_store"
STATE_OK = "ok"

#: 缺省插件元信息。版本号与 ``plugin.toml`` 的 ``version`` 手工保持一致。
_DEFAULT_PLUGIN: dict[str, Any] = {
    "id": "agent_bridge",
    "name": "Agent 桥接",
    "version": "0.5.0",
    "description": "把本机编码 Agent CLI 桥接成 N.E.K.O 运行时入口（哨兵面板）。",
}

#: 非 ok 态的修法提示。每条都必须指向**该态真正的修法**，不能给通用文案 ——
#: 把「运行时没起来」诊断成「去打开 enabled」会让人白改一遍配置。
_FIX_HINTS: dict[str, str] = {
    STATE_RUNTIME_MISSING: (
        "哨兵运行时没有启动：先看插件日志里启动失败的记录（on_startup 抛错会让运行时停在未启动态，"
        "此时改配置无效）。"
    ),
    STATE_DISABLED: (
        "哨兵在配置里被关掉了：把 config.toml 的 [bridge.sentinel] 节里 enabled 改成 true，然后重启插件。"
    ),
    STATE_NO_STORE: (
        "账本不可用，所以没有任何历史记录：检查 [bridge.sentinel] 的 store_path 是否可写、"
        "以及日志里有没有「账本建表失败」。"
    ),
}


# -- 小工具 -----------------------------------------------------------------


def _as_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _opt_float(value: Any) -> float | None:
    """``None`` 透传成 ``None``（``ended_at`` 用它区分「还在跑」与「已收尾」）。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _section_flag(doctor: dict[str, Any], section: str, key: str = "enabled") -> bool:
    """读 ``doctor()[section][key]``，缺字段一律当 ``False``。"""
    block = doctor.get(section)
    if isinstance(block, dict):
        return bool(block.get(key))
    return False


def _read_doctor(sentinel: Any) -> dict[str, Any]:
    """安全地取 ``sentinel.doctor()``；拿不到就给空字典。

    宿主传进来的东西可能既没有 ``enabled`` 也没有 ``doctor``（见测试里的
    ``object()`` 用例），所以这里全程 ``getattr`` + ``callable`` 判断。
    """
    if sentinel is None:
        return {}
    getter = getattr(sentinel, "doctor", None)
    if not callable(getter):
        return {}
    try:
        raw = getter()
    except Exception:  # noqa: BLE001 —— 诊断自己炸了不该连累面板
        return {}
    return raw if isinstance(raw, dict) else {}


def _resolve_state(sentinel: Any, store_ok: bool) -> str:
    """判定四态。顺序即优先级。

    ``disabled`` 只在 ``enabled`` 属性**明确为 False** 时才成立。宿主可能塞进来一个
    既没有 ``enabled`` 也没有 ``doctor`` 的裸对象，此时 ``getattr`` 的默认值会是
    ``False`` —— 若把它当成「用户关掉了」，就会给出「去打开 enabled」的错误诊断。
    拿不准的时候交由账本可用性决定（``ok`` / ``no_store``）。
    """
    if sentinel is None:
        return STATE_RUNTIME_MISSING
    enabled = getattr(sentinel, "enabled", None)
    if enabled is False:
        return STATE_DISABLED
    if not store_ok:
        return STATE_NO_STORE
    return STATE_OK


# -- 三个列表 ---------------------------------------------------------------


def _collect_sessions(store: Any, *, limit: int, since: float) -> list[dict[str, Any]]:
    """取会话表行。``active`` 由 ``ended_at`` 是否为空决定。

    不给 ``session_id`` 排序或展示：``begin_session`` 从不设它，``upsert_session``
    写的是 ``getattr(session, "session_id", "")``，所以历史上它恒为空串。
    """
    try:
        rows = list(store.sessions(limit=limit, since=since) or [])
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ended_at = _opt_float(row.get("ended_at"))
        out.append(
            {
                "fingerprint": str(row.get("fingerprint") or ""),
                "agent": str(row.get("agent") or ""),
                "cwd": str(row.get("cwd") or ""),
                "goal": str(row.get("goal") or ""),
                "started_at": _as_float(row.get("started_at")),
                "ended_at": ended_at,
                "exit_code": _opt_int(row.get("exit_code")),
                "events": _as_int(row.get("events")),
                "alerts": _as_int(row.get("alerts")),
                # 只给事实：``active`` 就是「没有收尾记录」。**不要**在这里（或前端）
                # 加「挂了多久算僵死」的推断 —— 那既可能是进程被强杀（真异常），也可能
                # 是跑了几小时的构建（完全正常），两者用 ``ended_at`` 区分不了。
                # 真要检测僵死会话，得由运行时按 pid 查进程是否还活着，那才是事实。
                "active": ended_at is None,
            }
        )
    return out


def _collect_alerts(store: Any, *, limit: int, since: float) -> list[dict[str, Any]]:
    """取告警历史（含已投递的），时间倒序。"""
    try:
        rows = list(store.alerts(limit=limit, since=since) or [])
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                # id 是 alerts 表主键。必须给：同一秒可能落两条告警（红线拦截 + 判定升级
                # 常常成对出现），前端拿 ts 做 rowKey 会撞 key。
                "id": _as_int(row.get("id")),
                "ts": _as_float(row.get("ts")),
                "clock": fmt_clock(row.get("ts")),
                "level": str(row.get("level") or ""),
                "title": str(row.get("title") or ""),
                "body": str(row.get("body") or ""),
                "delivered": bool(row.get("delivered")),
                # ``channel`` 区分「外发成功」与「只有本地兜底」：两者都是 delivered=True，
                # 但含义完全不同，面板必须能分开显示。
                "channel": str(row.get("channel") or ""),
            }
        )
    return out


def _collect_timeline(store: Any, *, limit: int, since: float, until: float) -> list[dict[str, Any]]:
    """取事件时间线，**按时间正序**（``store.timeline`` 已内部 reverse）。

    这里刻意用 ``store.timeline`` 的原始行 + ``describe_event`` 单条映射，而不是
    ``build_timeline`` 的折叠结果。**刻意不折叠**：折叠后行数对不上账本，面板要能
    逐条数 —— 一次夜里有 6 条还是 600 条，是用户判断「今晚有多吵」的第一手依据，
    折成「+N 行」就把这个数字藏起来了。另外折叠项（``type: "fold"``）不带
    ``fingerprint``，而面板要支持按会话本地过滤。

    ``describe_event`` 只借用中文描述这一件事，不重构数据形状 —— 别把两者混为一谈。
    """
    try:
        rows = list(store.timeline(since=since, until=until, limit=limit) or [])
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        severity = str(row.get("severity") or "")
        out.append(
            {
                # 面板目前用 List 渲染（按 index 迭代），不需要主键；给出来是为了将来
                # 换成 DataTable 时不必再改这一层。纯附加字段，不影响任何现有消费方。
                "id": _as_int(row.get("id")),
                "ts": _as_float(row.get("ts")),
                "clock": fmt_clock(row.get("ts")),
                "kind": str(row.get("kind") or ""),
                "severity": severity,
                "level": level_of(severity),
                # 中文映射只此一处，复用 timeline 的实现，避免两套文案漂移。
                "text": describe_event(row),
                "fingerprint": str(row.get("fingerprint") or ""),
            }
        )
    return out


def _zero_stats() -> dict[str, Any]:
    """非 ok 态的统计占位：键必须齐，面板才不用做 None 判断。"""
    return {
        "sessions": 0,
        "events": 0,
        "alerts": 0,
        "blocked": 0,
        "by_severity": {},
        "by_level": {},
        # 零值不是「部分」：没账本可读的时候不该额外说数字不完整，那是另一件事
        # （``sentinel.state`` 已经在说）。这里给 False，语义才单一。
        "partial": False,
    }


def _collect_stats(store: Any, *, since: float, until: float) -> dict[str, Any]:
    """复用 ``briefing.build_briefing`` 拿聚合数，不重写一遍。

    选它而不是直接 ``store.stats()``，是因为它多做了两件正确的事：
    ``sessions`` 表为空但有事件时退一步用事件里的不同指纹数（单独跑预检不经派发
    登记，本来就没有 sessions 行），以及 ``blocked`` 按严重度归类。

    ``partial`` 是**下限标记**：聚合失败且列表顶到扫描上限时，``events``/``alerts``
    只是「至少有这么多」，前端要显示成 ``≥N``。别把它读成「统计有误差」——它的
    意思只有一个：这个数字可能小于真值。
    """
    try:
        data = build_briefing(store, since=since, until=until, max_items=1)
    except Exception:  # noqa: BLE001
        # 这里是「连 briefing 都炸了」——今天走不到（`build_briefing` 承诺不抛，
        # 内部每个数据源都降级），留着是给未来的改动上一道保险。
        # **必须标 partial=True**：此时给 ``partial: False`` + 四个 0，等于把「没读到」
        # 说成「确实没有」，正是面板全文在防的那类谎。标了才会显示成下限/未知。
        return {**_zero_stats(), "partial": True}
    if not isinstance(data, dict):
        return {**_zero_stats(), "partial": True}
    return {
        "sessions": _as_int(data.get("sessions")),
        "events": _as_int(data.get("events")),
        "alerts": _as_int(data.get("alerts")),
        "blocked": _as_int(data.get("blocked")),
        "by_severity": {str(k): _as_int(v) for k, v in dict(data.get("by_severity") or {}).items()},
        "by_level": {str(k): _as_int(v) for k, v in dict(data.get("by_level") or {}).items()},
        # 严格判 ``is True``：briefing 给的是 bool，但这里的契约是「只有明确说 True
        # 才当前提」——拿 ""/0/None 之类的真值冒充 True 会把「不知道」升级成「确定」。
        "partial": data.get("stats_partial") is True,
    }


def _guard_policy(doctor: dict[str, Any]) -> str:
    """身份核验档位名（``creation_time`` / ``time_window`` / ``none``）。

    它决定 ``guard_action=terminate`` 会不会因为「无法确认身份」被拒。**注意它跟
    ``guard_action`` 不是一回事**：那个是「命中红线时会做什么」，这个是「凭什么认人」。

    必须摊平：``doctor()["guard"]["policy"]`` 的真实类型是 **dict**
    （``ProcessGuard.identity_policy()`` 返回
    ``{"mode": ..., "window_seconds": ..., "platform": ...}``），直接 ``str()`` 会把
    整个 Python repr 渲染到面板上 —— 那是一串给开发者看的数据结构，不是档位名。
    ``capabilities()`` 保持布尔位、``policy`` 单独一棵子树是有意设计，所以这里适配
    消费者，不改 ``identity_policy()`` 的返回类型。

    兼容直接给 str 的情况：这只是对旧调用方和手工拼出来的 dict 的容错。**别据此把测试
    替身写成 str** —— 替身必须复现 ``identity_policy()`` 的真实形状，否则「消费者把 dict
    整体 ``str()``」这类缺陷会在全绿用例底下完全隐形，这次就是这么漏的。替身比生产端更
    宽松，就是一台假通过生成器。

    **这个摊平是有损的**：只留 ``mode``，``window_seconds`` / ``platform`` 不进 state。
    面板只需要「凭什么认人」这一档，多两个字段是把诊断细节塞进给人看的界面。代价是
    ``state.sentinel.guard_policy`` **不再承载完整策略**——将来要写「为什么 terminate
    被拒」这类判定或测试，那两个字段必须从 ``doctor()["guard"]["policy"]`` 取，不能从
    这里取。别把有损转换当成无损镜像。
    """
    block = doctor.get("guard")
    if not isinstance(block, dict):
        return ""
    policy = block.get("policy")
    if isinstance(policy, dict):
        return str(policy.get("mode") or "")
    return str(policy or "")


def _timebox_seconds(doctor: dict[str, Any]) -> float:
    """时间盒秒数；缺字段给 0.0 而不是省略键。"""
    block = doctor.get("timebox")
    if isinstance(block, dict):
        return _as_float(block.get("seconds"))
    return 0.0


def _parse_failures(doctor: dict[str, Any]) -> int:
    """适配器解析失败计数。和 ``dropped_events`` 同型：降级发生过的唯一证据。"""
    block = doctor.get("parse")
    if isinstance(block, dict):
        return _as_int(block.get("failures"))
    return 0


# -- 主入口 -----------------------------------------------------------------


def build_panel_state(
    store: Any,
    *,
    sentinel: Any = None,
    plugin: dict[str, Any] | None = None,
    entries: list[dict[str, Any]] | None = None,
    hours: float = 12.0,
    sessions_limit: int = 20,
    alerts_limit: int = 20,
    timeline_limit: int = 40,
    now: float | None = None,
    guard_action: str = "",
) -> dict[str, Any]:
    """聚合出 Hosted 面板的局部 state。

    返回的是**局部** state：``plugin`` / ``entries`` 由调用方传入（``sentinel_core``
    是独立包，不该知道宿主有哪些入口），``elapsed_ms`` 之类由包着 ``to_thread``
    的那层测。

    :param store: ``EventStore``；``None`` 表示账本没建起来。
    :param sentinel: ``SentinelRuntime`` 或同形鸭子对象；``None`` 表示运行时没起来。
    :param plugin: 插件元信息，原样透传。
    :param entries: 入口清单，原样透传。
    :param hours: 统计时间窗（小时）。
    :param sessions_limit: 会话最多几条。
    :param alerts_limit: 告警最多几条。
    :param timeline_limit: 时间线最多几条。
    :param now: 注入时钟，便于测试固定窗口；``None`` 取 ``time.time()``。
    :param guard_action: 命中红线时的干预动作（``alert`` 只告警 / ``terminate`` 真杀）。
        **必须由调用方从配置注入** —— 它是配置项 ``sentinel.guard_action``，
        ``doctor()["guard"]`` 里没有这个值（那里面是 ``enabled`` / ``policy``）。
        它和下面两个字段各管一件事，不能合并：用户最想知道的是「它会不会真杀我进程」，
        而 ``guard_enabled=True`` 只说明干预层起来了，不代表它会动手。
    :return: 见模块说明的结构化字典。**任何数据源异常都降级，绝不抛出。**
    """
    moment = time.time() if now is None else _as_float(now, time.time())
    span = _as_float(hours, 12.0)
    since = moment - span * 3600.0
    until = moment

    store_ok = bool(store is not None and getattr(store, "enabled", False))
    doctor = _read_doctor(sentinel)
    state = _resolve_state(sentinel, store_ok)

    sentinel_block = {
        "state": state,
        "enabled": bool(getattr(sentinel, "enabled", False)),
        "store_ok": store_ok,
        "l2": _section_flag(doctor, "l2"),
        # 三个 guard 字段分管三件事，别混：
        #   guard         = 命中红线会做什么（alert / terminate / ""=未知）
        #   guard_enabled = 干预层起没起来
        #   guard_policy  = 身份核验档位
        "guard": str(guard_action or ""),
        "guard_enabled": _section_flag(doctor, "guard"),
        "guard_policy": _guard_policy(doctor),
        "safepoint": _section_flag(doctor, "safepoint"),
        "webhook": _section_flag(doctor, "webhook", "configured"),
        "timebox_seconds": _timebox_seconds(doctor),
        # 丢事件数与解析失败数是「降级发生过」的唯一证据（日志在插件路径上看不见），
        # 必须透传；缺字段时给 0 而不是省略键。
        "dropped_events": _as_int(doctor.get("dropped_events")),
        "parse_failures": _parse_failures(doctor),
        "fix_hint": "" if state == STATE_OK else _FIX_HINTS.get(state, ""),
    }

    # 非 ok 态一律不再读账本：哨兵停了/没账本时摆出「上次残留的数字」比空着更误导，
    # 而且这些数在面板上本来就不显示。
    if state == STATE_OK:
        stats = _collect_stats(store, since=since, until=until)
        sessions = _collect_sessions(store, limit=sessions_limit, since=since)
        alerts = _collect_alerts(store, limit=alerts_limit, since=since)
        timeline = _collect_timeline(store, limit=timeline_limit, since=since, until=until)
    else:
        stats = _zero_stats()
        sessions = []
        alerts = []
        timeline = []

    return {
        "plugin": dict(plugin) if isinstance(plugin, dict) else dict(_DEFAULT_PLUGIN),
        "entries": list(entries) if isinstance(entries, list) else [],
        "sentinel": sentinel_block,
        "window": {"hours": span, "since": since, "until": until},
        "stats": stats,
        "sessions": sessions,
        "alerts": alerts,
        "timeline": timeline,
    }


#: 面板自己坏掉时用的 sentinel 块。逐字段与 ``build_panel_state`` 的正常输出同形 ——
#: 少一个键，前端就多一处 ``undefined``。
_FALLBACK_SENTINEL: dict[str, Any] = {
    "state": STATE_RUNTIME_MISSING,
    "enabled": False,
    "store_ok": False,
    "l2": False,
    "guard": "",
    "guard_enabled": False,
    "guard_policy": "",
    "safepoint": False,
    "webhook": False,
    "timebox_seconds": 0.0,
    "dropped_events": 0,
    "parse_failures": 0,
    "fix_hint": "面板状态生成失败，详见插件日志；下面的入口动作仍然可以正常使用。",
}


def degraded_state(
    plugin: dict[str, Any] | None,
    entries: list[dict[str, Any]] | None,
    *,
    guard_action: str = "",
    elapsed_ms: float = 0.0,
    now: float | None = None,
) -> dict[str, Any]:
    """面板在「自己拼装失败」时的返回值。

    为什么放在这里而不是 ``__init__.py``：那份字面量曾经在正常路径之外又写了一遍，
    加了 ``stats.partial`` 之后**当场漂了**（降级路径少了那个键）。形状只留一个
    定义处，才不会每加一个字段就漏一处；放在这里还能被 pytest 直接测到 —— 插件类
    在测试里从不实例化（见 ``tests/_sdk_stub.py``），写在 ``__init__.py`` 里就永远
    测不到。

    ``sessions`` / ``alerts`` / ``timeline`` 给 ``None`` 而不是 ``[]``：``[]`` 的意思
    是「读到了，确实没有」，而这里真相是「面板自己坏了，读都没读到」。给 ``[]`` 会让
    用户以为昨晚什么都没发生。

    :param plugin: 插件元信息，原样透传。
    :param entries: 入口清单，原样透传 —— 数据层挂了不该连带停掉「派发任务」。
    :param guard_action: 已经读到的干预动作；降级态下用户仍有权知道「会不会真杀进程」。
    :param elapsed_ms: 已耗时（毫秒）。
    :param now: 注入时钟，便于测试固定窗口。
    :return: 与 ``build_panel_state`` 同形的降级 state。
    """
    moment = time.time() if now is None else _as_float(now, time.time())
    span = 12.0
    sentinel_block = dict(_FALLBACK_SENTINEL)
    sentinel_block["guard"] = str(guard_action or "")
    return {
        "plugin": dict(plugin) if isinstance(plugin, dict) else dict(_DEFAULT_PLUGIN),
        "entries": list(entries) if isinstance(entries, list) else [],
        "sentinel": sentinel_block,
        "window": {"hours": span, "since": moment - span * 3600.0, "until": moment},
        "stats": _zero_stats(),
        "sessions": None,
        "alerts": None,
        "timeline": None,
        "elapsed_ms": round(_as_float(elapsed_ms), 1),
    }
