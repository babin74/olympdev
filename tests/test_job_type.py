import pytest

from olympdev.storage.filestorage import FileStorage
from olympdev.worker.cache.policy import CachePolicy
from olympdev.worker.job import JobContext, resolve_job_class
from olympdev.worker.jobs.compile import CompileJob, SolutionCompileJob
from olympdev.worker.jobs.simple_run import SimpleRunJob, SolutionRunJob
from olympdev.worker.jobs.tool_run import ToolRunJob, ValidatorJob


def test_make_input_digest_includes_job_type(storage: FileStorage):
    simple = SimpleRunJob.make_input_digest(
        storage, program=b"bin", input=b""
    )
    tool = ToolRunJob.make_input_digest(storage, program=b"bin")
    validator = ValidatorJob.make_input_digest(storage, program=b"bin")
    solution = SolutionRunJob.make_input_digest(
        storage, program=b"bin", input=b""
    )

    assert storage.download_dictionary(simple)["job_type"] == "simple_run"
    assert storage.download_dictionary(tool)["job_type"] == "tool_run"
    assert storage.download_dictionary(validator)["job_type"] == "validator"
    assert storage.download_dictionary(solution)["job_type"] == "solution_run"


def test_resolve_job_class(storage: FileStorage):
    simple = SimpleRunJob.make_input_digest(
        storage, program=b"bin", input=b""
    )
    validator = ValidatorJob.make_input_digest(storage, program=b"bin")
    compile_digest = SolutionCompileJob._upload_input(
        storage,
        {
            "files_digests": ["a" * 64],
            "files_paths": ["main.cpp"],
            "command": ["/usr/bin/g++", "-o", "a", 0],
        },
    )

    assert resolve_job_class(storage, simple) is SimpleRunJob
    assert resolve_job_class(storage, validator) is ValidatorJob
    assert resolve_job_class(storage, compile_digest) is SolutionCompileJob


def test_parse_arguments_rejects_job_type_mismatch(storage: FileStorage):
    digest = SimpleRunJob.make_input_digest(
        storage, program=b"bin", input=b""
    )
    job = ValidatorJob("valid-1")
    with pytest.raises(ValueError, match="job_type mismatch"):
        job._parse_arguments(JobContext(input_digest=digest, storage=storage))


def test_upload_input_rejects_reserved_job_type(storage: FileStorage):
    with pytest.raises(ValueError, match="reserved"):
        CompileJob._upload_input(storage, {"job_type": "compile"})


def test_role_cache_policies():
    assert SolutionRunJob("run").get_cache_policy() is CachePolicy.SolutionRun
    assert ValidatorJob("valid").get_cache_policy() is CachePolicy.Validator
    assert SimpleRunJob("run").get_cache_policy() is CachePolicy.Never
    assert CompileJob("compile").get_cache_policy() is CachePolicy.Never
    assert ToolRunJob("tool").get_cache_policy() is CachePolicy.Never


def test_resolve_job_class_unknown_type(storage: FileStorage):
    digest = storage.upload_dictionary({"job_type": "does-not-exist"})
    with pytest.raises(ValueError, match="unknown job_type"):
        resolve_job_class(storage, digest)
