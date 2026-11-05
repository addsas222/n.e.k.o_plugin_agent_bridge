"""``python -m bridge.sentinel_core`` —— 独立起一个本地哨兵。

参数
====

``--host``     绑定地址，默认 ``127.0.0.1``，只允许回环。
``--port``     端口，默认 ``8787``。
``--rules``    规则配置 JSON 文件，可选。
``--quiet``    不开 INFO 日志。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .server import VERSION, build_app, serve


def _load_rules(path: str) -> dict:
    """读规则配置。

    :param path: JSON 文件路径；空串表示不读。
    :return: 配置字典。
    """
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"规则配置读取失败（改用内置规则）：{type(exc).__name__}: {exc}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    :param argv: 参数列表；``None`` 表示用 ``sys.argv[1:]``。
    :return: 退出码。
    """
    parser = argparse.ArgumentParser(
        prog="python -m bridge.sentinel_core", description="本地 L1 安全预检服务"
    )
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（只允许回环）")
    parser.add_argument("--port", type=int, default=8787, help="监听端口")
    parser.add_argument("--rules", default="", help="规则配置 JSON 路径")
    parser.add_argument("--quiet", action="store_true", help="不输出 INFO 日志")
    # 显式传 argv，否则 --port 会被静默忽略（上游 v5.0.2 的这个坑别重犯）
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = _load_rules(args.rules)
    app = build_app(config, version=VERSION)
    try:
        server = serve(args.host, args.port, app=app)
    except ValueError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"端口不可用：{exc}", file=sys.stderr)
        return 3

    host, port = server.server_address[:2]
    print(f"哨兵已启动 http://{host}:{port}  version={VERSION}")
    print("  GET  /api/health    POST /api/pre-check    POST /api/attach    GET /api/state")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，正在停止……")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
