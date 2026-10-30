---
name: neko-route-hermes
description: |
  通过 agent_bridge 把任务派给本机 Hermes（Nous Research）。当用户要用 Hermes 跑非交互任务，
  或它返回「Gateway inaccessible」却显示成功时使用。
metadata:
  author: "local"
  version: "0.1.0"
  route: "providers/hermes/provider.toml"
  agent_id: "hermes"
---

# 路由：hermes

## 调用

| 场景 | 调用 |
|---|---|
| N.E.K.O 对话里 | LLM 工具 `agent_run`，`agent="hermes"` |
| 命令行 | `python bin/neko.py dispatch hermes "任务"` |
| 别名 | `hermes-agent` |

```
hermes -z "<task>"                # mode=flag：-z 吃值
hermes -z "<task>" -m <model>
```

## 一处必须知道

**没起 gateway 时，退出码是 0。**

```
$ hermes -z "reply OK"
Error: Gateway inaccessible: service_manager_unavailable
$ echo $?
0
```

标准的「假成功」：退出码 0、stdout 空、stderr 一行 Error。桥接用 `fail_patterns`
把这两句认成 `AGENT_RUNTIME_ERROR`，所以结果会变成失败而不是成功。

注意它和「未登录」是**两类**：处置动作不同 —— 这个要去把 gateway 服务起起来，
不是去登录。所以清单里用的是 `fail_patterns` 而不是 `auth_patterns`。

## 验证

```bash
python bin/neko.py dispatch hermes "只回一个字：好" --json
```

- `error` 以 `AGENT_RUNTIME_ERROR: Gateway inaccessible` 开头 → 起 gateway；
- `ok=true` → 正常。
