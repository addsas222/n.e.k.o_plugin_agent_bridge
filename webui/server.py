#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent_bridge 的本地控制台（HTTP + 静态页，纯标准库）。

    python -m webui.server                 # http://127.0.0.1:8787
    python -m webui.server --port 8790
    python -m webui.server --open          # 起完自动开浏览器

只绑 127.0.0.1：这是个能**真派发任务**的入口，暴露到局域网等于把本机 CLI 交出去。

接口（全部 JSON）
----------------
    GET  /api/agents              路由清单 + 解析结果（不含版本，快）
    GET  /api/version?agent=<id>  单个 Agent 的版本（慢，按需点）
    POST /api/dispatch            {agent, task, cwd?, timeout_seconds?}
    POST /api/enabled             {agent, enabled} → 写 ~/.neko-agent-bridge/config.json
    GET  /api/harness             Harness 矩阵（skill/MCP 装机状态）
    GET  /api/audit?limit=30      最近的派发记录（读 dispatch.jsonl）

为什么不做流式输出：派发已经是一次请求-响应，SSE 只会多一层要维护的协议；
页面用计时器 + 一次性返回就够判断「在跑 / 跑完了」。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bridge.config import AgentOverride, load_config
from bridge.registry import AGENT_SPECS, canonical_id, list_specs, resolve_agent
from bridge.runner import probe_version, run_agent
from bridge.tools import write_audit

ROOT = Path(__file__).resolve().parents[1]

BUILD = "r5"
"""服务端构建号。页面会拿它比对 —— 前后端版本不一致时，功能会「点了没反应」，
这是本仓库踩过的坑：改了 server.py 但没重启，前端拿不到新接口。"""

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(
    os.environ.get("NEKO_AGENT_BRIDGE_CONFIG")
    or (Path(os.path.expanduser("~")) / ".neko-agent-bridge" / "config.json")
)
"""写 enabled 开关的位置。与 ``bridge/config.py`` 认同一个环境变量，
这样测试可以指到临时文件，不会动用户的真实配置。"""
NEEDS_LOGIN: set[str] = set()
"""本进程内被判定「没登录」的 Agent。与插件的内存行为一致：重启即清。"""

_SLOW: set[str] = set()
"""探测超时/无输出的 Agent。**不等于不可用** —— 有的 CLI 未登录时静默挂起
（实测 step 40s+ 零输出）。面板要把这种和「可派发」分开，否则用户会以为它能用。"""

_VERSION_CACHE: dict[str, tuple[float, str]] = {}
"""id -> (取得时间, 版本串)。探测要真跑 CLI（最坏 45s），不缓存的话
12 行全点一遍等于把面板卡死几分钟 —— 而且用户会以为"点了没反应"。"""
VERSION_TTL = 600.0
AUDIT_PATH = CONFIG_PATH.parent / "dispatch.jsonl"
"""面板自己的派发记录。以前这里只读不写，于是"最近派发"永远显示别人的旧日志 ——
用户看到的就是一屏假数据。现在每次派发都落一条带 ts 和 source 的记录。"""


def _warm_versions() -> None:
    """启动后后台把已安装 Agent 的版本探一遍，写进缓存。

    为什么放服务端：面板一打开就该看到版本，而不是"先渲染一排按钮、点一下才动"。
    前端探测还受浏览器并发/超时影响，表现成"点了没反应"；放在这里，用 curl 就能验。
    """
    from concurrent.futures import ThreadPoolExecutor

    def one(spec) -> None:
        try:
            config = load_config()
            resolved = resolve_agent(spec, config.override_for(spec.id).exe_override)
            if not resolved.exists:
                return
            version = asyncio.run(probe_version(spec, resolved, config=config, timeout=10))
            if version and not version.startswith("<"):
                _VERSION_CACHE[spec.id] = (time.time(), version)
                _SLOW.discard(spec.id)
            else:
                _SLOW.add(spec.id)
                print(f"[warm] {spec.id:14} {version[:40]!r}", flush=True)
        except Exception as exc:  # noqa: BLE001 —— 预热失败只是没版本，不该影响服务
            print(f"[warm] {spec.id} 失败：{exc!r}", flush=True)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(one, list_specs()))


def agent_rows() -> list[dict]:
    config = load_config()
    rows: list[dict] = []
    for spec in list_specs():
        override = config.override_for(spec.id)
        resolved = resolve_agent(spec, override.exe_override)
        blocked = spec.id in NEEDS_LOGIN
        rows.append(
            {
                "id": spec.id,
                "label": spec.label,
                "mode": spec.mode,
                "binary": spec.binary,
                "source": resolved.source,
                "command": resolved.describe(),
                "exists": resolved.exists,
                "enabled": override.enabled,
                "needs_login": blocked,
                "ready": bool(resolved.exists and override.enabled and not blocked),
                "notes": spec.notes,
                "login_hint": spec.login_hint,
                "aliases": list(spec.aliases),
                "version": _VERSION_CACHE.get(spec.id, (0, ""))[1],
                "slow": spec.id in _SLOW,
                "install_cmd": getattr(spec, "install_cmd", ""),
                "install_hint": getattr(spec, "install_hint", ""),
            }
        )
    return rows


def audit_tail(limit: int) -> list[dict]:
    config = load_config()
    path = Path(config.audit_log or AUDIT_PATH)
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-limit:]
    out: list[dict] = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


_MATRIX_CACHE: dict[str, tuple[float, int, list[dict]]] = {}
"""frameworks.toml 的解析缓存：key 是路径，value 是 (mtime, size, 行)。

面板每次请求都要读它，而它只在部署时变。用 mtime+size 判失效，
比"每次重新 open + tomllib.load"省 60+ 次文件读 —— 66 个 harness 时差别明显。
"""


def _matrix_rows() -> list[dict]:
    matrix = ROOT / "skill" / "neko-bridge" / "frameworks.toml"
    if not matrix.is_file():
        return []
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover
        return []
    stat = matrix.stat()
    key = str(matrix)
    cached = _MATRIX_CACHE.get(key)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    with open(matrix, "rb") as handle:
        rows = tomllib.load(handle).get("harness", [])
    _MATRIX_CACHE[key] = (stat.st_mtime, stat.st_size, rows)
    return rows


def harness_rows() -> list[dict]:
    """读 frameworks.toml + 探目录，给面板一份「skill 装到哪、MCP 怎么接、缺了怎么装」。

    形态（form）分 cli / desktop / web / extension：同一个产品常常有多个形态
    （DeepSeek Harness 有 desktop 与 web，OpenCode 有 cli/desktop/web），
    面板必须能分开显示，否则「没装」这句话是错的 —— 装的可能是另一个形态。
    没显式写 form 的行按 kind 推导，避免给 59 行都补一个字段。
    """
    rows = _matrix_rows()
    if not rows:
        return []
    # 配置只读一次：以前这一行在 66 行的循环里，每个 harness 都重读一遍 config.json
    harness_dirs = _read_config_raw().get("harness_dirs", {})
    home = Path(os.path.expanduser("~"))
    derived = {"cli": "cli", "ide": "desktop", "desktop": "desktop", "web": "web",
               "server": "server", "generic": "shared"}
    out: list[dict] = []
    for row in rows:
        raw = str(row["skills_dir"]).replace("~", str(home))
        parent, _, tail = raw.partition("*/")
        # 通配展开时跳过「副本 / copy / backup」目录 —— 与 install.py 同一套规则，
        # 否则面板里会冒出一条 "default - 副本"，用户以为装了两份。
        skip = ("副本", "copy", "backup", "bak")
        dirs = [Path(raw)] if "*" not in raw else (
            sorted(
                entry / tail
                for entry in Path(parent.rstrip("/\\")).iterdir()
                if entry.is_dir() and not any(marker in entry.name.lower() for marker in skip)
            )
            if Path(parent.rstrip("/\\")).is_dir() else []
        )
        if not dirs:
            dirs = [Path(raw)] if raw else []
        extra = harness_dirs.get(row["id"])
        if extra:
            dirs = [Path(extra), *dirs]
        for directory in dirs:
            installed = bool(directory) and (directory / "neko-bridge" / "SKILL.md").is_file()
            out.append(
                {
                    "id": row["id"],
                    "label": row["label"],
                    "kind": row["kind"],
                    "form": row.get("form") or derived.get(row["kind"], row["kind"]),
                    "dir": str(directory) if directory else "",
                    "exists": bool(directory) and directory.is_dir(),
                    "installed": installed,
                    "verified": bool(row.get("verified")),
                    "source": row.get("source", ""),
                    "mcp_kind": row.get("mcp_kind", "none"),
                    "mcp_target": row.get("mcp_target", ""),
                    "install_cmd": row.get("install_cmd", ""),
                    "install_hint": row.get("install_hint", ""),
                    "url": row.get("url", ""),
                }
            )
    return out


def write_enabled(agent: str, enabled: bool) -> dict:
    spec_id = canonical_id(agent)
    if not spec_id:
        raise ValueError(f"未知 agent：{agent!r}")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    raw: dict = {}
    if CONFIG_PATH.is_file():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
    agents = raw.setdefault("agents", {})
    entry = agents.setdefault(spec_id, {})
    entry["enabled"] = bool(enabled)
    CONFIG_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"agent": spec_id, "enabled": bool(enabled), "config": str(CONFIG_PATH)}


# --------------------------------------------------------------------------
# 安装 / 定位
# --------------------------------------------------------------------------


def _read_config_raw() -> dict:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_config_raw(raw: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


def _matrix_row(target_id: str) -> dict | None:
    return next((row for row in _matrix_rows() if row["id"] == target_id), None)


def _install_command(target_id: str, kind: str) -> str:
    """取该目标的安装命令。**只认仓库里声明的**，不接受前端传命令。"""
    if kind == "route":
        spec = AGENT_SPECS.get(canonical_id(target_id) or "")
        return (getattr(spec, "install_cmd", "") or "") if spec else ""
    row = _matrix_row(target_id)
    return str(row.get("install_cmd", "")) if row else ""


def _after_install(target_id: str, kind: str) -> dict:
    """装完立刻回读状态：是不是真能用，版本是多少。"""
    if kind == "route":
        spec = AGENT_SPECS.get(canonical_id(target_id) or "")
        if not spec:
            return {"installed": False, "version": ""}
        config = load_config()
        resolved = resolve_agent(spec, config.override_for(spec.id).exe_override)
        version = ""
        if resolved.exists:
            version = asyncio.run(probe_version(spec, resolved, config=config))
        return {"installed": resolved.exists, "version": version, "source": resolved.source}
    for row in harness_rows():
        if row["id"] == target_id:
            return {"installed": row["exists"], "version": ""}
    return {"installed": False, "version": ""}


def install_target(target_id: str, kind: str = "route", dry_run: bool = False) -> dict:
    """执行该目标声明的安装命令，然后回读状态。

    安全边界：
    * 命令来自 repo 里的清单（`provider.toml` / `frameworks.toml`），**不是**请求体；
      前端只能给 id，注入面为零。
    * 超时 300s；输出截断到 4KB，只用于展示。
    * `dry_run` 只回显将执行的命令，测试与「先看看」用这条。
    """
    command = _install_command(target_id, kind)
    if not command:
        if kind == "route":
            spec = AGENT_SPECS.get(canonical_id(target_id) or "")
            hint = getattr(spec, "install_hint", "") if spec else ""
        else:
            hint = (_matrix_row(target_id) or {}).get("install_hint", "")
        return {
            "ok": False,
            "id": target_id,
            "command": "",
            "reason": "no-command",
            "hint": hint or "这个目标没有一键安装命令，请手动安装后在面板里「指定路径」。",
        }
    if dry_run:
        return {"ok": True, "dry_run": True, "id": target_id, "command": command}

    started = time.monotonic()
    try:
        # shell=True 在这里是安全的：command 只可能来自仓库清单（TOML），
        # 请求体只能提供 id。且安装命令本身就是 shell 语法（`bun add -g …`）。
        # 派发路径（run_agent）仍然一律走列表形式，绝不过 shell。
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        code, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return {"ok": False, "id": target_id, "command": command, "reason": "timeout",
                "log": "安装超时（300s）"}
    status = _after_install(target_id, kind)
    return {
        "ok": bool(status.get("installed")),
        "id": target_id,
        "command": command,
        "exit_code": code,
        "seconds": round(time.monotonic() - started, 1),
        "log": out[-4000:],
        "installed": bool(status.get("installed")),
        "version": status.get("version", ""),
    }


def install_skill(target_id: str, dry_run: bool = False) -> dict:
    """把 neko-bridge skill 拷进某个 harness 的 skills 目录。

    与「装 harness 本身」是两件事：这里只做文件复制（SKILL.md + bin + providers），
    不跑任何安装器。找不到目录就返回 need-locate，让前端弹输入框。
    """
    source = ROOT / "skill" / "neko-bridge"
    target_row = next((row for row in harness_rows() if row["id"] == target_id), None)
    if target_row is None:
        return {"ok": False, "id": target_id, "reason": "unknown"}
    directory = Path(target_row["dir"]) if target_row["dir"] else None
    if directory is None or not directory.is_dir():
        return {"ok": False, "id": target_id, "reason": "need-locate",
                "hint": "这个 harness 的目录不存在。先装应用，或手动指定路径。"}
    plan = [source / name for name in ("SKILL.md", "README.md", "bin", "providers")]
    if dry_run:
        return {"ok": True, "dry_run": True, "id": target_id,
                "target": str(directory / "neko-bridge"), "files": [p.name for p in plan if p.exists()]}
    destination = directory / "neko-bridge"
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for item in plan:
        if not item.exists():
            continue
        if item.is_dir():
            shutil.copytree(item, destination / item.name, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination / item.name)
        copied.append(item.name)
    ok = (destination / "SKILL.md").is_file()
    return {"ok": ok, "id": target_id, "target": str(destination), "files": copied}


def locate_target(target_id: str, path: str, kind: str = "route") -> dict:
    """默认路径找不到时，让用户手输一次。

    route  → 写 `exe_override`（桥接派发时用这个路径）
    harness → 记为额外候选目录，装 skill 时优先用它
    """
    text = (path or "").strip().strip('"')
    if not text:
        raise ValueError("路径不能为空")
    resolved_path = Path(os.path.expanduser(text))
    if not resolved_path.exists():
        raise ValueError(f"路径不存在：{resolved_path}")

    raw = _read_config_raw()
    if kind == "route":
        spec_id = canonical_id(target_id)
        if not spec_id:
            raise ValueError(f"未知 agent：{target_id!r}")
        raw.setdefault("agents", {}).setdefault(spec_id, {})["exe_override"] = str(resolved_path)
        _write_config_raw(raw)
        return {"ok": True, "id": spec_id, "kind": "route", "exe_override": str(resolved_path)}
    raw.setdefault("harness_dirs", {})[target_id] = str(resolved_path)
    _write_config_raw(raw)
    return {"ok": True, "id": target_id, "kind": "harness", "dir": str(resolved_path)}



def dispatch(agent: str, task: str, cwd: str, timeout_seconds: int) -> dict:
    spec_id = canonical_id(agent)
    if not spec_id:
        return {"ok": False, "error": f"未知 agent：{agent!r}", "agent": agent}
    spec = AGENT_SPECS[spec_id]
    config = load_config()
    override: AgentOverride = config.override_for(spec_id)
    resolved = resolve_agent(spec, override.exe_override)
    if not resolved.exists:
        return {"ok": False, "error": f"未找到可执行文件：{resolved.describe()}", "agent": spec_id}
    if spec_id in NEEDS_LOGIN:
        return {"ok": False, "agent": spec_id, "needs_login": True,
                "error": "该 CLI 上次返回未登录，本进程内已跳过（重启服务器可清）。"}
    task = (task or "").strip()
    if not task:
        return {"ok": False, "error": "task 不能为空", "agent": spec_id}

    seconds = config.clamp_timeout(int(timeout_seconds or 0) or config.default_timeout_seconds)
    started = time.monotonic()
    result = asyncio.run(
        run_agent(
            spec, resolved, task,
            config=config,
            cwd=config.resolve_cwd(cwd or None),
            model=override.model,
            timeout=seconds,
            extra_args=override.extra_args,
        )
    )
    if result.needs_login:
        NEEDS_LOGIN.add(spec_id)
    elif result.ok:
        NEEDS_LOGIN.discard(spec_id)

    try:
        audit_config = load_config()
        audit_config.audit_log = audit_config.audit_log or str(AUDIT_PATH)
        write_audit(
            audit_config,
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "source": "webui",
                "agent": spec_id,
                "exit_code": result.exit_code,
                "ok": bool(result.ok),
                "needs_login": bool(result.needs_login),
                "timed_out": bool(result.timed_out),
                "duration_ms": result.duration_ms,
                "task": task[:120],
            },
        )
    except Exception:  # noqa: BLE001 —— 审计失败不该影响返回
        pass
    return {
        "ok": bool(result.ok),
        "agent": spec_id,
        "label": result.label,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "needs_login": bool(result.needs_login),
        "error": result.error,
        "command": result.command_preview,
        "output": result.output_text(),
        "stderr": result.stderr.strip()[-1500:],
        "seconds": round(time.monotonic() - started, 1),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-bridge-webui/0.2"

    def log_message(self, fmt: str, *args) -> None:  # 安静点，别把每条请求刷到终端
        if os.environ.get("NEKO_WEBUI_VERBOSE"):
            super().log_message(fmt, *args)

    # ---------------------------------------------------------------- 基础
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: object, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ---------------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            page = HERE / "index.html"
            if not page.is_file():
                self._json({"error": "index.html 缺失"}, 500)
                return
            self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self._json(
                {
                    "build": BUILD,
                    "routes": len(AGENT_SPECS),
                    "features": [
                        "agents", "version", "harness", "dispatch", "enabled", "audit",
                        "install", "install_skill", "locate",
                    ],
                }
            )
            return
        if path == "/api/agents":
            self._json({"agents": agent_rows(), "count": len(AGENT_SPECS)})
            return
        if path == "/api/version":
            query = parse_qs(parsed.query)
            agent_id = canonical_id((query.get("agent") or [""])[0])
            if not agent_id:
                self._json({"error": "unknown agent"}, 400)
                return
            try:
                budget = int((query.get("timeout") or ["10"])[0])
            except ValueError:
                budget = 10
            budget = max(3, min(budget, 45))

            refresh = (query.get("refresh") or [""])[0] in ("1", "true")
            cached = _VERSION_CACHE.get(agent_id)
            # 手动「探测」带 refresh=1，必须绕过缓存 —— 否则用户点了个寂寞
            if cached and not refresh and (time.time() - cached[0]) < VERSION_TTL:
                self._json({"agent": agent_id, "version": cached[1], "cached": True})
                return

            spec = AGENT_SPECS[agent_id]
            config = load_config()
            resolved = resolve_agent(spec, config.override_for(agent_id).exe_override)
            started = time.monotonic()
            if not resolved.exists:
                version = ""
            elif cached and not refresh:
                version = cached[1]
            else:
                version = asyncio.run(probe_version(spec, resolved, config=config, timeout=budget))
            elapsed = time.monotonic() - started
            if version and not version.startswith("<"):
                _VERSION_CACHE[agent_id] = (time.time(), version)
                _SLOW.discard(agent_id)
            elif resolved.exists:
                _SLOW.add(agent_id)
            print(f"[version] {agent_id:14} {version[:40]!r} {elapsed:.1f}s", flush=True)
            self._json({
                "agent": agent_id,
                "version": "" if version.startswith("<") else version,
                "note": version if version.startswith("<") else "",
                "seconds": round(elapsed, 1),
            })
            return
        if path == "/api/harness":
            self._json({"harness": harness_rows()})
            return
        if path == "/api/audit":
            limit = int((parse_qs(parsed.query).get("limit") or ["30"])[0])
            self._json({"records": audit_tail(min(limit, 200))})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        payload = self._body()
        try:
            if path == "/api/dispatch":
                result = dispatch(
                    str(payload.get("agent") or ""),
                    str(payload.get("task") or ""),
                    str(payload.get("cwd") or ""),
                    int(payload.get("timeout_seconds") or 0),
                )
                self._json(result)
                return
            if path == "/api/enabled":
                self._json(write_enabled(str(payload.get("agent") or ""), bool(payload.get("enabled"))))
                return
            if path == "/api/install":
                self._json(
                    install_target(
                        str(payload.get("id") or ""),
                        str(payload.get("kind") or "route"),
                        bool(payload.get("dry_run")),
                    )
                )
                return
            if path == "/api/install_skill":
                self._json(
                    install_skill(
                        str(payload.get("id") or ""),
                        bool(payload.get("dry_run")),
                    )
                )
                return
            if path == "/api/locate":
                self._json(
                    locate_target(
                        str(payload.get("id") or ""),
                        str(payload.get("path") or ""),
                        str(payload.get("kind") or "route"),
                    )
                )
                return
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
            return
        except Exception as exc:  # noqa: BLE001 —— 面板不能因为一次请求炸掉
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        self._json({"error": "not found"}, 404)


def main() -> int:
    parser = argparse.ArgumentParser(description="agent_bridge 本地控制台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost"):
        print("只允许绑 127.0.0.1：这个面板能真派发任务，别暴露到网络上。", file=sys.stderr)
        return 2

    threading.Thread(target=_warm_versions, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    ready = len([row for row in agent_rows() if row["ready"]])
    print(f"agent_bridge 控制台  {url}")
    print(f"路由 {len(AGENT_SPECS)} 条，就绪 {ready} 条  ·  Ctrl+C 停止")
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
