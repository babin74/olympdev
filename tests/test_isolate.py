import shutil
import threading
import time

import pytest
from olympdev.isolate.runner import IsolateSandBox
from olympdev.isolate.types import IsolateLimits
from pathlib import Path

# g++/collect2 need PATH to find `ld`; isolate clears the environment by default.
COMPILER_ENV = {'PATH': '/usr/bin:/bin'}

pytestmark = pytest.mark.slow

@pytest.fixture
def isolate_sandbox():
    with IsolateSandBox(1) as sandbox:
        yield sandbox

def test_isolate_sandbox(isolate_sandbox):
    assert isolate_sandbox.box_root is not None
    assert isolate_sandbox.box_dir is not None
    assert isolate_sandbox.box_root.exists()
    assert isolate_sandbox.box_dir.exists()
    assert isolate_sandbox.box_root.is_dir()
    assert isolate_sandbox.box_dir.is_dir()
    
def test_empty_sandbox(test_assets_path: Path):
    with IsolateSandBox(1) as sandbox:
        sandbox.copy2(test_assets_path / 'sample_file.txt', 'sample_file.txt')
        assert (sandbox.box_dir / 'sample_file.txt').exists()
        assert (sandbox.box_dir / 'sample_file.txt').read_text() == 'Hello, world!'
    with IsolateSandBox(1) as sandbox:
        assert not (sandbox.box_dir / 'sample_file.txt').exists()
        try:
            sandbox.copy2(test_assets_path / 'sample_file.txt', '../sample_file.txt')
            assert False
        except ValueError:
            assert True

def test_sandbox_run(isolate_sandbox):
    meta = isolate_sandbox.run(
        ['/usr/bin/echo', 'Hello, world!'],
        IsolateLimits(time_limit_ms=1000, memory_limit_mb=64),
        stdout_file='stdout.txt',
    )

    assert meta.status == 'OK'
    assert meta.message == ''
    assert meta.exitcode == 0
    assert meta.exitsig == 0
    assert meta.killed is False
    assert meta.cg_oom_killed is False
    assert meta.time > 0
    assert meta.time_wall > 0
    assert meta.max_rss_kb > 0
    assert meta.cg_memory_kb > 0
    assert meta.csw_forced >= 0
    assert meta.csw_voluntary >= 0

    assert isolate_sandbox.read_text('stdout.txt') == 'Hello, world!\n'


def test_compile_and_run(isolate_sandbox, test_assets_path: Path):
    gxx = shutil.which('g++')
    if gxx is None:
        pytest.skip('g++ is not available')

    isolate_sandbox.copy2(test_assets_path / 'hello.cpp', 'hello.cpp')
    compile_meta = isolate_sandbox.run(
        [gxx, '-O2', '-o', 'hello', 'hello.cpp'],
        IsolateLimits(time_limit_ms=10000, memory_limit_mb=512),
        stdout_file='compile_stdout.txt',
        stderr_file='compile_stderr.txt',
        processes=100,
        env=COMPILER_ENV,
    )

    assert compile_meta.status == 'OK', isolate_sandbox.read_text('compile_stderr.txt')
    assert compile_meta.exitcode == 0
    assert isolate_sandbox.box_path('hello').is_file()

    meta = isolate_sandbox.run(
        ['./hello'],
        IsolateLimits(time_limit_ms=1000, memory_limit_mb=64),
        stdout_file='stdout.txt',
        stderr_file='stderr.txt',
    )

    assert meta.status == 'OK'
    assert meta.exitcode == 0
    assert isolate_sandbox.read_text('stdout.txt') == 'Hello, output world!\n'
    assert isolate_sandbox.read_text('stderr.txt') == 'Hello, debug world!\n'

def test_compile_and_run_memory(isolate_sandbox, test_assets_path: Path):
    gxx = shutil.which('g++')
    if gxx is None:
        pytest.skip('g++ is not available')
    isolate_sandbox.copy2(test_assets_path / 'memory.cpp', 'memory.cpp')
    compile_meta = isolate_sandbox.run(
        [gxx, '-O2', '-o', 'memory', 'memory.cpp'],
        IsolateLimits(time_limit_ms=10000, memory_limit_mb=512),
        stdout_file='compile_stdout.txt',
        stderr_file='compile_stderr.txt',
        processes=100,
        env=COMPILER_ENV,
    )

    assert compile_meta.status == 'OK', isolate_sandbox.read_text('compile_stderr.txt')
    assert compile_meta.exitcode == 0
    assert isolate_sandbox.box_path('memory').is_file()

    for _ in range(20):
        meta = isolate_sandbox.run(
            ['./memory'],
            IsolateLimits(time_limit_ms=1000, memory_limit_mb=128),
            stdout_file='stdout.txt',
            stderr_file='stderr.txt',
        )

        assert meta.status == 'OK'
        assert meta.exitcode == 0
        assert isolate_sandbox.read_text('stdout.txt') == '4278641890\n'


def test_sandbox_lock_serializes_same_box_id():
    order: list[str] = []
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        barrier.wait()
        with IsolateSandBox(2):
            order.append(f"{name}-in")
            time.sleep(0.2)
            order.append(f"{name}-out")

    threads = [
        threading.Thread(target=worker, args=("a",)),
        threading.Thread(target=worker, args=("b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(order) == 4
    assert order[0].endswith("-in")
    assert order[1].endswith("-out")
    assert order[2].endswith("-in")
    assert order[3].endswith("-out")


def test_sandbox_lock_timeout():
    started = threading.Event()

    def holder() -> None:
        with IsolateSandBox(3):
            started.set()
            time.sleep(1.5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert started.wait(timeout=5)
    with pytest.raises(TimeoutError, match="timed out waiting for isolate box 3"):
        with IsolateSandBox(3, lock_timeout_sec=0.2):
            pass
    thread.join()