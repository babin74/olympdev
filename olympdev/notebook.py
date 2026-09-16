"""One import for notebook sessions.

    from olympdev.notebook import Workbench

    wb = Workbench.open("example-contest", jobs=4)
    wb.problem("aplusb").verify()
"""

from olympdev.frontend.notebook import NotebookObserver, ProblemWorkbench, Workbench
from olympdev.frontend.view import (
    GroupScoreView,
    JobView,
    PipelineRunView,
    SolutionColumnView,
    StatementResultView,
    StressResultView,
    TestRowView,
    UsageView,
    VerificationReportView,
)

__all__ = [
    "GroupScoreView",
    "JobView",
    "NotebookObserver",
    "PipelineRunView",
    "ProblemWorkbench",
    "SolutionColumnView",
    "StatementResultView",
    "StressResultView",
    "TestRowView",
    "UsageView",
    "VerificationReportView",
    "Workbench",
]
