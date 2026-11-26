import struct

import pytest
from typer.testing import CliRunner

from nh_workbench import spark
from nh_workbench.cli import app

# --- a tiny protobuf writer for synthetic profiles ---


def varint(n: int) -> bytes:
    n &= (1 << 64) - 1
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def num(field: int, value: int) -> bytes:
    return varint(field << 3) + varint(value)


def dbl(field: int, value: float) -> bytes:
    return varint(field << 3 | 1) + struct.pack("<d", value)


def blob(field: int, data: bytes | str) -> bytes:
    data = data.encode() if isinstance(data, str) else data
    return varint(field << 3 | 2) + varint(len(data)) + data


def doubles(field: int, values: list[float]) -> bytes:
    return blob(field, struct.pack(f"<{len(values)}d", *values))


def ints(field: int, values: list[int]) -> bytes:
    return blob(field, b"".join(varint(v) for v in values))


def frame(cls: str, method: str, times: list[float], children: list[int]) -> bytes:
    return blob(3, cls) + blob(4, method) + doubles(8, times) + ints(9, children)


def window(wid: int, start: int, mspt_max: float) -> bytes:
    stats = num(1, 1200) + dbl(4, 19.5) + dbl(5, 30.0) + dbl(6, mspt_max) + num(7, 2) + num(9, -1)
    stats += num(10, 500) + num(11, start) + num(13, 60_000)
    return blob(7, num(1, wid) + blob(2, stats))


def counts(field: int, pairs: dict[str, int]) -> bytes:
    return b"".join(blob(field, blob(1, k) + num(2, v)) for k, v in pairs.items())


def world_stats() -> bytes:
    player = "unknown[class net.minecraft.entity.player.EntityPlayerMP]"
    farm = num(1, -8) + num(2, 6) + num(3, 15) + counts(4, {"Cow": 12, "Item": 3})
    quiet = num(1, 2) + num(2, -3) + num(3, 2) + counts(4, {player: 2})
    overworld = blob(1, "Overworld") + num(2, 17) + blob(3, num(1, 17) + blob(2, farm) + blob(2, quiet))
    nether = blob(1, "Nether") + num(2, 0)
    return num(1, 17) + counts(2, {"Cow": 12, "Item": 3, player: 2}) + blob(3, overworld) + blob(3, nether)


def heap_bytes(entries: list[tuple[str, int, int]]) -> bytes:
    platform = num(1, 0) + blob(2, "Forge") + blob(3, "10.13.4.1614") + blob(4, "1.7.10") + num(7, 2)
    stats = blob(1, blob(1, num(1, 2_000_000_000) + num(2, 4_000_000_000))) + num(7, 3)
    meta = blob(1, num(1, 1) + blob(2, "SomePlayer")) + blob(2, platform) + blob(3, stats)
    rows = b"".join(
        blob(2, num(1, order) + num(2, count) + num(3, size) + blob(4, cls))
        for order, (cls, count, size) in enumerate(entries)
    )
    return blob(1, meta) + rows


START = 1_795_000_000_000


def profile_bytes() -> bytes:
    # Main.run -> Mod.tick -> Mod.helper -> Mod.tick (recursion), and Main.run -> World.tick -> Mod.tick
    frames = [
        frame("x.Main", "run", [10, 10], [1, 2]),
        frame("a.Mod", "tick", [6, 2], [3]),
        frame("net.minecraft.World", "tick", [3, 7], [4]),
        frame("a.Mod", "helper", [5, 1], [5]),
        frame("a.Mod", "tick", [2, 6], []),
        frame("a.Mod", "tick", [1, 0], []),
    ]
    thread = blob(1, "Server thread") + b"".join(blob(3, f) for f in frames) + doubles(4, [10, 10]) + ints(5, [0])
    platform = num(1, 0) + blob(2, "Forge") + blob(3, "10.13.4.1614") + blob(4, "1.7.10") + num(7, 2)
    stats = blob(4, dbl(1, 14.7) + dbl(2, 18.7) + dbl(3, 19.5))
    stats += blob(5, blob(1, dbl(1, 20) + dbl(2, 900) + dbl(4, 16.5) + dbl(5, 35.2)))
    old_gen = blob(1, "G1 Old Gen") + blob(3, num(1, 3_000_000_000))
    heap = num(1, 1_600_000_000) + num(2, 3_000_000_000) + num(4, 3_500_000_000)
    stats += blob(1, blob(1, heap) + blob(3, old_gen)) + num(7, 2) + num(3, 1_800_000)
    stats += blob(8, world_stats())
    java = blob(1, "Adoptium") + blob(2, "25") + blob(4, "-Xmx8G --add-opens=java.base/java.io=ALL-UNNAMED")
    full_gc = blob(3, blob(1, "MarkSweepCompact") + blob(2, num(1, 7) + dbl(2, 2858.0)))
    system = blob(1, num(1, 2)) + full_gc + blob(6, java) + blob(9, blob(1, "OpenJDK 32-Bit Server VM"))
    creator = num(1, 1) + blob(2, "SomePlayer") + blob(3, "d96247b3-0000")
    aggregator = num(1, 1) + num(3, 200_000) + num(4, 34)
    meta = blob(1, creator) + num(2, START) + num(3, 4000) + blob(5, aggregator) + blob(7, platform)
    meta += blob(8, stats) + blob(9, system) + num(11, START + 120_000) + num(12, 2400)
    windows = window(100, START, 900.0) + window(101, START + 60_000, 120.0)
    return blob(1, meta) + blob(2, thread) + ints(6, [100, 101]) + windows


@pytest.fixture
def profile() -> spark.Profile:
    return spark.parse(profile_bytes())


def test_parse_reads_metadata_and_frames(profile):
    p = profile
    assert (p.platform, p.minecraft, p.spark_version) == ("Forge", "1.7.10", 2)
    assert p.ticked and p.tick_threshold_ms == 200 and p.included_ticks == 34 and p.ticks == 2400
    assert p.tps == (14.7, 18.7, 19.5) and p.mspt["p95"] == 35.2 and p.mspt["max"] == 900
    assert p.heap_total == 3_000_000_000 and p.duration_ms == 120_000 and p.interval_us == 4000
    assert [w.id for w in p.windows] == [100, 101] and p.windows[0].tile_entities == -1
    t = p.threads[0]
    assert t.name == "Server thread" and t.roots == [0] and len(t.frames) == 6
    assert t.frames[3].key == "a.Mod.helper" and t.frames[0].children == [1, 2]


def test_own_and_total_time(profile):
    view = spark.View(profile, profile.threads[0])
    assert view.whole == 20
    own = view.own_by_method()
    assert own["a.Mod.tick"] == 11 and own["a.Mod.helper"] == 5 and own["x.Main.run"] == 2
    total = view.total_by_method()
    assert total["a.Mod.tick"] == 16  # the recursive call inside is not counted twice
    assert total["a.Mod.helper"] == 6 and total["x.Main.run"] == 20
    assert view.own_by_package() == {"x (package)": 2, "a (package)": 16, "minecraft": 2}


def test_one_window_only(profile):
    view = spark.View(profile, profile.threads[0], windows=[101])
    assert view.whole == 10 and view.own_by_method()["a.Mod.tick"] == 7


def test_callers_and_callees_merge_call_sites(profile):
    view = spark.View(profile, profile.threads[0])
    starts = view.matches("mod.TICK")
    assert sorted(starts) == [1, 4]  # outermost only: the recursive one sits inside 1
    down = spark.callees(view, starts)
    tick = down.children["a.Mod.tick"]
    assert tick.total == 16 and tick.children["a.Mod.helper"].children["a.Mod.tick"].total == 1
    up = spark.callers(view, view.matches("helper"))
    helper = up.children["a.Mod.helper"]
    assert helper.total == 6 and helper.children["a.Mod.tick"].children["x.Main.run"].total == 6


def test_summary_report(profile):
    text = spark.summary(profile, "abc")
    assert "Mode: only ticks over 200 ms were kept (34 of 2400 ticks)" in text
    assert "SomePlayer" not in text and "d96247b3" not in text  # who recorded it stays out
    assert "JVM args: -Xmx8G (+1 --add-opens/--add-exports)" in text
    assert "## Worst minutes" in text and text.index("100  ") < text.index("101  ")
    assert " 55.0%  a.Mod.tick" in text  # own time, 11 of 20 ms
    assert " 80.0%  a.Mod.tick" in text  # total time, 16 of 20
    focus = spark.focus_report(profile, "abc", "helper", up=True)
    assert (
        "Matched 1 method(s): a.Mod.helper" in focus
        and "Mod.helper\n" in focus
        and "< Mod.tick\n" in focus
        and "< [x?] Main.run" in focus
    )
    with pytest.raises(spark.ProfileError, match="no frame matching"):
        spark.focus_report(profile, "abc", "nothing", up=False)


def test_mod_of_a_class(monkeypatch):
    table = {"galaxyspace": "GalaxySpace", "micdoodle8.mods.galacticraft.core": "Galacticraft", "gregtech": "GT"}
    monkeypatch.setattr(spark, "mod_packages", lambda: table)
    assert spark.package_of("galaxyspace.core.dimension.ChunkProviderSpaceCraters") == "GalaxySpace"
    assert spark.package_of("micdoodle8.mods.galacticraft.core.util.WorldUtil") == "Galacticraft"
    assert spark.package_of("micdoodle8.mods.galacticraft.planets.X") == "micdoodle8.mods.galacticraft (package)"
    assert spark.package_of("com.gtnewhorizon.gtnhlib.util.data.Lazy") == "com.gtnewhorizon.gtnhlib (package)"
    assert spark.package_of("mods.railcraft.common.Thing") == "mods.railcraft (package)"
    assert spark.package_of("cpw.mods.fml.common.eventhandler.EventBus") == "forge"
    assert spark.package_of("net.minecraft.world.World") == "minecraft"  # never taken from the table
    assert spark.package_of("libc.so.6") == "native code"
    assert spark.package_of("a.CommonProxy$$Lambda/0x10adfab0") == "a (package)"  # a lambda, not native code
    assert spark.package_of("a.B", {"a.B": "SomeMod"}) == "SomeMod"


def test_shipped_mod_table():
    table = spark.mod_packages()
    assert table["gregtech"] == "GregTech" and table["galaxyspace"] == "GalaxySpace"
    assert not any(k.startswith(("net.minecraft", "cpw.mods.fml", "java.")) for k in table)


def test_sources(tmp_path, monkeypatch):
    assert spark.code_of("https://spark.lucko.me/pWtoY8WVPg?hl=10") == "pWtoY8WVPg"
    assert spark.code_of("dXUoiL8JBW") == "dXUoiL8JBW"
    assert spark.code_of(str(tmp_path / "x.sparkprofile")) is None

    downloads = []

    def fake_download(code):
        downloads.append(code)
        return profile_bytes()

    monkeypatch.setattr(spark, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(spark, "_download", fake_download)
    p, name = spark.load("https://spark.lucko.me/AbCdEf1234")
    assert name == "AbCdEf1234" and p.platform == "Forge"
    spark.load("AbCdEf1234")  # saved copy
    assert downloads == ["AbCdEf1234"] and (tmp_path / "cache" / "spark" / "AbCdEf1234.sparkprofile").is_file()

    with pytest.raises(spark.ProfileError, match="not a spark sampler"):
        spark.parse(blob(1, b""))


def test_cli_needs_no_checkouts(tmp_path):
    file = tmp_path / "lag.sparkprofile"
    file.write_bytes(profile_bytes())
    runner = CliRunner()
    base = ["--root", str(tmp_path / "missing"), "tools", "spark"]
    res = runner.invoke(app, [*base, "summary", str(file)])
    assert res.exit_code == 0, res.output
    assert res.output.startswith("# spark profile lag")
    res = runner.invoke(app, [*base, "callers", str(file), "helper", "--window", "100"])
    assert res.exit_code == 0 and "Only" not in res.output and "Mod.helper" in res.output
    res = runner.invoke(app, [*base, "tree", str(file), "nope"])
    assert res.exit_code == 1


def test_native_and_library_time_counts_for_the_mod_that_asked():
    # mod tick -> minecraft region file -> java -> native: the mod asked for it
    frames = [
        spark.Frame("a.Mod", "tick", 0, [10], [1]),
        spark.Frame("net.minecraft.RegionFile", "write", 0, [8], [2]),
        spark.Frame("java.io.FileDescriptor", "sync", 0, [6], [3]),
        spark.Frame("libc.so.6", "fsync", 0, [5], []),
        spark.Frame("java.lang.Thread", "sleep", 0, [4], []),  # nobody but the JVM above it
    ]
    p = spark.Profile(threads=[spark.Thread("Server thread", frames, [0, 4], [14])])
    view = spark.View(p, p.threads[0])
    assert view.owners() == ["a (package)", "minecraft", "a (package)", "a (package)", "java"]
    assert view.own_by_package() == {"a (package)": 2 + 1 + 5, "minecraft": 2, "java": 4}


def test_readable_method_names(monkeypatch):
    monkeypatch.setattr(spark, "mcp_names", lambda minecraft: {"func_73154_d": "provideChunk"})
    p = spark.readable(spark.parse(profile_bytes()))
    assert p.names == "" and p.threads[0].frames[0].method == "run"  # no func_ names: nothing to do
    p.threads[0].frames[0].method = "func_73154_d"
    p.threads[0].frames[1].method = "func_99999_x"  # not in the mappings: kept
    spark.readable(p)
    assert p.names == "mcp" and [f.method for f in p.threads[0].frames[:2]] == ["provideChunk", "func_99999_x"]
    assert "method names from MCP" in spark.summary(p, "abc")
    monkeypatch.setattr(spark, "mcp_names", lambda minecraft: {})
    p.threads[0].frames[0].method = "func_73154_d"
    assert spark.readable(p).names == "raw" and "func_ names left as is" in spark.summary(p, "abc")


def test_call_paths_name_the_mods(monkeypatch):
    monkeypatch.setattr(spark, "mod_packages", lambda: {"a": "Mod A"})
    p = spark.parse(profile_bytes())
    text = spark.summary(p, "abc", min_pct=1)
    assert "[Mod A] Mod.tick" in text and "[x?] Main.run" in text
    assert "[Mod A] Mod.helper" not in text  # same mod as its caller: no new tag


def test_entities_by_world_and_chunk(profile):
    assert profile.world_entities == {"Overworld": 17, "Nether": 0}
    assert profile.entity_counts["Player"] == 2  # spark's unknown[class ...EntityPlayerMP]
    farm = max(profile.chunks, key=lambda c: c.total)
    assert (farm.world, farm.x, farm.z, farm.total) == ("Overworld", -8, 6, 15)
    text = spark.summary(profile, "abc")
    assert "## Entities: 17" in text and "By world: Overworld 17" in text and "Nether" not in text
    assert "   15  Overworld  x -120 z 104      Cow 12, Item 3" in text


def test_memory_and_machine_details(profile):
    assert (profile.heap_max, profile.old_gen_live, profile.uptime_ms) == (3_500_000_000, 3_000_000_000, 1_800_000)
    assert profile.gc_since_start["MarkSweepCompact"].collections == 7 and profile.engine == "java"
    text = spark.summary(profile, "abc")
    assert "heap 1.6 GB used, 3.0 GB allocated, 3.5 GB max; old gen after the last GC 3.0 GB" in text
    assert "GC since start MarkSweepCompact 7x avg 2.9 s" in text and "up 30m00s" in text
    assert "Java Adoptium 25 (OpenJDK 32-Bit Server VM)" in text


def test_findings(profile):
    text = spark.summary(profile, "abc")
    findings = text[text.index("## Findings") : text.index("## Worst minutes")]
    for expected in (
        "[warn] GC MarkSweepCompact: 7 collections since start, 2.9 s each on average",
        "[warn] Max heap 3.5 GB: GTNH usually needs 6 GB or more",
        "[warn] After the last GC 3.0 GB of 3.5 GB was still in use",
        "[warn] 32-bit Java",
        "[warn] Only 2 CPU thread(s)",
    ):
        assert expected in findings
    assert "froze" not in findings  # the slowest tick took 900 ms


def test_where_the_tick_goes(monkeypatch):
    monkeypatch.setattr(spark, "mod_packages", lambda: {"galaxyspace": "GalaxySpace", "serverutils": "ServerUtilities"})
    frames = [
        spark.Frame("net.minecraft.server.MinecraftServer", "run", 0, [20], [1, 3, 5]),
        spark.Frame("net.minecraft.network.NetworkSystem", "networkTick", 0, [8], [2]),
        spark.Frame("galaxyspace.Gen", "provideChunk", 0, [6], []),  # set off by a packet: world generation
        spark.Frame("java.lang.Thread", "sleep", 0, [2], []),  # between ticks
        spark.Frame("java.lang.Thread", "sleep", 0, [3], []),  # waiting for the backup's chunk writes
        spark.Frame("serverutils.task.backup.BackupTask", "execute", 0, [4], [4]),
    ]
    p = spark.Profile(threads=[spark.Thread("Server thread", frames, [0], [20])])
    view = spark.View(p, p.threads[0])
    parts = {label: total for label, (total, _) in spark.tick_parts(view).items()}
    assert parts == {"network": 2, "world generation": 6, "waiting between ticks": 2, "backups": 4}
    found = [f"[{f.level}] {f.text}" for f in spark.findings(p, view)]
    assert "[warn] World generation takes 30% of the server thread's time (GalaxySpace 30.0%)" in found[0]
    assert "[warn] Backups take 20% of the server thread's time" in found
    assert not any(f.startswith("[warn] GalaxySpace") for f in found)  # already named for world generation
    assert "[info] The server thread waits 10.0% of the time between ticks (headroom)" in found


HEAP_A = [("[B", 1000, 50_000_000), ("a.Machine", 10, 1_000), ("java.lang.String", 500, 20_000_000)]
HEAP_B = [("[B", 1200, 60_000_000), ("a.Machine", 90_000, 9_000_000), ("[[La.Machine;", 5, 400)]


def test_heap_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(spark, "mod_packages", lambda: {"a": "Mod A"})
    assert spark.kind_of(heap_bytes(HEAP_A)) == "heap" and spark.kind_of(profile_bytes()) == "sampler"
    assert spark._class_name("[B") == "byte[]" and spark._class_name("[[La/Machine;") == "a.Machine[][]"
    file = tmp_path / "before.sparkheap"
    file.write_bytes(heap_bytes(HEAP_A))
    heap, name = spark.load(str(file))
    assert isinstance(heap, spark.Heap) and name == "before" and heap.players == 3
    assert [e.cls for e in heap.entries] == ["byte[]", "a.Machine", "java.lang.String"]
    with pytest.raises(spark.ProfileError, match="heap summary"):
        spark.load_profile(str(file))

    text = spark.heap_report(heap, name)
    assert "SomePlayer" not in text and "Live objects: 1,510 in 3 classes, 70 MB" in text
    assert text.index("byte[]") < text.index("java.lang.String") < text.index("a.Machine")
    assert "a.Machine  [Mod A]" in text

    later = spark.parse_heap(heap_bytes(HEAP_B))
    growth = spark.heap_compare(heap, "before", later, "after")
    rows = growth.split("## Grew the most")[1].split("## By mod")[0].splitlines()[2:]
    assert "byte[]" in rows[0] and "+10 MB" in rows[0]
    assert "+89,990" in rows[1] and "[Mod A]" in rows[1] and "+400 B" in rows[2]
    by_mod = growth.split("## By mod")[1]
    assert "-   10 MB  java" in by_mod and "+    9 MB  Mod A" in by_mod


def test_cli_heap_summary_and_compare(tmp_path):
    before, after = tmp_path / "before.sparkheap", tmp_path / "after.sparkheap"
    before.write_bytes(heap_bytes(HEAP_A))
    after.write_bytes(heap_bytes(HEAP_B))
    profile = tmp_path / "lag.sparkprofile"
    profile.write_bytes(profile_bytes())
    runner = CliRunner()
    base = ["--root", str(tmp_path / "missing"), "tools", "spark"]
    res = runner.invoke(app, [*base, "summary", str(before)])
    assert res.exit_code == 0 and res.output.startswith("# spark heap summary before")
    res = runner.invoke(app, [*base, "compare", str(before), str(after)])
    assert res.exit_code == 0 and "## Grew the most" in res.output
    res = runner.invoke(app, [*base, "compare", str(before), str(profile)])
    assert res.exit_code == 1 and "not a heap summary" in res.output
    res = runner.invoke(app, [*base, "tree", str(before), "Machine"])
    assert res.exit_code == 1 and "no call tree" in res.output
