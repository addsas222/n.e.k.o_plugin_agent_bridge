# hermes 路由说明

| 项 | 值 |
|---|---|
| 路由 id | `hermes` |
| 别名 | `hermes-agent` |
| 可执行名 | `hermes`（`~/AppData/Local/hermes/bin/hermes`，git 安装） |
| 调用形态 | `hermes -z "<task>" [-m model]` |
| task 传递 | `mode = "flag"`（`-z` 吃值） |
| 本机版本 | v0.22.1+946.g8dcf6d5（2026.9.24） |
| 就绪状态 | 已安装；gateway 未运行 |

## 为什么它需要单独一类失败特征

| 现象 | 退出码 | 归类 |
|---|---|---|
| 没登录 | 0 或非 0 + 登录措辞 | `auth_patterns` → `needs_login` |
| **gateway 没起** | **0** | `fail_patterns` → `AGENT_RUNTIME_ERROR` |

两种都表现为「退出码 0 + stderr 一行」。混成一类会让用户拿着登录提示去找服务，
或者反过来。所以清单里分成两个字段。

## 安装 / 启动

```bash
# 安装（git 方式，装在 ~/AppData/Local/hermes）
# 启动常驻 gateway 后再派发
```

## 配置覆盖

```toml
[bridge.agents.hermes]
enabled = true
model = "anthropic/claude-sonnet-4-5"
timeout_seconds = 600
extra_args = ["--skills", "neko-bridge"]
```

## 已知限制

- `bin` 目录不在 PATH 上时，靠 `registry._EXTRA_BIN_DIRS` 兜底找不到 —— 因为它的
  安装目录是 `~/AppData/Local/hermes/bin`，不在默认兜底列表里；本机实测 `hermes`
  在 PATH 上，所以走 `which`。
- 受限环境下起 gateway 需要 `schtasks.exe`（本机沙箱已拦）。
- `--yolo` 会跳过全部确认，桥接默认不加。
