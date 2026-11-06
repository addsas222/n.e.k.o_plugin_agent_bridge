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

## 安全审计集成（内置，可选）

插件自带一整套安全审计器（`bridge/sentinel_core/`，**MIT**，纯标准库）。它不做桌面
界面、不打包 exe，只做审计本身：派发前看一眼能不能做、跑起来盯着有没有跑偏、
出事之后能查、能回滚、能通知。

### 能力清单

| 能力 | 做什么 | 默认 |
|------|--------|------|
| **L1 规则** | 派发前与运行中的红线/高危命令拦截 | 随 `enabled` 开启 |
| **L2 语义** | 意图漂移检测：单个动作都无害，合起来却在离开目标 | 关（要调模型，按 token 计费） |
| **事件账本** | SQLite 记录会话、事件、告警、安全点 | 随 `enabled` 开启 |
| **时间线** | 把事件串成「它是怎么一步步走到这一步的」，自动折叠噪声 | 随账本 |
| **晨报** | 聚合昨夜事件成一段早上能直接读的摘要 | 随账本 |
| **Token 审计** | **字段在、采集源不在**：各 CLI 不对外暴露 token 数，`input_tokens`/`output_tokens` 恒为 0。**不要拿它做成本核算** | 未接线 |
| **安全点** | 派发前在 git 工作区打快照（`git stash create`），支持回滚 | 关（`safepoint`，约 0.9s/次） |
| **进程干预** | 终止进程树（**不做挂起**，原因见下） | 只告警（`guard_action`） |
| **告警通道** | 本地日志 + webhook 推送 | 仅本地 |
| **时间盒** | 单个任务吃掉整轮时预警（只报不中断） | 关 |

对应四个入口：`sentinel_status`（各层状态）、`sentinel_timeline`（案发时间线）、
`sentinel_briefing`（晨报）、`sentinel_rollback`（回滚到安全点）。

### L1 能拦什么

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

### L1 不拦什么

* **语义级风险**：L1 只看字面。说「不要用 rm -rf /」也会命中——这是已知误报。
* **分步实施的危险**：先 `cd ..` 再 `rm -rf` 这种跨轮次组合，单条规则看不出来。

这两类要模型参与，属于 L2 的职责：

```toml
[bridge.sentinel]
enabled = true
l2_enabled = true      # 需要宿主模型网关，并在 plugin.toml 声明 [plugin.models.judge]
```

L2 装了三道成本闸门：最小判定间隔（`l2_min_interval`，默认 20s）、每次只喂最近
N 个动作（`l2_window`）、每小时调用上限（`l2_max_calls_per_hour`）。模型不可用时
（未绑槽位 403、能力不匹配 409、超时）它**静默降级**，只剩 L1 在守——绝不让派发失败。

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
| `store_path` | 空 | 事件账本（SQLite）路径；留空放插件 data 目录 |
| `retention_days` | `30` | 账本保留天数，超期记录在启动时清理 |
| `l2_enabled` | `false` | 开 L2 语义判定（要调模型、按 token 计费） |
| `l2_min_interval` | `20.0` | 两次 L2 判定的最小间隔（秒），防逐行触发烧穿 |
| `l2_window` | `10` | 每次判定只喂最近多少个动作 |
| `l2_max_calls_per_hour` | `120` | 每小时 L2 调用硬上限 |
| `guard_action` | `alert` | `alert` / `terminate`，**默认只告警** |
| `safepoint` | `false` | 派发前打安全点（`git stash create`，不动工作区）。每次约 0.9s，且会在目标仓库留一个 `refs/sentinel/sp-*` |
| `webhook_url` | 空 | 告警 webhook（仅 http/https）；留空只走本地通道 |
| `timebox_seconds` | `0` | 单任务时间盒上限（秒）；`0` 关闭 |

### 行为约定

* **默认关闭**：`enabled=false` 时 `run_agent` 完全不触碰审计层，现有行为与回归测试不变。
* **fail-open**：判定异常 / 账本写不进 / 模型不可用 / webhook 不通，一律降级放行。
  审计是增强层，绝不能反过来成为可用性故障点。
* **最小干预**：终止不可逆，所以 `guard_action` 默认 `alert`，必须显式开启。
  干预失败也只是降级成一条告警，不会重试。
* **为什么没有「挂起」**：`guard_action` 只有 `alert` / `terminate` 两个选项。
  挂起的完整语义需要「恢复」那一半，而插件里没有「暂停等人确认」的交互入口 ——
  缺了恢复，「挂起」的实际行为是「把 Agent 冻住，然后什么都不做，直到吃满
  dispatch timeout 被杀」：它比 `alert` 糟（没保护住任何东西），也比 `terminate`
  糟（制造「任务莫名变慢」的困惑，日志里看不到干预痕迹）。而「限时自动恢复」
  更坏，恢复后危险命令继续跑，是虚假的安全感。`guard.py` 仍保留
  `suspend`/`resume` 作为能力，只是策略层不选它。
* **可逆性非对称**：`guard.py` 的身份校验对动作区别对待 —— `resume`/`suspend`
  在无法确认 pid 身份时**放行**（可逆，最坏是误碰一下），`terminate` **拒绝**
  （不可逆，宁可不杀）。一个规则套三种动作会造出「冻住了、解不开、也杀不掉」。
* **绝不阻塞派发**：输出处理走内存队列 + 独立消费线程（上限 2000，满了丢弃并计入
  `dropped_events`），干预动作再套一层线程。这条是硬要求——审计线程一旦卡住，
  Agent 子进程会卡在写管道上，症状是「任务莫名超时」或「同样命令时快时慢」，
  而且日志里看不到任何干预痕迹，极难排查。
* **被拦下的任务**：结果是 `AGENT_BLOCKED_BY_SENTINEL`，以 `SENTINEL_BLOCK` 状态回显，
  并带上 `rule_id` 与中文原因。
* **诊断可见**：`agent_doctor` 显示「未启用 / 内置后端 / 外部后端已连接 / 外部后端连不上」；
  `sentinel_status` 显示各层开关与丢弃计数。

### 已知锐边

* **被判成「需要登录」的 Agent 会被直接跳过**：一旦某次调用命中鉴权特判，该 Agent 进
  `_needs_login`，之后的调用**连试都不试**，只能通过重载插件解除。判据是输出**形态**的
  启发式（`bridge/runner.py::_has_substantive_output`），不是权威判定：只认「省略号收尾 /
  动名词开头 / 工具标记前缀」这三种**可识别的进度形态**，其余一律算有产出。判不准时偏向
  放过——另一侧的代价是「这个 Agent 从此不可用」，与「多一句提示」不是一个量级。
  噪声表（Node/npm 的告警横幅）走同一原则：**只有整行确认是横幅才算噪声**，模板未列出的
  一律算产出。npm 的 `ERR!` 因此分成两档——`A complete log of this run...` / `errno` /
  `at ...` 归噪声，`code E404` 这类结论行算产出（详见 `_NOISE_RE` 注释）。
* **纯文本方言下每一行都会被记成 `goal`**：`Runtime._parse_line` 每行调一次
  `get_adapter(agent)`，而 `get_adapter` 按设计每次返回**新实例**（`adapters/text.py` 的
  docstring 明写「每次都返回新实例正是为此」，即让调用方自己持有它），于是
  `TextAdapter._goal_seen` 每行都从 `False` 开始，非错误行一律判成 `KIND_GOAL`。
  今天**没有任何代码按 `kind` 分支**（`KIND_GOAL` 只在 `adapters/base.py:61` 定义、
  无人引用），所以不影响拦截与判定，只污染账本里的 `kind` 字段——时间线上每条都标成
  「目标」。修法是在 `Runtime` 里按 agent 缓存适配器实例，`_parse_line` 改成 get-or-create，
  顺带让 `_goal_seen` 真正生效；改它要动解析路径，留到下一轮。
* **哨兵收尾等不到「最后一条」时会静默继续**：`_drain` 的 3 秒上限到点就返回、**不报错
  也不记录**，而 `finish_session` 无条件往下走。**触发条件不是「罕见」**：队列里还剩
  ≥2 条、且单条处理超过约 1.5 秒就够（`_maybe_judge` 要调模型、`_maybe_intervene` 里的
  `taskkill` 最长 10 秒，都在这个量级）——偏偏是「证据最密集」的那种会话最容易撞上。
  后果是：末尾几条事件可能没进账本、时间线与账本的先后顺序可能
  颠倒、`doctor()` 的 `dropped_events` 只统计「入队被拒」不统计「在途超时」。一旦碰上，
  代价就是「最该看到的那条告警没发出去」。修法是让 `_drain` 返回是否排空、超时就写一条
  `warning` 并计入 `dropped_events`——但它会改动 `finish_session` 的收尾路径，留到下一轮做，
  不在本版风险最高的函数上做收尾改动。
* **会话面板分不清「还在跑」与「进程被强杀」**：账本只记 `ended_at`，进程被强杀时它
  永远是 `NULL`，所以 `active = true` 的准确含义是**「没有收尾记录」**，而不是「正在
  运行」。面板照这个事实老实写「未收尾」（**不写「进行中」**——那是替账本断言一件它
  不知道的事），**刻意不做「挂了多久算僵死」的时间阈值**——
  那既可能是强杀（真异常），也可能是跑了几小时的构建/训练（完全正常），同一个阈值把
  两件事标成同一种状态；而且它得用浏览器时钟去判断账本里的时间戳，两边偏差用户看不见。
  真要检测僵死会话，正确做法是后端按 `pid` 查进程是否还活着（`sessions.pid` 已经在记），
  拿到的是事实而不是猜测——等 `attach_pid` 那种长驻会话真正接入后再做，留到下一轮。
  **做的时候必须同时记进程启动时间戳或命令行指纹**：光看 pid 存在与否会被 pid 复用骗到，
  那是唯一能把「死的」说成「活的」的路径，比今天一律报「未收尾」更坏。
* **统计数字可能是下限，`stats_partial` 就是在说这件事**：`store.stats()` 的聚合不可用
  时，计数会退回 `len(时间线)`，而那份列表被 `scan_limit`（`max_items=1` 时 200）截过。
  这时 `build_briefing()` 返回 `stats_partial: True`，面板给数字加 `≥`、晨报在计数行尾注
  「统计不完整，以上为下限」，**并且不再说「昨夜平安」**——那是对全量的断言，拿被截断的
  样本说不出来。判据按维度各判一次：只有「该计数缺失」且「顶替它的列表顶满上限」才算部分，
  所以正常夜间数据、以及「聚合失败但只捞回 5 条」都不会被误标。
  `by_severity` / `blocked` 在聚合失败时会从事件行重算，否则晨报会出现「结论：昨夜平安」
  但下面正列着三条 `[红线]` 的自相矛盾。
* **`block_severity` 在本地与服务端读同一个配置项**：设成 `red_line` 时只拦不可逆操作
  （`rm -rf ./dist` 这类可恢复删除会放行，但仍带 `rule_id` 回账本）。不会出现「服务端放宽
  了、本地还在拦」——两边判据同源。
* **安全点默认关**：`safepoint = false`（每次约 0.9s，且会在目标仓库留下
  `refs/sentinel/sp-*`）。关着时 `sentinel_rollback` 没有可用快照，直接返回
  「没有可用安全点」。

### 外部后端（可选）

内置这套是**独立实现**，与上游 [silverwolftyro/sentinel](https://github.com/silverwolftyro/sentinel)
（GPL-3.0 桌面产品，含 GUI 与进程树扫描）没有代码关系。

如果你已经在用上游那套、只想复用它的守护进程来做 L1 预检：

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

此时只有 L1 预检走外部进程；账本、时间线、晨报、安全点这些仍然用插件自己的。
两边只通过公开 HTTP 契约通信，**不 import 上游任何源码**，许可各自独立——详见仓库根
[`NOTICE`](NOTICE)。

## 隐私与数据留存（重要）

### 账本默认**明文**保存在本机

安全审计的账本是一个 SQLite 文件，**默认不做磁盘加密**：

| 插件形态 | 默认路径 |
| --- | --- |
| MCP / 桌面插件 | `~/.neko/agent_bridge/sentinel.db` |
| MCP 路径（Windows Roaming） | `~/AppData/Roaming/neko/agent_bridge/sentinel.db` |
| 独立运行 / 测试 | `./data/sentinel.db`（当前工作目录） |

里面会记下：会话指纹、工作目录、任务目标、事件摘要与细节、原始输出片段、告警正文、
Token 用量、安全点 ref。**这些内容包含你的命令原文与 Agent 的输出**。

**密钥会被打码**：落盘前所有自由文本字段（`summary` / `detail` / `raw` / `title` /
`body` / `label` / `goal`）都过一遍脱敏（`bridge/sentinel_core/redact.py`），
`sk-…`、`ghp_…`、`AKIA…`、`xox…`、JWT、PEM 私钥整块、`Authorization: Bearer …`、
`token=…` 这类 `key=value`、URL 里的 `user:pass@` 都会被替换成 `***`。

这是**正则启发式，不是加密，也不是密钥检测器**：

* 规则之外的自定义凭据格式（内部票据、自研 `X-Acme-Key`）不会被打码；
* 打码**不可逆**——账本不是密钥库，需要原文请自己重跑命令；
* 无法保证零漏判。**不要把生产环境的长期凭据通过这个插件跑**。

### 保留期与关闭

* 保留期由 `retention_days`（默认 30 天）控制，超出后 `prune()` 删除过期事件、
  已投递告警与 Token 记录；安全点在下次派发时按「30 天 / 保留最近 50 个」回收
  git ref（`refs/sentinel/*`）。
* **关掉账本 = 关掉整个安全审计**：`[bridge.sentinel] enabled = false`。此时不落盘、
  晨报与时间线无数据，L1 拦截也一并关闭。
* ⚠️ **目前没有「只关账本、保留 L1」的开关**。`store_path = ""` 表示「用默认路径」，
  **不是**关闭——写代码的人很容易把它误当成关掉。若你只需要 L1 且完全不希望落盘，
  把 `store_path` 指到一个临时目录（如 `store_path = "D:/tmp/neko-sentinel.db"`）
  并自行定期删除，或在上层每次派发后清理。
* 手动删除：停掉插件后直接删掉上面的 `.db` 文件（连同 `-wal` / `-shm`）。

### 开 webhook 时会把什么发出去

`webhook_url` 一通，每条告警的整行记录都会 POST 出去，包括：级别、标题、正文
（通常含命中的命令原文）、关联的事件与 `id`、时间戳、会话指纹。外发前会**再过一次**
脱敏（纵深防御，与落盘同一套规则），但内容本身仍是「你的命令与输出摘要」。

因此：

* **非本机地址强制 https**。`http://` 只允许指向 `127.0.0.0/8` / `::1` / `localhost`；
  指向外网会在启动时 warning，并**拒绝发送**（不会静默降级成只发标题）。
* **不跟随跨源重定向**。只有 scheme + host + port 完全相同的 302 才会重发一次，
  避免 `Authorization` 头跟着跳转跑到第三方主机。
* 通道失败不会重试轰炸：`failed` 计数如实上报，本地通道已收到就不影响告警留存。

## 许可

见仓库根 `LICENSE`（随 N.E.K.O 项目）。内置安全审计模块为原创实现，同样在 MIT 之下；
与上游哨兵的关系说明见 [`NOTICE`](NOTICE)。
