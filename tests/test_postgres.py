"""Integration checks, enabled only with an explicitly supplied disposable database."""
import os
from pathlib import Path
import unittest
import uuid

from bridge import BridgeError
from cloud.storage import PostgresStore, empty_state


@unittest.skipUnless(os.environ.get("BRIDGE_TEST_DATABASE_URL"), "No disposable Postgres URL configured")
class PostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.dsn = os.environ["BRIDGE_TEST_DATABASE_URL"]
        with psycopg.connect(cls.dsn, autocommit=True) as conn:
            conn.execute((Path(__file__).parent.parent / "cloud" / "schema.sql").read_text())

    def setUp(self):
        self.key = "test-" + uuid.uuid4().hex
        self.a = PostgresStore(self.dsn, self.key)
        self.b = PostgresStore(self.dsn, self.key)
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)

    def test_only_one_worker_can_own_a_lease(self):
        self.a.enqueue()
        self.assertIsNotNone(self.a.acquire())
        self.assertIsNone(self.b.acquire())
        self.a.release()
        self.assertIsNotNone(self.b.acquire())
        self.b.release()

    def test_event_during_sync_stays_pending(self):
        first = self.a.enqueue()
        self.a.acquire()
        self.b.enqueue()
        self.a.finish(first, True)
        self.assertTrue(self.b.status()["pending"])
        self.a.release()

    def test_expired_worker_cannot_save_or_release_successor(self):
        self.a.acquire()
        self.a.conn.execute("UPDATE vault_bridge.state SET lease_until=now()-interval '1 second' WHERE key=%s", (self.key,))
        self.assertIsNotNone(self.b.acquire())
        with self.assertRaisesRegex(BridgeError, "lease expired"):
            self.a.save(empty_state())
        self.a.release()
        self.b.save(empty_state())
        self.b.release()

    def test_journal_and_archive_survive_reconnect(self):
        self.a.acquire()
        state = empty_state()
        state["creates"]["id:sample"] = {"status": "pending"}
        self.a.save(state)
        self.a.archive("synthetic-backup", {"body": "Synthetic content"})
        self.a.release()
        self.assertEqual(self.b.acquire()[0], state)
        count = self.b.conn.execute("SELECT count(*) FROM vault_bridge.history WHERE vault_key=%s", (self.key,)).fetchone()[0]
        self.assertEqual(count, 1)
        self.b.release()

    def test_private_schema_and_rls(self):
        flags = self.a.conn.execute("SELECT relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                                    "WHERE n.nspname='vault_bridge' AND c.relkind='r'").fetchall()
        self.assertTrue(flags)
        self.assertTrue(all(r[0] for r in flags))
        grants = self.a.conn.execute("SELECT has_schema_privilege('vault_bridge', 'USAGE')").fetchone()
        self.assertTrue(grants[0])
        # PUBLIC has no explicit schema grants; only the database owner can use it by default.
        acl = self.a.conn.execute("SELECT nspacl::text FROM pg_namespace WHERE nspname='vault_bridge'").fetchone()[0]
        self.assertNotIn("=U/", acl)

    def test_verification_candidate_cannot_be_replaced(self):
        self.a.verification("synthetic-one")
        self.b.verification("synthetic-two")
        self.assertEqual(self.b.verification(), "synthetic-one")


if __name__ == "__main__":
    unittest.main()
