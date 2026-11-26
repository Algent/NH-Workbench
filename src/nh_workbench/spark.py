"""Read spark profiles (spark.lucko.me) and turn them into short text reports.

The upload is protobuf; the schema is spark-common/src/main/proto/spark/*.proto in lucko/spark. Only the
fields used here are read, by number, so profiles from newer spark versions still load.
"""

import csv
import re
import struct
import tomllib
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from importlib import resources
from pathlib import Path

from . import APP_NAME
from .config import cache_dir

DATA_URL = "https://spark-usercontent.lucko.me/{code}"
# Readable names for the func_ methods of Minecraft 1.7.10 (what GTNH development uses too).
MCP_URL = "https://maven.minecraftforge.net/de/oceanlabs/mcp/mcp_stable/12-1.7.10/mcp_stable-12-1.7.10.zip"
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
class ChunkEntities:
    world: str
    x: int  # chunk coordinates
    z: int
    total: int
    counts: dict[str, int]


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
    heap_total: int = 0  # committed
    heap_max: int = 0
    old_gen_live: int = 0  # the old generation right after the last GC: roughly what memory is really needed
    non_heap: int = 0
    gc: dict[str, Gc] = field(default_factory=dict)  # during the profile
    gc_since_start: dict[str, Gc] = field(default_factory=dict)
    uptime_ms: int = 0
    client: bool = False
    engine: str = ""  # "async" (async-profiler) or "java" (spark's own sampler)
    players: int = 0
    entities: int = 0
    entity_counts: dict[str, int] = field(default_factory=dict)
    world_entities: dict[str, int] = field(default_factory=dict)
    chunks: list[ChunkEntities] = field(default_factory=list)
    cpu_model: str = ""
    cpu_threads: int = 0
    cpu_process: tuple[float, float] = (0, 0)  # 1m, 15m, as a fraction of all cores
    cpu_system: tuple[float, float] = (0, 0)
    ram_used: int = 0
    ram_total: int = 0
    swap_used: int = 0
    swap_total: int = 0
    disk_used: int = 0
    disk_total: int = 0
    os: str = ""
    java: str = ""
    jvm: str = ""  # e.g. "OpenJDK 64-Bit Server VM"
    vm_args: str = ""
    mods: dict[str, str] = field(default_factory=dict)  # name -> version, when the platform reports them
    class_sources: dict[str, str] = field(default_factory=dict)  # class -> mod, when the platform reports them
    threads: list[Thread] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)  # in the order the frame times use
    names: str = ""  # "mcp" when func_ names were made readable, "raw" when that was not possible

    @property
    def duration_ms(self) -> int:
        return max(0, self.end - self.start)


@dataclass
class HeapEntry:
    cls: str  # class name; arrays end in []
    instances: int
    size: int  # bytes


@dataclass
class Heap:
    """A heap summary (/spark heapsummary): what is in memory, per class."""

    platform: str = ""
    platform_version: str = ""
    minecraft: str = ""
    spark_version: int = 0
    heap_used: int = 0
    heap_total: int = 0
    heap_max: int = 0
    gc: dict[str, Gc] = field(default_factory=dict)
    players: int = 0
    entries: list[HeapEntry] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(e.size for e in self.entries)


_UNNAMED = re.compile(r"^unknown\[class (?:[\w$]+\.)*([\w$]+)\]$")


def _entity_counts(entries: list["_Msg"]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for e in entries:
        name = e.str(1)
        m = _UNNAMED.match(name)
        if m:
            name = "Player" if m[1] in ("EntityPlayerMP", "EntityPlayer") else m[1]
        out[name] += e.int(2)
    return dict(out)


def _gcs(m: "_Msg", n: int) -> dict[str, Gc]:
    return {name: Gc(g.int(1), g.double(2), g.double(3)) for name, g in m.msg_map(n).items()}


def _old_gen_live(memory: "_Msg") -> int:
    for pool in memory.msgs(3):
        if any(word in pool.str(1) for word in ("Old Gen", "Tenured")):
            return pool.msg(3).int(1)
    return 0


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
        heap_max=stats.msg(1).msg(1).int(4),
        old_gen_live=_old_gen_live(stats.msg(1)),
        non_heap=stats.msg(1).msg(2).int(1),
        gc=_gcs(stats, 2),
        gc_since_start=_gcs(system, 3),
        uptime_ms=stats.int(3) or system.int(7),
        client=platform.int(1) == 1,
        engine="async" if meta.int(16) == 1 else "java",
        players=stats.int(7),
        entities=world.int(1),
        entity_counts=_entity_counts(world.msgs(2)),
        world_entities={w.str(1): w.int(2) for w in world.msgs(3)},
        chunks=[
            ChunkEntities(w.str(1), c.int(1), c.int(2), c.int(3), _entity_counts(c.msgs(4)))
            for w in world.msgs(3)
            for r in w.msgs(3)
            for c in r.msgs(2)
        ],
        cpu_model=cpu.str(4),
        cpu_threads=cpu.int(1),
        cpu_process=(cpu.msg(2).double(1), cpu.msg(2).double(2)),
        cpu_system=(cpu.msg(3).double(1), cpu.msg(3).double(2)),
        ram_used=system.msg(2).msg(1).int(1),
        ram_total=system.msg(2).msg(1).int(2),
        swap_used=system.msg(2).msg(2).int(1),
        swap_total=system.msg(2).msg(2).int(2),
        disk_used=system.msg(4).int(1),
        disk_total=system.msg(4).int(2),
        jvm=system.msg(9).str(1),
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


def kind_of(data: bytes) -> str:
    """ "heap" or "sampler", from the data itself (a downloaded file does not say)."""
    entries = _Msg(data).raw.get(2)
    if not entries or entries[0][0] != 2:
        raise ProfileError("not a spark profile or heap summary")
    first = _Msg(entries[0][1])
    # A heap entry starts with numbers (size is field 3); a profile's thread has its frames as field 3.
    if 3 in first.raw:
        return "heap" if first.raw[3][0][0] == 0 else "sampler"
    return "heap" if 2 in first.raw else "sampler"


def parse_heap(data: bytes) -> Heap:
    root = _Msg(data)
    meta = root.msg(1)
    platform, stats = meta.msg(2), meta.msg(3)
    return Heap(
        platform=platform.str(2),
        platform_version=platform.str(3),
        minecraft=platform.str(4),
        spark_version=platform.int(7),
        heap_used=stats.msg(1).msg(1).int(1),
        heap_total=stats.msg(1).msg(1).int(2),
        heap_max=stats.msg(1).msg(1).int(4),
        gc=_gcs(stats, 2),
        players=stats.int(7),
        entries=[HeapEntry(_class_name(e.str(4)), e.int(2), e.int(3)) for e in root.msgs(2)],
    )


_PRIMITIVES = {
    "B": "byte",
    "C": "char",
    "D": "double",
    "F": "float",
    "I": "int",
    "J": "long",
    "S": "short",
    "Z": "boolean",
}


def _class_name(name: str) -> str:
    """Class names as spark sends them, with JVM descriptors ([B, [Lfoo.Bar;) made readable."""
    dims = len(name) - len(name.lstrip("["))
    if not dims:
        return name
    base = name[dims:]
    base = base[1:-1] if base.startswith("L") and base.endswith(";") else _PRIMITIVES.get(base, base)
    return base.replace("/", ".") + "[]" * dims


def parse_upload(data: bytes) -> "Profile | Heap":
    return parse_heap(data) if kind_of(data) == "heap" else readable(parse(data))


_EXTENSIONS = {"sampler": ".sparkprofile", "heap": ".sparkheap"}


@dataclass(frozen=True)
class Saved:
    """A downloaded profile or heap summary in the cache, for the lists of saved ones."""

    path: Path
    kind: str  # "profile" or "heap summary"
    recorded: int  # epoch ms; 0 when the file does not say
    length_ms: int  # how long the profile ran; 0 for a heap summary
    downloaded: float  # epoch s


@cache
def _saved(path: Path, mtime: float) -> Saved:
    kind = "heap summary" if path.suffix == _EXTENSIONS["heap"] else "profile"
    recorded = length = 0
    try:
        meta = _Msg(path.read_bytes()).msg(1)  # only the top level is decoded: cheap even for big profiles
        if kind == "profile":
            recorded, length = meta.int(2), max(0, meta.int(11) - meta.int(2))
        else:
            recorded = meta.int(5)
    except (OSError, ProfileError, IndexError, ValueError):
        pass
    return Saved(path, kind, recorded, length, mtime)


def saved_info(path: Path) -> Saved:
    """What a saved file is and when it was recorded and downloaded (remembered until the file changes)."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return _saved(path, mtime)


# Reading the text reports back: which method or minute a line is about, for the UIs' drill-down.
_LEAD = re.compile(r"^\s*[\d.]+%\s+(?:\d+h\d\dm|\d+m\d\ds|[\d.]+s|\d+ [MG]B)?\s*")
_OWN = re.compile(r"\s+\(own [\d.]+%\)$")
_WINDOW_ROW = re.compile(r"^(\d+)\s+(?:\d+h\d\dm|\d+m\d\ds|[\d.]+s|\?)\s+\d+\s")
_METHOD = re.compile(r"^[\w$.<>/-]+$")
_TAG = re.compile(r"^\[[^\]]*\]\s*")  # [Mod] in front of a frame
RECENT_SAVED = 20  # saved profiles listed


def method_on_line(line: str) -> str | None:
    """The method (or package) a report line is about: the last frame of a call path, or the listed name."""
    if not _LEAD.match(line):
        return None
    rest = _OWN.sub("", _LEAD.sub("", line, count=1)).strip()
    last = _TAG.sub("", rest.rpartition(" > ")[2].strip().removeprefix("< "))
    return last if _METHOD.match(last) else None


def window_on_line(line: str) -> int | None:
    m = _WINDOW_ROW.match(line)
    return int(m[1]) if m else None


def when_text(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s).strftime("%Y-%m-%d %H:%M")


def saved_profiles() -> list[Path]:
    try:
        folder = cache_dir() / "spark"
        files = [*folder.glob("*.sparkprofile"), *folder.glob("*.sparkheap")]
    except OSError:
        return []
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:RECENT_SAVED]


def load(source: str, refresh: bool = False) -> "tuple[Profile | Heap, str]":
    """A profile or heap summary from a spark.lucko.me link, its code, or a downloaded file, and a short name."""
    code = code_of(source)
    if code is None:
        path = Path(source).expanduser()
        try:
            return parse_upload(path.read_bytes()), path.stem
        except OSError as e:
            raise ProfileError(f"cannot read {path}: {e.strerror or e}") from e
    folder = cache_dir() / "spark"
    if not refresh:
        for ext in _EXTENSIONS.values():
            if (folder / f"{code}{ext}").is_file():
                return parse_upload((folder / f"{code}{ext}").read_bytes()), code
    data = _download(code)
    upload = parse_upload(data)  # only keep what parses
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{code}{_EXTENSIONS[kind_of(data)]}").write_bytes(data)
    except OSError:
        pass
    return upload, code


def load_profile(source: str, refresh: bool = False) -> tuple[Profile, str]:
    upload, name = load(source, refresh)
    if not isinstance(upload, Profile):
        raise ProfileError(f"{name} is a heap summary: it has no call tree (summary and compare read it)")
    return upload, name


def load_heap(source: str, refresh: bool = False) -> tuple[Heap, str]:
    upload, name = load(source, refresh)
    if not isinstance(upload, Heap):
        raise ProfileError(f"{name} is a CPU profile, not a heap summary (/spark heapsummary)")
    return upload, name


@cache
def mcp_names(minecraft: str) -> dict[str, str]:
    """func_ name -> readable name from the MCP mappings, downloaded once. Empty when not available."""
    if minecraft != "1.7.10":
        return {}
    path = cache_dir() / "mcp" / MCP_URL.rpartition("/")[2]
    if not path.is_file():
        req = urllib.request.Request(MCP_URL, headers={"User-Agent": APP_NAME})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except (urllib.error.URLError, OSError):
            return {}
    try:
        with zipfile.ZipFile(path) as z:
            rows = csv.reader(z.read("methods.csv").decode("utf-8", "replace").splitlines())
            next(rows, None)  # searge,name,side,desc
            return {row[0]: row[1] for row in rows if len(row) > 1 and row[1]}
    except (OSError, KeyError, zipfile.BadZipFile):
        return {}


def readable(p: Profile) -> Profile:
    """Rename func_ methods to their MCP names, when the mappings can be had."""
    frames = [f for t in p.threads for f in t.frames if f.method.startswith("func_")]
    if not frames:
        return p
    names = mcp_names(p.minecraft)
    for f in frames:
        f.method = names.get(f.method, f.method)
    p.names = "mcp" if names else "raw"
    return p


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
    if "sampler" not in kind and "heap" not in kind:
        raise ProfileError(f"{code} is neither a profile nor a heap summary ({kind or 'unknown type'})")
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
NOT_MODS = {"java", "forge", "minecraft", "netty", "fastutil", "guava/gson", "native code"}
# Code that only does what its caller asked: its time counts for that caller in the per-mod totals.
_CHARGED = {"java", "netty", "fastutil", "guava/gson", "native code"}
_TLDS = {"com", "net", "org", "io", "me", "de", "dev", "gg", "fr", "it", "ru", "cn", "xyz", "info", "club", "uk"}


@cache
def mod_packages() -> dict[str, str]:
    """Package prefix -> mod, generated from the pack's jars by scripts/gen_mod_packages.py."""
    text = resources.files(__package__).joinpath("mod_packages.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)


def is_native(cls: str) -> bool:
    """A frame of the JVM or the OS (async-profiler reports those), not a Java class."""
    return cls.startswith(("native", "/")) or ".so" in cls


def package_of(cls: str, sources: dict[str, str] | None = None) -> str:
    """Who a class belongs to: the mod spark reports, the pack's table, else "<package> (package)"."""
    if sources and cls in sources:
        return sources[cls]
    for prefix, name in _BUILTIN:
        if cls.startswith(prefix):
            return name
    if is_native(cls):
        return "native code"
    parts = cls.split(".")[:-1] or [cls]
    table = mod_packages()
    for end in range(len(parts), 0, -1):
        mod = table.get(".".join(parts[:end]))
        if mod:
            return mod
    if parts[0] in _TLDS or (len(parts) > 1 and parts[1] == "mods"):
        guess = ".".join(parts[:3])
    else:
        guess = ".".join(parts[:2]) if parts[0] == "mods" else parts[0]
    return f"{guess} (package)"


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
        self._owners: list[str] | None = None

    def own(self, i: int) -> float:
        return max(0.0, self.total[i] - sum(self.total[c] for c in self.thread.frames[i].children))

    def own_by_method(self) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        for i, f in enumerate(self.thread.frames):
            out[f.key] += self.own(i)
        return out

    def owners(self) -> list[str]:
        """Per frame, who its time counts for. Native, JVM and library code does what it was asked: it counts for
        the nearest mod up the stack, else the nearest game code (a backup's disk writes go through Minecraft's
        region files, but the backup mod asked for them)."""
        if self._owners is None:
            frames, sources = self.thread.frames, self.profile.class_sources
            out = [""] * len(frames)
            # (frame, nearest mod above, nearest game code above)
            stack: list[tuple[int, str | None, str | None]] = [(r, None, None) for r in self.thread.roots]
            while stack:
                i, mod_above, game_above = stack.pop()
                label = package_of(frames[i].cls, sources)
                if label in _CHARGED:
                    out[i] = mod_above or game_above or label
                else:
                    out[i] = label
                    if label in NOT_MODS:
                        game_above = label
                    else:
                        mod_above = label
                stack += [(c, mod_above, game_above) for c in frames[i].children]
            self._owners = out
        return self._owners

    def own_by_package(self) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        for i, owner in enumerate(self.owners()):
            out[owner] += self.own(i)
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


def bytes_text(n: float) -> str:
    if abs(n) >= 1e9:
        return f"{n / 1e9:.1f} GB"
    if abs(n) >= 1e6:
        return f"{n / 1e6:.0f} MB"
    return f"{n / 1e3:.0f} KB" if abs(n) >= 1e3 else f"{n:.0f} B"


def span_text(ms: float) -> str:
    s = int(ms / 1000)
    if s >= 3600:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{ms / 1000:.1f}s"


def _ms(ms: float) -> str:
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms:.0f} ms"


def _gc_text(gcs: dict[str, Gc]) -> str:
    return ", ".join(f"{n} {g.collections}x avg {_ms(g.avg_ms)}" for n, g in gcs.items() if g.collections)


def amount(profile: Profile, value: float) -> str:
    return bytes_text(value) if profile.allocation else span_text(value)


def short_name(key: str) -> str:
    """Class.method without the package, to keep trees narrow."""
    cls, _, method = key.rpartition(".")
    if is_native(cls):  # library path and symbol
        return key.rpartition("/")[2]
    return f"{cls.rpartition('.')[2]}.{method}" if cls else key


def header(p: Profile, name: str) -> list[str]:
    when = datetime.fromtimestamp(p.start / 1000, UTC).strftime("%Y-%m-%d %H:%M UTC") if p.start else "?"
    mc = f", Minecraft {p.minecraft}" if p.minecraft else ""
    lines = [f"# spark profile {name}: {p.platform} {p.platform_version}{mc}, spark v{p.spark_version}"]
    what = "allocations" if p.allocation else f"every {p.interval_us / 1000:g} ms"
    what += {"async": " with async-profiler", "java": " with spark's Java sampler"}.get(p.engine, "")
    names = {"mcp": "; method names from MCP", "raw": "; func_ names left as is (MCP mappings unavailable)"}
    lines.append(f"Recorded {when} for {span_text(p.duration_ms)}, sampling {what}{names.get(p.names, '')}")
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
        server = (
            f"Server: TPS {p.tps[0]:.1f} / {p.tps[1]:.1f} / {p.tps[2]:.1f} (1, 5, 15 min);"
            f" tick ms median {m['median']:.1f}, p95 {m['p95']:.1f}, max {m['max']:.0f} (last min); players {p.players}"
        )
        if p.entities:
            server += f"; entities {p.entities}"
        if p.uptime_ms:
            server += f"; up {span_text(p.uptime_ms)}"
        lines.append(server)
    memory = []
    if p.heap_total:
        heap = f"heap {bytes_text(p.heap_used)} used, {bytes_text(p.heap_total)} allocated"
        memory.append(heap + (f", {bytes_text(p.heap_max)} max" if p.heap_max else ""))
    if p.old_gen_live:
        memory.append(f"old gen after the last GC {bytes_text(p.old_gen_live)}")
    if p.non_heap:
        memory.append(f"non-heap {bytes_text(p.non_heap)}")
    if _gc_text(p.gc):
        memory.append(f"GC during the profile {_gc_text(p.gc)}")
    if _gc_text(p.gc_since_start):
        memory.append(f"GC since start {_gc_text(p.gc_since_start)}")
    if memory:
        lines.append(f"Memory: {'; '.join(memory)}")
    machine = []
    if p.cpu_threads:
        machine.append(
            f"{p.cpu_model or 'CPU'} ({p.cpu_threads} threads), CPU use {100 * p.cpu_process[0]:.0f}% by the game,"
            f" {100 * p.cpu_system[0]:.0f}% overall"
        )
    if p.ram_total:
        machine.append(f"RAM {bytes_text(p.ram_used)} of {bytes_text(p.ram_total)}")
    if p.swap_used:
        machine.append(f"swap {bytes_text(p.swap_used)} of {bytes_text(p.swap_total)} in use")
    if p.disk_total:
        machine.append(f"disk {bytes_text(p.disk_used)} of {bytes_text(p.disk_total)}")
    java = f"Java {p.java}" + (f" ({p.jvm})" if p.jvm else "") if p.java else ""
    machine += [x for x in (p.os, java) if x]
    if machine:
        lines.append(f"Machine: {'; '.join(machine)}")
    if p.vm_args:
        args = p.vm_args.split()
        kept = [a for a in args if not a.startswith(("--add-opens", "--add-exports"))]
        dropped = f" (+{len(args) - len(kept)} --add-opens/--add-exports)" if len(kept) < len(args) else ""
        lines.append(f"JVM args: {' '.join(kept)}{dropped}")
    if p.mods:
        lines.append(f"Mods reported: {len(p.mods)}")
    return lines


def timeline(p: Profile, worst: int = 5) -> list[str]:
    windows = [w for w in p.windows if w.ticks]
    if len(windows) < 2:
        return []
    shown = sorted(windows, key=lambda w: -w.mspt_max)[:worst]
    lines = [f"\n## Worst minutes ({len(shown)} of {len(windows)}, by slowest tick; the id works with --window)"]
    columns = [("players", "players"), ("entities", "entities"), ("tile_entities", "tiles"), ("chunks", "chunks")]
    columns = [(attr, title) for attr, title in columns if any(getattr(w, attr) >= 0 for w in shown)]
    width = max(len(str(w.id)) for w in shown)
    head = f"{'id':<{width}}  {'at':>6}  {'ticks':>5}  {'TPS':>4}  {'median ms':>9}  {'max ms':>6}  {'CPU':>4}"
    lines.append(head + "".join(f"  {title:>8}" for _, title in columns))
    for w in shown:
        at = span_text(w.start - p.start) if w.start and p.start else "?"
        row = f"{w.id:<{width}}  {at:>6}  {w.ticks:5}  {w.tps:4.1f}  {w.mspt_median:9.1f}  {w.mspt_max:6.0f}"
        row += f"  {100 * w.cpu_process:3.0f}%"
        counts = (getattr(w, attr) for attr, _ in columns)
        lines.append(row + "".join(f"  {str(n) if n >= 0 else '?':>8}" for n in counts))
    return lines


def counts_text(counts: dict[str, int], n: int) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:n])


def entities(p: Profile, busiest: int = 5) -> list[str]:
    """Entities by type, by world, and the chunks with the most (a mob farm, dropped items piling up)."""
    if not p.entities:
        return []
    lines = [f"\n## Entities: {p.entities}"]
    if p.entity_counts:
        lines.append(f"By type: {counts_text(p.entity_counts, 8)}")
    worlds = {w: n for w, n in p.world_entities.items() if n}
    if worlds:
        lines.append(f"By world: {counts_text(worlds, 8)}")
    chunks = sorted((c for c in p.chunks if c.total), key=lambda c: -c.total)[:busiest]
    if chunks:
        lines.append("Busiest chunks (block coordinates of the chunk centre):")
        width = max(len(c.world) for c in chunks)
        for c in chunks:
            where = f"x {c.x * 16 + 8} z {c.z * 16 + 8}"
            lines.append(f"{c.total:5}  {c.world:<{width}}  {where:<16}  {counts_text(c.counts, 4)}")
    return lines


def tree_lines(view: View, top: Merged, min_pct: float, max_lines: int, up: bool = False) -> list[str]:
    """The merged tree, branches under min_pct of the thread left out.

    Going down, runs of frames that pass almost all their time to one child are folded onto one line. Going up
    (callers), that run is the answer, so each caller gets its own line, marked with <, at the same depth.
    """
    p = view.profile
    limit = view.whole * min_pct / 100
    lines: list[str] = []
    mods: dict[str, str] = {}

    def mod(key: str) -> str:
        if key not in mods:
            mods[key] = package_of(key.rpartition(".")[0], p.class_sources)
        return mods[key]

    def name(key: str, before: str) -> str:
        """Class.method, with [Mod] in front where a mod's code starts."""
        owner = mod(key)
        tag = f"[{owner.replace(' (package)', '?')}] " if owner not in NOT_MODS and owner != before else ""
        return tag + short_name(key)

    def fold(chain: list[Merged], before: str) -> str:
        """A run of frames on one line: the first, the last, and up to three where another mod takes over."""
        keys = [n.key for n in chain]
        keep = set(range(len(keys)))
        if len(keys) > 3:
            entries = [i for i in range(1, len(keys) - 1) if mod(keys[i]) not in NOT_MODS]
            entries = [i for i in entries if mod(keys[i]) != mod(keys[i - 1])]
            keep = {0, len(keys) - 1, *entries[:3]}
        parts, last, prev = [], -1, before
        for i in sorted(keep):
            if i - last > 1:
                parts.append(f"... {i - last - 1} frames ...")
            parts.append(name(keys[i], prev))
            prev, last = mod(keys[i]), i
        return " > ".join(parts)

    def walk(node: Merged, depth: int, before: str) -> None:
        chain = [node]
        while True:  # fold a run of frames that each pass almost all their time to one child
            kids = [c for c in chain[-1].children.values() if c.total >= limit]
            # Going up, a single caller continues the list whatever time branches off below it.
            if len(kids) != 1 or (not up and kids[0].total < 0.97 * chain[-1].total):
                break
            chain.append(kids[0])
        if len(lines) >= max_lines:
            return
        last = chain[-1]
        indent = "  " * depth
        if up:
            prev = before
            for n, frame_node in enumerate(chain):
                mark = "< " if n else ""
                line = f"{_pct(frame_node.total, view.whole)} {amount(p, frame_node.total):>8}  {indent}{mark}"
                lines.append(line + name(frame_node.key, prev))
                prev = mod(frame_node.key)
        else:
            rest = last.total - sum(c.total for c in last.children.values())
            own_text = f"  (own {_pct(rest, view.whole).strip()})" if rest >= limit else ""
            amount_text = f"{_pct(last.total, view.whole)} {amount(p, last.total):>8}"
            lines.append(f"{amount_text}  {indent}{fold(chain, before)}{own_text}")
        for child in sorted(last.children.values(), key=lambda c: -c.total):
            if child.total >= limit:
                walk(child, depth + 1, mod(last.key))

    for child in sorted(top.children.values(), key=lambda c: -c.total):
        if child.total >= limit:
            walk(child, 0, "")
    if len(lines) >= max_lines:
        lines.append(f"... cut at {max_lines} lines; raise --lines or --min")
    return lines


def thread_report(view: View, top_n: int, min_pct: float, max_lines: int) -> list[str]:
    p, t = view.profile, view.thread
    unit = "allocated" if p.allocation else "sampled"
    lines = [f"\n## Thread {t.name}: {amount(p, view.whole)} {unit}, {len(t.frames)} frames"]
    lines.append("\n### Most own time (spent in the method itself, not in what it calls)")
    for key, value in sorted(view.own_by_method().items(), key=lambda kv: -kv[1])[:top_n]:
        lines.append(f"{_pct(value, view.whole)}  {key}")
    lines.append("\n### Most total time (including what it calls), mods only")
    shown = 0
    for key, value in sorted(view.total_by_method().items(), key=lambda kv: -kv[1]):
        if package_of(key.rpartition(".")[0], p.class_sources) in NOT_MODS:
            continue
        lines.append(f"{_pct(value, view.whole)}  {key}")
        shown += 1
        if shown >= top_n:
            break
    lines.append("\n### Own time by mod")
    lines.append(
        "(native, JVM and library code counts for the nearest mod up the stack, else Minecraft or Forge; mixins"
        " for the mod that wrote them"
        + ("" if p.class_sources else "; (package) marks a guess where the pack's table has no entry")
        + ")"
    )
    for name, value in sorted(view.own_by_package().items(), key=lambda kv: -kv[1])[:top_n]:
        if value < view.whole * 0.002:
            break
        lines.append(f"{_pct(value, view.whole)}  {name}")
    lines.append(f"\n### Hot call paths (at least {min_pct:g}% of the thread; [Mod] where its code starts)")
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


@dataclass
class Finding:
    level: str  # "warn" or "info"
    text: str


def _is_worldgen(f: Frame) -> bool:
    if f.key.endswith(("ChunkProviderServer.populate", "GameRegistry.generateWorld")):
        return True
    return f.method in ("provideChunk", "func_73154_d") and not f.cls.endswith(
        ("ChunkProviderServer", "ChunkProviderClient")
    )


# Where the main thread's time goes, by the kind of frame its time is under (MCP names, else the raw ones).
# A frame inside another kind counts for the inner one: world generation set off by a packet is world generation.
IDLE = "waiting between ticks"
TICK_PARTS: list[tuple[str, Callable[[Frame], bool]]] = [
    ("entities", lambda f: f.method in ("updateEntityWithOptionalForce", "func_72866_a")),
    (
        "tile entities",
        lambda f: f.method in ("updateEntity", "func_145845_h") and not f.cls.startswith("net.minecraft.world.World"),
    ),
    ("world generation", _is_worldgen),
    ("chunk loading from disk", lambda f: f.key.endswith("AnvilChunkLoader.loadChunk")),
    ("world saving", lambda f: f.key.endswith(("MinecraftServer.saveAllWorlds", "WorldServer.saveAllChunks"))),
    ("backups", lambda f: "backup" in f.cls.lower()),
    ("network", lambda f: f.key.endswith("NetworkSystem.networkTick")),
    (IDLE, lambda f: f.key.startswith("java.lang.Thread.sleep")),
]


def tick_parts(view: View) -> dict[str, tuple[float, dict[str, float]]]:
    """Per kind of work: its time, and that time per mod (mods only)."""
    frames, owners = view.thread.frames, view.owners()
    out: dict[str, tuple[float, dict[str, float]]] = {}
    stack: list[tuple[int, str | None]] = [(r, None) for r in view.thread.roots]
    while stack:
        i, label = stack.pop()
        f = frames[i]
        found = next((name for name, wanted in TICK_PARTS if wanted(f)), None)
        if found and (found != IDLE or label is None):  # a sleep inside other work is part of that work
            label = found
        if label:
            total, by_mod = out.setdefault(label, (0.0, defaultdict(float)))
            own = view.own(i)
            out[label] = (total + own, by_mod)
            if owners[i] not in NOT_MODS:
                by_mod[owners[i]] += own
        stack += [(c, label) for c in f.children]
    return out


def _mod_name(mod: str) -> str:
    return mod.replace(" (package)", "?")


def top_mods_text(by_mod: dict[str, float], whole: float, n: int = 3) -> str:
    top = [(m, v) for m, v in sorted(by_mod.items(), key=lambda kv: -kv[1])[:n] if v >= whole * 0.01]
    return ", ".join(f"{_mod_name(m)} {_pct(v, whole).strip()}" for m, v in top)


def pauses_game(name: str) -> bool:
    """A collector whose average time is a pause of the whole game, not a concurrent cycle."""
    return not any(word in name for word in ("Concurrent", "Cycles"))


def _thread_findings(p: Profile, view: View, warn: list[str], info: list[str]) -> None:
    whole, thread = view.whole, view.thread.name.lower()
    of = "of the slow ticks" if p.ticked else f"of the {thread}'s time"
    parts = tick_parts(view)
    shown = []
    for label, _ in TICK_PARTS:
        total, by_mod = parts.get(label, (0.0, {}))
        if label == IDLE or total < whole * 0.01:
            continue
        mods = top_mods_text(by_mod, whole)
        shown.append(f"{label} {_pct(total, whole).strip()}" + (f" ({mods})" if mods else ""))
        share = f"{100 * total / whole:.0f}% {of}"
        if label == "world generation" and total >= whole * 0.1:
            by = f" ({mods})" if mods else ""
            warn.append(f"World generation takes {share}{by}: pregenerate the world, or the dimension being explored")
        elif label == "tile entities" and total >= whole * 0.3:
            warn.append(f"Tile entities (machines, pipes, cables) take {share}: {mods or '?'}")
        elif label == "entities" and total >= whole * 0.3:
            most = f"; most: {counts_text(p.entity_counts, 3)}" if p.entity_counts else ""
            warn.append(f"Entities take {share} ({p.entities} entities{most})")
        elif label == "backups" and total >= whole * 0.05:
            warn.append(f"Backups take {share}")
        elif label == "world saving" and total >= whole * 0.05:
            warn.append(f"World saving takes {share}")
        elif label == "chunk loading from disk" and total >= whole * 0.1:
            warn.append(f"Loading chunks from disk takes {share}: a slow disk, or players flying fast")
    for mod, value in sorted(view.own_by_package().items(), key=lambda kv: -kv[1]):
        if value < whole * 0.15:
            break
        named = any(f"{_mod_name(mod)} " in w for w in warn)  # already blamed above
        if mod not in NOT_MODS and not named:
            warn.append(f"{_mod_name(mod)} takes {_pct(value, whole).strip()} {of} in its own code")
    if thread not in ("server thread", "client thread"):
        return
    if shown:
        info.append(f"Where the {thread} goes: {'; '.join(shown)}")
    if not p.ticked:  # slow ticks have no time between ticks to measure
        idle = parts.get(IDLE, (0.0, {}))[0]
        if idle < whole * 0.05:
            warn.append(f"The {thread} is busy {100 - 100 * idle / whole:.0f}% of the time: no headroom left")
        else:
            info.append(f"The {thread} waits {_pct(idle, whole).strip()} of the time between ticks (headroom)")


def findings(p: Profile, view: View) -> list[Finding]:
    """What stands out for a GTNH server or client, by rules of thumb: problems first, then notes."""
    warn: list[str] = []
    info: list[str] = []
    game = "client" if p.client else "server"

    windows = [w for w in p.windows if w.ticks]
    if windows:
        slowest = max(windows, key=lambda w: w.mspt_max)
        if slowest.mspt_max >= 1000:
            at = f" at {span_text(slowest.start - p.start)}" if slowest.start and p.start else ""
            warn.append(f"A tick took {_ms(slowest.mspt_max)}{at} (minute {slowest.id}): the {game} froze")
        lowest = min(windows, key=lambda w: w.tps)
        if 0 < lowest.tps < 19 and not p.client:
            warn.append(f"TPS fell to {lowest.tps:.1f} in minute {lowest.id} (20 is full speed)")
    if p.mspt.get("median", 0) >= 50:
        warn.append(f"Median tick {p.mspt['median']:.0f} ms: over the 50 ms a tick may take, so TPS is below 20")
    elif p.mspt.get("p95", 0) >= 50:
        info.append(f"1 tick in 20 takes over 50 ms (p95 {p.mspt['p95']:.0f} ms): short stutters")

    during = any(g.collections for g in p.gc.values())
    for gc_name, g in (p.gc if during else p.gc_since_start).items():
        if g.collections and pauses_game(gc_name) and g.avg_ms >= 200:
            warn.append(
                f"GC {gc_name}: {g.collections} collections {'during the profile' if during else 'since start'},"
                f" {_ms(g.avg_ms)} each on average; the {game} stops for each one"
            )
    if p.heap_max and p.heap_max < 4e9:
        warn.append(f"Max heap {bytes_text(p.heap_max)}: GTNH usually needs 6 GB or more (-Xmx)")
    if p.heap_max and p.old_gen_live >= 0.8 * p.heap_max:
        warn.append(
            f"After the last GC {bytes_text(p.old_gen_live)} of {bytes_text(p.heap_max)} was still in use:"
            " memory is nearly full (raise -Xmx, or look for a leak with /spark heapsummary)"
        )
    if "32-Bit" in p.jvm:
        warn.append("32-bit Java: the heap can't grow past about 2-3 GB; install a 64-bit Java")
    if 0 < p.cpu_threads <= 2:
        warn.append(f"Only {p.cpu_threads} CPU thread(s): the game shares them with GC, chunk saving and networking")
    if p.cpu_system[0] >= 0.9 and p.cpu_process[0] < 0.6 * p.cpu_system[0]:
        warn.append(
            f"The machine's CPU is {100 * p.cpu_system[0]:.0f}% busy but the game uses only"
            f" {100 * p.cpu_process[0]:.0f}%: something else is using it"
        )
    if p.swap_used >= 256e6:
        warn.append(f"{bytes_text(p.swap_used)} of swap in use: the machine may be short on RAM")
    if p.disk_total and p.disk_used >= 0.95 * p.disk_total:
        warn.append(f"Disk {100 * p.disk_used / p.disk_total:.0f}% full")
    if any(word.startswith("1.8") for word in p.java.split()):
        info.append("Java 8: GTNH runs better on Java 17 or newer")

    if not p.allocation:
        _thread_findings(p, view, warn, info)

    busiest = max(p.chunks, key=lambda c: c.total, default=None)
    if busiest and busiest.total >= 50:
        c = busiest
        warn.append(
            f"{c.total} entities in one chunk, {c.world} x {c.x * 16 + 8} z {c.z * 16 + 8}"
            f" ({counts_text(c.counts, 3)}): a mob farm or items piling up"
        )
    if p.entity_counts.get("Item", 0) >= 500:
        warn.append(f"{p.entity_counts['Item']} dropped items on the ground")
    return [Finding("warn", t) for t in warn] + [Finding("info", t) for t in info]


def findings_lines(found: list[Finding]) -> list[str]:
    if not found:
        return []
    lines = ["\n## Findings (rules of thumb: check them against the numbers below)"]
    return lines + [f"[{f.level}] {f.text}" for f in found]


def summary(
    p: Profile, name: str, thread: str | None = None, windows: list[int] | None = None, top_n: int = 15,
    min_pct: float = 1.0, max_lines: int = 60,
) -> str:  # fmt: skip
    view = View(p, pick_thread(p, thread), windows)
    lines = header(p, name) + findings_lines(findings(p, view)) + timeline(p) + entities(p)
    threads = busiest_threads(p)
    if len(threads) > 1:
        weights = [(t, sum(t.times)) for t in threads]
        all_time = sum(w for _, w in weights) or 1
        listed = ", ".join(f"{t.name} {_pct(w, all_time).strip()}" for t, w in weights[:8])
        lines.append(f"\nThreads: {listed}")
    if windows:
        lines.append(f"\nOnly window(s) {', '.join(map(str, windows))} below.")
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
        lines.append("Read downwards: the method, then < its caller, < that caller's caller, and so on")
    lines.append("")
    lines += tree_lines(view, tree, min_pct, max_lines, up=up)
    return "\n".join(lines)


# --- heap summaries ------------------------------------------------------------------------------------------


def heap_owner(cls: str) -> str:
    """Who a class in memory belongs to; arrays count for their element type, primitive arrays for java."""
    base = cls.split("[", 1)[0]
    return "java" if "." not in base else package_of(base)


def heap_header(h: Heap, name: str, what: str = "heap summary") -> list[str]:
    mc = f", Minecraft {h.minecraft}" if h.minecraft else ""
    lines = [f"# spark {what} {name}: {h.platform} {h.platform_version}{mc}, spark v{h.spark_version}"]
    memory = []
    if h.heap_total:
        heap = f"heap {bytes_text(h.heap_used)} used, {bytes_text(h.heap_total)} allocated"
        memory.append(heap + (f", {bytes_text(h.heap_max)} max" if h.heap_max else ""))
    if _gc_text(h.gc):
        memory.append(f"GC {_gc_text(h.gc)}")
    if memory:
        lines.append(f"Memory: {'; '.join(memory)}; players {h.players}")
    return lines


def heap_by_owner(entries: Iterable[HeapEntry]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for e in entries:
        totals = out[heap_owner(e.cls)]
        totals[0] += e.size
        totals[1] += e.instances
    return out


def heap_report(h: Heap, name: str, top_n: int = 25) -> str:
    total = h.size or 1
    lines = heap_header(h, name)
    objects = sum(e.instances for e in h.entries)
    lines.append(f"Live objects: {objects:,} in {len(h.entries):,} classes, {bytes_text(h.size)}")
    lines.append(f"\n## Biggest classes (of {len(h.entries):,})")
    lines.append(f"{'size':>8}  {'share':>6}  {'objects':>11}  class  [mod]")
    for e in sorted(h.entries, key=lambda e: -e.size)[:top_n]:
        owner = heap_owner(e.cls)
        tag = f"  [{owner}]" if owner not in NOT_MODS else ""
        lines.append(f"{bytes_text(e.size):>8}  {_pct(e.size, total)}  {e.instances:>11,}  {e.cls}{tag}")
    lines.append("\n## By mod (arrays count for their element type)")
    for owner, (size, count) in sorted(heap_by_owner(h.entries).items(), key=lambda kv: -kv[1][0])[:top_n]:
        lines.append(f"{bytes_text(size):>8}  {_pct(size, total)}  {count:>11,}  {owner}")
    lines.append(
        "\nbyte[], int[], Object[] and String are shared by everything, so they rarely point at a culprit; a"
        " class from a mod with far more objects than expected does. A leak shows as growth: take a second"
        " summary later and compare them."
    )
    return "\n".join(lines)


def heap_compare(a: Heap, a_name: str, b: Heap, b_name: str, top_n: int = 25) -> str:
    """What grew in memory from summary a to summary b (taken later)."""
    before = {e.cls: e for e in a.entries}
    after = {e.cls: e for e in b.entries}
    deltas = []
    for cls in before.keys() | after.keys():
        old, new = before.get(cls), after.get(cls)
        size = (new.size if new else 0) - (old.size if old else 0)
        count = (new.instances if new else 0) - (old.instances if old else 0)
        deltas.append((cls, size, count, new))
    lines = heap_header(b, f"{a_name} -> {b_name}", "heap growth")
    lines.append(
        f"Live objects: {bytes_text(a.size)} -> {bytes_text(b.size)} ({'+' if b.size >= a.size else '-'}"
        f"{bytes_text(abs(b.size - a.size))}). Growth between two summaries points at leaks; one moment can be noise."
    )
    lines.append("\n## Grew the most")
    lines.append(f"{'+size':>9}  {'+objects':>11}  {'now':>8}  class  [mod]")
    for cls, size, count, new in sorted(deltas, key=lambda d: -d[1])[:top_n]:
        if size <= 0:
            break
        owner = heap_owner(cls)
        tag = f"  [{owner}]" if owner not in NOT_MODS else ""
        now = bytes_text(new.size) if new else "gone"
        lines.append(f"{'+' + bytes_text(size):>9}  {count:>+11,}  {now:>8}  {cls}{tag}")
    lines.append("\n## By mod")
    old_owner, new_owner = heap_by_owner(a.entries), heap_by_owner(b.entries)
    changes = {
        o: new_owner.get(o, [0, 0])[0] - old_owner.get(o, [0, 0])[0] for o in old_owner.keys() | new_owner.keys()
    }
    for owner, size in sorted(changes.items(), key=lambda kv: -abs(kv[1]))[:15]:
        if size:
            lines.append(f"{'+' if size > 0 else '-'}{bytes_text(abs(size)):>8}  {owner}")
    return "\n".join(lines)
