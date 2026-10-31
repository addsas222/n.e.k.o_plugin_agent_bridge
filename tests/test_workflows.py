"""守住 workflow 的结构：发布前必须有测试门禁。

为什么值得一条测试：`release.yml` 是 neko-plugin 的**模板生成文件**，头部写着
`managed-template`。用工具重新生成时会覆盖本地改动 —— 而"门禁被悄悄删掉"
是最难发现的一类退化：流水线照样绿，只是不再拦。

这里不引 PyYAML（CI 不装第三方包），直接用行级正则断言骨架，
只盯那几行关键结构，不做通用 YAML 解析。
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
RELEASE = WORKFLOWS / "release.yml"


def _read(path: Path) -> str:
    assert path.is_file(), f"缺少 workflow：{path}"
    return path.read_text(encoding="utf-8")


def test_release_is_gated_on_tests() -> None:
    """release 必须 `needs: tests`，且 tests job 真的跑 pytest。

    不用 `uses:` 复用的理由写在 release.yml 里：复用路径只有 tag 触发时才被执行，
    而 tag 就是发布 —— 发布流水线不该留未经执行的间接层。
    """
    text = _read(RELEASE)
    assert re.search(r"^  tests:\s*$", text, re.MULTILINE), "release.yml 缺 tests job"
    assert re.search(
        r"^    needs: tests\s*$", text, re.MULTILINE
    ), "release job 必须 needs: tests —— 否则测试不绿也会发布"
    assert "python -m pytest tests -q" in text, "tests job 没有真的跑 pytest"


def test_ci_runs_on_tags() -> None:
    """main 与 tag 都要跑测试，PR 也要。"""
    text = _read(CI)
    assert re.search(r"^    branches: \[main\]\s*$", text, re.MULTILINE), "ci.yml 缺 branches"
    assert re.search(
        r'^    tags: \["v\*"\]\s*$', text, re.MULTILINE
    ), 'ci.yml 的 push 触发要包含 tags: ["v*"]'
    assert re.search(r"^  pull_request:", text, re.MULTILINE), "ci.yml 缺 pull_request"


def test_both_workflows_pin_same_versions() -> None:
    """两处测试依赖必须完全一致。

    `filterwarnings = ["error"]` 会把插件自身的 DeprecationWarning 升级成
    INTERNALERROR；不钉版本时 CI 装到的 pytest-asyncio 与本地不是同一套，
    表现就是"本地绿、CI 红"。内联两份步骤就必然有重复，重复的危险是漂移 ——
    所以这里直接比对两边的 pin 集合。
    """
    pins: dict[str, list[str]] = {}
    for name, path in (("ci", CI), ("release", RELEASE)):
        text = _read(path)
        found = re.findall(r'"(pytest(?:-asyncio)?==[0-9.]+)"', text)
        assert found, f"{name}.yml 没有钉版本"
        pins[name] = sorted(found)
    assert pins["ci"] == pins["release"], f"两处 pins 不一致：{pins}"
