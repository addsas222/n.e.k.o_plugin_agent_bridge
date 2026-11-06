"""输出方言适配层与 ``_PipeReader.on_chunk`` 的测试。

两件事必须钉死：
1. **方言解析**：三家结构化协议各自能认出工具/命令/文件，认不出来就退化；
2. **回调不杀线程**：``on_chunk`` 抛异常时读取线程必须继续活着 —— 线程一死管道
   就没人读，子进程会永远卡在 write 上（详见 ``bridge.runner._PipeReader`` 的说明）。
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading

# isort: split
# bridge 在插件仓库内是一方包、在 CI 的检出布局下会被当成三方包；
# 显式切段，两种 cwd 下的 I001 判定就都稳定了。

from bridge.runner import _execute_blocking, _PipeReader
from bridge.sentinel_core.adapters import LIST_ADAPTERS, detect_adapter, get_adapter
from bridge.sentinel_core.adapters.base import RAW_LIMIT
from bridge.sentinel_core.adapters.claude import ClaudeAdapter
from bridge.sentinel_core.adapters.codex import CodexAdapter
from bridge.sentinel_core.adapters.mcode import McodeAdapter
from bridge.sentinel_core.adapters.text import TextAdapter

# ---------------------------------------------------------------------------
# 样本：全部取自本机真实产物（~/.claude/…/*.jsonl、~/.codex/sessions/…/rollout-*.jsonl）
# ---------------------------------------------------------------------------

CLAUDE_BASH = json.dumps(
    {
        "type": "assistant",
        "timestamp": "2026-11-03T14:49:41.510Z",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_014oCNzX5zpLjndgNNBVbqhA",
                    "name": "Bash",
                    "input": {"command": "ls -la /c/Users/54301", "description": "List home directory"},
                }
            ],
        },
    }
)

CLAUDE_READ = json.dumps(
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "Read",
                    "input": {"file_path": "bridge/runner.py"},
                }
            ],
        },
    }
)

CLAUDE_THINKING = json.dumps(
    {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "嗯……"}]}}
)

CODEX_EXEC = json.dumps(
    {
        "timestamp": "2026-10-18T13:28:22.860Z",
        "ordinal": 14,
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "status": "completed",
            "id": "fc_resp_6ad4c970dcbe_1",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": "git --version; python --version", "workdir": "E:/work"}),
            "call_id": "call_6ad4c973dcbe_0",
        },
    }
)

CODEX_PATCH = json.dumps(
    {
        "timestamp": "2026-10-20T04:43:06.147Z",
        "ordinal": 1532,
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "id": "f45dbefd",
            "status": "completed",
            "call_id": "call_00_Lq",
            "name": "apply_patch",
            "input": "*** Begin Patch\n*** Add File: _work/extract_assets.py\n+print(1)\n*** End Patch\n",
        },
    }
)

MCODE_TOOL = json.dumps(
    {
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q tests"}},
        ],
    },
    ensure_ascii=False,
)

MCODE_RESULT_FAILED = json.dumps(
    {"type": "result", "result": {"status": "failed", "error": "STRUCTURED_OUTPUT_INVALID"}},
    ensure_ascii=False,
)


# ---------------------------------------------------------------------------
# claude 方言
# ---------------------------------------------------------------------------


def test_claude_parses_bash_as_command():
    event = ClaudeAdapter().parse(CLAUDE_BASH)
    assert event is not None
    assert event.kind == "command"
    assert event.tool == "Bash"
    assert "ls -la" in event.text
    assert event.ts > 0
    assert event.raw.startswith('{"type": "assistant"')


def test_claude_parses_file_tool_as_file():
    event = ClaudeAdapter().parse(CLAUDE_READ)
    assert event is not None
    assert event.kind == "file"
    assert event.path == "bridge/runner.py"
    assert event.tool == "Read"


def test_claude_skips_thinking_and_metadata_lines():
    adapter = ClaudeAdapter()
    assert adapter.parse(CLAUDE_THINKING) is None
    assert adapter.parse('{"type": "mode", "mode": "normal"}') is None
    assert adapter.parse("plain text from text mode") is None


def test_claude_marks_failed_result_as_error():
    line = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "轮次用尽"})
    event = ClaudeAdapter().parse(line)
    assert event is not None
    assert event.kind == "error"
    assert "error_max_turns" in event.text or "轮次用尽" in event.text


# ---------------------------------------------------------------------------
# codex 方言
# ---------------------------------------------------------------------------


def test_codex_parses_function_call_as_command():
    event = CodexAdapter().parse(CODEX_EXEC)
    assert event is not None
    assert event.kind == "command"
    assert event.tool == "exec_command"
    assert "git --version" in event.text
    assert event.ts > 0


def test_codex_parses_apply_patch_as_file():
    event = CodexAdapter().parse(CODEX_PATCH)
    assert event is not None
    assert event.kind == "file"
    assert event.path == "_work/extract_assets.py"
    assert event.tool == "apply_patch"


def test_codex_ignores_duplicated_event_msg_and_metadata():
    adapter = CodexAdapter()
    duplicated = json.dumps(
        {
            "type": "event_msg",
            "payload": {"type": "item_completed", "item": {"type": "CommandExecution", "command": ["ls"]}},
        }
    )
    assert adapter.parse(duplicated) is None
    assert adapter.parse('{"type": "session_meta", "payload": {"cwd": "C:/tmp"}}') is None


def test_codex_parses_plain_patch_header():
    event = CodexAdapter().parse("*** Update File: bridge/sentinel_core/adapters/codex.py")
    assert event is not None
    assert event.kind == "file"
    assert event.path == "bridge/sentinel_core/adapters/codex.py"


# ---------------------------------------------------------------------------
# mcode 方言
# ---------------------------------------------------------------------------


def test_mcode_parses_message_tool_use_as_command():
    event = McodeAdapter().parse(MCODE_TOOL)
    assert event is not None
    assert event.kind == "command"
    assert event.tool == "Bash"
    assert "pytest" in event.text


def test_mcode_parses_generic_envelope_best_effort():
    line = json.dumps({"type": "generic", "event": {"kind": "tool", "tool": "read_file", "path": "README.md"}})
    event = McodeAdapter().parse(line)
    assert event is not None
    assert event.kind == "file"
    assert event.path == "README.md"
    assert event.tool == "read_file"


def test_mcode_marks_failed_result_as_error():
    event = McodeAdapter().parse(MCODE_RESULT_FAILED)
    assert event is not None
    assert event.kind == "error"
    assert "failed" in event.text


def test_mcode_ignores_heartbeat_and_delta():
    adapter = McodeAdapter()
    assert adapter.parse('{"type": "heartbeat", "turnId": "t-1"}') is None
    assert adapter.parse('{"type": "delta", "chunkIndex": 3, "content": "ab"}') is None


# ---------------------------------------------------------------------------
# text 方言（兜底）
# ---------------------------------------------------------------------------


def test_text_marks_first_line_as_goal():
    adapter = TextAdapter()
    goal = adapter.parse("把 README 里的安装步骤更新到最新版本")
    assert goal is not None
    assert goal.kind == "goal"
    assert "README" in goal.text
    follow = adapter.parse("正在读取 README.md")
    assert follow is not None
    assert follow.kind == "text"


def test_text_marks_error_lines_in_both_languages():
    adapter = TextAdapter()
    adapter.parse("目标：修个 bug")
    for line in ("Traceback: Error: boom", "运行失败：找不到模块", "错误：权限不足"):
        event = adapter.parse(line)
        assert event is not None
        assert event.kind == "error", line


def test_text_does_not_treat_first_error_line_as_goal():
    adapter = TextAdapter()
    first = adapter.parse("Error: 无法启动 CLI")
    assert first is not None
    assert first.kind == "error"
    # 目标留给后面第一段正常输出
    goal = adapter.parse("修一下构建脚本里的路径")
    assert goal is not None
    assert goal.kind == "goal"


def test_text_returns_none_for_blank_lines():
    assert TextAdapter().parse("   ") is None
    assert TextAdapter().parse("") is None


# ---------------------------------------------------------------------------
# 退化与容错：任何输入都不能抛异常
# ---------------------------------------------------------------------------


def test_garbage_input_never_raises_and_falls_back_to_text():
    junk = [
        "",
        "   ",
        "not json at all",
        "{broken json",
        "[1, 2, 3]",
        '"just a string"',
        "null",
        '{"type": "assistant"}',
        '{"type": "assistant", "message": {"content": "字符串形式的正文"}}',
        '{"type": "response_item", "payload": null}',
        '{"type": "message", "content": [{"type": "tool_use"}]}',
    ]
    for adapter in (ClaudeAdapter(), CodexAdapter(), McodeAdapter(), TextAdapter()):
        for line in junk:
            event = adapter.parse(line)  # 不抛异常即可
            assert event is None or isinstance(event.text, str)


def test_deeply_nested_generic_is_bounded():
    """畸形嵌套不能把递归挖穿。"""
    line = json.dumps({"type": "generic", "a": {"b": {"c": {"d": {"tool": "x", "path": "y.py"}}}}})
    event = McodeAdapter().parse(line)
    assert event is None or event.kind in ("tool", "file", "command")


def test_raw_is_truncated_to_limit():
    long_command = "echo " + "A" * (RAW_LIMIT * 2)
    line = json.dumps(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash", "input": {"command": long_command}}]},
        }
    )
    event = ClaudeAdapter().parse(line)
    assert event is not None
    assert len(event.raw) == RAW_LIMIT
    # 摘要必须是一句话，不能把整段命令塞进去
    assert len(event.text) < len(long_command)


# ---------------------------------------------------------------------------
# 选方言
# ---------------------------------------------------------------------------


def test_get_adapter_unknown_name_falls_back_to_text():
    assert get_adapter("claude").name == "claude"
    assert get_adapter("  CODEX ").name == "codex"
    assert get_adapter("不存在的方言").name == "text"
    assert get_adapter("").name == "text"


def test_get_adapter_returns_fresh_instances():
    """text 方言带目标状态，复用实例会让下一路会话永远等不到 goal。"""
    assert get_adapter("text") is not get_adapter("text")


def test_detect_adapter_picks_claude_from_mixed_sample():
    sample = "\n".join([CLAUDE_BASH, CLAUDE_READ, CLAUDE_THINKING])
    assert detect_adapter(sample).name == "claude"


def test_detect_adapter_picks_codex_from_mixed_sample():
    sample = "\n".join([CODEX_EXEC, CODEX_PATCH, '{"type": "session_meta", "payload": {}}'])
    assert detect_adapter(sample).name == "codex"


def test_detect_adapter_picks_mcode_from_mixed_sample():
    sample = "\n".join(['{"type": "heartbeat", "turnId": "t"}', MCODE_TOOL, MCODE_RESULT_FAILED])
    assert detect_adapter(sample).name == "mcode"


def test_detect_adapter_falls_back_to_text_for_plain_log():
    sample = "\n".join(["正在读取 src/main.py", "已写入 3 个文件", "跑完 12 个测试"])
    assert detect_adapter(sample).name == "text"
    assert detect_adapter("").name == "text"


def test_list_adapters_covers_every_factory():
    assert LIST_ADAPTERS == ("claude", "codex", "mcode", "text")
    for name in LIST_ADAPTERS:
        assert get_adapter(name).name == name


# ---------------------------------------------------------------------------
# _PipeReader.on_chunk
# ---------------------------------------------------------------------------


def _run_reader(payload: bytes, *, limit: int | None = None, on_chunk=None) -> _PipeReader:
    """跑一个读完即退出的 _PipeReader（io.BytesIO 读到 EOF 就结束）。"""
    reader = _PipeReader(io.BytesIO(payload), limit if limit is not None else len(payload) * 2, on_chunk)
    reader.start()
    reader.join(5.0)
    assert not reader.is_alive(), "读取线程没有退出"
    return reader


def test_pipe_reader_calls_on_chunk_with_raw_bytes():
    seen: list[bytes] = []
    payload = b'{"type": "assistant"}\n{"type": "result"}\n'
    _run_reader(payload, on_chunk=seen.append)
    assert b"".join(seen) == payload


def test_pipe_reader_survives_raising_callback():
    """回调抛异常绝不能杀死读取线程（线程一死，管道写满后子进程会永远卡住）。"""
    payload = b"x" * 4096
    calls: list[bytes] = []

    def boom(chunk: bytes) -> None:
        calls.append(chunk)
        raise RuntimeError("解析器炸了")

    reader = _run_reader(payload, on_chunk=boom)
    assert calls, "回调没有被调用"
    assert reader.data == payload
    assert not reader.truncated


def test_pipe_reader_still_emits_past_output_limit():
    """限幅只约束内存，不约束读取与回调 —— 否则解析器会看不到超出上限的部分。"""
    payload = b"y" * 1000
    seen: list[bytes] = []
    reader = _run_reader(payload, limit=100, on_chunk=seen.append)
    assert reader.data == payload[:100]
    assert reader.truncated is True
    assert b"".join(seen) == payload


def test_pipe_reader_without_on_chunk_keeps_old_behaviour():
    payload = b"abcdef" * 100
    reader = _run_reader(payload)
    assert reader.data == payload
    assert reader.truncated is False
    assert reader.on_chunk is None


def test_pipe_reader_thread_is_daemon():
    reader = _PipeReader(io.BytesIO(b""), 16)
    assert reader.daemon is True
    assert isinstance(reader, threading.Thread)


# ---------------------------------------------------------------------------
# on_chunk 的端到端透传（真跑一个子进程）
# ---------------------------------------------------------------------------

_STREAM_LINE = '{"type": "assistant", "message": {"role": "assistant", "content": []}}'


def _run_blocking(on_chunk):
    """跑一条真的会打印一行 JSON 的子进程，把 on_chunk 一路透传下去。"""
    script = "print(" + repr(_STREAM_LINE) + ")"
    return _execute_blocking(
        [sys.executable, "-c", script],
        cwd=None,
        timeout=60,
        output_limit=1 << 16,
        env=dict(os.environ),
        stream_grace=5.0,
        on_chunk=on_chunk,
    )


def test_execute_blocking_streams_chunks_to_callback():
    chunks: list[bytes] = []
    exit_code, stdout, _, timed_out, _, error, _ = _run_blocking(chunks.append)
    assert exit_code == 0
    assert not timed_out
    assert not error
    assert b"".join(chunks).decode("utf-8").strip() == _STREAM_LINE
    assert stdout.strip() == _STREAM_LINE


def test_execute_blocking_keeps_stdout_when_callback_raises():
    """回调炸掉也必须拿到完整 stdout（读取线程不能被带走）。"""

    def boom(chunk: bytes) -> None:
        raise RuntimeError("观测端炸了")

    exit_code, stdout, _, timed_out, _, error, _ = _run_blocking(boom)
    assert exit_code == 0
    assert not timed_out
    assert not error
    assert stdout.strip() == _STREAM_LINE
