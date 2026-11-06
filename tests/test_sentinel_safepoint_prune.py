"""安全点 ref 回收的回归测试（code-reviewer C3）。

每次派发都会在**用户仓库**里留一个 ``refs/sentinel/sp-*``。这些 ref 会挡住
``git gc`` 回收对象，而 ``store.prune()`` 只清表、不碰 git —— 长期下来用户的仓库里
会攒出成千上万个引用，且没有任何入口能删掉。这里钉死三件事：

1. 老 ref 与超量 ref 会被真的 ``git update-ref -d`` 掉（不是只删账本记录）；
2. 账本里对应的记录跟着删，不留悬空行；
3. ``create()`` 里的自动清理**按小时节流**，且失败绝不影响建安全点。
"""

from __future__ import annotations

import subprocess
import time

import pytest

# isort: split
# bridge 在插件仓库内是一方包、在 CI 的检出布局下会被当成三方包；
# 显式切段，两种 cwd 下的 I001 判定就都稳定了。

from bridge.sentinel_core import safepoint as safepoint_module
from bridge.sentinel_core.safepoint import SafePointManager
from bridge.sentinel_core.store import EventStore

FP = "sess-prune|D:/work/repo"
DAY = 86400.0

needs_git = pytest.mark.skipif(
    not safepoint_module._find_git(),
    reason="本机没有 git，安全点功能不可用",
)


def _git(repo, *args: str) -> subprocess.CompletedProcess:
    """在仓库里跑一条（不带加固的）git —— 模拟用户/测试自己的操作。"""
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _make_repo(base):
    """建一个有一个提交的小仓库。"""
    repo = base / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "tester")
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    return repo


def _add_ref(repo, epoch: int, suffix: str) -> str:
    """按我们的命名规则造一个 ref（名字里带时间戳）。"""
    name = f"refs/sentinel/sp-{int(epoch)}-{suffix:0>8}"
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", name, head)
    return name


def _refs(repo) -> list[str]:
    out = _git(repo, "for-each-ref", "--format=%(refname)", "refs/sentinel/").stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# prune 本体
# ---------------------------------------------------------------------------


@needs_git
def test_prune_deletes_old_refs_and_store_rows(tmp_path):
    repo = _make_repo(tmp_path)
    store = EventStore(tmp_path / "sentinel.db")
    manager = SafePointManager(store)

    old_a = _add_ref(repo, time.time() - 40 * DAY, "aaaaaaa1")
    old_b = _add_ref(repo, time.time() - 31 * DAY, "bbbbbbb2")
    fresh = _add_ref(repo, time.time(), "ccccccc3")
    for ref in (old_a, old_b, fresh):
        store.add_safepoint(kind="git", ref=ref, label="x", fingerprint=FP)

    removed = manager.prune(str(repo), max_age_days=30, keep=50)

    assert removed == 2
    assert _refs(repo) == [fresh], "老 ref 必须被真的删掉，而不是只删账本记录"
    # 账本里对应记录一起清，不留悬空行。
    # ``older_than=now+1`` 的语义是「比此刻更早的全都算」= 列出全部记录。
    remaining = {row["ref"] for row in store.safepoints(older_than=time.time() + 1)}
    assert remaining == {fresh}
    # 反向确认：按 30 天前的界去查过期记录，应该一条都没有了。
    assert store.safepoints(older_than=time.time() - 30 * DAY) == []


@needs_git
def test_prune_keeps_newest_by_count(tmp_path):
    repo = _make_repo(tmp_path)
    manager = SafePointManager()
    now = time.time()
    names = [_add_ref(repo, now - index, f"dddddd{index}") for index in range(4)]

    removed = manager.prune(str(repo), max_age_days=3650, keep=2)

    assert removed == 2
    assert sorted(_refs(repo)) == sorted(names[:2]), "保留的应该是最新的 2 个"


@needs_git
def test_prune_caps_work_per_run(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(safepoint_module, "_PRUNE_MAX_PER_RUN", 1)
    manager = SafePointManager()
    for index in range(3):
        _add_ref(repo, time.time() - (index + 100) * DAY, f"eeeeee{index}")

    assert manager.prune(str(repo), max_age_days=30, keep=0) == 1
    assert len(_refs(repo)) == 2, "单轮不许无限删（会把一次派发卡死）"


def test_prune_without_cwd_is_noop(tmp_path):
    store = EventStore(tmp_path / "sentinel.db")
    manager = SafePointManager(store)
    # 没有仓库路径就无法安全地 update-ref：宁可不动。
    assert manager.prune(None) == 0


@needs_git
def test_prune_ignores_foreign_ref_names(tmp_path):
    repo = _make_repo(tmp_path)
    manager = SafePointManager()
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # 不合我们命名规则（没有 sp-<epoch>-）的 ref 不许动，哪怕它很老。
    _git(repo, "update-ref", "refs/sentinel/custom-name", head)

    assert manager.prune(str(repo), max_age_days=1, keep=0) == 0
    assert _refs(repo) == ["refs/sentinel/custom-name"]


# ---------------------------------------------------------------------------
# create() 里的自动清理
# ---------------------------------------------------------------------------


@needs_git
def test_create_prunes_at_most_once_per_interval(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, )
    calls: list[str] = []

    def _count(self, cwd=None, **_kwargs):
        calls.append(str(cwd))
        return 0

    monkeypatch.setattr(SafePointManager, "prune", _count)
    manager = SafePointManager()

    manager.create(str(repo))
    manager.create(str(repo))
    assert len(calls) == 1, "同一次进程里两次 create 只该清理一次"

    # 把节流时间戳清零 → 下一次 create 又会清理。
    manager._last_prune = 0.0
    manager.create(str(repo))
    assert len(calls) == 2


@needs_git
def test_prune_failure_does_not_break_create(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)

    def _boom(self, cwd=None, **_kwargs):
        raise RuntimeError("清理炸了")

    monkeypatch.setattr(SafePointManager, "prune", _boom)
    manager = SafePointManager()

    point = manager.create(str(repo), label="开工前", fingerprint=FP)
    assert point is not None, "清理失败不能让建安全点失败"
    assert point["ref"]


@needs_git
def test_prune_actually_runs_on_first_create(tmp_path):
    """默认路径（不 monkeypatch）：第一次 create 会顺手清掉过老的 ref。"""
    repo = _make_repo(tmp_path)
    stale = _add_ref(repo, time.time() - 400 * DAY, "fffffff9")

    point = SafePointManager().create(str(repo))

    assert point is not None
    assert stale not in _refs(repo), "过期 ref 应该在建安全点时被回收"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
