"""进程干预 —— 让哨兵在 Agent 跑偏时真的能「踩一脚刹车」。

为什么有这个模块
================

派发前的 L1 预检只能拦在门口：命令一旦跑起来，唯一的止血手段就是对那个进程
做事——先挂起问一句，还是直接按掉。这些操作全是平台私有 API（Windows 的
ntdll、POSIX 的信号），散落在业务代码里既难测，也容易在出错时把派发流程一起
带崩，所以集中成一个「只提供能力、不做决策」的小模块。

设计红线
========

* **只提供能力，不做策略**：是否真的挂起/杀掉由上层按判定结果决定。这里绝不
  自作主张——「检测到危险就自动杀进程」会把用户正在跑的编译、迁移、打包一起
  杀掉，代价远大于收益。
* **失败即 ``False``**：拿不到权限、进程已退出、句柄无效、ctypes 不可用，都是
  运行中的正常情况，不是错误。不抛异常、不打堆栈，只记 debug。
* **不碰自己**：对自己的进程动手会让整个插件乃至宿主一起卡死，所以
  ``suspend`` / ``resume`` / ``terminate`` 对 ``os.getpid()`` 一律拒绝。
* **``terminate`` 是阻塞调用，绝不能挂在输出读取回调里**：最坏要等 ``grace`` +
  ``taskkill`` 超时（默认约 13 秒）。读取线程一被占住就没人读管道，子进程卡死在
  ``write`` 上；``runner`` 那边只会在 ``stream_grace``（5 秒）的总预算里干等，到点
  交给后台线程收尾。于是现象是「这次任务慢了或超时了」，日志里却看不到任何干预
  痕迹——极难查。要动手就另起线程（``_PipeReader._emit`` 只兜异常，不兜阻塞）。

平台差异（本机实测记录）
========================

* Windows 没有 SIGSTOP。挂起只能靠 ``ntdll.NtSuspendProcess``，它挂起的是
  **进程的全部线程**；也没有「优雅退出」这一档，``terminate`` 只有
  ``taskkill /T /F`` 和 ``TerminateProcess`` 两种硬手段。
* Windows 上 ``os.kill(pid, 0)`` **不是探活**：CPython 会退化成
  ``OpenProcess(PROCESS_ALL_ACCESS)`` + ``TerminateProcess``，等于把进程杀了。
  探活必须走 ``OpenProcess`` + ``GetExitCodeProcess``。
* ``NtSuspendProcess`` 对**自己**也生效，而且挂起后连日志都写不出来（线程停了），
  所以自测时只能拿子进程当靶子。
* POSIX 上被自己 SIGTERM 掉的子进程在 ``wait()`` 之前是僵尸，``os.kill(pid, 0)``
  仍然成功，``is_alive`` 因此返回 True；``terminate`` 会走到 SIGKILL 兜底
  （幂等，无害）。

身份校验（为什么不能只判 pid 存活）
===================================

pid 会被系统复用。如果目标进程已经退出、这个 pid 恰好被别的进程拿走，
``suspend`` / ``terminate`` 打中的就是**无关进程** —— 一次误杀可能是别人的
构建、数据库或编辑器。所以动手前要比对「创建时间」这一身份指纹：

* **Linux**：``/proc/<pid>/stat`` 第 22 字段（starttime）。解析必须先 ``rsplit(")", 1)``
  再从 ``)`` 之后切分 —— 第 2 字段 comm 允许含空格和括号（``(tmux: server)``），
  直接 ``split(" ")`` 会让后面所有字段错位，校验就形同虚设。
* **Windows**：``GetProcessTimes`` 的 ``CreationTime``。
* **拿不到创建时间的平台 / 进程**：退化成「记下 pid 与首次见到它的时刻」的时间锚。

三档身份（:meth:`ProcessGuard.identity_mode`）
---------------------------------------------

上面这些机制不是「全有或全无」，而是三档，且**必须让调用方不读文档也能分辨**：

* ``creation_time`` —— 拿到了「pid 复用时必然改变」的创建时间，校验成立；
* ``time_window`` —— 拿不到创建时间，只能用时间锚猜：能挡住「隔很久的复用」，
  挡不住「同一次任务生命周期内的复用」（macOS、隐藏 procfs 的容器都落在这一档，
  因为这些平台仍能用 ``os.kill(pid, 0)`` 观察目标是否还在）；
* ``none`` —— 连存活都观察不到（Windows 上 ctypes / ``OpenProcess`` 完全不可用），
  这时本模块对「这个 pid 是谁」一无所知。

``capabilities()["identity_check"]`` 只在 ``creation_time`` 档为 ``True`` ——
让它在降级档也报 ``True``，等于把「比了个时间差」冒充成「已校验身份」，
这正是原缺陷换个字段名的写法。

可逆性非对称（为什么 ``resume`` 和 ``terminate`` 的待遇不一样）
--------------------------------------------------------------

身份校验不能一个规则套三个动作，否则会出现**模块自己制造、自己撤不回来**的状态：

1. ``suspend`` 成功（进程被冻住），首次见到 pid 时记下了时间锚；
2. 窗口早已过期（或本机 ``identity_mode == "none"``），要 ``resume``；
3. 如果 ``resume`` 也 fail-closed → **冻着**；想改用 ``terminate`` 补救 → 同样被拒。

于是任务吃满 dispatch timeout，而用户只看到「莫名挂住」，日志里没有任何干预痕迹。
所以按**可逆性**分流：

* ``suspend`` / ``resume`` —— **可逆**。最坏情况是「碰了无关进程一下」：
  ``SIGCONT`` 对正常进程基本无害，``SIGSTOP`` 之后还能再 ``resume`` 回来。而
  fail-closed 的代价是冻住撤不回，两者不对称，所以这两档放行（并记 warning）。
* ``terminate`` —— **不可逆**。宁可不杀，也不打错人，继续 fail-closed。

注意「已确认复用」（创建时间不匹配）和「无法校验」是两件事：前者三项全拒（那是
确知要打错人），只有后者才走上面的可逆性放行。

失效必须留痕
============

* **窗口过期**这一刻单独记一条 warning —— 放行了 ``resume`` 也要记，否则外部看到
  的只有「校验悄悄没了」；
* **指纹表满**那一刻记一条 warning，并且必须说明后果（此后对未能登记的 pid 只放
  行可逆动作）。两种原因分开写：「扫描后确认全在运行」与「本次未扫描（节流中）」——
  后者我们根本没看，断言「全是活进程」就成了假话。淘汰已退出记录时记一条 debug，
  并写明被淘汰的 pid 与正要登记的 pid（淘汰的是别人，不能让人以为说的是调用方）；
* **某 pid 取不到创建时间**（受保护进程、跨完整性级别）而本机本该有能力时，记一次
  warning —— 「校验生效」和「校验悄悄失效」在外部看起来一模一样，只能靠日志分辨。

四种拒绝措辞互不相同（:meth:`ProcessGuard._usable`）：真的被复用、窗口过期、
平台无能力、指纹表满未能登记 —— 混用会把人引向错误的排查方向。

**登记不上不算比对通过**。指纹表满且全是活进程时 ``_remember`` 会放弃登记，该 pid
归入 ``unknown``（只放行可逆动作），而不是 ``same``：若归 ``same``，容量边界就变成了
「对任意新 pid 关闭身份校验」的能力悬崖 —— 表一满，terminate 又能打错人了。
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any

__all__ = ["ProcessGuard"]

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# -- Windows 访问权限位与状态码（WinNT.h / ntstatus.h） ----------------------
_PROCESS_TERMINATE = 0x0001
_PROCESS_SUSPEND_RESUME = 0x0800
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
#: ``GetExitCodeProcess`` 用它表示「还没退出」。
_STILL_ACTIVE = 259
#: ``NTSTATUS`` 的成功值。
_STATUS_SUCCESS = 0

#: ``taskkill`` 等待上限（进程树已经僵住时不能把自己也卡主）。
_TASKKILL_TIMEOUT = 10.0
#: 轮询间隔：够细，又不至于把 CPU 烧起来。
_POLL_INTERVAL = 0.05

#: 身份指纹表的容量上限。pid 复用会让表只增不减，满了要按规则处理。
#:
#: 从 256 提到 1024：256 在「并发任务数 × 每任务登记的 pid 数」面前很容易被填满，
#: 而填满之后的淘汰**会静默让校验对某个 pid 失效**（它再出现时被当「首次见到」）。
#: 提高上限只是把概率压低，真正兜住的是 ``_remember`` 里那条 warning —— 必须能
#: 在日志里看出「校验对谁失效了」，不能靠调大数字假装问题不存在。
#:
#: 满了以后的行为是明确的（见 ``_remember`` 的 docstring）：**只淘汰已经退出的
#: pid，绝不挤掉活进程的记录**。挤掉一条活的换一条新的，等于把「原本能识别的 pid」
#: 变成「无法识别」，是净损失。
_MAX_IDENTITY_ENTRIES = 1024
#: 时间锚的有效期（秒）。这是**任务级窗口**，不是防复用窗口。
#:
#: 语义要说清，否则会被当成「滑动窗」误改：它锚在「首次见到该 pid 的时刻」，而
#: 那个时刻必然落在某一次派发的生命周期内。窗口存在的目的只有一个 ——
#: **让这一次任务从开始到收尾都能被干预**，所以它必须大于任何可能的 dispatch
#: 超时（``config.toml`` 里 ``default_timeout_seconds = 600``、
#: ``max_timeout_seconds = 3600``），这里取 2 倍余量。
#:
#: 曾经是 60 秒且不刷新，后果是「首次见到 60 秒之后**永久**失效」—— 在 macOS 这类
#: 只能时间锚的平台上，任何活过 60 秒的任务干预能力等于 0，而且没有任何日志说明
#: 原因。改成任务级窗口后，**代价**是「同一次任务生命周期内 pid 被复用」识别不出来；
#: 但在 ``time_window`` 档本来就识别不出来（没有创建时间可比特），这是能力边界，
#: 不是这次改动带来的新损失。
#:
#: 刻意**不做滑动续期**（比对成功就刷新时刻）：那会把「pid 复用防护」变成「活跃度
#: 检测」——一个持续上报危险信号的 agent 会不断给自己续信任，等于「只要它在闹就一直
#: 被信任」，把防打错进程的能力整片削掉，而且削得隐蔽（功能看起来完全正常）。
_IDENTITY_WINDOW_SECONDS = 7200.0
#: 表满时「找一条死记录腾位置」的扫描最短间隔（秒）。见 ``_evict_dead``。
_EVICT_SCAN_INTERVAL = 60.0

_IDENTITY_SAME = "same"
_IDENTITY_REUSED = "reused"
_IDENTITY_UNKNOWN = "unknown"

#: ``_remember`` 的三种结果。表满这条路上，「扫过、全是活的」「节流里、根本没扫」
#: 「取指纹期间被别人占满」都腾不出位置，但原因不同 —— 混成一句就变成「我们没看，
#: 却断言里面全活着」。
_REGISTERED = "registered"
_TABLE_FULL = "table_full"
_SCAN_THROTTLED = "throttled"
_FILLED_MEANWHILE = "filled_meanwhile"

#: 走「可逆性非对称」的动作：身份无法校验时也放行（否则会冻住撤不回，见模块docstring）。
_REVERSIBLE_ACTIONS = frozenset({"suspend", "resume"})

#: ``identity_mode == "none"`` 的启动期一次性提醒是否已打过（见 ``identity_mode``）。
_NONE_MODE_WARNED = False


class _WinApi:
    """Windows 侧 ctypes 绑定的薄封装。

    为什么包一层：``ctypes.WinDLL`` / ``ctypes.wintypes`` 在非 Windows 上不存在，
    ntdll 的导出也不是每个环境都有（受限容器、被精简的系统映像都可能缺）。
    任何一步失败就把自己标成不可用，上层据此降级为「这台机器不能干预」，
    而不是在调用点崩掉。
    """

    def __init__(self) -> None:
        self.ready = False
        self.suspend_ok = False
        self.resume_ok = False
        self._kernel32: Any = None
        # 绑定好签名的函数必须握住：每次重新 getattr 拿到的可能是新对象，
        # argtypes 没设的话 64 位句柄会被截成 32 位 int 传进去。
        self._suspend: Any = None
        self._resume: Any = None

    def load(self) -> None:
        """尝试加载 kernel32 / ntdll 并绑定签名（幂等，失败就保持不可用）。"""
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.TerminateProcess.restype = wintypes.BOOL
            kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            # 身份校验要用：同 pid 复用是真实存在的，光判存活会误伤无关进程。
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            self._kernel32 = kernel32

            ntdll = ctypes.WinDLL("ntdll")
            for name in ("NtSuspendProcess", "NtResumeProcess"):
                try:
                    func = getattr(ntdll, name)
                except AttributeError:
                    # 老系统或精简映像可能没有这两个导出，缺哪个降级哪个。
                    logger.debug("%s 不存在，对应的进程干预能力不可用", name)
                    continue
                func.restype = wintypes.LONG
                func.argtypes = [wintypes.HANDLE]
                if name == "NtSuspendProcess":
                    self._suspend = func
                    self.suspend_ok = True
                else:
                    self._resume = func
                    self.resume_ok = True
            self.ready = True
        except Exception as exc:  # noqa: BLE001 —— 平台 API 缺失是正常情况，不是错误
            logger.debug("Windows 进程 API 不可用，进程干预降级：%s: %s", type(exc).__name__, exc)

    # -- 原始调用 -----------------------------------------------------------
    def open_process(self, pid: int, access: int) -> Any:
        """拿进程句柄。

        :param pid: 进程 ID。
        :param access: 访问权限位。
        :return: 句柄；失败返回 ``None``（无权限 / 无此进程都走这里）。
        """
        if self._kernel32 is None:
            return None
        try:
            handle = self._kernel32.OpenProcess(access, 0, int(pid))
        except Exception:  # noqa: BLE001
            return None
        return handle or None

    def close(self, handle: Any) -> None:
        """关句柄（失败无所谓，句柄是进程内资源）。"""
        if self._kernel32 is None or not handle:
            return
        try:
            self._kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass

    def nt_call(self, name: str, handle: Any) -> bool:
        """调 ``NtSuspendProcess`` / ``NtResumeProcess``。

        :param name: 导出名。
        :param handle: 进程句柄。
        :return: ``NTSTATUS == 0`` 才算成功。
        """
        func = self._suspend if name == "NtSuspendProcess" else self._resume
        if func is None or not handle:
            return False
        try:
            status = func(handle)
        except Exception:  # noqa: BLE001
            return False
        return int(status or 0) == _STATUS_SUCCESS

    def exit_code(self, pid: int) -> int | None:
        """取进程退出码。

        :param pid: 进程 ID。
        :return: 退出码；拿不到句柄返回 ``None``。
        """
        handle = self.open_process(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
        if not handle:
            return None
        try:
            import ctypes
            from ctypes import wintypes

            code = wintypes.DWORD(0)
            if not self._kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return int(code.value)
        except Exception:  # noqa: BLE001
            return None
        finally:
            self.close(handle)

    def creation_time(self, pid: int) -> int | None:
        """取进程创建时间（``GetProcessTimes`` 的 ``CreateTime``，FILETIME 拼成 64 位）。

        这是 Windows 上唯一可靠的「身份指纹」：pid 复用后创建时间一定不同，
        而进程名、命令行都可能被同名程序撞上。

        :param pid: 进程 ID。
        :return: 创建时间（100ns 单位）；拿不到返回 ``None``。
        """
        handle = self.open_process(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
        if not handle:
            return None
        try:
            import ctypes
            from ctypes import wintypes

            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            ok = self._kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            if not ok:
                return None
            return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        except Exception:  # noqa: BLE001 —— 拿不到就当没有身份指纹
            return None
        finally:
            self.close(handle)

    def terminate(self, pid: int) -> bool:
        """``TerminateProcess``（不递归，进程树里的子进程会变孤儿）。"""
        handle = self.open_process(pid, _PROCESS_TERMINATE)
        if not handle:
            return False
        try:
            return bool(self._kernel32.TerminateProcess(handle, 1))
        except Exception:  # noqa: BLE001
            return False
        finally:
            self.close(handle)


_WIN_API: _WinApi | None = None


def _win_api() -> _WinApi:
    """取（并缓存）Windows API 封装。

    :return: 封装实例；非 Windows 平台调用方不会走到这里。
    """
    global _WIN_API
    if _WIN_API is None:
        api = _WinApi()
        api.load()
        _WIN_API = api
    return _WIN_API


def _has_taskkill() -> bool:
    """``taskkill`` 是否可用（它是 Windows 上唯一带进程树语义的手段）。"""
    if not _IS_WINDOWS:
        return False
    try:
        return bool(shutil.which("taskkill"))
    except Exception:  # noqa: BLE001
        return False


def _parse_starttime(stat_line: str) -> str | None:
    """从 ``/proc/<pid>/stat`` 的内容里取出第 22 字段（starttime）。

    **必须先从最后一个 ``)`` 切开**：第 2 字段 ``comm`` 是进程名，内核允许它含空格
    甚至括号（实测 ``(tmux: server)``、``(a)b``），直接按空格切会让后面每个字段
    整体右移，取到的「第 22 字段」其实是别的字段 —— 校验看起来在工作，实际比的是
    无关数字。

    切完之后的 ``fields[0]`` 是整个 stat 的第 3 字段（state），所以第 22 字段是
    ``fields[22 - 3]``。

    :param stat_line: ``/proc/<pid>/stat`` 的整行内容。
    :return: starttime 字符串；内容不可解析时返回 ``None``。
    """
    parts = stat_line.rsplit(")", 1)
    if len(parts) != 2:
        return None
    fields = parts[1].split()
    # 需要至少 20 个后续字段才能覆盖到第 22 个（含 state 在内的 3..22）
    if len(fields) < 20:
        return None
    return fields[19]


def _linux_starttime(pid: int) -> str | None:
    """读 Linux 进程的 starttime（内核启动以来的 jiffies，pid 复用时必然不同）。

    :param pid: 进程 ID。
    :return: starttime；读不到（已退出 / 非 Linux / procfs 未挂载）返回 ``None``。
    """
    try:
        with open(f"/proc/{int(pid)}/stat", "r", encoding="utf-8", errors="replace") as handle:
            return _parse_starttime(handle.read())
    except (OSError, ValueError):
        return None


def _taskkill_tree(pid: int) -> bool:
    """``taskkill /T /F`` 掉整棵进程树。

    ``/T`` 是「连同子进程」——只杀父进程会留下孤儿继续占用文件与端口，
    这正是 runner 里那条注释同样的教训。

    :param pid: 根进程 ID。
    :return: 命令返回 0 才算成功。
    """
    try:
        completed = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(int(pid))],
            capture_output=True,
            timeout=_TASKKILL_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001 —— 超时/找不到可执行文件都算失败
        logger.debug("taskkill 调用失败（pid=%s）：%s: %s", pid, type(exc).__name__, exc)
        return False
    if completed.returncode != 0:
        logger.debug("taskkill 返回 %s（pid=%s）", completed.returncode, pid)
        return False
    return True


def _bounded_add(bucket: set[int], pid: int) -> None:
    """往「只用于日志去重」的集合里加一项，超上限就整体清空。

    这些集合的唯一用途是「同一件事别对同一个 pid 重复刷日志」，不是业务状态，
    所以腾位置时直接清空即可 —— 代价是极端情况下同一 pid 的日志会再来一条，
    远好过让它们跟着长跑宿主一起无限增长。

    **只给 ``_expiry_noted`` / ``_degrade_noted`` 这种「丢了不致命」的集合用。**
    绝不能拿它管 ``_identities``：那里丢一条等于把「已校验」降级成「首次见到即
    信任」（``_remember`` 的 docstring 第 3 条），所以身份表必须走
    ``_remember`` + ``_evict_dead``（只淘汰死进程、绝不整体清空）。
    """
    if len(bucket) >= _MAX_IDENTITY_ENTRIES:
        bucket.clear()
    bucket.add(pid)


def _table_full_detail(why: str) -> str:
    """把「为什么没登记上」翻成一句人话。

    日志和拒绝理由**共用同一句**：两处各写一遍必然漂移，而这类文案漂移的后果是
    「日志说是 A，拒绝理由说是 B」，排查时只能靠猜。

    :param why: ``_remember`` 返回的原因。
    :return: 可直接嵌进日志/理由的说明。
    """
    if why == _SCAN_THROTTLED:
        return "本次未扫描：距上次扫描不足 %.0fs" % _EVICT_SCAN_INTERVAL
    if why == _FILLED_MEANWHILE:
        return "取指纹期间表被并发登记填满"
    return "扫描后确认全部仍在运行"


class ProcessGuard:
    """跨平台的进程干预能力集合。

    只回答两个问题：「这台机器能做到什么」（:meth:`capabilities`）和
    「对某个 PID 做一次」（:meth:`suspend` / :meth:`resume` / :meth:`terminate`）。
    判断该不该动手，是上层策略的事。
    """

    def __init__(self) -> None:
        self._caps: dict[str, bool] | None = None
        #: pid → (指纹类型, 指纹值)。首次见到某 pid 时惰性捕获（见 ``_usable``）。
        self._identities: dict[int, tuple[str, Any]] = {}
        #: 身份档次缓存：本机能力运行时不会变，反复 ``OpenProcess`` 探自己没意义。
        self._mode: str | None = None
        #: 已经为它记过「窗口过期」warning 的 pid（避免每次调用都刷一条）。
        self._expiry_noted: set[int] = set()
        #: 已经为它记过「取不到创建时间，降级为时间锚」warning 的 pid。
        self._degrade_noted: set[int] = set()
        #: 指纹表满且全是活进程时的降级提示是否已打过。
        self._table_full_warned = False
        #: 上次扫描「死记录」的时刻（限频用，见 ``_evict_dead``）。
        self._evict_scan_at = 0.0
        #: 保护 ``_identities`` 的登记/腾位/淘汰。干预动作可能来自后台线程
        #: （见 ``runtime`` 的 intervene 线程），而「查容量 → 取指纹 → 写入」这三步
        #: 中间会被别的线程插入；不加锁的话容量上限只是「大致不超过」。
        #: ``_evict_dead`` 是内部助手，调用方必须已持有它（普通 ``Lock`` 不可重入）。
        self._identity_lock = threading.Lock()

    # -- 能力自述 -----------------------------------------------------------
    def capabilities(self) -> dict[str, bool]:
        """报告本机能做到哪些干预动作。

        上层拿到 ``False`` 就该放弃对应策略（比如「挂起后询问」在缺
        ``NtSuspendProcess`` 的机器上做不了，只能退化成告警）。

        ``identity_check`` 的定义是**「是否拿到了真正的身份指纹」**，即
        ``identity_mode() == "creation_time"``。它在时间锚档刻意报 ``False``：
        那一档只能挡住「隔很久的复用」，挡不住「同一次任务生命周期内的复用」，
        报 ``True`` 会让调用方（不读文档地）以为已经校验过身份。三档的区别看
        :meth:`identity_policy`。

        **能力边界（写在这里，便于配置了 ``guard_action`` 的人直接看到）**：在
        ``time_window`` 档下，首次见到某 pid 后超过 ``identity_policy()["window_seconds"]``
        （默认 7200s = 2 × ``max_timeout_seconds``）的会话，``terminate`` 会被拒 ——
        那是任务级窗口的性质，不是 bug；``suspend`` / ``resume`` 不受影响。
        ``none`` 档则 ``terminate`` 一律被拒（启动日志会有一条 warning）。

        :return: ``{"suspend", "resume", "terminate", "tree_kill", "identity_check"}``。
        """
        if self._caps is not None:
            return dict(self._caps)

        if _IS_WINDOWS:
            api = _win_api()
            suspend_ok = bool(api.ready and api.suspend_ok)
            resume_ok = bool(api.ready and api.resume_ok)
            # TerminateProcess 万古不变，taskkill 只是提供了树语义。
            # 探一下自己能不能拿句柄即可；句柄必须关，否则每调一次 capabilities()
            # 就漏一个内核句柄（宿主是长跑进程，漏到上限就会开不出新句柄）。
            probe = api.open_process(os.getpid(), _PROCESS_QUERY_LIMITED_INFORMATION)
            try:
                terminate_ok = bool(probe)
            finally:
                api.close(probe)
            tree_ok = _has_taskkill()
        else:
            suspend_ok = hasattr(signal, "SIGSTOP")
            resume_ok = hasattr(signal, "SIGCONT")
            terminate_ok = hasattr(signal, "SIGTERM")
            tree_ok = hasattr(os, "killpg") and hasattr(os, "getpgid")

        self._caps = {
            "suspend": suspend_ok,
            "resume": resume_ok,
            "terminate": terminate_ok,
            "tree_kill": tree_ok,
            "identity_check": self.identity_mode() == "creation_time",
        }
        return dict(self._caps)

    def identity_mode(self) -> str:
        """本机对进程身份的校验档次（三态，供调用方按语义而不是按平台名分岔）。

        三档的含义见模块 docstring：``creation_time`` / ``time_window`` / ``none``。
        判据用 ``os.getpid()`` 实调一次真探针 —— 只看 ``kernel32`` 加载成功不算数
        （那只能证明 DLL 在，不证明 ``GetProcessTimes`` 可用）。

        ``none`` 档会在**首次判定时**打一条 warning：这一档下 ``terminate`` 一律被拒，
        而用户如果配了 ``guard_action = "terminate"``，不提醒就会以为功能在工作。
        只打一次，避免每次干预都刷屏。

        :return: ``"creation_time"`` / ``"time_window"`` / ``"none"``。
        """
        global _NONE_MODE_WARNED

        if self._mode is not None:
            return self._mode

        if self._identity_capable():
            mode = "creation_time"
        elif _IS_WINDOWS and not _win_api().ready:
            # 连存活都探不了（``is_alive`` 在这种情况下恒为 False）：不只是没有
            # 创建时间，而是对目标一无所知。POSIX 上不会走到这里 ——
            # ``os.kill(pid, 0)`` 永远可用，那算能观察，落 time_window。
            mode = "none"
        else:
            mode = "time_window"

        self._mode = mode
        if mode == "none" and not _NONE_MODE_WARNED:
            _NONE_MODE_WARNED = True
            logger.warning(
                "本机无法校验进程身份（%s：进程 API 不可用），不可逆干预（terminate）"
                "将被一律拒绝；suspend/resume 仍放行（可逆，见 guard 模块说明）",
                sys.platform,
            )
        return mode

    # -- 探活 ---------------------------------------------------------------
    def is_alive(self, pid: int) -> bool:
        """进程是否还在跑。

        注意 POSIX 上僵尸进程也算「活着」（信号还能发进去，只是不会被执行），
        这是 §平台差异 里记的那条取舍，上层别拿它做严格的存活判定。

        :param pid: 进程 ID。
        :return: 活着为 ``True``；已退出、无权限、PID 非法的都是 ``False``。
        """
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        if pid <= 0:
            return False

        try:
            if _IS_WINDOWS:
                api = _win_api()
                if not api.ready:
                    # ctypes 都不可用时没有任何可靠的探活手段：
                    # 绝不能用 os.kill(pid, 0) 兜底，那在 Windows 上是杀进程。
                    return False
                code = api.exit_code(pid)
                return code is not None and code == _STILL_ACTIVE
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                # 存在但不属于我们 —— 活着，只是碰不得。
                return True
            return True
        except Exception as exc:  # noqa: BLE001 —— 探活失败按「不知道」处理，不当异常抛
            logger.debug("探活失败（pid=%s）：%s: %s", pid, type(exc).__name__, exc)
            return False

    # -- 挂起 / 恢复 --------------------------------------------------------
    def suspend(self, pid: int) -> bool:
        """挂起进程（Windows 挂起全部线程，POSIX 发 SIGSTOP）。

        :param pid: 进程 ID。
        :return: 动作是否真的送达；失败一律 ``False``。
        """
        target = self._usable(pid, "suspend")
        if target is None:
            return False
        try:
            if _IS_WINDOWS:
                api = _win_api()
                if not (api.ready and api.suspend_ok):
                    return False
                handle = api.open_process(target, _PROCESS_SUSPEND_RESUME)
                if not handle:
                    logger.debug("拿不到挂起权限（pid=%s）", target)
                    return False
                try:
                    return api.nt_call("NtSuspendProcess", handle)
                finally:
                    api.close(handle)
            self._posix_signal(target, signal.SIGSTOP, tree=False)
            return True
        except ProcessLookupError:
            return False
        except Exception as exc:  # noqa: BLE001
            logger.debug("挂起失败（pid=%s）：%s: %s", target, type(exc).__name__, exc)
            return False

    def resume(self, pid: int) -> bool:
        """恢复被 :meth:`suspend` 挂起的进程。

        :param pid: 进程 ID。
        :return: 动作是否真的送达；失败一律 ``False``。
        """
        target = self._usable(pid, "resume")
        if target is None:
            return False
        try:
            if _IS_WINDOWS:
                api = _win_api()
                if not (api.ready and api.resume_ok):
                    return False
                handle = api.open_process(target, _PROCESS_SUSPEND_RESUME)
                if not handle:
                    logger.debug("拿不到恢复权限（pid=%s）", target)
                    return False
                try:
                    return api.nt_call("NtResumeProcess", handle)
                finally:
                    api.close(handle)
            self._posix_signal(target, signal.SIGCONT, tree=False)
            return True
        except ProcessLookupError:
            return False
        except Exception as exc:  # noqa: BLE001
            logger.debug("恢复失败（pid=%s）：%s: %s", target, type(exc).__name__, exc)
            return False

    # -- 终止 ---------------------------------------------------------------
    def terminate(self, pid: int, *, tree: bool = True, grace: float = 3.0) -> bool:
        """终止进程（可选连同整棵进程树）。

        Windows 走 ``taskkill /T /F`` → ``TerminateProcess``；POSIX 走
        ``SIGTERM`` → 等 ``grace`` 秒 → ``SIGKILL``。

        ⚠️ **这是阻塞调用**（最坏 ``grace`` + ``taskkill`` 超时）。别在输出读取
        回调（``_PipeReader`` 那条线程）里直接调：线程被占住就没人读管道，
        Agent 进程会卡死在 ``write`` 上，接着 ``runner`` 会白等满 ``stream_grace``
        才收尾——表现成「任务变慢/超时」，而不是一条明确的错误。

        :param pid: 进程 ID。
        :param tree: 是否连同子进程一起处理。只杀根进程会留下孤儿占着文件和端口。
        :param grace: POSIX 上给对方的退出缓冲；Windows 上用作等待上限。
        :return: 动作是否送达。已经是死的、没权限的都是 ``False``——那不代表异常。
        """
        target = self._usable(pid, "terminate")
        if target is None:
            return False
        try:
            if _IS_WINDOWS:
                return self._terminate_windows(target, tree=tree, grace=grace)
            return self._terminate_posix(target, tree=tree, grace=grace)
        except Exception as exc:  # noqa: BLE001
            logger.debug("终止失败（pid=%s）：%s: %s", target, type(exc).__name__, exc)
            return False

    def _terminate_windows(self, pid: int, *, tree: bool, grace: float) -> bool:
        """Windows 终止路径。

        :param pid: 进程 ID。
        :param tree: 是否需要树语义。
        :param grace: 等 taskkill 生效的上限。
        :return: 是否成功让它停下。
        """
        if tree and _has_taskkill() and _taskkill_tree(pid):
            if self._wait_dead(pid, grace):
                return True
        api = _win_api()
        if api.ready and api.terminate(pid):
            return True
        # 两种硬手段都没成功：可能是权限不够，也可能进程早就退了。
        logger.debug("终止未生效（pid=%s），可能已退出或权限不足", pid)
        return False

    def _terminate_posix(self, pid: int, *, tree: bool, grace: float) -> bool:
        """POSIX 终止路径：先礼后兵。

        :param pid: 进程 ID。
        :param tree: 是否连同进程组一起处理。
        :param grace: SIGTERM 之后等多久再 SIGKILL。
        :return: 是否成功让它停下。
        """
        self._posix_signal(pid, signal.SIGTERM, tree=tree)
        if self._wait_dead(pid, grace):
            return True
        self._posix_signal(pid, signal.SIGKILL, tree=tree)
        return True

    # -- 内部 ---------------------------------------------------------------
    def _identity_capable(self) -> bool:
        """本机是否能**真的**取到「pid 复用时必然改变」的创建时间指纹。

        故意用 ``os.getpid()`` 探一次真调用，而不是只看 ``kernel32`` 打开成功：
        后者只证明 DLL 加载了，**不证明 ``GetProcessTimes`` 绑定成功**。

        这只证明「机制可用」，**不证明对任意目标都取得到**：受保护进程、跨完整性
        级别的目标会被 ``OpenProcess`` 拒掉，那时 :meth:`_identity` 退化成时间锚，
        而 :meth:`identity_mode` 仍是 ``creation_time``。所以能力位报的是「尽力而为
        的必要条件」，逐 pid 的真实情况靠 :meth:`_remember` 的降级日志暴露。

        :return: 实测能取到为真；macOS 等平台为假（调用方据此降级策略）。
        """
        try:
            if sys.platform.startswith("linux"):
                return _linux_starttime(os.getpid()) is not None
            if _IS_WINDOWS:
                api = _win_api()
                if not api.ready:
                    return False
                # GetProcessTimes 需要 PROCESS_QUERY_LIMITED_INFORMATION，拿自己探最稳。
                return api.creation_time(os.getpid()) is not None
        except Exception as exc:  # noqa: BLE001 —— 探针自身失败按「没有能力」处理
            logger.debug("身份指纹探针失败，按无能力处理：%s: %s", type(exc).__name__, exc)
        return False

    def identity_policy(self) -> dict[str, Any]:
        """描述本机的身份校验方式与真实边界，供上层展示 / 决定策略。

        ``capabilities()`` 的每个值都是布尔（上层按位取用），所以「这是哪一档」和
        「时间窗多少秒」这些非布尔事实单独放这里，不去搅乱它的类型契约。

        :return: ``{"mode": "creation_time" | "time_window" | "none",
            "window_seconds": float, "platform": str}``。``window_seconds`` 是
            时间锚的有效期，是**任务级窗口**（覆盖整次派发），不是防复用窗口 ——
            详见 ``_IDENTITY_WINDOW_SECONDS`` 的说明。
        """
        return {
            "mode": self.identity_mode(),
            "window_seconds": _IDENTITY_WINDOW_SECONDS,
            "platform": sys.platform,
        }

    def forget(self, pid: int) -> None:
        """丢弃某个 pid 的身份记录（会话结束时调用最合适）。

        时间窗是任务级窗口，锚在「首次见到」。如果上层在任务收尾时调一次本方法，
        下一轮即使 pid 被复用也会重新登记一次，等于把窗口压回到真正的任务范围。
        不调也不会错（窗口会自己过期），只是会多挡一会儿。

        :param pid: 进程 ID。
        """
        try:
            target = int(pid)
        except (TypeError, ValueError):
            return
        with self._identity_lock:
            self._identities.pop(target, None)
            self._expiry_noted.discard(target)
            self._degrade_noted.discard(target)

    def identity_snapshot(self) -> dict[str, Any]:
        """当前身份指纹状态的一份只读快照（诊断用，**不要**拿它做判定）。

        为什么要有：容量边界是这一类「校验悄悄失效」里唯一无法从返回值看出来的东西 ——
        表满之后 ``terminate`` 被拒是对的，但没人知道「为什么突然拒」。有了它，
        doctor / 测试 / 日志都能问一句「现在登记了多少、离上限还有多远」。

        返回的列表都是拷贝，改它不影响内部状态。

        :return: ``{"mode", "registered", "capacity", "pids", "expiry_noted",
            "degrade_noted", "table_full_warned"}``。
        """
        with self._identity_lock:
            registered = sorted(self._identities)
            expiry_noted = sorted(self._expiry_noted)
            degrade_noted = sorted(self._degrade_noted)
        return {
            "mode": self.identity_mode(),
            "registered": len(registered),
            "capacity": _MAX_IDENTITY_ENTRIES,
            "pids": registered,
            "expiry_noted": expiry_noted,
            "degrade_noted": degrade_noted,
            "table_full_warned": self._table_full_warned,
        }

    def _identity(self, pid: int) -> tuple[str, Any]:
        """取该 pid 的当前身份指纹。

        取不到创建时间时**不报错、不拒绝**，退化成 ``("seen", 此刻)`` —— 进程正在
        退出、procfs 暂时不可读都是常态，误拒的代价比放行大。但本机明明有能力却
        对某个 pid 取不到时，会记**一次** warning：受保护进程 / 跨完整性级别是真实
        存在的情况，「校验生效」和「校验悄悄失效」在外部看起来一模一样。

        :param pid: 进程 ID。
        :return: ``("starttime" | "creation", 值)``；取不到就 ``("seen", 首次见到时刻)``。
        """
        if sys.platform.startswith("linux"):
            starttime = _linux_starttime(pid)
            if starttime is not None:
                return ("starttime", starttime)
        elif _IS_WINDOWS:
            api = _win_api()
            if api.ready:
                created = api.creation_time(pid)
                if created is not None:
                    return ("creation", created)
        self._note_degrade(pid)
        return ("seen", time.monotonic())

    def _note_degrade(self, pid: int) -> None:
        """某个 pid 取不到创建时间时记一次 warning（同一个 pid 只记一次）。

        必须先排除「进程已经退出」：那是最常见的情形，此时取不到创建时间是**预期**
        的，不是降级。把两者混成同一条 warning，等于用一条高频误报把这条真正要报的
        信息埋掉 —— 而这个信息（本机有能力、这个目标却读不到）恰恰是唯一能暴露
        「校验悄悄失效」的痕迹。

        :param pid: 进程 ID。
        """
        if self.identity_mode() != "creation_time":
            # 整机都没能力时不必逐 pid 记账，``identity_mode`` 那侧的说明更准确。
            return
        if pid in self._degrade_noted:
            return
        if not self.is_alive(pid):
            # 进程已退出 / 拿不到句柄：读不到创建时间是常态，不是降级。
            return
        _bounded_add(self._degrade_noted, pid)
        logger.warning(
            "取不到运行中的 pid %s 的创建时间（受保护进程或跨完整性级别？），对该 pid 的"
            "身份校验降级为时间锚：同一次任务内仍可干预，但识别不出窗口内的 pid 复用",
            pid,
        )

    def _remember(self, pid: int) -> tuple[bool, str]:
        """登记一个 pid 的身份（只在首次见到它时调用）。

        表满时的行为是**明确的三选一**（不是「默认 FIFO 然后悄悄失效」）：

        1. 优先淘汰「已经 ``is_alive()`` 为假」的记录 —— 死掉的 pid 不会再被动手，
           留着只是占位；
        2. 全是活进程时，**不为新 pid 腾位置**，直接放弃登记并在首次记一条 warning。
           理由：已登记的记录是唯一提供保护的东西，挤掉一条活的换一条新的，等于
           让「原本能识别的 pid」变成「无法识别」，净损失；
        3. 放弃登记的那个 pid 此后按 ``unknown`` 处理（见 :meth:`_identity_state`）：
           只放行 ``suspend`` / ``resume``，``terminate`` 一律拒绝 —— 它的身份**从未
           被确认过**，而 terminate 不可逆。这是刻意的降级，不是失效：至少不会在表
           满之后突然又能打错人。

        三步都在 ``_identity_lock`` 里做。取指纹（``_identity``）可能是一次
        ``OpenProcess`` / 读 procfs，期间别的线程完全可能把这个 pid 登记了、或者把
        表填满了 —— 所以取完指纹要**复查**容量与是否已登记，不能直接写进去。

        :param pid: 进程 ID。
        :return: ``(是否真的登记成功, 原因)``；原因 ∈ ``registered`` / ``table_full`` /
            ``throttled`` / ``filled_meanwhile``。**不要只看第一项就下结论**：几种失败
            要给出不同措辞（见 :meth:`_identity_state`），把原因压成布尔就没法说准了。
        """
        with self._identity_lock:
            return self._remember_locked(pid)

    def _remember_locked(self, pid: int) -> tuple[bool, str]:
        """``_remember`` 的实体；调用方必须已持有 ``_identity_lock``。

        :param pid: 进程 ID。
        :return: 同 :meth:`_remember`。
        """
        if pid in self._identities:
            return (True, _REGISTERED)

        evicted: int | None = None
        if len(self._identities) >= _MAX_IDENTITY_ENTRIES:
            evicted, scanned = self._evict_dead()
            if evicted is None:
                why = _TABLE_FULL if scanned else _SCAN_THROTTLED
                self._warn_table_full(pid, why=why)
                return (False, why)

        identity = self._identity(pid)

        # 复查：上面那一段跑在没有 GIL 保护语义的空档里（一次系统调用），
        # 别的线程可能刚把这个 pid 登记了，也可能把刚腾出的位置又占满了。
        if pid in self._identities:
            return (True, _REGISTERED)
        if len(self._identities) >= _MAX_IDENTITY_ENTRIES:
            self._warn_table_full(pid, why=_FILLED_MEANWHILE)
            return (False, _FILLED_MEANWHILE)

        self._identities[pid] = identity
        if evicted is not None:
            # 淘汰的是**别人**，所以这条日志必须把两个 pid 都写出来：只写「表已满」
            # 会让人以为提示说的是调用方这个 pid，排查方向直接跑偏。
            logger.debug(
                "身份指纹表已满：淘汰已退出的 pid=%s 以登记 pid=%s（死进程不会再被动手，"
                "它的记录只占位）",
                evicted,
                pid,
            )
        return (True, _REGISTERED)

    def _warn_table_full(self, pid: int, *, why: str) -> None:
        """表满且没腾出位置时留痕（一次实例生命周期内只记一条 warning）。

        :param pid: 没能登记的 pid。
        :param why: 没登记上的原因（见 ``_table_full_detail``）—— 三种原因必须分开写，
            混成一句就变成「我们没看，却断言里面全活着」。
        """
        detail = _table_full_detail(why)
        if self._table_full_warned:
            logger.debug(
                "身份指纹表已满，pid %s 未登记（%s；此后它的 terminate 会被拒绝，"
                "只放行可逆动作）",
                pid,
                detail,
            )
            return
        self._table_full_warned = True
        logger.warning(
            "身份指纹表已满（%d 条，%s）：pid %s 无法登记，此后对它的 terminate 会被拒绝，"
            "只放行可逆动作（suspend/resume）。这不是错误，是容量边界 —— 表满期间"
            "**每个**新 pid 都落在这个状态，最长持续 %.0fs（下一次淘汰扫描腾出位置为止）；"
            "长跑宿主里应在上层收尾时调 forget(pid) 释放",
            len(self._identities),
            detail,
            pid,
            _EVICT_SCAN_INTERVAL,
        )

    def _evict_dead(self) -> tuple[int | None, bool]:
        """淘汰一条「进程已死」的身份记录，给新记录腾位置。

        扫描有频率上限：一次扫描最多 ``_MAX_IDENTITY_ENTRIES`` 次 ``is_alive``，而表满
        之后**每次**新 pid 进来都会走到这里。不限频的话，「表满」这件事本身会变成一个
        比它想避免的问题更严重的性能坑（Windows 上每次探活都是一次 ``OpenProcess``）。

        调用方必须已持有 ``_identity_lock``（``Lock`` 不可重入，这里不再加锁）。

        :return: ``(被淘汰的 pid 或 None, 本次是否真的扫描过)``。第二项用来区分
            「扫过但全是活的」和「节流里没扫」—— 两者都腾不出位置，但原因不同。
        """
        now = time.monotonic()
        if now - self._evict_scan_at < _EVICT_SCAN_INTERVAL:
            return (None, False)
        self._evict_scan_at = now
        for pid in list(self._identities):
            if self.is_alive(pid):
                continue
            self._identities.pop(pid, None)
            return (pid, True)
        return (None, True)

    def _note_expiry(self, pid: int, age: float) -> None:
        """时间锚过期时记一次 warning（同一个 pid 只记一次）。

        必须在**放行 ``resume`` 的时候也记**：否则「校验悄悄没了」这件事只有等到
        某次被拒绝才会暴露，而可逆动作永远不会被拒绝。

        :param pid: 进程 ID。
        :param age: 距首次见到它过了多少秒。
        """
        if pid in self._expiry_noted:
            return
        _bounded_add(self._expiry_noted, pid)
        logger.warning(
            "pid %s 的身份校验窗口已过期（首次见到在 %.0fs 前，窗口 %.0fs）："
            "此后对该 pid 只能放行可逆动作，terminate 会被拒绝",
            pid,
            age,
            _IDENTITY_WINDOW_SECONDS,
        )

    def _identity_state(self, pid: int) -> tuple[str, str]:
        """判断当前占用 ``pid`` 的还是不是我们最初见到的那一个。

        **返回三态 + 拒绝理由**（``("same", "")`` 表示可以动手）。分三态而不是布尔，
        是因为「确知打错人」和「无法确认是谁」的处置完全不同：前者三项全拒（没有
        任何理由去碰一个已知的无关进程），后者只拒不可逆动作（否则会冻住撤不回）。

        理由必须说准：把「本机没有身份校验能力」写成「pid 被复用了」，会让人去查一个
        不存在的复用问题 —— 这两句话会把人引向完全相反的方向。

        归入 ``unknown`` 的四种情况各有一套措辞：平台无能力、窗口过期、指纹表满未能
        登记、以及（下文的）取不到指纹时的 fail-open。它们要排查的东西完全不同，所以
        不能共用一句「无法确认」。

        已知局限（写在这里以免后人误以为它是硬保证）：**首次见到时无法回溯** ——
        如果进程在我们看到它之前就已经被复用，记下的就是新进程的指纹。
        TODO: 更彻底的做法是把锚点前移到进程创建处（``attach_pid`` 那一刻是唯一
        100% 确定「这个 pid 是我亲生的」的时刻），在那里登记创建时间，比对就从
        「和第一次想动手时看到的那个进程比」变成「和出生时刻比」，
        ``_identities`` 的容量与淘汰问题也一并消失。

        :param pid: 进程 ID。
        :return: ``(state, reason)``，state ∈ ``same`` / ``reused`` / ``unknown``。
        """
        if self.identity_mode() == "none":
            # 三档里唯一「什么都不知道」的一档：没有时间锚可比，也没有存活可观察。
            return (
                _IDENTITY_UNKNOWN,
                # 措辞刻意不提「拒绝」：理由会被可逆动作的**放行**日志原样引用，
                # 写成「…，拒绝不可逆操作」会让人在一条「放行 suspend」的日志里读到
                # 「拒绝」，反而更糊涂。该不该拒由 ``_usable`` 按动作决定。
                f"本机无法校验进程身份（{sys.platform}：进程 API 不可用）",
            )

        recorded = self._identities.get(pid)
        if recorded is None:
            # 首次见到：此刻只能信它（这是已知局限，见 docstring）。
            registered, why = self._remember(pid)
            if registered:
                return (_IDENTITY_SAME, "")
            # **登记不上 ≠ 比对通过。** 表满且腾不出位置时 ``_remember`` 放弃登记，
            # 于是这个 pid 永远走不到下面的比对 —— 它的身份从头到尾没被确认过。
            # 归 ``unknown`` 而不是 ``same``：若归 ``same``，容量边界就变成了
            # 「对任意新 pid 关闭身份校验」的能力悬崖，表一满 terminate 又能打错人。
            # 措辞是第四种，与「复用」「窗口过期」「平台无能力」都不同 —— 这个
            # 原因要查的是 forget(pid) 的收尾 / 容量，不是 pid 复用，也不是换平台。
            # 「节流」/「扫过全活」/「取指纹期间被并发填满」要分开写：只有中间那种
            # 的前提是**真的看过**，混成一句就是在没看的时候断言「全在运行」。
            return (
                _IDENTITY_UNKNOWN,
                f"身份指纹表已满（{len(self._identities)}/{_MAX_IDENTITY_ENTRIES} 条）"
                f"且未能腾出位置（{_table_full_detail(why)}），该 pid 未能登记，无法确认身份",
            )

        kind, value = recorded
        if kind == "seen":
            # 时间锚档：没有创建时间可比，只能按任务级窗口近似。
            age = time.monotonic() - float(value)
            if age <= _IDENTITY_WINDOW_SECONDS:
                return (_IDENTITY_SAME, "")
            self._note_expiry(pid, age)
            return (
                _IDENTITY_UNKNOWN,
                f"身份校验窗口已过期（首次见到在 {age:.0f}s 前，窗口 {_IDENTITY_WINDOW_SECONDS:.0f}s），"
                f"无法确认该 pid 是否还是原进程",
            )

        current = self._identity(pid)
        if current[0] != kind:
            # 此刻取不到指纹（进程正在退出、procfs 暂时不可读）：fail-open。
            # 真正的保护来自「能取到时一定比对」，而不是在这里误拒。
            return (_IDENTITY_SAME, "")
        if current[1] == value:
            return (_IDENTITY_SAME, "")
        return (
            _IDENTITY_REUSED,
            f"已被其他进程复用（{kind} 指纹不匹配：{value} → {current[1]}）",
        )

    def _usable(self, pid: Any, action: str) -> int | None:
        """校验 PID 是否是我们允许动手的对象。

        三道闸：pid 合法、不是自己、**身份对得上**。``suspend`` / ``resume`` /
        ``terminate`` 都从这里过，所以身份校验不存在「漏掉某个入口」的问题。

        第三道闸按**可逆性**分流，这是本层唯一需要区分动作的地方（理由见模块
        docstring 的「可逆性非对称」一节）：

        ============  ==================  ==============================
        身份状态       ``suspend/resume``  ``terminate``
        ============  ==================  ==============================
        ``same``      放行                放行
        ``reused``    拒绝（确知打错人）  拒绝（确知打错人）
        ``unknown``   放行 + warning      拒绝 + 各自措辞
        ============  ==================  ==============================

        ``reused`` 三项全拒是有意的：那不是「不确定」，而是「确知这个 pid 已经属于
        别人」，没有任何理由去碰它。而 ``unknown`` 只拒不可逆动作，否则会出现
        「冻住了、解不开、也杀不掉」—— 模块自己制造一个撤不回来的状态，比误碰一下
        严重得多。

        :param pid: 原始 PID。
        :param action: 动作名（用于日志，也用于判定可逆性）。
        :return: 规范化后的 PID；不允许动手时返回 ``None``。
        """
        try:
            target = int(pid)
        except (TypeError, ValueError):
            return None
        if target <= 0:
            return None
        if target == os.getpid():
            # 对自己动手 = 把插件和宿主一起冻住，永远不许。
            logger.warning("拒绝对自身进程执行 %s（pid=%s）", action, target)
            return None

        state, reason = self._identity_state(target)
        if state == _IDENTITY_SAME:
            return target
        if state == _IDENTITY_REUSED:
            # 真的比对过且不匹配：这里才可以理直气壮说「被复用」。
            logger.warning("拒绝 %s：pid %s %s", action, target, reason)
            return None
        if action in _REVERSIBLE_ACTIONS:
            # 无法确认身份，但动作可逆：放行的代价只是「碰了无关进程一下」，
            # 而 fail-closed 的代价是冻住撤不回。注意 —— 这里**不是**为了绕过校验，
            # 而是这一档唯一不会把模块自己逼进死角的选法。
            logger.warning(
                "放行 %s：pid %s 无法确认身份，但该动作可逆，宁可放行（%s）",
                action,
                target,
                reason,
            )
            return target
        logger.warning("拒绝 %s：pid %s %s", action, target, reason)
        return None

    @staticmethod
    def _posix_signal(pid: int, sig: int, *, tree: bool) -> None:
        """发一个 POSIX 信号（``tree`` 时优先发给整个进程组）。

        :param pid: 进程 ID。
        :param sig: 信号。
        :param tree: 是否对进程组发。
        """
        if tree and hasattr(os, "killpg"):
            try:
                pgid = os.getpgid(pid)
            except OSError:
                pgid = -1
            # 绝不信号自己的进程组 —— 那等于自杀。
            if pgid > 0 and pgid != os.getpgrp():
                try:
                    os.killpg(pgid, sig)
                    return
                except OSError:
                    pass
        os.kill(pid, sig)

    def _wait_dead(self, pid: int, grace: float) -> bool:
        """轮询等进程消失。

        :param pid: 进程 ID。
        :param grace: 最长等待秒数（``<= 0`` 表示只探一次）。
        :return: 等到了返回 ``True``。
        """
        deadline = time.monotonic() + max(0.0, float(grace or 0.0))
        while True:
            if not self.is_alive(pid):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_INTERVAL)
