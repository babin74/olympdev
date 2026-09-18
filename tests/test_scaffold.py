"""The skeleton `olympdev new` writes.

The point of a scaffold is that it works before it is edited, so these check
that what comes out loads as a real problem. Whether it also *passes*
verification needs a compiler and a sandbox; that lives in
tests/test_aplusb_pipeline.py's slow company, not here.
"""

import json
from pathlib import Path

import pytest

from olympdev.frontend.cli.main import main
from olympdev.problem.package import Contest, ProblemPackage
from olympdev.problem.scaffold import new_contest, new_problem, register_problem
from olympdev.problem.types import ContestConfiguration


def test_new_problem_loads_as_a_problem(tmp_path: Path):
    new_problem(tmp_path / "demo")
    package = ProblemPackage.load(tmp_path / "demo")
    assert package.name == "demo"
    assert package.main_solution().name == "main"
    # Every source the skeleton's info.json names must actually be there.
    for source in package.configuration.sources:
        assert package.file(source.file).is_file()
    assert package.file(package.main_solution().file).is_file()


def test_new_problem_titles_the_statement_after_the_directory(tmp_path: Path):
    new_problem(tmp_path / "two-sum")
    package = ProblemPackage.load(tmp_path / "two-sum")
    assert package.statement_spec("en").title == "two-sum"


def test_new_problem_takes_an_explicit_title(tmp_path: Path):
    new_problem(tmp_path / "demo", title="Сумма двух чисел")
    package = ProblemPackage.load(tmp_path / "demo")
    assert package.statement_spec("en").title == "Сумма двух чисел"


def test_new_contest_lists_its_first_problem(tmp_path: Path):
    new_contest(tmp_path / "cup", problem="demo")
    contest = Contest.load(tmp_path / "cup")
    assert list(contest.problems) == ["demo"]


def test_new_problem_refuses_to_overwrite(tmp_path: Path):
    new_problem(tmp_path / "demo")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        new_problem(tmp_path / "demo")


def test_a_refused_scaffold_writes_nothing(tmp_path: Path):
    """The check is up front, so a refusal cannot leave a half-written tree."""
    directory = tmp_path / "demo"
    directory.mkdir()
    (directory / "info.json").write_text("mine", encoding="utf-8")
    with pytest.raises(FileExistsError):
        new_problem(directory)
    assert (directory / "info.json").read_text(encoding="utf-8") == "mine"
    assert not (directory / "solutions").exists()


def test_register_problem_is_idempotent(tmp_path: Path):
    new_contest(tmp_path / "cup", problem="demo")
    contest_file = tmp_path / "cup" / "olympdev.json"
    register_problem(contest_file, "demo")
    assert json.loads(contest_file.read_text())["problems"] == ["demo"]


def test_cli_new_problem_registers_it_in_a_surrounding_contest(tmp_path: Path, capsys):
    new_contest(tmp_path / "cup", problem="first")
    assert main(["new", "problem", str(tmp_path / "cup" / "second")]) == 0
    capsys.readouterr()
    contest = Contest.load(tmp_path / "cup")
    assert sorted(contest.problems) == ["first", "second"]


def test_cli_schema_prints_the_models(capsys):
    assert main(["schema", "info"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert "time_limit_ms" in schema["properties"]
    assert main(["schema", "contest"]) == 0
    assert "problems" in json.loads(capsys.readouterr().out)["properties"]


def test_cli_schema_writes_a_file(tmp_path: Path, capsys):
    out = tmp_path / "nested" / "info.schema.json"
    assert main(["schema", "info", "--out", str(out)]) == 0
    capsys.readouterr()
    assert "properties" in json.loads(out.read_text())


@pytest.mark.slow
def test_a_fresh_skeleton_verifies(tmp_path: Path):
    """The whole point of a scaffold: it works before it is edited. Every
    piece has to agree - generator, validator, checker, sample and the
    reference solution - or an author's first command fails on our code."""
    import shutil

    from olympdev.frontend.notebook import Workbench

    if shutil.which("g++") is None:
        pytest.skip("g++ is not available")
    new_contest(tmp_path / "cup", problem="demo")
    view = Workbench.open(tmp_path / "cup").problem("demo").verify()
    assert view.matched, view.columns and view.columns[0].reason


def test_contest_configuration_rejects_a_problem_that_is_not_a_directory_name():
    for name in ("", "..", "a/b"):
        with pytest.raises(ValueError, match="invalid problem name"):
            ContestConfiguration.model_validate({"problems": [name]})


def test_contest_configuration_rejects_a_repeated_problem():
    with pytest.raises(ValueError, match="listed twice"):
        ContestConfiguration.model_validate({"problems": ["a", "a"]})


def test_contest_statements_are_per_language():
    """Title, venue and date are printed, so each language carries its own."""
    config = ContestConfiguration.model_validate({
        "problems": ["a"],
        "statements": [
            {"lang": "en", "title": "Winter Cup", "location": "Kazan", "date": "3 March"},
            {"lang": "ru", "title": "Зимний кубок", "location": "Казань", "date": "3 марта"},
        ],
    })
    assert config.statement_by_lang("ru").title == "Зимний кубок"
    assert config.statement_by_lang("en").location == "Kazan"
    assert config.statement_by_lang("de") is None


def test_contest_rejects_two_statements_for_one_language():
    with pytest.raises(ValueError, match="share a lang"):
        ContestConfiguration.model_validate({
            "problems": ["a"],
            "statements": [
                {"lang": "ru", "title": "A", "date": ""},
                {"lang": "ru", "title": "B", "date": ""},
            ],
        })


def test_the_old_single_statement_shape_says_what_to_do():
    """One shared block for every language was the bug; name the fix."""
    with pytest.raises(ValueError, match="'statement' is now 'statements'"):
        ContestConfiguration.model_validate({
            "problems": ["a"],
            "statement": {"title": "Cup", "date": ""},
        })


def test_contest_names_the_language_it_has_no_booklet_for(tmp_path: Path):
    new_contest(tmp_path / "cup", problem="demo")
    contest = Contest.load(tmp_path / "cup")
    with pytest.raises(KeyError, match="no contest statement for lang 'ru'"):
        contest.statement_config("ru")
