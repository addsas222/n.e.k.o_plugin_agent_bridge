# providers/ —— Agent 路由的数据登记层

这里放的是「一个 Agent 怎么被调用」的**声明**，不是代码。

`bridge/registry.py` 里那张 `AGENT_SPECS` 表是内置的 9 个；其余 Agent 全部在
`providers/<id>/provider.toml` 里登记。插件启动时（**导入期**）扫描本目录并合并，
所以新增一个 Agent = 放一个目录，不改任何 Python、也不用改 `plugin.toml`。

```
providers/
├── README.md                  ← 本文件（字段与约定）
├── gemini/                    ← 路由 id「gemini」
│   ├── provider.toml          ← 声明（机器读这个）
│   ├── SKILL.md               ← 对接该 Agent 的 skill（人读：怎么调、有什么坑）
│   └── README.md              ← 运维说明（安装、登录、配置覆盖）
├── command-code/
└── pi/
```

---

## 一、注册方式

三步，全在文件系统里：

1. **建目录**：`providers/<id>/`，目录名就是路由 id。
2. **写 `provider.toml`**：至少 `schema`、`[provider].id`、`binary`、`mode`，以及
   `flag`/`switch` 模式下的 `prompt_flag`；**并且必须有 `auth_patterns`**（见第四节）。
3. **让它生效**：
   - 代码放在宿主插件目录时 —— 重启 N.E.K.O，或
     `POST /plugin/agent_bridge/reload`（单插件 reload 才会重读）。
     ⚠️ `/plugins/refresh` **不会**重新 import 模块，路由不会刷新。
   - 只是用 skill 时 —— 直接 `python bin/neko.py agents`，每次进程启动都会重扫。

写完自查：

```bash
python -m pytest tests/test_providers.py -q     # 清单合法性 / 别名冲突 / argv 形态
python skill/neko-bridge/bin/neko.py agents --json | grep '"agent": "<id>"'
```

### 为什么必须「导入期」加载

`__init__.py` 把 Agent 列表烘进了装饰器：

```python
_AGENT_IDS  = [spec.id for spec in list_specs()]
_AGENT_HINT = "、".join(_AGENT_IDS)
@llm_tool(description=f"…（{_AGENT_HINT}）…")   # 类定义时就求值
```

改成运行期懒加载，新增的 Agent 会「能派发但不在工具描述里」——模型永远看不到它，
等于没接。所以 `registry.py` 在模块末尾调用 `_merge_providers()`，宁可启动慢几毫秒。

---

## 二、命名约定

| 对象 | 规则 | 例子 |
|---|---|---|
| **目录名 / id** | `^[a-z][a-z0-9-]{1,31}$`，小写、可含连字符，**必须等于目录名** | `gemini`、`command-code` |
| **别名 `aliases`** | 小写，全局唯一（不能撞 id、不能撞别的别名） | `cmdc`、`gcli` |
| **label** | 给人看的名字，可中文，带产品方 | `Gemini CLI（Google）` |
| **binary** | PATH 上查找的可执行名；省略时取 id | `command-code` |
| 文件名 | `provider.toml`（Python 3.10 及更早用 `provider.json`） | — |

**id 用连字符还是下划线？** 用连字符。宿主的插件 id 允许 `[A-Za-z0-9_-]`，但路由 id
会出现在 `agent="..."` 参数和 URL 里，连字符更安全（不会和 Python 标识符混淆）。

**为什么坚持「目录名 == id」**：宿主定位插件就是这条规矩（`describe_plugin_entry_directory_mismatch`）。
路由层沿用同一条，避免出现「列表里叫 a、目录叫 b」这种只能翻文件才对得上号的状态。
不符会**拒绝加载并告警**，不会猜。

**哪些名字不要占**：`cmd`、`sh`、`bash`、`node`、`python` —— 它们是 shell / 解释器。
`command-code` 的横幅自称 `cmd`，本仓库**故意不登记** `cmd` 这个别名：
让模型用 `agent="cmd"` 去派任务，歧义成本远大于便利。

---

## 三、清单字段全集

```toml
schema = 1                       # 结构版本，目前只有 1

[provider]
id            = "gemini"         # 必填，== 目录名
label         = "Gemini CLI（Google）"
binary        = "gemini"         # 省略时取 id
mode          = "flag"           # flag | switch | positional（见下）
prompt_flag   = "-p"             # mode=flag/switch 时必填
model_flag    = "-m"             # 可选，支持 --model 的 CLI 才写
fixed_args    = ["run"]          # 可选，必须排在最前面的参数（子命令）
json_args     = ["-o", "json"]   # 可选，json_output=true 时追加
version_args  = ["--version"]    # 可选，默认就是它
aliases       = ["gcli"]         # 可选，额外调用名
notes         = "一句话说明"      # 会在 agents/doctor 里回显
hints         = ["坑 1", "坑 2"]  # 运维提示，回显给模型/人
auth_patterns = ["please set an auth method"]   # 必填（见第四节）
login_hint    = "设 GEMINI_API_KEY"             # 未登录时怎么修
```

### 三种 `mode`：任务正文怎么交出去

| mode | argv 形状 | 适用 | 判据 |
|---|---|---|---|
| `flag` | `cli -p "<task>"` | 开关**吃值** | 帮助里写 `-p, --prompt <string>` |
| `switch` | `cli -p "<task>"` | 开关**是布尔**，正文走位置参数 | 帮助里写 `--print, -p`（后面没有 `<value>`），或 `-p [query]` |
| `positional` | `cli run "<task>"` | 任务就是位置参数 | 帮助里写 `cli [options] [messages...]` |

`flag` 与 `switch` 拼出来的 argv 一模一样 —— 分开登记是为了让清单**自解释**。
「`-p` 后面到底跟不跟值」是接新 CLI 最容易写错的一处：写成 `flag` 但 CLI 其实是布尔，
任务正文会变成位置参数以外的东西；反过来写成 `switch` 而 CLI 吃值，正文会被当成
下一个参数的开头。**对着 `--help` 抄，别猜。**

`fixed_args` 会与 shim 里已内联的旗标去重（`evox.cmd` 自带 `--evox-cli` 就踩过）。

---

## 四、`auth_patterns` 是必填项，不是可选项

**「文件在」不等于「能用」。** 这些 CLI 未登录时不一定给非 0 退出码，也不一定报错得
让你看得懂。实测三种表现：

| Agent | 未登录时退出码 | stderr |
|---|---|---|
| `claude` | **0** | `Authentication required. Please use /login …` |
| `gemini` | **41** | `Please set an Auth method … GEMINI_API_KEY …` |
| `command-code` | **3** | `Error: Authentication failed. Please run "cmdc login" first.` |

`bridge/runner.py` 里有一张全局特征表 `_AUTH_PATTERNS`，但它是**按最初 9 个 CLI 的
真实措辞**写的。换一个产品就漏 —— 上面两条新措辞都不在里面，而它们的退出码又恰好
非 0，于是只被报成普通 `FAILED`：不进 `needs_login` 集合（后续不会被跳过），也不提示
「这台没登录，换一个」。

所以每个清单必须自带该产品的原文措辞（正则片段，大小写不敏感）：

```toml
auth_patterns = ["please set an auth method", "specify one of the following environment variables"]
```

**怎么取到原文**：故意在未登录状态下跑一次，把 stderr 抄下来。

```bash
gemini -p "hi"; echo "exit=$?"            # 别用管道！$? 会变成 tail 的退出码
```

> 这条踩过：`gemini -p hi | tail -6; echo $?` 打出 `0`，于是误判成「假成功」。
> 管道的退出码来自最后一个命令，不是 CLI。

另外三条经验：

- 只看 stderr **开头 400 字符**，且要求 stdout 没有实质内容（防止把「写个登录页」
  的正文误判）；
- 特征别写得太宽（`"api key"` 会命中正常回答）；用产品特有的一整句；
- 没验证过的措辞要写明「推断值」——`providers/pi/provider.toml` 就是这种情况。

---

## 五、冲突策略（都不静默）

| 情况 | 行为 |
|---|---|
| provider 的 id 与内置 Agent 同名 | **跳过** + 告警。绝不覆盖内置的调用形态 |
| 别名撞到已登记的 id | 丢该别名 + 告警 |
| 别名被别的 provider 占用 | 丢该别名 + 告警 |
| `id` 与目录名不一致 | 拒绝加载该清单 + 告警 |
| `mode` 非法 / 缺 `prompt_flag` | 拒绝加载该清单 + 告警 |
| TOML 写坏 | 跳过该目录 + 告警，其余照常 |

一句话：**一份坏清单只能让自己失效，不能影响别人，也不能改掉内置 Agent。**

想改内置 Agent 的调用形态，走运行期配置（看得见、可回滚）：

```toml
# %LOCALAPPDATA%\N.E.K.O\plugins\agent_bridge\config\plugin.toml
[bridge.agents.claude]
enabled = false
exe_override = "C:\\full\\path\\to\\claude.exe"
model = "sonnet"
timeout_seconds = 900
extra_args = ["--verbose"]
```

| 配置项 | 作用 |
|---|---|
| `enabled` | 关掉后 `agents_list` / `agent_run` 都跳过它 |
| `exe_override` | 指到真实可执行文件（`.cmd` 垫片解析失败时的唯一出路） |
| `model` | 等价于每次派发都带 `model_flag` |
| `timeout_seconds` | 单 Agent 超时，受 `[bridge].max_timeout_seconds` 夹住 |
| `extra_args` | 追加参数（`--yolo`、`--no-skills` 这类都要显式写） |
| `env` | 该 Agent 子进程的额外环境变量 |

---

## 六、查找顺序

`bridge/providers.py::provider_roots()` 与 skill 的 `_provider_roots()` 都按这个顺序，
**取第一个存在的目录**：

1. 环境变量 `NEKO_BRIDGE_PROVIDERS`（测试 / 临时切换用）；
2. `<插件目录>/providers`（部署后的位置；源码树里就是仓库根）；
3. skill 自带的 `skill/neko-bridge/providers`（随 skill 分发的那份副本）。

第 3 项是 `sync_providers.py` 从第 2 项生成的**产物**，不手改：

```bash
python skill/neko-bridge/sync_providers.py --check    # CI/提交前校验，漂了退出码 2
python skill/neko-bridge/sync_providers.py --apply
```

`tests/test_providers.py::test_skill_provider_copy_is_in_sync` 会把这条守住。

---

## 七、什么才算「一个 Agent」

接新路由前先判断该不该接。本机的实测判定（2026-10-30）：

| CLI | 判定 | 依据 |
|---|---|---|
| `gemini` | ✅ 接 | `-p/--prompt` 非交互，`-m/--model`，有独立产品身份 |
| `command-code` | ✅ 接 | `-p/--print` 非交互，`--model`，`--output-format` |
| `pi` | ✅ 接 | `--print/-p` 非交互，`--model`，与 omp 是**两个产品** |
| `opencode2` | ❌ 不接 | 与 `opencode` 同一个包（v2.1.4），只是重复的二进制名 |
| `paperclipai` | ❌ 不接 | 是**安装/诊断** CLI（`install`/`doctor`/`configure`），没有一次性任务模式；且要求 Node ≥ 24 |
| `rea` / `rea-agents` | ❌ 不接 | 逆向工程工具链（`analyze`/`compare`/`capture-*`），不是编码 Agent |
| `vikingbot` | ❌ 不接 | 常驻 assistant + gateway，没有「派一条任务拿一次结果」的语义 |
| `sg` / `ast-grep` | ❌ 不接 | 静态分析工具，不是 Agent |

判据三条，缺一不可：

1. **能一次性执行**：给一段任务文本，跑完退出（不是常驻服务、不是交互 TUI）；
2. **有明确的非交互开关**：`-p` / `--print` / `run` 子命令，且能在 `--help` 里看到；
3. **是"能干活"的 Agent**，不是"配置它自己"的工具。

---

## 八、新增一个 Agent 的检查清单

```
[ ] 目录名 == id，全小写，含连字符
[ ] mode 对着 --help 抄对了（尤其 flag 与 switch 的区别）
[ ] prompt_flag / model_flag 在真机上验过一次 dry-run argv
[ ] auth_patterns 抄的是真实未登录 stderr 原文
[ ] login_hint 给了可执行的修复动作
[ ] hints 写了至少一条「与别家不同」的地方
[ ] SKILL.md 有调用方式、实际 argv、验证命令
[ ] README.md 有安装、登录、配置覆盖、已知限制
[ ] python -m pytest tests/test_providers.py -q  全绿
[ ] python skill/neko-bridge/sync_providers.py --apply
[ ] python bin/neko.py dispatch <id> "只回一个字：好"   真跑一次
```
