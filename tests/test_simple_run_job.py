import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from olympdev.isolate import IsolateFactory, IsolateMeta
from olympdev.storage.filestorage import FileStorage
from olympdev.worker.job import JobContext, JobStatus
from olympdev.worker.jobs.simple_run import SimpleRunJob, SimpleRunJobResult


def _build_hello_binary(test_assets_path: Path) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "hello.cpp"
        binary = Path(tmp) / "hello"
        source.write_bytes((test_assets_path / "hello.cpp").read_bytes())
        subprocess.run(
            ["g++", "-O2", "-o", str(binary), str(source)],
            check=True,
            capture_output=True,
        )
        return binary.read_bytes()


@pytest.fixture
def hello_binary(test_assets_path: Path) -> bytes:
    if shutil.which("g++") is None:
        pytest.skip("g++ is not available")
    return _build_hello_binary(test_assets_path)


def test_simple_run_job_result_roundtrip(storage: FileStorage):
    result = SimpleRunJobResult(
        stdout_digest=storage.upload_bytes(b"stdout"),
        stderr_digest=storage.upload_bytes(b"stderr"),
        meta=IsolateMeta(status="OK", exitcode=0, message=""),
    )

    digest = result.upload(storage)
    loaded = SimpleRunJobResult.from_storage(storage, digest)

    assert loaded == result
    assert loaded.to_dict() == result.to_dict()


def test_simple_run_job_result_from_dict():
    data = {
        "stdout_digest": "a" * 64,
        "stderr_digest": "b" * 64,
        "meta": IsolateMeta(status="OK", exitcode=0, message="").to_dict(),
    }

    result = SimpleRunJobResult.from_dict(data)

    assert result.stdout_digest == data["stdout_digest"]
    assert result.stderr_digest == data["stderr_digest"]
    assert result.meta.status == "OK"


def test_simple_run_job_make_input_digest(storage: FileStorage, hello_binary: bytes):
    input_digest = SimpleRunJob.make_input_digest(
        storage,
        program=hello_binary,
        input="test input",
        time_limit_ms=1000,
        wall_time_ms=1000,
        memory_limit_mb=64,
    )

    arguments = storage.download_dictionary(input_digest)
    assert arguments["job_type"] == "simple_run"
    assert storage.exists(arguments["program_digest"])
    assert storage.exists(arguments["input_digest"])
    assert storage.download_bytes(arguments["input_digest"]) == b"test input"
    assert arguments["time_limit_ms"] == 1000
    assert arguments["wall_time_ms"] == 1000
    assert arguments["memory_limit_mb"] == 64
    assert arguments["cg_memory_limit_kb"] == 64 * 1024
    assert arguments["aspace_limit_kb"] is None
    assert arguments["file_size_limit_kb"] == 1024 * 16


def test_simple_run_job_make_input_digest_requires_program_or_digest(storage: FileStorage):
    with pytest.raises(ValueError, match="exactly one"):
        SimpleRunJob.make_input_digest(storage)

    with pytest.raises(ValueError, match="exactly one"):
        SimpleRunJob.make_input_digest(
            storage,
            program=b"binary",
            program_digest="a" * 64,
        )


def test_simple_run_job_parse_arguments(storage: FileStorage, hello_binary: bytes):
    input_digest = SimpleRunJob.make_input_digest(
        storage,
        program=hello_binary,
        input=b"",
        time_limit_ms=1000,
        wall_time_ms=2000,
        memory_limit_mb=128,
        cg_memory_limit_kb=4096,
        aspace_limit_kb=2048,
        file_size_limit_kb=8192,
    )
    job = SimpleRunJob("run-1")
    context = JobContext(input_digest=input_digest, storage=storage)

    arguments = job._parse_arguments(context)

    assert storage.exists(arguments.program_digest)
    assert storage.exists(arguments.input_digest)
    assert arguments.limits.time_limit_ms == 1000
    assert arguments.limits.wall_time_ms == 2000
    assert arguments.limits.memory_limit_mb == 128
    assert arguments.limits.cg_memory_limit_kb == 4096
    assert arguments.limits.aspace_limit_kb == 2048
    assert arguments.limits.file_size_limit_kb == 8192


@pytest.mark.slow
def test_simple_run_job_success(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    hello_binary: bytes,
):
    input_digest = SimpleRunJob.make_input_digest(
        storage,
        program=hello_binary,
        input=b"",
        time_limit_ms=1000,
        wall_time_ms=1000,
        memory_limit_mb=64,
    )
    job = SimpleRunJob("run-success")
    context = JobContext(
        input_digest=input_digest,
        storage=storage,
        isolate_factory=isolate_factory,
    )

    job.execute(context)

    assert job.get_status() == JobStatus.COMPLETED
    assert job.get_failure_reason() is None

    result = job.get_result(storage)
    assert result is not None
    assert storage.exists(result.stdout_digest)
    assert storage.exists(result.stderr_digest)
    assert result.meta.status == "OK"
    assert result.meta.exitcode == 0
    assert storage.download_bytes(result.stdout_digest) == b"Hello, output world!\n"
    assert storage.download_bytes(result.stderr_digest) == b"Hello, debug world!\n"


@pytest.mark.slow
def test_simple_run_job_nonzero_exit_is_not_job_failure(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
):
    program = _build_binary(b"#include <cstdlib>\nint main() { return 42; }")

    input_digest = SimpleRunJob.make_input_digest(
        storage,
        program=program,
        input=b"",
        time_limit_ms=1000,
        wall_time_ms=1000,
        memory_limit_mb=64,
    )
    job = SimpleRunJob("run-nonzero-exit")
    job.execute(JobContext(
        input_digest=input_digest,
        storage=storage,
        isolate_factory=isolate_factory,
    ))

    assert job.get_status() == JobStatus.COMPLETED
    result = job.get_result(storage)
    assert result is not None
    assert result.meta.exitcode == 42
    assert result.meta.status == "RE"


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


@pytest.mark.slow
@pytest.mark.parametrize(
    ("source", "expected_status"),
    [
        (b"int main(){while(1){}}", "TO"),
        (b"#include <csignal>\nint main(){std::raise(SIGABRT);}", "SG"),
    ],
)
def test_simple_run_job_program_outcome_statuses_complete_job(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    source: bytes,
    expected_status: str,
):
    program = _build_binary(source)
    input_digest = SimpleRunJob.make_input_digest(
        storage,
        program=program,
        input=b"",
        time_limit_ms=200,
        wall_time_ms=500,
        memory_limit_mb=64,
    )
    job = SimpleRunJob(f"run-{expected_status.lower()}")
    job.execute(JobContext(
        input_digest=input_digest,
        storage=storage,
        isolate_factory=isolate_factory,
    ))

    assert job.get_status() == JobStatus.COMPLETED
    assert job.get_failure_reason() is None

    result = job.get_result(storage)
    assert result is not None
    assert result.meta.status == expected_status
    assert not result.meta.is_sandbox_error()


def test_simple_run_input_digest_without_reload(storage: FileStorage, hello_binary: bytes):
    input_blob = storage.upload_bytes(b"from-digest")
    input_digest = SimpleRunJob.make_input_digest(
        storage,
        program=hello_binary,
        input_digest=input_blob,
    )
    arguments = storage.download_dictionary(input_digest)
    assert arguments["input_digest"] == input_blob


def test_simple_run_rejects_zero_memory(storage: FileStorage, hello_binary: bytes):
    with pytest.raises(ValidationError, match="memory_limit_mb"):
        SimpleRunJob.make_input_digest(
            storage,
            program=hello_binary,
            memory_limit_mb=0,
        )


def test_simple_run_execute_marks_failed_on_bad_digest(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
):
    digest = storage.upload_dictionary({"not": "a-run-job"})
    job = SimpleRunJob("bad-digest")
    job.execute(JobContext(
        input_digest=digest,
        storage=storage,
        isolate_factory=isolate_factory,
    ))
    assert job.get_status() is JobStatus.FAILED
    assert job.get_failure_reason() is not None
