"""案发时间线 —— 把散落的事件串成「它怎么一步步走到这一步」。

为什么单独一层
==============

早上看到一份被改乱的 diff，第一个问题永远不是「改了什么」，而是「它怎么会想到
去改那里」。单条告警回答不了这个：告警只说「此刻这一句危险」，而漂移是几步之前
就发生的。

这一层做三件事：

1. **排序**：统一按时间正序。事件在库里是倒序写、正序读，容易反。
2. **折叠**：连续的同种类低价值记录（普通输出行）折成 ``+N 行``，
   否则一条时间线会被几百行噪声淹掉。
3. **定位关键节点**：预检拦截、判定升级、告警、错误、安全点，这些是「节点」，
   渲染时带标记，其余是「背景」。
"""

from __future__ import annotations

import time
from typing import Any, Iterable

from .rules import SEVERITY_RED_LINE, SEVERITY_STANDARD, VERDICT_DANGEROUS, VERDICT_LOOP
from .store import (
    KIND_ERROR,
    KIND_PRECHECK,
    KIND_SYSTEM,
    KIND_TOOL,
    KIND_VERDICT,
    LEVEL_CRITICAL,
    LEVEL_INFO,
    LEVEL_WARN,
)

__all__ = [
    "build_timeline",
    "describe_event",
    "fmt_clock",
    "level_of",
    "render_timeline",
    "render_timeline_lines",
]

#: 会被当成「关键节点」的事件种类。
_NODE_KINDS = frozenset({KIND_PRECHECK, KIND_VERDICT, KIND_ERROR, KIND_TOOL, KIND_SYSTEM})

#: 折叠阈值：连续多少条背景事件以上才折。
_FOLD_THRESHOLD = 4


def fmt_clock(ts: Any) -> str:
    """把时间戳格式化成 ``HH:MM:SS``。

    **全包唯一的一份**：``panel._clock`` 曾经是它的逐字拷贝。两份拷贝的差别只在
    捕获的异常类型（那边多一个 ``TypeError``），于是同一条坏时间戳在时间线上是
    ``--:--:--``、在面板上却可能抛异常。这里按宽的那个收口。

    :param ts: 时间戳（可为 ``None``）。
    :return: 时钟字符串；无法格式化时给占位符而不是抛。
    """
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "--:--:--"


def describe_event(row: dict[str, Any]) -> str:
    """给一条事件写一句人话。

    :param row: 事件行。
    :return: 描述。
    """
    kind = str(row.get("kind") or "")
    summary = str(row.get("summary") or "").strip()
    rule_id = str(row.get("rule_id") or "").strip()

    if kind == KIND_PRECHECK:
        # 判定值是 rules 里的大写常量（DANGEROUS / LOOP / ON_TRACK），不是小写字面量。
        # 这里曾经写成小写比较，结果所有真拦截都被渲染成「放行」—— 那是会骗人的 bug。
        verdict = str(row.get("verdict") or "").strip().upper()
        if verdict == VERDICT_DANGEROUS:
            head = "派发前拦截"
        elif verdict == VERDICT_LOOP:
            head = "原地打转"
        else:
            head = "派发前放行"
        tail = f"（{rule_id}）" if rule_id else ""
        return f"{head}{tail}：{summary}" if summary else f"{head}{tail}"
    if kind == KIND_VERDICT:
        tail = f"（{rule_id}）" if rule_id else ""
        return f"判定升级{tail}：{summary}" if summary else f"判定升级{tail}"
    if kind == KIND_ERROR:
        return f"出错：{summary}" if summary else "出错"
    if kind == KIND_TOOL:
        return f"调用工具：{summary}" if summary else "调用工具"
    if kind == KIND_SYSTEM:
        return summary or "系统事件"
    return summary or kind or "事件"


def _is_node(row: dict[str, Any]) -> bool:
    """判断这条事件是否值得单独占一行。

    :param row: 事件行。
    :return: 是否关键节点。
    """
    if str(row.get("kind") or "") in _NODE_KINDS:
        return True
    severity = str(row.get("severity") or "")
    return bool(severity)


def build_timeline(
    store: Any,
    *,
    since: float | None = None,
    until: float | None = None,
    fingerprint: str = "",
    severity: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """从账本里取一条时间线，并按「节点 / 折叠后的背景」组织好。

    :param store: ``EventStore`` 实例。
    :param since: 起始时间戳。
    :param until: 结束时间戳。
    :param fingerprint: 只看某一路会话。
    :param severity: 只看某严重度。
    :param limit: 最多取几条。
    :return: 条目列表，每项为 ``{"type": "node"|"fold"|"line", ...}``。
    """
    rows = store.timeline(
        since=since,
        until=until,
        fingerprint=fingerprint,
        severity=severity,
        limit=limit,
    )
    return _organize(rows)


def _organize(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """把原始事件行组织成节点与折叠块。

    :param rows: 按时间正序的事件行。
    :return: 条目列表。
    """
    items: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    def flush() -> None:
        if not pending:
            return
        if len(pending) >= _FOLD_THRESHOLD:
            first = pending[0]
            items.append(
                {
                    "type": "fold",
                    "count": len(pending),
                    "ts": float(first.get("ts") or 0),
                    "clock": fmt_clock(first.get("ts") or 0),
                    "kinds": sorted({str(item.get("kind") or "") for item in pending}),
                }
            )
        else:
            for item in pending:
                items.append(_as_line(item))
        pending.clear()

    for row in rows:
        if _is_node(row):
            flush()
            items.append(_as_node(row))
        else:
            pending.append(row)
    flush()
    return items


def level_of(severity: Any) -> str:
    """把事件严重度映射成告警级别三档（``critical`` / ``warn`` / ``info``）。

    **这是全包唯一的一份映射。** 它曾经被抄在四处：``timeline._as_node``、
    ``panel._level_of``、``briefing._as_highlight`` 的字典、以及 ``runtime`` 里
    两句 ``LEVEL_CRITICAL if severity == "red_line" else LEVEL_WARN``。四份拷贝
    意味着「标准级算 warn」这个判断改一次要改四处，漏一处就出现同一条事件在
    时间线上是 warn、在面板上却是 info。

    :param severity: 事件严重度（``red_line`` / ``standard`` / 其它）。
    :return: 级别名。
    """
    text = str(severity or "")
    if text == SEVERITY_RED_LINE:
        return LEVEL_CRITICAL
    if text == SEVERITY_STANDARD:
        return LEVEL_WARN
    return LEVEL_INFO


def _as_node(row: dict[str, Any]) -> dict[str, Any]:
    """把一条事件转成节点条目。

    :param row: 事件行。
    :return: 节点条目。
    """
    severity = str(row.get("severity") or "")
    level = level_of(severity)
    return {
        "type": "node",
        "id": row.get("id"),
        "ts": float(row.get("ts") or 0),
        "clock": fmt_clock(row.get("ts") or 0),
        "kind": str(row.get("kind") or ""),
        "severity": severity,
        "level": level,
        "rule_id": str(row.get("rule_id") or ""),
        "fingerprint": str(row.get("fingerprint") or ""),
        "text": describe_event(row),
        "detail": str(row.get("detail") or ""),
    }


def _as_line(row: dict[str, Any]) -> dict[str, Any]:
    """把一条背景事件转成普通行。

    :param row: 事件行。
    :return: 行条目。
    """
    return {
        "type": "line",
        "id": row.get("id"),
        "ts": float(row.get("ts") or 0),
        "clock": fmt_clock(row.get("ts") or 0),
        "kind": str(row.get("kind") or ""),
        "text": str(row.get("summary") or "").strip(),
    }


def render_timeline_lines(items: Iterable[dict[str, Any]]) -> list[str]:
    """把时间线条目渲染成纯文本行（每项一行）。

    :param items: ``build_timeline`` 的返回值。
    :return: 文本行列表。
    """
    out: list[str] = []
    for item in items:
        kind = item.get("type")
        if kind == "fold":
            kinds = "/".join(item.get("kinds") or [])
            out.append(f"{item.get('clock', '--:--:--')}  ·  … 折叠 {item.get('count')} 条（{kinds}）")
        elif kind == "node":
            marker = {"critical": "!!", "warn": " !"}.get(str(item.get("level") or ""), " ·")
            out.append(f"{item.get('clock', '--:--:--')} {marker} {item.get('text', '')}")
        else:
            text = str(item.get("text") or "").strip()
            if text:
                out.append(f"{item.get('clock', '--:--:--')}    {text}")
    return out


def render_timeline(items: Iterable[dict[str, Any]], *, limit: int = 80) -> str:
    """把时间线渲染成一段可直接回给用户的文本。

    :param items: ``build_timeline`` 的返回值。
    :param limit: 最多保留多少行（超出时保留头尾）。
    :return: 渲染结果。
    """
    lines = render_timeline_lines(items)
    if not lines:
        return "（这段时间没有记录）"
    if len(lines) > limit:
        head = lines[: limit // 2]
        tail = lines[-(limit - len(head) - 1) :]
        omitted = len(lines) - len(head) - len(tail)
        lines = [*head, f"… 省略 {omitted} 行 …", *tail]
    return "\n".join(lines)
