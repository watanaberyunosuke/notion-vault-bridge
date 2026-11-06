import copy
import unittest
import uuid
from unittest.mock import patch

from bridge import BridgeError, split_note
from cloud.engine import CloudBridge, Links
from cloud.storage import Dropbox, empty_state
from mock_notion import FakeNotion


PARENT = "12345678-1234-4234-8234-123456789abc"


class MemoryStore:
    def __init__(self):
        self.state = empty_state()
        self.history = []
        self.owned = True

    def save(self, state):
        self.assert_owner()
        self.state = copy.deepcopy(state)

    def archive(self, label, payload):
        self.assert_owner()
        self.history.append((label, copy.deepcopy(payload)))

    def assert_owner(self):
        if not self.owned:
            raise BridgeError("Sync lease expired")


class MemoryDropbox:
    root = "/obsidian/example-vault"

    def move_note(self, entry, destination):
        current, raw = self.files[entry["id"]]
        moved = {**current, "path_display": destination}
        self.files[entry["id"]] = moved, raw
        return copy.deepcopy(moved)

    def __init__(self):
        self.files = {}
        self.writes = 0
        self.on_write = None

    def create(self, filename, raw):
        path = "/obsidian/example-vault/" + filename
        if any(entry["path_display"] == path for entry, _ in self.files.values()):
            raise BridgeError("Dropbox filename conflict")
        fid = "id:" + uuid.uuid4().hex
        entry = {"id": fid, "path_display": path, "rev": "1", ".tag": "file"}
        self.files[fid] = entry, raw
        return copy.deepcopy(entry)

    def list_notes(self):
        return [copy.deepcopy(e) for e, _ in self.files.values()]

    def read(self, entry):
        return copy.deepcopy(self.files[entry["id"]])

    def write(self, entry, raw):
        self.writes += 1
        if self.on_write:
            callback, self.on_write = self.on_write, None
            callback()
        current, _ = self.files[entry["id"]]
        if current["rev"] != entry["rev"]:
            raise BridgeError("Dropbox revision conflict")
        current = copy.deepcopy(current)
        current["rev"] = str(int(current["rev"]) + 1)
        self.files[entry["id"]] = current, raw
        return current

    def edit(self, fid, raw):
        entry, _ = self.files[fid]
        entry = copy.deepcopy(entry)
        entry["rev"] = str(int(entry["rev"]) + 1)
        self.files[fid] = entry, raw


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.notion = FakeNotion()
        self.dropbox = MemoryDropbox()

    def run_sync(self, enabled=True, budget=100):
        engine = CloudBridge(self.dropbox, self.notion, self.store, PARENT,
                             copy.deepcopy(self.store.state), enabled, budget)
        return engine.run()

    def seed(self):
        entry = self.dropbox.create("Home.md", "# Home\n\nOriginal body.\n")
        self.run_sync()
        pid = self.store.state["notes"][entry["id"]]["page_id"]
        return entry["id"], pid

    def test_initial_sync_and_repeat_are_idempotent(self):
        fid, pid = self.seed()
        raw = self.dropbox.files[fid][1]
        self.assertEqual(split_note(raw)[2], pid)
        writes = self.dropbox.writes
        self.run_sync()
        self.assertEqual(self.notion.creates, 1)
        self.assertEqual(self.dropbox.writes, writes)

    def test_wiki_links_between_new_notes(self):
        home = self.dropbox.create("Home.md", "See [[Research]].")
        research = self.dropbox.create("Research.md", "Research note")
        self.run_sync()
        home_id = self.store.state["notes"][home["id"]]["page_id"]
        research_id = self.store.state["notes"][research["id"]]["page_id"]
        self.assertIn(research_id.replace("-", ""), self.notion.pages[home_id]["body"])
        self.assertIn("[[Research]]", self.dropbox.files[home["id"]][1])
        self.assertEqual(self.notion.creates, 2)
        self.run_sync()
        self.assertEqual(self.notion.creates, 2)

    def test_local_upload_and_remote_download(self):
        fid, pid = self.seed()
        self.dropbox.edit(fid, self.dropbox.files[fid][1].replace("Original", "Local"))
        self.assertIn("upload", [e["action"] for e in self.run_sync()])
        self.assertIn("Local", self.notion.pages[pid]["body"])
        self.notion.pages[pid]["body"] += "\n\nRemote change."
        self.assertIn("download", [e["action"] for e in self.run_sync()])
        self.assertIn("Remote change", self.dropbox.files[fid][1])

    def test_conflict_preserves_versions_and_new_conflict_snapshots(self):
        fid, pid = self.seed()
        self.dropbox.edit(fid, self.dropbox.files[fid][1] + "Local change")
        self.notion.pages[pid]["body"] += "Remote change"
        before = copy.deepcopy(self.dropbox.files[fid])
        self.run_sync()
        self.assertEqual(self.store.state["issues"][fid]["action"], "conflict")
        self.assertEqual(self.dropbox.files[fid], before)
        saved = len(self.store.history)
        self.run_sync()
        self.assertEqual(len(self.store.history), saved)
        self.notion.pages[pid]["body"] += " Another edit"
        self.run_sync()
        self.assertEqual(len(self.store.history), saved + 1)

    def test_file_rename_uses_dropbox_id(self):
        fid, pid = self.seed()
        self.dropbox.files[fid][0]["path_display"] = "/obsidian/example-vault/New name.md"
        self.run_sync()
        self.assertEqual(self.notion.pages[pid]["title"], "New name")
        self.assertEqual(self.notion.creates, 1)

    def test_notion_title_change_retains_local_filename(self):
        fid, pid = self.seed()
        self.notion.pages[pid]["title"] = "Remote renamed"
        self.run_sync()
        self.assertEqual(self.store.state["notes"][fid]["title"], "Remote renamed")
        self.assertTrue(self.dropbox.files[fid][0]["path_display"].endswith("Home.md"))

    def test_dropbox_revision_conflict_does_not_overwrite(self):
        fid, pid = self.seed()
        self.notion.pages[pid]["body"] = "Remote change"
        self.dropbox.on_write = lambda: self.dropbox.edit(fid, self.dropbox.files[fid][1] + "Concurrent edit")
        self.assertIn("review", [e["action"] for e in self.run_sync()])
        self.assertIn("Concurrent edit", self.dropbox.files[fid][1])

    def test_uncertain_notion_create_is_not_retried_or_imported(self):
        self.dropbox.create("New.md", "Body")
        self.notion.fail_after_create = True
        self.run_sync()
        self.run_sync()
        self.assertEqual(self.notion.creates, 1)
        self.assertEqual(len(self.dropbox.files), 1)

    def test_known_created_page_recovers_after_interrupted_upload(self):
        self.dropbox.create("New.md", "Body")
        real = self.notion.update
        self.notion.update = lambda *a, **k: (_ for _ in ()).throw(BridgeError("Interrupted"))
        self.run_sync()
        self.assertEqual(self.notion.creates, 1)
        self.notion.update = real
        self.run_sync()
        self.assertEqual(self.notion.creates, 1)
        self.assertEqual(len(self.store.state["notes"]), 1)

    def test_new_remote_page_import_and_restart(self):
        pid = self.notion.create(PARENT, "Remote", "Remote note")
        self.run_sync()
        self.assertEqual(len(self.dropbox.files), 1)
        self.run_sync()
        self.assertEqual(len(self.dropbox.files), 1)
        self.assertEqual(next(iter(self.store.state["notes"].values()))["page_id"], pid)
        self.assertEqual(next(iter(self.dropbox.files.values()))[0]["path_display"], "/obsidian/example-vault/Remote.md")

    def test_duplicate_notion_titles_get_distinct_filenames(self):
        for _ in range(2):
            self.notion.create(PARENT, "Remote", "Remote note")
        self.run_sync()
        self.assertEqual(
            {entry["path_display"] for entry, _ in self.dropbox.files.values()},
            {"/obsidian/example-vault/Remote.md", "/obsidian/example-vault/Remote (2).md"},
        )

    def test_rich_parent_and_nested_child_import_without_notion_writeback(self):
        parent = self.notion.create(PARENT, "Rich parent", "")
        child = self.notion.create(parent, "Nested child", "Child content")
        self.notion.pages[parent]["body"] = (
            f'<b>Bold</b> and <code>code</code>\n\n'
            f'<page url="https://www.notion.so/{child.replace("-", "")}">Nested child</page>'
        )
        # Rich markup stays in Notion even though its local representation is editable text.
        self.notion.pages[parent]["body"] += '\n<toggle>Details\n\nMore text</toggle>'
        self.notion.pages[parent]["body"] += '\n<b><code>table.`name`</code></b>'
        with patch.object(self.notion, "children", return_value=[parent]):
            self.run_sync()
            mapped = self.store.state["notes"]
            self.assertEqual({row["page_id"] for row in mapped.values()}, {parent, child})
            child_file = next(fid for fid, row in mapped.items() if row["page_id"] == child)
            self.assertEqual(mapped[child_file]["path"], "/obsidian/example-vault/Rich parent/Nested child.md")
            self.dropbox.move_note(self.dropbox.files[child_file][0], "/obsidian/example-vault/Nested child.md")
            self.run_sync()
            self.assertEqual(self.store.state["notes"][child_file]["path"],
                             "/obsidian/example-vault/Rich parent/Nested child.md")
            parent_file = next(fid for fid, row in mapped.items() if row["page_id"] == parent)
            raw = self.dropbox.files[parent_file][1]
            self.assertIn("notion_sync_mode: reference", raw)
            self.assertIn("**Bold** and `code`", raw)
            self.assertIn("`` table.`name` ``", raw)
            self.assertNotIn("\x00", raw)
            self.dropbox.edit(parent_file, raw + "\nLocal edit\n")
            events = self.run_sync()
        self.assertTrue(any(e["key"] == parent_file and e["action"] == "conflict" for e in events))
        self.assertNotIn("Local edit", self.notion.pages[parent]["body"])

    def test_both_deletion_directions_retain_other_copy(self):
        fid, pid = self.seed()
        del self.notion.pages[pid]
        self.run_sync()
        self.assertIn(fid, self.dropbox.files)
        self.notion.pages[pid] = {"id": pid, "title": "Home", "body": "Body"}
        del self.dropbox.files[fid]
        self.run_sync()
        self.assertIn(pid, self.notion.pages)
        self.assertFalse(self.dropbox.files)

    def test_dry_run_does_not_write(self):
        self.dropbox.create("New.md", "Body")
        self.notion.create(PARENT, "Remote", "Body")
        self.run_sync(enabled=False)
        self.assertEqual(self.notion.creates, 1)
        self.assertEqual(len(self.dropbox.files), 1)
        self.assertFalse(self.store.state["notes"])

    def test_duplicate_page_ids_abort_before_writes(self):
        fid, _ = self.seed()
        self.dropbox.create("Duplicate.md", self.dropbox.files[fid][1])
        writes = self.dropbox.writes
        with self.assertRaisesRegex(BridgeError, "Duplicate"):
            self.run_sync()
        self.assertEqual(self.dropbox.writes, writes)

    def test_scope_boundary(self):
        dropbox = Dropbox("/obsidian/example-vault", "key", "secret", "refresh", "account")
        for path in ("/obsidian/example-vault-other/a.md", "/other/a.md", "/obsidian/example-vault/.trash/a.md",
                     "/obsidian/example-vault/../a.md"):
            self.assertFalse(dropbox.in_scope(path))
        self.assertTrue(dropbox.in_scope("/obsidian/example-vault/Folder/a.md"))

    def test_lease_loss_prevents_external_writes(self):
        self.dropbox.create("New.md", "Body")
        self.store.owned = False
        with self.assertRaisesRegex(BridgeError, "lease expired"):
            self.run_sync()
        self.assertEqual(self.notion.creates, 0)

    def test_wiki_link_roundtrip_and_ambiguity(self):
        links = Links([("/Research.md", PARENT)])
        self.assertEqual(links.to_local(links.to_notion("See [[Research|details]].")), "See [[Research|details]].")
        with self.assertRaises(BridgeError):
            links.to_notion("[[Missing]]")

    def test_notion_empty_paragraph_markers(self):
        fid, pid = self.seed()
        self.notion.pages[pid]["body"] = "<p />\n\nRemote body\n\n<p />"
        self.run_sync()
        self.assertEqual(split_note(self.dropbox.files[fid][1])[1], "Remote body")


if __name__ == "__main__":
    unittest.main()
