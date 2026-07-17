"""Worker runtime for the wave-2B job queue (spec §3.3).

``registry.py`` holds the process-wide table of registered job kinds;
``runtime.py`` holds the asyncio loop that claims and runs them. Started
from ``app/main.py``'s lifespan when ``role_enabled(Role.WORKER)``.
"""
