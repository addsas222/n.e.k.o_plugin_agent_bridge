"""事件账本 —— 哨兵的内置持久化层（SQLite，纯标准库）。

为什么需要它
============

只有日志尾随的时候，哨兵是个「看过就忘」的旁观者：告警弹完就没了，早上想知道
昨晚发生了什么只能去翻 Agent 自己的日志。要让下面这些能力成立，必须先有一本账：

* **案发时间线**：把「它怎么一步步走到这一步」串起来；
* **夜间晨报**：聚合昨夜事件成一份摘要；
* **Token 消耗审计**：按会话累计用量；
* **安全点回滚**：记下每个回滚点，才能知道回滚过几次、回到了哪。

设计取舍
========

* **SQLite 而不是 JSONL**：要按 session / 时间窗 / 严重度做过滤和聚合，一次
  ``WHERE`` 就能出结果，不必把整个文件读进内存再筛。
* **WAL 模式**：尾随线程写、诊断入口读，两者并发是常态；默认的 rollback journal
  会让读阻塞写。
* **每线程一个连接**：``sqlite3.Connection`` 不跨线程共享。用 ``threading.local``
  各拿各的，比全局锁简单也更并行。
* **fail-open 到底**：账本写失败（磁盘满、文件被占用）绝不能反过来让派发失败。
  所有公开方法吞异常并降级，只有显式传 ``strict=True`` 才往外抛。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .redact import scrub
from .rules import SEVERITY_RED_LINE, SEVERITY_STANDARD

__all__ = ["EventStore", "default_db_path", "resolve_db_path"]

logger = logging.getLogger(__name__)

#: 落盘前要脱敏的**自由文本**字段。
#: 事件种类。
KIND_PRECHECK = "precheck"
KIND_LINE = "line"
KIND_VERDICT = "verdict"
KIND_TOOL = "tool"
KIND_ERROR = "error"
KIND_SYSTEM = "system"

#: 告警级别。
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_CRITICAL = "critical"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    fingerprint TEXT PRIMARY KEY,
    session_id  TEXT,
    cwd         TEXT,
    pid         INTEGER,
    agent       TEXT,
    goal        TEXT,
    started_at  REAL,
    ended_at    REAL,
    exit_code   INTEGER,
    events      INTEGER DEFAULT 0,
    alerts      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,
    severity    TEXT,
    verdict     TEXT,
    rule_id     TEXT,
    summary     TEXT,
    detail      TEXT,
    raw         TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     INTEGER,
    fingerprint  TEXT,
    ts           REAL NOT NULL,
    level        TEXT NOT NULL,
    title        TEXT,
    body         TEXT,
    delivered    INTEGER DEFAULT 0,
    channel      TEXT,
    delivered_at REAL
);

CREATE TABLE IF NOT EXISTS safepoints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,
    ref         TEXT,
    label       TEXT,
    files       INTEGER DEFAULT 0,
    restored    INTEGER DEFAULT 0,
    restored_at REAL
);

CREATE TABLE IF NOT EXISTS tokens (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint   TEXT,
    ts            REAL NOT NULL,
    agent         TEXT,
    model         TEXT,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    saved_tokens  INTEGER DEFAULT 0
);

-- 面板会话表按 started_at 倒序取（见 sessions()）。_SCHEMA 由 _ensure_schema
-- 幂等执行，所以这条对既有库自动生效，不需要迁移脚本。
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_events_ts       ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_fp       ON events(fingerprint, ts);
CREATE INDEX IF NOT EXISTS idx_events_severity ON events(severity, ts);
CREATE INDEX IF NOT EXISTS idx_alerts_pending  ON alerts(delivered, ts);
CREATE INDEX IF NOT EXISTS idx_safepoints_fp   ON safepoints(fingerprint, ts);
CREATE INDEX IF NOT EXISTS idx_tokens_ts       ON tokens(ts);
"""


def default_db_path() -> Path:
    """默认账本位置。

    跟着插件的数据目录走；拿不到时退到当前工作目录下的 ``data/``。

    :return: 账本文件路径。
    """
    return Path.cwd() / "data" / "sentinel.db"


def resolve_db_path(raw: Any, *, fallback: Any = None) -> Path:
    """把配置里的账本路径解析成 ``Path``。

    :param raw: 配置值（可能是空、相对路径或绝对路径）。
    :param fallback: 兜底目录（通常是插件的 data 目录）。
    :return: 账本文件路径；相对路径按兜底目录展开。
    """
    text = str(raw or "").strip()
    if not text:
        base = Path(str(fallback)) if fallback else default_db_path().parent
        return base / "sentinel.db"
    path = Path(text).expanduser()
    if path.is_absolute():
        return path
    base = Path(str(fallback)) if fallback else default_db_path().parent
    return base / path


class EventStore:
    """事件账本。

    :ivar path: 账本文件路径。
    :ivar enabled: 是否启用（``path`` 为空时自动关闭，退化成不落盘）。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_db_path()
        self.enabled = bool(str(path or "").strip()) or path is not None
        self._local = threading.local()
        self._schema_ready = False
        self._lock = threading.Lock()
        if self.enabled:
            self._ensure_schema()

    # -- 连接与建表 ---------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        """取本线程的连接（首次调用时建立）。

        :return: SQLite 连接。
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        # WAL：读写并发不互斥。内存库不支持 WAL，失败就算了。
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            logger.debug("账本切换 WAL 失败（不影响使用）")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        self._local.conn = conn
        return conn

    def _ensure_schema(self) -> None:
        """建表（幂等）。"""
        with self._lock:
            if self._schema_ready:
                return
            try:
                conn = self._connect()
                conn.executescript(_SCHEMA)
                conn.commit()
                self._schema_ready = True
            except sqlite3.Error as exc:
                logger.warning("账本建表失败，哨兵将不落盘：%s: %s", type(exc).__name__, exc)
                self.enabled = False

    def close(self) -> None:
        """关闭本线程的连接。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        try:
            conn.close()
        except sqlite3.Error:
            pass
        self._local.conn = None

    # -- 内部执行 -----------------------------------------------------------
    def _write(self, sql: str, params: Iterable[Any] = (), *, strict: bool = False) -> int:
        """执行一条写语句。

        :param sql: SQL。
        :param params: 参数。
        :param strict: 为真时异常外抛，否则记日志并返回 0。
        :return: 自增主键（无则 0）。
        """
        if not self.enabled:
            return 0
        try:
            conn = self._connect()
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            return int(cur.lastrowid or 0)
        except sqlite3.Error as exc:
            logger.warning("账本写入失败（已降级）：%s: %s", type(exc).__name__, exc)
            if strict:
                raise
            return 0

    def _read(self, sql: str, params: Iterable[Any] = (), *, strict: bool = False) -> list[dict[str, Any]]:
        """执行一条查询并转成字典列表。

        :param sql: SQL。
        :param params: 参数。
        :param strict: 为真时异常外抛，否则返回空列表。
        :return: 结果行。
        """
        if not self.enabled:
            return []
        try:
            conn = self._connect()
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            logger.warning("账本查询失败（已降级）：%s: %s", type(exc).__name__, exc)
            if strict:
                raise
            return []

    # -- 会话 ---------------------------------------------------------------
    def upsert_session(self, session: Any, *, strict: bool = False) -> None:
        """登记或更新一路会话。

        :param session: ``SessionContext`` 实例（鸭子类型：只要有那几个属性即可）。
        :param strict: 为真时异常外抛。
        """
        self._write(
            """
            INSERT INTO sessions (fingerprint, session_id, cwd, pid, agent, goal,
                                  started_at, events, alerts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                cwd     = excluded.cwd,
                pid     = excluded.pid,
                agent   = excluded.agent,
                goal    = COALESCE(NULLIF(excluded.goal, ''), sessions.goal),
                events  = excluded.events,
                alerts  = excluded.alerts
            """,
            (
                getattr(session, "fingerprint", ""),
                getattr(session, "session_id", ""),
                getattr(session, "cwd", ""),
                int(getattr(session, "pid", 0) or 0),
                getattr(session, "agent", ""),
                # goal 通常是用户原话（可能带示例里的 token），落盘前脱敏。
                scrub(getattr(session, "goal", "")),
                float(getattr(session, "created_at", 0) or time.time()),
                int(getattr(session, "events", 0) or 0),
                int(getattr(session, "alerts", 0) or 0),
            ),
            strict=strict,
        )

    def close_session(self, fingerprint: str, *, exit_code: int | None = None, strict: bool = False) -> None:
        """给一路会话收尾。

        :param fingerprint: 会话指纹。
        :param exit_code: 进程退出码。
        :param strict: 为真时异常外抛。
        """
        self._write(
            "UPDATE sessions SET ended_at = ?, exit_code = ? WHERE fingerprint = ?",
            (time.time(), exit_code, fingerprint),
            strict=strict,
        )

    def sessions(
        self,
        *,
        running: bool | None = None,
        since: float | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """按开始时间倒序取会话账目（面板会话表用）。

        **刻意不加 ``WHERE fingerprint != ''``。** 空指纹会话行在真实运行时不会
        产生：``SentinelRuntime.begin_session`` 用 ``uuid.uuid4().hex`` 生成身份，
        ``attach_pid`` 有 ``if not fingerprint: return`` 的守卫，而 ``upsert_session``
        全仓只有这两个调用点。所以真出现了空指纹行，那是「有东西绕过了登记流程」，
        属于该被看见的异常 —— 滤掉它等于把「账本里有脏数据」这条线索擦干净，
        而那正是排查时最需要的一条。面板对空指纹只做标注，不做过滤。

        **不做 ``LEFT JOIN events`` 聚合。** ``sessions`` 表自带 ``events`` /
        ``alerts`` 两个计数列（见 ``_SCHEMA``），聚合是白拿的；而
        ``LEFT JOIN events GROUP BY fingerprint`` 会把行数放大几百倍，还会把所有
        ``fingerprint`` 为空的事件全绑到同一条空指纹会话上，制造出一条假会话。

        不加 JOIN 的代价是这条 SQL 只读一张表，``started_at`` 上有
        ``idx_sessions_started`` 索引，而它建在 ``_SCHEMA`` 里、由
        ``_ensure_schema`` 实例级幂等执行，所以旧库升级后自动生效，不需要迁移脚本。

        :param running: ``True`` 只看进行中（``ended_at IS NULL``）；``False`` 只看
            已结束；``None`` 不限。**这个维度必须能在 SQL 层过滤**：``_sessions``
            是内存 registry，重启后账本里那些 ``ended_at`` 仍是 NULL，会跨重启累积，
            所以「取最新 20 条再在 Python 里切分」会让已结束那一块被僵尸会话挤空 ——
            那是「没读到」被显示成「没有」。
        :param since: 只取 ``started_at >= since`` 的会话。
        :param limit: 最多几条（取最近的 N 条）。
        :return: 会话行；账本不可用时为空列表（``_read`` 内部已降级）。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if running is True:
            clauses.append("ended_at IS NULL")
        elif running is False:
            clauses.append("ended_at IS NOT NULL")
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(float(since))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        return self._read(
            f"SELECT * FROM sessions {where} "
            "ORDER BY COALESCE(started_at, 0) DESC, fingerprint DESC LIMIT ?",
            params,
        )

    # -- 事件 ---------------------------------------------------------------
    def add_event(
        self,
        *,
        kind: str,
        fingerprint: str = "",
        severity: str = "",
        verdict: str = "",
        rule_id: str = "",
        summary: str = "",
        detail: str = "",
        raw: str = "",
        ts: float | None = None,
        strict: bool = False,
    ) -> int:
        """记一条事件。

        :param kind: 事件种类（``KIND_*``）。
        :param fingerprint: 会话指纹。
        :param severity: 严重度。
        :param verdict: 判定。
        :param rule_id: 命中规则。
        :param summary: 一句话摘要（时间线上显示的就是它）。
        :param detail: 细节。
        :param raw: 原始文本（可截断后存）。
        :param ts: 时间戳，默认当前。
        :param strict: 为真时异常外抛。
        :return: 事件 ID。

        ``summary`` / ``detail`` / ``raw`` 是自由文本，落盘前会过 :func:`redact.scrub`：
        账本默认明文存在本机，而这三处最容易夹带命令里的 token。
        """
        return self._write(
            """
            INSERT INTO events (fingerprint, ts, kind, severity, verdict, rule_id, summary, detail, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fingerprint,
                float(ts if ts is not None else time.time()),
                kind,
                severity,
                verdict,
                rule_id,
                scrub(summary),
                scrub(detail),
                scrub(raw),
            ),
            strict=strict,
        )

    # -- 告警 ---------------------------------------------------------------
    def add_alert(
        self,
        *,
        level: str,
        title: str,
        body: str = "",
        fingerprint: str = "",
        event_id: int = 0,
        ts: float | None = None,
        strict: bool = False,
    ) -> int:
        """记一条待投递的告警。

        :param level: 级别（``LEVEL_*``）。
        :param title: 标题。
        :param body: 正文。
        :param fingerprint: 会话指纹。
        :param event_id: 关联事件。
        :param ts: 时间戳。
        :param strict: 为真时异常外抛。
        :return: 告警 ID。

        ``title`` / ``body`` 落盘前过 :func:`redact.scrub`：告警正文里通常会原样
        引用命中的命令，那正是最容易带出 token 的地方。
        """
        return self._write(
            """
            INSERT INTO alerts (event_id, fingerprint, ts, level, title, body)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_id or None,
                fingerprint,
                float(ts if ts is not None else time.time()),
                level,
                scrub(title),
                scrub(body),
            ),
            strict=strict,
        )

    def pending_alerts(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """取尚未投递的告警（按时间正序）。

        :param limit: 最多几条。
        :return: 告警行。
        """
        return self._read(
            "SELECT * FROM alerts WHERE delivered = 0 ORDER BY ts ASC LIMIT ?",
            (int(limit),),
        )

    def alerts(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """按时间窗取告警（**含已投递的**），时间倒序。

        与 :meth:`pending_alerts` 的区别：那个是「投递队列」，这个是「历史账目」。
        晨报要的是后者 —— 已经推给 webhook 的告警同样属于昨夜发生的事。

        :param since: 起始时间戳（含）。
        :param until: 结束时间戳（含）。
        :param limit: 最多几条。
        :return: 告警行。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(float(since))
        if until is not None:
            clauses.append("ts <= ?")
            params.append(float(until))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        return self._read(f"SELECT * FROM alerts {where} ORDER BY ts DESC, id DESC LIMIT ?", params)

    def mark_delivered(self, alert_id: int, *, channel: str = "", strict: bool = False) -> None:
        """标记告警已投递。

        :param alert_id: 告警 ID。
        :param channel: 投递通道。
        :param strict: 为真时异常外抛。
        """
        self._write(
            "UPDATE alerts SET delivered = 1, channel = ?, delivered_at = ? WHERE id = ?",
            (channel, time.time(), int(alert_id)),
            strict=strict,
        )

    # -- 安全点 -------------------------------------------------------------
    def add_safepoint(
        self,
        *,
        kind: str,
        label: str = "",
        ref: str = "",
        fingerprint: str = "",
        files: int = 0,
        ts: float | None = None,
        strict: bool = False,
    ) -> int:
        """记一个安全点。

        :param kind: 类型（``git`` / ``files``）。
        :param label: 人类可读标签。
        :param ref: 引用（git 树对象、备份目录等）。
        :param fingerprint: 会话指纹。
        :param files: 涉及文件数。
        :param ts: 时间戳。
        :param strict: 为真时异常外抛。
        :return: 安全点 ID。

        ``label`` 是自由文本（常直接引用用户那句话），落盘前过 :func:`redact.scrub`；
        ``ref`` 是 git 对象名，原样保留——它是恢复时要用的**凭据**，打码就回滚不了。
        """
        return self._write(
            """
            INSERT INTO safepoints (fingerprint, ts, kind, ref, label, files)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (fingerprint, float(ts if ts is not None else time.time()), kind, ref, scrub(label), int(files)),
            strict=strict,
        )

    def safepoints(
        self,
        *,
        older_than: float | None = None,
        keep: int = 0,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        """取「该清理了」的安全点记录（供 git ref 回收使用）。

        语义是**并集**：``older_than`` 之前的老记录，**加上**按时间倒序第 ``keep`` 个
        之后的多余记录。两者都满足「删掉不影响用户」——老的是过期垃圾，多的是噪音。

        :param older_than: 时间戳；早于它的记录算过期。``None`` 表示不按年龄筛。
        :param keep: 保留最近多少条；``<= 0`` 表示不按数量筛。
        :param limit: 单次最多扫多少行（防止一次读空整张表）。
        :return: 待清理的记录，时间倒序。
        """
        rows = self._read(
            "SELECT * FROM safepoints ORDER BY ts DESC, id DESC LIMIT ?",
            (int(max(1, limit)),),
        )
        selected: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            try:
                ts = float(row.get("ts") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            too_old = older_than is not None and ts < float(older_than)
            beyond_keep = keep > 0 and index >= int(keep)
            if too_old or beyond_keep:
                selected.append(row)
        return selected

    def forget_safepoints(self, refs: Iterable[str], *, strict: bool = False) -> int:
        """删掉指定 ``ref`` 的安全点记录（ref 已被 git 回收时调用）。

        :param refs: 安全点的 ``ref`` 集合。
        :param strict: 为真时异常外抛。
        :return: 实际删除的行数。
        """
        names = [str(item) for item in refs if str(item or "").strip()]
        if not names or not self.enabled:
            return 0
        try:
            conn = self._connect()
            placeholders = ",".join("?" * len(names))
            cur = conn.execute(f"DELETE FROM safepoints WHERE ref IN ({placeholders})", tuple(names))
            conn.commit()
            return int(cur.rowcount or 0)
        except sqlite3.Error as exc:
            logger.warning("安全点记录清理失败（已降级）：%s: %s", type(exc).__name__, exc)
            if strict:
                raise
            return 0

    def mark_restored(self, safepoint_id: int, *, strict: bool = False) -> None:
        """标记安全点已回滚。

        :param safepoint_id: 安全点 ID。
        :param strict: 为真时异常外抛。
        """
        self._write(
            "UPDATE safepoints SET restored = 1, restored_at = ? WHERE id = ?",
            (time.time(), int(safepoint_id)),
            strict=strict,
        )

    def latest_safepoint(self, *, fingerprint: str = "") -> dict[str, Any] | None:
        """取最近一个未回滚的安全点。

        :param fingerprint: 会话指纹；空则取全局最近一个。
        :return: 安全点行；没有则 ``None``。
        """
        if fingerprint:
            rows = self._read(
                "SELECT * FROM safepoints WHERE fingerprint = ? AND restored = 0 ORDER BY ts DESC LIMIT 1",
                (fingerprint,),
            )
        else:
            rows = self._read("SELECT * FROM safepoints WHERE restored = 0 ORDER BY ts DESC LIMIT 1")
        return rows[0] if rows else None

    # -- Token --------------------------------------------------------------
    def add_tokens(
        self,
        *,
        agent: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        saved_tokens: int = 0,
        fingerprint: str = "",
        ts: float | None = None,
        strict: bool = False,
    ) -> int:
        """记一次 Token 用量。

        :param agent: Agent 标识。
        :param model: 模型名。
        :param input_tokens: 输入 Token。
        :param output_tokens: 输出 Token。
        :param saved_tokens: 被拦下而省下的估算值。
        :param fingerprint: 会话指纹。
        :param ts: 时间戳。
        :param strict: 为真时异常外抛。
        :return: 记录 ID。
        """
        return self._write(
            """
            INSERT INTO tokens (fingerprint, ts, agent, model, input_tokens, output_tokens, saved_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fingerprint,
                float(ts if ts is not None else time.time()),
                agent,
                model,
                int(input_tokens),
                int(output_tokens),
                int(saved_tokens),
            ),
            strict=strict,
        )

    # -- 查询 ---------------------------------------------------------------
    def timeline(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        fingerprint: str = "",
        severity: str = "",
        kinds: Iterable[str] = (),
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """按时间正序取事件，构成一条时间线。

        :param since: 起始时间戳（含）。
        :param until: 结束时间戳（含）。
        :param fingerprint: 只看某一路会话。
        :param severity: 只看某严重度。
        :param kinds: 只看某些种类。
        :param limit: 最多几条（取最近的 N 条后按时间正序返回）。
        :return: 事件行。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(float(since))
        if until is not None:
            clauses.append("ts <= ?")
            params.append(float(until))
        if fingerprint:
            clauses.append("fingerprint = ?")
            params.append(fingerprint)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        kind_list = [str(item) for item in kinds if str(item)]
        if kind_list:
            clauses.append(f"kind IN ({','.join('?' * len(kind_list))})")
            params.extend(kind_list)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        rows = self._read(f"SELECT * FROM events {where} ORDER BY ts DESC, id DESC LIMIT ?", params)
        rows.reverse()
        return rows

    def stats(self, *, since: float | None = None, until: float | None = None) -> dict[str, Any]:
        """汇总一段时间的账目，供晨报与诊断使用。

        :param since: 起始时间戳。
        :param until: 结束时间戳。
        :return: ``sessions`` / ``events`` / ``alerts`` / ``blocked`` / ``tokens`` 等。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(float(since))
        if until is not None:
            clauses.append("ts <= ?")
            params.append(float(until))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        events = self._read(
            f"SELECT kind, severity, COUNT(*) AS n FROM events {where} GROUP BY kind, severity",
            params,
        )
        by_kind: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        for row in events:
            kind = str(row.get("kind") or "")
            by_kind[kind] = by_kind.get(kind, 0) + int(row.get("n") or 0)
            severity = str(row.get("severity") or "")
            if severity:
                by_severity[severity] = by_severity.get(severity, 0) + int(row.get("n") or 0)

        alerts = self._read(
            f"SELECT level, COUNT(*) AS n FROM alerts {where} GROUP BY level",
            params,
        )
        by_level = {str(row.get("level") or ""): int(row.get("n") or 0) for row in alerts}

        tokens = self._read(
            f"""
            SELECT COALESCE(SUM(input_tokens), 0)  AS tin,
                   COALESCE(SUM(output_tokens), 0) AS tout,
                   COALESCE(SUM(saved_tokens), 0)  AS tsaved
            FROM tokens {where}
            """,
            params,
        )
        token_row = tokens[0] if tokens else {}

        # 会话用的是 `started_at`，而 `where` 里的子句是给 `ts` 写的，不能复用 ——
        # 但**窗口必须一样**：以前这里只过滤 `started_at >= since`、整个忽略 `until`，
        # 于是「昨晚 00:00-06:00 的晨报」会把今天上午才开的会话也算进去。
        session_clauses: list[str] = []
        session_params: list[Any] = []
        if since is not None:
            session_clauses.append("started_at >= ?")
            session_params.append(float(since))
        if until is not None:
            session_clauses.append("started_at <= ?")
            session_params.append(float(until))
        session_where = f"WHERE {' AND '.join(session_clauses)}" if session_clauses else ""
        sessions = self._read(
            f"SELECT COUNT(*) AS n FROM sessions {session_where}",
            session_params,
        )

        return {
            "sessions": int(sessions[0]["n"]) if sessions else 0,
            "events": sum(by_kind.values()),
            "by_kind": by_kind,
            "by_severity": by_severity,
            "alerts": sum(by_level.values()),
            "by_level": by_level,
            "blocked": int(by_severity.get(SEVERITY_RED_LINE, 0)) + int(by_severity.get(SEVERITY_STANDARD, 0)),
            "input_tokens": int(token_row.get("tin") or 0),
            "output_tokens": int(token_row.get("tout") or 0),
            "saved_tokens": int(token_row.get("tsaved") or 0),
        }

    def prune(self, *, days: int = 30, strict: bool = False) -> int:
        """删掉过老的记录。

        **不碰 ``safepoints`` 表**：安全点的记录与用户仓库里的 ``refs/sentinel/*``
        是一一对应的，先删记录会让那些 git ref 变成永远没人认领的孤儿（反而更难清）。
        安全点的回收由 :meth:`SafePointManager.prune` 负责——它同时删 ref 与记录，
        且需要仓库路径才能 ``update-ref -d``。

        :param days: 保留天数。
        :param strict: 为真时异常外抛。
        :return: 删除的事件行数（其余表不单独统计）。
        """
        cutoff = time.time() - max(1, int(days)) * 86400
        if not self.enabled:
            return 0
        try:
            conn = self._connect()
            cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            conn.execute("DELETE FROM alerts WHERE ts < ? AND delivered = 1", (cutoff,))
            conn.execute("DELETE FROM tokens WHERE ts < ?", (cutoff,))
            conn.commit()
            return int(cur.rowcount or 0)
        except sqlite3.Error as exc:
            logger.warning("账本清理失败（已降级）：%s: %s", type(exc).__name__, exc)
            if strict:
                raise
            return 0
