"""Agent CLI 登记表，以及 Windows 下可执行文件的解析。

为什么需要 shim 解析：本机的 Agent CLI 有三种分发形态。

1. bun 全局安装 —— `node_modules\\.bin\\<name>.EXE`，是真实 PE 可执行文件，
   可以直接交给 CreateProcess。
2. 独立二进制 —— 例如 atomcode.exe、evox.exe。
3. 生成的 .cmd 包装器 —— 例如 evox.cmd、qwenpaw.cmd。CreateProcess 不能直接
   执行 .cmd；而走 `cmd.exe /c` 又会引入引号与 `%` 展开的转义地狱（任务描述是
   任意用户文本，可能含 % & | ^ 等字符）。因此这里解析 .cmd 内容，还原出真正
   的 .exe 与它需要的前缀参数，从而绕开 shell。

   包装器还有一种形态是「解释器 + 脚本」：npm 生成的 shim 会把参数转发给
   `node "<script>"`，而那个 script 是无扩展名的 JS 文件 —— 它自己不是可执行
   文件，直接交给 CreateProcess 会失败。这类 shim 会被还原成
   `exe=<解释器> argv_prefix=(<脚本路径>, ...)`。

关于 `cmd-fallback`（解析不出真身时的兜底）：cmd.exe 的行解析规则与
`subprocess.list2cmdline` 的转义规则**互不兼容** —— list2cmdline 用 `\"` 表示
字面引号，而 cmd 根本不认这个转义，`%VAR%` 在引号内也照样展开。实测：只要正文
里有一个未配平的 `"`，紧跟其后的 `&` 就会越界执行（审计 poc_inject3 的 4/7）。
因此**任务正文一旦经过 cmd.exe 就无法既安全又保真**。结论：cmd.exe 兜底只允许
承载**完全由我们自己构造、不含任何外部文本**的命令行（例如 `--version` 探测）；
派发任务时若只剩兜底路径，一律拒绝执行并提示改用 `exe_override`，绝不把用户
正文交给 shell。
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
from dataclasses import dataclass, field
from typing import Iterable

# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSpec:
    """一个 Agent CLI 的登记信息。"""

    id: str
    """稳定标识，也是 MCP 工具的 `agent` 参数取值。"""

    label: str
    binary: str
    """用于在 PATH 上查找的可执行文件名。"""

    mode: str
    """任务描述的传递方式：

    - "flag"       用 prompt_flag 指定的开关携带，例如 `omp -p "<task>"`；
                   开关自身要吃值（`-p, --prompt <string>`）。
    - "switch"     prompt_flag 是**布尔开关**，任务正文作为位置参数跟在后面，
                   例如 `pi -p "<task>"` 与 `cmd -p "<query>"`（取值可选）。
                   与 flag 的区别只在语义：这类 CLI 的 `-p` 单独出现就够，
                   正文是位置参数，所以顺序是「先开关、后正文」。
    - "positional" 作为位置参数追加在固定参数之后，例如 `opencode run "<task>"`。

    switch 与 flag 拼出来的 argv 形状相同，但**分开登记**是为了让清单自解释：
    新接一个 CLI 时，「-p 后面到底跟不跟值」正是最容易写错的一处。
    """

    fixed_args: tuple[str, ...] = ()
    """必须位于最前面的固定参数，例如 `("--evox-cli",)` 或 `("agent", "exec")`。"""

    prompt_flag: str | None = None
    model_flag: str | None = None
    json_args: tuple[str, ...] = ()
    version_args: tuple[str, ...] = ("--version",)
    notes: str = ""
    hints: tuple[str, ...] = field(default_factory=tuple)
    """已知限制或运维提示，会在 agents_list / agent_doctor 中回显。"""

    aliases: tuple[str, ...] = field(default_factory=tuple)
    """这个 Agent 额外接受的调用名（来自 provider 清单，已过全局去重）。

    内置 Agent 的别名仍写在模块级 ``ALIASES`` 里 —— 两者最终合并进同一张表，
    ``canonical_id`` 不区分来源。
    """

    auth_patterns: tuple[str, ...] = field(default_factory=tuple)
    """**这个 Agent 专属**的鉴权失败特征（正则片段，大小写不敏感）。

    全局那组 ``runner._AUTH_PATTERNS`` 是按前 9 个 CLI 的实际措辞写的，换一个产品
    就会漏判 —— 实测 gemini 的 "Please set an Auth method … GEMINI_API_KEY" 与
    command-code 的 "Authentication failed. Please run \"cmdc login\" first."
    都不在里面。它们退出码非 0（41 / 3），漏判的后果是「被报成普通 FAILED」：
    不进 ``needs_login`` 集合、不被跳过、也不提示换一个 Agent。所以每个新 Agent
    必须自带特征，别指望全局表覆盖。
    """

    login_hint: str = ""
    """登录/鉴权失败的处置提示（例如 `gemini` 要设 GEMINI_API_KEY）。回显在 notes 之后。"""

    install_cmd: str = ""
    """一键安装命令（白名单：只来自清单，永不来自请求体）。

    面板上「未安装 → 装一个」按钮执行的就是它；执行完立刻回读可执行状态与版本，
    装成功按钮才消失。
    """

    install_hint: str = ""
    """没有一键安装时给人工步骤（官网/包名/前置条件）。"""

    fail_patterns: tuple[str, ...] = field(default_factory=tuple)
    """**非鉴权**的运行期失败特征（正则片段，大小写不敏感）。

    有些 CLI 的"跑不了"既不是登录问题、也不给非 0 退出码。实测 hermes：

        $ hermes -z "reply OK"
        Error: Gateway inaccessible: service_manager_unavailable
        $ echo $?      # → 0

    退出码 0、stdout 空，于是被判成「成功，输出是这行 Error」。这类必须和
    ``auth_patterns`` 分开：处置动作完全不同（一个去登录，一个去起服务）。
    """


# --------------------------------------------------------------------------
# 登记表 —— 取自《本机 Agent 清单》实测结果
# --------------------------------------------------------------------------

AGENT_SPECS: dict[str, AgentSpec] = {
    "codebuddy": AgentSpec(
        id="codebuddy",
        label="CodeBuddy Code（腾讯）",
        binary="codebuddy",
        mode="flag",
        prompt_flag="-p",
        model_flag="--model",
        json_args=("--output-format", "json"),
        notes="凭据挂在 WorkBuddy 桌面端；未登录时返回退出码 1 且无输出。",
    ),
    "claude": AgentSpec(
        id="claude",
        label="Claude Code（Anthropic）",
        binary="claude",
        mode="flag",
        prompt_flag="-p",
        model_flag="--model",
        json_args=("--output-format", "json"),
        notes="部分沙箱环境下启动时调用 reg.exe 会命中程序黑名单。",
    ),
    "dsh": AgentSpec(
        id="dsh",
        label="DeepSeek Harness",
        binary="dsh",
        mode="positional",
        fixed_args=("--profile", "headless"),
        model_flag=None,
        notes="任务描述作为位置参数放在 --profile 之后。",
    ),
    "omp": AgentSpec(
        id="omp",
        label="Oh My Pi",
        binary="omp",
        mode="flag",
        prompt_flag="-p",
        model_flag="--model",
        json_args=("--mode", "json"),
        notes="同 monorepo 另有 mnemopi（记忆）与 omp-stats（用量）。",
    ),
    "evox": AgentSpec(
        id="evox",
        label="EvoX",
        binary="evox",
        mode="flag",
        fixed_args=("--evox-cli",),
        prompt_flag="-p",
        model_flag="--model",
        notes="evox.exe 需要 --evox-cli 才进入 CLI 模式；PATH 上是 evox.cmd 包装器。",
    ),
    "opencode": AgentSpec(
        id="opencode",
        label="OpenCode",
        binary="opencode",
        mode="positional",
        fixed_args=("run",),
        model_flag="--model",
        json_args=("--format", "json"),
        notes="非交互式走独立的 run 子命令。",
    ),
    "openclaw": AgentSpec(
        id="openclaw",
        label="OpenClaw",
        binary="openclaw",
        mode="positional",
        fixed_args=("agent", "exec"),
        model_flag="--model",
        json_args=("--json",),
        notes="agent exec 为一次性隔离执行，不需要常驻 Gateway。",
    ),
    "atomcode": AgentSpec(
        id="atomcode",
        label="AtomCode",
        binary="atomcode",
        mode="flag",
        prompt_flag="-p",
        model_flag=None,
        json_args=("--output-format", "jsonl"),
        notes="登录走 AtomGit OAuth；支持 --prompt-file 与 --ephemeral。",
    ),
    "qwenpaw": AgentSpec(
        id="qwenpaw",
        label="QwenPaw",
        binary="qwenpaw",
        mode="flag",
        fixed_args=("task",),
        prompt_flag="-i",
        model_flag="-m",
        notes="内置跨 Agent 协作工具（submit_to_agent 等）；PATH 上是 .cmd 包装器。",
    ),
}

# 常见别名，方便人手调用（MCP 客户端会通过 enum 限制取值，这里主要给人用）
#
# ⚠️ 曾经这里有一条 `"pi": "omp"`。**它是错的，已移除**：
#    `omp` 的真名是 `@oh-my-pi/pi-coding-agent`（v18.x），而 PATH 上的 `pi`
#    来自另一个包 `@earendil-works/pi-coding-agent`（v1.x）—— 两个产品，
#    共用了一个可执行名。实测 `pi --version` → 1.4.0，`omp --version` → 18.12.6。
#    保留那条别名等于「按 pi 却跑 omp」，而且真正的 pi 永远调不到。
#    现在 `pi` 是独立登记的路由（providers/pi），omp 用 `omp` / `oh-my-pi` / `ompi`。
ALIASES: dict[str, str] = {
    "cbc": "codebuddy",
    "codebuddy-code": "codebuddy",
    "oh-my-pi": "omp",
    "ompi": "omp",
    "oc": "opencode",
    "claw": "openclaw",
    "atom": "atomcode",
    "qwen": "qwenpaw",
}


def canonical_id(name: str) -> str | None:
    """把用户/模型给的名字归一化到登记表里的 id，找不到返回 None。"""
    key = (name or "").strip().lower()
    if key in AGENT_SPECS:
        return key
    return ALIASES.get(key)


# --------------------------------------------------------------------------
# Windows shim 解析
# --------------------------------------------------------------------------

_ASSIGN_RE = re.compile(r'set\s+"?([A-Za-z_][A-Za-z0-9_]*)=(.*?)"?\s*$', re.IGNORECASE | re.MULTILINE)
_VAR_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")
_SHIM_MOD_RE = re.compile(r"%~([a-zA-Z]+)(\d)")
_FLAG_RE = re.compile(r"(?<![\w-])(--?[A-Za-z][\w-]*)")
_TOKEN_RE = re.compile(r'"([^"]*)"|([^\s"]+)')
_SEPARATORS = frozenset({"&", "&&", "|", "||"})
"""shim 里把「准备动作」和「真正启动」分开的命令分隔符。"""
_CMD_EXT = (".cmd", ".bat")
_EXE_EXT = (".exe", ".com")

#: PATH 找不到时的兜底搜索目录（只在这些目录里找可执行文件，不递归）。
#:
#: 为什么需要：``shutil.which`` 只看 ``PATH``，而宿主进程（N.E.K.O 的嵌入式 Python、
#: IDE 里起的任务）常常**没有继承**用户 shell 的 ``PATH``。这时 ``~/.bun/bin`` 里的
#: ``omp.exe`` 明明在，却会被判成 ``missing``，报错还写「未在 PATH 中找到」。
#:
#: bun 的安装形态有一处必须知道：它**同时**写 ``X.bunx``（装载脚本，不是可执行文件）
#: 和 ``X.exe``（8 KiB 的 PE 桩）。``which`` 命中 ``.exe`` 所以能直接 CreateProcess；
#: 自己按名字找文件时必须优先挑 ``.exe``，绝不能把 ``.bunx`` 交给 CreateProcess。
_EXTRA_BIN_DIRS = (
    "~/.bun/bin",                 # bun add -g
    "~/AppData/Roaming/npm",      # npm -g（Windows）
    "~/.local/bin",               # pipx / uv
    "~/.stepcode/bin",            # StepCode（自带安装器）
    "~/AppData/Local/hermes/bin", # Hermes（git 安装）
    "~/.qwenpaw/bin",             # QwenPaw
    "~/AppData/Local/AtomCode",   # AtomCode
    "~/AppData/Local/evox/bin",   # EvoX
    "/usr/local/bin",
)


def _find_extra(binary: str) -> str | None:
    """在兜底目录里找 ``binary``，优先可执行扩展名，跳过 ``.bunx`` 这类装载脚本。"""
    for directory in _EXTRA_BIN_DIRS:
        base = os.path.expanduser(directory)
        if not os.path.isdir(base):
            continue
        for ext in (*_EXE_EXT, *_CMD_EXT):
            candidate = os.path.join(base, binary + ext)
            if os.path.isfile(candidate):
                return candidate
    return None
_SCRIPT_INTERPRETERS = {
    ".js": "node",
    ".mjs": "node",
    ".cjs": "node",
}
"""脚本扩展名 → 解释器。npm 生成的 shim 会把参数转发给一个无扩展名的 JS 文件，
需要还原成「解释器 + 脚本」才能交给 CreateProcess。"""
_SHEBANG_INTERPRETERS = ("node", "bun", "deno")
"""无扩展名脚本靠 `#!` 行判断解释器时，能认出的名字。"""
CMD_FALLBACK = "cmd-fallback"
"""解析不出真身时，退回 `cmd.exe /d /s /c <shim>` 的标记。

**这条路径不能承载任何外部文本。** cmd 的行解析与 `list2cmdline` 的转义规则不兼容，
正文里的 `"` / `%` 都能越界（见模块头注释），所以只有我们自己构造的命令行
（如 `--version` 探测）才允许走这里。
"""


def _log_warning(message: str) -> None:
    """给配置类问题留个可见痕迹。

    registry 是纯函数层，不持有 logger；用标准库 logging 记一条 warning，
    宿主（插件）与 skill 两边都能在日志里看到，而不是静默丢弃配置。
    """
    logging.getLogger(__name__).warning(message)



class ResolvedCommand:
    """一个 Agent 最终的可执行调用形态。"""

    __slots__ = ("argv_prefix", "exe", "source", "exists", "detail")

    def __init__(
        self,
        exe: str,
        argv_prefix: Iterable[str] = (),
        *,
        source: str = "path",
        exists: bool = True,
        detail: str = "",
    ) -> None:
        self.exe = exe
        self.argv_prefix = tuple(argv_prefix)
        self.source = source
        self.exists = exists
        self.detail = detail

    def describe(self) -> str:
        parts = [self.exe, *self.argv_prefix]
        return " ".join(parts)

    def to_dict(self) -> dict[str, object]:
        return {
            "exe": self.exe,
            "argv_prefix": list(self.argv_prefix),
            "source": self.source,
            "exists": self.exists,
            "detail": self.detail,
        }


def _shim_self_paths(path: str) -> dict[str, str]:
    """把参数修饰符 `%~d0` / `%~p0` / `%~dp0` 等解析成脚本自身的路径片段。

    **必须在 `%VAR%` 展开之后处理**（见 `_expand_vars` 里的说明）。

    注意 `%~dp0` 是**盘符 + 目录**，且结尾带反斜杠（例如 `C:\\Users\\x\\`），
    拼接时不要再补分隔符。
    """
    drive, tail = os.path.splitdrive(os.path.abspath(path))
    directory, filename = os.path.split(tail)
    stem, ext = os.path.splitext(filename)
    if not directory:
        directory = os.sep
    if not directory.endswith(("\\", "/")):
        directory += os.sep
    return {
        "d": drive,
        "p": directory,
        "dp": drive + directory,
        "n": stem,
        "x": ext,
        "nx": stem + ext,
    }


def _expand_vars(text: str, variables: dict[str, str], self_paths: dict[str, str] | None = None) -> str:
    def _sub(match: re.Match[str]) -> str:
        name = match.group(1).upper()
        if name in variables:
            return variables[name]
        return os.environ.get(match.group(1), "")

    # 顺序很重要：先展开 %VAR%（`%dp0%` 会被展开成 `%~dp0` 这样的字面文本），
    # 再解析 `%~dp0`。反过来的话，`%~dp0` 里的 `%` 会被 `_VAR_RE` 当成变量起始符，
    # 找不到 `~dp0` 就替换成空串，路径整段丢失。
    text = _VAR_RE.sub(_sub, _VAR_RE.sub(_sub, text))

    if self_paths:
        def _modifier(match: re.Match[str]) -> str:
            # 只支持 `%0`（脚本自身）；`%1` 之类是调用参数，解析期拿不到
            if match.group(2) != "0":
                return ""
            return self_paths.get(match.group(1).lower(), "")

        text = _SHIM_MOD_RE.sub(_modifier, text)
    return text


def _launch_tokens(head: str) -> list[tuple[str, bool]]:
    """切出「真正启动进程」那一段的 token，并记住每个 token 原本是否带引号。

    shim 里常有命令链，例如 codebuddy.cmd 的转发行是
    `endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%" "<脚本>"`。
    真正启动的只有最后一个 `&` 之后的部分，所以按**未被引号包裹的**分隔符切一刀。
    """
    pairs = _TOKEN_RE.findall(head)
    tokens = [(quoted or bare, bool(quoted)) for quoted, bare in pairs]
    cut = 0
    for index, (value, quoted) in enumerate(tokens):
        if not quoted and value in _SEPARATORS:
            cut = index + 1
    return tokens[cut:]


def _interpreter_for(script: str) -> str | None:
    """判断该脚本文件要用什么解释器启动。

    先看扩展名（`x.js` → node）；npm 生成的 shim 常指向**没有扩展名**的入口
    （例如 codebuddy 的 `bin\\codebuddy`），这时读首行的 shebang
    （`#!/usr/bin/env node`）来判断。
    """
    by_ext = _SCRIPT_INTERPRETERS.get(os.path.splitext(script)[1].lower())
    if by_ext is not None:
        return by_ext
    # 扩展名可能是伪造的（`x.cjs` 也会有 shebang），所以扩展名未知时再读首行
    try:
        with open(script, "r", encoding="utf-8", errors="replace") as handle:
            first_line = handle.readline(200).strip()
    except OSError:
        return None
    if not first_line.startswith("#!"):
        return None
    for name in _SHEBANG_INTERPRETERS:
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", first_line, re.IGNORECASE):
            return name
    return None


def _resolve_shim_target(
    head: str,
    variables: dict[str, str],
    self_paths: dict[str, str],
) -> tuple[str, tuple[str, ...]] | None:
    """把 shim 的转发行还原成 `(可执行文件, 前缀参数)`。

    shim 有两种常见形态：

    1. **直接转发**：`"C:\\path\\agent.exe" %*` —— 可执行文件本身就是 PE。
    2. **解释器 + 脚本**：`"%_prog%" "%dp0%\\node_modules\\...\\codebuddy" %*` ——
       真身是**无扩展名的 JS 脚本**，必须由 node 解释执行。把那个脚本直接交给
       CreateProcess 会失败，所以这里还原成 `exe=<解释器>, prefix=(脚本, ...)`。

    返回 None 表示解析不出可安全启动的目标，调用方应退回 cmd.exe 兜底。
    """

    def _value(token: str) -> str:
        # `%~dp0` 自带结尾反斜杠，shim 里又常写成 `%dp0%\sub\...`，
        # 展开后会出现 `\\`；折叠掉，否则日志里看着像坏路径。
        text = _expand_vars(token.strip('"'), variables, self_paths).strip().strip('"')
        return re.sub(r"([\\/])\1+", r"\1", text)

    values = [value for value, _quoted in _launch_tokens(head) if value]
    if not values:
        return None

    target = _value(values[0])
    if not target:
        return None
    rest = [_value(token) for token in values[1:]]

    # 形态 1：第一个 token 就是个能启动的 PE
    if target.lower().endswith(_EXE_EXT):
        if not os.path.isabs(target) or not os.path.isfile(target):
            return None
        return target, _flags_of(rest)

    # 形态 2：第一个 token 是解释器（裸名字或带路径），后面跟着一个真实存在的脚本文件。
    if os.path.isfile(target):
        return None  # 存在但不是可执行文件 —— 不能交给 CreateProcess
    script_index = next((i for i, token in enumerate(rest) if os.path.isfile(token)), None)
    if script_index is None:
        return None
    script = rest[script_index]
    interpreter_name = _interpreter_for(script)
    if interpreter_name is None:
        return None

    interpreter = target if os.path.isabs(target) else (shutil.which(target) or shutil.which(interpreter_name))
    if not interpreter or not os.path.isfile(interpreter):
        return None
    # 脚本前后的 token 是解释器自己的开关（例如 `node --no-warnings "x.js"`）
    return interpreter, (*_flags_of(rest[:script_index]), script, *_flags_of(rest[script_index + 1:]))


def _flags_of(tokens: Iterable[str]) -> tuple[str, ...]:
    """从 token 列表里挑出形如 `--flag` 的参数（保持原顺序、去重）。"""
    seen: list[str] = []
    for token in tokens:
        for flag in _FLAG_RE.findall(token):
            if flag not in seen:
                seen.append(flag)
    return tuple(seen)


def parse_cmd_shim(path: str) -> ResolvedCommand | None:
    """从 .cmd/.bat 包装器里还原真实可执行文件与前缀参数。

    返回 None 表示解析不出可安全启动的目标，调用方应退回 cmd.exe /d /s /c。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except OSError:
        return None

    self_paths = _shim_self_paths(path)
    variables: dict[str, str] = {}
    for match in _ASSIGN_RE.finditer(raw):
        variables[match.group(1).upper()] = match.group(2)

    # 展开 set 赋值中的 %VAR%（例如 qwenpaw 的 REAL_BIN）
    for _ in range(2):
        for key in list(variables):
            variables[key] = _expand_vars(variables[key], variables, self_paths)

    # 找出真正转发参数的那一行：包含 %*，且不是注释
    line = ""
    for candidate in raw.splitlines():
        stripped = candidate.strip()
        if not stripped or stripped.lower().startswith(("rem", "::", "@echo")):
            continue
        if "%*" in candidate:
            line = candidate
    if not line:
        return None

    head, _, _ = line.partition("%*")
    target = _resolve_shim_target(head, variables, self_paths)
    if target is None:
        return None
    exe, prefix = target

    return ResolvedCommand(
        exe=exe,
        argv_prefix=prefix,
        source="cmd-shim",
        exists=True,
        detail=os.path.basename(path),
    )


def resolve_agent(spec: AgentSpec, override_exe: str | None = None) -> ResolvedCommand:
    """定位一个 Agent 的可执行调用形态。

    解析顺序：显式覆盖 → PATH 上的 .exe → PATH 上的 .cmd 解析 → cmd.exe 兜底。
    """
    if override_exe:
        # 用 shlex 而不是 str.split()：绝对路径可能带空格（"C:\\Program Files\\a b\\x.exe"），
        # split() 会把它切碎并要求 parts[0] 存在，于是整条 override 被静默丢弃。
        try:
            parts = shlex.split(override_exe, posix=False)
        except ValueError as exc:
            _log_warning(f"exe_override 无法解析（{exc}）：{override_exe!r}，已忽略并回落 PATH")
            parts = []
        parts = [part.strip('"') for part in parts if part.strip('"')]
        if parts:
            head = parts[0]
            # 裸命令名（如 "cmd.exe"）按 PATH 解析；绝对/相对路径按原样判断。
            resolved_head = head if os.path.isabs(head) else (shutil.which(head) or head)
            if os.path.isfile(resolved_head):
                return ResolvedCommand(resolved_head, parts[1:], source="override")
            _log_warning(
                f"exe_override 指向的文件不存在：{head!r}"
                f"（agent={spec.id}），已忽略并回落 PATH 解析"
            )
        elif override_exe.strip():
            _log_warning(f"exe_override 为空或不可用：{override_exe!r}（agent={spec.id}），已忽略")

    found = shutil.which(spec.binary) or _find_extra(spec.binary)
    if not found:
        return ResolvedCommand(
            spec.binary,
            source="missing",
            exists=False,
            detail=f"未在 PATH 或 {', '.join(_EXTRA_BIN_DIRS)} 中找到",
        )

    lowered = found.lower()
    if not lowered.endswith(_CMD_EXT):
        return ResolvedCommand(found, source="path")

    parsed = parse_cmd_shim(found)
    if parsed is not None:
        return parsed

    # 解析失败 —— 退回 cmd.exe /c。**该路径不得承载任务正文**（见 CMD_FALLBACK 说明），
    # runner 会在派发任务时直接拒绝，只有 --version 这类自造命令行才会走到这里。
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    cmd_exe = os.path.join(system_root, "System32", "cmd.exe")
    return ResolvedCommand(
        cmd_exe,
        ("/d", "/s", "/c", found),
        source=CMD_FALLBACK,
        detail="解析 .cmd 失败，退回 cmd.exe 兜底（仅可承载插件自造的命令行）",
    )


def build_argv(
    spec: AgentSpec,
    resolved: ResolvedCommand,
    task: str,
    *,
    model: str | None = None,
    json_output: bool = False,
    extra_args: Iterable[str] = (),
) -> list[str]:
    """拼出最终命令行。"""
    # shim 里可能已经内联了同样的旗标（例如 evox.cmd 的 `--evox-cli` 与
    # spec.fixed_args 重复），去重后再拼，否则会出现 `--evox-cli --evox-cli`。
    fixed = [arg for arg in spec.fixed_args if arg not in resolved.argv_prefix]
    argv: list[str] = [resolved.exe, *resolved.argv_prefix, *fixed]

    if spec.mode == "flag":
        if not spec.prompt_flag:
            raise ValueError(f"agent '{spec.id}' 的 mode=flag 但缺少 prompt_flag")
        argv += [spec.prompt_flag, task]
    elif spec.mode == "switch":
        # 开关是布尔的（`--print` / `-p`），正文跟后面。拼出来与 flag 同形，
        # 但登记时分开写，避免把「不吃值的开关」误写成 flag 后在真机上才发现。
        if not spec.prompt_flag:
            raise ValueError(f"agent '{spec.id}' 的 mode=switch 但缺少 prompt_flag")
        argv += [spec.prompt_flag, task]
    elif spec.mode == "positional":
        argv.append(task)
    else:
        raise ValueError(f"agent '{spec.id}' 的 mode 未知：{spec.mode!r}")

    if json_output and spec.json_args:
        argv += list(spec.json_args)
    if model and spec.model_flag:
        argv += [spec.model_flag, model]

    argv += [str(a) for a in extra_args]
    return argv


def build_version_argv(spec: AgentSpec, resolved: ResolvedCommand, *, with_fixed_args: bool = True) -> list[str]:
    """拼版本探测命令行。

    `with_fixed_args=False` 用于「去掉 fixed_args 重试」：有些 CLI 的子命令不接受
    `--version`（qwenpaw 的 `task --version` 只打 usage），去掉子命令再试才拿得到。
    """
    fixed = [arg for arg in spec.fixed_args if arg not in resolved.argv_prefix] if with_fixed_args else []
    return [resolved.exe, *resolved.argv_prefix, *fixed, *spec.version_args]


def redact_argv(argv: list[str], task: str) -> list[str]:
    """把命令行里的任务正文换成占位符，用于日志与回显。"""
    out: list[str] = []
    for token in argv:
        out.append("<task>" if token == task else token)
    return out


def list_specs() -> list[AgentSpec]:
    return [AGENT_SPECS[key] for key in sorted(AGENT_SPECS)]


# --------------------------------------------------------------------------
# provider 数据化登记 —— 在**导入期**合并，不能延迟
# --------------------------------------------------------------------------
#
# 为什么必须在 import 时完成：`__init__.py` 把 `_AGENT_IDS` / `_AGENT_HINT` 烘进了
# `@plugin_entry` / `@llm_tool` 的 description（装饰器参数在**类定义时**求值）。
# 如果改成运行期按需加载，新增的 Agent 会「能跑但不在描述里」—— 模型永远看不到它。
# 所以这里在模块末尾就把 providers/ 合并进 AGENT_SPECS。

_PROVIDER_SOURCE: str = ""
_PROVIDER_WARNINGS: list[str] = []


def _merge_providers() -> None:
    """把 providers/ 下的清单并进 AGENT_SPECS / ALIASES（只取第一个存在的根）。

    只取第一个：多个根同时合并时，后一个里的同 id 清单会被判成「撞名」而刷一堆
    无意义告警。按 ``providers.provider_roots()`` 的顺序取第一个可用目录，
    符合「环境变量覆盖部署目录」的直觉。
    """
    global _PROVIDER_SOURCE
    from .providers import load_into, provider_roots

    for root in provider_roots():
        if not os.path.isdir(root):
            continue
        _PROVIDER_SOURCE = root
        _PROVIDER_WARNINGS.extend(
            load_into(AGENT_SPECS, ALIASES, root=root, spec_factory=AgentSpec)
        )
        break


def provider_source() -> str:
    """这次启动实际读了哪个 providers 目录（诊断用；没有就是空串）。"""
    return _PROVIDER_SOURCE


def provider_warnings() -> list[str]:
    """provider 清单的加载告警（诊断用）。"""
    return list(_PROVIDER_WARNINGS)


_merge_providers()
