"""本机 Agent 桥接 —— N.E.K.O 原生插件。

把本机的编码 Agent CLI 包装成 N.E.K.O 可以调用的东西，分两层：

* ``@plugin_entry`` —— **运行时入口**。出现在插件管理器的「入口点」里，可以手动触发，
  也参与独立用户插件 Agent 的路由。长任务走这一层。
* ``@llm_tool`` —— **对话期工具**。注册到 main_server 的 ``/api/tools``，猫娘在对话中
  可以直接调用。这一层有硬约束：``timeout`` 最大 300 秒，所以它只适合
  「派活 + 拿结果」这种中等长度任务，别指望它跑十分钟。

两条路径共用 ``.bridge`` 子包里的注册表与执行层（那是 ``neko_agent_bridge`` MCP 服务端
同一份代码的同步副本，见 ``plugin_src/sync_bridge.py``）。

宿主是 N.E.K.O 自带的嵌入式 Python，所以这里**只依赖标准库**。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Callable

_STANDALONE = not __package__
"""True = 本文件被当作**独立模块**导入，而不是包的一部分。

pytest 推导测试模块名时就会这样导入仓库根的 ``__init__.py``（``__package__ == ""``）。
那种状态下相对导入和宿主 SDK 都不可用。两处适配：

1. 给它一个合成包上下文，让 ``from .bridge...`` 能解析；
2. 宿主 SDK 缺失时用 ``tests/_sdk_stub.py`` 顶上（仅此分支，宿主环境永远走真货）。

宿主把本仓库挂载成 ``agent_bridge`` 包时，``__package__`` 非空，下面两段都不执行。
"""

if _STANDALONE:  # pragma: no cover —— 只发生在 pytest 推导模块名的那一瞬间
    # 让下面的 `from bridge...` 兜底分支能找到兄弟包（仓库根加入 sys.path）
    sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from plugin.sdk.plugin import (
        Err,
        NekoPluginBase,
        Ok,
        SdkError,
        lifecycle,
        llm_tool,
        neko_plugin,
        plugin_entry,
        ui,
    )
except ModuleNotFoundError as exc:
    if exc.name != "plugin" or not _STANDALONE:
        # 生产环境缺 SDK 必须照常炸：静默降级会让插件"加载成功"、然后在运行到一半
        # 以看不懂的方式失败，比直接 ImportError 难查得多。
        raise
    import importlib.util as _ilu

    _stub_path = Path(__file__).resolve().parent / "tests" / "_sdk_stub.py"
    _spec = _ilu.spec_from_file_location("_agent_bridge_sdk_stub", _stub_path)
    _stub = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_stub)  # type: ignore[union-attr]
    Err, NekoPluginBase, Ok, SdkError = _stub.Err, _stub.NekoPluginBase, _stub.Ok, _stub.SdkError
    lifecycle, llm_tool = _stub.lifecycle, _stub.llm_tool
    neko_plugin, plugin_entry, ui = _stub.neko_plugin, _stub.plugin_entry, _stub.ui

try:  # 包上下文（宿主把本仓库挂载成 agent_bridge 包）
    from .bridge.audit import status_snapshot
    from .bridge.config import BridgeConfig, config_from_dict
    from .bridge.registry import (
        AGENT_SPECS,
        AgentSpec,
        ResolvedCommand,
        canonical_id,
        list_specs,
        resolve_agent,
    )
    from .bridge.runner import probe_version, run_agent, set_sentinel_runtime
    from .bridge.tools import format_agent_result, write_audit
except ImportError:  # 独立模块（pytest 推导模块名时）—— 兄弟包从仓库根解析
    from bridge.audit import status_snapshot
    from bridge.config import BridgeConfig, config_from_dict
    from bridge.registry import (
        AGENT_SPECS,
        AgentSpec,
        ResolvedCommand,
        canonical_id,
        list_specs,
        resolve_agent,
    )
    from bridge.runner import probe_version, run_agent, set_sentinel_runtime
    from bridge.tools import format_agent_result, write_audit


# 宿主对入口有看门狗（默认很短）。Agent 任务动辄几分钟，所以长任务入口必须显式声明超时。
_ENTRY_TIMEOUT = 1800.0
# @llm_tool 的硬上限是 300 秒，超了会被协议拒绝。
_LLM_TOOL_TIMEOUT = 300.0

_AGENT_IDS = [spec.id for spec in list_specs()]
_AGENT_HINT = "、".join(_AGENT_IDS)


def _load_panel_module() -> Any:
    """延迟导入面板模块（``build_panel_state`` / ``degraded_state`` 都在里面）。

    刻意不做顶层 eager import：``__init__.py`` 顶部那段
    ``try/except ImportError`` 是「哨兵没启用也必须能启动插件」的结构保障，
    往里塞一个新模块等于给启动路径多加一个失败点，而面板只是展示层。
    """
    try:
        from .bridge.sentinel_core import panel as module
    except ImportError:  # 独立模块导入（pytest 推导模块名时）
        from bridge.sentinel_core import panel as module
    return module


@neko_plugin
class AgentBridgePlugin(NekoPluginBase):
    """把本机 Agent CLI 桥接成 N.E.K.O 入口与对话工具。"""
    @ui.context(id="agent_bridge_panel")
    async def _ui_panel_state(self):
        """Hosted 面板状态。

        聚合逻辑全部在 ``bridge/sentinel_core/panel.py``：写在这里就永远测不到
        （本仓库的测试从不实例化插件类，见 ``tests/_sdk_stub.py``）。这个函数只负责
        三件事：把宿主侧的元信息塞进去、丢进线程跑、兜住任何漏网异常。

        **为什么必须 to_thread**：账本查询是同步 SQLite，直接在协程里跑会阻塞事件循环。

        **为什么外面还要再包一层 try**：``build_panel_state`` 内部对每个数据源都降级了，
        但它自己也可能整段炸（拼装 bug、传入奇怪类型）。这个返回值直接进 ``props.state``，
        抛出去就是整个面板白屏 —— 用户会以为插件坏了，实际只是数据层某个分支写错。
        面板不该成为新的故障点：审计数据看不到是次要的，连「点按钮派活」都做不了才是主要的。
        """
        plugin = {
            'id': 'agent_bridge',
            'name': 'Agent 桥接',
            'version': '0.5.0',  # 与 plugin.toml 的 version 手工保持一致，改一处要改两处
            'description': '把本机编码 Agent CLI 桥接成 N.E.K.O 运行时入口(哨兵面板)。',
        }
        # 入口清单从 SDK 真元数据生成（``self.list_entries()`` 读的是各入口的
        # ``EventMeta``），不手抄。手抄那份已经漂移过：``agent_run`` 的 description
        # 是空串（真入口有一长串说明，告诉模型有哪些 Agent 可选），而
        # ``has_required`` 一律写 True —— ``agents_list`` / ``agent_doctor`` 其实任何
        # 参数都不必填，面板因此把它们渲染成带必填星号的表单。
        try:
            entries = self._entry_rows()
        except Exception as exc:  # noqa: BLE001 —— 入口清单不该拖垮面板
            self._log('warning', f'读取入口清单失败，面板将以空清单降级：{exc!r}')
            entries = []
        if not isinstance(entries, list):
            entries = []

        runtime = self._sentinel
        store = getattr(runtime, 'store', None)
        started = time.perf_counter()

        # 干预动作来自配置（sentinel.guard_action），doctor() 里没有这个值。
        # 单独兜一层：读不到配置只让这一个字段显示「未知」，不该把整个面板打进降级态
        # —— 配置文件的 IO 失败和「数据层拼装失败」是两件严重程度不同的事。
        guard_action = ''
        try:
            config = await self._config()
            guard_action = str(getattr(config.sentinel, 'guard_action', '') or '')
        except Exception as exc:  # noqa: BLE001
            self._log('warning', f'读取配置失败，干预动作显示为未知：{exc!r}')

        # 面板模块只在这里用一次：它同时提供正常聚合与降级骨架，所以先拿到模块本身，
        # 不要一次只取一个函数 —— 降级路径还需要它。
        panel_module = None
        try:
            panel_module = _load_panel_module()
            partial = await asyncio.to_thread(
                panel_module.build_panel_state,
                store,
                sentinel=runtime,
                plugin=plugin,
                entries=entries,
                guard_action=guard_action,
            )
            if not isinstance(partial, dict):
                raise TypeError(f'build_panel_state 返回了 {type(partial).__name__}，期望 dict')
            state = dict(partial)
            state['elapsed_ms'] = round((time.perf_counter() - started) * 1000.0, 1)
            return state
        except Exception as exc:  # noqa: BLE001
            self._log('warning', f'面板状态生成失败，已降级：{exc!r}')
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
            if panel_module is not None:
                try:
                    return panel_module.degraded_state(
                        plugin,
                        entries,
                        # 配置读到了就照实显示干预动作：它和「数据层挂了」是两回事，
                        # 用户在降级态下仍然有权知道「命中红线会不会真杀我进程」。
                        guard_action=guard_action,
                        elapsed_ms=elapsed_ms,
                    )
                except Exception as exc2:  # noqa: BLE001
                    self._log('warning', f'降级态构造失败，只返回入口清单：{exc2!r}')
            # 连 panel 模块都导不进来（理论上只有包结构被破坏时）：给最小的可用集合。
            # 不再在这里手抄一份完整形状 —— 那份字面量曾经和 panel.py 各写一遍，
            # 加了 stats.partial 之后当场漂了。前端对缺失字段本来就有兜底。
            return {'plugin': plugin, 'entries': entries, 'elapsed_ms': elapsed_ms}

    def _entry_rows(self) -> list[dict[str, Any]]:
        """把 SDK 的入口元数据整理成面板要用的字段。

        ``timeout`` 是面板判断「慢入口」的依据（见 ui/panel.tsx 的 SLOW_MS）：
        以前这里从不返回它，于是 TSX 里 ``const SLOW = {}`` 永远是空的，慢入口
        分支（带 timeoutMs 的 api.call）根本走不到 —— 而 agent_run 声明的可是
        1800 秒。
        """
        rows: list[dict[str, Any]] = []
        try:
            entries = self.list_entries()
        except Exception as exc:  # noqa: BLE001 — 面板状态拿不到不该让面板炸掉
            self._log("debug", f"读取入口元数据失败：{exc!r}")
            return rows

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # 只保留「面板上能按的运行时入口」：
            #   - lifecycle（startup/shutdown）不是给用户点的按钮；
            #   - dynamic 的 __llm_tool__* 是对话期工具，没有对应的 @ui.action，
            #     渲染出来只会得到一张「动作未注册」的卡片。
            if entry.get("event_type") != "plugin_entry" or entry.get("dynamic"):
                continue
            schema = entry.get("input_schema")
            schema = schema if isinstance(schema, dict) else {}
            properties = schema.get("properties")
            properties = properties if isinstance(properties, dict) else {}
            required = schema.get("required")
            required = [str(x) for x in required] if isinstance(required, (list, tuple)) else []
            timeout = entry.get("timeout")
            rows.append(
                {
                    "id": str(entry.get("id", "")),
                    "name": str(entry.get("name") or entry.get("id") or ""),
                    "description": str(entry.get("description") or ""),
                    # has_params 决定面板是否渲染参数表单；has_required 决定星号。
                    "has_params": bool(properties),
                    "has_required": bool(required),
                    "timeout": float(timeout) if isinstance(timeout, (int, float)) else 0.0,
                }
            )
        return rows


    def __init__(self, ctx):
        # 必须最先调用：基类 __init__ 末尾会自动注册本类上被 @llm_tool 标记的方法。
        super().__init__(ctx)
        self._bridge_config: BridgeConfig | None = None
        self._config_lock = asyncio.Lock()
        self._parallel_sem: asyncio.Semaphore | None = None
        # 曾经回「需要登录」的 Agent。CLI 未登录时退出码是 0，光看退出码永远发现不了，
        # 于是记录下来：后续直接拦下，别再让模型一次次挑中同一台没登录的机器。
        # 跑成功一次就自动摘掉（用户可能刚登录完）。
        self._needs_login: set[str] = set()
        # 哨兵运行时（安全审计）。未启用时它是个「每个方法都立刻返回」的空壳，
        # 派发路径不会因此多出一次分支判断之外的开销。
        # 类型标 Any 而不是 SentinelRuntime：那个类在 on_startup 里才导入（见那里的说明），
        # 这里写具体类型会让文件顶部又需要一个 eager import。
        self._sentinel: Any = None

    # ------------------------------------------------------------------ 基础

    def _logger(self):
        logger = getattr(self, "logger", None)
        if logger is None:
            logger = getattr(getattr(self, "ctx", None), "logger", None)
        return logger

    def _log(self, level: str, message: str) -> None:
        logger = self._logger()
        if logger is None:
            return
        try:
            getattr(logger, level, logger.info)(message)
        except Exception:  # noqa: BLE001 — 日志永远不该影响主流程
            pass

    def _config_source_path(self) -> str:
        """插件基础配置（plugin.toml）的真实路径。

        **不要用 ``getattr(self.config, "path", "")``** —— SDK 的 ``PluginConfig``
        （``plugin/sdk/shared/core/config.py``）根本没有 ``path`` 属性，那行代码永远
        拿到空串，于是诊断里永远显示「（内置默认）」，哪怕配置确实是读来的。
        真正的来源在 ``ctx.config_path``：SDK 自己解析 ``plugin_dir`` / i18n 时用的
        就是它（``base.py`` 的 ``_load_plugin_i18n``）。再退到 ``metadata``，
        最后退回 ``plugin_dir / plugin.toml`` 这个 SDK 的默认位置。
        """

        def _from_ctx() -> object:
            return getattr(getattr(self, "ctx", None), "config_path", None)

        def _from_metadata() -> object:
            return self.metadata.get("config_path")

        def _from_plugin_dir() -> object:
            return self.config_dir / "plugin.toml"

        for getter in (_from_ctx, _from_metadata, _from_plugin_dir):
            try:
                candidate = getter()
            except Exception as exc:  # noqa: BLE001 — 逐个来源降级，不该因此起不来
                self._log("debug", f"读取配置路径来源失败：{exc!r}")
                continue
            if candidate:
                return str(candidate)
        return ""

    def _runtime_config_path(self) -> str:
        """运行时覆盖配置的路径（Profile 写入这里），拿不到就返回空串。"""
        try:
            return str(self.runtime_config_path)
        except Exception as exc:  # noqa: BLE001 — 只是诊断信息，缺了不影响主流程
            self._log("debug", f"读取运行时配置路径失败：{exc!r}")
            return ""

    async def _config(self) -> BridgeConfig:
        """读插件配置并缓存。读失败就退回默认值 —— 桥接不该因为配置写坏而起不来。"""
        if self._bridge_config is not None:
            return self._bridge_config
        async with self._config_lock:
            if self._bridge_config is not None:
                return self._bridge_config

            raw: dict[str, Any] = {}
            source = self._config_source_path()
            try:
                dumped = await self.config.dump()
                if isinstance(dumped, dict):
                    section = dumped.get("bridge")
                    if isinstance(section, dict):
                        raw = section
            except Exception as exc:  # noqa: BLE001
                self._log("warning", f"读取插件配置失败，使用默认值：{exc!r}")

            config = config_from_dict(raw, source)
            if not config.audit_log:
                try:
                    config.audit_log = str(self.data_path("dispatch.jsonl"))
                except Exception:  # noqa: BLE001
                    pass

            self._bridge_config = config
            return config

    def _data_dir(self) -> Path:
        """插件数据目录 —— 账本这类运行期状态放这里，不进源码包。"""
        try:
            return Path(str(self.data_path("sentinel.db"))).parent
        except Exception:  # noqa: BLE001
            return Path.cwd() / "data"

    def _make_judge_fn(self, config: BridgeConfig) -> Callable[[str], str] | None:
        """构造 L2 判定用的模型回调。

        ``SemanticJudge`` 是同步的（跑在哨兵线程上），而宿主网关是 async，所以
        每次调用起一个临时事件循环。哨兵线程本身没有运行中的 loop，``asyncio.run``
        在那里是安全的；反过来如果在事件循环线程里调它，会抛 RuntimeError，
        被 ``SemanticJudge`` 接住并降级 —— 也不会连累 L1。

        未绑槽位（403）、未声明（403）、能力不匹配（409）都只是一次降级，
        不是致命错误：L2 本来就可以缺省。
        """
        if not getattr(config.sentinel, "l2_enabled", False):
            return None
        models = getattr(getattr(self, "ctx", None), "models", None)
        if models is None:
            return None

        def _judge(prompt: str) -> str:
            async def _call() -> str:
                client = await models.get_client()
                response = await client.chat.completions.create(
                    model="judge",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                )
                choices = getattr(response, "choices", None) or []
                if not choices:
                    return ""
                message = getattr(choices[0], "message", None)
                return str(getattr(message, "content", "") or "")

            return asyncio.run(_call())

        return _judge

    def _audit(self, config: BridgeConfig, record: dict[str, Any]) -> None:
        try:
            write_audit(config, record)
        except Exception as exc:  # noqa: BLE001
            self._log("debug", f"审计写入失败：{exc!r}")

    def _semaphore(self, config: BridgeConfig) -> asyncio.Semaphore:
        if self._parallel_sem is None:
            self._parallel_sem = asyncio.Semaphore(max(1, int(config.max_parallel)))
        return self._parallel_sem

    def _ready_agents(self, config: BridgeConfig) -> list[str]:
        """本机真正能跑的 Agent：配置启用 + 可执行文件在 + 没被「需要登录」拉黑。"""
        ready: list[str] = []
        for spec in list_specs():
            override = config.override_for(spec.id)
            if not override.enabled or spec.id in self._needs_login:
                continue
            if resolve_agent(spec, override.exe_override).exists:
                ready.append(spec.id)
        return ready

    def _agent_hint(self, config: BridgeConfig) -> str:
        ready = self._ready_agents(config)
        return f"当前可用：{'、'.join(ready) or '（无）'}"

    # ---------------------------------------------------------------- 生命周期

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        config = await self._config()
        ready = 0
        for spec in list_specs():
            override = config.override_for(spec.id)
            if override.enabled and resolve_agent(spec, override.exe_override).exists:
                ready += 1

        # 哨兵运行时。**整体包在 try 里**：审计是增强层，它起不来绝不能让桥接起不来。
        sentinel_note = "，哨兵 关"
        # 导入推迟到这里，不是随手为之：哨兵默认关闭，绝大多数用户用不到这一整包
        # （sqlite3 + urllib.request + http.server + ctypes 实测约 425ms）。放在文件
        # 顶部的 eager import 会让「默认关闭」变成一句空话，更糟的是原来那句
        # `try: import / except ImportError: 无哨兵` 会把「模块里真有语法错误」
        # 伪装成「这台机器不支持哨兵」—— 静默降级比崩溃更难查。
        # 现在导入失败会落进下面的 except，作为一条 warning 出来。
        try:
            # 导入本身也在 try 里：兜底那句 `from bridge.sentinel_core import ...`
            # 在独立模块布局下同样可能抛 ImportError，而它一旦穿出 on_startup，
            # 后果不是「哨兵退回关闭态」而是「整个插件启动失败」—— 审计是增强层，
            # 绝不该有这种权力。外层 except 兜住导入，异常一律降级成一条 warning。
            try:
                from .bridge.sentinel_core import SentinelRuntime
            except ImportError:  # 独立模块布局（pytest 推导模块名时）
                from bridge.sentinel_core import SentinelRuntime
            runtime = SentinelRuntime(
                config.sentinel,
                judge_fn=self._make_judge_fn(config),
                data_dir=self._data_dir(),
            )
            set_sentinel_runtime(runtime)
            self._sentinel = runtime
            if runtime.enabled:
                info = runtime.doctor()
                parts = ["L2 " + ("开" if info["l2"]["enabled"] else "关")]
                parts.append("账本 " + ("开" if info["store"]["ok"] else "关"))
                parts.append("干预 " + str(config.sentinel.guard_action))
                sentinel_note = "，哨兵 开（" + "，".join(parts) + "）"
        except Exception as exc:  # noqa: BLE001 —— 审计起不来就退回关闭态
            self._log("warning", f"哨兵运行时初始化失败，已退回关闭态：{exc!r}")
            set_sentinel_runtime(None)

        self._log(
            "info",
            f"本机 Agent 桥接就绪：{ready}/{len(_AGENT_IDS)} 个 Agent 可用"
            f"（默认超时 {config.default_timeout_seconds}s，审计 {config.audit_log or '关闭'}{sentinel_note}）",
        )
        return Ok({"ready": ready, "total": len(_AGENT_IDS)})

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        # 摘掉运行时再关账本：派发可能是并发进行的，先让新调用看到 None
        set_sentinel_runtime(None)
        if self._sentinel is not None:
            try:
                self._sentinel.flush_alerts()
                self._sentinel.close()
            except Exception as exc:  # noqa: BLE001
                self._log("warning", f"哨兵收尾失败（已忽略）：{exc!r}")
            self._sentinel = None
        self._log("info", "本机 Agent 桥接停止")
        return Ok({"status": "stopped"})

    # ------------------------------------------------------------ 运行时入口

    @ui.action(label="列出可用 Agent", refresh_context=True)

    @plugin_entry(
        id="agents_list",
        name="列出可用 Agent",
        description="列出本机登记的编码 Agent CLI，含解析到的可执行文件、来源与就绪状态。",
        timeout=_ENTRY_TIMEOUT,
        llm_result_fields=["summary", "agents"],
    )
    async def agents_list(
        self,
        probe_versions: Annotated[bool, "是否逐个探测版本号（较慢，每个约 1-3 秒）"] = False,
    **_):
        config = await self._config()
        rows: list[dict[str, Any]] = []
        pending_specs: list[tuple[AgentSpec, ResolvedCommand, dict[str, Any]]] = []
        for spec in list_specs():
            override = config.override_for(spec.id)
            resolved = resolve_agent(spec, override.exe_override)
            blocked = spec.id in self._needs_login
            row: dict[str, Any] = {
                "agent": spec.id,
                "label": spec.label,
                "enabled": override.enabled,
                "ready": bool(resolved.exists and override.enabled and not blocked),
                "needs_login": blocked,
                "command": resolved.describe(),
                "source": resolved.source,
                "version": "",
            }
            rows.append(row)
            if probe_versions and resolved.exists and override.enabled:
                pending_specs.append((spec, resolved, row))

        if pending_specs:
            # 版本探测必须**并发**：每个 Agent 最坏 45 秒（probe_version 的硬超时），
            # 9 个串行就是 405 秒。宿主对入口有看门狗，串行会把入口跑超时。
            # 并发后最坏仍在 45 秒量级，稳稳落在上面声明的 timeout 之内。
            results = await asyncio.gather(
                *(
                    probe_version(spec, resolved, config=config)
                    for spec, resolved, _row in pending_specs
                ),
                return_exceptions=True,
            )
            for (_spec, _resolved, row), result in zip(pending_specs, results, strict=True):
                # 探测失败不该让整个列表失败 —— 版本号只是附加信息
                row["version"] = "" if isinstance(result, BaseException) else str(result)

        ready = [row["agent"] for row in rows if row["ready"]]
        summary = f"共 {len(rows)} 个登记 Agent，{len(ready)} 个已就绪：{'、'.join(ready) or '（无）'}"
        return Ok({"summary": summary, "agents": rows, "total": len(rows), "ready": len(ready)})

    @ui.action(label="派发任务给 Agent", refresh_context=True)

    @plugin_entry(
        id="agent_run",
        name="派发任务给 Agent",
        description=(
            "把一条任务交给指定的本机编码 Agent CLI 执行，返回它的输出。"
            f"可选 Agent：{_AGENT_HINT}。"
        ),
        timeout=_ENTRY_TIMEOUT,
        llm_result_fields=["summary", "output", "exit_code", "timed_out"],
    )
    async def agent_run(
        self,
        agent: Annotated[str, f"Agent 标识，取值之一：{_AGENT_HINT}"],
        task: Annotated[str, "要交给该 Agent 执行的自然语言任务描述"],
        cwd: Annotated[str, "可选：工作目录绝对路径，留空用配置里的默认值"] = "",
        timeout_seconds: Annotated[int, "可选：超时秒数，0 表示用配置默认值"] = 0,
        model: Annotated[str, "可选：指定模型（部分 CLI 支持）"] = "",
    **_):
        outcome = await self._dispatch(agent, task, cwd=cwd, timeout_seconds=timeout_seconds, model=model)
        if isinstance(outcome, str):  # 校验失败的说明
            return Err(SdkError(outcome))
        return Ok(outcome)

    @ui.action(label="并行派发多个任务", refresh_context=True)

    @plugin_entry(
        id="agent_run_parallel",
        name="并行派发多个任务",
        description=(
            "同时把多条任务派发给多个 Agent。tasks_json 是一个 JSON 数组，"
            '每项形如 {"agent": "dsh", "task": "跑一下单元测试"}。'
            "是否真的并行由配置 allow_parallel / max_parallel 决定。"
        ),
        timeout=_ENTRY_TIMEOUT,
        llm_result_fields=["summary", "results"],
    )
    async def agent_run_parallel(
        self,
        tasks_json: Annotated[str, 'JSON 数组，例如 [{"agent":"dsh","task":"列出仓库结构"}]'],
    **_):
        config = await self._config()
        try:
            parsed = json.loads(tasks_json or "[]")
        except json.JSONDecodeError as exc:
            return Err(SdkError(f"tasks_json 不是合法 JSON：{exc}"))
        if not isinstance(parsed, list) or not parsed:
            return Err(SdkError("tasks_json 必须是非空 JSON 数组"))
        if not config.allow_parallel and len(parsed) > 1:
            return Err(SdkError("配置里 allow_parallel = false，无法并行派发"))

        async def one(item: Any) -> dict[str, Any]:
            if not isinstance(item, dict):
                return {"agent": "?", "ok": False, "error": "每一项都必须是对象"}
            agent = str(item.get("agent") or "")
            task = str(item.get("task") or "")
            timeout = item.get("timeout_seconds") or 0
            try:
                timeout = int(timeout)
            except (TypeError, ValueError):
                timeout = 0
            outcome = await self._dispatch(
                agent,
                task,
                cwd=str(item.get("cwd") or ""),
                timeout_seconds=timeout,
                model=str(item.get("model") or ""),
                semaphore=self._semaphore(config),
            )
            if isinstance(outcome, str):
                return {"agent": agent, "ok": False, "error": outcome}
            return {"agent": outcome.get("agent", agent), **outcome}

        results = await asyncio.gather(*(one(item) for item in parsed))
        succeeded = sum(1 for row in results if row.get("ok"))
        summary = f"并行派发 {len(results)} 条，成功 {succeeded} 条"
        return Ok({"summary": summary, "results": results, "total": len(results), "succeeded": succeeded})

    @ui.action(label="诊断桥接链路", refresh_context=True)

    @plugin_entry(
        id="agent_doctor",
        name="诊断桥接链路",
        description="检查宿主平台、解释器版本、配置来源、每个 Agent 的解析与版本情况，以及哨兵审计的连通性。",
        timeout=_ENTRY_TIMEOUT,  # 默认 probe_versions=True 会逐个探测全部 CLI，30 秒看门狗不够
        llm_result_fields=["summary", "agents", "sentinel"],
    )
    async def agent_doctor(
        self,
        agent: Annotated[str, "可选：只诊断某一个 Agent 标识"] = "",
        probe_versions: Annotated[bool, "是否探测各 CLI 版本"] = True,
    **_):
        config = await self._config()
        only = canonical_id(agent.strip()) if agent.strip() else None
        if agent.strip() and not only:
            return Err(SdkError(f"未知的 agent：{agent!r}。可选：{_AGENT_HINT}"))

        specs = [AGENT_SPECS[only]] if only else list_specs()
        lines = [f"平台 {sys.platform} · Python {sys.version.split()[0]}"]
        # 配置来源必须报真路径：SDK 的 PluginConfig 没有 .path，以前那行 getattr
        # 永远是空串，于是不管配置读没读进来都显示「（内置默认）」。
        source_path = config.source_path or self._config_source_path()
        lines.append(f"配置来源：{source_path or '（未知：宿主未暴露 config_path）'}")
        runtime_path = self._runtime_config_path()
        if runtime_path and runtime_path != source_path:
            lines.append(f"运行时覆盖：{runtime_path}")
        lines.append(f"审计日志：{config.audit_log or '（关闭）'}")

        # 审计层：内置后端在进程内判定，HTTP 后端才需要走网络。放到线程里，
        # 别让一次诊断把宿主事件循环卡住。
        sentinel_info = await asyncio.to_thread(status_snapshot, config.sentinel)
        if not sentinel_info["enabled"]:
            lines.append("安全审计：未启用（可选，见 README「安全审计集成」）")
        elif sentinel_info["backend"] == "builtin":
            lines.append(
                "安全审计：内置后端 · v{version} · 会话 {sessions}（进程内判定，无需外部进程）".format(
                    **sentinel_info
                )
            )
        elif sentinel_info["reachable"]:
            lines.append(
                "安全审计：外部后端 {url} · v{version} · 会话 {sessions}（L2 休眠 {l2_sleeping}）".format(
                    **sentinel_info
                )
            )
        else:
            lines.append(
                f"安全审计：外部后端 {sentinel_info['url']} 连不上（fail-open，不影响派发）"
            )

        # 先并行探测版本号，再拼报告 —— 串行探测 9 个 Agent 最坏 405 秒，
        # 会把入口跑过宿主看门狗（同 agents_list 的处理）。
        probes: dict[str, str] = {}
        probe_jobs = []
        for spec in specs:
            override = config.override_for(spec.id)
            resolved = resolve_agent(spec, override.exe_override)
            if probe_versions and resolved.exists and override.enabled:
                probe_jobs.append((spec, resolved))
        if probe_jobs:
            results = await asyncio.gather(
                *(probe_version(spec, resolved, config=config) for spec, resolved in probe_jobs),
                return_exceptions=True,
            )
            for (spec, _resolved), result in zip(probe_jobs, results, strict=True):
                probes[spec.id] = "" if isinstance(result, BaseException) else str(result)

        rows: list[dict[str, Any]] = []
        for spec in specs:
            override = config.override_for(spec.id)
            resolved = resolve_agent(spec, override.exe_override)
            version = probes.get(spec.id, "")
            if not override.enabled:
                mark = "--"
            elif not resolved.exists or version.startswith("<"):
                mark = "!!"
            else:
                mark = "OK"
            lines.append(f"{mark} {spec.id}  {resolved.describe()}")
            rows.append(
                {
                    "agent": spec.id,
                    "mark": mark,
                    "command": resolved.describe(),
                    "source": resolved.source,
                    "enabled": override.enabled,
                    "exists": resolved.exists,
                    "version": version,
                }
            )

        healthy = sum(1 for row in rows if row["mark"] == "OK")
        summary = f"诊断完成：{healthy}/{len(rows)} 个 Agent 标记为 OK"
        return Ok(
            {
                "summary": summary,
                "report": "\n".join(lines),
                "agents": rows,
                "healthy": healthy,
                "sentinel": sentinel_info,
            }
        )

    # ------------------------------------------------------------ 对话期工具

    @llm_tool(
        name="agent_list",
        description=(
            f"列出本机可用的编码 Agent CLI（{_AGENT_HINT}）及其就绪状态。"
            "当用户问「你本机有哪些编码 Agent 能用」时调用。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        timeout=60.0,
    )
    async def agent_list_tool(self):
        # 成功路径直接返回业务 payload，**不要**自己再包一层 ``output``。
        # 宿主的 llm_tools 路由（plugin/server/routes/llm_tools.py）约定：
        #   - 返回值是普通 dict（只有 output 也算普通数据）→ 包成
        #     {"output": <返回值>, "is_error": false}
        #   - 返回值里带 is_error，或同时带 output+images → 视为信封，原样规范化
        # 所以自己包一层会让模型看到两层 output；只有「显式报错」才需要写信封。
        try:
            config = await self._config()
            rows = []
            for spec in list_specs():
                override = config.override_for(spec.id)
                resolved = resolve_agent(spec, override.exe_override)
                blocked = spec.id in self._needs_login
                rows.append(
                    {
                        "agent": spec.id,
                        "label": spec.label,
                        # 「文件在」不等于「能用」：没登录的 CLI 一样能启动、一样退出码 0。
                        "ready": bool(resolved.exists and override.enabled and not blocked),
                        "needs_login": blocked,
                        "command": resolved.describe(),
                    }
                )
            ready = [row["agent"] for row in rows if row["ready"]]
            blocked_ids = [row["agent"] for row in rows if row["needs_login"]]
            return {
                "ready_agents": ready,
                "needs_login_agents": blocked_ids,
                "all_agents": rows,
                "note": (
                    f"共 {len(rows)} 个，{len(ready)} 个就绪"
                    + (f"；未登录已跳过：{'、'.join(blocked_ids)}" if blocked_ids else "")
                ),
            }
        except Exception as exc:  # noqa: BLE001
            return {"output": {"reason": f"列举失败：{exc}"}, "is_error": True, "error": "AGENT_LIST_FAILED"}

    @llm_tool(
        name="agent_run",
        description=(
            "把一条编码任务派发给本机某个 Agent CLI 执行并返回输出，适合写代码、改文件、"
            "分析仓库、跑测试等。任务描述要自包含。注意单次调用最多 300 秒。"
            "派发前先调 agent_list 拿 ready_agents（没登录的 CLI 会被自动跳过）；"
            "若返回 needs_login=true，说明那台 CLI 未登录、任务没执行，换 ready_agents 里的重试。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": f"Agent 标识，先用 agent_list 查 ready_agents，取值之一：{_AGENT_HINT}",
                },
                "task": {"type": "string", "description": "要交给该 Agent 执行的自然语言任务描述"},
                "cwd": {"type": "string", "description": "可选：工作目录绝对路径"},
                "timeout_seconds": {"type": "integer", "description": "可选：超时秒数，上限 300"},
            },
            "required": ["agent", "task"],
        },
        timeout=_LLM_TOOL_TIMEOUT,
    )
    async def agent_run_tool(
        self,
        *,
        agent: str,
        task: str,
        cwd: str = "",
        timeout_seconds: int = 0,
    ):
        try:
            requested = int(timeout_seconds or 0)
        except (TypeError, ValueError):
            requested = 0
        # 对话期工具被协议限制在 300 秒内，这里再夹一次，避免用户填了更大的值却被硬切。
        requested = min(requested, int(_LLM_TOOL_TIMEOUT)) if requested > 0 else int(_LLM_TOOL_TIMEOUT)

        outcome = await self._dispatch(agent, task, cwd=cwd, timeout_seconds=requested, model="")
        if isinstance(outcome, str):
            return {"output": {"reason": outcome}, "is_error": True, "error": "AGENT_RUN_REJECTED"}
        if not outcome.get("ok"):
            return {
                "output": outcome,
                "is_error": True,
                "error": "AGENT_RUN_FAILED",
            }
        # 成功时直接返回 payload：宿主路由会包成 {"output": outcome, "is_error": false}。
        return outcome

    # ------------------------------------------------------------------ 内部

    async def _dispatch(
        self,
        agent: str,
        task: str,
        *,
        cwd: str = "",
        timeout_seconds: int = 0,
        model: str = "",
        semaphore: asyncio.Semaphore | None = None,
    ) -> dict[str, Any] | str:
        """派发一条任务。返回结果字典，或在参数/环境不合法时返回一句错误说明。"""
        config = await self._config()

        spec_id = canonical_id(str(agent or "").strip())
        if not spec_id:
            return f"未知的 agent：{agent!r}。可选：{_AGENT_HINT}"
        spec = AGENT_SPECS[spec_id]
        override = config.override_for(spec.id)
        if not override.enabled:
            return f"agent {spec.id} 已在配置里禁用"

        resolved = resolve_agent(spec, override.exe_override)
        if not resolved.exists:
            return f"未找到 {spec.id} 的可执行文件：{resolved.describe()}（可用 enabled/exe_override 调整）"

        # 已经确认过没登录的，直接拦：再跑一次只是白等一个进程 + 拿回同一句登录提示。
        # 拉黑名单只能靠「重新加载插件」清掉（拦住之后就没机会跑到成功分支），所以
        # 提示里必须写清楚这一点，别让用户以为登录完重试一次就能过。
        if spec.id in self._needs_login:
            return (
                f"agent {spec.id} 上次调用返回「需要登录」，已跳过（登录完请重新加载本插件）。"
                f"请改用：{self._agent_hint(config)}"
            )

        task_text = str(task or "").strip()
        if not task_text:
            return "task 不能为空"

        seconds = config.clamp_timeout(
            int(timeout_seconds or 0) or override.timeout_seconds or config.default_timeout_seconds
        )
        workdir = config.resolve_cwd(cwd or None)

        async def invoke():
            return await run_agent(
                spec,
                resolved,
                task_text,
                config=config,
                cwd=workdir,
                model=model or None,
                timeout=seconds,
                extra_args=override.extra_args,
            )

        if semaphore is None:
            result = await invoke()
        else:
            async with semaphore:
                result = await invoke()

        text = format_agent_result(result)
        summary = text.splitlines()[0] if text else f"[agent_run] {spec.id}"
        self._audit(
            config,
            {
                "agent": spec.id,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "needs_login": result.needs_login,
                "duration_ms": result.duration_ms,
                "cwd": workdir or "",
                "task": task_text[:300],
            },
        )

        if result.needs_login:
            self._needs_login.add(spec.id)
        elif result.ok:
            self._needs_login.discard(spec.id)  # 用户可能刚登录完，别永久拉黑

        return {
            "ok": bool(result.ok),
            "agent": spec.id,
            "label": result.label,
            "summary": summary,
            "output": text,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "needs_login": bool(result.needs_login),
            "output_truncated": result.output_truncated,
            "duration_seconds": round(result.duration_ms / 1000, 1),
            "command": result.command_preview,
            "error": result.error,
            "retry_with": self._ready_agents(config) if result.needs_login else [],
        }

    # ------------------------------------------------------------ 哨兵审计

    @ui.action(label="哨兵状态", refresh_context=True)

    @plugin_entry(
        id="sentinel_status",
        name="哨兵状态",
        description="查看安全审计各层的状态（L1/L2、账本、干预、告警、时间盒）与近期统计。",
        timeout=_ENTRY_TIMEOUT,
        llm_result_fields=["summary", "layers"],
    )
    async def sentinel_status(self, **_):
        runtime = self._sentinel
        if runtime is None:
            return Err(SdkError("哨兵运行时未初始化（插件尚未完成启动，或初始化时出错）"))
        info = await asyncio.to_thread(runtime.doctor)
        layers = {
            "l1": True,
            "l2": bool(info["l2"].get("enabled")),
            "store": bool(info["store"].get("ok")),
            "guard": str((await self._config()).sentinel.guard_action),
            "webhook": bool(info["webhook"].get("configured")),
            "timebox_seconds": float(info["timebox"].get("seconds") or 0.0),
        }
        if not info["enabled"]:
            summary = "哨兵未启用（见 README「Sentinel 安全审计集成」）"
        else:
            parts = [
                "L2 " + ("开" if layers["l2"] else "关"),
                "账本 " + ("开" if layers["store"] else "关"),
                "干预 " + layers["guard"],
                "告警 " + ("webhook" if layers["webhook"] else "仅本地"),
            ]
            dropped = int(info.get("dropped_events") or 0)
            if dropped:
                parts.append(f"丢弃事件 {dropped}")
            # 解析失败数只在非零时出现：0 是常态，常驻一个「解析失败 0」会让这一行
            # 变成噪声，而真正非零时就淹在固定文本里看不见了。
            # 它衡量的是「适配器有没有坏」——坏掉时所有结构化字段会静默变空，
            # 账本里只留下「输出本来就长这样」的假象。
            parse_failed = int((info.get("parse") or {}).get("failures") or 0)
            if parse_failed:
                parts.append(f"解析失败 {parse_failed}")
            summary = "哨兵已启用：" + "，".join(parts)
        return Ok({"summary": summary, "layers": layers, "detail": info})

    @ui.action(label="案发时间线", refresh_context=True)

    @plugin_entry(
        id="sentinel_timeline",
        name="案发时间线",
        description="把最近的事件串成时间线，回答「它是怎么一步步走到这一步的」。",
        timeout=_ENTRY_TIMEOUT,
        llm_result_fields=["summary", "timeline"],
    )
    async def sentinel_timeline(
        self,
        hours: Annotated[float, "往前看多少小时"] = 12.0,
        session: Annotated[str, "只看某一路会话指纹（留空看全部）"] = "",
        **_,
    ):
        runtime = self._sentinel
        if runtime is None:
            return Err(SdkError("哨兵运行时未初始化"))
        since = time.time() - max(0.1, float(hours)) * 3600
        text = await asyncio.to_thread(runtime.timeline, since=since, fingerprint=session)
        lines = [line for line in text.splitlines() if line.strip()]
        return Ok(
            {
                "summary": lines[0] if lines else "（这段时间没有记录）",
                "timeline": text,
                "lines": len(lines),
            }
        )

    @ui.action(label="夜间值守晨报", refresh_context=True)

    @plugin_entry(
        id="sentinel_briefing",
        name="夜间值守晨报",
        description="聚合最近一段时间的事件，给出一份早上能直接读的安全摘要。",
        timeout=_ENTRY_TIMEOUT,
        llm_result_fields=["summary", "briefing"],
    )
    async def sentinel_briefing(
        self,
        hours: Annotated[float, "往前看多少小时（夜里挂机建议 12）"] = 12.0,
        **_,
    ):
        runtime = self._sentinel
        if runtime is None:
            return Err(SdkError("哨兵运行时未初始化"))
        text = await asyncio.to_thread(runtime.briefing, hours=float(hours))
        lines = [line for line in text.splitlines() if line.strip()]
        return Ok({"summary": lines[0] if lines else "", "briefing": text})

    @ui.action(label="回滚到安全点", refresh_context=True)

    @plugin_entry(
        id="sentinel_rollback",
        name="回滚到安全点",
        description="把工作区恢复到最近一个安全点（派发前打的 git 快照）。不可逆，请确认后再调用。",
        timeout=_ENTRY_TIMEOUT,
        llm_result_fields=["summary"],
    )
    async def sentinel_rollback(
        self,
        session: Annotated[str, "会话指纹（留空取全局最近一个安全点）"] = "",
        cwd: Annotated[str, "工作目录（留空用默认工作目录）"] = "",
        **_,
    ):
        runtime = self._sentinel
        if runtime is None:
            return Err(SdkError("哨兵运行时未初始化"))
        config = await self._config()
        workdir = config.resolve_cwd(cwd or None)
        if not workdir:
            return Err(SdkError("没有可回滚的工作目录：请显式给出 cwd"))
        ok = await asyncio.to_thread(runtime.rollback, workdir, fingerprint=session)
        if not ok:
            return Err(SdkError("回滚失败：没有可用安全点，或该目录不是 git 仓库"))
        return Ok({"summary": f"已回滚到安全点：{workdir}", "cwd": workdir})
