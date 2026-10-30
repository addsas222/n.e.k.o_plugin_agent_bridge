# Agent Bridge for AstrBot

把本机编码 Agent 接进 AstrBot 的聊天里：一条命令派活，另一条看谁可用。

这份插件的写法**照 AstrBot 自己的规范**（`metadata.yaml` 四字段 + `class Main(star.Star)` +
`@filter.command` + `yield event.plain_result()`），对照的是仓库里
`astrbot/builtin_stars/*/main.py` 与 `astrbot/core/star/updater.py::validate_plugin_metadata`。

## 安装

```bash
# 1. 起控制台（插件自己不启动它）
python webui/server.py        # http://127.0.0.1:8787

# 2. 把本目录拷进 AstrBot 的插件目录
#    默认是 <AstrBot>/data/plugins/<插件名>/（数据根可用 ASTRBOT_ROOT 覆盖）
cp -r plugins/astrbot "<AstrBot>/data/plugins/agent_bridge"

# 3. 重启 AstrBot（或在管理面板里重载插件）
```

目录结构必须是这样（AstrBot 只认这两个文件在最外层）：

```
data/plugins/agent_bridge/
├── metadata.yaml    # name / desc / version / author —— 四个都必填
├── main.py          # class Main(star.Star)
└── README.md
```

## 命令

| 命令 | 作用 |
|---|---|
| `/bridge_status` | 列出 14 条路由：✔ 可派发 / ! 需登录 / … 慢待验证 / · 已禁用 / ✖ 没装 |
| `/bridge <agent> <任务>` | 派给指定 Agent，例：`/bridge gemini 把 README 的错别字列出来` |
| `/bridge <任务>` | 不给 agent 时自动挑第一个可用的 |

返回的正文超过 1500 字会被截断并注明——完整输出在控制台（WebUI）里看。

## 为什么走 HTTP 而不是 import

AstrBot 和 agent_bridge 可能是两个 Python 环境（AstrBot 有自己的 venv 与依赖树）。
最稳的边界是本地 HTTP：`127.0.0.1:8787/api/*`，和 VS Code 扩展、WebUI 用的是同一套接口。
副作用是**必须先起控制台**，否则 `/bridge` 会明确告诉你连不上，而不是静默失败。

## 另一条路：MCP

AstrBot 本身就是 MCP 客户端（`astrbot/core/agent/mcp_client.py`），配置段用
`mcpServers`（与 Claude Desktop 同形状）。想让它直接拿到 8 个 `neko_*` 工具，
在 `data/cmd_config.json` 里加：

```json
{
  "mcpServers": {
    "agent_bridge": {
      "command": "python",
      "args": ["<agent_bridge>/skill/neko-bridge/bin/neko.py", "mcp"],
      "env": { "NEKO_BRIDGE_PROVIDERS": "<agent_bridge>/providers" }
    }
  }
}
```

两条路可并存：MCP 让模型自己调工具，插件让你手动一条命令派发。

## 边界

- 只连 `127.0.0.1`，不对外。
- `/bridge` 是**真执行**（会改文件、花额度），建议只给管理员用；
  AstrBot 的权限过滤是 `@filter.permission_type(filter.PermissionType.ADMIN)`，本插件没加，
  因为多数部署里它就是私人助手——要收紧的话在 `bridge` 上补这一行即可。
- 单次最长等 600 秒（`DEFAULT_TIMEOUT`），超时返回的是控制台的超时结果。
