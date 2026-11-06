import fcntl
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from bridge import Bridge, BridgeError, Notion, note_text, page_id, split_note
from mock_notion import FakeNotion


PARENT = "11111111-1111-1111-1111-111111111111"


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.state = self.root / "state"
        self.api = FakeNotion()
        self.bridge = Bridge(self.vault, self.state, PARENT, self.api, apply=True, settle=0)
        self.addCleanup(self.bridge.db.close)

    def seed(self):
        path = self.vault / "Research.md"
        path.write_text("# Research\n\nOriginal note.\n")
        self.bridge.run()
        pid = split_note(path.read_text())[2]
        return path, pid

    def actions(self):
        return [e["action"] for e in self.bridge.run()]

    def test_upload_new_and_stable_restart(self):
        path, pid = self.seed()
        self.assertEqual(self.api.pages[pid]["body"], "# Research\n\nOriginal note.")
        self.assertIn("synced", self.actions())
        self.bridge.db.close()
        self.bridge = Bridge(self.vault, self.state, PARENT, self.api, True, settle=0)
        self.addCleanup(self.bridge.db.close)
        self.assertIn("synced", self.actions())
        self.assertEqual(self.api.creates, 1)

    def test_local_edit_upload(self):
        path, pid = self.seed()
        path.write_text(path.read_text().replace("Original", "Edited"))
        self.assertIn("upload", self.actions())
        self.assertIn("Edited", self.api.pages[pid]["body"])
        self.assertIn("synced", self.actions())
        self.assertEqual(self.api.updates, 1)

    def test_remote_edit_download_and_frontmatter(self):
        path, pid = self.seed()
        path.write_text(path.read_text().replace("---\n", "---\ntags: [research]\n", 1))
        self.api.pages[pid]["body"] = "Remote text"
        self.assertIn("download", self.actions())
        self.assertIn("tags: [research]", path.read_text())
        self.assertEqual(split_note(path.read_text())[1], "Remote text")

    def test_remote_new_child_download(self):
        pid = self.api.create(PARENT, "A / title", "New remote child")
        self.assertIn("download-new", self.actions())
        paths = list(self.vault.glob("*.md"))
        self.assertEqual(len(paths), 1)
        self.assertEqual(split_note(paths[0].read_text())[2], pid)
        self.assertIn("synced", self.actions())

    def test_conflict_preserves_both_then_reconcile(self):
        path, pid = self.seed()
        path.write_text(path.read_text().replace("Original", "Local"))
        before = path.read_text()
        self.api.pages[pid]["body"] = "Remote edit"
        self.assertIn("conflict", self.actions())
        self.assertEqual(path.read_text(), before)
        self.assertEqual(self.api.pages[pid]["body"], "Remote edit")
        self.assertEqual(len(list((self.state / "conflicts" / pid).glob("*.local.md"))), 1)
        head, _, _ = split_note(before)
        path.write_text(note_text(head, "Remote edit", pid))
        self.assertIn("synced", self.actions())

    def test_rename_retains_identity_and_updates_title(self):
        path, pid = self.seed()
        new = path.with_name("Renamed.md")
        path.rename(new)
        self.assertIn("upload", self.actions())
        self.assertEqual(self.api.pages[pid]["title"], "Renamed")
        self.assertEqual(self.api.creates, 1)

    def test_remote_title_updates_baseline_without_local_rename(self):
        path, pid = self.seed()
        self.api.pages[pid]["title"] = "Remote title"
        self.actions()
        self.assertTrue(path.exists())
        path.write_text(path.read_text().replace("Original", "Edited"))
        self.actions()
        self.assertEqual(self.api.pages[pid]["title"], "Remote title")

    def test_local_delete_never_deletes_or_redownloads(self):
        path, pid = self.seed()
        path.unlink()
        self.assertIn("review", self.actions())
        self.assertIn(pid, self.api.pages)
        self.assertFalse(list(self.vault.glob("*.md")))

    def test_remote_delete_does_not_delete_local(self):
        path, pid = self.seed()
        del self.api.pages[pid]
        self.assertIn("review", self.actions())
        self.assertTrue(path.exists())

    def test_duplicate_id_aborts_before_remote_writes(self):
        path, _ = self.seed()
        path.with_name("Copy.md").write_text(path.read_text())
        with self.assertRaisesRegex(BridgeError, "Duplicate"):
            self.bridge.run()
        self.assertEqual(self.api.creates, 1)
        self.assertEqual(self.api.updates, 0)

    def test_initial_mapped_difference_is_conflict(self):
        pid = self.api.create(PARENT, "Existing", "Remote")
        path = self.vault / "Existing.md"
        path.write_text(note_text("", "Local", pid))
        self.assertIn("conflict", self.actions())
        self.assertEqual(self.api.updates, 0)

    def test_dry_run_changes_neither_notes_nor_remote(self):
        path = self.vault / "New.md"
        path.write_text("New note")
        self.bridge.apply = False
        self.assertIn("upload-new", self.actions())
        self.assertEqual(path.read_text(), "New note")
        self.assertFalse(self.api.pages)
        self.assertEqual(self.bridge.db.execute("SELECT count(*) FROM notes").fetchone()[0], 0)

    def test_uncertain_create_never_retries_create(self):
        path = self.vault / "New.md"
        path.write_text("New note")
        self.api.fail_after_create = True
        self.assertIn("review", self.actions())
        self.actions()
        self.assertEqual(self.api.creates, 1)
        self.assertEqual(len(list(self.vault.glob("*.md"))), 1)

    def test_created_id_journal_recovers_when_local_file_changes(self):
        path = self.vault / "New.md"
        path.write_text("First version")
        self.api.on_create = lambda: path.write_text("Changed while creating")
        self.assertIn("review", self.actions())
        self.api.on_create = None
        self.assertIn("conflict", self.actions())
        self.assertEqual(self.api.creates, 1)
        self.assertEqual(len(list(self.vault.glob("*.md"))), 1)

    def test_local_edit_during_download_is_not_overwritten(self):
        path, pid = self.seed()
        self.api.pages[pid]["body"] = "Remote edit"
        self.api.on_get = lambda: path.write_text(path.read_text().replace("Original", "Concurrent"))
        self.assertIn("review", self.actions())
        self.assertIn("Concurrent", path.read_text())

    def test_unsupported_content_not_uploaded(self):
        path = self.vault / "Complex.md"
        path.write_text("![[drawing.png]]")
        self.assertIn("review", self.actions())
        self.assertEqual(self.api.creates, 0)

    def test_removed_mapping_not_recreated(self):
        path, _ = self.seed()
        path.write_text("Mapping was lost")
        self.assertIn("review", self.actions())
        self.assertEqual(self.api.creates, 1)

    def test_subfolders_flatten_only_on_notion_side(self):
        folder = self.vault / "Topic"
        folder.mkdir()
        path = folder / "Research.md"
        path.write_text("Nested local note")
        self.assertIn("upload-new", self.actions())
        self.assertTrue(path.exists())
        self.assertEqual(len(self.api.children(PARENT)), 1)

    def test_state_identity_and_location_guards(self):
        with self.assertRaisesRegex(BridgeError, "outside"):
            Bridge(self.vault, self.vault / "state", PARENT, self.api)
        with self.assertRaisesRegex(BridgeError, "different"):
            Bridge(self.vault, self.state, str(uuid.uuid4()), self.api)

    def test_symlink_note_rejected(self):
        source = self.root / "outside.md"
        source.write_text("Outside")
        (self.vault / "Link.md").symlink_to(source)
        with self.assertRaisesRegex(BridgeError, "Symlink"):
            self.bridge.run()

    def test_download_dry_run_does_not_create_file(self):
        self.api.create(PARENT, "Remote", "Remote body")
        self.bridge.apply = False
        self.assertIn("download-new", self.actions())
        self.assertFalse(list(self.vault.glob("*.md")))

    def test_duplicate_bridge_process_is_blocked(self):
        with (self.state / "bridge.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(BridgeError, "Another"):
                self.bridge.run()

    def test_remote_edit_between_read_and_upload_stops_upload(self):
        path, pid = self.seed()
        path.write_text(path.read_text().replace("Original", "Local"))
        real_get = self.api.get
        reads = []

        def get(page):
            reads.append(page)
            if len(reads) == 2:
                self.api.pages[pid]["body"] = "New concurrent Notion edit"
            return real_get(page)

        with patch.object(self.api, "get", side_effect=get):
            self.assertIn("review", self.actions())
        self.assertEqual(self.api.updates, 0)
        self.assertIn("Local", path.read_text())

    def test_readback_mismatch_keeps_original_baseline(self):
        path, pid = self.seed()
        original = self.bridge.db.execute("SELECT body FROM notes WHERE id=?", (pid,)).fetchone()[0]
        path.write_text(path.read_text().replace("Original", "Local"))
        real_update = self.api.update

        def update(*args, **kwargs):
            real_update(*args, **kwargs)
            self.api.pages[pid]["body"] = "Converted differently by service"

        with patch.object(self.api, "update", side_effect=update):
            self.assertIn("conflict", self.actions())
        self.assertEqual(self.bridge.db.execute("SELECT body FROM notes WHERE id=?", (pid,)).fetchone()[0], original)

    def test_hidden_state_and_unsupported_remote_content_are_not_imported(self):
        folder = self.vault / ".obsidian"
        folder.mkdir()
        (folder / "settings.md").write_text("Hidden settings")
        self.api.create(PARENT, "Complex remote", '<database url="example"/>')
        self.assertIn("review", self.actions())
        self.assertEqual(self.api.creates, 1)
        self.assertFalse(list(self.vault.glob("*.md")))

    def test_existing_local_target_not_overwritten_by_remote_discovery(self):
        pid = self.api.create(PARENT, "Remote", "Remote body")
        path = self.vault / f"Remote--{pid}.md"
        path.write_text("Existing unmapped file")
        # A settling local note is not uploaded during this pass.
        self.bridge.settle = 60
        self.assertIn("review", self.actions())
        self.assertEqual(path.read_text(), "Existing unmapped file")


class AdapterTests(unittest.TestCase):
    def test_pagination(self):
        api = Notion("test-token")
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        responses = [
            {"results": [{"type": "child_page", "id": ids[0]}], "has_more": True, "next_cursor": "next"},
            {"results": [{"type": "paragraph", "id": str(uuid.uuid4())},
                         {"type": "child_page", "id": ids[1]}], "has_more": False},
        ]
        with patch.object(api, "request", side_effect=responses) as request:
            self.assertEqual(api.children(PARENT), ids)
            self.assertIn("start_cursor=next", request.call_args[0][1])

    def test_markdown_update_shape_and_warning_handling(self):
        api = Notion("test-token")
        with patch.object(api, "request", return_value={}) as request:
            api.update(PARENT, "Old", "New")
            method, route, data = request.call_args[0]
            self.assertEqual(method, "PATCH")
            self.assertEqual(data["markdown_version"], "v2")
            self.assertEqual(data["update_content"]["content_updates"], [{"old_str": "Old", "new_str": "New"}])
            self.assertNotIn("allow_deleting_content", data)
        for response in ({"warnings": [{"code": "no_match"}]}, {"truncated": True},
                         {"unknown_block_ids": [PARENT]}):
            with self.assertRaises(BridgeError):
                api.complete(response)

    def test_frontmatter_and_ids(self):
        raw = "---\ntags: [a]\n---\nBody\n"
        head, body, pid = split_note(raw)
        result = note_text(head, body, PARENT)
        self.assertEqual(split_note(result)[2], PARENT)
        self.assertIn("tags: [a]", result)
        self.assertEqual(page_id(f"https://www.notion.so/Title-{PARENT.replace('-', '')}?x=y"), PARENT)


if __name__ == "__main__":
    unittest.main()
