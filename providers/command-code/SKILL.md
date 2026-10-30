---
name: neko-route-command-code
description: |
  通过 agent_bridge 把任务派给本机 Command Code（`command-code` / `cmdc`）。当用户要用
  Command Code 跑非交互任务、或它返回「Authentication failed」却被当成成功时使用。
metadata:
  author: "local"
  version: "0.1.0"
  route: "providers/command-code/provider.toml"
  agent_id: "command-code"
---

# 路由：command-code

## 三种调用方式

| 场景 | 调用 |
|---|---|
| N.E.K.O 对话里 | LLM 工具 `agent_run`，`agent="command-code"` |
| 命令行 | `python bin/neko.py dispatch command-code "任务"` |
| 别名 | `cmdc`、`commandcode` |

**为什么不登记 `cmd` 这个别名**：它的横幅自称 `cmd`，但在 Windows 上 `cmd` 是
cmd.exe。让人（或模型）用 `agent="cmd"` 去派任务是个不必要的歧义，所以只收
`cmdc` / `commandcode`。

## 实际拼出的命令行

```
command-code -p "<task>"                          # 默认
command-code -p "<task>" --model <model>          # 指定 model
command-code -p "<task>" --output-format json     # 仅当 json_output=true
```

它的帮助写的是 `-p, --print [query]` —— **取值可选**，正文按位置参数跟后面，
所以登记为 `mode = "switch"` 而不是 `flag`。拼出来的样子一样，但登记方式说明了
「`-p` 单独出现也合法」这件事，日后换 CLI 时不容易写错。

## 三处与别人不同

**一、未登录时退出码是 3。**

```
$ command-code -p "reply with OK only"
Error: Authentication failed. Please run "cmdc login" first.
$ echo $?
3
```

全局鉴权特征表里只有 `authentication required`，**没有** `authentication failed`，
所以清单自带 `auth_patterns`。不写这条，桥接只会把它报成普通 FAILED —— 不会进
`needs_login`、不会跳过、也不会告诉你「换一个已登录的 Agent」。

**二、`--max-turns` 默认 100，命中上限的退出码是 8。** 那不是「普通失败」，是
「被截断」。长任务要么拆小，要么在配置里放宽：

```toml
[bridge.agents.command-code]
extra_args = ["--max-turns", "200"]
```

**三、权限模式默认保守。** `--yolo` / `--permission-mode dont-ask` 会跳过确认，
桥接不替你加。headless 场景需要时显式开；`--trust` 只跳过项目信任提示，不等于 `--yolo`。

## 验证

```bash
python bin/neko.py dispatch command-code "只回一个字：好"
```

返回里 `needs_login=true` → 先 `cmdc login`，再重新加载插件（拉黑只能靠重载清掉）。
