"""内置精简哨兵的规则层 —— 命令解析与红线判定。

定位
====

只做一件事：给定一段任务文本（或一条命令），判断它是不是「一旦跑完就回不来」。
不做语义理解、不调模型、不依赖上下文——这是 L1 秒回层的全部职责。

三条设计原则
============

* **纯标准库**：解析器自己写，不引入 shlex 之外的东西，也不要求 pyyaml。
* **宁可漏报，不可误报**：误报会拦掉用户正在做的正事，比漏报更伤。所以判定
  全部落在**可解释的结构化特征**上（路径分类、参数标志、管道方向），而不是
  「看起来像危险命令」。
* **解析失败即放行**：任何一段解析不出来就跳过那一段，由上层把整次判定标成
  ``degraded``。安全层永远不能成为可用性故障点。

分层
====

``standard``
    「这条命令有风险」，多数可恢复：``git push -f``、``chmod`` 提权、``shutdown``。
``red_line``
    「这条命令跑完就不可逆」：递归删根、抹盘、下载即执行。红线是唯一会真正
    拦下派发的级别。
"""

from __future__ import annotations

import re
import shlex
from collections import deque
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "SEVERITY_NONE",
    "SEVERITY_RED_LINE",
    "SEVERITY_STANDARD",
    "VERDICT_DANGEROUS",
    "VERDICT_LOOP",
    "VERDICT_ON_TRACK",
    "Command",
    "LoopDetector",
    "RuleEngine",
    "Verdict",
    "classify_target",
    "parse_command",
    "split_segments",
]

# ---------------------------------------------------------------------------
# 判定与严重度
# ---------------------------------------------------------------------------

#: 未发现异常。
VERDICT_ON_TRACK = "ON_TRACK"
#: 命中高危命令。
VERDICT_DANGEROUS = "DANGEROUS"
#: 疑似重复读循环。
VERDICT_LOOP = "LOOP"

#: 无异常。
SEVERITY_NONE = "none"
#: 有风险但通常可恢复。
SEVERITY_STANDARD = "standard"
#: 不可逆。
SEVERITY_RED_LINE = "red_line"

# ---------------------------------------------------------------------------
# 路径分类
# ---------------------------------------------------------------------------

#: 文件系统根。
TARGET_ROOT = "root"
#: 用户家目录。
TARGET_HOME = "home"
#: 系统关键目录本身。
TARGET_SENSITIVE = "sensitive"
#: 含通配符的路径。
TARGET_WILDCARD = "wildcard"
#: 其余（相对路径、普通绝对路径、具体文件）。
TARGET_OTHER = "other"

#: 一旦整体删除就会让系统或用户数据回不来的 POSIX 目录。
_SENSITIVE_POSIX = frozenset(
    {
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/home",
        "/lib",
        "/lib32",
        "/lib64",
        "/media",
        "/mnt",
        "/opt",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/srv",
        "/sys",
        "/usr",
        "/var",
    }
)

#: 同上，Windows 侧（统一转成小写正斜杠后比较）。
_SENSITIVE_WIN = frozenset(
    {
        "c:/windows",
        "c:/program files",
        "c:/program files (x86)",
        "c:/programdata",
        "c:/users",
    }
)

#: 表示「整个家目录」的字面量。
_HOME_LITERALS = frozenset({"~", "$home", "${home}", "%userprofile%"})

#: 单个字母驱动的 Windows 盘根，如 ``c:`` / ``c:/`` / ``c:/*``。
_RE_WIN_DRIVE_ROOT = re.compile(r"[a-z]:/*\**")
#: POSIX 根与它的通配形式，如 ``/`` / ``/*`` / ``/**``。
_RE_POSIX_ROOT = re.compile(r"//?\**")

# ---------------------------------------------------------------------------
# 命令解析
# ---------------------------------------------------------------------------

#: 这些前缀命令本身不做业务，剥掉后才是真正要看的命令。
_PREFIX_COMMANDS = frozenset(
    {
        "sudo",
        "doas",
        "env",
        "command",
        "exec",
        "nohup",
        "nice",
        "ionice",
        "setsid",
        "stdbuf",
        "time",
        "timeout",
        "watch",
    }
)

#: 环境变量赋值形式 ``FOO=bar``。
_RE_ENV_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")

#: 会删东西的命令。
_DELETE_COMMANDS = frozenset({"del", "erase", "rd", "rm", "rmdir", "srm", "unlink"})

#: 递归标志（已统一小写；``-rf`` 会被拆成 ``r`` 和 ``f``）。
_RECURSIVE_FLAGS = frozenset({"r", "recursive"})
#: 强制标志。
_FORCE_FLAGS = frozenset({"f", "force"})


@dataclass(frozen=True)
class Command:
    """一条解析后的命令。

    :ivar raw: 原始片段。
    :ivar name: 归一化命令名（小写、去路径、去 ``.exe``）。
    :ivar args: 命令名之后的原始参数。
    :ivar flags: 拆开后的标志集合（``-rf`` → ``{"r", "f"}``）。
    :ivar targets: 非标志参数，即操作对象。
    """

    raw: str
    name: str
    args: tuple[str, ...]
    flags: frozenset[str]
    targets: tuple[str, ...]


def _basename(token: str) -> str:
    """取命令名：去引号、去目录、去扩展名、转小写。

    :param token: 原始 token。
    :return: 归一化命令名。
    """
    text = str(token or "").strip().strip('"').strip("'")
    text = text.replace("\\", "/")
    text = text.rsplit("/", 1)[-1]
    if text.lower().endswith(".exe"):
        text = text[: -len(".exe")]
    return text.lower()


def split_segments(text: str) -> list[str]:
    """按 shell 分隔符切分命令片段。

    会尊重引号——引号内的 ``;`` 和 ``|`` 不切。切完再各自解析，这样
    ``cd /tmp && rm -rf /`` 里的两个动作能被分别判定，而不是当成一条糊在一起。

    :param text: 任务文本或命令行。
    :return: 非空片段列表。
    """
    out: list[str] = []
    buf: list[str] = []
    quote = ""
    index = 0
    total = len(text)
    while index < total:
        char = text[index]
        if quote:
            buf.append(char)
            if char == "\\" and index + 1 < total:
                index += 1
                buf.append(text[index])
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"":
            quote = char
            buf.append(char)
            index += 1
            continue
        if char in ";|&\n":
            out.append("".join(buf))
            buf = []
            while index < total and text[index] in ";|&\n":
                index += 1
            continue
        buf.append(char)
        index += 1
    out.append("".join(buf))
    return [seg for seg in (item.strip() for item in out) if seg]


def _split_flags(tokens: Sequence[str]) -> tuple[set[str], list[str]]:
    """把 ``-rf`` 拆成 ``{"r", "f"}``，并把操作对象分离出来。

    :param tokens: 命令名之后的 token。
    :return: (标志集合, 操作对象列表)。
    """
    flags: set[str] = set()
    targets: list[str] = []
    stop = False
    for token in tokens:
        if stop:
            targets.append(token)
            continue
        if token == "--":
            stop = True
            continue
        if token.startswith("-") and len(token) > 1 and not token[1].isdigit():
            body = token.lstrip("-")
            if token.startswith("--"):
                flags.add(body.split("=", 1)[0].lower())
            else:
                flags.update(char.lower() for char in body)
                flags.add(body.lower())
            continue
        targets.append(token)
    return flags, targets


def parse_command(segment: str) -> Command | None:
    """解析单个命令片段。

    会剥掉 ``sudo`` / ``env`` / ``FOO=bar`` 这类前缀以及它们自己的选项，
    这样 ``sudo -u root rm -rf /`` 依然能被识别成一条 ``rm``。

    :param segment: 命令片段。
    :return: 解析结果；解析不出命令名时返回 ``None``。
    """
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        tokens = segment.split()
    if not tokens:
        return None

    index = 0
    total = len(tokens)
    while index < total:
        token = tokens[index]
        if _basename(token) in _PREFIX_COMMANDS:
            index += 1
            while index < total and tokens[index].startswith("-"):
                index += 1
            continue
        if _RE_ENV_ASSIGN.fullmatch(token):
            index += 1
            continue
        break
    if index >= total:
        return None

    name = _basename(tokens[index])
    rest = tokens[index + 1 :]
    flags, targets = _split_flags(rest)
    return Command(
        raw=segment, name=name, args=tuple(rest), flags=frozenset(flags), targets=tuple(targets)
    )


def classify_target(raw: str) -> str:
    """判断一个操作对象属于哪类路径。

    这是红线判定的地基：``rm -rf /`` 和 ``rm -rf ./dist`` 的唯一区别就在这里。

    :param raw: 原始路径字符串。
    :return: 分类常量之一。
    """
    text = str(raw or "").strip().strip('"').strip("'")
    if not text:
        return TARGET_OTHER
    low = text.replace("\\", "/").lower()
    # 盘根：c: / c:/ / c:/* / c:\*
    if _RE_WIN_DRIVE_ROOT.fullmatch(low):
        return TARGET_ROOT
    # POSIX 根：/ / /* /**
    if _RE_POSIX_ROOT.fullmatch(low):
        return TARGET_ROOT
    core = low.rstrip("/*")
    if core in _HOME_LITERALS or low in {f"{item}/*" for item in _HOME_LITERALS}:
        return TARGET_HOME
    if core in _SENSITIVE_POSIX or core in _SENSITIVE_WIN:
        return TARGET_SENSITIVE
    if "*" in low or "?" in low:
        return TARGET_WILDCARD
    return TARGET_OTHER


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """一次判定的结果。

    :ivar verdict: ``ON_TRACK`` / ``DANGEROUS`` / ``LOOP``。
    :ivar severity: ``none`` / ``standard`` / ``red_line``。
    :ivar rule_id: 稳定规则 ID，便于调用方按 ID 做策略。
    :ivar reason: 中文说明，直接给人看。
    :ivar evidence: 命中的原文片段。
    :ivar category: 红线类别（非红线时为空串）。
    """

    verdict: str = VERDICT_ON_TRACK
    severity: str = SEVERITY_NONE
    rule_id: str = ""
    reason: str = ""
    evidence: str = ""
    category: str = ""

    @property
    def dangerous(self) -> bool:
        """是否命中危险。"""
        return self.verdict == VERDICT_DANGEROUS

    @property
    def red_line(self) -> bool:
        """是否红线级。"""
        return self.severity == SEVERITY_RED_LINE

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 化的字典。"""
        return {
            "verdict": self.verdict,
            "severity": self.severity,
            "rule_id": self.rule_id,
            "reason": self.reason,
            "evidence": self.evidence,
            "category": self.category,
        }


def _on_track() -> Verdict:
    """构造「无异常」。"""
    return Verdict()


def _dangerous(
    rule_id: str,
    reason: str,
    evidence: str,
    *,
    severity: str = SEVERITY_STANDARD,
    category: str = "",
) -> Verdict:
    """构造「命中」。

    :param rule_id: 规则 ID。
    :param reason: 中文说明。
    :param evidence: 命中片段。
    :param severity: 严重度。
    :param category: 红线类别。
    :return: 判定结果。
    """
    return Verdict(
        verdict=VERDICT_DANGEROUS,
        severity=severity,
        rule_id=rule_id,
        reason=reason,
        evidence=evidence[:400],
        category=category,
    )


# ---------------------------------------------------------------------------
# 红线：下载即执行
# ---------------------------------------------------------------------------

#: 取回远程内容的命令。
_RE_FETCH = r"(?:curl|wget)"
#: 管道右侧可以吞掉任意代码的解释器。
_RE_INTERPRETER = r"(?:ba|z|k|da)?sh|(?:ba|z|k)?python[0-9.]*|perl|ruby|node|php"
#: ``curl … | sh`` / ``wget -qO- … | sudo bash``。
_RE_REMOTE_EXEC = re.compile(
    rf"\b{_RE_FETCH}\b[^|;&\n]*\|\s*(?:sudo\s+|doas\s+)?(?:[^\s|;&]*/)?(?:{_RE_INTERPRETER})\b",
    re.IGNORECASE,
)


def _check_remote_exec(text: str) -> Verdict | None:
    """检测「下载完直接管道给解释器」。

    :param text: 原始文本（要看跨管道的整句，所以不能只喂切好的片段）。
    :return: 命中结果；没命中返回 ``None``。
    """
    match = _RE_REMOTE_EXEC.search(text)
    if not match:
        return None
    return _dangerous(
        "catastrophic.remote_exec",
        "把网络上下载的内容直接交给解释器执行，执行什么由远端决定",
        match.group(0),
        severity=SEVERITY_RED_LINE,
        category="remote_exec",
    )


#: 输出重定向进块设备，如 ``cat disk.img > /dev/sda``。
#: 明确不覆盖 ``/dev/null`` —— 那是丢弃输出，最常用的安全写法。
_RE_BLOCK_DEVICE_WRITE = re.compile(
    r">\s*/dev/(?:sd|hd|nvme\d|vd|xvd|mmcblk|disk|loop)\w*", re.IGNORECASE
)


def _check_block_device_write(text: str) -> Verdict | None:
    """检测把输出重定向进块设备。

    这是 ``dd of=/dev/sda`` 的另一种写法，原版和多数规则表都漏了这一条。

    :param text: 原始文本。
    :return: 命中结果；没命中返回 ``None``。
    """
    match = _RE_BLOCK_DEVICE_WRITE.search(text)
    if not match:
        return None
    return _dangerous(
        "catastrophic.disk_wipe",
        "把数据直接重定向进块设备，原有内容会被覆盖",
        match.group(0),
        severity=SEVERITY_RED_LINE,
        category="disk_wipe",
    )


# ---------------------------------------------------------------------------
# 红线：抹盘
# ---------------------------------------------------------------------------


def _check_disk_wipe(cmd: Command) -> Verdict | None:
    """检测抹盘 / 覆写块设备。

    :param cmd: 解析后的命令。
    :return: 命中结果；没命中返回 ``None``。
    """
    name = cmd.name
    if name.startswith("mkfs"):
        return _dangerous(
            "catastrophic.disk_wipe",
            "重建文件系统会抹掉整个分区的数据",
            cmd.raw,
            severity=SEVERITY_RED_LINE,
            category="disk_wipe",
        )
    if name == "format":
        for target in cmd.targets:
            if re.fullmatch(r"[A-Za-z]:?", target.strip().strip('"')):
                return _dangerous(
                    "catastrophic.disk_wipe",
                    f"格式化磁盘分区 {target}",
                    cmd.raw,
                    severity=SEVERITY_RED_LINE,
                    category="disk_wipe",
                )
    if name == "dd":
        for arg in cmd.args:
            low = arg.replace("\\", "/").lower()
            if low.startswith("of=/dev/") or low.startswith("of=\\\\.\\"):
                return _dangerous(
                    "catastrophic.disk_wipe",
                    "把数据直接写到块设备，原有内容不可恢复",
                    cmd.raw,
                    severity=SEVERITY_RED_LINE,
                    category="disk_wipe",
                )
    if name == "shred":
        for target in cmd.targets:
            if "/dev/" in target.replace("\\", "/").lower():
                return _dangerous(
                    "catastrophic.disk_wipe",
                    "粉碎块设备",
                    cmd.raw,
                    severity=SEVERITY_RED_LINE,
                    category="disk_wipe",
                )
    return None


# ---------------------------------------------------------------------------
# 红线：删根 / 批量删除
# ---------------------------------------------------------------------------


def _is_recursive(cmd: Command) -> bool:
    """命令是否带递归标志。"""
    return bool(cmd.flags & _RECURSIVE_FLAGS)


def _is_forced(cmd: Command) -> bool:
    """命令是否带强制标志。"""
    return bool(cmd.flags & _FORCE_FLAGS)


def _check_delete(cmd: Command) -> Verdict | None:
    """检测删除类命令。

    分三档：删根 / 删家目录 / 删系统关键目录本身 → 红线；
    其余带 ``-r`` 或 ``-f`` 的删除 → 普通风险。

    :param cmd: 解析后的命令。
    :return: 命中结果；没命中返回 ``None``。
    """
    if cmd.name == "find":
        return _check_find(cmd)
    if cmd.name not in _DELETE_COMMANDS:
        return None

    recursive = _is_recursive(cmd)
    for target in cmd.targets:
        kind = classify_target(target)
        if kind == TARGET_ROOT:
            return _dangerous(
                "catastrophic.root_delete",
                f"删除文件系统根 {target}，整机数据不可恢复",
                cmd.raw,
                severity=SEVERITY_RED_LINE,
                category="root_delete",
            )
        if kind == TARGET_HOME:
            return _dangerous(
                "catastrophic.root_delete",
                f"删除整个家目录 {target}",
                cmd.raw,
                severity=SEVERITY_RED_LINE,
                category="root_delete",
            )
        if kind == TARGET_SENSITIVE and recursive:
            return _dangerous(
                "catastrophic.root_delete",
                f"递归删除系统关键目录 {target}",
                cmd.raw,
                severity=SEVERITY_RED_LINE,
                category="root_delete",
            )

    if recursive or _is_forced(cmd):
        rule_id = "dangerous.rm_rf" if recursive and _is_forced(cmd) else "dangerous.rm_recursive"
        return _dangerous(rule_id, "递归/强制删除，误删后难以恢复", cmd.raw)
    return None


def _check_find(cmd: Command) -> Verdict | None:
    """检测 ``find`` 的批量删除用法。

    :param cmd: 解析后的命令。
    :return: 命中结果；没命中返回 ``None``。
    """
    destructive = "delete" in cmd.flags or "exec" in cmd.flags or "execdir" in cmd.flags
    if not destructive:
        return None
    paths = [item for item in cmd.targets if not item.startswith("-")]
    start = paths[0] if paths else "."
    kind = classify_target(start)
    if kind in {TARGET_ROOT, TARGET_HOME, TARGET_SENSITIVE}:
        return _dangerous(
            "catastrophic.mass_delete",
            f"从 {start} 起批量删除，范围覆盖整个系统",
            cmd.raw,
            severity=SEVERITY_RED_LINE,
            category="mass_delete",
        )
    return _dangerous("dangerous.find_delete", "批量删除文件", cmd.raw)


def _check_permission_open(cmd: Command) -> Verdict | None:
    """检测「把权限放开到所有人」的操作。

    ``chmod 777 ./script.sh`` 是日常操作，不该拦；``chmod -R 777 /`` 是把整机
    的写权限交出去，性质接近删根。区别只在**作用范围**，所以判定也落在范围上。

    :param cmd: 解析后的命令。
    :return: 命中结果；没命中返回 ``None``。
    """
    if cmd.name != "chmod":
        return None
    targets = list(cmd.targets)
    if len(targets) < 2:
        return None
    mode = targets[0].lower().strip()
    wide = mode in {"777", "a+rwx", "ugo+rwx"} or "777" in mode
    if not wide:
        return None
    for path in targets[1:]:
        kind = classify_target(path)
        if kind in {TARGET_ROOT, TARGET_HOME, TARGET_SENSITIVE}:
            return _dangerous(
                "catastrophic.permission_open",
                f"把 {path} 的权限放开给所有人，等于拆掉系统的访问控制",
                cmd.raw,
                severity=SEVERITY_RED_LINE,
                category="permission_open",
            )
    if _is_recursive(cmd):
        return _dangerous("dangerous.chmod_recursive", "递归放开目录权限", cmd.raw)
    return None


# ---------------------------------------------------------------------------
# 普通风险：正则规则表
# ---------------------------------------------------------------------------

#: (规则 ID, 正则, 中文说明)。顺序即优先级。
_TEXT_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "dangerous.git_push_force",
        re.compile(r"\bgit\s+push\b[^\n|;&]*(?:\s--force\b|\s-f\b|\s--force-with-lease\b)"),
        "强制推送会覆盖远端历史",
    ),
    (
        "dangerous.invoke_expression",
        re.compile(r"\b(?:Invoke-Expression|iex)\b", re.IGNORECASE),
        "把字符串当代码执行（PowerShell）",
    ),
    (
        "dangerous.set_execution_policy",
        re.compile(r"\bSet-ExecutionPolicy\b", re.IGNORECASE),
        "放宽 PowerShell 脚本执行策略",
    ),
    (
        "dangerous.remove_item_recurse",
        re.compile(r"\bRemove-Item\b[^\n|;&]*-(?:Recurse|Force)\b", re.IGNORECASE),
        "递归强制删除（PowerShell）",
    ),
    (
        "dangerous.takeown",
        re.compile(r"\btakeown\b", re.IGNORECASE),
        "夺取文件所有权",
    ),
    (
        "dangerous.chown_root",
        re.compile(r"\bchown\b[^\n|;&]*\b(?:root|0):?\w*\b", re.IGNORECASE),
        "把文件所有权交给 root",
    ),
    (
        "dangerous.history_clear",
        re.compile(r"\bhistory\s+-c\b|>\s*~?/?.?(?:bash|zsh)_history\b", re.IGNORECASE),
        "清空命令历史，事后无法追溯",
    ),
    (
        "dangerous.shutdown",
        re.compile(r"\b(?:shutdown|reboot|poweroff|halt)\b(?:\s+-[a-z]+|\s+/[a-z])?", re.IGNORECASE),
        "关机 / 重启会中断所有正在跑的任务",
    ),
    (
        "dangerous.del_fsq",
        re.compile(r"\bdel\b[^\n|;&]*\s/[fsq]\b", re.IGNORECASE),
        "强制递归删除（del /F /S /Q）",
    ),
    (
        "dangerous.rmdir_s",
        re.compile(r"\b(?:rmdir|rd)\b[^\n|;&]*\s/s\b", re.IGNORECASE),
        "递归删除目录树（rmdir /S）",
    ),
    (
        "dangerous.sudo",
        re.compile(r"(?:^|[\s;&|(])sudo\s+\S", re.IGNORECASE),
        "以 root 身份执行，影响范围超出当前用户",
    ),
)

# ---------------------------------------------------------------------------
# 循环检测
# ---------------------------------------------------------------------------


class LoopDetector:
    """检测「反复读同一个东西」的死循环。

    只在挂载监控时使用（预检是单次文本，用不上）。判据是最近 ``window`` 次
    操作里，同一个 ``(工具, 目标)`` 出现次数达到 ``threshold``。

    :param window: 观察窗口长度。
    :param threshold: 触发阈值。
    """

    def __init__(self, *, window: int = 12, threshold: int = 5) -> None:
        self.window = max(2, int(window))
        self.threshold = max(2, int(threshold))
        self._recent: deque[tuple[str, str]] = deque(maxlen=self.window)
        self._last: tuple[str, str] = ("", "")

    def observe(self, tool: str, target: str) -> bool:
        """记录一次操作并判断是否构成循环。

        :param tool: 工具名。
        :param target: 操作目标。
        :return: 是否判定为循环。
        """
        key = (str(tool or "").strip().lower(), str(target or "").strip())
        self._last = key
        self._recent.append(key)
        if not key[1]:
            return False
        return sum(1 for item in self._recent if item == key) >= self.threshold

    def reset(self) -> None:
        """清空窗口。"""
        self._recent.clear()
        self._last = ("", "")

    @property
    def current(self) -> tuple[str, str]:
        """最近一次观察到的 ``(工具, 目标)``。"""
        return self._last


# ---------------------------------------------------------------------------
# 规则引擎
# ---------------------------------------------------------------------------


class RuleEngine:
    """把解析与判定串起来的入口。

    :param config: 可选配置字典；目前只认 ``dangerous_commands``（追加正则）
        与 ``disabled_rules``（按 ID 关闭）。
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        raw = config if isinstance(config, dict) else {}
        self._disabled = {
            str(item).strip()
            for item in (raw.get("disabled_rules") or [])
            if str(item).strip()
        }
        self._extra = self._compile_extra(raw.get("dangerous_commands"))
        self._text_rules = tuple(
            item for item in _TEXT_RULES if item[0] not in self._disabled
        )
        self._loop = LoopDetector()

    @staticmethod
    def _compile_extra(patterns: Any) -> tuple[tuple[str, re.Pattern[str], str], ...]:
        """把配置里追加的正则编译进来。

        :param patterns: 正则字符串列表。
        :return: 编译后的规则元组。
        """
        out: list[tuple[str, re.Pattern[str], str]] = []
        for index, item in enumerate(patterns or []):
            text = str(item or "").strip()
            if not text:
                continue
            try:
                out.append((f"custom.{index}", re.compile(text), "自定义危险命令"))
            except re.error:
                continue
        return tuple(out)

    # -- 对外 ------------------------------------------------------------
    def check_text(self, text: str) -> Verdict:
        """判定一段任务文本或命令。

        :param text: 输入文本。
        :return: 判定结果。
        """
        raw = str(text or "")
        if not raw.strip():
            return _on_track()

        verdict = self._check_red_line(raw)
        if verdict is not None:
            return verdict
        return self._check_standard(raw) or _on_track()

    def check_event(self, event: dict[str, Any]) -> Verdict:
        """判定一条结构化事件（``tool_call`` 形态）。

        :param event: 事件字典。
        :return: 判定结果。
        """
        if not isinstance(event, dict):
            return _on_track()
        tool = event.get("tool")
        if not isinstance(tool, dict):
            return self.check_text(str(event.get("text") or ""))
        name = str(tool.get("name") or "")
        arguments = tool.get("arguments")
        if isinstance(arguments, dict):
            text = str(arguments.get("command") or arguments.get("text") or "")
            if not text:
                text = " ".join(str(value) for value in arguments.values() if isinstance(value, str))
        else:
            text = str(arguments or "")
        verdict = self.check_text(text)
        if verdict.verdict == VERDICT_ON_TRACK and name and self._loop.observe(name, text):
            return Verdict(
                verdict=VERDICT_LOOP,
                severity=SEVERITY_STANDARD,
                rule_id="repeat_read_loop",
                reason="同一个目标被反复读取，疑似陷入循环",
                evidence=text[:200],
            )
        return verdict

    # -- 内部 ------------------------------------------------------------
    def _enabled(self, rule_id: str) -> bool:
        """这条规则是否没被配置关掉。

        :param rule_id: 规则 ID。
        :return: 是否启用。
        """
        return rule_id not in self._disabled

    def _accept(self, hit: Verdict | None) -> Verdict | None:
        """过滤掉被配置禁用的命中。

        :param hit: 命中结果。
        :return: 保留的命中，或 ``None``。
        """
        if hit is None or not self._enabled(hit.rule_id):
            return None
        return hit

    def _check_red_line(self, text: str) -> Verdict | None:
        """跑红线判定。

        :param text: 原始文本。
        :return: 命中结果；没命中返回 ``None``。
        """
        # 这两类要看**跨管道 / 跨重定向**的整句，所以喂原文而不是切好的片段
        hit = self._accept(_check_remote_exec(text)) or self._accept(_check_block_device_write(text))
        if hit is not None:
            return hit

        for segment in split_segments(text):
            cmd = parse_command(segment)
            if cmd is None:
                continue
            hit = self._accept(
                _check_disk_wipe(cmd) or _check_delete(cmd) or _check_permission_open(cmd)
            )
            if hit is not None:
                return hit
        return None

    def _check_standard(self, text: str) -> Verdict | None:
        """跑普通风险判定。

        :param text: 原始文本。
        :return: 命中结果；没命中返回 ``None``。
        """
        for rule_id, pattern, reason in self._text_rules:
            match = pattern.search(text)
            if match:
                return _dangerous(rule_id, reason, match.group(0))
        for rule_id, pattern, reason in self._extra:
            match = pattern.search(text)
            if match:
                return _dangerous(rule_id, reason, match.group(0))
        return None
