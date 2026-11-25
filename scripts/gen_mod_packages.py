"""Generate src/nh_workbench/mod_packages.toml: Java package prefix -> mod name, for spark reports.

    uv run python scripts/gen_mod_packages.py "<pack>/.minecraft/mods" "<server pack>/mods" ...

Reads every jar: the mod name from mcmod.info (else the file name), the packages from its classes. A package
gets the mod that owns it alone, or the one holding nearly all its classes (mods often ship copies of other
mods' APIs). Packages still shared are printed so they can be settled in OVERRIDES below. For a package
found in several folders, the last folder holding it decides (mods get merged and renamed between packs), so
list the newest pack last.
"""

import json
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "nh_workbench" / "mod_packages.toml"
DOMINANT = 0.7  # share of a package's classes one mod needs to own it despite copies elsewhere
# Never mapped: the game, Forge, and libraries also on the game's own classpath (a copy in a mod jar is not
# where those classes load from).
SKIP = (
    "net.minecraft", "net.minecraftforge", "cpw.mods.fml", "java", "javax", "jdk", "sun", "com.sun",
    "org.apache", "com.google", "io.netty", "it.unimi", "org.lwjgl", "com.mojang", "paulscode", "gnu.trove",
    "com.ibm.icu", "org.objectweb", "joptsimple", "org.slf4j", "org.jetbrains", "org.intellij", "kotlin",
    "kotlinx", "scala", "org.bouncycastle", "tv.twitch", "com.typesafe", "akka", "LZMA", "argo", "oshi",
    "com.jcraft", "org.json", "org.yaml", "com.fasterxml", "META-INF",
)  # fmt: skip
TLDS = {"com", "net", "org", "io", "me", "de", "dev", "gg", "fr", "it", "ru", "cn", "xyz", "info", "club", "uk"}
# Decisions for packages the jars do not settle: prefix -> mod name ("" leaves it unmapped).
OVERRIDES: dict[str, str] = {
    "buildcraft.api": "BuildCraft",
    "cofh.api": "CoFH Core",
    "com.gtnewhorizon.gtnhlib.color": "GTNH Lib",
}

_VERSION = re.compile(r"[-_+](?=v?\d)")
_FORMAT = re.compile(r"§.")


def mod_name(jar: zipfile.ZipFile, path: Path) -> str:
    try:
        info = json.loads(jar.read("mcmod.info").decode("utf-8", "replace"), strict=False)
        mods = info.get("modList", []) if isinstance(info, dict) else info
        name = _FORMAT.sub("", str(mods[0].get("name") or "")).strip()
        if name:
            return name
    except (KeyError, ValueError, IndexError, AttributeError, TypeError):
        pass
    return _VERSION.split(path.stem)[0]


def skipped(package: str) -> bool:
    return any(package == s or package.startswith(s + ".") for s in SKIP)


class Node:
    def __init__(self) -> None:
        self.children: dict[str, Node] = {}
        self.classes: dict[str, int] = defaultdict(int)  # mod -> classes in this subtree
        self.direct: dict[str, int] = defaultdict(int)  # mod -> classes in this very package
        self.folder = -1  # the folder the counts come from: the last one with classes here

    def count(self, folder: int, mod: str, direct: bool) -> None:
        if self.folder != folder:
            self.classes.clear()
            self.direct.clear()
            self.folder = folder
        self.classes[mod] += 1
        if direct:
            self.direct[mod] += 1


def build(folders: list[Path]) -> Node:
    root = Node()
    for index, folder in enumerate(folders):
        for path in sorted(folder.rglob("*.jar")):
            try:
                with zipfile.ZipFile(path) as jar:
                    name = mod_name(jar, path)
                    classes = [n for n in jar.namelist() if n.endswith(".class") and "/" in n]
            except (OSError, zipfile.BadZipFile) as e:
                print(f"skipped {path.name}: {e}", file=sys.stderr)
                continue
            for entry in classes:
                parts = entry.split("/")[:-1]
                if parts[0] == "META-INF" or skipped(".".join(parts)):
                    continue
                node = root
                for depth, part in enumerate(parts, 1):
                    node = node.children.setdefault(part, Node())
                    node.count(index, name, depth == len(parts))
    return root


def allowed(path: list[str]) -> bool:
    """Too broad to map on its own: com, org.., or mods.."""
    return len(path) >= 2 or (path[0] not in TLDS and path[0] != "mods")


def namespace(path: list[str]) -> bool:
    """An organisation's namespace (com.gtnewhorizons): shared by many mods, so only mapped when one owns it all."""
    return len(path) == 2 and (path[0] in TLDS or path[0] == "mods")


def owner(classes: dict[str, int], share: float = DOMINANT) -> str | None:
    if not classes:
        return None
    total = sum(classes.values())
    mod, count = max(classes.items(), key=lambda kv: kv[1])
    return mod if count == total or count >= share * total else None


def resolve(root: Node) -> tuple[dict[str, str], list[str]]:
    table: dict[str, str] = {}
    unclear: list[str] = []

    def walk(node: Node, path: list[str], inherited: str | None) -> None:
        prefix = ".".join(path)
        if prefix in OVERRIDES:
            if OVERRIDES[prefix] != inherited:
                table[prefix] = OVERRIDES[prefix]
            inherited = OVERRIDES[prefix] or None
        elif path and allowed(path):
            # The whole subtree, else just the classes in this package (its subpackages decide for themselves).
            share = 1.0 if namespace(path) else DOMINANT
            mod = owner(node.classes, share) or owner(node.direct, share)
            if mod is None:
                if node.direct and inherited is None:
                    shares = sorted(node.direct.items(), key=lambda kv: -kv[1])[:4]
                    unclear.append(f"{prefix}: " + ", ".join(f"{m} {c}" for m, c in shares))
            else:
                if mod != inherited:
                    table[prefix] = mod
                if len(node.classes) == 1 and not any(k.startswith(prefix + ".") for k in OVERRIDES):
                    return  # the whole subtree is that mod's
                inherited = mod
        for part, child in sorted(node.children.items()):
            walk(child, [*path, part], inherited)

    walk(root, [], None)
    return {k: v for k, v in table.items() if v}, unclear


def label(folder: Path) -> str:
    """The pack a mods folder belongs to: the nearest folder name with a version or date in it."""
    return next((f.name for f in [folder, *folder.parents] if re.search(r"\d", f.name)), folder.name)


def main() -> None:
    folders = [Path(a) for a in sys.argv[1:]]
    if not folders or not all(f.is_dir() for f in folders):
        sys.exit(__doc__)
    table, unclear = resolve(build(folders))
    lines = [
        "# Java package prefix -> mod, for spark reports; the longest matching prefix wins.",
        "# Generated by scripts/gen_mod_packages.py from: " + ", ".join(label(f) for f in folders),
        "# Rerun it rather than editing by hand; decisions for shared packages go in its OVERRIDES.",
        "",
    ]
    lines += [f"{json.dumps(prefix)} = {json.dumps(mod, ensure_ascii=False)}" for prefix, mod in sorted(table.items())]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(table)} prefixes, {len(set(table.values()))} mods -> {OUT}")
    if unclear:
        print(f"\n{len(unclear)} shared packages left unmapped (settle them in OVERRIDES if they matter):")
        print("\n".join(f"  {u}" for u in unclear))


if __name__ == "__main__":
    main()
