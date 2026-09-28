# Project Brief

## Purpose

- Tạo điểm truy cập `ai.lushmedia.net` cho người dùng đăng nhập và sử dụng giao diện Forge đầy đủ trên hai máy Ubuntu.
- Cần lịch sử job dễ nhận biết bằng thumbnail, trạng thái và máy xử lý, tương tự mục Job History của ComfyUI nhưng phù hợp Forge.

## Current evidence

- `lush-media-video` dùng FastAPI, SQLite, worker ComfyUI riêng cho mỗi GPU, health check và round-robin theo độ bận. Đây là project tham chiếu, không phải codebase để sửa.
- Forge `main` gọi `shared.demo.queue(64)` và dùng `FIFOLock` cho GPU calls. Native queue thuộc từng Forge instance.
- A1111 Agent Scheduler có queue/history ở một WebUI; tài liệu chính thức của extension chỉ công bố A1111 và Vladmandic, còn nhiều node là roadmap. Chưa có bằng chứng nó tương thích với Forge hiện dùng.
- Hai Ubuntu chạy cùng Forge commit `dfdcbab685e57677014f05a3309b48cc87383167`, Python 3.10.21, Gradio 4.40.0; mỗi instance bind `127.0.0.1:7860`.
- Extension riêng nối native Generate task/progress/save callbacks vào account history và first-output thumbnail.
- Gradio 4.40.0 uses the HTTP event stream at `/queue/data` for progress; native Generate remains in use.
- Hub owns the upstream `/queue/data` stream for each submitted Gradio session; browser disconnects only detach that browser and do not trigger Forge's session cleanup.
- Hub stores each Generate payload and Gradio event metadata with the account job so reloads, queue state and per-job cancellation can be reconciled.
- Forge's native Gallery and latent preview stay under Forge/Gradio control; Hub does not cover or replace them. When the history drawer is open, its small per-job preview uses the last-seen `id_live_preview` cursor.
- Completed-job thumbnail capture has two sources: Forge's image-save callback and a fallback from the returned Gallery image list. Hub accepts a delayed thumbnail callback without regressing a `done` job.
- Confirmed runtime sample: one completed `dog` job had neither `image_path` nor a thumbnail; its status was `done`. This is a worker callback coverage gap, not a failed history read.
- Latest runtime sample after F5: `task(luk8ikcoyc15p02)` (`dog`) is `done` with no output path/thumbnail, while the following `cat` job has both. The robust worker callback fallback has now been rolled out to both Ubuntu Forge instances.
- A short 4-step generation passed through the Hub on each worker; both returned `done`, a JPEG thumbnail and a PNG output.
- Native Forge progress is serialized per tab in the injected queue-controls script: queued events still enter Forge immediately, but only the currently running event owns the shared Gallery preview loop. This avoids multiple native `requestProgress` loops racing over one Gallery.
- When Chrome returns from a background tab, the injected controls use Forge's existing local task id to restore the native progress/latent-preview poll if its progress DOM was lost while the tab was throttled.
- The controls keep a separate current-task marker so Forge's legacy single `localStorage` key cannot be cleared by the first job while a later queued job is still active; visibility restore prefers the Hub's authoritative running job.
- QueueRelay does not replay old `process_completed` or `close_stream` messages after a browser subscriber has existed; this prevents a reconnect from overwriting the native Gallery with an older output.
- Root-level LoRAs on a worker are shared by every account assigned to that worker. The curated FLUX 8-step LoRA is installed at the same root path on both machines.
- User uploads are stored as `__lush_owner_<account_id>__<filename>` in the worker's LoRA directory. Hub serves only shared and matching account entries, and validates LoRA tags before queued or API generation.
- In-flight LoRA uploads use a random ID for percent progress and worker-confirmed cancellation; the worker removes partial or just-saved data when canceled.
- The public Git repository is `lushmediadev/Lush_Forge`; VPS systemd runs from `/opt/lush-forge-hub/repo`. Production database and environment stay under `/var/lib/lush-forge-hub` and `/etc/lush-forge-hub.env`; the existing virtualenv remains at `/opt/lush-forge-hub/app/.venv`.

## Intended system shape

- VPS Git checkout `/opt/lush-forge-hub/repo` chạy dưới user `forgehub` tại `172.17.0.1:8036`.
- Admin tạo/khóa tài khoản, đặt lại mật khẩu và gán cố định một worker. Thay assignment sẽ thu hồi sessions.
- Ubuntu GPU 1/2 giữ nguyên Forge local-only; reverse SSH tunnels riêng và history extension đã được cài.
- Hub thêm history drawer vào giao diện Forge; danh sách, thumbnail và output được giới hạn theo tài khoản.
- `ai.lushmedia.net` resolves to the VPS and Caddy serves verified HTTPS directly to the Hub; Hub's persistent account cookie is the only login layer for this internal trial.

## Confirmed product behavior

- Một admin tạo tài khoản nhân viên và gán mỗi tài khoản cố định vào Máy 1 hoặc Máy 2.
- Nhiều nhân viên được phép dùng chung một tài khoản; queue/history thuộc tài khoản đó.
- Closing or reloading a browser does not cancel an accepted queue event; another login to the same account sees the same DB status and progress.
- Người dùng là nhân viên nội bộ, sử dụng giao diện Forge đầy đủ.

## Runtime facts still needed

- Account passwords may be one character for the internal trial and active sessions persist for up to one year. Live credentials are stored only on the VPS.
- Public HTTPS login/admin/root paths are verified; a Generate through the public Caddy route passed.

## Safety invariants

- Không ảnh hưởng deployment, DB hoặc queue hiện tại của `lush-media-video`.
- Chỉ tính job là đã hoàn thành khi có output thực và liên kết đến đúng người gửi.
- Không nhận mọi request từ browser rồi phát tán tùy ý đến Forge worker; giới hạn worker IDs và origin URLs ở config.
- Only the Hub-owned queue relay may keep a worker's Gradio session open; browser disconnects must never propagate as upstream `/queue/data` disconnects.

## Verification

- Kiểm tra login, proxy HTTP/WebSocket, account gắn đúng worker, upload/generate, disconnect và quyền xem lịch sử.
- Verified HTTPS, Basic Auth, Hub login/admin, authenticated Forge root/history injection, and SSE + Generate + thumbnail/result through Caddy.
