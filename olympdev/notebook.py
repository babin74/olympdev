"""One import for notebook sessions.

    from olympdev.notebook import Workbench

    wb = Workbench.open("example-contest", jobs=4)
    wb.problem("aplusb").compare()
"""

from olympdev.frontend.notebook import NotebookObserver, ProblemWorkbench, Workbench
from olympdev.frontend.view import (
    JobView,
    PipelineRunView,
    SolutionColumnView,
    TestingReportView,
    TestRowView,
    UsageView,
)

__all__ = [
    "JobView",
    "NotebookObserver",
    "PipelineRunView",
    "ProblemWorkbench",
    "SolutionColumnView",
    "TestRowView",
    "TestingReportView",
    "UsageView",
    "Workbench",
]
