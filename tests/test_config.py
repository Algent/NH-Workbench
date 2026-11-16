import pytest

from gtnh_devenv_tools import config


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
