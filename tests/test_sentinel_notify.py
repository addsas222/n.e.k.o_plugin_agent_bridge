"""告警通道 / 夜间晨报 / 时间盒的测试。

这三块决定「拦下来之后有没有人知道」以及「单个子任务会不会把整轮挂死」，都是
半夜挂机场景的关键路径，所以要能被单独钉死：

* 通道层：成功判据、失败不抛、至少一条成功即送达；
* 晨报：空账本不编造、红线排第一、结论行必须在；
* 时间盒：阈值分级、warn 只打扰一次、``limit<=0`` 关闭。

HTTP 服务器一律由 fixture 起在 ``127.0.0.1:0``（系统分配端口）并在收尾时
``shutdown`` + ``server_close`` + ``join``，避免后台线程泄漏污染其他测试。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

# isort: split
# bridge 在插件仓库内是一方包、在 CI 的检出布局下会被当成三方包；
# 显式切段，两种 cwd 下的 I001 判定就都稳定了。

from bridge.sentinel_core.briefing import build_briefing, render_briefing
from bridge.sentinel_core.channels import ChannelHub, LocalChannel, WebhookChannel
from bridge.sentinel_core.store import EventStore
from bridge.sentinel_core.timebox import TimeBox

FP = "sess-9|D:/work/night"


# ---------------------------------------------------------------------------
# 本地 HTTP 服务器（测试替身）
# ---------------------------------------------------------------------------


class _Recorder(BaseHTTPRequestHandler):
    """记录收到的 POST 体，按类属性里的状态码回包。"""

    status = 200
    location = ""
    received: list[bytes] = []

    def do_POST(self) -> None:  # noqa: N802 —— 基类规定的方法名
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).received.append(body)
        self.send_response(type(self).status)
        if 300 <= int(type(self).status) < 400:
            self.send_header("Location", type(self).location or "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 —— 基类签名
        """静音：测试不需要访问日志。"""


@pytest.fixture
def webhook_server():
    """起一个可指定状态码（与重定向目标）的本地 HTTP 服务器。

    :return: ``factory(status=200, location=None) -> (url, handler_cls)``；收尾时统一关闭。
    """
    started: list[tuple[ThreadingHTTPServer, threading.Thread]] = []

    def factory(
        status: int = 200,
        location: str | None = None,
        handler_cls: type[BaseHTTPRequestHandler] | None = None,
    ) -> tuple[str, type[_Recorder]]:
        if handler_cls is not None:
            handler = handler_cls  # type: ignore[assignment]
        else:
            handler = type("Handler", (_Recorder,), {"status": status, "location": location or "", "received": []})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, name="test-webhook", daemon=True)
        thread.start()
        started.append((server, thread))
        host, port = server.server_address[0], server.server_address[1]
        return f"http://{host}:{port}/hook", handler

    yield factory

    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 通道层
# ---------------------------------------------------------------------------


def test_local_channel_always_succeeds():
    channel = LocalChannel()
    assert channel.name == "local"
    assert channel.send({"id": 1, "level": "critical", "title": "红线拦截", "body": "rm -rf /"}) is True
    # 脏数据也不许抛（兜底通道的契约）。
    assert channel.send({}) is True


def test_webhook_rejects_non_http_scheme():
    for url in ("file:///tmp/hook", "ftp://example.com/hook", "cmd://x", "", "not-a-url"):
        channel = WebhookChannel(url, timeout=0.5)
        assert channel.send({"id": 1, "title": "x"}) is False, url


def test_webhook_success_posts_utf8_json(webhook_server):
    url, handler = webhook_server(200)
    channel = WebhookChannel(url, timeout=2.0)
    alert = {"id": 7, "level": "critical", "title": "红线拦截：删根", "body": "rm -rf /"}

    assert channel.send(alert) is True
    assert handler.received, "服务器没有收到请求"
    body = handler.received[0]
    # ensure_ascii=False：中文必须以原始 UTF-8 出现，不能被转成 \uXXXX。
    assert "红线拦截".encode() in body
    assert json.loads(body.decode("utf-8")) == alert


def test_webhook_server_error_returns_false(webhook_server):
    url, _handler = webhook_server(500)
    assert WebhookChannel(url, timeout=2.0).send({"id": 1, "title": "x"}) is False


def test_webhook_unreachable_returns_false():
    # 127.0.0.1:1 基本不会有人监听 → 连接被拒；关键是「返回 False 而不是抛异常」。
    assert WebhookChannel("http://127.0.0.1:1/hook", timeout=0.5).send({"id": 1, "title": "x"}) is False


def test_hub_clears_pending_when_any_channel_succeeds(tmp_path):
    store = EventStore(tmp_path / "t.db")
    store.add_alert(level="critical", title="红线拦截：删根", body="rm -rf /", fingerprint=FP)
    store.add_alert(level="warn", title="高危命令", fingerprint=FP)
    assert len(store.pending_alerts()) == 2

    hub = ChannelHub([LocalChannel()])
    result = hub.dispatch_pending(store)

    assert result == {"sent": 2, "failed": 0, "skipped": 0}
    assert store.pending_alerts() == []


def test_hub_keeps_pending_when_all_channels_fail(tmp_path):
    store = EventStore(tmp_path / "t.db")
    store.add_alert(level="critical", title="红线拦截：抹盘", fingerprint=FP)

    hub = ChannelHub([WebhookChannel("http://127.0.0.1:1/hook", timeout=0.5)])
    result = hub.dispatch_pending(store)

    assert result == {"sent": 0, "failed": 1, "skipped": 0}
    pending = store.pending_alerts()
    assert len(pending) == 1
    assert pending[0]["title"] == "红线拦截：抹盘"


def test_hub_falls_back_to_next_channel(tmp_path, webhook_server):
    url, _handler = webhook_server(500)
    store = EventStore(tmp_path / "t.db")
    store.add_alert(level="warn", title="高危命令", fingerprint=FP)

    # 第一条通道 500，第二条兜底成功。告警不丢，但失败必须如实计进 failed ——
    # 「有人成功了」不能成为瞒掉 webhook 失败的理由（否则用户以为外发也通了）。
    hub = ChannelHub([WebhookChannel(url, timeout=2.0), LocalChannel()])
    assert hub.dispatch_pending(store) == {"sent": 1, "failed": 1, "skipped": 0}
    assert store.pending_alerts() == []


def test_hub_without_channels_does_nothing(tmp_path):
    store = EventStore(tmp_path / "t.db")
    store.add_alert(level="warn", title="高危命令", fingerprint=FP)

    # 没有通道：一次投递都没发生，三个计数都该是 0（计数单位是通道尝试次数）。
    assert ChannelHub().dispatch_pending(store) == {"sent": 0, "failed": 0, "skipped": 0}
    assert len(store.pending_alerts()) == 1


def test_broken_channel_does_not_break_dispatch(tmp_path):
    class Exploding:
        name = "boom"

        def send(self, alert: dict) -> bool:
            raise RuntimeError("通道实现不守约定")

    store = EventStore(tmp_path / "t.db")
    store.add_alert(level="warn", title="高危命令", fingerprint=FP)

    hub = ChannelHub([Exploding(), LocalChannel()])
    assert hub.dispatch_pending(store) == {"sent": 1, "failed": 1, "skipped": 0}
    assert store.pending_alerts() == []


def test_hub_reaches_every_channel_in_production_order(tmp_path):
    """C1 回归：生产装配是 ``[LocalChannel(), WebhookChannel(...)]``。

    旧实现「第一条成功即 break」，而 LocalChannel 恒成功 —— webhook 一次都不会被调用，
    告警却被标成 delivered=1。这条用例按**生产顺序**装配，断言 webhook 确实收到了一次。
    """
    calls: list[str] = []

    class FakeWebhook:
        name = "webhook"

        def send(self, alert: dict) -> bool:
            calls.append(str(alert.get("title") or ""))
            return True

    store = EventStore(tmp_path / "t.db")
    store.add_alert(level="critical", title="红线拦截：删根", fingerprint=FP)

    hub = ChannelHub([LocalChannel(), FakeWebhook()])
    result = hub.dispatch_pending(store)

    assert calls == ["红线拦截：删根"], "webhook 被跳过了一次都没调用"
    assert result == {"sent": 2, "failed": 0, "skipped": 0}
    assert store.pending_alerts() == []
    # 账本记的是**非兜底**通道名（webhook 真的发出去了），不是先跑到的 local。
    assert store.alerts()[0]["channel"] == "webhook", "账本把 webhook 成功记成了 local"


def test_delivered_channel_falls_back_to_local_only_when_webhook_fails(tmp_path):
    """外发全挂时才落 ``local``：账本要回答「人有没有被通知到」。"""
    class DeadWebhook:
        name = "webhook"

        def send(self, alert: dict) -> bool:
            return False

    store = EventStore(tmp_path / "t.db")
    store.add_alert(level="critical", title="红线拦截：删根", fingerprint=FP)
    hub = ChannelHub([LocalChannel(), DeadWebhook()])

    assert hub.dispatch_pending(store) == {"sent": 1, "failed": 1, "skipped": 0}
    # 外发失败不该让告警永远卡在 pending（本地已收到），但账本必须写清走的是兜底。
    assert store.pending_alerts() == []
    assert store.alerts()[0]["channel"] == "local"


def test_hub_tolerates_missing_store():
    assert ChannelHub([LocalChannel()]).dispatch_pending(None) == {"sent": 0, "failed": 0, "skipped": 0}


# ---------------------------------------------------------------------------
# 通道安全：明文外发 / 重定向 / 外发脱敏
# （对应 sec-audit 的 L4 / L5 / M6）
# ---------------------------------------------------------------------------


def test_webhook_refuses_plaintext_http_to_remote_host(monkeypatch):
    """L5：非本机主机 + 明文 http 一律拒发，而且根本不该发起连接。"""

    def _boom(*_args, **_kwargs):
        raise AssertionError("不该对外网发起明文 http 请求")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    channel = WebhookChannel("http://example.com/hook", timeout=0.5)

    assert channel.refuses({"id": 1}) != ""
    assert channel.send({"id": 1, "title": "x"}) is False


def test_webhook_allows_loopback_plaintext():
    # 回环地址上跑明文 http 不算外发，仍然允许（本机调试最常见的用法）。
    for url in ("http://127.0.0.1:9/hook", "http://localhost:9/hook", "http://[::1]:9/hook"):
        assert WebhookChannel(url, timeout=0.2).refuses({"id": 1}) == "", url
    # https 到外网也不拒（加密后没有明文外发问题）。
    assert WebhookChannel("https://example.com/hook").refuses({"id": 1}) == ""


def test_webhook_invalid_scheme_reports_reason():
    channel = WebhookChannel("file:///etc/passwd")
    assert "scheme" in channel.refuses({"id": 1})
    assert channel.send({"id": 1}) is False


def test_hub_counts_refused_target_as_skipped(tmp_path):
    """拒发的通道不算 failed：重试永远不会成功，记 skipped 才不刷屏。"""
    store = EventStore(tmp_path / "t.db")
    store.add_alert(level="critical", title="红线拦截", fingerprint=FP)

    hub = ChannelHub([WebhookChannel("http://example.com/hook")])
    assert hub.dispatch_pending(store) == {"sent": 0, "failed": 0, "skipped": 1}
    # 仍然留在 pending，但每轮只是本地判定，不会发请求。
    assert len(store.pending_alerts()) == 1


def test_webhook_does_not_follow_redirect(webhook_server):
    """L5：跨源 302 不跟随，跳转目标收不到任何请求（Authorization 与 body 不外泄）。"""
    target_url, target_handler = webhook_server(200)
    redirect_url, _redirect_handler = webhook_server(302, location=target_url)
    assert urlsplit(redirect_url).netloc != urlsplit(target_url).netloc

    channel = WebhookChannel(redirect_url, timeout=2.0, headers={"Authorization": "Bearer super-secret-token"})
    assert channel.send({"id": 1, "title": "x"}) is False

    # 关键断言：跳转目标一次都没被访问过。
    assert target_handler.received == []


class _SameOriginRedirect(BaseHTTPRequestHandler):
    """先 302 到同源的 ``/final``，再在 ``/final`` 上回 200。"""

    seen: list[str] = []

    def do_POST(self) -> None:  # noqa: N802 —— 基类规定的方法名
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        type(self).seen.append(self.path)
        if self.path == "/hook":
            self.send_response(302)
            self.send_header("Location", "/final")
        else:
            self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 —— 基类签名
        """静音。"""


def test_webhook_follows_same_origin_redirect_once(webhook_server):
    """L5：同源 302 手工重发一次（很多 webhook 只是补个结尾斜杠）。"""
    url, handler = webhook_server(handler_cls=_SameOriginRedirect)
    channel = WebhookChannel(url, timeout=2.0, headers={"Authorization": "Bearer tok"})

    assert channel.send({"id": 1, "title": "x"}) is True
    # 同源才重发，且只重发一次：/hook 收 302 → /final 收 200。
    assert handler.seen == ["/hook", "/final"]


def test_webhook_redacts_secrets_before_sending(webhook_server):
    """M6：命令原文里的凭据不能原样出网。"""
    url, handler = webhook_server(200)
    channel = WebhookChannel(url, timeout=2.0)
    alert = {
        "id": 3,
        "level": "critical",
        "title": "拦下红线级命令",
        "body": "deploy --token=abc123 --user=bob; curl -H 'Authorization: Bearer xyz.abc-123_456' https://api",
        "detail": "OPENAI_API_KEY=sk-abcdefghijklmnop1234",
        "fingerprint": FP,
    }

    assert channel.send(alert) is True
    raw = handler.received[0].decode("utf-8")
    payload = json.loads(raw)

    assert "abc123" not in raw
    assert "xyz.abc-123_456" not in raw
    assert "sk-abcdefghijklmnop1234" not in raw
    assert "***" in payload["body"]
    assert "--user=bob" in payload["body"], "非敏感参数不该被打码"
    # 账本字段照旧：脱敏只作用于外发副本。
    assert alert["body"].find("abc123") >= 0


# ---------------------------------------------------------------------------
# 晨报
# ---------------------------------------------------------------------------


def _seed_red_line(store: EventStore, *, ts: float) -> int:
    """写一条红线拦截事件（模拟派发前拦截）。"""
    return store.add_event(
        kind="precheck",
        fingerprint=FP,
        severity="red_line",
        verdict="dangerous",
        rule_id="rm-root",
        summary="rm -rf / --no-preserve-root",
        ts=ts,
    )


def test_briefing_on_empty_ledger(tmp_path):
    store = EventStore(tmp_path / "t.db")
    now = time.time()
    data = build_briefing(store, since=now - 3600)

    assert data["events"] == 0
    assert data["alerts"] == 0
    assert data["blocked"] == 0
    assert data["highlights"] == []
    assert data["first_seen"] is None and data["last_seen"] is None
    assert data["by_severity"] == {} and data["by_level"] == {}
    assert data["usage"]["total_tokens"] == 0
    assert data["range"]["since"] == pytest.approx(now - 3600)
    assert data["range"]["until"] is None


def test_briefing_puts_red_line_first(tmp_path):
    store = EventStore(tmp_path / "t.db")
    now = time.time()
    store.add_event(kind="precheck", fingerprint=FP, severity="standard", summary="rm -rf ./dist", ts=now - 10)
    store.add_event(kind="line", fingerprint=FP, summary="普通输出", ts=now - 5)
    red_id = _seed_red_line(store, ts=now - 60)
    store.add_alert(level="warn", title="高危命令", body="chmod 777", fingerprint=FP, ts=now - 9)

    data = build_briefing(store, since=now - 3600)

    assert data["events"] == 3
    assert data["blocked"] == 2  # red_line + standard
    assert data["by_severity"]["red_line"] == 1
    assert data["highlights"], "应该有高亮条目"
    first = data["highlights"][0]
    assert first["type"] == "event"
    assert first["id"] == red_id
    assert first["severity"] == "red_line"
    # 告警排在红线之后、普通高危之前。
    assert data["highlights"][1]["type"] == "alert"


def test_render_briefing_empty_says_no_records(tmp_path):
    store = EventStore(tmp_path / "t.db")
    text = render_briefing(build_briefing(store, since=time.time() - 3600))

    assert "这段时间没有记录" in text
    # 不许把「没记录」说成「一切正常」。
    assert "平安" not in text
    assert "一切正常" not in text


def test_render_briefing_red_line_conclusion(tmp_path):
    store = EventStore(tmp_path / "t.db")
    now = time.time()
    _seed_red_line(store, ts=now)
    text = render_briefing(build_briefing(store, since=now - 3600))
    lines = text.splitlines()

    assert lines[0].startswith("哨兵晨报")
    assert "结论" in lines[1]
    assert "红线" in lines[1]
    assert "关键事件" in text
    assert "rm -rf / --no-preserve-root" in text
    assert "Token" in text
    assert len(lines) <= 25


def test_render_briefing_survives_garbage():
    # 渲染路径 fail-open：脏数据也要给一句人话，而不是抛异常。
    assert render_briefing({})
    assert render_briefing({"highlights": [{}], "by_severity": None})


# ---------------------------------------------------------------------------
# 时间盒
# ---------------------------------------------------------------------------


def test_timebox_silent_before_warn_ratio():
    box = TimeBox(100.0, warn_ratio=0.8)
    started = 1000.0
    assert box.check(started, now=1079.0) is None


def test_timebox_warns_only_once():
    box = TimeBox(100.0, warn_ratio=0.8)
    started = 1000.0

    first = box.check(started, now=1080.0)
    assert first is not None
    assert first["level"] == "warn"
    assert first["limit"] == 100.0
    assert first["elapsed"] == pytest.approx(80.0)
    assert first["overrun"] == 0.0

    # 同一 started_at 第二次不再打扰。
    assert box.check(started, now=1085.0) is None


def test_timebox_expires_with_overrun():
    box = TimeBox(60.0, warn_ratio=0.8)
    started = 1000.0

    assert box.check(started, now=1048.0)["level"] == "warn"
    expired = box.check(started, now=1075.0)
    assert expired is not None
    assert expired["level"] == "expired"
    assert expired["overrun"] == pytest.approx(15.0)
    assert expired["elapsed"] == pytest.approx(75.0)
    # 超时状态每次都要报，不受「提醒一次」限制。
    assert box.check(started, now=1080.0)["level"] == "expired"


def test_timebox_label_roundtrip():
    box = TimeBox(30.0)
    started = box.start(label="codex / 重构 diff")
    report = box.check(started, now=started + 31.0)
    assert report is not None
    assert report["label"] == "codex / 重构 diff"


def test_timebox_disabled_when_limit_non_positive():
    for limit in (0, -1.0):
        box = TimeBox(limit)
        started = 1000.0
        assert box.check(started, now=1_000_000.0) is None
        assert box.elapsed(started, now=1_100.0) == pytest.approx(100.0)


def test_timebox_elapsed_is_clamped():
    box = TimeBox(10.0)
    # 时钟倒退时不许返回负数。
    assert box.elapsed(1000.0, now=900.0) == 0.0
