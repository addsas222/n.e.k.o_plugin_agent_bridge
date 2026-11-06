"""安全点加固的回归测试（对应 sec-audit 的 H1 / L2）。

为什么单独一个文件
==================

这两条不是「功能」，而是**审计模块自身变成攻击面**的防线：safepoint 在派发**之前**
就在目标仓库里跑 git，而 git 会读那个仓库自己的 ``.git/config``。一个恶意仓库只要
写两行配置，就能让「安全检查」先替它执行任意程序，而且这条命令既不在 Agent 的
argv 里、也不在任何输出里 —— L1 全是瞎的。

所以这里不只断言「参数传对了」，还要**真的复现一次攻击**：造一个带
``core.fsmonitor`` / 带钩子的临时仓库，先证明这两个口子在**本机**确实会执行
（不带加固的裸 git 命令会 ``touch`` 出标记文件），再断言加固后不再执行。
只断言参数不执行真实命令的测试，改一次参数名就假绿了。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# isort: split
# bridge 在插件仓库内是一方包、在 CI 的检出布局下会被当成三方包；
# 显式切段，两种 cwd 下的 I001 判定就都稳定了。

from bridge.sentinel_core import safepoint as safepoint_module
from bridge.sentinel_core.safepoint import _GIT_HARDENING, SafePointManager

FP = "sess-sp|D:/work/repo"

needs_git = pytest.mark.skipif(
    not safepoint_module._find_git(),
    reason="本机没有 git，安全点功能不可用",
)


# ---------------------------------------------------------------------------
# 造仓库 / 造攻击载荷
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """在仓库里跑一条**不带加固**的 git（测试自己的操作，模拟用户/攻击者）。"""
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _make_repo(base: Path, *, dirty: bool = False) -> Path:
    """建一个正常的小仓库。

    :param base: 父目录。
    :param dirty: 为真时留一个未提交改动，让 safepoint 走 stash 分支。
    :return: 仓库目录。
    """
    repo = base / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "tester")
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    if dirty:
        (repo / "a.txt").write_text("v2\n", encoding="utf-8")
    return repo


def _write_marker_script(path: Path, marker: Path) -> None:
    """写一个「被执行就 touch 标记文件」的脚本。

    Windows 上 ``core.fsmonitor`` 用 ``.cmd``（实测可行）；POSIX 用带 shebang 的
    脚本。两条路径都要能跑，否则这个测试在 CI 上就是假绿。

    :param path: 脚本路径。
    :param marker: 标记文件路径。
    """
    if os.name == "nt":
        path.write_text(f'@echo off\r\necho hit >> "{marker}"\r\necho 1\r\n', encoding="utf-8")
    else:
        path.write_text(f'#!/bin/sh\necho hit >> "{marker}"\nprintf 1\n', encoding="utf-8")
        path.chmod(0o755)


def _write_hook_script(path: Path, marker: Path) -> None:
    """写一个钩子脚本（git 在所有平台上都用 sh 跑钩子）。

    :param path: 钩子路径。
    :param marker: 标记文件路径。
    """
    path.write_text(f'#!/bin/sh\necho hit >> "{marker}"\n', encoding="utf-8")
    path.chmod(0o755)


def _no_spaces(repo: Path) -> None:
    """``core.fsmonitor`` 的值会被 git 按空格切分，路径带空格时攻击载荷本身就废了。

    这种情况下「没执行」不能证明加固有效，属于无效实验，跳过而不是假绿。
    """
    if " " in str(repo):
        pytest.skip("临时目录路径含空格，fsmonitor 载荷无法生效（无法构成有效实验）")


# ---------------------------------------------------------------------------
# H1：目标仓库不能决定我们执行什么程序
# ---------------------------------------------------------------------------


@needs_git
def test_every_git_command_is_hardened(tmp_path, monkeypatch):
    """所有 git 调用都必须带 -c core.fsmonitor=false -c core.hooksPath=。

    这条是「覆盖性」检查：新增一条 git 调用却忘了走 ``_run`` 时，这里会红。
    """
    repo = _make_repo(tmp_path, dirty=True)
    seen: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(argv, **kwargs):
        seen.append([str(item) for item in argv])
        return _Result()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    manager = SafePointManager()
    assert manager.git, "本机没有 git，测试前提不成立"
    manager.create(str(repo), label="开工前", fingerprint=FP)

    assert seen, "没有捕获到任何 git 调用"
    expected = list(_GIT_HARDENING)
    for argv in seen:
        assert argv[1 : 1 + len(expected)] == expected, argv


@needs_git
def test_repo_fsmonitor_cannot_execute(tmp_path):
    """H1 主场景：``core.fsmonitor`` 指向任意程序时，git status 会执行它。"""
    repo = _make_repo(tmp_path, dirty=True)
    _no_spaces(repo)
    marker = repo / "fsmonitor-hit.txt"
    script = repo / "payload.cmd" if os.name == "nt" else repo / "payload.sh"
    _write_marker_script(script, marker)
    _git(repo, "config", "core.fsmonitor", str(script).replace("\\", "/"))

    # 先证明这个口子在本机是真的：裸 git 会执行它。
    _git(repo, "status", "--porcelain")
    if not marker.exists():
        pytest.skip("本机 git 不执行 core.fsmonitor（无法构成有效实验）")
    marker.unlink()

    # 加固后：SafePointManager 里的每条 git 都可能触发 status，一次都不许执行。
    manager = SafePointManager()
    point = manager.create(str(repo), label="开工前", fingerprint=FP)
    assert point is not None
    assert not marker.exists(), "core.fsmonitor 仍然被执行了 —— 加固失效"


@needs_git
def test_repo_hook_cannot_execute(tmp_path):
    """H1 第二条路径：仓库自带的钩子会在改写索引时被调用。"""
    repo = _make_repo(tmp_path, dirty=True)
    marker = repo / "hook-hit.txt"
    hook = repo / ".git" / "hooks" / "post-index-change"
    hook.parent.mkdir(parents=True, exist_ok=True)
    _write_hook_script(hook, marker)

    # 先证明这个口子在本机是真的：裸 git checkout 会跑 post-index-change。
    (repo / "a.txt").write_text("v3\n", encoding="utf-8")
    _git(repo, "checkout", "--", ".")
    if not marker.exists():
        pytest.skip("本机 git 不执行 .git/hooks/post-index-change（无法构成有效实验）")
    marker.unlink()

    # 加固后：restore 会走 checkout -- . / stash apply，都会写索引，但钩子不能跑。
    (repo / "a.txt").write_text("v2\n", encoding="utf-8")
    manager = SafePointManager()
    point = manager.create(str(repo), label="开工前", fingerprint=FP)
    assert point is not None
    (repo / "a.txt").write_text("被改坏了\n", encoding="utf-8")
    assert manager.restore(str(repo), point["ref"]) is True
    assert not marker.exists(), "仓库自带的钩子仍然被执行了 —— 加固失效"


@needs_git
def test_restore_never_checks_out_arbitrary_tree(tmp_path, monkeypatch):
    """恢复路径里不许出现 ``git checkout <ref> -- .``（会触发 smudge 过滤器）。

    只允许 ``git checkout -- .``（索引 → 工作区）这种不涉及任意 tree 的写法。
    """
    repo = _make_repo(tmp_path, dirty=True)
    manager = SafePointManager()
    point = manager.create(str(repo), label="开工前", fingerprint=FP)
    assert point is not None
    (repo / "a.txt").write_text("被改坏了\n", encoding="utf-8")

    calls: list[list[str]] = []
    real_run = subprocess.run

    def _spy(argv, **kwargs):
        calls.append([str(item) for item in argv])
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", _spy)
    assert manager.restore(str(repo), point["ref"]) is True

    checkout_calls = [argv for argv in calls if "checkout" in argv]
    for argv in checkout_calls:
        tail = argv[argv.index("checkout") + 1 :]
        assert tail == ["--", "."], f"出现了按任意 tree 覆盖工作区的写法：{argv}"
    assert not hasattr(SafePointManager, "_checkout_tree"), "第三档兜底应当已被删除"


# ---------------------------------------------------------------------------
# L2：ref 是选项注入面
# ---------------------------------------------------------------------------


@needs_git
def test_restore_rejects_option_like_ref(tmp_path):
    """以 ``-`` 开头的 ref 会被 git 当成选项解析，必须在入口挡掉。"""
    repo = _make_repo(tmp_path, dirty=True)
    manager = SafePointManager()
    assert manager.create(str(repo)) is not None

    for bad in ("-x", "--upload-pack=calc", "--exec=calc", "-", "--", "../etc/passwd", "HEAD", "abc", "z" * 40):
        assert manager.restore(str(repo), bad) is False, bad


@needs_git
def test_restore_refuses_when_ref_is_not_current_head(tmp_path):
    """普通 commit 快照只支持「ref 就是当前 HEAD」；否则明确失败而不是覆盖整棵树。"""
    repo = _make_repo(tmp_path, dirty=False)
    manager = SafePointManager()
    point = manager.create(str(repo))
    assert point is not None and point["mode"] == "head"

    # 打快照之后又提交了一次 → ref 不再等于 HEAD。
    (repo / "b.txt").write_text("new\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "later")
    (repo / "a.txt").write_text("被改坏了\n", encoding="utf-8")

    assert manager.restore(str(repo), point["ref"]) is False
    # 工作区没有被悄悄改写：失败就是失败，不做「尽力而为」的猜测。
    assert (repo / "a.txt").read_text(encoding="utf-8").strip() == "被改坏了"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
