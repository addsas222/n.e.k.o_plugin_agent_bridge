# command-code 路由说明

| 项 | 值 |
|---|---|
| 路由 id | `command-code` |
| 别名 | `cmdc`、`commandcode` |
| 可执行名 | `command-code`（bun 全局包名 `command-code`，横幅自称 `cmd`） |
| 调用形态 | `command-code -p "<task>" [--model M] [--output-format json]` |
| task 传递 | `mode = "switch"`（`-p` 取值可选，正文走位置参数） |
| 本机版本 | 1.88.0 |
| 就绪状态 | 已安装，**未登录** |

## 安装 / 登录

```bash
bun add -g command-code
cmdc login                 # 或 command-code login
```

## 配置覆盖

```toml
[bridge.agents.command-code]
enabled = true
model = "claude-sonnet-4-5"
timeout_seconds = 900
extra_args = ["--max-turns", "200", "--trust"]
```

## 已知限制

- 未登录 → 退出码 **3** + `Authentication failed`，靠 `auth_patterns` 认成 needs_login。
- `--max-turns` 命中上限退出码 **8**（不是 1），日志里会被标成 FAILED，属预期。
- `--output-format json` 输出 NDJSON 事件流 + 末行 result，看正文时别开。
- `--cloud` 会把会话跑在云端（beta），桥接不使用。
