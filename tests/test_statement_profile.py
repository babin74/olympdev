"""The statement sandbox profile and the TeX font cache it binds.

Cyrillic statements need LH/T2A metrics that TeX generates on demand and
caches under ``$TEXMFVAR``. The sandbox can neither generate nor write them,
so the profile binds the host's cache read-only - but only when the sandbox
user could actually read it.
"""

from pathlib import Path

from olympdev.isolate.profiles import COMPILE_PROFILE, statement_profile
from olympdev.settings import (
    Settings,
    StatementEngine,
    StatementToolchainSettings,
    using_settings,
)


def _settings_with(texmf_var: Path | str | None) -> Settings:
    return Settings(
        statement_toolchains={
            StatementEngine.LATEX: StatementToolchainSettings(texmf_var=texmf_var)
        }
    )


def _readable_cache(tmp_path: Path) -> Path:
    cache = tmp_path / "texmf-var"
    (cache / "fonts" / "tfm").mkdir(parents=True)
    for path in (cache, cache / "fonts", cache / "fonts" / "tfm"):
        path.chmod(0o755)
    return cache


def test_a_readable_cache_is_bound_read_only(tmp_path: Path):
    with using_settings(_settings_with(_readable_cache(tmp_path))):
        profile = statement_profile()
    rendered = profile.dir_rules.to_cli()
    assert len(rendered) == 1
    assert rendered[0].startswith("--dir=texmf-var=")
    # No `rw`: a statement reads the cache and never adds to it.
    assert ":rw" not in rendered[0] and not rendered[0].endswith("rw")
    assert profile.env["TEXMFVAR"] == "/texmf-var"


def test_an_unreadable_cache_is_not_bound(tmp_path: Path):
    """Binding it would succeed and still leave TeX unable to read it, so
    the failure would blame a font instead of the directory's mode."""
    cache = _readable_cache(tmp_path)
    cache.chmod(0o750)
    try:
        with using_settings(_settings_with(cache)):
            profile = statement_profile()
    finally:
        cache.chmod(0o755)
    assert profile.dir_rules.to_cli() == []
    assert "TEXMFVAR" not in profile.env


def test_an_empty_setting_disables_the_mount():
    with using_settings(_settings_with("")):
        profile = statement_profile()
    assert profile.dir_rules.to_cli() == []
    assert "TEXMFVAR" not in profile.env


def test_a_missing_directory_is_not_bound(tmp_path: Path):
    with using_settings(_settings_with(tmp_path / "nowhere")):
        profile = statement_profile()
    assert profile.dir_rules.to_cli() == []


def test_the_statement_profile_keeps_the_compile_profile_shape(tmp_path: Path):
    """latexmk forks pdflatex and needs PATH, exactly like a C++ compile."""
    with using_settings(_settings_with("")):
        profile = statement_profile()
    assert profile.processes == COMPILE_PROFILE.processes
    assert profile.env["PATH"] == COMPILE_PROFILE.env["PATH"]
