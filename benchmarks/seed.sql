CREATE TABLE IF NOT EXISTS items (
  id SERIAL PRIMARY KEY,
  username VARCHAR(100) NOT NULL,
  community VARCHAR(100) NOT NULL,
  content TEXT NOT NULL,
  upvotes INTEGER NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  title VARCHAR(255),
  author VARCHAR(100)
);
CREATE INDEX IF NOT EXISTS ix_items_id ON items (id);
CREATE TABLE IF NOT EXISTS fixture_items (LIKE items INCLUDING DEFAULTS INCLUDING CONSTRAINTS);
TRUNCATE fixture_items;
INSERT INTO fixture_items (id, username, community, content, upvotes, created_at, title, author)
SELECT i, '@bench_' || (i % 500),
       (ARRAY['r/kubernetes','r/azure','r/terraform','r/gitops'])[(i % 4) + 1],
       left('Deterministic synthetic benchmark record ' || i || ' ' || repeat('x', 512), 512),
       i % 100,
       TIMESTAMPTZ '2026-01-01 00:00:00+00' + i * INTERVAL '1 second',
       'Synthetic benchmark fixture', '@bench_' || (i % 500)
FROM generate_series(1,10000) AS t(i);
TRUNCATE items RESTART IDENTITY;
INSERT INTO items SELECT * FROM fixture_items;
SELECT setval(pg_get_serial_sequence('items','id'), 10000, true);
ANALYZE items;
SELECT count(*) AS fixture_rows, pg_size_pretty(pg_total_relation_size('items')) AS table_and_index_size FROM items;
