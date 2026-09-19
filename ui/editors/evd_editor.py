'''
EVD script editor.

The editable state is one string of EVDCODE, the block-structured source form
`evd_tool` decompiles to and compiles from. Two tabs edit that same string:

    Structure   one row per EVDCODE line, with the palette, drag-to-reorder,
                drag-to-insert and a parameter inspector for the selected line
    Code        the EVDCODE text itself, syntax-highlighted, for bulk edits

Because a line of EVDCODE is a line of text and also a command, the two views
are the same list seen twice and cannot drift. Every mutation re-validates by
compiling, so a change that would not assemble is refused at the point it is
made rather than at save time.
'''
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from PyQt6.QtCore import (
    Qt, QAbstractListModel, QModelIndex, QMimeData, QPoint, QRect, QSize,
    QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QColor, QDrag, QFont, QFontMetrics, QPainter, QSyntaxHighlighter,
    QTextCharFormat, QTextCursor, QTextDocument,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QCompleter, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem,
    QMenu, QPlainTextEdit, QPushButton, QSplitter, QStackedLayout, QStyle,
    QStyledItemDelegate, QStyleOptionViewItem, QTabWidget, QTableWidget,
    QTableWidgetItem, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget, QInputDialog
)

from core.contracts import BaseEditor
from core.node import VfsNode
from core.registry import Registry
from core.handlers import evd_leaf
from core.handlers.evd_leaf import Arg, CodeLine, EvdCompileError, unique_label
from core.handlers.evd_leaf import EVDHandler, EvdEditorPayload, EvdSavePayload, EvdError
from utilities import hline

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###------------------------------------------- Presentation -------------------------------------------###

_CATEGORY_COLORS = {
    evd_leaf.CATEGORY_JUMP:         '#D06060',
    evd_leaf.CATEGORY_SCRIPT_START: '#4CAF50',
    evd_leaf.CATEGORY_MARKER_SEEK:  '#AB6BD6',
    evd_leaf.CATEGORY_EXPRESSION:   '#4A90D9',
    evd_leaf.CATEGORY_HIGH:         '#D06BD0',
    evd_leaf.CATEGORY_TERMINAL:     '#D6A02A',
    evd_leaf.CATEGORY_MACRO:        '#26A69A',
    evd_leaf.CATEGORY_STRUCTURE:    '#8A8A8A',
    evd_leaf.CATEGORY_NORMAL:       '#C8C8C8',
}
_LEGEND = (
    (evd_leaf.CATEGORY_JUMP, 'Branch'),
    (evd_leaf.CATEGORY_SCRIPT_START, 'Script start'),
    (evd_leaf.CATEGORY_MARKER_SEEK, 'Marker seek'),
    (evd_leaf.CATEGORY_EXPRESSION, 'Value'),
    (evd_leaf.CATEGORY_HIGH, 'Marker'),
    (evd_leaf.CATEGORY_MACRO, 'Macro'),
)
_COLOR_GUTTER      = '#6E6E6E'
_COLOR_LINE_NUMBER = '#5A5A5A'
_COLOR_ADDRESS     = '#8FA876'
_COLOR_ARG_KEY = '#9AA7B8'
_COLOR_VALUE   = '#C8C8C8'
_COLOR_STRING  = '#C9946A'
_COLOR_NUMBER  = '#B5CEA8'
_COLOR_COMMENT = '#6A8759'
_COLOR_ERROR   = '#D45050'
_COLOR_MATCH   = '#C8A24A'   # occurrence highlight, as an IDE marks the symbol under the caret
_COLOR_OK      = '#4CAF50'

_MIME_MOVE_LINE = 'application/x-evd-line-number'
_MIME_NEW_COMMAND = 'application/x-evd-command-name'

_VALIDATE_DEBOUNCE_MS = 400
_MAX_UNDO = 60

_RE_HL_KEY = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*=')
_RE_HL_NUMBER = re.compile(r'\b(?:0x[0-9A-Fa-f]+|-?\d+(?:\.\d+)?)\b')
_RE_HL_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_STRUCTURE_HEADS = frozenset({'label', 'entry', 'header', 'headerExtra', 'header_extra', 'marker_table'})

_TEMPLATE_MEANING_NOTE = (
    '\n\n(Generated from the parameter name, not traced from the handler -- '
    'treat it as a naming convention rather than a finding.)'
)

_VALUE_COLUMN_NARROW = 120
_VALUE_COLUMN_WIDTH = 240
_VALUE_COLUMN_MAX = 420


def _category_color(category: str) -> str:
    return _CATEGORY_COLORS.get(category, _CATEGORY_COLORS[evd_leaf.CATEGORY_NORMAL])

###------------------------------------------- Editor state -------------------------------------------###

@dataclass
class _Snapshot:
    '''One undoable state. Text is the whole script, which keeps undo honest
    for edits that span lines (block moves, deletes) without a diff model.'''
    code:     str
    selected: int


###------------------------------------------------ Editor -------------------------------------------------###

@Registry.register_editor(
    name='EVD Script Editor',
    handler=EVDHandler,
    extensions=('.evd',),
    categories=(),
    is_fallback=False,
)
class EvdEditor(BaseEditor):
    '''EVDCODE editor with a structure view and a text view over one script.'''

    def __init__(self, parent: QWidget | None = None, data_resolver=None) -> None:
        super().__init__(parent)
        self._data_resolver = data_resolver
        self._code:     str = ''
        self._lines:    list[CodeLine] = []
        self._raw:      bytes = b''
        self._stats:    evd_leaf.ScriptStats | None = None
        self._source:   str = ''
        self._offsets:  dict[int, int] = {}
        self._error:    EvdCompileError | None = None
        self._generation = 0
        self._undo_stack: list[_Snapshot] = []
        self._redo_stack: list[_Snapshot] = []
        self._suppress_text_signal = False
        self._folded:   set[int] = set()  # collapsed block openers, shared by both views
        self._highlight: str = ''         # value key lit up in both views
        self._typing = False           # a run of keystrokes shares one undo entry
        self._structure_stale = False  # the row list is behind the text being typed
        self._setup_ui()

    ###------------------------------------------ Construction ------------------------------------------###

    def _setup_ui(self) -> None:
        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)

        self._placeholder = QLabel()
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)

        editor_widget = QWidget()
        editor_layout = QVBoxLayout(editor_widget)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(0)
        editor_layout.addWidget(self._build_toolbar())

        self._structure_view = StructureView()
        self._structure_view.lineMoved.connect(self._on_line_moved)
        self._structure_view.commandDropped.connect(self._on_command_dropped)
        self._structure_view.deleteRequested.connect(self._on_delete_requested)
        self._structure_view.currentLineChanged.connect(self._on_line_selected)
        self._structure_view.toggleYieldRequested.connect(self._on_toggle_yield)
        self._structure_view.gotoLabelRequested.connect(self.goto_label)
        self._structure_view.addLabelRequested.connect(self._on_add_label_requested)
        self._structure_view.selectJumpTargetRequested.connect(self._on_select_jump_target_requested)
        self._structure_view.foldToggled.connect(self.toggle_fold)
        self._structure_view.valuePicked.connect(self.highlight_value)
        self._structure_view.doubleClicked.connect(self._on_row_double_clicked)

        self._code_view = CodeView()
        self._code_view.textEdited.connect(self._on_code_text_edited)
        self._code_view.commandDropped.connect(self._on_command_dropped_text)
        self._code_view.cursorLineChanged.connect(self._on_code_cursor_moved)
        self._code_view.foldToggled.connect(self.toggle_fold)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._structure_view, 'Structure')
        self._tabs.addTab(self._code_view, 'Code')
        self._tabs.currentChanged.connect(self._on_tab_changed)

        self._palette = CommandPalette()
        self._palette.commandActivated.connect(self._on_palette_activated)

        top_splitter = QSplitter(Qt.Orientation.Horizontal)
        top_splitter.addWidget(self._tabs)
        top_splitter.addWidget(self._palette)
        top_splitter.setStretchFactor(0, 4)
        top_splitter.setStretchFactor(1, 1)

        self._inspector = ParameterInspector()
        self._inspector.applyRequested.connect(self._on_inspector_apply)
        self._problems = ProblemsPanel()
        self._problems.lineActivated.connect(self.goto_line)
        self._lowered = LoweredSourcePanel()

        self._bottom_tabs = QTabWidget()
        self._bottom_tabs.addTab(self._inspector, 'Parameters')
        self._bottom_tabs.addTab(self._problems, 'Problems')
        self._bottom_tabs.addTab(self._lowered, 'Lowered EVDSRC')

        vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        vertical_splitter.addWidget(top_splitter)
        vertical_splitter.addWidget(self._bottom_tabs)
        vertical_splitter.setStretchFactor(0, 3)
        vertical_splitter.setStretchFactor(1, 1)

        editor_layout.addWidget(vertical_splitter)
        editor_layout.addWidget(hline())
        editor_layout.addWidget(self._build_status_bar())

        self._stack.addWidget(self._placeholder)
        self._stack.addWidget(editor_widget)

        self._validate_timer = QTimer(self)
        self._validate_timer.setSingleShot(True)
        self._validate_timer.setInterval(_VALIDATE_DEBOUNCE_MS)
        self._validate_timer.timeout.connect(self._validate_now)

    def _on_add_label_requested(self, target_line: int) -> None:
        name, ok = QInputDialog.getText(self, "Add Label", "New label name:")
        if ok and name:
            indent = self._insert_indent(target_line)
            text = f"{' ' * indent}label ({name})"

            # Inject the new label line and re-apply
            merged = self._lines[:target_line - 1] + evd_leaf.parse_code(text) + self._lines[target_line - 1:]
            code = evd_leaf.render_code(merged)
            self._apply_code(code, f"Add label '{name}'", select_line=target_line)

    def _on_select_jump_target_requested(self, target_line: int) -> None:
        # Gather all currently valid labels in the file
        labels = [name for name, _ in evd_leaf.iter_labels(self._lines)]
        if not labels:
            return

        line = self._line(target_line)
        if line is None:
            return

        # Pre-select the current target if it exists in the list
        current_idx = labels.index(line.target_label) if line.target_label in labels else 0

        # Open blocking dropdown selection menu
        selected_label, ok = QInputDialog.getItem(
            self,
            "Select Jump Target",
            "Target:",
            labels,
            current_idx,
            False # False prevents the user from typing a custom string not in the list
        )

        if ok and selected_label and selected_label != line.target_label:
            for i, arg in enumerate(line.args):
                if not arg.key:
                    continue
                if arg.key in ('goto','target'):
                    args = line.args[:i] + (Arg(key=arg.key, value=selected_label),) + line.args[i+1:]
                    break

            self._apply_line_args(target_line, args, 'goto')
            # Re-render the full code with the modified line and apply the change
            code = evd_leaf.render_code(self._lines)
            self._apply_code(code, f"Update jump target to '{selected_label}'", select_line=target_line)

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName('SurfaceToolbar')
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(8)

        self._info_label = QLabel('No script loaded')
        lay.addWidget(self._info_label)
        lay.addStretch(1)

        self._fold_button = QPushButton('Collapse all')
        self._fold_button.setObjectName('BtnSurface')
        self._fold_button.setToolTip(
            'Collapse every block. Individual blocks fold from the arrow in the gutter, '
            'or with the left and right arrow keys, in either tab.'
        )
        self._fold_button.clicked.connect(self.toggle_fold_all)
        lay.addWidget(self._fold_button)

        self._normalize_button = QPushButton('Normalize')
        self._normalize_button.setObjectName('BtnSurface')
        self._normalize_button.setToolTip(
            'Compile the script and decompile the result. Rewrites derived parameters, '
            're-sugars if/else blocks and refreshes the name comments. The bytes do not change.'
        )
        self._normalize_button.clicked.connect(self.normalize)
        lay.addWidget(self._normalize_button)

        lay.addWidget(_LegendWidget())
        return bar

    def _build_status_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName('SurfaceToolbar')
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(6, 2, 6, 2)
        self._status_label = QLabel('Ready')
        self._status_label.setWordWrap(True)
        lay.addWidget(self._status_label, stretch=1)
        return bar

    ###--------------------------------------- Loading lifecycle ---------------------------------------###

    def begin_loading(self, node: VfsNode) -> None:
        super().begin_loading(node)
        self._reset_state()
        self._placeholder.setText(f'Loading {node.name}...')
        self._stack.setCurrentIndex(0)

    def receive_data(self, result: EvdEditorPayload, data_resolver=None) -> None:
        self._data_resolver = data_resolver
        if not isinstance(result, EvdEditorPayload):
            self.show_error(f'Unexpected result type: {type(result).__name__}, expected EvdEditorPayload')
            return
        logger.debug(result)
        self._original_payload = result
        self._raw    = result.raw
        self._stats  = result.stats
        self._source = result.source
        self.set_dirty(False)
        self._populate_ui(result)
        self._stack.setCurrentIndex(1)
        if result.warning:
            self._set_status(result.warning, error=True)
        else:
            self._set_status(f'Loaded {result.name} -- {result.stats.summary()}', error=False)
        logger.info(f'[gen {self._generation}] loaded {result.name}: {result.stats.summary()}')

    def _populate_ui(self, data: EvdEditorPayload | EvdSavePayload) -> None:
        self._code = data.code
        if isinstance(data, EvdSavePayload):
            self._lines = evd_leaf.parse_code(data.code)
            self._offsets = {}
            self._refresh_views(preserve_selection=False)
            self._validate_timer.start()
        else:
            self._lines = list(data.lines)
            self._offsets = dict(data.offsets)
            self._refresh_views(preserve_selection=False)
            self._lowered.set_text(data.source)
            self._problems.clear()
        self._emit_undo_state()

    def show_error(self, message: str) -> None:
        '''Load failures land here from EditorPage; edit failures go to the status bar.'''
        logger.error(f'[gen {self._generation}] {self.__class__.__name__}: {message}')
        if not self._code:
            self._placeholder.setText(f'Could not open this script:\n\n{message}')
            self._stack.setCurrentIndex(0)
            return
        self._set_status(message, error=True)

    def _set_status(self, message: str, error: bool) -> None:
        self._status_label.setStyleSheet(f'color: {_COLOR_ERROR if error else _COLOR_OK};')
        self._status_label.setText(message)

    def _reset_state(self) -> None:
        self._code   = ''
        self._lines  = []
        self._raw    = b''
        self._stats  = None
        self._source = ''
        self._offsets = {}
        self._error  = None
        self._generation = 0
        self._undo_stack = []
        self._redo_stack = []
        self._typing = False
        self._structure_stale = False
        self._folded = set()
        self._highlight = ''
        self._structure_view.set_lines([], {}, set())
        self._code_view.set_text('')
        self._inspector.clear()
        self._problems.clear()
        self._lowered.set_text('')
        self._info_label.setText('No script loaded')
        self._emit_undo_state()

    def cleanup(self) -> None:
        self._validate_timer.stop()
        super().cleanup()

    ###------------------------------------------ Save lifecycle ------------------------------------------###

    def current_data(self) -> EvdSavePayload:
        '''Live EVDCODE, compiled by EVDHandler.decode_editor_data on save.'''
        if not self._code:
            raise EvdError('No script loaded')
        return EvdSavePayload(self._code)

    def discard_changes(self) -> None:
        if self.is_dirty() and self.current_node and self._original_payload is not None:
            self._pending_data = None
            self._undo_stack.clear()
            self._redo_stack.clear()
            self._populate_ui(self._original_payload)
            self.set_dirty(False)
            self._set_status('Changes discarded', error=False)

    ###--------------------------------------- State transitions ----------------------------------------###

    def _apply_code(self, code: str, action_desc: str, *, validate: bool = True,
                    select_line: int | None = None) -> bool:
        '''Commit `code` as the new script.

        A rejected compile is a rejected edit: the previous state stays, so the
        editor never holds a script that could not be saved. Text typed in the
        code view is the one exception -- it is committed unvalidated so the
        user can type through an intermediate state, and reported in Problems.
        '''
        if code == self._code:
            return True
        t0 = time.monotonic()
        if validate:
            built = evd_leaf.assemble(code)
            if not built.ok:
                elapsed = (time.monotonic() - t0) * 1000
                logger.warning(f'[gen {self._generation}] {action_desc} REJECTED ({elapsed:.1f}ms): {built.error}')
                self._set_status(f'{action_desc} rejected: {built.error}', error=True)
                self._problems.set_errors([built.error])
                self._bottom_tabs.setCurrentWidget(self._problems)
                return False
            self._offsets = built.offsets

        self._push_undo()
        self._generation += 1
        self._code = code
        self._lines = evd_leaf.parse_code(code)
        self._refresh_views(preserve_selection=True, select_line=select_line)
        self.set_dirty(True)
        self._emit_undo_state()
        if validate:
            self._error = None
            elapsed = (time.monotonic() - t0) * 1000
            logger.info(f'[gen {self._generation}] {action_desc} applied ({elapsed:.1f}ms), {len(self._lines)} lines')
            self._report_label_problems(f'{action_desc} applied')
        else:
            self._validate_timer.start()
        return True

    def _report_label_problems(self, success_message: str) -> None:
        '''Report stranded jump targets after an edit that compiled.

        Deleting or moving a label is the one edit that assembles cleanly and
        still changes where a branch goes, so it has to be said out loud. The
        check needs no compile, which is why it runs on every applied edit.
        '''
        stranded = evd_leaf.undefined_label_problems(self._lines)
        self._problems.set_errors(list(stranded))
        self._code_view.mark_error_line(stranded[0].line if stranded else None)
        if stranded:
            self._set_status(
                f'{success_message}, but {len(stranded)} jump target(s) now name a label this '
                f'script does not define -- see Problems', error=True)
            logger.warning(f'[gen {self._generation}] stranded targets: {[str(s) for s in stranded]}')
        else:
            self._set_status(success_message, error=False)

    def _push_undo(self) -> None:
        self._typing = False  # any snapshot ends the current run of keystrokes
        self._undo_stack.append(_Snapshot(self._code, self._structure_view.current_line()))
        if len(self._undo_stack) > _MAX_UNDO:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def _restore(self, snapshot: _Snapshot, action_desc: str) -> None:
        self._generation += 1
        self._code = snapshot.code
        self._lines = evd_leaf.parse_code(snapshot.code)
        self._refresh_views(preserve_selection=False, select_line=snapshot.selected)
        self.set_dirty(bool(self._undo_stack))
        self._emit_undo_state()
        # The label check is free, so it refreshes now; the compile result
        # follows from the debounced validator a moment later.
        self._report_label_problems(f'{action_desc} ({len(self._lines)} lines)')
        self._validate_timer.start()

    def undo(self) -> None:
        if not self._undo_stack:
            return
        self._redo_stack.append(_Snapshot(self._code, self._structure_view.current_line()))
        self._restore(self._undo_stack.pop(), 'Undo')

    def redo(self) -> None:
        if not self._redo_stack:
            return
        self._undo_stack.append(_Snapshot(self._code, self._structure_view.current_line()))
        self._restore(self._redo_stack.pop(), 'Redo')

    def _emit_undo_state(self) -> None:
        self.undo_state_changed.emit(bool(self._undo_stack), bool(self._redo_stack))

    def _refresh_views(self, *, preserve_selection: bool, select_line: int | None = None) -> None:
        target = select_line if select_line is not None else (
            self._structure_view.current_line() if preserve_selection else None
        )
        self._prune_folds()
        self._structure_view.set_lines(self._lines, self._offsets, self._folded)
        self._code_view.set_folds(evd_leaf.foldable(self._lines), self._folded)
        self._structure_view.set_highlight(self._highlight)
        self._code_view.set_highlight(self._highlight, self._lines)
        self._structure_stale = False
        self._suppress_text_signal = True
        try:
            self._code_view.set_text(self._code)
        finally:
            self._suppress_text_signal = False
        if target:
            self._structure_view.select_line(target)
            self._code_view.goto_line(target)
        self._update_info_label()
        self._on_line_selected(self._structure_view.current_line())

    def _update_info_label(self) -> None:
        commands = sum(1 for line in self._lines if line.kind == evd_leaf.KIND_COMMAND)
        blocks = sum(1 for line in self._lines if line.opens and line.kind == evd_leaf.KIND_COMMAND)
        labels = sum(1 for line in self._lines if line.kind == evd_leaf.KIND_LABEL)
        text = f'{len(self._lines)} lines, {commands} commands, {blocks} blocks, {labels} labels'
        if self._stats:
            text += f' -- {self._stats.byte_size} bytes on load'
        self._info_label.setText(text)

    ###------------------------------------------- Validation -------------------------------------------###

    def _validate_now(self) -> None:
        if not self._code:
            return
        self._typing = False  # the next keystroke starts a fresh undo step
        self._sync_from_text()
        built = evd_leaf.assemble(self._code)
        error = built.error
        self._error = error
        if built.offsets:
            self._offsets = built.offsets
            self._structure_view.set_offsets(self._offsets)
        # Stranded branches assemble, so the compiler says nothing about them.
        # They are reported alongside its errors because a jump into the middle
        # of an unrelated command is worse than a script that will not build.
        stranded = evd_leaf.undefined_label_problems(self._lines)
        self._problems.set_errors(([error] if error else []) + stranded)
        self._code_view.mark_error_line(error.line if error else
                                        (stranded[0].line if stranded else None))
        if error is not None:
            self._set_status(str(error), error=True)
        elif stranded:
            self._set_status(
                f'Script compiles, but {len(stranded)} jump target(s) name a label this script '
                f'does not define -- see Problems', error=True)
        else:
            self._set_status('Script compiles', error=False)

    def normalize(self) -> None:
        '''Round-trip the script through the compiler to canonicalise it.'''
        self._sync_from_text()
        if not self._code:
            return
        try:
            data = evd_leaf.compile_code(self._code)
            code = evd_leaf.decompile_code(data, self.current_node.name if self.current_node else 'Main')
        except EvdCompileError as e:
            self._set_status(f'Cannot normalize: {e}', error=True)
            self._problems.set_errors([e])
            self._bottom_tabs.setCurrentWidget(self._problems)
            return
        if code == self._code:
            self._set_status('Already normalized', error=False)
            return
        self._apply_code(code, 'Normalize')
        self._lowered.set_text(evd_leaf.decompile_source(data))

    ###--------------------------------------------- Folding --------------------------------------------###

    ###------------------------------------- Value occurrences ------------------------------------###

    def highlight_value(self, key: str) -> None:
        """Light up every occurrence of one value, the way an IDE marks a symbol.

        Scoped to argument values, so tracing event value 1009 never lights up a
        label or a parameter name that happens to read the same. Numbers compare
        numerically: 1009 and 0x3F1 are one value, because which spelling a line
        uses is an accident of how it decompiled.
        """
        if key == self._highlight:
            return
        self._highlight = key
        self._structure_view.set_highlight(key)
        self._code_view.set_highlight(key, self._lines)
        if not key:
            return
        count = sum(len(evd_leaf.value_occurrences(line, key)) for line in self._lines)
        shown = key[1:] if key.startswith('#') else key[1:]
        self._set_status(f'{count} occurrence(s) of {shown}', error=False)

    def toggle_fold(self, opener: int) -> None:
        '''Collapse or expand one block, in both views at once.'''
        if opener in self._folded:
            self._folded.discard(opener)
        else:
            self._folded.add(opener)
        self._apply_folds()

    def toggle_fold_all(self) -> None:
        spans = evd_leaf.foldable(self._lines)
        # The event block wraps the whole script; collapsing it alone would hide
        # everything and read as a bug, so "collapse all" means every block
        # inside it.
        inner = {opener for opener in spans if self._lines[opener - 1].kind != evd_leaf.KIND_EVENT}
        self._folded = set() if self._folded else inner
        self._apply_folds()

    def _apply_folds(self) -> None:
        self._structure_view.set_folded(self._folded)
        self._code_view.set_folds(evd_leaf.foldable(self._lines), self._folded)
        self._fold_button.setText('Expand all' if self._folded else 'Collapse all')

    def _prune_folds(self) -> None:
        '''Drop folds that no longer sit on a block after an edit.

        Line numbers move when lines are added or removed, so a remembered fold
        can end up pointing at something that is not a block any more. Keeping
        only the ones that still are means a fold is at worst on a real block,
        never on nothing.
        '''
        spans = evd_leaf.foldable(self._lines)
        self._folded &= set(spans)

    def goto_line(self, number: int) -> None:
        # A line inside a collapsed block cannot be shown; open it first, which
        # is what any editor does when a search lands inside a fold.
        for opener, end in evd_leaf.foldable(self._lines).items():
            if opener in self._folded and opener < number <= end:
                self._folded.discard(opener)
        self._apply_folds()
        self._structure_view.select_line(number)
        self._code_view.goto_line(number)

    def goto_label(self, name: str) -> None:
        '''Jump to the label a branch names, or say why it cannot.'''
        self._sync_from_text()
        target = next((number for label, number in evd_leaf.iter_labels(self._lines) if label == name), None)
        if target is None:
            self._set_status(
                f'{name} is not a label in this script; it will assemble as a raw byte offset',
                error=True)
            return
        self.goto_line(target)
        address = self._offsets.get(target)
        where = f' at 0x{address:04X}' if address is not None else ''
        self._set_status(f'{name} is line {target}{where}', error=False)

    ###---------------------------------------- Structure editing ----------------------------------------###

    def _on_line_selected(self, number: int | None) -> None:
        line = self._line(number)
        self._inspector.show_line(line, self._offsets.get(number) if number else None)

    def _on_row_double_clicked(self, index) -> None:
        '''Double-clicking a branch follows it, the way a debugger would.'''
        line = self._line(index.row() + 1 if index.isValid() else None)
        if line is not None and line.target_label:
            self.goto_label(line.target_label)

    def _line(self, number: int | None) -> CodeLine | None:
        self._sync_from_text()
        if number is None or not (1 <= number <= len(self._lines)):
            return None
        return self._lines[number - 1]

    def _on_line_moved(self, moved_line: int, target_line: int) -> None:
        '''Move a line, or the whole block when the line opens one.'''
        self._sync_from_text()
        start, end = evd_leaf.block_range(self._lines, moved_line)
        if start <= target_line <= end + 1:
            return  # dropped inside itself
        block = self._lines[start - 1:end]
        rest = self._lines[:start - 1] + self._lines[end:]
        # target_line indexes the original list; shift it past the removed block
        insert_at = target_line - 1 - (end - start + 1) if target_line > end else target_line - 1
        insert_at = max(0, min(insert_at, len(rest)))
        merged = rest[:insert_at] + block + rest[insert_at:]
        self._apply_code(evd_leaf.render_code(merged), 'Move', select_line=insert_at + 1)

    def _on_delete_requested(self, number: int) -> None:
        self._sync_from_text()
        line = self._line(number)
        if line is None:
            return
        start, end = evd_leaf.block_range(self._lines, number)
        remaining = self._lines[:start - 1] + self._lines[end:]
        count = end - start + 1
        desc = f'Delete {count} lines' if count > 1 else 'Delete'
        self._apply_code(evd_leaf.render_code(remaining), desc, select_line=max(1, start - 1))

    def _on_toggle_yield(self, number: int) -> None:
        '''Flip the one-frame yield on a command.

        `yield=1` is the printed form of the header's keep-going bit being
        clear, so removing it and adding it are the two halves of the same edit.
        '''
        line = self._line(number)
        if line is None or line.kind != evd_leaf.KIND_COMMAND:
            return
        if line.arg('yield') is not None:
            args = tuple(a for a in line.args if a.key != 'yield')
            desc = 'Clear yield'
        else:
            args = line.args + (Arg('yield', '1'),)
            desc = 'Set yield'
        self._apply_line_args(number, args, desc)

    def _on_command_dropped(self, name: str, target_line: int) -> None:
        self._insert_command(name, target_line)

    def _on_command_dropped_text(self, name: str, target_line: int) -> None:
        self._insert_command(name, target_line)
        self._tabs.setCurrentWidget(self._code_view)

    def _on_palette_activated(self, name: str) -> None:
        current = self._structure_view.current_line()
        self._insert_command(name, (current + 1) if current else self._default_insert_line())

    def _default_insert_line(self) -> int:
        '''Just before the closing brace of the event, which is always last.'''
        return max(1, len(self._lines))

    def _insert_command(self, name: str, target_line: int) -> None:
        '''Insert a template for `name` at `target_line`.

        Templates are built to assemble as inserted, so a new command lands as a
        working line rather than a zero payload to reverse engineer. Two of them
        (`option`, `raw`) only mean something once the author supplies the
        missing part; those go in unvalidated so the stub is there to edit, and
        Problems says what it is still missing.
        '''
        self._sync_from_text()
        if not self._lines:
            return
        target_line = max(2, min(target_line, len(self._lines)))
        indent = self._insert_indent(target_line)
        text = evd_leaf.command_template(name, indent, evd_leaf.unique_label(self._lines))
        if not text:
            self._set_status(f'No insertable form for {name}', error=True)
            return
        merged = self._lines[:target_line - 1] + evd_leaf.parse_code(text) + self._lines[target_line - 1:]
        code = evd_leaf.render_code(merged)
        error = evd_leaf.validate_code(code)
        if error is None:
            if self._apply_code(code, f'Insert {name}', select_line=target_line):
                self._bottom_tabs.setCurrentWidget(self._inspector)
            return
        self._apply_code(code, f'Insert {name}', validate=False, select_line=target_line)
        self._problems.set_errors([error])
        self._bottom_tabs.setCurrentWidget(self._problems)
        self._set_status(f'Inserted {name} as a stub -- {error}', error=True)

    def _insert_indent(self, target_line: int) -> int:
        '''Indent of the line the insert lands before, so blocks stay aligned.'''
        line = self._line(target_line)
        if line is None:
            return 4
        if line.kind == evd_leaf.KIND_CLOSE:
            previous = self._line(target_line - 1)
            return previous.indent if previous and not previous.closes else line.indent + 4
        return line.indent

    ###--------------------------------------- Parameter editing ----------------------------------------###

    def _on_inspector_apply(self, number: int, args: object) -> None:
        self._apply_line_args(number, tuple(args), 'Edit parameters')  # type: ignore[arg-type]

    def _apply_line_args(self, number: int, args: tuple[Arg, ...], action_desc: str) -> None:
        self._sync_from_text()
        result = evd_leaf.apply_line_edit(self._lines, number, args)
        if not result.ok:
            error = result.error
            logger.warning(f'[gen {self._generation}] {action_desc} REJECTED: {result.error_text}')
            self._set_status(f'{action_desc} rejected: {result.error_text}', error=True)
            # Not every rejection carries a line of its own, so fall back to the
            # line being edited rather than leaving the user without one.
            located = EvdCompileError(error.message, error.line or number) if error else None
            self._problems.set_errors([located])
            self._bottom_tabs.setCurrentWidget(self._problems)
            self.goto_line(number)
            return
        self._push_undo()
        self._generation += 1
        self._code = result.text
        self._offsets = result.offsets
        self._lines = evd_leaf.parse_code(result.text)
        self._refresh_views(preserve_selection=False, select_line=number)
        self.set_dirty(True)
        self._error = None
        self._emit_undo_state()
        note = ''
        if result.dropped:
            note = (f' -- recomputed {", ".join(result.dropped)}, which is derived '
                    f'from what you changed')
        self._report_label_problems(f'{action_desc} applied{note}')
        logger.info(f'[gen {self._generation}] {action_desc} line {number}: {result.changed_line.strip()}')

    ###------------------------------------------ Code view ---------------------------------------------###

    def _on_code_text_edited(self, text: str) -> None:
        if self._suppress_text_signal or text == self._code:
            return
        # Typing goes through unvalidated so an intermediate state is not fought
        # with; the debounced validator reports it in Problems a moment later.
        # A run of keystrokes is one undo step, not one per character, or a
        # sentence of typing would flush the structural history behind it.
        if not self._typing:
            self._push_undo()
            self._typing = True
            self._emit_undo_state()
        self._generation += 1
        self._code = text
        # Reparsing and rebuilding the row list costs ~40ms on a long script,
        # which is a keystroke's whole budget, and the tab that list lives on is
        # not even on screen while typing. Both wait for the debounce; the text
        # itself is current, so a save in between is still correct.
        self._structure_stale = True
        self.set_dirty(True)
        self._validate_timer.start()

    def _sync_from_text(self) -> None:
        '''Catch the line model up with text typed in the code view.

        Called before anything that reads `self._lines` and by the debounced
        validator. A no-op unless the code view has moved ahead.
        '''
        if not self._structure_stale:
            return
        self._structure_stale = False
        self._lines = evd_leaf.parse_code(self._code)
        self._prune_folds()
        current = self._code_view.current_line()
        self._structure_view.set_lines(self._lines, self._offsets, self._folded)
        self._code_view.set_folds(evd_leaf.foldable(self._lines), self._folded)
        self._structure_view.select_line(current)
        self._update_info_label()

    def _on_code_cursor_moved(self, number: int) -> None:
        self.highlight_value(self._code_view.value_at_cursor(self._lines))
        if self._structure_stale:
            # Typing moves the cursor on every keystroke. Following it here
            # would reparse the file each time, which is exactly the work the
            # debounce exists to avoid; it reselects when it catches up.
            return
        self._structure_view.select_line(number, scroll=False)
        self._on_line_selected(number)

    def _on_tab_changed(self, index: int) -> None:
        if self._tabs.widget(index) is self._code_view:
            current = self._structure_view.current_line()
            if current:
                self._code_view.goto_line(current)
        else:
            self._sync_from_text()
            self._structure_view.select_line(self._code_view.current_line())


###--------------------------------------------- Structure view ---------------------------------------------###

class LineModel(QAbstractListModel):
    '''One row per EVDCODE line.

    A model with a painting delegate rather than a widget per row: a script is
    routinely 700 lines and the previous per-row-widget approach stalled the UI
    on every edit.
    '''

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._lines: list[CodeLine] = []
        self._offsets: dict[int, int] = {}
        self._folded: set[int] = set()
        self._highlight: str = ''
        self._spans: dict[int, int] = {}
        self._visible: list[CodeLine] = []
        self._row_of: dict[int, int] = {}

    def set_lines(self, lines: list[CodeLine], offsets: dict[int, int] | None = None,
                  folded: set[int] | None = None) -> None:
        self.beginResetModel()
        self._lines = list(lines)
        if offsets is not None:
            self._offsets = offsets
        if folded is not None:
            self._folded = set(folded)
        self._rebuild()
        self.endResetModel()

    def set_folded(self, folded: set[int]) -> None:
        self.beginResetModel()
        self._folded = set(folded)
        self._rebuild()
        self.endResetModel()

    def _rebuild(self) -> None:
        self._spans = evd_leaf.foldable(self._lines)
        hidden = evd_leaf.hidden_lines(self._lines, self._folded)
        self._visible = [line for line in self._lines if line.number not in hidden]
        self._row_of = {line.number: row for row, line in enumerate(self._visible)}

    def set_offsets(self, offsets: dict[int, int]) -> None:
        self._offsets = offsets
        if self._visible:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._visible) - 1, 0))

    def offset_of(self, number: int) -> int | None:
        return self._offsets.get(number)

    def line_at(self, row: int) -> CodeLine | None:
        return self._visible[row] if 0 <= row < len(self._visible) else None

    def line_number_at(self, row: int) -> int | None:
        line = self.line_at(row)
        return line.number if line else None

    def row_of(self, number: int) -> int | None:
        '''The row a line is on, or None when a collapsed block is hiding it.'''
        return self._row_of.get(number)

    def fold_end(self, number: int) -> int | None:
        return self._spans.get(number)

    def is_folded(self, number: int) -> bool:
        return number in self._folded

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        return 0 if parent.isValid() else len(self._visible)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):  # type: ignore[override]
        line = self.line_at(index.row())
        if line is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return line.text
        if role == Qt.ItemDataRole.UserRole:
            return line
        if role == Qt.ItemDataRole.UserRole + 1:
            return self._offsets.get(line.number)
        if role == Qt.ItemDataRole.UserRole + 2:
            end = self._spans.get(line.number)
            if end is None:
                return None
            return (line.number in self._folded,
                    evd_leaf.fold_summary(self._lines, line.number) if line.number in self._folded else '')
        if role == Qt.ItemDataRole.UserRole + 3:
            return self._highlight
        if role == Qt.ItemDataRole.ToolTipRole:
            return _line_tooltip(line, self._offsets.get(line.number))
        return None

    def set_highlight(self, key: str) -> None:
        if key == self._highlight:
            return
        self._highlight = key
        if self._visible:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._visible) - 1, 0))


def _line_tooltip(line: CodeLine, offset: int | None = None) -> str:
    info = line.info
    address = (f'at 0x{offset:04X} &mdash; a label here would be named '
               f'<code>{evd_leaf.label_for_offset(offset)}</code>') if offset is not None else ''
    if info is None:
        return '<br>'.join(part for part in (line.text.strip(), address) if part)
    bits = [f'<b>{info.name}</b>']
    if address:
        bits.append(address)
    if info.engine and info.engine != info.name:
        bits.append(f'engine name <code>{info.engine}</code>')
    if info.opcode is not None:
        bits.append(f'opcode 0x{info.opcode:02X} &mdash; {info.family}')
    if info.summary:
        bits.append(info.summary)
    if info.raw:
        bits.append('<i>No structured form yet; carries raw payload words.</i>')
    return '<br>'.join(bits)


class LineDelegate(QStyledItemDelegate):
    '''Paints a line as gutter number, indent, coloured head, arguments, comment.

    Each coloured run is positioned by measuring the text before it, using the
    painter's own metrics. A free-standing QFontMetrics is resolved for the
    screen rather than for the paint device, so its advances can be half again
    what actually gets drawn; stepping by those would open a visible gap in
    front of every `=` and `,`.
    '''

    # Two gutters. The address is the one that matters here -- a jump target is
    # named after the byte it sits at, so `loc_02BC` is findable only by reading
    # addresses down the column. The line number stays because every compiler
    # diagnostic and the Problems list speak in lines.
    FOLD_GUTTER = 16
    LINE_GUTTER = 40
    ADDR_GUTTER = 52
    GUTTER = FOLD_GUTTER + LINE_GUTTER + ADDR_GUTTER
    PADDING = 8

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._font = QFont('Courier New')
        self._font.setStyleHint(QFont.StyleHint.Monospace)
        self._font.setFixedPitch(True)
        self._metrics = QFontMetrics(self._font, parent)

    def row_height(self) -> int:
        return self._metrics.height() + 6

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # type: ignore[override]
        line: CodeLine | None = index.data(Qt.ItemDataRole.UserRole)
        width = self.GUTTER + self.PADDING
        if line is not None:
            width += self._metrics.horizontalAdvance(' ' * (line.depth * 4) + line.text.strip() + '  ')
        return QSize(width, self.row_height())

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:  # type: ignore[override]
        line: CodeLine | None = index.data(Qt.ItemDataRole.UserRole)
        if line is None:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setFont(self._font)
        rect = option.rect
        if option.state & QStyle.StateFlag.State_Selected:
            # A wash rather than the full highlight brush, so the category
            # colours that carry the meaning stay readable when selected.
            wash = QColor(option.palette.highlight().color())
            wash.setAlpha(90)
            painter.fillRect(rect, wash)

        metrics = painter.fontMetrics()
        baseline = rect.y() + (rect.height() + metrics.ascent() - metrics.descent()) // 2
        right = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        centre = int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)

        fold = index.data(Qt.ItemDataRole.UserRole + 2)
        if fold is not None:
            collapsed, _summary = fold
            painter.setPen(QColor(_COLOR_ARG_KEY))
            painter.drawText(QRect(rect.x(), rect.y(), self.FOLD_GUTTER, rect.height()),
                             centre, '▸' if collapsed else '▾')

        painter.setPen(QColor(_COLOR_LINE_NUMBER))
        painter.drawText(QRect(rect.x() + self.FOLD_GUTTER, rect.y(), self.LINE_GUTTER - 6, rect.height()),
                         right, str(line.number))

        offset = index.data(Qt.ItemDataRole.UserRole + 1)
        if line.kind in (evd_leaf.KIND_BLANK, evd_leaf.KIND_COMMENT):
            offset = None
        if offset is not None:
            # Printed the way the decompiler names a label, so `loc_02BC` in a
            # goto can be matched against this column by eye.
            painter.setPen(QColor(_COLOR_ADDRESS))
            painter.drawText(
                QRect(rect.x() + self.FOLD_GUTTER + self.LINE_GUTTER, rect.y(),
                      self.ADDR_GUTTER - 6, rect.height()),
                right, f'{offset:04X}')

        origin = rect.x() + self.GUTTER + self.PADDING
        self._metrics = painter.fontMetrics()   # cached so hit-testing matches what was drawn
        drawn = ' ' * (line.depth * 4)
        highlight = index.data(Qt.ItemDataRole.UserRole + 3)

        for text, color, italic, bold, key in self.segments(line, fold):
            if not text:
                continue
            font = QFont(self._font)
            font.setItalic(italic)
            font.setBold(bold)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            x = origin + metrics.horizontalAdvance(drawn)
            if key and key == highlight:
                wash = QColor(_COLOR_MATCH)
                wash.setAlpha(70)
                painter.fillRect(QRect(x - 1, rect.y() + 1, metrics.horizontalAdvance(text) + 2,
                                       rect.height() - 2), wash)
            painter.setPen(QColor(color))
            painter.drawText(x, baseline, text)
            drawn += text
        painter.restore()

    def segments(self, line: CodeLine, fold=None) -> list[tuple[str, str, bool, bool, str]]:
        '''The coloured runs a line is drawn from: (text, colour, italic, bold, value key).

        Painting and hit-testing share this, so what a click lands on is exactly
        what was drawn -- there is no second layout to drift from the first.
        '''
        out: list[tuple[str, str, bool, bool, str]] = []

        def add(text: str, color: str, italic: bool = False, bold: bool = False, key: str = '') -> None:
            out.append((text, color, italic, bold, key))

        structure = _CATEGORY_COLORS[evd_leaf.CATEGORY_STRUCTURE]
        if line.kind in (evd_leaf.KIND_CLOSE, evd_leaf.KIND_ELSE):
            add(line.text.strip(), structure, bold=True)
        elif line.kind in (evd_leaf.KIND_BLANK, evd_leaf.KIND_UNKNOWN):
            add(line.text.strip(), _COLOR_VALUE)
        elif line.kind == evd_leaf.KIND_COMMENT:
            add(line.comment, _COLOR_COMMENT, italic=True)
        elif line.kind == evd_leaf.KIND_EVENT:
            add('event ', structure, bold=True)
            add(line.args[0].value if line.args else '', _COLOR_VALUE)
            add(' {', structure, bold=True)
        else:
            add(line.head, _category_color(line.category), bold=True)
            add('(', _COLOR_VALUE)
            for i, arg in enumerate(line.args):
                if i:
                    add(', ', _COLOR_VALUE)
                if arg.key:
                    add(arg.key, _COLOR_ARG_KEY)
                    add('=', _COLOR_VALUE)
                add(arg.value, _COLOR_STRING if arg.value.startswith('"') else _COLOR_VALUE,
                    key=evd_leaf.value_key(arg.value) or '')
            add(')', _COLOR_VALUE)
            if line.opens:
                add(' {', structure, bold=True)

        if line.comment and line.kind != evd_leaf.KIND_COMMENT:
            add('   ' + line.comment, _COLOR_COMMENT, italic=True)
        if fold is not None and fold[0]:
            # The collapsed body, stated rather than just missing, so a fold is
            # never mistaken for a block that happens to be empty.
            add(fold[1], _COLOR_GUTTER, italic=True)
        return out

    def value_at(self, line: CodeLine, x: int) -> str | None:
        '''The value key of the segment at pixel `x`, or None.'''
        drawn = ' ' * (line.depth * 4)
        cursor = self.GUTTER + self.PADDING
        for text, _color, italic, bold, key in self.segments(line):
            font = QFont(self._font)
            font.setItalic(italic)
            font.setBold(bold)
            metrics = QFontMetrics(font, self.parent()) if self._metrics is None else self._metrics
            start = cursor + metrics.horizontalAdvance(drawn)
            width = metrics.horizontalAdvance(text)
            if start <= x < start + width:
                return key or None
            drawn += text
        return None


class StructureView(QListView):
    '''The EVDCODE line list: reorderable, droppable and deletable.'''

    lineMoved = pyqtSignal(int, int)              # moved line number, target line number
    commandDropped = pyqtSignal(str, int)         # command name, target line number
    deleteRequested = pyqtSignal(int)             # line number
    currentLineChanged = pyqtSignal(object)       # line number or None
    toggleYieldRequested = pyqtSignal(int)        # line number
    gotoLabelRequested = pyqtSignal(str)          # label name
    foldToggled = pyqtSignal(int)                 # opener line number
    valuePicked = pyqtSignal(str)                 # value key, or '' to clear
    addLabelRequested =pyqtSignal(int)
    selectJumpTargetRequested = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = LineModel(self)
        self.setModel(self._model)
        self.setItemDelegate(LineDelegate(self))
        self.setObjectName('TextMono')
        # Rows differ in width, not height, and long lines are common: uniform
        # sizes would clip them with no way to scroll across.
        self.setUniformItemSizes(False)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setAcceptDrops(True)
        self.setDragEnabled(False)  # drags are started by hand so a click still selects
        self.setDropIndicatorShown(True)
        self._drag_origin: QPoint | None = None
        self.selectionModel().currentChanged.connect(
            lambda current, _previous: self.currentLineChanged.emit(
                self._model.line_number_at(current.row()) if current.isValid() else None
            )
        )

    def set_lines(self, lines: list[CodeLine], offsets: dict[int, int] | None = None,
                  folded: set[int] | None = None) -> None:
        self._model.set_lines(lines, offsets, folded)

    def set_offsets(self, offsets: dict[int, int]) -> None:
        self._model.set_offsets(offsets)

    def set_folded(self, folded: set[int]) -> None:
        current = self.current_line()
        self._model.set_folded(folded)
        self.select_line(current, scroll=False)

    def set_highlight(self, key: str) -> None:
        self._model.set_highlight(key)

    def current_line(self) -> int | None:
        '''The script line under the cursor. Rows are not line numbers once a
        block is collapsed, so everything goes through the model's mapping.'''
        index = self.currentIndex()
        return self._model.line_number_at(index.row()) if index.isValid() else None

    def select_line(self, number: int | None, scroll: bool = True) -> None:
        if number is None:
            return
        row = self._model.row_of(number)
        if row is None:
            return  # inside a collapsed block; nothing to put a cursor on
        index = self._model.index(row, 0)
        self.setCurrentIndex(index)
        if scroll:
            self.scrollTo(index, QAbstractItemView.ScrollHint.EnsureVisible)

    ### drag out ---------------------------------------------------------

    def mousePressEvent(self, e) -> None:
        point = e.position().toPoint()
        if e.button() == Qt.MouseButton.LeftButton and point.x() < LineDelegate.FOLD_GUTTER:
            number = self._model.line_number_at(self.indexAt(point).row())
            if number is not None and self._model.fold_end(number):
                self.foldToggled.emit(number)
                return
        if e.button() == Qt.MouseButton.LeftButton:
            index = self.indexAt(point)
            line = index.data(Qt.ItemDataRole.UserRole) if index.isValid() else None
            if line is not None:
                delegate = self.itemDelegate()
                key = delegate.value_at(line, point.x()) if isinstance(delegate, LineDelegate) else None
                self.valuePicked.emit(key or '')
            self._drag_origin = point
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self._drag_origin is None or not (e.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(e)
            return
        if (e.position().toPoint() - self._drag_origin).manhattanLength() < QApplication.startDragDistance():
            super().mouseMoveEvent(e)
            return
        index = self.indexAt(self._drag_origin)
        if not index.isValid():
            super().mouseMoveEvent(e)
            return
        mime = QMimeData()
        mime.setData(_MIME_MOVE_LINE, str(index.row() + 1).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)
        self._drag_origin = None

    ### drop in ----------------------------------------------------------

    def dragEnterEvent(self, e) -> None:
        md = e.mimeData()
        if md.hasFormat(_MIME_MOVE_LINE) or md.hasFormat(_MIME_NEW_COMMAND):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e) -> None:
        e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        target = self._drop_target_line(e.position().toPoint())
        md = e.mimeData()
        if md.hasFormat(_MIME_MOVE_LINE):
            self.lineMoved.emit(int(bytes(md.data(_MIME_MOVE_LINE)).decode()), target)
            e.acceptProposedAction()
        elif md.hasFormat(_MIME_NEW_COMMAND):
            self.commandDropped.emit(bytes(md.data(_MIME_NEW_COMMAND)).decode(), target)
            e.acceptProposedAction()
        else:
            e.ignore()

    def _drop_target_line(self, pos: QPoint) -> int:
        '''The script line a drop lands before.

        Dropping below a collapsed block has to land after the whole block, not
        after its opener, or the insert would appear inside something folded.
        '''
        index = self.indexAt(pos)
        if not index.isValid():
            return len(self._model._lines)
        number = self._model.line_number_at(index.row())
        if number is None:
            return len(self._model._lines)
        if pos.y() > self.visualRect(index).center().y():
            end = self._model.fold_end(number) if self._model.is_folded(number) else None
            return (end or number) + 1
        return number

    ### keys and context menu ---------------------------------------------

    def keyPressEvent(self, e) -> None:
        number = self.current_line()
        if number and e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.deleteRequested.emit(number)
            return
        # Left collapses, Right expands -- the convention every tree and code
        # folder uses, so it needs no discovering.
        if number and e.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right) and self._model.fold_end(number):
            collapsing = e.key() == Qt.Key.Key_Left
            if collapsing != self._model.is_folded(number):
                self.foldToggled.emit(number)
            return
        super().keyPressEvent(e)

    def contextMenuEvent(self, e) -> None:
        index = self.indexAt(e.pos())
        if not index.isValid():
            return
        number = self._model.line_number_at(index.row())
        if number is None:
            return
        self.setCurrentIndex(index)
        line: CodeLine | None = index.data(Qt.ItemDataRole.UserRole)

        menu = QMenu(self)

        add_label_action = menu.addAction('Add label here')
        selected_target_action = None
        if line is not None and line.target_label:
            selected_target_action = menu.addAction('Select jump target')
        menu.addSeparator()

        goto_action = None
        target = line.target_label if line is not None else None
        if target:
            label_exists = any(name == target for name, _ in evd_leaf.iter_labels(self._model._lines))
            if label_exists:
                goto_action = menu.addAction(f'Go to {target}')
            else:
                goto_action = menu.addAction(f'Go to {target} (Not found)')
                goto_action.setEnabled(False)
                create_label_action = menu.addAction(f'Create missing label "{target}" here')
            menu.addSeparator()
        yield_action = None
        if line is not None and line.kind == evd_leaf.KIND_COMMAND:
            has_yield = line.arg('yield') is not None
            yield_action = menu.addAction('Clear yield (run next command immediately)' if has_yield
                                          else 'Set yield (pause one game frame)')
        copy_action = menu.addAction('Copy line')
        menu.addSeparator()
        if line is not None and line.opens and line.block_end:
            delete_action = menu.addAction(f'Delete block ({line.block_end - line.number + 1} lines)')
        else:
            delete_action = menu.addAction('Delete line')

        chosen = menu.exec(e.globalPos())
        if chosen is None:
            return
        if chosen is add_label_action:
            self.addLabelRequested.emit(number)
        elif selected_target_action is not None and chosen is selected_target_action:
            self.selectJumpTargetRequested.emit(number)
        if chosen is delete_action:
            self.deleteRequested.emit(number)
        elif goto_action is not None and chosen is goto_action and target:
            self.gotoLabelRequested.emit(target)
        elif chosen is yield_action:
            self.toggleYieldRequested.emit(number)
        elif chosen is copy_action and line is not None:
            QApplication.clipboard().setText(line.text.strip())


###------------------------------------------------ Code view ------------------------------------------------###

class EvdCodeHighlighter(QSyntaxHighlighter):
    '''EVDCODE syntax colouring, keyed to the same categories the rows use.'''

    def __init__(self, document: QTextDocument) -> None:
        super().__init__(document)
        self._comment = self._format(_COLOR_COMMENT, italic=True)
        self._string  = self._format(_COLOR_STRING)
        self._number  = self._format(_COLOR_NUMBER)
        self._key     = self._format(_COLOR_ARG_KEY)
        self._brace   = self._format(_CATEGORY_COLORS[evd_leaf.CATEGORY_STRUCTURE], bold=True)
        self._heads   = {
            category: self._format(color, bold=True)
            for category, color in _CATEGORY_COLORS.items()
        }

    @staticmethod
    def _format(color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        fmt.setFontWeight(QFont.Weight.Bold if bold else QFont.Weight.Normal)
        fmt.setFontItalic(italic)
        return fmt

    def highlightBlock(self, text: str | None) -> None:
        if not text:
            return
        split = evd_leaf.comment_start(text)
        code = text if split < 0 else text[:split]

        head_start = len(code) - len(code.lstrip())
        stripped = code.strip()
        if stripped.startswith('event ') or stripped.startswith('option'):
            self.setFormat(head_start, len(stripped.split()[0]), self._heads[evd_leaf.CATEGORY_STRUCTURE])
        else:
            paren = code.find('(')
            if paren > 0:
                head = code[head_start:paren].strip()
                if head:
                    category = (evd_leaf.CATEGORY_STRUCTURE if head in _STRUCTURE_HEADS
                                else evd_leaf.COMMANDS.category(head))
                    self.setFormat(head_start, len(head),
                                   self._heads.get(category, self._heads[evd_leaf.CATEGORY_NORMAL]))

        for i, ch in enumerate(code):
            if ch in '{}':
                self.setFormat(i, 1, self._brace)

        for match in _RE_HL_KEY.finditer(code):
            self.setFormat(match.start(1), len(match.group(1)), self._key)
        for match in _RE_HL_NUMBER.finditer(code):
            self.setFormat(match.start(), len(match.group()), self._number)
        for match in _RE_HL_STRING.finditer(code):
            self.setFormat(match.start(), len(match.group()), self._string)
        if split >= 0:
            self.setFormat(split, len(text) - split, self._comment)


class _LineNumberArea(QWidget):
    def __init__(self, editor: 'CodeEdit') -> None:
        super().__init__(editor)
        self._editor = editor
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def sizeHint(self) -> QSize:
        return QSize(self._editor.line_number_width(), 0)

    def paintEvent(self, event) -> None:
        self._editor.paint_line_numbers(event)

    def mousePressEvent(self, a0) -> None:
        if a0 is None or a0.button() != Qt.MouseButton.LeftButton:
            return
        number = self._editor.fold_number_at(a0.position().toPoint().y())
        if number is not None:
            self._editor.foldToggled.emit(number)


class CodeEdit(QPlainTextEdit):
    '''Plain text editor with a line-number gutter, folding and command drops.'''

    commandDropped = pyqtSignal(str, int)
    foldToggled = pyqtSignal(int)

    FOLD_MARGIN = 16

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._spans: dict[int, int] = {}
        self._folded: set[int] = set()
        self.setObjectName('TextMono')
        font = QFont('Courier New')
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(QFontMetrics(font).horizontalAdvance(' ') * 4)
        self.setAcceptDrops(True)
        self._gutter = _LineNumberArea(self)
        self.blockCountChanged.connect(lambda _: self._update_gutter_width())
        self.updateRequest.connect(self._on_update_request)
        self._update_gutter_width()

    def line_number_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return self.FOLD_MARGIN + 12 + self.fontMetrics().horizontalAdvance('9') * digits

    def set_folds(self, spans: dict[int, int], folded: set[int]) -> None:
        '''Collapse the given blocks by hiding their text blocks.

        Visibility is presentation only -- `toPlainText` still returns the whole
        script, so a fold can never change what gets compiled or saved.
        '''
        self._spans = spans
        self._folded = set(folded)
        hidden = set()
        for opener in self._folded:
            end = spans.get(opener)
            if end:
                hidden.update(range(opener + 1, end + 1))
        document = self.document()
        changed = False
        block = document.firstBlock()
        while block.isValid():
            visible = (block.blockNumber() + 1) not in hidden
            if block.isVisible() != visible:
                block.setVisible(visible)
                changed = True
            block = block.next()
        if changed:
            document.markContentsDirty(0, document.characterCount())
            self.viewport().update()
            self._gutter.update()

    def _update_gutter_width(self) -> None:
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)

    def _on_update_request(self, rect, dy: int) -> None:
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        cr = self.contentsRect()
        self._gutter.setGeometry(QRect(cr.left(), cr.top(), self.line_number_width(), cr.height()))

    def paint_line_numbers(self, event) -> None:
        painter = QPainter(self._gutter)
        height = self.fontMetrics().height()
        block = self.firstVisibleBlock()
        number = block.blockNumber() + 1
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(QColor(_COLOR_GUTTER))
                painter.drawText(self.FOLD_MARGIN, int(top),
                                 self._gutter.width() - self.FOLD_MARGIN - 6, height,
                                 int(Qt.AlignmentFlag.AlignRight), str(number))
                if number in self._spans:
                    painter.setPen(QColor(_COLOR_ARG_KEY))
                    painter.drawText(0, int(top), self.FOLD_MARGIN, height,
                                     int(Qt.AlignmentFlag.AlignHCenter),
                                     '▸' if number in self._folded else '▾')
            block = block.next()
            # A hidden block contributes no height, so the next visible one
            # starts where this one would have.
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            number += 1

    def mousePressEvent(self, e) -> None:
        if e.position().toPoint().x() < 0:  # inside the gutter, handled there
            return
        super().mousePressEvent(e)

    def fold_number_at(self, y: int) -> int | None:
        '''The foldable line whose gutter row contains `y`.'''
        block = self.firstVisibleBlock()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        while block.isValid():
            bottom = top + self.blockBoundingRect(block).height()
            if block.isVisible() and top <= y < bottom:
                number = block.blockNumber() + 1
                return number if number in self._spans else None
            block = block.next()
            top = bottom
        return None

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasFormat(_MIME_NEW_COMMAND):
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e) -> None:
        if e.mimeData().hasFormat(_MIME_NEW_COMMAND):
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e) -> None:
        md = e.mimeData()
        if md.hasFormat(_MIME_NEW_COMMAND):
            cursor = self.cursorForPosition(e.position().toPoint())
            self.commandDropped.emit(bytes(md.data(_MIME_NEW_COMMAND)).decode(),
                                     cursor.blockNumber() + 1)
            e.acceptProposedAction()
            return
        super().dropEvent(e)


class CodeView(QWidget):
    '''The EVDCODE text tab.'''

    textEdited = pyqtSignal(str)
    commandDropped = pyqtSignal(str, int)
    cursorLineChanged = pyqtSignal(int)
    foldToggled = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._edit = CodeEdit()
        self._highlighter = EvdCodeHighlighter(self._edit.document())
        self._edit.textChanged.connect(lambda: self.textEdited.emit(self._edit.toPlainText()))
        self._edit.cursorPositionChanged.connect(
            lambda: self.cursorLineChanged.emit(self._edit.textCursor().blockNumber() + 1)
        )
        self._edit.commandDropped.connect(self.commandDropped)
        self._edit.foldToggled.connect(self.foldToggled)
        layout.addWidget(self._edit)
        self._error_line: int | None = None
        self._folds: tuple[dict[int, int], set[int]] = ({}, set())
        self._highlight: tuple[str, list] = ('', [])

    def set_text(self, text: str) -> None:
        if text == self._edit.toPlainText():
            return
        cursor_line = self.current_line()
        blocked = self._edit.blockSignals(True)
        try:
            self._edit.setPlainText(text)
        finally:
            self._edit.blockSignals(blocked)
        self.goto_line(cursor_line)
        self._refresh_selections()
        # setPlainText rebuilds the document, so the folds have to be re-applied
        # to the new text blocks or the view silently unfolds itself.
        self._edit.set_folds(*self._folds)

    def text(self) -> str:
        return self._edit.toPlainText()

    def current_line(self) -> int:
        return self._edit.textCursor().blockNumber() + 1

    def goto_line(self, number: int) -> None:
        block = self._edit.document().findBlockByNumber(max(0, number - 1))
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        blocked = self._edit.blockSignals(True)
        try:
            self._edit.setTextCursor(cursor)
        finally:
            self._edit.blockSignals(blocked)
        self._edit.ensureCursorVisible()

    def set_folds(self, spans: dict[int, int], folded: set[int]) -> None:
        self._folds = (spans, set(folded))
        self._edit.set_folds(spans, folded)

    def mark_error_line(self, number: int | None) -> None:
        self._error_line = number
        self._refresh_selections()

    def set_highlight(self, key: str, lines: list[CodeLine]) -> None:
        self._highlight = (key, lines)
        self._refresh_selections()

    def value_at_cursor(self, lines: list[CodeLine]) -> str:
        '''The value key under the caret, for picking one by moving the cursor.'''
        cursor = self._edit.textCursor()
        number = cursor.blockNumber() + 1
        if not (1 <= number <= len(lines)):
            return ''
        found = evd_leaf.token_at(lines[number - 1].text, cursor.positionInBlock())
        if found is None:
            return ''
        # Only a value counts, and the line model already knows which spans are
        # values, so ask it rather than guessing from the token alone.
        key = evd_leaf.value_key(found[0]) or ''
        if not key:
            return ''
        start = found[1]
        return key if any(a <= start < b for a, b in
                          evd_leaf.value_occurrences(lines[number - 1], key)) else ''

    def _refresh_selections(self) -> None:
        selections: list[QTextEdit.ExtraSelection] = []
        key, lines = self._highlight
        if key:
            fmt = QTextCharFormat()
            wash = QColor(_COLOR_MATCH)
            wash.setAlpha(70)
            fmt.setBackground(wash)
            for line in lines:
                block = self._edit.document().findBlockByNumber(line.number - 1)
                if not block.isValid():
                    continue
                for start, end in evd_leaf.value_occurrences(line, key):
                    cursor = QTextCursor(block)
                    cursor.setPosition(block.position() + start)
                    cursor.setPosition(block.position() + end, QTextCursor.MoveMode.KeepAnchor)
                    selection = QTextEdit.ExtraSelection()
                    selection.format = fmt                 # type: ignore[attr-defined]
                    selection.cursor = cursor              # type: ignore[attr-defined]
                    selections.append(selection)
        if self._error_line is not None:
            block = self._edit.document().findBlockByNumber(max(0, self._error_line - 1))
            if block.isValid():
                fmt = QTextCharFormat()
                fmt.setBackground(QColor(_COLOR_ERROR).darker(220))
                fmt.setProperty(QTextCharFormat.Property.FullWidthSelection, True)
                selection = QTextEdit.ExtraSelection()
                selection.format = fmt                     # type: ignore[attr-defined]
                selection.cursor = QTextCursor(block)      # type: ignore[attr-defined]
                selections.append(selection)
        self._edit.setExtraSelections(selections)


###--------------------------------------------- Command palette ---------------------------------------------###

class CommandPalette(QWidget):
    '''Every insertable command and construct, grouped by opcode family.'''

    commandActivated = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText('Filter commands...')
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        self._tree = _PaletteTree()
        self._tree.setHeaderHidden(True)
        self._tree.itemDoubleClicked.connect(
            lambda item, _column: self._activate(item)
        )
        layout.addWidget(self._tree)

        hint = QLabel('Drag into the script, or double-click to insert.')
        hint.setObjectName('TextSubtitle')
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._items: list[tuple[QTreeWidgetItem, str]] = []
        self._populate()

    def _populate(self) -> None:
        groups: dict[str, QTreeWidgetItem] = {}
        for name, family, summary in evd_leaf.COMMANDS.palette_entries():
            parent = groups.get(family)
            if parent is None:
                parent = QTreeWidgetItem(self._tree, [family])
                parent.setFlags(Qt.ItemFlag.ItemIsEnabled)
                groups[family] = parent
            item = QTreeWidgetItem(parent, [name])
            item.setData(0, Qt.ItemDataRole.UserRole, name)
            info = evd_leaf.COMMANDS.get(name)
            category = evd_leaf.COMMANDS.category(name)
            item.setForeground(0, QColor(_category_color(category)))
            tip = [f'<b>{name}</b>']
            if info and info.opcode is not None:
                tip.append(f'opcode 0x{info.opcode:02X}')
            if summary:
                tip.append(summary)
            item.setToolTip(0, '<br>'.join(tip))
            self._items.append((item, f'{name} {summary}'.lower()))
        structure_group = groups.get('Structure')
        if structure_group is not None:
            self._tree.expandItem(structure_group)

    def _activate(self, item: QTreeWidgetItem) -> None:
        name = item.data(0, Qt.ItemDataRole.UserRole)
        if name:
            self.commandActivated.emit(name)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for item, searchable in self._items:
            item.setHidden(bool(needle) and needle not in searchable)
        for i in range(self._tree.topLevelItemCount()):
            group = self._tree.topLevelItem(i)
            if group is None:
                continue
            visible = any(not group.child(c).isHidden() for c in range(group.childCount()))
            group.setHidden(not visible)
            if needle and visible:
                group.setExpanded(True)


class _PaletteTree(QTreeWidget):
    '''Drag source for palette entries.'''

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setDragDropMode(QTreeWidget.DragDropMode.DragOnly)

    def mimeData(self, items) -> QMimeData:  # type: ignore[override]
        mime = QMimeData()
        for item in items:
            name = item.data(0, Qt.ItemDataRole.UserRole)
            if name:
                mime.setData(_MIME_NEW_COMMAND, str(name).encode())
                break
        return mime


###------------------------------------------ Parameter inspector -------------------------------------------###

class IdCombo(QComboBox):
    '''An id field as a searchable list of `id: name`, still open to any value.

    Editable on purpose. The tables name 382 characters and 671 items but the
    game has more ids than that, and an id with no name is an ordinary thing to
    want to type -- so the list is a shortcut, never a restriction.
    '''

    def __init__(self, domain: str, value: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._domain = domain
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        # Reserves the strip paintEvent draws the chevron into, so a long name
        # does not run underneath it.
        self.setStyleSheet('QComboBox { padding-right: 18px; }')
        for number, name in evd_leaf.domain_choices(domain):
            self.addItem(f'{number}: {name}', number)

        completer = self.completer()
        if completer is not None:
            # Matching anywhere, not just at the start: an author knows the name
            # of the thing, not the number it sorts under.
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)

        number = evd_leaf.parse_number(value)
        index = self.findData(number) if number is not None else -1
        if index >= 0:
            self.setCurrentIndex(index)
        else:
            # An id with no name, or a value that is not a number at all
            # (`character=Jack` compiles too). Shown verbatim.
            self.setCurrentIndex(-1)
            self.setEditText(value)

    def value(self) -> str:
        '''The field text to write: the id alone, never the display label.'''
        text = self.currentText().strip()
        head, sep, _name = text.partition(':')
        if sep and evd_leaf.parse_number(head) is not None:
            return head.strip()
        return text

    def paintEvent(self, a0) -> None:
        '''Draw the chevron the app-wide style leaves off.

        `QComboBox::drop-down { border: none }` in the theme flattens the arrow
        everywhere, which is fine for a combo that reads as a control on its own
        but not for one sitting in a table cell -- there it looks like ordinary
        text and nobody would think to click it. Drawn here rather than by
        changing the shared stylesheet, which every other combo in the app uses.
        '''
        super().paintEvent(a0)
        painter = QPainter(self)
        painter.setPen(QColor(_COLOR_ARG_KEY))
        rect = self.rect().adjusted(0, 0, -6, 0)
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), '▾')
        painter.end()



class ParameterInspector(QWidget):
    '''Edit one line's parameters, with the role of each spelled out.

    Input parameters are yours. Derived ones are computed from inputs and
    cross-checked on compile, so they are shown read-only: change the input they
    come from and the editor drops the stale value for you.
    '''

    applyRequested = pyqtSignal(int, object)  # line number, tuple[Arg, ...]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._line: CodeLine | None = None
        self._offset: int | None = None
        # packed field name -> (word as it was on the line, its part layout)
        self._packed: dict[str, tuple[int, tuple[tuple[str, int, int], ...]]] = {}
        self._explicit_parts: set[str] = set()   # halves the line writes out as well as packs
        self._combos: dict[int, IdCombo] = {}     # table row -> its id picker
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        header = QHBoxLayout()
        self._title = QLabel('Select a line')
        self._title.setObjectName('TextHeader')
        header.addWidget(self._title)
        header.addStretch(1)
        self._show_derived = QPushButton('Show derived values')
        self._show_derived.setObjectName('BtnSurface')
        self._show_derived.setCheckable(True)
        self._show_derived.setToolTip(
            'Some of a command\'s values are not set directly: they are computed from other '
            'values on the same line. `character_number` is the low half of `character`, for '
            'example. They are hidden because there is nothing useful to do with them here -- '
            'set the value they come from and they follow. Turn this on to see them anyway.'
        )
        self._show_derived.toggled.connect(lambda _: self.show_line(self._line, self._offset))
        header.addWidget(self._show_derived)
        self._apply = QPushButton('Apply')
        self._apply.setObjectName('BtnImportant')
        self._apply.setToolTip(
            'Write the values in this table back to the selected line. Nothing changes until '
            'you press it, and if the result would not assemble the edit is refused and the '
            'line is left as it was.'
        )
        self._apply.clicked.connect(self._emit_apply)
        self._apply.setEnabled(False)
        header.addWidget(self._apply)
        layout.addLayout(header)

        self._summary = QLabel()
        self._summary.setObjectName('TextMuted')
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(['Parameter', 'Role', 'Value', 'Meaning'])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.SelectedClicked
            | QTableWidget.EditTrigger.EditKeyPressed
        )
        header_view = self._table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        # Interactive rather than ResizeToContents: the id fields put a combo box
        # in this column, and contents-sizing measures the cell's text, not the
        # widget sitting on top of it, so the picker would be clipped to the
        # width of a bare number.
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table)

    def clear(self) -> None:
        self._line = None
        self._offset = None
        self._packed.clear()
        self._explicit_parts.clear()
        self._combos.clear()
        self._title.setText('Select a line')
        self._summary.clear()
        self._table.setRowCount(0)
        self._apply.setEnabled(False)

    def show_line(self, line: CodeLine | None, offset: int | None = None) -> None:
        self._line = line
        self._offset = offset
        self._combos.clear()
        self._table.clearContents()
        self._table.setRowCount(0)
        if line is None or not line.is_call:
            self._title.setText('Select a line')
            self._summary.setText('' if line is None else f'{line.kind}: no parameters to edit.')
            self._apply.setEnabled(False)
            return

        info = line.info
        title = f'Line {line.number}'
        if offset is not None:
            title += f' @ 0x{offset:04X}'
        title += f': {line.head}'
        if info and info.opcode is not None:
            title += f'  (opcode 0x{info.opcode:02X})'
        self._title.setText(title)
        summary = info.summary if info else ''
        if info and info.engine and info.engine != info.name:
            summary = f'{summary}  (engine name: {info.engine})'
        if info and info.raw:
            summary += ('  This command has no structured form yet: its payload is raw words, '
                        'preserved exactly as decoded.')
        self._summary.setText(summary.strip() or 'No documented summary for this line.')
        self._apply.setEnabled(True)

        present = {a.key: a.value for a in line.args if a.key}
        positional = [a for a in line.args if not a.key]
        rows: list[tuple[str, str, str, str, str]] = [
            ('', 'positional', a.value, 'Unnamed argument, passed through as written.', '')
            for a in positional
        ]
        self._packed.clear()
        self._explicit_parts.clear()
        self._combos.clear()
        # A field that is a half of a packed word on this line is rendered once,
        # as an editable part underneath that word -- never also as a row of its
        # own, which for the commands that spell both out would be a duplicate.
        part_names: set[str] = set()
        for arg in line.args:
            specs = evd_leaf.packed_spec(info, arg.key) if arg.key else None
            if specs and evd_leaf.parse_number(arg.value) is not None:
                part_names.update(name for name, _s, _m in specs)
        self._explicit_parts.update(a.key for a in line.args if a.key in part_names)

        seen: set[str] = set(part_names)
        for arg in line.args:
            if not arg.key or arg.key in part_names:
                continue
            param = info.by_name.get(arg.key) if info else None
            role = param.role if param else 'input'
            if role == 'derived' and not self._show_derived.isChecked():
                continue
            seen.add(arg.key)
            meaning = info.meaning_of(arg.key) if info else 'No command index loaded.'
            rows.append((arg.key, role, arg.value, meaning, ''))
            rows.extend(self._packed_rows(info, arg, present))
        if info:
            for param in info.parameters:
                if param.name in seen or param.name in present or not param.is_input:
                    continue
                rows.append((param.name, 'unset', '', param.meaning, ''))

        self._table.setRowCount(len(rows))
        for row, (name, role, value, meaning, owner) in enumerate(rows):
            name_item = QTableWidgetItem(f'  {name}' if owner else name)
            name_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            role_item = QTableWidgetItem(role)
            role_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            if role == 'derived':
                role_item.setToolTip('Recomputed from the inputs it comes from; edit those instead.')
                role_item.setForeground(QColor(_COLOR_GUTTER))
            value_item = QTableWidgetItem(value)
            if owner:
                value_item.setData(Qt.ItemDataRole.UserRole, owner)
                name_item.setForeground(QColor(_COLOR_ARG_KEY))
                role_item.setForeground(QColor(_COLOR_ARG_KEY))
            if role == 'derived':
                value_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                value_item.setForeground(QColor(_COLOR_GUTTER))
            meaning_item = QTableWidgetItem(meaning)
            meaning_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            meaning_item.setToolTip(meaning)
            # A meaning generated from the parameter's own name is a naming
            # convention, not a finding. Muting it keeps a template sentence
            # from reading like something traced out of the handler.
            evidence = info.evidence_of(name) if info and name else 'none'
            if evidence in ('template', 'none', 'unset'):
                meaning_item.setForeground(QColor(_COLOR_GUTTER))
                meaning_item.setToolTip(
                    meaning + _TEMPLATE_MEANING_NOTE
                )
            elif evidence == 'untraced':
                meaning_item.setForeground(QColor(_COLOR_GUTTER))
            self._table.setItem(row, 0, name_item)
            self._table.setItem(row, 1, role_item)
            self._table.setItem(row, 2, value_item)
            self._table.setItem(row, 3, meaning_item)

            domain = evd_leaf.symbol_domain(info, name) if role != 'derived' else None
            if domain:
                combo = IdCombo(domain, value)
                self._table.setCellWidget(row, 2, combo)
                self._combos[row] = combo
        self._size_value_column()

    def _size_value_column(self) -> None:
        '''Wide enough for the widest picker on this line, narrow otherwise.'''
        width = _VALUE_COLUMN_WIDTH if self._combos else _VALUE_COLUMN_NARROW
        for combo in self._combos.values():
            width = max(width, combo.sizeHint().width() + 24)
        self._table.setColumnWidth(2, min(width, _VALUE_COLUMN_MAX))

    def _packed_rows(self, info, arg: Arg, present: dict[str, str]) -> list[tuple[str, str, str, str, str]]:
        '''Editable id and variant rows underneath a packed selector word.

        A character selector is one word holding an id and a variant, and the
        variant is only reachable by hand-computing `variant << 16 | id`. These
        rows expose the halves; Apply folds them back into the word.

        They are an editing aid, not a spelling. Writing `character_number=` to
        the file instead of `character=` is accepted by some commands, rejected
        by others, and on the rest silently drops the operand -- the line then
        means "the script's default character" and assembles to different bytes
        with no error. So the packed word is always what gets written.
        '''
        specs = evd_leaf.packed_spec(info, arg.key)
        word = evd_leaf.parse_number(arg.value)
        if specs is None or word is None:
            return []
        self._packed[arg.key] = (word, specs)
        shifts = {name: shift for name, shift, _mask in specs}
        rows: list[tuple[str, str, str, str, str]] = []
        for name, value in evd_leaf.split_packed(word, specs).items():
            meaning = (info.meaning_of(name) if info else '') or f'Part of the packed `{arg.key}` word.'
            # The id half decimal and the variant byte hex, matching how the
            # decompiler prints them when a command spells them out itself.
            if shifts[name] == 0:
                text = str(value)
                resolved = evd_leaf.SYMBOLS.lookup('character', value)
                if resolved:
                    meaning = f'{resolved} — {meaning}'
            else:
                text = f'0x{value:02X}'
            rows.append((name, f'part of {arg.key}', text, meaning, arg.key))
        return rows

    def _emit_apply(self) -> None:
        line = self._line
        # `event Name {` and a bare `}` have no call to rebuild, and rendering
        # one from an empty parameter table would destroy the line. The button
        # is disabled for them; this makes that a property of the method too.
        if line is None or not line.is_call:
            return
        edited: dict[str, str] = {}
        positional: list[str] = []
        part_edits: dict[str, dict[str, int]] = {}
        for row in range(self._table.rowCount()):
            name_item = self._table.item(row, 0)
            role_item = self._table.item(row, 1)
            value_item = self._table.item(row, 2)
            if name_item is None or value_item is None or role_item is None:
                continue
            name, role = name_item.text().strip(), role_item.text()
            combo = self._combos.get(row)
            value = combo.value() if combo is not None else value_item.text().strip()
            owner = value_item.data(Qt.ItemDataRole.UserRole)
            if owner:
                original = self._packed.get(owner)
                number = evd_leaf.parse_number(value)
                if original is not None and number is not None:
                    # Only a part the user actually moved overrides the word, so
                    # editing the word directly still wins for the bits the
                    # parts were never touched on.
                    if number != evd_leaf.split_packed(original[0], original[1]).get(name):
                        part_edits.setdefault(owner, {})[name] = number
                    if name in self._explicit_parts:
                        # The line spells this half out as well as packing it.
                        # Both are written, and they have to agree or the
                        # compiler rejects the line for disagreeing with itself.
                        edited[name] = value
                continue
            if role == 'positional':
                positional.append(value)
                continue
            if not name or not value:
                continue
            edited[name] = value

        for owner, values in part_edits.items():
            specs = self._packed[owner][1]
            base = evd_leaf.parse_number(edited.get(owner, ''))
            if base is None:
                base = self._packed[owner][0]
            edited[owner] = f'0x{evd_leaf.compose_packed(base, values, specs):08X}'

        # Keep the line's own order, then anything newly filled in, then the
        # derived fields the table is hiding -- so an unrelated edit never
        # reorders or silently drops what the decompiler printed.
        args: list[Arg] = [Arg('', value) for value in positional]
        for arg in line.args:
            if not arg.key:
                continue
            if arg.key in edited:
                args.append(Arg(arg.key, edited.pop(arg.key)))
            elif not self._show_derived.isChecked() and (
                    line.info.role_of(arg.key) == 'derived' if line.info else False):
                args.append(arg)
        args.extend(Arg(key, value) for key, value in edited.items())
        self.applyRequested.emit(line.number, tuple(args))


###--------------------------------------------- Problems panel ----------------------------------------------###

class ProblemsPanel(QWidget):
    '''Compiler diagnostics, one row each, clickable to jump to the line.'''

    lineActivated = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        self._status = QLabel('No problems')
        layout.addWidget(self._status)
        self._list = QListWidget()
        self._list.setObjectName('TextMono')
        self._list.itemActivated.connect(self._on_activated)
        self._list.itemClicked.connect(self._on_activated)
        layout.addWidget(self._list)

    def clear(self) -> None:
        self._list.clear()
        self._status.setText('No problems -- the script compiles.')
        self._status.setStyleSheet(f'color: {_COLOR_OK};')

    def set_errors(self, errors: list[EvdCompileError | None]) -> None:
        real = [e for e in errors if e is not None]
        self._list.clear()
        if not real:
            self.clear()
            return
        self._status.setText(f'{len(real)} problem(s)')
        self._status.setStyleSheet(f'color: {_COLOR_ERROR};')
        for error in real:
            label = f'line {error.line}: {error.message}' if error.line else error.message
            item = QListWidgetItem(label)
            item.setForeground(QColor(_COLOR_ERROR))
            if error.line:
                item.setData(Qt.ItemDataRole.UserRole, error.line)
            self._list.addItem(item)

    def _on_activated(self, item: QListWidgetItem) -> None:
        line = item.data(Qt.ItemDataRole.UserRole)
        if line:
            self.lineActivated.emit(int(line))


###------------------------------------------- Lowered source panel ------------------------------------------###

class LoweredSourcePanel(QWidget):
    '''The flat EVDSRC the block form lowers to: what the compiler actually reads.'''

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        header = QLabel('EVDSRC -- the flat form EVDCODE lowers to before assembly')
        header.setObjectName('TextMuted')
        layout.addWidget(header)
        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setObjectName('TextMono')
        self._text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self._text)

    def set_text(self, text: str) -> None:
        self._text.setPlainText(text)


###------------------------------------------------- Legend --------------------------------------------------###

class _LegendWidget(QWidget):
    '''Colour key for the command categories, shown top right.'''

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        for category, label in _LEGEND:
            dot = QLabel('●')
            dot.setStyleSheet(f'color: {_CATEGORY_COLORS[category]};')
            text = QLabel(label)
            text.setObjectName('TextSubtitle')
            lay.addWidget(dot)
            lay.addWidget(text)
