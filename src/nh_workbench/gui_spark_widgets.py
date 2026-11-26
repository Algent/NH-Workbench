"""Pieces of the desktop spark window: cards, findings, charts, and the call tree model."""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from html import escape

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QPersistentModelIndex, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPalette, QPen, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from . import gui_theme, spark

# Categorical colours in a fixed order, light and dark steps (colour-blind safe as neighbours).
SERIES = (
    ("#2a78d6", "#3987e5"),
    ("#eb6834", "#d95926"),
    ("#1baf7a", "#199e70"),
    ("#eda100", "#c98500"),
    ("#e87ba4", "#d55181"),
    ("#008300", "#0ca30c"),
    ("#6250d6", "#9085e9"),
)
NO_INDEX = QModelIndex()  # the default parent: the invisible root
BAR_ROLE = Qt.ItemDataRole.UserRole + 10  # a cell's share, 0..1, drawn as a bar behind its text
SORT_ROLE = Qt.ItemDataRole.UserRole + 11
OK, WARN, BAD = "ok", "warn", "bad"


@dataclass
class Ink:
    """The colours to paint with, from the theme the app shows."""

    fg: QColor
    muted: QColor
    surface: QColor
    panel: QColor
    primary: QColor
    good: QColor
    warn: QColor
    bad: QColor
    dark: bool

    def series(self, i: int) -> QColor:
        return QColor(SERIES[i][1 if self.dark else 0])

    def status(self, status: str) -> QColor | None:
        return {OK: self.good, WARN: self.warn, BAD: self.bad}.get(status)


def ink(widget: QWidget, theme: str) -> Ink:
    pal = widget.palette()
    c = gui_theme.colors(theme)
    role = QPalette.ColorRole

    def themed(key: str, fallback: str) -> QColor:
        return QColor(c[key].hex6) if c else QColor(fallback)

    surface = pal.color(role.Base)
    return Ink(
        fg=pal.color(role.Text),
        muted=pal.color(role.PlaceholderText),
        surface=surface,
        panel=pal.color(role.Button),
        primary=pal.color(role.Highlight),
        good=themed("success", "#2e7d32"),
        warn=themed("warning", "#e65100"),
        bad=themed("error", gui_theme.WARNING),
        dark=surface.lightness() < 128,
    )


class Card(QFrame):
    """One number that matters, with a line of detail under it and a stripe saying whether it is fine."""

    def __init__(self, title: str, ink_of: Callable[[], Ink]) -> None:
        super().__init__()
        self.ink_of = ink_of
        self.state = ""
        self.title = QLabel(title)
        self.value = QLabel("-")
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        points = self.value.font().pointSizeF()  # the app's style sheet sets the size: say it in one too
        self.value.setStyleSheet(f"font-size: {points * 1.8:.1f}pt; font-weight: 600;")
        self.title.setEnabled(False)  # greyed
        self.detail.setEnabled(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 8, 10, 8)
        layout.setSpacing(2)
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.detail)
        layout.addStretch(1)
        self.setMinimumWidth(170)

    def set(self, value: str, detail: str, state: str = "", label: str = "", tip: str = "") -> None:
        """state ok, warn or bad colours the stripe; label says the same in words, next to the title."""
        self.value.setText(value)
        self.detail.setText(detail)
        self.state = state
        base = self.title.text().split("  ·  ")[0]
        self.title.setText(f"{base}  ·  {label}" if label else base)
        self.setToolTip(tip)
        self.update()

    def paintEvent(self, event) -> None:
        c = self.ink_of()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c.surface)
        p.drawRoundedRect(QRectF(self.rect()), 6, 6)
        stripe = c.status(self.state)
        if stripe:
            p.setBrush(stripe)
            p.drawRoundedRect(QRectF(0, 0, 4, self.height()), 2, 2)


class Findings(QLabel):
    """The report's findings: problems with a warning mark, then notes."""

    def __init__(self, ink_of: Callable[[], Ink]) -> None:
        super().__init__()
        self.ink_of = ink_of
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.found: list[spark.Finding] = []

    def show_findings(self, found: list[spark.Finding]) -> None:
        self.found = found
        c = self.ink_of()
        rows = []
        found = [f for f in found if not f.text.startswith("Where the")]  # the stacked bar shows that
        for title, level, mark, color in (
            ("Problems", "warn", "&#9650;", c.warn),  # a triangle
            ("Notes", "info", "&#9679;", c.muted),  # a dot
        ):
            items = [f for f in found if f.level == level]
            if not items:
                continue
            rows.append(f"<p style='margin-top:6px; margin-bottom:2px'><b>{title} ({len(items)})</b></p>")
            rows += [
                f"<p style='margin:1px 0 1px 8px'><span style='color:{color.name()}'>{mark}</span>"
                f"&nbsp;&nbsp;{escape(f.text)}</p>"
                for f in items
            ]
        self.setText("".join(rows) or "<p>Nothing stands out.</p>")


class PartsBar(QWidget):
    """Where the thread's time goes, as one stacked bar with a legend under it."""

    def __init__(self, ink_of: Callable[[], Ink]) -> None:
        super().__init__()
        self.ink_of = ink_of
        self.parts: list[tuple[str, float, str, int]] = []  # label, share, tooltip, colour slot (-1: grey)
        self.legend = QLabel()
        self.legend.setWordWrap(True)
        self.legend.setTextFormat(Qt.TextFormat.RichText)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 22, 0, 0)  # the bar is painted in that margin
        layout.addWidget(self.legend)
        self.setMouseTracking(True)

    def set_parts(self, parts: list[tuple[str, float, str, int]]) -> None:
        self.parts = parts
        c = self.ink_of()
        items = []
        for label, share, _, slot in parts:
            swatch = "&#9633;" if slot == -2 else "&#9632;"  # waiting: an empty square, like its outlined segment
            color = self._color(c, slot).name()
            items.append(f"<span style='color:{color}'>{swatch}</span>&nbsp;{escape(label)}&nbsp;{100 * share:.1f}%")
        self.legend.setText("&nbsp;&nbsp;&nbsp; ".join(items))
        self.update()

    @staticmethod
    def _color(c: Ink, slot: int) -> QColor:
        """A series colour; -1 (other work) grey, -2 (waiting) the muted ink its outline is drawn in."""
        if slot >= 0:
            return c.series(slot)
        grey = QColor(c.muted)
        if slot == -1:
            grey.setAlpha(120)
        return grey

    def _segments(self) -> list[tuple[QRectF, int]]:
        width, x, out = self.width(), 0.0, []
        for i, (_, share, _, _) in enumerate(self.parts):
            w = share * width
            if w >= 1:
                out.append((QRectF(x, 0, max(1.0, w - 2), 16), i))  # 2 px gap between segments
            x += w
        return out

    def paintEvent(self, event) -> None:
        c = self.ink_of()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for rect, i in self._segments():
            color = self._color(c, self.parts[i][3])
            if self.parts[i][3] == -2:
                p.setPen(QPen(color, 1, Qt.PenStyle.DashLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                rect = rect.adjusted(0.5, 0.5, -0.5, -0.5)
            else:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(color)
            p.drawRoundedRect(rect, 3, 3)

    def mouseMoveEvent(self, event) -> None:
        for rect, i in self._segments():
            if rect.contains(event.position()):
                label, share, tip, _ = self.parts[i]
                QToolTip.showText(event.globalPosition().toPoint(), f"{label}: {100 * share:.1f}%{tip}", self)
                return
        QToolTip.hideText()


class Timeline(QWidget):
    """Each minute of the recording: its slowest tick as a bar, the median tick as a line, log scale.

    A click on a minute emits its id (None when it was the one already picked).
    """

    picked = Signal(object)
    LEFT, BOTTOM, TOP, RIGHT = 56, 22, 24, 10

    def __init__(self, ink_of: Callable[[], Ink]) -> None:
        super().__init__()
        self.ink_of = ink_of
        self.profile: spark.Profile | None = None
        self.windows: list[spark.Window] = []
        self.selected: set[int] = set()
        self.hover = -1
        self.setMouseTracking(True)
        self.setMinimumHeight(210)

    def set_profile(self, profile: spark.Profile | None, selected: list[int] | None) -> None:
        self.profile = profile
        self.windows = [w for w in profile.windows if w.ticks] if profile else []
        self.selected = set(selected or ())
        self.update()

    def _range(self) -> tuple[float, float]:
        lo = min((w.mspt_median for w in self.windows if w.mspt_median > 0), default=1)
        hi = max((w.mspt_max for w in self.windows), default=100)
        return max(1.0, lo / 2), max(hi * 1.3, 100.0)

    def _plot(self) -> QRectF:
        width, height = self.width() - self.LEFT - self.RIGHT, self.height() - self.TOP - self.BOTTOM
        return QRectF(self.LEFT, self.TOP, width, height)

    def _y(self, ms: float, plot: QRectF) -> float:
        lo, hi = self._range()
        t = (math.log10(max(ms, lo)) - math.log10(lo)) / (math.log10(hi) - math.log10(lo))
        return plot.bottom() - t * plot.height()

    def _slot(self, x: float, plot: QRectF) -> int:
        if not self.windows or not plot.left() <= x <= plot.right():
            return -1
        return min(len(self.windows) - 1, int((x - plot.left()) / plot.width() * len(self.windows)))

    def paintEvent(self, event) -> None:
        c = self.ink_of()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot()
        if not self.windows:
            p.setPen(c.muted)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No per-minute data in this profile")
            return
        lo, hi = self._range()
        grid = QColor(c.muted)
        grid.setAlpha(60)
        small = p.font()
        small.setPointSizeF(small.pointSizeF() * 0.85)
        p.setFont(small)
        drawn: list[float] = []
        for ms in (50, 1, 10, 100, 1000, 10000, 5, 500, 5000, 50000, 2, 20, 200, 2000, 20000):  # most useful first
            y = self._y(ms, plot)
            if not lo <= ms <= hi or any(abs(y - d) < 18 for d in drawn):
                continue
            drawn.append(y)
            budget = ms == 50
            pen = QPen(c.fg if budget else grid, 1, Qt.PenStyle.DashLine if budget else Qt.PenStyle.SolidLine)
            p.setPen(pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(c.muted)
            label = f"{ms // 1000} s" if ms >= 1000 else f"{ms} ms"
            right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            p.drawText(QRectF(0, y - 8, self.LEFT - 6, 16), right, label)
        n = len(self.windows)
        slot = plot.width() / n
        bar = max(2.0, min(slot * 0.7, 28.0))
        for i, w in enumerate(self.windows):
            x = plot.left() + slot * i + (slot - bar) / 2
            top = self._y(w.mspt_max, plot)
            color = QColor(c.primary)
            if self.selected and w.id not in self.selected:
                color.setAlpha(80)
            if i == self.hover:
                color = color.lighter(125) if c.dark else color.darker(115)
            path = QPainterPath()
            path.addRoundedRect(QRectF(x, top, bar, plot.bottom() - top), min(3.0, bar / 2), min(3.0, bar / 2))
            p.fillPath(path, color)
        line = QPen(c.fg, 2)
        p.setPen(line)
        points = [
            QPointF(plot.left() + slot * (i + 0.5), self._y(w.mspt_median, plot)) for i, w in enumerate(self.windows)
        ]
        for a, b in zip(points, points[1:], strict=False):
            p.drawLine(a, b)
        if n == 1:
            p.drawEllipse(points[0], 3, 3)
        p.setPen(c.muted)
        start = self.profile.start if self.profile else 0
        marks = max(1, n // 6)
        for i in range(0, n, marks):
            w = self.windows[i]
            at = spark._span(w.start - start) if w.start and start else str(i)
            x = plot.left() + slot * (i + 0.5)
            p.drawText(QRectF(x - 40, plot.bottom() + 4, 80, 16), Qt.AlignmentFlag.AlignCenter, at)
        self._legend(p, c)

    def _legend(self, p: QPainter, c: Ink) -> None:
        x = float(self.LEFT)
        metrics = p.fontMetrics()
        for kind, text in (("bar", "slowest tick of the minute"), ("line", "median tick"), ("dash", "50 ms: 20 TPS")):
            if kind == "bar":
                p.fillRect(QRectF(x, 7, 8, 10), c.primary)
            else:
                solid = kind == "line"
                p.setPen(QPen(c.fg, 2 if solid else 1, Qt.PenStyle.SolidLine if solid else Qt.PenStyle.DashLine))
                p.drawLine(QPointF(x, 12), QPointF(x + 14, 12))
            x += 20 if kind != "bar" else 14
            p.setPen(c.muted)
            p.drawText(QPointF(x, 16), text)
            x += metrics.horizontalAdvance(text) + 18

    def mouseMoveEvent(self, event) -> None:
        i = self._slot(event.position().x(), self._plot())
        if i != self.hover:
            self.hover = i
            self.update()
        if i < 0:
            QToolTip.hideText()
            return
        w = self.windows[i]
        start = self.profile.start if self.profile else 0
        at = spark._span(w.start - start) if w.start and start else "?"
        counts = [f"players {w.players}", f"entities {w.entities}", f"chunks {w.chunks}"]
        tip = (
            f"Minute {w.id}, at {at}\n"
            f"TPS {w.tps:.1f}, median tick {w.mspt_median:.1f} ms, slowest {w.mspt_max:.0f} ms\n"
            f"CPU {100 * w.cpu_process:.0f}%, {', '.join(counts)}\n"
            + ("Click: back to all minutes" if w.id in self.selected else "Click: only this minute")
        )
        QToolTip.showText(event.globalPosition().toPoint(), tip, self)

    def leaveEvent(self, event) -> None:
        self.hover = -1
        self.update()

    def mousePressEvent(self, event) -> None:
        i = self._slot(event.position().x(), self._plot())
        if i >= 0:
            wid = self.windows[i].id
            self.picked.emit(None if self.selected == {wid} else wid)


class BarDelegate(QStyledItemDelegate):
    """Cells with a share (BAR_ROLE) get a bar behind their text."""

    def __init__(self, parent, ink_of: Callable[[], Ink]) -> None:
        super().__init__(parent)
        self.ink_of = ink_of

    def paint(self, painter, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> None:
        share = index.data(BAR_ROLE)
        if share and not option.state & QStyle.StateFlag.State_Selected:
            color = QColor(self.ink_of().primary)
            color.setAlpha(70)
            rect = QRectF(option.rect).adjusted(2, 4, -2, -4)
            rect.setWidth(max(1.0, rect.width() * min(1.0, float(share))))
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(rect, 2, 2)
            painter.restore()
        super().paint(painter, option, index)


# --- the call tree ------------------------------------------------------------------------------------


class _Node:
    """A row: a run of frames that pass (almost) all their time down to one child, shown as one line."""

    __slots__ = ("chain", "parent", "row", "kids")

    def __init__(self, chain: list[spark.Merged], parent: "_Node | None", row: int) -> None:
        self.chain, self.parent, self.row = chain, parent, row
        self.kids: list[_Node] | None = None


class CallTreeModel(QAbstractItemModel):
    """spark's merged call tree (what is called, or who calls) with folded runs and lazily made rows."""

    COLUMNS = ("Method", "Total", "Time", "Own", "Mod")
    MIN_SHARE = 0.0005  # of the thread: smaller branches are left out

    def __init__(self, top: spark.Merged, view: spark.View, up: bool) -> None:
        super().__init__()
        self.view, self.up = view, up
        self.whole = view.whole
        self.limit = view.whole * self.MIN_SHARE
        self._mods: dict[str, str] = {}
        self.root = _Node([top], None, 0)

    # what a row is

    def mod(self, key: str) -> str:
        if key not in self._mods:
            self._mods[key] = spark.package_of(key.rpartition(".")[0], self.view.profile.class_sources)
        return self._mods[key]

    def _fold(self, start: spark.Merged) -> list[spark.Merged]:
        chain = [start]
        while True:
            kids = [c for c in chain[-1].children.values() if c.total >= self.limit]
            if len(kids) != 1 or (not self.up and kids[0].total < 0.97 * chain[-1].total):
                return chain
            chain.append(kids[0])

    def kids(self, node: _Node) -> list[_Node]:
        if node.kids is None:
            found = [c for c in node.chain[-1].children.values() if c.total >= self.limit]
            found.sort(key=lambda c: -c.total)
            node.kids = [_Node(self._fold(m), node, i) for i, m in enumerate(found)]
        return node.kids

    def node(self, index: QModelIndex) -> _Node:
        return index.internalPointer() if index.isValid() else self.root

    def key(self, index: QModelIndex) -> str:
        return self.node(index).chain[-1].key

    def starts_mod(self, node: _Node) -> str:
        """The mod whose code starts on this row, if one does."""
        above = self.mod(node.parent.chain[-1].key) if node.parent and node.parent is not self.root else ""
        for m in node.chain:
            owner = self.mod(m.key)
            if owner not in spark._NOT_MODS and owner != above:
                return owner
            above = owner
        return ""

    def label(self, node: _Node) -> str:
        names = [spark._short(m.key) for m in node.chain]
        sep = " < " if self.up else " > "
        if len(names) > 3:
            names = [names[0], f"... {len(names) - 2} frames ...", names[-1]]
        mod = self.starts_mod(node)
        tag = f"[{mod.replace(' (package)', '?')}] " if mod else ""
        return tag + sep.join(names)

    def own(self, node: _Node) -> float:
        last = node.chain[-1]
        return max(0.0, last.total - sum(c.total for c in last.children.values()))

    # the Qt model

    def index(self, row: int, column: int, parent: QModelIndex | QPersistentModelIndex = NO_INDEX) -> QModelIndex:
        kids = self.kids(self.node(parent))
        if 0 <= row < len(kids):
            return self.createIndex(row, column, kids[row])
        return QModelIndex()

    def parent(self, index: QModelIndex | QPersistentModelIndex = NO_INDEX):  # type: ignore[override]
        if not index.isValid():
            return QModelIndex()
        up = index.internalPointer().parent
        if up is None or up is self.root:
            return QModelIndex()
        return self.createIndex(up.row, 0, up)

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = NO_INDEX) -> int:
        if parent.isValid() and parent.column() != 0:
            return 0
        return len(self.kids(self.node(parent)))

    def hasChildren(self, parent: QModelIndex | QPersistentModelIndex = NO_INDEX) -> bool:
        if parent.isValid() and parent.column() != 0:
            return False
        last = self.node(parent).chain[-1]
        return any(c.total >= self.limit for c in last.children.values())

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = NO_INDEX) -> int:
        return len(self.COLUMNS)

    def headerData(self, section: int, orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        node: _Node = index.internalPointer()
        col, last = index.column(), node.chain[-1]
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return self.label(node)
            if col == 1:
                return f"{100 * last.total / self.whole:.1f}%"
            if col == 2:
                return spark.amount(self.view.profile, last.total)
            if col == 3:
                own = self.own(node)
                return f"{100 * own / self.whole:.1f}%" if not self.up and own >= self.limit else ""
            return self.mod(last.key).replace(" (package)", "?")
        if role == BAR_ROLE and col == 1:
            return last.total / self.whole
        if role == Qt.ItemDataRole.ToolTipRole and col == 0:
            return ("\n< " if self.up else "\n> ").join(m.key for m in node.chain)
        if role == Qt.ItemDataRole.FontRole and col == 0 and self.starts_mod(node):
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (1, 2, 3):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None


def expand_hot(tree, model: CallTreeModel, share: float = 0.05, budget: int = 150) -> None:
    """Open the branches that hold at least `share` of the thread, breadth first, up to `budget` rows."""
    queue, rows = [QModelIndex()], 0
    while queue and rows < budget:
        parent = queue.pop(0)
        for row in range(model.rowCount(parent)):
            index = model.index(row, 0, parent)
            rows += 1
            if model.node(index).chain[-1].total >= model.whole * share and model.hasChildren(index):
                tree.expand(index)
                queue.append(index)


class ReportHighlighter(QSyntaxHighlighter):
    """Colours in the text report: headings, findings, mod tags, and the shares that matter."""

    _PCT = re.compile(r"(?<![\w.])(\d{1,3}\.\d)%")
    _TAG = re.compile(r"\[[^\]\s][^\]]*\](?= )")
    _OWN = re.compile(r"\(own [\d.]+%\)")

    def __init__(self, document, ink_of: Callable[[], Ink]) -> None:
        super().__init__(document)
        self.ink_of = ink_of

    @staticmethod
    def _fmt(color: QColor, bold: bool = False) -> QTextCharFormat:
        f = QTextCharFormat()
        f.setForeground(color)
        if bold:
            f.setFontWeight(QFont.Weight.Bold)
        return f

    def _share(self, c: Ink, match: re.Match, start: int) -> None:
        """Own time: 10% or more stands out, 30% or more is most of the work."""
        share = float(match[1])
        color = c.bad if share >= 30 else c.warn if share >= 10 else None
        if color is not None:
            self.setFormat(start + match.start(), match.end() - match.start(), self._fmt(color, True))

    def highlightBlock(self, text: str) -> None:
        c = self.ink_of()
        if text.startswith("#"):
            self.setFormat(0, len(text), self._fmt(c.primary, True))
            self.setCurrentBlockState(1 if "own time" in text.lower() else 0)  # a section of own time shares
            return
        self.setCurrentBlockState(max(0, self.previousBlockState()))
        for marker, color in (("[warn]", c.warn), ("[info]", c.muted)):
            if text.startswith(marker):
                self.setFormat(0, len(marker), self._fmt(color, True))
                return
        for m in self._TAG.finditer(text):
            self.setFormat(m.start(), m.end() - m.start(), self._fmt(c.series(0), True))
        for m in self._OWN.finditer(text):
            self.setFormat(m.start(), m.end() - m.start(), self._fmt(c.muted))
            inner = self._PCT.search(m[0])
            if inner:
                self._share(c, inner, m.start())
        if self.currentBlockState() == 1:
            first = self._PCT.match(text.lstrip())
            if first:
                self._share(c, first, len(text) - len(text.lstrip()))
