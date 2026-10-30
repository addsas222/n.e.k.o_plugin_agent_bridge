# agent_bridge：现有结构与路由机制，以及「其余 Agent」的接入

> 分析对象：`github.com/addsas222/n.e.k.o_plugin_agent_bridge`（`main`，2026-10-30 克隆）。
> 本文是**改动前的结构分析 + 改动后的接入说明**，所有数字与措辞都来自本机实测，
> 不是估计值。行号对应当前工作副本。

---

## 1. 结论摘要

| 项 | 改动前 | 改动后 |
|---|---|---|
| 路由登记位置 | `bridge/registry.py::AGENT_SPECS`（Python 字面量） | 内置 9 条留在原处；其余在 `providers/<id>/provider.toml` |
| 新增一个 Agent | 改 2 处源码（插件 + skill），手抄同步 | 放 1 个目录，0 行 Python |
| 已登记 Agent | 9 | **12**（+ `gemini`、`command-code`、`pi`） |
| 未登录识别 | 一张全局措辞表，只覆盖最初 9 个 | 每条路由自带 `auth_patterns` |
| 别名冲突 | `pi` 指向 `omp`（错，见 §4.3） | `pi` 独立；`pi→omp` 已移除 |
| 每个 Agent 的文档 | 无 | `providers/<id>/{SKILL.md,README.md}` |

---

## 2. 现有插件结构

```
agent_bridge/                  ← 目录名必须 == plugin.toml 的 id == entry 里的包名
├── plugin.toml                ← 清单：4 个 entries + SDK 版本区间 + [bridge] 业务段 + ui 面板
├── __init__.py           679 行 ← 插件类：4 个 @plugin_entry + 2 个 @llm_tool + 1 个 @ui.context
├── bridge/                    ← 执行层（纯标准库，可脱机单测，不 import plugin.sdk）
│   ├── registry.py       570 行 ← AGENT_SPECS / ALIASES / .cmd 垫片解析 / build_argv
│   ├── runner.py         583 行 ← 子进程派发 / 鉴权识别 / 超时 / 输出限幅
│   ├── config.py         190 行 ← BridgeConfig + 逐 Agent 覆盖 + MSYS 路径归一
│   └── tools.py           77 行 ← 结果排版 + jsonl 审计
├── config.example.toml        ← 首次运行模板（宿主复制成 config/plugin.toml）
├── ui/panel.tsx               ← Hosted 面板
├── skill/neko-bridge/         ← 反方向 skill：把 N.E.K.O 装进别的 Agent
│   ├── bin/neko.py      1267 行 ← 自带一份 AGENT_SPECS（**曾是手抄的第二份**）
│   └── install.py             ← 分发到 12 个 Agent 的 skills 目录
└── tests/
```

设计上的三个正确决定（不要推翻）：

1. **`bridge/` 不 import `plugin.sdk`** —— 所以能脱离宿主跑单测（`tests/conftest.py`
   只把插件根注册成「只有 `__path__` 的命名空间包」，绕开 `__init__.py`）。
2. **不经过 `cmd.exe`** —— `.cmd` 垫片由 `parse_cmd_shim()` 还原成真实 exe + 前缀参数；
   解析不出就返回 `AGENT_UNRESOLVED` 而不是回退 shell（见 §3.2）。
3. **`@plugin_entry` 与 `@llm_tool` 分层** —— 前者有长超时（1800s），后者被协议硬限 300s。

---

## 3. 路由机制：五层

一条任务从「模型说派给 omp」到拿到输出，经过五层。改任何一层之前先看它在哪。

### 3.1 名称归一 —— `registry.canonical_id()`

```python
key = name.strip().lower()
if key in AGENT_SPECS:  return key        # id 精确命中
return ALIASES.get(key)                   # 别名，找不到返回 None
```

顺序很重要：**id 先于别名**。所以新增一个与旧别名同名的 id 会自动「抢回」那个名字
（`pi` 就是这么修的）。

### 3.2 可执行解析 —— `registry.resolve_agent()`

```
exe_override（配置） → PATH 上的 .exe → 解析 .cmd 垫片 → cmd-fallback
```

| 结果 `source` | 含义 |
|---|---|
| `override` | 配置里 `exe_override` 指的文件存在 |
| `path` | PATH 上直接找到 PE 可执行（`.EXE`） |
| `cmd-shim` | 从 `.cmd` 里还原出真身 + 前缀参数（bun/npm 的垫片） |
| `missing` | 没找到 → `AGENT_UNRESOLVED`，拒绝派发 |
| `cmd-fallback` | 解析失败。**只允许承载自造命令行**（如 `--version`），任务正文永不进 shell |

`.cmd` 解析踩过的三类形态：直接转发 PE、`解释器 + 脚本`（npm 把参数转给无扩展名的
JS 文件）、`set` 变量间接引用（`"%REAL_BIN%" %*`）。

### 3.3 argv 构造 —— `registry.build_argv()`

```
[exe] + [垫片前缀参数] + [fixed_args 去重] + [prompt 段] + [json_args] + [model_flag] + [extra_args]
```

`prompt 段` 由 `mode` 决定，这是**本次唯一扩展的语义**（新增 `switch`）：

| mode | 产物 | 判定依据 |
|---|---|---|
| `flag` | `-p "<task>"` | 帮助写 `-p, --prompt <string>`（吃值） |
| `switch` | `-p "<task>"` | 帮助写 `--print, -p`（布尔）或 `-p [query]`（可选值） |
| `positional` | `"<task>"` | 帮助写 `[messages...]` |

### 3.4 派发 —— `runner.run_agent()`

四个「不报错但会挂死」的坑都在这层处理完了（`_execute_blocking` 的注释写得很细）：

- `stdin=DEVNULL`（否则子进程抢读父进程的 MCP stdin）；
- 阻塞式 `Popen.wait(timeout=)`，**不用** `asyncio` 的子进程等待（孙进程握着管道写端，
  `wait()` 会挂到孙进程结束，超时失效）；
- 超时 `taskkill /F /T` 连进程树一起清；
- 输出限幅但**不停止读取**（否则管道写满，子进程永久阻塞）。

### 3.5 暴露 —— `@plugin_entry` / `@llm_tool`

| 入口 | 类型 | 超时 |
|---|---|---|
| `agents_list` | entry | 1800s（`probe_versions` 会并发探测全部版本） |
| `agent_run` | entry | 1800s |
| `agent_run_parallel` | entry | 1800s（受 `allow_parallel`/`max_parallel`） |
| `agent_doctor` | entry | 1800s |
| `agent_list` | llm_tool | 60s |
| `agent_run` | llm_tool | **300s（协议硬上限）** |

`@llm_tool` 的返回契约特别容易错：**成功返回裸 payload**，只有失败才写信封
`{"output": ..., "is_error": True, "error": ...}`。自己包一层会得到双层 `output`。

---

## 4. 改动前发现的五个结构性问题

### 4.1 两张手抄的规格表

`skill/neko-bridge/bin/neko.py:171` 原文：

```
# 登记表与 agent_bridge 的 AGENT_SPECS 同源（改动请两边同步）。
```

而且**结构还不一样**：插件侧是 `@dataclass AgentSpec`（12 个字段），skill 侧是 8 元组
`(label, binary, mode, fixed_args, prompt_flag, model_flag, json_args, notes)`。
新增一个 Agent 要改两处、对齐两套形状、再记得同步部署副本。

### 4.2 新增 Agent 必须改源码，而代码目录 == 状态目录

用户插件的代码目录（`%LOCALAPPDATA%\N.E.K.O\plugins\agent_bridge\`）**同时是状态目录**
（宿主在里面生成 `config/`、`data/`）。覆盖式部署会把改过的源码连同用户配置一起抹掉。
把路由写进源码，等于每次升级都要重做一遍。

### 4.3 `pi` 被当成 `omp` 的别名（真 bug）

```python
ALIASES = { ..., "pi": "omp", ... }
```

实测两个包都提供名为 `pi` 的入口，但是**两个产品**：

```
~/.bun/bin/pi.bunx   → @earendil-works/pi-coding-agent/dist/bundle/cli.js   (v1.4.0)
~/.bun/bin/omp.bunx  → @oh-my-pi/pi-coding-agent/dist/cli.js                (v18.12.6)
```

后果：写 `pi` 跑的是 omp（看起来"正常"，所以一直没被发现），真正的 pi 永远调不到。
**已修正**：`pi` 独立登记，omp 保留 `omp` / `oh-my-pi` / `ompi`。这是有意的行为变更。

### 4.4 Agent 列表被烘进装饰器

```python
_AGENT_IDS  = [spec.id for spec in list_specs()]     # 导入期求值
@llm_tool(description=f"…（{_AGENT_HINT}）…")        # 类定义时求值
```

所以路由层**必须**在导入期加载完成。这也决定了 `providers` 的加载时机只能放模块末尾
（`_merge_providers()`），不能做成「第一次调用时懒加载」。

### 4.5 名字被硬编码在四处文字里

`plugin.toml` 的 `description` / `short_description` / `keywords`，以及 `@llm_tool` 的
`description`，都手写了一串 Agent 名。加一个 Agent 要改 4 处文案，漏一处就出现
「面板说有 9 个、实际能派 12 个」。本次把 `@llm_tool` 的描述改成 `_AGENT_HINT` 插值，
`plugin.toml` 改成「内置 9 个 + providers 动态登记」的表述。

---

## 5. 接入方案：路由数据化

### 5.1 为什么是「数据文件」而不是「每个 Agent 一个 Python 模块」

| 方案 | 新增成本 | 升级存活 | 学习成本 |
|---|---|---|---|
| 每个 Agent 写一个 `AgentSpec` 子类 | 改源码 + import | 覆盖式部署会丢 | 要读代码 |
| **每个 Agent 一个 `provider.toml`** | 放一个目录 | 目录不被覆盖 | 读文档即可 |
| 全部塞进 `bridge/config` 的 TOML 段 | 改宿主生成的配置 | 与用户配置混在一起 | 会和面板互相覆盖 |

选第二个。核心判据是 4.2：只有「独立目录」能在覆盖式部署下存活。

### 5.2 合并规则（`bridge/providers.py`）

```python
# registry.py 模块末尾
for root in provider_roots():          # 环境变量 → <插件>/providers
    if os.path.isdir(root):
        load_into(AGENT_SPECS, ALIASES, root=root, spec_factory=AgentSpec)
        break
```

`load_into` 的硬规则（都有测试守着）：

| 情况 | 行为 |
|---|---|
| id 与内置同名 | 跳过 + 告警（**不覆盖**） |
| 别名撞 id / 撞别的别名 | 丢该别名 + 告警 |
| id ≠ 目录名 | 拒绝该清单 + 告警 |
| mode 非法 / 缺 prompt_flag | 拒绝该清单 + 告警 |
| TOML 写坏 | 跳过该目录，其余照常 |

「一份坏清单只能让自己失效」是刻意设计的：宿主里一个异常会让 5 个入口一起消失。

### 5.3 单源分发

```
providers/                            ← 唯一手改源
   ↓ sync_providers.py --apply
skill/neko-bridge/providers/          ← 生成副本（随 skill 分发，不手改）
```

`tests/test_providers.py::test_skill_provider_copy_is_in_sync` 在每次 pytest 时校验，
漂了就红。这直接终结了 4.1 的「两边同步」。

---

## 6. 本次接入的三条路由

| id | 产品 | mode | argv | 未登录退出码 | 状态 |
|---|---|---|---|---|---|
| `gemini` | Gemini CLI（`@google/gemini-cli`）0.65.0 | `flag` | `gemini -p "<task>" [-m M]` | **41** | 已就绪（缺鉴权） |
| `command-code` | Command Code 1.88.0 | `switch` | `command-code -p "<task>" [--model M]` | **3** | 已就绪（未登录） |
| `pi` | pi（`@earendil-works/pi-coding-agent`）1.4.0 | `switch` | `pi -p "<task>" [--model M]` | 未观测 | 沙箱拦在 reg.exe |

别名：`gcli`/`geminicli` → gemini；`cmdc`/`commandcode` → command-code；
`pi-coding-agent`/`earendil-pi` → pi。

**没有登记 `cmd` 作为 command-code 的别名** —— 它在 Windows 上是 cmd.exe，让模型
用 `agent="cmd"` 派任务是不必要的歧义。

### 6.1 为什么每条都必须带 `auth_patterns`

改之前用真实输出验过全局表：

```
gemini         -> *** MISSED ***
command-code   -> *** MISSED ***
claude(对照)   -> DETECTED
```

漏判的后果不是「假成功」（它们退出码非 0），而是**说不清**：被报成普通 `FAILED`，
不进 `needs_login` 集合（后续不会被跳过），也不提示「换一个已登录的」。
补上之后实测：

```
gemini        exit_code=41  needs_login=True  login_hint=设 GEMINI_API_KEY…
command-code  exit_code=3   needs_login=True  login_hint=cmdc login…
```

---

## 7. 每个 Agent 的目录结构与文档

三条路由都用同一个形状（一个目录 = 一条路由 + 一份 skill + 一份运维说明）：

```
providers/<id>/
├── provider.toml    ← 声明（机器读）。id/binary/mode/prompt_flag/auth_patterns/…
├── SKILL.md         ← skill（Agent 读）。何时用、三种调用方式、实际 argv、
│                      与别家不同的地方、验证命令
└── README.md        ← 运维（人读）。安装、登录、配置覆盖表、已知限制
```

| 目录 | SKILL.md 的 name | 特别说明 |
|---|---|---|
| `providers/gemini/` | `neko-route-gemini` | `-o json` 会换掉 stdout 形状 |
| `providers/command-code/` | `neko-route-command-code` | `--max-turns` 命中上限退出码是 **8** |
| `providers/pi/` | `neko-route-pi` | 开头就写「pi ≠ omp」对照表 |

**skill 命名约定**：`neko-route-<id>`。前缀统一，在 skills 列表里自动聚成一组；
`<id>` 与路由 id 一致，不会出现「skill 名和 agent 名对不上」。

---

## 8. 注册方式 / 命名约定 / 配置项

完整字段表在 `providers/README.md`，这里只列要点。

**注册三步**：建目录 → 写 `provider.toml` → 重启或 `POST /plugin/agent_bridge/reload`。
注意 `/plugins/refresh` 只重扫注册表、**不重新 import 模块**，路由不会刷新。

**命名**：id 小写 + 连字符，必须 == 目录名；别名全局唯一；`cmd`/`sh`/`node`/`python`
这类 shell 名不要占。

**必填项**：`schema`、`id`、`binary`、`mode`、`prompt_flag`（flag/switch）、
`auth_patterns`。

**运行期配置**（改宿主生成的 `config/plugin.toml`，不动清单）：

| 键 | 作用 |
|---|---|
| `enabled` | 关掉后列表与派发都跳过 |
| `exe_override` | 指到真实 exe（垫片解析失败时唯一出路） |
| `model` | 等价于每次派发带 `model_flag` |
| `timeout_seconds` | 单 Agent 超时，受 `max_timeout_seconds` 夹住 |
| `extra_args` | 追加参数（`--yolo`、`--no-skills` 需显式写） |
| `env` | 子进程额外环境变量 |

---

## 9. 验证与回归

```bash
# 1. 清单层（不联网、不跑 CLI，1 秒）
"C:/Python314/python.exe" -m pytest tests -q
#    → 29 passed, 1 skipped（跳过项需要 NEKO_TEST_LIVE_CLI=1）

# 2. 插件侧合并结果
python -c "import sys;sys.path.insert(0,'.');from bridge.registry import AGENT_SPECS,provider_source;print(provider_source());print(len(AGENT_SPECS))"
#    → …\agent_bridge\providers / 12

# 3. skill 侧（应与插件侧完全一致）
python skill/neko-bridge/bin/neko.py agents --json

# 4. 实弹派发
python skill/neko-bridge/bin/neko.py dispatch gemini "只回一个字：好" --json
#    → exit_code=41, needs_login=true, login_hint=...

# 5. 副本未漂
python skill/neko-bridge/sync_providers.py --check
```

`pytest` 期望的解释器是 `C:\Python314\python.exe`（自带 pytest 9.1.1）；
本机默认的 `python`（3.13）没装 pytest。

**必须用目录名 `agent_bridge` 检出**：`tests/conftest.py` 把插件根预注册成名为
`agent_bridge` 的命名空间包。若把仓库 clone 成别的目录名（例如 `repo_src`），
pytest 会把根 `__init__.py` 当普通包导入，直接 `ModuleNotFoundError: plugin.sdk`。

---

## 10. 边界：扫到但没接的

判据三条缺一不可：能一次性执行、有明确非交互开关、是"干活的 Agent"而非"配置自己的工具"。

| CLI | 判定 | 依据 |
|---|---|---|
| `opencode2` | 不接 | 与 `opencode` 同一包（均 v2.1.4），重复二进制名 |
| `paperclipai` | 不接 | 安装/诊断 CLI（`install`/`doctor`/`configure`），无任务模式；要求 Node ≥ 24 |
| `rea` / `rea-agents` | 不接 | 逆向工程工具链（`analyze`/`capture-*`），不是编码 Agent |
| `vikingbot` | 不接 | 常驻 assistant + gateway，没有「派一条拿一次结果」的语义 |
| `sg` / `ast-grep` | 不接 | 静态分析工具 |
| `mcode` | 不接 | 本机安装已损坏（`node:internal/modules/cjs/loader` 报错） |

---

## 11. 迁移注意

1. **`pi` 语义变了**：以前 `agent="pi"` 跑 omp，现在跑真正的 pi。依赖旧行为的脚本
   把 `pi` 改成 `omp`。`oh-my-pi` / `ompi` 仍指向 omp。
2. **版本号**：`plugin.toml` 与面板里的 `version` 从 `0.1.0` → `0.2.0`；
   skill 从 `0.2.0` → `0.3.0`。
3. **部署**：`providers/` 必须随插件目录一起拷（它是插件的一部分），
   且**只增不删**（代码目录就是状态目录，`config/` 与 `data/` 是宿主的）。
4. **`pi` 的 `auth_patterns` 是推断值**：本机沙箱把它拦在 `reg.exe` 程序黑名单上，
   没拿到真实未登录文本。换到无此限制的环境后请实测并替换成原文措辞。
