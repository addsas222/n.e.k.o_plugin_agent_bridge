"""AstrBot 插件：把 agent_bridge 的本机 Agent 路由接到聊天里。

两个命令：
    /bridge_status          看哪些 Agent 可用（含需登录 / 慢）
    /bridge <任务>          派给指定 Agent，例：/bridge gemini 把 README 的错别字列出来
    /bridge <任务>          不给 Agent 时用配置里的默认（或自动挑第一个可用）

为什么不直接 import agent_bridge：AstrBot 与它可能是两个 Python 环境，
最稳的边界是**本地 HTTP**（127.0.0.1:8787），和 VS Code 扩展用同一套接口。
零第三方依赖：只用标准库 urllib，在线程里跑，不阻塞事件循环。

前置：先起控制台 `python webui/server.py`（默认 8787）。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter

BASE_URL = "http://127.0.0.1:8787"
DEFAULT_TIMEOUT = 600
MAX_REPLY_CHARS = 1500


def _request(method: str, path: str, payload: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_agents() -> list[dict]:
    return _request("GET", "/api/agents", timeout=10).get("agents", [])


def _dispatch(agent: str, task: str, timeout: int) -> dict:
    return _request(
        "POST",
        "/api/dispatch",
        {"agent": agent, "task": task, "timeout_seconds": timeout},
        timeout=timeout + 30,
    )


def _clip(text: str, limit: int = MAX_REPLY_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，共 {len(text)} 字；完整输出在控制台里看）"


class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context

    @filter.command("bridge_status")
    async def bridge_status(self, event: AstrMessageEvent):
        """看本机哪些编码 Agent 可用。"""
        try:
            agents = await asyncio.to_thread(_fetch_agents)
        except Exception as exc:  # noqa: BLE001 —— 出错要回人话，不是抛栈
            yield event.plain_result(
                f"连不上 agent_bridge 控制台（{BASE_URL}）：{exc}\n"
                f"先在终端跑 `python webui/server.py`。"
            )
            return

        ready = [a for a in agents if a.get("ready")]
        lines = [f"本机 {len(agents)} 条路由，可用 {len(ready)} 条："]
        for agent in agents:
            if not agent.get("exists"):
                mark = "✖"
                state = "没装"
            elif agent.get("needs_login"):
                mark = "!"
                state = "需登录"
            elif agent.get("slow"):
                mark = "…"
                state = "慢/待验证"
            elif agent.get("ready"):
                mark = "✔"
                state = "可派发"
            else:
                mark = "·"
                state = "已禁用"
            version = (agent.get("version") or "").strip()
            lines.append(f"{mark} {agent['id']}（{state}）{(' ' + version) if version else ''}")
        yield event.plain_result("\n".join(lines))

    @filter.command("bridge")
    async def bridge(self, event: AstrMessageEvent):
        """派活给本机 Agent：/bridge <agent> <任务> 或 /bridge <任务>。"""
        raw = (event.message_str or "").strip()
        body = raw.split(maxsplit=1)[1] if " " in raw else ""
        if not body:
            yield event.plain_result(
                "用法：\n"
                "/bridge_status  看谁可用\n"
                "/bridge gemini 把 README 的错别字列出来\n"
                "/bridge 只回一个字：好   （不给 agent 时自动挑一个可用的）"
            )
            return

        try:
            agents = await asyncio.to_thread(_fetch_agents)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(
                f"连不上 agent_bridge 控制台（{BASE_URL}）：{exc}\n"
                f"先在终端跑 `python webui/server.py`。"
            )
            return

        by_id = {a["id"]: a for a in agents}
        aliases = {}
        for agent in agents:
            for alias in agent.get("aliases") or []:
                aliases[alias] = agent["id"]

        first, _, rest = body.partition(" ")
        agent_id = first if (first in by_id or first in aliases) else ""
        if agent_id:
            agent_id = by_id[agent_id]["id"] if agent_id in by_id else aliases[agent_id]
            task = rest.strip()
        else:
            agent_id = next((a["id"] for a in agents if a.get("ready")), "")
            task = body

        if not agent_id:
            yield event.plain_result("没有可用的 Agent，先用 /bridge_status 看看。")
            return
        if not task:
            yield event.plain_result("任务不能为空。例：/bridge gemini 把 README 的错别字列出来")
            return

        yield event.plain_result(f"派给 {agent_id}，可能需要几分钟…")
        try:
            result = await asyncio.to_thread(_dispatch, agent_id, task, DEFAULT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"派发失败：{exc}")
            return

        if result.get("ok"):
            yield event.plain_result(_clip(f"✔ {agent_id} 完成\n{result.get('output') or '(无输出)'}"))
            return
        reason = result.get("error") or ""
        if result.get("needs_login"):
            reason = f"该 Agent 没登录，任务没执行。{reason}"
        yield event.plain_result(_clip(f"✖ {agent_id} 失败（exit={result.get('exit_code')}）\n{reason}"))
