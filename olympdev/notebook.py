"""One import for notebook sessions.

    from olympdev.notebook import Workbench

    wb = Workbench.open("example-contest", jobs=4)
    wb.problem("aplusb").verify()
"""

from olympdev.frontend.notebook import NotebookObserver, ProblemWorkbench, Workbench
from olympdev.frontend.view import (
    JobView,
    PipelineRunView,
    SolutionColumnView,
    TestRowView,
    UsageView,
    VerificationReportView,
)

__all__ = [
    "JobView",
    "NotebookObserver",
    "PipelineRunView",
    "ProblemWorkbench",
    "SolutionColumnView",
    "TestRowView",
    "UsageView",
    "VerificationReportView",
    "Workbench",
]
