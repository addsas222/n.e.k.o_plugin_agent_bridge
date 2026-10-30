#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读清点：已安装软件 + 所有用户目录，找出「本机有哪些 Agent harness」。

    python tools/inventory.py                 # 写 docs/harness-inventory.md
    python tools/inventory.py --json          # 打到 stdout
    python tools/inventory.py --stdout        # 只打印 markdown

约束：**全程只读**。不装、不删、不改任何软件或配置；唯一的写操作是生成报告，
可以用 --stdout 完全避免落盘。

数据来源
--------
1. 注册表卸载项（HKLM 64/32 位 + HKCU）—— "已安装软件"的权威口径；
2. `Program Files` / `%LOCALAPPDATA%\\Programs`（C: 与 E: 都扫）—— 便携版不写注册表；
3. 每个盘的用户目录顶层（含隐藏）—— Agent 的配置通常在 `~/.<name>`；
4. `bun` / `npm` 全局包 —— CLI 型 Agent 的实际安装方式。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "skill" / "neko-bridge" / "frameworks.toml"
REPORT = ROOT / "docs" / "harness-inventory.md"

#: 名称关键词 → 规范 id。命中即认为这是 Agent harness 相关软件。
KEYWORDS: dict[str, str] = {
    "claude": "claude", "codex": "codex", "commandcode": "commandcode",
    "command-code": "commandcode", "dsh": "dsh", "gemini": "gemini", "codebuddy": "codebuddy",
    "cursor": "cursor", "windsurf": "devin-desktop", "devin": "devin-desktop",
    "trae": "trae", "qoder": "qoder", "kiro": "kiro", "antigravity": "antigravity",
    "cline": "cline", "roo": "roo-code", "kilo": "kilo-code", "continue": "continue-cli",
    "copilot": "copilot", "aider": "aider", "openhands": "openhands", "crush": "crush",
    "goose": "goose", "amp": "amp", "droid": "droid", "factory": "droid", "warp": "warp",
    "junie": "junie", "mistral": "mistral-vibe", "kimi": "kimi-code", "zcode": "zcode",
    "minimax": "minimax-code", "step": "stepcode", "deepseek": "deepseek-harness",
    "qwen": "qwenpaw", "opencode": "opencode", "oh-my-pi": "omp", "omp": "omp",
    "evox": "evox", "atomcode": "atomcode", "openclaw": "openclaw", "openfang": "openfang",
    "pen": "pencil", "pencil": "pencil", "doubao": "doubao", "workbuddy": "workbuddy",
    "obsidian": "obsidian", "notion": "notion", "ollama": "ollama", "n.e.k.o": "neko",
    "hermes": "hermes", "codebuff": "freebuff", "astrbot": "astrbot",
}

#: 明显不是 Agent 的（命中关键词但属工具/编辑器）。
NOT_AGENT = {"obsidian", "notion", "ollama", "neko"}


def read_registry() -> list[dict]:
    """读卸载表。只读，失败就返回空（非 Windows 或权限不足）。"""
    try:
        import winreg
    except ImportError:
        return []
    rows: list[dict] = []
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", 0),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0),
    ]
    for hive, path, view in roots:
        try:
            key = winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        for index in range(winreg.QueryInfoKey(key)[0]):
            try:
                name = winreg.EnumKey(key, index)
                sub = winreg.OpenKey(key, name)
                display = winreg.QueryValueEx(sub, "DisplayName")[0]
                version = ""
                location = ""
                try:
                    version = winreg.QueryValueEx(sub, "DisplayVersion")[0]
                except OSError:
                    pass
                try:
                    location = winreg.QueryValueEx(sub, "InstallLocation")[0]
                except OSError:
                    pass
                rows.append({"name": str(display), "version": str(version), "location": str(location)})
            except OSError:
                continue
    return sorted(rows, key=lambda r: r["name"].lower())


def program_dirs() -> list[Path]:
    bases = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
    ]
    for drive in "CDEFG":
        bases.append(Path(f"{drive}:/Users/{Path.home().name}/AppData/Local/Programs"))
    seen: list[Path] = []
    for base in bases:
        if base and base.is_dir() and base not in seen:
            seen.append(base)
    out: list[Path] = []
    for base in seen:
        for entry in sorted(base.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                out.append(entry)
    return out


def user_homes() -> list[Path]:
    homes: list[Path] = []
    for drive in "CDEFG":
        candidate = Path(f"{drive}:/Users/{Path.home().name}")
        if Path(f"{drive}:/").exists() and candidate.is_dir() and candidate not in homes:
            homes.append(candidate)
    return homes or [Path.home()]


def package_managers() -> list[str]:
    out: list[str] = []
    for cmd in (["bun", "pm", "ls", "-g"], ["npm", "ls", "-g", "--depth=0"]):
        exe = shutil.which(cmd[0])
        if not exe:
            continue
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=60)
            for line in (proc.stdout or "").splitlines():
                text = line.strip().lstrip("├└─│ ")
                if "@" in text and not text.startswith("("):
                    out.append(f"{cmd[0]}: {text}")
        except (OSError, subprocess.SubprocessError):
            continue
    return out


def classify(label: str) -> str:
    """按**词边界**匹配关键词。

    不能用 `in`：`omp` 会命中 "Compatibility"、`pen` 会命中 "Open Design"、
    `droid` 会命中 "Android" —— 上一版就是这么把三个无关软件错认成 harness 的。
    """
    low = label.lower()
    for keyword, harness_id in KEYWORDS.items():
        if harness_id in NOT_AGENT:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])"
        if re.search(pattern, low):
            return harness_id
    return ""


def registered() -> dict[str, dict]:
    with open(MATRIX, "rb") as handle:
        return {row["id"]: row for row in tomllib.load(handle)["harness"]}


def build() -> dict:
    known = registered()
    software = read_registry()
    programs = program_dirs()
    homes = user_homes()

    hits: dict[str, list[str]] = {}
    others: dict[str, list[str]] = {}
    for item in software:
        harness_id = classify(item["name"])
        if harness_id:
            hits.setdefault(harness_id, []).append(f"registry: {item['name']} {item['version']}".strip())
        else:
            low = item["name"].lower()
            for keyword, other_id in KEYWORDS.items():
                if other_id in NOT_AGENT and re.search(
                    r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])", low
                ):
                    others.setdefault(other_id, []).append(item["name"])
                    break
    for entry in programs:
        harness_id = classify(entry.name)
        if harness_id:
            hits.setdefault(harness_id, []).append(f"dir: {entry}")

    home_dirs: list[dict] = []
    for home in homes:
        try:
            entries = sorted(p.name for p in home.iterdir() if p.is_dir())
        except OSError:
            continue
        for name in entries:
            if name in ("AppData", "Application Data", "Local Settings", "Documents", "Downloads"):
                continue
            harness_id = classify(name.lstrip("."))
            home_dirs.append({"home": str(home), "dir": name, "harness": harness_id})

    return {
        "software": software,
        "programs": [str(p) for p in programs],
        "homes": [str(h) for h in homes],
        "home_dirs": home_dirs,
        "packages": package_managers(),
        "hits": hits,
        "others": others,
        "known": sorted(known),
        "not_agent": sorted(NOT_AGENT),
    }


def to_markdown(data: dict) -> str:
    known = set(data["known"])
    hits = data["hits"]
    lines: list[str] = []
    lines.append("# 本机 Agent Harness 清点（只读）\n")
    lines.append("> 由 `tools/inventory.py` 生成。全程只读：不装、不删、不改任何东西。\n")
    lines.append(f"- 用户目录：{', '.join(data['homes'])}")
    lines.append(f"- 注册表已安装项：{len(data['software'])} 条")
    lines.append(f"- 程序目录：{len(data['programs'])} 个")
    lines.append(f"- 矩阵已登记：{len(known)} 个框架\n")

    lines.append("## 一、识别出的 Agent harness\n")
    lines.append("| harness | 已登记 | 证据 |")
    lines.append("|---|---|---|")
    for harness_id in sorted(hits):
        evidence = "<br>".join(hits[harness_id][:3])
        mark = "✅" if harness_id in known else "❌ **未登记**"
        lines.append(f"| `{harness_id}` | {mark} | {evidence} |")
    missing = sorted(set(hits) - known - set(data["not_agent"]))
    lines.append("")
    if missing:
        lines.append(f"**缺口**：{', '.join('`' + m + '`' for m in missing)} —— 见了但矩阵里没有。\n")
    else:
        lines.append("**无缺口**：识别出的都已在矩阵里（或明确归为非 Agent）。\n")

    lines.append("## 二、非 Agent（命中关键词但属工具/编辑器/运行时）\n")
    for other_id in sorted(data.get("others", {})):
        names = ", ".join(sorted(set(data["others"][other_id]))[:4])
        lines.append(f"- `{other_id}` ← {names}")
    lines.append("")

    lines.append("## 三、用户目录里的隐藏配置目录\n")
    lines.append("| 盘 | 目录 | 判定 |")
    lines.append("|---|---|---|")
    for row in data["home_dirs"]:
        if not row["dir"].startswith("."):
            continue
        who = f"`{row['harness']}`" if row["harness"] else "—"
        lines.append(f"| {row['home']} | `{row['dir']}` | {who} |")
    lines.append("")

    lines.append("## 四、包管理器全局安装\n")
    if data["packages"]:
        lines.append("```")
        lines.extend(data["packages"])
        lines.append("```")
    else:
        lines.append("（bun / npm 不可用）")
    lines.append("")

    lines.append("## 五、程序目录清单\n")
    for path in data["programs"]:
        harness_id = classify(Path(path).name)
        lines.append(f"- `{path}`" + (f"  → `{harness_id}`" if harness_id else ""))
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读清点本机 harness")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--stdout", action="store_true", help="只打印，不写文件")
    args = parser.parse_args(argv)

    data = build()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    text = to_markdown(data)
    if args.stdout:
        print(text)
        return 0
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(text, encoding="utf-8")
    print(f"已写入 {REPORT}（{len(text)} 字符）")
    print(f"识别 harness {len(data['hits'])} 个，矩阵已登记 {len(data['known'])} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
