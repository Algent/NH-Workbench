# Development

## Tests and checks

```
uv run --extra desktop pytest   # without the extra, the desktop UI's tests are skipped
uv run ruff check
uv run ruff format --check
```

GitHub Actions runs all three on Linux, Windows and macOS for every push and pull request.

## How the code is laid out

Everything is in `src/nh_workbench`. The UIs sit on top of modules that know nothing about Textual or Qt.

| Module | What it does |
|---|---|
| `proc.py` | Runs git, gh and every other process (the only module that does) |
| `git.py`, `github.py`, `pack.py` | One repo's state, GitHub's PR data, the pack's versions and jars |
| `ops.py` | Work across many repos at once: scan, fetch, pull, cleanup, checkout, clone |
| `models.py`, `config.py` | The data classes, and the config file with its folders |
| `render.py` | The Rich text of every cell and table, shared by the command line and both UIs |
| `views.py` | What both UIs show: the tabs, columns, sorts, which actions each tab has, the detail pane |
| `workbench.py` | What both UIs keep: repo states, PR and pack data, and the batches that update them |
| `keys.py` | The rebindable keys, shared by both UIs |
| `spark.py` | Reads spark profiles and heap summaries, and writes the text reports |
| `skill.py`, `agent_skill/` | The spark-profile skill for coding agents, and its launcher |
| `cli.py` | The command line (Typer) |
| `tui.py`, `tui_spark.py`, `tui.tcss` | The terminal UI (Textual), also served in a browser by `nh-workbench web` |
| `gui*.py` | The desktop UI (Qt): main window, Settings, spark window, colours |

## The wiki

The wiki is generated from the `wiki` folder of the repository. Edit the pages there: every push to master that
changes them publishes them (`.github/workflows/sync-wiki.yml`), and pages edited on GitHub are overwritten by
the next sync. Pictures go in `wiki/images`.

The screenshots of both UIs and of the spark window are taken from your own checkouts, offscreen, with your home
folder shown as `C:\Users\Player`:

```
uv run python scripts/wiki_screenshots.py             # all of them
uv run python scripts/wiki_screenshots.py desktop     # or only some: desktop, terminal, spark
```

## The spark mod table

1.7.10 profiles do not say which mod a class comes from, so spark reports look it up in a table of package
prefixes, `src/nh_workbench/mod_packages.toml`. It is generated from a pack's jars, and classes added by mixins
count for the mod that wrote them.

After mods were added or renamed, regenerate it from the mods folders of recent packs, newest last:

```
uv run python scripts/gen_mod_packages.py "<2.9.0 client>/.minecraft/mods" "<2.9.0 server>/mods" "<daily>/.minecraft/mods"
```

It prints the packages that several mods share without a clear owner. Decide those in its `OVERRIDES`.
