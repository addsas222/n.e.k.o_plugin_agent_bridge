"""`ProcessGuard` 的进程身份校验测试（pid 复用防护 + 可逆性非对称）。

为什么单独测
============

只判「pid 还活着」不够：目标退出后 pid 会被系统复用，此时 `suspend` /
`terminate` 打中的是**无关进程**。防护靠「创建时间指纹」，它有几个最容易写错的
地方，本文件专门钉住：

1. `/proc/<pid>/stat` 的解析。第 2 字段 `comm` 允许含空格和括号，直接 `split(" ")`
   会让后面所有字段错位 —— 校验看起来在工作，实际比的是无关数字；
2. 拿不到指纹的平台 / 进程必须**明确降级**并在 `capabilities()` 里如实标注，
   不能假装安全；
3. 无法确认身份时**不能一个规则套三个动作**：`suspend` / `resume` 可逆（放行），
   `terminate` 不可逆（拒绝）。否则会出现「模块把 Agent 冻住、然后解不开也杀不掉」
   —— 这一条是本文件后半部分的重点，它测的是**行为**，不是能力位。

为什么断言行为而不只断言能力位
==============================

只断言 `capabilities()` 里报了什么，测不出「重复命中给自己续信任」「过期后拒绝
理由说错」这类副作用 —— 那些 bug 的能力位完全正常，功能看起来也在工作。所以
后半部分每条用例都断言「某个动作最后有没有真的送达」。
"""

from __future__ import annotations

import logging
import os
import signal
from types import SimpleNamespace

import pytest

# isort: split
# bridge 在插件仓库内是一方包、在 CI 的检出布局下会被当成三方包；
# 显式切段，两种 cwd 下的 I001 判定就都稳定了。

from bridge.sentinel_core import guard as guard_module
from bridge.sentinel_core.guard import ProcessGuard, _parse_starttime

#: 一个不会被当作自身进程的 pid（测试里 `_identity` 全被替身接管，不会真发信号）。
_FAKE_PID = 999_999_999
_LOGGER = "bridge.sentinel_core.guard"


def _stat_line(comm: str, starttime: str = "987654321") -> str:
    """按 `/proc/<pid>/stat` 的字段顺序拼一行，把 starttime 放在第 22 字段。"""
    # `) ` 之后的第一项是第 3 字段（state），所以第 22 字段是第 20 项（index 19）。
    rest = ["S", "1", "4242", "4242", "0", "-1", "4194560", "1"]
    rest += ["0"] * 11                      # 补到 index 18
    rest.append(starttime)                  # index 19 == 第 22 字段
    rest += ["0"] * 5                       # 后面还有若干字段，真实文件里也远不止
    return f"4242 ({comm}) " + " ".join(rest)


def _guard_with_clock(monkeypatch, mode: str = "creation_time", clock: dict | None = None):
    """造一个受控时钟 + 指定身份档次的 guard。

    :param monkeypatch: pytest 的 monkeypatch。
    :param mode: 直接写缓存（`identity_mode()` 会原样返回），免得依赖运行平台。
    :param clock: 复用同一个时钟；同一条用例里多个 guard 必须共用，否则后建的
        guard 会把模块级 `time` 换成新时钟，先前那个 guard 的窗口就「回退」了。
    :return: `(guard, clock)`，`clock["now"]` 就是当前单调时钟读数。
    """
    clock = clock if clock is not None else {"now": 1000.0}
    monkeypatch.setattr(
        guard_module, "time", SimpleNamespace(monotonic=lambda: clock["now"])
    )
    guard = ProcessGuard()
    guard._mode = mode
    return guard, clock


def _record_signals(monkeypatch):
    """把 POSIX 信号发送换成记录器，并强制走 POSIX 分支。

    Windows 上 `resume` 走的是 `NtResumeProcess`，拿真实 pid 去调必然失败，测不出
    「到底有没有尝试送达」。这里把平台判定压成 POSIX 并拦在信号出口上，
    于是断言的是「动作有没有被放行到发送信号那一步」—— 正是要测的那一层。

    :param monkeypatch: pytest 的 monkeypatch。
    :return: 收到信号的列表，元素为 `(pid, sig)`。
    """
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(guard_module, "_IS_WINDOWS", False)
    # Windows 的 `signal` 没有 SIGSTOP / SIGCONT（POSIX 语义），补上才能走到发送那一步。
    monkeypatch.setattr(signal, "SIGSTOP", getattr(signal, "SIGSTOP", 19), raising=False)
    monkeypatch.setattr(signal, "SIGCONT", getattr(signal, "SIGCONT", 18), raising=False)
    monkeypatch.setattr(
        ProcessGuard,
        "_posix_signal",
        staticmethod(lambda pid, sig, *, tree: sent.append((pid, sig))),
    )
    return sent


# ---------------------------------------------------------------------------
# 1. /proc/<pid>/stat 解析（含进程名带空格/括号的坑）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "comm",
    ["tmux: server", "a)b", "node (cluster) worker", "plain", "带 空格 的 进程"],
)
def test_parse_starttime_survives_comm_with_spaces_and_parens(comm):
    """进程名里的空格与括号不能让字段错位——必须从最后一个 `)` 之后切。"""
    assert _parse_starttime(_stat_line(comm)) == "987654321"


def test_parse_starttime_reads_the_22nd_field_not_a_neighbour():
    """把 starttime 换成能一眼认出的值，确认取的确实是第 22 字段。"""
    line = _stat_line("x", starttime="1729384756")
    assert _parse_starttime(line) == "1729384756"


@pytest.mark.parametrize("broken", ["", "no parens here", "4242 (x) S 1", "4242 (unterminated"])
def test_parse_starttime_returns_none_on_malformed(broken):
    assert _parse_starttime(broken) is None


# ---------------------------------------------------------------------------
# 2. pid 复用必须被拒绝（确知打错人时三项全拒）
# ---------------------------------------------------------------------------


def test_identity_mismatch_refuses_action(monkeypatch):
    """同 pid 但创建时间变了 = 已经是另一个进程，三个动作入口都必须拒绝。"""
    guard = ProcessGuard()
    current = {"value": "1000"}
    monkeypatch.setattr(guard, "_identity", lambda pid: ("starttime", current["value"]))

    assert guard._usable(_FAKE_PID, "suspend") == _FAKE_PID, "首次见到应记录并放行"
    assert guard._usable(_FAKE_PID, "suspend") == _FAKE_PID, "同一进程不该误拒"

    current["value"] = "2000"                 # pid 被系统复用给了别的进程
    assert guard._usable(_FAKE_PID, "suspend") is None, "指纹变了必须拒绝"
    assert guard.suspend(_FAKE_PID) is False
    assert guard.terminate(_FAKE_PID) is False
    # 连可逆的 resume 也拒：这不是「不确定」，是「确知这个 pid 已经属于别人」。
    assert guard.resume(_FAKE_PID) is False


def test_identity_match_allows_action(monkeypatch):
    """指纹一致时不能误拒（否则「挂起后询问」这条策略会永远失效）。"""
    guard = ProcessGuard()
    monkeypatch.setattr(guard, "_identity", lambda pid: ("creation", 1729384756))
    assert guard._usable(_FAKE_PID, "terminate") == _FAKE_PID
    assert guard._usable(_FAKE_PID, "terminate") == _FAKE_PID


def test_probe_failure_is_fail_open(monkeypatch):
    """这一刻取不到指纹时不许误拒：进程正在退出也会取不到。"""
    guard = ProcessGuard()
    replies = iter([("starttime", "1000"), ("seen", 0.0)])
    monkeypatch.setattr(guard, "_identity", lambda pid: next(replies))
    assert guard._usable(_FAKE_PID, "suspend") == _FAKE_PID
    assert guard._usable(_FAKE_PID, "suspend") == _FAKE_PID


def test_other_pid_still_refused_after_ten_identical_hits(monkeypatch):
    """同一个 pid 被连续命中十次，不能让**别的** pid 跟着沾光。

    这是「重复命中不能产生有利效果」的行为断言：如果哪天有人把续期逻辑写进
    `_usable` 的成功路径（「比对成功就刷新时刻」），窗口会变成活跃度检测，
    一个持续上报危险信号的 agent 就能不断给自己续信任。那时本条用例会红。
    """
    guard, clock = _guard_with_clock(monkeypatch, mode="time_window")
    monkeypatch.setattr(guard, "_identity", lambda pid: ("seen", clock["now"]))
    for _ in range(10):
        assert guard._usable(_FAKE_PID, "terminate") == _FAKE_PID

    clock["now"] += guard_module._IDENTITY_WINDOW_SECONDS + 1.0
    # 老 pid 的窗口已经过期（十次命中并没有给它续期）
    assert guard._usable(_FAKE_PID, "terminate") is None

    # 另一个 pid 是独立的记录，不该因为它被拒
    other = _FAKE_PID - 1
    assert guard._usable(other, "terminate") == other


# ---------------------------------------------------------------------------
# 3. 时间锚：任务级窗口、不续期、过期必须留痕
# ---------------------------------------------------------------------------


def test_time_window_expiry_refuses_terminate(monkeypatch):
    """时间锚的窗口是任务级的（远大于 dispatch 超时），过期后 terminate 被拒。"""
    guard, clock = _guard_with_clock(monkeypatch, mode="time_window")
    monkeypatch.setattr(guard, "_identity", lambda pid: ("seen", clock["now"]))

    assert guard._usable(_FAKE_PID, "terminate") == _FAKE_PID
    clock["now"] += 599.0                     # 还没到 default_timeout_seconds=600
    assert guard._usable(_FAKE_PID, "terminate") == _FAKE_PID
    clock["now"] += guard_module._IDENTITY_WINDOW_SECONDS
    assert guard._usable(_FAKE_PID, "terminate") is None


def test_window_covers_longest_allowed_dispatch_timeout():
    """窗口必须大于 `max_timeout_seconds`（3600）：否则长任务末段仍然失效。"""
    assert guard_module._IDENTITY_WINDOW_SECONDS > 3600.0


def test_expiry_is_logged_even_when_the_action_passes(monkeypatch, caplog):
    """resume 被放行时也要留下「窗口已过期」的痕迹。

    否则外部看到的只有「校验悄悄没了」：可逆动作永远不会被拒，日志里也就永远
    不会提到过期这件事。
    """
    guard, clock = _guard_with_clock(monkeypatch, mode="time_window")
    monkeypatch.setattr(guard, "_identity", lambda pid: ("seen", clock["now"]))
    _record_signals(monkeypatch)
    assert guard.resume(_FAKE_PID) is True

    clock["now"] += guard_module._IDENTITY_WINDOW_SECONDS + 1.0
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert guard._usable(_FAKE_PID, "resume") == _FAKE_PID, "可逆动作必须放行"
        assert guard._usable(_FAKE_PID, "resume") == _FAKE_PID, "第二次不该重复刷日志"

    expired = [r for r in caplog.records if "此后对该 pid 只能放行可逆动作" in r.getMessage()]
    assert len(expired) == 1, f"过期日志应恰好一条，实际 {len(expired)} 条"


def test_forget_frees_the_window_for_the_next_task(monkeypatch):
    """`forget()` 把记录清掉，下一轮重新登记 —— 窗口回到真正的任务范围。"""
    guard, clock = _guard_with_clock(monkeypatch, mode="time_window")
    monkeypatch.setattr(guard, "_identity", lambda pid: ("seen", clock["now"]))
    assert guard._usable(_FAKE_PID, "terminate") == _FAKE_PID

    clock["now"] += guard_module._IDENTITY_WINDOW_SECONDS + 1.0
    assert guard._usable(_FAKE_PID, "terminate") is None
    guard.forget(_FAKE_PID)
    assert guard._usable(_FAKE_PID, "terminate") == _FAKE_PID


# ---------------------------------------------------------------------------
# 4. 可逆性非对称（P0：不许把自己冻死又撤不回来）
# ---------------------------------------------------------------------------


def test_resume_survives_identity_check_timeout(monkeypatch):
    """窗口过期后 resume 仍必须放行，并且真的把 SIGCONT 发出去了。

    这条钉的是死锁链条：suspend 成功 → 窗口过期 → resume 被同一套校验拒 →
    进程冻着，改杀也杀不掉，只能等 dispatch 超时。断言到「信号真的发了」这一步，
    而不是只看 `_usable` 的返回值。
    """
    guard, clock = _guard_with_clock(monkeypatch, mode="time_window")
    monkeypatch.setattr(guard, "_identity", lambda pid: ("seen", clock["now"]))
    sent = _record_signals(monkeypatch)

    assert guard.suspend(_FAKE_PID) is True, "首次挂起应放行"
    assert sent == [(_FAKE_PID, signal.SIGSTOP)]

    clock["now"] += guard_module._IDENTITY_WINDOW_SECONDS + 70.0
    assert guard.resume(_FAKE_PID) is True, "过期后 resume 若被拒，进程就永远冻着"
    assert sent[-1] == (_FAKE_PID, signal.SIGCONT)


def test_suspend_also_passes_when_identity_is_unknown(monkeypatch):
    """suspend 同属可逆动作，同一套规则放行（SIGSTOP 之后还能 resume 回来）。"""
    guard, clock = _guard_with_clock(monkeypatch, mode="time_window")
    monkeypatch.setattr(guard, "_identity", lambda pid: ("seen", clock["now"]))
    sent = _record_signals(monkeypatch)

    assert guard.suspend(_FAKE_PID) is True
    clock["now"] += guard_module._IDENTITY_WINDOW_SECONDS + 1.0
    assert guard.suspend(_FAKE_PID) is True
    assert sent == [(_FAKE_PID, signal.SIGSTOP), (_FAKE_PID, signal.SIGSTOP)]


def test_terminate_still_refuses_when_identity_unknown(monkeypatch, caplog):
    """同一条件下 terminate 必须继续拒绝：不可逆的宁可不做。"""
    guard, clock = _guard_with_clock(monkeypatch, mode="time_window")
    monkeypatch.setattr(guard, "_identity", lambda pid: ("seen", clock["now"]))
    sent = _record_signals(monkeypatch)
    assert guard._usable(_FAKE_PID, "terminate") == _FAKE_PID

    clock["now"] += guard_module._IDENTITY_WINDOW_SECONDS + 1.0
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert guard.terminate(_FAKE_PID) is False
    assert sent == [], "拒绝就必须一个信号都不发"
    assert any("窗口已过期" in r.getMessage() for r in caplog.records)


def test_none_mode_passes_reversible_but_refuses_terminate(monkeypatch, caplog):
    """`identity_mode == "none"` 复用同一套规则，不写第二套分支。"""
    guard, clock = _guard_with_clock(monkeypatch, mode="none")
    sent = _record_signals(monkeypatch)

    assert guard.suspend(_FAKE_PID) is True
    assert guard.resume(_FAKE_PID) is True
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert guard.terminate(_FAKE_PID) is False
    assert sent == [(_FAKE_PID, signal.SIGSTOP), (_FAKE_PID, signal.SIGCONT)]


def test_three_refusal_wordings_are_distinct(monkeypatch, caplog):
    """三种拒绝原因的措辞必须互不相同，否则排查会走错方向。

    真的被复用（要查为什么）／窗口过期（该收尾了）／平台无能力（是环境限制，
    没得查）—— 混用一句话就会把人带到不存在的问题上。
    """
    reused, _ = _guard_with_clock(monkeypatch, mode="creation_time")
    monkeypatch.setattr(reused, "_identity", lambda pid: ("creation", 100))
    assert reused._usable(_FAKE_PID, "terminate") == _FAKE_PID
    monkeypatch.setattr(reused, "_identity", lambda pid: ("creation", 200))

    expired, clock = _guard_with_clock(monkeypatch, mode="time_window")
    monkeypatch.setattr(expired, "_identity", lambda pid: ("seen", clock["now"]))
    assert expired._usable(_FAKE_PID, "terminate") == _FAKE_PID
    clock["now"] += guard_module._IDENTITY_WINDOW_SECONDS + 1.0

    powerless, _ = _guard_with_clock(monkeypatch, mode="none", clock=clock)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert reused._usable(_FAKE_PID, "terminate") is None
        assert expired._usable(_FAKE_PID, "terminate") is None
        assert powerless._usable(_FAKE_PID, "terminate") is None

    messages = [r.getMessage() for r in caplog.records]
    reused_msg = next(m for m in messages if "复用" in m)
    expired_msg = next(m for m in messages if "窗口已过期" in m)
    none_msg = next(m for m in messages if "无法校验进程身份" in m)
    assert len({reused_msg, expired_msg, none_msg}) == 3
    assert "复用" not in none_msg, "平台没能力不是 pid 复用，说错会引错排查方向"


# ---------------------------------------------------------------------------
# 5. 指纹表：容量边界必须明确、优先淘汰死进程、不许牺牲已有保护
# ---------------------------------------------------------------------------


def test_identity_table_is_bounded(monkeypatch):
    """pid 复用会让指纹表只增不减，必须有上限，否则长跑进程会吃满内存。"""
    guard = ProcessGuard()
    monkeypatch.setattr(guard, "_identity", lambda pid: ("starttime", "1"))
    # 全部「还活着」→ 表满后不再接收新记录，条数不会超上限。
    monkeypatch.setattr(guard, "is_alive", lambda pid: True)
    for pid in range(1, guard_module._MAX_IDENTITY_ENTRIES + 50):
        guard._remember(pid)
    assert len(guard._identities) == guard_module._MAX_IDENTITY_ENTRIES


def test_table_full_does_not_evict_live_entries(monkeypatch, caplog):
    """表满且全是活进程时，**不能**挤掉已登记的记录来给新 pid 腾位置。

    挤掉一条活的 = 让「原本能识别的 pid」变成「无法识别」，净损失。正确行为是
    放弃登记新 pid（它的身份**从未被确认过**，因此只放行可逆动作，见下一条用例）
    并留一条 warning 承认校验不完整。
    """
    guard = ProcessGuard()
    monkeypatch.setattr(guard, "_identity", lambda pid: ("starttime", str(pid)))
    monkeypatch.setattr(guard, "is_alive", lambda pid: True)
    for pid in range(1, guard_module._MAX_IDENTITY_ENTRIES + 1):
        assert guard._remember(pid)[0] is True

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert guard._remember(424_242)[0] is False, "不该为新 pid 挤掉活着的记录"
        assert guard._remember(424_243)[0] is False

    assert 1 in guard._identities, "最老的活记录必须留着（它才是保护）"
    assert 424_242 not in guard._identities
    full = [r for r in caplog.records if "指纹表已满" in r.getMessage()]
    assert len(full) == 1, f"降级提示只该打一次，实际 {len(full)} 条"


def test_table_full_evicts_dead_entries_first(monkeypatch):
    """有死进程的记录时先淘汰它 —— 死 pid 不会再被动手，留着只占位。"""
    guard = ProcessGuard()
    monkeypatch.setattr(guard, "_identity", lambda pid: ("starttime", str(pid)))
    alive = {1: False}                        # pid 1 已经退出，其余都活着
    monkeypatch.setattr(guard, "is_alive", lambda pid: alive.get(pid, True))
    for pid in range(1, guard_module._MAX_IDENTITY_ENTRIES + 1):
        guard._remember(pid)

    assert guard._remember(424_242)[0] is True
    assert 1 not in guard._identities, "死进程的记录应优先被淘汰"
    assert 2 in guard._identities, "活着的记录不许动"
    assert 424_242 in guard._identities


def test_table_full_refuses_terminate_instead_of_trusting(monkeypatch, caplog):
    """表满且全是活进程时，未能登记的新 pid 归 `unknown`：terminate 拒、可逆放行。

    这是「登记不上 ≠ 比对通过」的**后果**断言。`_remember` 返回 False 只说明没登记
    成功，不代表身份比对通过；若这一支继续返回 `same`，指纹表一满就等于对任意新
    pid 关闭了身份校验 —— 容量边界变成能力悬崖。

    可逆动作必须仍然放行：那一档的代价只是「碰了无关进程一下」，而 fail-closed 的
    代价是冻住撤不回。
    """
    guard, _ = _guard_with_clock(monkeypatch, mode="creation_time")
    monkeypatch.setattr(guard, "_identity", lambda pid: ("creation", str(pid)))
    monkeypatch.setattr(guard, "is_alive", lambda pid: True)
    for pid in range(1, guard_module._MAX_IDENTITY_ENTRIES + 1):
        assert guard._remember(pid)[0] is True

    newcomer = 424_242
    sent = _record_signals(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        state, reason = guard._identity_state(newcomer)
        assert state == guard_module._IDENTITY_UNKNOWN, "登记不上不是比对通过"
        assert "指纹表已满" in reason, "措辞要指明是容量边界"
        assert "复用" not in reason, "表满不是 pid 复用，说错会引错排查方向"

        # 原始缺陷的形态不是「返回了错的值」，而是「返回了一个**永久**错的值」：
        # 第二次进来又走回 `_remember` → 仍然没登记上 → 仍然 same。所以这里必须连问两次。
        assert guard._identity_state(newcomer)[0] == guard_module._IDENTITY_UNKNOWN
        assert guard._usable(newcomer, "terminate") is None, "身份从未确认过，不可逆动作必须拒"
        assert guard._usable(newcomer, "terminate") is None, "第二次也必须是拒，不能只错一次"
        # 可逆动作要断言「真的送达了」，不只是 `_usable` 放行 —— 后者测不出送达层。
        assert guard.resume(newcomer) is True, "可逆动作仍放行，否则冻住撤不回"

    assert sent == [(newcomer, signal.SIGCONT)]
    assert any("terminate 会被拒绝" in r.getMessage() for r in caplog.records), (
        "降级提示必须说清后果，不能只说「退化为首次见到」"
    )


def test_table_full_reason_distinguishes_throttled_from_all_alive(monkeypatch):
    """「扫过全是活的」与「节流里没扫」必须分开写。

    60 秒扫描节流意味着表满之后的第二个新 pid 根本不会被扫描。这时断言「里面全是
    活进程」是**假话** —— 我们没看。原因说错会把排查引向「为什么全是活进程」，而
    真实答案可能只是「还没到下次扫描」。
    """
    guard, _ = _guard_with_clock(monkeypatch, mode="creation_time")
    monkeypatch.setattr(guard, "_identity", lambda pid: ("creation", str(pid)))
    monkeypatch.setattr(guard, "is_alive", lambda pid: True)
    for pid in range(1, guard_module._MAX_IDENTITY_ENTRIES + 1):
        guard._remember(pid)

    ok, why = guard._remember(424_242)
    assert (ok, why) == (False, guard_module._TABLE_FULL), "第一次会真的扫描一遍"

    # 同一个时钟刻度上再来一个：距上次扫描 0s < 60s，这次直接放弃扫描。
    ok, why = guard._remember(424_243)
    assert (ok, why) == (False, guard_module._SCAN_THROTTLED)

    state, reason = guard._identity_state(424_243)
    assert state == guard_module._IDENTITY_UNKNOWN
    assert "本次未扫描" in reason, "没扫就不能说「全在运行」"
    assert "全为活进程" not in reason


def test_eviction_logs_both_pids_and_does_not_blame_the_caller(monkeypatch, caplog):
    """淘汰的是**别人**时，日志必须同时写出被淘汰的 pid 和调用方的 pid。

    只写「表已满」会让人以为提示在说调用方这个 pid，排查方向直接跑偏。反过来，
    调用方明明登记成功了，就不该出现「无法登记」这四个字。
    """
    guard, _ = _guard_with_clock(monkeypatch, mode="creation_time")
    monkeypatch.setattr(guard, "_identity", lambda pid: ("creation", str(pid)))
    alive = {1: False}                        # 只有 pid 1 死了，可以拿来腾位置
    monkeypatch.setattr(guard, "is_alive", lambda pid: alive.get(pid, True))
    for pid in range(1, guard_module._MAX_IDENTITY_ENTRIES + 1):
        guard._remember(pid)

    newcomer = 424_242
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        ok, why = guard._remember(newcomer)

    assert (ok, why) == (True, guard_module._REGISTERED)
    assert newcomer in guard._identities
    evicted = [r.getMessage() for r in caplog.records if "淘汰已退出的 pid=1 " in r.getMessage()]
    assert evicted, "淘汰日志必须写明被淘汰的是谁"
    assert f"pid={newcomer}" in evicted[0], "被淘汰的与正要登记的两个 pid 都要写出来"
    assert not [r for r in caplog.records if "无法登记" in r.getMessage()], (
        "调用方已经登记上了，不能报「无法登记」"
    )


def test_remember_rechecks_capacity_after_probing(monkeypatch):
    """取指纹期间表被别的线程占满时，不能再往表里写（上限不是「大致不超过」）。

    `_identity()` 是一次系统调用（读 procfs / `OpenProcess`），期间别的线程完全可能
    把最后几个位置占掉。所以「检查容量 → 取指纹 → 写入」中间必须复查一次。
    """
    guard, _ = _guard_with_clock(monkeypatch, mode="creation_time")
    monkeypatch.setattr(guard, "is_alive", lambda pid: True)
    monkeypatch.setattr(guard, "_identity", lambda pid: ("creation", str(pid)))
    # 计数包装要在填表**之前**挂上，因为下面断言的是「填表阶段一次淘汰扫描都没发生」
    # 这件事本身，而不是「某个私有时间戳还等于初值」—— 后者今天恰好等价，明天只要
    # 有人给 `_evict_scan_at` 换个初值（比如防「启动瞬间就允许扫描」），这条断言就会
    # 为一个与行为无关的原因变红，读报错的人第一反应还会是去改产品代码。
    scans: list[int] = []
    original_evict = guard._evict_dead

    def _counting_evict():
        scans.append(1)
        return original_evict()

    monkeypatch.setattr(guard, "_evict_dead", _counting_evict)
    for pid in range(1, guard_module._MAX_IDENTITY_ENTRIES):
        guard._remember(pid)
    # 前提：必须站在「差一个位置就满」的点上。这样 `_remember` **绝不会**先走
    # 淘汰分支，本用例测的就一定是「复查」而不是「节流」。这两句写成断言而不是
    # 注释，是为了哪天常量或实现变了，先红在前提上，而不是给出一个看不懂的
    # 「常量不匹配」（`_TABLE_FULL` / `_SCAN_THROTTLED` / `_FILLED_MEANWHILE` 三者
    # 长得很像，弄混了很难从失败信息看出来）。
    assert len(guard._identities) == guard_module._MAX_IDENTITY_ENTRIES - 1
    assert scans == [], "填表阶段不该触发淘汰扫描"

    newcomer = 424_242

    def _probe(pid):
        # 模拟「刚腾出来的位置被别的线程抢走」
        guard._identities[123_456] = ("creation", "other")
        return ("creation", str(pid))

    monkeypatch.setattr(guard, "_identity", _probe)
    ok, why = guard._remember(newcomer)

    assert (ok, why) == (False, guard_module._FILLED_MEANWHILE)
    assert newcomer not in guard._identities, "没位置就不许写进去"
    assert len(guard._identities) == guard_module._MAX_IDENTITY_ENTRIES, "绝不能超上限"


def test_identity_snapshot_reports_capacity_and_is_a_copy(monkeypatch):
    """快照要能问出「登记了多少、离上限多远」，且改它不能影响内部状态。"""
    guard, _ = _guard_with_clock(monkeypatch, mode="creation_time")
    monkeypatch.setattr(guard, "_identity", lambda pid: ("creation", str(pid)))
    guard._remember(_FAKE_PID)

    snap = guard.identity_snapshot()
    assert snap["registered"] == 1
    assert snap["capacity"] == guard_module._MAX_IDENTITY_ENTRIES
    assert snap["pids"] == [_FAKE_PID]
    assert snap["mode"] == "creation_time"

    snap["pids"].append(1)                    # 拿到的是拷贝
    assert 1 not in guard._identities, "快照必须是拷贝，不能是内部容器本身"

    guard.forget(_FAKE_PID)
    after = guard.identity_snapshot()
    assert after["pids"] == [] and after["registered"] == 0


# ---------------------------------------------------------------------------
# 6. capabilities / identity_policy 如实报告
# ---------------------------------------------------------------------------


def test_capabilities_reports_identity_check_honestly():
    guard = ProcessGuard()
    caps = guard.capabilities()
    assert "identity_check" in caps
    assert isinstance(caps["identity_check"], bool)
    # 只有真拿到创建时间才为 True：时间锚档报 True 等于把「比了个时间差」
    # 冒充成「已校验身份」。
    assert caps["identity_check"] is (guard.identity_mode() == "creation_time")
    # 原有四个能力位不能被改动
    assert {"suspend", "resume", "terminate", "tree_kill"} <= set(caps)


def test_identity_policy_exposes_mode_and_task_level_window():
    """三档与窗口秒数要从 policy 里读得到，且不能是布尔位挤在一起。"""
    guard = ProcessGuard()
    policy = guard.identity_policy()
    assert policy["mode"] in {"creation_time", "time_window", "none"}
    assert policy["mode"] == guard.identity_mode()
    assert policy["window_seconds"] == guard_module._IDENTITY_WINDOW_SECONDS
    assert policy["platform"] == guard_module.sys.platform


@pytest.mark.parametrize(
    ("linux_ok", "windows_ready", "expected"),
    [
        (True, True, "creation_time"),
        (False, True, "time_window"),
        (False, False, "none"),
    ],
)
def test_identity_mode_maps_observability_to_three_states(
    monkeypatch, linux_ok, windows_ready, expected
):
    """三档的判据是「能观察到什么」，不是「平台叫什么名字」。

    隐藏 procfs 的容器仍能用 `os.kill(pid, 0)` 探活 → `time_window`（不是 none）；
    `none` 只在连存活都观察不到时成立（Windows 上进程 API 完全不可用）。
    """
    monkeypatch.setattr(guard_module, "_NONE_MODE_WARNED", True)   # 别让它打日志干扰
    monkeypatch.setattr(guard_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        guard_module, "_win_api", lambda: SimpleNamespace(ready=windows_ready)
    )
    monkeypatch.setattr(ProcessGuard, "_identity_capable", lambda self: linux_ok)

    assert ProcessGuard().identity_mode() == expected


def test_none_mode_warns_once_at_startup(monkeypatch, caplog):
    """`none` 档必须让人在启动日志里看得到，否则配了 terminate 的人以为功能在工作。"""
    monkeypatch.setattr(guard_module, "_NONE_MODE_WARNED", False)
    monkeypatch.setattr(guard_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(guard_module, "_win_api", lambda: SimpleNamespace(ready=False))
    monkeypatch.setattr(ProcessGuard, "_identity_capable", lambda self: False)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert ProcessGuard().identity_mode() == "none"
        assert ProcessGuard().identity_mode() == "none"   # 第二个实例不再重复打

    warned = [r for r in caplog.records if "无法校验进程身份" in r.getMessage()]
    assert len(warned) == 1, f"启动期提醒应恰好一条，实际 {len(warned)} 条"


# ---------------------------------------------------------------------------
# 7. 自身进程永远不许动（先于身份探测）
# ---------------------------------------------------------------------------


def test_self_pid_still_refused_before_identity_probe(monkeypatch):
    """对自己的拒绝先于身份校验：既不能动手，也不白开一次句柄。"""

    def _should_not_probe(pid):
        raise AssertionError("对自己不该探测身份")

    guard = ProcessGuard()
    monkeypatch.setattr(guard, "_identity", _should_not_probe)
    assert guard._usable(os.getpid(), "terminate") is None


# ---------------------------------------------------------------------------
# 8. L1：capabilities() 的探活句柄必须关掉
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="CloseHandle 只在 Windows 上存在")
def test_capabilities_closes_probe_handle(monkeypatch):
    """每次 capabilities() 都开一个句柄；不关就是长跑宿主上的句柄泄漏。"""

    class _FakeApi:
        ready = True
        suspend_ok = True
        resume_ok = True

        def __init__(self) -> None:
            self.opened = 0
            self.closed = 0

        def open_process(self, pid, access):
            self.opened += 1
            return 4242 if self.opened == 1 else 0

        def close(self, handle):
            if handle:
                self.closed += 1

    fake = _FakeApi()
    monkeypatch.setattr(guard_module, "_WIN_API", fake)
    caps = ProcessGuard().capabilities()

    assert caps["terminate"] is True
    assert fake.opened == 1
    assert fake.closed == fake.opened, "探活句柄没关：每调一次 capabilities() 就漏一个"
