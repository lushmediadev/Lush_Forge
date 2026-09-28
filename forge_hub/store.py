"""SQLite accounts and revocable server-side sessions."""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt

from .settings import WORKER_IDS


USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")
# Internal team deployment: keep the browser session across normal revisits.
# Admin account deactivation/worker reassignment still revokes sessions.
SESSION_DAYS = 365
COOKIE_NAME = "forge_hub_session"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _password_hash(password: str, *, temporary: bool = False) -> str:
    if len(password) < 1 and not temporary:
        raise ValueError("Mật khẩu không được để trống")
    if not password:
        raise ValueError("Mật khẩu không được để trống")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


class Store:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def _db(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
                    worker_id TEXT NOT NULL CHECK(worker_id IN ('forge1', 'forge2')),
                    is_active INTEGER NOT NULL DEFAULT 1,
                    must_change_password INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_account_idx ON sessions(account_id);
                CREATE TABLE IF NOT EXISTS jobs (
                    task_id TEXT PRIMARY KEY,
                    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    worker_id TEXT NOT NULL CHECK(worker_id IN ('forge1', 'forge2')),
                    kind TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    session_hash TEXT,
                    event_id TEXT,
                    fn_index INTEGER,
                    request_json TEXT,
                    error_message TEXT,
                    image_path TEXT,
                    thumbnail BLOB,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_account_created_idx ON jobs(account_id, created_at DESC);
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
            if "must_change_password" not in columns:
                conn.execute("ALTER TABLE accounts ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
            job_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            job_migrations = {
                "session_hash": "TEXT",
                "event_id": "TEXT",
                "fn_index": "INTEGER",
                "request_json": "TEXT",
                "error_message": "TEXT",
            }
            for name, definition in job_migrations.items():
                if name not in job_columns:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            conn.execute("CREATE INDEX IF NOT EXISTS jobs_worker_queue_idx ON jobs(worker_id, status, created_at)")

    @staticmethod
    def _account(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "worker_id": row["worker_id"],
            "is_active": bool(row["is_active"]),
            "must_change_password": bool(row["must_change_password"]),
            "created_at": row["created_at"],
        }

    def create_account(self, username: str, password: str, role: str, worker_id: str, *, temporary: bool = False) -> dict:
        username = username.strip()
        if not USERNAME_RE.fullmatch(username):
            raise ValueError("Tên tài khoản cần 3–32 ký tự: chữ, số, dấu chấm, gạch dưới hoặc gạch ngang")
        if role not in ("admin", "user") or worker_id not in WORKER_IDS:
            raise ValueError("Role hoặc máy Forge không hợp lệ")
        if temporary and role != "admin":
            raise ValueError("Chỉ tài khoản admin đầu tiên được dùng mật khẩu tạm")
        password_hash = _password_hash(password, temporary=temporary)
        with self._db() as conn:
            if temporary and conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] > 0:
                raise ValueError("Mật khẩu tạm chỉ được dùng khi khởi tạo app")
            try:
                cur = conn.execute(
                    "INSERT INTO accounts (username, password_hash, role, worker_id, must_change_password, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (username, password_hash, role, worker_id, int(temporary), _now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Tên tài khoản đã tồn tại") from exc
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (cur.lastrowid,)).fetchone()
        return self._account(row)

    def authenticate(self, username: str, password: str) -> dict | None:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE username = ?", (username.strip(),)).fetchone()
        if row is None or not row["is_active"]:
            return None
        if not bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("ascii")):
            return None
        return self._account(row)

    def create_session(self, account_id: int) -> str:
        token = secrets.token_urlsafe(32)
        with self._db() as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, account_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (_token_hash(token), account_id, (_now() + timedelta(days=SESSION_DAYS)).isoformat(), _now().isoformat()),
            )
        return token

    def resolve_session(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self._db() as conn:
            row = conn.execute(
                """
                SELECT a.* FROM sessions s JOIN accounts a ON a.id = s.account_id
                WHERE s.token_hash = ? AND s.expires_at > ? AND a.is_active = 1
                """,
                (_token_hash(token), _now().isoformat()),
            ).fetchone()
            if row:
                # Extend an active browser session so a frequently used
                # internal account does not unexpectedly return to login.
                conn.execute(
                    "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                    ((_now() + timedelta(days=SESSION_DAYS)).isoformat(), _token_hash(token)),
                )
        return self._account(row) if row else None

    def revoke_session(self, token: str | None) -> None:
        if not token:
            return
        with self._db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    def list_accounts(self) -> list[dict]:
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY role, username COLLATE NOCASE").fetchall()
        return [self._account(row) for row in rows]

    def update_account(self, account_id: int, *, worker_id: str | None = None, is_active: bool | None = None) -> dict:
        if worker_id is not None and worker_id not in WORKER_IDS:
            raise ValueError("Máy Forge không hợp lệ")
        with self._db() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if row is None:
                raise KeyError(account_id)
            new_worker = worker_id if worker_id is not None else row["worker_id"]
            new_active = int(is_active) if is_active is not None else row["is_active"]
            conn.execute("UPDATE accounts SET worker_id = ?, is_active = ? WHERE id = ?", (new_worker, new_active, account_id))
            if worker_id is not None or is_active is not None:
                conn.execute("DELETE FROM sessions WHERE account_id = ?", (account_id,))
            updated = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return self._account(updated)

    def reset_password(self, account_id: int, password: str, *, temporary: bool = False) -> None:
        password_hash = _password_hash(password, temporary=temporary)
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE accounts SET password_hash = ?, must_change_password = ? WHERE id = ?",
                (password_hash, int(temporary), account_id),
            )
            if not cur.rowcount:
                raise KeyError(account_id)
            conn.execute("DELETE FROM sessions WHERE account_id = ?", (account_id,))

    def enqueue_job(
        self, task_id: str, account_id: int, worker_id: str, kind: str, prompt: str,
        session_hash: str, fn_index: int, request_json: str,
    ) -> bool:
        if not task_id.startswith("task(") or not task_id.endswith(")") or len(task_id) > 100:
            return False
        if worker_id not in WORKER_IDS or kind not in ("txt2img", "img2img"):
            return False
        if not session_hash or len(session_hash) > 128 or not request_json or len(request_json) > 20_000_000:
            return False
        with self._db() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO jobs
                   (task_id, account_id, worker_id, kind, prompt, session_hash, fn_index, request_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, account_id, worker_id, kind, prompt[:500], session_hash, fn_index, request_json, _now().isoformat()),
            )
        return cur.rowcount == 1

    def attach_job_event(self, worker_id: str, task_id: str, session_hash: str, event_id: str) -> bool:
        if worker_id not in WORKER_IDS or not session_hash or len(session_hash) > 128 or not event_id or len(event_id) > 128:
            return False
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE jobs SET session_hash = ?, event_id = ? WHERE task_id = ? AND worker_id = ?",
                (session_hash, event_id, task_id, worker_id),
            )
        return cur.rowcount == 1

    def active_queue_session(self, account_id: int, session_hash: str) -> dict | None:
        with self._db() as conn:
            row = conn.execute(
                """SELECT worker_id FROM jobs
                   WHERE account_id = ? AND session_hash = ? AND status IN ('queued', 'running', 'cancelling')
                   ORDER BY created_at DESC LIMIT 1""",
                (account_id, session_hash),
            ).fetchone()
        return dict(row) if row else None

    def list_recoverable_jobs(self) -> list[dict]:
        with self._db() as conn:
            rows = conn.execute(
                """SELECT task_id, account_id, worker_id, kind, prompt, status, session_hash,
                          event_id, fn_index, request_json, created_at
                   FROM jobs WHERE status IN ('queued', 'running') AND request_json IS NOT NULL
                   ORDER BY created_at"""
            ).fetchall()
        return [dict(row) for row in rows]

    def begin_job_cancel(self, account_id: int, task_id: str) -> dict | None:
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status = 'cancelling' WHERE account_id = ? AND task_id = ? AND status = 'queued'",
                (account_id, task_id),
            )
            if not cur.rowcount:
                return None
            row = conn.execute(
                "SELECT task_id, worker_id, session_hash, event_id, fn_index FROM jobs WHERE account_id = ? AND task_id = ?",
                (account_id, task_id),
            ).fetchone()
        return dict(row) if row else None

    def restore_job_queue(self, account_id: int, task_id: str) -> None:
        with self._db() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'queued' WHERE account_id = ? AND task_id = ? AND status = 'cancelling'",
                (account_id, task_id),
            )

    def mark_cancel_race_running(self, account_id: int, task_id: str) -> bool:
        with self._db() as conn:
            cur = conn.execute(
                """UPDATE jobs SET status = 'running', started_at = COALESCE(started_at, ?)
                   WHERE account_id = ? AND task_id = ? AND status = 'cancelling'""",
                (_now().isoformat(), account_id, task_id),
            )
        return cur.rowcount == 1

    def finish_job_cancel(self, account_id: int, task_id: str, error_message: str | None = None) -> bool:
        with self._db() as conn:
            cur = conn.execute(
                """UPDATE jobs SET status = 'cancelled', finished_at = ?, error_message = ?,
                          request_json = NULL
                   WHERE account_id = ? AND task_id = ? AND status = 'cancelling'""",
                (_now().isoformat(), error_message, account_id, task_id),
            )
        return cur.rowcount == 1

    def cancel_orphaned_job(self, account_id: int, task_id: str) -> bool:
        with self._db() as conn:
            cur = conn.execute(
                """UPDATE jobs SET status = 'cancelled', finished_at = ?,
                          error_message = 'Job cũ không còn trong hàng đợi Forge; đã xóa mục chờ.'
                   WHERE account_id = ? AND task_id = ? AND status = 'cancelling'
                     AND event_id IS NULL AND request_json IS NULL""",
                (_now().isoformat(), account_id, task_id),
            )
        return cur.rowcount == 1

    def update_job_session(self, worker_id: str, task_id: str, session_hash: str, request_json: str) -> bool:
        if worker_id not in WORKER_IDS or not session_hash or len(session_hash) > 128:
            return False
        with self._db() as conn:
            cur = conn.execute(
                """UPDATE jobs SET session_hash = ?, event_id = NULL, request_json = ?,
                          status = 'queued', started_at = NULL, finished_at = NULL, error_message = NULL
                   WHERE task_id = ? AND worker_id = ? AND status IN ('queued', 'running')""",
                (session_hash, request_json, task_id, worker_id),
            )
        return cur.rowcount == 1

    def update_job_event(
        self, worker_id: str, task_id: str, event: str,
        image_path: str | None = None, thumbnail: bytes | None = None,
    ) -> bool:
        if worker_id not in WORKER_IDS or event not in ("running", "thumbnail", "done", "failed"):
            return False
        with self._db() as conn:
            row = conn.execute(
                "SELECT status FROM jobs WHERE task_id = ? AND worker_id = ?", (task_id, worker_id),
            ).fetchone()
            if not row:
                return False
            if event == "thumbnail":
                if thumbnail is None or len(thumbnail) > 150_000:
                    return False
                if row["status"] in ("failed", "cancelled"):
                    return True
                conn.execute(
                    """UPDATE jobs SET image_path = COALESCE(image_path, ?),
                              thumbnail = COALESCE(thumbnail, ?),
                              status = CASE WHEN status = 'cancelling' THEN 'running' ELSE status END,
                              started_at = CASE WHEN status = 'cancelling' THEN COALESCE(started_at, ?) ELSE started_at END
                       WHERE task_id = ?""",
                    (image_path, thumbnail, _now().isoformat(), task_id),
                )
                return True
            if row["status"] in ("done", "failed", "cancelled"):
                return True
            if event == "running":
                conn.execute(
                    "UPDATE jobs SET status = 'running', started_at = COALESCE(started_at, ?) WHERE task_id = ?",
                    (_now().isoformat(), task_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status = ?, finished_at = ?, request_json = NULL WHERE task_id = ?",
                    (event, _now().isoformat(), task_id),
                )
        return True

    def list_jobs(
        self, account_id: int, limit: int = 100,
        created_from: str | None = None, created_before: str | None = None,
    ) -> list[dict]:
        filters = ["account_id = ?"]
        values: list[object] = [account_id]
        if created_from:
            filters.append("created_at >= ?")
            values.append(created_from)
        if created_before:
            filters.append("created_at < ?")
            values.append(created_before)
        values.append(min(max(limit, 1), 1000))
        with self._db() as conn:
            rows = conn.execute(
                """SELECT task_id, worker_id, kind, prompt, status, created_at,
                          started_at, finished_at,
                          error_message,
                          thumbnail IS NOT NULL AS has_thumbnail,
                          image_path IS NOT NULL AS has_image
                   FROM jobs WHERE """ + " AND ".join(filters) + " ORDER BY created_at DESC LIMIT ?",
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_job(self, account_id: int, task_id: str) -> dict | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE account_id = ? AND task_id = ?", (account_id, task_id),
            ).fetchone()
        return dict(row) if row else None

    def delete_finished_job(self, account_id: int, task_id: str) -> bool:
        with self._db() as conn:
            cur = conn.execute(
                "DELETE FROM jobs WHERE account_id = ? AND task_id = ? AND status IN ('done', 'failed', 'cancelled')",
                (account_id, task_id),
            )
        return cur.rowcount == 1
