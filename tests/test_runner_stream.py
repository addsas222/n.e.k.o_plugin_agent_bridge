"""stderr 不能被 stdout 的读线程饿死 —— 「静默假成功」的回归测试。

为什么单独立一个文件
====================

这条用例保护的不是适配层，也不是 ``_PipeReader`` 的单元行为，而是
``run_agent`` 的**结果正确性**：一段被吞掉的 stderr 会让未登录的 CLI 变成
``ok=True``。放在 runner 的流式行为这一层，才盯得住它。

失效链条（修复前）
==================

``_execute_blocking`` 的宽限期是**两个读线程共享一个 deadline**：

* 父进程派生一个继承 stdout/stderr 的孙进程后立刻退出，孙进程继续握着两个写端；
* ``out_reader.join(remaining)`` 因此等满整个 ``stream_grace``；
* 轮到 ``err_reader`` 时 ``remaining <= 0``，**join 被跳过**，``err_reader.data`` 为空；
* ``_match_head`` 的 ``if not text`` 直接短路 → 不做鉴权判定；
* ``needs_login=False``、``error=""``、``exit_code=0`` → ``ok=True``。

而 ``_detect_auth_failure`` 的注释自己就写着：这类 CLI 未登录时**退出码是 0**、
只往 stderr 打一行。所以这条竞态恰好把它存在的唯一目的废掉 —— 用户看到「成功」，
实际一行活都没干。

为什么是 5.5 秒
===============

``run_agent`` 用的是生产默认 ``_STREAM_GRACE = 5.0``，孙进程必须在**宽限期之后**
才写 stderr 才能复现；而修复后 ``err_reader`` 有自己的一份预算，只要写成**宽限期
的 1.1 倍**（5.5s）就一定落在它自己的预算内。这个区间是唯一的：小于 5.0 复现不了，
大于 10.0 修复后也等不到。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# isort: split
# bridge 在插件仓库内是一方包、在 CI 的检出布局下会被当成三方包；
# 显式切段，两种 cwd 下的 I001 判定就都稳定了。

from bridge.config import BridgeConfig
from bridge.registry import AgentSpec, ResolvedCommand
from bridge.runner import (
    _NOISE_RE,
    _PROGRESS_LINE_RE,
    _PROGRESS_MAX_CHARS,
    _detect_auth_failure,
    run_agent,
)

LOGIN_LINE = "Authentication required. Please use /login command to sign in"

#: neko.py 是独立分发的单文件（不能 import bridge），但带着同一套鉴权判定逻辑。
_NEKO_PATH = (
    Path(__file__).resolve().parents[1] / "skill" / "neko-bridge" / "bin" / "neko.py"
)


def _load_neko_module():
    """按路径加载 neko.py，返回模块本身。

    不能用 import 语句：文件名不是合法包名，且脚本顶层有 argparse 之外的自检，
    所以用 importlib 按文件加载。加载期间要临时把 ``bin/`` 放进 ``sys.path``：
    neko.py 会 ``from provider_manifest import discover``（同目录随 skill 分发），
    不补路径就只会在测试里 ModuleNotFoundError —— 那是环境差异，不是它的问题。
    """
    bin_dir = str(_NEKO_PATH.parent)
    added = bin_dir not in sys.path
    if added:
        sys.path.insert(0, bin_dir)
    try:
        spec = importlib.util.spec_from_file_location("_neko_bin_for_test", _NEKO_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if added:
            sys.path.remove(bin_dir)
    return module


def _load_neko():
    """取 ``detect_needs_login``（大多数用例只需要这一个函数）。"""
    return _load_neko_module().detect_needs_login


_FAKE_CLI = '''\
"""假 CLI：父进程派生孙进程后立刻退出，孙进程握着写端晚一步才说「没登录」。"""
import subprocess
import sys
import time

DELAY = {delay}
MESSAGE = {message!r}

if len(sys.argv) > 1 and sys.argv[1] == "--grandchild":
    time.sleep(DELAY)          # 晚于 runner 的宽限期才写，制造竞态
    sys.stderr.write(MESSAGE + "\\n")
    sys.stderr.flush()
    raise SystemExit(0)

# 孙进程不重定向任何东西，于是它继承了这两个管道的写端
subprocess.Popen([sys.executable, __file__, "--grandchild"])
# 正文故意写成「可识别的进度形态」（``working…``）：判据是形态，不是长度。
# 用一句真实结论会让鉴权判定被正确地跳过，本用例就会因为另一个原因通过，
# 掩盖真正要测的「stderr 有没有被截断」。
# 注意本模板要过 ``str.format``，注释里不能出现未转义的花括号。
print("working…")
'''


@pytest.mark.asyncio
async def test_stderr_not_truncated_when_stdout_holds_pipe(tmp_path):
    """stdout 被孙进程占住时，stderr 里的登录提示仍必须被读到。"""
    fake = tmp_path / "fake_cli.py"
    fake.write_text(_FAKE_CLI.format(delay=5.5, message=LOGIN_LINE), encoding="utf-8")

    spec = AgentSpec(id="fake", label="Fake CLI", binary="fake", mode="flag", prompt_flag="-p")
    resolved = ResolvedCommand(sys.executable, argv_prefix=[str(fake)], source="test", exists=True)
    config = BridgeConfig(default_timeout_seconds=60, max_timeout_seconds=120)

    result = await run_agent(spec, resolved, "打个招呼", config=config, cwd=str(tmp_path))

    # 先钉住「stderr 被读到了」——这是失败时的第一现场，比 ok 判定更好定位
    assert result.stderr.strip() == LOGIN_LINE, (
        f"stderr 被截断了（读线程预算被 stdout 吃光）：{result.stderr!r}"
    )
    assert result.needs_login is True
    assert result.ok is False, "未登录的 CLI 必须判成失败，绝不能是假成功"
    assert "AGENT_NEEDS_LOGIN" in (result.error or "")


# ---------------------------------------------------------------------------
# 第二道闸门：stdout 里有一行进度提示，不能把鉴权判定整个废掉
# ---------------------------------------------------------------------------

#: 真实 CLI 未登录时经常先打一句这种进度行（23 字符，远超旧的 8 字符行阈值）。
_PROGRESS_ONLY_STDOUT = "reading src/app.py now"

#: 一段「真的干完活了」的正文（多行、有结论、有代码）。
#:
#: 用长正文而不是一句短话，是为了让这条用例测的是「误报有没有发生」，而不是
#: 「判据的具体数值是多少」—— 判据以后换成别的形态它会照旧成立，不会莫名变红。
_REAL_STDOUT = "\n".join(
    [
        "读完了 input.csv，一共 3 行，下面是整理后的结果：",
        "",
        "def main():",
        "    data = load('input.csv')",
        "    for row in data:",
        "        print(row['name'], row['score'])",
        "    return 0",
        "",
        "if __name__ == '__main__':",
        "    raise SystemExit(main())",
        "",
        "改动要点：",
        "1. 把 load() 的返回值改成显式校验，空文件直接返回 0，不再抛 KeyError；",
        "2. 打印改成 f-string，避免和 locale 有关的编码问题；",
        "3. main() 的返回值透传给 SystemExit，退出码因此能反映真实状态。",
        "",
        "跑了一遍 tests/test_app.py，12 passed，没有回归。",
    ]
)

#: **短**但确实是结果的正文。这条是判据的关键分界：
#:
#: 它 19 个字符，和 ``Thinking…``（9 字符）落在同一个长度量级 —— 任何「累计长度」
#: 阈值都必然在其中一侧判错（调高 → 短答案被当成没干活 → 该 Agent 进 needs_login
#: 名单后被永久排除；调低 → 进度提示被当成产出 → 未登录报成成功）。所以判据看的是
#: **行的形态**，而这句必须被判成「有实质产出」。
_SHORT_REAL_STDOUT = "int main(){return 0;}"


def test_auth_detected_when_stdout_only_has_progress_line():
    """stdout 只有一行进度提示 + stderr 是完整登录提示 → 必须判「需要登录」。

    这条失效**不依赖时序**：旧判据是「stdout 任一行 ≥ 8 字符就算有实质产出」，
    一句 ``Thinking…`` 就能击穿它，于是鉴权特判被跳过、`ok=True` 假成功。
    """
    assert _detect_auth_failure(_PROGRESS_ONLY_STDOUT, LOGIN_LINE) == LOGIN_LINE


def test_auth_not_flagged_when_stdout_has_real_output():
    """反向保护：真的干完活了就不该被误报成未登录。"""
    assert _detect_auth_failure(_REAL_STDOUT, LOGIN_LINE) == ""
    assert _detect_auth_failure(_SHORT_REAL_STDOUT, LOGIN_LINE) == "", "短答案也是答案"


@pytest.mark.parametrize(
    "stdout",
    [
        "编译失败：3 个 error，2 个 warning",
        "npm install 完成，添加 3 个包",
        "npm run build 通过，产物在 dist/",
        "warning 级别有 2 条，都在 vendor 里",
        "3 个测试失败，0 个通过",
    ],
)
def test_real_conclusions_are_not_mistaken_for_no_output(stdout):
    """真实结论的外形千奇百怪，但只要它是一句话，就不能被判成「没干活」。

    这一组钉的是噪声判据的**方向**：只有「整行本身就是一条警告」才算噪声，不能是
    「以某个词开头」，更不能是「提到了那些词」。曾经用前缀表（``"npm "`` /
    ``"node:"``）加子串判断（``"warning" in line``），于是上面这几行都被吃掉；若它们
    恰好是唯一一行，判定就变成「没产出」→ 误报未登录 → 该 Agent 被拉黑。
    """
    assert _detect_auth_failure(stdout, LOGIN_LINE) == ""


@pytest.mark.parametrize(
    "stdout",
    [
        "(node:1234) Warning: experimental loader",
        # Node 实际打的是 ``ExperimentalWarning``：按 ``warning`` 起头会漏，噪声表失效
        "(node:1234) ExperimentalWarning: stream/web is an experimental feature",
        "(node:1234) DeprecationWarning: Buffer() is deprecated",
        "npm warn deprecated left-pad@1.0.0",
        # npm 的样板尾部：每次失败都有，放它算产出则「未登录→npm 报错」永远判不出
        "npm ERR! A complete log of this run can be found in:",
        "npm ERR!     at Object.<anonymous> (x.js:1)",  # 栈帧，不是结论
        "WARNING: PowerShell profile failed to load",
        "at foo (bar.js:12)",
    ],
)
def test_bare_warning_banners_are_still_treated_as_noise(stdout):
    """反向：整行本身就是一条告警横幅时仍然算噪声。

    收窄判据不能收到「什么都不算噪声」——否则未登录的 CLI 先打一行 Node 告警就能把
    鉴权特判整个废掉，那是另一种失效。
    """
    assert _detect_auth_failure(stdout, LOGIN_LINE) != ""


def test_neko_py_agrees_with_bridge_on_the_same_scenario():
    """neko.py 是同一套逻辑的第二份独立实现，两份必须给出同一答案。

    一份修了、另一份没修，比两份都错更难查 —— 所以这里用同一组场景对照。
    场景刻意覆盖判据的**两侧边界**：进度提示（要判）与短答案（不许判）落在同一个
    长度量级上，只靠长度分不开它们。
    """
    detect_needs_login = _load_neko()
    cases = [
        (_PROGRESS_ONLY_STDOUT, LOGIN_LINE, True),      # 一行进度提示
        ("Thinking…", LOGIN_LINE, True),                # 9 字符，旧闸门被它击穿
        ("● Thinking…", LOGIN_LINE, True),              # 带状态标记前缀
        ("working…", LOGIN_LINE, True),                 # 可识别的进度形态
        ("done", LOGIN_LINE, False),                    # 单个短词不算进度：判不准时偏向「有产出」
        ("ok", LOGIN_LINE, False),                      # 同上：短答案不是进度
        ("WARNING: profile failed to load", LOGIN_LINE, True),   # 噪声行本身不算产出
        ("编译失败：3 个 error，2 个 warning", LOGIN_LINE, False),  # 提到 warning 的结论仍是结论
        ("npm ERR! code E404", LOGIN_LINE, False),       # npm 的**真错误**是结论，不是噪声
        ("npm ERR! A complete log of this run can be found in:", LOGIN_LINE, True),  # 样板行是噪声
        ("", LOGIN_LINE, True),                         # stdout 空
        ("   ", LOGIN_LINE, True),                      # 只有空白
        (_SHORT_REAL_STDOUT, LOGIN_LINE, False),        # 19 字符的**真答案**
        (_REAL_STDOUT, LOGIN_LINE, False),              # 多行正文
        (_PROGRESS_ONLY_STDOUT, "", False),             # stderr 没有鉴权措辞
    ]
    for stdout, stderr, expected in cases:
        bridge_verdict = bool(_detect_auth_failure(stdout, stderr))
        neko_verdict = bool(detect_needs_login("codebuddy", stdout, stderr))
        assert bridge_verdict == neko_verdict, f"两份实现不一致：stdout={stdout!r}"
        assert bridge_verdict is expected, f"判错了：stdout={stdout!r} stderr={stderr!r}"


def test_shared_regexes_are_literally_identical():
    """两份实现的**正则字面量**必须一致，而不是「恰好给出同一答案」。

    上面那条场景对照有一个它抓不到的盲区：**两边一起判错时它仍然通过** —— 它只证明
    两条路径彼此一致，不证明这个答案对。规格表（``_SUBSTANTIVE_SPEC``）补的是「答案
    对不对」，但规格表只跑 bridge 那一侧。

    所以这里再钉一条更窄的约束：``_NOISE_RE`` / ``_PROGRESS_LINE_RE`` 的 ``pattern``
    与 ``flags`` 逐字相同，阈值常量相等。**注释可以各写各的，正则不行。** 这条曾经真的
    踩过：``_has_substantive_output`` 补了 ANSI 剥离，neko.py 没补，同一段彩色进度提示
    在两边给出不同答案。
    """
    neko = _load_neko_module()
    for name, bridge_re, neko_re in (
        ("_NOISE_RE", _NOISE_RE, neko._NOISE_RE),
        ("_PROGRESS_LINE_RE", _PROGRESS_LINE_RE, neko._PROGRESS_LINE_RE),
    ):
        assert bridge_re.pattern == neko_re.pattern, f"{name} 的 pattern 两边不一致"
        assert bridge_re.flags == neko_re.flags, f"{name} 的 flags 两边不一致"
    assert _PROGRESS_MAX_CHARS == neko._PROGRESS_MAX_CHARS, "_PROGRESS_MAX_CHARS 两边不一致"

