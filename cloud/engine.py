"""Bounded reconciliation over Dropbox IDs, Notion IDs and common snapshots."""
from pathlib import PurePosixPath
from posixpath import relpath
import re
import time
from urllib.error import URLError
from urllib.parse import urlsplit

from bridge import BridgeError, check_content, digest, normalise, note_text, page_id, split_note
from cloud.notion_format import child_pages, download_attachment, render_reference


def notion_comparison(body, *, comparison=True):
    """Normalise Notion italics, and compare known transport-only changes."""
    parts = re.split(r"(?ms)(^`{3,}[^\n]*\n.*?^`{3,}[ \t]*(?=\n|$)|`[^`\n]+`)", body)
    for index, part in enumerate(parts):
        if index % 2:
            if comparison:
                parts[index] = re.sub(r"^(`{3,})dataview(?=\n)", r"\1javascript", part)
        else:
            part = re.sub(r"<i>([\w -]+)</i>", r"*\1*", part)
            part = re.sub(r"<b>([\w -]+)</b>", r"**\1**", part)
            part = re.sub(r"<code>([^<>`\n&]+)</code>", r"`\1`", part)
            if comparison:
                part = re.sub(
                    r"\]\(https://(?:www\.notion\.so/|app\.notion\.com/p/)([0-9a-f]{32})\)",
                    r"](https://www.notion.so/\1)", part)
            parts[index] = part
    return "".join(parts)


class Links:
    def __init__(self, mapping):
        self.by_name = {}
        self.by_id = {}
        for path, pid in mapping:
            self.by_name.setdefault(PurePosixPath(path).stem, set()).add(pid)
            self.by_id[pid.replace("-", "")] = PurePosixPath(path).stem

    def to_notion(self, body):
        def replace(match):
            target, _, alias = match[1].partition("|")
            if "#" in target or "/" in target:
                raise BridgeError("Wiki links with headings or paths need review")
            ids = self.by_name.get(target, set())
            if len(ids) != 1:
                raise BridgeError("Wiki link target is missing or ambiguous")
            pid = next(iter(ids))
            label = alias or target
            if any(c in label for c in "[]\\\n"):
                raise BridgeError("Unsupported wiki link label")
            return f"[{label}](https://www.notion.so/{pid.replace('-', '')})"
        notion_body = re.sub(r"(?<!!)\[\[([^\]\n]+)\]\]", replace, body)
        check_content(notion_body)
        return notion_body

    def to_local(self, body):
        local_body = notion_comparison(body, comparison=False)
        check_content(local_body)
        def replace(match):
            target = self.by_id.get(match[2].replace("-", ""))
            if target is None:
                return match[0]
            label = match[1]
            return f"[[{target}]]" if label == target else f"[[{target}|{label}]]"
        return re.sub(r"\[([^\]\n]+)\]\(https://(?:www\.notion\.so/|app\.notion\.com/p/)"
                      r"([0-9a-f-]{32,36})\)", replace, local_body)


class CloudBridge:
    def __init__(self, dropbox, notion, store, parent, state, enabled=False, budget=180):
        self.dropbox, self.notion, self.store = dropbox, notion, store
        self.parent, self.state, self.enabled = page_id(parent), state, enabled
        for key in ("notes", "creates", "imports", "issues", "children", "assets"):
            self.state.setdefault(key, {})
        self.deadline = time.monotonic() + budget
        self.events = []
        self.complete = True

    def check_time(self):
        if time.monotonic() >= self.deadline:
            self.complete = False
            raise BridgeError("Invocation budget reached; pending work remains queued")

    def event(self, action, key, detail=""):
        self.events.append({"action": action, "key": key, "detail": detail})
        if action in {"review", "conflict"}:
            self.state["issues"][key] = {"action": action, "detail": detail}
        else:
            self.state["issues"].pop(key, None)

    def checkpoint(self):
        if self.enabled:
            self.store.save(self.state)

    def get(self, pid, *, nested=False):
        get_reference = getattr(self.notion, "get_reference", self.notion.get)
        remote = get_reference(pid)
        if nested:
            self.validate_child(pid, remote)
        if "\x00" in remote["body"]:
            remote["body"] = remote["body"].replace("\x00", "\ufffd")
            remote["incomplete"] = True
        # Notion renders otherwise empty paragraph blocks as <p /> in Markdown v2.
        remote["body"] = normalise(re.sub(r"(?m)^<p\s*/>[ \t]*$", "", remote["body"]).strip("\n"))
        self.state["children"][pid] = sorted(set(child_pages(remote["body"])))
        return remote

    def scope(self, direct):
        """Follow child-page blocks from the selected parent, up to the vault limit."""
        depth = dict.fromkeys(sorted(direct), 0)
        queue = list(depth)
        for pid in queue:
            for child in self.state["children"].get(pid, []):
                if child not in depth and child != self.parent and len(depth) < 200:
                    depth[child] = depth[pid] + 1
                    queue.append(child)
        return depth

    def reference_body(self, remote, note_path):
        self.asset_failures = 0
        folder = str(PurePosixPath(note_path).parent)

        def asset(url, kind):
            parsed = urlsplit(url)
            key = digest(parsed.netloc + parsed.path)
            if key in self.state["assets"]:
                return relpath(self.dropbox.root + "/" + self.state["assets"][key], folder)
            if not self.enabled:
                return None
            self.check_time()
            try:
                filename, data = download_attachment(url)
                self.store.assert_owner()
                path = self.dropbox.save_attachment(filename, data)
            except (BridgeError, URLError, TimeoutError, ValueError):
                self.asset_failures += 1
                return None
            self.state["assets"][key] = path
            self.checkpoint()
            return relpath(self.dropbox.root + "/" + path, folder)

        def title_for(url):
            try:
                return self.links.by_id.get(page_id(url).replace("-", ""))
            except BridgeError:
                return None

        return render_reference(remote["body"], remote["id"], title_for, asset,
                                incomplete=remote.get("incomplete", False))

    def reference_event(self, key):
        if self.asset_failures:
            self.event("review", key, f"{self.asset_failures} attachments link back to Notion")
        else:
            self.event("reference", key)

    def validate_child(self, pid, remote):
        if "parent" not in remote:
            owner = next((p for p, children in self.state["children"].items() if pid in children), None)
        else:
            owner = self.notion.owning_page(remote["parent"])
        depth = self.scope(self.direct_pages)
        if depth.get(owner, 201) >= depth.get(pid, 201) or pid not in self.state["children"].get(owner, []):
            for children in self.state["children"].values():
                if pid in children:
                    children.remove(pid)
            raise BridgeError("Child page could not be verified inside the selected Notion tree")
        remote["owner"] = owner

    def page_folder(self, remote):
        owner = remote.get("owner")
        if not owner:
            return PurePosixPath(self.dropbox.root)
        if owner not in self.paths:
            raise BridgeError("Import the parent note before its child pages")
        parent = PurePosixPath(self.paths[owner])
        return parent.parent / parent.stem

    def sync_reference(self, fid, note, remote, row):
        if row and note["body"] != row["local_body"]:
            self.conflict(fid, note["raw"], remote, "Local edits to a Notion reference were preserved")
            return
        current = note["entry"]["path_display"]
        destination = current
        if remote.get("owner") or (row and row.get("parent_page_id")):
            destination = str(self.page_folder(remote) / PurePosixPath(current).name)
        if row is None and note["body"] != self.reference_body(remote, current):
            self.conflict(fid, note["raw"], remote, "Reference has no matching baseline; local content preserved")
            return
        body = self.reference_body(remote, destination)
        moved = destination != current
        if moved and self.enabled:
            self.store.assert_owner()
            moved_entry = self.dropbox.move_note(note["entry"], destination)
            if moved_entry["rev"] != note["entry"]["rev"]:
                moved_entry, current_raw = self.dropbox.read(moved_entry)
                if current_raw != note["raw"]:
                    raise BridgeError("Note content changed during the move; local edits retained")
            note["entry"] = moved_entry
            self.paths[note["page_id"]] = note["entry"]["path_display"]
        if moved or row is None or body != note["body"] or row.get("mode") != "reference":
            self.accept(fid, note, body, remote, mode="reference")
        self.reference_event(fid)
        if moved:
            self.events.append({"action": "move-dropbox", "key": fid, "detail": ""})

    def conflict(self, key, local, remote, reason):
        fingerprint = digest(local + repr(sorted(remote.items())))
        fingerprints = self.state.setdefault("conflict_hashes", {})
        if self.enabled and fingerprints.get(key) != fingerprint:
            self.store.archive("conflict", {"key": key, "local": local, "notion": remote})
            fingerprints[key] = fingerprint
        self.event("conflict", key, reason)
        self.checkpoint()

    def run(self):
        children = set(self.notion.children(self.parent))
        self.direct_pages = children
        progress = set(self.state.get("progress", [])) if self.enabled else set()

        def finished(key):
            progress.add(key)
            if self.enabled:
                self.state["progress"] = sorted(progress)

        files = self.dropbox.list_notes()
        notes, ids = {}, {}
        for entry in files:
            self.check_time()
            fid = entry["id"]
            staged = self.state["creates"].get(fid, {})
            previous = self.state["notes"].get(fid, {})
            if previous and "note:" + fid in progress:
                raw = head = body = ""
                pid = previous["page_id"]
            else:
                entry, raw = self.dropbox.read(entry)
                head, body, pid = split_note(raw)
            known_id = staged.get("page_id") or previous.get("page_id")
            if pid and known_id and pid != known_id:
                raise BridgeError("Mapped note changed Notion identity; reconcile it before syncing")
            pid = pid or known_id
            if pid:
                if pid in ids:
                    raise BridgeError("Duplicate Notion page ID in the vault; no writes performed")
                ids[pid] = fid
            notes[fid] = {"entry": entry, "raw": raw, "head": head, "body": body, "page_id": pid}

        # Allocate pages first so wiki links between new notes can use stable Notion IDs.
        for fid, note in notes.items():
            if note["page_id"]:
                continue
            self.check_time()
            try:
                # Check the supported syntax with placeholder URLs before creating anything.
                check_content(re.sub(r"(?<!!)\[\[[^\]\n]+\]\]", "[link](https://example.invalid)", note["body"]))
                staged = self.state["creates"].get(fid)
                if staged:
                    raise BridgeError("Previous create has an uncertain outcome; link the existing Notion page ID")
                self.event("create-notion", fid)
                if self.enabled:
                    self.state["creates"][fid] = {"path": note["entry"]["path_display"], "status": "pending"}
                    self.checkpoint()
                    self.store.assert_owner()
                    pid = self.notion.create(self.parent, PurePosixPath(note["entry"]["path_display"]).stem, "")
                    self.state["creates"][fid].update(page_id=pid, status="created")
                    self.checkpoint()
                    note["page_id"] = pid
                    children.add(pid)
                    ids[pid] = fid
            except BridgeError as exc:
                self.event("review", fid, str(exc))
                self.checkpoint()

        mapping = [(n["entry"]["path_display"], n["page_id"]) for n in notes.values() if n["page_id"]]
        # Include filenames planned for Notion-only imports in the reverse link map.
        for pid in children - set(ids):
            row = self.state["imports"].get(pid)
            if row:
                mapping.append((row["filename"], pid))
        self.links = Links(mapping)
        self.paths = {pid: path for path, pid in mapping if path.startswith("/")}

        depth = self.scope(children)
        for fid, note in sorted(notes.items(), key=lambda item: depth.get(item[1]["page_id"], 201)):
            if not note["page_id"] or "note:" + fid in progress:
                continue
            try:
                self.check_time()
                if note["page_id"] not in self.scope(children):
                    raise BridgeError("Notion page moved, deleted or became inaccessible; Dropbox note retained")
                self.sync_note(fid, note, nested=note["page_id"] not in children)
            except BridgeError as exc:
                self.event("review", fid, str(exc))
                self.checkpoint()
                if not self.complete:
                    break
            finished("note:" + fid)

        for fid in set(self.state["notes"]) - set(notes):
            self.event("review", fid, "Dropbox note deleted or moved out of scope; Notion page retained")

        uncertain = any(not v.get("page_id") and not notes.get(fid, {}).get("page_id")
                        for fid, v in self.state["creates"].items())
        tracked = {row["page_id"] for row in self.state["notes"].values()}
        if uncertain:
            self.event("review", "imports", "Remote discovery paused while a page creation is uncertain")
        elif self.complete:
            occupied = {
                PurePosixPath(note["entry"]["path_display"]).name.casefold()
                for note in notes.values()
            }
            occupied.update(PurePosixPath(row["filename"]).name.casefold() for row in self.state["imports"].values())
            seen = set(ids) | tracked | {key[7:] for key in progress if key.startswith("import:")}
            while (remaining := [pid for pid in self.scope(children) if pid not in seen]):
                pid = remaining[0]
                seen.add(pid)
                try:
                    self.check_time()
                    imported = self.import_note(pid, occupied, nested=pid not in children)
                    if imported:
                        finished("note:" + imported)
                except BridgeError as exc:
                    self.event("review", pid, str(exc))
                    self.checkpoint()
                    if not self.complete:
                        break
                finished("import:" + pid)
        if self.complete and self.enabled:
            self.state.pop("progress", None)
        self.checkpoint()
        return self.events

    def sync_note(self, fid, note, *, nested=False):
        pid = note["page_id"]
        remote = self.get(pid, nested=nested)
        row = self.state["notes"].get(fid)
        if nested or remote.get("incomplete") or (row and row.get("mode") == "reference") or "notion_sync_mode: reference\n" in note["head"]:
            return self.sync_reference(fid, note, remote, row)
        try:
            check_content(notion_comparison(remote["body"]))
        except BridgeError:
            return self.sync_reference(fid, note, remote, row)
        rendered = self.links.to_notion(note["body"])
        staged = self.state["creates"].get(fid, {}).get("page_id") == pid
        stem = PurePosixPath(note["entry"]["path_display"]).stem
        title = stem if row is None or stem != PurePosixPath(row["path"]).stem else row["title"]
        if notion_comparison(rendered) == notion_comparison(remote["body"]) and (not row or title == remote["title"]):
            self.accept(fid, note, note["body"], remote)
            self.event("synced", fid)
            return
        if row is None:
            if not staged or remote["body"]:
                self.conflict(fid, note["raw"], remote, "No common baseline; reconcile both versions")
                return
            local_changed, remote_changed = True, False
        else:
            local_changed = (note["body"], title) != (row["local_body"], row["title"])
            remote_changed = (remote["body"], remote["title"]) != (row["remote_body"], row["title"])
        if local_changed and remote_changed:
            self.conflict(fid, note["raw"], remote, "Both versions changed since the last successful sync")
        elif local_changed:
            self.event("upload", fid)
            if self.enabled:
                self.store.archive("before-notion-update", remote)
                self.store.assert_owner()
                latest = self.get(pid)
                current, raw = self.dropbox.read(note["entry"])
                if latest != remote or current["rev"] != note["entry"]["rev"] or raw != note["raw"]:
                    raise BridgeError("Content changed during sync; retry later")
                self.store.assert_owner()
                self.notion.update(pid, remote["body"], rendered, title if title != remote["title"] else None)
                verified = self.get(pid)
                if (notion_comparison(verified["body"]), verified["title"]) != (notion_comparison(rendered), title):
                    self.conflict(fid, note["raw"], verified, "Notion read-back differs; baseline retained")
                    return
                self.accept(fid, note, note["body"], verified)
        elif remote_changed:
            if remote["body"] != row["remote_body"] and re.search(r"(?m)^`{3,}dataview$", row["local_body"]):
                self.conflict(fid, note["raw"], remote, "Notion edited a note with a Dataview block; review required")
                return
            self.event("download", fid)
            body = note["body"] if remote["body"] == row["remote_body"] else self.links.to_local(remote["body"])
            self.accept(fid, note, body, remote)
        else:
            self.event("synced", fid)

    def accept(self, fid, note, body, remote, *, mode=None):
        if not self.enabled:
            return
        head = note["head"]
        if mode == "reference" and "notion_sync_mode: reference\n" not in head:
            head = head[:-4] + "notion_sync_mode: reference\n---\n" if head else "---\nnotion_sync_mode: reference\n---\n"
        text = note_text(head, body, note["page_id"])
        if text != note["raw"]:
            self.store.archive("before-dropbox-update", {"entry": note["entry"], "raw": note["raw"]})
            self.store.assert_owner()
            self.dropbox.write(note["entry"], text)
        self.state["notes"][fid] = {"page_id": note["page_id"], "path": note["entry"]["path_display"],
            "local_body": normalise(body), "remote_body": remote["body"], "title": remote["title"]}
        if mode:
            self.state["notes"][fid]["mode"] = mode
        if remote.get("owner"):
            self.state["notes"][fid]["parent_page_id"] = remote["owner"]
        self.state["issues"].pop(fid, None)
        self.state["creates"].pop(fid, None)
        self.state["imports"].pop(note["page_id"], None)
        self.checkpoint()

    def import_note(self, pid, occupied, *, nested=False):
        remote = self.get(pid, nested=nested)
        reference = nested or remote.get("incomplete", False)
        if not reference:
            try:
                body = self.links.to_local(remote["body"])
            except BridgeError:
                reference = True
        slug = re.sub(r"[^\w .-]", "-", remote["title"]).strip(" .")[:80] or "Untitled"
        filename = f"{slug}.md"
        number = 2
        while filename.casefold() in occupied:
            filename = f"{slug} ({number}).md"
            number += 1
        occupied.add(filename.casefold())
        path = self.page_folder(remote) / filename
        filename = str(path.relative_to(self.dropbox.root))
        if reference:
            body = self.reference_body(remote, str(path))
        self.event("create-dropbox", pid, "Notion reference" if reference else "")
        if not self.enabled:
            self.paths[pid] = str(path)
            return
        staged = self.state["imports"].get(pid)
        if staged:
            # A successful-but-unacknowledged upload is recovered by its embedded ID on the next scan.
            raise BridgeError("Previous Dropbox import was interrupted; inspect the target before retrying")
        self.state["imports"][pid] = {"filename": filename, "status": "pending"}
        self.checkpoint()
        self.store.assert_owner()
        head = "---\nnotion_sync_mode: reference\n---\n" if reference else ""
        entry = self.dropbox.create(filename, note_text(head, body, pid))
        self.paths[pid] = entry["path_display"]
        self.links.by_id[pid.replace("-", "")] = path.stem
        self.state["notes"][entry["id"]] = {"page_id": pid, "path": entry["path_display"],
            "local_body": body, "remote_body": remote["body"], "title": remote["title"]}
        if reference:
            self.state["notes"][entry["id"]]["mode"] = "reference"
            self.reference_event(entry["id"])
        if remote.get("owner"):
            self.state["notes"][entry["id"]]["parent_page_id"] = remote["owner"]
        self.state["imports"].pop(pid, None)
        self.checkpoint()
        return entry["id"]
