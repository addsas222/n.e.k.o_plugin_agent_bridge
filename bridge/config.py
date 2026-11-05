"""可选的外部配置。

默认零配置即可运行；如需覆盖（禁用某个 Agent、指定 exe 路径、调整超时、
开启审计日志），在下面任一位置放 config.json：

    %USERPROFILE%\\.neko-agent-bridge\\config.json
    <包目录>/../bridge.config.json

也可以用环境变量 NEKO_AGENT_BRIDGE_CONFIG 指向任意路径。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

_MSYS_PATH_RE = re.compile(r"^/([A-Za-z])/(.*)$")


def normalize_path(path: str) -> str:
    """把 MSYS / Git-Bash 风格路径转成 Windows 原生路径。

    `/c/Users/foo` → `C:\\Users\\foo`。

    为什么需要：从 Git Bash、MSYS 或 WSL 环境启动时，`PWD` 等环境变量里存的是这种
    形式的路径。Agent CLI 拿到它去做 chdir 会直接失败 —— 实测 opencode 报
    `Failed to change directory to /c/Users/...`。所以进入子进程前必须归一化。
    """
    text = str(path or "").strip().strip('"')
    if not text:
        return text
    match = _MSYS_PATH_RE.match(text)
    if match:
        rest = match.group(2).replace("/", "\\")
        return f"{match.group(1).upper()}:\\{rest}"
    return text


def _default_config_paths() -> list[str]:
    home = os.path.expanduser("~")
    package_dir = os.path.dirname(os.path.abspath(__file__))
    return [
        os.path.join(home, ".neko-agent-bridge", "config.json"),
        os.path.join(os.path.dirname(package_dir), "bridge.config.json"),
    ]


@dataclass
class AgentOverride:
    enabled: bool = True
    exe_override: str | None = None
    model: str | None = None
    timeout_seconds: int | None = None
    extra_args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class SentinelConfig:
    """安全审计（L1 派发前预检）的配置。

    默认走**内置后端**：插件自带一份精简哨兵（``bridge/sentinel_core``，MIT），
    在进程内直接判定，不起服务、不走网络。想对接外部的完整版 Sentinel 时，
    把 ``backend`` 设成 ``http`` 并给出 ``url``。

    **默认全部关闭** —— 未启用时派发行为完全不变。
    """

    enabled: bool = False
    """是否启用安全审计。关闭时派发路径零改动。"""
    backend: str = ""
    """后端选择：``builtin``（进程内，默认）/ ``http``（外部守护进程）。

    留空表示自动推断——配了 ``url`` 就走 http，否则用内置。
    """
    url: str = ""
    """外部守护进程地址（只允许本机回环）；仅 ``backend=http`` 时使用。"""
    block_on_deny: bool = True
    """预检判定为 block 时，是否真的拒绝派发；设为 false 则只记录不拦截。"""
    attach: bool = True
    """派发后是否把该路会话登记给审计层后台监控（best-effort）。"""
    timeout: float = 2.0
    """HTTP 后端的单次调用超时（秒）；不可达时按 fail-open 处理。"""
    block_severity: str = "standard"
    """达到哪个严重度才真的拦：``standard``（默认，含可恢复的高危操作）
    或 ``red_line``（只拦不可逆操作，``rm -rf ./dist`` 这类会放行）。"""


@dataclass
class BridgeConfig:
    default_cwd: str = ""
    default_timeout_seconds: int = 600
    max_timeout_seconds: int = 3600
    max_output_bytes: int = 400_000
    audit_log: str = ""
    allow_parallel: bool = True
    max_parallel: int = 4
    agents: dict[str, AgentOverride] = field(default_factory=dict)
    sentinel: SentinelConfig = field(default_factory=SentinelConfig)
    source_path: str = ""

    # ---------------------------------------------------------------- 读取

    def override_for(self, agent_id: str) -> AgentOverride:
        return self.agents.get(agent_id, AgentOverride())

    def is_enabled(self, agent_id: str) -> bool:
        return self.override_for(agent_id).enabled

    def clamp_timeout(self, value: Any) -> int:
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            seconds = self.default_timeout_seconds
        if seconds <= 0:
            seconds = self.default_timeout_seconds
        return min(seconds, self.max_timeout_seconds)

    def resolve_cwd(self, requested: str | None) -> str | None:
        """解析工作目录：显式请求优先，其次默认值；不存在则返回 None。"""
        for candidate in (requested, self.default_cwd):
            if not candidate:
                continue
            path = os.path.abspath(os.path.expanduser(normalize_path(str(candidate))))
            if os.path.isdir(path):
                return path
        return None

    # ---------------------------------------------------------------- 加载


def _as_str_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    return ()


def _as_str_map(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return {}


def config_from_dict(raw: Any, source_path: str = "") -> BridgeConfig:
    """从已解析的字典构建配置。

    MCP 服务端从 config.json 读，原生插件从它自己的 plugin.toml 读（TOML → dict 后
    结构一致），两边共用同一套解析与兜底规则，避免两处行为漂移。
    """
    config = BridgeConfig()
    config.source_path = source_path

    if not isinstance(raw, dict):
        return config

    if isinstance(raw.get("default_cwd"), str):
        config.default_cwd = raw["default_cwd"]
    if isinstance(raw.get("audit_log"), str):
        config.audit_log = raw["audit_log"]
    if isinstance(raw.get("allow_parallel"), bool):
        config.allow_parallel = raw["allow_parallel"]

    for attr, key in (
        ("default_timeout_seconds", "default_timeout_seconds"),
        ("max_timeout_seconds", "max_timeout_seconds"),
        ("max_output_bytes", "max_output_bytes"),
        ("max_parallel", "max_parallel"),
    ):
        value = raw.get(key)
        if isinstance(value, int) and value > 0:
            setattr(config, attr, value)

    agents_raw = raw.get("agents")
    if isinstance(agents_raw, dict):
        for agent_id, payload in agents_raw.items():
            if not isinstance(payload, dict):
                continue
            override = AgentOverride(
                enabled=bool(payload.get("enabled", True)),
                exe_override=str(payload["exe_override"]) if payload.get("exe_override") else None,
                model=str(payload["model"]) if payload.get("model") else None,
                timeout_seconds=(
                    int(payload["timeout_seconds"])
                    if isinstance(payload.get("timeout_seconds"), int)
                    else None
                ),
                extra_args=_as_str_list(payload.get("extra_args")),
                env=_as_str_map(payload.get("env")),
            )
            config.agents[str(agent_id)] = override

    # 哨兵安全审计配置：plugin.toml 里是嵌套的 [bridge.sentinel]（→ bridge 子表），
    # config.json 里也可能直接是顶层 [sentinel]。两边都认。
    sentinel_raw = raw.get("sentinel")
    if not isinstance(sentinel_raw, dict):
        sentinel_raw = (raw.get("bridge") or {}).get("sentinel")
    if isinstance(sentinel_raw, dict):
        config.sentinel = _sentinel_from_dict(sentinel_raw)

    return config


def _sentinel_from_dict(raw: dict[str, Any]) -> SentinelConfig:
    """从字典构建安全审计配置；任何字段缺失/非法都退回默认值，绝不因配置写坏而崩。"""
    cfg = SentinelConfig()
    if isinstance(raw.get("enabled"), bool):
        cfg.enabled = raw["enabled"]
    if isinstance(raw.get("backend"), str) and raw["backend"].strip():
        cfg.backend = raw["backend"].strip().lower()
    if isinstance(raw.get("url"), str) and raw["url"].strip():
        cfg.url = raw["url"].strip()
    if isinstance(raw.get("block_on_deny"), bool):
        cfg.block_on_deny = raw["block_on_deny"]
    if isinstance(raw.get("attach"), bool):
        cfg.attach = raw["attach"]
    if isinstance(raw.get("timeout"), (int, float)) and float(raw["timeout"]) > 0:
        cfg.timeout = float(raw["timeout"])
    severity = raw.get("block_severity")
    if isinstance(severity, str) and severity.strip().lower() in {"standard", "red_line"}:
        cfg.block_severity = severity.strip().lower()
    return cfg


def load_config(explicit_path: str | None = None) -> BridgeConfig:
    """读配置。任何异常都退化为默认配置 —— 桥接服务不能因为配置写坏而起不来。"""
    candidates: list[str] = []
    if explicit_path:
        candidates.append(explicit_path)
    env_path = os.environ.get("NEKO_AGENT_BRIDGE_CONFIG", "").strip()
    if env_path:
        candidates.append(env_path)
    candidates += _default_config_paths()

    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(loaded, dict):
            return config_from_dict(loaded, path)

    return BridgeConfig()
