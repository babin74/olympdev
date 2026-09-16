from olympdev.worker.jobs.compile import (
    CheckerCompileJob,
    CompileJob,
    GeneratorCompileJob,
    ModelSolutionCompileJob,
    SolutionCompileJob,
    ValidatorCompileJob,
)
from olympdev.worker.jobs.simple_run import (
    ModelSolutionRunJob,
    SimpleRunJob,
    SolutionRunJob,
)
from olympdev.worker.jobs.statement_compile import StatementCompileJob
from olympdev.worker.jobs.tool_run import CheckerJob, GeneratorJob, ToolRunJob, ValidatorJob

from olympdev.worker.jobs.compile import COMPILE_JOB_CLASSES, compile_job_class
from olympdev.worker.jobs.simple_run import SIMPLE_RUN_JOB_CLASSES
from olympdev.worker.jobs.tool_run import TOOL_RUN_JOB_CLASSES
from olympdev.worker.roles import ProgramRole

#: Every role's run job, whatever its shape.
RUN_JOB_CLASSES = {**SIMPLE_RUN_JOB_CLASSES, **TOOL_RUN_JOB_CLASSES}


def run_job_class(role: ProgramRole):
    return RUN_JOB_CLASSES[role]


__all__ = [
    "COMPILE_JOB_CLASSES",
    "RUN_JOB_CLASSES",
    "CheckerCompileJob",
    "CheckerJob",
    "CompileJob",
    "GeneratorCompileJob",
    "GeneratorJob",
    "ModelSolutionCompileJob",
    "ModelSolutionRunJob",
    "SimpleRunJob",
    "SolutionCompileJob",
    "SolutionRunJob",
    "StatementCompileJob",
    "ToolRunJob",
    "ValidatorCompileJob",
    "ValidatorJob",
    "compile_job_class",
    "run_job_class",
]


def ensure_jobs_registered() -> None:
    """Import concrete jobs so ``AbstractJob`` registry is populated."""
    return
