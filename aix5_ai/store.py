"""Single-process SQLite storage with serialized access from background workers."""

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import tempfile
import threading
import time
import uuid
from functools import wraps
from pathlib import Path

from argon2 import PasswordHasher
from cryptography.fernet import Fernet, InvalidToken

from .config import DB_PATH, DEFAULT_SETTINGS
from .schemas import CameraConfig

log = logging.getLogger("aix5.store")
CAMERA_FIELDS = set(CameraConfig.model_fields) | {"id", "source_type", "created_at"}


def synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return call


class Store:
    def __init__(self, path=DB_PATH):
        self._lock = threading.RLock()
        self.root = Path(path).parent
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;
        PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,config TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rules(id TEXT PRIMARY KEY,camera_id TEXT NOT NULL,
          config TEXT NOT NULL,FOREIGN KEY(camera_id) REFERENCES sources(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS detections(id TEXT PRIMARY KEY,camera_id TEXT NOT NULL,
          created REAL NOT NULL,payload TEXT NOT NULL,image_path TEXT NOT NULL,size INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS detection_time ON detections(created);
        CREATE INDEX IF NOT EXISTS detection_camera ON detections(camera_id,created);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS onvif_nonces(nonce_hash TEXT PRIMARY KEY,expires REAL NOT NULL);
        """)
        if not self.get("device_id"):
            self.set("device_id", uuid.uuid4().hex)
            self.set("settings", DEFAULT_SETTINGS)
            self.set("ha_token", secrets.token_urlsafe(32))
        settings = self.get("settings", {})
        if (
            "preview_resolution" not in settings
            or "allow_software_fallback" not in settings
            or "timezone" not in settings
        ):
            self.set("settings", {**DEFAULT_SETTINGS, **settings})
        # Preserve all existing credentials, including older installations.
        if not self.get("password_hash"):
            password = secrets.token_urlsafe(18)
            self.set("username", "admin")
            self.set("password_hash", PasswordHasher().hash(password))
            self.set_onvif_password(password)
            credentials = Path(path).parent / "initial-admin.txt"
            credentials.write_text(
                f"用户名：admin\n密码：{password}\n首次登录后请修改密码。\n"
            )
            credentials.chmod(0o600)
        self.set("credential_version", 3)
        Path(path).chmod(0o600)

    @synchronized
    def close(self):
        self.db.close()

    @synchronized
    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    @synchronized
    def set(self, key, value):
        with self.db:
            self.db.execute(
                "INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    @synchronized
    def cameras(self):
        cameras = [
            {k: v for k, v in json.loads(r["config"]).items() if k in CAMERA_FIELDS}
            for r in self.db.execute("SELECT config FROM sources ORDER BY rowid")
        ]
        order = {cid: index for index, cid in enumerate(self.get("camera_order", []))}
        # Unsorted/new cameras retain their insertion order at the end.
        return sorted(cameras, key=lambda camera: order.get(camera["id"], len(order)))

    @synchronized
    def reorder_cameras(self, camera_ids):
        existing = {row["id"] for row in self.db.execute("SELECT id FROM sources")}
        if len(camera_ids) != len(existing) or set(camera_ids) != existing:
            raise ValueError("设备列表已变化，请刷新后重新排序")
        self.set("camera_order", camera_ids)

    @synchronized
    def camera(self, cid):
        return next((c for c in self.cameras() if c["id"] == cid), None)

    @synchronized
    def save_camera(self, camera):
        config = {k: v for k, v in camera.items() if k in CAMERA_FIELDS}
        with self.db:
            self.db.execute(
                "INSERT INTO sources VALUES (?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config",
                (camera["id"], json.dumps(config, ensure_ascii=False)),
            )

    @synchronized
    def delete_camera(self, cid):
        with self.db:
            self.db.execute("DELETE FROM sources WHERE id=?", (cid,))
            order = self.get("camera_order", [])
            if cid in order:
                self.db.execute(
                    "UPDATE kv SET value=? WHERE key='camera_order'",
                    (json.dumps([item for item in order if item != cid]),),
                )

    @synchronized
    def rules(self, cid=None, *, newest_first=False):
        rows = self.db.execute(
            "SELECT config FROM rules" + (" WHERE camera_id=?" if cid else "")
            + (" ORDER BY rowid DESC" if newest_first else " ORDER BY rowid"),
            (cid,) if cid else (),
        )
        return [json.loads(r["config"]) for r in rows]

    @synchronized
    def save_rule(self, rule):
        with self.db:
            self.db.execute(
                "INSERT INTO rules VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config",
                (rule["id"], rule["camera_id"], json.dumps(rule, ensure_ascii=False)),
            )

    @synchronized
    def delete_rule(self, rid):
        with self.db:
            self.db.execute("DELETE FROM rules WHERE id=?", (rid,))

    @synchronized
    def create_session(self):
        token = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            self.db.execute(
                "INSERT INTO sessions VALUES (?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), time.time() + 43200),
            )
        return token

    @synchronized
    def valid_session(self, token):
        return bool(
            token
            and self.db.execute(
                "SELECT 1 FROM sessions WHERE token_hash=? AND expires>?",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
        )

    @synchronized
    def logout(self, token):
        with self.db:
            self.db.execute(
                "DELETE FROM sessions WHERE token_hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )

    @synchronized
    def change_password(self, password_hash, password=None):
        self.change_account(self.get("username"), password_hash, password)

    @synchronized
    def change_account(self, username, password_hash=None, password=None):
        # Prepare encryption before updating the account so a filesystem error
        # cannot leave its Argon2 hash and ONVIF secret on different passwords.
        encrypted = (
            self._encrypt_onvif_password(
                username, password_hash or self.get("password_hash"), password
            )
            if password is not None
            else None
        )
        with self.db:
            self.db.execute(
                "UPDATE kv SET value=? WHERE key='username'", (json.dumps(username),)
            )
            if password_hash is not None:
                self.db.execute(
                    "UPDATE kv SET value=? WHERE key='password_hash'",
                    (json.dumps(password_hash),),
                )
            self.db.execute("DELETE FROM sessions")
            self.db.execute("DELETE FROM kv WHERE key='onvif_credential'")
            if encrypted is not None:
                self.db.execute(
                    "INSERT INTO kv VALUES ('onvif_credential', ?)",
                    (json.dumps(encrypted),),
                )
        try:
            (self.root / "initial-admin.txt").unlink(missing_ok=True)
        except OSError:
            log.exception("初始账号文件清理失败")

    def _onvif_cipher(self, *, create=False):
        """Keep the encryption key outside SQLite and restrict it to its owner."""
        path = self.root / ".onvif-secret.key"
        try:
            path.chmod(0o600)
            return Fernet(path.read_bytes())
        except (FileNotFoundError, ValueError):
            if not create:
                raise
        # Only a verified password write may recover a missing/malformed key.
        # Publish a complete file atomically: power loss during the write must
        # not leave an empty key that prevents subsequent valid web logins.
        key = Fernet.generate_key()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.root, prefix=".onvif-key-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return Fernet(key)

    def _encrypt_onvif_password(self, username, password_hash, password):
        payload = json.dumps(
            {
                "username": username,
                "password_hash": password_hash,
                "password": password,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        return self._onvif_cipher(create=True).encrypt(payload).decode("ascii")

    @synchronized
    def set_onvif_password(self, password):
        """Persist a password already verified against the current account."""
        if self.onvif_password() == password:
            return
        self.set(
            "onvif_credential",
            self._encrypt_onvif_password(
                self.get("username"), self.get("password_hash"), password
            ),
        )

    @synchronized
    def onvif_password(self):
        encrypted = self.get("onvif_credential")
        if not encrypted:
            return None
        try:
            payload = json.loads(
                self._onvif_cipher().decrypt(encrypted.encode("ascii"))
            )
        except (OSError, InvalidToken, ValueError, TypeError):
            # Fail closed if a backup was restored without its local key.
            return None
        if payload.get("username") != self.get("username") or payload.get(
            "password_hash"
        ) != self.get("password_hash"):
            return None
        return payload.get("password")

    @synchronized
    def consume_onvif_nonce(self, nonce_hash, expires, now):
        with self.db:
            self.db.execute("DELETE FROM onvif_nonces WHERE expires<?", (now,))
            try:
                self.db.execute(
                    "INSERT INTO onvif_nonces VALUES (?,?)", (nonce_hash, expires)
                )
            except sqlite3.IntegrityError:
                return False
        return True

    @synchronized
    def insert_event(self, event, image=None):
        path = Path(event["image_path"])
        temp_path = None
        published = False
        if image is not None and path.exists():
            raise FileExistsError(path)
        try:
            if image is not None:
                with tempfile.NamedTemporaryFile(
                    dir=path.parent, prefix=".event-", delete=False
                ) as f:
                    temp_path = Path(f.name)
                    f.write(image)
                    f.flush()
                    os.fsync(f.fileno())
                temp_path.replace(path)
                published = True
                event["size"] = len(image)
            with self.db:
                self.db.execute(
                    "INSERT INTO detections VALUES (?,?,?,?,?,?)",
                    (
                        event["id"],
                        event["camera_id"],
                        event["created"],
                        json.dumps(event, ensure_ascii=False),
                        str(path),
                        event["size"],
                    ),
                )
        except BaseException:
            if published:
                path.unlink(missing_ok=True)
            raise
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)

    @synchronized
    def events(self, cid=None, limit=100, event_type=None, target_type=None):
        clauses, args = [], []
        for column, value in [
            ("camera_id", cid),
            ("json_extract(payload,'$.event_type')", event_type),
            ("json_extract(payload,'$.target_type')", target_type),
        ]:
            if value:
                clauses.append(column + "=?")
                args.append(value)
        sql = "SELECT payload FROM detections"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created DESC LIMIT ?"
        args.append(int(limit))
        return [json.loads(row["payload"]) for row in self.db.execute(sql, args)]

    @synchronized
    def event(self, eid):
        row = self.db.execute(
            "SELECT payload FROM detections WHERE id=?", (eid,)
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    @synchronized
    def purge(self, days, max_bytes):
        cutoff = time.time() - days * 86400
        with self.db:
            rows = self.db.execute(
                "SELECT id,image_path,size FROM detections WHERE created<? ORDER BY created",
                (cutoff,),
            ).fetchall()
            for row in rows:
                self._delete_event(row)
            total = self.db.execute(
                "SELECT COALESCE(SUM(size),0) AS n FROM detections"
            ).fetchone()["n"]
            if total > max_bytes:
                rows = self.db.execute(
                    "SELECT id,image_path,size FROM detections ORDER BY created"
                ).fetchall()
                for row in rows:
                    if total <= int(max_bytes * 0.9):
                        break
                    if self._delete_event(row):
                        total -= row["size"]

    def _delete_event(self, row):
        try:
            Path(row["image_path"]).unlink(missing_ok=True)
        except OSError:
            log.exception("事件截图清理失败 id=%s", row["id"])
            return False
        self.db.execute("DELETE FROM detections WHERE id=?", (row["id"],))
        return True
