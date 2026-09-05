import pytest
from pathlib import Path

from pydantic import ValidationError

from olympdev.isolate._dir_rules import (
    IsolateDirectoryRule,
    IsolateDirectoryRules,
    IsolateRemoveDirectoryRule,
)


def test_bind_in_out_with_options():
    rule = IsolateDirectoryRule(
        path_in_sandbox=Path("/work"),
        path_on_host=Path("/home/user/work"),
        write_access=True,
        device_access=True,
        noexec=True,
        ignore_missing=True,
        recursive_bind=False,
    )
    assert rule.to_cli() == "--dir=work=/home/user/work:rw:dev:noexec:maybe:norec"


def test_shorthand_binds_slash_dir_to_dir():
    assert IsolateDirectoryRule(path_in_sandbox=Path("/bin")).to_cli() == "--dir=bin"
    assert IsolateDirectoryRule(path_in_sandbox=Path("usr")).to_cli() == "--dir=usr"


def test_relative_host_path_gets_dot_slash_prefix():
    rule = IsolateDirectoryRule(
        path_in_sandbox=Path("/box"),
        path_on_host=Path("box"),
        write_access=True,
    )
    assert rule.to_cli() == "--dir=box=./box:rw"


def test_tmp_accepts_no_out_and_implies_rw():
    rule = IsolateDirectoryRule(path_in_sandbox=Path("/tmp"), tmp_dir=True)
    assert rule.to_cli() == "--dir=tmp:tmp"
    with pytest.raises(ValidationError, match="right-hand side"):
        IsolateDirectoryRule(
            path_in_sandbox=Path("/tmp"),
            path_on_host=Path("/var/tmp"),
            tmp_dir=True,
        )


def test_fs_uses_out_as_filesystem_name():
    rule = IsolateDirectoryRule(path_in_sandbox=Path("/proc"), fs="proc")
    assert rule.to_cli() == "--dir=proc=proc:fs"
    shm = IsolateDirectoryRule(
        path_in_sandbox=Path("/dev/shm"),
        fs="tmpfs",
        write_access=True,
    )
    assert shm.to_cli() == "--dir=dev/shm=tmpfs:rw:fs"


def test_fs_and_tmp_are_mutually_exclusive():
    with pytest.raises(ValidationError, match="mutually exclusive"):
        IsolateDirectoryRule(path_in_sandbox=Path("/x"), fs="tmpfs", tmp_dir=True)


def test_unsupported_filesystem():
    with pytest.raises(ValidationError, match="Invalid filesystem"):
        IsolateDirectoryRule(path_in_sandbox=Path("/x"), fs="nfs")


def test_remove_rule():
    assert IsolateRemoveDirectoryRule(path_in_sandbox=Path("/lib64")).to_cli() == "--dir=lib64="
    with pytest.raises(ValidationError):
        IsolateRemoveDirectoryRule(path_in_sandbox=Path("/a/../b"))


def test_user_rules_do_not_emit_defaults():
    rules = IsolateDirectoryRules(
        rules=[
            IsolateDirectoryRule(
                path_in_sandbox=Path("/opt"),
                path_on_host=Path("/opt"),
            )
        ]
    )
    assert rules.to_cli() == ["--dir=opt=/opt"]


def test_no_default_dirs_flag():
    rules = IsolateDirectoryRules(
        allow_default_directories=False,
        rules=[
            IsolateDirectoryRule(
                path_in_sandbox=Path("/box"),
                path_on_host=Path("box"),
                write_access=True,
            )
        ],
    )
    assert rules.to_cli() == ["--no-default-dirs", "--dir=box=./box:rw"]


def test_colon_and_equals_in_paths_are_rejected():
    with pytest.raises(ValidationError, match="must not contain"):
        IsolateDirectoryRule(path_in_sandbox=Path("/foo:bar"))
    with pytest.raises(ValidationError, match="must not contain"):
        IsolateDirectoryRule(
            path_in_sandbox=Path("/work"),
            path_on_host=Path("/home/user/a=b"),
        )
    with pytest.raises(ValidationError, match="must not contain"):
        IsolateRemoveDirectoryRule(path_in_sandbox=Path("lib=64"))


def test_replacement_keeps_original_position():
    rules = IsolateDirectoryRules(
        allow_default_directories=False,
        rules=[
            IsolateDirectoryRule(
                path_in_sandbox=Path("/a"),
                path_on_host=Path("/host/a"),
            ),
            IsolateDirectoryRule(
                path_in_sandbox=Path("/a/b"),
                path_on_host=Path("/host/ab"),
            ),
            IsolateDirectoryRule(
                path_in_sandbox=Path("/a"),
                path_on_host=Path("/host/a2"),
                write_access=True,
            ),
        ],
    )
    assert [rule.to_cli() for rule in rules.rules] == [
        "--dir=a=/host/a2:rw",
        "--dir=a/b=/host/ab",
    ]
