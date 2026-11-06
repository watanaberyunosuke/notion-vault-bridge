# Validation record

Author: Harry Watson

## Run the checks

```bash
python3 -m unittest discover -s tests -v
npm ci
npm test
```

Set `BRIDGE_TEST_DATABASE_URL` to a disposable Postgres database to include the database integration tests. The integration tests initialise the bridge schema and write test data.

The unit tests exercise the actual sync engine with an in-memory Notion substitute, plus API payload shape, pagination and response-warning checks with mocked transport. They cover local and remote edits, restart continuity, dry runs, local renames, remote titles, both deletion directions, duplicate IDs, lost page-create responses, create recovery, edits during download/upload, read-back mismatches, unsupported content, process locks, existing-file collisions, identity scoping and symlink rejection.

The offline demonstration uses synthetic Notion pages and sample local Markdown. Run `python3 demo.py` to generate its report. No credentials or network requests were used. A successful offline test does not establish that the live API or either editor will preserve every format identically.

Before enabling continuous live sync, use a dedicated parent page and small pilot vault to verify a local edit, a Notion edit, a new page in each direction, a conflicting edit and a restart. Check simple Markdown round-trip behaviour with the actual Notion API and external disk-edit behaviour with the active editors.
