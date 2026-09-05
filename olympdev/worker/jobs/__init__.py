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
from olympdev.worker.jobs.tool_run import CheckerJob, GeneratorJob, ToolRunJob, ValidatorJob

__all__ = [
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
    "ToolRunJob",
    "ValidatorCompileJob",
    "ValidatorJob",
]


def ensure_jobs_registered() -> None:
    """Import concrete jobs so ``AbstractJob`` registry is populated."""
    return
