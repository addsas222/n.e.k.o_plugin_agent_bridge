---
name: neko-route-gemini
description: |
  通过 agent_bridge 把任务派给本机 Gemini CLI（`gemini`）。当用户要用 Gemini 跑非交互任务
  （写代码、分析仓库、跑命令），或 gemini 路由出现「退出码 0 却没干活」时使用。
metadata:
  author: "local"
  version: "0.1.0"
  route: "providers/gemini/provider.toml"
  agent_id: "gemini"
---

# 路由：gemini

这是 **agent_bridge 的一条路由** 的 skill 面 —— 告诉你怎么把任务派给 Gemini CLI，
以及它跟别的 Agent 不一样的三处地方。

路由声明在 `providers/gemini/provider.toml`（唯一真源，改名/加参数改那里，不改代码）。

## 三种调用方式

| 场景 | 调用 |
|---|---|
| N.E.K.O 对话里 | LLM 工具 `agent_run`，`agent="gemini"` |
| 插件面板 / 入口 | `agent_run` 入口，`agent=gemini` |
| 命令行（skill / CI） | `python bin/neko.py dispatch gemini "任务"` |
| 别名 | `gcli`、`geminicli` |

## 实际拼出的命令行

```
gemini -p "<task>"                       # 默认
gemini -p "<task>" -m gemini-2.5-pro     # 指定 model
gemini -p "<task>" -o json               # 仅当 json_output=true
```

`-p/--prompt` **自己吃值**，所以登记为 `mode = "flag"`。

## 三处与别人不同

**一、未配置鉴权时的退出码是 41，不是 1。** 实测：

```
$ gemini -p "reply with OK only"
Please set an Auth method in your C:\Users\<user>\.gemini\settings.json or specify one of
the following environment variables before running: GEMINI_API_KEY, GOOGLE_GENAI_USE_VERTEXAI,
GOOGLE_GENAI_USE_GCA
$ echo $?
41
```

不认这句话的后果不是「假成功」，而是「说不清」：退出码非 0 只会被报成普通 FAILED，
既不进 `needs_login` 集合（后续不会被跳过），也不提示「这台没登录，换一个」。
所以清单自带 `auth_patterns = ["please set an auth method", ...]`，把它认成
`AGENT_NEEDS_LOGIN` 并带上 `login_hint`。

**二、`-o json` 会换掉 stdout 形状。** 开了 json 之后正文在 `response` 字段里，
`format_agent_result` 只是把 stdout 原样贴出来。要正文就别开 `json_args`。

**三、默认不自动批准动作。** `--yolo` / `--approval-mode yolo` 能自动接受所有工具调用，
桥接**不会**替你加。批处理场景确实需要时，在配置里显式写：

```toml
[bridge.agents.gemini]
extra_args = ["--approval-mode", "auto_edit"]
```

## 验证

```bash
python -c "import sys; sys.path.insert(0,'.'); from bridge.registry import AGENT_SPECS, canonical_id; print(AGENT_SPECS[canonical_id('gcli')])"
python bin/neko.py agents --json | grep -A3 '"agent": "gemini"'
```

先看 `ready`，再跑：

```bash
python bin/neko.py dispatch gemini "只回一个字：好"
```

`needs_login=true` 就是没配 `GEMINI_API_KEY`，换一个已登录的 Agent 重试。
