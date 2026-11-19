import time

from typer.testing import CliRunner

from gtnh_devenv_tools import github
from gtnh_devenv_tools.cli import app
from gtnh_devenv_tools.models import PrInfo, ReviewItem


def org_items() -> list[ReviewItem]:
    def item(number: int, size: int, draft: bool = False, reviews: int = 0) -> ReviewItem:
        pr = PrInfo(number, "OPEN", draft, None, None, "", f"https://x/{number}", f"title {number}", size, 0, reviews)
        return ReviewItem("o/m", pr, "bob", None, True)

    return [item(1, 500), item(2, 10, reviews=1), item(3, 50), item(4, 5, draft=True)]


def test_prs_org_uses_the_saved_search_then_sorts_and_filters(sandbox, monkeypatch):
    searched = []

    async def fake_org(org):
        searched.append(org)
        return github.OrgPrs(org_items(), set(), time.time())

    monkeypatch.setattr(github, "cached_org", lambda org: github.OrgPrs(org_items(), set(), time.time()))
    monkeypatch.setattr(github, "fetch_org", fake_org)
    runner = CliRunner()
    args = ["--root", str(sandbox.root), "prs", "--org"]

    res = runner.invoke(app, [*args, "--sort", "small", "--max-size", "100"])
    assert res.exit_code == 0, res.output
    assert res.output.index("#2") < res.output.index("#3") and "#1" not in res.output and "#4" not in res.output
    assert searched == []

    res = runner.invoke(app, [*args, "--unreviewed", "--drafts", "--limit", "2"])
    assert "#1" in res.output and "#3" in res.output and "#2" not in res.output and "2 of 3 PRs" in res.output

    runner.invoke(app, [*args, "--refresh"])
    assert searched == ["GTNewHorizons"]


def test_prs_rejects_bad_options(sandbox):
    runner = CliRunner()
    assert runner.invoke(app, ["--root", str(sandbox.root), "prs", "--mine", "--org"]).exit_code == 2
    assert runner.invoke(app, ["--root", str(sandbox.root), "prs", "--sort", "size"]).exit_code == 2
