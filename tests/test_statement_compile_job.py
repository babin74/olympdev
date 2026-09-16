"""``_compiler_dir_allowed``: which directories a statement compiler may live
in. Fast, no sandbox/toolchain needed - pure path logic.
"""

from pathlib import Path

import olympdev.worker.jobs.statement_compile as statement_compile
from olympdev.worker.jobs.statement_compile import _compiler_dir_allowed


def test_compiler_dir_allowed_for_standard_dirs():
    assert _compiler_dir_allowed(Path("/usr/bin/latexmk"))
    assert _compiler_dir_allowed(Path("/usr/local/bin/pdflatex"))


def test_compiler_dir_allowed_for_texlive_install_tl_layout():
    """install-tl.sh (TeX Live's own upstream installer) lays binaries out
    as /usr/local/texlive/<year>/bin/<arch>/ - common, not a one-off."""
    assert _compiler_dir_allowed(Path("/usr/local/texlive/2026/bin/x86_64-linux/latexmk"))
    assert _compiler_dir_allowed(Path("/usr/local/texlive/2024/bin/aarch64-linux/pdflatex"))


def test_compiler_dir_rejects_unrelated_directories():
    assert not _compiler_dir_allowed(Path("/opt/evil/latexmk"))
    assert not _compiler_dir_allowed(
        Path("/usr/local/texlive/2026/texmf-dist/scripts/latexmk/latexmk.pl")
    )


def test_compiler_dir_allowed_does_not_follow_symlinks(tmp_path, monkeypatch):
    """A TeX Live ``latexmk`` is itself often a symlink into
    ``texmf-dist/scripts/latexmk/`` - the allowlist must check the path as
    given (the directory an operator's settings actually name), never
    resolve() through the symlink to its target's directory."""
    target_dir = tmp_path / "elsewhere"
    target_dir.mkdir()
    target = target_dir / "latexmk.pl"
    target.write_text("#!/usr/bin/env perl\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    link = bin_dir / "latexmk"
    link.symlink_to(target)

    monkeypatch.setattr(statement_compile, "_STATEMENT_COMPILER_DIRS", (bin_dir,))

    assert _compiler_dir_allowed(link)
    # The symlink's own target directory is not itself allowed - otherwise
    # this test would pass for the wrong reason.
    assert not _compiler_dir_allowed(target)
