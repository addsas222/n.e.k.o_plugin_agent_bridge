"""进程干预 / 安全点 / 纠偏提示的测试。

这三块是「派发前拦截」之外的另一半能力：出了事之后怎么止血、怎么退回去、
怎么把话说给 Agent 听。它们都要在真实系统上动真东西（进程、git），所以测试
自己必须负责清理——漏掉一个 sleep 进程就能把整个测试套件挂住。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

# isort: split
# bridge 在插件仓库内是一方包、在 CI 的检出布局下会被当成三方包；
# 显式切段，两种 cwd 下的 I001 判定就都稳定了。

from bridge.sentinel_core.guard import ProcessGuard
from bridge.sentinel_core.inject import build_correction_prompt, build_resume_hint
from bridge.sentinel_core.safepoint import SafePointManager
from bridge.sentinel_core.store import EventStore

FP = "sess-guard|D:/work/demo"

#: 本机没装 git 时跳过所有涉及真实仓库的用例。
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="本机没有 git")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _dead_pid() -> int:
    """拿一个确定已经退出的 PID（用真实子进程，不靠猜数字）。"""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _git(cwd, *args: str) -> subprocess.CompletedProcess[str]:
    """在临时仓库里跑一条 git 命令。"""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def _make_repo(tmp_path, *, dirty: bool) -> "os.PathLike[str]":
    """造一个最小的、已提交过一次的仓库；``dirty`` 时额外改一个文件。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "sentinel@example.com")
    _git(repo, "config", "user.name", "sentinel")
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "init")
    if dirty:
        (repo / "a.txt").write_text("v2\n", encoding="utf-8")
    return repo


# ---------------------------------------------------------------------------
# ProcessGuard：能力自述与探活
# ---------------------------------------------------------------------------


def test_capabilities_reports_booleans():
    caps = ProcessGuard().capabilities()
    # 子集断言而不是精确相等：`identity_check`（进程身份校验是否可用）是后加的第
    # 五位，以后再加能力位时也不该让这条用例假崩——它要保证的是「这四位存在且是
    # 布尔」，不是「只有这四位」。
    assert {"suspend", "resume", "terminate", "tree_kill"} <= set(caps)
    assert all(isinstance(value, bool) for value in caps.values())
    # 任何平台都有硬杀手段，这一位必须是 True，否则上层连「按掉」都做不了。
    assert caps["terminate"] is True


def test_capabilities_is_stable_across_calls():
    guard = ProcessGuard()
    assert guard.capabilities() == guard.capabilities()


def test_is_alive_for_self_and_dead_pid():
    guard = ProcessGuard()
    assert guard.is_alive(os.getpid()) is True
    assert guard.is_alive(_dead_pid()) is False


def test_is_alive_rejects_nonsense_pid():
    guard = ProcessGuard()
    assert guard.is_alive(0) is False
    assert guard.is_alive(-1) is False
    assert guard.is_alive(None) is False
    assert guard.is_alive("not-a-pid") is False


# ---------------------------------------------------------------------------
# ProcessGuard：失败必须是 False，而不是异常
# ---------------------------------------------------------------------------


def test_interventions_on_missing_pid_return_false():
    guard = ProcessGuard()
    gone = _dead_pid()
    assert guard.suspend(gone) is False
    assert guard.resume(gone) is False
    assert guard.terminate(gone) is False


def test_refuses_to_intervene_on_itself():
    """对自己动手会把插件和宿主一起冻住，必须拒绝。"""
    guard = ProcessGuard()
    assert guard.suspend(os.getpid()) is False
    assert guard.resume(os.getpid()) is False
    assert guard.terminate(os.getpid()) is False


def test_win_api_load_failure_does_not_raise(monkeypatch):
    """ntdll 打不开（受限容器 / 精简映像）时必须静默降级。"""
    import ctypes

    from bridge.sentinel_core import guard as guard_module

    if sys.platform != "win32":
        pytest.skip("只在 Windows 上验证这条降级路径")

    api = guard_module._WinApi()

    def _boom(*args, **kwargs):
        raise OSError("找不到 ntdll")

    monkeypatch.setattr(ctypes, "WinDLL", _boom)
    api.load()
    assert api.ready is False
    assert api.open_process(os.getpid(), 0x1000) is None
    assert api.exit_code(os.getpid()) is None
    assert api.terminate(os.getpid()) is False
    assert api.nt_call("NtSuspendProcess", 1) is False


def test_missing_windows_api_degrades_to_false(monkeypatch):
    """整条 Windows API 链不可用时，四个动作都只能是 False。"""
    from bridge.sentinel_core import guard as guard_module

    if sys.platform != "win32":
        pytest.skip("只在 Windows 上验证这条降级路径")

    # 故意不调 load()，模拟「一个绑都没绑上」的状态。
    monkeypatch.setattr(guard_module, "_WIN_API", guard_module._WinApi())
    guard = ProcessGuard()
    gone = _dead_pid()
    caps = guard.capabilities()
    assert caps["suspend"] is False
    assert caps["resume"] is False
    assert guard.is_alive(gone) is False
    assert guard.suspend(gone) is False
    assert guard.resume(gone) is False
    assert guard.terminate(gone, tree=False) is False


# ---------------------------------------------------------------------------
# ProcessGuard：对真实子进程动手
# ---------------------------------------------------------------------------


def test_suspend_resume_then_terminate_real_child():
    guard = ProcessGuard()
    caps = guard.capabilities()
    if not (caps["suspend"] and caps["resume"]):
        pytest.skip("这台机器缺少挂起/恢复能力")

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert guard.is_alive(child.pid) is True

        assert guard.suspend(child.pid) is True
        # 挂起只是停住线程，进程本身还在。
        assert guard.is_alive(child.pid) is True

        assert guard.resume(child.pid) is True
        assert guard.is_alive(child.pid) is True

        assert guard.terminate(child.pid, grace=5.0) is True
        assert guard.is_alive(child.pid) is False
        assert child.wait(timeout=10) is not None
    finally:
        # 无论上面哪一步失败，都不许留下一个占着 30 秒的 sleep 进程。
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_terminate_works_on_suspended_process():
    """挂起状态下也要能按掉——否则「挂起后询问」的策略会把自己锁死。"""
    guard = ProcessGuard()
    if not (guard.capabilities()["suspend"] and guard.capabilities()["terminate"]):
        pytest.skip("这台机器缺少挂起或终止能力")

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert guard.suspend(child.pid) is True
        assert guard.terminate(child.pid, grace=5.0) is True
        assert guard.is_alive(child.pid) is False
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


# ---------------------------------------------------------------------------
# SafePointManager：非 git 目录一律放弃
# ---------------------------------------------------------------------------


def test_non_git_directory_has_no_safepoint(tmp_path):
    manager = SafePointManager()
    assert manager.is_git_repo(str(tmp_path)) is False
    assert manager.create(str(tmp_path), label="开工前") is None
    # 不存在的目录同样不能炸
    assert manager.is_git_repo(str(tmp_path / "nope")) is False
    assert manager.create("") is None


# ---------------------------------------------------------------------------
# SafePointManager：git 仓库
# ---------------------------------------------------------------------------


@needs_git
def test_safepoint_snapshots_dirty_tree_and_records_it(tmp_path):
    repo = _make_repo(tmp_path, dirty=True)
    store = EventStore(tmp_path / "sentinel.db")
    manager = SafePointManager(store)

    assert manager.is_git_repo(str(repo)) is True
    point = manager.create(str(repo), label="开工前", fingerprint=FP)
    assert point is not None
    assert point["kind"] == "git"
    # 有改动 → 用 stash create 的游离 commit，而不是 HEAD。
    assert point["mode"] == "stash"
    assert len(point["ref"]) == 40
    assert point["files"] == 1
    assert point["label"] == "开工前"

    # 关键红线：create 不能改动工作区，也不能往 stash 列表里塞东西。
    assert (repo / "a.txt").read_text(encoding="utf-8").strip() == "v2"
    assert _git(repo, "stash", "list").stdout.strip() == ""

    latest = store.latest_safepoint(fingerprint=FP)
    assert latest is not None
    assert latest["ref"] == point["ref"]
    assert latest["files"] == 1


@needs_git
def test_restore_brings_files_back_and_marks_restored(tmp_path):
    repo = _make_repo(tmp_path, dirty=True)
    store = EventStore(tmp_path / "sentinel.db")
    manager = SafePointManager(store)
    point = manager.create(str(repo), label="开工前", fingerprint=FP)
    assert point is not None

    (repo / "a.txt").write_text("被 Agent 改坏了\n", encoding="utf-8")
    (repo / "b.txt").write_text("新增文件\n", encoding="utf-8")

    assert manager.restore(str(repo), point["ref"], safepoint_id=point["id"]) is True
    assert (repo / "a.txt").read_text(encoding="utf-8").strip() == "v2"
    # 未跟踪的新文件不在快照里，回滚不删它——这是有意的取舍。
    assert (repo / "b.txt").exists()
    # 已回滚的安全点不再是「最近可回滚点」。
    assert store.latest_safepoint(fingerprint=FP) is None

    # apply 而不是 pop：同一个快照可以再用一次。
    (repo / "a.txt").write_text("又改坏了\n", encoding="utf-8")
    assert manager.restore(str(repo), point["ref"]) is True
    assert (repo / "a.txt").read_text(encoding="utf-8").strip() == "v2"


@needs_git
def test_safepoint_falls_back_to_head_when_tree_is_clean(tmp_path):
    repo = _make_repo(tmp_path, dirty=False)
    manager = SafePointManager()
    point = manager.create(str(repo))
    assert point is not None
    assert point["mode"] == "head"
    assert point["files"] == 0
    # 没 store 时不记账，也不该多出一个 id 字段。
    assert "id" not in point

    (repo / "a.txt").write_text("改坏了\n", encoding="utf-8")
    assert manager.restore(str(repo), point["ref"]) is True
    assert (repo / "a.txt").read_text(encoding="utf-8").strip() == "v1"


@needs_git
def test_restore_rejects_bad_ref_without_raising(tmp_path):
    repo = _make_repo(tmp_path, dirty=True)
    manager = SafePointManager()
    assert manager.restore(str(repo), "0" * 40) is False
    assert manager.restore(str(repo), "") is False
    assert manager.restore(str(tmp_path), "0" * 40) is False


@needs_git
def test_create_on_repo_without_commits_returns_none(tmp_path):
    """空仓库：既没有改动也没有 HEAD，只能返回 None，不能编一个 ref 出来。"""
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    manager = SafePointManager()
    assert manager.is_git_repo(str(repo)) is True
    assert manager.create(str(repo)) is None


# ---------------------------------------------------------------------------
# inject：纠偏提示
# ---------------------------------------------------------------------------


def test_correction_prompt_names_rule_and_evidence():
    prompt = build_correction_prompt(
        {
            "rule_id": "catastrophic.root_delete",
            "reason": "删除文件系统根 /，整机数据不可恢复",
            "evidence": "rm -rf / --no-preserve-root",
        }
    )
    assert "catastrophic.root_delete" in prompt
    assert "rm -rf / --no-preserve-root" in prompt
    assert "停止" in prompt
    assert len(prompt.splitlines()) <= 15


def test_correction_prompt_advice_differs_by_rule():
    delete = build_correction_prompt({"rule_id": "catastrophic.root_delete"})
    disk = build_correction_prompt({"rule_id": "catastrophic.disk_wipe"})
    remote = build_correction_prompt({"rule_id": "catastrophic.remote_exec"})
    permission = build_correction_prompt({"rule_id": "dangerous.chmod_recursive"})

    # 删除类：先确认目标
    assert "ls" in delete
    # 抹盘类：不要碰设备
    assert "块设备" in disk
    # 下载即执行：先落盘读一遍
    assert "远程内容" in remote
    # 权限类：别提权
    assert "sudo" in permission
    # 四组建议必须互不相同，否则分类逻辑白写
    assert len({delete, disk, remote, permission}) == 4


def test_correction_prompt_never_invents_goal():
    without = build_correction_prompt({"rule_id": "dangerous.rm_rf"})
    assert "你的原始目标" not in without

    with_goal = build_correction_prompt({"rule_id": "dangerous.rm_rf"}, goal="修好登录页的校验逻辑")
    assert "修好登录页的校验逻辑" in with_goal
    assert len(with_goal.splitlines()) <= 15


def test_correction_prompt_flattens_and_truncates_evidence():
    prompt = build_correction_prompt(
        {"rule_id": "dangerous.rm_rf", "evidence": "第一行\n第二行\t带制表符"},
        agent="codebuddy",
    )
    assert "codebuddy" in prompt
    assert "第一行 第二行 带制表符" in prompt

    long_prompt = build_correction_prompt({"rule_id": "dangerous.rm_rf", "evidence": "x" * 500})
    assert "…" in long_prompt
    assert max(len(line) for line in long_prompt.splitlines()) < 400


def test_correction_prompt_survives_empty_verdict():
    prompt = build_correction_prompt({})
    assert "未命名规则" in prompt
    assert len(prompt.splitlines()) <= 15


def test_correction_prompt_accepts_duck_typed_verdict():
    """非 dict 也要能喂（``Verdict`` 数据类、适配层的 ``LineEvent`` 都是这种）。

    这里用本地替身而不是 import 适配层的 ``LineEvent``：字段名与它一致
    （ts/kind/text/raw），但不把本测试绑到别人模块的演进上。缺 ``rule_id``
    时只能退化成「未命名规则」，绝不能因为取字段就炸。
    """
    from dataclasses import dataclass

    @dataclass
    class _Event:
        ts: float
        kind: str
        text: str
        raw: str = ""

    prompt = build_correction_prompt(_Event(ts=0.0, kind="error", text="命令返回非零退出码", raw="Error: EACCES"))
    assert "未命名规则" in prompt
    assert len(prompt.splitlines()) <= 15


# ---------------------------------------------------------------------------
# inject：恢复提示
# ---------------------------------------------------------------------------


def test_resume_hint_without_safepoint():
    assert "没有可用安全点" in build_resume_hint(None)
    assert "没有可用安全点" in build_resume_hint({})


def test_resume_hint_uses_stash_apply_for_stash_snapshots():
    hint = build_resume_hint({"kind": "git", "ref": "a" * 40, "mode": "stash", "files": 3, "cwd": "D:/repo"})
    assert "stash apply " + "a" * 40 in hint
    assert "3 个改动文件" in hint
    assert "D:/repo" in hint


def test_resume_hint_uses_checkout_for_head_snapshots():
    hint = build_resume_hint({"kind": "git", "ref": "b" * 40, "mode": "head"})
    assert "checkout " + "b" * 40 + " -- ." in hint


@pytest.mark.parametrize("mode", ["head", "stash"])
def test_resume_hint_warns_about_smudge_filters(mode):
    """提示里必须写明「这条命令会跑仓库指定的过滤器」，并给出不经滤器的退路。

    只在 hint 里给一条会触发 smudge 的命令，等于告诉用户「照这个做」——而 smudge
    正是我们自己拒绝自动执行的那条路径。
    """
    hint = build_resume_hint({"kind": "git", "ref": "c" * 40, "mode": mode})
    assert "smudge" in hint
    assert "git show " + "c" * 40 + ":<路径>" in hint
