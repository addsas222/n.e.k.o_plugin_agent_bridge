---
name: neko-route-pi
description: |
  通过 agent_bridge 把任务派给本机 pi（`@earendil-works/pi-coding-agent`）。当用户要用
  pi 跑非交互任务、或把 pi 和 omp（Oh My Pi）搞混时使用 —— 它们是两个产品，只是共用了
  可执行名。
metadata:
  author: "local"
  version: "0.1.0"
  route: "providers/pi/provider.toml"
  agent_id: "pi"
---

# 路由：pi

> ⚠️ **先读这一段：pi ≠ omp。**
>
> | 可执行名 | 真实包 | 本机版本 |
> |---|---|---|
> | `omp` | `@oh-my-pi/pi-coding-agent` | 18.12.6 |
> | `pi` | `@earendil-works/pi-coding-agent` | 1.4.0 |
>
> 桥接里曾经有 `ALIASES["pi"] = "omp"`，那等于「按 pi 却跑 omp」，真正的 pi 永远
> 调不到。这条别名已移除：`pi` 现在走本路由，omp 用 `omp` / `oh-my-pi` / `ompi`。

## 三种调用方式

| 场景 | 调用 |
|---|---|
| N.E.K.O 对话里 | LLM 工具 `agent_run`，`agent="pi"` |
| 命令行 | `python bin/neko.py dispatch pi "任务"` |
| 别名 | `pi-coding-agent`、`earendil-pi` |

## 实际拼出的命令行

```
pi -p "<task>"                    # mode=switch：-p 是布尔开关，正文是位置参数
pi -p "<task>" --model <pattern>
pi -p "<task>" --mode json        # 仅当 json_output=true
```

注意 `--mode`，**不是** `--output-format`：pi 的选项名和别家不一样，写错了不报错、
只是没生效。

## 与别人不同的地方

**一、`-p` 不吃值。** 帮助原文：`--print, -p  Non-interactive mode: process prompt
and exit`。任务是位置参数（`pi [options] [--] [@files...] [messages...]`），所以登记
成 `mode = "switch"`。写成 `flag` 在 pi 上恰好也能跑，但那是巧合，换个 CLI 就翻车。

**二、扩展 / 技能 / MCP 都要显式开。** 它默认会做发现扫描，批量派发时启动开销明显：

```toml
[bridge.agents.pi]
extra_args = ["--no-extensions", "--no-skills", "--no-mcp"]
```

反过来，需要扩展时用 `-e <path>` 或 `--skill <path>`。

**三、鉴权特征是本机推断值。** 本机沙箱把 `pi -p` 拦在程序黑名单（`reg.exe`）上，
没拿到真实的未登录文本。`auth_patterns = ["pi auth"]` 依据的是
`pi auth <command>  Print credentials or check provider readiness` 这个子命令。
**登录后请实测一次**，把原文措辞补进 `provider.toml`。

## 验证

```bash
python bin/neko.py dispatch pi "只回一个字：好"
```

若返回「无输出 + 非 0 退出码」，先看是不是被程序黑名单拦了 `reg.exe`
（`claude` 在同一台机器上有同样的现象）。
