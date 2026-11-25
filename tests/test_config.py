import pytest

from nh_workbench import APP_NAME, OLD_APP_NAME, config
from nh_workbench.config import move_old_dirs  # the real one: conftest stubs config.move_old_dirs for other tests


def test_save_setting_keeps_comments_and_nests(tmp_path):
    path = tmp_path / "nested" / "config.toml"
    config.save_setting(("root",), "C:/somewhere", path)  # creates the file and its folder
    path.write_text('# my notes\nroot = "C:/somewhere"\n\n[repos.Old]\nignore = true\n', encoding="utf-8")

    config.save_setting(("repos", "ForgeEssentials", "group"), "reference", path)
    config.save_setting(("repos", "ForgeEssentials", "ignore"), True, path)
    config.save_setting(("root",), "D:/elsewhere", path)
    config.save_setting(("repos", "Old", "ignore"), None, path)
    config.save_setting(("repos", "Old", "missing"), None, path)  # removing what is not there is fine

    text = path.read_text(encoding="utf-8")
    assert text.startswith("# my notes\n")
    cfg = config.load(path=path)
    assert cfg.root.as_posix() == "D:/elsewhere"
    assert cfg.repo("forgeessentials") == config.RepoConfig(group="reference", ignore=True)
    assert cfg.repo("old") == config.RepoConfig()

    # A name in another case edits the existing table instead of adding a second one.
    config.save_setting(("repos", "forgeessentials", "ignore"), None, path)
    text = path.read_text(encoding="utf-8")
    assert "[repos.forgeessentials]" not in text and "[repos.ForgeEssentials]" in text
    cfg = config.load(path=path)
    assert cfg.repo("ForgeEssentials") == config.RepoConfig(group="reference") and cfg.hidden == []


def test_save_setting_refuses_broken_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("this is = = not toml", encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.save_setting(("root",), "x", path)
    assert path.read_text(encoding="utf-8") == "this is = = not toml"


def test_refresh_minutes_default_and_bounds(tmp_path):
    path = tmp_path / "config.toml"
    assert config.load(tmp_path, path).refresh_minutes == 15
    path.write_text("refresh_minutes = -5\n", encoding="utf-8")
    assert config.load(tmp_path, path).refresh_minutes == 0
    path.write_text("refresh_minutes = 60\n", encoding="utf-8")
    assert config.load(tmp_path, path).refresh_minutes == 60


def test_keep_branches_list_or_single_name(tmp_path):
    path = tmp_path / "config.toml"
    config.save_setting(("repos", "ForgeMultipart", "keep_branches"), ["algent/java", "old"], path)
    path.write_text(path.read_text(encoding="utf-8") + '[repos.Other]\nkeep_branches = "solo"\n', encoding="utf-8")
    cfg = config.load(tmp_path, path)
    assert cfg.repo("forgemultipart").keep_branches == ("algent/java", "old")
    assert cfg.repo("other").keep_branches == ("solo",)


def test_find_ide_prefers_config_then_toolbox_script(tmp_path):
    import os

    cfg = config.Config(root=tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "pycharm.cmd").write_text("")
    (scripts / "pycharm").write_text("")
    name = "idea1.cmd" if os.name == "nt" else "idea"
    (scripts / name).write_text("")
    (scripts / ("idea" if os.name == "nt" else "idea.cmd")).write_text("")  # the other system's script
    assert config.find_ide(cfg, scripts) == str(scripts / name)
    cfg.ide_command = "my-ide"
    assert config.find_ide(cfg, scripts) == "my-ide"


def test_suggest_root_prefers_a_folder_with_repos(tmp_path):
    assert config.suggest_root(tmp_path) is None
    (tmp_path / "Projects").mkdir()
    assert config.suggest_root(tmp_path) == tmp_path / "Projects"
    (tmp_path / "git" / "SomeMod" / ".git").mkdir(parents=True)
    assert config.suggest_root(tmp_path) == tmp_path / "git"


def fake_dirs(monkeypatch, tmp_path):
    """Folders laid out like on Windows: the config in its own folder, the cache in a Cache folder inside another."""
    monkeypatch.setattr(config, "_config_dir", lambda name: tmp_path / "Roaming" / name)
    monkeypatch.setattr(config, "_cache_dir", lambda name: tmp_path / "Local" / name / "Cache")


def test_old_dirs_are_moved_once(tmp_path, monkeypatch):
    fake_dirs(monkeypatch, tmp_path)
    old_config, old_cache = tmp_path / "Roaming" / OLD_APP_NAME, tmp_path / "Local" / OLD_APP_NAME / "Cache"
    old_config.mkdir(parents=True)
    (old_config / "config.toml").write_text('root = "C:/somewhere"\n', encoding="utf-8")
    (old_cache / "spark").mkdir(parents=True)
    (old_cache / "org-prs.json").write_text("[]", encoding="utf-8")

    assert move_old_dirs() == [(old_config, config._config_dir(APP_NAME)), (old_cache, config.cache_dir())]
    assert config.load().root.as_posix() == "C:/somewhere"
    assert (config.cache_dir() / "org-prs.json").is_file() and (config.cache_dir() / "spark").is_dir()
    assert not old_config.exists() and not (tmp_path / "Local" / OLD_APP_NAME).exists()
    assert move_old_dirs() == []  # nothing left to move


def test_old_dirs_never_replace_new_ones(tmp_path, monkeypatch):
    fake_dirs(monkeypatch, tmp_path)
    for name, root in ((OLD_APP_NAME, "C:/old"), (APP_NAME, "C:/new")):
        (tmp_path / "Roaming" / name).mkdir(parents=True)
        (tmp_path / "Roaming" / name / "config.toml").write_text(f'root = "{root}"\n', encoding="utf-8")
    assert move_old_dirs() == []
    assert config.load().root.as_posix() == "C:/new"
    assert (tmp_path / "Roaming" / OLD_APP_NAME / "config.toml").is_file()


def test_old_config_is_copied_when_it_cannot_move(tmp_path, monkeypatch):
    fake_dirs(monkeypatch, tmp_path)
    old = tmp_path / "Roaming" / OLD_APP_NAME
    old.mkdir(parents=True)
    (old / "config.toml").write_text('root = "C:/somewhere"\n', encoding="utf-8")

    def locked(src, dst):
        raise PermissionError("in use")

    monkeypatch.setattr(config.shutil, "move", locked)
    new = config.config_path()
    assert move_old_dirs() == [(old / "config.toml", new)]
    assert config.load().root.as_posix() == "C:/somewhere" and (old / "config.toml").is_file()
