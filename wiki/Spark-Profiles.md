# spark profiles

Turns a [spark](https://spark.lucko.me) profile, the link players paste when reporting lag, into a short text
report that reads well for people and LLMs alike (a few thousand tokens, no screenshots). This needs no
checkouts.

```
nh-workbench tools spark summary https://spark.lucko.me/XXXXXXXXXX
nh-workbench tools spark tree XXXXXXXXXX makeCrater                # what a method calls, all its call sites merged
nh-workbench tools spark callers XXXXXXXXXX ASMEventHandler.invoke  # who calls it
```

## What the report shows

The summary has the platform, how the profile was recorded (every tick, or only ticks over N ms, which
changes how to read the percentages), TPS, MSPT, memory, GC, CPU and JVM arguments, the worst minutes, then
for the server thread: the methods with the most own time and total time, own time per mod, and the hot call
paths, with `[Mod]` where a mod's code starts. Entities are listed by type, by world, and the busiest chunks
with their block coordinates.

Heap summaries (`/spark heapsummary`) read the same way: `summary` lists the biggest classes in memory and
the total per mod, and `compare EARLIER LATER` shows what grew between two of them, which is how a leak
shows. In the UI, `m` on a heap summary compares it with a later one.

- Method names: Minecraft's `func_` names are replaced with their MCP names (stable 12, what 1.7.10 development
  uses), from mappings downloaded once from Forge's Maven into the cache folder. Offline, the names stay as is.
- Own time per mod: time in native code, the JVM and libraries (netty, fastutil, guava) counts for the nearest
  mod up the call stack, else Minecraft or Forge. A backup's disk writes count for the backup mod, not for
  "native code".
- Mods: 1.7.10 profiles do not say which mod a class comes from, so the tool ships a table of package prefixes
  per mod. Anything it misses is shown as `<package> (package)`.

## In the UI

Tools tab, Enter on spark, then paste a link (saved profiles are listed). On the report:

| Key | Action |
|---|---|
| `Enter` | On a method: what it calls. On one of the worst minutes: narrow to it |
| `t` / `c` | What the method on the cursor line calls / who calls it (asked for when the line has none) |
| `w` / `h` | Pick a minute / a thread |
| `s` | Jump straight to the summary |
| `y` / `e` | Copy the report (for an LLM) / save it as a file |
| `o` | Open another profile |
| `Esc` | Step back to the previous view (on the summary: close) |
| `q` | Close |

## On the command line

`--thread NAME` picks another thread, `--window ID` (repeatable) narrows everything to the minutes listed under
"Worst minutes", `--min` and `--lines` control how much of the call tree is shown. A link or code also accepts a
downloaded `.sparkprofile` file. Downloads are kept in the cache folder under `spark`; `--refresh` downloads
again. The name and UUID of whoever recorded the profile are left out of the reports.

## Skill for Claude Code, Codex and OpenCode

```
nh-workbench tools install-skill    # for the agents found in your home folder; --for claude|codex|opencode|all
```

(or F2 where the spark screen asks for a link) installs a `spark-profile` skill, so a spark link pasted in any
project gets read with these commands instead of the web viewer: for Claude Code in `~/.claude/skills`, for
Codex in `~/.agents/skills`, for OpenCode in `~/.config/opencode/skills`.

It runs through a small launcher that needs only uv: it uses the checkout it was installed from, else
`nh-workbench` (or the old `gtnh-dev`) on PATH, else the published version from GitHub through `uvx` (the agent
then offers once to point it at a checkout).
