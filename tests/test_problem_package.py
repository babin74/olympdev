"""The example contest must load and build without any hand-written glue."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from olympdev.problem.build import build_problem_testing_input, compile_spec
from olympdev.problem.package import Contest, ProblemPackage, load_target
from olympdev.problem.types import ProblemConfiguration, ProblemSourceType
from olympdev.storage.filestorage import FileStorage
from olympdev.worker.roles import ProgramRole


@pytest.fixture
def contest_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "example-contest"


def test_example_info_json_loads_as_written(contest_dir: Path):
    """The shipped info.json is the model's format - no translation layer."""
    package = ProblemPackage.load(contest_dir / "aplusb")
    assert package.configuration.time_limit_ms == 1000
    assert package.configuration.memory_limit_mb == 512
    assert package.name == "aplusb"


def test_contest_lists_its_problems(contest_dir: Path):
    contest = Contest.load(contest_dir)
    assert list(contest.problems) == ["aplusb"]
    assert contest.problem("aplusb").name == "aplusb"
    with pytest.raises(KeyError, match="no problem 'nope'"):
        contest.problem("nope")


@pytest.mark.parametrize("target", ["", "olympdev.json", "aplusb", "aplusb/info.json"])
def test_load_target_accepts_contest_or_problem(contest_dir: Path, target: str):
    contest = load_target(contest_dir / target if target else contest_dir)
    assert "aplusb" in contest.problems


def test_roles_are_derived_from_the_problem(contest_dir: Path):
    package = ProblemPackage.load(contest_dir / "aplusb")
    roles = package.role_files()
    assert set(roles) == {
        ProgramRole.SOLUTION,
        ProgramRole.CHECKER,
        ProgramRole.MODEL,
        ProgramRole.GENERATOR,
        ProgramRole.VALIDATOR,
    }
    # The model is the main solution, because tests carry no answer files.
    assert package.configuration.needs_model_solution()
    assert roles[ProgramRole.MODEL].name == "sol-ac.cpp"


def test_model_is_omitted_when_every_test_has_an_answer(tmp_path: Path):
    (tmp_path / "a.in").write_bytes(b"")
    (tmp_path / "a.out").write_bytes(b"")
    (tmp_path / "check.cpp").write_bytes(b"")
    (tmp_path / "sol.cpp").write_bytes(b"")
    (tmp_path / "info.json").write_text(json.dumps({
        "time_limit_ms": 1000,
        "memory_limit_mb": 64,
        "tests": [{"input_file": "a.in", "output_file": "a.out"}],
        "solutions": [{"file": "sol.cpp", "tag": "main"}],
        "sources": [{"file": "check.cpp", "type": "check"}],
    }))
    package = ProblemPackage.load(tmp_path)
    assert set(package.role_files()) == {ProgramRole.SOLUTION, ProgramRole.CHECKER}


def test_missing_checker_is_reported_at_load_not_at_run(tmp_path: Path):
    (tmp_path / "a.in").write_bytes(b"")
    (tmp_path / "a.out").write_bytes(b"")
    (tmp_path / "sol.cpp").write_bytes(b"")
    (tmp_path / "info.json").write_text(json.dumps({
        "time_limit_ms": 1000,
        "memory_limit_mb": 64,
        "tests": [{"input_file": "a.in", "output_file": "a.out"}],
        "solutions": [{"file": "sol.cpp", "tag": "main"}],
    }))
    with pytest.raises(ValueError, match="checker source is required"):
        ProblemPackage.load(tmp_path).role_files()


def test_solution_lookup_by_tag(contest_dir: Path):
    package = ProblemPackage.load(contest_dir / "aplusb")
    assert package.solution("WA").file == "solutions/sol-wa.cpp"
    assert package.main_solution().file == "solutions/sol-ac.cpp"
    with pytest.raises(KeyError, match="no solution tagged 'nope'"):
        package.solution("nope")


def test_duplicate_solution_tags_are_rejected():
    with pytest.raises(ValidationError, match="duplicate solution tag 'WA'"):
        ProblemConfiguration.model_validate({
            "time_limit_ms": 1000,
            "memory_limit_mb": 64,
            "solutions": [
                {"file": "a.cpp", "tag": "main"},
                {"file": "b.cpp", "tag": "WA"},
                {"file": "c.cpp", "tag": "WA"},
            ],
        })


def test_solutions_require_a_main():
    with pytest.raises(ValidationError, match="tagged 'main'"):
        ProblemConfiguration.model_validate({
            "time_limit_ms": 1000,
            "memory_limit_mb": 64,
            "solutions": [{"file": "b.cpp", "tag": "WA"}],
        })


def test_duplicate_sources_of_one_type_are_rejected():
    with pytest.raises(ValidationError, match="duplicate check source"):
        ProblemConfiguration.model_validate({
            "time_limit_ms": 1000,
            "memory_limit_mb": 64,
            "sources": [
                {"file": "a.cpp", "type": "check"},
                {"file": "b.cpp", "type": "check"},
            ],
        })


def test_package_file_rejects_escaping_paths(contest_dir: Path):
    package = ProblemPackage.load(contest_dir / "aplusb")
    with pytest.raises(ValueError, match=r"\.\.|relative"):
        package.file("../../secret.txt")


def test_compile_spec_attaches_testlib_only_for_tool_roles(tmp_path: Path):
    testlib = tmp_path / "testlib.h"
    testlib.write_bytes(b"")
    source = tmp_path / "ncmp.cpp"
    source.write_bytes(b"")

    checker = compile_spec(ProgramRole.CHECKER, source, testlib_path=testlib)
    assert [name for _, name in checker.sources] == ["ncmp.cpp", "testlib.h"]
    assert checker.command[-3:] == ["-o", "checker", 0]

    solution = compile_spec(ProgramRole.SOLUTION, source, testlib_path=testlib)
    assert [name for _, name in solution.sources] == ["ncmp.cpp"]
    assert solution.command[-3:] == ["-o", "solution", 0]


def test_compile_spec_uses_the_configured_toolchain(tmp_path: Path):
    from olympdev.settings import Settings, using_settings

    source = tmp_path / "main.cpp"
    source.write_bytes(b"")
    custom = Settings.model_validate({
        "toolchains": {"cpp": {"compiler": "/usr/bin/g++", "flags": ["-O3", "-g"]}}
    })
    with using_settings(custom):
        spec = compile_spec(ProgramRole.SOLUTION, source)
    assert spec.command[:3] == ["/usr/bin/g++", "-O3", "-g"]


def test_build_input_uploads_every_role(storage: FileStorage, contest_dir: Path):
    package = ProblemPackage.load(contest_dir / "aplusb")
    digest = build_problem_testing_input(storage, package)
    arguments = storage.download_dictionary(digest)
    assert set(arguments["compiles"]) == {
        "solution", "checker", "model", "generator", "validator",
    }
    assert arguments["problem_name"] == "aplusb"
    assert len(arguments["tests"]) == 6
    for compile_digest in arguments["compiles"].values():
        assert storage.exists(compile_digest)


def test_build_input_accepts_raw_submitted_bytes(storage: FileStorage, contest_dir: Path):
    package = ProblemPackage.load(contest_dir / "aplusb")
    digest = build_problem_testing_input(
        storage, package, solution=b"int main() { return 0; }"
    )
    arguments = storage.download_dictionary(digest)
    assert storage.exists(arguments["compiles"]["solution"])


def test_build_input_selects_a_solution_by_tag(storage: FileStorage, contest_dir: Path):
    package = ProblemPackage.load(contest_dir / "aplusb")
    wa = build_problem_testing_input(
        storage, package, solution=package.solution("WA")
    )
    main = build_problem_testing_input(storage, package)
    assert wa != main
