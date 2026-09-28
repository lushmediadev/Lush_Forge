# UI System

## Surfaces

- `/hub/login` and `/hub/change-password`: photographic sign-in surfaces using the Lush Video background and original Lush Media monogram, with compact charcoal forms and reduced-motion support.
- `/hub/admin`: account management with worker status, account search, account assignment, lock/unlock, and password reset. Surface effects stay subtle so table state and actions remain clear.
- Internal trial auth uses one Hub login; successful browser sessions persist across revisits and do not require a second Caddy login prompt.
- Forge remains the native Gradio UI. `history.js` adds a text `Hàng đợi` launcher and white drawer that matches the Forge light canvas.
- The history launcher badge counts account jobs in `queued`, `running`, or the brief `cancelling` transition; terminal jobs do not affect the count.
- The drawer provides prompt/status filters plus local date bounds; queued rows expose a per-job red × cancel action.
- Active rows show Forge progress and a per-job live preview while the drawer is open; Forge's `id_live_preview` is sent as the last-seen cursor in a single poll.
- Leave the main Forge Gallery and latent-preview behavior native and untouched. Do not add a Hub overlay or replace the in-progress image area.
- The native Generate button has a small green count for account jobs that are running or queued; terminal history is excluded.
- The Lora toolbar has `Upload LoRA` beside Search. Root/shared LoRAs are available to every account on that worker; private upload cards are filtered by account manifest and display the original filename while using a unique internal tag. During upload the control widens, shows transfer percent, and exposes a separate × cancel action.
- `queue-controls.js` keeps Generate large and Interrupt/Skip compact underneath; it syncs visibility from both tab submissions and Hub queue state so a later queued job does not lose its controls when an earlier callback finishes. It serializes native `requestProgress` per tab, keeps its own current-task marker, and restores the authoritative running job when a backgrounded Chrome tab becomes visible again.

## Tokens and constraints

- Charcoal: `--page`, `--surface`, `--surface-2`.
- Text: `--text`, `--muted`; accent: Lush green `--accent`.
- History panel uses neutral white/gray surfaces for Forge's light UI.
- Keep Vietnamese UTF-8, compact controls, keyboard close with Escape, and mobile drawer width.

## Key files

- `forge_hub/static/style.css`
- `forge_hub/static/history.css`
- `forge_hub/static/queue-controls.js` and `forge_hub/static/queue-controls.css`
- `forge_hub/static/login.html`
- `forge_hub/static/admin.html`
