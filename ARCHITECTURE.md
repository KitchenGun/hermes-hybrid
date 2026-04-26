# hermes-hybrid Architecture

This document captures non-obvious architectural decisions that aren't
self-evident from the code. For per-feature plans, see `C:\Users\kang9\.claude\plans\`.

## Watcher runtime (hermes-hybrid extension)

The official Hermes agent provides cron + manual triggers only. Watchers
— event-driven or polling-driven jobs that fire outside the user request
path — are a hermes-hybrid extension. They live alongside the official
schema as `category: watcher` YAMLs under `profiles/<id>/watchers/`,
which the official Hermes parser ignores (same status as our
`x-hermes-hybrid:` namespace).

The watcher runtime is a long-lived `asyncio` loop owned by the Discord
bot process. It does **not** route through `~/.hermes/cron/jobs.json`
or the Hermes CLI scheduler — those handle `category: cron` only.

### Idempotency contract

Every notification event is uniquely identified by:

```
(profile_id, watcher_name, account, message_id)
```

- `profile_id` and `watcher_name` come from the watcher YAML's location
  on disk.
- `account` is empty for non-mail watchers; for `mail_poll` it is the
  account name from `accounts.yaml`.
- `message_id` is whatever the upstream provider returns as a stable id
  — Gmail's `users.messages.id` (opaque string) or Naver IMAP's UID
  (numeric string). Both are stable for the lifetime of a message in
  INBOX.

The `watcher_state` SQLite table stores `last_dedup_key` per
`(profile_id, watcher_name, account)`. The runtime persists the newest
seen `message_id` after each tick, so:

- A bot restart does not re-alert messages already processed.
- A retry within the same tick does not double-send (the high-water
  mark advances before the webhook POST returns; on POST failure the
  webhook is retried with the same body, not the dedup state reset).
- Per-account isolation: if Gmail authentication breaks, the Naver
  account's last seen UID is unaffected.

This is the same shape as Temporal's idempotency-key pattern
(`(workflow_id, activity_id)`) — composite keys consistent across
retries, unique across executions.

### First-run seeding

The first time a watcher runs against a freshly registered account
(`last_dedup_key IS NULL`), it records the newest INBOX message as the
high-water mark **without notifying**. This avoids the "you have 47 old
emails" flood right after registration. Notifications start from the
next tick.

### Per-account actor model (concurrent polling)

Each watcher gets one `asyncio.Task`. Inside that task, all accounts
are polled concurrently via `asyncio.gather(..., return_exceptions=True)`.
Per-account exceptions are caught and logged independently — a hung
IMAP socket on one account does not delay alerts from the other
accounts.

This is an actor-per-mailbox pattern scaled down to asyncio:

```
WatcherRunner
└── asyncio.Task per watcher YAML
    ├── account 1 poll  ─┐
    ├── account 2 poll  ─┤── asyncio.gather → merge → webhook
    └── account 3 poll  ─┘
```

## Future upgrade paths

These are documented but not implemented today. Adopt them when the
load profile or scope changes.

1. **Gmail Push (watch + Pub/Sub)** — replaces 5-min polling with
   ~second-latency push. Requires GCP project, Pub/Sub topic, and a
   public HTTPS endpoint for push delivery. Personal-bot scope cannot
   reasonably host the endpoint without tunneling, so this is parked
   until the bot has a real address.

2. **Naver low-latency** — Naver does not expose any push mechanism
   (no API at all for personal `@naver.com` mailboxes). Faster
   notifications for Naver accounts means shortening the polling
   interval, with a quadratic increase in IMAP load.

3. **Apprise notifier abstraction** — mailrise uses
   [Apprise](https://github.com/caronc/apprise) to support 60+
   notification channels (Slack, Telegram, Matrix, push services, plain
   email, etc.). Today we POST to a single Discord webhook directly
   from `urllib`. Adopting Apprise becomes attractive once we want
   non-Discord delivery channels.

4. **LangGraph orchestrator (component D)** — the request-handling path
   (rule layer → skill surface → router → executor → validator → retry
   loop) is currently a hand-rolled state machine. Migrating it to a
   LangGraph `StateGraph` makes the workflow explicit, gives us
   built-in checkpointing for HITL interrupts, and keeps the per-node
   logic small. The mail watcher runtime is unaffected by this
   migration since it does not flow through `Orchestrator.handle()`.

## Why no `hermes-email` gateway

The official Hermes email gateway is for the inverse use case: email
addressed *to* the bot is the input channel (instead of Slack or
Discord). It uses a single dedicated mailbox, IMAP polls for unseen
mail, and SMTP replies in-thread. That doesn't fit "monitor my personal
inbox and alert me on new mail," so we ship our own watcher and mail
provider abstraction instead.
