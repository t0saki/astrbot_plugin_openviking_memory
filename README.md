# astrbot_plugin_openviking_memory

中文 | [English](README_EN.md)

为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 提供 [OpenViking](https://github.com/volcengine/OpenViking) 长期记忆能力。

自动捕获群聊/私聊对话，在每次 LLM 请求前语义召回相关记忆，支持可配置的群/用户记忆隔离。

## 安装

1. 在 AstrBot WebUI → 插件管理 中添加本仓库地址
2. 填写插件配置（见下方）
3. 重载插件

### 前置条件

- AstrBot >= 4.23.1
- OpenViking 服务端已运行并可访问（部署方式参考 [OpenViking 文档](https://docs.openviking.ai)）
- 拥有 Admin API key（用于自动创建 venue user）或 User API key（`global_user` 模式）

## 功能概览

- **自动捕获**：每条用户消息和 bot 回复自动写入 OV session，工具调用的输入/输出也会被捕获
- **自动召回**：每次 LLM 请求前，插件搜索 OV 中的相关记忆并追加到系统提示
- **自动提交**：根据消息数、token 估算值或空闲超时自动 commit session，触发长期记忆提取
- **历史消化**：首次接入群聊时，自动拉取平台历史消息并入库（默认开启，可关闭）

### 与 AstrBot 内置知识库的关系

互补，而非替代：

- **内置知识库**：手动上传的稳态文档（手册、FAQ），仅 WebUI 管理，无 per-user 隔离
- **本插件**：对话流自动入库 + 语义召回 + 长期画像/偏好抽取，按 venue（群/私聊）隔离

## 配置

安装后在 AstrBot WebUI 中配置。

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `ov_base_url` | `http://localhost:1933` | OpenViking 服务端地址 |
| `ov_admin_api_key` | | Admin API key，用于自动创建 venue user（`global_user` 模式下可用 `ov_user_api_key` 代替） |
| `ov_user_api_key` | | 普通 User API key，`global_user` 模式下直接使用，无需 Admin key |
| `ov_account_id` | | OV account ID（留空从 API key 自动解析） |
| `isolation_mode` | `venue_user` | 隔离模式，见下方说明 |
| `isolation_overrides` | `{}` | 按群号覆盖隔离模式，如 `{"123456": "venue_user_fanout"}` |
| `auto_recall_enabled` | `true` | 是否自动召回 |
| `recall_limit` | `8` | 最多召回条数 |
| `recall_min_score` | `0.35` | 语义匹配最低分 |
| `recall_token_budget` | `2000` | 注入上下文的 token 预算 |
| `commit_message_threshold` | `20` | 累积 N 条消息后自动 commit |
| `commit_token_threshold` | `4096` | 累积 token 超过此值后自动 commit |
| `commit_idle_seconds` | `1800` | 空闲 N 秒后自动 commit |
| `backfill_on_first_seen` | `true` | 首次接入群聊时拉取历史 |
| `backfill_max_messages` | `500` | 每群最多拉取历史条数 |
| `ingest_attachments` | `false` | 是否将图片/文件推送至 OV resources（需 VLM） |
| `capture_tool_io` | `true` | 是否捕获工具调用输入/输出 |

## 隔离模式

记忆隔离粒度在 **OV user** 层面（不是 session）。每个 venue（群或私聊）对应一个 OV user。

| 模式 | OV user 映射 | 行为 |
|------|-------------|------|
| `venue_user`（默认） | 每群 = 1 OV user；每个私聊 = 1 OV user | 群内共享记忆，群间隔离 |
| `venue_user_fanout` | 同上 | + 用户的消息扇出到该用户所在的所有其他群/私聊 |
| `global_user` | 整个 bot 实例 = 1 OV user | 所有记忆共享 |

### Fanout 模式

用户 A 在群 G 发消息后，该消息也会写入 A 当前在的所有其他群和私聊。这样 bot 在群 H 回复 A 时也能知道 A 在群 G 说过什么。

代价：每条消息产生 N 次写入（N = 用户所在 venue 数）。小规模部署完全可以承受。

## 推荐：添加 OV MCP 工具

强烈建议在 AstrBot WebUI → 插件 → MCP 页面添加 OpenViking MCP 服务，让 LLM 能主动调用 search、remember、read、list 等工具：

```json
{
  "transport": "streamable_http",
  "url": "http://localhost:1933/mcp",
  "headers": {
    "Authorization": "Bearer <你的 ov_admin_api_key>"
  },
  "timeout": 5,
  "sse_read_timeout": 300
}
```

将 `url` 和 `Authorization` 替换为实际的 OV 服务端地址和 API key（与插件配置中填写的一致）。

> 由于 AstrBot 插件架构限制，插件无法自动注册 MCP 服务，需手动添加。不添加不影响插件的自动召回/捕获功能，但 LLM 将无法主动发起记忆搜索或写入。

## 命令

| 命令 | 权限 | 说明 |
|------|------|------|
| `/ov_status` | 所有人 | 查看插件连通性、pending 消息数、backfill 状态 |
| `/ov_backfill` | 管理员 | 强制重新执行当前群的历史消化 |

## License

MIT
