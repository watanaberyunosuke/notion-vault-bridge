# Notion vault bridge

Author: Harry Watson

The Vercel/Dropbox/Postgres implementation is documented in [CLOUD-SETUP.md](CLOUD-SETUP.md). The instructions below describe the original local-file pilot; do not run both workers on the same vault.

A local Python service that maps one Markdown vault to one existing Notion parent page. Every `.md` note, including notes in subfolders, maps to one **direct child** Notion page. Obsidian and OpenKnowledge edit the same local folder. Notion holds the canonical successfully synced content; offline local changes remain pending until uploaded.

This is a tested pilot implementation. The tests and demonstration use a synthetic Notion service. No real Notion workspace, Dropbox folder, Obsidian app or OpenKnowledge session was changed or validated during development.

## Requirements

- macOS or Linux, Python 3.10 or later. No third-party Python packages required. The process lock uses `fcntl`, so Windows is not currently supported.
- A locally available vault folder. If using Dropbox, keep the folder available offline.
- A Notion internal connection with read, insert and update content access, and the required property capabilities. Grant it access to the chosen parent page and its children.
- One bridge instance and one persistent local state directory per vault. Keep state outside the vault and outside Dropbox. Do not run competing bridge instances on other computers.

## Run the offline demonstration

From this package directory:

```bash
python3 demo.py
python3 -m unittest discover -s tests -v
```

The demo creates temporary sample files, runs the actual sync engine against an in-memory Notion substitute, and prints the report location. It makes no network requests and needs no credentials. To retain the result at a chosen **new** directory:

```bash
python3 demo.py --directory /absolute/path/to/new-demo-folder
```

It exercises page creation, local edits, remote edits, new remote children, local renaming, competing edits and local deletion. The report identifies all Notion data as synthetic.

## Connect a real pilot vault

1. Copy a few ordinary Markdown notes into a pilot vault. Use a dedicated, empty parent page in Notion for the first live run.
2. Create an [internal Notion connection](https://developers.notion.com/guides/connections-and-tokens/internal-connections) and grant it access to that page.
3. Set its token locally. In zsh, this reads it without displaying it or saving the token itself in shell history:

```bash
read -s 'NOTION_TOKEN?Notion connection token: '
export NOTION_TOKEN
```

Do not paste credentials into chat or store them in your vault.

4. Run a dry run, replacing the example paths and parent URL:

```bash
python3 bridge.py \
  --vault /absolute/path/to/pilot-vault \
  --parent 'YOUR_NOTION_PARENT_PAGE_URL' \
  --state-dir /absolute/path/to/local-bridge-state
```

The default is a dry run: no note content or Notion pages are changed. It does create the local state directory, its identity record and a lock file. `upload-new` means a child would be created; `download-new` means a local note would be created.

5. Apply one pass:

```bash
python3 bridge.py \
  --vault /absolute/path/to/pilot-vault \
  --parent 'YOUR_NOTION_PARENT_PAGE_URL' \
  --state-dir /absolute/path/to/local-bridge-state \
  --apply
```

6. After checking the result in Notion and the local editor, repeat automatically:

```bash
python3 bridge.py \
  --vault /absolute/path/to/pilot-vault \
  --parent 'YOUR_NOTION_PARENT_PAGE_URL' \
  --state-dir /absolute/path/to/local-bridge-state \
  --apply --watch --interval 60
```

Stop with Control-C. This command stays in the foreground; no background service is installed. The bridge waits at least two seconds after a local note was last modified before processing it. A one-shot run may report `pending`; run again after the note settles.

## Behaviour

| Situation | Result |
|---|---|
| New local note | Create one direct Notion child, then add `notion_page_id` to local frontmatter |
| New direct Notion child | Download as a root-level Markdown file with title and UUID in its filename |
| Local content changed | Recheck Notion, upload, read back and verify before accepting a new baseline |
| Notion content changed | Back up the local note, then update it if it has not changed during the pass |
| Both copies changed | Preserve both and report `conflict`; do not overwrite either |
| Local file renamed or moved | Retain its Notion page ID; a changed filename stem updates the Notion title |
| Notion title changed | Retain the local filename and update the title baseline, avoiding broken local paths |
| Local file deleted | Retain the Notion page and report it; do not automatically download it again |
| Notion page deleted, moved or inaccessible | Retain the local note and report it; do not recreate the page |
| Duplicate page ID in notes | Abort the pass before content writes |
| Missing ID on a previously tracked path | Report it; do not create another page |

The parent page's body is not synced or replaced. Existing direct child pages are discovered through the paginated blocks API. Grandchildren, Notion Folders and databases are outside this first version. Hidden files/folders and non-`.md` files are ignored; symlink notes/folders are rejected.

Local YAML frontmatter is preserved and excluded from the Notion page body. Its fields are not mapped to database properties. Do not remove or duplicate `notion_page_id`. When copying a note to create a new page, deliberately remove its ID from the copy first.

Do not rename a pending new note until its creation has completed. If a previously tracked note loses its ID and is also renamed, the bridge cannot reliably recover its identity. Restore the ID before syncing.

## Supported content and limits

The initial format is ordinary Markdown: paragraphs, headings, lists, code, block quotes and HTTP/mail/heading links. It normalises CRLF line endings and trailing newlines, but does not attempt general syntax conversion. Different normalisations returned by Notion result in review rather than a silently accepted baseline.

Wiki links, images/embeds, local or relative links, tables, raw HTML/Notion custom tags, horizontal rules and detected plugin syntax are flagged for review. Attachments, property mapping, automatic link rewriting and deletion propagation are not implemented. The format checks are conservative, not a complete Markdown parser; use simple pilot pages first. External links to Notion pages remain URLs rather than local wiki links.

Notion responses containing warnings, unknown blocks or truncation stop the affected operation. API writes may already have applied before a warning or verification mismatch is detected. Backups and the previous baseline remain available; review the actual Notion result before continuing.

The bridge uses Notion's Markdown `v2` API, version header `2026-03-11`, checks the latest page before uploading, and uses a full-body search-and-replace rather than unconditional replacement when content exists. It never enables deletion of child pages/databases. Title and body writes are separate API operations; partial completion is reported on a later comparison.

There is **no cross-app atomic lock**. A Notion user can still edit between requests; an external editor can edit between the final filesystem check and replacement. The bridge reduces these windows, keeps snapshots and detects many competing edits, but is not a real-time collaborative merge service. Avoid editing the same note simultaneously in different apps or computers.

OpenKnowledge documents external disk-edit reconciliation. This bridge is an external filesystem writer; it does not use OpenKnowledge MCP or provide MCP edit attribution. Live compatibility with an active OpenKnowledge editor still needs a pilot check. For OpenKnowledge projects with a custom content directory, point `--vault` at the actual notes directory. Do not initialise OpenKnowledge in this package directory; open the separate note vault instead.

## Conflicts and recovery

Reports are printed as JSON events. Exit codes for one-shot runs are `0` for no review findings (including a pending note), `2` for conflicts/review, and `1` for a fatal failure. Watch mode continues across per-page review findings and stops on a fatal failure.

State contains:

```text
state.sqlite3                  page mappings, common baseline, creation journal
history/                       snapshots before replacing notes or uploading edits
conflicts/<page-id>/            preserved local content and remote JSON snapshots
```

These files contain your knowledge content: treat them as private and back them up. Losing state also loses the common baseline. The bridge then refuses to choose between differing mapped copies automatically. Never delete state to clear a conflict.

To resolve a conflict, compare its two saved versions. Keep the ID/frontmatter and make the local note body match the desired final Notion body; update Notion with the chosen result if necessary. Once both bodies agree and no competing local rename remains, the next pass accepts that shared version. Old reports remain as history.

If a page-create request times out after reaching Notion, the bridge records an uncertain attempt and does not retry creation. New remote discovery pauses to avoid bringing back the uncertain page as a second note. Find the page in Notion and add its actual `notion_page_id` to the original local note, then reconcile the bodies. If no page was created, retain the journal and inspect the attempt before clearing it; there is deliberately no automatic retry/reset command.

To adopt an existing note/page pair, add the page ID to the note's frontmatter and make the bodies agree before the first applied run. The page must be a direct child of the configured parent. A fresh local note with no ID is always considered new; the bridge never guesses identity from matching titles.

## Sources

- [Notion Markdown API](https://developers.notion.com/guides/data-apis/working-with-markdown-content)
- [Read Markdown, truncation and unknown blocks](https://developers.notion.com/reference/retrieve-page-markdown)
- [Update Markdown and child-content protection](https://developers.notion.com/reference/update-page-markdown)
- [List child blocks](https://developers.notion.com/reference/get-block-children)
- [OpenKnowledge with Obsidian vaults](https://openknowledge.ai/docs/migrate/obsidian)
- [OpenKnowledge external disk-edit handling](https://openknowledge.ai/docs/reference/what-open-knowledge-writes)
- [Obsidian file storage](https://obsidian.md/help/data-storage)
