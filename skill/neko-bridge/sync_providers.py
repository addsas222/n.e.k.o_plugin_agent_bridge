#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把仓库根的 ``providers/`` 同步成 skill 自带副本 ``skill/neko-bridge/providers/``。

为什么需要这一步
----------------
``providers/<id>/provider.toml`` 是路由的**唯一手改源**。但 skill 会被
``install.py`` 拷进某个 Agent 的 skills 目录，那时 ``../../providers`` 已经不在
它旁边了 —— 所以 skill 必须自带一份副本。

副本是**产物**，不是第二个源：只由本脚本生成，谁都不手改。``--check`` 用来在
CI / 提交前确认副本没漂。这也是对旧结构（两张手抄的 AGENT_SPECS）的正面修正。

用法
----
    python sync_providers.py --check     # 只校验（不一致退出码 2）
    python sync_providers.py --apply     # 真的同步
    python sync_providers.py --list      # 列出会同步哪些文件
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
SOURCE = SKILL_DIR.parents[1] / "providers"      # <repo>/providers
TARGET = SKILL_DIR / "providers"

#: 只同步这些文件名（``SKILL.md`` 是给 Agent 看的说明，一并带过去）。
PAYLOAD = ("provider.toml", "SKILL.md", "README.md")


def wanted_files() -> list[tuple[Path, Path]]:
    """返回 (源, 目标) 对，按 provider id 排序。"""
    pairs: list[tuple[Path, Path]] = []
    if not SOURCE.is_dir():
        return pairs
    for directory in sorted(SOURCE.iterdir()):
        if not directory.is_dir() or directory.name.startswith((".", "_")):
            continue
        manifest = directory / "provider.toml"
        if not manifest.is_file():
            continue
        for name in PAYLOAD:
            source = directory / name
            if source.is_file():
                pairs.append((source, TARGET / directory.name / name))
    return pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sync-providers", description="同步 providers/ 到 skill 副本")
    parser.add_argument("--check", action="store_true", help="只校验，不一致返回退出码 2")
    parser.add_argument("--apply", action="store_true", help="真的写入")
    parser.add_argument("--list", action="store_true", help="列出将同步的文件")
    args = parser.parse_args(argv)

    if not SOURCE.is_dir():
        print(f"源目录不存在：{SOURCE}", file=sys.stderr)
        return 2

    pairs = wanted_files()
    if args.list:
        for source, target in pairs:
            print(f"{source.relative_to(SOURCE)}  ->  {target.relative_to(SKILL_DIR)}")
        print(f"共 {len(pairs)} 个文件")
        return 0

    stale: list[tuple[Path, Path]] = []
    for source, target in pairs:
        if not target.is_file() or not filecmp.cmp(source, target, shallow=False):
            stale.append((source, target))

    if args.check:
        if not stale:
            print(f"providers 副本与源一致（{len(pairs)} 个文件）")
            return 0
        print(f"providers 副本已漂移，{len(stale)} 个文件不一致：", file=sys.stderr)
        for source, _target in stale:
            print(f"  {source.relative_to(SOURCE)}", file=sys.stderr)
        print("跑 `python sync_providers.py --apply` 修好再提交。", file=sys.stderr)
        return 2

    if not args.apply:
        print(f"[dry-run] 将写入 {len(stale)} / {len(pairs)} 个文件。加 --apply 真的写。")
        return 0

    for source, target in stale:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        print(f"更新 {target.relative_to(SKILL_DIR)}")
    print(f"完成：{len(stale)} 个文件已同步（共 {len(pairs)} 个）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
