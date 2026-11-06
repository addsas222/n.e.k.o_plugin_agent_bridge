"""子进程派发层。

必须先弄清楚的四个坑（每一个都是「不报错、只在生产里挂死」的类型）：

1. **stdin 必须隔离。** 桥接服务自己的 stdin 承载 MCP 的 JSON-RPC 流；如果被派发
   出去的 Agent CLI 继承它，两边会互相抢读，协议立刻崩掉。所以一律用 DEVNULL。
2. **不能用 asyncio 的子进程等待。** asyncio 的 `Process.wait()` 要等所有管道 EOF
   才返回，而 Agent CLI 派生的孙进程会一直握着管道写端，导致超时机制失效。
   详见 `_execute_blocking` 的说明。
3. **超时要连进程树一起清。** 这些 Agent CLI 自身会再派生 node/bun/python 子进程，
   只杀父进程会留下孤儿进程继续占用文件与端口。Windows 上用 taskkill /T /F。
4. **输出要限幅但不能停止读取。** 读满上限后若不再读，管道写满会让子进程永久阻塞；
   所以上限只约束内存，不约束读取动作。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Iterable

from .audit import build_client, gated_pre_check
from .config import BridgeConfig, normalize_path
from .registry import (
    CMD_FALLBACK,
    AgentSpec,
    ResolvedCommand,
    build_argv,
    build_version_argv,
    redact_argv,
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_VERSION_RE = re.compile(r"\d+\.\d+")
_NOISE_RE = re.compile(
    # Node 横幅：``Warning`` / ``ExperimentalWarning`` / ``DeprecationWarning``。
    # 必须用 ``\w*warning`` 而不是 ``warning``：Node 打印的是
    # ``(node:1234) ExperimentalWarning: ...``，按 ``warning`` 起头匹配会漏掉 ——
    # 漏在**安全侧**（这行算产出，于是不判未登录），但噪声表就形同虚设了。
    r"^\(?node:\d+\)?\s*(?:\w*warning|deprecat)"
    r"|^experimentalwarning"                    # 无 node 前缀的同名横幅
    r"|^warning:"                               # 独立的 warning 横幅
    r"|^warning$"
    r"|^npm\s+warn"                             # npm 告警（deprecated / peer 之类）
    # npm 的**样板尾部**与栈帧。这一条必须留着：``npm ERR! A complete log of this run
    # can be found in:`` 是每次 npm 失败的最后一行，放它算产出，就等于「凡是经 npm
    # 起手的失败都有产出」，于是「CLI 未登录 → npm 随之报错」永远判不出登录。
    r"|^npm\s+(?:ERR!|error)\s+(?:A complete log of this run|errno|at\s)",
    re.IGNORECASE,
)
"""启动期噪声行的**完整形态**（Node/npm 的告警横幅之类）。

刻意不用「以某词开头」的前缀表：原表里有 ``"npm "`` 和 ``"node:"``，于是
``npm install 完成，添加 3 个包``（真实结论）被当成噪声丢掉。丢掉唯一一行就等于判定
「这次没干活」—— 误报未登录，那个 Agent 会被拉进 ``_needs_login``，重载插件前完全
不可用。

判据只能是「这行**本身**是警告」，不能是「这行以某个词开头」，更不能是「这行提到了
那些词」（后者连 ``编译失败：3 个 error，2 个 warning`` 也会被吃掉）。

**npm 的 ``ERR!`` 要分两档，不能整片归一类。** 整片算噪声（旧写法 ``err!``）会让
``npm ERR! code E404`` 这种**真错误**被判成「没干活」；整片算产出则让
``A complete log of this run...`` 这种样板行替每一次 npm 失败兜底。所以只把
**样板尾部 / 栈帧 / errno 行**归噪声，``code E404`` 这类结论行算产出 —— 与
``_has_substantive_output`` 那条「判不准时偏向有产出」同向。
"""


def _is_noise(line: str) -> bool:
    """这一行本身是不是启动期噪声（已 strip 的单行）。

    :param line: 单个非空行（可含首尾空白，内部自行处理）。
    :return: 是噪声为 ``True``。
    """
    return bool(_NOISE_RE.match(line.strip()))

_IS_WINDOWS = sys.platform == "win32"
_READ_CHUNK = 65536
logger = logging.getLogger(__name__)

#: 哨兵运行时（由插件 startup 注入；未注入时为 ``None``，派发行为完全不变）。
#: 为什么用模块级而不是函数参数：``run_agent`` 有多个调用点（对话入口、并行派发、
#: 测试），加参数会把签名扩散到每一条路径；而运行时的语义本来就是进程级单例。
_sentinel_runtime: Any = None


def set_sentinel_runtime(runtime: Any) -> None:
    """注入哨兵运行时。

    :param runtime: ``SentinelRuntime`` 实例；传 ``None`` 表示卸载（关闭审计）。
    """
    global _sentinel_runtime
    _sentinel_runtime = runtime


_ENV_STRIP = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONEXECUTABLE",
    "PYTHONUSERBASE",
    "PYTHONLEGACYWINDOWSSTDIO",
)
"""派发给子进程前要剔除的 Python 环境变量。

宿主场景：N.E.K.O 的插件进程跑在自带的嵌入式 Python 3.11 上，其 ``resources/bin/``
被整体加进了 sys.path，且存在 ``python311.dll``。这些变量一旦传给别的 Python 解释器
（qwenpaw 的 venv、或用户自己在 Agent 里起的脚本），对方会以
``Failed to import encodings module`` / ``python311.dll conflicts`` 直接失败。
"""

_STREAM_GRACE = 5.0
"""读完最后一段输出的宽限期（秒）。

子进程退出后，如果它派生的 node/bun 孙进程还活着，管道的写端就没有关闭，
读取线程永远等不到 EOF。给 5 秒让正常输出落完，到点就放弃等待并保留已有数据。"""

_AUTH_HEAD_CHARS = 400
"""鉴权提示的搜索范围：只看输出开头这么多字符。"""

_AUTH_MAX_CHARS = 800
"""只有「短输出」才可能是登录提示。

真跑完的 Agent 一定会把结果写进 stdout，见 ``_detect_auth_failure``。"""

_PROGRESS_MAX_CHARS = 60
"""单行超过这个长度，就不再按「进度提示」处理。

进度提示是「一行几个字的动态」（``Thinking…``、``reading src/app.py``），不会写成
一句 60 字的话；而真实结论常常正好是一句话。所以长度判据必须落在**单行**上。

为什么不能落在累计总长上：``int main(){return 0;}`` 是 19 个字符的完整答案，
``Thinking…`` 是 9 个字符的进度提示 —— **两者都短**。任何单一累计阈值都必然在
某一侧出错：调高了，短答案被判成「没干活」（误报登录，且该 Agent 会被永久拉黑，
见 ``__init__.py`` 的 ``_needs_login``）；调低了，进度提示被当成产出（漏报登录、
假成功）。形态判据不需要在这两种错误之间选。
"""

_PROGRESS_LINE_RE = re.compile(
    r"(?:\.{3,}|…)\s*$"                                    # 省略号收尾
    r"|^(?:thinking|reading|writing|editing|searching|analy[sz]ing|planning"
    r"|generating|loading|fetching|running|checking|building|compiling"
    r"|installing|waiting|starting|processing|scanning|reviewing|applying)\b"
    r"|^[●⏺•·$>✦✻✽|]\s*",                                  # 工具调用 / 状态标记前缀
    re.IGNORECASE,
)
"""「像进度提示」的行形态：省略号收尾、动名词开头、工具标记前缀。

只收**能认出来**的形态。曾试过再加一条「单个短词」（`done` / `ok` / `42`）来盖住
CLI 的状态回显，已撤回：那个边界只反映「里面有没有标点」，`ok` / `PASS` / `LGTM`
会被判成没干活，而 `Done!` / `v1.2` 能过 —— 它区分的不是「是不是答案」，只是
「像不像我列的那几个词」。

判不准时一律偏向「有产出」：另一侧的后果是该 Agent 被拉进 `_needs_login`，在重载
插件前完全不可用（见 ``__init__.py``），代价与「多一句提示」不是一个量级。
"""

_AUTH_PATTERNS = (
    re.compile(r"authentication\s+required", re.IGNORECASE),
    re.compile(r"unauthenticated", re.IGNORECASE),
    re.compile(r"not\s+(?:logged\s?in|authenticated|signed\s?in)", re.IGNORECASE),
    re.compile(r"(?:please\s+)?use\s+`?/login`?", re.IGNORECASE),
    re.compile(r"(?:please\s+)?run\s+`?/login`?", re.IGNORECASE),
    re.compile(r"invalid\s+api\s*key", re.IGNORECASE),
    re.compile(r"credentials?\s+(?:expired|invalid|not\s+found|missing)", re.IGNORECASE),
    re.compile(r"请(?:先)?登录|尚未登录|未登录|需要登录|登录后(?:再|才)"),
)
"""「这台 CLI 还没登录」的常见措辞（中英文都收）。"""


@lru_cache(maxsize=64)
def _compile_extra_patterns(extra_patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """把清单里声明的额外鉴权特征编译成正则，坏正则只丢这一条。

    清单是**外部数据**（用户可以手改），一条写错的正则不该让每次派发都抛
    ``re.error`` —— 那会把「未登录」变回「调用失败」，掩盖真正的原因。
    """
    compiled: list[re.Pattern[str]] = []
    for raw in extra_patterns or ():
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            compiled.append(re.compile(text, re.IGNORECASE))
        except re.error:
            logging.getLogger(__name__).warning(f"auth_patterns 里的正则无效，已忽略：{text!r}")
    return tuple(compiled)


def _match_head(stdout: str, stderr: str, patterns: Iterable[re.Pattern[str]]) -> str:
    """在 stderr 开头找第一个命中的特征，返回**命中那一行**（否则空串）。

    三条门槛与鉴权判定共用：stderr 要短、只看开头、stdout 不能有实质内容。
    """
    text = (stderr or "").strip()
    if not text or len(text) > _AUTH_MAX_CHARS:
        return ""
    head = text[:_AUTH_HEAD_CHARS]
    matched = next((p for p in patterns if p.search(head)), None)
    if matched is None or _has_substantive_output(stdout):
        return ""
    for line in text.splitlines():
        if line.strip() and matched.search(line):
            return line.strip()
    return next((line.strip() for line in text.splitlines() if line.strip()), head[:120])


def _detect_auth_failure(
    stdout: str,
    stderr: str,
    extra_patterns: Iterable[str] = (),
) -> str:
    """识别「CLI 因为没登录而拒绝干活」，返回提示首行；不是这种情况就返回空串。

    **为什么必须单独判**：这些 CLI 未登录时**退出码是 0**，只往 stderr 打一行
    「Authentication required. Please use /login command to sign in」。
    于是 ``AgentResult.ok`` 判定为成功，那行登录提示被当成「任务输出」原样交给
    上层模型 —— 模型只能从一段莫名其妙的输出里猜「哦原来要登录」。

    **为什么还要 ``extra_patterns``**：下面 ``_AUTH_PATTERNS`` 是按最初 9 个 CLI 的
    真实措辞写的，换个产品就会漏。实测两个新 Agent 都会漏判：

    - gemini：``Please set an Auth method in your …settings.json or specify one of
      the following environment variables before running: GEMINI_API_KEY, …``
      （退出码 **41**）
    - command-code：``Error: Authentication failed. Please run "cmdc login" first.``
      （退出码 **3**）

    漏判的后果不是「假成功」而是「说不清」：它们退出码非 0，于是被报成普通 FAILED，
    既不进 ``needs_login`` 集合（不会被跳过），也不提示「这台没登录，换一个」。
    而且同一族 CLI 里也有给 0 的（claude 就是），所以只靠退出码无法覆盖。
    每个 Agent 在 ``providers/<id>/provider.toml`` 里自带 ``auth_patterns``，
    由 ``run_agent`` 透传进来。全局表继续兜住老 Agent，新 Agent 不必改这里一行代码。

    **不能要求 stdout 为空**：启动期真出问题时，CLI 往往先往 stdout 打几行噪声
    （codebuddy 的 PowerShell 警告就是），登录提示照样在 stderr 里，一旦用
    「stdout 非空就不判」当闸门，这类失败会被报成 ``ok=True``。

    误报防护改成两条更准的：
    1. **只看输出开头**（``_AUTH_HEAD_CHARS``）—— 真跑完任务的 Agent 结果在
       stdout 正文里，不会把登录措辞放在最前面；
    2. **要求 stdout 里没有实质内容** —— 噪声行（警告/空行）与进度提示都不算实质
       产出，判据见 ``_has_substantive_output``：按**行的形态**判断，不看总长度。
       这样「让人写登录页」不会被误伤，而「未登录时先打一句 Thinking…」也不会把
       整次判定废掉。
    """
    patterns = _AUTH_PATTERNS + _compile_extra_patterns(extra_patterns)
    return _match_head(stdout, stderr, patterns)


def _looks_like_progress(line: str) -> bool:
    """这一行是否只是进度/状态提示，而不是任务结果。

    :param line: 已 strip 的单个非空行。
    :return: 像进度提示为 ``True``。
    """
    if len(line) > _PROGRESS_MAX_CHARS:
        # 进度提示不会写成一句 60 字的话；长句一律当正文，宁可漏报登录。
        return False
    return bool(_PROGRESS_LINE_RE.search(line))


def _has_substantive_output(stdout: str) -> bool:
    """stdout 里是否有「像任务结果」的内容（滤掉警告、空行、纯符号行）。

    判据是**行的形态**，不是总长度：只要有一行不像进度提示，就说明这次真的产出了
    东西。长度只在单行内做辅助判断（见 ``_PROGRESS_MAX_CHARS``）。

    为什么不能用累计总长：``int main(){return 0;}`` 是 19 个字符的完整答案，
    ``Thinking…`` 是 9 个字符的进度提示 —— **两者都短**。累计阈值调高了，短答案被
    判成「没干活」，该 Agent 会被 `_needs_login` 永久拉黑（只能重载插件解除）；
    调低了，进度提示被当成产出，未登录被报成成功。形态判据不必在这两种错误里选。
    """
    for line in strip_ansi(stdout or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if _is_noise(stripped):
            continue
        if lowered.startswith("at "):
            # 栈帧行（``at foo (file:12)``）是运行时噪声，不是结论。
            continue
        if not _looks_like_progress(stripped):
            return True
    return False



def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _pick_version_line(text: str) -> str:
    """挑出版本那一行。

    有些 CLI 会先往 stdout/stderr 吐 Node 的实验特性警告，直接取第一行会拿到警告
    而不是版本号，所以先滤掉噪声行，再优先取含 `数字.数字` 的行。
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    # Node 的告警常占好几行，末行形如 "Use `node --trace-warnings ...`"，一并滤掉。
    # 这里保留 ``"warning" in`` 的子串判断（与产出判据不同）：挑版本号时宁可跳过
    # 一句提到 warning 的行，代价只是版本号显示得朴素一点，不会造成误判。
    candidates = [
        ln for ln in lines
        if not _is_noise(ln)
        and "warning" not in ln.lower()
    ]
    if not candidates:
        candidates = lines
    for line in candidates:
        if _VERSION_RE.search(line):
            return line
    return candidates[0]


@dataclass
class AgentResult:
    agent: str
    label: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    output_truncated: bool = False
    command_preview: str = ""
    error: str = ""
    needs_login: bool = False
    """CLI 因为没登录而拒绝执行（``error`` 以 ``AGENT_NEEDS_LOGIN:`` 开头）。"""
    pid: int | None = None
    """被派发子进程的 PID（哨兵挂载用；拿不到就是 ``None``）。"""
    blocked_by_sentinel: bool = False
    """任务被哨兵 L1 预检拦下（``error`` 以 ``AGENT_BLOCKED_BY_SENTINEL:`` 开头）。"""
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.error

    def output_text(self) -> str:
        """优先返回 stdout；为空时退回 stderr（很多 CLI 把结果写 stderr）。"""
        return self.stdout.strip() or self.stderr.strip()


class _PipeReader(threading.Thread):
    """按上限收集一个管道，且**即使调用方不再等待也保留已读到的部分**。

    两个反直觉但必须处理的点：

    1. 达到上限后不能停止读取。管道缓冲区被写满后子进程会阻塞在 write 上永不退出，
       于是整个调用只能等超时。上限只约束内存，不约束读取动作。
    2. 读循环不能无条件等 EOF。这些 Agent CLI 会派生 node/bun 孙进程，它们同样继承
       着管道的写端；父进程退出后写端仍未关闭，EOF 永远不来（详见下方 _execute 的说明）。
       所以本线程是 daemon，调用方只给固定宽限期，到点就放弃等待。

    ``on_chunk`` 是给上层做**实时分析**的钩子（把输出流结构化成人能看懂的事件）。
    两个设计约束都来自上面的第 1 点：**回调抛出的任何异常都必须在这里吞掉**——本线程
    一死管道就没人在读，缓冲区写满后子进程会永远卡在 write 上；同时回调拿到的是
    **原始未截断**的块，因为限幅只约束内存、解析需要完整字节。
    """

    def __init__(
        self,
        pipe: Any,
        limit: int,
        on_chunk: Callable[[bytes], None] | None = None,
    ) -> None:
        super().__init__(daemon=True)
        self.pipe = pipe
        self.limit = limit
        self.on_chunk = on_chunk
        self.buf = bytearray()
        self.truncated = False

    def run(self) -> None:
        try:
            while True:
                chunk = self.pipe.read(_READ_CHUNK)
                if not chunk:
                    break
                room = self.limit - len(self.buf)
                if room > 0:
                    self.buf.extend(chunk[:room])
                    if room < len(chunk):
                        self.truncated = True
                else:
                    self.truncated = True
                # 先落缓冲再回调：回调再慢也不该让我们少存数据。
                self._emit(chunk)
        except (OSError, ValueError):
            # 管道被对端关闭，或调用方为了解除阻塞而 close 了读端
            pass

    def _emit(self, chunk: bytes) -> None:
        """把一块原始输出交给观察者，**失败只记日志**。

        不能把回调用 try 包进外层：外层的 ``except (OSError, ValueError)`` 只兜管道
        错误，而观察者（解析器/事件账本）任何异常都会顺着 run() 冒出去杀掉读取线程。
        那正是本类文档里说的死锁：线程一死→没人读管道→子进程卡死在 write 上。
        """
        if self.on_chunk is None:
            return
        try:
            self.on_chunk(chunk)
        except Exception as exc:  # noqa: BLE001 —— 观察者失败绝不能杀死读取线程
            logger.debug("输出流回调失败（已忽略，继续读取）：%s: %s", type(exc).__name__, exc)

    @property
    def data(self) -> bytes:
        return bytes(self.buf)


def _close_in_background(pipe: Any) -> None:
    """在后台线程里关闭管道。

    不能在当前线程直接 close：FileIO.close() 会等待该 fd 上正在进行的 read 返回，
    而那个 read 正卡在孙进程占着的写端上 —— 直接 close 会把调用方拖到孙进程结束
    （实测多等 10 秒以上）。丢给 daemon 线程去收尾，写端一断开它自然就完成了。
    """

    def _run() -> None:
        try:
            pipe.close()
        except (OSError, ValueError):
            pass

    threading.Thread(target=_run, daemon=True).start()


def _kill_tree_sync(process: subprocess.Popen[bytes]) -> None:
    """同步结束进程及其派生进程。"""
    pid = process.pid
    if pid is None:
        return
    if _IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass


def _execute_blocking(
    argv: list[str],
    *,
    cwd: str | None,
    timeout: int,
    output_limit: int,
    env: dict[str, str],
    stream_grace: float,
    on_spawn: Callable[[int], None] | None = None,
    on_chunk: Callable[[bytes], None] | None = None,
) -> tuple[int | None, str, str, bool, bool, str, int | None]:
    """同步跑一条命令。返回 (exit_code, stdout, stderr, 超时, 输出截断, 错误, pid)。

    两个可选钩子，语义不同，别混用：``on_spawn`` 在进程刚起来时给一次 pid（哨兵挂接），
    ``on_chunk`` 在**每读到一块 stdout** 时给一次原始字节（实时结构化分析）。
    ``on_chunk`` 只挂 stdout：结构化协议（stream-json / rollout JSONL）都走 stdout，
    stderr 是诊断噪声，混进来只会让事件流变脏；两路都挂还会让同一条日志出现两次。

    **为什么这里刻意用阻塞式 subprocess 而不是 asyncio 的子进程**：

    asyncio 的 `Process.wait()` 并不是「进程退出就返回」。看
    `asyncio/base_subprocess.py::_try_finish`，exit waiter 只在这两种情况被唤醒：
    管道从未连接成功，或者**所有管道都已断开（即读到 EOF）**。

    而 Agent CLI 会派生 node/bun 孙进程，它们继承着管道的写端。父进程退出后写端仍
    被孙进程握着，EOF 永远不会到来，于是 `wait()` 一直挂到孙进程自己结束为止 ——
    实测一个退出 0.5 秒的父进程配一个活 20 秒的孙进程，`wait()` 就是 20 秒才返回，
    用户配置的超时完全失效。

    阻塞式 `Popen.wait(timeout=...)` 走的是操作系统层面的进程句柄等待，不受管道状态
    影响，实测同一场景 0.5 秒返回。读取交给后台 daemon 线程，配合固定宽限期，
    于是「进程退出」与「输出读完」两件事彻底解耦。
    """
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        # bufsize=0 很关键。默认会拿到 BufferedReader，而 BufferedReader.read(n)
        # 的语义是「读满 n 字节或遇到 EOF 才返回」—— 在我们这种「父进程退出、孙进程
        # 仍占着写端」的场景里就永远返回不了。而且 BufferedReader.close() 要抢
        # 同一把缓冲区锁，会把想在宽限期后强行收尾的调用方一起卡死。
        # 裸 FileIO 的 read(n) 是单次系统调用，有多少返回多少，关闭也不会阻塞。
        "bufsize": 0,
    }

    # 工作目录与 PWD 必须一起处理。有些 CLI 不看 cwd，而是读 PWD 环境变量来决定
    # 工作目录；如果宿主是从 Git Bash / WSL 启起来的，PWD 会是 `/c/Users/...`
    # 这种 MSYS 形式，CLI 拿去 chdir 会直接失败（实测 opencode 就是这么挂的）。
    workdir = normalize_path(cwd) if cwd else os.getcwd()
    if os.path.isdir(workdir):
        kwargs["cwd"] = workdir
        env = dict(env)
        env["PWD"] = workdir
    kwargs["env"] = env

    if _IS_WINDOWS:
        # 桌面端宿主下不要弹出控制台窗口
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    try:
        process = subprocess.Popen(argv, **kwargs)
    except (OSError, ValueError) as exc:
        # 注意返回值必须是完整的 7 元组：少一个 pid 会在解包处直接 ValueError，
        # 把「启动失败」变成「派发层炸了」，错误信息全丢。
        return None, "", "", False, False, f"无法启动进程：{exc}", None

    # 进程一拉起来就通知观察者（哨兵挂接靠它拿 pid）。钩子必须**瞬时返回**：
    # 这里还在读管道的关键路径上，任何阻塞都会拖慢输出收集。
    if on_spawn is not None:
        try:
            on_spawn(process.pid)
        except Exception as exc:  # noqa: BLE001 —— 观察钩子失败不得影响派发
            logger.warning("派发后置钩子失败（已忽略）：%s: %s", type(exc).__name__, exc)

    assert process.stdout is not None and process.stderr is not None
    out_reader = _PipeReader(process.stdout, output_limit, on_chunk)
    err_reader = _PipeReader(process.stderr, output_limit)
    out_reader.start()
    err_reader.start()

    timed_out = False
    error = ""
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        error = f"超过 {timeout}s 未结束"
        _kill_tree_sync(process)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    # 宽限期**每个读线程各给一份**，不复用同一个 deadline。
    #
    # 曾经这里是共享的那一份，理由是「否则两个管道会把等待时间翻倍」—— 那个权衡是
    # 错的：它省下的只是异常路径上的几秒，代价却是把「未登录」判成「成功」。因为
    # join 按 (stdout, stderr) 顺序进行，stdout 被孙进程握着写端时会吃光整个预算，
    # 紧接着 err_reader 的 join 因 remaining <= 0 被直接跳过，stderr 拿到空串 →
    # `_match_head` 的 `if not text` 短路 → 不做鉴权判定 → needs_login=False 且
    # error="" → exit_code=0 时 `AgentResult.ok` 变成 True（而那个 CLI 其实只说了
    # 一行「Authentication required」）。
    #
    # 这个「独立预算」的真实代价，实测过，不要按「只差一点点」理解：
    #
    # * 正常路径**零额外开销** —— 父进程退出即两个管道 EOF，两个 join 都瞬时返回；
    # * 异常路径最坏 **2 × stream_grace**：两个 reader 虽并行跑，但等的是**同一个
    #   事件**（EOF）。孙进程活着时两个 pipe 都不会 EOF，两个 join 各自等满自己的
    #   预算，所以是 sum 不是 max。生产默认 5.0s → 最坏 10.0s（实测 grace=2.0 时
    #   两侧分别 4.42s / 4.49s）。
    # * 「两侧同时被占」**不是边角情况**：孙进程 `Popen()` 不重定向时天然继承
    #   stdout 与 stderr 两个 fd —— 那正是这个宽限期存在的场景本身。
    # * stderr 的实际捕获窗口是 **grace ~ 2×grace 之间自适应**，不是 grace：err 的
    #   预算从 `out_reader.join()` **返回之后**才开始算，所以 stdout 那侧被占得越久，
    #   stderr 的截止时刻越往后延（生产默认即 5s ~ 10s；写成「5 秒之后就没了」会让
    #   人低估已有保护）。
    #
    # 为什么仍然接受这个代价：宽限期只在「孙进程握着写端」时被吃满，而 stderr 判错
    # 会让整个审计判反方向（假成功没人会去查）。宁可多等 10 秒，也不要一个说
    # 「成功」的假结果。
    for reader in (out_reader, err_reader):
        reader.join(stream_grace)

    stdout_text = strip_ansi(out_reader.data.decode("utf-8", errors="replace"))
    stderr_text = strip_ansi(err_reader.data.decode("utf-8", errors="replace"))
    truncated = out_reader.truncated or err_reader.truncated

    # 仍被孙进程占着写端的读线程会一直卡在 read()。交给后台线程收尾，
    # 免得每来一个这样的任务就多挂一个占着 fd 的线程。
    for reader, pipe in ((out_reader, process.stdout), (err_reader, process.stderr)):
        if reader.is_alive():
            _close_in_background(pipe)

    return process.returncode, stdout_text, stderr_text, timed_out, truncated, error, process.pid


async def _execute(
    argv: list[str],
    *,
    cwd: str | None,
    timeout: int,
    output_limit: int,
    env_overrides: dict[str, str] | None = None,
    stream_grace: float = _STREAM_GRACE,
    on_spawn: Callable[[int], None] | None = None,
    on_chunk: Callable[[bytes], None] | None = None,
) -> tuple[int | None, str, str, bool, bool, str, int | None]:
    """在线程里跑 _execute_blocking，保持调用方是 async 的。

    返回值与 ``_execute_blocking`` 完全一致：
    ``(exit_code, stdout, stderr, timed_out, truncated, error, pid)``。

    ``on_spawn`` / ``on_chunk`` 原样透传给阻塞实现。注意两个回调都在**工作线程**里
    被调用，不是事件循环线程；它们必须自己保证线程安全且瞬时返回（阻塞读取线程
    等于拖慢整条输出收集链路）。
    """
    env = os.environ.copy()
    # 派发出去的 Agent CLI 里也有 Python 实现的（例如 qwenpaw 用自己的 venv）。
    # 当本进程跑在 N.E.K.O 的嵌入式 Python 3.11 里时，PYTHONHOME / PYTHONPATH 会指向
    # 宿主自己的 bin 目录（含 python311.dll 与顶层 asyncio/），那些子解释器会因此直接
    # 起不来。在唯一的子进程入口处统一清洗，覆盖 run_agent 与 probe_version 两条路径。
    # 顺序刻意为「先清洗后合并」：如果调用方显式给了这些变量，仍然以调用方为准。
    for _name in _ENV_STRIP:
        env.pop(_name, None)
    if env_overrides:
        env.update(env_overrides)

    return await asyncio.to_thread(
        _execute_blocking,
        argv,
        cwd=cwd,
        timeout=timeout,
        output_limit=output_limit,
        env=env,
        stream_grace=stream_grace,
        on_spawn=on_spawn,
        on_chunk=on_chunk,
    )


_SENTINEL_ATTACH_DELAY = 2.0
"""进程拉起后延迟多久再登记会话（秒）。

进程刚起来的那一瞬间，Agent 往往还没写出会话日志。延迟放在后台线程里，既不阻塞
管道读取，也给 CLI 留出写首行的时间。

内置后端只是把这一路登记进会话表（它不猜日志路径）；外部 HTTP 后端会按 cwd 扫
会话日志，扫不到就起一个空跟读线程并立即退出——两种情况都不影响派发。
"""


def _attach_in_background(client, *, cwd: str, pid: int, agent: str) -> None:
    """派发成功后把这一路登记给审计层（best-effort，绝不阻塞/影响派发）。"""

    def _run() -> None:
        try:
            time.sleep(_SENTINEL_ATTACH_DELAY)
            snapshot = client.attach(cwd=cwd or "", pid=pid, agent=agent)
            if isinstance(snapshot, dict) and not snapshot.get("log_path"):
                logger.debug("会话已登记但未关联日志（pid=%s, agent=%s）", pid, agent)
        except Exception as exc:  # noqa: BLE001 —— 登记失败不得影响派发
            logger.debug("会话登记失败（已忽略）：%s: %s", type(exc).__name__, exc)

    threading.Thread(target=_run, daemon=True, name="sentinel-attach").start()


async def run_agent(
    spec: AgentSpec,
    resolved: ResolvedCommand,
    task: str,
    *,
    config: BridgeConfig,
    cwd: str | None = None,
    model: str | None = None,
    timeout: int | None = None,
    json_output: bool = False,
    extra_args: tuple[str, ...] = (),
) -> AgentResult:
    """把一条非交互式任务派发给指定 Agent。"""
    override = config.override_for(spec.id)
    effective_model = model or override.model
    effective_extra = tuple(extra_args) + tuple(override.extra_args)
    effective_timeout = timeout if timeout is not None else override.timeout_seconds
    seconds = config.clamp_timeout(effective_timeout)

    if resolved.source == CMD_FALLBACK:
        # **绝不能把任务正文交给 cmd.exe。** cmd 的行解析与 list2cmdline 的转义规则
        # 不兼容：正文里的 `"` 会破坏引号配平，紧跟的 `&` 就越界执行；`%VAR%` 在
        # 引号内也照样展开。审计 PoC 的 7 个载荷里有 4 个正是这样逃出去的。
        # 解析不出真身时宁可拒绝，也不把用户文本送进 shell。
        return AgentResult(
            agent=spec.id,
            label=spec.label,
            exit_code=None,
            stdout="",
            stderr="",
            duration_ms=0,
            command_preview=resolved.describe(),
            error=(
                f"AGENT_UNRESOLVED: {spec.binary} 只能通过 cmd.exe 启动，而 cmd.exe 无法安全"
                f"承载任务正文（引号与 % 可越界执行）。请在 plugin.toml 的 "
                f"[bridge.overrides.{spec.id}] 里用 exe_override 指向真实可执行文件。"
                f"（{resolved.detail}）"
            ),
            extra={"unresolved_shim": resolved.exe},
        )

    argv = build_argv(
        spec,
        resolved,
        task,
        model=effective_model,
        json_output=json_output,
        extra_args=effective_extra,
    )
    workdir = config.resolve_cwd(cwd)

    # —— 哨兵 L1 安全预检闸门（对接哨兵 v5 无头守护进程）——
    # 未启用 / 守护进程没跑 / 降级 → 一律 fail-open（放行），派发行为完全不变。
    # 预检是同步 HTTP，扔线程里跑：2 秒超时不该把宿主的事件循环一起卡住。
    block = await asyncio.to_thread(gated_pre_check, config.sentinel, task)
    runtime = _sentinel_runtime
    if runtime is not None:
        # 判定已由 audit 侧完成（它同时支持内置与外部 HTTP 两种后端），这里只记账。
        # 重复判定的后果不是多花几毫秒，而是把「外部后端拦下了」在账本里写成
        # 「内置放行」——账做反了比没账更糟。
        #
        # 这里**刻意保持同步调用**（本项目唯一的例外）：实测稳态 note_pre_check
        # 0.37ms、begin_session 0.20ms，合计 0.6ms，低于事件循环的噪声底，包一层
        # to_thread 只是多一次调度开销。将来谁往这里加逻辑（begin_session 顺手打
        # 安全点、note_pre_check 改成写全量事件），量级会变 —— 那时必须重新评估，
        # 而不是照抄旁边的 take_safepoint 写法。
        runtime.note_pre_check(
            task,
            blocked=block is not None,
            rule_id=getattr(block, "rule_id", "") if block is not None else "",
            reason=getattr(block, "reason", "") if block is not None else "",
            evidence=getattr(block, "evidence", "") if block is not None else "",
            severity=getattr(block, "severity", "red_line") if block is not None else "",
            agent=spec.id,
        )
    if block is not None:
        preview = " ".join(redact_argv(argv, task))
        hint = f"（rule_id={block.rule_id}）" if block.rule_id else ""
        return AgentResult(
            agent=spec.id,
            label=spec.label,
            exit_code=None,
            stdout="",
            stderr="",
            duration_ms=0,
            command_preview=preview,
            error=(
                f"AGENT_BLOCKED_BY_SENTINEL: 任务被哨兵 L1 安全预检拦下"
                f"{hint}：{block.reason}".rstrip()
            ),
            blocked_by_sentinel=True,
            extra={
                "rule_id": block.rule_id,
                "reason": block.reason,
                "evidence": block.evidence,
            },
        )

    preview = " ".join(redact_argv(argv, task))

    # —— 会话登记与安全点（都在起进程之前）——
    # 指纹先按 cwd 定型（此刻还没有 PID），进程拉起之后再用 attach_pid 补进去。
    # 不这么做就会分裂成两条会话：一条有目标没 PID，一条有 PID 没目标。
    #
    # begin_session 同样**刻意保持同步**：实测 0.20ms，与上面的 note_pre_check
    # 合计 0.6ms，不值得为它多一层线程调度（理由见 note_pre_check 处那段）。
    fingerprint = ""
    box_started = 0.0
    if runtime is not None:
        fingerprint = runtime.begin_session(cwd=workdir or "", agent=spec.id, goal=task)
        if workdir:
            # 安全点内部串行跑多条 git（rev-parse / stash create / status --porcelain），
            # Windows 冷启动每条 5~15 秒，最坏能到 75 秒。宿主的事件循环是所有插件
            # 共用的，直接在这里同步调用等于让整个 N.E.K.O 一起卡住，所以丢线程。
            # 签名保持同步不变（与哨兵的约定），只在调用侧包一层。
            await asyncio.to_thread(runtime.take_safepoint, workdir, fingerprint=fingerprint)
        # 时间盒只有内存里的几次比较，没有 I/O，不必进线程。
        box_started = runtime.timebox_start(label=task[:60])

    # 哨兵挂接要在**进程刚起来**时拿到 pid；等 _execute 返回时进程已经退出，
    # 再 attach 只能挂到一具尸体上（这也是上一版最实质的缺陷）。
    attach_client = build_client(config.sentinel) if config.sentinel.attach else None

    def _on_spawn(pid: int) -> None:
        if attach_client is not None:
            _attach_in_background(attach_client, cwd=workdir or "", pid=pid, agent=spec.id)
        if runtime is not None:
            runtime.attach_pid(fingerprint, pid)

    def _on_chunk(chunk: bytes) -> None:
        if runtime is not None:
            runtime.on_output(chunk, fingerprint=fingerprint, agent=spec.id)

    started = time.monotonic()
    exit_code, stdout, stderr, timed_out, truncated, error, pid = await _execute(
        argv,
        cwd=workdir,
        timeout=seconds,
        output_limit=config.max_output_bytes,
        env_overrides=override.env or None,
        on_spawn=_on_spawn if (attach_client is not None or runtime is not None) else None,
        on_chunk=_on_chunk if runtime is not None else None,
    )
    duration_ms = int((time.monotonic() - started) * 1000)

    if runtime is not None:
        # 时间盒只报警不中断：中断一个正在写代码的 Agent 是不可逆的，
        # 该不该停由调用方决定，这里只负责把「它已经超了」说清楚。
        hit = runtime.timebox_check(box_started)
        if isinstance(hit, dict) and hit.get("level") == "expired":
            logger.warning("哨兵时间盒已超：%s", hit.get("label") or "任务")
        # 收尾要 drain 队列、落账本、写时间线，都是阻塞 I/O：同样不能占着事件循环。
        await asyncio.to_thread(runtime.finish_session, fingerprint, exit_code=exit_code)

    # 未登录的 CLI 会「退出码 0 + stderr 一行登录提示」，不特判就会被当成成功，
    # 上层模型拿到的 output 就是那行提示，只能自己猜为什么没干活。
    # 逐 Agent 的 auth_patterns 一并传进去：全局表只覆盖了最初 9 个 CLI 的措辞。
    needs_login = False
    if not timed_out and not error:
        auth_line = _detect_auth_failure(stdout, stderr, spec.auth_patterns)
        if auth_line:
            needs_login = True
            hint = f"（{spec.login_hint}）" if spec.login_hint else ""
            error = f"AGENT_NEEDS_LOGIN: {auth_line}{hint}"

    # 非鉴权的运行期失败：退出码 0，只在 stderr 说一句「服务没起来」。
    # 与上面互斥 —— 先认登录、再认运行期，否则同一行会被归成两类。
    if not timed_out and not error and spec.fail_patterns:
        fail_line = _match_head(stdout, stderr, _compile_extra_patterns(spec.fail_patterns))
        if fail_line:
            error = f"AGENT_RUNTIME_ERROR: {fail_line}"

    extra: dict[str, object] = {}
    if workdir:
        extra["cwd"] = workdir

    return AgentResult(
        agent=spec.id,
        label=spec.label,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        timed_out=timed_out,
        output_truncated=truncated,
        command_preview=preview,
        error=error,
        needs_login=needs_login,
        pid=pid,
        extra=extra,
    )


async def probe_version(
    spec: AgentSpec,
    resolved: ResolvedCommand,
    *,
    config: BridgeConfig,
    timeout: int = 45,
) -> str:
    """取一个 Agent 的版本号，用于 agents_list / agent_doctor。

    带 fixed_args 探测一次；若**看起来不像版本号**就摘掉 fixed_args 再试一次。
    qwenpaw 的 `task` 子命令不接受 `--version`，于是把 usage 文本当成版本号回显；
    去掉子命令后直接问 CLI 才拿得到真版本（sibling skill neko.py 里同样的兜底）。
    """
    override = config.override_for(spec.id)
    exit_code, stdout, stderr, timed_out, _, _, _ = await _execute(
        build_version_argv(spec, resolved),
        cwd=None,
        timeout=timeout,
        output_limit=8192,
        env_overrides=override.env or None,
    )
    if timed_out:
        return "<超时>"
    text = (stdout or stderr).strip()
    if text and _looks_like_version(_pick_version_line(text)):
        return _pick_version_line(text)[:120]

    if not spec.fixed_args:
        if not text:
            return f"<无输出 rc={exit_code}>"
        return _pick_version_line(text)[:120]

    _, retry_out, retry_err, retry_timed_out, _, _, _ = await _execute(
        build_version_argv(spec, resolved, with_fixed_args=False),
        cwd=None,
        timeout=timeout,
        output_limit=8192,
        env_overrides=override.env or None,
    )
    if retry_timed_out:
        return "<超时>"
    retry_text = (retry_out or retry_err).strip()
    if retry_text and _looks_like_version(_pick_version_line(retry_text)):
        return _pick_version_line(retry_text)[:120]
    # 两次都不像版本号 —— 宁可回显第一次的真实输出，也不要编一个假的
    if not text:
        return f"<无输出 rc={exit_code}>"
    return _pick_version_line(text)[:120]


def _looks_like_version(line: str) -> bool:
    """判断一行文本是否**真的**像版本号，而不是 usage 首行或日志时间戳。

    只看「出现了数字.数字」是不够的：qwenpaw 的 usage 里带路径
    （`...tools\\__init__.py:73 | 2026-10-08`）也能匹配到数字。所以要求这行
    以版本号为主体：要么整行就是 `1.2.3` 这类，要么是 `name 1.2.3` 这种短前缀。
    """
    if not line or len(line) > 120:
        return False
    # 明显的日志/用法行直接否掉
    lowered = line.lower()
    if any(marker in lowered for marker in ("usage:", "usage ", "options:", "traceback", ".py:", "error")):
        return False
    match = _VERSION_RE.search(line)
    if not match:
        return False
    # 版本号必须落在前 40 个字符内，且其后没有大段路径/解释文字
    if match.start() > 40:
        return False
    tail = line[match.end():].strip()
    return len(tail) <= 40

