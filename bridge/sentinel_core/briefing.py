"""夜间值守晨报 —— 早上打开电脑只想知道「昨晚有没有出事」。

为什么有这个模块
================

哨兵拦下了危险命令，但如果这件事只留在账本和日志里，等于**没有发生**：没人半夜
盯终端，早上的第一反应是「昨晚跑完了吗」。这一层把一段时间窗内的事件、告警、
Token 消耗压成一屏能读完的中文摘要，第一行直接给结论。

设计取舍
========

* **结论先行**：分三档——有红线拦截 / 只有普通告警 / 什么都没发生。让人 3 秒内
  决定是「马上处理」还是「关掉」。
* **只列最重要的 3 条**：晨报不是时间线。红线优先，其次告警，同级按时间倒序
  （最近的发生最可能还留着现场）。
* **数字只报账本里有的**：没有记录就写「这段时间没有记录」，绝不用「一切正常」
  这种听起来安心但可能是「哨兵没挂上」的话糊过去。
* **失败降级**：账本读不出来、数据结构脏了，都返回一段能看的文本，不抛异常——
  晨报不该成为新的故障点。
"""

from __future__ import annotations

import time
from typing import Any

from .metrics import render_usage
from .rules import SEVERITY_RED_LINE, SEVERITY_STANDARD
from .store import LEVEL_CRITICAL, LEVEL_WARN
from .timeline import describe_event, level_of

__all__ = ["build_briefing", "render_briefing"]

#: 单行里事件描述的最大字符数（再长就截断，保证一屏可读）。
_CLIP_CHARS = 68

#: 晨报最多多少行（超过就截断并提示）。
_MAX_LINES = 25

#: 有拦截时最多列几条关键事件。
_TOP_ITEMS = 3


def _clock(ts: Any) -> str:
    """把时间戳格式化成 ``MM-DD HH:MM``。

    :param ts: 时间戳（可为 ``None``）。
    :return: 时间字符串；无法格式化时返回占位符。
    """
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "--"


def _clip(text: str, limit: int = _CLIP_CHARS) -> str:
    """截断过长文本，保留可读性。

    :param text: 原文。
    :param limit: 最大字符数。
    :return: 截断后的文本。
    """
    body = " ".join(str(text or "").split())
    if len(body) <= limit:
        return body
    return body[: limit - 1] + "…"


def _empty_usage(since: float | None = None) -> dict[str, Any]:
    """构造一份「零用量」结构，供空摘要与降级路径复用。

    :param since: 统计起点。
    :return: 与 ``TokenLedger.summary`` 同构的字典。
    """
    return {
        "estimated": True,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "saved_tokens": 0,
        "calls": 0,
        "since": since,
    }


def _empty_briefing(since: float, until: float | None) -> dict[str, Any]:
    """构造一份空摘要（账本不可用或完全没有记录时的合法返回值）。

    :param since: 统计起点。
    :param until: 统计终点。
    :return: 摘要字典。
    """
    return {
        "range": {"since": float(since), "until": float(until) if until is not None else None},
        "sessions": 0,
        "events": 0,
        "alerts": 0,
        "blocked": 0,
        "by_severity": {},
        "by_level": {},
        "usage": _empty_usage(since),
        "highlights": [],
        "first_seen": None,
        "last_seen": None,
        # 键恒存在：调用方（面板）不该用 ``.get`` 摸一个「有时有有时没有」的字段，
        # 那会让「没有这个键」和「不是部分」长得一模一样。
        "stats_partial": False,
    }


def _range_alerts(store: Any, since: float, until: float | None, limit: int) -> list[dict[str, Any]]:
    """取时间窗内的告警行。

    只走 ``EventStore.alerts`` 这个**公开**方法。这里曾经绕过它、直接拿私有的
    ``_read`` 拼 SQL —— 那是「账本还不是我的文件、只有投递队列可用」时期的权宜：
    晨报要的是「历史账目」，而当时只有 ``pending_alerts``（投递队列）是公开的。
    现在 ``alerts()`` 已是公开接口，再拼 SQL 只会变成第二份真相：账本换查询方式
    （加索引、换存储、改表结构）时，晨报会静默跟丢，而且绕过公开接口意味着绕过它
    的脱敏与口径。

    拿不到 ``alerts`` 就返回空列表：晨报少一块，而不是整体失败。

    :param store: 账本。
    :param since: 起始时间戳。
    :param until: 结束时间戳。
    :param limit: 最多几条。
    :return: 告警行（时间倒序）。
    """
    query = getattr(store, "alerts", None)
    if not callable(query):
        return []
    try:
        return list(query(since=since, until=until, limit=limit) or [])
    except Exception:  # noqa: BLE001 —— 账本读失败不该让晨报崩掉
        return []


def _severity_distribution(stats: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, int]:
    """严重度分布。聚合给得出来就用它，给不出来就从事件行重算。

    为什么要重算：``render_briefing`` 用 ``red_line`` 判断「昨夜平安不平安」。
    ``stats`` 聚合失败时 ``by_severity`` 是空的，于是结论说「昨夜平安」，
    而下面「关键事件」里正列着三条 ``[红线]`` —— 一份自己跟自己打架的晨报，
    比没有晨报更糟。

    重算用的是同一份**可能被截断**的事件列表，所以它是下限；这个事实由
    ``stats_partial`` 负责标出来，不在这里再编一个标记。

    :param stats: ``store.stats()`` 的结果（可能为空）。
    :param events: 时间窗内的事件行（可能被 ``scan_limit`` 截断）。
    :return: ``{严重度: 条数}``；没有严重度的行不计入。
    """
    raw = stats.get("by_severity")
    if isinstance(raw, dict) and raw:
        return {str(key): int(value or 0) for key, value in raw.items()}
    counted: dict[str, int] = {}
    for row in events:
        # 脏行守卫：账本里混进非 dict 行时，这里不该比正常路径更早炸
        # （`_pick_highlights` 对同样的行也会炸，但那不该发生在更前面）。
        if not isinstance(row, dict):
            continue
        severity = str(row.get("severity") or "")
        if severity:
            counted[severity] = counted.get(severity, 0) + 1
    return counted


def _event_rank(row: dict[str, Any]) -> int:
    """给一条事件定「重要性档位」，数字越小越靠前。

    :param row: 事件行。
    :return: 档位。
    """
    severity = str(row.get("severity") or "")
    if severity == SEVERITY_RED_LINE:
        return 0
    if severity == SEVERITY_STANDARD:
        return 2
    return 3


def _alert_rank(row: dict[str, Any]) -> int:
    """给一条告警定档位。

    需求里说「红线优先，其次告警」——所以告警整体排在红线之后、普通高危事件之前。

    :param row: 告警行。
    :return: 档位。
    """
    return 1


def _as_highlight(row: dict[str, Any], *, kind: str = "event") -> dict[str, Any]:
    """把账本行转成晨报用的高亮条目。

    :param row: 账本行（事件或告警）。
    :param kind: ``event`` 或 ``alert``。
    :return: 高亮条目。
    """
    if kind == "alert":
        level = str(row.get("level") or "")
        text = str(row.get("title") or "").strip()
        body = str(row.get("body") or "").strip()
        if body:
            text = f"{text}：{body}" if text else body
    else:
        # 映射只有一份，在 ``timeline.level_of``。这里曾经是一张本地字典，
        # 与时间线、面板各自的 if 链并存，三份拷贝迟早会漂。
        level = level_of(row.get("severity"))
        text = describe_event(row)
    return {
        "type": kind,
        "id": row.get("id"),
        "ts": float(row.get("ts") or 0),
        "clock": _clock(row.get("ts")),
        "kind": str(row.get("kind") or kind),
        "severity": str(row.get("severity") or ""),
        "level": level,
        "rule_id": str(row.get("rule_id") or ""),
        "fingerprint": str(row.get("fingerprint") or ""),
        "text": text,
        "detail": str(row.get("detail") or ""),
    }


def _pick_highlights(
    events: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    max_items: int,
) -> list[dict[str, Any]]:
    """挑出最重要的 N 条：红线 → 告警 → 普通高危，同级按时间倒序。

    :param events: 时间窗内的事件。
    :param alerts: 时间窗内的告警。
    :param max_items: 最多几条。
    :return: 高亮条目（已排序）。
    """
    ranked: list[tuple[int, float, dict[str, Any]]] = []
    seen_events: set[Any] = set()
    for row in events:
        item = _as_highlight(row, kind="event")
        if item["id"] is not None:
            seen_events.add(item["id"])
        ranked.append((_event_rank(row), -item["ts"], item))
    for row in alerts:
        # 告警通常伴随一条同类事件，重复列出来只会占位。
        event_id = row.get("event_id")
        if event_id and event_id in seen_events:
            continue
        item = _as_highlight(row, kind="alert")
        ranked.append((_alert_rank(row), -item["ts"], item))
    ranked.sort(key=lambda entry: (entry[0], entry[1]))
    return [entry[2] for entry in ranked[: max(0, int(max_items))]]


def build_briefing(
    store: Any,
    *,
    since: float,
    until: float | None = None,
    max_items: int = 20,
) -> dict[str, Any]:
    """聚合一段时间窗内的事件，产出结构化晨报数据。

    :param store: ``EventStore`` 实例。
    :param since: 统计起点（时间戳）。
    :param until: 统计终点；``None`` 表示到现在。
    :param max_items: ``highlights`` 最多几条。
    :return: 见模块说明的结构化字典。**除了 store 自身的方法，本函数不因数据形状抛异常** ——
        账本若返回非 dict 行，入口处就把它们滤掉了（见下面的 ``isinstance`` 过滤），
        否则 ``_pick_highlights`` 里取 ``row.get`` 会抛 ``AttributeError``。过滤掉的行
        **不计入任何计数**，所以计数可能因此偏小；这是「宁可少报也不要崩」的取舍，
        ``stats_partial`` 不管这一档（它只管截断）。
    """
    try:
        start = float(since)
    except (TypeError, ValueError):
        start = 0.0
    try:
        stop = float(until) if until is not None else None
    except (TypeError, ValueError):
        stop = None

    if store is None:
        return _empty_briefing(start, stop)

    # 时间线一次最多扫这么多条：晨报只需要「最重要的几十条」，不必把整夜读进内存。
    scan_limit = min(1000, max(200, int(max_items) * 20))

    try:
        stats = store.stats(since=start, until=stop) or {}
    except Exception:  # noqa: BLE001 —— 账本已内部降级，这里再兜一层
        stats = {}
    try:
        events = list(store.timeline(since=start, until=stop, limit=scan_limit) or [])
    except Exception:  # noqa: BLE001
        events = []
    alerts = _range_alerts(store, start, stop, scan_limit)

    # 脏行在入口滤掉，别让它流到 `_pick_highlights` / `_severity_distribution` 里再炸。
    # 不滤的后果是崩点随数据漂移：`timeline=[None]` 崩在 sessions 回退、`alerts=[None]`
    # 崩在 `_pick_highlights`、带 sessions 时又崩在 `_as_highlight` —— 三个不同的函数，
    # 没有单一位置包得住，所以只能在**入口**统一。
    #
    # 丢掉的行走 ``stats_partial``：它和「截断」「读失败」是同一种事实（数字偏小），
    # 而这三种里面只有它连日志都没有。丢行单独说一句会变成第三个「不完整」字段。
    raw_event_count = len(events)
    raw_alert_count = len(alerts)
    events = [row for row in events if isinstance(row, dict)]
    alerts = [row for row in alerts if isinstance(row, dict)]
    dropped_rows = (raw_event_count - len(events)) + (raw_alert_count - len(alerts))

    data = _empty_briefing(start, stop)
    # 计数降级是否**可能少报**。判据按维度各判一次，再 OR：每个数字只有在
    # 「它自己的计数缺失」且「顶替它的那个列表顶满 scan_limit」时才是下限。
    #
    # **不能交叉相乘**（`(not e or not a) and (len_e >= L or len_a >= L)`）：
    # 那会把「events 精确 5000、告警真的 0 条」误判成部分（列表短只是采样短）。
    # 也不能只看 events：`stats()` 里两条 COUNT 是分开跑的，events 成功、alerts 撞上
    # `database is locked` 被 `_read` 吞成 0 是可能的 —— 那时 alerts 少报而 events 精确。
    counted_events = stats.get("events")
    counted_alerts = stats.get("alerts")
    # 截断判据用**过滤前**的行数：脏行被丢掉不代表这次没扫满上限。
    trimmed_events = not counted_events and raw_event_count >= scan_limit
    trimmed_alerts = not counted_alerts and raw_alert_count >= scan_limit
    stats_partial = bool(trimmed_events or trimmed_alerts or dropped_rows)

    by_severity = _severity_distribution(stats, events)
    by_level = {str(key): int(value or 0) for key, value in dict(stats.get("by_level") or {}).items()}
    input_tokens = int(stats.get("input_tokens") or 0)
    output_tokens = int(stats.get("output_tokens") or 0)
    saved_tokens = int(stats.get("saved_tokens") or 0)

    sessions = int(stats.get("sessions") or 0)
    if not sessions and events:
        # sessions 表只记「走过派发登记的会话」；单独跑预检（不经派发）不会有登记，
        # 此时退一步用事件里的不同指纹数，免得晨报出现「0 路会话却有 6 条事件」。
        sessions = len({str(row.get("fingerprint") or "") for row in events} - {""})

    data.update(
        {
            "sessions": sessions,
            "events": int(counted_events or len(events)),
            "alerts": int(counted_alerts or len(alerts)),
            # **只从 by_severity 算，不要读 stats["blocked"]**：
            # `store.stats()` 恒返回这个键，哪怕它内部那条 COUNT 被 `_read` 吞成空 ——
            # 那时它带回来的是个**假 0**。而 `by_severity` 已经过
            # `_severity_distribution`（SQL 有就用、没有就从事件行重算），
            # stats 正常时两者数值完全相同（同一个加法、同一份输入），
            # stats 部分失败时这里得到真值、那边得到 0。一个事实一个来源。
            # **代价**：`blocked` 的定义权从此在 `by_severity`。哪天 `store.stats()`
            # 改了 blocked 的口径（比如把 `critical` 也计入），这里**不会自动跟随** ——
            # 改那边必须回来改这一行，否则面板与晨报又会出现两个说法。
            "blocked": int(by_severity.get(SEVERITY_RED_LINE) or 0)
            + int(by_severity.get(SEVERITY_STANDARD) or 0),
            "stats_partial": stats_partial,
            "by_severity": by_severity,
            "by_level": by_level,
            "usage": {
                "estimated": True,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "saved_tokens": saved_tokens,
                "calls": len(events),
                "since": start,
            },
            "highlights": _pick_highlights(events, alerts, max_items),
            "first_seen": min((float(row.get("ts") or 0) for row in events), default=None),
            "last_seen": max((float(row.get("ts") or 0) for row in events), default=None),
        }
    )
    return data


def render_briefing(data: dict) -> str:
    """把结构化晨报渲染成一段给人读的中文文本。

    :param data: ``build_briefing`` 的返回值。
    :return: 文本（不超过 ``_MAX_LINES`` 行）。
    """
    try:
        lines = _render_lines(data)
    except Exception:  # noqa: BLE001 —— 渲染失败也不许炸，给一句降级提示
        return "（晨报生成失败，已降级。原始记录仍在账本里，可用时间线查看。）"
    if not lines:
        return "（这段时间没有记录）"
    if len(lines) > _MAX_LINES:
        omitted = len(lines) - (_MAX_LINES - 1)
        lines = [*lines[: _MAX_LINES - 1], f"… 另有 {omitted} 行已省略 …"]
    return "\n".join(lines)


def _marker(item: dict[str, Any]) -> str:
    """给高亮条目一个中文标签。

    告警条目即便级别是 ``critical`` 也标「告警」——「红线」这个词留给真正被拦下的
    红线级命令，否则早上的人会以为自己被拦了两次。

    :param item: 高亮条目。
    :return: ``红线`` / ``告警`` / ``记录``。
    """
    if str(item.get("type") or "") == "alert":
        return "告警"
    if str(item.get("level") or "") == LEVEL_CRITICAL:
        return "红线"
    if str(item.get("level") or "") == LEVEL_WARN:
        return "高危"
    return "记录"


def _render_lines(data: dict) -> list[str]:
    """真正干活的行拼接。

    :param data: 晨报数据。
    :return: 文本行。
    """
    rng = dict(data.get("range") or {})
    since = rng.get("since")
    until = rng.get("until")
    head = f"哨兵晨报 {_clock(since)} → {_clock(until) if until is not None else '现在'}"

    usage = dict(data.get("usage") or {})
    by_severity = dict(data.get("by_severity") or {})
    by_level = dict(data.get("by_level") or {})
    red_line = int(by_severity.get(SEVERITY_RED_LINE) or 0)
    standard = int(by_severity.get(SEVERITY_STANDARD) or 0)
    events = int(data.get("events") or 0)
    alerts = int(data.get("alerts") or 0)
    # 键缺失时才退回「按严重度推」——`build_briefing` 现在保证这个键是 int，
    # 这里的 `or` 只是给手工拼出来的旧 dict 兜底。
    blocked = int(data.get("blocked") or red_line + standard)
    # 统计不完整（聚合不可用 + 列表顶到 scan_limit）时，上面这些数字全是**下限**。
    # 文字晨报没有面板那种「≥N」的逐格标记，所以统一在计数行尾注一句，
    # 别让读者把「事件 200 条」当成真值 —— 那正是这次要修的少报。
    partial = bool(data.get("stats_partial"))
    highlights = list(data.get("highlights") or [])

    lines = [head]

    # 一档：什么都没发生。这里刻意不说「一切正常」——哨兵没观测到不代表没发生。
    if events == 0 and alerts == 0:
        lines.append("结论：这段时间没有记录。")
        lines.append("（若昨晚确实跑过任务，先确认哨兵有没有挂到对应 Agent 上。）")
        if int(usage.get("total_tokens") or 0):
            lines.append(render_usage(usage))
        return lines

    if red_line:
        lines.append(f"结论：昨夜拦下 {red_line} 条红线操作，建议先看下面的关键事件。")
    elif alerts:
        lines.append(f"结论：没有红线拦截，但有 {alerts} 条告警值得扫一眼。")
    elif standard:
        lines.append(f"结论：没有红线拦截，有 {standard} 条普通高危命令被放行/提示。")
    else:
        # 「平安」是对**全量**的断言。统计不完整时只能说样本内没看到 —— 数据被截了
        # 一半还说「昨夜平安」，就是拿旧样本回答新问题。
        if partial:
            lines.append("结论：样本内没有拦截也没有告警（统计不完整，不能当全量结论）。")
        else:
            lines.append(f"结论：昨夜平安，{events} 条记录里没有拦截也没有告警。")

    counts_line = (
        f"会话 {int(data.get('sessions') or 0)} 路 · 事件 {events} 条 · 告警 {alerts} 条 · 拦截 {blocked} 条"
    )
    if partial:
        counts_line += "（统计不完整，以上为下限）"
    lines.append(counts_line)

    severity_bits = []
    if red_line:
        severity_bits.append(f"红线 {red_line}")
    if standard:
        severity_bits.append(f"普通 {standard}")
    if severity_bits:
        lines.append("严重度：" + " / ".join(severity_bits))

    level_bits = []
    for name in (LEVEL_CRITICAL, LEVEL_WARN):
        count = int(by_level.get(name) or 0)
        if count:
            level_bits.append(f"{name} {count}")
    if level_bits:
        lines.append("告警级别：" + " / ".join(level_bits))

    first_seen = data.get("first_seen")
    last_seen = data.get("last_seen")
    if first_seen is not None and last_seen is not None:
        lines.append(f"时间跨度：{_clock(first_seen)} → {_clock(last_seen)}")

    if highlights:
        lines.append("关键事件（最重要在前）：")
        for item in highlights[:_TOP_ITEMS]:
            lines.append(f"  {item.get('clock', '--')}  [{_marker(item)}] {_clip(item.get('text') or '')}")

    lines.append(render_usage(usage))
    lines.append("完整过程：查账本时间线（按会话指纹过滤更快）。")
    return lines
