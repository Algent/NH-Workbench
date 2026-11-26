from pathlib import Path

from nh_workbench import config, keys


def test_letters_take_valid_overrides_only():
    cfg = config.Config(root=Path("."), keys={"fetch": "g", "pull": "too long", "nope": "z"})
    letters = keys.letters(cfg)
    assert letters["fetch"] == "g" and letters["pull"] == "p" and "nope" not in letters


def test_keys_may_be_shared_only_by_actions_never_on_the_same_tab():
    letters = keys.letters(config.Config(root=Path(".")))
    assert keys.clashes(letters) == []  # r, c and t are shared by default
    assert keys.clash_text({**letters, "clone": "r"}, "clone") == "r is already Rescan"  # both on the Pack tab
    assert keys.clash_text({**letters, "fetch": "F"}, "fetch") == "F is already Fetch the selected repo"
    assert keys.clash_text({**letters, "fetch": "v"}, "fetch") is None  # Show only is on All PRs, fetch is not
    assert keys.clash_text({**letters, "fetch": "R"}, "fetch") is None  # letters keep their case


def test_spark_keys_clash_only_among_themselves():
    letters = keys.letters(config.Config(root=Path(".")))
    assert keys.clash_text({**letters, "spark_copy": "t"}, "spark_copy") == "t is already spark: what the method calls"
    assert keys.clash_text({**letters, "spark_copy": "f"}, "spark_copy") is None  # f is Fetch all, on other screens


def test_shortcuts_compare_without_case():
    shortcuts = keys.shortcuts(config.Config(root=Path("."), shortcuts={"pull": "ctrl+shift+f"}))
    assert keys.clashes(shortcuts) == [("fetch", "pull")]
    assert keys.clashes({**shortcuts, "pull": ""}) == []


def test_keys_are_read_from_the_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[keys]\nfetch = 'g'\n[shortcuts]\nquit = ''\n")
    cfg = config.load(path=path)
    assert keys.letters(cfg)["fetch"] == "g" and keys.shortcuts(cfg)["quit"] == ""


def test_detail_hints_name_the_current_keys():
    from nh_workbench.models import PackEntry, PackInfo, PrInfo, ReviewItem
    from nh_workbench.views import org_detail, pack_detail

    letters = keys.letters(config.Config(root=Path("."), keys={"open_web": "b", "clone": "k"}))
    pr = PrInfo(1, "OPEN", False, None, None, "", "https://example.invalid/pull/1", "PR 1")
    hint = org_detail(ReviewItem("o/m", pr, "bob", None, True), "", None, letters).renderables[-1].plain
    assert "Enter or b: open   c: check out" in hint
    entry = PackEntry("Missing", "github", "1", repo="o/m")
    assert pack_detail(entry, PackInfo(entries=[entry]), letters).renderables[-1].plain == "press k to clone o/m"
