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


def heap_bytes(entries: list[tuple[str, int, int]], taken: int = 0, up: int = 0) -> bytes:
    platform = num(1, 0) + blob(2, "Forge") + blob(3, "10.13.4.1614") + blob(4, "1.7.10") + num(7, 2)
    stats = blob(1, blob(1, num(1, 2_000_000_000) + num(2, 4_000_000_000))) + num(7, 3) + (num(3, up) if up else b"")
    meta = blob(1, num(1, 1) + blob(2, "SomePlayer")) + blob(2, platform) + blob(3, stats)
    meta += num(5, taken) if taken else b""
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


def test_unnamed_methods_take_later_names_and_keep_mixin_suffixes(monkeypatch):
    mappings = {
        spark.MCP_URL: {"func_78471_a": "renderWorld"},
        spark.MCP_LATER_URLS[0]: {"func_147456_g": "updateBlocks", "func_78471_a": "renamedLater"},
        spark.MCP_LATER_URLS[2]: {"func_147456_g": "tickBlocks", "func_72375_a": "preparePlayer"},
    }
    monkeypatch.setattr(spark, "_mcp_methods", lambda url: dict(mappings.get(url, {})))
    names = spark._mcp_merged()
    assert names["func_78471_a"] == "renderWorld"  # 1.7.10's own name wins
    assert names["func_147456_g"] == "updateBlocks/func_147456_g"  # nearest first, its id kept for the 1.7.10 code
    assert names["func_72375_a"] == "preparePlayer/func_72375_a"
    monkeypatch.setattr(spark, "mcp_names", lambda minecraft: names)
    p = spark.parse(profile_bytes())
    p.threads[0].frames[0].method = "func_78471_a$surround$1"  # Angelica wraps it
    p.threads[0].frames[1].method = "func_147456_g"
    spark.readable(p)
    assert [f.method for f in p.threads[0].frames[:2]] == ["renderWorld$surround$1", "updateBlocks/func_147456_g"]
    line = " 3.5%  1.1s  WorldServer.updateBlocks/func_147456_g  (own 3.5%)"
    assert spark.method_on_line(line) == "WorldServer.updateBlocks/func_147456_g"  # Enter, t and c still work


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


def test_saved_info(tmp_path):
    saved = tmp_path / "abc.sparkprofile"
    saved.write_bytes(profile_bytes())
    heap = tmp_path / "def.sparkheap"
    heap.write_bytes(heap_bytes(HEAP_A))
    info = spark.saved_info(saved)
    assert (info.kind, info.recorded, info.length_ms) == ("profile", START, 120_000) and info.downloaded > 0
    assert spark.saved_info(heap).kind == "heap summary"
    broken = tmp_path / "bad.sparkprofile"
    broken.write_bytes(b"\xff\xff")
    assert spark.saved_info(broken).recorded == 0  # listed anyway, without a date


def test_memory_and_machine_details(profile):
    assert (profile.heap_max, profile.old_gen_live, profile.uptime_ms) == (3_500_000_000, 3_000_000_000, 1_800_000)
    assert profile.gc_since_start["MarkSweepCompact"].collections == 7 and profile.engine == "java"
    text = spark.summary(profile, "abc")
    assert "heap 1.5 GB used, 2.8 GB allocated, 3.3 GB max; old gen after the last GC 2.8 GB" in text
    assert "GC since start MarkSweepCompact 7x avg 2.9 s" in text and "up 30m00s" in text
    assert "Java Adoptium 25 (OpenJDK 32-Bit Server VM)" in text


def test_findings(profile):
    text = spark.summary(profile, "abc")
    findings = text[text.index("## Findings") : text.index("## Worst minutes")]
    for expected in (
        "[warn] GC MarkSweepCompact: 7 collections since start, 2.9 s each on average",
        "[warn] Max heap 3.3 GB: GTNH usually needs 6 GB or more",
        "[warn] After the last GC 2.8 GB of 3.3 GB was still in use",
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
    assert parts == {"network": 2, "world generation": 6, spark.IDLE: 2, "backups": 4}
    found = [f"[{f.level}] {f.text}" for f in spark.findings(p, view)]
    assert "[warn] World generation takes 30% of the server thread's time (GalaxySpace 30.0%)" in found[0]
    assert "[warn] Backups take 20% of the server thread's time" in found
    assert not any(f.startswith("[warn] GalaxySpace") for f in found)  # already named for world generation
    assert "[info] The server thread waits 10.0% of the time between ticks (headroom)" in found


def test_client_frame_limit_is_headroom(monkeypatch):
    monkeypatch.setattr(spark, "mod_packages", lambda: {"com.gtnewhorizons.angelica": "Angelica"})
    frames = [
        spark.Frame("net.minecraft.client.Minecraft", "runGameLoop", 0, [20], [1, 3]),
        spark.Frame("com.gtnewhorizons.angelica.rendering.FramePacer", "beforePresent", 0, [12], [2]),
        spark.Frame("org.lwjgl.system.JNI", "invokeJV", 0, [12], []),  # the frame limiter's sleep, in LWJGL
        spark.Frame("net.minecraft.client.renderer.EntityRenderer", "updateCameraAndRender", 0, [8], [4]),
        spark.Frame("com.gtnewhorizons.angelica.Render", "draw", 0, [2], []),
    ]
    p = spark.Profile(client=True, threads=[spark.Thread("Client thread", frames, [0], [20])])
    view = spark.View(p, p.threads[0])
    assert view.own_by_package()["Angelica"] == 14  # LWJGL is a library: its time counts for its caller
    parts = {label: total for label, (total, _) in spark.tick_parts(view).items()}
    assert parts == {spark.IDLE: 12, "rendering": 8}
    found = [f"[{f.level}] {f.text}" for f in spark.findings(p, view)]
    assert not any(f.startswith("[warn]") for f in found)  # Angelica works 10%: its sleep is not counted
    assert any("waits 60.0% of the time for the next frame" in f for f in found)


def test_findings_on_ticks_machine_and_mods():
    p = spark.Profile(
        start=START, end=START + 21_000, ticks=26, mspt={"mean": 21, "max": 18_660, "median": 4, "p95": 6},
        os="Windows 11 10.0 amd64",
        swap_used=14_000_000_000, disk_used=95_000_000_000, disk_total=100_000_000_000, cpu_threads=8,
        mods={"gregtech": "5.09.55.36", "dreamcraft": "2.10.16", "Waystones": "1.0"},
        gc_since_start={"G1 Old Generation": spark.Gc(1, 287.0, 0.0)},
        threads=[spark.Thread("Server thread", [spark.Frame("x.A", "run", 0, [1], [])], [0], [1])],
    )  # fmt: skip
    found = [f.text for f in spark.findings(p, spark.View(p, p.threads[0]))]
    assert "A tick took 18.7 s in the last minute: the server froze" in found  # in no minute of the profile
    assert "Only 26 ticks in 21.0s: about 1.2 TPS during the profile" in found
    lone = [f for f in spark.findings(p, spark.View(p, p.threads[0])) if f.text.startswith("GC G1 Old")]
    assert [f.level for f in lone] == ["info"] and "while the world loaded" in lone[0].text  # one, at load
    assert "Only 4.7 GB free on the disk: saves and backups may fail" in found
    assert not any("swap" in f for f in found)  # Windows' page file figures read as tens of GB in use
    text = "\n".join(spark.header(p, "abc"))
    assert "CPU (8 threads), CPU use not reported" in text
    assert "Mods reported: 3; NH Coremod 2.10.16, GregTech 5.09.55.36" in text


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
    assert "SomePlayer" not in text and "Live objects: 1,510 in 3 classes, 67 MB" in text
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


def test_heap_compare_starts_from_the_earlier_one():
    earlier = (spark.parse_heap(heap_bytes(HEAP_A, taken=START)), "Before")
    later = (spark.parse_heap(heap_bytes(HEAP_B, taken=START + 5_400_000)), "After")
    assert earlier[0].generated == START
    assert spark.heap_in_order(later, earlier) == (earlier, later)  # whichever was loaded first
    text = spark.heap_compare(*earlier, *later)
    assert "Taken 1h30m apart" in text and "+9 MB" in text and "restarted" not in text
    growth = {cls: (size, count) for cls, size, count, _ in spark.heap_deltas(earlier[0], later[0])}
    assert growth["a.Machine"] == (8_999_000, 89_990) and growth["java.lang.String"] == (-20_000_000, -500)


def test_lambdas_match_across_runs_and_restarts_are_told():
    lam = "gregtech.Base$$Lambda/0x"
    run1 = [(f"{lam}0000000090afa758", 10, 160), (f"{lam}0000000090afa2f8", 5, 80)]
    run2 = [(f"{lam}00000000aa000001", 15, 240), (f"[L{lam}00000000aa000002;", 1, 16)]
    a = spark.parse_heap(heap_bytes(run1, taken=START, up=600_000))
    b = spark.parse_heap(heap_bytes(run2, taken=START + 120_000, up=60_000))  # up 1 minute, 2 minutes later
    assert [(e.cls, e.instances, e.size) for e in a.entries] == [("gregtech.Base$$Lambda", 15, 240)]
    assert {e.cls for e in b.entries} == {"gregtech.Base$$Lambda", "gregtech.Base$$Lambda[]"}
    growth = {cls: size for cls, size, _, _ in spark.heap_deltas(a, b)}
    assert growth["gregtech.Base$$Lambda"] == 0  # the same lambda, at another address
    assert spark.heap_restarted(a, b)
    assert "The game restarted in between (up 1m00s" in spark.heap_compare(a, "A", b, "B")
    assert spark.hidden_class("java.lang.invoke.LambdaForm$MH/0x000000008e425400") == "java.lang.invoke.LambdaForm$MH"


def test_libraries_are_not_mods():
    libraries = {"org.objectweb.asm.ClassWriter": "asm", "scala.Option": "scala", "akka.actor.Actor": "akka"}
    for cls, owner in libraries.items():
        assert spark.package_of(cls) == owner and owner in spark.NOT_MODS


def test_thermos_recurring_freezes_and_autosaves():
    frames = [
        spark.Frame("net.minecraft.server.MinecraftServer", "run", 0, [100_000], [1, 2]),
        spark.Frame("net.minecraft.world.WorldServer", "saveAllChunks", 0, [4_000], []),  # 4% of the thread
        spark.Frame("thermos.wrapper.ChunkBlockHashMap", "get", 0, [3_000], []),
    ]
    slow = (60_000, 1200, 19.0, 40, 1500, 0.1, 3, 200, -1, 900)  # each minute had a 1.5 s tick
    minutes = [spark.Window(i, START + i * 60_000, *slow) for i in range(4)]
    p = spark.Profile(
        start=START, end=START + 450_000, ticks=9_000, java="Temurin 1.8.0_512", windows=minutes,
        swap_used=800 * 2**20, ram_used=90 * spark.GIB, ram_total=120 * spark.GIB, os="Debian GNU/Linux 12",
        threads=[spark.Thread("Server thread", frames, [0], [100_000])],
    )  # fmt: skip
    found = [f.text for f in spark.findings(p, spark.View(p, p.threads[0]))]
    assert any(f.startswith("Ticks over 1 s in 4 of 4 minutes") and "--only-ticks-over 500" in f for f in found)
    assert any(f.startswith("Each autosave (every 45 s) takes about 400 ms") for f in found)  # 10 saves in 9000 ticks
    assert "Thermos (Bukkit on Forge): its code and Bukkit's take 3.0% of the server thread" in found
    assert not any("Java 8" in f for f in found)  # Thermos runs only on Java 8
    assert not any("swap" in f for f in found)  # RAM is not full: Linux swaps idle pages anyway
    assert "with Thermos (Bukkit on Forge)" in spark.header(p, "abc")[0]


def test_an_opis_run_during_the_profile_is_told():
    def profile(timer_ms: float) -> spark.Profile:
        frames = [
            spark.Frame("net.minecraft.server.MinecraftServer", "run", 0, [1_000], [1]),
            spark.Frame("mcp.mobius.mobiuscore.profiler.ProfilerSection", "stop", 0, [timer_ms + 5], [2]),
            spark.Frame("mcp.mobius.opis.data.profilers.ProfilerTileEntityUpdate", "stop", 0, [timer_ms], []),
        ]
        return spark.Profile(threads=[spark.Thread("Server thread", frames, [0], [1_000])])

    on = profile(40)  # Opis timing every tile entity: 4% of the thread
    found = [f.text for f in spark.findings(on, spark.View(on, on.threads[0]))]
    told = "An Opis profiling run was on while spark recorded: Opis's timers take 4.5%"
    assert any(f.startswith(told) for f in found)
    starting = profile(1)  # a sample as a run starts: no run going on
    assert not any("Opis" in f.text for f in spark.findings(starting, spark.View(starting, starting.threads[0])))
