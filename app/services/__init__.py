"""FastAPI-layer services that compose repos but stay out of HTTP routing.

Used by ``app/api/*`` handlers when business logic spans multiple repos and
doesn't fit on a single repository class. Distinct from the top-level
``services/`` package which holds standalone background services (scheduler,
telegram_bot, …).
"""
