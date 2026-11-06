"""脱敏 —— 让凭据不进盘、不出网。

为什么有这个模块
================

账本（``sentinel.db``）默认落在本机磁盘上，而它记的东西恰恰是最容易夹带密钥的：
Agent 的命令全文、它读到的输出、判定理由。一条 ``deploy --token=…``、一句
``curl -H 'Authorization: Bearer …'``，密钥就永久留在了盘上；如果用户还配了
webhook，它会同一条路径离开本机。

所以这里只做一件事：**在写入之前、在发送之前，把看起来像凭据的片段换掉**。
两道口子都过同一个函数是刻意的——两套规则早晚会漂移，届时「本地打码了但外发
没打码」这种最难查的组合就会出现。

边界（别误读）
==============

* 这是**正则启发式**，不是密钥检测器。规则之外的自定义格式（内部签发的票据、
  自研的 ``X-Acme-Key``）不会被打码。要让某个形态进白名单，就在 ``_RULES`` 加一条。
* 只处理 ```` str``，不改结构化字段（``verdict`` / ``rule_id`` / ``kind`` 这类枚举）。
  打码枚举值会让账本失去可查性，而枚举值本身不承载用户数据。
* **不改写行数结构**：换行保留（时间线与晨报按行排版）；只替换片段、只在超长时截断。
* 打码后**不可逆**。这是设计意图：账本不是密钥库。真需要原文时请用户自己重跑。

失败处理
========

``scrub`` 不接受非字符串、不抛异常：拿不到文本就当空串处理。脱敏出问题绝不能
让记账失败——那是「安全设施反过来造成可用性故障」。
"""

from __future__ import annotations

import re

__all__ = ["DEFAULT_LIMIT", "MARK", "scrub", "scrub_fields"]

#: 打码后的占位符。用 ``***`` 而不是删掉：读者要看得出「这里原本有东西」。
MARK = "***"

#: 默认保留的最大字符数。账本不是日志仓，单字段超过这个量级基本是粘贴事故。
DEFAULT_LIMIT = 2000

#: 判断「像不像凭据」时用的 CJK 范围。
_CJK_RE = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")

#: 纯计数（``12345``、``343.5k``）不打码：晨报里的用量数字必须保持可读。
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?k?")

#: 敏感的键名。前后允许更多键名字符，所以 ``--api-key``、``OPENAI_API_KEY``、
#: ``X-Auth-Token`` 都能命中（``\b`` 在 ``-`` 前不成边界，故刻意不加）。
_KEY_KV = re.compile(
    r"(?i)([A-Za-z0-9_.\-]*(?:api[_-]?key|apikey|token|secret|password|passwd|pwd"
    r"|access[_-]?key|client[_-]?secret)[A-Za-z0-9_.\-]*)"
    r"(\s*[=:]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;&'\"]+)"
)

#: 固定形态的规则，按顺序执行（先整块、再带前缀的、最后通用 key=value）。
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM 私钥：整块替换，否则 base64 正文会被下面的规则逐行漏过。
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        f"-----BEGIN PRIVATE KEY-----{MARK}-----END PRIVATE KEY-----",
    ),
    # Authorization: Bearer <token>
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-+/=]{6,}"), rf"\1 {MARK}"),
    # JWT：三段点分，首段固定 base64 的 ``eyJ``。必须在通用规则前，否则会被拆散。
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}"),
        f"eyJ{MARK}",
    ),
    # OpenAI 风格
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"), f"sk-{MARK}"),
    # GitHub：ghp_ / gho_ / ghs_ / ghu_ / ghr_
    (re.compile(r"\bgh([pousr])_[A-Za-z0-9]{16,}"), rf"gh\1_{MARK}"),
    # AWS access key id
    (re.compile(r"\bAKIA[0-9A-Z]{12,20}\b"), f"AKIA{MARK}"),
    # Slack
    (re.compile(r"\b(xox[baprs])-[A-Za-z0-9\-]{10,}"), rf"\1-{MARK}"),
    # URL 里的 user:password@
    (re.compile(r"(?<=//)([^/\s:@]+):([^@/\s]+)@"), rf"\1:{MARK}@"),
)


def _looks_like_secret(value: str) -> bool:
    """判断 ``key=value`` 里的 ``value`` 像不像凭据。

    这条判据存在的唯一原因是**避免误伤人话**：``password: 请勿泄露`` 是中文说明，
    ``total_tokens: 12345`` 是计数，两者都不该被打成 ``***``，否则用户会以为账本坏了。

    :param value: 原始 value（可能带引号）。
    :return: 像凭据返回 ``True``。
    """
    core = value.strip().strip("\"'").strip()
    if len(core) < 6:
        # 太短的不可能是有效凭据，而 ``token: ***`` 这种已打码值也必须保持幂等。
        return False
    if _CJK_RE.search(core):
        return False
    if _NUMBER_RE.fullmatch(core):
        return False
    return True


def _mask_key_value(match: re.Match[str]) -> str:
    """把命中的 ``key=value`` 打码（保留键名与引号）。

    :param match: ``_KEY_KV`` 的匹配。
    :return: 替换后的文本。
    """
    head, sep, value = match.group(1), match.group(2), match.group(3)
    if not _looks_like_secret(value):
        return match.group(0)
    quote = value[0] if value[:1] in {'"', "'"} else ""
    return f"{head}{sep}{quote}{MARK}{quote}"


def scrub(text: str, *, limit: int = DEFAULT_LIMIT) -> str:
    """把一段文本里的凭据替换成 ``***``，并在超长时截断。

    :param text: 原文（非字符串按空串处理）。
    :param limit: 最大保留字符数；``<= 0`` 表示不截断。
    :return: 脱敏后的文本。
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:  # noqa: BLE001 —— 连 str() 都失败的脏对象，当空串
            return ""

    original_len = len(text)
    if limit and limit > 0 and original_len > limit * 4:
        # 只扫前 4 倍 limit：反正超出 limit 的部分马上会被截掉，没必要为几 MB 的
        # ``raw`` 跑一遍正则（这是每条输出行都要经过的热路径）。
        text = text[: limit * 4]

    out = text
    for pattern, replacement in _RULES:
        out = pattern.sub(replacement, out)
    out = _KEY_KV.sub(_mask_key_value, out)

    if limit and limit > 0 and len(out) > limit:
        # 截断标记带上原长：事后能判断「是被截了」还是「原本就这么短」。
        out = f"{out[:limit]}…（已截断，原长 {original_len} 字符）"
    return out


def scrub_fields(record: dict, fields: object, *, limit: int = DEFAULT_LIMIT) -> dict:
    """对字典里**指定字段**做脱敏，返回副本。

    只处理列出来的字段是刻意的：结构化字段（``verdict`` / ``rule_id`` / ``kind`` /
    ``severity``）必须原样保留，否则按规则聚合、按严重度过滤全部失效。

    :param record: 原始字典（不会被修改）。
    :param fields: 字段名集合。
    :param limit: 单字段最大字符数。
    :return: 脱敏后的新字典；``record`` 不是 dict 时原样返回。
    """
    if not isinstance(record, dict):
        return record
    names = {str(name) for name in fields}  # type: ignore[union-attr]
    out = dict(record)
    for name in names:
        value = out.get(name)
        if isinstance(value, str):
            out[name] = scrub(value, limit=limit)
    return out
