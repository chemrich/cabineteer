"""data_dir() — location and one-time legacy-store migration."""

from pathlib import Path

from cabineteer.paths import data_dir


def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


class TestDataDir:
    def test_fresh_home_returns_new_path(self, tmp_path, monkeypatch):
        home = _home(tmp_path, monkeypatch)
        assert data_dir() == home / ".cabineteer"
        # No side effects: nothing is created by resolving the path.
        assert not (home / ".cabineteer").exists()

    def test_legacy_store_migrates_once(self, tmp_path, monkeypatch):
        home = _home(tmp_path, monkeypatch)
        old = home / ".cabinet-mcp"
        (old / "projects").mkdir(parents=True)
        (old / "projects" / "sideboards.json").write_text("{}", encoding="utf-8")
        d = data_dir()
        assert d == home / ".cabineteer"
        assert (d / "projects" / "sideboards.json").read_text(encoding="utf-8") == "{}"
        assert not old.exists()

    def test_both_exist_prefers_new_untouched(self, tmp_path, monkeypatch):
        home = _home(tmp_path, monkeypatch)
        (home / ".cabinet-mcp").mkdir()
        (home / ".cabineteer" / "projects").mkdir(parents=True)
        assert data_dir() == home / ".cabineteer"
        # The legacy dir is left alone when a new store already exists.
        assert (home / ".cabinet-mcp").exists()

    def test_project_dir_routes_through_data_dir(self, tmp_path, monkeypatch):
        home = _home(tmp_path, monkeypatch)
        from cabineteer.project import project_dir
        assert project_dir() == home / ".cabineteer" / "projects"
