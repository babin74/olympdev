"""Statement rendering: pure functions, no jinja2/latex/sandbox needed.

Fast (no @pytest.mark.slow) - see tests/test_statement_pipeline.py for the
LaTeX-compiling half.
"""

from pathlib import Path

from olympdev.problem.build import _statement_fragment_sources
from olympdev.problem.package import ProblemPackage
from olympdev.problem.statement import render_subtask_table
from olympdev.problem.types import ProblemGroup

POINTS_DIR = Path(__file__).resolve().parent.parent / "example-contest" / "points"


def test_render_subtask_table_empty_for_no_groups():
    assert render_subtask_table([], lang="en") == ""


def test_render_subtask_table_includes_points_and_dependencies():
    groups = [
        ProblemGroup(id=1, points=40, name="small", depends_on=[]),
        ProblemGroup(id=2, points=60, name="large", depends_on=[1]),
    ]
    rendered = render_subtask_table(groups, lang="en")
    assert "\\SubtaskWithCost{1}{40}" in rendered
    assert "\\SubtaskWithCost{2}{60}" in rendered
    assert "small" in rendered and "large" in rendered
    assert "constraints of group(s) 1" in rendered


def test_render_subtask_table_localizes_to_russian():
    groups = [ProblemGroup(id=1, points=100, name="", depends_on=[])]
    rendered = render_subtask_table(groups, lang="ru")
    assert "\\SubtaskWithCost{1}{100}" in rendered


def test_render_subtask_table_sorts_by_group_id():
    groups = [
        ProblemGroup(id=2, points=60, depends_on=[]),
        ProblemGroup(id=1, points=40, depends_on=[]),
    ]
    rendered = render_subtask_table(groups, lang="en")
    assert rendered.index("{1}{40}") < rendered.index("{2}{60}")


def test_points_fragment_has_subtask_table_and_no_pending_samples():
    """example-contest/points has no samples at all, so building its
    statement needs no computed answers - only the group-points table."""
    package = ProblemPackage.load(POINTS_DIR)
    spec = package.statement_spec("en")
    fragment, sources, pending = _statement_fragment_sources(package, spec)
    assert pending == []
    assert "\\Constraints" in fragment
    assert "\\SubtaskWithCost{1}{40}" in fragment
    assert "\\SubtaskWithCost{2}{60}" in fragment


def test_ungrouped_fragment_has_no_subtask_section():
    """aplusb has no mode/groups - its fragment must not gain a \\Constraints
    block just because the statement pipeline now knows how to render one."""
    aplusb_dir = Path(__file__).resolve().parent.parent / "example-contest" / "aplusb"
    package = ProblemPackage.load(aplusb_dir)
    spec = package.statement_spec("en")
    fragment, _sources, _pending = _statement_fragment_sources(package, spec)
    assert "\\Constraints" not in fragment
    assert "\\SubtaskWithCost" not in fragment
