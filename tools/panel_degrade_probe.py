#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证面板降级路径：数据层整段炸掉时，入口清单是否还完整给出去。

    python tools/panel_degrade_probe.py          # 五种故障场景，全过则 exit 0

为什么需要这个探针：`tests/_sdk_stub.py` 明写「测试从不实例化插件类」，`conftest.py`
也把插件根注册成只有 ``__path__`` 的命名空间包 —— 所以 ``_ui_panel_state`` 那层
**降级兜底在 pytest 里永远测不到**。这里把插件 ``__init__.py`` 当独立模块加载
（它自带 SDK stub 兜底分支），再用假 ``self`` 绑到未装饰的函数上。

盯死这一件事：``build_panel_state`` 挂掉时 ``entries`` 必须仍是真清单。
``FALLBACK_ENTRIES`` 那 4 条恰好是非哨兵的 4 个，所以 entries 一旦被连带搞空，
蒸发的正是这次要做的 ``sentinel_*`` 四个 —— 面板会表现成「这插件没有哨兵功能」。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_plugin_module():
    """按 pytest 的 standalone 方式加载插件根 ``__init__.py``。"""
    spec = importlib.util.spec_from_file_location("_ab_probe", ROOT / "__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # spec.parent 给的是名字本身而不是 ''，会让插件算出 _STANDALONE=False；
    # 显式置空才能走那条自带 stub 兜底的分支。
    module.__package__ = ""
    sys.modules["_ab_probe"] = module
    spec.loader.exec_module(module)
    return module


PLUGIN = _load_plugin_module()

#: 假的入口清单。刻意包含 4 个 sentinel_* —— 它们是降级时最容易被连带搞没的那批。
ENTRIES = [
    {"id": i, "name": n, "description": "", "timeout": 1800,
     "has_params": True, "has_required": False}
    for i, n in [
        ("agents_list", "列出可用 Agent"),
        ("agent_run", "派发任务给 Agent"),
        ("agent_run_parallel", "并行派发多个任务"),
        ("agent_doctor", "诊断桥接链路"),
        ("sentinel_status", "哨兵状态"),
        ("sentinel_briefing", "哨兵简报"),
        ("sentinel_timeline", "哨兵时间线"),
        ("sentinel_rollback", "回滚工作区"),
    ]
]

REAL_LOADER = PLUGIN._load_panel_module
REAL_MODULE = REAL_LOADER()


def _panel_module(build_panel_state):
    """拼一个「长得像 panel 模块」的替身：只有 ``_ui_panel_state`` 用到的那两个成员。"""
    return types.SimpleNamespace(
        build_panel_state=build_panel_state,
        degraded_state=REAL_MODULE.degraded_state,
    )


class FakeSelf:
    """只提供 ``_ui_panel_state`` 真正用到的那四个成员。"""

    def __init__(self, entries, *, config_raises=False):
        self._entries = entries
        self._sentinel = None
        self._config_raises = config_raises
        self.logged: list[tuple[str, str]] = []

    def _entry_rows(self):
        return self._entries

    def _log(self, level, message):
        self.logged.append((level, message))

    async def _config(self):
        if self._config_raises:
            raise RuntimeError("模拟配置读不到")
        return types.SimpleNamespace(
            sentinel=types.SimpleNamespace(guard_action="alert")
        )


def invoke(entries, *, break_build, config_raises=False):
    """把面板模块换成指定的破坏方式，然后跑一次面板。

    ``break_build`` 取值：``raise``（聚合抛异常）/ ``non-dict``（返回错类型）/
    ``no-module``（连模块都导不进来，只剩最后兜底）/ ``real``（正常）。
    """
    fake = FakeSelf(entries, config_raises=config_raises)
    if break_build == "raise":
        def boom(*args, **kwargs):
            raise RuntimeError("模拟数据层整段炸掉")
        PLUGIN._load_panel_module = lambda: _panel_module(boom)
    elif break_build == "non-dict":
        PLUGIN._load_panel_module = lambda: _panel_module(lambda *a, **k: ["不是 dict"])
    elif break_build == "no-module":
        def no_import():
            raise ImportError("模拟包结构被破坏，panel 模块导不进来")
        PLUGIN._load_panel_module = no_import
    else:
        PLUGIN._load_panel_module = REAL_LOADER
    try:
        state = asyncio.run(PLUGIN.AgentBridgePlugin._ui_panel_state(fake))
    finally:
        PLUGIN._load_panel_module = REAL_LOADER
    return state, fake


def describe(label, state, fake):
    ids = [e["id"] for e in state.get("entries") or []]
    sentinel_ids = [i for i in ids if i.startswith("sentinel_")]
    sentinel = state.get("sentinel") or {}
    print(f"[{label}]")
    print(f"  entries={len(ids)}  sentinel_*={len(sentinel_ids)}")
    print(f"  sentinel.state={sentinel.get('state')!r}  guard={sentinel.get('guard')!r}")
    print(f"  sessions={state.get('sessions')!r}  alerts={state.get('alerts')!r}  timeline={state.get('timeline')!r}")
    for level, message in fake.logged:
        print(f"  log[{level}] {message}")
    return ids, sentinel_ids


def main() -> int:
    failures: list[str] = []

    def check(condition, message):
        if not condition:
            failures.append(message)

    print("=== 1. 正常路径（真 build_panel_state，store=None）===")
    state, fake = invoke(ENTRIES, break_build="real")
    ids, sentinel_ids = describe("normal", state, fake)
    check(len(ids) == 8, "正常路径 entries 不是 8 条")
    check(not fake.logged, f"正常路径不该有 warning：{fake.logged}")

    print("\n=== 2. build_panel_state 抛异常 ===")
    state, fake = invoke(ENTRIES, break_build="raise")
    ids, sentinel_ids = describe("raise", state, fake)
    check(len(ids) == 8, f"降级后 entries 丢了：{len(ids)}")
    check(len(sentinel_ids) == 4, f"四个 sentinel_* 入口在降级态蒸发了：只有 {sentinel_ids}")
    check(state["sessions"] is None, "降级态 sessions 必须是 None，不能是 []（那是另一种事实）")
    check(state["sentinel"]["state"] != "ok", "降级态不能报 ok，否则前端会去读编出来的数字")
    check(state["sentinel"]["guard"] == "alert", "配置读到了就该照实显示干预动作")
    check(state["sentinel"]["guard_enabled"] is False, "降级态 guard_enabled 应为 False")
    check(state["sentinel"]["guard_policy"] == "", "降级态 guard_policy 应为空串")
    check(bool(fake.logged), "降级必须留一条 warning 日志")

    print("\n=== 3. build_panel_state 返回非 dict ===")
    state, fake = invoke(ENTRIES, break_build="non-dict")
    ids, sentinel_ids = describe("non-dict", state, fake)
    check(len(ids) == 8, f"返回非 dict 时 entries 丢了：{len(ids)}")
    check(state["sessions"] is None, "返回非 dict 也应走同一条降级路径")

    print("\n=== 4. 入口清单本身就取不到 ===")
    state, fake = invoke([], break_build="raise")
    ids, sentinel_ids = describe("no-entries", state, fake)
    check(state["entries"] == [], "取不到就该是空清单，不能是 None")

    print("\n=== 5. _config() 也炸（配置读不到）===")
    state, fake = invoke(ENTRIES, break_build="raise", config_raises=True)
    ids, sentinel_ids = describe("boom-config", state, fake)
    check(len(ids) == 8, "配置炸了不该连带把 entries 搞没")
    check(state["sentinel"]["guard"] == "",
          "读不到配置时 guard 必须是空串（前端据此显示「未知」而不是「已开启干预」）")
    check(len(fake.logged) == 2, f"应各留一条 warning，实际 {len(fake.logged)} 条")

    print("\n=== 6. panel 模块整个导不进来（最后兜底）===")
    state, fake = invoke(ENTRIES, break_build="no-module")
    ids, sentinel_ids = describe("no-module", state, fake)
    check(len(ids) == 8, f"最后兜底仍要保住 entries：{len(ids)}")
    check(len(sentinel_ids) == 4, "最后兜底也要保住四个 sentinel_* 入口")
    # 导不进来时**不许编形状**：编一份 sentinel 块等于假装自己知道状态。
    # 前端对缺失字段本来就有兜底（state.sentinel 可缺），少给比给假的诚实。
    check("sentinel" not in state, "导不进来时不该编一个 sentinel 块出来")
    check("stats" not in state, "导不进来时不该编统计数字")
    check(bool(fake.logged), "最后兜底必须留 warning 日志")

    print()
    if failures:
        print(f"FAILED ({len(failures)})")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
