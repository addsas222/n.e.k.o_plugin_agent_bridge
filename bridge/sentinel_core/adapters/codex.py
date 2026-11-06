"""Codex CLI 方言 —— rollout 事件流与补丁信封。

为什么长这个样子
================

依据本机真实产物（``~/.codex/sessions/<年>/<月>/<日>/rollout-*.jsonl``，Codex CLI
0.160.x）。一行一个 JSON 对象，动作在 ``payload`` 里，且**同名调用会发布两遍**：
``response_item`` 是模型侧的调用意图，``event_msg/item_completed`` 是执行侧的完成
回执。实测同一份会话里 ``function_call`` 786 次、对应的 ``item_completed`` 里
``CommandExecution`` 628 + ``McpToolCall`` 41 + ``FileChange`` 61 + ``ImageView`` 27
基本一一对应，所以**只读 ``response_item``**：两边都读会把每个动作记两次。

三种动作形状（都来自实测样本）：

* ``function_call`` + ``name="exec_command"``，``arguments`` 是**再套一层的 JSON 字符串**，
  里面有 ``cmd`` / ``workdir`` / ``shell``；
* ``custom_tool_call`` + ``name="apply_patch"``，``input`` 是整段补丁，文件路径写在
  ``*** Update File: …`` 这类补丁信封头里；
* ``function_call`` 的其它工具（``view_image`` 带 ``path``、``write_stdin`` 只带 ``session_id``）。

非 JSON 行只认 ``*** Add/Update/Delete File:`` 这种补丁头 —— 那是 codex 独有的
apply_patch 信封；其余纯文本交给 ``text`` 方言，不在这里猜。
"""

from __future__ import annotations

import re
from typing import Any

from .base import (
    KIND_COMMAND,
    KIND_FILE,
    KIND_TEXT,
    KIND_TOOL,
    LineEvent,
    brief,
    clip,
    iso_ts,
    loads_object,
)

__all__ = ["CodexAdapter"]

#: 承载 shell 命令的参数名（``exec_command`` 用 ``cmd``，兼容写法用 ``command``）。
_COMMAND_KEYS = ("cmd", "command")
#: 承载文件路径的参数名。
_PATH_KEYS = ("path", "file_path")
#: 补丁信封头：``*** Update File: dir/name.py``（Add / Delete 同理）。
_PATCH_HEADER_RE = re.compile(r"^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s*(?P<path>.+?)\s*$", re.MULTILINE)
#: 认定「这是 codex 的流」的结构特征。
_SNIFF_MARKERS = (
    '"response_item"',
    '"custom_tool_call"',
    '"exec_command"',
    '"session_meta"',
    "*** Begin Patch",
)


class CodexAdapter:
    """Codex CLI 输出方言。"""

    name = "codex"

    def parse(self, line: str) -> LineEvent | None:
        """解析一行；不是 codex 的结构化行就返回 ``None``。

        :param line: 原始行。
        :return: 事件，或 ``None``。
        """
        obj = loads_object(line)
        if obj is None:
            return self._from_text(line)
        try:
            return self._from_object(obj, line)
        except Exception:  # noqa: BLE001 —— 观察层绝不允许抛异常
            return None

    def sniff(self, sample: str) -> bool:
        """看样本里有没有 rollout 事件的字段名或 apply_patch 信封。

        :param sample: 样本。
        :return: 像则为真。
        """
        text = sample or ""
        if not text:
            return False
        if any(marker in text for marker in _SNIFF_MARKERS):
            return True
        # 只抓到补丁正文中间几行的场景
        return _PATCH_HEADER_RE.search(text) is not None and '"type"' not in text

    # -- 内部 -------------------------------------------------------------
    def _from_object(self, obj: dict[str, Any], line: str) -> LineEvent | None:
        """按行对象的 ``type`` 分派；只认 ``response_item``。"""
        if str(obj.get("type") or "") != "response_item":
            # session_meta / turn_context / world_state / event_msg / token_usage_record
            # 都是会话元数据或与 response_item 重复的执行回执，不产事件。
            return None
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            return None
        ts = iso_ts(obj.get("timestamp"))
        item_type = str(payload.get("type") or "")
        if item_type == "function_call":
            return self._from_function_call(payload, line, ts)
        if item_type == "custom_tool_call":
            return self._from_custom_tool_call(payload, line, ts)
        if item_type == "message":
            return self._from_message(payload, line, ts)
        return None

    def _from_function_call(self, payload: dict[str, Any], line: str, ts: float) -> LineEvent | None:
        """``function_call``：``arguments`` 是一层 JSON 字符串，再按参数形状分类。"""
        name = str(payload.get("name") or "")
        arguments = _as_mapping(payload.get("arguments"))

        for key in _COMMAND_KEYS:
            command = _as_command_text(arguments.get(key))
            if command:
                return LineEvent(
                    ts=ts,
                    kind=KIND_COMMAND,
                    text=f"{name or 'shell'}：{brief(command, 140)}",
                    tool=name,
                    raw=clip(line),
                )
        for key in _PATH_KEYS:
            path = arguments.get(key)
            if isinstance(path, str) and path.strip():
                return LineEvent(
                    ts=ts,
                    kind=KIND_FILE,
                    text=f"{name or 'file'}：{brief(path, 140)}",
                    tool=name,
                    path=clip(path, 400),
                    raw=clip(line),
                )
        if not name:
            return None
        detail = brief(", ".join(sorted(arguments)), 90) if arguments else ""
        return LineEvent(
            ts=ts,
            kind=KIND_TOOL,
            text=f"{name}：{detail}".rstrip("："),
            tool=name,
            raw=clip(line),
        )

    def _from_custom_tool_call(self, payload: dict[str, Any], line: str, ts: float) -> LineEvent | None:
        """``custom_tool_call``：``input`` 是自由文本，``apply_patch`` 的路径在信封头里。"""
        name = str(payload.get("name") or "")
        body = payload.get("input")
        text = body if isinstance(body, str) else ""
        header = _PATCH_HEADER_RE.search(text)
        if header is not None:
            path = header.group("path").strip()
            return LineEvent(
                ts=ts,
                kind=KIND_FILE,
                text=f"{name or 'apply_patch'}：{brief(path, 140)}",
                tool=name,
                path=clip(path, 400),
                raw=clip(line),
            )
        if not text.strip():
            return None
        return LineEvent(
            ts=ts,
            kind=KIND_TOOL,
            text=f"{name or 'tool'}：{brief(text, 120)}",
            tool=name,
            raw=clip(line),
        )

    def _from_message(self, payload: dict[str, Any], line: str, ts: float) -> LineEvent | None:
        """助手正文算「它说了什么」；user/developer 注入的是上下文，不算动作。"""
        if str(payload.get("role") or "") != "assistant":
            return None
        text = brief(_blocks_text(payload.get("content")), 160)
        if not text:
            return None
        return LineEvent(ts=ts, kind=KIND_TEXT, text=text, raw=clip(line))

    def _from_text(self, line: str) -> LineEvent | None:
        """纯文本模式的兜底：只认补丁信封头。"""
        header = _PATCH_HEADER_RE.search(line or "")
        if header is None:
            return None
        path = header.group("path").strip()
        return LineEvent(
            ts=iso_ts(None),
            kind=KIND_FILE,
            text=f"补丁：{brief(path, 140)}",
            path=clip(path, 400),
            raw=clip(line),
        )


def _as_mapping(value: Any) -> dict[str, Any]:
    """把 ``arguments`` 归一成字典：可能是字典，也可能是 JSON 字符串。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = loads_object(value)
        return parsed if parsed is not None else {}
    return {}


def _as_command_text(value: Any) -> str:
    """把命令字段归一成一段可读文本：``str`` 直接用，列表按 argv 拼接。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(str(part) for part in value if str(part).strip()).strip()
    return ""


def _blocks_text(content: Any) -> str:
    """把消息 content（字符串或块数组）压成一段文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            parts.append(str(item.get("text") or ""))
        elif isinstance(item, str):
            parts.append(item)
    return " ".join(part for part in parts if part)
