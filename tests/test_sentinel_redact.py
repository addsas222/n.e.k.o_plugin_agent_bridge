"""脱敏的回归测试（sec-audit M6）。

要钉死三件事：

1. **该擦的擦掉**：``sk-`` / ``ghp_`` / ``AKIA`` / ``xoxb-`` / JWT / PEM / Bearer /
   ``key=value`` / URL 里的 ``user:pass@``；
2. **不该擦的别碰**：中文说明（``password: 请勿泄露``）与用量计数
   （``total_tokens: 12345``）必须原样保留 —— 误伤会让人以为账本坏了；
3. **真的落不到盘上、也出不了网**：写入后按**磁盘字节**检查，而不是读回来比对
   （读回来是格式化后的值，看不出 WAL/索引里有没有残留）。
"""

from __future__ import annotations

import json

import pytest

# isort: split
# bridge 在插件仓库内是一方包、在 CI 的检出布局下会被当成三方包；
# 显式切段，两种 cwd 下的 I001 判定就都稳定了。

from bridge.sentinel_core.channels import WebhookChannel, _redact_outbound
from bridge.sentinel_core.redact import MARK, scrub, scrub_fields
from bridge.sentinel_core.store import EventStore

FP = "sess-r|D:/work/repo"


# ---------------------------------------------------------------------------
# 各类密钥形态
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("export OPENAI_API_KEY=sk-abcdefghijklmnop1234", "sk-abcdefghijklmnop1234"),
        ("git clone https://ghp_abcdefghijklmnopqrstuvwxyz0123@github.com/x", "ghp_abcdefghijklmnopqrstuvwxyz0123"),
        ("aws_access_key_id=AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("slack xoxb-123456789012-abcdefghijkl", "xoxb-123456789012-abcdefghijkl"),
        (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
            "eyJhbGciOiJIUzI1NiJ9",
        ),
        ('curl -H "Authorization: Bearer xyz.abc-123_456"', "xyz.abc-123_456"),
        ("mysql://root:hunter2@db:3306/app", "hunter2"),
        ("deploy --token=abc123", "abc123"),
        ("password = 'hunter2'", "hunter2"),
        ("api_key: 0f0f0f0f0f0f", "0f0f0f0f0f0f"),
    ],
)
def test_scrub_masks_known_secret_forms(text, secret):
    out = scrub(text)
    assert secret not in out
    assert MARK in out


def test_scrub_masks_whole_pem_block():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nAAABBBCCC\n-----END RSA PRIVATE KEY-----"
    out = scrub(pem)
    assert "MIIEowIBAAKCAQEA" not in out
    assert "AAABBBCCC" not in out
    assert "PRIVATE KEY" in out, "保留标记，读者要看得出这里原本是私钥"


# ---------------------------------------------------------------------------
# 不误伤
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "password: 请勿泄露给别人",
        "这是 password 字段的说明，请谨慎填写。",
        "密码请用 password 字段填写，不要写在这里。",
        "total_tokens: 12345 input_tokens=280000",
        "Token 约 343.5k（输入 280.0k，估算值）",
        "git push --force origin main",
        "rm -rf / --no-preserve-root",
    ],
)
def test_scrub_leaves_ordinary_text_alone(text):
    assert scrub(text) == text


# ---------------------------------------------------------------------------
# 截断与幂等
# ---------------------------------------------------------------------------


def test_scrub_truncates_with_marker():
    out = scrub("x" * 5000, limit=100)
    assert out.startswith("x" * 100)
    assert "已截断" in out
    assert "5000" in out, "要能看出原始长度"


def test_scrub_truncation_counts_original_length():
    # 先脱敏后截断，但「原长」报的必须是**原文**长度。
    text = "--token=abc123 " + "y" * 4000
    out = scrub(text, limit=200)
    assert str(len(text)) in out


def test_scrub_is_idempotent():
    once = scrub("deploy --token=abc123 && Bearer xyz.abc-123_456")
    assert scrub(once) == once


def test_scrub_tolerates_weird_input():
    assert scrub(None) == ""
    assert scrub("") == ""
    # 不可转字符串的脏对象不许抛。
    assert isinstance(scrub(object()), str)


def test_scrub_fields_only_touches_listed_fields():
    record = {"summary": "--token=abc123", "verdict": "dangerous", "rule_id": "l2.x", "files": 3}
    out = scrub_fields(record, {"summary", "verdict"})
    assert "abc123" not in out["summary"]
    assert out["verdict"] == "dangerous", "枚举值必须原样保留"
    assert out["rule_id"] == "l2.x"
    assert out["files"] == 3
    assert record["summary"] == "--token=abc123", "不许改原对象"


# ---------------------------------------------------------------------------
# 落盘：按磁盘字节检查，不只看读回来的值
# ---------------------------------------------------------------------------


def test_secrets_never_reach_the_database_file(tmp_path):
    path = tmp_path / "sentinel.db"
    store = EventStore(path)
    store.add_event(
        kind="tool",
        fingerprint=FP,
        summary="deploy --token=abc123",
        detail="Authorization: Bearer xyz.abc-123_456",
        raw='{"cmd":"export API_KEY=sk-abcdefghijklmnop1234"}',
    )
    store.add_alert(
        level="critical",
        title="命中：--token=abc123",
        body="mysql://root:hunter2@db:3306/app",
        fingerprint=FP,
    )

    secrets = ("abc123", "xyz.abc-123_456", "sk-abcdefghijklmnop1234", "hunter2")
    blob = path.read_bytes()
    for secret in secrets:
        assert secret.encode() not in blob, f"{secret!r} 仍然明文留在账本文件里"

    # 读回来的文本也必须是打码后的（时间线/晨报都从这条路径取值）。
    assert "***" in store.timeline()[0]["summary"]
    assert "***" in store.alerts()[0]["body"]


def test_safepoint_label_is_scrubbed_but_ref_is_not(tmp_path):
    store = EventStore(tmp_path / "sentinel.db")
    ref = "a" * 40
    store.add_safepoint(kind="git", ref=ref, label="开工前：--token=abc123", fingerprint=FP)

    row = store.latest_safepoint(fingerprint=FP)
    assert row is not None
    assert "abc123" not in row["label"]
    assert row["ref"] == ref, "ref 是恢复时要用的凭据，绝不能打码"


def test_session_goal_is_scrubbed(tmp_path):
    store = EventStore(tmp_path / "sentinel.db")

    class _Session:
        fingerprint = FP
        session_id = "s1"
        cwd = "D:/work/repo"
        pid = 1
        agent = "claude"
        goal = "帮我 curl -H 'Authorization: Bearer tok.abc-123_456' 这个地址"
        created_at = 0.0
        events = 0
        alerts = 0

    store.upsert_session(_Session())
    assert b"tok.abc-123_456" not in (tmp_path / "sentinel.db").read_bytes()


# ---------------------------------------------------------------------------
# 外发：纵深防御的第二道
# ---------------------------------------------------------------------------


def test_redact_outbound_walks_nested_structures():
    payload = {
        "title": "t",
        "body": "--token=abc123",
        "nested": {"list": ["Bearer xyz.abc-123_456", 42]},
    }
    out = _redact_outbound(payload)
    assert "abc123" not in json.dumps(out, ensure_ascii=False)
    assert "xyz.abc-123_456" not in json.dumps(out, ensure_ascii=False)
    assert out["nested"]["list"][1] == 42
    # 原对象不被修改。
    assert payload["body"] == "--token=abc123"


def test_webhook_body_is_scrubbed_before_leaving(monkeypatch):
    channel = WebhookChannel("http://127.0.0.1:9/hook", timeout=0.1)
    captured: dict[str, bytes] = {}

    def _fake_post(url: str, payload: bytes, headers: dict):
        captured["body"] = payload
        return ("ok", 200)

    monkeypatch.setattr(channel, "_post_once", _fake_post)
    alert = {
        "id": 1,
        "level": "critical",
        "title": "拦下红线级命令",
        "body": "deploy --token=abc123; curl -H 'Authorization: Bearer xyz.abc-123_456'",
        "detail": "OPENAI_API_KEY=sk-abcdefghijklmnop1234",
    }

    assert channel.send(alert) is True
    raw = captured["body"].decode("utf-8")
    for secret in ("abc123", "xyz.abc-123_456", "sk-abcdefghijklmnop1234"):
        assert secret not in raw
