"""provider 清单层的回归测试。

守的是「新增 Agent 只改数据」这条约定 —— 一旦有人把路由又写回代码，或者清单里
漏了字段、id 和目录名对不上、别名撞车，这里就该红。

    python -m pytest tests/test_providers.py -q
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from bridge.providers import load_into
from bridge.registry import (
    AGENT_SPECS,
    ALIASES,
    AgentSpec,
    build_argv,
    canonical_id,
    resolve_agent,
)
from bridge.runner import _detect_auth_failure

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ROOT / "providers"


def provider_dirs() -> list[Path]:
    return sorted(
        path for path in PROVIDERS.iterdir()
        if path.is_dir() and not path.name.startswith((".", "_")) and (path / "provider.toml").is_file()
    )


def manifest_of(directory: Path) -> dict:
    with open(directory / "provider.toml", "rb") as handle:
        return tomllib.load(handle)


# --------------------------------------------------------------------------
# 清单本身
# --------------------------------------------------------------------------


def test_providers_dir_has_at_least_the_three_shipped_routes() -> None:
    names = {path.name for path in provider_dirs()}
    assert {"gemini", "command-code", "pi"} <= names


@pytest.mark.parametrize("directory", provider_dirs(), ids=lambda p: p.name)
def test_manifest_is_well_formed(directory: Path) -> None:
    """每个清单都要能解析、id == 目录名、mode 合法、开关类必须给 prompt_flag。"""
    raw = manifest_of(directory)
    assert raw.get("schema") == 1, f"{directory.name}: schema 必须是 1"

    section = raw["provider"]
    assert section["id"] == directory.name, f"{directory.name}: id 必须等于目录名"
    assert str(section["id"]).islower(), f"{directory.name}: id 必须小写"
    assert section["mode"] in ("flag", "switch", "positional"), f"{directory.name}: mode 非法"
    if section["mode"] in ("flag", "switch"):
        assert section.get("prompt_flag"), f"{directory.name}: mode={section['mode']} 必须声明 prompt_flag"
    assert section.get("binary") or section["id"]
    # 每个新 Agent 都必须自带失败特征：全局表只覆盖最初 9 个 CLI 的措辞。
    # 两种取其一即可 —— auth_patterns（去登录）或 fail_patterns（去起服务）。
    has_patterns = bool(section.get("auth_patterns") or section.get("fail_patterns"))
    timeout_only = section.get("failure_mode") == "timeout"
    assert has_patterns or timeout_only, (
        f"{directory.name}: 必须声明 auth_patterns / fail_patterns，"
        f'或 failure_mode="timeout"（无输出、只能靠超时判定）'
    )


@pytest.mark.parametrize("directory", provider_dirs(), ids=lambda p: p.name)
def test_every_provider_is_registered(directory: Path) -> None:
    assert directory.name in AGENT_SPECS, f"{directory.name} 没被合并进 AGENT_SPECS"
    spec = AGENT_SPECS[directory.name]
    assert spec.mode in ("flag", "switch", "positional")
    if spec.mode in ("flag", "switch"):
        assert spec.prompt_flag


def test_aliases_do_not_collide() -> None:
    """别名与 id 必须全局唯一，否则 canonical_id 的行为取决于字典插入顺序。"""
    ids = set(AGENT_SPECS)
    seen: dict[str, str] = {}
    for alias, target in ALIASES.items():
        assert alias not in ids, f"别名 {alias!r} 同时是一个 Agent id（指向 {target}）"
        assert alias not in seen, f"别名 {alias!r} 被 {seen[alias]} 与 {target} 同时占用"
        assert target in ids, f"别名 {alias!r} 指向未登记的 {target}"
        seen[alias] = target


# --------------------------------------------------------------------------
# 路由解析与 argv 拼装
# --------------------------------------------------------------------------


def test_switch_mode_appends_prompt_as_positional() -> None:
    """`pi -p "<task>"`：-p 是布尔开关，正文是位置参数。"""
    spec = AGENT_SPECS["pi"]
    assert spec.mode == "switch"
    resolved = resolve_agent(spec)
    argv = build_argv(spec, resolved, "TASK")
    assert argv[0].lower().endswith(("pi.exe", "pi")) or argv[0].endswith("pi")
    assert "-p" in argv
    assert argv[-1] == "TASK"
    assert argv.index("-p") < argv.index("TASK")


def test_flag_mode_keeps_prompt_next_to_flag() -> None:
    spec = AGENT_SPECS["gemini"]
    assert spec.mode == "flag"
    resolved = resolve_agent(spec)
    argv = build_argv(spec, resolved, "TASK")
    index = argv.index("-p")
    assert argv[index + 1] == "TASK"


def test_pi_is_not_omp() -> None:
    """回归：`pi` 曾经是 omp 的别名，等于「按 pi 跑 omp」。"""
    assert canonical_id("pi") == "pi"
    assert canonical_id("ompi") == "omp"
    assert AGENT_SPECS["pi"].binary != AGENT_SPECS["omp"].binary


def test_builtin_agents_are_untouched() -> None:
    """provider 层只做增量，内置 9 个的调用形态必须原样。"""
    expected = {
        "codebuddy": ("flag", "-p"),
        "claude": ("flag", "-p"),
        "dsh": ("positional", None),
        "omp": ("flag", "-p"),
        "evox": ("flag", "-p"),
        "opencode": ("positional", None),
        "openclaw": ("positional", None),
        "atomcode": ("flag", "-p"),
        "qwenpaw": ("flag", "-i"),
    }
    for agent_id, (mode, prompt_flag) in expected.items():
        spec = AGENT_SPECS[agent_id]
        assert spec.mode == mode, f"{agent_id} 的 mode 被改了"
        assert spec.prompt_flag == prompt_flag, f"{agent_id} 的 prompt_flag 被改了"


# --------------------------------------------------------------------------
# 鉴权识别
# --------------------------------------------------------------------------


def test_gemini_auth_text_is_recognised_by_its_own_patterns() -> None:
    """实测原文（退出码 41）。全局表认不出，必须靠清单自带的 auth_patterns。"""
    stderr = (
        r"Please set an Auth method in your C:\Users\user\.gemini\settings.json or specify "
        "one of the following environment variables before running: GEMINI_API_KEY, "
        "GOOGLE_GENAI_USE_VERTEXAI, GOOGLE_GENAI_USE_GCA"
    )
    assert _detect_auth_failure("", stderr, AGENT_SPECS["gemini"].auth_patterns)


def test_command_code_auth_text_is_recognised_by_its_own_patterns() -> None:
    """实测原文（退出码 3）。注意是 "Authentication failed"，全局表里只有 "required"。"""
    stderr = 'Error: Authentication failed. Please run "cmdc login" first.'
    assert _detect_auth_failure("", stderr, AGENT_SPECS["command-code"].auth_patterns)


def test_legit_output_is_not_mistaken_for_login_failure() -> None:
    """真跑完任务时不能误判 —— 否则一个正常结果会被当成「没登录」。"""
    stderr = "Error: Authentication failed. Please run \"cmdc login\" first."
    stdout = "我已经把 README 里的拼写问题列出来了，共 3 处，见上面的清单。"
    assert _detect_auth_failure(stdout, stderr, AGENT_SPECS["command-code"].auth_patterns) == ""


# --------------------------------------------------------------------------
# 冲突与漂移
# --------------------------------------------------------------------------


def test_duplicate_id_is_skipped_with_warning(tmp_path: Path) -> None:
    """与内置同名的 provider 必须被跳过 + 告警，绝不悄悄改掉 claude 的调用方式。"""
    directory = tmp_path / "claude"
    directory.mkdir()
    (directory / "provider.toml").write_text(
        "\n".join(
            [
                "schema = 1",
                "[provider]",
                'id = "claude"',
                'label = "假的"',
                'binary = "definitely-not-claude"',
                'mode = "flag"',
                'prompt_flag = "-x"',
                'auth_patterns = ["nope"]',
            ]
        ),
        encoding="utf-8",
    )
    specs = {"claude": AgentSpec(id="claude", label="真", binary="claude", mode="flag", prompt_flag="-p")}
    aliases: dict[str, str] = {}
    warnings = load_into(specs, aliases, root=str(tmp_path), spec_factory=AgentSpec)

    assert specs["claude"].binary == "claude", "内置 Agent 被 provider 覆盖了"
    assert any("同名" in item for item in warnings), f"没有留下告警：{warnings}"


def test_alias_conflict_is_dropped_not_silently_shadowed(tmp_path: Path) -> None:
    directory = tmp_path / "newbie"
    directory.mkdir()
    (directory / "provider.toml").write_text(
        "\n".join(
            [
                "schema = 1",
                "[provider]",
                'id = "newbie"',
                'binary = "newbie"',
                'mode = "flag"',
                'prompt_flag = "-p"',
                'aliases = ["claude", "nb"]',
                'auth_patterns = ["nope"]',
            ]
        ),
        encoding="utf-8",
    )
    specs: dict[str, AgentSpec] = {
        "claude": AgentSpec(id="claude", label="Claude", binary="claude", mode="flag", prompt_flag="-p")
    }
    aliases: dict[str, str] = {}
    warnings = load_into(specs, aliases, root=str(tmp_path), spec_factory=AgentSpec)

    assert "newbie" in specs
    assert aliases == {"nb": "newbie"}, f"冲突别名没被丢掉：{aliases}"
    assert any("claude" in item for item in warnings)


def test_skill_provider_copy_is_in_sync() -> None:
    """skill 里的 providers/ 是生成副本，必须与源一致（否则装出去的 skill 会缺路由）。"""
    script = ROOT / "skill" / "neko-bridge" / "sync_providers.py"
    result = subprocess.run(
        [sys.executable, str(script), "--check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, (
        f"providers 副本漂移：\n{result.stdout}\n{result.stderr}\n"
        f"跑 python skill/neko-bridge/sync_providers.py --apply 修好"
    )
