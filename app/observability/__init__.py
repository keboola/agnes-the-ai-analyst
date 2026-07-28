"""Prometheus metrics for the three-plane deployment model.

`app.observability.metrics` is the single module today (wave 2D task 1: the
`/metrics` endpoint + core HTTP request metrics + middleware). Later wave 2D
tasks add job-queue/worker metrics and coordination/readiness gauges to the
same module rather than splitting further — see
`docs/superpowers/plans/2026-07-17-three-plane-wave2d-observability.md`.
"""
