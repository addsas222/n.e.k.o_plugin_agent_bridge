"""内置哨兵的 HTTP 服务端 —— 四个端点，只用标准库。

端点契约
========

======================  ==========================================
``GET  /api/health``    ``{"status","version","mode","sessions","l2_sleeping","callback"}``
``GET  /api/state``     ``{"sessions":[...]}``
``POST /api/pre-check`` ``{"text"}`` → ``{"action","verdict","rule_id","reason","evidence","degraded"}``
``POST /api/attach``    ``{"cwd","pid","log_path","agent","session_id"}`` → 会话快照
======================  ==========================================

安全边界
========

只绑回环地址。这不是可配置项 —— 一个能回答「这条命令该不该拦」的服务，
暴露到局域网等于把安全策略的探测面送出去。
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .context import SessionRegistry, build_fingerprint
from .precheck import pre_check
from .rules import SEVERITY_STANDARD, RuleEngine
from .worker import SessionWorker

__all__ = ["DaemonApp", "LocalHTTPServer", "build_app", "serve", "VERSION"]

logger = logging.getLogger(__name__)

#: 服务版本。
VERSION = "1.0.0"

#: 请求体上限 —— 一个坏客户端不该能把内存吃光。
MAX_BODY_BYTES = 256 * 1024

#: 允许绑定的回环主机名。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class DaemonApp:
    """应用状态：会话表 + 规则引擎 + 监控线程。

    :param config: 规则配置（``dangerous_commands`` / ``disabled_rules``）。
    :param version: 版本号，健康检查里回给调用方。
    """

    def __init__(self, *, config: dict[str, Any] | None = None, version: str = "") -> None:
        self.config = config if isinstance(config, dict) else {}
        self.version = str(version or VERSION)
        self.registry = SessionRegistry()
        self.rules = RuleEngine(self.config)
        self._workers: dict[str, SessionWorker] = {}
        self._lock = threading.Lock()

    # -- 查询 ------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        """健康检查。

        :return: 健康字典。
        """
        sessions = self.registry.snapshot()
        return {
            "status": "running",
            "version": self.version,
            "mode": "headless",
            "sessions": len(sessions),
            "l2_sleeping": sum(1 for item in sessions if item.get("l2_sleeping")),
            "callback": False,
        }

    def state(self) -> dict[str, Any]:
        """全部会话快照。

        :return: 状态字典。
        """
        return {"sessions": self.registry.snapshot()}

    def pre_check(self, text: str) -> dict[str, Any]:
        """L1 预检。

        :param text: 任务文本。
        :return: 判定字典。
        """
        return pre_check(
            text,
            rules=self.rules,
            min_severity=str(self.config.get("block_severity") or SEVERITY_STANDARD),
        )

    # -- 挂载 ------------------------------------------------------------
    def attach(
        self,
        *,
        cwd: str = "",
        pid: int = 0,
        log_path: str = "",
        agent: str = "",
        session_id: str = "",
        goal: str = "",
    ) -> dict[str, Any]:
        """登记一路会话，并在给了 ``log_path`` 时启动后台监控。

        :param cwd: 工作目录。
        :param pid: 进程 ID。
        :param log_path: 会话日志路径；空串表示只登记不监控。
        :param agent: Agent 类型。
        :param session_id: Agent 会话 ID。
        :param goal: 任务目标（可选）。
        :return: 会话快照。
        """
        fingerprint = build_fingerprint(cwd=cwd, session_id=session_id, pid=pid)
        if not fingerprint:
            return {"error": "cwd / session_id / pid 至少要给一个"}

        context = self.registry.get_or_create(
            fingerprint,
            session_id=str(session_id or ""),
            cwd=str(cwd or ""),
            pid=int(pid or 0),
            agent=str(agent or ""),
            log_path=str(log_path or ""),
        )
        context.cwd = str(cwd or context.cwd)
        context.pid = int(pid or context.pid)
        context.agent = str(agent or context.agent)
        context.log_path = str(log_path or context.log_path)
        if goal:
            context.goal = str(goal)
        context.touch()

        with self._lock:
            worker = self._workers.get(fingerprint)
            if worker is None and context.log_path:
                worker = SessionWorker(context, RuleEngine(self.config))
                self._workers[fingerprint] = worker
                worker.start()

        return context.to_dict()

    def shutdown(self) -> None:
        """停掉全部监控线程。"""
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.stop()


class _Handler(BaseHTTPRequestHandler):
    """请求处理器。"""

    server_version = f"SentinelCore/{VERSION}"

    @property
    def app(self) -> DaemonApp:
        """当前 server 挂的应用实例。

        从 ``self.server`` 取而不是用类属性 —— 否则同进程起多个实例
        （测试里很常见）会互相覆盖。
        """
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """访问日志降到 DEBUG —— 终端要留给告警。"""
        logger.debug("%s - %s", self.address_string(), format % args)

    # -- 路由 ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的约定
        """处理 GET。"""
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/health":
            self._json(self.app.health())
            return
        if path == "/api/state":
            self._json(self.app.state())
            return
        self._json({"error": "not found", "path": path}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        """处理 POST。"""
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        payload = self._read_json()
        if payload is None:
            return
        if path == "/api/pre-check":
            text = str(payload.get("text") or payload.get("task") or "")
            self._json(self.app.pre_check(text))
            return
        if path == "/api/attach":
            self._json(
                self.app.attach(
                    cwd=str(payload.get("cwd") or ""),
                    pid=_as_int(payload.get("pid")),
                    log_path=str(payload.get("log_path") or ""),
                    agent=str(payload.get("agent") or ""),
                    session_id=str(payload.get("session_id") or ""),
                    goal=str(payload.get("goal") or ""),
                )
            )
            return
        self._json({"error": "not found", "path": path}, status=404)

    # -- 工具 ------------------------------------------------------------
    def _read_json(self) -> dict[str, Any] | None:
        """读并解析请求体。

        :return: 解析出的字典；非法时已回 400 并返回 ``None``。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            self._json({"error": "body too large"}, status=413)
            return None
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
        except (ValueError, OSError):
            self._json({"error": "invalid json"}, status=400)
            return None
        return data if isinstance(data, dict) else {}

    def _json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        """回一个 JSON 响应。

        :param payload: 响应体。
        :param status: HTTP 状态码。
        """
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            logger.debug("响应写出失败（客户端可能已断开）")


def _as_int(value: Any) -> int:
    """宽松地把值转成整数。

    :param value: 任意值。
    :return: 整数；转不了就 0。
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class LocalHTTPServer(ThreadingHTTPServer):
    """带应用引用的本地 HTTP 服务。

    :param address: ``(host, port)``。
    :param app: 应用实例。
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: DaemonApp) -> None:
        host = str(address[0] or "127.0.0.1")
        if host not in LOOPBACK_HOSTS:
            raise ValueError(f"只允许绑回环地址，收到 {host!r}")
        self.app = app
        super().__init__(address, _Handler)

    def server_close(self) -> None:
        """关服务时一并停掉监控线程。"""
        try:
            self.app.shutdown()
        finally:
            super().server_close()


def build_app(config: dict[str, Any] | None = None, *, version: str = "") -> DaemonApp:
    """建一个应用实例。

    :param config: 规则配置。
    :param version: 版本号。
    :return: 应用实例。
    """
    return DaemonApp(config=config, version=version)


def serve(
    host: str = "127.0.0.1",
    port: int = 8787,
    *,
    config: dict[str, Any] | None = None,
    app: DaemonApp | None = None,
) -> LocalHTTPServer:
    """启动服务（非阻塞，返回 server 供调用方自己 ``serve_forever``）。

    :param host: 绑定地址，只允许回环。
    :param port: 端口；``0`` 表示由系统分配（测试常用）。
    :param config: 规则配置。
    :param app: 复用的应用实例。
    :return: 已绑定的 server。
    """
    instance = app or build_app(config)
    return LocalHTTPServer((host, port), instance)
