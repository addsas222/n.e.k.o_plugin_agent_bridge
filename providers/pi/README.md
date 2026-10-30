# pi 路由说明

| 项 | 值 |
|---|---|
| 路由 id | `pi` |
| 别名 | `pi-coding-agent`、`earendil-pi` |
| 可执行名 | `pi`（`@earendil-works/pi-coding-agent`） |
| 调用形态 | `pi -p "<task>" [--model M] [--mode json]` |
| task 传递 | `mode = "switch"`（`-p` 布尔，正文位置参数） |
| 本机版本 | 1.4.0 |
| 就绪状态 | 已安装；本机沙箱下 `-p` 被 reg.exe 黑名单拦住，未验证到输出 |

## 与 omp 的关系（最容易搞错的一点）

两个包都提供名为 `pi` 的入口：

```
~/.bun/bin/pi.bunx   → @earendil-works/pi-coding-agent/dist/bundle/cli.js   （v1.4.0）
~/.bun/bin/omp.bunx  → @oh-my-pi/pi-coding-agent/dist/cli.js                （v18.12.6）
```

本仓库历史上把 `pi` 当 omp 的别名（`ALIASES["pi"] = "omp"`），导致：

- 想调 omp 的人写 `pi`，能跑，但跑的是 omp —— 看起来"正常"，所以一直没被发现；
- 真正的 pi 没有任何调用路径。

现已修正：`pi` 是独立路由，omp 保留 `omp` / `oh-my-pi` / `ompi`。
**这是一处有意的行为变更**，如果某个脚本依赖旧语义，把其中的 `pi` 改成 `omp`。

## 配置覆盖

```toml
[bridge.agents.pi]
enabled = true
model = "anthropic/claude-sonnet-4-5"
timeout_seconds = 600
extra_args = ["--no-extensions", "--no-skills"]
```

## 已知限制

- 本机沙箱：`pi -p` 无输出、非 0 退出（reg.exe 程序黑名单，与 `claude` 同因）。
- `auth_patterns` 尚未用真实未登录文本校准（见 `provider.toml` 里的注释）。
- `--mode json` / `--mode rpc` 会改变输出协议，默认不开。
