"""Session pipeline framework — shared utilities, contract, and per-processor
runner for any service that wants to extract data from Claude Code session
transcripts in /data/user_sessions/.

Processors live in services/session_processors/. Each one declares its own
cadence and its own state row keyed by (processor_name, session_file), so
adding a new processor today retroactively reprocesses all historical sessions
for that processor only, and a slow or failing processor cannot block any other.
"""
