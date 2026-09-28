### 2026-09-24 - Khởi tạo dự án Forge Hub

- Thêm project memory và tổng hợp kiến trúc từ app video tham chiếu và Forge upstream.
- Chưa kết nối hoặc thay đổi hai máy Ubuntu và VPS.

### 2026-09-24 - Chốt routing theo tài khoản

- Admin gán mỗi tài khoản vào một Forge; nhiều phiên đăng nhập của cùng tài khoản dùng chung queue/history.
- Kiểm tra LAN cho thấy hai máy có IP `192.168.110.26/.27`, Forge đang active, SSH port 22 chưa truy cập được từ máy Windows.

### 2026-09-24 - Gateway và private transport

- Thêm login/admin, SQLite sessions, proxy HTTP/WebSocket theo worker cố định và cấu hình deploy VPS.
- Kết nối SSH key đến cả hai Ubuntu và bật reverse tunnels riêng về VPS tại `127.0.0.1:18386/18387`; cả hai trả HTTP 200 từ VPS.
- Deploy Hub nội bộ tại `172.17.0.1:8036`; cài history callback extension lên hai Forge.
- Xác nhận login, đổi mật khẩu tạm, admin tạo/gán tài khoản và cấu hình worker online; chưa đưa DNS/Caddy ra công khai.

### 2026-09-24 - Hoàn tất luồng queue/history nội bộ

- Cài history extension lên cả hai Forge; Hub ghi nhận Generate qua native Gradio queue và phát status/thumbnail/result theo account.
- Xác nhận live 4-step generation, Gradio SSE và tải thumbnail/result qua Hub trên Forge 1 và Forge 2.
- Dọn account/job/ảnh thử; admin bootstrap được khôi phục ở trạng thái bắt buộc đổi mật khẩu.
- DNS/Caddy public route was pending at that time.

### 2026-09-24 - Public HTTPS trial route

- Owner added DNS A record; Caddy now routes HTTPS to Hub and rejects public `/hub/internal/*`.
- Added a temporary Caddy Basic Auth gate during the initial public trial rollout.
- Verified public TLS, Basic Auth challenge, Hub login/admin and authenticated Forge root.
- Verified public-domain Generate, Gradio SSE, JPEG thumbnail and PNG result; existing `video.lushmedia.net` remained healthy after Caddy restart.

### 2026-09-24 - Keep Generate available while queued

- Restyle the native Forge action area so Generate remains full size and Interrupt/Skip appear below it.
- Track in-tab Gradio submits so stop controls stay visible until the submitted jobs finish.

### 2026-09-25 - Keep queued state visible across consecutive jobs

- Sync native Interrupt/Skip visibility with authoritative Hub `queued`/`running` history state and show per-job progress/live preview while the drawer is open.
- Replace the text count on the history launcher with a green active-job badge; completed and failed jobs are excluded.

### 2026-09-25 - Make Forge queue independent of browser sessions

- Keep a Hub-owned Gradio SSE relay per session so tab reload/disconnect no longer clears pending Forge events; persist request/event metadata for account-wide history and exact queued cancellation.
- Add queue-row cancellation, local date range filtering, and the visible `Hàng đợi` launcher label.

### 2026-09-25 - Make queued cancellation truthful and immediate

- Verify Forge still considers the event queued before/after `/cancel`; if generation has started, keep the shared job visible as running instead of marking it cancelled.
- Delete a confirmed cancelled queue item from shared history in the cancellation request, so it does not depend on a second browser request.

### 2026-09-25 - Keep the queue preview tied to its active job

- Poll Forge's live preview with the last-seen image ID, follow the running/next queued job in the main Gallery, and show the account's active job count on Generate.
- Recover missing completed thumbnails from Forge's returned Gallery images and accept delayed thumbnail events after `done` without changing status.

### 2026-09-25 - Restore Forge's native latent preview

- Remove the Hub-injected Gallery overlay; Forge/Gradio remains responsible for the main latent preview across queued jobs.
- Poll Hub's per-job preview only while the history drawer is open; keep the Generate queue-count badge.

### 2026-09-25 - Serialize native preview loops across queued jobs

- Keep Forge's native `/internal/progress` preview path, but allow only one `requestProgress` loop per tab to own the shared Gallery at a time.
- Queue submissions remain immediate; the next native progress/latent preview loop starts from the previous job's completion callback.

### 2026-09-25 - Restore native preview after background-tab throttling

- On `visibilitychange`/`pageshow`, resume the Forge-native progress poll from the existing task marker when Chrome removed the progress DOM while the tab was backgrounded.
- Prefer the Hub's authoritative running task and preserve a separate client task marker across consecutive submissions.

### 2026-09-25 - Simplify internal trial login

- Remove the duplicate Caddy Basic Auth layer; Hub login is the only account prompt.
- Persist active Hub sessions for one year with sliding renewal and accept four-character trial passwords.
- Allow one-character passwords in the admin create/reset form for this internal trial.

### 2026-09-25 - Prevent stale Gallery output after SSE reconnect

- QueueRelay now filters replayed `process_completed` and `close_stream` messages after a browser subscriber has disconnected; live completion messages are unchanged.

### 2026-09-25 - Harden worker thumbnail callback mapping

- Use the last started task as a callback fallback when Forge clears `progress.current_task` early.
- Accept PIL-like and nested Gallery result values when building the fallback thumbnail. Worker rollout is pending.
- Rolled the callback update to both `ubuntu-ai1` and `root-ai2`; both Forge services returned active with empty queues.
# 2026-09-25

- Added authenticated LoRA upload from the Forge Lora toolbar. Files are streamed through the Hub to the account's assigned worker, written atomically into Forge's configured Lora directory, and rescanned for both Lora panes.
- Investigated the transient `Connection error` popup: worker logs show continuous Forge processing without a crash; the popup is consistent with a short browser/Hub Gradio SSE disconnect while the accepted backend job continues.
- Kept the Forge magnifying-glass icon inside the Search wrapper and placed the Upload LoRA action beside it with a dedicated upload icon.
- Restored the Upload LoRA outer border and added an 8px gap before the Search field.
- Added a worker fallback output path for Gallery results that missed Forge's save callback, and stopped native progress reconciliation from starting a second uncancellable Gallery loop.

### 2026-09-28 - Upload progress and Lush branding

- Added percent progress and a worker-confirmed cancel action to LoRA uploads, including cleanup of partial and newly saved files and retry when cancellation loses connectivity.
- Rebuilt login and admin layouts using the original Lush Video logo and photographic background; added password visibility, account search, clearer machine status, responsive layouts, and restrained motion.
- Copied the shared FLUX 8-step LoRA to both Forge workers and verified the matching SHA-256.
- Added account-manifest filtering, account-unique private upload names, and Hub prompt checks for LoRA access.
- Pushed sanitized source to `lushmediadev/Lush_Forge`; moved the VPS systemd working directory to `/opt/lush-forge-hub/repo` and added commit-based rollout with health-check rollback.
# 2026-09-28 - Repair worker events and queue recovery

- Fix the urllib/FastAPI `Request` name collision and persist worker lifecycle callbacks in a bounded local SQLite outbox with retry.
- Reconcile completed fallback images before queue replay, bound worker control calls, and recover stale cancellation requests without interrupting known running jobs.
- Avoid writing a duplicate fallback PNG when Forge already supplied a saved output path; only mark a job complete when its output path and thumbnail exist.
- Prune closed per-tab SSE channels after the reconnect grace window so session state does not accumulate over long runs.
- Backfill recent completed history entries missing a thumbnail/output path from worker result metadata without replaying generation.
- Make VPS rollout poll the health endpoint after restart before rollback; long-lived SSE connections can make systemd stop take up to its graceful-shutdown timeout.
- Keep worker outbox/result metadata private (`0700` directory, `0600` SQLite file); verify both callback tunnels deliver and drain an unknown-job sentinel without the previous `Request` exception.
- Confirmed the logged-in root route already proxies directly to Forge; deferred speculative UI changes pending a Chrome DevTools trace.
