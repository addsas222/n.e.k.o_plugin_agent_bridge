"""桌面插件包 + Harness 矩阵 + WebUI 的回归测试。

守住三件容易静默坏掉的事：
1. `frameworks.toml` 的结构（写错一个字段，install.py 会把 skill 装到奇怪的地方）；
2. `.mcpb` 的包结构（manifest 少一个字段，客户端直接拒绝安装，且不告诉你原因）；
3. WebUI 的派发入口对非法 agent 的处理（不能抛异常，要回结构化错误）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_frameworks_matrix_is_well_formed() -> None:
    with open(ROOT / "skill" / "neko-bridge" / "frameworks.toml", "rb") as handle:
        rows = tomllib.load(handle)["harness"]

    assert len(rows) >= 40, f"矩阵太小，疑似被截断：{len(rows)}"

    ids = [row["id"] for row in rows]
    assert len(ids) == len(set(ids)), f"存在重复 id：{sorted({i for i in ids if ids.count(i) > 1})}"

    for row in rows:
        assert row["kind"] in ("cli", "ide", "desktop", "web", "server", "generic"), f"{row['id']}: kind 非法"
        assert row.get("mcp_kind", "none") in ("cli", "file", "none"), f"{row['id']}: mcp_kind 非法"
        assert row.get("source") in ("local", "docs"), f"{row['id']}: 必须标注 source"
        if row.get("mcp_kind") == "cli":
            assert "{name}" in row.get("mcp_command", ""), f"{row['id']}: mcp_command 缺 {{name}} 占位"
            assert "{bin}" in row.get("mcp_command", ""), f"{row['id']}: mcp_command 缺 {{bin}} 占位"
        if row.get("mcp_kind") == "file":
            assert row.get("mcp_target"), f"{row['id']}: mcp_kind=file 但没给 mcp_target"
        directory = row.get("skills_dir", "")
        if directory:
            # 空串是合法的（表示该框架没有 skills 机制），但非空就必须是绝对/家目录路径，
            # 否则 install.py 会相对当前目录写文件。
            assert directory.startswith(("~", "/")) or ":" in directory, f"{row['id']}: skills_dir 必须是绝对路径"


def test_every_installed_cli_harness_has_a_route() -> None:
    """本机**真装了**的 CLI harness 必须有一条可派发路由。

    判据用 `shutil.which`，不是靠一张"已知不接"的清单 —— 那样装了新东西也不会被发现。
    这里抓过一次真缺口：codex 有 ~/.codex 配置目录，但 CLI 并不在 PATH 上；
    反过来，某天 `codex` 真装上了，这条会立刻红。
    """
    import shutil

    with open(ROOT / "skill" / "neko-bridge" / "frameworks.toml", "rb") as handle:
        rows = tomllib.load(handle)["harness"]
    from bridge.registry import canonical_id

    missing: list[str] = []
    for row in rows:
        if row["kind"] != "cli":
            continue
        # 用 canonical_id 而不是直接查 id：宿主目录名常与路由 id 不一致
        # （`~/.commandcode` vs 路由 `command-code`），别名也是合法入口。
        if canonical_id(row["id"]):
            continue
        if shutil.which(row["id"]):
            missing.append(row["id"])
    assert not missing, (
        f"这些 CLI 在本机 PATH 上但还没有路由：{missing}。"
        f"要么加 providers/<id>/provider.toml，要么把 frameworks.toml 的 kind 改掉。"
    )


def test_mcpb_bundle_builds_and_validates(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("build_mcpb", ROOT / "tools" / "build_mcpb.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    target = tmp_path / "agent_bridge.mcpb"
    module.build(target)
    assert target.is_file() and target.stat().st_size > 10_000
    assert module.check(target) == [], module.check(target)


def test_vscode_extension_manifest_matches_code() -> None:
    """VS Code 扩展：package.json 里声明的命令必须真的被 registerCommand。

    这个错法很典型 —— `contributes.commands` 写了但没 register，或者反过来；
    两边都不会报错，只是命令面板里按下去没反应。所以用桩 `vscode` 跑一遍 activate。
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过扩展冒烟")
    plugin = ROOT / "plugins" / "vscode"
    result = subprocess.run(
        [node, str(plugin / "smoke.js")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_webui_inline_script_runs() -> None:
    """把 index.html 的内联脚本放进 DOM 桩里真跑一遍。

    语法检查抓不到 `ReferenceError: renderDetail is not defined` 这类问题 ——
    只有执行到那一行才炸，而炸了以后整页按钮全哑。这条就是那个网。
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过 WebUI 冒烟")
    result = subprocess.run(
        [node, str(ROOT / "webui" / "smoke.js")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_inventory_scan_is_read_only_and_sane() -> None:
    """只读清点：不改任何东西，输出可解析，且不把 Android/Compatibility 认成 harness。"""
    import subprocess

    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "inventory.py"), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert isinstance(data["software"], list)
    assert isinstance(data["hits"], dict)
    # 词边界匹配的回归：这几个曾被误认成 harness
    for wrong in ("omp", "droid"):
        evidence = " ".join(data["hits"].get(wrong, []))
        assert "Compatibility" not in evidence and "Android" not in evidence


def test_inventory_classifier_word_boundaries() -> None:
    """分类器必须按词边界匹配。

    回归：`omp` 曾命中 "C**omp**atibility"、`pen` 命中 "O**pen** Design"、
    `droid` 命中 "An**droid**"，把无关软件认成 harness，缺口统计因此失真。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("inventory", ROOT / "tools" / "inventory.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    assert module.classify("AstrBot") == "astrbot"
    assert module.classify("AstrBot Launcher") == "astrbot"
    assert module.classify("Application Compatibility Toolkit") == ""
    assert module.classify("Open Design") == ""
    assert module.classify("Microsoft.NET.Sdk.Android.Manifest") == ""
    assert module.classify("Obsidian") == ""


def test_webui_dispatch_rejects_unknown_agent() -> None:
    spec = importlib.util.spec_from_file_location("webui_server", ROOT / "webui" / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    result = module.dispatch("definitely-not-an-agent", "x", "", 0)
    assert result["ok"] is False
    assert "未知" in result["error"]

    empty = module.dispatch("gemini", "   ", "", 0)
    assert empty["ok"] is False
