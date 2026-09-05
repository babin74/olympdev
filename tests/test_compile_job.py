import shutil
from pathlib import Path

import pytest

from olympdev.isolate import IsolateFactory, IsolateMeta
from olympdev.storage.filestorage import FileStorage
from olympdev.worker.job import JobContext, JobStatus
from olympdev.worker.jobs.compile import (
    CompileJob,
    CompileJobArguments,
    CompileJobResult,
)
from olympdev.worker.jobs.simple_run import SimpleRunJob, SimpleRunJobResult


def test_compile_job_arguments_to_cli():
    arguments = CompileJobArguments(
        file_digests=["digest-a", "digest-b"],
        file_paths=[Path("hello.cpp"), Path("helper.cpp")],
        command=["/usr/bin/g++", "-O2", "-o", "hello", 0, 1],
    )

    assert arguments.to_cli_arguments(["hello.cpp", "helper.cpp"]) == [
        "/usr/bin/g++",
        "-O2",
        "-o",
        "hello",
        "hello.cpp",
        "helper.cpp",
    ]


def test_compile_job_result_roundtrip(storage: FileStorage):
    result = CompileJobResult(
        stdout_digest=storage.upload_bytes(b"stdout"),
        stderr_digest=storage.upload_bytes(b"stderr"),
        meta=IsolateMeta(status="OK", exitcode=0, message=""),
        program_digest=storage.upload_bytes(b"binary"),
    )

    digest = result.upload(storage)
    loaded = CompileJobResult.from_storage(storage, digest)

    assert loaded == result
    assert loaded.to_dict() == result.to_dict()


def test_compile_job_result_from_dict():
    data = {
        "stdout_digest": "a" * 64,
        "stderr_digest": "b" * 64,
        "meta": IsolateMeta(status="RE", exitcode=1, message="failed").to_dict(),
        "program_digest": None,
    }

    result = CompileJobResult.from_dict(data)

    assert result.stdout_digest == data["stdout_digest"]
    assert result.stderr_digest == data["stderr_digest"]
    assert result.meta.status == "RE"
    assert result.meta.exitcode == 1
    assert result.meta.message == "failed"
    assert result.program_digest is None


def test_compile_job_make_input_digest(storage: FileStorage, test_assets_path: Path):
    gxx = shutil.which("g++")
    if gxx is None:
        pytest.skip("g++ is not available")
    input_digest = CompileJob.make_input_digest(
        storage,
        sources=[(test_assets_path / "hello.cpp", "hello.cpp")],
        command=[gxx, "-O2", "-o", "hello", 0],
    )

    arguments = storage.download_dictionary(input_digest)
    assert arguments["job_type"] == "compile"
    assert len(arguments["files_digests"]) == 1
    assert arguments["files_paths"] == ["hello.cpp"]
    assert arguments["command"] == [gxx, "-O2", "-o", "hello", 0]


def test_compile_job_parse_arguments(storage: FileStorage, test_assets_path: Path):
    gxx = shutil.which("g++")
    if gxx is None:
        pytest.skip("g++ is not available")
    input_digest = CompileJob.make_input_digest(
        storage,
        sources=[(test_assets_path / "hello.cpp", "hello.cpp")],
        command=[gxx, "-O2", "-o", "hello", 0],
    )
    job = CompileJob("compile-1")
    context = JobContext(input_digest=input_digest, storage=storage)

    arguments = job._parse_arguments(context)

    assert len(arguments.file_digests) == 1
    assert storage.exists(arguments.file_digests[0])
    assert arguments.file_paths == [Path("hello.cpp")]
    assert arguments.command == [gxx, "-O2", "-o", "hello", 0]


@pytest.mark.slow
def test_compile_job_success(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    test_assets_path: Path,
):
    gxx = shutil.which("g++")
    if gxx is None:
        pytest.skip("g++ is not available")

    input_digest = CompileJob.make_input_digest(
        storage,
        sources=[(test_assets_path / "hello.cpp", "hello.cpp")],
        command=[gxx, "-O2", "-o", "hello", 0],
    )
    job = CompileJob("compile-success")
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
    assert result.program_digest is not None
    assert storage.exists(result.program_digest)


@pytest.mark.slow
def test_compile_job_compilation_failure(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
):
    gxx = shutil.which("g++")
    if gxx is None:
        pytest.skip("g++ is not available")

    input_digest = CompileJob.make_input_digest(
        storage,
        sources=[(b"int main() { syntax error", "bad.cpp")],
        command=[gxx, "-O2", "-o", "bad", 0],
    )
    job = CompileJob("compile-failure")
    context = JobContext(
        input_digest=input_digest,
        storage=storage,
        isolate_factory=isolate_factory,
    )

    job.execute(context)

    assert job.get_status() == JobStatus.FAILED
    assert job.get_failure_reason() == "Compilation failed"

    result = job.get_result(storage)
    assert result is not None
    assert storage.exists(result.stdout_digest)
    assert storage.exists(result.stderr_digest)
    assert result.meta.exitcode != 0
    assert result.program_digest is None
    assert storage.download_bytes(result.stderr_digest)


@pytest.mark.slow
def test_compile_and_run(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    test_assets_path: Path,
):
    gxx = shutil.which("g++")
    if gxx is None:
        pytest.skip("g++ is not available")

    compile_input_digest = CompileJob.make_input_digest(
        storage,
        sources=[(test_assets_path / "hello.cpp", "hello.cpp")],
        command=[gxx, "-O2", "-o", "hello", 0],
    )
    compile_job = CompileJob("compile-and-run")
    compile_job.execute(JobContext(
        input_digest=compile_input_digest,
        storage=storage,
        isolate_factory=isolate_factory,
    ))

    assert compile_job.get_status() == JobStatus.COMPLETED
    compile_result = compile_job.get_result(storage)
    assert compile_result is not None
    assert compile_result.program_digest is not None

    run_input_digest = SimpleRunJob.make_input_digest(
        storage,
        program_digest=compile_result.program_digest,
        input=b"",
        time_limit_ms=1000,
        wall_time_ms=1000,
        memory_limit_mb=64,
    )
    run_job = SimpleRunJob("run-after-compile")
    run_job.execute(JobContext(
        input_digest=run_input_digest,
        storage=storage,
        isolate_factory=isolate_factory,
    ))

    assert run_job.get_status() == JobStatus.COMPLETED
    run_result = run_job.get_result(storage)
    assert run_result is not None
    assert run_result.meta.status == "OK"
    assert run_result.meta.exitcode == 0
    assert storage.download_bytes(run_result.stdout_digest) == b"Hello, output world!\n"
    assert storage.download_bytes(run_result.stderr_digest) == b"Hello, debug world!\n"


def _gxx() -> str:
    gxx = shutil.which("g++")
    if gxx is None:
        pytest.skip("g++ is not available")
    return gxx


def test_compile_rejects_plugin_flag(storage: FileStorage, test_assets_path: Path):
    with pytest.raises(ValueError, match="not allowed"):
        CompileJob.make_input_digest(
            storage,
            sources=[(test_assets_path / "hello.cpp", "hello.cpp")],
            command=[_gxx(), "-fplugin=./evil.so", "-o", "hello", 0],
        )


def test_compile_rejects_relative_compiler(storage: FileStorage, test_assets_path: Path):
    with pytest.raises(ValueError, match="absolute"):
        CompileJob.make_input_digest(
            storage,
            sources=[(test_assets_path / "hello.cpp", "hello.cpp")],
            command=["g++", "-o", "hello", 0],
        )


def test_compile_rejects_reserved_source_path(storage: FileStorage, test_assets_path: Path):
    with pytest.raises(ValueError, match="reserved"):
        CompileJob.make_input_digest(
            storage,
            sources=[(test_assets_path / "hello.cpp", "compile_stdout.txt")],
            command=[_gxx(), "-o", "hello", 0],
        )
