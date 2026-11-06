"""晨报的「部分统计」契约 —— 数字什么时候是下限，必须写在返回值里。

为什么有这个文件
================

``build_briefing`` 里曾经有一句 ``"events": int(stats.get("events") or len(events))``：
一旦 ``store.stats()`` 抛异常（被 ``briefing.py:257`` 吞成 ``stats = {}``），或者
``stats()`` 内部的 ``_read`` 把 sqlite 错误吞掉后返回 ``[]``（``events`` 算成 0，
撞上 ``or`` 的 falsy-0），数字就会退回 ``len(events)``。而 ``events`` 这个列表早被
``scan_limit = min(1000, max(200, max_items * 20))`` 截断过 —— ``max_items=1`` 时是
200 条。

结果：「昨夜真实 5000 条事件」会被安静地报成「200」。晨报的整个存在意义是让人早上
三秒内判断「昨晚有没有出事」，一个不说自己是下限的下限数字，比没有数字更糟。

所以契约是：**返回值里恒有一个 ``stats_partial`` 键，为真表示「事件/告警/会话/拦截
这几个数至少这么多，可能更多」。**

判据单因单果
------------

``stats_partial is True`` **当且仅当**两件事同时成立：

1. 计数确实降级到了列表长度（``stats`` 没给出可信数字），并且
2. 该列表已经顶到扫描上限（``len(events) >= scan_limit`` 或
   ``len(alerts) >= scan_limit``）—— 顶到上限才意味着「后面可能还有，被截了」。

只满足第 1 条的（比如账本炸了但只捞回 5 条）**不算部分**：列表是完整的，数字精确；
空库（真的 0 条、``stats`` 正常返回 0）也**不算部分** —— 不许对安静的夜晚报「部分」。

假对象的保真度
--------------

``FakeStore`` 的 ``stats`` / ``timeline`` / ``alerts`` 三个方法，参数名与真实
``EventStore``（``store.py:722`` / ``:677`` / ``:475``）逐条对齐，全关键字调用，
并且复现「超出 ``limit`` 就截断」的行为（真实实现靠 SQL ``LIMIT ?``，这里靠切片）。
替身比生产端宽松是上一轮 ``guard_policy`` 那个 dict-repr 缺陷溜过去的方式，这里不再犯。

同一份列表供四个数字用
----------------------

``events`` / ``by_severity`` / ``blocked`` / ``sessions`` 回退值全部可以从**同一份**
可能被 ``scan_limit`` 截断的事件列表推出来。所以「这是下限」只需要一个标记
（``stats_partial``）声明一次，不需要给每个数字配一个标志位 —— 下面的
``test_blocked_*`` 与 ``test_render_never_falls_back_to_degraded_banner`` 就是钉这件事。
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from bridge.sentinel_core.briefing import build_briefing, render_briefing
from bridge.sentinel_core.store import EventStore

FP = "fp-briefing-partial"

#: ``render_briefing`` 兜底文案的特征子串（``except Exception`` 的产物）。
#: 它出现就说明「渲染自己炸了」，而不是「数据不好看」—— 两者的处置完全不同。
_DEGRADED_BANNER = "晨报生成失败"


def _scan_limit(max_items: int) -> int:
    """时间线扫描上限的**同一条口径**（抄自 ``briefing.py:253``）。

    那边是 ``scan_limit = min(1000, max(200, int(max_items) * 20))``。改那边必须同步
    改这里，否则「列表有没有顶到扫描上限」这个判据会静默错位：测试还在按 200 判，
    实现已经按 1000 扫，于是部分统计的漏报重新变得测不出来。

    :param max_items: 晨报要的高亮条数。
    :return: 一次最多扫多少条事件/告警。
    """
    return min(1000, max(200, int(max_items) * 20))


def _zero_stats() -> dict[str, Any]:
    """与 ``EventStore.stats()``（``store.py:774``）**同键同构**的全零结果。

    ``by_kind`` 也在里面：真实实现返回它，替身不能少，否则「替身比生产端宽松」。

    :return: 统计字典。
    """
    return {
        "sessions": 0,
        "events": 0,
        "by_kind": {},
        "by_severity": {},
        "alerts": 0,
        "by_level": {},
        "blocked": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "saved_tokens": 0,
    }


def _event_rows(count: int, *, start: float) -> list[dict[str, Any]]:
    """造 ``count`` 条事件行，字段与 ``EventStore.timeline()`` 的返回行一致。

    :param count: 条数。
    :param start: 首条时间戳（之后逐条 +1 秒）。
    :return: 事件行（时间正序，与 ``timeline`` 一致）。
    """
    return [
        {
            "id": index + 1,
            "ts": start + index,
            "kind": "tool",
            "severity": "",
            "verdict": "",
            "rule_id": "",
            "fingerprint": f"{FP}-{index % 3}",
            "summary": f"第 {index} 条输出",
            "detail": "",
        }
        for index in range(count)
    ]


def _alert_rows(count: int, *, start: float) -> list[dict[str, Any]]:
    """造 ``count`` 条告警行，字段与 ``EventStore.alerts()`` 的返回行一致。

    :param count: 条数。
    :param start: 首条时间戳（之后逐条 +1 秒）。
    :return: 告警行（时间正序；替身内部会反转成真实实现的「时间倒序」）。
    """
    return [
        {
            "id": index + 1,
            "ts": start + index,
            "level": "warn",
            "title": f"第 {index} 条告警",
            "body": "",
            "event_id": None,
            "delivered": 0,
            "fingerprint": f"{FP}-{index % 3}",
        }
        for index in range(count)
    ]


class FakeStore:
    """复现 ``EventStore`` 的公开查询面，用于制造「聚合失败」这一类真实故障。

    :ivar stats_calls: 每次 ``stats()`` 收到的关键字参数（供断言传参口径）。
    :ivar timeline_limit: 最后一次 ``timeline()`` 收到的 ``limit``。
    :ivar alerts_limit: 最后一次 ``alerts()`` 收到的 ``limit``。
    """

    def __init__(
        self,
        *,
        stats: dict[str, Any] | None = None,
        stats_error: BaseException | None = None,
        events: list[dict[str, Any]] | None = None,
        alerts: list[dict[str, Any]] | None = None,
    ) -> None:
        self._stats = _zero_stats() if stats is None else dict(stats)
        self._stats_error = stats_error
        self._events = list(events or [])
        self._alerts = list(alerts or [])
        self.stats_calls: list[dict[str, Any]] = []
        self.timeline_limit: int | None = None
        self.alerts_limit: int | None = None

    def stats(self, *, since: float | None = None, until: float | None = None) -> dict[str, Any]:
        """签名对齐 ``EventStore.stats``（``store.py:722``）。

        :param since: 起始时间戳。
        :param until: 结束时间戳。
        :return: 统计字典。
        :raises BaseException: ``stats_error`` 非空时按构造抛出。
        """
        self.stats_calls.append({"since": since, "until": until})
        if self._stats_error is not None:
            raise self._stats_error
        return dict(self._stats)

    def timeline(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        fingerprint: str = "",
        severity: str = "",
        kinds: Iterable[str] = (),
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """签名对齐 ``EventStore.timeline``（``store.py:677``）。

        真实实现是 ``ORDER BY ts DESC LIMIT ?`` 再 ``reverse()``，等价于「取最近的
        ``limit`` 条后按时间正序返回」，这里用 ``rows[-limit:]`` 复现同样的截断语义。

        :param since: 起始时间戳。
        :param until: 结束时间戳。
        :param fingerprint: 会话指纹过滤（本替身不实现过滤，测试不依赖它）。
        :param severity: 严重度过滤（同上）。
        :param kinds: 种类过滤（同上）。
        :param limit: 最多几条。
        :return: 事件行（时间正序）。
        """
        self.timeline_limit = int(limit)
        return list(self._events[-int(limit):])

    def alerts(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """签名对齐 ``EventStore.alerts``（``store.py:475``）。

        真实实现按时间**倒序**返回并 ``LIMIT ?``，这里同样反转后截断。

        :param since: 起始时间戳。
        :param until: 结束时间戳。
        :param limit: 最多几条。
        :return: 告警行（时间倒序）。
        """
        self.alerts_limit = int(limit)
        return list(reversed(self._alerts))[: int(limit)]


def _require_partial_key(data: dict[str, Any], scene: str) -> bool:
    """断言 ``stats_partial`` 这个键**存在**，并返回它的值。

    用 ``in`` 而不是 ``data.get(...)``：``.get`` 会在键缺失时静默给出 ``None``（falsy），
    于是「契约没实现」会被伪装成「这个场景恰好不需要部分统计」—— 这正是本文件要防的
    那类静默失败。

    :param data: ``build_briefing`` 的返回值。
    :param scene: 场景描述（写进断言消息）。
    :return: ``stats_partial`` 的值。
    """
    assert "stats_partial" in data, (
        f"契约 1 要求 build_briefing 返回的 dict 恒有顶层键 stats_partial（{scene}）。"
        f"现在没有这个键，实际键={sorted(data)}。"
        "没有它，调用方无法区分「精确值」和「扫描上限截出来的下限」，"
        "真实 5000 条事件会安静地报成 200。"
    )
    return data["stats_partial"]


def test_stats_failure_with_truncated_timeline_is_partial() -> None:
    """聚合抛异常 + 时间线顶到扫描上限 → 数字是下限，必须标 partial。

    这是原始 bug 的现场：``stats()`` 炸了，晨报退回 ``len(events)``，而列表已被截到
    200 条，于是「至少 200 条」被当成「就是 200 条」报出去。
    """
    max_items = 1
    limit = _scan_limit(max_items)
    now = time.time()
    fake = FakeStore(
        stats_error=RuntimeError("database is locked"),
        events=_event_rows(limit, start=now - 3600),
    )

    data = build_briefing(fake, since=now - 3600, max_items=max_items)

    assert fake.timeline_limit == limit, (
        f"时间线必须按 scan_limit={limit} 扫描（briefing.py:253 口径：min(1000, max(200, max_items*20))，"
        f"max_items={max_items}）。实际 limit={fake.timeline_limit}。这个数变了，"
        "「列表顶到上限」的判据和本文件的截断长度都得跟着改，所以先在这里钉住。"
    )
    assert _require_partial_key(data, "stats 抛异常且时间线被截断") is True, (
        f"时间线取回 200 行，正好等于扫描上限 {limit}，说明后面可能还有被截掉的事件；"
        "同时 stats 抛了异常，晨报拿不到真实总数。"
        f"此时 events={data.get('events')} 只是下界（至少这么多），"
        "stats_partial 必须是 True 把这件事说出来。"
    )


def test_stats_failure_with_short_timeline_is_exact() -> None:
    """聚合抛异常但只捞回 5 条 → 列表完整，数字精确，不许标 partial。

    单因单果：partial 的因是「列表被截断」，不是「stats 抛过异常」。账本炸了但整夜
    只有 5 条记录时，晨报说「5 条」是真话，标成 partial 只会让人白紧张。
    """
    now = time.time()
    fake = FakeStore(
        stats_error=RuntimeError("database is locked"),
        events=_event_rows(5, start=now - 3600),
    )

    data = build_briefing(fake, since=now - 3600, max_items=1)

    partial = _require_partial_key(data, "stats 抛异常但时间线完整")
    assert partial is False, (
        f"时间线只取回 5 条，远未顶到扫描上限 {_scan_limit(1)}，也就是「这段时间就这么多条」，"
        "列表没有截断，数字是精确值。此时报 stats_partial=True 会让安静的夜晚看起来出过事。"
    )
    assert data["events"] == 5, (
        f"stats 拿不到时退回时间线条数是合法的降级，但必须是完整列表的长度 5；实际 {data['events']}。"
    )


def test_normal_stats_with_short_timeline_stays_exact() -> None:
    """stats 给出真值 5000、时间线只有 200 条采样 → partial 必须是 False。

    这条是关键单因单果用例：被判 partial 的因**只能是「数不清」**，不能是「列表短」。
    正常路径下 ``events`` 来自 ``stats()`` 的 COUNT，时间线那 200 条只是给高亮用的采样，
    把这种情形标成 partial 会让每一份正常晨报都自带免责声明，等于把标记变成噪声。
    """
    now = time.time()
    limit = _scan_limit(1)
    fake = FakeStore(
        stats={
            **_zero_stats(),
            "events": 5000,
            "by_kind": {"tool": 5000},
            "sessions": 7,
        },
        events=_event_rows(limit, start=now - 3600),
    )

    data = build_briefing(fake, since=now - 3600, max_items=1)

    partial = _require_partial_key(data, "stats 正常且时间线被截断")
    assert partial is False, (
        "stats() 正常返回了真实总数 5000，这个数字来自 SQL COUNT，不由时间线长度决定。"
        "时间线被截到 200 条只是采样短，不代表统计口径降级 —— 这时候标 partial 就是误报。"
    )
    assert data["events"] == 5000, (
        f"stats 可信时 events 必须照抄 5000；实际 {data['events']}。"
        f"若等于 {limit}，说明又退回了被截断的 len(events)，也就是本文件要防的静默少报。"
    )


def test_truncated_alerts_without_count_is_partial() -> None:
    """事件数可信但**告警数**降级且告警列表顶满 → 仍须标 partial（契约 2 的对称分支）。

    契约 2 写的是「``len(events) >= scan_limit``**或** ``len(alerts) >= scan_limit``」：
    ``stats_partial`` 声明的是「events / alerts / sessions / blocked 都可能是下限」，
    所以只要**任一**计数来自被截断的列表，这个标记就得亮。

    现实入口：``stats()`` 里 events 的 COUNT 成功了、alerts 的 COUNT 撞上
    ``database is locked`` 被 ``_read`` 吞成 ``[]``（两次查询之间锁被抢走），于是
    ``alerts`` 算成 0 → 撞 falsy → 退回 ``len(alerts) = 200``，而真值是 5000。
    此时 events 是精确的 5000，只有 alerts 少报 —— 但标记只有一个，必须亮。
    """
    now = time.time()
    limit = _scan_limit(1)
    fake = FakeStore(
        stats={
            **_zero_stats(),
            "events": 5000,
            "by_kind": {"tool": 5000},
            "sessions": 3,
            # alerts 计数缺失（= 0），模拟 alerts 的 COUNT 查询被吞成空结果。
        },
        events=_event_rows(limit, start=now - 3600),
        alerts=_alert_rows(limit, start=now - 3500),
    )

    data = build_briefing(fake, since=now - 3600, max_items=1)

    partial = _require_partial_key(data, "告警列表顶满但告警计数缺失")
    assert partial is True, (
        f"告警取回的行数正好等于扫描上限 {limit}（后面可能还有被截掉的），"
        "而 stats 没给出告警计数，所以 alerts="
        f"{data.get('alerts')} 只是下界。events 精确 5000 不能替 alerts 担保 —— "
        "stats_partial 声明的是「这几个数都可能是下限」，标记只看 events 就会对这个少报沉默。"
    )
    assert data["alerts"] == limit, (
        f"告警数退回完整列表长度是合法降级，但必须等于 {limit}；实际 {data['alerts']}。"
    )


def test_default_max_items_scan_limit_branch_marks_partial() -> None:
    """默认 ``max_items=20`` 时 scan_limit 是 400，不是 200 —— 标记得跟着这条分支走。

    上面几条都走 ``max_items=1``（面板那条路径，scan_limit=200）。晨报/通知走的是
    **默认 20**，此时 ``scan_limit = min(1000, max(200, 20*20)) = 400``。如果实现里把
    上限写死成 200，那么「正好 400 条」这种真实顶满的情形会被判成「没顶满」，
    partial 静默漏报 —— 这条用例就是钉住公式的第二个分支。
    """
    limit = _scan_limit(20)
    now = time.time()
    fake = FakeStore(
        stats_error=RuntimeError("database is locked"),
        events=_event_rows(limit, start=now - 7200),
    )

    data = build_briefing(fake, since=now - 7200)  # 不传 max_items：走默认 20

    assert limit == 400, (
        f"前提自检：min(1000, max(200, 20*20)) 应为 400，实际算出 {limit}。"
        "这条自检红了说明本文件 _scan_limit 与 briefing.py:253 的口径已经分叉。"
    )
    assert fake.timeline_limit == limit, (
        f"默认 max_items=20 时必须按 scan_limit={limit} 扫描；实际 limit={fake.timeline_limit}。"
        "写成 200 会让「正好 400 条」的真实顶满被判成没顶满。"
    )
    partial = _require_partial_key(data, "默认 max_items 且时间线顶满")
    assert partial is True, (
        f"扫描上限是 {limit}，时间线取回 {limit} 条正好顶满，stats 又抛了异常 —— "
        f"events={data.get('events')} 只是下界，stats_partial 必须是 True。"
    )


def test_none_store_is_not_partial() -> None:
    """账本对象为 ``None`` → 空摘要，且 stats_partial 仍是 False。

    没有账本不是「统计降级」，是「根本没开哨兵」。晨报已用「这段时间没有记录」表达它，
    再叠一个 partial 会让人以为是账本读坏了。
    """
    now = time.time()

    data = build_briefing(None, since=now - 3600)

    partial = _require_partial_key(data, "store is None")
    assert partial is False, (
        "store 为 None 时走 _empty_briefing()，一份空摘要里的 0 是精确的 0（确实没有账本），"
        "不是「至少 0 条」的下界。契约 1 要求这条路径也带 stats_partial=False。"
    )
    assert data["events"] == 0 and data["alerts"] == 0, (
        f"空摘要的计数必须是 0；实际 events={data.get('events')} alerts={data.get('alerts')}。"
    )


def test_empty_ledger_is_not_partial(tmp_path) -> None:
    """真实 EventStore、真的 0 条记录 → stats_partial 是 False。

    这里刻意用真实账本而不是替身：空库上 ``stats()`` 返回的 ``events`` 是 0，会撞上
    ``or`` 的 falsy 分支退回 ``len(events)``（也是 0）—— 这看起来像「降级」，但列表
    完整且为空，数字精确。别把这条写反成 True。
    """
    store = EventStore(tmp_path / "briefing.db")
    now = time.time()

    data = build_briefing(store, since=now - 3600)

    partial = _require_partial_key(data, "真实空账本")
    assert partial is False, (
        "空账本里 events=0 是**真的 0 条**，且时间线没有顶到扫描上限，数字精确可信。"
        "把安静的夜晚标成 partial 会训练用户忽略这个标记。"
    )
    assert data["events"] == 0, f"空账本的事件数必须是 0；实际 {data['events']}。"
    assert data["by_severity"] == {} and data["by_level"] == {}, (
        f"空账本的分布必须是空字典；实际 by_severity={data.get('by_severity')} "
        f"by_level={data.get('by_level')}。"
    )


def test_partial_does_not_change_other_field_shapes() -> None:
    """标记 partial 时，既有字段形状一个都不能少、不能变类型。

    ``stats_partial`` 是**新增**键，不是把别的键换掉。面板、通知、前端都按固定形状
    取值，降级路径少一个键就会在那里炸出 KeyError 或渲染出 ``None``。
    """
    now = time.time()
    limit = _scan_limit(1)
    fake = FakeStore(
        stats_error=RuntimeError("database is locked"),
        events=_event_rows(limit, start=now - 3600),
        alerts=_alert_rows(3, start=now - 3500),
    )

    data = build_briefing(fake, since=now - 3600, max_items=1)

    _require_partial_key(data, "字段形状")
    expected = {
        "range",
        "sessions",
        "events",
        "alerts",
        "blocked",
        "by_severity",
        "by_level",
        "usage",
        "highlights",
        "first_seen",
        "last_seen",
        "stats_partial",
    }
    missing = expected - set(data)
    assert not missing, (
        f"晨报字典缺键 {sorted(missing)}，实际键={sorted(data)}。"
        "stats_partial 只能是新增键，不许顶替或挤掉任何既有字段。"
    )
    assert isinstance(data["by_severity"], dict) and isinstance(data["by_level"], dict), (
        f"by_severity/by_level 必须是 dict；实际 {type(data['by_severity'])} / {type(data['by_level'])}。"
    )
    assert isinstance(data["usage"], dict), f"usage 必须是 dict；实际 {type(data['usage'])}。"
    for key in ("input_tokens", "output_tokens", "total_tokens", "saved_tokens", "calls", "since"):
        assert key in data["usage"], (
            f"usage 缺 {key}，实际键={sorted(data['usage'])}。"
            "render_briefing / 面板都按这些键取值，少一个就是 KeyError。"
        )
    assert isinstance(data["highlights"], list), f"highlights 必须是 list；实际 {type(data['highlights'])}。"
    assert data["range"]["since"] == float(now - 3600), (
        f"range.since 必须原样回传传入值；实际 {data['range'].get('since')}。"
    )
    assert data["range"]["until"] is None, f"range.until 在未传 until 时必须是 None；实际 {data['range'].get('until')}。"


def _broken_stats(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """模拟 ``store.stats()`` 撞上 ``database is locked``：**抛出去**，不是返回空。

    真实 ``EventStore.stats`` 内部 ``_read`` 会吞 sqlite 错误，但连接层以外的失败
    （锁超时、路径消失、schema 被改）照样会抛；``build_briefing`` 就是按「会抛」兜的。

    :return: 永不返回。
    :raises RuntimeError: 调用即抛。
    """
    raise RuntimeError("database is locked")


def _seed_red_lines(store: EventStore, count: int, *, start: float) -> None:
    """往真实账本里写 ``count`` 条红线拦截事件。

    :param store: 真实账本。
    :param count: 条数。
    :param start: 首条时间戳（之后逐条 +1 秒）。
    """
    for index in range(count):
        store.add_event(
            kind="precheck",
            fingerprint=FP,
            severity="red_line",
            verdict="dangerous",
            rule_id="rm-root",
            summary=f"rm -rf / 第 {index} 次",
            ts=start + index,
        )


def test_blocked_derived_from_events_when_stats_fails(tmp_path) -> None:
    """聚合炸了但事件列表完整 → ``blocked`` 必须从事件行推出来，不许写 0。

    这条是「一份自己跟自己打架的晨报」的回归：``blocked`` 曾是
    ``int(stats.get("blocked") or 0)``，聚合一失败就归零，于是结论说「昨夜平安」，
    而下面「关键事件」正列着三条 ``[红线] 派发前拦截``。**同一个屏幕两个说法**。

    ``blocked`` 是 ``stats()`` 四条 COUNT 之一，聚合失败时它压根没被算过 —— 写 0
    等于断言「昨夜一条都没拦」，而真相是「没人算过」。事件行里带 ``severity``，
    所以完整列表能给出**精确值**：此时 ``stats_partial`` 仍是 False（数字可信）。
    """
    store = EventStore(tmp_path / "blocked.db")
    now = time.time()
    _seed_red_lines(store, 5, start=now - 3600)
    assert store.stats(since=now - 7200)["blocked"] == 5, (
        "前提自检：账本里确实有 5 条红线拦截。这条红了说明种子数据没写进去，后面的断言就没意义。"
    )

    store.stats = _broken_stats  # type: ignore[method-assign] —— 只坏聚合这一条路
    data = build_briefing(store, since=now - 7200)

    assert data["by_severity"] == {"red_line": 5}, (
        f"stats 不给 by_severity 时必须从事件行重算；实际 {data['by_severity']}。"
        "空字典会让 render_briefing 的「有没有红线」判成 0，结论就会写「昨夜平安」。"
    )
    assert data["blocked"] == 5, (
        f"5 条红线事件的列表是完整的（5 < scan_limit 400），blocked 必须是精确的 5；"
        f"实际 {data['blocked']}。0 的含义是「昨夜一条都没拦」，那是另一件事。"
    )
    assert data["stats_partial"] is False, (
        "列表没被截断，从它推出的数字是精确值 —— 这时候标 partial 会让每个安静的夜晚都像出过事。"
    )

    text = render_briefing(data)
    assert "昨夜平安" not in text, (
        f"结论不许说「昨夜平安」：账本里有 5 条红线拦截，只是聚合读不到。实际输出：\n{text}"
    )
    assert "拦截 0 条" not in text, (
        f"「拦截 0 条」是把「没人算过」说成「确实没拦」。实际输出：\n{text}"
    )
    assert "昨夜拦下 5 条红线操作" in text, (
        f"结论必须按事件行报出 5 条红线拦截。实际输出：\n{text}"
    )


def test_blocked_is_lower_bound_when_events_truncated(tmp_path) -> None:
    """列表被截断时 ``blocked`` 是**下限**，由 ``stats_partial`` 统一声明一次。

    500 条真实红线、默认 ``max_items=20`` → ``scan_limit=400``，所以列表顶满、
    ``blocked`` 只能给 400。这不是少报，是「至少 400」—— 标记亮着，文本行尾也注明了，
    读者不会把它当真值。
    """
    store = EventStore(tmp_path / "trimmed.db")
    now = time.time()
    _seed_red_lines(store, 500, start=now - 3600)
    store.stats = _broken_stats  # type: ignore[method-assign]

    data = build_briefing(store, since=now - 7200)  # 不传 max_items：走默认 20

    limit = _scan_limit(20)
    assert limit == 400, f"前提自检：默认 max_items=20 的扫描上限应为 400，实际算出 {limit}。"
    assert data["events"] == limit, (
        f"事件数只能给列表长度 {limit}（真实 500 条被截掉了）；实际 {data['events']}。"
    )
    assert data["blocked"] == limit, (
        f"blocked 从被截断的列表推出来，只能给 {limit}；实际 {data['blocked']}。"
    )
    assert data["stats_partial"] is True, (
        "列表顶满 scan_limit 且聚合不可用 → 这些数字是下限，标记必须是 True。"
    )

    text = render_briefing(data)
    assert "拦截 0 条" not in text, f"不许把下限说成 0。实际输出：\n{text}"
    assert "拦截 400 条" in text, f"必须报出从列表推出来的 400。实际输出：\n{text}"
    assert "下限" in text, (
        f"文本晨报没有面板那种逐格「≥」，所以必须在计数行注一句「以上为下限」——"
        f"否则读者会把 400 当成真值（真值是 500）。实际输出：\n{text}"
    )
    assert "昨夜平安" not in text, f"统计不完整时只能说「样本内没看到」，不能说「平安」。实际输出：\n{text}"


def test_render_never_falls_back_to_degraded_banner(tmp_path) -> None:
    """反向哨兵：**合法**晨报数据下，``render_briefing`` 不许返回那句降级文案。

    ``render_briefing`` 外层是 ``except Exception`` → 「（晨报生成失败，已降级…）」。
    这个兜底会**把实现自伤伪装成降级**：比如渲染函数里引用了一个未赋值的局部名
    （``NameError``）—— ``py_compile`` 查不出、``ruff`` 查不出、而断言「输出里有某个
    子串」的测试也可能照样绿，因为读者只看到一句话，看不到它为什么是那句。

    所以这里不检查「输出了什么」，只检查「有没有掉进兜底」：一旦渲染自己炸了，
    下面每一条都会红，而不是安静地把整屏晨报换成一句道歉。
    """
    now = time.time()
    store = EventStore(tmp_path / "banner.db")
    empty = build_briefing(store, since=now - 3600)

    _seed_red_lines(store, 3, start=now - 3500)
    populated = build_briefing(store, since=now - 3600)

    broken = FakeStore(
        stats_error=RuntimeError("database is locked"),
        events=_event_rows(_scan_limit(1), start=now - 3600),
    )
    partial = build_briefing(broken, since=now - 3600, max_items=1)
    assert partial["stats_partial"] is True, "前提自检：这个样本必须是 partial，否则测不到 partial 的渲染分支。"

    cases: dict[str, dict[str, Any]] = {
        "空账本": empty,
        "有红线": populated,
        "统计不完整": partial,
        "空字典": {},
        "脏键": {"highlights": [{}], "by_severity": None},
    }
    for name, data in cases.items():
        text = render_briefing(data)
        assert _DEGRADED_BANNER not in text, (
            f"场景「{name}」的渲染掉进了兜底文案 —— 说明 _render_lines 自己抛了异常，"
            f"不是数据不好看。实际输出：\n{text}"
        )
        # 只看「没出现那句兜底」还不够：兜底文案以后被改成别的措辞时，上一条会静默失效，
        # 而这里不依赖任何措辞 —— `_render_lines` 恒以标题行开头，标题缺席就说明
        # 整个渲染被跳过了（不管是哪句文案替的）。
        assert "哨兵晨报" in text, (
            f"场景「{name}」的输出里没有标题行「哨兵晨报」，说明渲染没跑到底、被兜底替换了。"
            f"实际输出：\n{text}"
        )
        assert text.strip(), f"场景「{name}」渲染出空文本；晨报至少要有一行结论。"


class _SwallowGroupByStore(EventStore):
    """真实账本，只让「按严重度分组」那条 COUNT 被吞掉。

    用来复现真实可达的半坏状态：``EventStore.stats`` 里四条 COUNT 是**分开跑**的，
    ``_read`` 又是**按查询**吞 sqlite 错误（``store.py:252-270``），所以「这一条查询
    返回空、那一条照常」是可能的。``_read`` 的签名与真实实现逐条对齐。
    """

    def _read(self, sql: str, params: Iterable[Any] = (), *, strict: bool = False) -> list[dict[str, Any]]:
        """只吞事件分组那条查询，其余原样交给父类。

        :param sql: SQL。
        :param params: 参数。
        :param strict: 为真时异常外抛（本替身不拦异常，只拦「返回空」这种行为）。
        :return: 结果行。
        """
        if "GROUP BY kind, severity" in sql:
            return []
        return super()._read(sql, params, strict=strict)


def test_blocked_agrees_with_by_severity_when_one_count_is_swallowed(tmp_path) -> None:
    """只有事件分组那条 COUNT 被吞时，``blocked`` 必须等于 ``by_severity`` 推出的值。

    真实可达路径（不是构造出来的假 store）：``stats()`` 的四条 COUNT 分开跑，
    ``_read`` 按查询吞 sqlite 错误。于是可以出现

    * ``stats["events"] = 0``、``stats["by_severity"] = {}``、``stats["blocked"] = 0``（**假 0**）；
    * 而 ``timeline`` 照常成功 → ``by_severity`` 被 ``_severity_distribution`` 重算成
      ``{"red_line": 5}``。

    此时若 ``blocked`` 还去照抄 ``stats["blocked"]``，同一个 dict 里就会同时存在
    「by_severity 说 5 条红线」和「blocked 说 0」。下游随即裂开：文本晨报因为
    ``_render_lines`` 有 ``or red_line + standard`` 兜底而报 5，面板直接读 ``blocked``
    而报 0 —— **同一份数据两个说法**。``blocked`` 只能有 ``by_severity`` 一个来源。
    """
    store = _SwallowGroupByStore(tmp_path / "half.db")
    now = time.time()
    _seed_red_lines(store, 5, start=now - 3600)

    half_stats = store.stats(since=now - 7200)
    assert half_stats["blocked"] == 0 and half_stats["by_severity"] == {}, (
        f"前提自检：这条查询被吞后，真实 stats() 应当带回假 0；实际 {half_stats['blocked']!r} / "
        f"{half_stats['by_severity']!r}。自检红了说明吞查询的姿势不对，后面的断言没意义。"
    )

    data = build_briefing(store, since=now - 7200)

    assert data["by_severity"] == {"red_line": 5}, (
        f"stats 的 by_severity 空掉时必须从事件行重算；实际 {data['by_severity']}。"
    )
    assert data["blocked"] == 5, (
        f"blocked 必须与 by_severity 说的一致（5），不许照抄 stats 里的假 0；实际 {data['blocked']}。"
        "同一个 dict 里两个字段互相矛盾时，文本晨报与面板会各报一个数 —— 一份数据两个说法。"
    )

    text = render_briefing(data)
    assert "拦截 5 条" in text, f"文本晨报必须报 5 条拦截。实际输出：\n{text}"
    assert "拦截 0 条" not in text, f"文本晨报不许报假 0。实际输出：\n{text}"


def test_dirty_timeline_rows_are_skipped_not_raised() -> None:
    """时间线里混进非 dict 行时，晨报不许抛：脏行跳过、好行照计，并标成部分统计。

    这是 ``build_briefing`` 那句「不因数据形状抛异常」的兑现。不滤的话崩点会**随数据漂移**：
    ``timeline=[None]`` 崩在 sessions 回退、带 sessions 时崩在 ``_as_highlight``、
    ``alerts=[None]`` 崩在 ``_pick_highlights`` —— 三个不同的函数，没有单一位置包得住。

    丢行之所以要置 ``stats_partial``：它和「截断」「读失败」是同一种事实（数字偏小），
    而这三种里只有丢行连日志都没有。不标就等于安静地少报。
    """
    good = _event_rows(1, start=time.time() - 60)[0]
    store = FakeStore(stats=_zero_stats(), events=[None, good, 3])

    data = build_briefing(store, since=time.time() - 3600, max_items=1)

    assert data["events"] == 1, (
        f"脏行必须被跳过、好行照常计入（期望 1 条）；实际 {data['events']}。"
        "如果这里变成 3，说明脏行被当成了有效行；如果是 0，说明好行被连带丢了。"
    )
    assert data["stats_partial"] is True, (
        "丢了行却报「统计完整」，等于让数字安静地偏小 —— 这正是 stats_partial 要防的事。"
    )


def test_dirty_alert_rows_do_not_break_count_from_stats() -> None:
    """告警列表里有脏行时：不抛、计数仍取 ``stats`` 的精确值、但整份统计标成部分。

    计数与列表是两回事：``alerts`` 这个数字来自 ``stats()["alerts"]``（SQL COUNT），
    脏行只影响列表（也就是 ``highlights`` 的取材），所以数字该是精确的 3。
    但既然有行被丢，就不能声称「统计完整」—— 标记亮的是「这次统计不完整」，
    不是「这个数字一定错」。
    """
    stats = dict(_zero_stats())
    stats.update({"events": 7, "by_severity": {"red_line": 2}, "alerts": 3, "by_level": {"warn": 3}})
    store = FakeStore(
        stats=stats,
        events=_event_rows(7, start=time.time() - 3600),
        alerts=[None],
    )

    data = build_briefing(store, since=time.time() - 7200, max_items=1)

    assert data["alerts"] == 3, (
        f"告警计数来自 SQL COUNT，不该被列表里的脏行拖成 0；实际 {data['alerts']}。"
    )
    assert data["stats_partial"] is True, (
        "丢行必须让整份统计标成不完整 —— 否则「脏行被跳过」这件事对读者完全不可见。"
    )
