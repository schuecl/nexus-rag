"""Escaping for values interpolated into log records.

Log injection: a value containing a newline or carriage return can forge what
looks like a separate, earlier log entry -- "user X purged document Y" written
by an attacker rather than by this code. Control characters can also break
whatever parses the logs downstream (#73's SIEM export, or Loki if the
observability stack in #133 lands), which is where a forged line does the most
damage: it arrives already indexed and searchable alongside genuine ones.

The rule this encodes: anything that entered the process from outside it --
an OIDC claim, a request body, a queue payload -- is escaped before it reaches
a log record. Values this process controls (UUIDs it parsed, integers it
counted, its own status constants) do not need it, but escaping them is free
and removes the need to re-litigate which is which at every call site.

Escapes rather than strips, so a hostile value stays visible in the log as
what it was -- silently deleting the characters would hide the attempt.
"""

from __future__ import annotations

import re

# C0 controls plus DEL. Leaves printable Unicode alone: a username in a
# non-Latin script is not an attack, and mangling it would be its own bug.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def log_safe(value: object) -> str:
    """Render `value` for a log record with control characters escaped."""
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", str(value))
