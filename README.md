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

纯标准库 + 宿主自带 SDK，**无第三方 Python 依赖**。安全审计也是内置的（`bridge/sentinel_core`），
不需要额外的包、服务或子进程。

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
bridge/audit.py      审计后端选择与闸门（内置 / 外部 HTTP 两条路径）
bridge/sentinel_core/ 内置精简哨兵：规则、预检、会话表、四端点服务端
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

## 安全审计集成（内置 L1 预检）

插件自带一份精简的安全审计器（`bridge/sentinel_core/`，**MIT**，纯标准库，约 1.8k 行）。
它在**派发任务前**对用户那句话做一次秒级规则预检：命中「一旦执行就回不来」的操作
就直接拒绝派发，任务根本不会交给 Agent。

### 能拦什么

| 类别 | 例子 |
|------|------|
| 删根 | `rm -rf /`、`rm -rf ~`、`rm -rf /*`、`sudo rm -rf /etc` |
| 抹盘 | `mkfs.ext4 /dev/sdb1`、`dd of=/dev/sda`、`format C:`、`cat img > /dev/sda` |
| 下载即执行 | `curl … \| sh`、`wget … \| sudo bash`、`curl … \| python` |
| 批量删除 | `find / -name '*.log' -delete` |
| 拆访问控制 | `chmod -R 777 /` |
| 普通高危（可恢复） | `rm -rf <目录>`、`git push --force`、`shutdown`、`takeown`、`history -c` |

判定基于**结构化特征**（路径分类、参数标志、管道方向），不是「看起来像危险命令」的
模糊匹配——所以 `rm -rf ./dist` 和 `rm -rf /usr` 会被区分开，`echo hi > /dev/null`
不会误报成写盘。

### 不拦什么

* **语义级风险**：L1 只看字面，看不到意图。一句话里说「不要用 rm -rf /」也会命中。
* **分步实施的危险**：先 `cd ..` 再 `rm -rf` 这种跨轮次的组合，单条规则看不出来。

这类判定需要模型参与，属于完整版哨兵的能力范围，本模块不打算复刻。

### 启用

```toml
[bridge.sentinel]
enabled = true
```

就这样——内置后端在同一个进程里判定，不需要起服务、不需要额外进程、不需要联网。

### 配置项（`plugin.toml` 的 `[bridge.sentinel]`，或 `config.json` 的 `[sentinel]`）

| 字段 | 默认 | 说明 |
|------|------|------|
| `enabled` | `false` | 是否启用；关闭时派发路径零改动 |
| `backend` | 自动 | `builtin`（进程内）或 `http`（外部守护进程）。留空时：配了 `url` 走 http，否则用内置 |
| `url` | 空 | 外部守护进程地址（仅 `backend=http`；只允许本机回环，非回环地址直接拒绝） |
| `block_on_deny` | `true` | 命中后是否真的拒绝派发（`false` 则只记录不拦截） |
| `block_severity` | `standard` | 达到哪个严重度才拦。设成 `red_line` 就只拦不可逆操作，`rm -rf ./dist` 会放行（结果带 `suppressed: true`） |
| `attach` | `true` | 进程拉起后是否把会话登记给审计层（延迟 2 秒，best-effort） |
| `timeout` | `2.0` | HTTP 后端的单次超时（秒） |

### 行为约定

* **默认关闭**：`enabled=false` 时 `run_agent` 完全不触碰审计层，现有行为与回归测试不变。
* **fail-open**：判定异常 / 规则引擎起不来 / HTTP 后端不可达，一律**放行**并标记 `degraded`。
  安全层绝不能反过来成为可用性故障点。
* **被拦下的任务**：结果是 `AGENT_BLOCKED_BY_SENTINEL`，以 `SENTINEL_BLOCK` 状态回显，
  并带上 `rule_id` 与中文原因。
* **诊断可见**：`agent_doctor` 会显示「未启用 / 内置后端 / 外部后端已连接 / 外部后端连不上」。

### 想用完整版哨兵

上游 [silverwolftyro/sentinel](https://github.com/silverwolftyro/sentinel) 是一个独立的
桌面审计产品（GPL-3.0），包含 GUI、进程树扫描、多模型语义判定与守护者挂起。想对接它：

```bash
git clone https://github.com/silverwolftyro/sentinel
cd sentinel && python -m src.daemon.server        # 默认只绑 127.0.0.1:8787
```

```toml
[bridge.sentinel]
enabled = true
backend = "http"
url = "http://127.0.0.1:8787"
```

插件只通过公开的 HTTP 契约通信，**不 import 它任何源码**——两边的许可各自独立。
详见仓库根 [`NOTICE`](NOTICE)。

## 许可

见仓库根 `LICENSE`（随 N.E.K.O 项目）。内置安全审计模块为原创实现，同样在 MIT 之下；
与上游哨兵的关系说明见 [`NOTICE`](NOTICE)。
