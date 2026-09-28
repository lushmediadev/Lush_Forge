"""Report native Forge job lifecycle and first output thumbnail to Lush Forge Hub."""

from __future__ import annotations

import base64
from collections import OrderedDict
import hashlib
import io
import json
import hmac
import os
from queue import Empty, Full, Queue
import sqlite3
from pathlib import Path
import threading
import time
from uuid import UUID, uuid4
from urllib.error import HTTPError
from urllib.request import Request as UrlRequest, urlopen

from fastapi import File, Header, HTTPException, Request as FastAPIRequest, UploadFile
from PIL import Image as PILImage
from modules import progress, script_callbacks


CONFIG_PATH = Path.home() / ".config" / "lush-forge-hub" / "worker.json"
OUTPUT_ROOT = (Path.home() / "forge" / "outputs").resolve()
FALLBACK_OUTPUT_ROOT = (OUTPUT_ROOT / "lush-forge-history").resolve()
EVENTS_DB = Path.home() / ".local" / "state" / "lush-forge-hub" / "worker-events.sqlite3"
EVENT_OUTBOX_MAX_EVENTS = 4096
EVENT_OUTBOX_MAX_BYTES = 256 * 1024 * 1024
RESULT_CACHE_MAX_EVENTS = 4096
RESULT_CACHE_MAX_BYTES = 64 * 1024 * 1024
EVENTS: Queue[dict] = Queue(maxsize=256)
EVENTS_WAKE = threading.Event()
EVENTS_LOCK = threading.Lock()
THUMBNAILS: OrderedDict[str, bool] = OrderedDict()
OUTPUT_PATHS: OrderedDict[str, str] = OrderedDict()
COMPLETED: OrderedDict[str, bool] = OrderedDict()
LAST_TASK_ID: str | None = None
LORA_SUFFIXES = {".safetensors", ".ckpt", ".pt"}
LORA_UPLOAD_MAX_BYTES = 2 * 1024 * 1024 * 1024
LORA_UPLOADS: dict[str, dict] = {}
LORA_UPLOAD_LOCK = threading.Lock()
PRIVATE_LORA_PREFIX = "__lush_owner_"


def _config() -> dict | None:
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if config.get("worker_id") not in ("forge1", "forge2") or not config.get("key"):
            return None
        return config
    except (OSError, ValueError):
        return None


CONFIG = _config()


def _post(event: dict) -> None:
    if not CONFIG:
        return
    payload = {"worker_id": CONFIG["worker_id"], **event}
    request = UrlRequest(
        "http://127.0.0.1:18036/hub/internal/worker-events",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Forge-Worker-Key": CONFIG["key"]},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        response.read()


def _outbox_connect() -> sqlite3.Connection:
    EVENTS_DB.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(EVENTS_DB, timeout=5)
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS completed_results ("
        "task_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    return connection


def _outbox_add(event: dict) -> bool:
    payload = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
    payload_bytes = len(payload.encode("utf-8"))
    with EVENTS_LOCK:
        connection = _outbox_connect()
        try:
            count, total_bytes = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(payload)), 0) FROM events"
            ).fetchone()
            if count >= EVENT_OUTBOX_MAX_EVENTS or total_bytes + payload_bytes > EVENT_OUTBOX_MAX_BYTES:
                return False
            connection.execute(
                "INSERT INTO events (payload, created_at) VALUES (?, ?)", (payload, time.time())
            )
            connection.commit()
            try:
                EVENTS_DB.chmod(0o600)
            except OSError:
                pass
            return True
        finally:
            connection.close()


def _outbox_next() -> tuple[int, dict] | None:
    with EVENTS_LOCK:
        connection = _outbox_connect()
        try:
            row = connection.execute("SELECT id, payload FROM events ORDER BY id LIMIT 1").fetchone()
            return (int(row[0]), json.loads(row[1])) if row else None
        finally:
            connection.close()


def _outbox_delete(event_id: int) -> None:
    with EVENTS_LOCK:
        connection = _outbox_connect()
        try:
            connection.execute("DELETE FROM events WHERE id = ?", (event_id,))
            connection.commit()
        finally:
            connection.close()


def _remember_completed_result(task_id: str, payload: dict) -> None:
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    with EVENTS_LOCK:
        connection = _outbox_connect()
        try:
            connection.execute("DELETE FROM completed_results WHERE task_id = ?", (task_id,))
            connection.execute(
                "INSERT INTO completed_results (task_id, payload, created_at) VALUES (?, ?, ?)",
                (task_id, encoded, time.time()),
            )
            count, total_bytes = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(payload)), 0) FROM completed_results"
            ).fetchone()
            while count > RESULT_CACHE_MAX_EVENTS or total_bytes > RESULT_CACHE_MAX_BYTES:
                oldest = connection.execute(
                    "SELECT task_id, LENGTH(payload) FROM completed_results ORDER BY created_at LIMIT 1"
                ).fetchone()
                if not oldest:
                    break
                connection.execute("DELETE FROM completed_results WHERE task_id = ?", (oldest[0],))
                count -= 1
                total_bytes -= int(oldest[1])
            connection.commit()
            try:
                EVENTS_DB.chmod(0o600)
            except OSError:
                pass
        finally:
            connection.close()


def _completed_result(task_id: str) -> dict | None:
    with EVENTS_LOCK:
        connection = _outbox_connect()
        try:
            row = connection.execute(
                "SELECT payload FROM completed_results WHERE task_id = ?", (task_id,)
            ).fetchone()
            return json.loads(row[0]) if row else None
        finally:
            connection.close()


def _sender() -> None:
    retry_delay = 1.0
    failures = 0
    while True:
        try:
            pending = _outbox_next()
        except (OSError, sqlite3.Error, ValueError) as exc:
            print(f"[Lush Forge History] Event outbox unavailable: {type(exc).__name__}: {exc}")
            time.sleep(2)
            continue
        if pending is None:
            try:
                EVENTS.get(timeout=2)
            except Empty:
                pass
            EVENTS_WAKE.wait(timeout=2)
            EVENTS_WAKE.clear()
            continue

        event_id, event = pending
        try:
            EVENTS.get_nowait()
        except Empty:
            pass
        try:
            _post(event)
        except HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code not in (408, 425, 429):
                print(f"[Lush Forge History] Event rejected by Hub (HTTP {exc.code}); dropping stale event")
                try:
                    _outbox_delete(event_id)
                except (OSError, sqlite3.Error) as delete_exc:
                    print(f"[Lush Forge History] Event outbox cleanup failed: {type(delete_exc).__name__}")
                retry_delay = 1.0
                failures = 0
                continue
            failure = f"HTTP {exc.code}"
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
        else:
            try:
                _outbox_delete(event_id)
            except (OSError, sqlite3.Error) as exc:
                print(f"[Lush Forge History] Event outbox ack failed: {type(exc).__name__}: {exc}")
            retry_delay = 1.0
            failures = 0
            continue

        failures += 1
        if failures == 1 or failures % 8 == 0:
            print(f"[Lush Forge History] Event delivery deferred: {failure}; retrying in {retry_delay:g}s")
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 30.0)


def _emit(task_id: str | None, event: str, **extra) -> bool:
    if not CONFIG or not task_id or not task_id.startswith("task("):
        return False
    try:
        queued_event = {"task_id": task_id, "event": event, **extra}
        persisted = _outbox_add(queued_event)
        if not persisted:
            print("[Lush Forge History] Event outbox is full; event was not queued")
            return False
        try:
            EVENTS.put_nowait(queued_event)
        except Full:
            pass
        EVENTS_WAKE.set()
        return True
    except (OSError, sqlite3.Error) as exc:
        print(f"[Lush Forge History] Event could not be persisted: {type(exc).__name__}: {exc}")
        return False


def _remember(table: OrderedDict[str, bool], task_id: str) -> None:
    table[task_id] = True
    if len(table) > 1024:
        table.popitem(last=False)


def _safe_output_path(filename) -> str | None:
    if not filename:
        return None
    try:
        path = Path(filename).resolve()
        path.relative_to(OUTPUT_ROOT)
        return str(path)
    except (OSError, TypeError, ValueError):
        return None


def _emit_thumbnail(task_id: str | None, image, filename=None, *, completed: bool = False) -> bool:
    if not task_id or not _is_image_like(image):
        return False
    # A late result fallback may need to add image_path after the save callback
    # already supplied a thumbnail. Re-emitting the thumbnail is intentional;
    # the Hub stores the first image bytes and fills a previously missing path.
    if task_id in THUMBNAILS and not filename:
        return False
    try:
        thumbnail = image.copy()
        thumbnail.thumbnail((180, 180))
        if thumbnail.mode != "RGB":
            thumbnail = thumbnail.convert("RGB")
        buffer = io.BytesIO()
        thumbnail.save(buffer, format="JPEG", quality=78)
        payload = {"thumbnail_b64": base64.b64encode(buffer.getvalue()).decode("ascii")}
        safe_path = _safe_output_path(filename)
        if safe_path:
            payload["image_path"] = safe_path
        if completed:
            try:
                _remember_completed_result(task_id, payload)
            except (OSError, sqlite3.Error, ValueError) as exc:
                print(f"[Lush Forge History] Result recovery record failed: {type(exc).__name__}: {exc}")
        if _emit(task_id, "thumbnail", **payload):
            _remember(THUMBNAILS, task_id)
            if safe_path:
                OUTPUT_PATHS[task_id] = safe_path
                if len(OUTPUT_PATHS) > 1024:
                    OUTPUT_PATHS.popitem(last=False)
            return True
    except Exception as exc:
        print(f"[Lush Forge History] Thumbnail failed: {type(exc).__name__}: {exc}")
    return False


def _persist_fallback_image(task_id: str | None, image) -> str | None:
    """Persist a Gallery result when Forge did not fire on_image_saved."""
    if not task_id or not _is_image_like(image):
        return None
    try:
        FALLBACK_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
        target = FALLBACK_OUTPUT_ROOT / f"{digest}.png"
        temporary = FALLBACK_OUTPUT_ROOT / f".{digest}.uploading"
        image.copy().save(temporary, format="PNG")
        temporary.replace(target)
        return str(target)
    except Exception as exc:
        print(f"[Lush Forge History] Fallback output failed: {type(exc).__name__}: {exc}")
        try:
            temporary.unlink(missing_ok=True)
        except (UnboundLocalError, OSError):
            pass
        return None


def _is_image_like(value) -> bool:
    return isinstance(value, PILImage.Image) or (
        value is not None
        and callable(getattr(value, "copy", None))
        and callable(getattr(value, "save", None))
    )


def _first_result_image(value, depth: int = 0):
    if depth > 5 or value is None:
        return None
    if _is_image_like(value):
        return value
    if isinstance(value, dict):
        for key in ("image", "value", "data", "path", "name"):
            found = _first_result_image(value.get(key), depth + 1)
            if found is not None:
                return found
        return None
    path = getattr(value, "path", None) or getattr(value, "name", None)
    if isinstance(path, str):
        try:
            resolved = Path(path).resolve()
            resolved.relative_to(OUTPUT_ROOT)
            with PILImage.open(resolved) as image:
                return image.copy()
        except (OSError, TypeError, ValueError):
            pass
    if isinstance(value, str):
        try:
            resolved = Path(value).resolve()
            resolved.relative_to(OUTPUT_ROOT)
            with PILImage.open(resolved) as image:
                return image.copy()
        except (OSError, TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _first_result_image(item, depth + 1)
            if found is not None:
                return found
    return None


def _on_image_saved(params) -> None:
    task_id = progress.current_task or LAST_TASK_ID
    _emit_thumbnail(task_id, params.image, params.filename)


def _register_lora_upload_api(_: object, app) -> None:
    """Expose a private, worker-key-protected streaming LoRA upload endpoint."""
    from modules import shared
    import networks
    import network as lora_network

    lora_root = Path(getattr(shared.cmd_opts, "lora_dir", Path.home() / "forge" / "models" / "Lora")).expanduser().resolve()

    if not getattr(lora_network.NetworkOnDisk.get_alias, "_lush_account_scoped", False):
        original_init = lora_network.NetworkOnDisk.__init__
        original_get_alias = lora_network.NetworkOnDisk.get_alias

        def account_scoped_init(model, name, filename):
            original_init(model, name, filename)
            if model.name.startswith(PRIVATE_LORA_PREFIX):
                model.alias = model.name

        account_scoped_init._lush_account_scoped = True

        def account_scoped_alias(model):
            if model.name.startswith(PRIVATE_LORA_PREFIX):
                return model.name
            return original_get_alias(model)

        account_scoped_alias._lush_account_scoped = True
        lora_network.NetworkOnDisk.__init__ = account_scoped_init
        lora_network.NetworkOnDisk.get_alias = account_scoped_alias
        try:
            networks.list_available_networks()
        except Exception as exc:
            print(f"[Lush Forge History] Account LoRA index initialization failed: {type(exc).__name__}: {exc}")

    def require_worker_key(worker_key: str | None) -> None:
        expected = CONFIG.get("key", "") if CONFIG else ""
        if not expected or not worker_key or not hmac.compare_digest(expected, worker_key):
            raise HTTPException(status_code=403, detail="Worker key không hợp lệ")

    def private_prefix(account_id: int | None) -> str:
        if not isinstance(account_id, int) or account_id < 1:
            raise HTTPException(status_code=403, detail="Tài khoản upload không hợp lệ")
        return f"{PRIVATE_LORA_PREFIX}{account_id}__"

    def upload_target(filename: str, account_id: int) -> Path:
        if not filename or filename in {".", ".."} or len(filename) > 180 or "/" in filename or "\\" in filename:
            raise HTTPException(status_code=400, detail="Tên file LoRA không hợp lệ")
        if Path(filename).suffix.lower() not in LORA_SUFFIXES:
            raise HTTPException(status_code=400, detail="Chỉ nhận file .safetensors, .ckpt hoặc .pt")
        target = (lora_root / f"{private_prefix(account_id)}{filename}").resolve()
        target.relative_to(lora_root)
        if (lora_root / filename).exists():
            raise HTTPException(status_code=409, detail="LoRA này đã có bản dùng chung trên máy Forge")
        return target

    def prune_finished_uploads() -> None:
        now = time.monotonic()
        for upload_id, record in list(LORA_UPLOADS.items()):
            if not record.get("running") and now - record["updated"] > 3600:
                LORA_UPLOADS.pop(upload_id, None)

    async def upload_lora(
        file: UploadFile = File(...),
        worker_key: str | None = Header(default=None, alias="X-Forge-Worker-Key"),
        account_id: int | None = Header(default=None, alias="X-Lush-Account-Id"),
    ):
        require_worker_key(worker_key)
        private_prefix(account_id)

        filename = Path(file.filename or "").name.strip()
        suffix = Path(filename).suffix.lower()
        if not filename or filename in {".", ".."} or len(filename) > 180:
            raise HTTPException(status_code=400, detail="Tên file LoRA không hợp lệ")
        if suffix not in LORA_SUFFIXES:
            raise HTTPException(status_code=400, detail="Chỉ nhận file .safetensors, .ckpt hoặc .pt")
        lora_root.mkdir(parents=True, exist_ok=True)
        target = upload_target(filename, account_id)
        if target.exists():
            raise HTTPException(status_code=409, detail="File LoRA đã tồn tại trên Forge")

        temporary = lora_root / f".{filename}.{uuid4().hex}.uploading"
        size = 0
        try:
            with temporary.open("wb") as handle:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > LORA_UPLOAD_MAX_BYTES:
                        raise HTTPException(status_code=413, detail="File LoRA vượt quá giới hạn 2 GB")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if target.exists():
                raise HTTPException(status_code=409, detail="File LoRA đã tồn tại trên Forge")
            os.replace(temporary, target)
        finally:
            await file.close()
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

        try:
            networks.list_available_networks()
        except Exception as exc:
            print(f"[Lush Forge History] LoRA refresh failed: {type(exc).__name__}: {exc}")
        return {"ok": True, "filename": filename, "stored_name": target.stem, "size": size}

    app.add_api_route("/internal/lora/upload", upload_lora, methods=["POST"])

    async def upload_stream(
        upload_id: str, request: FastAPIRequest,
        worker_key: str | None = Header(default=None, alias="X-Forge-Worker-Key"),
        account_id: int | None = Header(default=None, alias="X-Lush-Account-Id"),
    ):
        require_worker_key(worker_key)
        private_prefix(account_id)
        try:
            upload_id = str(UUID(upload_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Mã upload không hợp lệ") from exc
        filename = request.query_params.get("filename", "")
        target = upload_target(filename, account_id)
        lora_root.mkdir(parents=True, exist_ok=True)
        temporary = lora_root / f".{upload_id}.uploading"
        size = 0

        with LORA_UPLOAD_LOCK:
            prune_finished_uploads()
            record = LORA_UPLOADS.setdefault(upload_id, {"cancelled": False, "path": None, "running": False, "account_id": account_id, "updated": time.monotonic()})
            if record["account_id"] != account_id:
                raise HTTPException(status_code=403, detail="Upload thuộc tài khoản khác")
            if record["cancelled"]:
                raise HTTPException(status_code=409, detail="Upload đã hủy")
            if record["running"] or record["path"] or target.exists():
                raise HTTPException(status_code=409, detail="File LoRA đã tồn tại trên Forge")
            record["running"] = True

        try:
            with temporary.open("wb") as handle:
                async for chunk in request.stream():
                    with LORA_UPLOAD_LOCK:
                        if record["cancelled"]:
                            raise HTTPException(status_code=409, detail="Upload đã hủy")
                        record["updated"] = time.monotonic()
                    size += len(chunk)
                    if size > LORA_UPLOAD_MAX_BYTES:
                        raise HTTPException(status_code=413, detail="File LoRA vượt quá giới hạn 2 GB")
                    handle.write(chunk)
                if size == 0:
                    raise HTTPException(status_code=400, detail="File LoRA trống")
                handle.flush()
                os.fsync(handle.fileno())
            with LORA_UPLOAD_LOCK:
                if record["cancelled"]:
                    raise HTTPException(status_code=409, detail="Upload đã hủy")
                if target.exists():
                    raise HTTPException(status_code=409, detail="File LoRA đã tồn tại trên Forge")
                os.replace(temporary, target)
                record["path"] = str(target)
                record["updated"] = time.monotonic()
            try:
                networks.list_available_networks()
            except Exception as exc:
                print(f"[Lush Forge History] LoRA refresh failed: {type(exc).__name__}: {exc}")
            with LORA_UPLOAD_LOCK:
                if record["cancelled"]:
                    raise HTTPException(status_code=409, detail="Upload đã hủy")
            return {"ok": True, "filename": filename, "stored_name": target.stem, "size": size}
        finally:
            temporary.unlink(missing_ok=True)
            with LORA_UPLOAD_LOCK:
                record["running"] = False

    async def cancel_upload(
        upload_id: str,
        worker_key: str | None = Header(default=None, alias="X-Forge-Worker-Key"),
        account_id: int | None = Header(default=None, alias="X-Lush-Account-Id"),
    ):
        require_worker_key(worker_key)
        private_prefix(account_id)
        try:
            upload_id = str(UUID(upload_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Mã upload không hợp lệ") from exc
        with LORA_UPLOAD_LOCK:
            prune_finished_uploads()
            record = LORA_UPLOADS.setdefault(upload_id, {"cancelled": False, "path": None, "running": False, "account_id": account_id, "updated": time.monotonic()})
            if record["account_id"] != account_id:
                raise HTTPException(status_code=403, detail="Upload thuộc tài khoản khác")
            record["cancelled"] = True
            record["updated"] = time.monotonic()
            path = record["path"]
            if path:
                Path(path).unlink(missing_ok=True)
                record["path"] = None
        if path:
            try:
                networks.list_available_networks()
            except Exception as exc:
                print(f"[Lush Forge History] LoRA refresh after cancel failed: {type(exc).__name__}: {exc}")
        return {"ok": True, "cancelled": True}

    app.add_api_route("/internal/lora/uploads/{upload_id}", upload_stream, methods=["POST"])
    app.add_api_route("/internal/lora/uploads/{upload_id}/cancel", cancel_upload, methods=["POST"])

    async def account_loras(
        worker_key: str | None = Header(default=None, alias="X-Forge-Worker-Key"),
        account_id: int | None = Header(default=None, alias="X-Lush-Account-Id"),
    ):
        require_worker_key(worker_key)
        prefix = private_prefix(account_id)
        items = []
        for path in sorted(lora_root.iterdir()) if lora_root.exists() else []:
            if not path.is_file() or path.suffix.lower() not in LORA_SUFFIXES:
                continue
            name = path.stem
            if name.startswith(PRIVATE_LORA_PREFIX) and not name.startswith(prefix):
                continue
            private = name.startswith(prefix)
            entry = networks.available_networks.get(name)
            aliases = [name] if private else [name, getattr(entry, "alias", name)]
            items.append({
                "name": name,
                "display_name": name[len(prefix):] if private else name,
                "scope": "private" if private else "shared",
                "aliases": list(dict.fromkeys(aliases)),
            })
        return {"items": items}

    app.add_api_route("/internal/lora/account-manifest", account_loras, methods=["GET"])

    async def task_result(
        task_id: str,
        worker_key: str | None = Header(default=None, alias="X-Forge-Worker-Key"),
    ):
        require_worker_key(worker_key)
        if not task_id.startswith("task(") or not task_id.endswith(")") or len(task_id) > 100:
            raise HTTPException(status_code=400, detail="Mã job không hợp lệ")

        digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
        output_path = (FALLBACK_OUTPUT_ROOT / f"{digest}.png").resolve()
        output_path.relative_to(FALLBACK_OUTPUT_ROOT)
        saved_result = _completed_result(task_id)
        if saved_result:
            return {
                "completed": True,
                "image_path": saved_result.get("image_path"),
                "thumbnail_b64": saved_result.get("thumbnail_b64"),
            }

        completed = task_id in COMPLETED or output_path.is_file()
        if not completed:
            return {"completed": False, "image_path": None, "thumbnail_b64": None}

        thumbnail_b64 = None
        if output_path.is_file():
            try:
                with PILImage.open(output_path) as source:
                    thumbnail = source.copy()
                thumbnail.thumbnail((180, 180))
                if thumbnail.mode != "RGB":
                    thumbnail = thumbnail.convert("RGB")
                buffer = io.BytesIO()
                thumbnail.save(buffer, format="JPEG", quality=78)
                thumbnail_b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
            except (OSError, ValueError) as exc:
                print(f"[Lush Forge History] Recovery thumbnail failed: {type(exc).__name__}: {exc}")
                output_path = None

        return {
            "completed": True,
            "image_path": str(output_path) if output_path else None,
            "thumbnail_b64": thumbnail_b64,
        }

    app.add_api_route("/internal/history/result/{task_id}", task_result, methods=["GET"])


if CONFIG and not getattr(progress.start_task, "_lush_history_wrapped", False):
    original_start = progress.start_task
    original_record = progress.record_results
    original_finish = progress.finish_task

    def start_task(task_id):
        global LAST_TASK_ID
        LAST_TASK_ID = task_id
        result = original_start(task_id)
        _emit(task_id, "running")
        return result

    def record_results(task_id, results):
        result = original_record(task_id, results)
        if task_id:
            result_image = _first_result_image(results)
            fallback_path = None
            output_path = OUTPUT_PATHS.get(task_id)
            if not output_path:
                fallback_path = _persist_fallback_image(task_id, result_image)
            _emit_thumbnail(task_id, result_image, fallback_path or output_path, completed=True)
        _remember(COMPLETED, task_id)
        _emit(task_id, "done")
        return result

    def finish_task(task_id):
        result = original_finish(task_id)
        if task_id not in COMPLETED:
            _emit(task_id, "failed")
        return result

    start_task._lush_history_wrapped = True
    progress.start_task = start_task
    progress.record_results = record_results
    progress.finish_task = finish_task
    script_callbacks.on_image_saved(_on_image_saved)
    if getattr(script_callbacks, "on_app_started", None):
        script_callbacks.on_app_started(_register_lora_upload_api, name="lush-forge-lora-upload")
    threading.Thread(target=_sender, name="lush-forge-history-sender", daemon=True).start()
    print(f"[Lush Forge History] Enabled on {CONFIG['worker_id']}")
