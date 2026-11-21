---
name: spark-profile
description: Read a spark report of a Minecraft 1.7.10 / GT New Horizons server or client (a spark.lucko.me link, its code, or a .sparkprofile or .sparkheap file) as text: CPU profiles to find what causes lag, heap summaries to find what fills memory. Use it whenever such a link or file shows up, instead of fetching the web page, which is a JavaScript viewer without the data.
---

# Reading spark profiles

Run everything through the launcher in this skill's folder (it finds gtnh-devenv-tools on its own):

```
uv run --no-project "<this skill's folder>/spark.py" summary https://spark.lucko.me/XXXXXXXXXX
```

The arguments are those of `gtnh-dev tools spark`; `--help` after any command lists the options.

## First use

If the launcher prints a note that no local checkout is set, it is running the published version from GitHub,
which works. Ask the user once whether they have a gtnh-devenv-tools checkout to use instead (it may have newer
changes); if they give a path, save it, and do not ask again in later conversations:

```
uv run --no-project "<this skill's folder>/spark.py" --set-checkout "<path to the checkout>"
```

## How to read a profile

1. `summary LINK` first. Read the **Mode** line before anything else: when only ticks over N ms were kept, all
   percentages describe those slow ticks, not normal play.
2. **Worst minutes** shows when it was bad (slowest tick, TPS, entities, chunks). To look at one minute only,
   add `--window ID` with the id from that table.
3. **Own time by mod** says whose code ran (native, JVM and library time counts for the mod that asked for it).
   **Most total time** and **Hot call paths** say what led there; `[Mod]` marks where a mod's code starts.
4. Drill into a method with `tree LINK METHOD` (what it calls, all call sites merged) and `callers LINK METHOD`
   (who calls it: read downwards, each `<` is the caller of the line above). METHOD is any part of
   `Class.method`, case does not matter, for example `makeCrater` or `ChunkProviderSpaceCraters`.
5. `--thread NAME` looks at another thread (the summary lists them); the server thread is the default.
6. **Entities** lists them by type, by world, and the busiest chunks with block coordinates: a mob farm or
   dropped items piling up shows there.

## Heap summaries (/spark heapsummary)

`summary LINK` on a heap summary lists the biggest classes in memory and the total per mod. One summary rarely
proves a leak (byte[], int[], Object[] and String are everyone's); ask for a second one taken later and run
`compare EARLIER LATER`: the classes and mods that grew are the suspects.

## Reporting back

- Name the mod and the method, with the percentage and what it means (share of slow ticks, or of all ticks),
  and the call path that explains why it runs (a player teleporting, chunk generation, a backup, an event
  handler run for every entity).
- Keep the user's reply short: findings and evidence, not the whole report.
- Be careful with: `ASMEventHandler.invoke` own time (event handler bodies inlined by the JIT: look at its
  callees), `Thread.sleep` (the server waiting, not work), and mods tagged with `?` (a guess from the package).
- The reports leave out who recorded the profile; keep it that way.
