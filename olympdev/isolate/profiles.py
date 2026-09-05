"""Fixed sandbox profiles.

A profile is the *only* way to choose what a sandboxed process gets beyond its
resource limits: how many processes it may spawn, what environment it sees and
what is mounted into it. There are exactly three, they are chosen by the job
type, and nothing reachable from a submission or a problem configuration can
alter them.

This replaces per-call ``processes`` / ``env`` / ``full_env`` / ``dir_rules``
arguments on ``IsolateSandBox.run``. In particular there is no way to ask for
the host environment: isolate's ``-e`` is never emitted.
"""

from __future__ import annotations

from olympdev.isolate._dir_rules import IsolateDirectoryRules
from olympdev.schema import FrozenModel


class SandboxProfile(FrozenModel):
    name: str
    # isolate's default is a single process; compilers need to fork.
    processes: int = 1
    # Passed as --env=K=V. Empty means the process starts with no environment.
    env: dict[str, str] = {}
    # Mounts. The default rules are isolate's own; profiles may narrow them.
    dir_rules: IsolateDirectoryRules = IsolateDirectoryRules()


# g++/collect2 need PATH to find `ld`; isolate clears the environment.
COMPILE_PROFILE = SandboxProfile(
    name="compile",
    processes=64,
    env={"PATH": "/usr/bin:/bin"},
)

# Participant solutions: one process, no environment.
RUN_PROFILE = SandboxProfile(name="run")

# Checkers, validators and generators. Same shape as RUN today; kept separate
# so tightening one does not silently tighten the other.
TOOL_PROFILE = SandboxProfile(name="tool")

PROFILES: dict[str, SandboxProfile] = {
    profile.name: profile
    for profile in (COMPILE_PROFILE, RUN_PROFILE, TOOL_PROFILE)
}
