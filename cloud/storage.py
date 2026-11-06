"""Dropbox optimistic writes and durable Postgres state. No local vault required."""
import contextlib
import json
import os
from pathlib import PurePosixPath
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid

from bridge import BridgeError


MAX_NOTE_BYTES = 1_000_000


class Dropbox:
    def __init__(self, root, app_key, app_secret, refresh_token, account_id, namespace=None):
        self.root = root.rstrip("/")
        if not self.root.startswith("/") or self.root == "" or ".." in PurePosixPath(self.root).parts:
            raise BridgeError("Dropbox vault must be an absolute, non-root folder")
        self.key, self.secret, self.refresh = app_key, app_secret, refresh_token
        self.expected_account = account_id
        self.namespace = namespace
        self.token, self.expires = None, 0

    def in_scope(self, path):
        return path.lower().startswith(self.root.lower() + "/") and not any(
            part.startswith(".") or part == ".." for part in PurePosixPath(path[len(self.root) + 1:]).parts)

    def authenticate(self):
        if self.token and time.time() < self.expires:
            return
        data = urlencode({"grant_type": "refresh_token", "refresh_token": self.refresh,
                          "client_id": self.key, "client_secret": self.secret}).encode()
        try:
            with urlopen(Request("https://api.dropboxapi.com/oauth2/token", data=data), timeout=15) as response:
                value = json.load(response)
        except (HTTPError, URLError, TimeoutError):
            raise BridgeError("Dropbox token refresh failed; check the app credentials") from None
        self.token = value["access_token"]
        self.expires = time.time() + value.get("expires_in", 14400) - 60

    def request(self, route, data, *, content=False, upload=None):
        self.authenticate()
        headers = {"Authorization": "Bearer " + self.token}
        if self.namespace and route.startswith("files/"):
            headers["Dropbox-API-Path-Root"] = json.dumps({".tag": "namespace_id", "namespace_id": self.namespace})
        if content:
            url = "https://content.dropboxapi.com/2/" + route
            headers["Dropbox-API-Arg"] = json.dumps(data, ensure_ascii=True)
            body = upload if upload is not None else b""
            if upload is not None:
                headers["Content-Type"] = "application/octet-stream"
        else:
            url = "https://api.dropboxapi.com/2/" + route
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode()
        try:
            with urlopen(Request(url, data=body, headers=headers), timeout=20) as response:
                if content and upload is None:
                    raw = response.read(MAX_NOTE_BYTES + 1)
                    if len(raw) > MAX_NOTE_BYTES:
                        raise BridgeError("Note exceeds the 1 MB pilot limit")
                    return json.loads(response.headers["Dropbox-API-Result"]), raw.decode("utf-8")
                return json.load(response)
        except HTTPError as exc:
            raise BridgeError(f"Dropbox HTTP {exc.code}; revision conflict, permissions or throttling needs review") from None
        except (URLError, TimeoutError, UnicodeError, json.JSONDecodeError):
            raise BridgeError("Dropbox request failed; retry via a later sync pass") from None

    def verify_account(self):
        value = self.request("users/get_current_account", None)
        if value["account_id"] != self.expected_account:
            raise BridgeError("Dropbox account differs from the configured account")

    def list_notes(self):
        self.verify_account()
        result = []
        response = self.request("files/list_folder", {
            "path": self.root, "recursive": True, "include_deleted": False, "limit": 1000})
        while True:
            for entry in response["entries"]:
                path = entry.get("path_display", entry.get("path_lower", ""))
                if entry[".tag"] == "file" and self.in_scope(path) and path.lower().endswith(".md"):
                    if entry.get("size", 0) > MAX_NOTE_BYTES:
                        raise BridgeError("Vault includes a note exceeding the 1 MB pilot limit")
                    result.append(entry)
            if not response.get("has_more"):
                break
            response = self.request("files/list_folder/continue", {"cursor": response["cursor"]})
        if len(result) > 200:
            raise BridgeError("Vault exceeds the 200-note cloud pilot limit")
        return result

    def read(self, entry):
        meta, raw = self.request("files/download", {"path": entry["id"]}, content=True)
        if not self.in_scope(meta.get("path_display", "")):
            raise BridgeError("File moved outside configured vault")
        return meta, raw.replace("\r\n", "\n")

    def write(self, entry, raw):
        path = entry["path_display"]
        if not self.in_scope(path):
            raise BridgeError("Refusing write outside the vault")
        if len(raw.encode()) > MAX_NOTE_BYTES:
            raise BridgeError("Note exceeds the 1 MB pilot limit")
        # Identity follows file IDs across passes. A path-scoped write fails if the
        # note moves during this pass, including a move outside the vault.
        return self.request("files/upload", {
            "path": path, "mode": {".tag": "update", "update": entry["rev"]},
            "autorename": False, "strict_conflict": True, "mute": True},
            content=True, upload=raw.encode())

    def create(self, filename, raw):
        path = self.root + "/" + filename
        if not self.in_scope(path):
            raise BridgeError("Invalid download filename")
        if len(raw.encode()) > MAX_NOTE_BYTES:
            raise BridgeError("Note exceeds the 1 MB pilot limit")
        self.ensure_parent(path)
        return self.request("files/upload", {
            "path": path, "mode": "add", "autorename": False, "strict_conflict": True, "mute": True},
            content=True, upload=raw.encode())

    def ensure_parent(self, path):
        folders = getattr(self, "_folders", set())
        self._folders = folders
        parent = self.root
        for component in PurePosixPath(path[len(self.root) + 1:]).parts[:-1]:
            parent += "/" + component
            if parent in folders:
                continue
            try:
                self.request("files/create_folder_v2", {"path": parent, "autorename": False})
            except BridgeError:
                if self.request("files/get_metadata", {"path": parent}).get(".tag") != "folder":
                    raise
            folders.add(parent)

    def move_note(self, entry, destination):
        source = entry["path_display"]
        if not self.in_scope(source) or not self.in_scope(destination):
            raise BridgeError("Refusing to move a note outside the vault")
        self.ensure_parent(destination)
        result = self.request("files/move_v2", {
            "from_path": source, "to_path": destination, "autorename": False,
        })["metadata"]
        if result["id"] != entry["id"]:
            raise BridgeError("Note changed during the move; retry sync from its new path")
        return result

    def save_attachment(self, filename, data):
        folder = self.root + "/Notion attachments"
        path = folder + "/" + filename
        if not self.in_scope(path):
            raise BridgeError("Refusing attachment write outside the vault")
        if not getattr(self, "_attachment_folder_ready", False):
            try:
                self.request("files/create_folder_v2", {"path": folder, "autorename": False})
            except BridgeError:
                if self.request("files/get_metadata", {"path": folder}).get(".tag") != "folder":
                    raise
            self._attachment_folder_ready = True
        try:
            self.request("files/upload", {"path": path, "mode": "add", "autorename": False,
                         "strict_conflict": True, "mute": True}, content=True, upload=data)
        except BridgeError:
            # Recover an upload whose acknowledgement was lost without replacing a file.
            import hashlib
            blocks = (hashlib.sha256(data[i:i + 4 * 1024 * 1024]).digest()
                      for i in range(0, len(data), 4 * 1024 * 1024))
            expected = hashlib.sha256(b"".join(blocks)).hexdigest()
            if self.request("files/get_metadata", {"path": path}).get("content_hash") != expected:
                raise
        return "Notion attachments/" + filename


def empty_state():
    return {"notes": {}, "creates": {}, "imports": {}, "issues": {}}


class PostgresStore:
    """A fenced, expiring lease; every mutation is committed independently."""
    def __init__(self, dsn, key):
        import psycopg
        self.conn = psycopg.connect(dsn, autocommit=True, connect_timeout=5,
                                    prepare_threshold=None, options="-c statement_timeout=5000")
        self.key, self.owner = key, None
        self.conn.execute("INSERT INTO vault_bridge.state (key) VALUES (%s) ON CONFLICT DO NOTHING", (key,))

    def close(self):
        self.conn.close()

    def enqueue(self):
        return self.conn.execute("UPDATE vault_bridge.state SET generation=generation+1 WHERE key=%s "
                                 "RETURNING generation", (self.key,)).fetchone()[0]

    def acquire(self):
        owner = uuid.uuid4()
        row = self.conn.execute("UPDATE vault_bridge.state SET lease=%s, "
            "lease_until=now()+interval '360 seconds' WHERE key=%s AND "
            "(lease_until IS NULL OR lease_until<now()) RETURNING payload,generation,processed",
            (owner, self.key)).fetchone()
        if row:
            self.owner = owner
        return row

    def save(self, payload):
        from psycopg.types.json import Jsonb
        row = self.conn.execute("UPDATE vault_bridge.state SET payload=%s WHERE key=%s AND lease=%s "
            "AND lease_until>now() RETURNING key", (Jsonb(payload), self.key, self.owner)).fetchone()
        if not row:
            raise BridgeError("Sync lease expired; stopped before another external write")

    def archive(self, label, payload):
        from psycopg.types.json import Jsonb
        self.assert_owner()
        self.conn.execute("INSERT INTO vault_bridge.history (vault_key,label,payload) VALUES (%s,%s,%s)",
                          (self.key, label, Jsonb(payload)))

    def assert_owner(self):
        row = self.conn.execute("SELECT 1 FROM vault_bridge.state WHERE key=%s AND lease=%s "
                                "AND lease_until>now()", (self.key, self.owner)).fetchone()
        if not row:
            raise BridgeError("Sync lease expired")

    def finish(self, generation, complete):
        if complete:
            self.conn.execute("UPDATE vault_bridge.state SET processed=GREATEST(processed,%s), "
                "last_run=now(), last_error=NULL WHERE key=%s AND lease=%s", (generation, self.key, self.owner))

    def release(self):
        self.conn.execute("UPDATE vault_bridge.state SET lease=NULL, lease_until=NULL WHERE key=%s AND lease=%s",
                          (self.key, self.owner))

    def status(self):
        row = self.conn.execute("SELECT payload,generation,processed,last_run,last_error "
                                "FROM vault_bridge.state WHERE key=%s", (self.key,)).fetchone()
        payload, generation, processed, last_run, error = row
        return {"mapped_notes": len(payload.get("notes", {})), "issues": payload.get("issues", {}),
                "pending": generation > processed, "last_run": last_run.isoformat() if last_run else None,
                "last_error": error}

    def verification(self, token=None):
        if token is not None:
            self.conn.execute("INSERT INTO vault_bridge.verification (vault_key,token) VALUES (%s,%s) "
                              "ON CONFLICT DO NOTHING", (self.key, token))
        row = self.conn.execute("SELECT token FROM vault_bridge.verification WHERE vault_key=%s", (self.key,)).fetchone()
        return row[0] if row else None


@contextlib.contextmanager
def store_from_env():
    key = os.environ["NOTION_PARENT_ID"] + ":" + os.environ["DROPBOX_VAULT_PATH"].lower()
    # Session startup options need a direct connection on Neon.
    dsn = os.environ.get("DATABASE_URL_UNPOOLED") or os.environ["DATABASE_URL"]
    store = PostgresStore(dsn, key)
    try:
        yield store
    finally:
        store.close()
