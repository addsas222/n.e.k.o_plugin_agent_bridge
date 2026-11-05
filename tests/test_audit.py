"""Sentinel 安全审计集成测试（pytest 可直接收集）。

覆盖：
  * ``SentinelClient`` 对哨兵 v5 本地 HTTP 接口的 health / pre_check / attach；
  * ``gated_pre_check`` 的四种关键路径：禁用 → 放行；启用且命中 → 拦截；
    守护进程不可达 → fail-open 放行；命中但 block_on_deny=false → 放行；
  * ``run_agent`` 接入网关后的行为：危险任务被拦下（且不真正派发）、安全任务照常派发、
    默认关闭时零回归。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

# isort: split
# bridge 在插件仓库内是一方包、在 CI 的检出布局下会被当成三方包；
# 显式切段，两种 cwd 下的 I001 判定就都稳定了。

from bridge.audit import (
    ACTION_ALLOW,
    ACTION_BLOCK,
    PreCheckResult,
    SentinelClient,
    build_client,
    gated_pre_check,
    is_loopback_url,
    status_snapshot,
)
from bridge.config import BridgeConfig, SentinelConfig, config_from_dict
from bridge.registry import AGENT_SPECS, ResolvedCommand
from bridge.runner import _execute_blocking, run_agent


class _Handler(BaseHTTPRequestHandler):
    """可配置的 mock 哨兵守护进程。命中 ``BLOCKME`` 子串即判 block。"""

    BLOCK_SUBSTRING = "BLOCKME"
    attach_seen: list[dict[str, Any]] = []

    def log_message(self, *args: Any) -> None:  # noqa: A002
        return

    def _send(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/health"):
            self._send(
                {
                    "status": "running",
                    "version": "5.0.0",
                    "mode": "headless",
                    "sessions": 1,
                    "l2_sleeping": 0,
                    "callback": False,
                }
            )
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        if self.path.startswith("/api/pre-check"):
            text = str(data.get("text") or "")
            if self.BLOCK_SUBSTRING in text:
                self._send(
                    {
                        "action": ACTION_BLOCK,
                        "verdict": "dangerous",
                        "rule_id": "test.red_line",
                        "reason": "命中测试红线",
                        "evidence": text,
                        "degraded": False,
                    }
                )
            else:
                self._send(
                    {
                        "action": ACTION_ALLOW,
                        "verdict": "",
                        "rule_id": "",
                        "reason": "",
                        "evidence": "",
                        "degraded": False,
                    }
                )
        elif self.path.startswith("/api/attach"):
            type(self).attach_seen.append(data)
            self._send(
                {
                    "fingerprint": "x",
                    "cwd": str(data.get("cwd", "")),
                    "pid": int(data.get("pid") or 0),
                    "agent": str(data.get("agent", "")),
                }
            )
        else:
            self._send({"error": "not found"}, 404)


@pytest.fixture
def sentinel_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Handler.attach_seen = []
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


# ---------------------------------------------------------------------------
# 1. SentinelClient 基础端点
# ---------------------------------------------------------------------------


def test_health_ok(sentinel_server):
    client = SentinelClient(sentinel_server, timeout=2.0)
    h = client.health()
    assert h.ok is True
    assert h.version == "5.0.0"


def test_pre_check_allow(sentinel_server):
    client = SentinelClient(sentinel_server, timeout=2.0)
    r = client.pre_check("请帮我写个函数")
    assert isinstance(r, PreCheckResult)
    assert r.allow is True
    assert r.action == ACTION_ALLOW


def test_pre_check_block(sentinel_server):
    client = SentinelClient(sentinel_server, timeout=2.0)
    r = client.pre_check("请执行 BLOCKME 的危险操作")
    assert r.allow is False
    assert r.action == ACTION_BLOCK
    assert r.rule_id == "test.red_line"
    assert r.reason == "命中测试红线"


def test_attach_records_payload(sentinel_server):
    client = SentinelClient(sentinel_server, timeout=2.0)
    out = client.attach(cwd="D:/work", pid=999, agent="codebuddy")
    assert isinstance(out, dict)
    assert _Handler.attach_seen == [{"cwd": "D:/work", "pid": 999, "agent": "codebuddy", "log_path": "", "session_id": ""}]


# ---------------------------------------------------------------------------
# 2. build_client / gated_pre_check 的路径分支
# ---------------------------------------------------------------------------


def test_build_client_disabled():
    assert build_client(SentinelConfig(enabled=False)) is None
    # 启用但没配地址 → 也算不可用
    assert build_client(SentinelConfig(enabled=True, url="")) is None
    assert build_client(SentinelConfig(enabled=True, url="http://127.0.0.1:8787")) is not None


def test_build_client_rejects_non_loopback():
    # 文档承诺「只允许本机回环」：非回环地址必须直接拒绝，别把任务正文发出去。
    assert build_client(SentinelConfig(enabled=True, url="http://192.168.1.10:8787")) is None
    assert build_client(SentinelConfig(enabled=True, url="https://example.com")) is None
    assert build_client(SentinelConfig(enabled=True, url="ftp://127.0.0.1:8787")) is None


def test_is_loopback_url():
    assert is_loopback_url("http://127.0.0.1:8787") is True
    assert is_loopback_url("http://127.1.2.3:8787") is True
    assert is_loopback_url("http://localhost:8787") is True
    assert is_loopback_url("http://[::1]:8787") is True
    assert is_loopback_url("http://10.0.0.2:8787") is False
    assert is_loopback_url("not a url") is False


def test_status_snapshot_disabled():
    info = status_snapshot(SentinelConfig(enabled=False))
    assert info["enabled"] is False
    assert info["reachable"] is False


def test_status_snapshot_reachable(sentinel_server):
    info = status_snapshot(SentinelConfig(enabled=True, url=sentinel_server, timeout=2.0))
    assert info["enabled"] is True
    assert info["reachable"] is True
    assert info["version"] == "5.0.0"
    assert info["sessions"] == 1


def test_status_snapshot_daemon_down():
    info = status_snapshot(SentinelConfig(enabled=True, url="http://127.0.0.1:1", timeout=0.3))
    assert info["enabled"] is True
    assert info["reachable"] is False


def test_config_reads_bridge_sentinel_section():
    # 插件侧传给 config_from_dict 的是 [bridge] 这一层，所以哨兵就是同级的 sentinel 键。
    cfg = config_from_dict(
        {
            "sentinel": {
                "enabled": True,
                "url": "http://127.0.0.1:9999",
                "block_on_deny": False,
                "attach": False,
                "timeout": 1.5,
            }
        }
    )
    assert cfg.sentinel.enabled is True
    assert cfg.sentinel.url == "http://127.0.0.1:9999"
    assert cfg.sentinel.block_on_deny is False
    assert cfg.sentinel.attach is False
    assert cfg.sentinel.timeout == 1.5


def test_config_sentinel_invalid_values_fall_back():
    cfg = config_from_dict({"sentinel": {"enabled": "yes", "url": "", "timeout": -1}})
    assert cfg.sentinel.enabled is False
    assert cfg.sentinel.url == "http://127.0.0.1:8787"
    assert cfg.sentinel.timeout == 2.0


def test_gated_disabled_returns_none():
    cfg = SentinelConfig(enabled=False)
    assert gated_pre_check(cfg, "BLOCKME 危险") is None


def test_gated_block_when_enabled(sentinel_server):
    cfg = SentinelConfig(enabled=True, url=sentinel_server, block_on_deny=True)
    result = gated_pre_check(cfg, "请执行 BLOCKME 的危险操作")
    assert isinstance(result, PreCheckResult)
    assert result.allow is False


def test_gated_fail_open_when_daemon_down():
    # 指向一个什么都没监听的端口：必须 fail-open（放行），且不得抛异常。
    cfg = SentinelConfig(enabled=True, url="http://127.0.0.1:1", timeout=0.3)
    assert gated_pre_check(cfg, "BLOCKME 危险") is None


def test_gated_block_but_not_enforced_is_allowed(sentinel_server):
    # block_on_deny=false：判定为 block 但仍放行（只记录不拦截）。
    cfg = SentinelConfig(enabled=True, url=sentinel_server, block_on_deny=False)
    assert gated_pre_check(cfg, "BLOCKME 危险") is None


# ---------------------------------------------------------------------------
# 3. run_agent 接入网关后的端到端行为
# ---------------------------------------------------------------------------


def _fake_execute(return_pid: int = 1234):
    async def _impl(*args, **kwargs):
        # 真实的 _execute 会在 Popen 之后调用 on_spawn；桩必须复刻这个时机，
        # 否则「挂接发生在进程存活期间」这条约束根本没被测到。
        on_spawn = kwargs.get("on_spawn")
        if on_spawn is not None:
            on_spawn(return_pid)
        return (0, "fake output", "", False, False, "", return_pid)
    return _impl


@pytest.mark.asyncio
async def test_run_agent_blocks_dangerous_task(monkeypatch, sentinel_server):
    monkeypatch.setattr("bridge.runner._execute", _fake_execute())
    called = {"n": 0}

    real = _fake_execute()

    def counting(*a, **k):
        called["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr("bridge.runner._execute", counting)

    config = BridgeConfig()
    config.sentinel = SentinelConfig(enabled=True, url=sentinel_server, block_on_deny=True, attach=True)
    resolved = ResolvedCommand("fake.exe", source="path", exists=True)
    result = await run_agent(
        AGENT_SPECS["codebuddy"], resolved, "请执行 BLOCKME 的危险操作", config=config, cwd="D:/work", timeout=10
    )
    # 被拦下：根本没真正派发
    assert result.blocked_by_sentinel is True
    assert result.error.startswith("AGENT_BLOCKED_BY_SENTINEL")
    assert result.extra.get("rule_id") == "test.red_line"
    assert called["n"] == 0
    assert result.pid is None


@pytest.mark.asyncio
async def test_run_agent_safe_task_proceeds(monkeypatch, sentinel_server):
    monkeypatch.setattr("bridge.runner._SENTINEL_ATTACH_DELAY", 0)
    monkeypatch.setattr("bridge.runner._execute", _fake_execute(return_pid=4321))
    config = BridgeConfig()
    config.sentinel = SentinelConfig(enabled=True, url=sentinel_server, block_on_deny=True, attach=True)
    resolved = ResolvedCommand("fake.exe", source="path", exists=True)
    result = await run_agent(
        AGENT_SPECS["codebuddy"], resolved, "请帮我写个函数", config=config, cwd="D:/work", timeout=10
    )
    assert result.blocked_by_sentinel is False
    assert result.ok is True
    assert result.pid == 4321
    # 派发后挂接是 fire-and-forget 后台线程，轮询等待其把 attach 发给哨兵
    for _ in range(40):
        if any(a.get("pid") == 4321 and a.get("agent") == "codebuddy" for a in _Handler.attach_seen):
            break
        time.sleep(0.05)
    assert any(a.get("pid") == 4321 and a.get("agent") == "codebuddy" for a in _Handler.attach_seen)


@pytest.mark.asyncio
async def test_run_agent_default_disabled_no_regression(monkeypatch):
    # 默认配置（哨兵关闭）：行为与旧版一致，正常派发。
    monkeypatch.setattr("bridge.runner._execute", _fake_execute(return_pid=7))
    resolved = ResolvedCommand("fake.exe", source="path", exists=True)
    result = await run_agent(
        AGENT_SPECS["codebuddy"], resolved, "请帮我写个函数", config=BridgeConfig(), cwd="D:/work", timeout=10
    )
    assert result.blocked_by_sentinel is False
    assert result.ok is True
    assert result.pid == 7


@pytest.mark.asyncio
async def test_run_agent_fail_open_when_sentinel_down(monkeypatch):
    # 哨兵启用但进程没跑：必须放行并照常派发（fail-open）。
    monkeypatch.setattr("bridge.runner._SENTINEL_ATTACH_DELAY", 0)
    monkeypatch.setattr("bridge.runner._execute", _fake_execute(return_pid=8))
    config = BridgeConfig()
    config.sentinel = SentinelConfig(enabled=True, url="http://127.0.0.1:1", timeout=0.3)
    resolved = ResolvedCommand("fake.exe", source="path", exists=True)
    result = await run_agent(
        AGENT_SPECS["codebuddy"], resolved, "BLOCKME 危险", config=config, cwd="D:/work", timeout=10
    )
    assert result.blocked_by_sentinel is False
    assert result.ok is True
    assert result.pid == 8


def test_execute_blocking_popen_failure_returns_full_tuple(monkeypatch):
    """启动失败也必须回完整的 7 元组 —— 少一个 pid 会在解包处炸掉整个派发层。"""

    def _boom(*args, **kwargs):
        raise OSError("模拟启动失败")

    monkeypatch.setattr("bridge.runner.subprocess.Popen", _boom)
    out = _execute_blocking(
        ["definitely-not-a-real-binary"],
        cwd=None,
        timeout=1,
        output_limit=100,
        env={},
        stream_grace=0.1,
    )
    assert len(out) == 7
    assert out[0] is None
    assert out[6] is None
    assert "无法启动进程" in out[5]
