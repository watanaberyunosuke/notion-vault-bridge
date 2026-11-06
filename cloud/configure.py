"""Interactive credential setup; secrets stay local and are sent through stdin."""
import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import subprocess
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import webbrowser

from bridge import page_id


ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env.local"
VERCEL = "vercel"
ALLOWED = {"NOTION_PARENT_ID", "DROPBOX_VAULT_PATH", "DROPBOX_NAMESPACE_ID", "SYNC_ENABLED",
           "DATABASE_URL", "NOTION_TOKEN", "DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN",
           "DROPBOX_ACCOUNT_ID", "CRON_SECRET", "NOTION_SETUP_KEY", "NOTION_VERIFICATION_TOKEN", "BRIDGE_ORIGIN"}


def load():
    if not ENV_FILE.exists():
        return {}
    values = {}
    for line in ENV_FILE.read_text().splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in ALLOWED:
            values[key] = json.loads(value)
    return values


def save(values):
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(ENV_FILE, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("".join(f"{key}={json.dumps(value)}\n" for key, value in values.items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["credentials", "dropbox", "push", "init-db", "status", "preview", "sync", "enable", "verification"])
    args = parser.parse_args()
    values = load()
    values.setdefault("SYNC_ENABLED", "false")
    values.setdefault("CRON_SECRET", secrets.token_urlsafe(32))
    values.setdefault("NOTION_SETUP_KEY", secrets.token_urlsafe(32))
    if args.action == "credentials":
        parent = input("Notion parent page URL or ID (blank keeps existing): ").strip()
        if parent:
            values["NOTION_PARENT_ID"] = page_id(parent)
        for key in ("DROPBOX_VAULT_PATH", "DROPBOX_NAMESPACE_ID"):
            value = input(f"{key} (blank keeps existing): ").strip()
            if value:
                values[key] = value
        for key in ("DATABASE_URL", "NOTION_TOKEN", "DROPBOX_APP_KEY", "DROPBOX_APP_SECRET"):
            value = getpass.getpass(key + " (blank keeps existing): ")
            if value:
                values[key] = value
        origin = input("Production origin (https://...; blank keeps existing): ").strip()
        if origin:
            values["BRIDGE_ORIGIN"] = origin.rstrip("/")
        save(values)
        print("Credentials saved privately to .env.local; no values printed.")
    elif args.action == "dropbox":
        query = urlencode({"client_id": values["DROPBOX_APP_KEY"], "response_type": "code",
            "token_access_type": "offline",
            "scope": "account_info.read files.metadata.read files.content.read files.content.write"})
        url = "https://www.dropbox.com/oauth2/authorize?" + query
        print("Authorise the Dropbox app in the browser. If it does not open, use:\n" + url)
        webbrowser.open(url)
        code = getpass.getpass("Dropbox authorisation code: ")
        data = urlencode({"code": code, "grant_type": "authorization_code",
            "client_id": values["DROPBOX_APP_KEY"], "client_secret": values["DROPBOX_APP_SECRET"]}).encode()
        try:
            with urlopen(Request("https://api.dropboxapi.com/oauth2/token", data=data), timeout=20) as response:
                token = json.load(response)
        except Exception:
            raise SystemExit("Dropbox authorisation failed; no credentials were printed") from None
        values["DROPBOX_REFRESH_TOKEN"] = token["refresh_token"]
        values["DROPBOX_ACCOUNT_ID"] = token["account_id"]
        save(values)
        print("Dropbox refresh token and account ID saved privately.")
    elif args.action == "push":
        for key, value in values.items():
            result = subprocess.run([VERCEL, "env", "add", key, "production", "--force", "--yes",
                "--sensitive"], input=value, text=True, cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode:
                raise SystemExit(f"Could not upload {key}; inspect Vercel configuration")
            print("Configured " + key)
        print("Redeploy production to apply these values.")
    elif args.action == "init-db":
        import psycopg
        try:
            with psycopg.connect(values["DATABASE_URL"], connect_timeout=5) as connection:
                connection.execute((ROOT / "cloud" / "schema.sql").read_text())
            print("Private sync schema initialised.")
        except Exception:
            raise SystemExit("Database initialisation failed; check the private connection settings") from None
    elif args.action == "enable":
        values["SYNC_ENABLED"] = "true"
        save(values)
        print("Writes enabled locally. Run push and redeploy to enable them in production.")
    else:
        action = {"status": "status", "preview": "preview", "sync": "run", "verification": "verification"}[args.action]
        data = {"action": action}
        if args.action == "sync":
            data["force"] = True
        request = Request(values["BRIDGE_ORIGIN"] + "/api/sync", data=json.dumps(data).encode(),
            headers={"Authorization": "Bearer " + values["CRON_SECRET"], "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=260) as response:
                result = json.load(response)
        except Exception:
            raise SystemExit("Protected request failed; check deployment access and configuration") from None
        if args.action == "verification":
            token = result.get("verification_token")
            if not token:
                raise SystemExit("No verification request received yet")
            values["NOTION_VERIFICATION_TOKEN"] = token
            save(values)
            # Use the system clipboard, not terminal output or logs, for Notion's confirmation form.
            subprocess.run(["pbcopy"], input=token, text=True, check=True)
            print("Verification token saved and copied to the clipboard. Paste it into Notion's Verify form.")
        else:
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
