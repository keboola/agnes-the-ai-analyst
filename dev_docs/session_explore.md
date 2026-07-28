# Session Exploration Guide

Guide for exploring Claude Code session transcripts to identify friction points and improve the analyst experience.

## Session Data Location

**Server:** `your-server` (alias: `kids`)
**Path:** `/data/user_sessions/`

Sessions are collected by systemd service `session-collector.timer` (runs every 30 minutes).

### Directory Structure

Sessions are organized by user:
```
/data/user_sessions/
├── john/
│   ├── 2026-02-10_49898dbe-5045-45f5-9177-2ff10917de4a.jsonl
│   └── ...
├── mike.brown/
│   └── ...
└── sam.taylor/
    └── ...
```

### Permission Issue (See Issue #147)

Session files are owned by `root:data-ops` with `-rw-------` permissions, making direct `scp` impossible. **Workaround:** Use `sudo cat` over SSH:

```bash
# This FAILS
scp kids:/data/user_sessions/john/session.jsonl .

# Use this instead
ssh kids "sudo cat /data/user_sessions/john/session.jsonl" > session.jsonl
```

---

## ⚠️ CRITICAL: Common Analysis Mistakes

Based on real-world friction point analysis (Feb 2026), avoid these common pitfalls:

### 1. File mtime ≠ Session Time

**WRONG:**
```bash
# This finds files modified in last 48h, NOT sessions active in last 48h
find /data/user_sessions -name '*.jsonl' -mtime -2
```

**Problem:** File modification time is when collector WROTE the file, not when session started/ended.

**Example:**
- Session starts: 5.2.2026 15:56
- Session ends: 9.2.2026 20:50 (laptop closed for 4 days)
- File mtime: 9.2.2026 20:50
- `-mtime -2` filter: MATCHES (file modified 2 days ago)
- **But session actually started 6 days ago!**

**CORRECT:**
```bash
# Download sessions first, then filter by internal timestamps
for session in *.jsonl; do
    FIRST_TS=$(jq -r 'select(.timestamp) | .timestamp' "$session" | head -1)
    # Check if FIRST_TS is within your time window
done
```

### 2. Session Gaps (Laptop Closed)

**WRONG:**
```python
# Summing elapsed times without checking for gaps
total_time = sum(event['data']['elapsedTimeSeconds'] for event in bash_events)
```

**Problem:** Sessions can span DAYS with laptop closed. Summing elapsed times includes idle time.

**Example:**
```
Session john.doe_2026-02-09_19c0a02f:
  First event: 2026-02-05 16:06:52
  Last event:  2026-02-09 15:18:50
  Span: 3 days, 23 hours

  Gap 1: 89.8 hours (laptop closed over weekend)
  Gap 2: 4.9 hours (lunch break)
  Gap 3: 5.4 hours (after work)

  TOTAL ACTIVE TIME: only 4.6 minutes!
  WRONG CALCULATION: 173.6 minutes (2.9 hours)
```

**CORRECT:**
```python
# Group events into active blocks (gap >10 min = new block)
def calculate_active_time(timestamps):
    blocks = []
    current_block_start = timestamps[0]
    prev_ts = timestamps[0]

    for ts in timestamps[1:]:
        gap_seconds = (ts - prev_ts).total_seconds()
        if gap_seconds > 600:  # >10 min gap
            blocks.append((current_block_start, prev_ts))
            current_block_start = ts
        prev_ts = ts

    blocks.append((current_block_start, timestamps[-1]))

    total_active = sum((end - start).total_seconds() for start, end in blocks)
    return total_active
```

### 3. Bash Progress Elapsed Times Are Cumulative

**WRONG:**
```python
# Interpreting elapsed times as per-operation duration
for event in bash_progress_events:
    if event['data']['elapsedTimeSeconds'] > 60:
        print(f"This operation took {elapsed}s")  # WRONG!
```

**Problem:** `elapsedTimeSeconds` is cumulative from command start, not per-operation.

**Example:**
```
bash_progress events for single rsync command:
  Event 1: elapsedTimeSeconds: 11  (11s from start)
  Event 2: elapsedTimeSeconds: 12  (12s from start)
  Event 3: elapsedTimeSeconds: 13  (13s from start)
  ...
  Event 96: elapsedTimeSeconds: 266 (266s from start)
```

**These are NOT 96 separate 11-266s operations!**
**This is ONE operation with 96 progress updates over 266 seconds.**

**CORRECT:**
```python
# Group by parent command, track min/max elapsed time
commands = {}
for event in bash_progress_events:
    cmd_id = event.get('parentUuid') or event.get('uuid')
    if cmd_id not in commands:
        commands[cmd_id] = {'min': float('inf'), 'max': 0}

    elapsed = event['data']['elapsedTimeSeconds']
    commands[cmd_id]['min'] = min(commands[cmd_id]['min'], elapsed)
    commands[cmd_id]['max'] = max(commands[cmd_id]['max'], elapsed)

# Total duration = max - min
for cmd_id, times in commands.items():
    duration = times['max'] - times['min']
    print(f"Command {cmd_id} took {duration}s")
```

### 4. Pre-Fix Sessions

**WRONG:** Reporting bugs that were already fixed.

**Problem:** Session file mtime doesn't tell you when session STARTED, only when it was written.

**Example:**
```
Issue #84 fixed: 2026-02-06 21:37:49
Session file: john.doe_2026-02-09_19c0a02f.jsonl
File mtime: 2026-02-09 20:50 (within 48h filter)

BUT: Session started 2026-02-05 15:56 (BEFORE fix!)
```

**CORRECT:**
```bash
# Always check when bug was fixed vs. when session started
git log --all --oneline --grep="issue-number" -- relevant/files

# Extract session start time
FIRST_TS=$(jq -r 'select(.timestamp) | .timestamp' session.jsonl | head -1)

# Compare: if session started before fix, ignore the bug report
```

### 5. Context-Free Bash Commands

**WRONG:**
```bash
# Reporting this as "user manually typed /tmp pattern"
pip freeze > /tmp/analyst_requirements.txt
scp /tmp/analyst_requirements.txt data-analyst:/tmp/...
```

**Problem:** You don't know if this was:
1. From our bootstrap.yaml (intended)
2. Claude improvising during troubleshooting (bug)
3. User typed manually (edge case)

**CORRECT:**
```bash
# Look at surrounding context in session
jq -r 'select(.type == "user") | .message.content' session.jsonl | tail -20

# Check if user pasted bootstrap instructions
grep -B10 -A10 "/tmp/analyst_requirements" session.jsonl

# Check our codebase - is this pattern in our scripts?
git grep "/tmp/analyst_requirements" -- scripts/ docs/
```

---

## Session JSONL Format

Each session is stored as a JSONL file with one JSON object per line. Each line represents an event in the session.

### Event Types

Based on real session analysis:

1. **user** - User messages
2. **assistant** - Assistant responses (contains tool_use in content array)
3. **progress** - Progress updates (hooks, bash execution)
4. **system** - System messages (often empty)
5. **file-history-snapshot** - File tracking snapshots
6. **queue-operation** - Queue management events
7. **summary** - Session summaries

### Common Event Structure

All events share:
```json
{
  "type": "user|assistant|progress|system|...",
  "sessionId": "uuid",
  "timestamp": "ISO 8601",
  "cwd": "/path/to/working/dir",
  "version": "2.1.29",
  "uuid": "event-uuid",
  "parentUuid": "parent-event-uuid"
}
```

### User Event

```json
{
  "type": "user",
  "message": {
    "role": "user",
    "content": "user's message text"
  },
  "isMeta": false
}
```

### Assistant Event with Tool Use

```json
{
  "type": "assistant",
  "message": {
    "role": "assistant",
    "content": [
      {
        "type": "text",
        "text": "response text"
      },
      {
        "type": "tool_use",
        "id": "tool-use-id",
        "name": "Bash",
        "input": {
          "command": "ls -la",
          "description": "List files"
        }
      }
    ]
  }
}
```

### Progress Event - Bash Execution

**Important:** Exit codes are NOT present in bash_progress events! Only output and timing.

```json
{
  "type": "progress",
  "data": {
    "type": "bash_progress",
    "output": "command output",
    "fullOutput": "complete output with formatting",
    "elapsedTimeSeconds": 1.234,
    "totalLines": 10,
    "timeoutMs": 120000
  }
}
```

### Progress Event - Hook

```json
{
  "type": "progress",
  "data": {
    "type": "hook_progress",
    "hookEvent": "SessionStart",
    "hookName": "SessionStart:clear",
    "command": "${CLAUDE_PLUGIN_ROOT}/hooks-handlers/session-start.sh"
  }
}
```

### Key Insight for Friction Detection

⚠️ **Exit codes and explicit errors are NOT tracked in the current session format!**

To find friction points, look for:
- Error keywords in bash output strings
- Repeated similar commands (retry patterns)
- User questions/confusion in messages
- Long bash execution times
- Context overflow (session continuations)

## Practical Commands

### Browse Sessions on Server

```bash
# List all sessions
ssh kids "ls -lh /data/user_sessions/"

# Count sessions
ssh kids "ls /data/user_sessions/ | wc -l"

# Find recent sessions (last 7 days)
ssh kids "find /data/user_sessions -name '*.jsonl' -mtime -7"

# Find sessions by user
ssh kids "ls /data/user_sessions/ | grep '^john-'"
```

### Download Sessions for Local Analysis

```bash
# Download specific session
scp kids:/data/user_sessions/john-2024-12-15-abc123.jsonl .

# Download all sessions for a user
scp kids:/data/user_sessions/john-*.jsonl ./sessions/

# Download recent sessions
ssh kids "find /data/user_sessions -mtime -7" | xargs -I {} scp kids:{} ./sessions/
```

### Quick Stats on Server

```bash
# Count events per session
ssh kids "wc -l /data/user_sessions/*.jsonl"

# Count tool uses
ssh kids "grep -h '\"type\": \"tool_use\"' /data/user_sessions/*.jsonl | wc -l"

# Count errors
ssh kids "grep -h '\"is_error\": true' /data/user_sessions/*.jsonl | wc -l"
```

## jq Queries for Analysis

### Extract All Bash Commands

```bash
# Extract from assistant messages with tool_use
jq -r 'select(.type == "assistant") | .message.content[]? | select(.type == "tool_use" and .name == "Bash") | .input.command' session.jsonl
```

### Find Bash Output (for error keywords)

```bash
# Extract bash execution output
jq -r 'select(.type == "progress" and .data.type == "bash_progress") | .data.fullOutput' session.jsonl
```

### Find Error Keywords in Output

Since exit codes aren't tracked, search output strings:

```bash
# Search bash output for errors
jq -r 'select(.type == "progress" and .data.type == "bash_progress") | .data.fullOutput' session.jsonl | grep -i "error\|failed\|permission denied"
```

### ⚠️ Exit Code Queries Don't Work

Exit codes are NOT present in the current session format. These queries will return nothing:

```bash
# These DON'T work with current format
jq 'select(.exit_code == 127)' session.jsonl  # Returns nothing
jq 'select(.data.exitCode != 0)' session.jsonl  # Returns nothing
```

### Count Tool Usage

```bash
jq -r 'select(.type == "tool_use") | .name' session.jsonl | sort | uniq -c
```

### Extract Error Messages

```bash
jq -r 'select(.is_error == true) | .content' session.jsonl
```

### Find Retry Patterns

```bash
# Find repeated similar commands (potential retry loops)
jq -r 'select(.type == "tool_use" and .name == "Bash") | .input.command' session.jsonl | sort | uniq -c | sort -rn
```

### Session Timeline

```bash
jq -r '[.timestamp, .type, .name // .role] | @tsv' session.jsonl
```

### Full Tool Use + Result

```bash
# Extract tool use followed by its result
jq -s 'group_by(.tool_use_id // empty) | .[] | select(length == 2)' session.jsonl
```

## grep Patterns for Friction Points

### Permission Errors

```bash
grep -i "permission denied" session.jsonl
grep -i "access denied" session.jsonl
grep -i "not permitted" session.jsonl
```

### Command Not Found

```bash
grep "command not found" session.jsonl
grep "No such file or directory" session.jsonl
```

### Authentication Issues

```bash
grep -i "authentication failed" session.jsonl
grep -i "unauthorized" session.jsonl
grep -i "forbidden" session.jsonl
```

### Timeout Issues

```bash
grep -i "timeout" session.jsonl
grep -i "timed out" session.jsonl
```

### Data Sync Issues

```bash
grep "sync_data.sh" session.jsonl
grep "rsync" session.jsonl | grep -i "error\|fail"
```

### Python/Environment Issues

```bash
grep "ModuleNotFoundError" session.jsonl
grep "ImportError" session.jsonl
grep "No module named" session.jsonl
```

## What to Look For (Friction Points)

### 1. Tool Failures

**Indicators:**
- `"is_error": true` in tool results
- Non-zero exit codes
- Exceptions in tool output

**Check:**
- Which tools fail most often?
- Are there common error patterns?
- Do errors lead to retry loops?

### 2. Permission Issues

**Indicators:**
- Exit code 126
- "Permission denied" messages
- Sudoers-related errors

**Check:**
- What operations need elevated permissions?
- Are there gaps in sudoers configuration?
- Do users hit permission walls frequently?

### 3. Command Not Found

**Indicators:**
- Exit code 127
- "command not found" messages

**Check:**
- Missing system utilities?
- PATH issues?
- Typos in commands vs. actual bugs?

### 4. Retry Loops

**Indicators:**
- Same command repeated multiple times
- Error → retry → error pattern
- High tool use count for single task

**Check:**
- What triggers retries?
- Are retries effective or just wasting time?
- Could better error messages prevent retries?

### 5. Data Sync Problems

**Indicators:**
- rsync errors
- Missing files after sync
- Stale data complaints

**Check:**
- Network issues?
- Permission problems on server?
- User confusion about when to sync?

### 6. Environment Setup Issues

**Indicators:**
- Missing Python modules
- Virtual environment problems
- Dependency conflicts

**Check:**
- Are setup instructions clear?
- Do users skip setup steps?
- Are requirements.txt files accurate?

### 7. User Confusion

**Indicators:**
- Repeated questions about same topic
- Commands tried in wrong directory
- Misunderstanding of data structure

**Check:**
- Documentation gaps?
- Confusing error messages?
- Missing guidance in critical moments?

## Creating GitHub Issues

### When to Create an Issue

Create a GitHub issue when you find:
- Repeated failures across multiple sessions
- Systematic problems (not user typos)
- Missing features that would prevent friction
- Documentation gaps that cause confusion
- Security or permission model issues

### Issue Template

```markdown
## Friction Point: [Short Description]

**Source:** Session exploration - [username]-[date]-[session_id]
**Frequency:** [How often this appears in sessions]
**Impact:** [High/Medium/Low]

### Problem

[Clear description of what goes wrong]

### Evidence

```
[Relevant excerpts from session JSONL]
```

### Root Cause

[Your analysis of why this happens]

### Proposed Solution

[How to fix this - could be code, documentation, or process change]

### Related Sessions

- session1.jsonl
- session2.jsonl
```

### Labels to Use

- `user-feedback` - Always use this for friction points from sessions
- `claude-learnings` - If the issue reveals Claude-specific patterns
- `bug` - If something is broken
- `documentation` - If docs are missing or unclear
- `enhancement` - If a feature would prevent the friction
- `security` - If related to permissions or access control
- `pipeline` - If data sync or transformation related

## Example Exploration Workflow

### Step 1: Get Overview

```bash
# How many sessions do we have?
ssh kids "ls /data/user_sessions/ | wc -l"

# When was the last session?
ssh kids "ls -lt /data/user_sessions/ | head -5"

# Which users have sessions?
ssh kids "ls /data/user_sessions/ | cut -d- -f1 | sort | uniq -c"
```

### Step 2: Sample Sessions

```bash
# Download a few recent sessions
mkdir -p ~/session-exploration
cd ~/session-exploration

# Get 5 most recent
ssh kids "ls -t /data/user_sessions/*.jsonl | head -5" | xargs -I {} scp kids:{} .
```

### Step 3: Quick Analysis

```bash
# For each session, check:
for session in *.jsonl; do
    echo "=== $session ==="
    echo "Events: $(wc -l < $session)"
    echo "Errors: $(jq 'select(.is_error == true)' $session | wc -l)"
    echo "Exit codes: $(jq -r 'select(.exit_code != null and .exit_code != 0) | .exit_code' $session | sort | uniq -c)"
    echo ""
done
```

### Step 4: Deep Dive on Errors

```bash
# Extract all errors to a file
jq 'select(.is_error == true)' *.jsonl > all_errors.json

# Analyze error patterns
jq -r '.content' all_errors.json | sort | uniq -c | sort -rn
```

### Step 5: Create Issues

For each distinct friction point:
1. Document the pattern
2. Collect evidence from sessions
3. Propose solution
4. Create GitHub issue with appropriate labels

## Tips

- **Start small:** Analyze 5-10 sessions first before scaling up
- **Look for patterns:** Single errors might be user mistakes, repeated errors are friction points
- **Context matters:** Read surrounding messages to understand user intent
- **Time analysis:** Check if friction points occur at specific times (e.g., after server updates)
- **User comparison:** Do all users hit the same issues or are some user-specific?

---

## Practical Example: Correct Session Analysis

Based on lessons learned from Feb 2026 friction point analysis:

### Step 1: Download Sessions with Correct Time Filtering

```bash
# Create analysis directory
mkdir -p ~/session-analysis/raw

# Download ALL sessions (we'll filter by timestamps later)
# OR download sessions from specific date range based on filename
ssh kids "find /data/user_sessions -name '*2026-02-1[01]*.jsonl' -type f" | while read session; do
    filename=$(echo $session | sed 's/\/data\/user_sessions\///' | tr '/' '_')
    ssh kids "sudo cat $session" > ~/session-analysis/raw/$filename
done
```

### Step 2: Filter by Internal Timestamps

```python
#!/usr/bin/env python3
"""Filter sessions by actual start/end time, not file mtime."""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Define time window
NOW = datetime.now(timezone.utc)
WINDOW_START = NOW - timedelta(hours=48)

session_dir = Path("~/session-analysis/raw").expanduser()
valid_sessions = []

for session_file in session_dir.glob("*.jsonl"):
    try:
        # Get first and last timestamp
        with open(session_file) as f:
            lines = f.readlines()

        first_event = json.loads(lines[0])
        last_event = json.loads(lines[-1])

        first_ts = datetime.fromisoformat(first_event['timestamp'].replace('Z', '+00:00'))
        last_ts = datetime.fromisoformat(last_event['timestamp'].replace('Z', '+00:00'))

        # Check if session STARTED within window (not just ended)
        if first_ts >= WINDOW_START:
            valid_sessions.append({
                'file': session_file.name,
                'start': first_ts,
                'end': last_ts,
                'duration': last_ts - first_ts
            })
            print(f"✓ {session_file.name}: {first_ts} -> {last_ts}")
        else:
            print(f"✗ {session_file.name}: started {first_ts} (before window)")

    except Exception as e:
        print(f"✗ {session_file.name}: error - {e}")

print(f"\nFound {len(valid_sessions)} sessions within 48h window")
```

### Step 3: Calculate Active Time (Skip Gaps)

```python
#!/usr/bin/env python3
"""Calculate real active time, excluding laptop-closed gaps."""

import json
from datetime import datetime
from pathlib import Path

def calculate_active_time(session_file, gap_threshold_minutes=10):
    """
    Calculate active time in session, excluding gaps >threshold.

    Args:
        session_file: Path to JSONL session file
        gap_threshold_minutes: Gap >this = laptop closed (default 10 min)

    Returns:
        dict with total_span, active_time, gaps
    """
    timestamps = []

    with open(session_file) as f:
        for line in f:
            try:
                event = json.loads(line)
                if ts_str := event.get('timestamp'):
                    ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                    timestamps.append(ts)
            except:
                pass

    if len(timestamps) < 2:
        return None

    timestamps.sort()

    # Find gaps
    gaps = []
    blocks = []
    block_start = timestamps[0]
    prev_ts = timestamps[0]

    for ts in timestamps[1:]:
        gap_seconds = (ts - prev_ts).total_seconds()
        if gap_seconds > gap_threshold_minutes * 60:
            # End current block
            blocks.append((block_start, prev_ts))
            gaps.append({
                'start': prev_ts,
                'end': ts,
                'duration_hours': gap_seconds / 3600
            })
            block_start = ts
        prev_ts = ts

    # Last block
    blocks.append((block_start, timestamps[-1]))

    # Calculate metrics
    total_span = (timestamps[-1] - timestamps[0]).total_seconds()
    active_time = sum((end - start).total_seconds() for start, end in blocks)
    gap_time = total_span - active_time

    return {
        'total_span_hours': total_span / 3600,
        'active_time_hours': active_time / 3600,
        'gap_time_hours': gap_time / 3600,
        'num_gaps': len(gaps),
        'gaps': gaps,
        'num_blocks': len(blocks)
    }

# Example usage
session = Path("~/session-analysis/raw/john.doe_2026-02-09_19c0a02f.jsonl").expanduser()
result = calculate_active_time(session)

print(f"Total span: {result['total_span_hours']:.2f} hours")
print(f"Active time: {result['active_time_hours']:.2f} hours")
print(f"Gap time: {result['gap_time_hours']:.2f} hours")
print(f"Number of gaps: {result['num_gaps']}")

for i, gap in enumerate(result['gaps'], 1):
    print(f"  Gap {i}: {gap['duration_hours']:.1f} hours ({gap['start']} -> {gap['end']})")
```

### Step 4: Verify Bug Fix Timeline

```bash
#!/bin/bash
# Check if session contains pre-fix bugs

SESSION_FILE="$1"
ISSUE_NUMBER="$2"  # e.g., 84

# Get session start time
FIRST_TS=$(jq -r 'select(.timestamp) | .timestamp' "$SESSION_FILE" | head -1)
echo "Session started: $FIRST_TS"

# Get when issue was fixed
echo -e "\nIssue #$ISSUE_NUMBER fix timeline:"
gh issue view $ISSUE_NUMBER --json closedAt,title | jq -r '"Closed: " + .closedAt + " - " + .title'

# Get relevant commits around that time
echo -e "\nRelevant commits:"
CLOSE_DATE=$(gh issue view $ISSUE_NUMBER --json closedAt -q .closedAt | cut -d'T' -f1)
git log --all --since="$CLOSE_DATE" --until="$(date -v+1d -j -f "%Y-%m-%d" "$CLOSE_DATE" +%Y-%m-%d)" \
    --oneline --grep="$ISSUE_NUMBER\|tmp\|requirements" -- scripts/ docs/setup/

echo -e "\nConclusion:"
if [[ "$FIRST_TS" < "$(gh issue view $ISSUE_NUMBER --json closedAt -q .closedAt)" ]]; then
    echo "⚠️  Session STARTED BEFORE fix - bug is expected"
else
    echo "✓ Session started AFTER fix - bug should not appear"
fi
```

### Step 5: Generate Accurate Report

```python
#!/usr/bin/env python3
"""Generate friction report with verified metrics."""

import json
from pathlib import Path
from collections import defaultdict

def analyze_friction_points(sessions_dir):
    """
    Analyze friction points with proper methodology.

    Avoids:
    - False positives from pre-fix sessions
    - Inflated time estimates from gaps
    - Misinterpreted cumulative elapsed times
    """
    friction_points = defaultdict(list)

    for session_file in Path(sessions_dir).glob("*.jsonl"):
        # 1. Check session time window
        active_time = calculate_active_time(session_file)
        if not active_time or active_time['total_span_hours'] > 48:
            continue  # Skip multi-day sessions or invalid

        # 2. Extract friction indicators
        with open(session_file) as f:
            for line in f:
                event = json.loads(line)

                # Example: Permission errors
                if event.get('type') == 'progress':
                    output = event.get('data', {}).get('fullOutput', '')
                    if 'Permission denied' in output:
                        friction_points['permission_errors'].append({
                            'session': session_file.name,
                            'timestamp': event.get('timestamp'),
                            'output': output[:200]
                        })

                # Example: Slow operations (using proper elapsed time interpretation)
                if event.get('type') == 'progress' and event.get('data', {}).get('type') == 'bash_progress':
                    # This is cumulative, not per-operation!
                    # Track command start/end, not individual progress events
                    pass

    return friction_points

# Generate report
friction = analyze_friction_points("~/session-analysis/raw")

for category, incidents in friction.items():
    print(f"\n## {category.replace('_', ' ').title()}")
    print(f"Found {len(incidents)} incidents")
    # ... detailed reporting
```

---

## Future Automation

Once we understand common patterns, we can build:
- Automated friction detection scripts
- Session quality metrics
- Alerting for critical failures
- User experience dashboards

But start with manual exploration to learn what matters.
