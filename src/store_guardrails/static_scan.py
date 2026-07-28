"""Static security scan for uploaded skill/agent/plugin bundles.

**Static scan is signal, not gate.** Substring matches flag candidates
for the LLM reviewer; treat them as suggestive, not authoritative. Any
attacker willing to obfuscate (`getattr(__builtins__, "ev"+"al")`,
base64-decoded eval, dynamic imports) trivially bypasses substring
matching, and legitimate code (e.g. a script that calls subprocess
intentionally) trips false positives that the LLM resolves with context.

The pipeline still treats a static-security finding as inline-blocking
because shipping known-bad patterns to the LLM is wasteful and the
admin override path exists for false positives — but operators reading
``inline_checks.static_security`` should NOT assume "no findings" means
"safe". The LLM verdict carries that determination.

Implementation notes:

- Pure pattern matching — no LLM, no execution.
- Documentation files (`.md`, `.txt`, `.rst`, `.html`, `.json`,
  `.yaml`, `.yml`) are skipped to avoid false positives on prose that
  legitimately discusses ``eval`` / ``exec`` / etc. Code files (`.py`,
  `.js`, `.sh`, …) remain in scope.
- Template-aware: text that only contains "exec-like" tokens *inside*
  a Jinja-style ``{{...}}`` placeholder is not flagged, since
  first-use customization is a feature.

Future work (tracked separately): AST mode for `.py` files behind a
flag, with false-positive comparison before flipping the default.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List


# Files we don't bother scanning. Two categories:
#   1. Binary content — irrelevant to a substring security scan.
#   2. Documentation / config — substring matches on prose ("see also
#      eval()", "configure exec_path:") are false positives that confuse
#      uploaders without adding signal. Code files (.py, .js, .sh, …)
#      remain in scope.
_SKIP_EXTENSIONS = {
    # Binary
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",
    ".mp3", ".mp4", ".mov", ".webm",
    ".zip", ".tar", ".gz", ".7z",
    ".pdf", ".woff", ".woff2", ".ttf", ".otf",
    ".pyc", ".pyo", ".so", ".dylib", ".dll",
    # Documentation / config (#6 honesty fix). Prose that mentions
    # `eval` / `exec` is not a security signal; flagging it taught
    # uploaders to ignore the static-security panel.
    ".md", ".txt", ".rst", ".html",
    ".json", ".yaml", ".yml", ".toml",
}

# Cap per file so a 50 MB README full of `eval(` doesn't OOM the worker.
_MAX_FILE_BYTES = 256 * 1024

# Strip Jinja-style placeholders before scanning. A finding inside
# `{{ASANA_PROJECT}}` is not a real exec call.
_TEMPLATE_RE = re.compile(r"\{\{[^}]*\}\}")

# Each rule: (severity, category, regex, human-readable reason).
# Severities: "high" — always a blocker; "medium" — currently treated as
# a blocker too (we ramp severity tiers in once we have real-world false
# positive data), recorded distinctly so the admin UI can filter.
_RULES: List[tuple[str, str, re.Pattern[str], str]] = [
    # Code execution
    ("high", "code_exec", re.compile(r"\beval\s*\("),
     "use of eval()"),
    ("high", "code_exec", re.compile(r"\bexec\s*\("),
     "use of exec()"),
    # Bash-style eval — separate rule because shell `eval $X` has no parens.
    ("high", "code_exec", re.compile(r"(?:^|[;|&\s])eval\s+[\"'$]"),
     "shell eval expanding a variable"),
    ("high", "code_exec", re.compile(r"\bos\.system\s*\("),
     "os.system() call"),
    ("high", "code_exec", re.compile(r"subprocess\.\w+\([^)]*shell\s*=\s*True"),
     "subprocess with shell=True"),
    ("high", "deserialization", re.compile(r"\bpickle\.loads?\s*\("),
     "pickle deserialization (RCE risk)"),
    ("medium", "code_exec",
     re.compile(r"base64\.b64decode\s*\([^)]*\)[^\n]{0,40}\b(eval|exec)\b"),
     "base64-decoded payload passed to eval/exec"),

    # Hardcoded secrets / credentials. Patterns aimed at high-confidence
    # provider tokens — false positives in skills should be near zero.
    ("high", "secret_leak",
     re.compile(r"\bsk-[a-zA-Z0-9_\-]{32,}"),
     "Anthropic / OpenAI-style API key literal"),
    ("high", "secret_leak",
     re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
     "AWS access key ID literal"),
    ("high", "secret_leak",
     re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
     "GitHub personal access token literal"),
    ("high", "secret_leak",
     re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
     "Slack token literal"),
    ("medium", "secret_leak",
     re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----"),
     "embedded private key"),

    # Destructive filesystem ops on user data
    ("high", "destructive_fs",
     re.compile(r"rm\s+-[rRf]+\s+(?:\$HOME|~|/)\b"),
     "rm -rf on $HOME or root"),
    ("medium", "destructive_fs",
     re.compile(r"shutil\.rmtree\s*\([^)]*(?:\$HOME|~|'/')"),
     "shutil.rmtree on $HOME or root"),

    # Path traversal in scripts (relative segments aimed outside plugin dir)
    ("medium", "path_traversal",
     re.compile(r"(?:'|\")(?:\.\./){3,}"),
     "deep parent-directory traversal"),

    # Reverse shells / suspicious network callouts. Hardcoded raw IPs +
    # netcat / bash /dev/tcp idioms.
    ("high", "reverse_shell",
     re.compile(r"bash\s+-i\s+>&\s*/dev/tcp/"),
     "bash reverse-shell idiom"),
    ("high", "reverse_shell",
     re.compile(r"\bnc\s+-[lne]+\s"),
     "netcat with listen flags (reverse shell)"),
    ("medium", "suspicious_url",
     re.compile(r"https?://\d{1,3}(\.\d{1,3}){3}"),
     "hardcoded raw IP URL"),
    ("medium", "suspicious_url",
     re.compile(r"\.onion\b"),
     "onion URL"),
]


def scan_dir(plugin_dir: Path) -> Dict[str, Any]:
    """Walk the plugin directory and apply every rule to every text file.

    Returns ``{"status": "pass"|"fail", "findings": [...]}``. A finding is
    a dict ``{file, line, category, severity, reason, snippet}``.
    """
    if not plugin_dir.is_dir():
        return {"status": "fail", "findings": [
            {"file": str(plugin_dir), "line": 0, "category": "config",
             "severity": "high", "reason": "plugin_dir_missing", "snippet": ""}
        ]}

    findings: List[Dict[str, Any]] = []

    for path in sorted(plugin_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in _SKIP_EXTENSIONS:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size == 0 or size > _MAX_FILE_BYTES:
            # Skip empty + oversized files (the upload-side size cap already
            # bounds the bundle as a whole; per-file cap prevents one giant
            # README from dominating the scan).
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        rel = path.relative_to(plugin_dir).as_posix()
        for lineno, raw_line in enumerate(text.splitlines(), start=1):
            stripped = _TEMPLATE_RE.sub("", raw_line)  # template-aware
            for severity, category, regex, reason in _RULES:
                if regex.search(stripped):
                    findings.append({
                        "file": rel,
                        "line": lineno,
                        "category": category,
                        "severity": severity,
                        "reason": reason,
                        "snippet": raw_line.strip()[:200],
                    })

    return {
        "status": "pass" if not findings else "fail",
        "findings": findings,
    }
