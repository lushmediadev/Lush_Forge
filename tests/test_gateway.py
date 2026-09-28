"""Focused auth and account-routing regression checks."""

import os
from pathlib import Path
import sqlite3
import json
from tempfile import TemporaryDirectory
import unittest
import base64
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient


class GatewayFlowTest(unittest.TestCase):
    def test_thumbnail_callback_after_done_still_populates_history(self):
        with TemporaryDirectory() as temporary:
            from forge_hub.store import Store

            store = Store(Path(temporary) / "jobs.sqlite3")
            store.initialize()
            account = store.create_account("alice", "AlicePassword_2026", "user", "forge1")
            task_id = "task(LATE-THUMBNAIL)"
            store.enqueue_job(
                task_id, account["id"], "forge1", "txt2img", "dog",
                "session-late", 4, '{"data":[]} ',
            )
            store.update_job_event("forge1", task_id, "done")
            accepted = store.update_job_event(
                "forge1", task_id, "thumbnail", "/home/ubuntu/forge/outputs/dog.png", b"jpeg",
            )

            self.assertTrue(accepted)
            job = store.get_job(account["id"], task_id)
            self.assertEqual(job["status"], "done")
            self.assertEqual(job["thumbnail"], b"jpeg")
            self.assertEqual(job["image_path"], "/home/ubuntu/forge/outputs/dog.png")

    def test_store_migrates_existing_history_without_dropping_queued_rows(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite3"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE accounts (
                    id INTEGER PRIMARY KEY, username TEXT NOT NULL, password_hash TEXT NOT NULL,
                    role TEXT NOT NULL, worker_id TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE jobs (
                    task_id TEXT PRIMARY KEY, account_id INTEGER NOT NULL, worker_id TEXT NOT NULL,
                    kind TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                    image_path TEXT, thumbnail BLOB, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
                );
                INSERT INTO accounts VALUES (1, 'alice', 'hash', 'user', 'forge1', 1, '2026-09-24T00:00:00+00:00');
                INSERT INTO jobs (task_id, account_id, worker_id, kind, prompt, status, created_at)
                    VALUES ('task(LEGACY-1)', 1, 'forge1', 'txt2img', 'kept', 'queued', '2026-09-24T00:00:00+00:00');
                """
            )
            conn.commit()
            conn.close()

            from forge_hub.store import Store

            store = Store(path)
            store.initialize()
            job = store.get_job(1, "task(LEGACY-1)")
            self.assertEqual(job["status"], "queued")
            self.assertIsNone(job["event_id"])
            self.assertIsNone(job["request_json"])

    def test_account_date_filter_and_specific_queue_cancel(self):
        with TemporaryDirectory() as temporary:
            os.environ["FORGE_HUB_DB_PATH"] = str(Path(temporary) / "jobs.sqlite3")
            os.environ["FORGE_HUB_PUBLIC_ORIGIN"] = "http://testserver"
            os.environ["FORGE_HUB_SECURE_COOKIE"] = "0"
            from forge_hub import app as module

            module.SETTINGS = module.load_settings()
            module.STORE = module.Store(module.SETTINGS.database_path)
            seen = []
            cancelled_events = set()
            event_by_task = {"task(CANCEL-1)": "event-one", "task(RACE-1)": "event-race"}

            async def worker(request: httpx.Request):
                body = await request.aread()
                seen.append((request.url.path, body))
                if request.url.path == "/internal/progress":
                    task_id = json.loads(body).get("id_task")
                    event_id = event_by_task.get(task_id)
                    if event_id == "event-race" and event_id in cancelled_events:
                        return httpx.Response(200, json={"active": True, "queued": False})
                    return httpx.Response(200, json={
                        "active": False,
                        "queued": bool(event_id and event_id not in cancelled_events),
                    })
                if request.url.path == "/queue/status":
                    return httpx.Response(200, json={"queue_size": 0})
                if request.url.path == "/cancel":
                    cancelled_events.add(json.loads(body).get("event_id"))
                    return httpx.Response(200, json={"success": True})
                return httpx.Response(404)

            with TestClient(module.app, base_url="http://testserver") as client:
                account = module.STORE.create_account("alice", "AlicePassword_2026", "user", "forge1")
                module.STORE.enqueue_job(
                    "task(CANCEL-1)", account["id"], "forge1", "txt2img", "first",
                    "session-one", 9, '{"data":[]} ',
                )
                module.STORE.attach_job_event("forge1", "task(CANCEL-1)", "session-one", "event-one")
                module.STORE.enqueue_job(
                    "task(ORPHAN-1)", account["id"], "forge1", "txt2img", "legacy",
                    "session-old", 9, '{"data":[]} ',
                )
                with module.STORE._db() as conn:
                    conn.execute(
                        "UPDATE jobs SET session_hash = NULL, event_id = NULL, fn_index = NULL, request_json = NULL WHERE task_id = ?",
                        ("task(ORPHAN-1)",),
                    )
                client.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(worker))
                client.app.state.queue_relay.client = client.app.state.http
                client.post("/hub/api/login", json={"username": "alice", "password": "AlicePassword_2026"})

                now = datetime.now(timezone.utc)
                lower = (now - timedelta(days=1)).isoformat()
                upper = (now + timedelta(days=1)).isoformat()
                filtered = client.get("/hub/api/jobs", params={"created_from": lower, "created_before": upper})
                self.assertEqual(filtered.status_code, 200)
                self.assertEqual({job["task_id"] for job in filtered.json()}, {"task(CANCEL-1)", "task(ORPHAN-1)"})

                cancelled = client.post(
                    "/hub/api/jobs/task(CANCEL-1)/cancel", headers={"Origin": "http://testserver"},
                )
                self.assertEqual(cancelled.status_code, 200, cancelled.text)
                self.assertTrue(cancelled.json()["deleted"])
                self.assertIsNone(module.STORE.get_job(account["id"], "task(CANCEL-1)"))
                cancel_body = next(body for path, body in seen if path == "/cancel")
                self.assertIn(b'"event_id":"event-one"', cancel_body)
                self.assertIn(b'"session_hash":"session-one"', cancel_body)

                module.STORE.enqueue_job(
                    "task(RACE-1)", account["id"], "forge1", "txt2img", "race",
                    "session-race", 9, '{"data":[]} ',
                )
                module.STORE.attach_job_event("forge1", "task(RACE-1)", "session-race", "event-race")
                race_cancel = client.post(
                    "/hub/api/jobs/task(RACE-1)/cancel", headers={"Origin": "http://testserver"},
                )
                self.assertEqual(race_cancel.status_code, 409, race_cancel.text)
                self.assertEqual(module.STORE.get_job(account["id"], "task(RACE-1)")["status"], "running")

                orphan_cancel = client.post(
                    "/hub/api/jobs/task(ORPHAN-1)/cancel", headers={"Origin": "http://testserver"},
                )
                self.assertEqual(orphan_cancel.status_code, 200, orphan_cancel.text)
                self.assertTrue(orphan_cancel.json()["orphaned"])
                self.assertTrue(orphan_cancel.json()["deleted"])
                self.assertIsNone(module.STORE.get_job(account["id"], "task(ORPHAN-1)"))

    def test_native_generate_creates_account_history_and_worker_event_completes_it(self):
        with TemporaryDirectory() as temporary:
            os.environ["FORGE_HUB_DB_PATH"] = str(Path(temporary) / "jobs.sqlite3")
            os.environ["FORGE_HUB_PUBLIC_ORIGIN"] = "http://testserver"
            os.environ["FORGE_HUB_SECURE_COOKIE"] = "0"
            os.environ["FORGE_HUB_FORGE1_KEY"] = "test-worker-key"
            from forge_hub import app as module

            module.SETTINGS = module.load_settings()
            module.STORE = module.Store(module.SETTINGS.database_path)

            async def worker(request: httpx.Request):
                if request.url.path == "/config":
                    return httpx.Response(200, json={
                        "components": [
                            {"id": 47, "props": {"elem_id": None}},
                            {"id": 5, "props": {"elem_id": "txt2img_prompt"}},
                        ],
                        "dependencies": [{"api_name": "txt2img_1", "inputs": [47, 5]}],
                    })
                if request.url.path == "/queue/join":
                    return httpx.Response(200, json={"event_id": "event-test-123"})
                if request.url.path == "/queue/data":
                    return httpx.Response(200, stream=httpx.ByteStream(
                        b'data: {"msg":"process_completed","event_id":"event-test-123","success":true}\n\n'
                        b'data: {"msg":"close_stream"}\n\n'
                    ))
                return httpx.Response(200, stream=httpx.ByteStream(b"ok"))

            with TestClient(module.app, base_url="http://testserver") as client:
                module.STORE.create_account("alice", "AlicePassword_2026", "user", "forge1")
                module.STORE.create_account("bob", "BobbyPassword_2026", "user", "forge1")
                original = client.app.state.http
                client.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(worker))
                client.app.state.queue_relay.client = client.app.state.http
                client.post("/hub/api/login", json={"username": "alice", "password": "AlicePassword_2026"})
                task_id = "task(TEST-123)"
                submitted = client.post("/queue/join", json={
                    "fn_index": 0, "data": [task_id, "an apple"], "session_hash": "session-test-123",
                })
                self.assertEqual(submitted.status_code, 200)
                self.assertEqual(submitted.json(), {"event_id": "event-test-123"})
                streamed = client.get("/queue/data", params={"session_hash": "session-test-123"})
                self.assertEqual(streamed.status_code, 200)
                self.assertIn('"process_completed"', streamed.text)
                listed = client.get("/hub/api/jobs").json()
                self.assertEqual([(job["task_id"], job["prompt"]) for job in listed], [(task_id, "an apple")])
                saved_job = module.STORE.get_job(1, task_id)
                self.assertEqual(saved_job["event_id"], "event-test-123")
                self.assertEqual(saved_job["session_hash"], "session-test-123")
                self.assertIn('"an apple"', saved_job["request_json"])
                denied = client.post(
                    "/hub/internal/worker-events", headers={"X-Forge-Worker-Key": "wrong"},
                    json={"worker_id": "forge1", "task_id": task_id, "event": "done"},
                )
                self.assertEqual(denied.status_code, 403)
                event = client.post(
                    "/hub/internal/worker-events", headers={"X-Forge-Worker-Key": "test-worker-key"},
                    json={"worker_id": "forge1", "task_id": task_id, "event": "thumbnail",
                          "image_path": "/home/ubuntu/forge/outputs/sample.png",
                          "thumbnail_b64": base64.b64encode(b"fake-jpeg").decode()},
                )
                self.assertEqual(event.status_code, 200, event.text)
                self.assertEqual(client.get(f"/hub/api/jobs/{task_id}/thumbnail").content, b"fake-jpeg")
                completed = client.post(
                    "/hub/internal/worker-events", headers={"X-Forge-Worker-Key": "test-worker-key"},
                    json={"worker_id": "forge1", "task_id": task_id, "event": "done"},
                )
                self.assertEqual(completed.status_code, 200)
                self.assertEqual(client.get("/hub/api/jobs").json()[0]["status"], "done")

                client.post("/hub/api/logout", headers={"Origin": "http://testserver"})
                client.post("/hub/api/login", json={"username": "bob", "password": "BobbyPassword_2026"})
                self.assertEqual(client.get("/hub/api/jobs").json(), [])
                self.assertEqual(client.get(f"/hub/api/jobs/{task_id}/thumbnail").status_code, 404)
                client.app.state.http = original
                client.app.state.queue_relay.client = original

    def test_temporary_admin_password_cannot_open_forge_until_changed(self):
        with TemporaryDirectory() as temporary:
            os.environ["FORGE_HUB_DB_PATH"] = str(Path(temporary) / "temporary.sqlite3")
            os.environ["FORGE_HUB_PUBLIC_ORIGIN"] = "http://testserver"
            os.environ["FORGE_HUB_SECURE_COOKIE"] = "0"
            from forge_hub import app as module

            module.SETTINGS = module.load_settings()
            module.STORE = module.Store(module.SETTINGS.database_path)
            with TestClient(module.app, base_url="http://testserver") as client:
                module.STORE.create_account("admin", "1234", "admin", "forge1", temporary=True)
                logged_in = client.post("/hub/api/login", json={"username": "admin", "password": "1234"})
                self.assertEqual(logged_in.status_code, 200)
                self.assertEqual(logged_in.json()["next"], "/hub/change-password")
                self.assertEqual(client.get("/", follow_redirects=False).headers["location"], "/hub/change-password")
                self.assertEqual(client.get("/hub/admin", follow_redirects=False).headers["location"], "/hub/change-password")
                changed = client.post(
                    "/hub/api/change-password", headers={"Origin": "http://testserver"},
                    json={"password": "NewAdminPassword_2026"},
                )
                self.assertEqual(changed.status_code, 200)
                self.assertEqual(
                    client.post("/hub/api/login", json={"username": "admin", "password": "1234"}).status_code,
                    401,
                )
                self.assertEqual(
                    client.post("/hub/api/login", json={"username": "admin", "password": "NewAdminPassword_2026"}).status_code,
                    200,
                )

    def test_account_assignment_controls_private_proxy_and_revokes_old_session(self):
        with TemporaryDirectory() as temporary:
            os.environ["FORGE_HUB_DB_PATH"] = str(Path(temporary) / "test.sqlite3")
            os.environ["FORGE_HUB_PUBLIC_ORIGIN"] = "http://testserver"
            os.environ["FORGE_HUB_SECURE_COOKIE"] = "0"
            from forge_hub import app as module

            module.SETTINGS = module.load_settings()
            module.STORE = module.Store(module.SETTINGS.database_path)
            requests_seen = []

            async def worker(request: httpx.Request):
                requests_seen.append((str(request.url), request.headers.get("cookie", ""), await request.aread()))
                return httpx.Response(200, stream=httpx.ByteStream(b"forge-response"), headers={"content-type": "text/plain"})

            with TestClient(module.app, base_url="http://testserver") as admin_client:
                module.STORE.create_account("admin", "AdminPassword_2026", "admin", "forge1")
                original = admin_client.app.state.http
                admin_client.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(worker))
                self.assertEqual(admin_client.get("/", follow_redirects=False).status_code, 303)
                self.assertEqual(
                    admin_client.post("/hub/api/login", json={"username": "admin", "password": "AdminPassword_2026"}).status_code,
                    200,
                )
                created = admin_client.post(
                    "/hub/api/accounts", headers={"Origin": "http://testserver"},
                    json={"username": "team", "password": "TeamPassword_2026", "worker_id": "forge2"},
                )
                self.assertEqual(created.status_code, 201, created.text)
                team_id = created.json()["id"]

                team_client = TestClient(module.app, base_url="http://testserver")
                self.assertEqual(
                    team_client.post("/hub/api/login", json={"username": "team", "password": "TeamPassword_2026"}).status_code,
                    200,
                )
                proxied = team_client.post("/queue/join", content=b'{"job":"abc"}')
                self.assertEqual(proxied.status_code, 200, proxied.text)
                self.assertEqual(proxied.content, b"forge-response")
                self.assertIn("127.0.0.1:18387/queue/join", requests_seen[-1][0])
                self.assertNotIn("forge_hub_session", requests_seen[-1][1])
                self.assertEqual(requests_seen[-1][2], b'{"job":"abc"}')

                changed = admin_client.patch(
                    f"/hub/api/accounts/{team_id}", headers={"Origin": "http://testserver"},
                    json={"worker_id": "forge1"},
                )
                self.assertEqual(changed.status_code, 200, changed.text)
                self.assertEqual(team_client.post("/queue/join", content=b"after-change").status_code, 401)

                admin_client.app.state.http = original

    def test_lora_upload_uses_assigned_worker_and_worker_key(self):
        with TemporaryDirectory() as temporary:
            os.environ["FORGE_HUB_DB_PATH"] = str(Path(temporary) / "upload.sqlite3")
            os.environ["FORGE_HUB_PUBLIC_ORIGIN"] = "http://testserver"
            os.environ["FORGE_HUB_SECURE_COOKIE"] = "0"
            os.environ["FORGE_HUB_FORGE2_KEY"] = "forge2-upload-key"
            from forge_hub import app as module

            module.SETTINGS = module.load_settings()
            module.STORE = module.Store(module.SETTINGS.database_path)
            seen = []

            async def worker(request: httpx.Request):
                body = await request.aread()
                seen.append((request.url.path, request.headers.get("x-forge-worker-key"), body))
                if request.url.path == "/internal/lora/upload":
                    return httpx.Response(200, json={"ok": True, "filename": "style.safetensors", "size": 3})
                return httpx.Response(404)

            with TestClient(module.app, base_url="http://testserver") as client:
                module.STORE.create_account("alice", "AlicePassword_2026", "user", "forge2")
                original = client.app.state.http
                client.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(worker))
                client.post("/hub/api/login", json={"username": "alice", "password": "AlicePassword_2026"})

                uploaded = client.post(
                    "/hub/api/lora/upload",
                    headers={"Origin": "http://testserver"},
                    files={"file": ("style.safetensors", b"abc", "application/octet-stream")},
                )
                self.assertEqual(uploaded.status_code, 200, uploaded.text)
                self.assertEqual(uploaded.json()["filename"], "style.safetensors")
                self.assertEqual(len(seen), 1)
                self.assertEqual(seen[0][0], "/internal/lora/upload")
                self.assertEqual(seen[0][1], "forge2-upload-key")
                self.assertIn(b"style.safetensors", seen[0][2])
                self.assertIn(b"abc", seen[0][2])
                client.app.state.http = original


if __name__ == "__main__":
    unittest.main()
