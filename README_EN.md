# astrbot_plugin_openviking_memory

[中文](README.md) | English

[OpenViking](https://github.com/volcengine/OpenViking) long-term memory integration for [AstrBot](https://github.com/AstrBotDevs/AstrBot).

Auto-captures conversations and performs semantic recall on every LLM request. Built on OpenViking's **peer memory model**: the bot is the session "self" and every person is a "peer", so OV builds a separate profile for each person the bot talks to — the bot "remembers everyone" across groups and sessions.

## How it works

- **Auto-capture**: Every user message and bot reply is written to an OpenViking session. Tool call I/O is captured too (AstrBot >= 4.23.1).
- **Peer profiles**: Incoming group messages carry a `peer_id` (the sender); on commit OV builds a per-person profile under `viking://user/<bot>/peers/<sender_id>/`. The bot's own replies and tool I/O stay "self".
- **Auto-recall**: Before each LLM request, the plugin recalls self (bot/group context) + the current speaker + recently-active members and appends them to the system prompt.
- **Auto-commit**: Sessions are committed (archived + memory extracted) based on message count, token threshold, or idle timeout.
- **Backfill**: On first encounter with a group, historical messages are pulled from the platform and ingested into OV.

## Relationship with AstrBot's built-in Knowledge Base

They are complementary:

- **Built-in KB**: Manually uploaded documents (manuals, FAQs). Admin-managed via WebUI. No per-user isolation.
- **This plugin**: Automatic conversation capture + semantic recall + per-person profile extraction (self + peer).

## Installation

1. In AstrBot WebUI, search **OpenViking Memory** in the Plugin Marketplace and install; or install from URL: `https://github.com/t0saki/astrbot_plugin_openviking_memory.git`
2. Fill in the plugin configuration (see below).
3. Reload the plugin.

## Configuration

All fields are configured via AstrBot WebUI after installation.

| Field | Default | Description |
|-------|---------|-------------|
| `ov_base_url` | `http://localhost:1933` | OpenViking server URL (must be a peer-contract build) |
| `ov_user_api_key` | | User API key, used directly as the bot self identity in `global` mode (**recommended**) |
| `ov_admin_api_key` | | Admin API key, only used in `venue` mode to mint per-venue users |
| `ov_account_id` | | OV account ID (auto-parsed from API key if empty; only needed when minting in `venue` mode) |
| `self_scope` | `global` | Memory-owner (self) granularity, see below |
| `global_user_id` | `astrbot-global` | Name of the minted bot-self user in `global` mode when only an admin key is available |
| `isolation_overrides` | `{}` | Per-group `self_scope` overrides `{"group_id": "venue"}` |
| `peer_enabled` | `true` | Tag incoming messages with `peer_id` and commit with self+peer policy (off = self-only memory) |
| `peer_recall_scope` | `speaker_plus_active` | Which peers to recall: `speaker` / `speaker_plus_active` / `none` |
| `peer_recall_active_window` | `5` | Max recently-active members also recalled in `speaker_plus_active` |
| `trusted_mode` | `false` | Send `X-OpenViking-Account/User` headers (only for OV servers in `auth_mode=trusted` behind a gateway) |
| `auto_recall_enabled` | `true` | Auto-recall on every LLM request |
| `recall_limit` | `8` | Max recalled entries |
| `recall_min_score` | `0.35` | Minimum semantic score |
| `recall_token_budget` | `2000` | Max tokens for injected context |
| `commit_message_threshold` | `20` | Auto-commit after N messages |
| `commit_token_threshold` | `4096` | Auto-commit when tokens exceed this |
| `commit_idle_seconds` | `1800` | Auto-commit after N seconds idle (also the "recent" window for peer recall) |
| `backfill_on_first_seen` | `true` | Pull history on first group encounter |
| `backfill_max_messages` | `500` | Max messages to backfill |
| `ingest_attachments` | `false` | Push images/files to OV resources |
| `capture_tool_io` | `true` | Record tool inputs/outputs |

> Legacy `isolation_mode` values (`venue_user` / `venue_user_fanout` / `global_user`) are still recognized and auto-mapped onto `self_scope` (`venue` / `global` / `global`) with a deprecation log. `venue_user_fanout` is superseded by `global` + peer.

## Isolation modes (self_scope)

The model is **one bot "self" + one "peer" per person**. `self_scope` controls the self (memory-owner) granularity; peers are always keyed per person.

| `self_scope` | self mapping | peer scope | Behavior |
|------|--------------|------------|----------|
| `global` (default) | Entire bot = 1 self (OV user) | Shared across venues, one profile per person | The bot "knows everyone" across groups; uses `ov_user_api_key` directly, no admin/user minting |
| `venue` | Each group/DM = 1 self | Isolated per venue | Groups isolated from each other (privacy-first); mints per-venue users via the admin key |

### Cross-person recall

All peers live under the same bot-self space (`viking://user/<bot>/peers/*`). Recall by default pulls self + the current speaker + recently-active members, so when A asks something the bot can also recall B's and C's profiles (e.g. "what does Bob like?").

> OpenViking does not allow searching *all* peers at once — each recalled person must be named explicitly, which `peer_recall_scope` controls. This is cleaner than the old fanout: each person's profile is stored once, with no lossy copying.

## Recommended: Adding OV MCP tools

We strongly recommend adding the OpenViking MCP server in AstrBot WebUI → Plugins → MCP, so the LLM can proactively use tools like search, remember, read, and list:

```json
{
  "transport": "streamable_http",
  "url": "http://localhost:1933/mcp",
  "headers": {
    "Authorization": "Bearer <your_root_api_key>"
  },
  "timeout": 5,
  "sse_read_timeout": 300
}
```

Replace `url` and `Authorization` with your actual OV server address and API key.

> Due to AstrBot plugin architecture limitations, the plugin cannot register MCP servers automatically (manual setup required) and cannot dynamically switch auth headers based on the current venue. A fixed key must be configured: Root key is recommended so the LLM can search across all venue users' memories; Admin keys can only see the admin's own content. The trade-off is that the LLM may retrieve content from unrelated venues — guide it via system prompt to judge relevance. Without MCP, the plugin's auto-recall/capture still works, but the LLM won't be able to proactively search or write memories.

## Commands

| Command | Permission | Description |
|---------|-----------|-------------|
| `/ov_status` | Anyone | Show plugin connectivity, pending messages, backfill status |
| `/ov_backfill` | Admin | Force re-run backfill for the current venue |

## Requirements

- AstrBot >= 4.23.1 (for tool I/O capture hooks; core features work on >= 4.9.2)
- **OpenViking server on the peer contract (PR #2236 or later)**, running in standard `api_key` mode (identity is derived from the Bearer key; the plugin no longer sends `X-OpenViking-*` identity headers unless `trusted_mode` is set)
- A User API key (`global` mode, recommended) or Admin API key (`venue` mode, mints per-venue users)

## License

MIT
