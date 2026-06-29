"""Spike: validate E2B pause -> resume -> reattach-to-running-process.

Standalone script (not pytest-collected). Validates the riskiest assumption
of docs/superpowers/specs/2026-06-10-chat-session-pause-resume-design.md:
that after ``pause()`` + ``AsyncSandbox.connect(sandbox_id)`` we can
``commands.connect(pid)`` back onto the still-running runner process, write
to its stdin, and receive its stdout — with the process's in-memory state
(here: a line counter) intact.

Run:  E2B_API_KEY=... python tests/e2e/spike_e2b_pause_resume.py
Requires e2b>=2.0.0.
"""

from __future__ import annotations

import asyncio
import os
import sys

from e2b import AsyncSandbox

ECHO_PROGRAM = """\
import sys
n = 0
for line in sys.stdin:
    n += 1
    print("echo[%d]: %s" % (n, line.strip()), flush=True)
"""


def _collector(label: str, sink: list[str]):
    def _cb(chunk) -> None:
        s = chunk if isinstance(chunk, str) else bytes(chunk).decode("utf-8", "replace")
        sink.append(s)
        print(f"{label}: {s.strip()}")

    return _cb


async def _wait_for(sink: list[str], needle: str, timeout: float = 15.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if any(needle in s for s in sink):
            return True
        await asyncio.sleep(0.25)
    return False


async def main() -> int:
    api_key = os.environ["E2B_API_KEY"]
    sb = await AsyncSandbox.create(template="base", api_key=api_key, timeout=600)
    print("sandbox:", sb.sandbox_id)
    try:
        await sb.files.write("/tmp/echo.py", ECHO_PROGRAM)
        pre: list[str] = []
        handle = await sb.commands.run(
            "python3 -u /tmp/echo.py",
            background=True,
            stdin=True,
            on_stdout=_collector("OUT(pre)", pre),
            timeout=0,
        )
        pid = handle.pid
        print("pid:", pid)

        await sb.commands.send_stdin(pid, "before-pause\n")
        assert await _wait_for(pre, "echo[1]: before-pause"), f"no pre-pause echo: {pre!r}"

        print("pausing...")
        await sb.pause()
        print("paused; connecting (auto-resume)...")
        sb2 = await AsyncSandbox.connect(sb.sandbox_id, api_key=api_key)
        print("resumed:", sb2.sandbox_id)

        post: list[str] = []
        h2 = await sb2.commands.connect(
            pid,
            on_stdout=_collector("OUT(post)", post),
            timeout=0,
        )
        print("reattached to pid", pid, "handle:", type(h2).__name__)
        await sb2.commands.send_stdin(pid, "after-resume\n")
        ok = await _wait_for(post, "echo[2]: after-resume")
        assert ok, f"reattach failed or counter reset — pre={pre!r} post={post!r}"
        print("SPIKE PASS: streams reattached, process memory (counter=2) survived")
        await sb2.kill()
        return 0
    except BaseException:
        try:
            await sb.kill()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
