#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 agent_bridge 打包成桌面插件 ``.mcpb``（MCP Bundle）。

为什么是 .mcpb
--------------
桌面 harness（Claude Desktop，以及任何支持 MCPB 的客户端）认的"插件"就是这个：
一个 zip，里面必须有 ``manifest.json``，其余是 server 代码。Anthropic 2025-06 发布，
原扩展名 ``.dxt`` 2025-09 改名 ``.mcpb``，格式未变。

用户视角的差别：改 JSON + 装 Python 依赖 → 双击文件 + 点 Install。
这正是"插件先行"要的效果。

    python tools/build_mcpb.py                 # 产出 dist/agent_bridge-<ver>.mcpb
    python tools/build_mcpb.py --check         # 只校验产物结构，不写
    python tools/build_mcpb.py --print-manifest

诚实边界
--------
* 服务端是 **Python**（本仓库零第三方依赖，但需要系统有 python 可执行）。
  Claude Desktop 自带的是 Node 运行时，不是 Python —— 所以这里声明
  ``server.type = "python"``，并在 README 里写明前置条件，不假装零依赖。
* 不做签名/公证；官方签名要 Anthropic 的密钥。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER_SRC = ROOT / "skill" / "neko-bridge" / "bin" / "neko.py"
PROVIDERS_SRC = ROOT / "skill" / "neko-bridge" / "providers"
DIST = ROOT / "dist"

NAME = "agent_bridge"
VERSION = "0.5.0"


def manifest() -> dict:
    return {
        "mcpb_version": "0.1",
        "name": NAME,
        "version": VERSION,
        "description": "本机 Agent 桥接：把 CodeBuddy / Claude Code / Gemini / omp / OpenCode 等 12 个编码 Agent 变成可调用工具。",
        "author": {"name": "local"},
        "server": {
            "type": "python",
            "entry_point": "server/neko.py",
            "mcp_config": {
                "command": "python",
                # ${__dirname} 由客户端替换成扩展解压目录（MCPB 规范内建占位符）
                "args": ["${__dirname}/server/neko.py", "mcp"],
                "env": {"NEKO_BRIDGE_PROVIDERS": "${__dirname}/server/providers"},
            },
        },
    }


def build(out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest(), ensure_ascii=False, indent=2))
        bundle.write(SERVER_SRC, "server/neko.py")
        bundle.writestr("README.md", "见 manifest.json 与 https://github.com/addsas222/n.e.k.o_plugin_agent_bridge\n")
        if PROVIDERS_SRC.is_dir():
            for path in sorted(PROVIDERS_SRC.rglob("*")):
                if path.is_file():
                    bundle.write(path, f"server/providers/{path.relative_to(PROVIDERS_SRC).as_posix()}")
    return out


def check(bundle_path: Path) -> list[str]:
    """结构自检。返回问题列表，空表示通过。

    官方校验器是 ``npx @anthropic-ai/mcpb validate``；这里用标准库把**必需字段**
    与 zip 布局验一遍，够挡住"打包漏文件"这类低级错误。
    """
    problems: list[str] = []
    if not bundle_path.is_file():
        return [f"产物不存在：{bundle_path}"]
    with zipfile.ZipFile(bundle_path) as bundle:
        names = set(bundle.namelist())
        if "manifest.json" not in names:
            problems.append("缺 manifest.json")
        else:
            data = json.loads(bundle.read("manifest.json"))
            for key in ("mcpb_version", "name", "version", "description", "author", "server"):
                if key not in data:
                    problems.append(f"manifest 缺字段 {key}")
            server = data.get("server", {})
            if server.get("type") not in ("node", "python", "binary"):
                problems.append(f"server.type 非法：{server.get('type')!r}")
            entry = server.get("entry_point", "")
            if entry and entry not in names:
                problems.append(f"entry_point {entry!r} 不在包里")
            mcp = server.get("mcp_config", {})
            if "command" not in mcp or "args" not in mcp:
                problems.append("server.mcp_config 缺 command/args")
        if "server/neko.py" not in names:
            problems.append("缺 server/neko.py")
        if not any(n.startswith("server/providers/") for n in names):
            problems.append("缺 server/providers/（skill 会退化成只有内置 9 个 Agent）")
        if bundle.testzip() is not None:
            problems.append("zip CRC 校验失败")
    return problems


def smoke(bundle_path: Path, *, timeout: float = 30.0) -> list[str]:
    """解包并**真启动**一次 MCP 服务，走 initialize → tools/list。

    为什么必须做：``check()`` 只验结构，一个能通过结构校验却起不来的包，
    装进客户端后表现是「扩展已安装，但没有任何工具」—— 比直接报错更难查。
    这里完全按 manifest 声明的 command/args 启动（含 ``${__dirname}`` 替换），
    和客户端的启动路径一致。
    """
    import subprocess
    import tempfile

    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mcpb-smoke-") as tmp:
        with zipfile.ZipFile(bundle_path) as bundle:
            bundle.extractall(tmp)
        data = json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
        mcp = data["server"]["mcp_config"]

        def subst(value: str) -> str:
            return value.replace("${__dirname}", tmp).replace("${__dirname_}", tmp)

        argv = [subst(mcp["command"]), *(subst(str(a)) for a in mcp["args"])]
        env = dict(os.environ)
        for key, value in (mcp.get("env") or {}).items():
            env[str(key)] = subst(str(value))

        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
        except OSError as exc:
            return [f"无法启动 {argv[0]!r}：{exc}"]

        def send(payload: dict) -> None:
            assert process.stdin is not None
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()

        def read_json(wanted_id: int) -> dict | None:
            """读到 id 匹配的那条为止（服务端可能先吐日志行）。"""
            assert process.stdout is not None
            deadline = time.time() + timeout
            while time.time() < deadline:
                line = process.stdout.readline()
                if not line:
                    return None
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("id") == wanted_id:
                    return payload
            return None

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "mcpb-smoke", "version": "0"}}})
            init = read_json(1)
            if init is None:
                problems.append("initialize 无响应（超时或进程退出）")
            elif "error" in init:
                problems.append(f"initialize 报错：{init['error']}")
            else:
                send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                listed = read_json(2)
                if listed is None:
                    problems.append("tools/list 无响应")
                elif "error" in listed:
                    problems.append(f"tools/list 报错：{listed['error']}")
                elif not listed.get("result", {}).get("tools"):
                    problems.append("tools/list 返回空工具集")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包 .mcpb 桌面插件")
    parser.add_argument("--out", default=str(DIST / f"{NAME}-{VERSION}.mcpb"))
    parser.add_argument("--check", action="store_true", help="只校验已有产物")
    parser.add_argument("--smoke", action="store_true", help="解包并真启动一次 MCP 服务")
    parser.add_argument("--print-manifest", action="store_true")
    args = parser.parse_args(argv)

    if args.print_manifest:
        print(json.dumps(manifest(), ensure_ascii=False, indent=2))
        return 0

    target = Path(args.out)
    if not args.check:
        if not SERVER_SRC.is_file():
            print(f"找不到 server 源：{SERVER_SRC}", file=sys.stderr)
            return 2
        build(target)
        print(f"已生成 {target}  ({target.stat().st_size / 1024:.1f} KiB)")

    problems = check(target)
    if problems:
        print("校验失败：", file=sys.stderr)
        for item in problems:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print(f"校验通过：{target.name}（manifest + server + providers 齐全）")

    if args.smoke:
        issues = smoke(target)
        if issues:
            print("冒烟失败：", file=sys.stderr)
            for item in issues:
                print(f"  - {item}", file=sys.stderr)
            return 1
        print("冒烟通过：MCP initialize + tools/list 正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
