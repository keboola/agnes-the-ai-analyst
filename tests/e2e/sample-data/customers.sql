-- Customers fixture for the E2E chat suite.
--
-- 500 rows across four countries — used for JOIN scenarios with `sales`
-- and for "describe a few rows" prompts.

CREATE TABLE IF NOT EXISTS customers AS
SELECT
    i                                  AS id,
    'customer_' || i                   AS name,
    (['US', 'UK', 'CZ', 'DE'])[1 + (i % 4)] AS country
FROM range(500) t(i);
