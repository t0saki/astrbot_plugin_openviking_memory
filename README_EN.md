# astrbot_plugin_openviking_memory

[中文](README.md) | English

[OpenViking](https://github.com/volcengine/OpenViking) long-term memory integration for [AstrBot](https://github.com/AstrBotDevs/AstrBot).

Auto-captures conversations, performs semantic recall on every LLM request, and supports configurable memory isolation across groups and platforms.

## How it works

- **Auto-capture**: Every user message and bot reply is written to an OpenViking session. Tool call I/O is captured too (AstrBot >= 4.23.1).
- **Auto-recall**: Before each LLM request, the plugin searches OV for relevant memories and appends them to the system prompt.
- **Auto-commit**: Sessions are committed (archived + memory extracted) based on message count, token threshold, or idle timeout.
- **Backfill**: On first encounter with a group, historical messages are pulled from the platform and ingested into OV.

## Relationship with AstrBot's built-in Knowledge Base

They are complementary:

- **Built-in KB**: Manually uploaded documents (manuals, FAQs). Admin-managed via WebUI. No per-user isolation.
- **This plugin**: Automatic conversation capture + semantic recall + long-term profile extraction. Isolated per venue (group/DM).

## Installation

1. In AstrBot WebUI, go to Plugin Management and add this plugin's repo URL.
2. Fill in the plugin configuration (see below).
3. Reload the plugin.

## Configuration

All fields are configured via AstrBot WebUI after installation.

| Field | Default | Description |
|-------|---------|-------------|
| `ov_base_url` | `http://localhost:1933` | OpenViking server URL |
| `ov_admin_api_key` | *(required)* | Admin API key for creating venue users |
| `ov_account_id` | *(required)* | OV account ID |
| `isolation_mode` | `venue_user` | See isolation modes below |
| `isolation_overrides` | `{}` | Per-group overrides `{"group_id": "mode"}` |
| `auto_recall_enabled` | `true` | Auto-recall on every LLM request |
| `recall_limit` | `8` | Max recalled entries |
| `recall_min_score` | `0.35` | Minimum semantic score |
| `recall_token_budget` | `2000` | Max tokens for injected context |
| `commit_message_threshold` | `20` | Auto-commit after N messages |
| `commit_token_threshold` | `4096` | Auto-commit when tokens exceed this |
| `commit_idle_seconds` | `1800` | Auto-commit after N seconds idle |
| `backfill_on_first_seen` | `true` | Pull history on first group encounter |
| `backfill_max_messages` | `500` | Max messages to backfill |
| `ingest_attachments` | `false` | Push images/files to OV resources |
| `capture_tool_io` | `true` | Record tool inputs/outputs |

## Isolation modes

Memory isolation happens at the **OV user** level (not session level). Each venue gets its own OV user.

| Mode | OV user mapping | Behavior |
|------|-----------------|----------|
| `venue_user` (default) | Each group = 1 OV user; each DM = 1 OV user | Memory shared within group, isolated between groups |
| `venue_user_fanout` | Same as above | + Each user's messages are fanned out to all their other venues |
| `global_user` | Entire bot = 1 OV user | All memory shared |

### Fanout mode

When a user sends a message in group G, it's also written to all other groups/DMs where the user is currently a member. This lets the bot know what a user said in group A when replying in group B.

Trade-off: each message results in N writes (N = user's venue count). Small-scale deployments can handle this easily.

## Optional: Adding OV MCP tools

To give the LLM direct access to all OpenViking MCP tools (search, remember, read, list, etc.), add OV as an MCP server in AstrBot's WebUI:

- **Type**: HTTP
- **URL**: `{ov_base_url}/mcp` (e.g. `http://localhost:1933/mcp`)
- **Authorization**: Bearer `{ov_admin_api_key}`

This is optional — the plugin's auto-recall/capture works without it.

## Commands

| Command | Permission | Description |
|---------|-----------|-------------|
| `/ov_status` | Anyone | Show plugin connectivity, pending messages, backfill status |
| `/ov_backfill` | Admin | Force re-run backfill for the current venue |

## Requirements

- AstrBot >= 4.23.1 (for tool I/O capture hooks; core features work on >= 4.9.2)
- OpenViking server running and accessible
- Admin API key with permission to create users

## License

MIT
