# Agent Bridge for VS Code

> **三步上手**
> 1. 装好扩展 → 左下角出现 `$(hubot) x/13 可用`
> 2. 按 `F1` 输入 **`Agent Bridge: ① 诊断`** —— 它会告诉你缺什么、并给你「启动控制台」按钮
> 3. 按 `Ctrl+Alt+A` → 选 Agent → 写任务 → 回车

装完不知道点哪里，就按 `F1` 打「Agent Bridge」，五条命令按序号排好。

## 为什么之前"点了没反应"

三个常见原因，现在都有兜底：

| 症状 | 原因 | 现在的处理 |
|---|---|---|
| 命令面板里有，按了没反应 | `activate()` 抛异常 → 所有命令失联 | 整体 `try/catch`，弹窗 + 输出面板打栈 |
| 启动后很久才能用 | `onStartupFinished` 才激活 | 每个命令都声明 `onCommand:*`，按下即激活 |
| 状态栏一直灰/没数字 | 本地控制台没跑 | 状态栏显示「控制台未启动 · 点我修」，点一下给启动按钮 |

## 五条命令

| 命令 | 快捷键 | 干什么 |
|---|---|---|
| `① 诊断（用不了先点这个）` | — | 逐项检查：仓库目录 / Python / 控制台 / 可用 Agent / MCP，每项都给修复动作 |
| `② 派发任务` | `Ctrl+Alt+A` | 两步：选 Agent → 写任务。灰掉的 Agent 派不出去 |
| `③ 看全部 Agent 状态` | — | 输出面板里列出 `✔ 可派发 / ! 需登录 / ✖ 没装` |
| `启动本地控制台` | — | 在终端里跑 `python webui/server.py` |
| `设默认 Agent` | — | 省掉每次选 |

诊断的输出长这样：

```
✔ 找到 agent_bridge 仓库
    C:\Users\…\agent_bridge
✔ Python 可用
    Python 3.13.12
✔ 控制台可达
    http://127.0.0.1:8787 · 13 条路由
✔ 有可用的 Agent
    12/13 就绪；需登录：gemini、command-code、pi、omp
✔ MCP 已注册给 VS Code
```

## 三层能力（规范出处）

| 层 | 规范 | 给谁用 |
|---|---|---|
| **LM Tool** | `contributes.languageModelTools` + `vscode.lm.registerTool` | Agent 模式自动调用（`#bridgeDispatch` / `#bridgeAgents`） |
| **MCP provider** | `contributes.mcpServerDefinitionProviders` + `vscode.lm.registerMcpServerDefinitionProvider` | 扩展自带 MCP，用户不用改配置就拿到 `neko.py` 的 8 个工具 |
| **命令** | `contributes.commands` / `keybindings` | 手动一键派发 |

出处：`code.visualstudio.com/api/extension-guides/tools`、`/api/extension-guides/mcp`。

## 安装

**已在本机验证**：VS Code **1.144.0**（commit `2c33b50b16`），扩展目录
`C:\Users\<user>\.vscode\extensions\local.agent-bridge-vscode-0.3.0`，
`code --list-extensions` 能列出 `local.agent-bridge-vscode`。

```bash
# 方式一：直接拷进扩展目录（本机用的就是这个），然后重启窗口
cp -r plugins/vscode "$USERPROFILE/.vscode/extensions/local.agent-bridge-vscode-0.3.0"
#   VS Code 里按 Ctrl+Shift+P → Developer: Reload Window

# 方式二：打开 plugins/vscode，按 F5 起扩展开发宿主（调试用）

# 方式三：打包分发（需 vsce，会联网装包，本机未执行）
npx @vscode/vsce package && code --install-extension agent-bridge-vscode-0.3.0.vsix
```

> 注意：`Code.exe --version` 在这台机器上返回的是 Node 版本（`v24.22.1`），
> 不代表 VS Code 版本。取真实版本要用 `bin/code.cmd --version`。

设置项：

| 键 | 默认 | 说明 |
|---|---|---|
| `agentBridge.baseUrl` | `http://127.0.0.1:8787` | 控制台地址 |
| `agentBridge.repoRoot` | 空 | 仓库根；留空自动向上找 |
| `agentBridge.defaultAgent` | 空 | 留空每次让你选 |
| `agentBridge.timeoutSeconds` | `300` | 单次超时 |
| `agentBridge.registerMcp` | `true` | 关掉只留手动派发 |

## 验证

```bash
node plugins/vscode/smoke.js
```

用桩 `vscode` 跑真 `activate()`，核对三层「清单声明 == 代码注册」，并检查 MCP 定义真的指向 `neko.py`。这条也进了 pytest（没有 node 才跳过）。

## 边界

- 只走 `127.0.0.1`。
- LM Tool 带确认框（会真执行、可能改文件）。
- VS Code < 1.95 没有 `vscode.lm.registerTool` 时自动降级为「命令 + MCP」，不会激活失败。
