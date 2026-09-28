"""Generate private, per-worker callback keys for initial deployment."""

from pathlib import Path
import json
import secrets


ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / ".access"
PRIVATE.mkdir(parents=True, exist_ok=True)

keys = {}
for worker_id in ("forge1", "forge2"):
    target = PRIVATE / f"{worker_id}-worker.json"
    if target.exists():
        data = json.loads(target.read_text(encoding="utf-8"))
        keys[worker_id] = data["key"]
    else:
        keys[worker_id] = secrets.token_hex(32)
        target.write_text(json.dumps({"worker_id": worker_id, "key": keys[worker_id]}) + "\n", encoding="utf-8")

env_path = PRIVATE / "forgehub.env"
env_path.write_text(
    "FORGE_HUB_DB_PATH=/var/lib/lush-forge-hub/forgehub.sqlite3\n"
    "FORGE_HUB_PUBLIC_ORIGIN=https://ai.lushmedia.net\n"
    "FORGE_HUB_SECURE_COOKIE=1\n"
    "FORGE_HUB_FORGE1_URL=http://127.0.0.1:18386\n"
    "FORGE_HUB_FORGE2_URL=http://127.0.0.1:18387\n"
    f"FORGE_HUB_FORGE1_KEY={keys['forge1']}\n"
    f"FORGE_HUB_FORGE2_KEY={keys['forge2']}\n",
    encoding="utf-8",
)
print("Generated private worker configs and VPS env under .access/")
