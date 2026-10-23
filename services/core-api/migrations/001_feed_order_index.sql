BEGIN;
SELECT pg_advisory_xact_lock(hashtext('threadops:feed-order-index'));
CREATE INDEX IF NOT EXISTS ix_items_created_at_desc ON items (created_at DESC);
COMMIT;
