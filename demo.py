#!/usr/bin/env python3
"""Exercise the real sync engine against synthetic Notion pages, entirely offline."""
import argparse
import json
from pathlib import Path
import shutil
import tempfile

from bridge import Bridge, split_note
from mock_notion import FakeNotion


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", help="New directory to retain demo notes and conflict reports")
    args = parser.parse_args()
    root = Path(args.directory).resolve() if args.directory else Path(tempfile.mkdtemp(prefix="notion-vault-demo-"))
    if args.directory:
        root.mkdir(parents=True, exist_ok=False)
    vault = root / "sample-vault"
    shutil.copytree(Path(__file__).parent / "examples" / "sample-vault", vault)
    api = FakeNotion()
    parent = "11111111-1111-1111-1111-111111111111"
    bridge = Bridge(vault, root / "state", parent, api, apply=True, settle=0)
    report = {"data": "Synthetic Notion pages; local sample files; no network calls", "steps": []}

    def run(label):
        events = bridge.run()
        # Keep the report portable by showing vault-relative paths.
        for event in events:
            event["path"] = event["path"].replace(str(root) + "/", "")
        report["steps"].append({"step": label, "events": events})
        return events

    run("Create one direct Notion child per local note")
    note = vault / "Research.md"
    pid = split_note(note.read_text())[2]
    note.write_text(note.read_text().replace("Initial findings", "Revised findings"))
    assert any(e["action"] == "upload" for e in run("Edit through a local editor"))
    api.pages[pid]["body"] += "\n\nAdded in synthetic Notion."
    assert any(e["action"] == "download" for e in run("Edit in synthetic Notion"))
    api.create(parent, "Remote note", "Created in synthetic Notion.")
    assert any(e["action"] == "download-new" for e in run("Create a new Notion child"))
    renamed = vault / "Research renamed.md"
    note.rename(renamed)
    assert any(e["action"] == "upload" for e in run("Rename local note; preserve page identity"))
    renamed.write_text(renamed.read_text() + "\nLocal competing change.\n")
    api.pages[pid]["body"] += "\n\nRemote competing change."
    assert any(e["action"] == "conflict" for e in run("Preserve conflicting edits"))
    (vault / "Welcome.md").unlink()
    assert any("deletion" in e["detail"] for e in run("Retain Notion page after local deletion"))
    report["synthetic_notion_pages"] = list(api.pages.values())
    (root / "demo-report.json").write_text(json.dumps(report, indent=2) + "\n")
    bridge.db.close()
    print(f"Offline demo passed. Report: {root / 'demo-report.json'}")


if __name__ == "__main__":
    main()
