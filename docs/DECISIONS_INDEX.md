# Decisions Index

| ID | Decision | Status | Scope |
|---|---|---|---|
| HUB-001 | Tách app Forge mới khỏi app video/ComfyUI production. | Active | Project boundary |
| HUB-002 | Forge không được truy cập trực tiếp từ Internet; dùng private transport qua VPS gateway. | Active | Network |
| HUB-003 | Admin gán cố định mỗi tài khoản vào một Forge; mọi phiên của tài khoản đó dùng cùng worker. | Active | Routing |
| HUB-004 | Queue/history thuộc tài khoản, có thể dùng chung bởi nhiều nhân viên biết thông tin đăng nhập. | Active | Jobs |
| HUB-005 | Hub giữ upstream Gradio SSE độc lập với browser; chỉ xác nhận hủy khi Forge không còn job trong queue, rồi xóa ngay mục lịch sử. | Active | Queue lifecycle |
| HUB-006 | Giữ latent preview/Gallery native của Forge; Hub chỉ thêm preview theo job trong drawer và badge tổng job đang chạy/chờ trên Generate. Native progress loop được serialize theo tab. | Active | Queue UX |
| HUB-007 | Internal trial chỉ dùng một lớp Hub login; bỏ Basic Auth Caddy, cookie persistent một năm và mật khẩu có thể 1 ký tự. | Active | Auth UX |
| HUB-008 | Không replay terminal SSE cũ sau reconnect để tránh Gallery nhận lại output cũ. | Active | Queue relay |
| HUB-009 | Root-level LoRAs trên mỗi worker dùng chung; LoRA người dùng upload được gắn account ID và tag duy nhất. | Active | LoRA access |
| HUB-010 | Upload/cancel được Hub gắn với account/worker, worker xác nhận hủy; Hub kiểm tra quyền LoRA trước generation. | Active | LoRA lifecycle |

Thông tin vận hành còn thiếu được ghi ở `docs/PROJECT_BRIEF.md`.
