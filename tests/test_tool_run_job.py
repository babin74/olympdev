import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from olympdev.isolate import IsolateFactory, IsolateMeta
from olympdev.storage.filestorage import FileStorage
from olympdev.worker.job import JobContext, JobStatus
from olympdev.worker.jobs.tool_run import (
    ToolRunJob,
    ToolRunJobResult,
    checker_verdict,
)
from olympdev.worker.verdict import Verdict


def _build_binary(source: bytes) -> bytes:
    if shutil.which("g++") is None:
        pytest.skip("g++ is not available")
    with tempfile.TemporaryDirectory() as tmp:
        source_path = Path(tmp) / "program.cpp"
        binary_path = Path(tmp) / "program"
        source_path.write_bytes(source)
        subprocess.run(
            ["g++", "-O2", "-o", str(binary_path), str(source_path)],
            check=True,
            capture_output=True,
        )
        return binary_path.read_bytes()


def test_checker_verdict_mapping():
    assert checker_verdict(IsolateMeta(status="OK", exitcode=0)) is Verdict.OK
    assert checker_verdict(IsolateMeta(status="RE", exitcode=1)) is Verdict.WA
    assert checker_verdict(IsolateMeta(status="RE", exitcode=2)) is Verdict.WA
    assert checker_verdict(IsolateMeta(status="RE", exitcode=7)) is Verdict.PT
    assert checker_verdict(IsolateMeta(status="RE", exitcode=3)) is Verdict.CF
    assert checker_verdict(IsolateMeta(status="TO")) is Verdict.CF
    assert checker_verdict(IsolateMeta(status="SG")) is Verdict.CF
    assert checker_verdict(IsolateMeta(status="XX")) is Verdict.FL
    assert checker_verdict(IsolateMeta(status="OK", cg_oom_killed=True)) is Verdict.CF


def test_tool_run_job_result_roundtrip(storage: FileStorage):
    result = ToolRunJobResult(
        stdout_digest=storage.upload_bytes(b"stdout"),
        stderr_digest=storage.upload_bytes(b"stderr"),
        meta=IsolateMeta(status="OK", exitcode=0, message=""),
    )
    digest = result.upload(storage)
    loaded = ToolRunJobResult.from_storage(storage, digest)
    assert loaded == result
    assert loaded.to_dict() == result.to_dict()


def test_tool_run_job_result_from_dict():
    data = {
        "stdout_digest": "a" * 64,
        "stderr_digest": "b" * 64,
        "meta": IsolateMeta(status="RE", exitcode=1, message="").to_dict(),
    }
    result = ToolRunJobResult.from_dict(data)
    assert result.stdout_digest == data["stdout_digest"]
    assert result.meta.exitcode == 1


def test_tool_run_job_make_input_digest_requires_program(storage: FileStorage):
    with pytest.raises(ValueError, match="exactly one"):
        ToolRunJob.make_input_digest(storage)
    with pytest.raises(ValueError, match="exactly one"):
        ToolRunJob.make_input_digest(
            storage, program=b"x", program_digest="a" * 64
        )


def test_tool_run_job_make_input_digest(storage: FileStorage):
    input_digest = ToolRunJob.make_input_digest(
        storage,
        program=b"binary",
        files=[(b"in", "input.txt")],
        file_digests=[("c" * 64, "answer.txt")],
        command=["input.txt", "output.txt", "answer.txt"],
        file_size_limit_kb=4096,
        aspace_limit_kb=2048,
    )
    arguments = storage.download_dictionary(input_digest)
    assert arguments["job_type"] == "tool_run"
    assert storage.exists(arguments["program_digest"])
    assert arguments["files_paths"] == ["input.txt", "answer.txt"]
    assert arguments["files_digests"][1] == "c" * 64
    assert arguments["command"] == ["input.txt", "output.txt", "answer.txt"]
    assert arguments["stdin_digest"] is None
    assert arguments["file_size_limit_kb"] == 4096
    assert arguments["aspace_limit_kb"] == 2048


def test_tool_run_job_parse_arguments(storage: FileStorage):
    input_digest = ToolRunJob.make_input_digest(
        storage,
        program=b"binary",
        files=[(b"in", "input.txt")],
        command=["input.txt"],
        stdin=b"seed",
        time_limit_ms=2000,
        wall_time_ms=4000,
        memory_limit_mb=128,
        file_size_limit_kb=8192,
    )
    job = ToolRunJob("tool-1")
    arguments = job._parse_arguments(
        JobContext(input_digest=input_digest, storage=storage)
    )
    assert arguments.command == ["input.txt"]
    assert arguments.limits.time_limit_ms == 2000
    assert arguments.limits.wall_time_ms == 4000
    assert arguments.limits.memory_limit_mb == 128
    assert arguments.limits.file_size_limit_kb == 8192
    assert storage.download_bytes(arguments.stdin_digest) == b"seed"


@pytest.mark.slow
def test_tool_run_job_generator_stdout(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
):
    program = _build_binary(
        b"#include <iostream>\nint main(){std::cout<<\"generated\\n\";}\n"
    )
    job = ToolRunJob("generate")
    job.execute(
        JobContext(
            input_digest=ToolRunJob.make_input_digest(
                storage, program=program, command=[]
            ),
            storage=storage,
            isolate_factory=isolate_factory,
        )
    )
    assert job.get_status() is JobStatus.COMPLETED
    result = job.get_result(storage)
    assert result is not None
    assert storage.download_bytes(result.stdout_digest) == b"generated\n"


@pytest.mark.slow
def test_tool_run_job_checker_ok_and_wa(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    test_assets_path: Path,
):
    checker = _build_binary((test_assets_path / "exact_checker.cpp").read_bytes())

    def run_checker(output: bytes, answer: bytes) -> ToolRunJobResult:
        job = ToolRunJob("check")
        job.execute(
            JobContext(
                input_digest=ToolRunJob.make_input_digest(
                    storage,
                    program=checker,
                    files=[
                        (b"", "input.txt"),
                        (output, "output.txt"),
                        (answer, "answer.txt"),
                    ],
                    command=["input.txt", "output.txt", "answer.txt"],
                ),
                storage=storage,
                isolate_factory=isolate_factory,
            )
        )
        assert job.get_status() is JobStatus.COMPLETED
        result = job.get_result(storage)
        assert result is not None
        return result

    ok = run_checker(b"yes\n", b"yes\n")
    assert checker_verdict(ok.meta) is Verdict.OK
    wa = run_checker(b"yes\n", b"no\n")
    assert checker_verdict(wa.meta) is Verdict.WA
    assert wa.meta.exitcode == 1


def test_tool_run_rejects_reserved_file_path(storage: FileStorage):
    with pytest.raises(ValueError, match="reserved"):
        ToolRunJob.make_input_digest(
            storage,
            program=b"binary",
            files=[(b"evil", "tool")],
        )
