"""StagingPage contents including; hexdiff, stats, staging queue files."""

from __future__ import annotations

from core.node import ModTracker, ModGroup, ConflictInfo
from core.contracts import SourceRebuildFlags
from ui.settings import Shortcut, Shortcuts

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QShortcut
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)
from ui.hex_model import EDITABLE_COLUMNS, HexGridModelBase, HexTableView
from utilities import hline, human_size, vline

_COL_CHANGED_FG = QColor('#E2A96B')
_COL_CHANGED_BG = QColor('#2A2218')
_COL_ADDED_FG = QColor('#7EC8A0')
_COL_ADDED_BG = QColor('#345342')
_COL_REMOVED_FG = QColor('#E06C75')
_COL_REMOVED_BG = QColor('#2A1A1C')

###--------------------------------------- Hex Model ----------------------------------------------###


class HexDiffModel(HexGridModelBase):
    """Read-only hex model with diff-based byte coloring"""

    def __init__(self, data: bytes, diff_mask: list[str], parent=None) -> None:
        super().__init__(data, parent)
        self._mask = diff_mask

    def _cell_color(self, pos: int, col: int, role: int) -> QBrush | None:
        if col not in EDITABLE_COLUMNS or pos >= len(self._mask):
            return None
        kind = self._mask[pos]
        pair = {
            'changed': (_COL_CHANGED_FG, _COL_CHANGED_BG),
            'added': (_COL_ADDED_FG, _COL_ADDED_BG),
            'removed': (_COL_REMOVED_FG, _COL_REMOVED_BG),
        }.get(kind)
        if not pair:
            return None
        fg, bg = pair
        return QBrush(fg if role == Qt.ItemDataRole.ForegroundRole else bg)

    ###--------------------------------- Diff Mask -------------------------------------------------###

    @staticmethod
    def build_masks(new_data: bytes, orig_data: bytes) -> tuple[list[str], list[str]]:
        """Produce diffs per byte"""
        n, o = len(new_data), len(orig_data)
        common = min(n, o)
        new_mask = ['same'] * n
        orig_mask = ['same'] * o

        for i in range(common):
            if new_data[i] != orig_data[i]:
                new_mask[i] = 'changed'
                orig_mask[i] = 'changed'

        for i in range(common, n):
            new_mask[i] = 'added'

        for i in range(common, o):
            orig_mask[i] = 'removed'

        return new_mask, orig_mask


def _make_hex_view(model: HexDiffModel) -> QTableView:
    view = HexTableView()
    view.setModel(model)
    view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
    return view


class HexDiffPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._entries: list[tuple[str, bytes, bytes]] = []
        self._setup_ui()
        self.clear()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 0, 0, 0)
        root.setSpacing(0)

        stats_bar = QWidget()
        stats_bar.setObjectName('SurfaceToolbar')
        stats_layout = QHBoxLayout(stats_bar)
        stats_layout.setContentsMargins(4, 4, 4, 4)
        stats_layout.setSpacing(12)

        self._node_label = QLabel('Select a modified file')
        self._node_label.setObjectName('TextHeader')
        self._stats_label = QLabel('')
        self._member_combo = QComboBox()
        self._member_combo.setToolTip('Nodes changed together as one package')
        self._member_combo.currentIndexChanged.connect(self._on_member_changed)

        legend = QHBoxLayout()
        legend.setSpacing(4)
        for colour, text in (
            (_COL_CHANGED_FG, 'Changed'),
            (_COL_ADDED_FG, 'Added'),
            (_COL_REMOVED_FG, 'Removed'),
        ):
            dot = QLabel('■')
            dot.setStyleSheet(f'color: {colour.name()}')
            lbl = QLabel(text)
            legend.addWidget(dot)
            legend.addWidget(lbl)
            legend.addSpacing(6)

        stats_layout.addWidget(self._node_label)
        stats_layout.addWidget(self._member_combo)
        stats_layout.addStretch()
        stats_layout.addWidget(self._stats_label)
        stats_layout.addStretch(12)
        stats_layout.addLayout(legend)
        stats_layout.setContentsMargins(6, 6, 6, 6)
        stats_layout.setVerticalSizeConstraint(QHBoxLayout.SizeConstraint.SetFixedSize)
        root.addWidget(stats_bar)

        # Hex view headers
        headers = QWidget()
        header_layout = QHBoxLayout(headers)
        header_layout.setContentsMargins(8, 8, 8, 8)
        self._new_header = QLabel('New (modified)')
        self._orig_header = QLabel('Original')
        self._new_header.setObjectName('TextHeader')
        self._orig_header.setObjectName('TextHeader')
        header_layout.addWidget(self._new_header, stretch=1)
        header_layout.addWidget(vline())
        header_layout.addWidget(self._orig_header, stretch=1)
        header_layout.setContentsMargins(6, 6, 6, 6)
        header_layout.setVerticalSizeConstraint(QHBoxLayout.SizeConstraint.SetFixedSize)
        root.addWidget(headers)
        root.addWidget(hline())

        ### Hex diff view
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(2)

        self._new_model = HexDiffModel(b'', [])
        self._orig_model = HexDiffModel(b'', [])
        self._new_view = _make_hex_view(self._new_model)
        self._orig_view = _make_hex_view(self._orig_model)

        splitter.addWidget(self._new_view)
        splitter.addWidget(self._orig_view)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter)

        self._new_view.verticalScrollBar().valueChanged.connect(self._orig_view.verticalScrollBar().setValue)
        self._orig_view.verticalScrollBar().valueChanged.connect(self._new_view.verticalScrollBar().setValue)

    ###-------------------------------- Public -------------------------------------###

    def load_group(self, entries: list[tuple[str, bytes, bytes]]) -> None:
        self._entries = list(entries)
        self._member_combo.blockSignals(True)
        self._member_combo.clear()
        for label, _, _ in self._entries:
            self._member_combo.addItem(label)
        self._member_combo.blockSignals(False)
        self._member_combo.setVisible(len(self._entries) > 1)
        if self._entries:
            self._show_entry(0)
        else:
            self.clear()

    def _on_member_changed(self, index: int) -> None:
        if 0 <= index < len(self._entries):
            self._show_entry(index)

    def _show_entry(self, index: int) -> None:
        label, new_data, orig_data = self._entries[index]
        self.load_diff(label, new_data, orig_data)

    def load_diff(self, node_name: str, new_data: bytes, orig_data: bytes) -> None:
        new_mask, orig_mask = HexDiffModel.build_masks(new_data, orig_data)

        changed = sum(1 for m in new_mask if m == 'changed')
        added = sum(1 for m in new_mask if m == 'added')
        removed = sum(1 for m in orig_mask if m == 'removed')

        self._new_model = HexDiffModel(new_data, new_mask)
        self._orig_model = HexDiffModel(orig_data, orig_mask)
        self._new_view.setModel(self._new_model)
        self._orig_view.setModel(self._orig_model)

        self._node_label.setText(node_name)
        self._new_header.setText(f'New ({human_size(len(new_data))})')
        self._orig_header.setText(f'Original ({human_size(len(orig_data))})')

        parts = []
        if changed:
            parts.append(f'{changed} byte(s) changed')
        if added:
            parts.append(f'{added} byte(s) added')
        if removed:
            parts.append(f'{removed} byte(s) removed')
        self._stats_label.setText(', '.join(parts) if parts else 'No differences')

    def clear(self) -> None:
        self._entries = []
        self._member_combo.blockSignals(True)
        self._member_combo.clear()
        self._member_combo.blockSignals(False)
        self._member_combo.setVisible(False)
        self._new_model = HexDiffModel(b'', [])
        self._orig_model = HexDiffModel(b'', [])
        self._new_view.setModel(self._new_model)
        self._orig_view.setModel(self._orig_model)
        self._node_label.setText('Select a modified file to view diff')
        self._new_header.setText('New')
        self._orig_header.setText('Original')
        self._stats_label.setText('')


###------------------------------------- Staging Page --------------------------------------###


class StagingPage(QWidget):
    """UI for managing the filesystem vs Staging Area"""

    request_file_browser = pyqtSignal()

    def __init__(self, dispatcher, rebuild_coordinator, parent=None) -> None:
        super().__init__(parent)
        self.dispatcher          = dispatcher
        self.rebuild_coordinator = rebuild_coordinator
        self.tracker: ModTracker = self.dispatcher.tracker
        self._selected_group_key: str | None = None
        self._option_boxes:       dict[SourceRebuildFlags, QCheckBox] = {}
        self._setup_ui()
        self._connect_signals()
        self._setup_shortcuts()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        v_split = QSplitter(Qt.Orientation.Vertical)
        v_split.setHandleWidth(4)

        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(6, 4, 4, 0)
        top_layout.setSpacing(6)

        lists_row = QHBoxLayout()
        lists_row.setSpacing(6)

        unstage_col = QVBoxLayout()
        unstage_lbl = QLabel('Unstaged Changes')
        unstage_lbl.setObjectName('TextHeader')
        self.unstaged_list = QListWidget()
        self.unstaged_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        unstage_col.addWidget(unstage_lbl)
        unstage_col.addWidget(self.unstaged_list)

        btn_col = QVBoxLayout()
        btn_col.setAlignment(Qt.AlignmentFlag.AlignCenter)
        btn_col.setSpacing(6)

        self.btn_stage = QPushButton('Stage >')
        self.btn_stage_all = QPushButton('Stage All >>')
        self.btn_unstage = QPushButton('< Unstage')
        self.btn_unstage_all = QPushButton('<< Unstage All')
        self.btn_revert = QPushButton('Revert')
        self.btn_revert_all = QPushButton('Revert All')
        for btn in (
            self.btn_stage,
            self.btn_stage_all,
            self.btn_unstage,
            self.btn_unstage_all,
            self.btn_revert,
            self.btn_revert_all,
        ):
            btn.setFixedWidth(140)
        btn_col.addWidget(self.btn_stage)
        btn_col.addWidget(self.btn_stage_all)
        btn_col.addSpacing(12)
        btn_col.addWidget(self.btn_unstage)
        btn_col.addWidget(self.btn_unstage_all)
        btn_col.addStretch()
        btn_col.addWidget(self.btn_revert)
        btn_col.addWidget(self.btn_revert_all)

        staged_col = QVBoxLayout()
        staged_lbl = QLabel('Staged (ready to build)')
        staged_lbl.setObjectName('TextHeader')
        self.staged_list = QListWidget()
        self.staged_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        staged_col.addWidget(staged_lbl)
        staged_col.addWidget(self.staged_list)

        lists_row.addLayout(unstage_col, stretch=1)
        lists_row.addLayout(btn_col)
        lists_row.addLayout(staged_col, stretch=1)
        top_layout.addLayout(lists_row)

        self.conflict_label = QLabel('')
        self.conflict_label.setWordWrap(True)
        self.conflict_label.setStyleSheet(f'color: {_COL_REMOVED_FG.name()}')
        self.conflict_label.setVisible(False)
        top_layout.addWidget(self.conflict_label)

        action_bar = QHBoxLayout()
        action_bar.setContentsMargins(6, 6, 6, 6)
        self.btn_back = QPushButton('< Back')
        self.btn_back.setToolTip(Shortcuts.text(Shortcut.BACK))
        self._patches_layout = QHBoxLayout()
        self._patches_layout.setSpacing(16)
        self.btn_confirm = QPushButton('Build New ISO')
        self.btn_confirm.setObjectName('BtnImportant')
        self.btn_confirm.setEnabled(False)
        action_bar.addWidget(self.btn_back)
        action_bar.addStretch()
        action_bar.addLayout(self._patches_layout)
        action_bar.addSpacing(24)
        action_bar.addWidget(self.btn_confirm)
        top_layout.addLayout(action_bar)

        self.diff_panel = HexDiffPanel()

        v_split.addWidget(top)
        v_split.addWidget(self.diff_panel)
        v_split.setStretchFactor(0, 1)
        v_split.setStretchFactor(1, 4)
        root.addWidget(v_split)

    def _connect_signals(self) -> None:
        self.btn_back.clicked.connect(self.request_file_browser.emit)
        self.btn_stage.clicked.connect(self._on_stage)
        self.btn_stage_all.clicked.connect(self._on_stage_all)
        self.btn_unstage.clicked.connect(self._on_unstage)
        self.btn_unstage_all.clicked.connect(self._on_unstage_all)
        self.btn_revert.clicked.connect(self._on_revert)
        self.btn_revert_all.clicked.connect(self._on_revert_all)

        self.tracker.state_changed.connect(self.refresh_lists)
        self.btn_confirm.clicked.connect(self._on_confirm)
        self.dispatcher.source_loaded.connect(lambda *_: self.refresh_patch_options())
        self.dispatcher.source_closed.connect(self.refresh_patch_options)

        self.unstaged_list.currentItemChanged.connect(self._on_item_changed)
        self.staged_list.currentItemChanged.connect(self._on_item_changed)

    def _on_confirm(self) -> None:
        staged = self.tracker.staged_nodes()
        flags  = self.build_flags()
        if not staged or flags is None:
            return
        self.rebuild_coordinator.request_rebuild(staged, flags)

    ###---------------------------------- Patch Options --------------------------------------###

    def showEvent(self, a0) -> None:
        super().showEvent(a0)
        self.refresh_patch_options()

    def refresh_patch_options(self) -> None:
        options = self.dispatcher.get_patch_options()
        if [option.flag for option in options] == list(self._option_boxes):
            return
        previous = {flag: box.isChecked() for flag, box in self._option_boxes.items()}
        for box in self._option_boxes.values():
            self._patches_layout.removeWidget(box)
            box.deleteLater()
        self._option_boxes.clear()
        for option in options:
            box = QCheckBox(option.name)
            box.setToolTip(option.description)
            box.setChecked(previous.get(option.flag, False))
            self._patches_layout.addWidget(box)
            self._option_boxes[option.flag] = box

    def build_flags(self) -> SourceRebuildFlags | None:
        return self.dispatcher.compose_build_flags(
            flag for flag, box in self._option_boxes.items() if box.isChecked()
        )

    def _setup_shortcuts(self) -> None:
        QShortcut(Shortcuts.sequence(Shortcut.BACK), self).activated.connect(self.request_file_browser.emit)

    def refresh_lists(self) -> None:
        """Modifies the list of modified nodes"""
        self.unstaged_list.clear()
        self.staged_list.clear()
        for group in self.tracker.unstaged_groups():
            item = _make_item(group, self.tracker.group_conflicts(group))
            self.unstaged_list.addItem(item)
            if self._selected_group_key == group.primary.hierarchical_id_str:
                self.unstaged_list.setCurrentItem(item)
        for group in self.tracker.staged_groups():
            item = _make_item(group, [])
            self.staged_list.addItem(item)
            if self._selected_group_key == group.primary.hierarchical_id_str:
                self.staged_list.setCurrentItem(item)
        self.btn_confirm.setEnabled(len(self.tracker.rebuild_queue) > 0)

    def _on_item_changed(self, current: QListWidgetItem, _prev) -> None:
        group: ModGroup | None = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        if group is None:
            self._selected_group_key = None
            self.diff_panel.clear()
            return
        self._selected_group_key = group.primary.hierarchical_id_str
        self.diff_panel.load_group([
            (f'{node}', node.pending_data or b'', self.tracker.get_original(node))
            for node in group.members
        ])

    def _selected_groups(self, widget: QListWidget) -> list[ModGroup]:
        return [item.data(Qt.ItemDataRole.UserRole) for item in widget.selectedItems()]

    def _show_refusals(self, refused: dict[ModGroup, list[ConflictInfo]]) -> None:
        if not refused:
            self.conflict_label.setVisible(False)
            return
        lines = []
        for group, conflicts in refused.items():
            lines.append(f'Not staged: {group.name}')
            lines.extend(f'    {c.reason}' for c in conflicts)
        lines.append('Revert one side of the conflict to continue.')
        self.conflict_label.setText('\n'.join(lines))
        self.conflict_label.setVisible(True)

    def _on_stage(self) -> None:
        refused = {}
        for group in self._selected_groups(self.unstaged_list):
            conflicts = self.tracker.stage_group(group)
            if conflicts:
                refused[group] = conflicts
        self._show_refusals(refused)

    def _on_stage_all(self) -> None:
        self._show_refusals(self.tracker.stage_all())

    def _on_unstage(self) -> None:
        for group in self._selected_groups(self.staged_list):
            self.tracker.unstage_group(group)

    def _on_unstage_all(self) -> None:
        self.tracker.unstage_all()

    def _on_revert(self) -> None:
        for group in self._selected_groups(self.unstaged_list) + self._selected_groups(self.staged_list):
            self.tracker.revert_group(group)
        self.conflict_label.setVisible(False)

    def _on_revert_all(self) -> None:
        self.tracker.revert_all()
        self._selected_group_key = None
        self.diff_panel.clear()
        self.conflict_label.setVisible(False)

def _make_item(group: ModGroup, conflicts: list[ConflictInfo]) -> QListWidgetItem:
    warn = bool(conflicts)
    item = QListWidgetItem(f'\u26a0 {group.name}' if warn else group.name)
    item.setData(Qt.ItemDataRole.UserRole, group)
    lines = [f'{node} ({human_size(node.size)})' for node in group.members]
    if warn:
        item.setForeground(QBrush(_COL_REMOVED_FG))
        lines.append('')
        lines.extend(conflict.reason for conflict in conflicts)
    item.setToolTip('\n'.join(lines))
    return item
