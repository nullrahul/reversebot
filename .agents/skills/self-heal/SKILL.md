---
name: self-heal
description: Triggers automatically upon command execution failure to log mistakes, document root causes, and patch context configurations.
---

# Self-Healing & Documentation Workflow

Use this skill whenever a terminal command fails or an environment mismatch occurs on either the local MacBook or the remote VPS terminal.

## Execution Steps

### 1. Document the Incident
Immediately append the error footprint to `.agents/skills/self-heal/failures.log`. Format it as follows:
```text
[TIMESTAMP]
COMMAND: <failed command>
OUTPUT: <raw stderr/stdout error sequence>
