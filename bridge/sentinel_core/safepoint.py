"""安全点 —— 在 Agent 动手之前留一份「退得回去」的快照。

为什么有这个模块
================

护栏能拦住命令，却拦不住「已经改坏的代码」。Agent 跑偏时最常见的损失不是删了
根目录（那是红线层的事），而是它把半个仓库改乱了、删了几个自己新建的文件，
然后说「我以为你已经改好了」。所以派发前要给每个会话留一个明确可回退的点。

为什么基于 git，而不是打包文件
==============================

Agent 的破坏性操作绝大多数发生在 git 工作区里，而 git 本身已经提供了完整的
快照能力——比我们自己 tar 一遍可靠得多，也不会把 ``node_modules``、
``.venv``、``dist`` 之类的东西卷进来（仓库的 ``.gitignore`` 已经替我们筛过了）。

代价是：**非 git 目录没有安全点**。这是有意的取舍——文件级备份的范围和风险
（漏文件、备份本身撑爆磁盘、恢复时覆盖用户新写的东西）远大于它能救回的损失。
``is_git_repo`` 返回 ``False`` 时上层应当直接跳过回滚策略，改成纯告警。

关键实现细节
============

* ``git stash create`` 而不是 ``git stash push``：前者只创建一个**游离 commit**
  并打印 hash，**不动工作区、不往 stash 列表里塞东西**。后者会把用户正在改的
  东西真的收走，那本身就是一次破坏。
* 游离 commit 会被 ``git gc`` 按
  ``gc.reflogExpireUnreachable``（默认 2 周）清掉，所以额外用
  ``git update-ref refs/sentinel/sp-<id>`` 给它挂一个引用。只新增 ref，
  不碰任何已有分支。
* 恢复用 ``git stash apply`` 而不是 ``pop``：apply 保留快照，同一个安全点可以
  反复回滚；pop 用完就没了，一旦回滚后又被跑坏就没有第二发子弹。
* 快照**不包含未跟踪文件**：``git stash create`` 的天性如此。这既是限制也是
  保护——回滚不会删掉 Agent 新建的文件，用户还能自己挑。

安全边界（重要，别误读）
========================

**本模块会在目标仓库里执行 git，而 git 必然读取该仓库自己的 ``.git/config``。**
这意味着一个恶意仓库可以让「审计模块」在 Agent 还没启动时先执行它自带的代码，
而且这条命令既不出现在 Agent 的 argv 里、也不出现在任何输出里 —— L1 预检看不到。
两条已被实测复现的路径：

* ``core.fsmonitor=<任意程序>``：``git status --porcelain`` 会执行它；
* ``.git/hooks/post-index-change``（或任何钩子）配合 ``git checkout`` /
  ``git stash apply`` 改写索引时被调用。

对策是给**每一条** git 命令前置 ``-c core.fsmonitor=false -c core.hooksPath=``
（见 ``_GIT_HARDENING``）。这挡掉上面两条，但**不是彻底隔离**：

* ``.gitattributes`` 里声明的 ``filter.<name>.smudge`` 仍然生效——只要工作区里
  有文件需要从对象库取出，git 就会调用 ``.git/config`` 里定义的那个过滤器；
* 我们因此**不再提供** ``git checkout <ref> -- .`` 这类「按任意 tree 覆盖工作区」
  的兜底路径（见 :meth:`SafePointManager.restore` 的两档策略）；
* 仓库的 ``.git/config`` 里还有别的可执行面（别名、``core.pager``、凭据助手），
  本条只处理已确认的两条。

结论：**不可信仓库请把 safepoint 整块关掉**（配置项 ``safepoint = false``）。
本模块不假装已经做到「在不可信目录里安全执行 git」。

失败处理
========

非 git、git 不存在、超时、rc 非 0——一律返回 ``None`` / ``False`` 并记日志，
绝不把异常抛回派发流程。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

__all__ = ["SafePointManager"]

logger = logging.getLogger(__name__)

#: 单条 git 命令的超时。git 正常都在百毫秒级，超这么久基本是锁冲突或网络挂起。
_GIT_TIMEOUT = 15.0

#: 每条 git 命令前置的加固参数。
#:
#: ``core.fsmonitor`` 会被 ``git status`` 调用，``core.hooksPath`` 决定钩子目录——
#: 两者都由**目标仓库自己的** ``.git/config`` 决定要执行什么程序，而 safepoint 是在
#: 派发**之前**跑的，等于 Agent 还没动手、仓库自带的代码已经执行过了。
#: 置空 hooksPath 让 git 找不到任何钩子；置 false 关掉文件系统监视器回调。
_GIT_HARDENING = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=")

#: 允许的 ref 形态：git 对象名的十六进制表示。
#:
#: 只放行十六进制是**选项注入**的防线：以 ``-`` 开头的字符串会被 git 当成选项
#: 解析（``--upload-pack=...`` 这类历史漏洞都是这么来的），而 ref 来自账本字段。
_REF_RE = re.compile(r"[0-9a-f]{4,64}")

#: 我们自己的 ref 命名规则（见 :meth:`SafePointManager._keep_ref`）：
#: ``refs/sentinel/sp-<epoch>-<8hex>``。时间戳直接写在名字里，所以清理时不必
#: 依赖 ``%(creatordate)``（那是**提交**时间，不是 ref 创建时间）。
_REF_TS_RE = re.compile(r"sp-(\d{9,})-")

#: 自动清理的节流间隔（秒）。``create`` 在派发热路径上（单次约 940ms），
#: 每分钟都扫一遍用户仓库是不负责的。
_PRUNE_INTERVAL = 3600.0

#: 单次清理最多删多少个 ref。删 ref 是 git 子进程操作，不能因为积压了几千个
#: 就把一次派发卡死；剩下的交给下一次（每小时一轮）。
_PRUNE_MAX_PER_RUN = 200


def _find_git() -> str:
    """找到 git 可执行文件。

    :return: 路径；找不到返回空串（上层据此降级成「不能建安全点」）。
    """
    try:
        return shutil.which("git") or ""
    except Exception:  # noqa: BLE001
        return ""


class SafePointManager:
    """基于 git 的安全点管理。

    :param store: 可选的 ``EventStore``；给了才记账，不给就只做快照。
    :param timeout: 单条 git 命令的超时秒数。
    """

    def __init__(self, store: Any = None, *, timeout: float = _GIT_TIMEOUT) -> None:
        self.store = store
        self.timeout = max(1.0, float(timeout or _GIT_TIMEOUT))
        self.git = _find_git()
        # 上次自动清理的时刻（单调时钟）。0 表示还没清理过 —— 第一次 create 会顺手
        # 收一遍历史 ref（长期使用后仓库里会攒下成百上千个 refs/sentinel/*）。
        self._last_prune = 0.0
        if not self.git:
            logger.debug("未找到 git，安全点功能降级为不可用")

    # -- 对外 ---------------------------------------------------------------
    def is_git_repo(self, cwd: str) -> bool:
        """``cwd`` 是否在某个 git 工作区里。

        在子目录里也会返回 ``True``（git 会自己往上找仓库根），这正是我们要的：
        Agent 常常在仓库的子目录里干活。

        :param cwd: 工作目录。
        :return: 是 git 工作区返回 ``True``。
        """
        root = self._dir(cwd)
        if root is None or not self.git:
            return False
        out = self._run(root, "rev-parse", "--is-inside-work-tree")
        return bool(out) and out.strip().lower() == "true"

    def create(self, cwd: str, *, label: str = "", fingerprint: str = "") -> dict[str, Any] | None:
        """在工作区当前状态打一个安全点。

        :param cwd: 工作目录（仓库根或它的子目录）。
        :param label: 人类可读标签，进账本后时间线里能看到「开工前的状态」。
        :param fingerprint: 会话指纹，用于按会话回滚。
        :return: 安全点字典（``kind`` / ``ref`` / ``label`` / ``files`` / ``mode``）；
            非 git 仓库或 git 不可用时返回 ``None``。
        """
        root = self._repo_root(cwd)
        if root is None:
            logger.debug("安全点跳过：不是 git 仓库（%s）", cwd)
            return None

        # stash create 只产出游离 commit，不动工作区、不动 stash 列表。
        stashed = self._run(root, "stash", "create")
        ref = (stashed or "").strip()
        mode = "stash"
        if not ref:
            if stashed is None:
                # git 本身出错了（锁冲突之类）。退到 HEAD 是尽力而为，
                # 但记一条日志，免得事后以为「改动都备份过了」。
                logger.warning("stash create 失败，安全点退化为 HEAD 快照（%s）", root)
            # 工作区干净（没东西可 stash）→ 当前 HEAD 就是快照。
            ref = (self._run(root, "rev-parse", "HEAD") or "").strip()
            mode = "head"
        if not ref:
            # 既没改动也没有提交（空仓库）→ 无从回滚。
            logger.debug("安全点跳过：拿不到可用 ref（%s）", root)
            return None

        files = self._changed_files(root)
        safepoint: dict[str, Any] = {
            "kind": "git",
            "ref": ref,
            "label": str(label or ""),
            "files": files,
            "mode": mode,
            "cwd": str(root),
        }
        if mode == "stash":
            # 游离 commit 会被 gc 清掉，挂个 ref 保住它。
            keep_ref = self._keep_ref(root, ref)
            if keep_ref:
                safepoint["keep_ref"] = keep_ref

        if self.store is not None:
            try:
                safepoint["id"] = int(
                    self.store.add_safepoint(
                        kind="git",
                        ref=ref,
                        label=str(label or ""),
                        fingerprint=fingerprint,
                        files=files,
                    )
                    or 0
                )
            except Exception as exc:  # noqa: BLE001 —— 记账失败不该影响快照本身
                logger.warning("安全点记账失败（已忽略）：%s: %s", type(exc).__name__, exc)
        # 顺手清一遍历史 ref（内部按小时节流，不会每次都扫）。放在最后：即使清理
        # 出问题也不影响刚刚建好的这个安全点。
        self._maybe_prune(root)
        return safepoint

    def restore(self, cwd: str, ref: str, *, safepoint_id: int | None = None) -> bool:
        """把工作区恢复到某个安全点。

        用 ``apply`` 而不是 ``pop``：快照可重复使用。新建的未跟踪文件不会被删。

        冲突处理是这里最容易翻车的地方：Agent 往往已经把自己改乱的同一个文件又改
        了一遍，直接 ``stash apply`` 会报「local changes would be overwritten」。
        恢复的语义本来就是「覆盖当前改动」，所以按**两档**递进：

        1. ``git stash apply <ref>``；
        2. 失败则先 ``git checkout -- .`` 退回索引内容再 apply 一次。

        两档都失败就**明确返回 ``False``**。这里刻意不再有第三档
        ``git checkout <ref> -- .``：那条路径会按任意 tree 覆盖工作区，从而触发
        ``.gitattributes`` 里声明的 smudge 过滤器（见模块 docstring 的安全边界）。
        宁可报失败让人人工处理，也不要用一条会执行仓库自带代码的路径凑一个成功。

        :param cwd: 工作目录。
        :param ref: :meth:`create` 返回的 ``ref``（必须是十六进制对象名）。
        :param safepoint_id: 账本里的安全点 ID；给了就在成功后标记为已回滚。
        :return: 恢复成功返回 ``True``。
        """
        target = str(ref or "").strip()
        # 只认十六进制对象名：以 "-" 开头的 ref 会被 git 当选项解析。
        if not _REF_RE.fullmatch(target):
            logger.warning("安全点恢复拒绝：ref 不是合法的十六进制对象名（ref=%r）", target[:40])
            return False
        root = self._repo_root(cwd)
        if root is None:
            logger.debug("恢复跳过：不是 git 仓库（%s）", cwd)
            return False

        parents = self._parents(root, target)
        if not parents:
            # 认不出 ref（多半已被 gc 清理，或根本不是这个仓库的对象）。
            logger.warning("安全点恢复失败：认不出 ref（可能已被清理，ref=%s）", target)
            return False
        if len(parents) >= 2:
            # stash create 产出的是 merge commit（HEAD + index）。
            # 第一档 quiet：冲突是常态，不该在日志里看起来像失败。
            ok = self._apply_stash(root, target, quiet=True)
            if not ok:
                # 同一文件两边都改过 → 先把已跟踪文件退回索引，再 apply 一次。
                logger.debug("stash apply 冲突，先退回索引内容再试（cwd=%s）", root)
                self._discard_worktree_changes(root)
                ok = self._apply_stash(root, target)
        else:
            # 普通 commit：只有「ref 就是当前 HEAD」这一种情况能安全恢复。
            ok = self._restore_to_commit(root, target)

        if not ok:
            logger.warning("安全点恢复失败（cwd=%s，ref=%s）", root, target)
            return False
        if self.store is not None and safepoint_id:
            try:
                self.store.mark_restored(int(safepoint_id))
            except Exception as exc:  # noqa: BLE001
                logger.warning("标记已回滚失败（已忽略）：%s: %s", type(exc).__name__, exc)
        return True

    def prune(self, cwd: str | None = None, *, max_age_days: int = 30, keep: int = 50) -> int:
        """清掉过老/过多的安全点 ref（以及账本里对应的记录）。

        为什么必须清：``create`` 每次都会在**用户仓库**里留一个
        ``refs/sentinel/sp-*``，这些 ref 会挡住 ``git gc`` 回收对象。长期跑下去，
        用户仓库里会攒出成千上万个引用，而且没有任何入口能删掉它们 —— 安全设施
        变成了仓库垃圾产生器。

        规则是并集：超过 ``max_age_days`` 的老 ref，**加上**按时间倒序第 ``keep``
        个之外的多余 ref。单次最多删 ``_PRUNE_MAX_PER_RUN`` 个，剩下的下一轮继续——
        清理不能把一次派发卡死。

        删 ref 也走 :meth:`_run`，因此同样带 H1 那两条 ``-c``：不能为了清理而开一条
        不受保护的 git 路径（``update-ref`` 也会读仓库配置）。

        :param cwd: 仓库目录（仓库根或其子目录）。``None`` 时无法定位仓库，直接返回 0。
        :param max_age_days: 保留天数。
        :param keep: 至少保留最近的多少个 ref（``<= 0`` 表示不按数量保底）。
        :return: 实际删掉的 ref 数量；任何失败降级为 warning 并返回已删数量。
        """
        if not self.git:
            return 0
        root = self._repo_root(cwd) if cwd else None
        if root is None:
            # 没有仓库路径就无法安全地删 ref（ref 是仓库内对象）。宁可这次不清理，
            # 也不能猜一个 cwd 去 update-ref。
            logger.debug("安全点清理跳过：没有可用的仓库路径（cwd=%r）", cwd)
            return 0

        entries = self._sentinel_refs(root)
        if not entries:
            return 0
        cutoff = time.time() - max(1, int(max_age_days)) * 86400
        # 名字里带时间戳的按时间倒序；认不出格式的一律不动（保守：不是我们的命名规则）。
        known = sorted(
            [(name, ts) for name, ts in entries if ts is not None],
            key=lambda item: item[1],
            reverse=True,
        )
        doomed = [
            name
            for index, (name, ts) in enumerate(known)
            if ts < cutoff or (keep > 0 and index >= int(keep))
        ]
        if not doomed:
            return 0
        if len(doomed) > _PRUNE_MAX_PER_RUN:
            logger.info("安全点积压 %s 个待清理 ref，本轮先清 %s 个", len(doomed), _PRUNE_MAX_PER_RUN)
            doomed = doomed[:_PRUNE_MAX_PER_RUN]

        removed = self._delete_refs(root, doomed)
        if removed:
            self._forget_refs(removed)
            logger.info("清理安全点 ref %s 个（cwd=%s）", len(removed), root)
        return len(removed)

    # -- 内部 ---------------------------------------------------------------
    def _maybe_prune(self, root: Path) -> None:
        """按小时节流地跑一次 :meth:`prune`。

        为什么要节流：``create`` 在派发热路径上，而清理要起 git 子进程（Windows 上
        每条 0.4~0.6s）。每小时一次是可接受的额外开销，每次派发都扫是不可接受的。

        :param root: 仓库根。
        """
        now = time.monotonic()
        if now - self._last_prune < _PRUNE_INTERVAL:
            return
        # 先登记时间戳再干活：并发的两次 create 不该同时清理同一个仓库。
        self._last_prune = now
        try:
            self.prune(str(root))
        except Exception as exc:  # noqa: BLE001 —— 清理失败绝不影响派发
            logger.warning("安全点自动清理失败（已降级）：%s: %s", type(exc).__name__, exc)

    def _sentinel_refs(self, root: Path) -> list[tuple[str, float | None]]:
        """列出仓库里所有 ``refs/sentinel/*``。

        :param root: 仓库根。
        :return: ``(ref 全名, 时间戳)`` 列表；时间戳从名字里的 ``sp-<epoch>-`` 解析，
            解析不出时为 ``None``。
        """
        out = self._run(root, "for-each-ref", "--format=%(refname)", "refs/sentinel/")
        if not out:
            return []
        entries: list[tuple[str, float | None]] = []
        for line in out.splitlines():
            name = line.strip()
            if not name:
                continue
            matched = _REF_TS_RE.search(name)
            entries.append((name, float(matched.group(1)) if matched else None))
        return entries

    def _delete_refs(self, root: Path, names: list[str]) -> list[str]:
        """删掉一批 ref。

        优先用 ``update-ref --stdin`` 一次性删完（一个 git 进程）；失败则退回逐条删，
        并只回报真正删掉的那些（账本记录要跟着删，不能删多了）。

        :param root: 仓库根。
        :param names: ref 全名列表。
        :return: 实际删除的 ref 全名列表。
        """
        if not names:
            return []
        payload = "".join(f"delete {name}\n" for name in names)
        if self._run(root, "update-ref", "--stdin", stdin_text=payload) is not None:
            return list(names)
        logger.debug("批量删 ref 失败，退回逐条删除（%s 个）", len(names))
        done: list[str] = []
        for name in names:
            if self._run(root, "update-ref", "-d", name, quiet=True) is not None:
                done.append(name)
        return done

    def _forget_refs(self, names: list[str]) -> None:
        """把账本里对应的安全点记录删掉。

        :param names: 已被 git 删掉的 ref 全名列表。
        """
        if self.store is None or not names:
            return
        forget = getattr(self.store, "forget_safepoints", None)
        if not callable(forget):
            # 账本实现不支持清理（鸭子类型的外部实现）：不报错，只是记录会留着。
            logger.debug("账本不支持 forget_safepoints，跳过记录清理")
            return
        try:
            forget(names)
        except Exception as exc:  # noqa: BLE001 —— 记录清理失败不影响 ref 已经删掉的事实
            logger.warning("安全点记录清理失败（已降级）：%s: %s", type(exc).__name__, exc)

    @staticmethod
    def _dir(cwd: str) -> Path | None:
        """把 ``cwd`` 规范化成存在的目录。

        空串要显式挡掉：``Path("")`` 等于 ``Path(".")``，那会悄悄拿插件自己的工作
        目录去建安全点——错得最隐蔽的一类 bug。

        :param cwd: 原始路径。
        :return: ``Path``；不存在或不是目录返回 ``None``。
        """
        text = str(cwd or "").strip()
        if not text:
            return None
        try:
            path = Path(text).expanduser()
        except (TypeError, ValueError, OSError):
            return None
        try:
            if not path.is_dir():
                return None
        except OSError:
            return None
        return path

    def _repo_root(self, cwd: str) -> Path | None:
        """把任意目录解析成 git 仓库根。

        统一在仓库根操作：Agent 常在子目录里干活，而 ``git stash`` /
        ``git checkout -- .`` 的作用范围跟着 pathspec 走，不先归一化就会出现
        「只恢复了子目录」这种半吊子结果。

        :param cwd: 工作目录。
        :return: 仓库根；不在仓库里或 git 不可用返回 ``None``。
        """
        base = self._dir(cwd)
        if base is None or not self.git:
            return None
        out = self._run(base, "rev-parse", "--show-toplevel")
        if not out or not out.strip():
            return None
        try:
            root = Path(out.strip())
            return root if root.is_dir() else None
        except (OSError, ValueError):
            return None

    def _env(self) -> dict[str, str]:
        """给 git 子进程的环境。

        两处都是为了「不要卡住」：``GIT_TERMINAL_PROMPT=0`` 避免等凭据输入，
        ``GIT_OPTIONAL_LOCKS=0`` 避免我们和 Agent 自己的 git 抢索引锁。

        :return: 环境变量字典。
        """
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_OPTIONAL_LOCKS"] = "0"
        return env

    def _run(self, cwd: Path, *args: str, quiet: bool = False, stdin_text: str | None = None) -> str | None:
        """跑一条 git 命令（已加固）。

        每条命令都前置 ``_GIT_HARDENING``：目标仓库的 ``.git/config`` 不能决定
        我们要执行什么程序。``-c`` 必须放在子命令**之前**，这是 git 的全局选项位置。

        :param cwd: 工作目录。
        :param args: 参数（不含 ``git`` 本身）。
        :param quiet: 为真时失败只记 debug。用于「预期可能失败」的调用——比如恢复的
            第一档 ``stash apply``，冲突是常态，记 warning 会让成功恢复看起来像出错。
        :param stdin_text: 通过 stdin 喂给 git 的内容（``update-ref --stdin`` 用）。
        :return: 成功时返回 stdout；失败、超时、异常一律返回 ``None`` 并记日志。
        """
        if not self.git:
            return None
        argv = [self.git, *_GIT_HARDENING, *args]
        base_kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "capture_output": True,
            "timeout": self.timeout,
            "env": self._env(),
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        }
        try:
            if stdin_text is None:
                completed = subprocess.run(
                    argv,
                    # 显式 utf-8：Windows 中文环境下默认按 cp936 解码，git 输出会炸。
                    encoding="utf-8",
                    errors="replace",
                    **base_kwargs,
                )
                stdout = completed.stdout or ""
                stderr = completed.stderr or ""
            else:
                # 有 stdin 时**必须走字节模式**：文本模式下 Windows 会把 "\n" 翻译成
                # "\r\n"，而 ``update-ref --stdin`` 的协议是严格的 ``命令 SP 参数 LF``，
                # 收到 CR 会报 "expected SP but got: ?"。（实测过的坑。）
                completed = subprocess.run(argv, input=stdin_text.encode("utf-8"), **base_kwargs)
                stdout = (completed.stdout or b"").decode("utf-8", "replace")
                stderr = (completed.stderr or b"").decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            logger.warning("git %s 超时（%.0fs，cwd=%s）", args[0] if args else "?", self.timeout, cwd)
            return None
        except OSError as exc:
            logger.warning("git 启动失败（cwd=%s）：%s: %s", cwd, type(exc).__name__, exc)
            return None

        if completed.returncode != 0:
            detail = stderr.strip().replace("\n", " ")[:300]
            log = logger.debug if quiet else logger.warning
            log("git %s 失败（rc=%s，cwd=%s）：%s", args[0] if args else "?", completed.returncode, cwd, detail)
            return None
        return stdout

    def _apply_stash(self, root: Path, ref: str, *, quiet: bool = False) -> bool:
        """``git stash apply`` 一次快照。

        apply 而不是 pop：快照留在原地，同一个安全点可以反复回滚。

        :param root: 仓库根。
        :param ref: 快照 ref。
        :param quiet: 为真时失败只记 debug（第一档冲突是常态）。
        :return: 成功返回 ``True``。
        """
        return self._run(root, "stash", "apply", ref, quiet=quiet) is not None

    def _discard_worktree_changes(self, root: Path) -> bool:
        """把已跟踪文件退回**索引**里的内容（``git checkout -- .``）。

        这是恢复的第 2 档：Agent 常常把同一个文件又改了一遍，直接 ``stash apply``
        会报「local changes would be overwritten」，先退回才有干净的落点。

        ``checkout -- .`` 走的是「索引 → 工作区」，不涉及任意 tree，也不像
        ``checkout <ref> -- .`` 那样把整棵树的内容转换跑一遍。

        :param root: 仓库根。
        :return: 成功返回 ``True``。
        """
        return self._run(root, "checkout", "--", ".") is not None

    def _restore_to_commit(self, root: Path, ref: str) -> bool:
        """恢复「工作区干净时打出的 HEAD 快照」。

        这种快照的 ``ref`` 是普通 commit，而不是 stash 造的 merge commit，
        ``git stash apply`` 对它无效（git 会直接报 ``not a stash-like commit``）。
        唯一安全的等价操作是「丢掉已跟踪文件的工作区改动」，前提是 ``ref`` 正好
        就是当前 ``HEAD``。

        ``ref`` 与当前 ``HEAD`` 不一致时**明确失败**：那种情况只能用
        ``git checkout <ref> -- .`` 覆盖整棵树，而那会触发 smudge 过滤器
        （见模块 docstring 的安全边界）。失败比执行仓库自带的代码好。

        :param root: 仓库根。
        :param ref: 快照 ref。
        :return: 成功返回 ``True``。
        """
        head = (self._run(root, "rev-parse", "HEAD") or "").strip()
        if not head:
            logger.debug("恢复跳过：拿不到当前 HEAD（%s）", root)
            return False
        if head != ref:
            logger.warning(
                "安全点恢复失败：ref 与当前 HEAD 不一致，且不是 stash 快照（ref=%s，head=%s）；"
                "为避免触发 .gitattributes 的内容转换过滤器，不自动覆盖工作区",
                ref,
                head,
            )
            return False
        return self._discard_worktree_changes(root)

    def _changed_files(self, root: Path) -> int:
        """数一下工作区里改了多少个已跟踪文件。

        只数已跟踪的：``git stash create`` 本来就不含未跟踪文件，报一个更大的
        数字只会让人误以为「这些都备份了」。

        :param root: 仓库目录。
        :return: 改动文件数；拿不到返回 0。
        """
        out = self._run(root, "status", "--porcelain", "--untracked-files=no")
        if not out:
            return 0
        return sum(1 for line in out.splitlines() if line.strip())

    def _parents(self, root: Path, ref: str) -> list[str] | None:
        """取某个 commit 的父提交。

        用来区分「stash 造的 merge commit」和「普通 commit」——两者的恢复命令不同。

        :param root: 仓库目录。
        :param ref: commit 引用。
        :return: ``[自身, 父1, 父2...]``；认不出返回 ``None``。
        """
        out = self._run(root, "rev-list", "--parents", "-n", "1", ref)
        if not out:
            return None
        parts = out.strip().split()
        return parts or None

    def _keep_ref(self, root: Path, ref: str) -> str:
        """给游离 commit 挂一个引用，免得被 ``git gc`` 清掉。

        :param root: 仓库目录。
        :param ref: commit hash。
        :return: 引用名；没挂上返回空串（不影响快照本身可用）。
        """
        name = f"sp-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        keep = f"refs/sentinel/{name}"
        if self._run(root, "update-ref", keep, ref) is None:
            return ""
        return keep
