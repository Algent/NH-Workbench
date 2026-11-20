"""Read spark profiles (spark.lucko.me) and turn them into short text reports.

The upload is protobuf; the schema is spark-common/src/main/proto/spark/*.proto in lucko/spark. Only the
fields used here are read, by number, so profiles from newer spark versions still load.
"""

import re
import struct
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import APP_NAME
from .config import cache_dir

DATA_URL = "https://spark-usercontent.lucko.me/{code}"
_CODE = re.compile(r"^[A-Za-z0-9]{6,16}$")
_URL = re.compile(r"spark\.lucko\.me/([A-Za-z0-9]{6,16})")


class ProfileError(Exception):
    pass


# --- protobuf wire format ------------------------------------------------------------------------


def _varint(buf: memoryview, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if i >= len(buf):
            raise ProfileError("truncated profile")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _fields(buf: memoryview) -> Iterator[tuple[int, int, object]]:
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 1:
            value, i = buf[i : i + 8], i + 8
        elif wire == 2:
            size, i = _varint(buf, i)
            value, i = buf[i : i + size], i + size
        elif wire == 5:
            value, i = buf[i : i + 4], i + 4
        else:
            raise ProfileError(f"not a spark profile (wire type {wire})")
        yield number, wire, value


class _Msg:
    """One decoded message: field number -> raw values, read through typed getters."""

    def __init__(self, buf: bytes | memoryview) -> None:
        self.raw: dict[int, list[tuple[int, object]]] = defaultdict(list)
        for number, wire, value in _fields(memoryview(buf)):
            self.raw[number].append((wire, value))

    def int(self, n: int) -> int:
        values = self.raw.get(n)
        if not values:
            return 0
        value = values[-1][1]
        return value - (1 << 64) if value >= 1 << 63 else value

    def double(self, n: int) -> float:
        values = self.raw.get(n)
        return struct.unpack("<d", values[-1][1])[0] if values else 0.0

    def str(self, n: int) -> str:
        values = self.raw.get(n)
        return bytes(values[-1][1]).decode("utf-8", "replace") if values else ""

    def msg(self, n: int) -> "_Msg":
        values = self.raw.get(n)
        return _Msg(values[-1][1] if values else b"")

    def msgs(self, n: int) -> list["_Msg"]:
        return [_Msg(v) for _, v in self.raw.get(n, ())]

    def doubles(self, n: int) -> list[float]:
        out: list[float] = []
        for wire, value in self.raw.get(n, ()):
            if wire == 2:  # packed
                out += struct.unpack(f"<{len(value) // 8}d", value)
            else:
                out.append(struct.unpack("<d", value)[0])
        return out

    def ints(self, n: int) -> list[int]:
        out: list[int] = []
        for wire, value in self.raw.get(n, ()):
            if wire == 2:  # packed
                i = 0
                while i < len(value):
                    v, i = _varint(value, i)
                    out.append(v)
            else:
                out.append(value)
        return out

    def str_map(self, n: int) -> dict[str, str]:
        return {e.str(1): e.str(2) for e in self.msgs(n)}

    def msg_map(self, n: int, int_keys: bool = False) -> dict:
        return {(e.int(1) if int_keys else e.str(1)): e.msg(2) for e in self.msgs(n)}


# --- profile model ---------------------------------------------------------------------------------


@dataclass
class Frame:
    cls: str
    method: str
    line: int
    times: list[float]  # per time window
    children: list[int]

    @property
    def key(self) -> str:
        return f"{self.cls}.{self.method}"


@dataclass
class Thread:
    name: str
    frames: list[Frame]
    roots: list[int]
    times: list[float]


@dataclass
class Window:
    """One minute of the recording, with what the server looked like then."""

    id: int
    start: int  # epoch ms
    duration: int  # ms
    ticks: int
    tps: float
    mspt_median: float
    mspt_max: float
    cpu_process: float
    players: int
    entities: int
    tile_entities: int
    chunks: int


@dataclass
class Gc:
    collections: int
    avg_ms: float
    every_ms: float


@dataclass
class Profile:
    platform: str = ""
    platform_version: str = ""
    minecraft: str = ""
    spark_version: int = 0
    start: int = 0  # epoch ms
    end: int = 0
    interval_us: int = 0
    allocation: bool = False  # sampled allocations (bytes) instead of execution time (ms)
    ticked: bool = False  # only ticks over a threshold were kept
    tick_threshold_ms: float = 0
    included_ticks: int = 0
    ticks: int = 0
    comment: str = ""
    tps: tuple[float, float, float] = (0, 0, 0)
    mspt: dict[str, float] = field(default_factory=dict)  # last 1m: mean, median, p95, max
    heap_used: int = 0
    heap_total: int = 0
    gc: dict[str, Gc] = field(default_factory=dict)
    players: int = 0
    entities: int = 0
    entity_counts: dict[str, int] = field(default_factory=dict)
    cpu_model: str = ""
    cpu_threads: int = 0
    cpu_process: tuple[float, float] = (0, 0)  # 1m, 15m, as a fraction of all cores
    cpu_system: tuple[float, float] = (0, 0)
    ram_used: int = 0
    ram_total: int = 0
    os: str = ""
    java: str = ""
    vm_args: str = ""
    mods: dict[str, str] = field(default_factory=dict)  # name -> version, when the platform reports them
    class_sources: dict[str, str] = field(default_factory=dict)  # class -> mod, when the platform reports them
    threads: list[Thread] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)  # in the order the frame times use

    @property
    def duration_ms(self) -> int:
        return max(0, self.end - self.start)


def parse(data: bytes) -> Profile:
    root = _Msg(data)
    if 1 not in root.raw or 2 not in root.raw:
        raise ProfileError("not a spark sampler profile (heap dumps and health reports are not supported)")
    meta = root.msg(1)
    platform = meta.msg(7)
    agg = meta.msg(5)
    stats = meta.msg(8)
    system = meta.msg(9)
    mspt = stats.msg(5).msg(1)
    tps = stats.msg(4)
    world = stats.msg(8)
    cpu = system.msg(1)
    java = system.msg(6)
    osm = system.msg(5)
    p = Profile(
        platform=platform.str(2),
        platform_version=platform.str(3),
        minecraft=platform.str(4),
        spark_version=platform.int(7),
        start=meta.int(2),
        end=meta.int(11),
        interval_us=meta.int(3),
        allocation=meta.int(15) == 1,
        ticked=agg.int(1) == 1,
        tick_threshold_ms=agg.int(3) / 1000,
        included_ticks=agg.int(4),
        ticks=meta.int(12),
        comment=meta.str(6),
        tps=(tps.double(1), tps.double(2), tps.double(3)),
        mspt={k: mspt.double(n) for k, n in (("mean", 1), ("max", 2), ("median", 4), ("p95", 5))},
        heap_used=stats.msg(1).msg(1).int(1),
        heap_total=stats.msg(1).msg(1).int(2),
        gc={name: Gc(g.int(1), g.double(2), g.double(3)) for name, g in stats.msg_map(2).items()},
        players=stats.int(7),
        entities=world.int(1),
        entity_counts={e.str(1): e.int(2) for e in world.msgs(2)},
        cpu_model=cpu.str(4),
        cpu_threads=cpu.int(1),
        cpu_process=(cpu.msg(2).double(1), cpu.msg(2).double(2)),
        cpu_system=(cpu.msg(3).double(1), cpu.msg(3).double(2)),
        ram_used=system.msg(2).msg(1).int(1),
        ram_total=system.msg(2).msg(1).int(2),
        os=" ".join(x for x in (osm.str(2), osm.str(3), osm.str(1)) if x),
        java=" ".join(x for x in (java.str(1), java.str(2)) if x),
        vm_args=java.str(4),
        mods={name: s.str(2) for name, s in meta.msg_map(13).items()},
        class_sources=root.str_map(3),
    )
    for t in root.msgs(2):
        frames = [Frame(f.str(3), f.str(4), f.int(6), f.doubles(8), f.ints(9)) for f in t.msgs(3)]
        p.threads.append(Thread(t.str(1), frames, t.ints(5), t.doubles(4)))
    stats_by_id = root.msg_map(7, int_keys=True)
    for wid in root.ints(6):
        w = stats_by_id.get(wid) or _Msg(b"")
        p.windows.append(
            Window(
                wid, w.int(11), w.int(13), w.int(1), w.double(4), w.double(5), w.double(6), w.double(2),
                w.int(7), w.int(8), w.int(9), w.int(10),
            )
        )  # fmt: skip
    return p


# --- loading -----------------------------------------------------------------------------------------


def code_of(source: str) -> str | None:
    """The upload code in a spark.lucko.me link (or a bare code), None for anything else."""
    m = _URL.search(source)
    if m:
        return m[1]
    return source if _CODE.match(source) and not Path(source).exists() else None


def load(source: str, refresh: bool = False) -> tuple[Profile, str]:
    """A profile from a spark.lucko.me link, its code, or a .sparkprofile file. Returns it and a short name."""
    code = code_of(source)
    if code is None:
        path = Path(source).expanduser()
        try:
            return parse(path.read_bytes()), path.stem
        except OSError as e:
            raise ProfileError(f"cannot read {path}: {e.strerror or e}") from e
    cached = cache_dir() / "spark" / f"{code}.sparkprofile"
    if cached.is_file() and not refresh:
        return parse(cached.read_bytes()), code
    data = _download(code)
    profile = parse(data)  # only keep what parses
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
    except OSError:
        pass
    return profile, code


def _download(code: str) -> bytes:
    req = urllib.request.Request(DATA_URL.format(code=code), headers={"User-Agent": APP_NAME})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            kind = resp.headers.get("Content-Type", "")
            data = resp.read()
    except urllib.error.HTTPError as e:
        raise ProfileError(f"{code}: {'not found (expired?)' if e.code == 404 else f'HTTP {e.code}'}") from e
    except (urllib.error.URLError, OSError) as e:
        raise ProfileError(f"{code}: download failed: {e}") from e
    if "sampler" not in kind:
        raise ProfileError(f"{code} is not a profiler upload ({kind or 'unknown type'})")
    return data


# --- analysis ------------------------------------------------------------------------------------------

# Frames that belong to the JVM, Minecraft or Forge rather than to a mod.
_BUILTIN = (
    ("java.", "java"),
    ("javax.", "java"),
    ("jdk.", "java"),
    ("sun.", "java"),
    ("com.sun.", "java"),
    ("net.minecraftforge.", "forge"),
    ("cpw.mods.fml.", "forge"),
    ("net.minecraft.", "minecraft"),
    ("io.netty.", "netty"),
    ("it.unimi.dsi.", "fastutil"),
    ("com.google.", "guava/gson"),
    ("native", "native code"),
)
_NOT_MODS = {"java", "forge", "minecraft", "netty", "fastutil", "guava/gson", "native code"}
_TLDS = {"com", "net", "org", "io", "me", "de", "dev", "gg", "fr", "it", "ru", "cn", "xyz", "info", "club", "uk"}


def package_of(cls: str, sources: dict[str, str] | None = None) -> str:
    """Who a class most likely belongs to: the mod spark reports, else a guess from its package."""
    if sources and cls in sources:
        return sources[cls]
    for prefix, name in _BUILTIN:
        if cls.startswith(prefix):
            return name
    if ".so" in cls or "/" in cls:
        return "native code"
    parts = cls.split(".")[:-1] or [cls]
    if parts[0] in _TLDS or (len(parts) > 1 and parts[1] == "mods"):
        return ".".join(parts[:3])
    return ".".join(parts[:2]) if parts[0] == "mods" else parts[0]


class View:
    """One thread of a profile, weighted by all time windows or only some."""

    def __init__(self, profile: Profile, thread: Thread, windows: Iterable[int] | None = None) -> None:
        self.profile, self.thread = profile, thread
        index = None
        if windows is not None:
            wanted = set(windows)
            index = [i for i, w in enumerate(profile.windows) if w.id in wanted]

        def weight(times: list[float]) -> float:
            if index is None:
                return sum(times)
            return sum(times[i] for i in index if i < len(times))

        self.total = [weight(f.times) for f in thread.frames]
        roots = sum(self.total[i] for i in thread.roots)
        self.whole = weight(thread.times) if thread.times else roots
        self.whole = self.whole or roots or 1.0
        self.parent = [-1] * len(thread.frames)
        for i, f in enumerate(thread.frames):
            for c in f.children:
                self.parent[c] = i

    def own(self, i: int) -> float:
        return max(0.0, self.total[i] - sum(self.total[c] for c in self.thread.frames[i].children))

    def own_by_method(self) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        for i, f in enumerate(self.thread.frames):
            out[f.key] += self.own(i)
        return out

    def own_by_package(self) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        sources = self.profile.class_sources
        for i, f in enumerate(self.thread.frames):
            out[package_of(f.cls, sources)] += self.own(i)
        return out

    def total_by_method(self) -> dict[str, float]:
        """Time spent in each method including what it called; recursion is counted once."""
        out: dict[str, float] = defaultdict(float)
        frames = self.thread.frames
        stack = [(r, frozenset()) for r in self.thread.roots]
        while stack:
            i, above = stack.pop()
            key = frames[i].key
            if key not in above:
                out[key] += self.total[i]
            inner = above | {key}
            stack += [(c, inner) for c in frames[i].children]
        return out

    def matches(self, needle: str) -> list[int]:
        """Outermost frames whose Class.method contains needle (any case)."""
        needle = needle.lower()
        found, frames = [], self.thread.frames
        stack = list(self.thread.roots)
        while stack:
            i = stack.pop()
            if needle in frames[i].key.lower():
                found.append(i)
            else:
                stack += frames[i].children
        return found


@dataclass
class Merged:
    """Call tree with identical frames under the same parent merged."""

    key: str
    total: float = 0.0
    children: dict[str, "Merged"] = field(default_factory=dict)

    def child(self, key: str) -> "Merged":
        if key not in self.children:
            self.children[key] = Merged(key)
        return self.children[key]


def callees(view: View, starts: list[int]) -> Merged:
    frames = view.thread.frames
    top = Merged("")

    def add(node: Merged, i: int) -> None:
        node.total += view.total[i]
        for c in frames[i].children:
            if view.total[c] > 0:
                add(node.child(frames[c].key), c)

    for i in starts:
        add(top.child(frames[i].key), i)
    top.total = sum(n.total for n in top.children.values())
    return top


def callers(view: View, starts: list[int]) -> Merged:
    frames = view.thread.frames
    top = Merged("")
    for i in starts:
        weight = view.total[i]
        node = top.child(frames[i].key)
        node.total += weight
        p = view.parent[i]
        while p >= 0:
            node = node.child(frames[p].key)
            node.total += weight
            p = view.parent[p]
    top.total = sum(n.total for n in top.children.values())
    return top


# --- report --------------------------------------------------------------------------------------------


def _pct(part: float, whole: float) -> str:
    return f"{100 * part / whole:5.1f}%" if whole else "    -"


def _bytes(n: float) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def _span(ms: float) -> str:
    s = int(ms / 1000)
    if s >= 3600:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{ms / 1000:.1f}s"


def amount(profile: Profile, value: float) -> str:
    return _bytes(value) if profile.allocation else _span(value)


def _short(key: str) -> str:
    """Class.method without the package, to keep trees narrow."""
    if ".so" in key or "/" in key:  # native frame: library path and symbol
        return key.rpartition("/")[2]
    cls, _, method = key.rpartition(".")
    return f"{cls.rpartition('.')[2]}.{method}" if cls else key


def header(p: Profile, name: str) -> list[str]:
    when = datetime.fromtimestamp(p.start / 1000, UTC).strftime("%Y-%m-%d %H:%M UTC") if p.start else "?"
    lines = [f"# spark profile {name}"]
    mc = f", Minecraft {p.minecraft}" if p.minecraft else ""
    lines.append(f"{p.platform} {p.platform_version}{mc}, spark v{p.spark_version}")
    what = "allocations" if p.allocation else f"every {p.interval_us / 1000:g} ms"
    lines.append(f"Recorded {when} for {_span(p.duration_ms)}, sampling {what}")
    if p.ticked:
        lines.append(
            f"Mode: only ticks over {p.tick_threshold_ms:g} ms were kept ({p.included_ticks} of {p.ticks} ticks)."
            " Percentages describe those slow ticks, not normal play."
        )
    elif p.ticks:
        lines.append(f"Mode: every tick kept ({p.ticks} ticks)")
    if p.comment:
        lines.append(f"Comment: {p.comment}")
    m = p.mspt
    if any(p.tps) or any(m.values()):
        lines.append(
            f"Server: TPS {p.tps[0]:.1f} (1m) / {p.tps[1]:.1f} (5m) / {p.tps[2]:.1f} (15m);"
            f" MSPT 1m median {m['median']:.1f}, p95 {m['p95']:.1f}, max {m['max']:.1f}; players {p.players}"
        )
    if p.heap_total:
        lines.append(f"Heap: {_bytes(p.heap_used)} used of {_bytes(p.heap_total)}")
    if p.gc:
        gcs = [f"{n} {g.collections}x avg {g.avg_ms:.0f} ms" for n, g in p.gc.items() if g.collections]
        lines.append(f"GC: {'; '.join(gcs) or 'none'}")
    if p.cpu_threads:
        lines.append(
            f"CPU: {p.cpu_model or '?'} ({p.cpu_threads} threads); process {100 * p.cpu_process[0]:.0f}% (1m),"
            f" system {100 * p.cpu_system[0]:.0f}% (1m)"
        )
    if p.ram_total:
        lines.append(f"RAM: {_bytes(p.ram_used)} used of {_bytes(p.ram_total)}")
    if p.os or p.java:
        lines.append(f"System: {p.os}; Java {p.java}")
    if p.vm_args:
        args = p.vm_args.split()
        kept = [a for a in args if not a.startswith(("--add-opens", "--add-exports"))]
        dropped = f" (+{len(args) - len(kept)} --add-opens/--add-exports)" if len(kept) < len(args) else ""
        lines.append(f"JVM args: {' '.join(kept)}{dropped}")
    if p.entities:
        top = sorted(p.entity_counts.items(), key=lambda kv: -kv[1])[:8]
        lines.append(f"Entities: {p.entities} ({', '.join(f'{k} {v}' for k, v in top)})")
    if p.mods:
        lines.append(f"Mods reported: {len(p.mods)}")
    return lines


def timeline(p: Profile, worst: int = 5) -> list[str]:
    windows = [w for w in p.windows if w.ticks]
    if len(windows) < 2:
        return []
    lines = [f"\n## Worst minutes (of {len(windows)}, by slowest tick); window ids work with --window"]
    lines.append("window  at       ticks   TPS  MSPT med   max  players entities tiles chunks")
    for w in sorted(windows, key=lambda w: -w.mspt_max)[:worst]:
        at = _span(w.start - p.start) if w.start and p.start else "?"
        counts = [str(n) if n >= 0 else "?" for n in (w.players, w.entities, w.tile_entities, w.chunks)]
        lines.append(
            f"{w.id:<7} {at:<8} {w.ticks:5}  {w.tps:4.1f}  {w.mspt_median:8.1f} {w.mspt_max:5.0f}"
            f"  {counts[0]:>7} {counts[1]:>8} {counts[2]:>5} {counts[3]:>6}"
        )
    return lines


def tree_lines(view: View, top: Merged, min_pct: float, max_lines: int, own: bool = True) -> list[str]:
    """The merged tree, branches under min_pct of the thread left out, single-child chains folded."""
    p = view.profile
    limit = view.whole * min_pct / 100
    lines: list[str] = []

    def walk(node: Merged, depth: int) -> None:
        chain = [node]
        while True:  # fold a run of frames that each pass almost all their time to one child
            kids = [c for c in chain[-1].children.values() if c.total >= limit]
            if len(kids) != 1 or kids[0].total < 0.97 * chain[-1].total:
                break
            chain.append(kids[0])
        if len(lines) >= max_lines:
            return
        names = [_short(n.key) for n in chain]
        if len(names) > 3:
            names = [names[0], f"... {len(names) - 2} frames ...", names[-1]]
        last = chain[-1]
        rest = last.total - sum(c.total for c in last.children.values())
        own_text = f"  (own {_pct(rest, view.whole).strip()})" if own and rest >= limit else ""
        lines.append(
            f"{_pct(last.total, view.whole)} {amount(p, last.total):>8}  {'  ' * depth}{' > '.join(names)}{own_text}"
        )
        for child in sorted(last.children.values(), key=lambda c: -c.total):
            if child.total >= limit:
                walk(child, depth + 1)

    for child in sorted(top.children.values(), key=lambda c: -c.total):
        if child.total >= limit:
            walk(child, 0)
    if len(lines) >= max_lines:
        lines.append(f"... cut at {max_lines} lines; raise --lines or --min")
    return lines


def thread_report(view: View, top_n: int, min_pct: float, max_lines: int) -> list[str]:
    p, t = view.profile, view.thread
    unit = "allocated" if p.allocation else "sampled"
    lines = [f"\n## Thread {t.name}: {amount(p, view.whole)} {unit}, {len(t.frames)} frames"]
    lines.append("\n### Most own time (the method itself, not what it calls)")
    for key, value in sorted(view.own_by_method().items(), key=lambda kv: -kv[1])[:top_n]:
        lines.append(f"{_pct(value, view.whole)}  {key}")
    lines.append("\n### Most total time (including what it calls), outside java, minecraft, forge and libraries")
    shown = 0
    for key, value in sorted(view.total_by_method().items(), key=lambda kv: -kv[1]):
        if package_of(key.rpartition(".")[0], p.class_sources) in _NOT_MODS:
            continue
        lines.append(f"{_pct(value, view.whole)}  {key}")
        shown += 1
        if shown >= top_n:
            break
    label = "mod" if p.class_sources else "package (1.7.10 profiles do not say which mod a class is from)"
    lines.append(f"\n### Own time by {label}")
    for name, value in sorted(view.own_by_package().items(), key=lambda kv: -kv[1])[:top_n]:
        if value < view.whole * 0.002:
            break
        lines.append(f"{_pct(value, view.whole)}  {name}")
    lines.append(f"\n### Hot call paths (at least {min_pct:g}% of the thread)")
    lines += tree_lines(view, callees(view, list(t.roots)), min_pct, max_lines)
    return lines


def busiest_threads(p: Profile) -> list[Thread]:
    def weight(t: Thread) -> float:
        return sum(t.times) or sum(sum(t.frames[i].times) for i in t.roots)

    return sorted(p.threads, key=weight, reverse=True)


def pick_thread(p: Profile, name: str | None) -> Thread:
    threads = busiest_threads(p)
    if not threads:
        raise ProfileError("the profile has no threads")
    if not name:
        server = next((t for t in threads if t.name.lower() in ("server thread", "client thread")), None)
        return server or threads[0]
    found = [t for t in threads if name.lower() in t.name.lower()]
    if not found:
        raise ProfileError(f"no thread matching {name!r}; threads: {', '.join(t.name for t in threads)}")
    return found[0]


def summary(
    p: Profile, name: str, thread: str | None = None, windows: list[int] | None = None, top_n: int = 15,
    min_pct: float = 1.0, max_lines: int = 60,
) -> str:  # fmt: skip
    lines = header(p, name) + timeline(p)
    threads = busiest_threads(p)
    if len(threads) > 1:
        weights = [(t, sum(t.times)) for t in threads]
        all_time = sum(w for _, w in weights) or 1
        listed = ", ".join(f"{t.name} {_pct(w, all_time).strip()}" for t, w in weights[:8])
        lines.append(f"\nThreads: {listed}")
    if windows:
        lines.append(f"\nOnly window(s) {', '.join(map(str, windows))} below.")
    view = View(p, pick_thread(p, thread), windows)
    lines += thread_report(view, top_n, min_pct, max_lines)
    lines.append(
        "\nNext: `tree SRC METHOD` for what a method calls, `callers SRC METHOD` for who calls it,"
        " --window ID for one minute, --thread NAME for another thread."
    )
    return "\n".join(lines)


def focus_report(
    p: Profile, name: str, method: str, up: bool, thread: str | None = None, windows: list[int] | None = None,
    min_pct: float = 0.5, max_lines: int = 80,
) -> str:  # fmt: skip
    view = View(p, pick_thread(p, thread), windows)
    starts = view.matches(method)
    if not starts:
        raise ProfileError(f"no frame matching {method!r} in thread {view.thread.name}")
    keys = sorted({view.thread.frames[i].key for i in starts})
    tree = (callers if up else callees)(view, starts)
    what = "Who calls" if up else "What is called by"
    lines = [f"# {what} {method!r} in {name}, thread {view.thread.name}"]
    lines.append(f"Matched {len(keys)} method(s): {', '.join(keys[:6])}{' ...' if len(keys) > 6 else ''}")
    lines.append(f"Their total: {_pct(tree.total, view.whole).strip()} of the thread, {amount(p, tree.total)}")
    if up:
        lines.append("Read each branch downwards as: method < its caller < that caller's caller")
    lines.append("")
    lines += tree_lines(view, tree, min_pct, max_lines, own=not up)
    return "\n".join(lines)
