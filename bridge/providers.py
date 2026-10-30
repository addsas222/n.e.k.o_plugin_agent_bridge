"""provider 登记的**适配层**：把清单变成 `AgentSpec`，并决定去哪儿找清单。

清单长什么样、怎么校验，见 `bridge/provider_manifest.py`（纯标准库，可 vendor）。
这里只做两件事：

1. `load_into()` —— 把 `ProviderManifest` 适配成插件运行时的 `AgentSpec`，
   合并进 `AGENT_SPECS` / `ALIASES`，冲突只告警不覆盖；
2. `provider_roots()` —— 决定去哪些目录找清单。

为什么拆开：skill 侧（`neko.py`）也要读同一份清单，但它被单独拷到别的 Agent 环境里，
不能依赖 `bridge` 包。把解析下沉到一个零依赖模块，两侧才能共用同一套规则，
而不是各写一遍然后慢慢漂。
"""

from __future__ import annotations

import os
from typing import Any

# 对外仍从本模块导入这些名字，避免调用方跑到 provider_manifest 里找
from .provider_manifest import (
    MANIFEST_FILENAMES,
    SCHEMA_VERSION,
    ProviderError,
    ProviderManifest,
    build_manifest,
    discover,
)

__all__ = [
    "MANIFEST_FILENAMES",
    "SCHEMA_VERSION",
    "ProviderError",
    "ProviderManifest",
    "build_manifest",
    "discover",
    "load_into",
    "provider_roots",
]

def load_into(
    specs: dict[str, Any],
    aliases: dict[str, str],
    *,
    root: str,
    spec_factory: Any,
) -> list[str]:
    """把 ``root`` 下的 provider 合并进 ``specs`` / ``aliases``（原地修改）。

    ``spec_factory`` 是 ``AgentSpec`` 的构造函数 —— 本模块不 import ``registry``，
    那是刻意的：``registry`` 会在模块末尾调用本函数，反向 import 会形成循环。

    返回告警列表（用于日志与 ``agent_doctor``）。**合并绝不覆盖已存在的 id。**
    """
    manifests, warnings = discover(root)
    if not manifests:
        return warnings

    taken_ids = set(specs)
    taken_aliases = {key: value for key, value in aliases.items()}

    for manifest in manifests:
        if manifest.id in taken_ids:
            warnings.append(
                f"provider {manifest.id!r}（{manifest.path}）与已登记的 Agent 同名，已跳过。"
                f"要改它的调用形态请用配置里的 exe_override / extra_args。"
            )
            continue

        usable_aliases: list[str] = []
        for alias in manifest.aliases:
            key = alias.strip().lower()
            if not key:
                continue
            if key == manifest.id:
                continue
            if key in taken_ids:
                warnings.append(
                    f"provider {manifest.id!r} 的别名 {key!r} 与已登记的 Agent id 冲突，已忽略该别名"
                )
                continue
            if key in taken_aliases and taken_aliases[key] != manifest.id:
                warnings.append(
                    f"provider {manifest.id!r} 的别名 {key!r} 已被 {taken_aliases[key]!r} 占用，"
                    f"已忽略该别名"
                )
                continue
            usable_aliases.append(key)

        specs[manifest.id] = spec_factory(
            id=manifest.id,
            label=manifest.label,
            binary=manifest.binary,
            mode=manifest.mode,
            fixed_args=manifest.fixed_args,
            prompt_flag=manifest.prompt_flag,
            model_flag=manifest.model_flag,
            json_args=manifest.json_args,
            version_args=manifest.version_args,
            notes=manifest.notes,
            hints=manifest.hints,
            aliases=tuple(usable_aliases),
            auth_patterns=manifest.auth_patterns,
            login_hint=manifest.login_hint,
            fail_patterns=manifest.fail_patterns,
            install_cmd=manifest.install_cmd,
            install_hint=manifest.install_hint,
        )
        taken_ids.add(manifest.id)
        for key in usable_aliases:
            taken_aliases[key] = manifest.id
        aliases.update({key: manifest.id for key in usable_aliases})

    return warnings


def provider_roots(explicit: str | None = None) -> list[str]:
    """provider 目录的搜索顺序。

    1. 显式参数（测试用）；
    2. 环境变量 ``NEKO_BRIDGE_PROVIDERS``；
    3. ``<插件目录>/providers``（部署后的正常位置）；
    4. ``<仓库根>/providers``（源码树里跑测试时的位置）。

    顺序即优先级；同 id 先出现的赢（后面的会被 ``load_into`` 判为重复而跳过）。
    """
    roots: list[str] = []
    if explicit:
        roots.append(explicit)
    env_value = os.environ.get("NEKO_BRIDGE_PROVIDERS", "").strip()
    if env_value:
        roots.append(env_value)
    package_dir = os.path.dirname(os.path.abspath(__file__))
    plugin_dir = os.path.dirname(package_dir)
    roots.append(os.path.join(plugin_dir, "providers"))
    return [root for root in roots if root]
