# gemini 路由说明

| 项 | 值 |
|---|---|
| 路由 id | `gemini` |
| 别名 | `gcli`、`geminicli` |
| 可执行名 | `gemini`（`@google/gemini-cli`） |
| 调用形态 | `gemini -p "<task>" [-m <model>]` |
| task 传递 | `mode = "flag"`（`-p` 吃值） |
| 本机版本 | 0.65.0 |
| 就绪状态 | 已安装，**未配置鉴权** |

## 安装

```bash
bun add -g @google/gemini-cli     # 或 npm i -g @google/gemini-cli
```

## 鉴权（本机当前缺这一步）

两种都可：

1. 环境变量 `GEMINI_API_KEY`；
2. `~/.gemini/settings.json` 里选一种 Auth 方式（API Key / Vertex AI / GCA）。

未配置时的表现见 `SKILL.md` —— 退出码 **41**，stderr 一句「Please set an Auth method…」。
桥接靠清单里的 `auth_patterns` 把它认成 `needs_login`，而不是普通失败。

## 配置覆盖

宿主里改 `%LOCALAPPDATA%\N.E.K.O\plugins\agent_bridge\config\plugin.toml`：

```toml
[bridge.agents.gemini]
enabled = true                     # 不想让它出现在对话里就 false
model = "gemini-2.5-pro"           # 等价于每次派发都带 -m
timeout_seconds = 900
extra_args = ["--approval-mode", "auto_edit"]
exe_override = "C:\\full\\path\\to\\gemini.exe"
```

`exe_override` 的用途：本机 `gemini` 是 `~/.bun/bin/gemini.EXE`（真实 PE，直接可用）。
如果换到 `gemini.cmd` 形态且 shim 解析失败，桥接会返回 `AGENT_UNRESOLVED` 而**不会**
把任务正文交给 cmd.exe —— 那时用 `exe_override` 指到真实 exe。

## 已知限制

- `-o json` 改变 stdout 结构，默认不开。
- 不自动加 `--yolo`：需要在 `extra_args` 里显式开。
- `--skip-trust` 在某些工作区（首次打开的目录）能少一轮交互提示。
