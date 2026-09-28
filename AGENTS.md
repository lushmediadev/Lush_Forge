# Lush Forge Hub working agreements

- Viết trao đổi, task/checklist và hướng dẫn triển khai bằng tiếng Việt. Giữ nguyên tên code, API field và command bằng tiếng Anh.
- Trước khi sửa project, đọc `docs/PROJECT_BRIEF.md` và `docs/MEMORY_INDEX.md`; chỉ mở module liên quan.
- Giữ app này độc lập với `lush-media-video` và dữ liệu video/ComfyUI đang chạy.
- Không xuất Forge trực tiếp ra Internet. Kết nối Forge qua private transport; truy cập công khai phải đi qua auth gateway.
- Không đặt secret, mật khẩu SSH, token Cloudflare, hoặc file dữ liệu production vào Git.
- Mọi thay đổi có ảnh hưởng đến Forge đang chạy phải có xác nhận từ runtime, rollback và smoke test.
- Sau mỗi Project Task, thêm một entry ngắn vào `docs/CHANGELOG.md`.
