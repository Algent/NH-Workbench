import struct

import pytest
from typer.testing import CliRunner

from gtnh_devenv_tools import spark
from gtnh_devenv_tools.cli import app

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
    stats += blob(1, blob(1, num(1, 1_600_000_000) + num(2, 3_000_000_000))) + num(7, 2)
    java = blob(1, "Adoptium") + blob(2, "25") + blob(4, "-Xmx8G --add-opens=java.base/java.io=ALL-UNNAMED")
    system = blob(6, java)
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
    assert view.own_by_package() == {"x": 2, "a": 16, "minecraft": 2}


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
        and "< Main.run" in focus
    )
    with pytest.raises(spark.ProfileError, match="no frame matching"):
        spark.focus_report(profile, "abc", "nothing", up=False)


def test_package_guess():
    assert spark.package_of("galaxyspace.core.dimension.ChunkProviderSpaceCraters") == "galaxyspace"
    assert spark.package_of("com.gtnewhorizon.gtnhlib.util.data.Lazy") == "com.gtnewhorizon.gtnhlib"
    assert spark.package_of("micdoodle8.mods.galacticraft.core.util.WorldUtil") == "micdoodle8.mods.galacticraft"
    assert spark.package_of("mods.railcraft.common.Thing") == "mods.railcraft"
    assert spark.package_of("cpw.mods.fml.common.eventhandler.EventBus") == "forge"
    assert spark.package_of("libc.so.6") == "native code"
    assert spark.package_of("a.B", {"a.B": "SomeMod"}) == "SomeMod"


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
