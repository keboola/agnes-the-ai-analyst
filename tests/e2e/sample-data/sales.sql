-- Sales fixture for the E2E chat suite.
--
-- 10k rows spanning 90 days, three regions (A/B/C), amounts in cents.
-- Small enough that `agnes describe` returns instantly; structured enough
-- that aggregations (sum by region, count by date) produce meaningful
-- assistant responses.

CREATE TABLE IF NOT EXISTS sales AS
SELECT
    i                                                  AS id,
    DATE '2026-01-01' + ((i % 90) || ' days')::INTERVAL AS order_date,
    (['A', 'B', 'C'])[1 + (i % 3)]                      AS region,
    100 + (i * 13) % 9000                               AS amount_cents
FROM range(10000) t(i);
