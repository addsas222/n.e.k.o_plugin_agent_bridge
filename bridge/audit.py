"""Sentinel 安全审计客户端（对接哨兵 v5 无头守护进程的本地 HTTP 接口）。

本模块是 agent_bridge 与哨兵（Sentinel，GPL-3.0）之间**唯一的**耦合面：只通过本机
``127.0.0.1`` 的 HTTP API 通信，**绝不 import 哨兵的任何源码**。

为什么是 HTTP 而不是 import
==========================
哨兵以 GPL-3.0 发布，而本插件随 N.E.K.O 项目采用 MIT。把 GPL 源码并入本仓库会触发
copyleft，迫使本仓库也变为 GPL。因此本仓库**只保留一个薄 HTTP 客户端**，哨兵以独立进程
（git submodule：``vendor/sentinel``）形式存在，运行时通过 ``/api/*`` 对接。
详见仓库根 ``NOTICE``。

接口契约见哨兵 ``src/daemon/server.py``（v5.0.0 起）：

* ``GET  /api/health``     → ``{"status","version","mode":"headless","sessions","l2_sleeping","callback"}``
* ``POST /api/pre-check``  ``{"text": "..."}`` →
  ``{"action":"allow"|"block","verdict","rule_id","reason","evidence","degraded"}``
* ``POST /api/attach``    ``{"cwd","pid","log_path","agent","session_id"}`` → 会话快照
* ``GET  /api/state``     → ``{"sessions":[...]}``

设计要点
========
* 哨兵未启动 / 接口超时 / 返回异常时一律** fail-open（放行）**——安全审计是可选增强层，
  绝不能因为哨兵没跑就把所有派发卡死。
* 预检只上 L1 纯规则、秒回、不调模型（哨兵那一侧的实现保证）；本侧再叠加一个短超时。
* 所有网络调用走标准库 ``urllib``，**零第三方依赖**，与插件整体「纯标准库」的约束一致。
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


def build_client(sentinel) -> Optional[SentinelClient]:
    """由 ``BridgeConfig.sentinel`` 构造客户端；未启用或没配地址则返回 ``None``。"""
    if sentinel is None or not getattr(sentinel, "enabled", False):
        return None
    url = str(getattr(sentinel, "url", "") or "").strip()
    if not url:
        return None
    if not is_loopback_url(url):
        # 配置能把任务正文发到任意主机 —— 而哨兵自己的安全边界是「只绑 127.0.0.1」。
        # 这里拒绝非回环地址，让文档里的承诺真正成立，而不是只写在注释里。
        logger.warning("哨兵地址不是本机回环，已拒绝：%s", url)
        return None
    return SentinelClient(url, timeout=float(getattr(sentinel, "timeout", 2.0) or 2.0))


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
    """取一份适合回显给用户/模型的哨兵状态快照。

    :param sentinel: ``BridgeConfig.sentinel``。
    :return: ``enabled`` / ``url`` / ``reachable`` / ``version`` / ``sessions`` /
             ``l2_sleeping``。未启用或地址不合法时 ``reachable`` 为 ``False``。
    """
    enabled = bool(getattr(sentinel, "enabled", False))
    url = str(getattr(sentinel, "url", "") or "").strip()
    info: dict[str, Any] = {
        "enabled": enabled,
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
