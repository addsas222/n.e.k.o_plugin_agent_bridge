"""MiniMax Code 方言 —— headless 的 stream-json 事件包。

为什么长这个样子
================

``mcode``（``@minimax-ai/code``）在 headless 模式下用
``mcode exec --output-format stream-json`` 吐 newline-delimited 事件，官方文档明确了
两件事：**结果走 stdout、诊断走 stderr**，以及 ``--output-format json`` 的产物是
一份 ``ExecResult``（带 ``schemaVersion`` 与 ``status``，取值 succeeded / failed /
timeout / cancelled / limit_exceeded）。

流式事件的七种包类型（``heartbeat`` / ``session-status`` / ``message`` / ``delta`` /
``generic`` / ``done`` / ``result``）来自它对外 SDK 文档对原生 stream-json 的
「原样透传」说明。本机装过 mcode 但二进制已损坏（``node:internal/modules/cjs/loader``），
拿不到现场样本，所以这里的解析**刻意写得宽**：

* ``message`` —— 完整消息快照，动作藏在 content 块里（与 Anthropic 同构）；
* ``generic`` —— 官方称它是「mcode 内部事件的向前兼容信封」，形状不稳定，
  所以用递归找 ``tool``/``name`` + ``command``/``path`` 的方式尽力提取，找不到就放弃；
* ``result`` —— 收尾，``status`` 决定成败；
* ``heartbeat`` / ``delta`` / ``done`` / ``session-status`` —— 存活信号或碎片增量，
  对「它在干什么」没有信息增量，返回 ``None``（``result`` 已经覆盖了收尾）。

非 JSON 行返回 ``None``：``--output-format text`` 只打印最终答案，交给 ``text`` 方言。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .base import (
    KIND_COMMAND,
    KIND_ERROR,
    KIND_FILE,
    KIND_TEXT,
    KIND_TOOL,
    LineEvent,
    brief,
    clip,
    iso_ts,
    loads_object,
)

__all__ = ["McodeAdapter"]

#: 承载 shell 命令的字段名。
_COMMAND_KEYS = ("command", "cmd")
#: 承载文件路径的字段名。
_PATH_KEYS = ("file_path", "path", "notebook_path")
#: 工具名的候选字段（``generic`` 信封形状不稳定，全都试一遍）。
_TOOL_NAME_KEYS = ("tool", "toolName", "tool_name", "name")
#: ``result`` 里表示成功状态的值。
_OK_STATUS = frozenset({"succeeded", "success", "ok", "completed", ""})
#: 递归挖掘 ``generic`` 信封的最大深度，防止畸形数据里的自引用结构把栈吃光。
_MAX_DEPTH = 3
#: 认定「这是 mcode 的流」的结构特征。
_SNIFF_MARKERS = ('"session-status"', '"chunkIndex"', '"heartbeat"', '"schemaVersion"')


class McodeAdapter:
    """MiniMax Code 输出方言。"""

    name = "mcode"

    def parse(self, line: str) -> LineEvent | None:
        """解析一行；不是 mcode 的结构化行就返回 ``None``。

        :param line: 原始行。
        :return: 事件，或 ``None``。
        """
        obj = loads_object(line)
        if obj is None:
            return None
        try:
            return self._from_object(obj, line)
        except Exception:  # noqa: BLE001 —— 观察层绝不允许抛异常
            return None

    def sniff(self, sample: str) -> bool:
        """看样本里有没有 mcode 特有的事件字段。

        :param sample: 样本。
        :return: 像则为真。
        """
        text = sample or ""
        if not text:
            return False
        return any(marker in text for marker in _SNIFF_MARKERS)

    # -- 内部 -------------------------------------------------------------
    def _from_object(self, obj: dict[str, Any], line: str) -> LineEvent | None:
        """按事件包类型分派。"""
        envelope = str(obj.get("type") or "")
        ts = iso_ts(obj.get("timestamp") or obj.get("ts"))
        if envelope == "message":
            return self._from_message(obj, line, ts)
        if envelope == "generic":
            return self._from_generic(obj, line, ts)
        if envelope == "result":
            return self._from_result(obj, line, ts)
        # heartbeat / delta / done / session-status：存活信号与增量碎片，不产事件。
        return None

    def _from_message(self, obj: dict[str, Any], line: str, ts: float) -> LineEvent | None:
        """消息快照：只有助手侧才有动作，用户侧是任务本身。"""
        body = obj.get("message")
        message = body if isinstance(body, dict) else obj
        role = str(message.get("role") or obj.get("role") or "")
        if role and role != "assistant":
            return None
        content = message.get("content")
        if isinstance(content, str):
            text = brief(content, 160)
            return LineEvent(ts=ts, kind=KIND_TEXT, text=text, raw=clip(line)) if text else None
        if not isinstance(content, list):
            return None
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "")
            if block_type in ("tool_use", "tool-call", "tool_call"):
                return self._tool_event(block, line, ts)
            if block_type in ("text", "output_text"):
                text = brief(block.get("text"), 160)
                if text:
                    return LineEvent(ts=ts, kind=KIND_TEXT, text=text, raw=clip(line))
        return None

    def _tool_event(self, block: dict[str, Any], line: str, ts: float) -> LineEvent:
        """按入参形状把一次工具调用分成 命令 / 文件 / 其它工具。"""
        name = str(block.get("name") or block.get("tool") or "")
        payload = block.get("input")
        if not isinstance(payload, dict):
            payload = block.get("arguments") if isinstance(block.get("arguments"), dict) else {}

        for key in _COMMAND_KEYS:
            command = payload.get(key)
            if isinstance(command, str) and command.strip():
                return LineEvent(
                    ts=ts,
                    kind=KIND_COMMAND,
                    text=f"{name or 'shell'}：{brief(command, 140)}",
                    tool=name,
                    raw=clip(line),
                )
        for key in _PATH_KEYS:
            path = payload.get(key)
            if isinstance(path, str) and path.strip():
                return LineEvent(
                    ts=ts,
                    kind=KIND_FILE,
                    text=f"{name or 'file'}：{brief(path, 140)}",
                    tool=name,
                    path=clip(path, 400),
                    raw=clip(line),
                )
        return LineEvent(
            ts=ts,
            kind=KIND_TOOL,
            text=f"{name or 'tool'}：{brief(', '.join(sorted(payload)), 90)}".rstrip("："),
            tool=name,
            raw=clip(line),
        )

    def _from_generic(self, obj: dict[str, Any], line: str, ts: float) -> LineEvent | None:
        """``generic`` 是「内部事件」的兼容信封，只能尽力提取。"""
        for payload in _iter_mappings(obj, _MAX_DEPTH):
            name = _first_text(payload, _TOOL_NAME_KEYS)
            command = _first_text(payload, _COMMAND_KEYS)
            path = _first_text(payload, _PATH_KEYS)
            if not name and not command and not path:
                continue
            if command:
                return LineEvent(
                    ts=ts,
                    kind=KIND_COMMAND,
                    text=f"{name or 'shell'}：{brief(command, 140)}",
                    tool=name,
                    raw=clip(line),
                )
            if path:
                return LineEvent(
                    ts=ts,
                    kind=KIND_FILE,
                    text=f"{name or 'file'}：{brief(path, 140)}",
                    tool=name,
                    path=clip(path, 400),
                    raw=clip(line),
                )
            if name:
                return LineEvent(
                    ts=ts,
                    kind=KIND_TOOL,
                    text=f"{name}：内部事件",
                    tool=name,
                    raw=clip(line),
                )
        return None

    def _from_result(self, obj: dict[str, Any], line: str, ts: float) -> LineEvent | None:
        """收尾包：``status`` 不是成功态就记一条错误。"""
        body = obj.get("result")
        result = body if isinstance(body, dict) else obj
        status = str(result.get("status") or obj.get("status") or "").strip()
        if status.lower() in _OK_STATUS:
            detail = brief(result.get("answer") or result.get("text") or "", 120)
            return LineEvent(
                ts=ts,
                kind=KIND_TEXT,
                text=f"任务结束：{detail}".rstrip("："),
                raw=clip(line),
            )
        detail = brief(
            result.get("error") or result.get("answer") or result.get("message") or status,
            160,
        )
        return LineEvent(ts=ts, kind=KIND_ERROR, text=f"任务失败（{status}）：{detail}", raw=clip(line))


def _iter_mappings(root: dict[str, Any], depth: int) -> Iterator[dict[str, Any]]:
    """按层序遍历字典里的嵌套字典，最多 ``depth`` 层。

    ``generic`` 信封的实际形状官方没有固定下来，与其赌一个字段路径，不如把嵌套
    几层都翻一遍找工具痕迹。

    :param root: 起点字典。
    :param depth: 最大层数。
    :yield: 命中的字典。
    """
    frontier: list[dict[str, Any]] = [root]
    for _ in range(max(1, depth)):
        next_frontier: list[dict[str, Any]] = []
        for mapping in frontier:
            yield mapping
            for value in mapping.values():
                if isinstance(value, dict):
                    next_frontier.append(value)
                elif isinstance(value, list):
                    next_frontier.extend(item for item in value if isinstance(item, dict))
        frontier = next_frontier
        if not frontier:
            return


def _first_text(mapping: dict[str, Any], keys: tuple[str, ...]) -> str:
    """取第一个非空字符串字段。"""
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
