# Lush Forge

Lush Forge là cổng đăng nhập và quản lý hàng đợi cho hai máy Forge nội bộ. Hub định tuyến cố định theo máy được gán trong tài khoản, lưu history trong SQLite và giữ stream Gradio hoạt động khi trình duyệt tải lại.

## LoRA

- LoRA đặt trực tiếp trong thư mục models/Lora của máy Forge là LoRA dùng chung cho mọi tài khoản được gán vào máy đó.
- LoRA upload từ giao diện được lưu với tên nội bộ gắn account ID. Hub chỉ hiển thị LoRA dùng chung và LoRA của tài khoản hiện tại; prompt có tag LoRA không thuộc danh sách này bị từ chối trước khi tạo ảnh.
- File LoRA được hỗ trợ: .safetensors, .ckpt, .pt; tối đa 2 GB. Upload có phần trăm, nút hủy và dọn file khi lệnh hủy được worker xác nhận.

## Chạy local

~~~bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
mkdir -p data
export FORGE_HUB_DB_PATH="$PWD/data/forgehub.sqlite3"
export FORGE_HUB_PUBLIC_ORIGIN="http://127.0.0.1:8036"
export FORGE_HUB_SECURE_COOKIE=0
FORGE_HUB_PRIVATE_BOOTSTRAP=1 python -m forge_hub.cli create-admin admin forge1
uvicorn forge_hub.app:app --host 127.0.0.1 --port 8036
~~~

CLI nhập mật khẩu tương tác và không in mật khẩu ra terminal. Tài khoản admin quản lý người dùng tại /hub/admin; người dùng thường được chuyển thẳng tới giao diện Forge.

## VPS

- Git checkout: /opt/lush-forge-hub/repo
- Runtime virtualenv hiện hữu: /opt/lush-forge-hub/app/.venv
- Cấu hình riêng: /etc/lush-forge-hub.env
- SQLite và history: /var/lib/lush-forge-hub
- systemd service: lush-forge-hub.service
- Forge workers: reverse ports 127.0.0.1:18386 và 127.0.0.1:18387

Rollout cập nhật commit checkout bằng deploy/vps/rollout.sh. Script lưu commit hiện tại để rollback, cập nhật main, restart Hub và kiểm tra /healthz; DB, environment và virtualenv nằm ngoài Git.

## Secrets và dữ liệu

Không commit .access/, .env, SQLite database, worker key, SSH key, mật khẩu hay output LoRA/ảnh. Production Forge chỉ bind loopback; public traffic phải đi qua Hub và Caddy.
