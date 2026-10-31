#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 ``neko-bridge`` 装进各个 Agent 框架（CLI 与 IDE 都算），并注册 MCP。

矩阵在 ``frameworks.toml`` —— 那是唯一需要维护的地方。本文件只是它的解释器：
同一份 skill 拷到每个框架的 skills 目录，再按该框架自己的方式注册 MCP。

用法
----
    python install.py                      # 列出每个框架的现状（默认 dry-run）
    python install.py --apply              # 真的装（只写 <skills_dir>/neko-bridge/）
    python install.py --apply --only claude,trae
    python install.py --mcp                # 只打印各框架的 MCP 注册方式
    python install.py --mcp --run          # CLI 形态的直接执行注册命令
    python install.py --home /tmp/x        # 换 home（测试用）

设计约束（与上游一致）
----------------------
* **只增不改**：只创建 ``<skills_dir>/neko-bridge/``，不碰该框架的其它配置；
  MCP 的 file 形态**只打印片段**，不替用户改 ``mcp.json``（那是他的配置文件）。
* **不发明目录**：skills 目录不存在就标 ``missing`` 跳过，不替某个 IDE 造结构。
* **备份目录跳过**：带「副本 / copy / backup」字样的展开结果不装。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_NAME = "neko-bridge"
HERE = Path(__file__).resolve().parent
MATRIX = HERE / "frameworks.toml"
PAYLOAD = ("SKILL.md", "README.md", "bin", "providers")
SKIP_MARKERS = ("副本", "copy", "backup", "bak")


def _force_utf8_stdout() -> None:
    """Windows 控制台默认不是 UTF-8（GitHub Actions 上是 cp1252）。

    脚本会打印中文，不重配的话 `print()` 直接 UnicodeEncodeError 退出 ——
    这不是编码洁癖，是会让 `--check` 这类命令在 Windows 上整条挂掉。
    """
    stream = getattr(sys, "stdout", None)
    if stream is not None and hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):  # 管道已关闭等极端情况，忽略
            pass


def load_matrix() -> list[dict]:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.9/3.10
        print("需要 Python 3.11+（tomllib）读取 frameworks.toml", file=sys.stderr)
        raise SystemExit(2)
    with open(MATRIX, "rb") as handle:
        return list(tomllib.load(handle).get("harness", []))


def expand(template: str, home: Path) -> list[Path]:
    """把 ``~`` 与 ``*`` 展开成实际目录。``*`` 只展开一级。"""
    raw = template.replace("~", str(home))
    if "*" not in raw:
        return [Path(raw)]
    parent, _, tail = raw.partition("*/")
    base = Path(parent.rstrip("/\\"))
    if not base.is_dir():
        return []
    return sorted(
        path / tail
        for path in base.iterdir()
        if path.is_dir() and not any(marker in path.name.lower() for marker in SKIP_MARKERS)
    )


def state_of(directory: Path) -> str:
    if not directory.is_dir():
        return "missing"
    if (directory / SKILL_NAME / "SKILL.md").is_file():
        return "installed"
    return "ready"


def install(directory: Path, *, force: bool) -> list[str]:
    target = directory / SKILL_NAME
    written: list[str] = []
    target.mkdir(parents=True, exist_ok=True)
    for name in PAYLOAD:
        source = HERE / name
        if not source.exists():
            continue
        destination = target / name
        if destination.exists() and not force:
            continue
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination)
        written.append(name)
    entry = target / "bin" / "neko.py"
    if entry.is_file():
        entry.chmod(0o755)
    return written


def mcp_line(row: dict, binary: str) -> str:
    kind = row.get("mcp_kind", "none")
    if kind == "cli":
        return row.get("mcp_command", "").replace("{name}", SKILL_NAME).replace("{bin}", binary)
    if kind == "file":
        return f"[file] 往 {row.get('mcp_target', '?')} 的 mcpServers 里加："
    return "[none] 该框架没有稳定的 MCP 注册方式；直接用它加载 skill 即可"


def generic_mcp_json(binary: str) -> str:
    return json.dumps(
        {"mcpServers": {SKILL_NAME: {"command": "python", "args": [binary, "mcp"]}}},
        indent=2,
        ensure_ascii=False,
    )


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(prog="install-neko-bridge", description="把 neko-bridge 装到各 Agent 框架")
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--mcp", action="store_true", help="打印各框架的 MCP 注册方式")
    parser.add_argument("--run", action="store_true", help="配合 --mcp：直接执行 CLI 形态的注册命令")
    args = parser.parse_args(argv)

    home = Path(args.home).expanduser()
    matrix = load_matrix()
    wanted = {item.strip() for item in args.only.split(",") if item.strip()}
    binary = str((HERE / "bin" / "neko.py").resolve())

    selected = [row for row in matrix if not wanted or row["id"] in wanted]
    rows: list[tuple[dict, Path, str]] = []
    for row in selected:
        template = row.get("skills_dir") or ""
        if not template:
            # 没有 skills 机制的框架（aider / zed / jules）只参与 MCP 与说明，不装目录。
            # 不能把空串喂给 expand()——那会退化成 cwd，等于往当前目录乱写。
            continue
        expanded = expand(template, home) or [Path(template.replace("~", str(home)))]
        for directory in expanded:
            rows.append((row, directory, state_of(directory)))

    if args.mcp:
        for row in selected:
            print(f"\n# {row['id']}  ({row['label']})")
            if not row.get("skills_dir"):
                print("[no-skills] 该框架没有 skills 目录机制；只用 MCP / AGENTS.md")
            print(mcp_line(row, binary))
            if row.get("mcp_kind") == "file":
                print(generic_mcp_json(binary))
            if args.run and row.get("mcp_kind") == "cli":
                command = mcp_line(row, binary)
                print(f"$ {command}")
                # command 来自 frameworks.toml 的 mcp_command，不是用户输入
                result = subprocess.run(command, shell=True, capture_output=True, text=True, encoding="utf-8")
                print(f"  → rc={result.returncode} {(result.stdout or result.stderr).strip()[:160]}")
        return 0

    print(f"skill 源：{HERE}")
    print(f"矩阵：{MATRIX}（{len(matrix)} 个框架）\n")
    print(f"{'id':13} {'kind':7} {'状态':10} 目录")
    for row, directory, state in rows:
        flag = "" if row.get("source") == "local" else "  (文档来源,未验证)"
        print(f"{row['id']:13} {row.get('kind', '?'):7} {state:10} {directory}{flag}")

    tally: dict[str, int] = {}
    for _row, _directory, state in rows:
        tally[state] = tally.get(state, 0) + 1
    print("\n" + " · ".join(f"{k} {v}" for k, v in sorted(tally.items())))

    if args.list or not args.apply:
        print("\n这是 dry-run。真的安装：--apply；看 MCP 注册：--mcp")
        return 0

    total = 0
    for row, directory, state in rows:
        if state == "missing":
            continue
        written = install(directory, force=args.force)
        total += len(written)
        verb = "更新" if state == "installed" else "安装"
        print(f"{verb} {directory / SKILL_NAME}: {', '.join(written) if written else '（已是最新）'}")
    print(f"\n完成：{total} 个文件。自检：python \"{HERE / 'bin' / 'neko.py'}\" agents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
