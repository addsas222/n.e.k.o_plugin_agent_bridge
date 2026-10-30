# Harness 全景：CLI 与桌面，谁能插件、谁能 skill

> 调研日期 2026-10-30。来源见文末。**插件先行**是本仓库的接入顺序：
> `.mcpb`（桌面插件）→ MCP 注册 → skills 目录 → 仅 AGENTS.md。

## 0. 先分清三件事

| 层 | 是什么 | 标准 | 谁定义 |
|---|---|---|---|
| **插件** | 打包好的能力单元 | 桌面 `.mcpb`（原 `.dxt`）；CLI 侧多为 MCP server | Anthropic MCPB |
| **MCP** | 工具/资源协议（stdio 或 Streamable HTTP） | JSON-RPC | Linux Foundation / AAIF |
| **skill** | 按需加载的说明书 + 脚本 | `SKILL.md` + YAML frontmatter | 事实标准，非强制 |
| **AGENTS.md** | 常驻项目规则 | 纯 Markdown，可嵌套 | AAIF（OpenAI 捐出） |

一个 harness 可以只支持其中一两层。**我们优先用能力最强的那层**：
桌面能装 `.mcpb` 就装 `.mcpb`（一键、密钥进钥匙串、自带运行时）；
CLI 能注册 MCP 就用它自己的 `mcp add`（工具自己写配置，不靠我们猜路径）；
都不行才退回 skills 目录；再不行只剩 AGENTS.md。

## 1. CLI 型 harness（可在终端被派发）

「插件通道」列 = 该 harness 接受外部能力的首选方式。

| Harness | 厂商/来源 | 插件通道 | skill | 可派发 | 备注 |
|---|---|---|---|---|---|
| Claude Code | Anthropic | MCP + 原生 plugin（marketplace） | ✅ | ✅ 本机 | `claude mcp add`；插件是 skills 的打包单位 |
| Codex CLI | OpenAI | MCP（`codex mcp add`） | ✅ | ✅ 本机 | Apache-2.0，Rust；原生读 AGENTS.md |
| Gemini CLI | Google | MCP + `extensions` | ✅ | ✅ 本机 | `gemini extensions install/link/validate` |
| OpenCode | SST | MCP + `plugin` | ✅ | ✅ 本机 | 唯一有文档化 client/server 分离（`opencode serve` :4096） |
| Command Code | — | MCP + `--skill` | ✅ | ✅ 本机 | `-p` 取值可选；未登录 exit=3 |
| pi | earendil-works | `-e` 扩展 + `--skill` | ✅ | ✅ 本机 | 与 omp 是两个产品，共用了 `pi` 可执行名 |
| omp（Oh My Pi） | oh-my-pi | MCP 文件 + Skillshare | ✅ | ✅ 本机 | `-p` 是布尔开关，正文走位置参数 |
| Qwen Code / QwenPaw | 阿里 | MCP | ✅ | ✅/部分 | QwenPaw 走 venv，子进程要清 PYTHONHOME |
| AtomCode | AtomGit | `atomcode plugin` + `.mcp.json` | ✅ | ✅ 本机 | 登录走 OAuth；未登录 exit=1 |
| EvoX | — | 无稳定 MCP | ✅ | ✅ 本机 | 需 `--evox-cli` 才进 CLI 模式 |
| OpenClaw | OpenClaw Fdn | MCP | ✅ | ✅ 本机 | 常驻 daemon + 29 个消息通道 |
| Kimi Code CLI | Moonshot | skills + 并行 subagents | ✅ | — | 未装 |
| Grok Build | xAI | MCP + ACP | — | — | 有 script mode |
| Mistral Vibe | Mistral | skills + hooks | ✅ | — | — |
| Kilo Code CLI | Kilo | MCP + skills | ✅ | — | Roo Code 的继任者（Roo 2026-05 归档） |
| Cline CLI | Cline | MCP | ✅ | — | 也有 headless 模式 |
| Cursor CLI | Anysphere | MCP | — | — | `cursor-agent` 可脚本化 |
| Copilot CLI | GitHub | MCP | — | — | 与 Copilot 账号共享 |
| Kiro CLI | AWS | MCP | — | — | spec-driven |
| Qoder CLI | 阿里 | MCP | — | — | Quest 模式 |
| Continue CLI | Continue | MCP | — | — | — |
| Amp | Sourcegraph | ACP | — | — | ACP 原生 |
| Goose | AAIF / Block | MCP（70+ 扩展） | — | — | YAML 配置，插件走 extensions |
| Aider | — | 无 MCP | ❌ 无 skills 目录 | — | 靠 `.aider.conf.yml` 的 `read:` 读 AGENTS.md |
| OpenHands CLI | All Hands | MCP | — | — | Docker 沙箱；process 模式官方标注不安全 |
| Warp | Warp | MCP | — | — | — |
| Plandex | — | 无 | ✅ | — | plan 分支 + diff 沙箱 |
| Junie CLI | JetBrains | MCP | — | — | 可在 CI headless |
| Hermes | Nous | MCP + ACP | ✅ 自写 skill | — | — |
| AdaL CLI | SylphAI | MCP | ✅（native） | — | worker agents |
| Freebuff | Codebuff | 无 | — | — | 免 API key，广告支持 |
| Zed Agent | Zed | **ACP 宿主** | — | — | 它跑的是上面这些 harness，本身不需要 skill |
| Roo Code | Roo | MCP | ✅ | — | **2026-05 归档**，迁移到 Kilo Code |

## 2. 桌面型 harness（IDE / 独立 App）

桌面端的「插件」主要就是 `.mcpb` 或 MCP 配置；它们**不可被 `subprocess` 派发**，
所以本仓库对这类只做「装插件 / 装 skill」，不做 `agent_run` 路由。

> **第二轮补充（本机实测）**：首轮只扫了 `C:\Users\<user>`，漏掉整个 `E:\Users\<user>`
> 与 `%LOCALAPPDATA%\Programs`。补扫后新增这些**已装但未登记**的桌面 App：
>
> | App | 位置 | 配置 | 判定 |
> |---|---|---|---|
> | DeepSeek Harness | `E:\…\Programs\DeepSeek Harness\DeepSeek Harness.exe` | `~/.dsh`（3 个 preset） | 桌面版；`dsh` CLI 不在 PATH，故不可派发 |
> | Cline | `E:\…\Local\Cline\cline-app.exe` | `~/.cline`（AGENTS.md / SOUL.md / MEMORY.md） | 桌面 + VS Code + CLI 三形态 |
> | Kimi Code | `E:\…\Programs\Kimi Code\Kimi Code.exe` | `~/.kimi-code/{config.toml,mcp.json}` | 有 MCP 配置，可直接接 |
> | ZCode | `E:\…\Programs\ZCode\ZCode.exe` | `~/.zcode/{agents,skills}` | 自带 skills 目录 |
> | MiniMax Code | `~/.minimax-code`（仅 releases） | — | 桌面 App |
> | Qoder CN | `~/.qoder-cn/skills` | skills | 与 Qoder 不同安装位 |
> | CodeBuddy / CommandCode | `~/.codebuddy`、`~/.commandcode` | settings.json + skills/ + mcp.json | 之前只登记了「路由」，漏了「宿主位」 |
>
> 教训写成了工具：`tools/discover_harness.py` 同时扫**多用户目录/多盘** + `Programs/*`，
> 把「已装未登记」列出来。人工 `ls` 已经漏过两次。


| Harness | 插件通道 | 配置位置 | skill | 备注 |
|---|---|---|---|---|
| **Claude Desktop** | **`.mcpb`（首选）** / connectors / JSON | `%APPDATA%\Claude\claude_desktop_config.json` | 2026-02 起归入 Customize | 改 JSON 后必须**完全退出**再启动 |
| ChatGPT Desktop | 远程 MCP connector（https） | — | — | 本地 stdio 接不进去，除非套 HTTP 转发 |
| Cursor | MCP（JSON） | `~/.cursor/mcp.json` | `.cursor/skills`、`.agents/skills` | VS Code fork；harness 与 IDE 不可分离 |
| Devin Desktop（原 Windsurf） | MCP（JSON） | `~/.codeium/windsurf/mcp_config.json` | `~/.codeium/windsurf/skills` | 2026-06 由 Cognition 更名 |
| CodeBuddy IDE | MCP | 应用内 | 插件/IDE/CLI 三端 | CLI 侧走 `codebuddy` 路由 |
| Trae / Trae CN | MCP（应用内） | 应用内 | `~/.trae/skills`、`~/.trae-cn/skills` | 字节；Builder Mode，MCP-native |
| Qoder IDE | MCP | 应用内 | `~/.qoder/skills` | 阿里；另有 CLI 与 JetBrains 插件 |
| Kiro | MCP | `~/.kiro/settings/mcp.json` | 目录需自建 | AWS；基于 Code OSS |
| Antigravity | MCP | `~/.gemini/config/skills` | ✅ | 读 GEMINI.md、`.agents/rules/` |
| VS Code + Copilot | MCP | `~/.copilot/mcp-config.json` | `~/.copilot/skills` | 也读 `.github/skills`、`.claude/skills` |
| JetBrains + AI / Junie | MCP | IDE 设置 | — | 11 个 IDE 共用 |
| Cline / Kilo Code（扩展） | MCP | 扩展设置 | ✅ | 逐次编辑审批 |
| Zed | ACP（跑别的 harness） | — | — | 不需要自己装 skill |
| Google Jules | 无（只有 AGENTS.md + setup 脚本） | — | ❌ | 定制面最窄 |
| Positron / Visual Studio | MCP | 应用内 | — | 数据科学 / .NET 场景 |
| **Open Design** | MCP | `%APPDATA%\Open Design` | — | **UI 设计 Agent**（Electron），不是纯设计工具 |
| **Pen Agent（Pencil）** | MCP | `~/.pencil`（agent-auth / pi-sessions / models.json） | — | App 叫 Pen、配置叫 `.pencil`；内部跑 pi 会话 |
| **VS Code（Agent 宿主）** | **专用扩展**（`plugins/vscode/`）+ MCP | `%APPDATA%\Code\User\mcp.json` 或工作区 `.vscode/mcp.json` | — | MCP/skill 覆盖不到编辑器内动作，必须写扩展；Copilot 本体见上 |

## 2.5 机器人框架（宿主）

这类不是"编码 Agent"，而是**接消息平台的宿主**：它自己不写代码，用插件扩展能力。
所以它们的"插件"是按各自规范写的原生插件，跟 skill 无关。

| 框架 | 插件规范 | 配置 / 数据根 | MCP |
|---|---|---|---|
| **AstrBot**（`AstrBotDevs/AstrBot`） | `data/plugins/<name>/`：`metadata.yaml`（**name/desc/version/author 四字段必填**）+ `main.py`；主类继承 `astrbot.api.star.Star` 或类名以 `Plugin` 结尾 / 叫 `Main`；回复 `yield event.plain_result()` | `<ASTRBOT_ROOT>/data/{config,plugins,plugin_data}`，`cmd_config.json` 在主配置位 | 是（自带 MCP 客户端，配置段 `mcpServers`，支持 stdio / sse / streamable-http） |

AstrBot 的内置 star 自己也用 `skills/<name>/SKILL.md` 组织技能 —— 与 Anthropic Agent Skills 同格式，
所以 skill 层在这里是复用的。

## 3. 本仓库怎么覆盖这两类

```
providers/<id>/provider.toml     → 12 条可派发路由（CLI 侧，真跑）
skill/neko-bridge/frameworks.toml → 53 个框架的「skill 装哪 / MCP 怎么注册」
tools/build_mcpb.py               → 桌面插件 .mcpb（dist/agent_bridge-<ver>.mcpb）
webui/                            → 状态面板（12 路由 + 53 框架）
```

三条命令对应三件事：

```bash
python skill/neko-bridge/install.py            # 看 53 个框架的装机状态
python skill/neko-bridge/install.py --mcp      # 每个框架的 MCP 注册方式
python tools/build_mcpb.py                     # 打桌面插件
```

**为什么不一键全装**：`frameworks.toml` 里 `source = "docs"` 的行是**未在本机验证**的
路径（桌面 IDE 的配置位置常随版本变）。`install.py` 只往**已存在**的目录写，
不存在就标 `missing` 跳过 —— 不替某个 IDE 造目录结构。

## 4. 不进矩阵的那些

| 名字 | 为什么 |
|---|---|
| `opencode2` | 与 `opencode` 同一个包（v2.1.4），重复二进制名 |
| `paperclipai` | 安装/诊断 CLI（`install`/`doctor`/`configure`），无任务模式；要 Node ≥ 24 |
| `rea` / `rea-agents` | 逆向工程工具链（`analyze`/`capture-*`），不是编码 agent |
| `vikingbot` | 常驻 assistant + gateway，没有「派一条拿一次结果」的语义 |
| `sg` / `ast-grep` | 静态分析工具 |
| `mcode` | 本机安装已损坏（`node:internal/modules/cjs/loader`） |
| `~/.omo`（Oh my Opencode） | OpenCode 的**插件**配置（`omo.jsonc`），不是独立 harness |
| `~/.evolver` | EvoX 的子工具；宿主已由 `.evox` 覆盖 |
| `~/.hindsight`、`~/.openviking`、`~/.agentscope-runtime` | 记忆层 / MCP 插件 / 运行时，不是 harness |

判据三条，缺一不可：**能一次性执行** + **有明确非交互开关** + **是干活的 agent**。

## 5. 来源

- [codingagents.md/landscape](https://codingagents.md/landscape/) —— 23+ agent 档案、MCP/ACP/@skills 三协议、AGENTS.md 治理
- [michaellivs.com/blog/cli-coding-agents-compared](https://michaellivs.com/blog/cli-coding-agents-compared) —— 36 个 CLI agent 的能力矩阵
- [aicoderscope.com/…harnesses-architecture-comparison-2026](https://aicoderscope.com/blog/ai-coding-harnesses-architecture-comparison-2026) —— 沙箱与 harness 可分离性
- [verysmartparrot.com/directories/hierarchical-skill-directory](https://verysmartparrot.com/directories/hierarchical-skill-directory) —— 各工具 skills 目录约定
- [agentsstandard.com](https://agentsstandard.com/) —— `~/.agents/{AGENTS.md,mcp-settings.json,skills/}`
- [anthropic.com/engineering/desktop-extensions](https://www.anthropic.com/engineering/desktop-extensions) —— `.mcpb` 规范（manifest 字段、zip 布局）
- [devindex.ai/tools](https://devindex.ai/tools/) —— 桌面 IDE 清单与状态
