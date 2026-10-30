"""AstrBot 插件的「规范符合性」测试：桩掉 astrbot 包，按宿主自己的规则验收。

AstrBot 对插件只有两条硬要求（见 `astrbot/core/star/updater.py` 与 `star_manager.py`）：
  1. `metadata.yaml` 必须含 name / desc / version / author，且都是字符串；
  2. 主类必须继承 `astrbot.api.star.Star`，或类名以 `Plugin` 结尾、或叫 `Main`。

这两条错了，插件在面板里表现为「加载失败」或干脆不出现 —— 而本机没有 AstrBot 运行环境，
所以用桩把规则本身验一遍，至少保证这份插件不会因为格式问题被拒。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "astrbot"

REQUIRED_METADATA = ("name", "desc", "version", "author")


def _install_stubs() -> None:
    """注入最小 astrbot 包：只够 import 插件，不模拟运行时。"""
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")

    class Star:
        def __init__(self, context=None):
            self.context = context

    class Context:  # noqa: D401 - 占位类型
        pass

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Star = Star
    star_mod.Context = Context

    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:  # noqa: D401 - 占位类型
        def __init__(self, message_str=""):
            self.message_str = message_str

        def plain_result(self, text):
            return text

    calls: list[str] = []

    class Filter:
        def command(self, name):
            def decorator(func):
                calls.append(name)
                func._astr_command = name
                return func

            return decorator

        class PermissionType:
            ADMIN = "admin"

        def permission_type(self, _kind):
            def decorator(func):
                return func

            return decorator

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = Filter()

    api.star = star_mod
    api.event = event_mod
    astrbot.api = api

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.event"] = event_mod


def _load_plugin():
    _install_stubs()
    spec = importlib.util.spec_from_file_location("astrbot_agent_bridge", PLUGIN / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_metadata_has_required_fields() -> None:
    import tomllib  # noqa: F401  —— 仅确保标准库可用

    import yaml  # type: ignore[import-not-found]

    metadata = yaml.safe_load((PLUGIN / "metadata.yaml").read_text(encoding="utf-8"))
    for field in REQUIRED_METADATA:
        assert field in metadata, f"metadata.yaml 缺 {field}"
        assert isinstance(metadata[field], str), f"metadata.yaml 的 {field} 必须是字符串"


def test_main_class_follows_astrbot_rule() -> None:
    module = _load_plugin()
    assert hasattr(module, "Main"), "插件主类必须叫 Main（或继承 Star / 以 Plugin 结尾）"
    main = module.Main(object())
    assert getattr(main.bridge, "_astr_command", "") == "bridge"
    assert getattr(main.bridge_status, "_astr_command", "") == "bridge_status"


def test_reply_is_clipped() -> None:
    module = _load_plugin()
    long_text = "x" * 5000
    clipped = module._clip(long_text)
    assert len(clipped) < len(long_text)
    assert "已截断" in clipped
    assert module._clip("short") == "short"


def test_dispatch_requires_console(monkeypatch) -> None:
    """控制台没起时必须回人话，而不是抛栈。"""
    module = _load_plugin()

    def boom(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(module, "_request", boom)

    import asyncio

    async def run():
        agent = module.Main(object())
        event = types.SimpleNamespace(message_str="/bridge_status", plain_result=lambda t: t)
        out = []
        async for chunk in agent.bridge_status(event):
            out.append(chunk)
        return "".join(out)

    text = asyncio.run(run())
    assert "连不上" in text
    assert "webui/server.py" in text


@pytest.mark.parametrize("command", ["/bridge", "/bridge_status"])
def test_commands_are_declared(command: str) -> None:
    module = _load_plugin()
    names = {
        getattr(obj, "_astr_command", "")
        for obj in vars(module.Main).values()
        if callable(obj)
    }
    assert command.lstrip("/") in names
