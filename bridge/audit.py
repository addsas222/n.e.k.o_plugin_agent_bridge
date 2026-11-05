"""安全审计闸门 —— 派发前的 L1 预检与派发后的会话登记。

两种后端
========

``builtin``（默认）
    插件自带的精简哨兵（:mod:`bridge.sentinel_core`，MIT），进程内直接判定。
    不起服务、不走网络、零第三方依赖，装好即用。

``http``
    外部完整版 Sentinel 无头守护进程，通过本机 ``127.0.0.1`` 的 ``/api/*`` 对接。
    适合已经在跑桌面版的用户。**不 import 对方任何源码**，只走公开 HTTP 契约。

两者接口完全一致（``health`` / ``pre_check`` / ``attach``），所以派发侧不需要
知道自己接的是哪一个。

设计要点
========

* 未启用 / 后端不可达 / 判定异常时一律 **fail-open（放行）**——安全审计是可选
  增强层，绝不能因为审计没跑就把所有派发卡死。
* 预检只上 L1 纯规则、秒回、不调模型；HTTP 侧再叠加一个短超时。
* 零第三方依赖。

对外契约（两种后端一致）：

* ``health()``             → :class:`HealthResult`
* ``pre_check(text)``      → :class:`PreCheckResult`；不可达时 ``None``
* ``attach(**kwargs)``     → 会话快照字典；失败时 ``None``
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

ACTION_ALLOW = "allow"
ACTION_BLOCK = "block"

#: 后端标识。
BACKEND_BUILTIN = "builtin"
BACKEND_HTTP = "http"


@dataclass
class PreCheckResult:
    """哨兵 L1 预检的判定结果。"""

    allow: bool
    action: str
    rule_id: str
    reason: str
    evidence: str
    degraded: bool
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PreCheckResult":
        action = str(data.get("action") or ACTION_ALLOW)
        return cls(
            allow=action != ACTION_BLOCK,
            action=action,
            rule_id=str(data.get("rule_id") or ""),
            reason=str(data.get("reason") or ""),
            evidence=str(data.get("evidence") or ""),
            degraded=bool(data.get("degraded", False)),
            raw=data,
        )

    @classmethod
    def allow_degraded(cls, note: str) -> "PreCheckResult":
        """哨兵判定层自身降级（规则引擎没起来）时，按哨兵的语义放行。"""
        return cls(True, ACTION_ALLOW, "", note, "", True, {})


@dataclass
class HealthResult:
    """哨兵守护进程的健康快照。"""

    ok: bool
    version: str = ""
    sessions: int = 0
    l2_sleeping: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class SentinelClient:
    """哨兵 v5 无头守护进程的本地 HTTP 客户端（只绑 127.0.0.1）。"""

    def __init__(self, base_url: str, *, timeout: float = 2.0) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = float(timeout or 2.0)

    # -- 低层请求 ----------------------------------------------------------
    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> Optional[dict]:
        """发一次请求；任何失败都返回 ``None``（调用方据此 fail-open）。"""
        if not self.base_url:
            return None
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        data: Optional[bytes] = None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.warning("哨兵接口调用失败 %s %s：%s: %s", method, path, type(exc).__name__, exc)
            return None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError as exc:
            logger.warning("哨兵返回非法 JSON %s：%s", path, exc)
            return None

    # -- 端点 --------------------------------------------------------------
    def health(self) -> HealthResult:
        data = self._request("GET", "api/health")
        if data is None:
            return HealthResult(False)
        return HealthResult(
            ok=str(data.get("status")) == "running",
            version=str(data.get("version") or ""),
            sessions=int(data.get("sessions") or 0),
            l2_sleeping=int(data.get("l2_sleeping") or 0),
            raw=data or {},
        )

    def pre_check(self, text: str) -> Optional[PreCheckResult]:
        data = self._request("POST", "api/pre-check", {"text": str(text or "")})
        if data is None:
            return None
        return PreCheckResult.from_dict(data if isinstance(data, dict) else {})

    def attach(
        self,
        *,
        cwd: str = "",
        pid: int = 0,
        agent: str = "",
        log_path: str = "",
        session_id: str = "",
    ) -> Optional[dict]:
        payload = {
            "cwd": str(cwd or ""),
            "pid": int(pid or 0),
            "agent": str(agent or ""),
            "log_path": str(log_path or ""),
            "session_id": str(session_id or ""),
        }
        return self._request("POST", "api/attach", payload)


class BuiltinBackend:
    """进程内后端 —— 直接调 :mod:`bridge.sentinel_core`，不起服务、不走网络。

    这是默认后端：安全判定在同一个进程里完成，没有端口、没有可达性问题，
    也就不存在「守护进程没起 → 悄悄失效」这种失败模式。

    :param min_severity: 达到哪个严重度才真的拦（``standard`` / ``red_line``）。
    :param config: 规则配置（``dangerous_commands`` / ``disabled_rules``）。
    """

    name = BACKEND_BUILTIN

    def __init__(
        self, *, min_severity: str = "standard", config: dict[str, Any] | None = None
    ) -> None:
        from .sentinel_core import build_app

        self.min_severity = str(min_severity or "standard")
        # 门槛走配置传进应用，否则 app.pre_check 会退回它自己的默认值
        merged = dict(config or {})
        merged["block_severity"] = self.min_severity
        self._app = build_app(merged)

    def health(self) -> HealthResult:
        """健康检查。内置后端永远可用，除非模块导入就失败。"""
        try:
            data = self._app.health()
        except Exception as exc:  # noqa: BLE001
            return HealthResult(False, raw={"error": f"{type(exc).__name__}: {exc}"})
        return HealthResult(
            ok=str(data.get("status")) == "running",
            version=str(data.get("version") or ""),
            sessions=int(data.get("sessions") or 0),
            l2_sleeping=int(data.get("l2_sleeping") or 0),
            raw=data,
        )

    def pre_check(self, text: str) -> Optional[PreCheckResult]:
        """L1 预检。内置后端失败时也返回结果（降级放行），不走 ``None``。"""
        try:
            data = self._app.pre_check(str(text or ""))
        except Exception as exc:  # noqa: BLE001 —— 任何异常都放行
            return PreCheckResult.allow_degraded(f"内置哨兵判定异常：{type(exc).__name__}")
        return PreCheckResult.from_dict(data if isinstance(data, dict) else {})

    def attach(
        self,
        *,
        cwd: str = "",
        pid: int = 0,
        agent: str = "",
        log_path: str = "",
        session_id: str = "",
    ) -> Optional[dict]:
        """登记一路会话。"""
        try:
            return self._app.attach(
                cwd=str(cwd or ""),
                pid=int(pid or 0),
                agent=str(agent or ""),
                log_path=str(log_path or ""),
                session_id=str(session_id or ""),
            )
        except Exception as exc:  # noqa: BLE001 —— 登记失败不影响派发
            logger.debug("内置哨兵登记会话失败（已忽略）：%s: %s", type(exc).__name__, exc)
            return None


def build_client(sentinel) -> Optional[SentinelClient | BuiltinBackend]:
    """由 ``BridgeConfig.sentinel`` 构造后端；未启用则返回 ``None``。

    后端选择规则（``backend`` 留空时）：
    配了 ``url`` → ``http``（尊重既有配置）；没配 → ``builtin``。

    :param sentinel: ``BridgeConfig.sentinel``。
    :return: 后端实例；未启用或配置不可用时 ``None``。
    """
    if sentinel is None or not getattr(sentinel, "enabled", False):
        return None

    backend = resolve_backend(sentinel)
    if backend == BACKEND_BUILTIN:
        return BuiltinBackend(
            min_severity=str(getattr(sentinel, "block_severity", "standard") or "standard"),
        )

    url = str(getattr(sentinel, "url", "") or "").strip()
    if not url:
        logger.warning("哨兵后端设为 http 但没配 url，本次不启用审计")
        return None
    if not is_loopback_url(url):
        # 配置能把任务正文发到任意主机 —— 而审计服务的边界是「只在本机」。
        logger.warning("哨兵地址不是本机回环，已拒绝：%s", url)
        return None
    return SentinelClient(url, timeout=float(getattr(sentinel, "timeout", 2.0) or 2.0))


def resolve_backend(sentinel) -> str:
    """推断该用哪个后端。

    :param sentinel: ``BridgeConfig.sentinel``。
    :return: ``builtin`` 或 ``http``。
    """
    explicit = str(getattr(sentinel, "backend", "") or "").strip().lower()
    if explicit in {BACKEND_BUILTIN, BACKEND_HTTP}:
        return explicit
    if explicit:
        logger.warning("未知的哨兵后端 %r，改用自动推断", explicit)
    # 配了地址说明用户手里有一个外部守护进程，尊重它
    return BACKEND_HTTP if str(getattr(sentinel, "url", "") or "").strip() else BACKEND_BUILTIN


def is_loopback_url(url: str) -> bool:
    """判断地址是否是本机回环（http/https + 127.0.0.0/8、localhost、::1）。"""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    return host == "localhost" or host == "::1" or host.startswith("127.")


def status_snapshot(sentinel) -> dict[str, Any]:
    """取一份适合回显给用户/模型的审计状态快照。

    :param sentinel: ``BridgeConfig.sentinel``。
    :return: ``enabled`` / ``backend`` / ``url`` / ``reachable`` / ``version`` /
             ``sessions`` / ``l2_sleeping``。未启用时 ``reachable`` 为 ``False``。
    """
    enabled = bool(getattr(sentinel, "enabled", False))
    url = str(getattr(sentinel, "url", "") or "").strip()
    info: dict[str, Any] = {
        "enabled": enabled,
        "backend": resolve_backend(sentinel) if enabled else "",
        "url": url,
        "reachable": False,
        "version": "",
        "sessions": 0,
        "l2_sleeping": 0,
    }
    client = build_client(sentinel)
    if client is None:
        return info
    health = client.health()
    info.update(
        reachable=health.ok,
        version=health.version,
        sessions=health.sessions,
        l2_sleeping=health.l2_sleeping,
    )
    return info


def gated_pre_check(sentinel, task: str) -> Optional[PreCheckResult]:
    """派发前的 L1 安全闸门。

    :param sentinel: ``BridgeConfig.sentinel``。
    :param task: 任务文本（用户交代的那句话）。
    :return: 被拦下且需要强制执行时返回 :class:`PreCheckResult`；其余情况返回 ``None``
             （放行，含「哨兵未启用 / 未启动 / 降级 / 仅告警不拦截」）。
    """
    client = build_client(sentinel)
    if client is None:
        return None
    try:
        result = client.pre_check(task)
    except Exception as exc:  # noqa: BLE001 —— 任何异常都放行，不卡派发
        logger.warning("哨兵预检异常（已放行）：%s: %s", type(exc).__name__, exc)
        return None
    if result is None:
        # 守护进程不可达 → fail-open，避免哨兵没跑就把所有派发卡死。
        return None
    block_on_deny = bool(getattr(sentinel, "block_on_deny", True))
    if not result.allow and block_on_deny:
        return result
    return None
