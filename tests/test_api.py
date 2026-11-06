from http.server import HTTPServer
import json
import os
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from api.sync import handler, run_pending
from cloud.storage import empty_state


class RuntimeWrappedHandler(handler):
    # Vercel adds this method above the application's class in its MRO.
    def handle_request(self):
        getattr(self, "do_" + self.command)()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), RuntimeWrappedHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.env = patch.dict(os.environ, {"CRON_SECRET": "synthetic-admin-key"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def request(self, data=None, key=None):
        headers = {"Authorization": "Bearer " + key} if key else {}
        request = Request("http://127.0.0.1:" + str(self.server.server_port),
            data=json.dumps(data).encode() if data is not None else None, headers=headers)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def test_unauthenticated_status_is_private(self):
        self.assertEqual(self.request()[0], 401)

    def test_authenticated_unconfigured_status_reports_missing_setup(self):
        code, value = self.request(key="synthetic-admin-key")
        self.assertEqual(code, 503)
        self.assertIn("DATABASE_URL", value["missing"])

    def test_unknown_post_action_is_rejected(self):
        self.assertEqual(self.request({"action": "erase"}, "synthetic-admin-key")[0], 400)

    def test_preview_cannot_write_or_consume_real_pending_work(self):
        values = {"SYNC_ENABLED": "true", "DROPBOX_VAULT_PATH": "/obsidian/example-vault",
            "DROPBOX_APP_KEY": "synthetic", "DROPBOX_APP_SECRET": "synthetic",
            "DROPBOX_REFRESH_TOKEN": "synthetic", "DROPBOX_ACCOUNT_ID": "synthetic",
            "NOTION_TOKEN": "synthetic", "NOTION_PARENT_ID": "12345678-1234-4234-8234-123456789abc"}
        store = MagicMock()
        store.acquire.return_value = (empty_state(), 7, 0)
        with patch.dict(os.environ, values), patch("api.sync.CloudBridge") as engine:
            engine.return_value.run.return_value = []
            result = run_pending(store, dry_run=True)
        self.assertEqual(result["status"], "preview")
        self.assertFalse(engine.call_args.kwargs["enabled"])
        store.finish.assert_not_called()
        store.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
