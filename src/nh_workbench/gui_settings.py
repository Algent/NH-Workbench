"""The desktop UI's Settings dialog: the terminal UI's Settings menu (x) as a form.

The values are saved together with Save; the buttons along the bottom (pack data, folders, update) act at once.
"""

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import config, gui_theme, proc, tui

if TYPE_CHECKING:
    from .gui import MainWindow

Change = tuple[tuple[str, ...], str | bool | int | list | None]  # keys in the config file, new value (None: remove)


def _row(*widgets: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    for i, widget in enumerate(widgets):
        row.addWidget(widget, 1 if i == 0 else 0)
    return row


def open_folder(which: str) -> None:
    folder = config.config_path().parent if which == "config" else config.cache_dir()
    folder.mkdir(parents=True, exist_ok=True)
    proc.open_path(folder)


class SettingsDialog(QDialog):
    def __init__(self, owner: "MainWindow", focus: str = "") -> None:
        super().__init__(owner)
        self.owner = owner
        cfg = owner.cfg
        self.setWindowTitle("Settings - NH Workbench")
        self.setMinimumWidth(680)

        self.theme = QComboBox()
        current = gui_theme.theme_name(cfg.theme)
        for name, label in {**gui_theme.THEMES, current: gui_theme.THEMES.get(current, current)}.items():
            self.theme.addItem(label, name)
        self.theme.setCurrentIndex(self.theme.findData(current))
        self.theme.setMaximumWidth(220)
        self.theme.setToolTip("Shared with the terminal UI: picking one there or here changes both")
        look = QFormLayout()
        look.addRow("Theme", _row(self.theme, QWidget()))

        self.root = QLineEdit(str(cfg.root))
        change_root = QPushButton("Change...")
        change_root.clicked.connect(self._pick_root)
        self.protocol = QComboBox()
        self.protocol.addItem("SSH", "ssh")
        self.protocol.addItem("HTTPS", "https")
        self.protocol.setCurrentIndex(self.protocol.findData(cfg.clone_protocol))
        self.protocol.setMaximumWidth(120)
        self.protocol.setToolTip("For new clones and the remotes the tool adds; existing ones are left as they are")
        checkouts = QFormLayout()
        checkouts.addRow("Repo folder", _row(self.root, change_root))
        checkouts.addRow("Clone over", _row(self.protocol, QWidget()))

        self.minutes = QSpinBox()
        self.minutes.setRange(0, 24 * 60)
        self.minutes.setSuffix(" min")
        self.minutes.setSpecialValueText("Off")
        self.minutes.setValue(cfg.refresh_minutes)
        self.minutes.setMaximumWidth(120)
        self.fetch = QCheckBox("Fetch every repo, not only rescan")
        self.fetch.setChecked(cfg.refresh_fetch)
        refresh = QFormLayout()
        refresh.addRow("Every", _row(self.minutes, QWidget()))
        refresh.addRow("", self.fetch)

        self.ide = QLineEdit(cfg.ide_command or "")
        found = config.find_ide(dataclasses.replace(cfg, ide_command=None, ide_off=False))
        self.ide.setPlaceholderText(
            f"Found automatically: {found}" if found else "Path to idea64.exe or an idea script, or a command on PATH"
        )
        self.ide.setClearButtonEnabled(True)
        browse_ide = QPushButton("Browse...")
        browse_ide.clicked.connect(self._pick_ide)
        self.ide_off = QCheckBox("I do not use IntelliJ: no IDE actions")
        self.ide_off.toggled.connect(lambda off: (self.ide.setEnabled(not off), browse_ide.setEnabled(not off)))
        self.ide_off.setChecked(cfg.ide_off)
        ide = QFormLayout()
        ide.addRow("Launcher", _row(self.ide, browse_ide))
        ide.addRow("", self.ide_off)

        self.hidden = self._list([(name, name) for name in cfg.hidden], "No hidden repos")
        self.kept = self._list([(f"{repo}  {branch}", (repo, branch)) for repo, branch in cfg.kept], "None protected")
        lists = QHBoxLayout()
        lists.addWidget(self._group("Hidden repos", self._list_box(self.hidden, "Show again")))
        lists.addWidget(self._group("Branches protected from cleanup", self._list_box(self.kept, "Unprotect")))

        refresh_pack = QPushButton("Download the pack data again")
        refresh_pack.setToolTip("Otherwise it is downloaded again every hour")
        refresh_pack.clicked.connect(lambda: (owner.refresh_pack(), refresh_pack.setEnabled(False)))
        config_folder = QPushButton("Open config folder")
        config_folder.clicked.connect(lambda: open_folder("config"))
        logs_folder = QPushButton("Open logs and cache folder")
        logs_folder.clicked.connect(lambda: open_folder("logs"))
        label, why = tui.tool_entry(owner.wb.tool, owner.wb.tool_updated)
        self.update_button = QPushButton(f"{label}: {why}" if why else label)
        self.update_button.setEnabled(why is None)
        self.update_button.clicked.connect(lambda: (owner.update_tool(), self.update_button.setEnabled(False)))
        actions = QHBoxLayout()
        for button in (refresh_pack, config_folder, logs_folder):
            actions.addWidget(button)
        actions.addStretch(1)

        where = QLabel(f"Config file: {config.config_path()}")
        where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        where.setEnabled(False)  # greyed, like the TUI's dim lines
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self._group("Look", look))
        layout.addWidget(self._group("Checkouts", checkouts))
        layout.addWidget(self._group("Auto-refresh", refresh))
        layout.addWidget(self._group("IntelliJ", ide))
        layout.addLayout(lists)
        layout.addLayout(actions)
        layout.addLayout(_row(self.update_button, QWidget()))
        layout.addWidget(where)
        layout.addWidget(buttons)
        if focus == "ide":
            self.ide.setFocus()

    @staticmethod
    def _group(title: str, inner) -> QGroupBox:
        box = QGroupBox(title)
        box.setLayout(inner)
        return box

    @staticmethod
    def _list(items: list[tuple[str, object]], empty: str) -> QListWidget:
        widget = QListWidget()
        widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        widget.setMaximumHeight(120)
        for text, data in items:
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, data)
            widget.addItem(item)
        if not items:
            item = QListWidgetItem(empty)
            item.setFlags(Qt.ItemFlag.NoItemFlags)  # greyed, cannot be picked
            widget.addItem(item)
        return widget

    @staticmethod
    def _list_box(widget: QListWidget, action: str) -> QVBoxLayout:
        button = QPushButton(action)
        button.setEnabled(False)
        widget.itemSelectionChanged.connect(lambda: button.setEnabled(bool(widget.selectedItems())))

        def drop() -> None:
            for item in widget.selectedItems():
                widget.takeItem(widget.row(item))

        button.clicked.connect(drop)
        box = QVBoxLayout()
        box.addWidget(widget)
        box.addLayout(_row(QWidget(), button))
        return box

    @staticmethod
    def _items(widget: QListWidget) -> set:
        return {widget.item(i).data(Qt.ItemDataRole.UserRole) for i in range(widget.count())}

    def _pick_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Folder with your checkouts", self.root.text())
        if folder:
            self.root.setText(folder)

    def _pick_ide(self) -> None:
        start = str(Path(self.ide.text()).parent) if self.ide.text() else ""
        path, _ = QFileDialog.getOpenFileName(self, "IntelliJ launcher", start)
        if path:
            self.ide.setText(path)

    def _error(self, text: str) -> None:
        QMessageBox.warning(self, "Settings", text)

    def changes(self) -> list[Change] | None:
        """What to write to the config file; None when a value is invalid (and said so)."""
        cfg = self.owner.cfg
        out: list[Change] = []
        if self.theme.currentData() != gui_theme.theme_name(cfg.theme):
            out.append((("theme",), self.theme.currentData()))
        root = self.root.text().strip()
        if root and Path(root).expanduser() != cfg.root:
            if not Path(root).expanduser().is_dir():
                self._error(f"Not a folder: {root}")
                return None
            out.append((("root",), root))
        if self.protocol.currentData() != cfg.clone_protocol:
            out.append((("clone_protocol",), self.protocol.currentData()))
        if self.minutes.value() != cfg.refresh_minutes:
            out.append((("refresh_minutes",), self.minutes.value()))
        if self.fetch.isChecked() != cfg.refresh_fetch:
            out.append((("refresh_fetch",), self.fetch.isChecked() or None))
        off, ide = self.ide_off.isChecked(), self.ide.text().strip()
        if off != cfg.ide_off or (not off and (ide or None) != cfg.ide_command):
            if not off and ide and not config.runnable(ide):
                self._error(f"Not found: {ide}\nTick the box below it if you do not use IntelliJ.")
                return None
            out.append((("ide_command",), False if off else ide or None))
        hidden = self._items(self.hidden)
        out += [(("repos", name, "ignore"), None) for name in cfg.hidden if name not in hidden]
        kept = self._items(self.kept)
        for repo in sorted({repo for repo, _ in cfg.kept}):
            before = cfg.repo(repo).keep_branches
            left = [b for b in before if (repo, b) in kept]
            if len(left) != len(before):
                out.append((("repos", repo, "keep_branches"), left or None))
        return out

    def accept(self) -> None:
        changes = self.changes()
        if changes is None:
            return
        if self.owner.apply_settings(changes):
            super().accept()
