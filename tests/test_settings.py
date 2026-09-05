from pathlib import Path

import pytest

from olympdev.settings import (
    CONFIG_ENV_VAR,
    Language,
    Settings,
    config_search_paths,
    find_config_file,
    get_settings,
    load_settings,
    using_settings,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_apply_without_a_config_file():
    settings = Settings()
    assert settings.source_path is None
    assert settings.limits.max_memory_limit_mb == 4096
    assert settings.isolate.cmd == "isolate"
    assert settings.toolchain(Language.CPP).flags == ["-O2"]


def test_load_settings_reads_toml_and_records_source(tmp_path: Path):
    config = _write(tmp_path / "olympdev.toml", """
[limits]
max_memory_limit_mb = 256

[isolate]
cmd = "/usr/local/bin/isolate"
sandbox_count = 8

[toolchains.cpp]
flags = ["-O2", "-std=c++20"]
""")
    settings = load_settings(config)
    assert settings.source_path == config
    assert settings.limits.max_memory_limit_mb == 256
    assert settings.isolate.sandbox_count == 8
    assert settings.toolchain(Language.CPP).flags == ["-O2", "-std=c++20"]
    # Untouched sections keep their defaults.
    assert settings.limits.max_time_limit_ms == 300_000


def test_search_chain_prefers_env_then_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    assert config_search_paths()[0] == tmp_path / "olympdev.toml"

    override = _write(tmp_path / "custom.toml", "")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(override))
    assert config_search_paths()[0] == override
    assert find_config_file() == override


def test_cwd_config_is_found_when_no_env_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    config = _write(tmp_path / "olympdev.toml", "[limits]\nmax_tests_per_problem = 7\n")
    assert find_config_file() == config
    assert load_settings().limits.max_tests_per_problem == 7


def test_missing_env_override_is_an_error_not_a_silent_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(CONFIG_ENV_VAR, str(tmp_path / "absent.toml"))
    with pytest.raises(FileNotFoundError, match="absent.toml"):
        find_config_file()


def test_invalid_toml_names_the_file(tmp_path):
    config = _write(tmp_path / "bad.toml", "this is not toml =")
    with pytest.raises(ValueError, match="invalid TOML"):
        load_settings(config)


def test_unknown_key_is_rejected(tmp_path):
    config = _write(tmp_path / "olympdev.toml", "[limits]\nmax_bananas = 3\n")
    with pytest.raises(Exception, match="max_bananas"):
        load_settings(config)


def test_using_settings_restores_previous_settings():
    before = get_settings()
    tightened = Settings.model_validate({"limits": {"max_memory_limit_mb": 1}})
    with using_settings(tightened):
        assert get_settings().limits.max_memory_limit_mb == 1
    assert get_settings() is before


def test_box_pool_must_fit_isolate_range():
    with pytest.raises(Exception, match="exceed the isolate maximum"):
        Settings.model_validate({"isolate": {"first_box_id": 990, "sandbox_count": 50}})


def test_reference_config_in_repo_is_valid():
    reference = Path(__file__).resolve().parent.parent / "olympdev.example.toml"
    assert load_settings(reference).limits.max_memory_limit_mb == 4096


def test_caps_are_enforced_at_validation_time():
    from olympdev.isolate import IsolateLimits

    tightened = Settings.model_validate({"limits": {"max_memory_limit_mb": 32}})
    with using_settings(tightened):
        with pytest.raises(Exception, match="memory_limit_mb"):
            IsolateLimits(time_limit_ms=1000, memory_limit_mb=64)
    # Outside the override the same limits validate fine.
    IsolateLimits(time_limit_ms=1000, memory_limit_mb=64)
