---
sidebar_position: 2
title: "Chat-to-Kanban Triage"
description: "Route selected gateway chat messages into Hermes Kanban triage tasks instead of running them immediately"
---

# Chat-to-Kanban Triage

Chat-to-Kanban triage lets a messaging gateway turn selected chat messages into durable Kanban triage cards. Instead of starting an agent turn immediately, Hermes creates a task in the chosen board, replies with a short acknowledgement, and stops there. A triager or dispatcher can then refine the request, assign the right specialist profile, and run the work through the normal Kanban lifecycle.

Use it when chat is your intake channel for work that should be reviewed, prioritized, or routed before execution:

- project requests dropped into Telegram, Discord, Slack, or another gateway platform
- bug reports that need acceptance criteria before implementation
- ideas that should be queued without interrupting the current conversation
- team channels where only explicit task requests should become agent work

Do not use it for interactive back-and-forth, one-off questions, or messages you want Hermes to execute right away. Those can still bypass triage; see [Bypass triage](#bypass-triage).

## Enable it

Add a `gateway.kanban_triage` block to `~/.hermes/config.yaml` and restart or reload the gateway:

```yaml
gateway:
  kanban_triage:
    enabled: true
    default_mode: normal
    triage_assignee: paul
    fallback_board: inbox
    create_missing_boards: true
    board_create_policy: explicit_project_only
    acknowledge: true
    ack_template: "Queued for triage: board={board_slug} task={task_id}"

    boards:
      inbox:
        aliases: [inbox]
        default_category: general
      hermes-agent:
        aliases: [hermes, "hermes agent", agent]
        default_category: engineering

    categories:
      general:
        board: inbox
        assignee: paul
      engineering:
        board: hermes-agent
        assignee: paul
```

With `default_mode: normal`, only messages with explicit triage language such as "add to Kanban", "make a task", "queue", "track", or "put on the board" are captured. Everything else continues to normal chat.

To disable the feature, remove the block or set:

```yaml
gateway:
  kanban_triage:
    enabled: false
```

The default is disabled, so existing gateway installs keep their current behavior until you opt in.

## Modes

`default_mode` controls how aggressively the gateway creates cards:

| Mode | Behavior |
|---|---|
| `off` | Never create triage cards for this scope. |
| `normal` | Create cards only when the message includes an explicit triage keyword. This is the default when the feature is enabled. |
| `triage_first` | Treat task-like messages as intake by default when classification confidence is at or above `min_confidence`. Open-ended conversation still goes to normal chat. |
| `force_triage` | Queue almost all non-command, non-conversation messages, even short requests. Immediate-execution keywords still bypass. |
| `inherit` | Only valid in platform/user/chat overrides; keep the broader scope's mode. |

You can override the mode by platform, channel/chat, thread, or user:

```yaml
gateway:
  kanban_triage:
    enabled: true
    default_mode: normal
    platforms:
      telegram:
        default_mode: triage_first
        chats:
          "-1001234567890":
            mode: force_triage
        channels:
          "17585":
            mode: off
    users:
      "telegram:123456789":
        mode: normal
```

Resolution order is: global `default_mode`, then platform `default_mode`, then platform `chats` / `channels` override for the chat or thread id, then `users` override keyed as `<platform>:<user_id>`.

## Board resolution and creation

When Hermes decides to create a card, it chooses a board in this order:

1. Explicit project field in the message, such as `project: new launch site:` or `board: hermes-agent:`.
2. A configured board alias under `gateway.kanban_triage.boards`.
3. The category's default board under `gateway.kanban_triage.categories`.
4. `fallback_board` (default: `inbox`).

Board slugs are normalized to lowercase alphanumerics, hyphens, and underscores, up to 64 characters. Generic project names such as `task`, `todo`, `misc`, or `work` are ignored.

If the selected board does not exist, `create_missing_boards` and `board_create_policy` decide whether Hermes creates it:

| Policy | Behavior |
|---|---|
| `explicit_project_only` | Create a missing board only when the message named it explicitly with `project:` or `board:`. This is the default. |
| `confident_project` | Create for explicit project names, or for confident classification matches. |
| `always` | Create any missing selected board. |
| `never` | Never create missing boards; use `fallback_board` instead. |

If board creation is not allowed and the selected board is missing, Hermes queues the task on `fallback_board` and includes the unresolved board hint in the routing metadata.

## What gets stored

The created task is a normal Kanban task with:

- `status: triage`
- `created_by: gateway-chat-triage`
- `assignee`: `triage_assignee` or the matching category's `assignee`
- priority `10` for urgent language (`urgent`, `asap`, `p0`, `critical`, `immediately`), `-5` for backlog language (`someday`, `backlog`, `low priority`, `nice to have`), otherwise `0`
- an idempotency key based on routing version, board, platform, chat/thread, message id, and the text hash when the platform has no message id

The task body contains a redacted copy of the original message, source details, extracted routing, initial acceptance criteria, detected links, and attachment count.

Hermes also writes a JSON comment with `chat_routing` metadata:

- `routing_version`
- source platform, chat id, channel id, thread id, message id, user id/display, and received timestamp
- text SHA-256, redacted excerpt, attachment count, and detected URLs
- classification intent, category, confidence, project hint, and signals
- board decision, including whether a board was created or fallback was used
- task decision: task id, assignee, priority, and `triage` status
- acknowledgement text and whether it was sent

The raw message text is not stored in metadata; the excerpt is redacted and the full text is represented by a hash. The task body still includes a redacted original message so the triager has enough context.

## Acknowledgement

When `acknowledge: true`, the gateway replies with `ack_template` after creating or deduplicating the card. The template supports:

- `{board_slug}`
- `{task_id}`

Default:

```text
Queued for triage: board={board_slug} task={task_id}
```

If Hermes created a new board, it appends `(created new board)`. If the message named a board that could not be confidently matched or created, it appends `(could not confidently match <slug>)`.

If `acknowledge: false`, Hermes still creates the task but returns no user-facing acknowledgement.

## Bypass triage

Triage runs only for non-command gateway messages. These inputs bypass card creation and continue to the normal gateway path:

- slash commands and other configured `command_prefixes` (default: `/`), such as `/help`, `/new`, `/background`, `/kanban`, or an installed skill command
- quick commands, because the gateway resolves them before triage routing
- immediate-execution keywords: `now:`, `execute:`, `run:`, `do now:`
- open-ended conversation or questions such as "what do you think about this?" or "how do I configure the gateway?"
- messages shorter than three meaningful tokens, unless the active mode is `force_triage`
- messages below `min_confidence` in `triage_first` mode

For normal conversation, ask the message as usual. For immediate execution while triage is active, prefix it with an immediate keyword:

```text
now: check disk usage on the server
```

For explicit queuing while the mode is `normal`, use a triage keyword:

```text
add this to kanban for Hermes Agent: fix gateway retries
```

## End-to-end example

Config:

```yaml
gateway:
  kanban_triage:
    enabled: true
    default_mode: normal
    triage_assignee: paul
    fallback_board: inbox
    create_missing_boards: true
    board_create_policy: explicit_project_only
    boards:
      hermes-agent:
        aliases: [hermes, "hermes agent", agent]
        default_category: engineering
    categories:
      engineering:
        board: hermes-agent
        assignee: paul
```

Incoming Telegram DM:

```text
add this to kanban for Hermes Agent: fix gateway retries
```

What happens:

1. The gateway sees this is not a slash command or quick command.
2. `kanban_triage` classifies the message as a `task_request` because it contains the explicit triage phrase "add this to kanban" and a task verb.
3. The phrase "Hermes Agent" matches the `hermes-agent` board alias.
4. Hermes creates a `triage` task on the `hermes-agent` board, assigned to `paul`, with routing metadata in a JSON comment.
5. The gateway does not start an agent turn for this message.
6. The user sees:

```text
Queued for triage: board=hermes-agent task=t_ab12cd34
```

A duplicate delivery of the same platform message returns the same task id instead of creating a second card.

## Config reference

| Key | Default | Notes |
|---|---:|---|
| `enabled` | `false` | Global opt-in for gateway chat triage. |
| `default_mode` | `normal` | One of `off`, `normal`, `triage_first`, `force_triage`. |
| `triage_assignee` | `paul` | Default profile that owns triage cards unless a category overrides it. |
| `fallback_board` | `inbox` | Board used when no board matches or creation is not allowed. |
| `min_confidence` | `0.70` | Minimum classification confidence for `triage_first`. |
| `create_missing_boards` | `true` | Allows board creation subject to `board_create_policy`. |
| `board_create_policy` | `explicit_project_only` | `explicit_project_only`, `confident_project`, `always`, or `never`. |
| `acknowledge` | `true` | Send a chat acknowledgement after creating/deduplicating a card. |
| `ack_template` | `Queued for triage: board={board_slug} task={task_id}` | Supports `{board_slug}` and `{task_id}`. |
| `immediate_execution_keywords` | `now:`, `execute:`, `run:`, `do now:` | Prefixes that force normal immediate handling. |
| `triage_keywords` | `queue`, `track`, `make a task`, `add to kanban`, `put on the board` | Phrases that explicitly request a card in `normal` mode. |
| `command_prefixes` | `/` | Prefixes treated as commands and never triaged. |
| `boards` | `{}` | Board aliases and default categories. |
| `categories` | `{}` | Category default board and assignee. |
| `platforms` | `{}` | Platform/chat/channel scoped mode overrides. |
| `users` | `{}` | Per-user mode overrides keyed as `<platform>:<user_id>`. |

## Migration and backwards compatibility

This feature is opt-in. If `gateway.kanban_triage` is missing, invalid, or has `enabled: false`, the gateway behaves as it did before: ordinary messages start normal agent turns, slash commands run as commands, and no Kanban task is created.

The current config location is:

```yaml
gateway:
  kanban_triage: {}
```

The loader also accepts an already-materialized `kanban_triage` mapping in gateway config data for compatibility with existing gateway config plumbing, but user-facing `config.yaml` should use the nested `gateway.kanban_triage` form.

Existing Kanban boards are untouched. The default board remains `default`; chat triage only creates or writes boards when routing selects them. If you never configure `fallback_board`, the first routed card may create an `inbox` board because that is the triage fallback, not because the global Kanban default changed.

Changing the YAML on a running gateway requires the same reload/restart path as other gateway routing options. Existing cards and metadata keep their original routing comments; new messages use the updated config.
