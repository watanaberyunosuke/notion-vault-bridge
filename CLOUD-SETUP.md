# Cloud setup

Author: Harry Watson

The cloud bridge runs as short-lived Vercel Functions on Vercel Hobby. Dropbox and Notion webhooks enqueue reconciliation; a daily Vercel cron catches missed events. Postgres stores common snapshots, page mappings, creation journals, pending work and backups. The cloud version does not use the local SQLite database.

## 1. Database

Connect a Neon Postgres database through the Vercel Marketplace. Use the direct connection URL (typically injected as `DATABASE_URL_UNPOOLED`) as the bridge's `DATABASE_URL`. Use a dedicated database for the bridge.

Install the Vercel CLI and run `vercel link` from the project directory to select your project. Then configure and initialise the database.

From the project directory:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m cloud.configure credentials
.venv/bin/python -m cloud.configure init-db
```

The setup prompt reads secrets without echoing them. Its `.env.local` is private, ignored by Git and excluded from deployment uploads. Do not paste tokens or database passwords into chat. Run the helper from a project directory outside Dropbox if you want local credentials to stay off Dropbox.

## 2. Notion connection

Create an [internal Notion connection](https://www.notion.so/profile/integrations), with read, insert and update content capabilities and the corresponding property capabilities. Add that connection to your chosen Notion parent page through the page's connections menu.

Enter the connection token into the credentials helper or the Vercel Production `NOTION_TOKEN` variable.

## 3. Dropbox app

Create a [Dropbox developer app](https://www.dropbox.com/developers/apps):

1. Choose Scoped access and Full Dropbox (an App Folder app cannot access an existing vault outside the app folder).
2. Enable only `account_info.read`, `files.metadata.read`, `files.content.read` and `files.content.write`.
3. Enter its app key and app secret through the credentials helper.
4. Run the offline-access OAuth flow:

```bash
.venv/bin/python -m cloud.configure dropbox
```

The helper opens Dropbox's authorisation page, asks for the authorisation code without echoing it, then stores the refresh token and account ID. The deployed service refreshes its short-lived access token automatically. Although Dropbox grants account-wide API access, the bridge's file operations are restricted to the configured vault and exclude hidden folders.

## 4. Configure and deploy

Use the credentials helper to enter the Notion parent page URL and the Dropbox vault path (for example, `/obsidian/example-vault`). Set `DROPBOX_NAMESPACE_ID` when the vault is in a namespace other than the account default.

Set `BRIDGE_ORIGIN` to the canonical production origin returned by Vercel (for example, `https://your-project.vercel.app`). Leave `SYNC_ENABLED=false` initially. The helper creates random administrator and webhook-setup keys; retain them securely.

```bash
.venv/bin/python -m cloud.configure credentials
.venv/bin/python -m cloud.configure push
vercel deploy --prod --yes
.venv/bin/python -m cloud.configure status
.venv/bin/python -m cloud.configure preview
```

`push` sends values to Vercel through stdin, not command-line arguments. Production credentials are stored as Vercel secrets. A redeployment is required after environment changes.

The preview uses real API reads but does not create or modify notes/pages. A new unmapped note is always new; filenames are not used to infer an existing Notion identity.

After reviewing the preview, enable writes:

```bash
.venv/bin/python -m cloud.configure enable
.venv/bin/python -m cloud.configure push
vercel deploy --prod --yes
```

The `preview` helper always runs without note/page writes, even when continuous sync is enabled. Run `.venv/bin/python -m cloud.configure sync` for a manual applied pass. To pause writes, set `SYNC_ENABLED=false` in Vercel and redeploy.

## 5. Webhooks

### Dropbox

In the Dropbox app settings add:

```text
https://YOUR_PRODUCTION_ORIGIN/api/webhook?provider=dropbox
```

The endpoint answers the challenge with plain text. Subsequent events must carry a valid `X-Dropbox-Signature` and include the configured Dropbox account ID. Only then does the bridge persist a pending sync and schedule the worker using Vercel's `waitUntil` helper.

### Notion

In the internal connection's Webhooks tab, create a subscription using:

```text
https://YOUR_PRODUCTION_ORIGIN/api/webhook?provider=notion&setup=YOUR_NOTION_SETUP_KEY
```

Get the setup key from your private environment configuration. It is only used to receive the initial verification candidate; it does not authorise sync operations. Subscribe to page creation, content changes, property changes, moves, deletion and restoration.

Notion sends a one-time verification token. The bridge stores that candidate privately; it is not written to logs. Retrieve it into the macOS clipboard:

```bash
.venv/bin/python -m cloud.configure verification
```

Paste it into Notion's Verify form. The helper also saves it locally as `NOTION_VERIFICATION_TOKEN`; push and redeploy again. The token becomes the key used to verify `X-Notion-Signature`. Never accept a candidate as a signing key until the Notion verification succeeds.

## 6. Live verification

With all credentials configured, run one applied sync and verify:

1. Direct child pages appear at the vault root. Nested pages appear in folders named for their parent notes.
2. The note containing a wiki link points to its corresponding Notion child page.
3. A local edit appears in Notion after Dropbox uploads it.
4. A Notion edit appears locally after Dropbox downloads it.
5. A new Notion child becomes one local `.md` note.
6. Competing edits produce a conflict with snapshots retained in Postgres.
7. A second sync or worker restart does not create duplicate pages.

Canvas, Bases, hidden configuration and trash remain outside the sync. Mixed-format and nested pages import as Notion reference notes. The bridge downloads supported attachments and keeps live database links. Edit reference pages in Notion. Heading and path wiki links are reported for review. Plain `[[Note]]` and `[[Note|label]]` links are converted using mapped page IDs, while the local note's original syntax is retained on uploads. Incoming Notion links may use the equivalent alias form rather than the original exact spelling.

Remote title changes are tracked without renaming local filenames. Local filename changes update Notion titles using the stable Dropbox file ID. Neither deletion direction deletes the other copy automatically.

## Operations and recovery

- `GET /api/sync` with `Authorization: Bearer CRON_SECRET`: protected readiness and sync status.
- `POST /api/sync`, body `{"action":"run","force":true}`: protected manual reconciliation.
- `GET /api/cron`: protected daily reconciliation; Vercel supplies the cron secret.
- The public landing page reveals no note content or sync activity.
- Both webhooks commit pending work before acknowledging it. A background timeout leaves work pending for another event, manual run or daily reconciliation. This is not an exactly-once queue and does not promise immediate retries after every failure.
- Each invocation uses an 80-second work budget and checkpoints pending work for the next request. A 360-second fenced lease prevents overlapping writers; Dropbox revisions reject stale file writes. Notion checks reduce concurrent-edit risk but are not an atomic cross-service transaction.
- Never blindly retry a timed-out page creation: its journal stops duplicate creation. Inspect Notion and put the existing page ID into the original note if the response was lost.
- Database snapshots and history contain private notes. Monitor their growth and back them up; automated retention cleanup is not included.
- Do not run the original local `bridge.py --watch` against the same vault while the cloud worker is active.
- Hosting remains within free limits while usage fits the Vercel Hobby and Neon Free quotas. Check the protected status endpoint if sync stops.

## Sources

- [Vercel Hobby cron limits](https://vercel.com/docs/cron-jobs/usage-and-pricing)
- [Vercel waitUntil](https://vercel.com/docs/functions/functions-api-reference)
- [Dropbox webhooks and signatures](https://docs.dropboxapi.com/dropbox-api/docs/webhooks)
- [Dropbox OAuth and refresh tokens](https://docs.dropboxapi.com/dropbox-api/docs/oauth)
- [Notion webhook verification](https://developers.notion.com/reference/webhooks)
- [Neon on the Vercel Marketplace](https://vercel.com/marketplace/neon)
