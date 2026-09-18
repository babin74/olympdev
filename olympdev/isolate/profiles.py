"""Fixed sandbox profiles.

A profile is the *only* way to choose what a sandboxed process gets beyond its
resource limits: how many processes it may spawn, what environment it sees and
what is mounted into it. They are chosen by the job type, and nothing
reachable from a submission or a problem configuration can alter them. The
statement profile is built by a function because it reads host settings; a
module-level constant derived from settings would make a tight host config
fail at import.

This replaces per-call ``processes`` / ``env`` / ``full_env`` / ``dir_rules``
arguments on ``IsolateSandBox.run``. In particular there is no way to ask for
the host environment: isolate's ``-e`` is never emitted.
"""

from __future__ import annotations

from pathlib import Path

from olympdev.isolate._dir_rules import IsolateDirectoryRule, IsolateDirectoryRules
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

#: Where the TeX font cache is mounted inside the sandbox.
TEXMF_VAR_SANDBOX_DIR = "texmf-var"


def statement_profile() -> SandboxProfile:
    """Typesetting a statement: the compile profile plus TeX's font cache.

    TeX Live ships only a handful of ready-made LH (Cyrillic) font metrics
    and generates the rest on demand with ``mktextfm``, caching them under
    ``$TEXMFVAR`` in the user's home. The sandbox has no home and no way to
    run ``mktextfm``, so without the host's cache a Cyrillic statement can
    never typeset, whatever it says.

    The cache is bound read-only, and the path comes from operator settings -
    never from a problem - so this stays a decision made here, in the one
    place mounts are decided.
    """
    from olympdev.settings import StatementEngine, get_settings

    toolchain = get_settings().statement_toolchain(StatementEngine.LATEX)
    texmf_var = toolchain.resolve_texmf_var()
    if texmf_var is None:
        return SandboxProfile(
            name="statement",
            processes=COMPILE_PROFILE.processes,
            env=dict(COMPILE_PROFILE.env),
        )
    return SandboxProfile(
        name="statement",
        processes=COMPILE_PROFILE.processes,
        env={**COMPILE_PROFILE.env, "TEXMFVAR": f"/{TEXMF_VAR_SANDBOX_DIR}"},
        dir_rules=IsolateDirectoryRules(
            rules=(
                IsolateDirectoryRule(
                    path_in_sandbox=Path(TEXMF_VAR_SANDBOX_DIR),
                    path_on_host=texmf_var,
                    # Read-only: a statement may read the font cache, never
                    # add to it. A host that has not generated a metric is a
                    # host provisioning problem, reported as one.
                    ignore_missing=True,
                ),
            )
        ),
    )
