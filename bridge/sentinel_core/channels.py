"""告警通道层 —— 把「拦下来了」这件事真的送到人眼前。

为什么有这个模块
================

账本里的告警只是**一条待投递记录**（``alerts.delivered = 0``）。半夜挂机时没人看
终端，如果没有人把它推出去，一次红线拦截的全部价值就只剩一行日志。这一层负责
「把账本里的待投递告警，经一条或多条通道送出去，送出后回写账本」。

设计取舍
========

* **通道可插拔**：本地日志、Webhook 各自实现 ``Channel``，宿主要接飞书、企业微信
  只需要再加一个类，不必改 hub。
* **兜底通道永不失败**：``LocalChannel`` 恒返回 ``True``，这样即使外网通道全挂，
  告警也会被标记为已投递、不会在 pending 里无限堆积（它有日志可查）。
* **每条告警走完所有通道，任一成功即视为送达**：多通道是「冗余」而不是「全部」，
  一条推出去就够人看见，不要求每条通道都回 200。**但绝不「第一条成功就 break」**：
  ``LocalChannel`` 恒成功且常排在第一个，break 会让外发通道一次都不被调用，而账本
  却写着「已送达」——那是安静地骗人。详见 :meth:`ChannelHub.dispatch_pending`。
* **兜底通道不冒领投递记录**：``LocalChannel`` 声明 ``fallback = True``，账本的
  ``alerts.channel`` 优先记**非兜底**通道名，只有外发全挂时才落 ``local``。否则用户
  翻账本会以为 webhook 通了。
* **全失败保留 pending**：不清账，下一轮 ``dispatch_pending`` 自然重试。重试节奏
  由调用方（定时器 / 下一轮派发）决定，本层不做退避、不做指数重试——不重试轰炸
  是刻意选择：告警风暴时把 webhook 打成 DDoS 只会让事情更糟。

安全边界（重要，别误读）
========================

**外发的内容是不受信任的，出去的每一条都要过三道闸：**

1. **scheme 与主机**：只允许 ``http`` / ``https``；``http`` 且主机不是本机
   （``127.0.0.1`` / ``::1`` / ``localhost`` / ``127.0.0.0/8``）一律拒发。
   原因：告警 body 里有 Agent 的命令原文与输出摘要，明文过网等于把它们交给路上
   任何人。**不静默降级成「只发标题」**——用户看不出少了什么比漏发更糟。
2. **只跟同源重定向**：``urllib`` 默认跟随 302/307，且跨主机重定向时会把
   ``Authorization`` 头**原样带过去**，307/308 还会把 POST body（命令全文）一起带走。
   :class:`_NoRedirect` 关掉自动跟随，改由 :meth:`WebhookChannel.send` 判定：
   scheme + host + port 全同才手工重发一次（上限 1 次），否则拒绝并记日志。
3. **外发脱敏**：``--token=…``、``API_KEY=…``、``Authorization: Bearer …``、
   ``sk-…`` 这类内容在打包成 JSON **之前**替换成 ``***``。规则与账本落盘共用
   :mod:`bridge.sentinel_core.redact`（同一套规则，避免两处漂移）。

三道闸都**不是**完整的 SSRF 防护：不解析 DNS（挡不住 ``http://internal.corp``）、
不判断解析后的 IP 段、不限制端口。完整的 SSRF 防护需要 DNS 解析 + IP 段判定，
那是「谁来配置这个 URL」的上游问题（配置只应由装机者填写，不该由 Agent 或用户
输入的文本推导）。如果你打算让这个 URL 变成用户可填字段，请先在配置入口做白名单。

失败处理
========

``send`` 一律以 ``False`` 表达失败，绝不抛异常；``dispatch_pending`` 把单条告警的
异常也吃掉并计入 ``failed``。安全告警层不能反过来成为可用性故障点。

注意区分两种「没发出去」：

* ``failed`` —— 试过了，暂时不行（超时、5xx）。下一轮该重试。
* ``skipped`` —— 这条**永远不该发**（地址不安全、缺主键）。重试没有意义，
  记 skipped 才不会每轮刷一遍失败日志。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

from .redact import scrub

__all__ = [
    "ALLOWED_SCHEMES",
    "Channel",
    "ChannelHub",
    "LocalChannel",
    "WebhookChannel",
]

logger = logging.getLogger(__name__)

#: 允许的 URL scheme（见模块 docstring 里的「安全边界」）。
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: 视为「本机」的主机名。回环地址上跑明文 http 不算外发。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "localhost.localdomain"})

#: 最多跟随几次同源重定向。一次就够：正常的 301 只是补个结尾斜杠。
_MAX_REDIRECTS = 1


def _redact_outbound(value: Any) -> Any:
    """递归脱敏一个待外发的结构。

    规则统一走 :func:`bridge.sentinel_core.redact.scrub`，与账本落盘**同一套** ——
    两套规则早晚会漂移，届时最容易出现「本地打码了、外发没打码」这种最难查的组合。
    这里是纵深防御的第二道：即使上游忘了脱敏，出口也会再擦一遍。

    在 ``json.dumps`` **之前**做：序列化后再改字符串，就要在 JSON 转义后的文本上
    匹配，稍有转义（``\\"``、``\\u…``）就会漏。

    :param value: 任意结构（告警行是 dict）。
    :return: 同样的结构，字符串字段已脱敏。
    """
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {key: _redact_outbound(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_outbound(item) for item in value]
    return value


def _is_loopback(host: str) -> bool:
    """主机名是否是本机。

    :param host: ``urlsplit(...).hostname`` 的结果。
    :return: 是本地回环则为真。
    """
    name = (host or "").strip().lower().strip("[]")
    if name in _LOOPBACK_HOSTS:
        return True
    # 整个 127.0.0.0/8 都是回环（127.0.0.53、127.1.2.3 之类）。
    return name.startswith("127.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """让 ``urllib`` **不自动**跟随重定向。

    返回 ``None`` 表示「这条重定向我不处理」，于是 ``urlopen`` 直接把 3xx 当
    ``HTTPError`` 抛出。默认处理器会跟下去，而 ``urllib`` 在跨主机重定向时**不会**
    剥掉 ``Authorization`` 头 —— 307/308 还会把 POST body（含命令全文）一起带过去，
    等于把凭据和正文交给跳转目标。

    跟随策略不放这里，而是交给 :meth:`WebhookChannel.send`：只有**同源**
    （scheme + host + port 全同）才手工重发一次，跨源一律拒绝。把判断放在 send 里
    是为了能记一条「为什么拒」的日志，而不是静默失败。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102 —— 见类 docstring
        # 显式返回 None：不构造新请求，等于不跟随。
        return None


def _origin(url: str) -> tuple[str, str, int]:
    """取 URL 的源（scheme, host, port）。

    :param url: 绝对 URL。
    :return: 三元组；端口按 scheme 补默认值。
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if scheme == "https" else 80)
    return scheme, host, int(port)


def _same_origin(base: str, other: str) -> bool:
    """两个 URL 是否同源。

    用「同源」而不是「同 host」是刻意的：``127.0.0.1:8080`` 跳到 ``127.0.0.1:9090``
    虽然同主机，但已经是另一个服务（可能是同一台机器上的别的租户），body 与
    ``Authorization`` 一样会外泄。

    :param base: 配置的 webhook 地址。
    :param other: 重定向目标（绝对 URL）。
    :return: 同源返回 ``True``。
    """
    try:
        return _origin(base) == _origin(other)
    except ValueError:
        # 端口非法之类：当作不同源。
        return False


class Channel(Protocol):
    """告警通道协议。

    任何具备 ``name`` 属性与 ``send(alert) -> bool`` 方法的对象都算通道，
    不需要继承任何基类（结构化子类型）。

    可选约定：实现 ``refuses(alert) -> str`` 表示「这条永远不该发」并给出原因；
    :class:`ChannelHub` 会把这类告警记成 ``skipped`` 而不是 ``failed``。
    """

    #: 通道名，会写进账本的 ``alerts.channel`` 字段，便于事后追「从哪条路送出去的」。
    name: str

    #: 可选约定（不写进 Protocol 契约，免得强制所有实现都声明）：
    #: ``fallback = True`` 表示这条通道是**兜底**（功能上永不失败、只保证告警不丢）。
    #: 兜底通道即使用例上成功了，也不会被记成 ``alerts.channel``——账本要回答的是
    #: 「人有没有真的被通知到」，而不是「日志有没有写下来」。:class:`LocalChannel` 是
    #: ``True``，其余实现不声明即视为 ``False``。

    def send(self, alert: dict) -> bool:
        """投递一条告警。

        :param alert: 账本里的告警行（``id`` / ``ts`` / ``level`` / ``title`` / ``body`` …）。
        :return: 是否投递成功。实现方不得抛异常。
        """
        ...


class LocalChannel:
    """兜底通道：只写日志。

    它永远是「成功」的。理由：账本才是真正的记录，这条通道的作用是让告警不要
    因为外网不可达而长期卡在 pending 队列里；能落日志就已经达成「事后可查」。
    """

    name = "local"

    #: 兜底通道标记，见 :class:`Channel` 的可选约定。用途是让账本别把「写了日志」
    #: 记成「已经通知到人」。
    fallback = True

    def send(self, alert: dict) -> bool:
        """把告警记进日志。

        :param alert: 告警行。
        :return: 恒为 ``True``。
        """
        level = str(alert.get("level") or "info")
        title = str(alert.get("title") or "").strip()
        body = str(alert.get("body") or "").strip()
        alert_id = alert.get("id")
        message = f"[哨兵告警:{level}] #{alert_id} {title}" + (f" —— {body}" if body else "")
        try:
            # critical 用 error 级别，warn 用 warning，其余 info：让日志过滤能分级。
            log = {
                "critical": logger.error,
                "error": logger.error,
                "warn": logger.warning,
                "warning": logger.warning,
            }.get(level.lower(), logger.info)
            log(message)
        except Exception:  # noqa: BLE001 —— 日志handler也可能是坏配置，兜底通道不许失败
            pass
        return True


class WebhookChannel:
    """Webhook 通道：一个 JSON POST，仅此而已。

    :ivar url: 目标地址。
    :ivar timeout: 单次请求超时（秒）。给短值：告警不该拖住派发线程。
    :ivar headers: 额外的请求头（如 ``Authorization``）。
    """

    name = "webhook"

    def __init__(self, url: str, *, timeout: float = 3.0, headers: dict | None = None) -> None:
        self.url = str(url or "").strip()
        self.timeout = float(timeout)
        self.headers = dict(headers or {})
        # 不跟随重定向（见 _NoRedirect）：默认处理器会把 Authorization 带过去。
        self._opener = urllib.request.build_opener(_NoRedirect())
        # 启动时体检一次：配置错了要在启动日志里就看见，而不是等第一次告警。
        # 判定本身在每次 send / refuses 时重算（一次 urlsplit，成本可忽略），
        # 这样运行期改了 self.url 也不会用到过期的结论。
        problem = self._target_problem()
        if problem:
            logger.warning(
                "Webhook 通道已禁用（%s）：%r —— 告警只会走本地通道",
                problem,
                self.url,
            )

    def _target_problem(self) -> str:
        """体检目标地址。

        :return: 不可用的原因；空串表示可用。
        """
        try:
            parts = urlsplit(self.url)
        except ValueError:
            return "地址无法解析"
        scheme = parts.scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            return f"scheme 必须是 http/https（当前 {scheme or '空'}）"
        host = (parts.hostname or "").strip()
        if not host or not parts.netloc:
            return "地址缺少主机名"
        if scheme == "http" and not _is_loopback(host):
            # 明文过网 = 把 Agent 的命令与输出摘要交给路上任何人。
            return "非本机主机不接受明文 http（会把命令与输出摘要暴露在网络上），请改用 https"
        return ""

    def refuses(self, alert: dict) -> str:
        """这条告警为什么不能发。

        与 ``send() -> False`` 的区别：``False`` 是「暂时失败、下次可重试」，
        refusal 是「这条永远不该发出去」。hub 据此记 ``skipped`` 而不是 ``failed``，
        避免每一轮都重新打一遍注定失败的请求、刷一屏失败日志。

        :param alert: 告警行（当前判定只看地址，不看内容）。
        :return: 原因；空串表示可以发。
        """
        return self._target_problem()

    def send(self, alert: dict) -> bool:
        """把告警 POST 出去。

        成功判据是 HTTP 2xx。超时、DNS 失败、连接被拒、非 2xx 状态、被拒绝跟随的
        重定向一律返回 ``False``——本层不重试，重试策略归 hub 与调用方。

        :param alert: 告警行（发送前会脱敏）。
        :return: 是否 2xx。
        """
        problem = self._target_problem()
        if problem:
            # 构造时已经 warning 过一次，这里只记 debug，免得每轮 flush 都刷屏。
            logger.debug("Webhook 投递跳过（%s）：%r", problem, self.url)
            return False
        try:
            # 脱敏必须在序列化之前做，见 _redact_outbound 的说明。
            payload = json.dumps(_redact_outbound(alert), ensure_ascii=False, default=str).encode("utf-8")
        except (TypeError, ValueError) as exc:
            # default=str 已经兜住绝大多数情况，这里只防极端脏数据。
            logger.warning("Webhook 负载序列化失败：%s: %s", type(exc).__name__, exc)
            return False

        headers = {"Content-Type": "application/json; charset=utf-8"}
        headers.update(self.headers)

        target = self.url
        for attempt in range(_MAX_REDIRECTS + 1):
            outcome = self._post_once(target, payload, headers)
            if outcome is None:
                # 超时 / DNS / 连接拒绝 / TLS：异常已在 _post_once 里记过日志。
                return False
            kind, detail = outcome
            if kind == "ok":
                return True
            if kind == "status":
                logger.warning("Webhook 返回非成功状态 %s：%r", detail, target)
                return False

            # kind == "redirect"：detail 是 Location。
            if attempt >= _MAX_REDIRECTS:
                logger.warning("Webhook 重定向次数超过上限 %s，放弃：%r", _MAX_REDIRECTS, self.url)
                return False
            nxt = urljoin(target, detail)
            if not _same_origin(self.url, nxt):
                logger.warning(
                    "Webhook 返回跨源重定向 → %r，已拒绝跟随（避免 Authorization 与 body 被带到其它源）：%r",
                    nxt,
                    self.url,
                )
                return False
            logger.info("Webhook 同源重定向 %r → %r，重发一次", target, nxt)
            target = nxt
        return False

    def _post_once(self, url: str, payload: bytes, headers: dict) -> tuple[str, Any] | None:
        """发一次 POST。

        :param url: 目标地址。
        :param payload: 已序列化（且已脱敏）的请求体。
        :param headers: 请求头。
        :return: ``("ok", status)`` / ``("status", code)`` / ``("redirect", location)``；
            异常返回 ``None``。
        """
        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = int(getattr(response, "status", 0) or 0)
                # 读完响应体（丢弃）：不读会导致连接不能复用，也算礼貌收尾。
                try:
                    response.read()
                except Exception:  # noqa: BLE001 —— 读响应失败不影响「已送达」的判定
                    pass
            return ("ok", status) if 200 <= status < 300 else ("status", status)
        except urllib.error.HTTPError as exc:
            code = int(getattr(exc, "code", 0) or 0)
            location = ""
            if 300 <= code < 400:
                try:
                    location = str((exc.headers or {}).get("Location") or "").strip()
                except Exception:  # noqa: BLE001 —— 头部异常就当没有 Location
                    location = ""
            try:
                exc.close()
            except Exception:  # noqa: BLE001
                pass
            if location:
                return ("redirect", location)
            return ("status", code or 0)
        except Exception as exc:  # noqa: BLE001 —— 超时 / DNS / 连接拒绝 / TLS 全走这条
            logger.warning("Webhook 投递失败（已降级）：%s: %s", type(exc).__name__, exc)
            return None


class ChannelHub:
    """通道集合与投递调度。

    :ivar channels: 已注册的通道（按注册顺序投递）。
    """

    def __init__(self, channels: list[Channel] | None = None) -> None:
        self.channels: list[Channel] = []
        for channel in channels or []:
            self.add(channel)

    def add(self, channel: Channel) -> None:
        """注册一条通道。

        :param channel: 通道实例。
        """
        if channel is None:
            return
        self.channels.append(channel)

    def dispatch_pending(self, store: Any, *, limit: int = 50) -> dict:
        """把账本里未投递的告警送出去，**每条告警都走完所有通道**。

        为什么不能「第一条成功就 break」：``LocalChannel`` 恒成功，而生产装配顺序是
        ``[LocalChannel(), WebhookChannel(...)]``。一旦 break，webhook 一次都不会被
        调用，告警却被标成 ``delivered=1`` —— 用户以为外发成功了，实际上什么都没发出去。
        这是「安静地骗人」，比投递失败更糟。

        所以：逐条告警遍历**全部**通道，各自尝试；只要**任一**成功就
        ``mark_delivered``（避免下一轮重复推送），全部失败才留在 pending。

        计数单位是**通道尝试次数**（不是告警条数），如实反映有没有真的发出去：

        * ``sent`` —— 成功的通道调用次数；
        * ``failed`` —— 失败且可重试的通道调用次数；
        * ``skipped`` —— 配置类跳过（通道 ``refuses``，如明文 http 指向外网）。
          它不计入 ``failed``：重试永远不会成功，记失败只会每轮刷一屏日志。

        注意语义上的一个取舍：``LocalChannel`` 恒成功时，失败的 webhook 不会在下一轮
        重试（本地已经收到了，告警不会丢）。**但它必须如实出现在 ``failed`` 里**，
        不能因为「有人成功了」就瞒掉。

        账本的 ``alerts.channel`` 记**优先非兜底**的通道名：外发成功就记 ``webhook``，
        外发全挂才记 ``local``。否则用户翻账本会以为外发通了。

        :param store: ``EventStore`` 实例（鸭子类型：只需 ``pending_alerts`` /
            ``mark_delivered``）。
        :param limit: 单轮最多处理几条告警。
        :return: ``{"sent": n, "failed": n, "skipped": n}``（单位见上）。
        """
        result = {"sent": 0, "failed": 0, "skipped": 0}
        if store is None:
            return result
        try:
            pending = store.pending_alerts(limit=max(1, int(limit)))
        except Exception as exc:  # noqa: BLE001 —— 账本读失败不该让调用方炸
            logger.warning("读取待投递告警失败（已降级）：%s: %s", type(exc).__name__, exc)
            return result

        for alert in pending or []:
            alert_id = alert.get("id")
            if not alert_id:
                # 没有主键就无法回写投递状态，送出去只会导致下一轮重复推送。
                result["skipped"] += 1
                continue

            # 优先记非兜底通道（真的把人通知到了），外发全挂才退回兜底通道。
            primary_by = ""
            fallback_by = ""
            for channel in self.channels:
                name = str(getattr(channel, "name", "") or "unknown")
                reason = _refusal(channel, alert)
                if reason:
                    # 通道主动拒发（如明文 http 指向外网）：配置问题，重试无意义。
                    result["skipped"] += 1
                    logger.debug("通道 %r 拒绝发送：%s", name, reason)
                    continue
                if _try_send(channel, alert):
                    result["sent"] += 1
                    if getattr(channel, "fallback", False):
                        fallback_by = fallback_by or name
                    else:
                        primary_by = primary_by or name
                else:
                    result["failed"] += 1

            delivered_by = primary_by or fallback_by
            if delivered_by:
                try:
                    store.mark_delivered(alert_id, channel=delivered_by)
                except Exception as exc:  # noqa: BLE001 —— 回写失败只影响下一轮重复推送
                    logger.warning("回写投递状态失败（已降级）：%s: %s", type(exc).__name__, exc)

        if result["failed"]:
            logger.warning(
                "本轮有 %s 次通道投递失败（success=%s skipped=%s）；本地通道已收到的不受影响",
                result["failed"],
                result["sent"],
                result["skipped"],
            )
        # 单位提醒：sent / failed / skipped 都是**通道尝试次数**，不是告警条数。
        # 一条告警配两条通道时 sent 可能是 2；别拿它当「推送了几条告警」显示给用户。
        return result


def _refusal(channel: Any, alert: dict) -> str:
    """问通道「这条你能不能发」。

    :param channel: 通道实例。
    :param alert: 告警行。
    :return: 拒绝原因；空串表示可以发（不实现该约定的通道一律视为可以发）。
    """
    checker = getattr(channel, "refuses", None)
    if not callable(checker):
        return ""
    try:
        return str(checker(alert) or "")
    except Exception as exc:  # noqa: BLE001 —— 第三方通道实现可能不守约定，这里兜住
        logger.warning("通道 %r 的 refuses 抛异常（已忽略）：%s: %s", getattr(channel, "name", "?"), type(exc).__name__, exc)
        return ""


def _try_send(channel: Any, alert: dict) -> bool:
    """调用一条通道并吞掉它的所有异常。

    :param channel: 通道实例。
    :param alert: 告警行。
    :return: 是否成功。
    """
    try:
        return bool(channel.send(alert))
    except Exception as exc:  # noqa: BLE001 —— 第三方通道实现可能不守约定，这里兜住
        logger.warning("通道 %r 抛异常（已降级）：%s: %s", getattr(channel, "name", "?"), type(exc).__name__, exc)
        return False
