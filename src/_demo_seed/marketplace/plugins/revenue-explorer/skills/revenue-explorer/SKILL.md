---
name: revenue-explorer
description: Summarise revenue, order count, and average order value over the demo orders_demo table, optionally grouped by day/week/month. Triggers on revenue, AOV, and order-trend questions over the demo dataset.
---

# Revenue Explorer

Answer revenue questions over the bundled synthetic `orders_demo` table — no SQL required.

1. Discover the shape: `agnes schema orders_demo` to confirm columns (`order_id`, `customer_id`, `order_date`, `amount`).
2. For a total or trend, run `agnes query "SELECT date_trunc('month', order_date) AS period, sum(amount) AS revenue, count(*) AS orders FROM orders_demo GROUP BY 1 ORDER BY 1"`.
3. For average order value, divide revenue by order count or use `avg(amount)`.
4. Report the result as a short table plus a one-line takeaway.

Everything runs locally against the demo data — there is no cost or production risk.
