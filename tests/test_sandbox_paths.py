from pathlib import Path

import pytest

from olympdev.isolate import sandbox_relpath


def test_sandbox_relpath_accepts_nested():
    assert sandbox_relpath("src/main.cpp") == "src/main.cpp"
    assert sandbox_relpath(Path("a/b")) == "a/b"


def test_sandbox_relpath_rejects_escape_and_reserved():
    with pytest.raises(ValueError, match="relative"):
        sandbox_relpath("/etc/passwd")
    with pytest.raises(ValueError, match="invalid"):
        sandbox_relpath("../box")
    with pytest.raises(ValueError, match="reserved"):
        sandbox_relpath("tool", reserved=frozenset({"tool"}))
    with pytest.raises(ValueError, match="'-'"):
        sandbox_relpath("-o")
