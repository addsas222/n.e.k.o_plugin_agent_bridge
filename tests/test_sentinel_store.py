"""账本 / 时间线 / Token 审计的测试。

这三块是后续所有能力的地基（晨报、复盘、消耗统计、回滚记录），
所以要能被单独钉死，而不是靠上层功能"顺带验证"。
"""

from __future__ import annotations

import time

# isort: split
# bridge 在插件仓库内是一方包、在 CI 的检出布局下会被当成三方包；
# 显式切段，两种 cwd 下的 I001 判定就都稳定了。

from bridge.sentinel_core.metrics import TokenLedger, estimate_tokens, render_usage
from bridge.sentinel_core.store import EventStore, default_db_path, resolve_db_path
from bridge.sentinel_core.timeline import build_timeline, render_timeline, render_timeline_lines

FP = "sess-1|D:/work/demo"


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------


def test_store_creates_schema_and_accepts_rows(tmp_path):
    store = EventStore(tmp_path / "t.db")
    assert store.enabled is True
    assert store._schema_ready is True

    event_id = store.add_event(kind="precheck", fingerprint=FP, verdict="allow", summary="写个排序")
    assert event_id > 0
    assert len(store.timeline()) == 1


def test_store_timeline_filters(tmp_path):
    store = EventStore(tmp_path / "t.db")
    now = time.time()
    store.add_event(kind="precheck", fingerprint=FP, summary="a", ts=now)
    store.add_event(kind="precheck", fingerprint="other", summary="b", ts=now + 1)
    store.add_event(kind="error", fingerprint=FP, severity="standard", summary="c", ts=now + 2)

    assert len(store.timeline()) == 3
    assert len(store.timeline(fingerprint=FP)) == 2
    assert len(store.timeline(severity="standard")) == 1
    assert len(store.timeline(kinds=["error"])) == 1
    assert len(store.timeline(since=now + 1)) == 2
    assert len(store.timeline(until=now + 1)) == 2


def test_store_timeline_returns_chronological_order(tmp_path):
    store = EventStore(tmp_path / "t.db")
    now = time.time()
    for offset, label in enumerate(["first", "second", "third"]):
        store.add_event(kind="line", summary=label, ts=now + offset)
    rows = store.timeline()
    assert [row["summary"] for row in rows] == ["first", "second", "third"]


def test_store_alerts_roundtrip(tmp_path):
    store = EventStore(tmp_path / "t.db")
    assert store.pending_alerts() == []
    alert_id = store.add_alert(level="critical", title="红线拦截", body="rm -rf /", fingerprint=FP)
    pending = store.pending_alerts()
    assert len(pending) == 1
    assert pending[0]["title"] == "红线拦截"
    store.mark_delivered(alert_id, channel="local")
    assert store.pending_alerts() == []


def test_store_safepoint_latest_skips_restored(tmp_path):
    store = EventStore(tmp_path / "t.db")
    first = store.add_safepoint(kind="git", ref="aaa", fingerprint=FP)
    store.add_safepoint(kind="git", ref="bbb", fingerprint=FP)
    assert store.latest_safepoint(fingerprint=FP)["ref"] == "bbb"

    store.mark_restored(first)
    # 已回滚的不再是「最近可回滚点」
    assert store.latest_safepoint(fingerprint=FP)["ref"] == "bbb"


def test_store_stats_counts_severity_and_tokens(tmp_path):
    store = EventStore(tmp_path / "t.db")
    store.add_event(kind="precheck", severity="red_line", summary="x")
    store.add_event(kind="precheck", severity="standard", summary="y")
    store.add_event(kind="line", summary="z")
    store.add_tokens(agent="codebuddy", input_tokens=100, output_tokens=50, saved_tokens=20)
    store.add_alert(level="critical", title="t")

    stats = store.stats()
    assert stats["events"] == 3
    assert stats["by_severity"] == {"red_line": 1, "standard": 1}
    assert stats["blocked"] == 2
    assert stats["alerts"] == 1
    assert stats["input_tokens"] == 100
    assert stats["saved_tokens"] == 20


def test_store_prune_keeps_recent(tmp_path):
    store = EventStore(tmp_path / "t.db")
    old = time.time() - 40 * 86400
    store.add_event(kind="line", summary="old", ts=old)
    store.add_event(kind="line", summary="new")
    assert store.prune(days=30) == 1
    rows = store.timeline()
    assert [row["summary"] for row in rows] == ["new"]


def test_store_upsert_session_and_close(tmp_path):
    class _Session:
        fingerprint = FP
        session_id = "sess-1"
        cwd = "D:/work/demo"
        pid = 4321
        agent = "codebuddy"
        goal = "修好登录页"
        events = 3
        alerts = 1
        created_at = time.time()

    store = EventStore(tmp_path / "t.db")
    store.upsert_session(_Session())
    store.close_session(FP, exit_code=0)
    # 再 upsert 一次不应把 goal 冲掉
    _Session.goal = ""
    store.upsert_session(_Session())
    rows = store._read("SELECT * FROM sessions WHERE fingerprint = ?", (FP,))
    assert rows[0]["goal"] == "修好登录页"
    assert rows[0]["exit_code"] == 0


def test_store_disabled_degrades_silently(tmp_path):
    """账本不可用时必须安静降级，绝不能把派发拖垮。"""
    store = EventStore(tmp_path / "t.db")
    store.enabled = False
    assert store.add_event(kind="line", summary="x") == 0
    assert store.timeline() == []
    assert store.stats()["events"] == 0
    assert store.prune(days=1) == 0


def test_store_reads_from_another_thread(tmp_path):
    """尾随线程写、诊断线程读是常态，连接必须每线程各一份。"""
    import threading

    store = EventStore(tmp_path / "t.db")
    done = threading.Event()

    def _writer():
        store.add_event(kind="line", summary="from-thread")
        done.set()

    threading.Thread(target=_writer, daemon=True).start()
    assert done.wait(5.0)
    assert len(store.timeline()) == 1


def test_resolve_db_path_prefers_data_dir(tmp_path):
    assert default_db_path().name == "sentinel.db"
    assert resolve_db_path("", fallback=tmp_path) == tmp_path / "sentinel.db"
    assert resolve_db_path("custom.db", fallback=tmp_path) == tmp_path / "custom.db"
    absolute = tmp_path / "abs.db"
    assert resolve_db_path(str(absolute), fallback=tmp_path) == absolute


# ---------------------------------------------------------------------------
# 会话查询（面板用）
# ---------------------------------------------------------------------------
#
# ``sessions()`` 是面板拿「哪几路会话、各自跑到哪了」的**唯一**公开入口。之前面板
# 只能读到事件流，于是它只能显示「有事发生」，说不出「是哪一路、结束了没有」。


def _session_obj(
    fingerprint: str,
    *,
    created_at: float,
    agent: str = "codebuddy",
    cwd: str = "D:/work/demo",
    goal: str = "",
    events: int = 0,
    alerts: int = 0,
):
    """造一个 ``upsert_session`` 能吃的会话对象。

    它用 ``getattr`` 取字段，所以**必须传对象不传 dict**：传 dict 会让所有字段落成
    默认空值，而且空指纹会撞主键，多路会话被静默合并成一行——这种错不报任何异常。
    """

    class _Session:
        pass

    obj = _Session()
    obj.fingerprint = fingerprint
    obj.session_id = fingerprint
    obj.cwd = cwd
    obj.pid = 1234
    obj.agent = agent
    obj.goal = goal
    obj.events = events
    obj.alerts = alerts
    obj.created_at = created_at
    return obj


def test_store_sessions_empty_returns_empty_list(tmp_path):
    store = EventStore(tmp_path / "t.db")
    assert store.sessions() == [], "空库必须返回空列表（不是 None，面板会直接 len()）"


def test_store_sessions_returns_rows_newest_first(tmp_path):
    store = EventStore(tmp_path / "t.db")
    now = time.time()
    # 先插旧的、再插新的：若按插入顺序返回就会红
    store.upsert_session(_session_obj("sess-old|D:/work/demo", created_at=now - 120, goal="旧会话", events=2, alerts=1))
    store.upsert_session(_session_obj("sess-new|D:/work/demo", created_at=now - 60, goal="新会话"))

    rows = store.sessions()
    assert len(rows) == 2, f"插了 2 路会话，sessions() 该回 2 条，实际 {len(rows)}"
    assert [row["fingerprint"] for row in rows] == ["sess-new|D:/work/demo", "sess-old|D:/work/demo"], (
        "会话必须按 started_at 倒序（最近跑的排最上面）"
    )

    required = {"fingerprint", "agent", "cwd", "goal", "started_at", "ended_at", "exit_code", "events", "alerts"}
    missing = required - set(rows[0])
    assert not missing, f"面板要的字段缺失：{sorted(missing)}（实际键：{sorted(rows[0])}）"
    assert rows[1]["goal"] == "旧会话", f"goal 要透传到面板，实际 {rows[1]['goal']!r}"
    assert rows[1]["events"] == 2, "events 计数要透传"
    assert rows[1]["alerts"] == 1, "alerts 计数要透传"
    assert rows[1]["ended_at"] is None, "没收尾的会话 ended_at 必须是 None（面板靠它判断 active）"


def test_store_sessions_respects_limit(tmp_path):
    store = EventStore(tmp_path / "t.db")
    now = time.time()
    for index in range(3):
        store.upsert_session(_session_obj(f"sess-{index}", created_at=now - (3 - index) * 10))

    rows = store.sessions(limit=1)
    assert len(rows) == 1, f"limit=1 必须只回 1 条，实际 {len(rows)}"
    assert rows[0]["fingerprint"] == "sess-2", "截断时要保留最新的那一路，而不是最早插入的那一路"


def test_store_sessions_since_filters_by_started_at(tmp_path):
    store = EventStore(tmp_path / "t.db")
    now = time.time()
    store.upsert_session(_session_obj("sess-old", created_at=now - 3600))
    store.upsert_session(_session_obj("sess-new", created_at=now - 60))

    rows = store.sessions(since=now - 600)
    assert [row["fingerprint"] for row in rows] == ["sess-new"], (
        "since 之前开始的会话不该算进这次查询（面板只关心时间窗内的会话）"
    )


def test_store_sessions_empty_when_disabled(tmp_path):
    store = EventStore(tmp_path / "t.db")
    store.upsert_session(_session_obj("sess-1", created_at=time.time() - 10))
    store.enabled = False
    assert store.sessions() == [], "账本关掉后所有读路径都要安静返回空，不能抛也不能读旧数据"


def test_store_sessions_upsert_same_fingerprint_keeps_one_row(tmp_path):
    store = EventStore(tmp_path / "t.db")
    now = time.time()
    store.upsert_session(_session_obj("sess-dup", created_at=now - 10, goal="第一次"))
    store.upsert_session(_session_obj("sess-dup", created_at=now - 10, goal="第二次", events=5))

    rows = store.sessions()
    assert len(rows) == 1, f"同一 fingerprint 必须只有一行（主键约束），实际 {len(rows)} 行"
    assert rows[0]["events"] == 5, "重复登记要覆盖计数，不能累加出双份"


# ---------------------------------------------------------------------------
# 时间线
# ---------------------------------------------------------------------------


def test_timeline_folds_noise_but_keeps_nodes(tmp_path):
    store = EventStore(tmp_path / "t.db")
    now = time.time()
    store.add_event(kind="precheck", verdict="allow", summary="帮我写个排序", ts=now)
    for index in range(5):
        store.add_event(kind="line", summary=f"读到 {index}", ts=now + 1 + index)
    store.add_event(
        kind="precheck",
        severity="red_line",
        verdict="dangerous",
        rule_id="catastrophic_root_delete",
        summary="rm -rf /",
        ts=now + 10,
    )

    items = build_timeline(store)
    types = [item["type"] for item in items]
    assert types == ["node", "fold", "node"]
    assert items[1]["count"] == 5
    # 红线必须是 critical 级别，否则渲染时会被当成普通节点
    assert items[2]["level"] == "critical"


def test_timeline_keeps_short_background_as_lines(tmp_path):
    store = EventStore(tmp_path / "t.db")
    now = time.time()
    store.add_event(kind="precheck", verdict="allow", summary="开始", ts=now)
    store.add_event(kind="line", summary="只有一行噪声", ts=now + 1)

    items = build_timeline(store)
    assert [item["type"] for item in items] == ["node", "line"]
    assert items[1]["text"] == "只有一行噪声"


def test_timeline_rendering_marks_severity(tmp_path):
    store = EventStore(tmp_path / "t.db")
    now = time.time()
    store.add_event(
        kind="precheck",
        severity="red_line",
        verdict="dangerous",
        rule_id="catastrophic_root_delete",
        summary="rm -rf /",
        ts=now,
    )
    lines = render_timeline_lines(build_timeline(store))
    assert len(lines) == 1
    assert "!!" in lines[0]
    assert "catastrophic_root_delete" in lines[0]


def test_timeline_marks_blocked_as_blocked(tmp_path):
    """拦截必须渲染成「拦截」。

    这里曾经踩过：``verdict`` 存的是 rules 里的大写常量 ``DANGEROUS``，
    而渲染层拿小写 ``"dangerous"`` 比较，永远不相等 —— 于是每一条真拦截
    都在时间线上写成「派发前放行」。这种错不报任何异常，只是安静地骗人。
    """
    store = EventStore(tmp_path / "t.db")
    store.add_event(
        kind="precheck",
        severity="red_line",
        verdict="DANGEROUS",
        rule_id="catastrophic.root_delete",
        summary="rm -rf /",
    )
    lines = render_timeline_lines(build_timeline(store))
    assert "拦截" in lines[0]
    assert "放行" not in lines[0]


def test_timeline_empty_renders_placeholder(tmp_path):
    assert render_timeline(build_timeline(EventStore(tmp_path / "t.db"))) == "（这段时间没有记录）"


def test_timeline_truncates_long_output(tmp_path):
    store = EventStore(tmp_path / "t.db")
    now = time.time()
    for index in range(40):
        store.add_event(
            kind="precheck",
            severity="standard",
            verdict="dangerous",
            summary=f"事件 {index}",
            ts=now + index,
        )
    text = render_timeline(build_timeline(store), limit=10)
    assert "省略" in text
    assert len(text.splitlines()) == 10


# ---------------------------------------------------------------------------
# Token 审计
# ---------------------------------------------------------------------------


def test_estimate_tokens_handles_cjk_and_ascii():
    assert estimate_tokens("") == 0
    # 纯中文：一字一 token（「帮我写一个快速排序函数」共 11 字）
    assert estimate_tokens("帮我写一个快速排序函数") == 11
    # 纯英文：4 字符一 token（24 非空字符 → 6）
    assert estimate_tokens("abcdefghijklmnopqrstuvwx") == 6


def test_token_ledger_accumulates(tmp_path):
    store = EventStore(tmp_path / "t.db")
    ledger = TokenLedger(store)
    ledger.record(prompt="帮我写一个快速排序函数", output="好的")
    ledger.record(prompt="再改一下", output="改好了")

    summary = ledger.summary()
    assert summary["estimated"] is True
    assert summary["total_tokens"] == summary["input_tokens"] + summary["output_tokens"]
    assert summary["calls"] == 2


def test_token_ledger_prefers_exact_numbers(tmp_path):
    ledger = TokenLedger(EventStore(tmp_path / "t.db"))
    result = ledger.record(prompt="很长的文本" * 100, input_tokens=42, output_tokens=7)
    assert result == {"input": 42, "output": 7, "saved": 0}


def test_token_ledger_records_blocked_savings(tmp_path):
    store = EventStore(tmp_path / "t.db")
    ledger = TokenLedger(store)
    saved = ledger.record_blocked(task="rm -rf / --no-preserve-root")
    assert saved > 0
    assert store.stats()["saved_tokens"] == saved


def test_token_summary_from_store_since(tmp_path):
    store = EventStore(tmp_path / "t.db")
    ledger = TokenLedger(store)
    ledger.record(input_tokens=10, output_tokens=5, saved_tokens=3)
    summary = ledger.summary(since=time.time() - 60)
    assert summary["input_tokens"] == 10
    assert summary["saved_tokens"] == 3


def test_render_usage_marks_estimate():
    text = render_usage({"total_tokens": 1500, "input_tokens": 1000, "output_tokens": 500, "saved_tokens": 0})
    assert "1.5k" in text
    assert "估算值" in text
    assert render_usage({}) == "Token：无记录"
