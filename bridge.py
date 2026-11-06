#!/usr/bin/env python3
"""One Markdown vault <-> one Notion parent page. Python standard library only."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid


class BridgeError(Exception):
    pass


def page_id(value):
    page_ref = value.split("?")[0].rstrip("/")
    match = re.search(r"([0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$", page_ref)
    if not match:
        raise BridgeError("Expected a Notion page UUID or page URL")
    return str(uuid.UUID(match[1]))


def normalise(body):
    return body.replace("\r\n", "\n").rstrip("\n")


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def split_note(raw):
    """Preserve user frontmatter verbatim; never publish it as page content."""
    if raw.startswith("---\n"):
        end = raw.find("\n---\n", 4)
        if end < 0:
            raise BridgeError("Unterminated YAML frontmatter")
        head, body = raw[:end + 5], raw[end + 5:]
    else:
        head, body = "", raw
    ids = re.findall(r"^notion_page_id:\s*[\"']?([0-9a-fA-F-]+)[\"']?\s*$", head, re.M)
    if "notion_page_id:" in head and len(ids) != 1:
        raise BridgeError("Invalid or duplicate notion_page_id frontmatter")
    return head, normalise(body), page_id(ids[0]) if ids else None


def note_text(head, body, pid):
    if not head:
        head = f"---\nnotion_page_id: {pid}\n---\n"
    elif not re.search(r"^notion_page_id:", head, re.M):
        head = head[:-4] + f"notion_page_id: {pid}\n---\n"
    return head + normalise(body) + "\n"


def check_content(body):
    # Strict first-version contract: reject constructs that cannot round-trip safely.
    plain = re.sub(r"^(`{3,}|~{3,}).*?^\1[^\n]*$", "", body, flags=re.M | re.S)
    plain = re.sub(r"`[^`\n]+`", "", plain)
    patterns = {
        r"<[/!?A-Za-z]": "HTML/Notion custom blocks",
        r"\[\[|!\[": "wiki links, embeds or images",
        r"\]\((?!https?://|mailto:|#)": "local attachment or relative links",
        r"(?m)^\s*\|": "tables",
        r"\$\$|(?m:^\s*>\s*\[!)|==[^=]+==|%%": "math, callouts or plugin syntax",
        r"\{(?:color|background)=": "Notion formatting attributes",
        r"(?m)^\s*(?:---+|\*\*\*+)\s*$": "horizontal rules/frontmatter in body",
    }
    for pattern, label in patterns.items():
        if re.search(pattern, plain):
            raise BridgeError(f"Unsupported content: {label}; page left for review")


class Notion:
    """REST adapter. Retry GET/429 only; never blindly retry ambiguous writes."""
    def __init__(self, token):
        self.token = token
        self.last_call = 0.0

    def request(self, method, route, data=None):
        for attempt in range(4):
            time.sleep(max(0, .36 - (time.monotonic() - self.last_call)))
            self.last_call = time.monotonic()
            req = Request("https://api.notion.com/v1/" + route,
                          data=json.dumps(data).encode() if data is not None else None,
                          method=method, headers={"Authorization": f"Bearer {self.token}",
                          "Notion-Version": "2026-03-11", "Content-Type": "application/json"})
            try:
                with urlopen(req, timeout=45) as response:
                    return json.load(response)
            except HTTPError as exc:
                if attempt < 3 and (exc.code == 429 or (method == "GET" and exc.code >= 500)):
                    try:
                        delay = float(exc.headers.get("Retry-After", 2 ** attempt))
                    except ValueError:
                        delay = 2 ** attempt
                    time.sleep(min(30, max(1, delay)))
                    continue
                # Do not expose request headers, credentials or page bodies in logs.
                raise BridgeError(f"Notion HTTP {exc.code} during {method}; inspect connection access or API logs") from None
            except (URLError, TimeoutError, json.JSONDecodeError):
                raise BridgeError(f"Notion {method} failed; write outcome may be uncertain") from None
        raise BridgeError("Notion retry limit reached")

    def children(self, parent):
        result, cursor = [], None
        while True:
            query = {"page_size": 100}
            if cursor:
                query["start_cursor"] = cursor
            response = self.request("GET", f"blocks/{parent}/children?{urlencode(query)}")
            result.extend(page_id(item["id"]) for item in response["results"]
                          if item["type"] == "child_page")
            if not response.get("has_more"):
                return result
            cursor = response["next_cursor"]

    def get(self, pid):
        return self._get(pid, reference=False)

    def get_reference(self, pid):
        return self._get(pid, reference=True)

    def _get(self, pid, *, reference):
        from html import escape
        meta = self.request("GET", f"pages/{pid}")
        response = self.request("GET", f"pages/{pid}/markdown?markdown_version=v2")
        body = response["markdown"]
        incomplete = bool(response.get("truncated") or response.get("unknown_block_ids") or response.get("warnings"))
        if not reference:
            self.complete(response)
        elif incomplete:
            links = {}
            for block_id in response.get("unknown_block_ids", []):
                try:
                    block = self.request("GET", f"blocks/{page_id(block_id)}")
                except BridgeError:
                    continue
                kind = block["type"]
                if kind == "link_to_page":
                    target = block[kind].get(block[kind].get("type", ""))
                    if target:
                        links[page_id(block_id)] = "https://www.notion.so/" + page_id(target).replace("-", "")
                elif kind == "link_preview":
                    links[page_id(block_id)] = block[kind].get("url", "")

            def resolve(match):
                try:
                    url = links.get(page_id(match[1]))
                except BridgeError:
                    return match[0]
                return f'<link-preview url="{escape(url, quote=True)}" />' if url else match[0]

            body = re.sub(r'<unknown\b[^>]*?url="([^"]+)"[^>]*/>', resolve, body)
        title_prop = next((v for v in meta["properties"].values() if v.get("type") == "title"), {})
        title = "".join(t.get("plain_text", t.get("text", {}).get("content", ""))
                        for t in title_prop.get("title", [])) or "Untitled"
        result = {"id": pid, "title": title, "body": normalise(body)}
        if reference:
            result["incomplete"] = incomplete
            result["parent"] = meta.get("parent", {})
        return result

    def owning_page(self, parent):
        for _ in range(12):
            kind = parent.get("type")
            if kind == "page_id":
                return page_id(parent["page_id"])
            if kind != "block_id":
                break
            block_id = page_id(parent["block_id"])
            block = self.request("GET", f"blocks/{block_id}")
            if block.get("type") == "child_page":
                return block_id
            parent = block.get("parent", {})
        return None

    @staticmethod
    def complete(response):
        if response.get("truncated") or response.get("unknown_block_ids") or response.get("warnings"):
            raise BridgeError("Notion returned incomplete content or conversion warnings; review required")

    def create(self, parent, title, body):
        response = self.request("POST", "pages", {
            "parent": {"page_id": parent},
            "properties": {"title": {"type": "title", "title": [{"type": "text", "text": {"content": title}}]}},
            "markdown": body, "markdown_version": "v2"})
        return page_id(response["id"])

    def update(self, pid, old_body, new_body, title=None):
        if old_body != new_body:
            # Full-body search matches the fetched version; child-page deletion stays disabled.
            if old_body:
                command = {"type": "update_content", "update_content": {
                    "content_updates": [{"old_str": old_body, "new_str": new_body}]}}
            else:
                command = {"type": "insert_content", "insert_content": {"content": new_body}}
            response = self.request("PATCH", f"pages/{pid}/markdown", {
                **command, "markdown_version": "v2"})
            self.complete(response)
        if title is not None:
            self.request("PATCH", f"pages/{pid}", {"properties": {
                "title": {"title": [{"type": "text", "text": {"content": title}}]}}})


class Bridge:
    def __init__(self, vault, state_dir, parent, api, apply=False, settle=2):
        self.vault = Path(vault).resolve()
        self.state_dir = Path(state_dir).resolve()
        if self.state_dir == self.vault or self.vault in self.state_dir.parents:
            raise BridgeError("State directory must be outside the vault")
        if not self.vault.is_dir():
            raise BridgeError("Vault directory does not exist")
        self.parent, self.api, self.apply, self.settle = page_id(parent), api, apply, settle
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.state_dir / "state.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, path TEXT UNIQUE, body TEXT, title TEXT);
            CREATE TABLE IF NOT EXISTS creates (path TEXT PRIMARY KEY, id TEXT, status TEXT);
        """)
        identity = json.dumps([str(self.vault), self.parent])
        old = self.db.execute("SELECT value FROM config WHERE key='identity'").fetchone()
        if old and old[0] != identity:
            self.db.close()
            raise BridgeError("State belongs to a different vault or parent page")
        self.db.execute("INSERT OR IGNORE INTO config VALUES ('identity', ?)", (identity,))
        self.db.commit()
        self.events = []

    def event(self, action, path, detail=""):
        self.events.append({"action": action, "path": str(path), "detail": detail})

    def save(self, pid, path, body, title):
        self.db.execute("INSERT INTO notes VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                        "path=excluded.path, body=excluded.body, title=excluded.title",
                        (pid, str(path.relative_to(self.vault)), body, title))
        self.db.commit()

    def backup(self, name, content):
        folder = self.state_dir / "history" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{time.time_ns()}.md").write_text(content, encoding="utf-8")

    def write(self, path, expected, content):
        if path.is_symlink():
            raise BridgeError("Refusing symlink write")
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != expected:
            raise BridgeError("Local note changed during sync; retry next pass")
        if expected is not None:
            self.backup(digest(str(path))[:16], expected)
        # Narrow the race window; external editors do not honour the bridge process lock.
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp.open("x", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current != expected:
                raise BridgeError("Local note changed before save")
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def conflict(self, pid, path, local, remote, reason):
        self.event("conflict", path, reason)
        if self.apply:
            folder = self.state_dir / "conflicts" / pid
            folder.mkdir(parents=True, exist_ok=True)
            # Content-addressed reports preserve earlier competing versions.
            key = digest(local + json.dumps(remote, sort_keys=True))[:16]
            (folder / f"{key}.local.md").write_text(local, encoding="utf-8")
            (folder / f"{key}.notion.json").write_text(json.dumps(remote, indent=2), encoding="utf-8")

    def scan(self):
        notes, ids = [], {}
        for path in sorted(self.vault.rglob("*.md")):
            if any(p.startswith(".") for p in path.relative_to(self.vault).parts):
                continue
            if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != self.vault):
                raise BridgeError("Symlink notes/folders are not supported")
            raw = path.read_text(encoding="utf-8")
            head, body, pid = split_note(raw)
            if pid:
                if pid in ids:
                    raise BridgeError(f"Duplicate notion_page_id in {ids[pid].name} and {path.name}")
                ids[pid] = path
            notes.append((path, raw, head, body, pid))
        return notes, ids

    def run(self):
        self.events = []
        with (self.state_dir / "bridge.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BridgeError("Another bridge process is running") from None
            # Resolve the shared parent before attempting writes.
            children = set(self.api.children(self.parent))
            notes, ids = self.scan()
            paths = {str(n[0].relative_to(self.vault)) for n in notes}
            for path, raw, head, body, pid in notes:
                try:
                    if time.time() - path.stat().st_mtime < self.settle:
                        self.event("pending", path, "Waiting for local edits to settle")
                        continue
                    if pid is None:
                        self.new_note(path, raw, head, body, children)
                    elif pid not in children:
                        self.event("review", path, "Page deleted, moved, inaccessible or outside configured parent")
                    else:
                        self.existing(path, raw, head, body, pid)
                except BridgeError as exc:
                    self.event("review", path, str(exc))
            for row in self.db.execute("SELECT * FROM notes").fetchall():
                if row["id"] not in ids and row["path"] not in paths:
                    self.event("review", row["path"], "Local deletion detected; Notion page retained")
            tracked = {r[0] for r in self.db.execute("SELECT id FROM notes")}
            tracked.update(r[0] for r in self.db.execute("SELECT id FROM creates WHERE id IS NOT NULL"))
            # New Notion children become root-level Markdown files. Known local deletions stay deleted.
            uncertain = self.db.execute("SELECT path FROM creates WHERE id IS NULL").fetchall()
            unresolved = [r[0] for r in uncertain if r[0] not in {
                str(n[0].relative_to(self.vault)) for n in notes if n[4]
            }]
            if unresolved:
                self.event("review", "remote discovery", "Paused until uncertain page creation is reconciled")
            for pid in sorted(children - set(ids) - tracked) if not unresolved else []:
                try:
                    remote = self.api.get(pid)
                    check_content(remote["body"])
                    slug = re.sub(r"[^\w .-]", "-", remote["title"]).strip(" .")[:80] or "Untitled"
                    path = self.vault / f"{slug}--{pid}.md"
                    if path.exists():
                        raise BridgeError("Download target exists; refusing overwrite")
                    self.event("download-new", path)
                    if self.apply:
                        self.write(path, None, note_text("", remote["body"], pid))
                        self.save(pid, path, remote["body"], remote["title"])
                except BridgeError as exc:
                    self.event("review", pid, str(exc))
        return self.events

    def new_note(self, path, raw, head, body, children):
        relative = str(path.relative_to(self.vault))
        if self.db.execute("SELECT 1 FROM notes WHERE path=?", (relative,)).fetchone():
            raise BridgeError("Mapping removed from a tracked note; restore notion_page_id")
        check_content(body)
        record = self.db.execute("SELECT * FROM creates WHERE path=?", (relative,)).fetchone()
        if record and record["id"]:
            pid = record["id"]
            if pid not in children:
                raise BridgeError("Previously created page is not visible under parent")
        elif record:
            raise BridgeError("Uncertain previous create; find page in Notion and add its ID to note before retrying")
        else:
            self.event("upload-new", path)
            if not self.apply:
                return
            self.backup(digest(str(path))[:16], raw)
            self.db.execute("INSERT INTO creates VALUES (?, NULL, 'pending')", (relative,))
            self.db.commit()
            pid = self.api.create(self.parent, path.stem, body)
            self.db.execute("UPDATE creates SET id=?, status='created' WHERE path=?", (pid, relative))
            self.db.commit()
        remote = self.api.get(pid)
        self.write(path, raw, note_text(head, body, pid))
        if remote["body"] != body:
            self.conflict(pid, path, raw, remote, "Create read-back differs; review formatting before baseline")
            return
        self.save(pid, path, body, remote["title"])

    def existing(self, path, raw, head, body, pid):
        remote = self.api.get(pid)
        check_content(body)
        check_content(remote["body"])
        row = self.db.execute("SELECT * FROM notes WHERE id=?", (pid,)).fetchone()
        if row is None:
            if body != remote["body"]:
                self.conflict(pid, path, raw, remote, "No baseline; reconcile existing copies before first sync")
            else:
                self.event("adopt", path)
                if self.apply:
                    self.save(pid, path, body, remote["title"])
            return
        old_path = self.vault / row["path"]
        # A local filename change changes the title; remote titles do not rename local paths.
        title = path.stem if path.stem != old_path.stem else row["title"]
        local_changed = (body, title) != (row["body"], row["title"])
        remote_changed = (remote["body"], remote["title"]) != (row["body"], row["title"])
        if body == remote["body"] and (title == remote["title"] or not local_changed):
            self.event("synced", path)
            if self.apply:
                self.save(pid, path, body, remote["title"])
        elif local_changed and remote_changed:
            self.conflict(pid, path, raw, remote, "Both copies changed since last sync")
        elif local_changed:
            self.event("upload", path)
            if self.apply:
                self.backup(pid, json.dumps(remote, indent=2))
                latest = self.api.get(pid)
                if latest != remote:
                    raise BridgeError("Notion changed during sync; retry next pass")
                if path.read_text(encoding="utf-8") != raw:
                    raise BridgeError("Local note changed during sync")
                self.api.update(pid, remote["body"], body,
                                title if title != remote["title"] else None)
                verified = self.api.get(pid)
                if (verified["body"], verified["title"]) != (body, title):
                    self.conflict(pid, path, raw, verified, "Write read-back differs; baseline retained")
                    return
                self.save(pid, path, body, title)
        elif remote_changed:
            self.event("download", path)
            if self.apply:
                self.write(path, raw, note_text(head, remote["body"], pid))
                self.save(pid, path, remote["body"], remote["title"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--parent", required=True, help="Notion parent page ID or URL")
    parser.add_argument("--state-dir", required=True, help="Private local directory outside vault/Dropbox")
    parser.add_argument("--apply", action="store_true", help="Enable local and remote writes; default is a dry run")
    parser.add_argument("--watch", action="store_true", help="Repeat passes until interrupted")
    parser.add_argument("--interval", type=float, default=60)
    args = parser.parse_args()
    if args.interval < 5:
        parser.error("--interval must be at least 5 seconds")
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        parser.error("Set NOTION_TOKEN locally; do not paste credentials into chat")
    try:
        bridge = Bridge(args.vault, args.state_dir, args.parent, Notion(token), args.apply)
        while True:
            events = bridge.run()
            print(json.dumps(events, ensure_ascii=False, indent=2), flush=True)
            if not args.watch:
                return 2 if any(e["action"] in {"conflict", "review"} for e in events) else 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    except (BridgeError, OSError, sqlite3.Error) as exc:
        print(f"Bridge stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
