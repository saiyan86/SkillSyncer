"""Git invocation helpers for SkillSyncer.

Centralizes the construction of ``git`` argv lists so that authenticated
operations (e.g. cloning a private GitHub App-protected repo) can pass
an ``http.extraHeader`` config without ever storing the credential in
``config.yaml``, ``state.yaml``, or any report.

The header is sourced from (in order of precedence):

1. The explicit ``extra_header`` argument passed to a helper.
2. ``$SKILLSYNCER_GIT_HTTP_EXTRA_HEADER``.

When neither is set, git runs unconfigured and behaves exactly as
before. Callers are responsible for redacting any header value before
emitting log output — see :func:`redact`.
"""

from __future__ import annotations

import os
from typing import Iterable, Sequence

# Config key used in ``-c <key>=<value>`` for HTTP auth headers.
_GIT_EXTRA_HEADER_KEY = "http.extraHeader"

# Env var name. Documented; never written by SkillSyncer itself.
ENV_VAR = "SKILLSYNCER_GIT_HTTP_EXTRA_HEADER"


def get_extra_header(explicit: str | None = None) -> str | None:
    """Resolve the active ``http.extraHeader`` value, if any.

    ``explicit`` (typically the value of ``--git-extra-header``) wins
    over the environment variable. Empty strings are treated as "not
    set".
    """
    if explicit:
        return explicit
    val = os.environ.get(ENV_VAR)
    return val or None


def build_git_argv(
    args: Sequence[str],
    *,
    extra_header: str | None = None,
) -> list[str]:
    """Build a ``git`` argv list, optionally injecting an extra HTTP
    header via ``-c http.extraHeader=...``.

    The ``-c`` flags are placed *before* the subcommand so they apply
    to it, matching git's documented behavior.
    """
    header = get_extra_header(extra_header)
    cmd: list[str] = ["git"]
    if header:
        cmd.extend(["-c", f"{_GIT_EXTRA_HEADER_KEY}={header}"])
    cmd.extend(args)
    return cmd


def redact(parts: Iterable[str], extra_header: str | None = None) -> list[str]:
    """Return ``parts`` with any occurrence of the active extra header
    value replaced by ``<redacted>``. Used when echoing argv into logs
    or error messages.
    """
    header = get_extra_header(extra_header)
    out = list(parts)
    if not header:
        return out
    return [
        p.replace(header, "<redacted>") if isinstance(p, str) else p
        for p in out
    ]
