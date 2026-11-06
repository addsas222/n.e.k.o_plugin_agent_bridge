"""哨兵运行时 —— 把各层组装成一个「能用的东西」。

为什么需要这一层
================

各层单独拿出来都能用，但真正的调用方（`bridge/runner.py` 的派发路径、插件入口）
不该知道有 ``EventStore``、``SemanticJudge``、``ChannelHub`` 这些对象，更不该自己
去编排「先预检、再记账、起进程后登记会话、输出时喂适配器」的顺序。

这一层就是那个顺序。它有三个硬性设计目标：

1. **默认什么都不做**：``enabled=false`` 时构造出来的对象每个字段都是 ``None``，
   调用它的方法立刻返回，派发路径的开销约等于零。
2. **任何一层挂了都不影响其它层**：模型不可用只降级 L2；账本写不进只丢记录；
   webhook 不通只留 pending。审计是增强层，不能变成可用性故障点。
3. **线程安全**：尾随线程会同时往里写，账本自己做了每线程连接，这里只保证
   共享状态（会话表）有锁。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .channels import ChannelHub, LocalChannel, WebhookChannel
from .context import SessionRegistry
from .judge import SemanticJudge
from .metrics import TokenLedger, render_usage
from .precheck import ACTION_BLOCK, pre_check
from .rules import SEVERITY_RED_LINE, SEVERITY_STANDARD
from .store import (
    KIND_ERROR,
    KIND_LINE,
    KIND_PRECHECK,
    KIND_SYSTEM,
    KIND_TOOL,
    LEVEL_CRITICAL,
    LEVEL_WARN,
    EventStore,
    resolve_db_path,
)
from .timebox import TimeBox
from .timeline import build_timeline, render_timeline

__all__ = ["SentinelRuntime"]

logger = logging.getLogger(__name__)

#: 单条原始输出存进账本时的最大长度（再长就截断，账本不是日志仓库）。
_RAW_MAX_CHARS = 2000

#: 适配器不可用时，输出行按这个长度截断成摘要。
_SUMMARY_MAX_CHARS = 200

#: 输出流队列上限。满了就丢事件 —— 少记几条远好过把 Agent 卡死。
_QUEUE_MAX = 2000

#: 同一路会话两次干预之间的最小间隔（秒）。
#:
#: 一条 ``rm -rf`` 在 Agent 的输出里往往会重复出现（原样回显、工具调用参数、
#: 报错重试各来一次）。按命中次数动手的话，40 行危险行 = 40 根线程 + 40 次
#: ``taskkill /F /T`` 打同一个 pid（单次上限 10 秒）；而「杀一个进程」这件事本身
#: 是幂等的，重复做只增加把干预线程池打满、以及误伤复用 pid 的风险。
_INTERVENE_MIN_INTERVAL = 30.0

#: 策略层认可的干预动作。``suspend`` 曾是其中之一，现已移除。
#: ``guard.py`` 仍保留 ``suspend``/``resume`` 作为**能力**，只是不再由策略选它。
_GUARD_ACTIONS = frozenset({"alert", "terminate"})


class SentinelRuntime:
    """哨兵运行时。

    :ivar config: ``SentinelConfig``。
    :ivar store: 事件账本；未启用为 ``None``。
    :ivar judge: L2 判定器；未开启为 ``None``。
    :ivar guard: 进程干预器；``guard_action=alert`` 时为 ``None``。
    :ivar safepoints: 安全点管理器；未开启为 ``None``。
    :ivar channels: 告警通道；未启用为 ``None``。
    :ivar timebox: 时间盒；``timebox_seconds<=0`` 时为 ``None``。
    :ivar metrics: Token 账本。
    """

    def __init__(
        self,
        config: Any,
        *,
        judge_fn: Callable[[str], str] | None = None,
        data_dir: str | Path | None = None,
    ) -> None:
        self.config = config
        self.enabled = bool(getattr(config, "enabled", False))
        self._lock = threading.Lock()
        self._sessions = SessionRegistry()
        self._started_at = time.time()
        # 停止信号用 Event，而不是「往队列投一个 None」当结束标记。
        # 原因见 _consume：只有独立于队列的信号，才可能在「收到停止」之后继续把
        # 队列排空再退出；靠轮询标志的老写法一收到停止就 return，残留条目静默消失。
        # 关闭态也要建：close() 在任何状态下都必须可调用（插件 shutdown 是无条件调）。
        self._stop = threading.Event()
        # 干预幂等表：fingerprint -> 上次动手的 monotonic 时刻（见 _maybe_intervene）。
        # 关闭态同样初始化，保证任何入口取用都不会 AttributeError。
        self._intervened_at: dict[str, float] = {}
        self._intervene_lock = threading.Lock()
        # 适配器解析失败次数（见 _parse_line）。和 _dropped 同型：都是「静默降级」的计数。
        # 光有 debug 日志不够用 —— 插件路径上没人调 logging.basicConfig（全仓只有
        # ``__main__.py`` 那个独立 CLI 入口设过级别），root logger 走宿主的 INFO/WARNING，
        # 于是这行 debug 永远不输出。计数是这条降级路径唯一可观测的证据。
        self._parse_failures = 0
        # 在途条目数：已从队列取出、正在 _process 里处理（见 _consume / _drain）。
        # 关闭态也要有：_drain 在任何状态下都可能被调用。
        self._inflight = 0

        if not self.enabled:
            self.store = None
            self.judge = None
            self.guard = None
            self.safepoints = None
            self.channels = None
            self.timebox = None
            self.metrics = TokenLedger(None)
            self._queue = None
            self._dropped = 0
            self._consumer = None
            return

        db_path = resolve_db_path(getattr(config, "store_path", ""), fallback=data_dir)
        self.store = EventStore(db_path)
        # 启动时清一次过期记录；失败不影响使用
        try:
            self.store.prune(days=int(getattr(config, "retention_days", 30) or 30))
        except Exception as exc:  # noqa: BLE001
            logger.debug("账本清理失败（已忽略）：%s: %s", type(exc).__name__, exc)

        self.judge = self._build_judge(judge_fn)
        self.guard = self._build_guard()
        self.safepoints = self._build_safepoints()
        self.channels = self._build_channels()
        self.timebox = self._build_timebox()
        self.metrics = TokenLedger(self.store)

        # —— 输出流队列 ——
        # on_output 跑在管道那侧的 daemon 读取线程上。那个线程一旦阻塞，子进程写满
        # 缓冲区就会永久卡在 write 上，整个派发只能等超时。而这条链上偏偏全是慢动作：
        # L2 要同步调模型（秒级）、guard 要操作系统调用、SQLite 还要抢写锁。
        # 所以这里只入队，处理交给独立线程；队列满了就丢弃 —— 宁可少记几条事件，
        # 也绝不能把 Agent 卡死。
        self._queue: queue.Queue[Any] | None = queue.Queue(maxsize=_QUEUE_MAX)
        self._dropped = 0
        self._consumer: threading.Thread | None = threading.Thread(
            target=self._consume, daemon=True, name="sentinel-runtime"
        )
        self._consumer.start()

    # -- 各层构建（每层都可能缺失，缺了就是 None）---------------------------
    # 约定：这里每个 ``getattr(cfg, key, 默认)`` 的兜底值都必须与 ``config.py`` 里
    # 对应字段的默认值**逐字一致**。兜底只对「字段不存在的配置对象」生效，一旦抄错，
    # 那些对象就会绕过新默认值（曾发生过：safepoint 兜底为 True 而配置已改为 False）。
    def _build_judge(self, judge_fn: Callable[[str], str] | None) -> SemanticJudge | None:
        if not getattr(self.config, "l2_enabled", False):
            return None
        return SemanticJudge(
            judge_fn,
            min_interval=float(getattr(self.config, "l2_min_interval", 20.0) or 20.0),
            window=int(getattr(self.config, "l2_window", 10) or 10),
            max_calls_per_hour=int(getattr(self.config, "l2_max_calls_per_hour", 120) or 120),
        )

    def _build_guard(self) -> Any:
        action = str(getattr(self.config, "guard_action", "alert") or "alert").lower()
        if action == "alert":
            return None
        try:
            from .guard import ProcessGuard

            return ProcessGuard()
        except Exception as exc:  # noqa: BLE001 —— 干预层不可用就退回只告警
            logger.warning("进程干预层不可用，退回只告警：%s: %s", type(exc).__name__, exc)
            return None

    def _build_safepoints(self) -> Any:
        # 兜底值必须与 ``config.py`` 里 ``SentinelConfig.safepoint`` 的默认值一致（现在是
        # False）。getattr 的兜底只在「配置对象缺这个字段」时才生效 —— 那种情况多半来自
        # 老版本配置文件或测试用的鸭子类型配置；兜底写成 True 等于让这些对象**绕过**
        # 现在的默认关闭，去跑每条 5~15 秒的 git（还会往目标仓库写 refs/sentinel/*）。
        if not getattr(self.config, "safepoint", False):
            return None
        try:
            from .safepoint import SafePointManager

            return SafePointManager(self.store)
        except Exception as exc:  # noqa: BLE001
            logger.warning("安全点层不可用，已关闭：%s: %s", type(exc).__name__, exc)
            return None

    def _build_channels(self) -> ChannelHub:
        # 返回类型刻意不带 ``| None``：本方法**永远**造得出来（``ChannelHub([])`` 是合法
        # 对象，``dispatch_pending`` 对空列表返回全 0）。写成 Optional 会让调用方以为
        # 「没有通道」是一种需要特判的状态，从而加一个永远不成立的分支。
        # 注意 ``self.channels`` 属性仍保留 ``| None`` —— 未启用分支确实是 None。
        channels: list[Any] = [LocalChannel()]
        url = str(getattr(self.config, "webhook_url", "") or "").strip()
        if url:
            channels.append(WebhookChannel(url))
        return ChannelHub(channels)

    def _build_timebox(self) -> TimeBox | None:
        seconds = float(getattr(self.config, "timebox_seconds", 0.0) or 0.0)
        if seconds <= 0:
            return None
        return TimeBox(seconds)

    def _min_severity(self) -> str:
        """本地判定用的拦截门槛（与 ``server.py`` / ``audit.py`` 读同一个配置项）。

        不传给 ``pre_check`` 的话，``block_severity = "red_line"`` 在本地路径上等于
        没配：服务端/HTTP 后端只拦不可逆操作，进程内仍然按 ``standard`` 拦。这种
        「两边判据不一致」只会在最难受的场景暴露 —— 用户为了放行 ``rm -rf ./dist``
        显式放宽了配置，结果命令一出现在 Agent 输出里就被本地判成 block，配上
        ``guard_action = "terminate"`` 还会把任务杀掉。

        取值必须收窄到 ``{standard, red_line}``：``pre_check`` 对未知值会落回 standard，
        但 ``"none"`` 恰好是合法严重度（rank 0，等于**什么都拦**），把外部配置原样
        塞进去会造出一个比默认更严的门槛。

        :return: ``standard`` 或 ``red_line``。
        """
        value = str(getattr(self.config, "block_severity", SEVERITY_STANDARD) or "").strip().lower()
        if value not in {SEVERITY_STANDARD, SEVERITY_RED_LINE}:
            return SEVERITY_STANDARD
        return value

    # -- L1 预检 ------------------------------------------------------------
    def pre_check(self, task: str, *, fingerprint: str = "", agent: str = "") -> dict[str, Any]:
        """派发前预检，并把结果记进账本。

        :param task: 任务文本。
        :param fingerprint: 会话指纹。
        :param agent: Agent 标识。
        :return: ``pre_check`` 的判定字典；未启用时返回恒放行的字典。
        """
        if not self.enabled:
            return {"action": "allow", "degraded": True, "rule_id": "", "reason": "", "severity": "none"}

        result = pre_check(task, min_severity=self._min_severity())
        event_id = 0
        try:
            if self.store is not None:
                event_id = self.store.add_event(
                    kind=KIND_PRECHECK,
                    fingerprint=fingerprint,
                    severity=str(result.get("severity") or ""),
                    verdict=str(result.get("verdict") or ""),
                    rule_id=str(result.get("rule_id") or ""),
                    summary=(task or "")[:_SUMMARY_MAX_CHARS],
                    detail=str(result.get("reason") or ""),
                    raw=(task or "")[:_RAW_MAX_CHARS],
                )
            if result.get("action") == ACTION_BLOCK:
                self.metrics.record_blocked(agent=agent, fingerprint=fingerprint, task=task)
                self._raise_alert(
                    level=LEVEL_CRITICAL,
                    title="派发前拦截",
                    body=str(result.get("reason") or ""),
                    fingerprint=fingerprint,
                    rule_id=str(result.get("rule_id") or ""),
                    event_id=event_id,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("预检记账失败（已忽略）：%s: %s", type(exc).__name__, exc)
        return result

    def note_pre_check(
        self,
        task: str,
        *,
        blocked: bool = False,
        rule_id: str = "",
        reason: str = "",
        severity: str = "",
        evidence: str = "",
        fingerprint: str = "",
        agent: str = "",
    ) -> None:
        """记一条**已经由别处判定过**的预检结果。

        判定入口在 ``bridge/audit.py``（它同时支持内置与外部 HTTP 两种后端）。
        这里只记账与告警，不重复判定——重复判定的后果不是多花几毫秒，而是让
        「外部后端拦下了」这件事在账本里被写成「内置放行」，把账做反。

        :param task: 任务文本。
        :param blocked: 是否被判定为拦截。
        :param rule_id: 命中规则。
        :param reason: 中文原因。
        :param severity: 严重度。
        :param evidence: 证据片段。
        :param fingerprint: 会话指纹。
        :param agent: Agent 标识。
        """
        if not self.enabled:
            return
        event_id = self._record(
            KIND_PRECHECK,
            fingerprint,
            (task or "")[:_SUMMARY_MAX_CHARS],
            severity=severity,
            verdict="DANGEROUS" if blocked else "ON_TRACK",
            rule_id=rule_id,
            detail=reason or evidence,
            raw=task,
        )
        if not blocked:
            return
        try:
            self.metrics.record_blocked(agent=agent, fingerprint=fingerprint, task=task)
        except Exception as exc:  # noqa: BLE001
            logger.debug("拦截计量失败（已忽略）：%s: %s", type(exc).__name__, exc)
        self._raise_alert(
            level=LEVEL_CRITICAL if severity == "red_line" else LEVEL_WARN,
            title="派发前拦截",
            body=reason or evidence,
            fingerprint=fingerprint,
            rule_id=rule_id,
            event_id=event_id,
        )

    # -- 会话生命周期 -------------------------------------------------------
    def begin_session(self, *, cwd: str = "", pid: int = 0, agent: str = "", goal: str = "") -> str:
        """登记一路会话，返回会话身份（``fingerprint``）。

        身份**必须是这一路派发独有的**，不能由 ``cwd`` 推导出来。本方法在 ``Popen``
        之前被调用，那时 ``pid`` 必然是 0，所以 ``build_fingerprint(cwd=..., pid=0)``
        的结果实际只由 ``cwd`` 决定。而 ``allow_parallel=true`` / ``max_parallel=4``
        是默认配置：两个任务跑在同一个工作目录时，它们会拿到**同一个**身份、
        命中同一个 ``SessionContext``，随后 ``attach_pid`` 让后启动的覆盖先启动的。
        等到危险事件命中、``guard_action=terminate`` 时，被 ``taskkill /F /T`` 掉的
        是**另一个任务**的进程 —— 不可逆，且受害者完全无辜。

        所以身份改成现生成的 uuid，每次调用都是独立一路。
        ``build_fingerprint`` 仍留在 ``context.py``（别处还有引用），只是不再当身份用。

        :param cwd: 工作目录。
        :param pid: 进程 PID（通常为 0，进程拉起后再由 ``attach_pid`` 补）。
        :param agent: Agent 标识。
        :param goal: 任务目标（用于 L2 判定）。
        :return: 会话身份；未启用时为空串。
        """
        if not self.enabled:
            return ""
        fingerprint = uuid.uuid4().hex
        session = self._sessions.get_or_create(fingerprint, cwd=cwd, pid=pid, agent=agent, goal=goal)
        # 身份是新的，所以这里总是新建一路会话，字段已经带进去了；下面这几行是给
        # 「以后有人让 begin_session 复用既有会话」留的护栏：get_or_create 对已存在的
        # 会话不回写字段，goal 也只在原来为空时才补，避免后续调用把最初目标覆盖掉。
        if cwd:
            session.cwd = cwd
        if pid:
            session.pid = pid
        if agent:
            session.agent = agent
        if goal and not session.goal:
            session.goal = goal
        try:
            if self.store is not None:
                self.store.upsert_session(session)
        except Exception as exc:  # noqa: BLE001
            logger.debug("会话登记失败（已忽略）：%s: %s", type(exc).__name__, exc)
        return fingerprint

    def attach_pid(self, fingerprint: str, pid: int) -> None:
        """把 PID 补进一个已存在的会话（只回填一次）。

        PID 只有在 ``Popen`` 之后才拿得到，所以不能在 ``begin_session`` 里一次定型。
        但**绝不能无条件覆盖**：会话一旦已经有 pid，再写一次就意味着「干预目标换人」。
        例子：同 cwd 的两路会话（以前会共享身份）先后拉起，pid 互相覆盖，``terminate``
        就会打到另一路任务上 —— 不可逆。所以要留这道「已有就不写」的闸。
        """
        if not self.enabled or not fingerprint or pid <= 0:
            return
        session = self._sessions.get(fingerprint)
        if session is None:
            return
        if int(getattr(session, "pid", 0) or 0) > 0:
            return
        session.pid = int(pid)
        try:
            if self.store is not None:
                self.store.upsert_session(session)
        except Exception as exc:  # noqa: BLE001
            logger.debug("会话 PID 回填失败（已忽略）：%s: %s", type(exc).__name__, exc)

    def take_safepoint(self, cwd: str, *, fingerprint: str = "", label: str = "派发前") -> dict[str, Any] | None:
        """在派发前打一个安全点。

        :param cwd: 工作目录。
        :param fingerprint: 会话指纹。
        :param label: 标签。
        :return: 安全点信息；不可用或非 git 仓库时为 ``None``。
        """
        if not self.enabled or self.safepoints is None:
            return None
        try:
            return self.safepoints.create(cwd, label=label, fingerprint=fingerprint)
        except Exception as exc:  # noqa: BLE001
            logger.debug("安全点创建失败（已忽略）：%s: %s", type(exc).__name__, exc)
            return None

    def rollback(self, cwd: str, *, fingerprint: str = "") -> bool:
        """回滚到最近一个安全点。

        :param cwd: 工作目录。
        :param fingerprint: 会话指纹。
        :return: 是否成功。
        """
        if not self.enabled or self.safepoints is None or self.store is None:
            return False
        latest = self.store.latest_safepoint(fingerprint=fingerprint)
        if not latest:
            return False
        try:
            ok = bool(self.safepoints.restore(cwd, str(latest.get("ref") or ""), safepoint_id=latest.get("id")))
        except Exception as exc:  # noqa: BLE001
            logger.warning("回滚失败：%s: %s", type(exc).__name__, exc)
            return False
        if ok:
            self._record(KIND_SYSTEM, fingerprint, "已回滚到安全点")
        return ok

    def finish_session(self, fingerprint: str, *, exit_code: int | None = None) -> None:
        """会话收尾：记退出码、释放 pid 身份记录、投递积压告警。

        :param fingerprint: 会话指纹。
        :param exit_code: 进程退出码。
        """
        if not self.enabled:
            return
        # 先把队列里剩下的消化完，再收尾。否则任务最后一次输出里如果就藏着
        # 那条危险命令，它会因为「进程已结束」而被无声丢掉。
        self._drain()
        try:
            if self.store is not None:
                self.store.close_session(fingerprint, exit_code=exit_code)
        except Exception as exc:  # noqa: BLE001
            logger.debug("会话收尾失败（已忽略）：%s: %s", type(exc).__name__, exc)
        # 释放该 pid 的身份记录。``ProcessGuard`` 的时间窗锚在「首次见到这个 pid」，
        # 不清掉的话，长跑宿主里下一次复用同一 pid 的任务只剩半截窗口可用（越晚派发
        # 越接近过期，最后 ``terminate`` 会因「无法确认身份」被拒）；``_expiry_noted``
        # / ``_degrade_noted`` 里那个 pid 也会一直留着。
        # 收尾正是 ``forget`` 自己文档里指定的时机：此刻这一路任务确定结束了。
        session = self._sessions.get(fingerprint)
        pid = int(getattr(session, "pid", 0) or 0) if session is not None else 0
        if pid > 0 and self.guard is not None:
            try:
                self.guard.forget(pid)
            except Exception as exc:  # noqa: BLE001 —— 释放失败不能拖累收尾
                logger.debug("释放 pid 身份记录失败（已忽略）：%s: %s", type(exc).__name__, exc)
        self.flush_alerts()

    # -- 输出流 -------------------------------------------------------------
    def on_output(self, chunk: bytes | str, *, fingerprint: str = "", agent: str = "") -> None:
        """喂一段 Agent 输出。

        **必须立刻返回**：本方法跑在管道那侧的 daemon 读取线程上。那个线程只要阻塞
        一次，子进程写满缓冲区就会卡在 write 上，任务只能等超时。所以这里只做入队，
        解析、记账、L2 判定、干预全部在独立线程里完成。

        :param chunk: 原始输出（字节或文本）。
        :param fingerprint: 会话指纹。
        :param agent: Agent 标识。
        """
        if not self.enabled:
            return
        text = chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else str(chunk)
        if not text.strip():
            return
        if self._queue is None:
            # 已 close：不再处理。此时 runner 的闭包仍持有引用，还会往里喂。
            return
        try:
            self._queue.put_nowait((text, fingerprint, agent))
        except queue.Full:
            # 满了就丢，并计数。丢事件的代价是「这段时间少几条记录」，
            # 阻塞的代价是「整个任务超时失败」—— 两者不是一个量级。
            self._dropped += 1

    def _process(self, text: str, fingerprint: str, agent: str) -> None:
        """处理一批原始输出（在哨兵线程里跑）。

        :param text: 原始文本。
        :param fingerprint: 会话指纹。
        :param agent: Agent 标识。
        """
        for line in text.splitlines():
            clean = line.strip()
            if clean:
                self._on_line(clean, fingerprint=fingerprint, agent=agent)

    def _consume(self) -> None:
        """哨兵线程主循环：从队列取数据并处理，直到收到停止信号**且队列已排空**。

        为什么不能「收到停止就 return」：``close()`` 之后调用方立刻就关账本，队列里
        残留的条目会被无声丢弃。而任务最后一条输出恰恰可能是关键证据（「我要删库了」
        往往就是收尾那句），取证链断在最后一公里，``doctor()`` 还会报
        ``dropped_events: 0`` —— 等于把丢数据说成没丢。所以停止信号只表示「不再等
        新数据」，队列里已经有的必须处理完；真来不及的由 ``close()`` 记进 ``_dropped``。
        """
        # 进线程时抓一次队列引用：close() 会把 self._queue 置 None 来拒绝新入队，
        # 消费线程必须继续把手里的那个队列排空，不能因为 self._queue 变 None 就退出。
        pending = self._queue
        if pending is None:
            return
        while True:
            try:
                item = pending.get(timeout=0.1)
            except queue.Empty:
                # 队列空了：只有这时才允许在停止信号已经到达的情况下退出。
                if self._stop.is_set():
                    return
                continue
            if item is None:
                # 兼容老式结束标记（close 仍会投一个用来唤醒阻塞的 get）。
                return
            text, fingerprint, agent = item
            # 在途计数：已从队列取出、正在 ``_process`` 里的条目数。``_drain`` 必须把它
            # 并进判据 —— ``qsize() == 0`` 只说明「没有待取的」，不说明「最后一条已处理
            # 完」。漏掉在途会让**最后一条**成为唯一被丢的那条，而它恰恰最可能是关键
            # 证据（与 M4 同病根，只是场景从「close 立刻返回」换成「队列空了没处理完」）。
            # 必须用 try/finally：``_process`` 抛异常时不减，``_drain`` 会永远等不到 0，
            # 收尾就变成必然卡满 timeout。
            self._inflight += 1
            try:
                self._process(text, fingerprint, agent)
            except Exception as exc:  # noqa: BLE001 —— 单条事件失败不能杀掉消费线程
                logger.debug("事件处理失败（已忽略）：%s: %s", type(exc).__name__, exc)
            finally:
                self._inflight -= 1

    def _drain(self, *, timeout: float = 3.0, quiet: float = 0.3) -> None:
        """等队列消化完（带超时）。

        判据是**静默期**而不是「队列为空」：管道那侧的读线程是 daemon，进程退出后
        孙进程仍握着写端，数据会零星继续到达（直到 EOF 或宽限期到点被收掉）。
        按「空即齐了」来判，会把尾巴事件丢掉。

        「待消化」必须算**队列里的 + 在途的**（``_inflight``）：条目被 ``get()`` 出来
        的那一刻 ``qsize()`` 就归零了，但处理还在跑。只看队列长度，收尾会在「最后一条
        正在处理」时判定为「齐了」—— 而最后一条恰恰最常是「任务收尾那句危险命令」。

        :param timeout: 最长等多久。
        :param quiet: 连续多久没有新数据就算消化完。
        """
        if self._queue is None:
            return
        deadline = time.monotonic() + max(0.0, timeout)
        last_size = self._queue.qsize() + self._inflight
        quiet_since = time.monotonic()
        while time.monotonic() < deadline:
            # 在途数并进来之后，现有静默期逻辑不用改：在途条目处理完时 size 从 1 变 0，
            # 正好触发 quiet_since 重新计时，语义自然成立。
            size = self._queue.qsize() + self._inflight
            if size != last_size:
                last_size = size
                quiet_since = time.monotonic()
            elif size == 0 and (time.monotonic() - quiet_since) >= quiet:
                return
            time.sleep(0.02)

    def _on_line(self, line: str, *, fingerprint: str, agent: str) -> None:
        """处理单行输出。

        :param line: 一行文本。
        :param fingerprint: 会话指纹。
        :param agent: Agent 标识。
        """
        event = self._parse_line(line, agent=agent)
        kind = getattr(event, "kind", KIND_LINE) if event is not None else KIND_LINE
        summary = getattr(event, "text", "") if event is not None else line[:_SUMMARY_MAX_CHARS]
        tool = getattr(event, "tool", "") if event is not None else ""

        mapped = KIND_TOOL if kind == "tool" else (KIND_ERROR if kind == "error" else KIND_LINE)
        self._record(mapped, fingerprint, summary or line[:_SUMMARY_MAX_CHARS], raw=line, tool=tool)

        # 单行动作本身危险 → 直接按 L1 记账并告警（不等 L2）。
        # 门槛同样走 ``block_severity``：否则用户在配置里放宽到 red_line 之后，派发闸门
        # 放行了，这里的输出路径仍按 standard 拦，还会连带触发干预。
        verdict = pre_check(line, min_severity=self._min_severity())
        if verdict.get("action") == ACTION_BLOCK:
            event_id = self._record(
                KIND_PRECHECK,
                fingerprint,
                line[:_SUMMARY_MAX_CHARS],
                severity=str(verdict.get("severity") or ""),
                verdict=str(verdict.get("verdict") or ""),
                rule_id=str(verdict.get("rule_id") or ""),
                detail=str(verdict.get("reason") or ""),
                raw=line,
            )
            self._raise_alert(
                level=LEVEL_CRITICAL if verdict.get("severity") == "red_line" else LEVEL_WARN,
                title="运行中发现危险操作",
                body=str(verdict.get("reason") or ""),
                fingerprint=fingerprint,
                rule_id=str(verdict.get("rule_id") or ""),
                event_id=event_id,
            )
            self._maybe_intervene(fingerprint, verdict)
            return

        self._maybe_judge(fingerprint=fingerprint, agent=agent, action=summary or line)

    def _parse_line(self, line: str, *, agent: str) -> Any:
        """用适配器把一行归一化成事件。

        失败必须**可观测**，而且不能只靠日志：适配器坏掉意味着**每一行**都退化成纯
        文本，账本里只表现为「结构化字段全空」，事后无法区分「适配器没生效」和「输出
        本来就没有结构化字段」。而 ``logger.debug`` 在插件路径上等于没有日志（没人给
        ``sentinel_core`` 设级别），所以这里同时累加 ``self._parse_failures``，
        由 ``doctor()`` 暴露 —— 与 ``dropped_events`` 同型，读者才不会把
        「没看到异常」当成「一切正常」。

        :param line: 一行文本。
        :param agent: Agent 标识。
        :return: ``LineEvent``；适配器不可用时为 ``None``。
        """
        try:
            from .adapters import get_adapter

            return get_adapter(agent).parse(line)
        except Exception as exc:  # noqa: BLE001 —— 适配器缺失或解析失败都退化
            self._parse_failures += 1
            logger.debug("解析输出行失败（已降级为纯文本）：%s: %s", type(exc).__name__, exc)
            return None

    def _maybe_judge(self, *, fingerprint: str, agent: str, action: str) -> None:
        """在节流允许时做一次 L2 判定。

        :param fingerprint: 会话指纹。
        :param agent: Agent 标识。
        :param action: 本次动作摘要。
        """
        if self.judge is None:
            return
        session = self._sessions.get(fingerprint) if fingerprint else None
        goal = getattr(session, "goal", "") if session is not None else ""
        actions = self._recent_actions(fingerprint, limit=int(getattr(self.config, "l2_window", 10) or 10))
        if action and (not actions or actions[-1] != action):
            actions = [*actions, action]

        result = self.judge.judge(goal=goal, actions=actions, context=getattr(session, "cwd", "") or "")
        if not result.drift:
            return
        if result.skipped:
            return  # 同一条漂移已经报过，不重复打扰
        event_id = self._record(
            "verdict",
            fingerprint,
            result.verdict.reason,
            severity=result.verdict.severity,
            verdict=result.verdict.verdict,
            rule_id=result.verdict.rule_id,
            detail=result.verdict.evidence,
        )
        level = LEVEL_CRITICAL if result.verdict.severity == "red_line" else LEVEL_WARN
        self._raise_alert(
            level=level,
            title="意图漂移",
            body=result.verdict.reason,
            fingerprint=fingerprint,
            rule_id=result.verdict.rule_id,
            event_id=event_id,
        )
        self._maybe_intervene(fingerprint, result.verdict.to_dict() if hasattr(result.verdict, "to_dict") else {})

    def _recent_actions(self, fingerprint: str, *, limit: int) -> list[str]:
        """取最近的工具/命令类动作，供 L2 判定使用。

        :param fingerprint: 会话指纹。
        :param limit: 最多几条。
        :return: 动作摘要列表（时间正序）。
        """
        if self.store is None:
            return []
        rows = self.store.timeline(fingerprint=fingerprint, kinds=[KIND_TOOL, KIND_LINE], limit=limit)
        return [str(row.get("summary") or "") for row in rows if str(row.get("summary") or "").strip()]

    # -- 干预 ---------------------------------------------------------------
    def _maybe_intervene(self, fingerprint: str, verdict: dict[str, Any]) -> None:
        """按配置的干预级别动手，并始终生成一份纠偏建议。

        顺序很重要：**先生成纠偏提示，再考虑动手**。因为「该怎么做」这件事不该
        依赖「能不能杀进程」——很多环境下干预能力不可用（容器、权限不足），
        但把告警变成一条能粘回 Agent 的指令，是纯文本工作，永远做得到。

        **同一路会话 30 秒内只动手一次**（``_INTERVENE_MIN_INTERVAL``）。一条
        ``rm -rf`` 在 Agent 输出里会反复出现（回显、工具参数、重试），每命中一次就
        起一根线程 + 一次 ``guard.terminate`` 的话，40 行危险行 = 40 次
        ``taskkill /F /T`` 打同一个 pid（单次上限 10 秒），干预线程池被打满，
        而「杀一个进程」本身是幂等的 —— 重复动作只增加风险、不增加保护。
        重复命中记一条 ``KIND_SYSTEM`` 说明「已触发、跳过」，**不是失败**：
        记成 ``KIND_ERROR`` 会让时间线看起来像干预失败了。

        :param fingerprint: 会话指纹。
        :param verdict: 判定字典。
        """
        session = self._sessions.get(fingerprint) if fingerprint else None
        goal = getattr(session, "goal", "") if session is not None else ""
        try:
            from .inject import build_correction_prompt

            prompt = build_correction_prompt(verdict, goal=goal)
            if prompt:
                self._record(KIND_SYSTEM, fingerprint, "已生成纠偏建议", detail=prompt)
        except Exception as exc:  # noqa: BLE001 —— 生成不出建议不影响干预
            logger.debug("纠偏建议生成失败（已忽略）：%s: %s", type(exc).__name__, exc)

        # 策略判定**必须**在 ``guard`` / ``pid`` 判空之前：``config.py`` 对老配置明确
        # 承诺「运行时会记一条说明」，而 guard 没建起来（alert 档、或初始化失败）与 pid
        # 尚未回填都是常态 —— 把那两行 return 放在前面，这条承诺在最需要它的场景里
        # 恰好不会兑现，用户只会看到「配了 suspend，什么也没发生」。
        action = str(getattr(self.config, "guard_action", "alert") or "alert").lower()
        if action not in _GUARD_ACTIONS:
            # ``suspend`` 是**曾经支持过、现已移除**的策略。老配置文件里仍可能写着它，
            # 所以必须容忍：既不能让一个陈旧取值把派发路径炸掉，也**不能**悄悄当成
            # ``terminate`` —— 那等于把「只提醒」的配置升级成「杀进程」，方向不可逆。
            # 按 alert 处理（上面那条纠偏建议就是 alert 的全部产出）并记一条说明，
            # 让人知道配置里有这个值、以及它为什么不生效。
            hint = "已移除" if action == "suspend" else "未知取值"
            self._record(KIND_SYSTEM, fingerprint, f"guard_action 配置为 {action}（{hint}，按 alert 处理）")
            return
        if action == "alert":
            return
        if self.guard is None:
            # 值合法、但干预层没起来（``_build_guard`` 初始化失败或平台不支持）。这是
            # **另一件事**，绝不能和上面「值非法」合并成一句话：两句话指向完全相反的
            # 排查方向 —— 一个要改配置，一个要去查 guard 为什么没建成。
            self._record(
                KIND_SYSTEM,
                fingerprint,
                f"guard_action 配置为 {action}，但干预层未启用（按 alert 处理）",
            )
            return
        pid = int(getattr(session, "pid", 0) or 0)
        if pid <= 0:
            return

        if not self._claim_intervention(fingerprint):
            self._record(KIND_SYSTEM, fingerprint, "干预已触发，跳过重复动作")
            return

        def _act() -> None:
            """在独立线程里动手。

            ``terminate`` 是阻塞的：POSIX 要等 grace 秒再 SIGKILL，Windows 要等
            taskkill（超时上限 10 秒）。如果就在消费线程里调，这段时间事件会积压、
            时间线出现空洞。所以干预单独开一条线程，代价是「动手的一瞬间」和
            「记账的一瞬间」不同步 —— 对审计来说这无关紧要。
            """
            try:
                ok = self.guard.terminate(pid)
                self._record(KIND_SYSTEM, fingerprint, f"已终止进程 {pid}" if ok else f"终止 {pid} 失败")
            except Exception as exc:  # noqa: BLE001 —— 干预失败只能降级成告警
                logger.warning("干预动作失败（已降级为告警）：%s: %s", type(exc).__name__, exc)
                self._record(KIND_ERROR, fingerprint, f"干预失败：{type(exc).__name__}")

        threading.Thread(target=_act, daemon=True, name="sentinel-intervene").start()

    def _claim_intervention(self, fingerprint: str) -> bool:
        """登记「这一路会话本窗口内要动手」；已经动过手则返回 ``False``。

        判定与登记必须在同一把锁里：``_maybe_intervene`` 会被消费线程反复调用，
        「先查后写」分开做的话，两次命中可能同时通过检查，等于没有幂等。

        :param fingerprint: 会话指纹。
        :return: 是否取得了本窗口的动手资格。
        """
        key = str(fingerprint or "")
        now = time.monotonic()
        with self._intervene_lock:
            last = self._intervened_at.get(key)
            if last is not None and (now - last) < _INTERVENE_MIN_INTERVAL:
                return False
            self._intervened_at[key] = now
            return True

    # -- 告警 ---------------------------------------------------------------
    def _raise_alert(
        self,
        *,
        level: str,
        title: str,
        body: str = "",
        fingerprint: str = "",
        rule_id: str = "",
        event_id: int = 0,
    ) -> None:
        """记一条告警。

        ``event_id`` 是**必须传**的：``briefing.py`` 的 ``_pick_highlights`` 拿它去重
        （同一条事件已经作为高亮列出来了，就别再把伴随它的告警列一遍）。以前这里不传，
        ``alerts.event_id`` 永远是 NULL，去重分支永不执行 —— 同一次拦截在晨报里出现
        两行，读者会以为拦了两次。没有关联事件的告警保持 0。

        :param level: 级别。
        :param title: 标题。
        :param body: 正文。
        :param fingerprint: 会话指纹。
        :param rule_id: 命中规则。
        :param event_id: 关联事件 ID（``_record``/``add_event`` 的返回值）；无为 0。
        """
        if self.store is None:
            return
        try:
            self.store.add_alert(
                level=level,
                title=title,
                body=body,
                fingerprint=fingerprint,
                event_id=int(event_id or 0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("告警记账失败（已忽略）：%s: %s", type(exc).__name__, exc)
        if rule_id:
            logger.debug("告警命中规则：%s", rule_id)

    def flush_alerts(self) -> dict[str, int]:
        """把积压告警推给各通道。

        :return: ``{"sent": n, "failed": n, "skipped": n}``；未启用时全 0。
        """
        if not self.enabled or self.channels is None or self.store is None:
            return {"sent": 0, "failed": 0, "skipped": 0}
        try:
            return self.channels.dispatch_pending(self.store)
        except Exception as exc:  # noqa: BLE001
            logger.debug("告警投递失败（已忽略）：%s: %s", type(exc).__name__, exc)
            return {"sent": 0, "failed": 1, "skipped": 0}

    # -- 查询 ---------------------------------------------------------------
    def _record(
        self,
        kind: str,
        fingerprint: str,
        summary: str,
        *,
        severity: str = "",
        verdict: str = "",
        rule_id: str = "",
        detail: str = "",
        raw: str = "",
        tool: str = "",
    ) -> int:
        """写一条事件（所有写入都走这里，保证字段一致）。

        :param kind: 事件种类。
        :param fingerprint: 会话指纹。
        :param summary: 摘要。
        :param severity: 严重度。
        :param verdict: 判定。
        :param rule_id: 规则。
        :param detail: 细节。
        :param raw: 原文。
        :param tool: 工具名（会拼进细节）。
        :return: 事件 ID（告警靠它关联事件，见 ``_raise_alert``）；未写入为 0。
        """
        if self.store is None:
            return 0
        text = str(summary or "")[:_SUMMARY_MAX_CHARS]
        if tool:
            text = f"[{tool}] {text}"
        try:
            return int(
                self.store.add_event(
                    kind=kind,
                    fingerprint=fingerprint,
                    severity=severity,
                    verdict=verdict,
                    rule_id=rule_id,
                    summary=text,
                    detail=detail,
                    raw=str(raw or "")[:_RAW_MAX_CHARS],
                )
                or 0
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("事件写入失败（已忽略）：%s: %s", type(exc).__name__, exc)
            return 0

    def timeline(self, *, since: float | None = None, fingerprint: str = "", limit: int = 120) -> str:
        """渲染时间线。

        :param since: 起始时间戳。
        :param fingerprint: 只看某一路会话。
        :param limit: 最多取几条。
        :return: 文本时间线。
        """
        if not self.enabled or self.store is None:
            return "（安全审计未启用）"
        items = build_timeline(self.store, since=since, fingerprint=fingerprint, limit=limit)
        return render_timeline(items)

    def briefing(self, *, hours: float = 12.0) -> str:
        """渲染晨报。

        :param hours: 往前看多少小时。
        :return: 文本晨报。
        """
        if not self.enabled or self.store is None:
            return "（安全审计未启用）"
        try:
            from .briefing import build_briefing, render_briefing

            since = time.time() - max(1.0, float(hours)) * 3600
            return render_briefing(build_briefing(self.store, since=since))
        except Exception as exc:  # noqa: BLE001
            logger.debug("晨报生成失败：%s: %s", type(exc).__name__, exc)
            return "（晨报生成失败）"

    def usage(self, *, hours: float = 24.0) -> str:
        """渲染 Token 用量。

        :param hours: 往前看多少小时。
        :return: 一行文本。
        """
        if not self.enabled:
            return "（安全审计未启用）"
        since = time.time() - max(1.0, float(hours)) * 3600
        return render_usage(self.metrics.summary(since=since))

    def timebox_start(self, *, label: str = "") -> float:
        """开一个时间盒。

        :param label: 标签。
        :return: 起始时间戳；未启用时为 0。
        """
        if self.timebox is None:
            return 0.0
        return self.timebox.start(label=label)

    def timebox_check(self, started_at: float) -> dict[str, Any] | None:
        """检查时间盒。

        :param started_at: ``timebox_start`` 的返回值。
        :return: 命中信息；未启用或未到阈值时 ``None``。
        """
        if self.timebox is None or not started_at:
            return None
        try:
            return self.timebox.check(started_at)
        except Exception as exc:  # noqa: BLE001
            logger.debug("时间盒检查失败：%s: %s", type(exc).__name__, exc)
            return None

    def doctor(self) -> dict[str, Any]:
        """诊断快照，供 ``agent_doctor`` 与面板显示。

        :return: 各层可用性。
        """
        info: dict[str, Any] = {
            "enabled": self.enabled,
            # ``ok`` 不能写成 ``bool(self.store)``：``EventStore`` 没有 ``__bool__``，
            # 那等于恒为 True。而账本建表失败时 ``EventStore`` 只是把 ``self.enabled``
            # 置 False，之后所有写入静默 ``return 0``。用 ``bool(self.store)`` 的话，
            # doctor 会报「账本 OK」、启动日志会打「账本 开」，而实际上一个字节都没落盘
            # —— 把「没在记」说成「记着」，是审计工具最不该犯的错。
            "store": {
                "path": str(getattr(self.store, "path", "")) if self.store else "",
                "ok": bool(self.store and self.store.enabled),
            },
            "l2": {"enabled": self.judge is not None},
            "guard": {"enabled": self.guard is not None},
            "safepoint": {"enabled": self.safepoints is not None},
            "webhook": {"configured": bool(getattr(self.config, "webhook_url", ""))},
            "timebox": {"seconds": float(getattr(self.config, "timebox_seconds", 0.0) or 0.0)},
            "dropped_events": self._dropped,
            # 适配器解析失败计数。和 ``dropped_events`` 同型：都是「降级发生过」的唯一
            # 证据 —— 日志在插件路径上看不见（见 ``_parse_line``），所以必须有这个数。
            "parse": {"failures": self._parse_failures},
        }
        if self.judge is not None:
            info["l2"].update(self.judge.capabilities())
        if self.guard is not None:
            try:
                info["guard"].update(self.guard.capabilities())
            except Exception:  # noqa: BLE001
                pass
        if self.guard is not None:
            try:
                # 单独一棵子树，不并进上面的 ``capabilities()``：那边是「布尔能力位」
                # 的类型契约，塞进 str/float 会让按位取用的上层代码出意外。
                # 这里回答的是另一个问题 —— 本机处在哪一档、窗口多少秒。它决定了
                # ``terminate`` 会不会因为「无法确认身份」被拒，是排障第一个要看的东西。
                info["guard"]["policy"] = self.guard.identity_policy()
            except Exception:  # noqa: BLE001
                pass
        if self.store is not None:
            try:
                stats = self.store.stats(since=self._started_at)
                info["store"].update({key: stats.get(key) for key in ("sessions", "events", "alerts")})
            except Exception:  # noqa: BLE001
                pass
        return info

    def close(self, *, timeout: float = 2.0) -> None:
        """释放资源（先停消费线程并排空队列，再关账本连接）。

        顺序不能反：账本一关，消费线程之后再处理事件就是静默丢弃（``_record`` 里
        只剩一条 debug 日志），最后一条关键输出会消失得无影无踪。所以这里先给停止
        信号、再等排空、最后才关库；等不到的那部分必须计进 ``_dropped``，
        ``doctor()`` 才不会把「丢了 N 条」报成 0。

        :param timeout: 等消费线程排空队列的最长秒数。
        """
        queue_ref = self._queue
        consumer = self._consumer
        if queue_ref is not None:
            # 先置 None 拒绝新的入队（runner 的闭包仍持有 runtime，进程退出后还会喂），
            # 再给停止信号。消费线程进线程时已抓住队列引用，所以它不会因为这一步
            # 提前退出 —— 它会先把队列排空，见 _consume。
            self._queue = None
            self._stop.set()
            try:
                # 投一个 None 只是把阻塞中的 get 唤醒，让排空不必等满一个 timeout 周期。
                queue_ref.put_nowait(None)
            except queue.Full:
                pass
            if consumer is not None:
                consumer.join(timeout=max(0.0, float(timeout)))
            if consumer is not None and consumer.is_alive():
                # 排空窗口用完了：消费线程可能正卡在一次阻塞干预（taskkill 最长 10 秒）
                # 里，剩下这些条目不会再被处理，如实计数。
                while True:
                    try:
                        queue_ref.get_nowait()
                    except queue.Empty:
                        break
                    self._dropped += 1
            self._consumer = None
        if self.store is not None:
            try:
                self.store.close()
            except Exception:  # noqa: BLE001
                pass
