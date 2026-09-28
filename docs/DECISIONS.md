# Active Decisions

## HUB-001: Separate deployment

The new Forge app owns its own source, users, configuration, deployment and data. It may reuse architectural patterns from `lush-media-video` but must not share or migrate its live database implicitly.

## HUB-002: Private worker access

The VPS gateway reaches each Forge through private networking or a restricted tunnel. Public users reach Forge only through `ai.lushmedia.net` after authentication.

## HUB-003: Fixed account assignment

An admin creates an account and assigns `forge1` or `forge2`. Every login session for that account reaches the same worker until the admin changes the assignment.

## HUB-004: Account job workspace

Multiple employees may share one account. Queue and history items are associated with the account, so those employees see the same work. Accounts on the same worker should not see each other's history by default.

## HUB-005: Browser-independent native queue relay

The Hub persists a Generate payload and its Gradio event/session metadata, then owns the upstream `/queue/data` SSE stream. Browser connections subscribe downstream; when a tab reloads or closes, only that subscriber ends, while Forge continues processing the accepted event. The Hub cancels only after verifying the event is still queued and verifies it has left the queue before responding. If Forge has started it, the Hub keeps it visible as running. A confirmed queued cancellation removes its history row immediately. Forge's native queue still runs the actual generation on the assigned GPU.

## HUB-006: Keep native Forge preview and show visible count

Forge remains the owner of the main Gallery and latent preview. Hub must not cover or replace that surface; its per-job preview is limited to the history drawer and uses the last-seen `id_live_preview` cursor only while that drawer is open. The injected controls serialize native `requestProgress` per tab so queued submissions do not start competing Gallery loops, and resume the current native poll when a backgrounded tab becomes visible. Generate shows the account's running-plus-queued count. The worker history extension captures the thumbnail from the returned Gallery as a fallback when the image-save callback did not provide one.

## HUB-007: Single persistent login for internal trial

The internal trial removes the duplicate Caddy Basic Auth prompt. Hub account cookies persist for one year and refresh while active; admin lock or worker reassignment still revokes sessions. Account passwords may be one character during the trial, and normal user creation does not force a password-change step.

## HUB-008: Do not replay stale terminal Gallery output

QueueRelay may replay live progress state to a reconnecting browser, but after a subscriber has existed it must not replay old `process_completed` or `close_stream` messages. Otherwise Gradio can apply an older completion to the current Gallery while Hub history remains correct.

## HUB-009: Shared and account-private LoRAs

Root-level LoRAs on each worker are shared by all accounts assigned to that worker. User uploads get the internal prefix `__lush_owner_<account_id>__`, a unique Forge tag and an account-manifest entry. The Hub filters native cards and validates LoRA tags before queued and direct txt2img/img2img generation.

## HUB-010: Account-bound LoRA upload lifecycle

The Hub chooses the worker from the authenticated account and supplies a private worker key plus account ID. Uploads use atomic temporary files and an unpredictable upload ID. A cancel is confirmed by that worker and removes partial or just-committed data; the UI retries a missed cancel when the connection returns.

## HUB-011: Git-managed VPS source

The VPS service runs code from `/opt/lush-forge-hub/repo`, tracking the public `main` branch. The existing virtualenv, environment file, and SQLite data remain outside the checkout. `deploy/vps/rollout.sh` records the previous commit, fast-forwards from origin, restarts the service, checks health, and restores the previous commit if health fails.
