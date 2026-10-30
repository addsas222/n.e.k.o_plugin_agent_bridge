"""provider 清单的解析层 —— **纯标准库，零 bridge 依赖**。

为什么要单独一个文件
--------------------
插件侧（`bridge/providers.py`）与 skill 侧（`skill/neko-bridge/bin/neko.py`）
原本各有一套 TOML 解析。两套规则的差别不是风格问题，是**真的会漂**：

- skill 那份不校验 id 正则、不认 `schema`、不检查 `prompt_flag` 必填；
- 两份的 `auth_patterns` / `fail_patterns` 读取各写一遍，加字段时只改一边。

把解析收敛到这里。它只依赖标准库，因此可以**原样 vendor** 到 skill 的 `bin/`
目录（由 `skill/neko-bridge/sync_providers.py` 生成，不要手改副本）。

分工
----
* 本模块：清单长什么样、怎么校验、目录怎么扫；
* `bridge/providers.py`：适配成 `AgentSpec`（插件运行时模型）；
* `neko.py`：适配成自己的 8 元组 + `PROVIDER_AUTH`。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

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
