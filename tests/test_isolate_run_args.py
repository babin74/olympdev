from pathlib import Path

import pytest
from pydantic import ValidationError

from olympdev.isolate.runner import IsolateSandBox, _build_init_args, _build_run_args
from olympdev.isolate._dir_rules import (
    IsolateDirectoryRule,
    IsolateDirectoryRules,
)
from olympdev.isolate.profiles import (
    COMPILE_PROFILE,
    RUN_PROFILE,
    TOOL_PROFILE,
    SandboxProfile,
)
from olympdev.isolate.types import (
    IsolateLimits,
    IsolateMeta,
    IsolateQuota,
)


def test_build_run_args_extra_time_is_limit_plus_grace():
    args = _build_run_args(
        IsolateLimits(time_limit_ms=1000, memory_limit_mb=64),
        ["/usr/bin/true"],
        meta_path="/tmp/meta",
    )
    x_index = args.index("-x")
    assert args[x_index + 1] == "1.500"


def test_build_run_args_aspace_and_cg_memory_independent():
    args = _build_run_args(
        IsolateLimits(
            time_limit_ms=1000,
            memory_limit_mb=64,
            aspace_limit_kb=2048,
            cg_memory_limit_kb=4096,
        ),
        ["/usr/bin/true"],
        meta_path="/tmp/meta",
    )
    assert args[args.index("-m") + 1] == "2048"
    cg_index = args.index("--cg-mem")
    assert args[cg_index + 1] == "4096"


def test_build_run_args_always_emits_memory_limits():
    """There is no way to ask for an unbounded run: -m and --cg-mem are always set."""
    args = _build_run_args(
        IsolateLimits(time_limit_ms=1000, memory_limit_mb=64),
        ["/usr/bin/true"],
        meta_path="/tmp/meta",
    )
    assert args[args.index("-m") + 1] == "65536"
    assert args[args.index("--cg-mem") + 1] == "65536"


def test_zero_memory_is_rejected():
    with pytest.raises(ValidationError, match="memory_limit_mb"):
        IsolateLimits(time_limit_ms=1000, memory_limit_mb=0)


def test_build_run_args_includes_profile_dir_rules_before_run():
    profile = SandboxProfile(
        name="bound",
        dir_rules=IsolateDirectoryRules(
            allow_default_directories=False,
            rules=[
                IsolateDirectoryRule(
                    path_in_sandbox=Path("/opt"), path_on_host=Path("/opt")
                )
            ],
        ),
    )
    args = _build_run_args(
        IsolateLimits(time_limit_ms=1000, memory_limit_mb=64),
        ["/usr/bin/true"],
        profile=profile,
        meta_path="/tmp/meta",
    )
    run_at = args.index("--run")
    assert args.index("--no-default-dirs") < run_at
    assert args.index("--dir=opt=/opt") < run_at


def test_profile_supplies_env_and_processes():
    args = _build_run_args(
        IsolateLimits(time_limit_ms=1000, memory_limit_mb=64),
        ["/usr/bin/true"],
        profile=COMPILE_PROFILE,
        meta_path="/tmp/meta",
    )
    assert "--env=PATH=/usr/bin:/bin" in args
    assert "--processes=64" in args


def test_run_profile_is_single_process_with_no_environment():
    args = _build_run_args(
        IsolateLimits(time_limit_ms=1000, memory_limit_mb=64),
        ["/usr/bin/true"],
        profile=RUN_PROFILE,
        meta_path="/tmp/meta",
    )
    assert "--processes=1" in args
    assert not [arg for arg in args if arg.startswith("--env")]


def test_host_environment_can_never_be_requested():
    """isolate -e would hand the program the host environment; nothing emits it."""
    for profile in (RUN_PROFILE, TOOL_PROFILE, COMPILE_PROFILE):
        args = _build_run_args(
            IsolateLimits(time_limit_ms=1000, memory_limit_mb=64),
            ["/usr/bin/true"],
            profile=profile,
            meta_path="/tmp/meta",
        )
        assert "-e" not in args


def test_build_run_args_zero_extra_time():
    args = _build_run_args(
        IsolateLimits(time_limit_ms=1000, memory_limit_mb=64, extra_time_ms=0),
        ["/usr/bin/true"],
        meta_path="/tmp/meta",
    )
    assert args[args.index("-x") + 1] == "1.000"


def test_build_run_args_file_size_limit():
    args = _build_run_args(
        IsolateLimits(time_limit_ms=1000, memory_limit_mb=64, file_size_limit_kb=256),
        ["/usr/bin/true"],
        meta_path="/tmp/meta",
    )
    assert args[args.index("-f") + 1] == "256"


def test_negative_limits_rejected():
    with pytest.raises(ValidationError, match="time_limit_ms"):
        IsolateLimits(time_limit_ms=-1, memory_limit_mb=64)


def test_limits_above_max_rejected():
    with pytest.raises(ValidationError, match="memory_limit_mb"):
        IsolateLimits(time_limit_ms=1000, memory_limit_mb=10_000)


def test_env_key_with_equals_rejected():
    with pytest.raises(ValueError, match="environment variable"):
        _build_run_args(
            IsolateLimits(time_limit_ms=1000, memory_limit_mb=64),
            ["/usr/bin/true"],
            profile=SandboxProfile(name="bad", env={"FOO=BAR": "baz"}),
            meta_path="/tmp/meta",
        )


def test_from_meta_file_rejects_bad_numbers(tmp_path):
    meta_path = tmp_path / "meta.txt"
    meta_path.write_text("time:not-a-float\n")
    with pytest.raises(ValueError, match="invalid isolate meta value"):
        IsolateMeta.from_meta_file(meta_path)


def test_build_run_args_wall_time_override():
    args = _build_run_args(
        IsolateLimits(time_limit_ms=1000, memory_limit_mb=64, wall_time_ms=5000),
        ["/usr/bin/true"],
        meta_path="/tmp/meta",
    )
    assert args[args.index("-w") + 1] == "5.000"


def test_run_without_context_raises():
    box = IsolateSandBox(1)
    with pytest.raises(RuntimeError, match="not initialized"):
        box.run(["/usr/bin/true"], IsolateLimits(time_limit_ms=1000, memory_limit_mb=64))


def test_build_run_args_stderr_defaults_to_dev_null():
    args = _build_run_args(
        IsolateLimits(time_limit_ms=1000, memory_limit_mb=64),
        ["/usr/bin/true"],
        meta_path="/tmp/meta",
    )
    assert args[args.index("-r") + 1] == "/dev/null"


def test_build_run_args_stderr_file_overrides_dev_null():
    args = _build_run_args(
        IsolateLimits(time_limit_ms=1000, memory_limit_mb=64),
        ["/usr/bin/true"],
        stderr_arg="stderr.txt",
        meta_path="/tmp/meta",
    )
    assert args[args.index("-r") + 1] == "stderr.txt"


def test_build_init_args_quota():
    assert _build_init_args() == ["--cg", "--init"]
    assert _build_init_args(IsolateQuota(blocks=102400, inodes=256)) == [
        "--cg", "-q", "102400,256", "--init",
    ]


def test_quota_rejects_negative():
    with pytest.raises(ValidationError, match="non-negative"):
        IsolateQuota(blocks=-1, inodes=10)


def test_box_id_out_of_range():
    with pytest.raises(ValueError, match="box_id"):
        IsolateSandBox(-1)
    with pytest.raises(ValueError, match="box_id"):
        IsolateSandBox(1000)


def test_public_package_export():
    import olympdev.isolate as isolate_mod

    assert isolate_mod.IsolateSandBox is IsolateSandBox
    assert isolate_mod.IsolateQuota is IsolateQuota
    assert isolate_mod.sandbox_relpath is not None
    assert not hasattr(isolate_mod, "DEFAULT_DIRECTORY_RULES")
    # Mounts are a profile decision; the grammar types stay private.
    for name in (
        "IsolateDirectoryRule",
        "IsolateDirectoryRules",
        "IsolateRemoveDirectoryRule",
    ):
        assert not hasattr(isolate_mod, name), name
