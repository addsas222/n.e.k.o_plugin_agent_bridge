#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对全部登记 Agent 真跑一条最小任务，输出就绪矩阵。

    python tools/probe_routes.py                 # 表格
    python tools/probe_routes.py --json          # 机器可读
    python tools/probe_routes.py --only gemini,pi
    python tools/probe_routes.py --task "..."    # 换任务（默认只让它回一个字）

它派的是**真进程**，会花额度，所以不进 pytest（pytest 里那份是 `NEKO_TEST_LIVE_CLI=1` 才跑）。
用途：接入新 Agent 之后，一次看清哪些真能干活、哪些只是装上了没登录。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from bridge.config import BridgeConfig
from bridge.registry import AGENT_SPECS, resolve_agent
from bridge.runner import run_agent  # noqa: F401


def one(agent_id: str, task: str, timeout: int) -> dict:
    spec = AGENT_SPECS[agent_id]
    resolved = resolve_agent(spec)
    row: dict = {"agent": agent_id, "resolved": resolved.source, "exists": resolved.exists}
    if not resolved.exists:
        row |= {"status": "MISSING", "exit_code": None, "seconds": 0.0, "detail": resolved.detail}
        return row

    config = BridgeConfig()
    result = asyncio.run(run_agent(spec, resolved, task, config=config, timeout=timeout))
    if result.needs_login:
        status = "NEEDS_LOGIN"
    elif result.ok:
        status = "OK"
    elif result.timed_out:
        status = "TIMEOUT"
    else:
        status = "FAILED"
    row |= {
        "status": status,
        "exit_code": result.exit_code,
        "seconds": round(result.duration_ms / 1000, 1),
        "detail": (result.error or result.output_text().strip().splitlines()[0][:80]) if not result.ok else "",
        "cmd": result.command_preview,
    }
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument("--task", default="只回一个字：好")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--jobs", type=int, default=6, help="并发进程数，别设太大（会同时烧额度）")
    args = parser.parse_args()

    wanted = [item.strip() for item in args.only.split(",") if item.strip()] or sorted(AGENT_SPECS)
    unknown = [item for item in wanted if item not in AGENT_SPECS]
    if unknown:
        print(f"未知 Agent：{', '.join(unknown)}", file=sys.stderr)
        return 2

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        rows = list(pool.map(lambda a: one(a, args.task, args.timeout), wanted))

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print(f"{'agent':14} {'status':12} {'exit':>5} {'秒':>6}  {'source':11} detail")
    for row in rows:
        print(
            f"{row['agent']:14} {row['status']:12} {str(row['exit_code']):>5} "
            f"{row['seconds']:>6}  {row['resolved']:11} {row['detail'][:60]}"
        )
    tally: dict[str, int] = {}
    for row in rows:
        tally[row["status"]] = tally.get(row["status"], 0) + 1
    print("\n" + " · ".join(f"{key} {value}" for key, value in sorted(tally.items()))
          + f" · 总耗时 {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
