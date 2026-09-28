# Forge Worker Bridge

## Runtime contract

| Worker | Ubuntu host | Forge bind | VPS reverse port | Callback forward |
|---|---|---|---|---|
| `forge1` | `ubuntu-ai1` (`192.168.110.26`) | `127.0.0.1:7860` | `127.0.0.1:18386` | local `127.0.0.1:18036` -> VPS `172.17.0.1:8036` |
| `forge2` | `root-ai2` (`192.168.110.27`) | `127.0.0.1:7860` | `127.0.0.1:18387` | local `127.0.0.1:18036` -> VPS `172.17.0.1:8036` |

Both workers use distinct SSH keys; each key is limited to one reverse listener plus the Hub callback port. Worker key lives in `~/.config/lush-forge-hub/worker.json` and must stay out of Git.

## Extension behavior

- `extension/lush-forge-history/scripts/lush_forge_history.py` hooks native Forge progress and image-save callbacks.
- Hub finds `txt2img`/`img2img` indexes and prompt fields dynamically from Forge `/config`.
- Gradio 4.40.0 uses HTTP SSE at `/queue/data` for UI progress; it is not a WebSocket endpoint.
- Hub `QueueRelay` owns the upstream SSE session; do not proxy the worker stream lifecycle directly to the browser or Forge removes queued events on disconnect.
- Per-job cancellation uses the saved Gradio `session_hash`, `fn_index`, and `event_id`; account ownership is checked before contacting the assigned worker.
- Events track running/done/failed; one 180px JPEG thumbnail and first output image path are stored per task. The image-save callback is primary; `record_results` falls back to the first returned Gallery image so a successful generation still has a thumbnail when no saved path was emitted.
- Thumbnail events may arrive after the `done` event; Store fills the thumbnail without reverting the terminal status.
- Root-level LoRAs on each worker are shared by all accounts assigned to that worker. Account uploads use `__lush_owner_<account_id>__` names so same-named uploads cannot collide.
- Upload/manifest routes require both worker key and account ID from Hub. They write atomic temporary files under Forge's configured `lora_dir`, reject duplicates and files over 2 GB, and refresh the native index. Cancellation checks the owning account and removes partial or just-saved files.
- Private network aliases resolve to their account-unique filename so Forge does not map two users' same-named uploads to the same alias.
- Restart Forge after extension changes. Check queue empty and GPU idle before restart.

## Operations

- `systemctl --user status forge.service`
- `systemctl --user status forge-hub-tunnel.service`
- `curl -I http://127.0.0.1:7860/`
- `curl http://127.0.0.1:18036/healthz`
- On VPS: `curl -I http://127.0.0.1:18386/` and `:18387/`.
