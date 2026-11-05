# agent_bridge

N.E.K.O 本机 Agent 桥接插件。把本机的编码 Agent CLI 桥接成 N.E.K.O 的**运行时入口**，
并注册为**对话期 LLM 工具**，让猫娘在聊天里就能直接把活派给它们。

内置 9 个：CodeBuddy / Claude Code / dsh / omp / EvoX / OpenCode / OpenClaw / AtomCode /
QwenPaw。**其余通过 `providers/<id>/provider.toml` 动态登记**（本仓库自带 Gemini CLI /
Command Code / pi / Hermes / StepCode，合计 **14 条路由**），新增一个 Agent = 放一个目录，
不改任何 Python。

- 路由机制与扩展方式：[`docs/agent-routing.md`](docs/agent-routing.md)
- 清单字段与命名约定：[`providers/README.md`](providers/README.md)

反方向见 [`skill/neko-bridge`](skill/neko-bridge/)：把 N.E.K.O. 做成可移植 Skill
装到那些 Agent 里，让它们反过来调用 N.E.K.O.。

## 入口

| 入口 | 类型 | 说明 |
|------|------|------|
| `agents_list` | 运行时入口 | 列出全部登记 Agent（内置 + providers）的解析结果与就绪状态；`probe_versions=true` 时并发探测版本 |
| `agent_run` | 运行时入口 | 把一条任务派给指定 Agent，返回输出；支持 `cwd` / `timeout_seconds` / `model` |
| `agent_run_parallel` | 运行时入口 | 按 JSON 数组并行派发多条任务，受 `allow_parallel` / `max_parallel` 约束 |
| `agent_doctor` | 运行时入口 | 诊断桥接链路：平台、解释器、配置来源、每个 Agent 的解析与版本，以及哨兵审计的连通性 |
| `agent_list` | LLM 工具 | 对话期列举可用 Agent（含未登录标注） |
| `agent_run` | LLM 工具 | 对话期派发任务（单次上限 300 秒，受协议硬限） |

## 关键设计

**「文件在」不等于「能用」。** 没登录的 CLI 一样能启动、一样退出码 0。
所以桥接会识别鉴权失败（`bridge/runner.py:_detect_auth_failure`），
把该 Agent 标记为 `needs_login` 并在后续派发中**跳过**，
`agents_list` / `agent_list` 都会如实标注，避免把「没干活」当成「干完了」。

**不经过 `cmd.exe`。** 早期实现对 `.cmd` 垫片走 `cmd.exe /c` 回退，
拼接用户传入的任务文本会构成**命令注入**。现在 `bridge/registry.py`
自行解析 `.cmd` 垫片（`parse_cmd_shim`）、展开变量（`_expand_vars`）、
构造 argv（`build_argv`），全部走 `subprocess` 的列表形式，不经过 shell。
解析不到可执行文件时返回 `AGENT_UNRESOLVED` 而不是回退到 `cmd.exe`。

> 这条只约束**派发**路径。`webui/` 的「安装」与 `install.py` 的 `--run` 用
> `shell=True` 执行 `bun add -g …` 这类命令 —— 那些命令**来自仓库清单**
> （`provider.toml` / `frameworks.toml`），请求体只能给 id，注入面为零；
> 而且它们本来就是 shell 语法，不走 shell 反而拼不出来。

**版本探测必须并发。** 每个 Agent 最坏 45 秒，9 个串行 405 秒会撞宿主看门狗；
`agents_list` 用 `asyncio.gather` 并发探测，最坏仍在 45 秒量级。

## 配置

`config.example.toml` 是模板。宿主首次运行会把它**逐字节复制**到
`%LOCALAPPDATA%\N.E.K.O\plugins\agent_bridge\config\plugin.toml`，
**之后用户改的是那一份**，改模板不会覆盖已有配置。

```toml
[bridge]
default_timeout_seconds = 600   # 单次派发的默认超时
max_timeout_seconds = 3600      # 上限
max_output_bytes = 400000       # 单次输出上限（超出截断尾部并标注）
default_cwd = ""                # 工作目录，留空不切换
audit_log = ""                  # 留空写到 data/dispatch.jsonl
allow_parallel = true
max_parallel = 4

# 按需覆盖单个 Agent
# [bridge.agents.dsh]
# enabled = true
# timeout_seconds = 900
# exe_override = "C:\\full\\path\\to\\dsh.exe"
# extra_args = ["--verbose"]
```

## 依赖

纯标准库 + 宿主自带 SDK，**无第三方 Python 依赖**。`vendor/sentinel` 是可选审计进程的
子模块（GPL-3.0，独立进程运行），插件自身**不 import** 它的任何源码，不装也能正常派发。

## 测试

```bash
python -m pytest tests -q
```

`tests/conftest.py` 把本目录注册为命名空间包（只设 `__path__`），
以绕开插件 `__init__.py` 对 `plugin.sdk` 的导入 —— 这样单测不依赖宿主 SDK。

## 目录

```
bridge/registry.py   Agent 规格表 + .cmd 垫片解析 + argv 构造（命令注入防线）
bridge/providers.py  providers/<id>/provider.toml 的发现、校验与合并
bridge/runner.py     派发执行 + 鉴权失败识别 + 超时与输出截断
bridge/config.py     配置读取与逐 Agent 覆盖
bridge/tools.py      说明性模块（实际入口都在 __init__.py）
providers/<id>/      每条动态路由：provider.toml + SKILL.md + README.md
docs/agent-routing.md 路由机制、已知问题与扩展步骤
ui/panel.tsx         Hosted TSX 面板
skill/neko-bridge/   反向 Skill：把 N.E.K.O. 装进别的 Agent（自带 providers 副本）
bridge/audit.py      哨兵本地 HTTP 客户端（纯标准库；不含任何 GPL 源码）
vendor/sentinel/     哨兵子模块（GPL-3.0，可选，独立进程运行）
```

## 新增一个 Agent

```bash
mkdir providers/myagent
$EDITOR providers/myagent/provider.toml     # schema/id/binary/mode/prompt_flag/auth_patterns
python -m pytest tests/test_providers.py -q
python skill/neko-bridge/sync_providers.py --apply
python skill/neko-bridge/bin/neko.py dispatch myagent "只回一个字：好"
```

字段说明见 [`providers/README.md`](providers/README.md)，完整步骤见
[`docs/agent-routing.md`](docs/agent-routing.md) §8。

## Sentinel 安全审计集成（可选）

本插件可对接 **[哨兵 Sentinel](https://github.com/silverwolftyro/sentinel)**——一个只读的
Agent 自动审计员，提供 L1 规则红线拦截、意图漂移检测与守护者挂起能力。合并进来后，
agent_bridge 在**派发任务前**会用哨兵的 L1 规则做一次秒级安全预检，命中红线/危险词就
**拒绝派发**；派发时还能把这一路会话挂给哨兵后台静默监控。

### 为什么是「子模块 + HTTP 接口」

哨兵以 **GPL-3.0** 发布，本插件随 N.E.K.O 项目采用 **MIT**。为避免 GPL 的 copyleft 把本仓库
整体拖入 GPL，本插件**不并入哨兵源码**，而是：

1. 把哨兵作为 **git submodule** 放进仓库（`vendor/sentinel`），代码留在独立 Git 对象里、保持
   GPL-3.0 不变；
2. 在 `bridge/audit.py` 中只写一个**纯标准库**的 HTTP 客户端，通过哨兵 v5 无头守护进程的
   本地接口（默认 `127.0.0.1:8787`）对接——这是哨兵 README 中明确为「第三方 Agent 插件」
   预留的安检/挂起接口。

许可边界与分发义务见仓库根 [`NOTICE`](NOTICE)。

### 启用步骤

```bash
# 1) 拉取哨兵子模块源码（仅首次）
git submodule update --init vendor/sentinel

# 2) 在本机拉起哨兵无头守护进程（只绑 127.0.0.1）
cd vendor/sentinel
python -m src.daemon.server --port 8787
#   （另开一个终端跑插件即可；哨兵进程挂掉不影响派发，本插件会 fail-open 放行）

# 3) 在 plugin.toml 的 [bridge.sentinel] 里把 enabled 改为 true
```

> 只想确认通不通？打开插件的「诊断桥接链路」入口（`agent_doctor`），
> 报告里会有一行 `哨兵：已连接 …` / `哨兵：已启用但连不上 …`，不用去翻日志。

### 配置项（plugin.toml 的 `[bridge.sentinel]`）

| 字段 | 默认 | 说明 |
|------|------|------|
| `enabled` | `false` | 是否启用哨兵审计；关闭时派发路径零改动 |
| `url` | `http://127.0.0.1:8787` | 哨兵守护进程地址（只允许本机回环，非回环地址会被直接拒绝） |
| `block_on_deny` | `true` | 预检判定 `block` 时是否真的拒绝派发（`false` 则只记录不拦截） |
| `attach` | `true` | 进程拉起后是否把会话挂给哨兵后台监控（延迟 2 秒，避开日志尚未落盘；best-effort） |
| `timeout` | `2.0` | 单个接口超时（秒）；哨兵未启动按 fail-open 处理 |

### 行为约定

* **默认关闭**：`enabled=false` 时，`run_agent` 完全不触碰哨兵，现有行为与回归测试不变。
* **fail-open**：哨兵未启动 / 接口超时 / 返回异常时一律放行，安全审计是可选增强层，
  绝不会因为哨兵没跑就把所有派发卡死。
* **预检只上 L1**：纯规则、秒回、不调模型（哨兵侧保证）；语义级判定（L2）由哨兵在
  `attach` 后的后台监控里完成。
* **挂接不阻塞派发**：pid 在 `Popen` 之后立刻拿到，挂接在后台线程里延后 2 秒发出——
  太早挂接时 Agent 还没写出会话日志，哨兵会挂到一个空跟读线程上。
* **诊断可见**：`agent_doctor` 的报告与结构化结果里都有 `sentinel` 一节，
  分别覆盖「未启用 / 已连接 / 启用了但连不上」三种状态。
* **被拦下的任务**：结果是 `AGENT_BLOCKED_BY_SENTINEL`，并以 `SENTINEL_BLOCK` 状态回显，
  任务根本不会交给 Agent。

## 许可

见仓库根 `LICENSE`（随 N.E.K.O 项目）。另见仓库根 [`NOTICE`](NOTICE) 关于哨兵 GPL-3.0
子模块的许可边界与分发义务说明。
