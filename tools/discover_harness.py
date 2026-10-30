#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫用户目录，找出「已安装但还没进 frameworks.toml」的 Agent harness。

    python tools/discover_harness.py            # 候选表
    python tools/discover_harness.py --json
    python tools/discover_harness.py --all      # 连已登记的也列出来

为什么需要它：harness 的配置目录散在三个地方，人工 `ls` 漏过两次 ——
  1. 家目录的隐藏目录（`~/.cline`、`~/.kimi-code`、`~/.zcode` …）；
  2. `%LOCALAPPDATA%\\Programs\\<App>\\<App>.exe`（桌面 App 不写 PATH）；
  3. **多用户目录/多盘**：本机 `C:\\Users\\x` 与 `E:\\Users\\x` 同时存在，
     只扫 `~` 会漏掉一半。

判据：一个目录里出现 `config.toml` / `settings.json` / `mcp*.json` / `skills/`
之一，才当作候选；只有 `releases/`、`cache/` 这种不当。
"""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "skill" / "neko-bridge" / "frameworks.toml"

MARKERS = ("config.toml", "settings.json", "auth.json", "credentials")
MARKER_GLOBS = ("mcp*.json", "*.jsonc")
SKILL_DIRS = ("skills", "skill_pool")
#: 明显不是 Agent 的（配置切换器 / 记忆层 / 运行时 / 缓存）。
NOT_HARNESS = {"cc-switch", "hindsight", "agentscope-runtime", "eigenflux", "mem0",
               "neko-agent-bridge", "playwright-mcp", "vscode", "vscode-shared",
               # EvoX 的子工具，配置在 .evolver；宿主本身已由 .evox 覆盖
               "evolver",
               # Oh my Opencode —— OpenCode 的插件配置（omo.jsonc），不是独立 harness
               "omo",
               "bun", "cargo", "rustup", "docker", "npm-cache", "node_modules"}

#: 目录名/App 名与登记 id 对不上时的换算（否则每次扫描都报同一批假阳性）。
ID_ALIASES = {
    "qoder-ide": "qoder",
    "pen": "pencil",
    "microsoft-vs-code": "vscode",
    "n.e.k.o.-launcher": "neko",
}


def registered() -> set[str]:
    with open(MATRIX, "rb") as handle:
        return {row["id"] for row in tomllib.load(handle)["harness"]}


def homes() -> list[Path]:
    """所有存在的用户目录（C: / E: …），去重。"""
    found: list[Path] = []
    for drive in "CDEFG":
        candidate = Path(f"{drive}:/Users/{Path.home().name}")
        if Path(f"{drive}:/").exists() and candidate.is_dir() and candidate not in found:
            found.append(candidate)
    return found or [Path.home()]


def probe(directory: Path) -> dict | None:
    """判断一个目录是不是 harness 的配置根，返回证据。"""
    # 统一小写：`Doubao` 与登记表里的 `doubao` 是同一个东西，
    # 不归一化会让扫描器天天报同一个假阳性。
    name = directory.name.lstrip(".").lower()
    evidence: list[str] = []
    for marker in MARKERS:
        if (directory / marker).is_file():
            evidence.append(marker)
    for pattern in MARKER_GLOBS:
        for path in directory.glob(pattern):
            if path.is_file():
                evidence.append(path.name)
                break
    for skills in SKILL_DIRS:
        if (directory / skills).is_dir():
            evidence.append(f"{skills}/")
    if not evidence:
        return None
    return {"id": ID_ALIASES.get(name, name), "dir": str(directory), "evidence": sorted(set(evidence))}


def scan(*, include_registered: bool) -> list[dict]:
    known = registered()
    rows: list[dict] = []
    seen: set[str] = set()

    for home in homes():
        for entry in sorted(home.iterdir()):
            if not entry.is_dir() or entry.name in ("AppData", "Application Data", "Local Settings"):
                continue
            if entry.name.lstrip(".") in NOT_HARNESS:
                continue
            info = probe(entry)
            if info and info["id"] not in seen:
                seen.add(info["id"])
                rows.append({**info, "kind": "config-dir", "registered": info["id"] in known})

        programs = home / "AppData" / "Local" / "Programs"
        if programs.is_dir():
            for app in sorted(programs.iterdir()):
                if not app.is_dir() or app.name.startswith("."):
                    continue
                exes = [p.name for p in app.glob("*.exe") if not p.name.lower().startswith("uninstall")]
                if not exes:
                    continue
                key = app.name.lower().replace(" ", "-")
                key = ID_ALIASES.get(key, key)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({"id": key, "dir": str(app), "evidence": exes[:2],
                             "kind": "desktop-app", "registered": key in known})

    return rows if include_registered else [row for row in rows if not row["registered"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发现未登记的 harness")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--all", action="store_true", help="包含已登记项")
    args = parser.parse_args(argv)

    rows = scan(include_registered=args.all)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print(f"扫描的用户目录：{', '.join(str(path) for path in homes())}")
    print(f"已登记：{len(registered())} 个框架\n")
    if not rows:
        print("没有发现未登记的候选。")
        return 0
    print(f"{'id':20} {'类型':12} 证据 / 位置")
    for row in rows:
        print(f"{row['id']:20} {row['kind']:12} {', '.join(row['evidence'])}  ← {row['dir']}")
    print(f"\n{len(rows)} 个候选。确认是 Agent 的，就加进 skill/neko-bridge/frameworks.toml。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
