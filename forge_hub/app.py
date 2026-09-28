"""Authenticated gateway for two private Forge instances."""

from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
import hmac
import json
from pathlib import Path, PurePosixPath
import re
import secrets
import uuid
from urllib.parse import quote, urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from .settings import load_settings
from .queue_relay import QueueRelay
from .store import COOKIE_NAME, SESSION_DAYS, Store


SETTINGS = load_settings()
STORE = Store(SETTINGS.database_path)
ASSET_DIR = Path(__file__).parent / "static"
HOP_HEADERS = {
    "authorization", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length",
}
OUTPUT_ROOT = "/home/ubuntu/forge/outputs/"
MAX_GENERATION_REQUEST_BYTES = 20_000_000
QUEUE_RECOVERY_GRACE_SECONDS = 90
CANCEL_RECOVERY_GRACE_SECONDS = 20
WORKER_CONTROL_TIMEOUT = httpx.Timeout(connect=5, read=8, write=15, pool=5)
FORGE_HEAD_INJECTION = (
    '<link rel="stylesheet" href="/hub/assets/queue-controls.css">'
    '<link rel="stylesheet" href="/hub/assets/history.css">'
    '<link rel="stylesheet" href="/hub/assets/lora-upload.css?v=3" data-lush-lora-upload>'
    '<script defer src="/hub/assets/history.js"></script>'
    '<script defer src="/hub/assets/queue-controls.js"></script>'
    '<script defer src="/hub/assets/lora-upload.js?v=3" data-lush-lora-upload></script>'
).encode("utf-8")
NON_BLOCKING_FORGE_SCRIPT = b' src="file=extensions/sd-webui-infinite-image-browsing/javascript/index.js?'
MAX_LORA_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
LORA_SUFFIXES = {".safetensors", ".ckpt", ".pt"}
LORA_TAG_RE = re.compile(r"<lora:([^:>]+):[^>]*>", re.IGNORECASE)


async def _app_worker_json(
    app: FastAPI, worker_id: str, path: str, body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> dict:
    url = SETTINGS.worker_urls[worker_id] + path
    try:
        if body is None:
            response = await app.state.http.get(url, headers=headers, timeout=WORKER_CONTROL_TIMEOUT)
        else:
            response = await app.state.http.post(
                url, json=body, headers=headers, timeout=WORKER_CONTROL_TIMEOUT,
            )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f"{worker_id} unavailable") from exc


async def _resubmit_saved_job(app: FastAPI, job: dict) -> None:
    payload = json.loads(job["request_json"])
    session_hash = "hub-" + secrets.token_urlsafe(18)
    payload["session_hash"] = session_hash
    request_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    updated = await asyncio.to_thread(
        STORE.update_job_session, job["worker_id"], job["task_id"], session_hash, request_json,
    )
    if not updated:
        return
    response = await app.state.http.post(
        SETTINGS.worker_urls[job["worker_id"]] + "/queue/join",
        content=request_json,
        headers={"Content-Type": "application/json"},
        timeout=WORKER_CONTROL_TIMEOUT,
    )
    response.raise_for_status()
    event_id = response.json().get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise RuntimeError("Forge did not return an event id during recovery")
    await asyncio.to_thread(STORE.attach_job_event, job["worker_id"], job["task_id"], session_hash, event_id)
    await app.state.queue_relay.ensure(job["worker_id"], job["account_id"], session_hash)


async def _reconcile_completed_job(app: FastAPI, job: dict) -> bool | None:
    worker_id = job["worker_id"]
    worker_key = SETTINGS.worker_keys.get(worker_id, "")
    result_path = "/internal/history/result/" + quote(job["task_id"], safe="")
    try:
        result = await _app_worker_json(
            app, worker_id, result_path,
            headers={"X-Forge-Worker-Key": worker_key},
        )
    except RuntimeError:
        return None
    if not result.get("completed"):
        return False

    image_path = result.get("image_path")
    thumbnail_b64 = result.get("thumbnail_b64")
    thumbnail = None
    if isinstance(thumbnail_b64, str):
        try:
            thumbnail = base64.b64decode(thumbnail_b64, validate=True)
        except (binascii.Error, ValueError):
            thumbnail = None

    if _valid_image_path(image_path) and thumbnail and len(thumbnail) <= 150_000:
        await asyncio.to_thread(
            STORE.update_job_event, worker_id, job["task_id"], "thumbnail", image_path, thumbnail,
        )
        await asyncio.to_thread(STORE.update_job_event, worker_id, job["task_id"], "done")
    else:
        await asyncio.to_thread(
            STORE.fail_recovery_job, worker_id, job["task_id"],
            "Forge đã kết thúc job nhưng không còn ảnh kết quả để khôi phục; Hub không tự chạy lại để tránh tạo ảnh trùng.",
        )
    return True


async def _recover_queued_jobs(app: FastAPI) -> None:
    while True:
        app.state.queue_relay.prune_closed()
        jobs = await asyncio.to_thread(STORE.list_recoverable_jobs)
        for job in jobs:
            status = job["status"]
            if status not in ("queued", "running", "cancelling"):
                continue
            if status != "cancelling" and (not job["request_json"] or not job["session_hash"]):
                continue

            if status == "cancelling" and job.get("cancel_requested_at"):
                try:
                    cancel_time = datetime.fromisoformat(job["cancel_requested_at"])
                    cancel_age = (datetime.now(timezone.utc) - cancel_time.astimezone(timezone.utc)).total_seconds()
                except (TypeError, ValueError):
                    cancel_age = CANCEL_RECOVERY_GRACE_SECONDS
                if cancel_age < CANCEL_RECOVERY_GRACE_SECONDS:
                    continue

            channel = app.state.queue_relay.get(job["worker_id"], job["account_id"], job["session_hash"])
            if channel and not channel.closed and status != "cancelling":
                continue
            try:
                progress = await _app_worker_json(app, job["worker_id"], "/internal/progress", {
                    "id_task": job["task_id"], "live_preview": False,
                })
            except RuntimeError:
                continue

            if status == "cancelling" and progress.get("active"):
                await asyncio.to_thread(
                    STORE.mark_cancel_race_running, job["account_id"], job["task_id"],
                )
                continue

            if status == "cancelling" and progress.get("queued"):
                if not job.get("event_id") or not job.get("session_hash") or job.get("fn_index") is None:
                    continue
                try:
                    await _app_worker_json(app, job["worker_id"], "/cancel", {
                        "event_id": job["event_id"],
                        "session_hash": job["session_hash"],
                        "fn_index": job["fn_index"],
                    })
                except RuntimeError:
                    pass
                await asyncio.sleep(0.25)
                try:
                    progress = await _app_worker_json(app, job["worker_id"], "/internal/progress", {
                        "id_task": job["task_id"], "live_preview": False,
                    })
                except RuntimeError:
                    continue
                if progress.get("active"):
                    await asyncio.to_thread(
                        STORE.mark_cancel_race_running, job["account_id"], job["task_id"],
                    )
                    continue
                if progress.get("queued"):
                    continue

            if status == "cancelling" and not progress.get("active") and not progress.get("queued"):
                reconciled = await _reconcile_completed_job(app, job)
                if reconciled is True:
                    continue
                if reconciled is None and progress.get("completed"):
                    continue
                if progress.get("completed"):
                    await asyncio.to_thread(
                        STORE.fail_recovery_job, job["worker_id"], job["task_id"],
                        "Forge đã kết thúc job nhưng không còn ảnh kết quả để khôi phục; Hub không tự chạy lại để tránh tạo ảnh trùng.",
                    )
                    continue
                if await asyncio.to_thread(
                    STORE.finish_job_cancel, job["account_id"], job["task_id"],
                ):
                    await asyncio.to_thread(
                        STORE.delete_finished_job, job["account_id"], job["task_id"],
                    )
                continue

            if progress.get("active") or progress.get("queued"):
                try:
                    await app.state.queue_relay.ensure(job["worker_id"], job["account_id"], job["session_hash"])
                except RuntimeError:
                    pass
                continue

            reconciled = await _reconcile_completed_job(app, job)
            if reconciled is True:
                continue
            if reconciled is None and progress.get("completed"):
                continue
            if progress.get("completed"):
                await asyncio.to_thread(
                    STORE.fail_recovery_job, job["worker_id"], job["task_id"],
                    "Forge đã kết thúc job nhưng không còn ảnh kết quả để khôi phục; Hub không tự chạy lại để tránh tạo ảnh trùng.",
                )
                continue

            if job["status"] == "running":
                await asyncio.to_thread(
                    STORE.fail_recovery_job, job["worker_id"], job["task_id"],
                    "Forge không còn báo job đang chạy và không có ảnh kết quả để khôi phục; Hub không tự chạy lại để tránh tạo ảnh trùng.",
                )
                continue

            try:
                pending = await _app_worker_json(app, job["worker_id"], "/internal/pending-tasks")
                if job["task_id"] in pending.get("tasks", []):
                    try:
                        await app.state.queue_relay.ensure(
                            job["worker_id"], job["account_id"], job["session_hash"],
                        )
                    except RuntimeError:
                        pass
                    continue
            except RuntimeError:
                continue

            try:
                created = datetime.fromisoformat(job["created_at"])
                age = (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                age = QUEUE_RECOVERY_GRACE_SECONDS
            if age < QUEUE_RECOVERY_GRACE_SECONDS:
                continue
            try:
                await _resubmit_saved_job(app, job)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                print(f"[Lush Forge Hub] Queue recovery deferred for {job['worker_id']}: {type(exc).__name__}")
        await asyncio.sleep(15)


async def _recover_completed_history(app: FastAPI) -> None:
    jobs = await asyncio.to_thread(STORE.list_completed_jobs_missing_output)
    for job in jobs:
        try:
            await _reconcile_completed_job(app, job)
        except Exception as exc:
            print(f"[Lush Forge Hub] Completed result backfill deferred for {job['worker_id']}: {type(exc).__name__}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(STORE.initialize)
    app.state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=8, read=None, write=600, pool=8),
        follow_redirects=False,
        trust_env=False,
    )
    app.state.generate_inputs = {}
    app.state.queue_relay = QueueRelay(app.state.http, SETTINGS.worker_urls)
    app.state.queue_recovery_task = asyncio.create_task(_recover_queued_jobs(app), name="forge-queue-recovery")
    app.state.completed_history_task = asyncio.create_task(
        _recover_completed_history(app), name="forge-completed-history-recovery",
    )
    try:
        yield
    finally:
        app.state.queue_recovery_task.cancel()
        app.state.completed_history_task.cancel()
        with suppress(asyncio.CancelledError):
            await app.state.queue_recovery_task
        with suppress(asyncio.CancelledError):
            await app.state.completed_history_task
        await app.state.queue_relay.close()
        await app.state.http.aclose()


app = FastAPI(title="Lush Forge Hub", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/hub/assets", StaticFiles(directory=ASSET_DIR), name="assets")


class LoginBody(BaseModel):
    username: str
    password: str


class NewAccountBody(BaseModel):
    username: str
    password: str
    worker_id: str


class UpdateAccountBody(BaseModel):
    worker_id: str | None = None
    is_active: bool | None = None


class PasswordBody(BaseModel):
    password: str = Field(min_length=1)


class WorkerEventBody(BaseModel):
    worker_id: str
    task_id: str
    event: str
    image_path: str | None = None
    thumbnail_b64: str | None = None


def _lora_filename(filename: str | None) -> str:
    """Return a safe, flat LoRA filename or raise a client-facing error."""
    name = Path(filename or "").name.strip()
    if not name or name in {".", ".."} or len(name) > 180 or "/" in (filename or "") or "\\" in (filename or ""):
        raise HTTPException(status_code=400, detail="Tên file LoRA không hợp lệ")
    if Path(name).suffix.lower() not in LORA_SUFFIXES:
        raise HTTPException(status_code=400, detail="Chỉ nhận file .safetensors, .ckpt hoặc .pt")
    return name


async def _account(request: Request) -> dict | None:
    return await asyncio.to_thread(STORE.resolve_session, request.cookies.get(COOKIE_NAME))


async def _required_account(request: Request) -> dict:
    account = await _account(request)
    if not account:
        raise HTTPException(status_code=401, detail="Phiên đăng nhập đã hết hạn")
    return account


async def _required_admin(request: Request) -> dict:
    account = await _required_account(request)
    if account["must_change_password"]:
        raise HTTPException(status_code=403, detail="Cần đổi mật khẩu tạm")
    if account["role"] != "admin":
        raise HTTPException(status_code=403, detail="Cần tài khoản admin")
    return account


def _check_origin(request: Request) -> None:
    # Browser mutations must originate from this app. SameSite=Lax protects the
    # cookie too, but explicit origin checking covers browsers with old policy.
    if request.headers.get("origin") != SETTINGS.public_origin:
        raise HTTPException(status_code=403, detail="Origin không hợp lệ")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/hub/login")
async def login_page(request: Request):
    account = await _account(request)
    if account:
        return RedirectResponse("/hub/change-password" if account["must_change_password"] else "/", status_code=303)
    return FileResponse(ASSET_DIR / "login.html", media_type="text/html; charset=utf-8")


@app.post("/hub/api/login")
async def login(body: LoginBody, request: Request):
    account = await asyncio.to_thread(STORE.authenticate, body.username, body.password)
    if not account:
        await asyncio.sleep(0.5)
        raise HTTPException(status_code=401, detail="Sai tài khoản hoặc mật khẩu")
    token = await asyncio.to_thread(STORE.create_session, account["id"])
    response = JSONResponse({"ok": True, "account": account, "next": "/hub/change-password" if account["must_change_password"] else "/"})
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, secure=SETTINGS.secure_cookie,
        samesite="lax", max_age=SESSION_DAYS * 86400, path="/",
    )
    return response


@app.post("/hub/api/logout")
async def logout(request: Request):
    _check_origin(request)
    await asyncio.to_thread(STORE.revoke_session, request.cookies.get(COOKIE_NAME))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/hub/api/me")
async def me(request: Request):
    return await _required_account(request)


def _parse_datetime_bound(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{name} không hợp lệ") from exc
    if parsed.tzinfo is None:
        raise HTTPException(status_code=400, detail=f"{name} phải có múi giờ")
    return parsed.astimezone(timezone.utc).isoformat()


@app.get("/hub/api/jobs")
async def list_jobs(
    request: Request, limit: int = 100,
    created_from: str | None = None, created_before: str | None = None,
):
    account = await _required_account(request)
    start = _parse_datetime_bound(created_from, "created_from")
    end = _parse_datetime_bound(created_before, "created_before")
    if start and end and start >= end:
        raise HTTPException(status_code=400, detail="Khoảng ngày lọc không hợp lệ")
    return await asyncio.to_thread(STORE.list_jobs, account["id"], limit, start, end)


@app.get("/hub/api/jobs/{task_id}/thumbnail")
async def job_thumbnail(task_id: str, request: Request):
    account = await _required_account(request)
    job = await asyncio.to_thread(STORE.get_job, account["id"], task_id)
    if not job or not job["thumbnail"]:
        raise HTTPException(status_code=404, detail="Chưa có thumbnail")
    return Response(job["thumbnail"], media_type="image/jpeg", headers={"Cache-Control": "private, max-age=60"})


@app.get("/hub/api/jobs/{task_id}/image")
async def job_image(task_id: str, request: Request):
    account = await _required_account(request)
    job = await asyncio.to_thread(STORE.get_job, account["id"], task_id)
    if not job or not job["image_path"]:
        raise HTTPException(status_code=404, detail="Chưa có ảnh kết quả")
    path = job["image_path"]
    if not _valid_image_path(path):
        raise HTTPException(status_code=404, detail="Đường dẫn ảnh không hợp lệ")
    url = SETTINGS.worker_urls[job["worker_id"]] + "/file=" + quote(path, safe="/")
    try:
        upstream = await request.app.state.http.send(
            request.app.state.http.build_request("GET", url), stream=True,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Không tải được ảnh từ Forge") from exc
    if upstream.status_code != 200:
        await upstream.aclose()
        raise HTTPException(status_code=404, detail="Ảnh không còn trên Forge")
    return StreamingResponse(
        upstream.aiter_raw(), media_type=upstream.headers.get("content-type", "image/png"),
        headers={"Cache-Control": "private, max-age=60"}, background=BackgroundTask(upstream.aclose),
    )


@app.delete("/hub/api/jobs/{task_id}")
async def delete_job(task_id: str, request: Request):
    account = await _required_account(request)
    _check_origin(request)
    deleted = await asyncio.to_thread(STORE.delete_finished_job, account["id"], task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Chỉ có thể xóa job đã kết thúc")
    return {"ok": True}


async def _worker_json(request: Request, worker_id: str, path: str, body: dict | None = None) -> dict:
    try:
        return await _app_worker_json(request.app, worker_id, path, body)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"Không liên lạc được với {worker_id}") from exc


async def _worker_lora_upload(request: Request, account: dict, upload: UploadFile, filename: str) -> dict:
    """Stream one authenticated LoRA file to the account's private Forge worker."""
    worker_id = account["worker_id"]
    url = SETTINGS.worker_urls[worker_id] + "/internal/lora/upload"
    try:
        response = await request.app.state.http.post(
            url,
            headers={
                "X-Forge-Worker-Key": SETTINGS.worker_keys.get(worker_id, ""),
                "X-Lush-Account-Id": str(account["id"]),
            },
            files={
                "file": (
                    filename,
                    upload.file,
                    upload.content_type or "application/octet-stream",
                ),
            },
        )
    except (httpx.HTTPError, OSError) as exc:
        raise HTTPException(status_code=502, detail=f"Không liên lạc được với {worker_id}") from exc

    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400:
        detail = data.get("detail") if isinstance(data, dict) else None
        if response.status_code in (400, 409, 413):
            raise HTTPException(status_code=response.status_code, detail=detail or "Forge từ chối file LoRA")
        raise HTTPException(status_code=502, detail="Forge không nhận được file LoRA")
    if not isinstance(data, dict) or not data.get("ok"):
        raise HTTPException(status_code=502, detail="Forge không xác nhận file LoRA")
    return {"ok": True, "filename": data.get("filename", filename), "size": data.get("size", 0)}


async def _account_lora_manifest(request: Request, account: dict) -> dict:
    worker_id = account["worker_id"]
    try:
        response = await request.app.state.http.get(
            SETTINGS.worker_urls[worker_id] + "/internal/lora/account-manifest",
            headers={
                "X-Forge-Worker-Key": SETTINGS.worker_keys.get(worker_id, ""),
                "X-Lush-Account-Id": str(account["id"]),
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Không tải được danh sách LoRA từ {worker_id}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise HTTPException(status_code=502, detail="Danh sách LoRA từ Forge không hợp lệ")
    return payload


@app.get("/hub/api/lora/manifest")
async def account_lora_manifest(request: Request):
    account = await _required_account(request)
    return await _account_lora_manifest(request, account)


@app.post("/hub/api/lora/uploads/{upload_id}")
async def stream_lora_upload(upload_id: str, request: Request):
    account = await _required_account(request)
    _check_origin(request)
    try:
        upload_id = str(uuid.UUID(upload_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Mã upload không hợp lệ") from exc
    filename = _lora_filename(request.query_params.get("filename"))
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            byte_count = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Content-Length không hợp lệ") from exc
        if byte_count < 1 or byte_count > MAX_LORA_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File LoRA trống hoặc vượt giới hạn 2 GB")

    worker_id = account["worker_id"]
    url = SETTINGS.worker_urls[worker_id] + f"/internal/lora/uploads/{upload_id}?{urlencode({'filename': filename})}"
    headers = {
        "Content-Type": "application/octet-stream",
        "X-Forge-Worker-Key": SETTINGS.worker_keys.get(worker_id, ""),
        "X-Lush-Account-Id": str(account["id"]),
    }
    if content_length:
        headers["Content-Length"] = content_length
    try:
        response = await request.app.state.http.post(url, content=request.stream(), headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Không liên lạc được với {worker_id}") from exc
    try:
        result = response.json()
    except ValueError:
        result = {}
    if response.status_code >= 400:
        detail = result.get("detail") if isinstance(result, dict) else None
        status_code = response.status_code if response.status_code in (400, 403, 409, 413) else 502
        raise HTTPException(status_code=status_code, detail=detail or "Forge không nhận được LoRA")
    if not isinstance(result, dict) or not result.get("ok"):
        raise HTTPException(status_code=502, detail="Forge không xác nhận file LoRA")
    return {"ok": True, "filename": filename, "size": result.get("size", 0)}


@app.post("/hub/api/lora/uploads/{upload_id}/cancel")
async def cancel_lora_upload(upload_id: str, request: Request):
    account = await _required_account(request)
    _check_origin(request)
    try:
        upload_id = str(uuid.UUID(upload_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Mã upload không hợp lệ") from exc
    worker_id = account["worker_id"]
    try:
        response = await request.app.state.http.post(
            SETTINGS.worker_urls[worker_id] + f"/internal/lora/uploads/{upload_id}/cancel",
            headers={
                "X-Forge-Worker-Key": SETTINGS.worker_keys.get(worker_id, ""),
                "X-Lush-Account-Id": str(account["id"]),
            },
        )
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Không xác nhận được lệnh hủy trên {worker_id}") from exc
    return {"ok": bool(result.get("cancelled")), "cancelled": bool(result.get("cancelled"))}


def _string_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _string_values(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _string_values(child)


async def _validate_lora_access(request: Request, account: dict, value) -> None:
    requested = {name.strip() for text in _string_values(value) for name in LORA_TAG_RE.findall(text)}
    if not requested:
        return
    manifest = await _account_lora_manifest(request, account)
    allowed: set[str] = set()
    for item in manifest["items"]:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("name"), str):
            allowed.add(item["name"])
        aliases = item.get("aliases")
        if isinstance(aliases, list):
            allowed.update(alias for alias in aliases if isinstance(alias, str))
    denied = sorted(requested - allowed)
    if denied:
        raise HTTPException(status_code=403, detail="Tài khoản này không có quyền dùng LoRA: " + ", ".join(denied[:5]))


@app.get("/sdapi/v1/loras")
async def account_lora_api(request: Request):
    account = await _required_account(request)
    manifest = await _account_lora_manifest(request, account)
    allowed = {item.get("name") for item in manifest["items"] if isinstance(item, dict)}
    try:
        response = await request.app.state.http.get(SETTINGS.worker_urls[account["worker_id"]] + "/sdapi/v1/loras")
        response.raise_for_status()
        models = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Không tải được LoRA từ Forge") from exc
    return [item for item in models if isinstance(item, dict) and item.get("name") in allowed]


@app.post("/hub/api/lora/upload")
async def upload_lora(request: Request):
    account = await _required_account(request)
    _check_origin(request)
    raw_length = request.headers.get("content-length")
    try:
        if raw_length and int(raw_length) > MAX_LORA_UPLOAD_BYTES + 1_048_576:
            raise HTTPException(status_code=413, detail="File LoRA vượt quá giới hạn 2 GB")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Content-Length không hợp lệ") from exc

    try:
        form = await request.form(max_files=1, max_fields=4)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail="Không đọc được file LoRA") from exc
    upload = form.get("file")
    if not isinstance(upload, StarletteUploadFile):
        raise HTTPException(status_code=400, detail="Chưa chọn file LoRA")
    filename = _lora_filename(upload.filename)
    if upload.size is not None and upload.size > MAX_LORA_UPLOAD_BYTES:
        await upload.close()
        raise HTTPException(status_code=413, detail="File LoRA vượt quá giới hạn 2 GB")
    try:
        await upload.seek(0)
        return await _worker_lora_upload(request, account, upload, filename)
    finally:
        await upload.close()


@app.post("/hub/api/jobs/{task_id}/cancel")
async def cancel_job(task_id: str, request: Request):
    account = await _required_account(request)
    _check_origin(request)
    job = await asyncio.to_thread(STORE.begin_job_cancel, account["id"], task_id)
    if not job:
        raise HTTPException(status_code=409, detail="Job không còn ở trạng thái chờ")

    if job["event_id"] and job["session_hash"] and job["fn_index"] is not None:
        async def get_progress() -> dict:
            return await _worker_json(request, job["worker_id"], "/internal/progress", {
                "id_task": task_id, "live_preview": False,
            })

        # Forge's /cancel endpoint can acknowledge a request even after a job
        # has left the queue. Never turn that acknowledgement alone into a
        # cancelled history state: confirm the worker still sees it queued.
        try:
            progress = await get_progress()
            if progress.get("active"):
                await asyncio.to_thread(STORE.mark_cancel_race_running, account["id"], task_id)
                raise HTTPException(status_code=409, detail="Job đã bắt đầu chạy; không thể hủy bằng nút X")
            if not progress.get("queued"):
                await asyncio.sleep(0.25)
                progress = await get_progress()
                if progress.get("active"):
                    await asyncio.to_thread(STORE.mark_cancel_race_running, account["id"], task_id)
                    raise HTTPException(status_code=409, detail="Job đã bắt đầu chạy; không thể hủy bằng nút X")
                if not progress.get("queued"):
                    await asyncio.to_thread(STORE.restore_job_queue, account["id"], task_id)
                    raise HTTPException(status_code=409, detail="Forge chưa xác nhận job còn trong hàng đợi")

            await _worker_json(request, job["worker_id"], "/cancel", {
                "event_id": job["event_id"],
                "session_hash": job["session_hash"],
                "fn_index": job["fn_index"],
            })
        except HTTPException:
            current = await asyncio.to_thread(STORE.get_job, account["id"], task_id)
            if current and current["status"] == "cancelling":
                await asyncio.to_thread(STORE.restore_job_queue, account["id"], task_id)
            raise

        clear_samples = 0
        try:
            for attempt in range(5):
                if attempt:
                    await asyncio.sleep(0.25)
                progress = await get_progress()
                if progress.get("active"):
                    await asyncio.to_thread(STORE.mark_cancel_race_running, account["id"], task_id)
                    raise HTTPException(status_code=409, detail="Job đã bắt đầu chạy; vẫn đang theo dõi tiến trình")
                if progress.get("queued"):
                    clear_samples = 0
                else:
                    clear_samples += 1
                    if clear_samples >= 2:
                        break
        except HTTPException:
            current = await asyncio.to_thread(STORE.get_job, account["id"], task_id)
            if current and current["status"] == "cancelling":
                await asyncio.to_thread(STORE.restore_job_queue, account["id"], task_id)
            raise
        if clear_samples < 2:
            await asyncio.to_thread(STORE.restore_job_queue, account["id"], task_id)
            raise HTTPException(status_code=409, detail="Forge chưa xác nhận đã gỡ job khỏi hàng đợi")
        if not await asyncio.to_thread(STORE.finish_job_cancel, account["id"], task_id):
            raise HTTPException(status_code=409, detail="Trạng thái job vừa thay đổi; hãy tải lại lịch sử")
        await asyncio.to_thread(STORE.delete_finished_job, account["id"], task_id)
        return {"ok": True, "status": "cancelled", "deleted": True}

    # Older history rows did not persist Gradio event metadata. Only clear them
    # when Forge confirms there is no matching live task and its queue is empty.
    try:
        progress, queue = await asyncio.gather(
            _worker_json(request, job["worker_id"], "/internal/progress", {
                "id_task": task_id, "live_preview": False,
            }),
            _worker_json(request, job["worker_id"], "/queue/status"),
        )
        if progress.get("active") or progress.get("queued") or int(queue.get("queue_size", 0)) != 0:
            await asyncio.to_thread(STORE.restore_job_queue, account["id"], task_id)
            raise HTTPException(status_code=409, detail="Job cũ không có mã hủy; chưa thể xác nhận an toàn")
    except HTTPException:
        await asyncio.to_thread(STORE.restore_job_queue, account["id"], task_id)
        raise
    if not await asyncio.to_thread(STORE.cancel_orphaned_job, account["id"], task_id):
        raise HTTPException(status_code=409, detail="Không thể xóa mục chờ cũ")
    await asyncio.to_thread(STORE.delete_finished_job, account["id"], task_id)
    return {"ok": True, "status": "cancelled", "orphaned": True, "deleted": True}


def _valid_image_path(path: str) -> bool:
    return (
        isinstance(path, str) and path.startswith(OUTPUT_ROOT)
        and ".." not in PurePosixPath(path).parts
        and PurePosixPath(path).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
    )


@app.post("/hub/internal/worker-events")
async def worker_event(body: WorkerEventBody, request: Request):
    expected = SETTINGS.worker_keys.get(body.worker_id, "")
    supplied = request.headers.get("x-forge-worker-key", "")
    if not expected or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="Worker key không hợp lệ")
    if body.image_path and not _valid_image_path(body.image_path):
        raise HTTPException(status_code=400, detail="Đường dẫn ảnh không hợp lệ")
    thumbnail = None
    if body.thumbnail_b64:
        if len(body.thumbnail_b64) > 200_000:
            raise HTTPException(status_code=400, detail="Thumbnail quá lớn")
        try:
            thumbnail = base64.b64decode(body.thumbnail_b64, validate=True)
        except binascii.Error as exc:
            raise HTTPException(status_code=400, detail="Thumbnail không hợp lệ") from exc
    updated = await asyncio.to_thread(
        STORE.update_job_event, body.worker_id, body.task_id, body.event, body.image_path, thumbnail,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Không tìm thấy job phù hợp")
    return {"ok": True}


@app.get("/hub/change-password")
async def change_password_page(request: Request):
    account = await _account(request)
    if not account:
        return RedirectResponse("/hub/login", status_code=303)
    if not account["must_change_password"]:
        return RedirectResponse("/", status_code=303)
    return FileResponse(ASSET_DIR / "change-password.html", media_type="text/html; charset=utf-8")


@app.post("/hub/api/change-password")
async def change_own_password(body: PasswordBody, request: Request):
    account = await _required_account(request)
    _check_origin(request)
    await asyncio.to_thread(STORE.reset_password, account["id"], body.password)
    response = JSONResponse({"ok": True, "next": "/hub/login"})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/hub/api/workers")
async def workers(request: Request):
    await _required_admin(request)

    async def probe(worker_id: str, origin: str) -> dict:
        try:
            response = await request.app.state.http.get(origin + "/", timeout=4)
            return {"id": worker_id, "online": response.status_code < 500}
        except httpx.HTTPError:
            return {"id": worker_id, "online": False}

    return await asyncio.gather(*(probe(k, v) for k, v in SETTINGS.worker_urls.items()))


@app.get("/hub/admin")
async def admin_page(request: Request):
    account = await _account(request)
    if not account:
        return RedirectResponse("/hub/login", status_code=303)
    if account["must_change_password"]:
        return RedirectResponse("/hub/change-password", status_code=303)
    if account["role"] != "admin":
        return RedirectResponse("/", status_code=303)
    return FileResponse(ASSET_DIR / "admin.html", media_type="text/html; charset=utf-8")


@app.get("/hub/api/accounts")
async def list_accounts(request: Request):
    await _required_admin(request)
    return await asyncio.to_thread(STORE.list_accounts)


@app.post("/hub/api/accounts")
async def create_account(body: NewAccountBody, request: Request):
    await _required_admin(request)
    _check_origin(request)
    try:
        account = await asyncio.to_thread(STORE.create_account, body.username, body.password, "user", body.worker_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(account, status_code=201)


@app.patch("/hub/api/accounts/{account_id}")
async def update_account(account_id: int, body: UpdateAccountBody, request: Request):
    admin = await _required_admin(request)
    _check_origin(request)
    if body.worker_id is None and body.is_active is None:
        raise HTTPException(status_code=400, detail="Không có thay đổi")
    if admin["id"] == account_id and body.is_active is False:
        raise HTTPException(status_code=400, detail="Không thể tự khóa tài khoản admin")
    try:
        return await asyncio.to_thread(
            STORE.update_account, account_id, worker_id=body.worker_id, is_active=body.is_active,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/hub/api/accounts/{account_id}/password")
async def reset_password(account_id: int, body: PasswordBody, request: Request):
    await _required_admin(request)
    _check_origin(request)
    try:
        await asyncio.to_thread(STORE.reset_password, account_id, body.password)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản") from exc
    return {"ok": True}


def _forward_cookies(raw_cookie: str) -> str:
    kept = []
    for item in raw_cookie.split(";"):
        if item.strip().partition("=")[0] != COOKIE_NAME:
            kept.append(item.strip())
    return "; ".join(item for item in kept if item)


def _upstream_url(account: dict, path: str, query: str = "") -> str:
    base = SETTINGS.worker_urls[account["worker_id"]]
    target = f"{base}/{path}"
    return f"{target}?{query}" if query else target


def _proxy_headers(request: Request) -> dict[str, str]:
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    cookies = _forward_cookies(request.headers.get("cookie", ""))
    if cookies:
        headers["cookie"] = cookies
    else:
        headers.pop("cookie", None)
    headers["x-forwarded-host"] = request.headers.get("x-forwarded-host", request.headers.get("host", ""))
    headers["x-forwarded-proto"] = request.headers.get("x-forwarded-proto", request.url.scheme)
    return headers


async def _generation_inputs(request: Request, account: dict, force: bool = False) -> dict[int, tuple[str, int]]:
    worker_id = account["worker_id"]
    if not force and worker_id in request.app.state.generate_inputs:
        return request.app.state.generate_inputs[worker_id]
    try:
        response = await request.app.state.http.get(SETTINGS.worker_urls[worker_id] + "/config", timeout=8)
        response.raise_for_status()
        config = response.json()
        component_ids = {
            item.get("props", {}).get("elem_id"): item["id"]
            for item in config.get("components", [])
        }
        mapping = {}
        for index, dependency in enumerate(config.get("dependencies", [])):
            name = str(dependency.get("api_name") or "")
            if name.startswith("txt2img_"):
                kind = "txt2img"
            elif name.startswith("img2img_"):
                kind = "img2img"
            else:
                continue
            prompt_component = component_ids.get(kind + "_prompt")
            inputs = dependency.get("inputs") or []
            if prompt_component in inputs:
                mapping[index] = (kind, inputs.index(prompt_component))
        request.app.state.generate_inputs[worker_id] = mapping
        return mapping
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return {}


async def _extract_generation(request: Request, account: dict, body: bytes) -> dict | None:
    try:
        if len(body) > MAX_GENERATION_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="Job quá lớn để lưu vào hàng đợi")
        payload = json.loads(body)
        fn_index = int(payload.get("fn_index"))
        session_hash = payload.get("session_hash")
        data = payload.get("data")
        if not isinstance(session_hash, str) or not session_hash or len(session_hash) > 128:
            return None
        if not isinstance(data, list) or not data:
            return None
        task_id = data[0]
        if not isinstance(task_id, str) or not task_id.startswith("task(") or len(task_id) > 100:
            return None
        mapping = await _generation_inputs(request, account)
        if fn_index not in mapping:
            mapping = await _generation_inputs(request, account, force=True)
        if fn_index not in mapping:
            return None
        kind, prompt_index = mapping[fn_index]
        prompt = data[prompt_index] if prompt_index < len(data) else ""
        return {
            "task_id": task_id,
            "kind": kind,
            "prompt": prompt if isinstance(prompt, str) else "",
            "session_hash": session_hash,
            "fn_index": fn_index,
            "request_json": body.decode("utf-8"),
        }
    except (ValueError, TypeError, IndexError):
        return None


async def _cancel_gradio_event(app: FastAPI, worker_id: str, generation: dict, event_id: str) -> None:
    with suppress(httpx.HTTPError):
        await app.state.http.post(
            SETTINGS.worker_urls[worker_id] + "/cancel",
            json={
                "event_id": event_id,
                "session_hash": generation["session_hash"],
                "fn_index": generation["fn_index"],
            },
            timeout=WORKER_CONTROL_TIMEOUT,
        )


async def _proxy_generation_join(account: dict, request: Request, generation: dict) -> Response:
    worker_id = account["worker_id"]
    try:
        saved_payload = json.loads(generation["request_json"])
    except (KeyError, TypeError, ValueError):
        saved_payload = {}
    await _validate_lora_access(request, account, saved_payload.get("data", []))
    inserted = await asyncio.to_thread(
        STORE.enqueue_job,
        generation["task_id"], account["id"], worker_id, generation["kind"],
        generation["prompt"], generation["session_hash"], generation["fn_index"],
        generation["request_json"],
    )
    if not inserted:
        raise HTTPException(status_code=409, detail="Job đã tồn tại hoặc không thể lưu vào hàng đợi")

    url = SETTINGS.worker_urls[worker_id] + "/queue/join"
    try:
        upstream_request = request.app.state.http.build_request(
            "POST", url, headers=_proxy_headers(request), content=generation["request_json"],
            timeout=WORKER_CONTROL_TIMEOUT,
        )
        upstream = await request.app.state.http.send(upstream_request, stream=True)
        response_body = await upstream.aread()
    except httpx.HTTPError as exc:
        await asyncio.to_thread(STORE.update_job_event, worker_id, generation["task_id"], "failed")
        raise HTTPException(status_code=502, detail=f"{worker_id} chưa kết nối") from exc

    response_headers = {
        key: value for key, value in upstream.headers.items()
        if key.lower() not in HOP_HEADERS and key.lower() != "set-cookie"
    }
    status_code = upstream.status_code
    cookies = upstream.headers.get_list("set-cookie")
    content_type = upstream.headers.get("content-type")
    await upstream.aclose()

    if status_code >= 400:
        await asyncio.to_thread(STORE.update_job_event, worker_id, generation["task_id"], "failed")
        response = Response(response_body, status_code=status_code, headers=response_headers, media_type=content_type)
        for value in cookies:
            response.headers.append("set-cookie", value)
        return response

    try:
        join_data = json.loads(response_body)
        event_id = join_data.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("Forge did not return an event id")
    except (ValueError, TypeError, AttributeError) as exc:
        await asyncio.to_thread(STORE.update_job_event, worker_id, generation["task_id"], "failed")
        raise HTTPException(status_code=502, detail="Forge không trả mã event cho job") from exc

    attached = await asyncio.to_thread(
        STORE.attach_job_event, worker_id, generation["task_id"], generation["session_hash"], event_id,
    )
    if not attached:
        await _cancel_gradio_event(request.app, worker_id, generation, event_id)
        await asyncio.to_thread(STORE.update_job_event, worker_id, generation["task_id"], "failed")
        raise HTTPException(status_code=500, detail="Không lưu được mã event của job")

    await request.app.state.queue_relay.start(worker_id, account["id"], generation["session_hash"])

    response = Response(response_body, status_code=status_code, headers=response_headers, media_type=content_type)
    for value in cookies:
        response.headers.append("set-cookie", value)
    return response


async def _proxy_http(path: str, request: Request):
    account = await _account(request)
    if not account:
        if request.method in ("GET", "HEAD"):
            return RedirectResponse("/hub/login", status_code=303)
        raise HTTPException(status_code=401, detail="Cần đăng nhập")
    if account["must_change_password"]:
        if request.method in ("GET", "HEAD"):
            return RedirectResponse("/hub/change-password", status_code=303)
        raise HTTPException(status_code=403, detail="Cần đổi mật khẩu tạm")
    if path in ("internal/lora/upload", "internal/lora/account-manifest") or path.startswith("internal/lora/uploads/"):
        raise HTTPException(status_code=404, detail="Không tìm thấy endpoint")
    generation = None
    body = None
    if path == "queue/join" and request.method == "POST":
        body = await request.body()
        generation = await _extract_generation(request, account, body)
        if generation:
            return await _proxy_generation_join(account, request, generation)
    if path in ("sdapi/v1/txt2img", "sdapi/v1/img2img") and request.method == "POST":
        body = await request.body()
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            payload = {}
        await _validate_lora_access(request, account, payload)
    url = _upstream_url(account, path, request.url.query)
    headers = _proxy_headers(request)
    content = body if body is not None else request.stream()
    try:
        upstream_request = request.app.state.http.build_request(
            request.method, url, headers=headers, content=content,
        )
        upstream = await request.app.state.http.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"{account['worker_id']} chưa kết nối") from exc

    response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_HEADERS and k.lower() != "set-cookie"}
    location = response_headers.get("location")
    if location and location.startswith(SETTINGS.worker_urls[account["worker_id"]]):
        response_headers["location"] = location[len(SETTINGS.worker_urls[account["worker_id"]]):] or "/"
    if path == "" and request.method == "GET" and upstream.status_code == 200 and "text/html" in upstream.headers.get("content-type", ""):
        async def injected_html():
            marker = b"</head>"
            tail = b""
            injected = False
            async for chunk in upstream.aiter_bytes():
                if injected:
                    yield chunk
                    continue
                data = tail + chunk
                lowered = data.lower()
                # Infinite Image Browsing is a secondary tab. Its legacy
                # classic script currently blocks Gradio hydration while it
                # probes its own settings/path endpoints. Defer only this
                # known extension script so Txt2img can paint first; the
                # script still runs before DOMContentLoaded and keeps its tab.
                data = data.replace(
                    NON_BLOCKING_FORGE_SCRIPT,
                    b' defer' + NON_BLOCKING_FORGE_SCRIPT,
                    1,
                )
                lowered = data.lower()
                index = lowered.find(marker)
                if index >= 0:
                    yield data[:index] + FORGE_HEAD_INJECTION + data[index:]
                    injected = True
                    tail = b""
                    continue
                keep = len(marker) - 1
                if len(data) > keep:
                    yield data[:-keep]
                    tail = data[-keep:]
                else:
                    tail = data
            if tail:
                yield tail

        for key in ("content-encoding", "etag", "last-modified"):
            response_headers.pop(key, None)
        response = StreamingResponse(
            injected_html(), status_code=200,
            headers=response_headers, media_type="text/html; charset=utf-8",
            background=BackgroundTask(upstream.aclose),
        )
    else:
        response = StreamingResponse(
            upstream.aiter_raw(), status_code=upstream.status_code,
            headers=response_headers, background=BackgroundTask(upstream.aclose),
        )
    for value in upstream.headers.get_list("set-cookie"):
        response.headers.append("set-cookie", value)
    return response


@app.api_route("/", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def forge_root(request: Request):
    return await _proxy_http("", request)


@app.get("/queue/data")
async def forge_queue_data(request: Request):
    account = await _account(request)
    if not account:
        return RedirectResponse("/hub/login", status_code=303)
    if account["must_change_password"]:
        return RedirectResponse("/hub/change-password", status_code=303)
    session_hash = request.query_params.get("session_hash", "")
    if not session_hash or len(session_hash) > 128:
        return await _proxy_http("queue/data", request)

    relay: QueueRelay = request.app.state.queue_relay
    channel = relay.get(account["worker_id"], account["id"], session_hash)
    if channel and channel.error:
        channel = None
    if channel is None:
        active_session = await asyncio.to_thread(STORE.active_queue_session, account["id"], session_hash)
        if active_session:
            try:
                channel = await relay.ensure(account["worker_id"], account["id"], session_hash)
            except (RuntimeError, TimeoutError) as exc:
                raise HTTPException(status_code=502, detail="Không khôi phục được stream queue Forge") from exc
    if channel is None:
        return await _proxy_http("queue/data", request)
    return StreamingResponse(
        relay.stream(channel, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def forge_proxy(path: str, request: Request):
    return await _proxy_http(path, request)


@app.websocket("/{path:path}")
async def forge_websocket(path: str, websocket: WebSocket):
    account = await asyncio.to_thread(STORE.resolve_session, websocket.cookies.get(COOKIE_NAME))
    if not account or account["must_change_password"]:
        await websocket.close(code=1008)
        return
    http_url = _upstream_url(account, path, websocket.url.query)
    ws_url = http_url.replace("http://", "ws://", 1)
    offered = websocket.scope.get("subprotocols", [])
    headers = {
        "X-Forwarded-Host": websocket.headers.get("x-forwarded-host", websocket.headers.get("host", "")),
        "X-Forwarded-Proto": websocket.headers.get("x-forwarded-proto", "https" if websocket.url.scheme == "wss" else "http"),
    }
    cookies = _forward_cookies(websocket.headers.get("cookie", ""))
    if cookies:
        headers["Cookie"] = cookies
    try:
        async with ws_connect(
            ws_url, additional_headers=headers, subprotocols=offered or None,
            open_timeout=8, max_size=None,
        ) as upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)

            async def client_to_forge():
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    if message.get("text") is not None:
                        await upstream.send(message["text"])
                    elif message.get("bytes") is not None:
                        await upstream.send(message["bytes"])

            async def forge_to_client():
                async for message in upstream:
                    if isinstance(message, str):
                        await websocket.send_text(message)
                    else:
                        await websocket.send_bytes(message)

            tasks = [asyncio.create_task(client_to_forge()), asyncio.create_task(forge_to_client())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                with suppress(ConnectionClosed, WebSocketDisconnect, asyncio.CancelledError):
                    task.result()
    except (OSError, TimeoutError, ConnectionClosed):
        with suppress(RuntimeError):
            await websocket.close(code=1013)
