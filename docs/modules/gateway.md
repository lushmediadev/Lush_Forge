# Gateway

## Entry points

- `forge_hub/app.py`: FastAPI HTTP/WebSocket proxy và login/admin endpoints.
- `forge_hub/settings.py`: env và hai worker URLs cố định.
- `forge_hub/static/`: login/admin UI UTF-8.
- `forge_hub/store.py`: SQLite account, revocable session, persisted Generate payload, Gradio event metadata, job history and cancellation state.
- `forge_hub/queue_relay.py`: owns one upstream Gradio SSE connection per browser session; downstream disconnects do not close Forge's queue listener.
- Closed SSE channels remain available briefly for reconnect, then the Hub prunes them to keep per-tab session state bounded over long runs.

## Contract

- Hub endpoints ở `/hub/*`; mọi path khác đi vào Forge của worker đã gán. Forge giữ root path mặc định.
- Session token ngẫu nhiên chỉ nằm trong HttpOnly cookie và hash SHA-256 trong SQLite; cookie này bị loại trước khi proxy lên Forge.
- Admin đổi worker, khóa tài khoản hoặc đặt lại mật khẩu sẽ thu hồi mọi session của tài khoản đó.
- Worker URLs là cấu hình server, không nhận từ browser.
- `queue/join` Generate request được nhận diện theo Forge `/config` `fn_index`; payload được lưu trước khi chuyển request, rồi `event_id`/`session_hash` được lưu để hủy đúng job chờ.
- Cancel kiểm tra `/internal/progress` trước/sau lệnh `/cancel`; nếu Forge đã bắt đầu tạo thì giữ job là `running`, không báo hủy giả. Khi Forge xác nhận job đã rời queue, Hub xóa bản ghi lịch sử ngay trong cùng request.
- Worker control requests có read deadline 8 giây; stream `/queue/data` vẫn là kết nối dài hạn. Recovery kiểm tra active/queued/completed theo từng task, đối soát ảnh worker đã lưu trước replay và không tự gửi lại job đã chạy/kết thúc. Job `cancelling` bị bỏ dở được xử lý lại sau grace period.
- Khi Hub khởi động, một lượt backfill sửa các job `done` gần đây đang thiếu thumbnail/output path từ worker result cache; không chạy lại generation.
- History API trả job, ảnh thumbnail, và output đúng account; worker callbacks yêu cầu key riêng.
- Drawer live preview gửi `id_live_preview` mới nhất cùng `/internal/progress` request; chỉ poll khi drawer mở. Không phủ hay can thiệp Gallery/latent preview gốc của Forge.
- Forge Gradio 4.40.0 reports generation events as HTTP SSE on `/queue/data`; the Hub relays messages to browsers while maintaining its own upstream connection across browser reloads.
- Queue relay replay excludes terminal `process_completed`/`close_stream` messages after the first subscriber disconnects; live subscribers still receive terminal output normally.
- The injected queue-controls layer serializes Forge's native `requestProgress` calls per tab. This is UI-only: `/queue/join` remains immediate and Forge's backend FIFO queue is unchanged.
- On `visibilitychange`/`pageshow`, it resumes the native poll using the Hub's running task first, with a separate client marker as fallback; this avoids Forge's legacy single `${tab}_task_id` key being cleared by an earlier queued job.
- History date bounds are converted to UTC and queried within the authenticated account only.
- `GET /hub/api/lora/manifest` returns shared LoRAs and only the signed-in account's private LoRAs from its assigned worker.
- Upload/cancel endpoints bind requests to the session account and selected worker; the browser never supplies a worker ID. Hub filters `/sdapi/v1/loras` and rejects unavailable `<lora:...>` tags before queued and direct txt2img/img2img generation.

## Runtime

- Git checkout `/opt/lush-forge-hub/repo` chạy dưới user `forgehub`; systemd dùng virtualenv hiện hữu `/opt/lush-forge-hub/app/.venv` và kết nối `127.0.0.1:18386/18387` qua reverse tunnels.
- Caddy nhận HTTPS `ai.lushmedia.net` và proxy đến app qua host Docker gateway `172.17.0.1:8036`.
- Caddy does not add a second Basic Auth prompt for the internal trial; Hub's HttpOnly account cookie is persistent and refreshed on active use.
- Các tài khoản dùng chung giữ cùng worker và cùng history.
- Internal worker callback bị Caddy public route chặn; worker gọi nó qua SSH local forward tới `127.0.0.1:18036`.
- A short live Generate on both workers reached `done`; account APIs returned the associated thumbnail and image.
