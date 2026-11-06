"""Authenticated Vercel Python function: queue, reconcile and inspect a vault."""
from http.server import BaseHTTPRequestHandler
import hmac
import json
import os
from urllib.parse import urlsplit, parse_qs

from bridge import BridgeError, Notion
from cloud.engine import CloudBridge
from cloud.storage import Dropbox, store_from_env


REQUIRED = ("DATABASE_URL", "NOTION_TOKEN", "NOTION_PARENT_ID", "DROPBOX_VAULT_PATH",
            "DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN", "DROPBOX_ACCOUNT_ID",
            "CRON_SECRET")


def run_pending(store, dry_run=False):
    record = store.acquire()
    if record is None:
        return {"status": "busy", "events": []}
    state, generation, processed = record
    try:
        if generation <= processed and not dry_run:
            return {"status": "idle", "events": []}
        enabled = not dry_run and os.environ.get("SYNC_ENABLED") == "true"
        target = state.get("progress_generation", generation)
        if enabled:
            state["progress_generation"] = target
            store.save(state)
        dbx = Dropbox(os.environ["DROPBOX_VAULT_PATH"], os.environ["DROPBOX_APP_KEY"],
            os.environ["DROPBOX_APP_SECRET"], os.environ["DROPBOX_REFRESH_TOKEN"],
            os.environ["DROPBOX_ACCOUNT_ID"], os.environ.get("DROPBOX_NAMESPACE_ID"))
        # Leave time for an in-flight API call and response at the request deadline.
        bridge = CloudBridge(dbx, Notion(os.environ["NOTION_TOKEN"]), store,
            os.environ["NOTION_PARENT_ID"], state, enabled=enabled, budget=80)
        events = bridge.run()
        state["last_events"] = events
        if bridge.complete and enabled:
            state.pop("progress_generation", None)
        store.save(state)
        if not dry_run:
            store.finish(target, bridge.complete)
    finally:
        store.release()
    if dry_run:
        return {"status": "preview", "events": events}
    return {"status": "pending" if store.status()["pending"] else "complete", "events": events}


class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        # Do not log request bodies, query parameters, credentials or note content.
        return

    def reply(self, code, value):
        payload = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.dispatch_bridge("status")

    def do_POST(self):
        self.dispatch_bridge(None)

    def dispatch_bridge(self, action):
        secret = os.environ.get("CRON_SECRET", "")
        supplied = self.headers.get("Authorization", "")
        if not secret or not hmac.compare_digest(supplied, "Bearer " + secret):
            self.reply(401, {"error": "Unauthorised"})
            return
        missing = [key for key in REQUIRED if not os.environ.get(key)]
        # Verification/status only need the database; credentials are added during setup.
        data = {}
        if action is None:
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size > 65536 or size < 0:
                    raise ValueError()
                data = json.loads(self.rfile.read(size))
                action = data.get("action", "run")
            except (ValueError, AttributeError):
                self.reply(400, {"error": "Invalid request"})
                return
        if action not in {"status", "verification", "enqueue", "run", "preview", "cron"}:
            self.reply(400, {"error": "Unknown action"})
            return
        essential = ("DATABASE_URL", "NOTION_PARENT_ID", "DROPBOX_VAULT_PATH")
        if any(not os.environ.get(key) for key in essential):
            self.reply(503, {"status": "setup-required", "missing": missing})
            return
        try:
            with store_from_env() as store:
                if action == "status":
                    result = store.status()
                    result["missing"] = missing
                    result["sync_enabled"] = os.environ.get("SYNC_ENABLED") == "true"
                elif action == "verification":
                    token = data.get("token")
                    if token is not None and (not isinstance(token, str) or len(token) > 1000):
                        raise BridgeError("Invalid verification token")
                    result = {"verification_token": store.verification(token)}
                elif action == "enqueue":
                    result = {"generation": store.enqueue()}
                elif missing:
                    self.reply(503, {"status": "setup-required", "missing": missing})
                    return
                else:
                    if action == "cron" or (action == "run" and data.get("force")):
                        store.enqueue()
                    result = run_pending(store, dry_run=action == "preview")
                self.reply(200, result)
        except BridgeError as exc:
            self.reply(409, {"error": str(exc)})
        except Exception as exc:
            # Database/transport exceptions can contain DSNs or credentials.
            print(f"Sync request failed: {type(exc).__name__}", flush=True)
            self.reply(503, {"error": "Sync unavailable; check database setup, service access and private diagnostics"})
