"""哨兵运行时的测试。

运行时是各层的编排者，它的价值全在**边界**上：

* 关闭态必须真的什么都不做（否则「默认关闭」就是一句空话）；
* 输出流必须立刻返回（否则读取线程被占，子进程卡在写管道上）；
* 收尾前必须消化完队列（否则任务最后那句危险命令会被无声丢掉）；
* 同一个 cwd 上的并发任务必须是**两路**会话（否则 terminate 会打到别人的进程上）；
* 干预必须幂等（一句 rm -rf 会重复出现几十次）。
"""

from __future__ import annotations

import os
import queue
import threading
import time
from types import SimpleNamespace

# isort: split
# bridge 在插件仓库内是一方包、在 CI 的检出布局下会被当成三方包；
# 显式切段，两种 cwd 下的 I001 判定就都稳定了。

from bridge.config import SentinelConfig
from bridge.sentinel_core.briefing import build_briefing
from bridge.sentinel_core.runtime import SentinelRuntime
from bridge.sentinel_core.store import KIND_ERROR, KIND_PRECHECK, KIND_SYSTEM

FP_CWD = "D:/work/demo"


class _CountingGuard:
    """假干预器：只记账不真动手，用来断言「动手了几次」。"""

    def __init__(self) -> None:
        self.terminated: list[int] = []
        self.suspended: list[int] = []

    def terminate(self, pid: int) -> bool:
        self.terminated.append(int(pid))
        return True

    def suspend(self, pid: int) -> bool:
        self.suspended.append(int(pid))
        return True

    def capabilities(self) -> dict:
        return {"fake": True}


def _wait_for(predicate, *, timeout: float = 2.0) -> bool:
    """轮询等某个条件成立（干预跑在独立线程里，只能这样断言）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()



def _runtime(tmp_path, **overrides):
    """造一个启用态运行时，账本落在临时目录。"""
    config = SentinelConfig(enabled=True, safepoint=False)
    for key, value in overrides.items():
        setattr(config, key, value)
    return SentinelRuntime(config, data_dir=tmp_path)


# ---------------------------------------------------------------------------
# 关闭态
# ---------------------------------------------------------------------------


def test_disabled_runtime_touches_nothing():
    rt = SentinelRuntime(SentinelConfig(enabled=False))
    try:
        assert rt.enabled is False
        assert rt.store is None and rt.judge is None and rt.guard is None
        assert rt.pre_check("rm -rf /")["action"] == "allow"
        assert rt.begin_session(cwd=FP_CWD) == ""
        rt.on_output(b"rm -rf /\n")
        rt.note_pre_check("rm -rf /", blocked=True, agent="x")
        assert rt.timeline() == "（安全审计未启用）"
        assert rt.briefing() == "（安全审计未启用）"
        assert rt.usage() == "（安全审计未启用）"
        assert rt.rollback(FP_CWD) is False
        assert rt.timebox_start() == 0.0
        assert rt.timebox_check(0.0) is None
        assert rt.flush_alerts() == {"sent": 0, "failed": 0, "skipped": 0}
        assert rt.doctor()["enabled"] is False
        rt.close()
    finally:
        rt.close()


def test_disabled_runtime_does_not_create_db(tmp_path):
    target = tmp_path / "should-not-exist.db"
    rt = SentinelRuntime(SentinelConfig(enabled=False, store_path=str(target)), data_dir=tmp_path)
    try:
        assert not target.exists()
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 预检与记账
# ---------------------------------------------------------------------------


def test_pre_check_records_event(tmp_path):
    rt = _runtime(tmp_path)
    try:
        assert rt.pre_check("帮我写个排序函数")["action"] == "allow"
        rt.pre_check("rm -rf / --no-preserve-root")
        rows = rt.store.timeline()
        assert len(rows) == 2
        assert rows[1]["severity"] == "red_line"
        assert rows[1]["rule_id"]
    finally:
        rt.close()


def test_pre_check_block_writes_alert_and_savings(tmp_path):
    rt = _runtime(tmp_path)
    try:
        rt.pre_check("rm -rf / --no-preserve-root", agent="codebuddy")
        assert rt.store.stats()["alerts"] == 1
        assert rt.store.stats()["saved_tokens"] > 0
    finally:
        rt.close()


def test_local_precheck_honors_block_severity(tmp_path):
    """``block_severity`` 是公开配置项，进程内的本地判定也必须读它。

    设成 ``red_line`` 的用户是在明确要求「只拦不可逆操作」：派发闸门（``audit.py`` /
    ``server.py``）确实放宽了，但本地 ``pre_check`` 若仍按 ``standard`` 拦，同一条可恢复
    的 ``rm -rf ./dist`` 就会在输出路径上被判 block —— 配上 ``guard_action="terminate"``
    时会把用户**显式放行**的任务杀掉。所以两条路径（派发预检 + 逐行输出）都要验。
    """
    strict = _runtime(tmp_path / "strict")
    relaxed = _runtime(tmp_path / "relaxed", block_severity="red_line")
    try:
        # 1) 派发预检路径
        assert strict.pre_check("rm -rf ./dist")["action"] == "block"
        loose = relaxed.pre_check("rm -rf ./dist")
        assert loose["action"] != "block", "block_severity=red_line 没有生效"
        assert loose["rule_id"], "放宽只是不拦，规则命中信息仍要带回来"

        # 2) 逐行输出路径 —— 这条才是会连带触发 terminate 的那条。
        #    注意：派发预检本身无论放行与否都会记一条 KIND_PRECHECK（指纹为空），
        #    所以这里按会话指纹把「输出路径」的行挑出来判，否则会被第 1 步的行干扰。
        fps = {}
        for name, rt in (("strict", strict), ("relaxed", relaxed)):
            fps[name] = rt.begin_session(cwd=FP_CWD, agent="codebuddy")
            rt.on_output("rm -rf ./dist\n", fingerprint=fps[name], agent="codebuddy")
            rt.finish_session(fps[name], exit_code=0)

        def _line_prechecks(rt, fp):
            return [row for row in rt.store.timeline() if row["kind"] == KIND_PRECHECK and row["fingerprint"] == fp]

        assert _line_prechecks(strict, fps["strict"]), "standard 档应当把输出里的 rm -rf ./dist 拦下来"
        assert not _line_prechecks(relaxed, fps["relaxed"]), "red_line 档不该拦可恢复删除"
        assert relaxed.store.stats()["alerts"] == 0, "放宽档不应产生告警（告警会触发干预）"
    finally:
        strict.close()
        relaxed.close()


def test_note_pre_check_does_not_re_judge(tmp_path):
    """记账入口不能自己再判一遍 —— 否则「外部后端拦下」会被写成「内置放行」。"""
    rt = _runtime(tmp_path)
    try:
        # 任务文本是安全的，但外部判定说拦了：账本必须照外部结论记
        rt.note_pre_check("帮我看下日志", blocked=True, rule_id="ext.1", reason="外部判定", severity="standard")
        rows = rt.store.timeline()
        assert len(rows) == 1
        assert rows[0]["verdict"] == "DANGEROUS"
        assert rows[0]["rule_id"] == "ext.1"
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


def test_attach_pid_keeps_one_fingerprint(tmp_path):
    """先定型、后补 pid —— 同一路会话只被回填一次，不能因为补了 pid 就分裂成两条。"""
    rt = _runtime(tmp_path)
    try:
        fp = rt.begin_session(cwd=FP_CWD, agent="codebuddy", goal="修好登录页")
        # 身份不再由 cwd 推导（同 cwd 的并发任务必须互不干扰，见下一条用例），
        # 但它仍然是一路会话的稳定句柄。
        assert fp and fp != FP_CWD
        rt.attach_pid(fp, 4321)
        sessions = rt.store._read("SELECT * FROM sessions")
        assert len(sessions) == 1
        assert sessions[0]["pid"] == 4321
        assert sessions[0]["goal"] == "修好登录页"
    finally:
        rt.close()


def test_two_sessions_same_cwd_do_not_collide(tmp_path):
    """同 cwd 的两路并发任务必须各自独立 —— 这是不可逆故障的防线。

    ``allow_parallel=true`` / ``max_parallel=4`` 是默认配置。身份若只由 cwd 决定，
    两路任务会共享一个 session，后启动的 pid 覆盖先启动的；随后一次 ``terminate``
    就会打断**另一个任务**的进程。所以这里要求：身份不同、pid 各归各的。
    """
    rt = _runtime(tmp_path)
    try:
        first = rt.begin_session(cwd=FP_CWD, agent="codebuddy")
        second = rt.begin_session(cwd=FP_CWD, agent="codebuddy")
        assert first and second and first != second
        rt.attach_pid(first, 1111)
        rt.attach_pid(second, 2222)
        rows = rt.store._read("SELECT fingerprint, pid FROM sessions")
        assert len(rows) == 2
        pids = {row["fingerprint"]: row["pid"] for row in rows}
        assert pids[first] == 1111
        assert pids[second] == 2222
        # 内存侧的会话上下文也不能串味。
        assert rt._sessions.get(first).pid == 1111
        assert rt._sessions.get(second).pid == 2222
    finally:
        rt.close()


def test_attach_pid_does_not_overwrite_existing(tmp_path):
    """已经回填过 pid 的会话，再 attach 一次必须被忽略（覆盖 = 换干预目标）。"""
    rt = _runtime(tmp_path)
    try:
        fp = rt.begin_session(cwd=FP_CWD, agent="codebuddy")
        rt.attach_pid(fp, 1111)
        rt.attach_pid(fp, 9999)
        session = rt._sessions.get(fp)
        assert session.pid == 1111
        rows = rt.store._read("SELECT pid FROM sessions WHERE fingerprint = ?", (fp,))
        assert rows[0]["pid"] == 1111
    finally:
        rt.close()


def test_goal_not_overwritten_by_later_calls(tmp_path):
    rt = _runtime(tmp_path)
    try:
        first = rt.begin_session(cwd=FP_CWD, goal="最初的目标")
        second = rt.begin_session(cwd=FP_CWD, goal="后来的调用")
        assert first != second
        # 两路会话各记各的目标，后一路不会污染前一路。
        assert rt._sessions.get(first).goal == "最初的目标"
        assert rt._sessions.get(second).goal == "后来的调用"
        rt.attach_pid(first, 4321)  # 补 pid 不该动 goal
        assert rt._sessions.get(first).goal == "最初的目标"
        rows = rt.store._read("SELECT fingerprint, goal FROM sessions WHERE fingerprint = ?", (first,))
        assert rows[0]["goal"] == "最初的目标"
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 输出流：非阻塞 + 不丢尾巴
# ---------------------------------------------------------------------------


def test_on_output_returns_immediately(tmp_path):
    """600 条危险行必须秒回 —— 这个方法跑在管道读取线程上，慢一次就够毁掉任务。"""
    rt = _runtime(tmp_path)
    try:
        fp = rt.begin_session(cwd=FP_CWD, agent="codebuddy")
        started = time.perf_counter()
        for index in range(600):
            rt.on_output(f"line {index} rm -rf / --no-preserve-root\n", fingerprint=fp, agent="codebuddy")
        elapsed = time.perf_counter() - started
        assert elapsed < 2.0, f"on_output 太慢：{elapsed:.2f}s（它必须只做入队）"
    finally:
        rt.close()


def test_finish_session_drains_queue_before_closing(tmp_path):
    """任务最后一段输出里的危险命令不能被丢掉。"""
    rt = _runtime(tmp_path)
    try:
        fp = rt.begin_session(cwd=FP_CWD, agent="codebuddy")
        rt.on_output("rm -rf / --no-preserve-root\n", fingerprint=fp, agent="codebuddy")
        rt.finish_session(fp, exit_code=0)
        rows = rt.store.timeline()
        assert any(row["severity"] == "red_line" for row in rows)
    finally:
        rt.close()


def test_finish_session_releases_pid_identity(tmp_path):
    """收尾时必须调 ``guard.forget``：否则下一个复用同一 pid 的任务只有半截窗口。

    时间窗锚在「首次见到这个 pid」，不清掉的话长跑宿主里越晚派发的任务越接近窗口
    过期，最后 ``terminate`` 会因「无法确认身份」被拒 —— 而 ``_expiry_noted`` /
    ``_degrade_noted`` 里那个 pid 也会一直留着。

    ``_identities`` 是**惰性**登记的（``_remember`` 只在 ``_usable`` 里触发），而且
    本机若处在 ``identity_mode() == "none"`` 档，``_usable`` 压根不会登记；所以这里
    直接把「已登记」的三个结构摆上，验的是 ``forget`` 的清理范围，不是这台机器的
    身份校验能力。
    """
    # guard_action=alert 时 guard 为 None（没有可释放的东西），必须用 terminate 建出真 guard。
    rt = _runtime(tmp_path, guard_action="terminate")
    try:
        assert rt.guard is not None
        fp = rt.begin_session(cwd=str(tmp_path))
        pid = os.getpid() + 1  # 只登记，不发信号
        rt.attach_pid(fp, pid)
        rt.guard._identities[pid] = ("seen", time.monotonic())
        rt.guard._expiry_noted.add(pid)
        rt.guard._degrade_noted.add(pid)
        assert pid in rt.guard._identities

        rt.finish_session(fp)

        assert pid not in rt.guard._identities
        assert pid not in rt.guard._expiry_noted
        assert pid not in rt.guard._degrade_noted
    finally:
        rt.close()


def test_doctor_reports_identity_policy(tmp_path):
    """doctor 必须能看出「这台机器凭什么校验身份」，否则 terminate 被拒时无从解释。

    ``capabilities()`` 全是布尔位，看不出「处在哪一档、窗口多少秒」，所以
    ``identity_policy()`` 单独挂在 ``info["guard"]["policy"]`` 下。
    """
    rt = _runtime(tmp_path, guard_action="terminate")  # alert 档没有 guard，也就没有 policy
    try:
        policy = rt.doctor()["guard"]["policy"]
        assert policy["mode"] in {"creation_time", "time_window", "none"}
        assert policy["window_seconds"] > 3600
        assert policy["platform"]
    finally:
        rt.close()


def test_parse_failures_are_counted_and_visible(tmp_path, monkeypatch):
    """适配器坏掉必须能在 doctor 里看见，不能只靠一条 debug 日志。

    ``sentinel_core`` 在插件路径上没人设日志级别（全仓只有 ``__main__.py`` 那个独立
    CLI 入口设过），所以 ``logger.debug`` 实际不输出；而适配器全坏意味着每一行都退化
    成纯文本，账本上只表现为「结构化字段全空」—— 和「输出本来就没结构」不可区分。
    计数必须和 ``dropped_events`` 一样出现在 ``doctor()`` 里。
    """

    def boom(name: str):
        raise RuntimeError(f"适配器 {name} 坏了")

    rt = _runtime(tmp_path)
    try:
        fp = rt.begin_session(cwd=FP_CWD, agent="codebuddy")
        # 正常解析一组行先跑一遍：计数必须是 0，否则这个字段恒非零就没有意义。
        rt.on_output("普通输出\n", fingerprint=fp, agent="codebuddy")
        rt.finish_session(fp, exit_code=0)
        assert rt.doctor()["parse"]["failures"] == 0, "正常解析不该计数"

        monkeypatch.setattr("bridge.sentinel_core.adapters.get_adapter", boom)
        rt.on_output("第一行\n第二行\n", fingerprint=fp, agent="codebuddy")
        rt.finish_session(fp, exit_code=0)  # 再排空一次

        assert rt._parse_failures == 2, "解析失败没有被计数"
        assert rt.doctor()["parse"]["failures"] == 2
        # 降级不等于丢行：解析失败的两行仍要以纯文本进账本。
        assert len(rt.store.timeline()) >= 3
    finally:
        rt.close()


def test_finish_session_waits_for_inflight_item(tmp_path, monkeypatch):
    """收尾必须等「已取出但还在处理」的那一条，不能只看队列空不空。

    ``qsize() == 0`` 只说明没有待取的条目：消费线程可能刚把它 ``get()`` 出来、正在
    ``_process`` 里跑，此时按「队列空」判定就会认为「齐了」直接返回。而**最后一条**
    恰恰最可能是关键证据（「我要删库了」往往是收尾那句），于是取证链正好断在最该
    保住的那条上，并且 ``_dropped`` 还是 0。

    判据不是「最终记账正确」（松掉在途计数后，处理会在之后补完、终态仍然正确），
    而是**收尾返回那一刻在途数必须为 0** —— 只有这个断言能把「等过」和「没等」分开。
    """
    rt = _runtime(tmp_path)
    try:
        fp = rt.begin_session(cwd=FP_CWD, agent="codebuddy")
        original = rt._process
        entered = threading.Event()
        release = threading.Event()

        def blocked_process(text: str, fingerprint: str, agent: str) -> None:
            entered.set()
            # 由测试放行，而不是自己计时：计时会让这条用例重新变成「靠负载碰运气」。
            assert release.wait(5.0), "测试没有放行 _process"
            original(text, fingerprint, agent)

        monkeypatch.setattr(rt, "_process", blocked_process)
        rt.on_output("我要删库了\n", fingerprint=fp, agent="codebuddy")
        assert entered.wait(5.0), "消费线程没有取到条目"
        # 此刻队列已空（条目被取走了），但在途 1 条 —— 这正是竞态窗口。
        assert rt._queue.qsize() == 0
        assert rt._inflight == 1

        inflight_at_return: list[int] = []

        def finisher() -> None:
            rt.finish_session(fp, exit_code=0)
            inflight_at_return.append(rt._inflight)

        worker = threading.Thread(target=finisher, daemon=True, name="test-finisher")
        worker.start()
        time.sleep(0.4)  # 老实现会在 ~0.3s 的静默期后直接返回
        release.set()
        worker.join(6.0)
        assert not worker.is_alive(), "finish_session 没有返回"

        assert inflight_at_return == [0], "收尾在仍有在途事件时就返回了（最后一条会被丢）"
        # 处理已完成，事件必须已经落账。
        assert len(rt.store.timeline()) == 1
    finally:
        rt.close()


def test_queue_full_degrades_to_dropped_counter(tmp_path, monkeypatch):
    """队列满时必须丢弃并计数，绝不能阻塞调用方。"""
    rt = _runtime(tmp_path)
    try:
        small = queue.Queue(maxsize=2)
        rt._queue = small
        for index in range(5):
            rt.on_output(f"line {index}\n", fingerprint="", agent="x")
        assert rt._dropped > 0
        assert rt.doctor()["dropped_events"] == rt._dropped
    finally:
        rt.close()


def test_close_is_idempotent_and_stops_consumer(tmp_path):
    rt = _runtime(tmp_path)
    rt.begin_session(cwd=FP_CWD)
    rt.close()
    assert rt._queue is None
    rt.close()  # 再来一次不应该炸


def test_close_drains_queue_and_counts_dropped(tmp_path, monkeypatch):
    """close() 必须把队列排空；实在来不及的那些要计进 ``_dropped``，不能假装没丢。

    前半段验证「排空」：处理慢一点，但仍在等待窗口内 → 一条都不能少，
    ``dropped_events`` 必须是 0。后半段验证「如实计数」：处理卡住超过窗口 →
    剩下的条目必须计入 ``_dropped``（doctor 才不会把丢数据报成 0）。
    """
    # —— 排空：4 条危险行全部要落到账本里 ——
    rt = _runtime(tmp_path)
    try:
        fp = rt.begin_session(cwd=FP_CWD)
        original = rt._process

        def slow_process(text: str, fingerprint: str, agent: str) -> None:
            time.sleep(0.03)
            original(text, fingerprint, agent)

        monkeypatch.setattr(rt, "_process", slow_process)
        for _ in range(4):
            rt.on_output("rm -rf / --no-preserve-root\n", fingerprint=fp, agent="codebuddy")
        rt.close()
        assert rt._dropped == 0, "close() 把还没处理的事件丢了"
        rows = rt.store.timeline()
        assert sum(1 for row in rows if row["severity"] == "red_line") == 4
    finally:
        rt.close()

    # —— 来不及：消费线程卡在一次慢处理里，剩余条目要计数 ——
    stuck = _runtime(tmp_path / "stuck")
    try:

        def stuck_process(text: str, fingerprint: str, agent: str) -> None:
            time.sleep(0.6)  # 远长于下面的 close 等待窗口

        monkeypatch.setattr(stuck, "_process", stuck_process)
        for index in range(3):
            stuck.on_output(f"line {index}\n", fingerprint="", agent="x")
        time.sleep(0.05)  # 让消费线程先取走第一条，剩下的留在队列里
        stuck.close(timeout=0.2)
        assert stuck._dropped >= 1, "来不及处理的事件没有计入 dropped"
        assert stuck.doctor()["dropped_events"] == stuck._dropped
    finally:
        stuck.close()


# ---------------------------------------------------------------------------
# 干预
# ---------------------------------------------------------------------------


def test_intervene_is_idempotent_per_session(tmp_path):
    """一句 rm -rf 会重复出现几十次，动手只能一次。

    每命中一次就起线程 + 一次 ``taskkill /F /T`` 的话，40 行危险行 = 40 次打同一个
    pid（单次上限 10 秒）。这里断言：连续两次命中，假 guard 只被调用一次，
    第二次记一条「跳过重复动作」（``KIND_SYSTEM``，不是失败）。
    """
    rt = _runtime(tmp_path, guard_action="terminate")
    try:
        guard = _CountingGuard()
        rt.guard = guard
        fp = rt.begin_session(cwd=FP_CWD, agent="codebuddy", goal="清理构建产物")
        rt.attach_pid(fp, 4242)
        verdict = {"severity": "red_line", "verdict": "DANGEROUS", "rule_id": "L1.1", "reason": "rm -rf /"}

        rt._maybe_intervene(fp, verdict)
        assert _wait_for(lambda: len(guard.terminated) == 1), "第一次命中没有动手"
        rt._maybe_intervene(fp, verdict)
        time.sleep(0.2)  # 给「多出来的那根线程」一点时间暴露问题

        assert guard.suspended == []
        assert guard.terminated == [4242]
        rows = rt.store.timeline()
        assert any(row["kind"] == KIND_SYSTEM and "跳过重复动作" in row["summary"] for row in rows)
        assert not [row for row in rows if row["kind"] == KIND_ERROR]
    finally:
        rt.close()


def test_removed_suspend_action_falls_back_to_alert(tmp_path):
    """老配置里写着 suspend：必须容忍，且**不能**静默当成 terminate。"""
    rt = _runtime(tmp_path)
    try:
        guard = _CountingGuard()
        rt.guard = guard
        rt.config.guard_action = "suspend"
        fp = rt.begin_session(cwd=FP_CWD, agent="codebuddy")
        rt.attach_pid(fp, 4242)

        rt._maybe_intervene(fp, {"severity": "red_line", "verdict": "DANGEROUS", "reason": "rm -rf /"})
        time.sleep(0.2)

        assert guard.terminated == [], "suspend 被悄悄升级成了 terminate"
        assert guard.suspended == [], "suspend 已从策略层移除，不该再被调用"
        rows = rt.store.timeline()
        hit = [row for row in rows if row["kind"] == KIND_SYSTEM and "suspend" in row["summary"]]
        assert hit, "没有记下「配置里有 suspend 已移除」"
        assert "已移除" in hit[0]["summary"]
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 告警与晨报去重
# ---------------------------------------------------------------------------


def test_alert_carries_event_id_and_briefing_dedups(tmp_path):
    """告警必须带上关联事件 id，否则晨报里同一次拦截会被列两行。"""
    rt = _runtime(tmp_path)
    try:
        fp = rt.begin_session(cwd=FP_CWD, agent="codebuddy")
        rt.on_output("rm -rf / --no-preserve-root\n", fingerprint=fp, agent="codebuddy")
        rt.finish_session(fp, exit_code=0)

        alerts = rt.store.alerts()
        assert len(alerts) == 1
        assert alerts[0]["event_id"], "告警没有关联事件 id，晨报去重分支是死代码"
        linked = rt.store._read("SELECT * FROM events WHERE id = ?", (alerts[0]["event_id"],))
        assert linked and linked[0]["severity"] == "red_line"

        data = build_briefing(rt.store, since=time.time() - 3600)
        highlights = data["highlights"]
        # 事件出现一次，伴随它的告警不再单独占位。
        assert sum(1 for item in highlights if item["severity"] == "red_line") == 1
        assert [item for item in highlights if item["type"] == "alert"] == []
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 其它能力
# ---------------------------------------------------------------------------


def test_timebox_disabled_by_default(tmp_path):
    rt = _runtime(tmp_path)
    try:
        assert rt.timebox_start() == 0.0
        assert rt.timebox_check(0.0) is None
    finally:
        rt.close()


def test_timebox_reports_expiry(tmp_path):
    rt = _runtime(tmp_path, timebox_seconds=0.05)
    try:
        started = rt.timebox_start(label="长任务")
        time.sleep(0.12)
        hit = rt.timebox_check(started)
        assert isinstance(hit, dict)
        assert hit["level"] == "expired"
    finally:
        rt.close()


def test_rollback_without_safepoint_fails_softly(tmp_path):
    rt = _runtime(tmp_path)
    try:
        assert rt.rollback(str(tmp_path)) is False
    finally:
        rt.close()


def test_safepoint_fallback_matches_config_default(tmp_path):
    """配置对象缺 ``safepoint`` 字段时，兜底值必须与 ``config.py`` 的默认值一致（False）。

    兜底写成 True，会让「老配置 / 鸭子类型配置」绕过现在的默认关闭，去跑每条
    5~15 秒的 git（还会往目标仓库写 ``refs/sentinel/*``）。
    """
    config = SimpleNamespace(
        enabled=True,
        store_path="",
        retention_days=30,
        l2_enabled=False,
        guard_action="alert",
        webhook_url="",
        timebox_seconds=0.0,
        # 刻意不给 safepoint：模拟老配置对象
    )
    rt = SentinelRuntime(config, data_dir=tmp_path)
    try:
        assert rt.safepoints is None
        assert rt.doctor()["safepoint"]["enabled"] is False
    finally:
        rt.close()


def test_doctor_reports_layers(tmp_path):
    rt = _runtime(tmp_path)
    try:
        info = rt.doctor()
        assert info["enabled"] is True
        assert info["store"]["ok"] is True
        assert info["l2"]["enabled"] is False
        assert info["guard"]["enabled"] is False
        assert "dropped_events" in info
    finally:
        rt.close()


def test_doctor_reports_store_not_ok_when_disabled(tmp_path):
    """建表失败后 ``EventStore.enabled`` 会被置 False，此后写入全部静默丢弃。

    这时 doctor 若还报 ``ok: True``，就是把「没在记」说成「记着」。
    """
    rt = _runtime(tmp_path)
    try:
        assert rt.store is not None
        rt.store.enabled = False  # 模拟 _ensure_schema 失败后的降级态
        info = rt.doctor()
        assert info["store"]["ok"] is False
        assert info["store"]["path"]  # 路径仍然报出来，方便排查
    finally:
        rt.close()


def test_usage_marks_estimate(tmp_path):
    rt = _runtime(tmp_path)
    try:
        rt.metrics.record(input_tokens=100, output_tokens=50)
        assert "估算值" in rt.usage(hours=1)
    finally:
        rt.close()
