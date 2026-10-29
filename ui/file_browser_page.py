"""
File browser page contains all the UI for the file tree and metadata panel.
Separates the visual layout (FileBrowserPage) from the interaction logic (FileBrowserBehavior)
"""

from __future__ import annotations

import functools
import logging
import threading
from pathlib import Path
from typing import Any

from core.contracts import BaseEditor
from core.dispatcher import Dispatcher
from core.metadata_manager import NodeMetadataStore, address_to_key, key_to_address
from core.node import VfsNode
from core.registry import GLOBAL_ACTIONS, Registry
from core.workers import ActionDef, ActionResult, ActionStatus, ActionType, EditorPayload
from PyQt6 import sip
from PyQt6.QtCore import (
    QEasingCurve, QEvent, QModelIndex, QObject, QPoint,
    QPropertyAnimation, Qt, QTimer, pyqtSignal
)
from PyQt6.QtGui import QColor, QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QSplitter, QStackedWidget, QTextEdit, QTreeView,
    QVBoxLayout, QWidget
)
from ui.editor_page import EditorPage, EditorSession
from ui.logger import LoggingWindow
from ui.theme_manager import ThemeManager
from ui.tree_model import FlatSearchModel, TreeProxyModel, VfsTreeModel
from utilities import hline, human_size

logger = logging.getLogger(f'radiata.{__name__}')

# Context menu priority by ActionType. Lower integer values appear higher in the context menu.
_ACTION_TYPE_PRIORETY: dict[ActionType, int] = {
    ActionType.TREE_EXPAND: 0,
    ActionType.PROCESS:     1,
    ActionType.DIALOG:      2,
    ActionType.EXPORT:      3,
    ActionType.IMPORT:      4,
}

###------------------------------------------ Workspace UI -------------------------------------###


class FileBrowserPage(QWidget):
    """
    View component responsible for the visual layout of the page.
    Constructs the splitters, tree views, metadata panel, and log console.
    Contains strictly UI construction; logic is deferred to FileBrowserBehavior.
    """

    def __init__(self, metadata_store: NodeMetadataStore, parent=None) -> None:
        super().__init__(parent)
        self.metadata_store = metadata_store
        self._init_views()
        self._assemble_layout()

    def _init_views(self) -> None:
        """Instantiat all UI widgets deferring packing them into layouts"""
        self.tree_view = QTreeView()
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_view.setUniformRowHeights(True)
        self.tree_view.setAnimated(False)

        self.search_results_view = QListView()
        self.search_results_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.search_results_view.setUniformItemSizes(True)
        self.sidebar_stack = QStackedWidget()
        self.sidebar_stack.addWidget(self.tree_view)
        self.sidebar_stack.addWidget(self.search_results_view)

        self.metadata_panel = FileMetadataPanel(self.metadata_store)
        self.log_console    = LoggingWindow()

    def _assemble_layout(self) -> None:
        """Build the layout using nested QSplitters for user-adjustable panels."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Horizontal split:  Tree | Metadata
        self.h_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.h_splitter.addWidget(self.sidebar_stack)
        self.h_splitter.addWidget(self.metadata_panel)
        self.h_splitter.setStretchFactor(0, 3)
        self.h_splitter.setStretchFactor(1, 2)

        # Vertical split: Main area | Log
        self.v_splitter = QSplitter(Qt.Orientation.Vertical)
        self.v_splitter.addWidget(self.h_splitter)
        self.v_splitter.addWidget(self.log_console)
        self.v_splitter.setStretchFactor(0, 4)
        self.v_splitter.setStretchFactor(1, 1)

        layout.addWidget(self.v_splitter)

        # Review bar
        self.review_bar = QWidget()
        self.review_bar.setObjectName('ReviewBar')
        bar_layout = QHBoxLayout(self.review_bar)
        bar_layout.setContentsMargins(12, 8, 12, 8)

        self.status_label = QLabel('No pending ISO modifications')
        self.btn_review = QPushButton('Review Rebuild Queue')
        bar_layout.addWidget(self.status_label)
        bar_layout.addStretch()
        bar_layout.addWidget(self.btn_review)

        layout.addWidget(self.review_bar)
        self.review_bar.setVisible(False)

    def update_review_bar(self, has_mods: bool, count: int) -> None:
        """Toggle the visibility and update text on the pending modifications popup."""
        self.review_bar.setVisible(has_mods)
        self.status_label.setText(f'{count} file(s) modified and ready for review')

    def append_log(self, message: str) -> None:
        """Helper to accept background thread signal messages."""
        self.log_console.append_log(f'{message}', 1)


###-------------------------------------------- File Browser Signals -------------------------###


class FileBrowserBehavior(QObject):
    """
    Interactive/Behavioral logic for the file browser.
    Manages user interactions, coordinates model updates, dispatches tasks towards
    the background workers, and handles the lifecycle of Editor sessions.
    """

    def __init__(
        self,
        file_browser:   FileBrowserPage,
        editor_page:    EditorPage,
        dispatcher:     Dispatcher,
        metadata_store: NodeMetadataStore,
    ) -> None:
        super().__init__(parent=file_browser)
        self._main_thread_id = threading.get_ident()
        self.view            = file_browser
        self.editor_page     = editor_page
        self.dispatcher      = dispatcher
        self.metadata_store  = metadata_store
        self.window: QWidget | None = self.view.window()

        # Model references
        self.tree_model:     VfsTreeModel | None = None
        self.proxy_model:  TreeProxyModel | None = None
        self._last_selected_node: VfsNode | None = None

        # Setup Invisible Search Buffer
        # Resets after 1.5 seconds of inactivity.
        self.search_buffer = ''
        self.search_timer  = QTimer(self)
        self.search_timer.setInterval(1500)
        self.search_timer.setSingleShot(True)
        self.search_timer.timeout.connect(self.clear_search_buffer)

        # Connect tracker state for file modifications
        self.dispatcher.tracking_update.connect(self.on_tracking_update)
        self.dispatcher.action_complete.connect(self.handle_action_result)

        self._current_session: EditorSession | None = None

        # Bind view signals
        self.view.metadata_panel.metadata_changed.connect(self._on_metadata_changed)

        # Setup all event signals at startup to prevent duplications on soft resets
        self.view.search_results_view.clicked.connect(self._on_search_result_clicked)
        self.view.search_results_view.doubleClicked.connect(self._on_search_double_click)
        self.view.search_results_view.customContextMenuRequested.connect(self.on_search_context_menu)

        self.view.tree_view.customContextMenuRequested.connect(self.on_tree_context_menu)
        self.view.tree_view.doubleClicked.connect(self.handle_tree_double_click)

        self.view.metadata_panel.tagClicked.connect(self.on_tag_clicked)

        # Install event filters at startup to prevent duplications on soft resets
        self.view.installEventFilter(self)
        self.view.tree_view.installEventFilter(self)
        self.view.search_results_view.installEventFilter(self)

    def init_file_tree(self, root_node: VfsNode) -> None:
        """Initializes the models once an ISO is successfully loaded"""
        if not self.dispatcher.vfs:
            raise TypeError("No filesystem currenlty loaded. Can't initialize workspace")
        ### Tree Models
        self.tree_model  = VfsTreeModel(self.dispatcher.vfs)
        self.proxy_model = TreeProxyModel()
        self.proxy_model.setSourceModel(self.tree_model)

        ### State Memory
        self.window: QWidget | None = self.view.window()
        if self.window is not None and hasattr(self.window, 'app_settings'):
            self.proxy_model.set_show_hidden(self.window.app_settings.show_hidden_files)  # type: ignore

        ### Tree View
        self.view.tree_view.setModel(self.proxy_model)  # The model sorts with lessThan

        ### Search / Filter
        self.search_model = FlatSearchModel(self.dispatcher.vfs, self.metadata_store)
        self.view.search_results_view.setModel(self.search_model)

        self.search_overlay = SearchOverlay(self.view.window())

        ### Tree Selection Model
        tree_selection = self.view.tree_view.selectionModel()
        if tree_selection:
            tree_selection.currentChanged.connect(self.handle_tree_select)

        ### Tree headers
        header = self.view.tree_view.header()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
            self.view.tree_view.setColumnWidth(0, 150)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
            self.view.tree_view.setColumnWidth(1, 400)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
            self.view.tree_view.setColumnWidth(2, 85)

        self.view.tree_view.expandToDepth(1)
        self.view.update_review_bar(False, 0)

    def on_tracking_update(self, modified_count: int, staged_count: int) -> None:
        """Controls the apply modifications button visibility"""
        total = modified_count + staged_count
        self.view.review_bar.setVisible(total > 0)
        self.view.status_label.setText(f'{total} modification(s) pending.')

    def _on_layout_ready(self, node: VfsNode) -> None:
        """
        Callback triggered on first event cycle when swapping back to FileBrowserPage.
        Auto-scrolls to the passed node.
        """
        if not self.tree_model or not self.proxy_model:
            return
        source_index = self.tree_model.index_for_node(node)
        if not source_index.isValid:
            return
        proxy_index = self.proxy_model.mapFromSource(source_index)
        if not proxy_index.isValid():
            logger.warning(f'Node {node.name} is not visible in current tree filter')
            return

        self.view.tree_view.expand(proxy_index)
        self.view.tree_view.scrollTo(proxy_index, QAbstractItemView.ScrollHint.PositionAtTop)
        self.view.tree_view.setCurrentIndex(proxy_index)

    def _on_metadata_changed(
        self, node: VfsNode, title: str, description: str, tags: tuple[str, ...]
    ) -> None:
        """Update the metadata store and refresh the panel"""
        hids_str = node.hierarchical_id_str
        node.name = title
        node.category = tags
        self.metadata_store.register(node.address, title=title, description=description, tags=list(tags))
        self.view.metadata_panel.load_node(node, title, description, tags)

    ###----------------- Tree interactions-------------------###

    def handle_tree_select(self, current: QModelIndex, _previous: QModelIndex) -> None:
        """
        Populates the metadata panel when a node is single-clicked in the tree view.
        """
        if not current.isValid() or self.proxy_model is None:
            return
        node: VfsNode | None = self.proxy_model.mapToSource(current).data(Qt.ItemDataRole.UserRole)
        if not node:
            return
        self._last_selected_node = node

        # Load metadata falling back to node properties if none exists
        meta  = self.metadata_store.get(node.address)
        title = meta.title if meta and meta.title else node.name
        desc  = meta.description if meta else ''
        tags  = meta.tags if meta and meta.tags else node.category
        self.view.metadata_panel.load_node(node, title, desc, tags)

        # Attempt to resolve custom format properties via registry
        props_def = Registry.get_action(node, 'Properties')
        if props_def:
            self.dispatcher.execute_node_action(node, 'Properties')
        else:
            self.view.metadata_panel.set_properties_text('-')

    def handle_tree_double_click(self, index: QModelIndex) -> None:
        """
        Route double-click to primary expansion actions (Unpack, Decompress)
        if applicable else to best editor
        """
        if not self.proxy_model:
            return
        node: VfsNode | None = self.proxy_model.mapToSource(index).data(Qt.ItemDataRole.UserRole)
        if not node:
            return
        # Primary expansion Branch
        profiles = Registry.get_handler_profiles(node)
        if profiles:
            for profile in profiles:
                for action_def in profile.actions:
                    if action_def.name in ('Decompress', 'Unpack'):
                        self.route_action(node, action_def)
                        return
        # Best editor branch
        editor_classes = Registry.get_editors(node)
        if editor_classes:
            self.launch_editor(node, editor_classes[0])

    def _on_search_result_clicked(self, index: QModelIndex) -> None:
        """
        Handles single-clicks in the search view.
        Single click on a ghost node will attempt to resolve and
        scroll to it in the tree view.
        """
        entry = self.search_model.data(index, Qt.ItemDataRole.UserRole)
        if entry.node:
            self._handle_goto(entry.node)
        else:  # Ghost node
            self.dispatcher.resolve_ghost_node(key_to_address(entry.address), self._handle_goto)

    def _on_search_double_click(self, index: QModelIndex) -> None:
        """
        Handles double-clicks in the search view.
        Double-click on a registered node will open the best editor.
        Double-click on a ghost node is treated as a single-click.
        """
        if not self.dispatcher.nav or not self.dispatcher.vfs:
            return
        entry = self.search_model.data(index, Qt.ItemDataRole.UserRole)
        node  = entry.node
        if node is None:
            node = self.dispatcher.vfs.get_node(entry.address)
        if node is None:
            logger.warning(f'No node found for address: {entry.address}')
            return
        editor_classes = Registry.get_editors(node)
        if editor_classes:
            self.launch_editor(node, editor_classes[0])

    def on_tag_clicked(self, tag_name: str) -> None:
        """Filters the search proxy by the clicked tag name."""
        self.search_buffer = tag_name
        self.on_search_updated(tag_name)
        self.search_overlay.show_text(f'Tag: {tag_name}')

    def on_search_updated(self, query: str):
        """
        Manages the transition between the tree and search views.
        Snapshot the last selection and queue it to the next tree view event cycle
        so that the user has a consistent anchor point.
        """
        if not query:
            self.view.sidebar_stack.setCurrentIndex(0)
            selection = self._last_selected_node
            if selection is not None:
                QTimer.singleShot(1, lambda: self._on_layout_ready(selection))
            return
        self.view.sidebar_stack.setCurrentIndex(1)
        self.search_model.set_query(query)

    ###---------------------- Context Menu -----------------------###

    def on_search_context_menu(self, position) -> None:
        """Prepares the clicked search result node for the context menu builder."""
        if not self.search_model or not self.dispatcher.nav or not self.dispatcher.vfs:
            return
        index = self.view.search_results_view.indexAt(position)
        if not index.isValid():
            return
        entry = self.search_model.data(index, Qt.ItemDataRole.UserRole)
        node  = entry.node
        address = node.address
        if not node and self.dispatcher.vfs:
            self.dispatcher.resolve_ghost_node(address, self._handle_goto)
            node = self.dispatcher.vfs.get_node(address)
        if not node:
            logger.error(f'No node found for {address}')
            return
        self._build_context_menu(node, self.view.search_results_view.mapToGlobal(position))

    def on_tree_context_menu(self, position) -> None:
        """Prepares the clicked tree node for the context menu builder."""
        if not self.proxy_model:
            return
        proxy_index = self.view.tree_view.indexAt(position)
        if not proxy_index.isValid():
            return
        node: VfsNode | None = self.proxy_model.mapToSource(proxy_index).data(Qt.ItemDataRole.UserRole)
        if not node:
            return
        viewport = self.view.tree_view.viewport()
        if viewport:
            self._build_context_menu(node, viewport.mapToGlobal(position))

    def _build_context_menu(self, node: VfsNode, position) -> None:
        """
        Construct the right-click context menu dynamically based on
        plugins and node state, with ordering.
        """
        menu = QMenu(self.view)

        # Build Editor section (top)
        editor_classes: list[type[BaseEditor]] = Registry.get_editors(node)
        for editor_class in editor_classes:
            plugin_name = getattr(editor_class, '_plugin_name', editor_class.__name__)
            open_action = menu.addAction(f'Open in {plugin_name}')
            if open_action is None:
                continue
            if editor_class is editor_classes[0]:
                font = open_action.font()
                font.setBold(True)
                open_action.setFont(font)
            open_action.triggered.connect(
                lambda checked=False, e=editor_class, n=node: self.launch_editor(n, e)
            )
        menu.addSeparator()

        # Build format-specific actions and global-actions (bottom)
        action_defs: list[ActionDef] = []
        profiles = Registry.get_handler_profiles(node)
        if profiles:
            for profile in profiles:
                action_defs.extend(profile.actions)
        # Filter global actions out of sentinel nodes
        # Hex editor doesn't need to exist for 00 bytes, better to save visual space.
        if not node.is_hidden and node.size > 0:
            action_defs.extend(GLOBAL_ACTIONS)
        # Sort by ActionType priority weights
        action_defs.sort(key=lambda a: _ACTION_TYPE_PRIORETY.get(a.action_type, 99))

        for action_def in action_defs:
            if action_def.name == 'Properties':  # Filter out properties from user (displayed in right panel)
                continue
            qt_action = menu.addAction(action_def.name)
            if qt_action is None:
                continue
            qt_action.triggered.connect(lambda checked=False, d=action_def, n=node: self.route_action(n, d))

        # Provide a quick goto option from the search to the tree view
        if self.view.sidebar_stack.currentIndex() == 1:
            search_action = menu.addAction('Go to in Tree View')
            if search_action is not None:
                search_action.triggered.connect(lambda checked=False, n=node: self._handle_goto(n))

        menu.exec(position)

    def _handle_goto(self, node: VfsNode) -> None:
        """Go to selected search node in tree view"""
        self._last_selected_node = node
        self.view.sidebar_stack.setCurrentIndex(0)
        QTimer.singleShot(1, lambda: self._on_layout_ready(node))

    ###------------------------- Routing ---------------------------###

    def route_action(self, node: VfsNode, action_def: ActionDef) -> None:
        """
        Intercepts actions that require blocking UI before dispatching
        to the background thread. (file dialogs)
        """
        kwargs: dict = {}
        match action_def.action_type:
            case ActionType.EXPORT:
                path, _ = QFileDialog.getSaveFileName(self.view, action_def.name, f'{node.name}{node.extension}', 'All Files (*)')
                if not path:
                    return
                kwargs['file_path'] = Path(path)
            case ActionType.IMPORT:
                path, _ = QFileDialog.getOpenFileName(self.view, action_def.name, '', 'All Files (*)')
                if not path:
                    return
                kwargs['file_path'] = Path(path)
            case ActionType.TREE_EXPAND | ActionType.PROCESS | ActionType.DIALOG:
                pass
            case _:
                logger.error(f'Unhandled ActionType {action_def.action_type} for {action_def.name}')
        self.dispatcher.execute_node_action(node, action_def.name, **kwargs)

    ###----------------------- Post Action ------------------------###

    def handle_action_result(self, result: ActionResult) -> None:
        """Callback receiver for tasks finished on a background worker."""
        if result.status == ActionStatus.FAILURE:
            logger.warning(f'{result.action_name} failed: {result.message}')
            return

        action_def = Registry.get_action(result.node, result.action_name)
        if not action_def:
            logger.debug(f'No ActionDef for action "{result.action_name}"')
            return

        match action_def.action_type:
            case ActionType.DIALOG:
                if result.action_name == 'Properties':
                    if result.payload or result.message:
                        self.view.metadata_panel.set_properties_text(str(result.payload or result.message))
                    else:
                        logger.warning('"Properties" action returned without payload...')
                else:
                    QMessageBox.information(
                        self.view,
                        action_def.name,
                        str(result.payload or result.message),
                    )
            case ActionType.TREE_EXPAND:
                self._on_expand_complete(result)
            case ActionType.PROCESS:
                if isinstance(result.payload, bytes) and result.payload:
                    editor_classes = Registry.get_editors(result.node)
                    if editor_classes:
                        self.launch_editor(result.node, editor_classes[0])
            case ActionType.EXPORT:
                if hasattr(self.window, 'toast'):
                    self.window.toast.show_message(f'{result.message if result.message else result.node.name}') # type: ignore
            case ActionType.IMPORT:
                pass  # Review bar updates automatically for user feedback (on_tracking_update)

    def _on_expand_complete(self, result: ActionResult) -> None:
        """Automatically expand the file tree node when a tree expansion completes"""
        if result.status != ActionStatus.SUCCESS or not self.tree_model or not self.proxy_model:
            return
        if result.action_name == 'Unpack' or hasattr(result, 'node'):
            orig_node = result.node
            source_parent_idx = self.tree_model.index_for_node(orig_node)
            proxy_parent_idx = self.proxy_model.mapFromSource(source_parent_idx)
            if proxy_parent_idx.isValid():
                self.view.tree_view.setExpanded(proxy_parent_idx, True)
                if self.view.sidebar_stack.currentIndex() == 1:  # For search model actions scroll to
                    QTimer.singleShot(0, lambda: self._scroll_to(proxy_parent_idx))

    def _scroll_to(self, proxy_index: QModelIndex) -> None:
        """Utility to navigate the tree view to a specific proxy index."""
        if proxy_index.isValid():
            self.view.sidebar_stack.setCurrentIndex(0)
            self.view.tree_view.scrollTo(proxy_index, QTreeView.ScrollHint.PositionAtTop)
            self.view.tree_view.setCurrentIndex(proxy_index)

    ###------------------- Editor --------------------###

    def launch_editor(self, node: VfsNode, editor_class: type[BaseEditor]) -> None:
        """
        Coordinates the opening of a file into a specific editor UI.
        Manages the EditorSession object which manages state while background workers load data.
        """
        if self._current_session and not self._current_session.is_done():
            self._current_session.cancel()
        new_editor = editor_class(data_resolver=self.dispatcher)

        # Build the dispatch funtion that will close confirm/reject callbacks
        def dispatch_fn(node: VfsNode, data: Any) -> None:
            self.dispatcher.apply_edit(
                node,
                data,
                new_editor,
                on_success=session.confirm_save,
                on_failure=session.reject_save,
            )

        session = EditorSession(node=node, editor=new_editor, dispatch_callback=dispatch_fn)
        self._current_session = session
        new_editor.begin_loading(node)
        self.editor_page.load_editor(session)

        # Open the editor
        if hasattr(self.window, 'stack'):
            from ui.main_window import AppPage

            self.window.stack.setCurrentIndex(AppPage.EDITOR) # type: ignore

        # Start the data processing for the editor
        task_handle = self.dispatcher.open_editor(node, new_editor)
        if not task_handle:
            session.fail('Navigato not initialised.')
            return
        session.set_active_task(task_handle)
        task_handle.finished.connect(functools.partial(self._on_editor_data_ready, session))

        plugin_name = getattr(editor_class, '_plugin_name', editor_class.__name__)
        logger.info(f'Opening "{node.name}" in {plugin_name}')

    def _on_editor_data_ready(self, session: EditorSession, success: bool, payload: Any) -> None:
        """
        Callback that feeds an editor payload from the background thread to the editor.
        Passes through 6 validation guards before injecting data.
        """
        if threading.get_ident() != self._main_thread_id:  # Thread validation
            logger.error('_on_editor_data_ready ran off the main thread')
        if not session.is_active():  # Session state
            logger.debug(f'{session} result discarded - state is {session.state!r}')
            if sip.isdeleted(session.editor):
                return
            session.editor.cleanup()
            return
        if session is not self._current_session:  # Session currency
            logger.debug(f'{session} discarded - superseded by newer session')
            session.cancel()
            if sip.isdeleted(session.editor):
                return
            session.editor.cleanup()
            return
        if not success:  # Task success
            session.fail(str(payload))
            return
        if not isinstance(payload, EditorPayload):  # Payload type
            session.fail(f'Unexpected payload type: {type(payload).__name__} (expected EditorPayload)')
            return
        if payload.node is not session.node:  # Node Identity
            session.fail(
                f'Payload node mismatch - received data for "{payload.node.name}", expected "{session.node.name}"'
            )
            return
        # Data passed all guards, finalize the editor initialization
        session.complete(payload.data)
        logger.debug(f'{session} populated successfully.')

    ###-------------------- Search --------------------###

    def eventFilter(self, obj: QObject, event: QKeyEvent) -> bool:
        """
        Intercepts raw keystrokes globally on the FileBrowserPage to drive the search feature.
        Ignoring certain key combos so shortcuts are still valid.
        """
        if event.type() == QEvent.Type.KeyPress:
            key_event: QKeyEvent = event
            # Ignore keyboard events when modifiers are held
            if key_event.modifiers() & (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.AltModifier
                | Qt.KeyboardModifier.MetaModifier
            ):
                return super().eventFilter(obj, event)

            if key_event.key() == Qt.Key.Key_Escape:  # Esc
                self.search_buffer = ''
                self.on_search_updated('')
                self.search_overlay.hide_overlay()
                self.search_timer.stop()
                return True

            if key_event.key() == Qt.Key.Key_Backspace:  # Backspace
                if self.view.sidebar_stack.currentIndex() == 1:
                    # If the overlay is not visible or the buffer is empty
                    # treat the backspace exactly the same as pressing Escape
                    if not self.search_overlay.isVisible() or not self.search_buffer:
                        self.search_buffer = ''
                        self.on_search_updated('')
                        self.search_overlay.hide_overlay()
                        self.search_timer.stop()
                        return True
                    # Otherwise, treat it as a regular backspace
                    self.search_buffer = self.search_buffer[:-1]
                    if self.search_buffer == '':
                        self.on_search_updated('')
                        self.search_overlay.hide_overlay()
                        self.search_timer.stop()
                        return True
                    self.on_search_updated(self.search_buffer)
                    self.search_overlay.show_text(self.search_buffer)
                    self.search_timer.start()
                    return True

            text = key_event.text()
            if len(text) > 0 and text.isprintable():  # Ensure Printable
                self.search_buffer += text
                self.on_search_updated(self.search_buffer)
                self.search_overlay.show_text(self.search_buffer)
                self.search_timer.start()
                return True
        return super().eventFilter(obj, event)

    def clear_search_buffer(self) -> None:
        """
        Clears the search buffer. To reset proxy "Esc" in eventFilter.
        Alternatively erasing all text will clear from the eventFilter itself.
        """
        self.search_buffer = ''
        self.search_model._query = ''
        self.search_overlay.hide_overlay()


###----------------------------------- Metadata Panel ------------------------------------###


class FileMetadataPanel(QWidget):
    """
    Displays properties, tags, general file info, and user notes for the currently selected file.
    Utilizes a QStackedWidget to seamlessly swap between the read-only complete panel and editable metadata.
    """

    metadata_changed = pyqtSignal(object, str, str, tuple)  # (node, title, description, tags)
    tagClicked = pyqtSignal(str)  # tag str

    def __init__(
        self,
        metadata_store: NodeMetadataStore,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = metadata_store
        self._current_node: VfsNode | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        """Contructs the base layout and the QStackedWidget pages."""
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())
        root.addWidget(hline())

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_view_page())
        self._stack.addWidget(self._build_edit_page())
        root.addWidget(self._stack)

    def _build_header(self) -> QWidget:
        """Builds the static top bar containing the file title and edit button."""
        bar = QWidget()
        bar.setObjectName('SurfaceToolbar')
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 6, 8, 6)

        self._name_label = QLabel('No file selected')
        self._name_label.setObjectName('TextTitle')
        self._hid_label = QLabel('')
        self._hid_label.setObjectName('TextHeader')

        name_col = QVBoxLayout()
        name_col.setSpacing(1)
        name_col.addWidget(self._name_label)
        name_col.addWidget(self._hid_label)

        self._edit_btn = QPushButton('Edit')
        self._edit_btn.setObjectName('TextTitle')
        self._edit_btn.setFixedWidth(70)
        self._edit_btn.setEnabled(False)
        self._edit_btn.clicked.connect(self._enter_edit_mode)

        layout.addLayout(name_col, stretch=1)
        layout.addWidget(self._edit_btn)
        return bar

    def _build_view_page(self) -> QWidget:
        """Builds the read-only panel UI, defers filling in the data."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        ### Tags
        self._tags_row = QHBoxLayout()
        self._tags_row.setSpacing(4)
        self._tags_row.setContentsMargins(0, 0, 0, 0)
        tags_wrap = QWidget()
        tags_wrap.setLayout(self._tags_row)
        layout.addWidget(tags_wrap)
        ### Description
        self._desc_label = QLabel()
        self._desc_label.setWordWrap(True)
        self._desc_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._desc_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._desc_label)
        layout.addWidget(hline())
        ### Properties
        props_header = QLabel('Properties')
        props_header.setObjectName('TextHeader')
        self._props_label = QLabel('-')
        self._props_label.setWordWrap(True)
        self._props_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(props_header)
        layout.addWidget(self._props_label)
        layout.addWidget(hline())
        ### File Info
        info_header = QLabel('File Info')
        info_header.setObjectName('TextHeader')
        self._info_container = QWidget()
        self._info_layout = QVBoxLayout(self._info_container)
        self._info_layout.setContentsMargins(0, 0, 0, 0)
        self._info_layout.setSpacing(2)
        layout.addWidget(info_header)
        layout.addWidget(self._info_container)
        layout.addStretch()

        scroll.setWidget(content)
        return scroll

    def _build_edit_page(self) -> QWidget:
        """Builds the editable form inputs for modifying metadata."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        layout.addWidget(QLabel('Title'))
        self._title_edit = QLineEdit()
        self._title_edit.setPlaceholderText('Display name...')
        layout.addWidget(self._title_edit)

        layout.addWidget(QLabel('Tags (comma-separated)'))
        self._tags_edit = QLineEdit()
        self._tags_edit.setPlaceholderText('ex. Character, Texture, System...')
        layout.addWidget(self._tags_edit)

        layout.addWidget(QLabel('Description'))
        self._desc_edit = QTextEdit()
        self._desc_edit.setPlaceholderText('Notes...')
        self._desc_edit.setFixedHeight(90)
        layout.addWidget(self._desc_edit)

        btn_row = QHBoxLayout()
        self._save_btn = QPushButton('Save')
        self._cancel_btn = QPushButton('Cancel')
        self._save_btn.setObjectName('BtnImportant')
        self._save_btn.clicked.connect(self._on_save)
        self._cancel_btn.clicked.connect(self._exit_edit_mode)
        btn_row.addStretch()
        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(self._save_btn)
        layout.addLayout(btn_row)
        layout.addStretch()

        return page

    def load_node(self, node: VfsNode, title: str, description: str, tags: tuple[str, ...]) -> None:
        """Populate the panel data from the given VfsNode and its metadata."""
        self._current_node = node

        self._name_label.setText(title or node.name)
        self._hid_label.setText(address_to_key(node.address))
        self._edit_btn.setEnabled(True)

        self._clear_layout(self._tags_row)
        pill_source = tags if tags else node.category
        for tag in pill_source:
            if not tag:
                continue
            pill = _ClickableTag(tag)
            pill.tagClicked.connect(self.tagClicked.emit)
            self._tags_row.addWidget(pill)
        self._tags_row.addStretch()

        self._desc_label.setText(description or 'No description')
        self._props_label.setText('-')
        self._clear_layout(self._info_layout)
        for label, value in [
            ('Size', human_size(node.size)),
            ('Offset', hex(node.offset) if node.offset else '-'),
            ('Physical', 'Yes' if node.is_physical else 'No'),
            ('Links', ', '.join(address_to_key(link) for link in node.links) or '-'),
        ]:
            self._info_layout.addWidget(self._info_row(label, value))

        self._exit_edit_mode()

    def _info_row(self, label: str, value: str) -> QWidget:
        """Helper for generating key/value pair rows in the file info section."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        key = QLabel(label)
        key.setObjectName('TextHeader')
        val = QLabel(value)
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(key)
        layout.addStretch()
        layout.addWidget(val)
        return row

    def set_properties_text(self, text: str) -> None:
        """Called asynchronously by FileBrowserBehavior when Properties action completes."""
        self._props_label.setText(text or '-')

    def clear(self) -> None:
        """Resets the UI back to default placeholder state."""
        self._current_node = None
        self._name_label.setText('No file selected')
        self._hid_label.setText('')
        self._desc_label.setText('')
        self._props_label.setText('-')
        self._edit_btn.setEnabled(False)
        self._clear_layout(self._tags_row)
        self._clear_layout(self._info_layout)
        self._exit_edit_mode()

    def _clear_layout(self, layout) -> None:
        """Recursively cleans up all child widgets and sub-layouts for a QLayout."""
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            if item.layout():
                self._clear_layout(item.layout())

    @property
    def current_node(self) -> VfsNode | None:
        return self._current_node

    def _enter_edit_mode(self) -> None:
        """Transfers the current UI text into the form inputs and swaps the stacked widget."""
        if not self._current_node:
            return
        self._title_edit.setText(self._name_label.text())
        # Reconstruct comma-separated strings from the tag UI pills
        pills: list[_ClickableTag] = [
            widget
            for i in range(self._tags_row.count())
            if (item := self._tags_row.itemAt(i)) is not None
            and isinstance(widget := item.widget(), _ClickableTag)
        ]
        self._tags_edit.setText(', '.join(p.text() for p in pills))
        self._desc_edit.setPlainText(
            self._desc_label.text() if self._desc_label.text() != 'No description.' else ''
        )
        self._edit_btn.setEnabled(False)
        self._stack.setCurrentIndex(1)

    def _exit_edit_mode(self) -> None:
        """Re-enables edit button and swaps view stack back to read-only."""
        self._edit_btn.setEnabled(self._current_node is not None)
        self._stack.setCurrentIndex(0)

    def _on_save(self) -> None:
        """Emits the form contents via signal to save the edits."""
        if not self._current_node:
            return
        title = self._title_edit.text().strip()
        desc  = self._desc_edit.toPlainText().strip()
        tags  = tuple(t.strip() for t in self._tags_edit.text().split(',') if t.strip())
        self.metadata_changed.emit(self._current_node, title, desc, tags)


class _ClickableTag(QLabel):
    """
    Interactive UI pill for file tags/categories. Emits tag string when clicked.
    Tag string is recieved by the search overlay for instant filtering.
    """

    tagClicked = pyqtSignal(str)

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName('BtnSurface')
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, ev: QMouseEvent | None) -> None:
        if ev and ev.button() == Qt.MouseButton.LeftButton:
            self.tagClicked.emit(self.text())


###------------------------------------------------- Search Overlay ---------------------------------------------------------###


class SearchOverlay(QLabel):
    """
    Floating centered text overlay that provides visual feedback for the invisible search.
    Automatically fades out and hids itself when idle.
    """

    def __init__(self, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setObjectName('TextOverlay')
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.opacity = 0.0

        # Timer for the fade-out animation
        self.fade_timer = QTimer(self)
        self.fade_timer.setInterval(50)
        self.fade_timer.timeout.connect(self._fade_step)

        # Triggers the fade-out after idle
        self.idle_timer = QTimer(self)
        self.idle_timer.setInterval(300)
        self.idle_timer.setSingleShot(True)
        self.idle_timer.timeout.connect(self.fade_timer.start)

        self.hide()

    def _apply_opacity(self) -> None:
        """Bake current opacity alpha into the widget stylesheet.

        Replaces the old QGraphicsOpacityEffect approach which forced offscreen
        rasterization and caused Windows repaint artifacts.  The background is
        kept fully transparent; only the text colour carries the alpha.
        """
        c = QColor(ThemeManager.active_theme.TEXT)
        alpha = int(self.opacity * 255)
        self.setStyleSheet(
            f'#TextOverlay {{'
            f'color: rgba({c.red()}, {c.green()}, {c.blue()}, {alpha});'
            f'background-color: rgba(0, 0, 0, 0);'
            f'font-weight: bold;'
            f'font-size: 42px;'
            f'}}'
        )

    def show_text(self, text: str) -> None:
        """Updates the text and immediately forces the widget to maximum opacity."""
        if not text:
            self.hide_overlay()
            return

        self.setText(text)
        self.opacity = 0.80
        self._apply_opacity()
        self.adjustSize()

        # Calculates the center of the window
        if self.parentWidget():
            parent = self.parentWidget()
            if parent:
                geo = parent.geometry()
                self.move(
                    (geo.width() - self.width()) // 2,
                    (geo.height() - self.height()) // 2,
                )
        self.show()
        self.fade_timer.stop()
        self.idle_timer.start()

    def _fade_step(self) -> None:
        """Decreases alpha linearly. Called via timer until fully transparent."""
        self.opacity -= 0.05
        if self.opacity <= 0:
            self.opacity = 0
            self.fade_timer.stop()
            self.hide()
        self._apply_opacity()

    def hide_overlay(self) -> None:
        """Hard stops animations and immediately hides widget."""
        self.opacity = 0
        self.fade_timer.stop()
        self.idle_timer.stop()
        self.hide()


###------------------------------------------- Toast Notification ------------------------------------###


class ToastNotification(QLabel):
    """
    Bottom-right popup alert for non-blocking user feedback.
    Animates upward upon creation and recedes downwards automatically.
    """

    def __init__(self, parent, message: str, duration_ms: int = 3000):
        super().__init__(parent)
        self.setText(message)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft)

        # Setup floating window behavior
        self.setWindowFlags(Qt.WindowType.SubWindow | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setObjectName('Popup')

        # Visual elements
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(10)
        shadow.setColor(Qt.GlobalColor.black)
        shadow.setOffset(0, 3)
        self.setGraphicsEffect(shadow)

        self.adjustSize()
        self.target_pos, self.hidden_pos = self._calculate_positions()
        self.move(self.hidden_pos)
        self.show()

        self._slide_up(duration_ms)

    def _calculate_positions(self) -> tuple[QPoint, QPoint]:
        """Determines the display anchor and off-screen anchor points based on parent geometry."""
        parent = self.parentWidget()
        if not parent:
            return QPoint(0, 0), QPoint(0, 0)

        parent_rect = parent.rect()
        margin_right = 25
        margin_bottom = 50
        x = parent_rect.width() - self.width() - margin_right
        y = parent_rect.height() - self.height() - margin_bottom
        target_pos = QPoint(x, y)
        hidden_pos = QPoint(x, parent_rect.height())
        return target_pos, hidden_pos

    def _slide_up(self, display_duration: int) -> None:
        """Animates the toast upward with a smooth deceleration curve"""
        self.anim = QPropertyAnimation(self, b'pos')
        self.anim.setDuration(350)
        self.anim.setStartValue(self.hidden_pos)
        self.anim.setEndValue(self.target_pos)
        self.anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.anim.start()

        self.dismiss_timer = QTimer(self)
        self.dismiss_timer.setSingleShot(True)
        self.dismiss_timer.timeout.connect(self._slide_down)
        self.dismiss_timer.start(display_duration)

    def _slide_down(self) -> None:
        """Animates the toast downward and cleans up the object."""
        self.anim = QPropertyAnimation(self, b'pos')
        self.anim.setDuration(300)
        self.anim.setStartValue(self.target_pos)
        self.anim.setEndValue(self.hidden_pos)
        self.anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self.anim.finished.connect(self.close)
        self.anim.start()
