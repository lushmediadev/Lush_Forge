# Worker events, shared-account stability, and Forge startup

## Scope

- Fix the confirmed worker-to-Hub event callback failure and deploy safely to both Forge workers.
- Review queue/history behavior when several tabs or users share one account and worker.
- Diagnose slow entry to the Forge UI; preserve the existing Hub authentication and private Forge transport.

## Confirmed evidence

- Both Forge workers are reachable by SSH, `forge.service` is active, and each binds `127.0.0.1:7860`.
- Machine 1 repeatedly logs `Event delivery failed: TypeError: patched_init() got an unexpected keyword argument 'data'` during job activity.
- `extension/lush-forge-history/scripts/lush_forge_history.py` imports `Request` from `urllib.request` then shadows it with `fastapi.Request`; `_post()` passes urllib-only `data=` to the shadowing class.
- The public Hub health/login routes respond quickly, but the authenticated Forge page has not been profiled in Chrome DevTools because that MCP is unavailable.
- Local source changes now alias the two `Request` classes, persist callback events/results in a bounded/retrying SQLite outbox, expose a worker-key-protected result-recovery endpoint, backfill recent completed rows missing thumbnails, bound Hub worker-control requests, reconcile stuck cancels, prune closed SSE channels, and prevent recovery from blindly resubmitting a completed/running task.
- The fixed worker extension is deployed to both Ubuntu workers, with timestamped backups; both Forge services were restarted sequentially only after confirming native queue size/pending tasks/GPU utilization were zero.
- Post-restart checks: both services and tunnels are active, result endpoint rejects an invalid worker key with HTTP 403, and an unknown-job callback sentinel reaches Hub and is rejected with HTTP 404 then drained from the outbox (no `patched_init` error). Both queues remain empty and GPUs idle.
- Hub source changes are pushed; SSH root key access is installed. Hub code rollout and the additive `cancel_requested_at` schema migration succeeded; no job rows were manually deleted or rewritten except automatic event/result recovery.
- The first Hub rollout attempt reached `f957178` but the old script checked health immediately after a graceful stop that timed out at 25 seconds; it rolled back to `ac310e7`. The rollout script was changed to poll health and deployed successfully at `dd170b3`; public `/healthz` is 200 and the root SSH key is installed.
- The startup backfill found six historical `done` rows with missing output metadata but no task-specific result in either worker cache/fallback. They were left unchanged rather than guessing which Forge output belongs to which account job.
- Both Forge Python 3.10.21 environments lacked `joblib`, which prevents the built-in soft-inpainting script from loading; pinning/installing the compatible package is the remaining worker runtime adjustment.
- Live read-only checks: both Forge services and both tunnel units are active; worker callback tunnels return Hub health in 94–122 ms; local Forge root/config TTFB is about 51/4 ms. The logged-in root route already proxies directly to Forge, and Caddy compression is configured.
- `joblib==1.5.3` is now pinned in `deploy/ubuntu/forge-extra-requirements.txt` and installed in both Forge virtualenvs; Forge 1 restarted and its fresh log no longer reports the `soft_inpainting.py` import error.
- Chrome DevTools MCP was added to the user's Codex config, but the current Codex process has not loaded it yet. FCP/LCP tracing requires restarting Codex and resuming this task.
- After restart, Chrome DevTools trace measured LCP 4.27 s (TTFB 363 ms, render delay 3.91 s), with the synchronous Infinite Image Browsing script/settings probe as the longest 5.97 s chain. A Hub HTML transform now adds `defer` only to that script.

## Guardrails

- Never expose or commit worker keys, account data, database files, browser cookies, or private SSH keys.
- Check queue/progress and GPU state before any worker restart; do not discard queued jobs or history.
- Keep Forge private behind the existing SSH tunnels and Hub authentication.
- Test shared-account job ownership, callback idempotency, recovery, and browser-disconnect behavior using existing code paths; avoid duplicate generation during recovery.

## Next steps

1. Review the worker callback, queue relay/store transitions, recovery loop, and login-to-Forge proxy path.
2. Review static diffs, account-sharing/polling semantics, and fallback-output disk use; check rollback paths.
3. Verify syntax/static behavior and perform only safe live smoke checks after confirming queues are idle.
4. Commit/push and roll out the worker extension; roll out Hub recovery changes with rollback evidence.
5. After Codex restarts, capture browser performance tracing; consider manual historical output mapping only if the user explicitly wants that risk.
