-- Private server-only schema. Apply once to a dedicated database.
CREATE SCHEMA IF NOT EXISTS vault_bridge;
REVOKE ALL ON SCHEMA vault_bridge FROM PUBLIC;
CREATE TABLE IF NOT EXISTS vault_bridge.state (
    key text PRIMARY KEY,
    payload jsonb NOT NULL DEFAULT '{"notes":{},"creates":{},"imports":{},"issues":{}}',
    generation bigint NOT NULL DEFAULT 0,
    processed bigint NOT NULL DEFAULT 0,
    lease uuid,
    lease_until timestamptz,
    last_run timestamptz,
    last_error text
);
CREATE TABLE IF NOT EXISTS vault_bridge.history (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vault_key text NOT NULL REFERENCES vault_bridge.state(key),
    label text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS vault_bridge.verification (
    vault_key text PRIMARY KEY REFERENCES vault_bridge.state(key),
    token text NOT NULL
);
ALTER TABLE vault_bridge.state ENABLE ROW LEVEL SECURITY;
ALTER TABLE vault_bridge.history ENABLE ROW LEVEL SECURITY;
ALTER TABLE vault_bridge.verification ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON ALL TABLES IN SCHEMA vault_bridge FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA vault_bridge FROM PUBLIC;
