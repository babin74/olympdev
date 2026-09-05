"""The five program roles a problem is tested with, as data.

Solution, model, checker, generator and validator differ only in a handful of
attributes: which job types carry them, how a failed compile is reported, and
what the binary is called. Spelling that out once here is what lets pipelines,
jobs and the build layer loop over roles instead of repeating themselves five
times.

Job type strings are part of the on-disk format (they key the job registry and
appear in stored job inputs), so they are written out literally rather than
derived from the role name.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from olympdev.worker.cache.policy import CachePolicy
from olympdev.worker.verdict import Verdict


class ProgramRole(StrEnum):
    SOLUTION = "solution"
    MODEL = "model"
    CHECKER = "checker"
    GENERATOR = "generator"
    VALIDATOR = "validator"


class RunKind(StrEnum):
    """Which run job shape a role uses once compiled."""

    SIMPLE = "simple"   # fed a test on stdin (solution, model)
    TOOL = "tool"       # testlib-style argv and files (checker, generator, validator)


@dataclass(frozen=True)
class RoleSpec:
    role: ProgramRole
    label: str
    binary_name: str
    compile_job_type: str
    compile_cache_policy: CachePolicy
    #: Verdict when this role fails to compile. A participant's solution is a
    #: compile error; a broken model means testing itself failed; a broken
    #: checker/generator/validator is a checker failure.
    compile_failure_verdict: Verdict
    run_kind: RunKind
    run_job_type: str
    run_cache_policy: CachePolicy
    #: Roles every problem must provide.
    required: bool = False


ROLE_SPECS: dict[ProgramRole, RoleSpec] = {
    spec.role: spec
    for spec in (
        RoleSpec(
            role=ProgramRole.SOLUTION,
            label="Solution",
            binary_name="solution",
            compile_job_type="solution_compile",
            compile_cache_policy=CachePolicy.SolutionCompile,
            compile_failure_verdict=Verdict.CE,
            run_kind=RunKind.SIMPLE,
            run_job_type="solution_run",
            run_cache_policy=CachePolicy.SolutionRun,
            required=True,
        ),
        RoleSpec(
            role=ProgramRole.CHECKER,
            label="Checker",
            binary_name="checker",
            compile_job_type="checker_compile",
            compile_cache_policy=CachePolicy.CheckerCompile,
            compile_failure_verdict=Verdict.CF,
            run_kind=RunKind.TOOL,
            run_job_type="checker",
            run_cache_policy=CachePolicy.Checker,
            required=True,
        ),
        RoleSpec(
            role=ProgramRole.MODEL,
            label="Model solution",
            binary_name="model",
            compile_job_type="model_solution_compile",
            compile_cache_policy=CachePolicy.ModelSolutionCompile,
            compile_failure_verdict=Verdict.FL,
            run_kind=RunKind.SIMPLE,
            run_job_type="model_solution_run",
            run_cache_policy=CachePolicy.ModelSolutionRun,
        ),
        RoleSpec(
            role=ProgramRole.GENERATOR,
            label="Generator",
            binary_name="gen",
            compile_job_type="generator_compile",
            compile_cache_policy=CachePolicy.GeneratorCompile,
            compile_failure_verdict=Verdict.CF,
            run_kind=RunKind.TOOL,
            run_job_type="generator",
            run_cache_policy=CachePolicy.Generator,
        ),
        RoleSpec(
            role=ProgramRole.VALIDATOR,
            label="Validator",
            binary_name="valid",
            compile_job_type="validator_compile",
            compile_cache_policy=CachePolicy.ValidatorCompile,
            compile_failure_verdict=Verdict.CF,
            run_kind=RunKind.TOOL,
            run_job_type="validator",
            run_cache_policy=CachePolicy.Validator,
        ),
    )
}

#: Deterministic order for issuing compiles and reporting.
ROLE_ORDER: tuple[ProgramRole, ...] = tuple(ROLE_SPECS)

REQUIRED_ROLES: frozenset[ProgramRole] = frozenset(
    role for role, spec in ROLE_SPECS.items() if spec.required
)


def role_spec(role: ProgramRole) -> RoleSpec:
    return ROLE_SPECS[role]


def job_class_name(job_type: str) -> str:
    """Class name for a generated job, e.g. model_solution_run -> ModelSolutionRunJob.

    Derived from the job type so generated classes keep the names they had
    when each was written out by hand.
    """
    return "".join(part.title() for part in job_type.split("_")) + "Job"
