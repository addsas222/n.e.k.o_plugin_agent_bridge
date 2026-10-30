"""Agent 路由的**数据化登记层**。

为什么需要这一层
----------------
``registry.AGENT_SPECS`` 是写在 Python 源码里的字面量，而
``skill/neko-bridge/bin/neko.py`` 里还有一份**同源手抄表**（那个文件第 171 行自己写着
「改动请两边同步」）。两处结构还不一样：插件侧是 dataclass，skill 侧是 8 元组。

后果：新增一个 Agent 要改两处源码、记得同步部署副本，而且用户插件的代码目录**同时
就是状态目录** —— 覆盖式部署会把你改的源码连同 ``config/`` 一起抹掉。

所以把「一个 Agent 怎么被调用」抽成纯数据：``providers/<id>/provider.toml``。
插件与 skill 都从同一个目录读，新增 Agent = 放一个目录，不改任何 Python。

目录约定
--------
::

    providers/
    ├── README.md
    └── <id>/
        ├── provider.toml      路由声明（本模块读它）
        ├── SKILL.md           对接该 Agent 的 skill（Agent 侧使用说明）
        └── README.md          运维说明（安装 / 验证 / 已知坑）

命名约定见 ``providers/README.md``。硬规则只有三条：

1. ``id`` 必须等于**目录名**，并匹配 ``^[a-z][a-z0-9-]{1,31}$``；
2. ``id`` 与 ``aliases`` 在**全局**唯一 —— 撞了不静默覆盖，只告警并丢弃该条；
3. ``mode`` 必须能说清任务正文怎么交给 CLI，否则拒绝加载而不是猜。

冲突策略
--------
本模块**从不覆盖**已登记的 id：``AGENT_SPECS`` 里的内置 Agent 优先，provider 撞名会被
跳过并留下告警。这是刻意的 —— 一个放错位置的清单文件不该悄悄改掉 ``claude`` 的调用方式。
真要替换某个内置 Agent 的调用形态，用配置里的 ``exe_override`` / ``extra_args``
（那是运行期覆盖，看得见、可回滚）。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
"""清单结构版本。未来不兼容变更时递增，旧版清单一律跳过并告警。"""

MANIFEST_FILENAMES = ("provider.toml", "provider.json")
"""先找 TOML（与 ``plugin.toml`` 同一套习惯），没有就找 JSON。

JSON 是给 Python 3.10 及更早版本准备的兜底 —— ``tomllib`` 3.11 才进标准库，
而 skill 侧承诺「任意 Python 3.9+」。插件宿主是 3.11，永远走 TOML 分支。
"""

_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
_MODES = ("flag", "switch", "positional")


@dataclass(frozen=True)
class ProviderManifest:
    """一个 provider 目录解析后的结果。"""

    id: str
    label: str
    binary: str
    mode: str
    fixed_args: tuple[str, ...] = ()
    prompt_flag: str | None = None
    model_flag: str | None = None
    json_args: tuple[str, ...] = ()
    version_args: tuple[str, ...] = ("--version",)
    notes: str = ""
    hints: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    auth_patterns: tuple[str, ...] = ()
    login_hint: str = ""
    fail_patterns: tuple[str, ...] = ()
    install_cmd: str = ""
    install_hint: str = ""
    path: str = ""

    def describe(self) -> str:
        return f"{self.id} ({self.mode}) ← {self.path}"


class ProviderError(ValueError):
    """清单内容非法（不是「文件找不到」那种可忽略的情况）。"""


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------


def _parse_manifest_file(path: str) -> dict[str, Any]:
    lowered = path.lower()
    if lowered.endswith(".json"):
        import json

        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    else:
        try:
            import tomllib
        except ModuleNotFoundError:  # Python < 3.11
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ModuleNotFoundError as exc:  # pragma: no cover - 环境相关
                raise ProviderError(
                    f"读取 {path} 需要 Python 3.11+（tomllib）或已安装 tomli。"
                    f"该环境两者都没有；可改用同目录的 provider.json。"
                ) from exc
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    if not isinstance(data, dict):
        raise ProviderError(f"{path} 顶层必须是表/对象")
    return data


def _as_tuple(value: Any, *, field_name: str, path: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if not isinstance(item, (str, int, float)):
                raise ProviderError(f"{path} 的 {field_name} 只允许字符串数组，遇到 {type(item).__name__}")
            out.append(str(item))
        return tuple(out)
    raise ProviderError(f"{path} 的 {field_name} 必须是字符串或字符串数组")


def _opt_str(value: Any, *, field_name: str, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProviderError(f"{path} 的 {field_name} 必须是字符串")
    text = value.strip()
    return text or None


def build_manifest(raw: dict[str, Any], *, path: str, dir_name: str) -> ProviderManifest:
    """把一个已解析的字典校验并转成 :class:`ProviderManifest`。

    校验失败一律抛 :class:`ProviderError`，由 :func:`load_into` 收集成告警后跳过 ——
    一个坏清单不该让整个插件起不来（宿主里那样会连带 5 个入口一起消失）。
    """
    schema = raw.get("schema", SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        raise ProviderError(f"{path} 的 schema={schema!r}，本模块只认 {SCHEMA_VERSION}")

    section = raw.get("provider")
    if not isinstance(section, dict):
        raise ProviderError(f"{path} 缺少 [provider] 表")

    provider_id = str(section.get("id") or "").strip()
    if not provider_id:
        raise ProviderError(f"{path} 缺少 [provider].id")
    if not _ID_RE.match(provider_id):
        raise ProviderError(
            f"{path} 的 id={provider_id!r} 不合法：需匹配 {_ID_RE.pattern}"
            f"（小写字母开头，只含小写字母/数字/连字符，2-32 字符）"
        )
    if provider_id != dir_name:
        # 宿主靠「目录名 == id」定位插件，路由层沿用同一条规矩，避免出现
        # 「列表里叫 a、目录叫 b」这种只能靠翻文件才能对上号的情况。
        raise ProviderError(f"{path} 的 id={provider_id!r} 与目录名 {dir_name!r} 不一致")

    mode = str(section.get("mode") or "").strip()
    if mode not in _MODES:
        raise ProviderError(f"{path} 的 mode={mode!r} 非法，可选：{'/'.join(_MODES)}")

    prompt_flag = _opt_str(section.get("prompt_flag"), field_name="prompt_flag", path=path)
    if mode in ("flag", "switch") and not prompt_flag:
        raise ProviderError(f"{path} 的 mode={mode} 必须声明 prompt_flag")

    binary = str(section.get("binary") or provider_id).strip()
    if not binary:
        raise ProviderError(f"{path} 的 binary 为空")

    return ProviderManifest(
        id=provider_id,
        label=str(section.get("label") or provider_id),
        binary=binary,
        mode=mode,
        fixed_args=_as_tuple(section.get("fixed_args"), field_name="fixed_args", path=path),
        prompt_flag=prompt_flag,
        model_flag=_opt_str(section.get("model_flag"), field_name="model_flag", path=path),
        json_args=_as_tuple(section.get("json_args"), field_name="json_args", path=path),
        version_args=_as_tuple(section.get("version_args"), field_name="version_args", path=path)
        or ("--version",),
        notes=str(section.get("notes") or ""),
        hints=_as_tuple(section.get("hints"), field_name="hints", path=path),
        aliases=_as_tuple(section.get("aliases"), field_name="aliases", path=path),
        auth_patterns=_as_tuple(section.get("auth_patterns"), field_name="auth_patterns", path=path),
        login_hint=str(section.get("login_hint") or ""),
        fail_patterns=_as_tuple(section.get("fail_patterns"), field_name="fail_patterns", path=path),
        install_cmd=str(section.get("install_cmd") or ""),
        install_hint=str(section.get("install_hint") or ""),
        path=path,
    )


def discover(root: str) -> tuple[list[ProviderManifest], list[str]]:
    """扫描 ``root`` 下的一级子目录，返回 (清单, 告警)。

    只认**一级子目录**里的清单（与宿主的插件扫描规则一致），不递归 —— 递归会让
    ``providers/x/vendor/provider.toml`` 这种依赖目录一起被当成 Agent。
    """
    manifests: list[ProviderManifest] = []
    warnings: list[str] = []
    if not root or not os.path.isdir(root):
        return manifests, warnings

    for name in sorted(os.listdir(root)):
        directory = os.path.join(root, name)
        if not os.path.isdir(directory) or name.startswith((".", "_")):
            continue
        manifest_path = ""
        for filename in MANIFEST_FILENAMES:
            candidate = os.path.join(directory, filename)
            if os.path.isfile(candidate):
                manifest_path = candidate
                break
        if not manifest_path:
            continue
        try:
            raw = _parse_manifest_file(manifest_path)
            manifests.append(build_manifest(raw, path=manifest_path, dir_name=name))
        except (ProviderError, OSError, ValueError) as exc:
            warnings.append(f"provider 清单被跳过：{exc}")
    return manifests, warnings


# --------------------------------------------------------------------------
# 合并
# --------------------------------------------------------------------------


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


def warn_all(warnings: Iterable[str]) -> None:
    for message in warnings:
        logger.warning(message)
