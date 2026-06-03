---
name: cohort-helper
description: Build customer cohorts and measure repeat purchases by joining the demo orders_demo and customers_demo tables. Triggers on cohort, retention, and repeat-purchase questions over the demo dataset.
---

# Cohort Helper

Group demo customers into cohorts and measure repeat-purchase behaviour.

1. Confirm columns: `agnes schema orders_demo` and `agnes schema customers_demo` (`customer_id`, `name`, `country`).
2. Repeat purchasers: `agnes query "SELECT count(*) FROM (SELECT customer_id FROM orders_demo GROUP BY 1 HAVING count(*) > 1)"`.
3. Cohort by country: join on `customer_id` and group by `country`, e.g. `agnes query "SELECT c.country, count(DISTINCT o.customer_id) AS customers, count(*) AS orders FROM orders_demo o JOIN customers_demo c USING (customer_id) GROUP BY 1 ORDER BY 2 DESC"`.
4. Summarise each cohort with its size and repeat rate.

All analysis is local against the bundled synthetic dataset.
