from pathlib import Path

from olympdev import resources


def test_olymp_sty_is_bundled():
    content = resources.read_asset_text("olymp.sty")
    assert content.startswith("%")
    assert r"\ProvidesPackage{olymp}" in content


def test_asset_file_context_manager():
    with resources.asset_file("olymp.sty") as path:
        assert isinstance(path, Path)
        assert path.is_file()
        assert path.name == "olymp.sty"
        assert path.read_text(encoding="utf-8").startswith("%")


def test_asset_path_is_readable():
    asset = resources.asset_path("olymp.sty")
    assert asset.is_file()
    assert asset.name == "olymp.sty"
