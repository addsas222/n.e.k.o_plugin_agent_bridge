"""Claude Code 方言 —— Anthropic 消息对象。

为什么长这个样子
================

依据本机真实产物（``~/.claude/projects/<项目>/<会话>.jsonl``，以及
``claude -p --output-format stream-json`` 的 stdout）：两者是**同一套行协议**，
一行一个 JSON 对象，动作藏在消息的 content 块里::

    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_…", "name": "Bash",
         "input": {"command": "ls -la", "description": "List files"}}]}}

所以要读的不是「日志文本」，而是 **content 块数组**：``tool_use`` 是动作，
``text`` 是它的嘴，``thinking`` 是内部独白（对「它在干什么」没有信息增量，丢掉），
``user`` 里的 ``tool_result`` 是工具回执（只在 ``is_error`` 时才有信息量）。

非 JSON 行一律返回 ``None``：文本模式下 claude 只打印最终答案，那部分交给
``text`` 方言兜底，在这里硬猜只会制造噪声。
"""

from __future__ import annotations

import json
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

__all__ = ["ClaudeAdapter"]

#: 承载 shell 命令的 input 字段（Bash 系列用 ``command``，少数封装用 ``cmd``）。
_COMMAND_KEYS = ("command", "cmd")
#: 承载文件路径的 input 字段。
_PATH_KEYS = ("file_path", "notebook_path", "path")
#: 无信息量的 content 块类型：内部独白与加密思考签名。
_SKIP_BLOCKS = frozenset({"thinking", "redacted_thinking", "signature"})
#: 认定「这是 claude 的流」的结构特征。
_SNIFF_MARKERS = (
    '"tool_use"',
    '"tool_result"',
    '"isSidechain"',
    '"parentUuid"',
    '"sessionId"',
)


class ClaudeAdapter:
    """Claude Code 输出方言。"""

    name = "claude"

    def parse(self, line: str) -> LineEvent | None:
        """解析一行；不是 claude 的结构化行就返回 ``None``。

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
        """看样本里有没有 Anthropic 消息块或 claude 的会话元数据。

        会话刚开头的那几十行往往全是元数据（``ai-title`` / ``agent-name`` /
        ``mode``），一条消息都没有。这类样本也算「像」：结构化的行马上就来，
        这时候退回纯文本方言反而会把它后面的 ``tool_use`` 全糊成文本。

        :param sample: 样本。
        :return: 像则为真。
        """
        text = sample or ""
        if not text or not any(marker in text for marker in _SNIFF_MARKERS):
            return False
        if '"tool_use"' in text or '"tool_result"' in text or '"parentUuid"' in text or '"isSidechain"' in text:
            return True
        # 只剩会话元数据时只能靠字段拼写：claude 用 camelCase ``sessionId``，
        # codex 是 ``session_meta`` / ``session_id``，mcode 是 ``turnId``，不撞车。
        if '"sessionId"' in text and '"type"' in text:
            return True
        # 只有收尾块的截断样本（只抓到最后一行的场景）
        return '"type":"result"' in text.replace(" ", "") and '"subtype"' in text

    # -- 内部 -------------------------------------------------------------
    def _from_object(self, obj: dict[str, Any], line: str) -> LineEvent | None:
        """按行对象的 ``type`` 分派。"""
        kind_of_line = str(obj.get("type") or "")
        ts = iso_ts(obj.get("timestamp"))
        if kind_of_line == "assistant":
            return self._from_assistant(obj, line, ts)
        if kind_of_line == "user":
            return self._from_user(obj, line, ts)
        if kind_of_line == "result":
            return self._from_result(obj, line, ts)
        # system / summary / mode / permission-mode / file-history-snapshot 等
        # 都是会话元数据，与「正在执行什么动作」无关。
        return None

    def _from_assistant(self, obj: dict[str, Any], line: str, ts: float) -> LineEvent | None:
        """助手消息：优先取第一个 ``tool_use``，没有就取正文。"""
        message = obj.get("message")
        blocks = message.get("content") if isinstance(message, dict) else obj.get("content")
        if isinstance(blocks, str):
            return LineEvent(ts=ts, kind=KIND_TEXT, text=brief(blocks, 160), raw=clip(line))
        if not isinstance(blocks, list):
            return None
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "")
            if block_type in _SKIP_BLOCKS:
                # 内部独白（thinking）对「正在做什么」没有信息增量
                continue
            if block_type == "tool_use":
                return self._tool_event(block, line, ts)
            if block_type == "text":
                text = brief(block.get("text"), 160)
                if text:
                    return LineEvent(ts=ts, kind=KIND_TEXT, text=text, raw=clip(line))
        return None

    def _tool_event(self, block: dict[str, Any], line: str, ts: float) -> LineEvent:
        """把一个 ``tool_use`` 块按 input 的形状分成 命令 / 文件 / 其它工具。"""
        name = str(block.get("name") or "")
        raw_input = block.get("input")
        payload = raw_input if isinstance(raw_input, dict) else {}

        for key in _COMMAND_KEYS:
            command = payload.get(key)
            if isinstance(command, str) and command.strip():
                return LineEvent(
                    ts=ts,
                    kind=KIND_COMMAND,
                    text=f"{name or 'Bash'}：{brief(command, 140)}",
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
        # 其余工具（Glob/Grep/WebFetch/TodoWrite…）只有名字与参数摘要
        detail = brief(json.dumps(payload, ensure_ascii=False), 90) if payload else ""
        return LineEvent(
            ts=ts,
            kind=KIND_TOOL,
            text=f"{name or 'tool'}：{detail}".rstrip("："),
            tool=name,
            raw=clip(line),
        )

    def _from_user(self, obj: dict[str, Any], line: str, ts: float) -> LineEvent | None:
        """工具回执：只有报错时才有信息量，成功回执是噪声。"""
        message = obj.get("message")
        blocks = message.get("content") if isinstance(message, dict) else obj.get("content")
        if not isinstance(blocks, list):
            return None
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            if not block.get("is_error"):
                continue
            return LineEvent(
                ts=ts,
                kind=KIND_ERROR,
                text=f"工具报错：{brief(_content_text(block.get('content')), 160)}",
                raw=clip(line),
            )
        return None

    def _from_result(self, obj: dict[str, Any], line: str, ts: float) -> LineEvent | None:
        """stream-json 的收尾块：``is_error`` 决定它是不是一次失败。"""
        subtype = str(obj.get("subtype") or "")
        failed = bool(obj.get("is_error")) or subtype not in ("", "success")
        detail = brief(obj.get("result") or obj.get("error") or subtype, 160)
        if failed:
            return LineEvent(ts=ts, kind=KIND_ERROR, text=f"任务失败：{detail}", raw=clip(line))
        return LineEvent(
            ts=ts,
            kind=KIND_TEXT,
            text=f"任务结束：{detail}".rstrip("："),
            raw=clip(line),
        )


def _content_text(content: Any) -> str:
    """把 ``tool_result`` 的 content（字符串或块数组）压成一段文本。"""
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
