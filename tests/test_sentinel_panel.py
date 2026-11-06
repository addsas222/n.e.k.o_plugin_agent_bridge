"""面板数据聚合层（``bridge.sentinel_core.panel``）的规格测试。

为什么这一层要单独存在
======================

面板要把哨兵的「会话 / 告警 / 时间线」展示出来，聚合逻辑**不能**写在
``__init__.py`` 的 ``_ui_panel_state`` 里：本仓库的测试从不实例化插件类
（见 ``tests/_sdk_stub.py`` 与 ``tests/conftest.py``），写在那儿就永远测不到。
抽成纯函数后，面板的每一种状态都能被钉死——尤其是「空表格」的三种成因
（没挂上 / 关了 / 没账本）必须彼此可区分，否则面板会把「没在看」渲染成
「一切正常」，那是审计工具最不该犯的错。

测试纪律
========

* 时钟一律注入 ``now``，不依赖 ``time.time()``，否则用例会在跨窗口的边界上偶发红；
* 造数据只走 ``EventStore`` 的公开方法，不碰 SQL；
* 断言「行为契约」（返回什么、怎么排序、降级不抛），不断言内部函数名与私有字段。
"""

from __future__ import annotations

import pytest

# isort: split
# bridge 在插件仓库内是一方包、在 CI 的检出布局下会被当成三方包；
# 显式切段，两种 cwd 下的 I001 判定就都稳定了。

from bridge.sentinel_core.panel import build_panel_state
from bridge.sentinel_core.store import EventStore

#: 固定时钟：2023-11-14 前后。所有造出来的数据都相对它偏移，用例因此完全确定。
NOW = 1_700_000_000.0
HOUR = 3600.0

#: 契约要求三张表里每一行都必须有的键。
#: 用 ``>=`` 断言（不是 ``==``）：实现可以多给字段（比如 ``session_id`` / ``pid``）。
SESSION_KEYS = frozenset(
    {"fingerprint", "agent", "cwd", "goal", "started_at", "ended_at", "exit_code", "events", "alerts", "active"}
)
ALERT_KEYS = frozenset({"ts", "clock", "level", "title", "body", "delivered", "channel"})
TIMELINE_KEYS = frozenset({"ts", "clock", "kind", "severity", "level", "text", "fingerprint"})


class _SessionObj:
    """给 ``EventStore.upsert_session`` 用的会话对象。

    它用 ``getattr`` 取字段，所以**必须传对象不传 dict**：传 dict 的话所有字段都会
    取到默认空值，而且空指纹会撞主键，五路会话会被静默合并成一行。
    """

    def __init__(
        self,
        fingerprint: str,
        *,
        created_at: float,
        agent: str = "codebuddy",
        cwd: str = "D:/work/demo",
        goal: str = "",
        events: int = 0,
        alerts: int = 0,
    ) -> None:
        self.fingerprint = fingerprint
        self.session_id = fingerprint
        self.cwd = cwd
        self.pid = 1234
        self.agent = agent
        self.goal = goal
        self.events = events
        self.alerts = alerts
        self.created_at = created_at


class _FakeSentinel:
    """鸭子类型的 ``SentinelRuntime``。

    契约只允许面板读两样东西：``enabled`` 属性与 ``doctor()``。这个假对象就只实现
    这两样——面板若去摸私有字段（``_dropped`` / ``_parse_failures``），这里会立刻
    暴露成默认值不符，而不是悄悄通过。
    """

    def __init__(self, *, enabled: bool = True, doctor: dict | None = None) -> None:
        self.enabled = enabled
        self._doctor = doctor

    def doctor(self) -> dict:
        if self._doctor is not None:
            return self._doctor
        # 形状必须与 ``SentinelRuntime.doctor()`` 一致：``guard.policy`` 是
        # ``ProcessGuard.identity_policy()`` 的**返回值本身**，也就是一个 dict
        # （``{"mode", "window_seconds", "platform"}``），不是字符串。
        # 这里曾经图省事写成 ``"policy": "strict"``，于是面板把 dict 喂给
        # ``str()`` 得到 ``"{'mode': 'creation_time', ...}"`` 这种 Python repr
        # 直接显示在「身份档位」上——假对象的保真度不够，真 bug 就藏过去了。
        return {
            "enabled": self.enabled,
            "store": {"path": "D:/data/sentinel.db", "ok": True},
            "l2": {"enabled": True},
            "guard": {
                "enabled": True,
                "policy": {"mode": "creation_time", "window_seconds": 7200.0, "platform": "win32"},
            },
            "safepoint": {"enabled": False},
            "webhook": {"configured": True},
            "timebox": {"seconds": 90.0},
            "dropped_events": 3,
            "parse": {"failures": 2},
        }


def _store(tmp_path, name: str = "panel.db") -> EventStore:
    """建一个独立账本，避免用例之间互相看见对方的行。"""
    return EventStore(tmp_path / name)


def _seed_sessions(store: EventStore, count: int, *, base: float = NOW - 600.0, step: float = 60.0) -> None:
    """插入 ``count`` 路会话，时间从旧到新，指纹 ``sess-0`` … ``sess-{count-1}``。"""
    for index in range(count):
        store.upsert_session(
            _SessionObj(
                f"sess-{index}",
                created_at=base + index * step,
                agent=f"agent-{index}",
                goal=f"任务 {index}",
                events=index,
                alerts=index,
            )
        )


# ---------------------------------------------------------------------------
# 1. 四种 state：每种都要说清「现在是什么情况、该怎么修」
# ---------------------------------------------------------------------------


def test_state_runtime_missing_when_sentinel_is_none(tmp_path):
    state = build_panel_state(_store(tmp_path), sentinel=None, now=NOW)
    sentinel = state["sentinel"]
    assert sentinel["state"] == "runtime_missing", "哨兵运行时根本没起来，这时候报 ok 等于骗人"
    assert sentinel["enabled"] is False, "运行时缺失时 enabled 必须是明确的 False（不是 None 也不是缺省）"
    assert "启动" in sentinel["fix_hint"], (
        f"fix_hint 必须指到该态的修法（去查插件日志里的启动失败），实际是 {sentinel['fix_hint']!r}"
    )


def test_state_disabled_when_sentinel_switched_off(tmp_path):
    state = build_panel_state(_store(tmp_path), sentinel=_FakeSentinel(enabled=False), now=NOW)
    sentinel = state["sentinel"]
    assert sentinel["state"] == "disabled", "配置里关掉哨兵是「停了」，不是「一切正常」"
    assert sentinel["enabled"] is False
    assert "[bridge.sentinel]" in sentinel["fix_hint"], (
        f"disabled 的修法必须指到配置节 [bridge.sentinel]，实际是 {sentinel['fix_hint']!r}"
    )


def test_state_no_store_when_store_is_none(tmp_path):
    state = build_panel_state(None, sentinel=_FakeSentinel(), now=NOW)
    sentinel = state["sentinel"]
    assert sentinel["state"] == "no_store", "没有账本就没有历史可展示，不能报 ok"
    assert sentinel["store_ok"] is False, "store 为 None 时 store_ok 必须是 False"
    hint = sentinel["fix_hint"]
    assert "账本" in hint or "store_path" in hint, f"no_store 的修法必须指到账本/store_path，实际是 {hint!r}"


def test_state_no_store_when_store_disabled(tmp_path):
    store = _store(tmp_path)
    store.enabled = False  # 建表失败或路径不可写时 EventStore 就是这么降级的
    state = build_panel_state(store, sentinel=_FakeSentinel(), now=NOW)
    sentinel = state["sentinel"]
    assert sentinel["state"] == "no_store", "账本自己 enabled=False（一个字节都没落盘）也是 no_store"
    assert sentinel["store_ok"] is False
    hint = sentinel["fix_hint"]
    assert "账本" in hint or "store_path" in hint, f"no_store 的修法必须指到账本/store_path，实际是 {hint!r}"


def test_state_ok_when_runtime_and_store_wired(tmp_path):
    store = _store(tmp_path)
    state = build_panel_state(store, sentinel=_FakeSentinel(), now=NOW)
    sentinel = state["sentinel"]
    assert sentinel["state"] == "ok"
    assert sentinel["fix_hint"] == "", "一切正常时不该给修法提示，否则用户以为要动手"
    assert sentinel["enabled"] is True
    assert sentinel["store_ok"] is True, "账本可用时 store_ok 必须是 True"
    # doctor() 的嵌套字典要摊平成面板能直接渲染的标量
    assert sentinel["l2"] is True, "l2 必须是 bool，面板拿它画能力位"
    assert sentinel["safepoint"] is False
    assert sentinel["webhook"] is True
    # ``guard`` 是**干预动作**（alert 只告警 / terminate 真杀），由调用方从配置
    # ``sentinel.guard_action`` 注入——``doctor()["guard"]`` 里没有这个值，那里面是
    # ``enabled`` 与 ``policy``。这条只锁类型，不锁取值（默认 ""=未知，见实现注释）。
    assert isinstance(sentinel["guard"], str), (
        f"guard 必须是 str（干预动作 alert/terminate），实际是 {type(sentinel['guard'])}"
    )
    assert sentinel["timebox_seconds"] == 90.0, "timebox 秒数要透传 doctor() 的值"
    assert sentinel["dropped_events"] == 3, "丢了多少事件是「降级发生过」的唯一证据，必须透传"
    assert sentinel["parse_failures"] == 2, "解析失败计数同上，丢了就再也查不出来"


# ---------------------------------------------------------------------------
# 2. 三种「空」必须可区分 —— 这是本模块存在的理由
# ---------------------------------------------------------------------------


def test_empty_ledger_is_ok_state_with_empty_lists(tmp_path):
    state = build_panel_state(_store(tmp_path), sentinel=_FakeSentinel(), now=NOW)
    assert state["sentinel"]["state"] == "ok", "账本开着只是还没数据，这是 ok 不是故障"
    assert state["sessions"] == [], "没数据就是空列表"
    assert state["alerts"] == []
    assert state["timeline"] == []


def test_three_empty_looking_cases_have_distinct_states(tmp_path):
    """三种「面板上什么都没有」的成因不能塌成同一个 state。"""
    seeded = _store(tmp_path, "seeded.db")
    _seed_sessions(seeded, 1)
    seeded.add_alert(level="warn", title="已有告警", ts=NOW - 60)

    disabled = build_panel_state(seeded, sentinel=_FakeSentinel(enabled=False), now=NOW)
    no_store = build_panel_state(None, sentinel=_FakeSentinel(), now=NOW)
    empty_ok = build_panel_state(_store(tmp_path, "fresh.db"), sentinel=_FakeSentinel(), now=NOW)

    for name, state in (("disabled", disabled), ("no_store", no_store), ("ok", empty_ok)):
        assert state["sessions"] == [], f"{name} 场景下 sessions 必须是空列表"
        assert state["alerts"] == [], f"{name} 场景下 alerts 必须是空列表"
        assert state["timeline"] == [], f"{name} 场景下 timeline 必须是空列表"

    states = [disabled["sentinel"]["state"], no_store["sentinel"]["state"], empty_ok["sentinel"]["state"]]
    assert len(set(states)) == 3, f"三种空必须能区分，不能都塌成同一种 state：{states}"
    assert states == ["disabled", "no_store", "ok"], f"state 取值必须与契约一致：{states}"


# ---------------------------------------------------------------------------
# 3. 时间窗
# ---------------------------------------------------------------------------


def test_window_excludes_event_outside_range(tmp_path):
    store = _store(tmp_path)
    store.add_event(kind="line", fingerprint="fp-in", summary="窗内事件", ts=NOW - 600)
    store.add_event(kind="line", fingerprint="fp-out", summary="窗外事件", ts=NOW - 13 * HOUR)

    state = build_panel_state(store, sentinel=_FakeSentinel(), hours=12.0, now=NOW)

    fingerprints = {row["fingerprint"] for row in state["timeline"]}
    assert fingerprints == {"fp-in"}, "时间窗外的事件不该出现在面板时间线上"
    assert state["window"]["hours"] == 12.0
    assert state["window"]["since"] == NOW - 12 * HOUR, "since 必须是 now - hours*3600"
    assert state["window"]["until"] >= NOW, f"until 不该早于注入的 now，实际是 {state['window']['until']}"
    assert state["stats"]["events"] == 1, "stats 也必须只算窗口内的，否则面板会把上一周的事算进今晚"


# ---------------------------------------------------------------------------
# 4. 三个 limit 各自生效（不能互相顶掉，也不能被忽略）
# ---------------------------------------------------------------------------


def test_sessions_limit_caps_rows(tmp_path):
    store = _store(tmp_path)
    _seed_sessions(store, 5)
    state = build_panel_state(store, sentinel=_FakeSentinel(), sessions_limit=2, now=NOW)
    returned = [row["fingerprint"] for row in state["sessions"]]
    assert len(returned) == 2, f"sessions_limit=2 必须只回 2 条，实际 {returned}"
    assert returned == ["sess-4", "sess-3"], f"截断要保留最近的几条（倒序在前），实际 {returned}"


def test_alerts_limit_caps_rows(tmp_path):
    store = _store(tmp_path)
    for index in range(5):
        store.add_alert(level="warn", title=f"告警 {index}", ts=NOW - 300 + index * 10)
    state = build_panel_state(store, sentinel=_FakeSentinel(), alerts_limit=3, now=NOW)
    titles = [row["title"] for row in state["alerts"]]
    assert len(titles) == 3, f"alerts_limit=3 必须只回 3 条，实际 {titles}"
    assert titles == ["告警 4", "告警 3", "告警 2"], f"截断要保留最新的几条，实际 {titles}"


def test_timeline_limit_caps_rows(tmp_path):
    store = _store(tmp_path)
    for index in range(5):
        store.add_event(kind="line", summary=f"事件 {index}", ts=NOW - 300 + index * 10)
    state = build_panel_state(store, sentinel=_FakeSentinel(), timeline_limit=2, now=NOW)
    texts = [row["text"] for row in state["timeline"]]
    assert len(texts) == 2, f"timeline_limit=2 必须只回 2 条，实际 {texts}"


# ---------------------------------------------------------------------------
# 5. 排序：会话倒序、告警倒序、时间线正序
# ---------------------------------------------------------------------------


def test_sessions_sorted_by_started_at_desc(tmp_path):
    store = _store(tmp_path)
    # 故意乱序插入：按插入顺序返回就会红
    for fingerprint, offset in (("sess-0", 300.0), ("sess-1", 100.0), ("sess-2", 200.0)):
        store.upsert_session(_SessionObj(fingerprint, created_at=NOW - offset))

    state = build_panel_state(store, sentinel=_FakeSentinel(), now=NOW)
    started = [row["started_at"] for row in state["sessions"]]
    assert started == sorted(started, reverse=True), f"会话必须按 started_at 倒序（最近的在上）：{started}"
    assert [row["fingerprint"] for row in state["sessions"]] == ["sess-1", "sess-2", "sess-0"]


def test_alerts_sorted_newest_first(tmp_path):
    store = _store(tmp_path)
    for index, offset in enumerate((300.0, 100.0, 200.0)):
        store.add_alert(level="warn", title=f"告警 {index}", ts=NOW - offset)

    state = build_panel_state(store, sentinel=_FakeSentinel(), now=NOW)
    stamps = [row["ts"] for row in state["alerts"]]
    assert stamps == sorted(stamps, reverse=True), f"告警必须时间倒序（最近的发生最可能还留着现场）：{stamps}"


def test_timeline_sorted_oldest_first(tmp_path):
    store = _store(tmp_path)
    for index, offset in enumerate((300.0, 100.0, 200.0)):
        store.add_event(kind="line", summary=f"事件 {index}", ts=NOW - offset)

    state = build_panel_state(store, sentinel=_FakeSentinel(), now=NOW)
    stamps = [row["ts"] for row in state["timeline"]]
    assert stamps == sorted(stamps), f"时间线必须按时间正序（事故是顺着读的）：{stamps}"


# ---------------------------------------------------------------------------
# 6. active：ended_at 为空就是在跑
# ---------------------------------------------------------------------------


def test_session_active_flag_follows_ended_at(tmp_path):
    store = _store(tmp_path)
    store.upsert_session(_SessionObj("still-running", created_at=NOW - 300))
    store.upsert_session(_SessionObj("finished", created_at=NOW - 200))
    store.close_session("finished", exit_code=0)

    rows = {row["fingerprint"]: row for row in build_panel_state(store, sentinel=_FakeSentinel(), now=NOW)["sessions"]}
    assert rows["still-running"]["active"] is True, "ended_at 为空的会话必须标记为 active=True"
    assert rows["finished"]["active"] is False, "已收尾的会话不能还挂着「进行中」"
    assert rows["finished"]["exit_code"] == 0, "退出码要透传，面板靠它区分「正常结束」和「崩了」"


# ---------------------------------------------------------------------------
# 7. 字段形状：面板直接按 key 取用，缺一个就是渲染时 KeyError
# ---------------------------------------------------------------------------


def test_session_rows_have_contracted_keys(tmp_path):
    store = _store(tmp_path)
    _seed_sessions(store, 1)
    rows = build_panel_state(store, sentinel=_FakeSentinel(), now=NOW)["sessions"]
    assert rows, "这里有 1 路会话，sessions 不该是空的"
    missing = SESSION_KEYS - set(rows[0])
    assert not missing, f"会话行缺少面板要用的键：{sorted(missing)}（实际键：{sorted(rows[0])}）"


def test_alert_rows_have_contracted_keys(tmp_path):
    store = _store(tmp_path)
    alert_id = store.add_alert(level="critical", title="红线拦截", body="rm -rf /", ts=NOW - 60)
    store.mark_delivered(alert_id, channel="local")

    rows = build_panel_state(store, sentinel=_FakeSentinel(), now=NOW)["alerts"]
    assert rows, "这里有 1 条告警，alerts 不该是空的"
    missing = ALERT_KEYS - set(rows[0])
    assert not missing, f"告警行缺少面板要用的键：{sorted(missing)}（实际键：{sorted(rows[0])}）"
    assert bool(rows[0]["delivered"]) is True, "已投递的告警不能被面板显示成「还在队列里」"
    assert rows[0]["channel"] == "local", f"投递通道要透传，实际是 {rows[0]['channel']!r}"


def test_timeline_rows_have_contracted_keys(tmp_path):
    store = _store(tmp_path)
    store.add_event(kind="precheck", severity="red_line", verdict="DANGEROUS", summary="rm -rf /", ts=NOW - 60)
    rows = build_panel_state(store, sentinel=_FakeSentinel(), now=NOW)["timeline"]
    assert rows, "这里有 1 条事件，timeline 不该是空的"
    missing = TIMELINE_KEYS - set(rows[0])
    assert not missing, f"时间线行缺少面板要用的键：{sorted(missing)}（实际键：{sorted(rows[0])}）"
    assert isinstance(rows[0]["text"], str) and rows[0]["text"], "text 是时间线唯一给人看的那句话，不能是空串"
    assert isinstance(rows[0]["clock"], str) and rows[0]["clock"], "clock 是面板上的时刻标签，不能是空串"


# ---------------------------------------------------------------------------
# 8. 降级：账本读不出来也不能让面板跟着崩
# ---------------------------------------------------------------------------


def test_degrades_when_store_stats_raises(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _seed_sessions(store, 2)

    def _boom(*args, **kwargs):
        raise RuntimeError("账本查询炸了")

    monkeypatch.setattr(store, "stats", _boom)

    try:
        state = build_panel_state(store, sentinel=_FakeSentinel(), now=NOW)
    except Exception as exc:  # noqa: BLE001 —— 这里就是要证明「不抛」
        pytest.fail(f"store.stats 抛错时面板必须降级返回，而不是把异常抛给 UI：{exc!r}")

    assert isinstance(state, dict), "降级路径也必须返回完整的 dict"
    assert state["sentinel"]["state"] == "ok", "统计读不出来不代表哨兵没在跑：state 不该被拖成故障态"
    stats = state["stats"]
    for key in ("sessions", "events", "alerts", "blocked", "by_severity", "by_level"):
        assert key in stats, f"stats 缺少键 {key}（降级时也要给零值，面板才不用做 None 判断）"
    assert isinstance(stats["by_severity"], dict) and isinstance(stats["by_level"], dict)


def test_bare_object_sentinel_does_not_crash(tmp_path):
    """宿主传进来的东西可能既没有 ``enabled`` 也没有 ``doctor``。"""
    try:
        state = build_panel_state(_store(tmp_path), sentinel=object(), now=NOW)
    except Exception as exc:  # noqa: BLE001 —— 这里就是要证明「不抛」
        pytest.fail(f"哨兵对象缺属性时面板不该崩（用 getattr 兜默认值）：{exc!r}")

    assert isinstance(state, dict)
    # 不锁死具体值：只看「没崩」和「落在合理的两档之一」，锁死就成了测实现细节。
    assert state["sentinel"]["state"] in {"ok", "no_store"}, (
        f"裸对象哨兵的 state 应落在 ok/no_store 之一，实际 {state['sentinel']['state']!r}"
    )


# ---------------------------------------------------------------------------
# 9. plugin / entries 透传
# ---------------------------------------------------------------------------


def test_plugin_and_entries_are_passed_through(tmp_path):
    plugin = {"id": "agent_bridge", "name": "Agent 桥接", "version": "9.9.9", "extra": {"nested": 1}}
    entries = [{"id": "agent_run", "name": "派发任务", "has_params": True, "timeout": 1800.0}]

    state = build_panel_state(_store(tmp_path), sentinel=_FakeSentinel(), plugin=plugin, entries=entries, now=NOW)
    assert state["plugin"] == plugin, "plugin 是宿主的元信息，面板要原样显示，不能被改写"
    assert state["entries"] == entries, "entries 是入口清单，原样透传（多一层包装面板就读不到了）"


def test_defaults_when_plugin_and_entries_omitted(tmp_path):
    state = build_panel_state(_store(tmp_path), sentinel=_FakeSentinel(), now=NOW)
    plugin = state["plugin"]
    assert isinstance(plugin, dict), f"plugin 缺省必须是 dict，实际是 {type(plugin)}"
    assert "id" in plugin and "name" in plugin, f"缺省 plugin 至少要有 id/name，实际键：{sorted(plugin)}"
    assert plugin["id"] == "agent_bridge", f"缺省 plugin.id 必须是 agent_bridge，实际 {plugin['id']!r}"
    assert "Agent" in str(plugin["name"]), f"缺省 plugin.name 要能看出这是 Agent 桥接，实际 {plugin['name']!r}"
    assert isinstance(state["entries"], list), f"entries 缺省必须是 list（面板直接 map），实际 {type(state['entries'])}"
    assert state["entries"] == [], "没传 entries 时给空列表，而不是 None"


# ---------------------------------------------------------------------------
# 10. guard_action 透传：别把「干预动作」和「身份档位」混成一个字段
# ---------------------------------------------------------------------------


def test_guard_action_is_passed_through_not_confused_with_policy(tmp_path):
    """``guard`` 是**干预动作**（会不会真杀进程），不是身份档位。

    ``doctor()["guard"]`` 里只有 ``enabled`` / ``policy``，动作来自调用方注入的配置
    ``sentinel.guard_action``（``doctor()`` 里根本没有这个值）。两者混掉的话，面板会把
    「身份档位 creation_time」显示成「命中红线会做什么」，用户就再也回答不了那个真正
    的问题：它到底会不会动手。
    """
    store = _store(tmp_path)
    for action in ("terminate", "alert"):
        sentinel = build_panel_state(store, sentinel=_FakeSentinel(), now=NOW, guard_action=action)["sentinel"]
        assert sentinel["guard"] == action, f"guard 必须原样透传 guard_action={action!r}，实际 {sentinel['guard']!r}"
        assert sentinel["guard"] != "creation_time", "guard 不能填 doctor 里的身份档位（那是 guard_policy 的事）"

    # 没注入配置时不许猜一个动作出来：诚实地说「不知道」比假装「只告警」安全得多。
    default = build_panel_state(store, sentinel=_FakeSentinel(), now=NOW)["sentinel"]
    assert isinstance(default["guard"], str), f"未注入时 guard 仍必须是 str，实际 {type(default['guard'])}"
    assert default["guard"] not in {"alert", "terminate"}, (
        f"没传 guard_action 时不能凭空给出 alert/terminate，实际 {default['guard']!r}"
    )


def test_guard_policy_flattens_identity_mode_not_dict_repr(tmp_path):
    """``guard_policy`` 必须是档位**名字**，不是 ``identity_policy()`` 的 repr。

    ``ProcessGuard.identity_policy()`` 返回的是 dict（``{"mode", "window_seconds",
    "platform"}``），``runtime.doctor()`` 把它原样塞进 ``doctor()["guard"]["policy"]``。
    面板只负责把它摊平成一句能显示的话——**一旦直接 ``str()`` 这个 dict**，面板上
    「身份档位」那一栏就会出现 ``{'mode': 'creation_time', ...}`` 这种 Python repr：
    排障第一眼要看的东西，变成一坨给开发者看的数据结构。
    """
    for mode in ("creation_time", "time_window", "none"):
        sentinel = _FakeSentinel(
            doctor={"guard": {"enabled": True, "policy": {"mode": mode, "window_seconds": 7200.0, "platform": "win32"}}}
        )
        block = build_panel_state(_store(tmp_path, f"{mode}.db"), sentinel=sentinel, now=NOW)["sentinel"]
        assert block["guard_policy"] == mode, (
            f"guard_policy 必须摊平成 identity_policy()['mode']（{mode!r}），实际 {block['guard_policy']!r}"
        )
        assert "{" not in block["guard_policy"], (
            f"guard_policy 里出现了 dict 的 repr（面板会把 Python 结构直接显示给用户）：{block['guard_policy']!r}"
        )


def test_guard_policy_stays_empty_and_reasons_are_not_packed_into_it(tmp_path):
    """缺 ``policy`` 时 state 必须给空串，**不许自己编「未启用 / 读取失败 / 未知」**。

    ``guard`` 里没有 ``policy`` 有三种形状（见下表），它们的**原因各不相同**，
    而原因由前端拿 ``guard_enabled`` 与空串一起拼出来展示。一旦把原因塞进
    ``guard_policy``（第四个值 ``"disabled"`` / ``"error"``），就是「一个字段表达
    两件事」——那正是这个字段拆成 ``guard`` / ``guard_enabled`` / ``guard_policy``
    三个的原因。state 保持单义，翻译留给展示层。

    **可达性标注**（不标的话，下一个读的人会以为三种形状现在都会发生，去追一个
    不存在的 bug）：

    * ``{enabled: False}`` —— **现状可达**。``guard`` 未构造时 ``doctor()["guard"]``
      就是这个形状，面板今天就可能显示「干预层：关 + 身份档位：未知」。
    * ``{enabled: True, policy: {}}`` 与 ``{enabled: True}`` —— **结构性不可达**。
      ``runtime.doctor()`` 里 ``enabled`` 和 ``policy`` 挂在同一个
      ``if self.guard is not None`` 上；``identity_policy()`` 又是全域的（内部三层
      兜底把失败降级成合法档位，从不抛），而 ``mode`` 恒为三个非空串之一。
      这两行锁的是**未来退化**，不是现状缺陷。
    """

    cases = [
        ("干预层根本没起来（guard 未构造）—— 现状可达", {"enabled": False}, False),
        ("policy 存在但为空 dict —— 结构性不可达，锁未来退化", {"enabled": True, "policy": {}}, True),
        ("policy 键整个缺失 —— 结构性不可达，锁未来退化", {"enabled": True}, True),
    ]
    for index, (label, guard_block, expect_enabled) in enumerate(cases):
        sentinel = _FakeSentinel(doctor={"guard": guard_block})
        block = build_panel_state(_store(tmp_path, f"nopolicy-{index}.db"), sentinel=sentinel, now=NOW)["sentinel"]
        assert block["guard_policy"] == "", (
            f"{label}：guard_policy 必须是空串（原因由前端拼），实际 {block['guard_policy']!r}"
        )
        assert block["guard_enabled"] is expect_enabled, (
            f"{label}：guard_enabled 必须独立反映干预层状态（{expect_enabled}），实际 {block['guard_enabled']!r}"
        )
