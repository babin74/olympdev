"""Submit a .cpp as an aplusb participant solution and inspect isolate cutoffs.

Attack tests must go through ``ProblemTestingPipeline`` the same way a real
submission does. Payloads never run on the host.

Payloads are sized so a missed isolate limit cannot reasonably OOM the WSL
utility VM (cgroup churn + unbounded fork/malloc is what knocked WSL down).
"""

from __future__ import annotations

import resource
import shutil
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path

import pytest

from olympdev.isolate import IsolateFactory, IsolateLimits, IsolateMeta
from olympdev.problem.types import (
    ProblemConfiguration,
    ProblemSolution,
    ProblemSource,
    ProblemSourceType,
    ProblemTest,
)
from olympdev.resources import asset_file
from olympdev.storage.filestorage import FileStorage
from olympdev.worker.job import JobContext, JobStatus
from olympdev.worker.jobs.compile import COMPILE_JOB_LIMITS, CompileJob, CompileJobResult
from olympdev.worker.jobs.simple_run import SimpleRunJob, SimpleRunJobResult
from olympdev.worker.pipeline import PipelineContext, PipelineStatus, run_pipeline
from olympdev.worker.pipelines.problem_testing import ProblemTestingPipeline
from olympdev.worker.verdict import Verdict

PAYLOADS = Path(__file__).resolve().parent / "payloads"

HOST_MARKERS = (
    Path("/tmp/olympdev-attack-pwned"),
    Path("/var/tmp/olympdev-attack-pwned"),
)

APLUSB_TIME_MS = 1000
APLUSB_WALL_MS = 2000
APLUSB_MEMORY_MB = 512
APLUSB_CG_MEM_KB = APLUSB_MEMORY_MB * 256
APLUSB_FILE_SIZE_KB = 16 * 1024

COMPILE_TIME_CAP_SEC = COMPILE_JOB_LIMITS.time_sec() + 2.0
COMPILE_WALL_CAP_SEC = COMPILE_JOB_LIMITS.wall_sec() + 2.0
COMPILE_CG_CAP_KB = COMPILE_JOB_LIMITS.cg_memory_kb() + 64 * 1024
RUN_TIME_CAP_SEC = APLUSB_TIME_MS / 1000.0 + 1.5
RUN_WALL_CAP_SEC = APLUSB_WALL_MS / 1000.0 + 1.5
RUN_CG_CAP_KB = APLUSB_CG_MEM_KB + 64 * 1024

COMPILE_PIPELINE_CAP_SEC = 45.0
RUNTIME_PIPELINE_CAP_SEC = 90.0
HOST_RSS_CAP_KB = 1_500_000
MIN_HOST_AVAILABLE_KB = 512 * 1024

_STATIC_SAMPLES = ("tests/sample-01.txt", "tests/sample-02.txt")
_STATIC_TESTS = ("tests/manual-01.txt",)
_GENERATED_TESTS = (
    ("gen -c=1e3", "random-01.txt"),
    ("gen -c=1e6", "random-02.txt"),
    ("gen -c=1e9", "random-03.txt"),
)

CONTAINED_VERDICTS = frozenset({
    Verdict.CE, Verdict.TL, Verdict.ML, Verdict.RE, Verdict.WA,
})


def running_on_wsl() -> bool:
    try:
        text = Path("/proc/version").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "microsoft" in text.lower() or "wsl" in text.lower()


def mem_available_kb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1])
    except OSError:
        return 0
    return 0


def payload_path(name: str) -> Path:
    path = PAYLOADS / name
    if not path.is_file() or path.suffix != ".cpp":
        raise FileNotFoundError(name)
    return path


def payload_bytes(name: str) -> bytes:
    data = payload_path(name).read_bytes()
    if len(data) > 256 * 1024:
        raise ValueError(f"payload {name} is too large to upload from the host")
    return data


def aplusb_configuration() -> ProblemConfiguration:
    return ProblemConfiguration(
        time_limit=APLUSB_TIME_MS,
        memory_limit=APLUSB_MEMORY_MB,
        input_file_name="input.txt",
        output_file_name="output.txt",
        samples=[ProblemTest(input_file=path) for path in _STATIC_SAMPLES],
        tests=[
            ProblemTest(input_file=_STATIC_TESTS[0]),
            *[
                ProblemTest(gen_cmd=cmd, name=name)
                for cmd, name in _GENERATED_TESTS
            ],
        ],
        solutions=[
            ProblemSolution(file="solutions/sol-ac.cpp", tag="main"),
            ProblemSolution(file="solutions/sol-wa.cpp", tag="WA"),
            ProblemSolution(file="solutions/sol-tl.cpp", tag="TL"),
        ],
        sources=[
            ProblemSource(file="source/gen.cpp", type=ProblemSourceType.GEN),
            ProblemSource(file="source/ncmp.cpp", type=ProblemSourceType.CHECK),
            ProblemSource(file="source/valid.cpp", type=ProblemSourceType.VALID),
        ],
    )


def gxx() -> str:
    path = shutil.which("g++")
    if path is None:
        pytest.skip("g++ is not available")
    return path


def submit_aplusb_cpp(
    storage: FileStorage,
    aplusb_dir: Path,
    source: Path | bytes,
    *,
    sandbox_name: str = "main.cpp",
) -> str:
    compiler = gxx()
    with asset_file("testlib.h") as testlib:
        return ProblemTestingPipeline.make_input_digest(
            storage,
            sources=[(source, sandbox_name)],
            command=[compiler, "-O2", "-o", "solution", 0],
            checker_sources=[
                (aplusb_dir / "source" / "ncmp.cpp", "ncmp.cpp"),
                (testlib, "testlib.h"),
            ],
            checker_command=[compiler, "-O2", "-o", "checker", 0],
            model_sources=[(aplusb_dir / "solutions" / "sol-ac.cpp", "model.cpp")],
            model_command=[compiler, "-O2", "-o", "model", 0],
            generator_sources=[
                (aplusb_dir / "source" / "gen.cpp", "gen.cpp"),
                (testlib, "testlib.h"),
            ],
            generator_command=[compiler, "-O2", "-o", "gen", 0],
            validator_sources=[
                (aplusb_dir / "source" / "valid.cpp", "valid.cpp"),
                (testlib, "testlib.h"),
            ],
            validator_command=[compiler, "-O2", "-o", "valid", 0],
            configuration=aplusb_configuration(),
            problem_dir=aplusb_dir,
            problem_name="aplusb",
        )


class SubmissionOutcome:
    def __init__(
        self,
        *,
        pipeline: ProblemTestingPipeline,
        storage: FileStorage,
        elapsed_sec: float,
        source_name: str,
    ) -> None:
        self.pipeline = pipeline
        self.storage = storage
        self.elapsed_sec = elapsed_sec
        self.source_name = source_name
        self.result = pipeline.get_result(storage)

    @property
    def compile_meta(self) -> IsolateMeta | None:
        if self.result is None or self.result.compile_digest is None:
            return None
        compiled = CompileJobResult.from_storage(
            self.storage, self.result.compile_digest
        )
        return compiled.meta

    def run_results(self) -> list[SimpleRunJobResult]:
        if self.result is None:
            return []
        loaded: list[SimpleRunJobResult] = []
        for case in self.result.tests:
            if case.run_digest is None:
                continue
            loaded.append(
                SimpleRunJobResult.from_storage(self.storage, case.run_digest)
            )
        return loaded

    def run_stdouts(self) -> list[bytes]:
        out: list[bytes] = []
        for run in self.run_results():
            out.append(self.storage.download_bytes(run.stdout_digest))
        return out


def run_aplusb_submission(
    storage: FileStorage,
    isolate_factory: IsolateFactory,
    aplusb_dir: Path,
    source: Path | bytes,
    *,
    source_name: str = "main.cpp",
    pipeline_cap_sec: float = RUNTIME_PIPELINE_CAP_SEC,
) -> SubmissionOutcome:
    input_digest = submit_aplusb_cpp(
        storage, aplusb_dir, source, sandbox_name=source_name
    )
    pipeline = ProblemTestingPipeline("aplusb-attack")
    started = time.monotonic()
    run_pipeline(
        pipeline,
        PipelineContext(
            input_digest=input_digest,
            storage=storage,
            isolate_factory=isolate_factory,
        ),
    )
    elapsed = time.monotonic() - started
    if elapsed > pipeline_cap_sec:
        pytest.fail(
            f"pipeline ran {elapsed:.1f}s (cap {pipeline_cap_sec:.0f}s); "
            "isolate did not cut the payload off in time"
        )
    return SubmissionOutcome(
        pipeline=pipeline,
        storage=storage,
        elapsed_sec=elapsed,
        source_name=source_name,
    )


def assert_pipeline_survived(outcome: SubmissionOutcome) -> None:
    status = outcome.pipeline.get_status()
    assert status is PipelineStatus.COMPLETED, (
        f"{outcome.source_name}: pipeline {status} "
        f"reason={outcome.pipeline.get_failure_reason()!r}"
    )
    assert outcome.result is not None
    assert outcome.result.verdict is not Verdict.FL
    assert outcome.result.verdict is not Verdict.CF


def assert_contained(
    outcome: SubmissionOutcome,
    allowed: Iterable[Verdict] = CONTAINED_VERDICTS,
) -> None:
    assert_pipeline_survived(outcome)
    assert outcome.result is not None
    allowed_set = frozenset(allowed)
    assert outcome.result.verdict in allowed_set, (
        f"{outcome.source_name}: verdict {outcome.result.verdict} "
        f"not in {sorted(v.value for v in allowed_set)}"
    )
    assert outcome.result.verdict is not Verdict.OK


def assert_compile_limits(outcome: SubmissionOutcome) -> None:
    meta = outcome.compile_meta
    assert meta is not None, f"{outcome.source_name}: missing compile meta"
    assert not meta.is_sandbox_error()
    assert meta.time <= COMPILE_TIME_CAP_SEC, (
        f"compile cpu {meta.time}s exceeded cap {COMPILE_TIME_CAP_SEC}s"
    )
    assert meta.time_wall <= COMPILE_WALL_CAP_SEC, (
        f"compile wall {meta.time_wall}s exceeded cap {COMPILE_WALL_CAP_SEC}s"
    )
    assert meta.cg_memory_kb <= COMPILE_CG_CAP_KB, (
        f"compile cg-mem {meta.cg_memory_kb} KiB exceeded cap {COMPILE_CG_CAP_KB}"
    )


def assert_run_limits(outcome: SubmissionOutcome) -> None:
    runs = outcome.run_results()
    assert runs, f"{outcome.source_name}: expected at least one solution run"
    for run in runs:
        meta = run.meta
        assert not meta.is_sandbox_error()
        assert meta.time <= RUN_TIME_CAP_SEC, (
            f"run cpu {meta.time}s exceeded cap {RUN_TIME_CAP_SEC}s"
        )
        assert meta.time_wall <= RUN_WALL_CAP_SEC, (
            f"run wall {meta.time_wall}s exceeded cap {RUN_WALL_CAP_SEC}s"
        )
        assert meta.cg_memory_kb <= RUN_CG_CAP_KB, (
            f"run cg-mem {meta.cg_memory_kb} KiB exceeded cap {RUN_CG_CAP_KB}"
        )


def assert_host_rss_bounded() -> None:
    self_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    child_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    assert self_rss <= HOST_RSS_CAP_KB, (
        f"pytest RSS {self_rss} KiB exceeded host cap {HOST_RSS_CAP_KB}"
    )
    assert child_rss <= HOST_RSS_CAP_KB, (
        f"child RSS {child_rss} KiB exceeded host cap {HOST_RSS_CAP_KB}"
    )


def assert_host_markers_absent() -> None:
    leftover = [path for path in HOST_MARKERS if path.exists()]
    assert leftover == [], f"payload wrote outside the sandbox: {leftover}"


def probe_isolate_enforces_limits(isolate_factory: IsolateFactory) -> None:
    """Abort the attack suite if isolate would not contain a runaway program."""
    available = mem_available_kb()
    if available and available < MIN_HOST_AVAILABLE_KB:
        pytest.exit(
            f"only {available} KiB MemAvailable; refusing attack payloads "
            "(this is how a previous run knocked WSL over)",
            returncode=2,
        )

    wall_probe = IsolateLimits(
        time_limit_ms=200,
        wall_time_ms=400,
        memory_limit_mb=32,
        extra_time_ms=0,
    )
    with isolate_factory.get_sandbox() as sandbox:
        meta = sandbox.run(["/bin/sleep", "30"], wall_probe)
    if meta.status != "TO" or meta.time_wall > 3.0:
        pytest.exit(
            "isolate did not enforce wall-time (sleep 30s survived); "
            "refusing to run attack payloads",
            returncode=2,
        )

    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("g++ is not available")

    source = (
        b"#include <cstddef>\n"
        b"int main() {\n"
        b"  const std::size_t n = 80ull * 1024 * 1024;\n"
        b"  volatile char* p = new char[n];\n"
        b"  for (std::size_t i = 0; i < n; i += 4096) p[i] = 1;\n"
        b"  return 0;\n"
        b"}\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        storage = FileStorage(Path(tmp) / "storage")
        compile_digest = CompileJob.make_input_digest(
            storage,
            sources=[(source, "ml.cpp")],
            command=[compiler, "-O2", "-o", "ml", 0],
        )
        compile_job = CompileJob("probe-ml-compile")
        compile_job.execute(JobContext(
            input_digest=compile_digest,
            storage=storage,
            isolate_factory=isolate_factory,
        ))
        if compile_job.get_status() is not JobStatus.COMPLETED:
            pytest.exit(
                "could not compile memory probe; refusing attack payloads",
                returncode=2,
            )
        compiled = compile_job.get_result(storage)
        assert compiled is not None and compiled.program_digest is not None
        run_digest = SimpleRunJob.make_input_digest(
            storage,
            program_digest=compiled.program_digest,
            input=b"",
            time_limit_ms=1000,
            wall_time_ms=1000,
            memory_limit_mb=16,
            cg_memory_limit_kb=16 * 1024,
        )
        run_job = SimpleRunJob("probe-ml-run")
        run_job.execute(JobContext(
            input_digest=run_digest,
            storage=storage,
            isolate_factory=isolate_factory,
        ))
        run_result = run_job.get_result(storage)
        if run_result is None:
            pytest.exit("memory probe produced no result", returncode=2)
        killed = (
            run_result.meta.cg_oom_killed
            or run_result.meta.status in {"SG", "TO", "RE"}
            or not run_result.meta.is_successful_termination()
        )
        if not killed:
            pytest.exit(
                "isolate did not enforce memory limit (80 MiB survived 16 MiB); "
                "refusing to run attack payloads",
                returncode=2,
            )
