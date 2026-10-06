'''
Dispatcher handles most of the coordination work between the UI and logic
Functions as a signal proxy
'''
from __future__ import annotations

import functools
import threading
import platform
from enum import Enum, auto
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable
from PyQt6.QtCore import pyqtSignal, QObject, Qt, QTimer
from PyQt6.QtWidgets import QWidget, QFileDialog

from core.registry import Registry
from core.node import VfsManager, ModTracker, VfsNode, NodeConflictError
from core.workers import (
    TaskCoordinator, ActionStatus, ActionResult, Actions, ActionType, TaskHandle, LogChannel,
    ActionDef, EditorPayload
)
from core.contracts import PackageIntent, BasePatch, BaseVirtualPatch
from core.native.block_device import BlockDevice
from core.navigator import VfsNavigator
from core.metadata_manager import NodeMetadataStore

if TYPE_CHECKING:
    from core.contracts import BaseEditor, SourceRebuildFlags
    from core.handlers.iso_container import SourceHandler

import logging
logger = logging.getLogger(f'radiata.{__name__}')

class ConflictChoice(Enum):
    '''
    UI-facing resolution for a NodeConflictError, chosen via
    ConflictResolverDialog. ModTracker only detects and raises.
    Dispatcher is what interprets a choice and acts on it using
    ModTracker's API (revert_node+retry, or apply_modification(force=True)).
    '''
    KEEP_OLD  = auto()
    KEEP_NEW  = auto()
    KEEP_BOTH = auto()

###------------------------------------------------- Task signal relay ---------------------------------------------------###

class TaskRelay(QObject):
    '''
    Own the signal wiring for generic tasks, routing to the appropriate page.
    Not for dedicated tasks like rebuild.
    '''
    log      = pyqtSignal(LogChannel, str)       # (channel, message)
    progress = pyqtSignal(LogChannel, int, str)  # (channel, percent, *label)
    def track(self, handle: TaskHandle) -> TaskHandle:
        '''Attach the handle's generic signals to the relay, routed by the channel/label. Return handle for chaining'''
        handle.log_message.connect(lambda msg: self.log.emit(handle.channel, msg))
        handle.progress.connect(lambda pct: self.progress.emit(handle.channel, pct, handle.label))
        return handle

###----------------------------------------------------- Dispatch -------------------------------------------------###

class Dispatcher(QObject):
    '''
    Bridge between UI and logic

    Node actions pass to Actions.dispatch which routes by ActionDef.action_type.
    Dispatcher does not need to now what an action needs to execute.
    '''
    # Tree / Tracker
    source_loaded        = pyqtSignal(bool, object)   # (success, result[root | error_msg])
    source_closed        = pyqtSignal()               # Source torn down
    expand_requested  = pyqtSignal(VfsNode, object)   # (VfsNode, wait_event)
    tracking_update   = pyqtSignal(int, int)          # (modified_count, staged_count)
    conflict_prompt   = pyqtSignal(VfsNode, str, str) # (new_node, other node(s), reason)
    # ISO verification
    source_verified = pyqtSignal(str)                   # Build string. Used for get_build and verify_source with the difference being specificity
    # Generic node actions
    action_complete  = pyqtSignal(ActionResult)      # ActionResult
    # Metadata inconsistencies
    ghost_node_confirmed_missing = pyqtSignal(tuple) # hids

    def __init__(self) -> None:
        super().__init__()
        self._main_thread_id = threading.get_ident()
        self.vfs:             VfsManager | None = None
        self.active_handler:  SourceHandler | None = None
        self.nav:             VfsNavigator | None = None
        self._metadata_store: NodeMetadataStore | None = None
        self.tracker          = ModTracker()
        self.task_coordinator = TaskCoordinator()
        self.relay            = TaskRelay()
        self._active_channel  = LogChannel.BROWSER
        self._pending_conflict_choice: ConflictChoice | None = None
        self._setup_connections()

    def _setup_connections(self) -> None:
        '''Relay tracker signals to UI'''
        self.tracker.state_changed.connect(self._relay_tracking_state)
        self.expand_requested.connect(self._handle_expand_requested, Qt.ConnectionType.QueuedConnection) # type: ignore this is valid
        self.ghost_node_confirmed_missing.connect(self._handle_ghost_node_confirmed_missing, Qt.ConnectionType.QueuedConnection) # type: ignore this is valid

    def set_active_channel(self, channel: LogChannel) -> None:
        '''Set the log channel to a new log window. Must be manually reset to browser after task.'''
        self._active_channel = channel

    def _start(
        self,
        fn: Callable,
        *args,
        label: str = '',
        channel: LogChannel | None = None,
        **kwargs
    ) -> TaskHandle:
        '''Wires the appropriate signal to channel during task startup.'''
        handle = self.task_coordinator.start_task(fn, *args, channel=channel or self._active_channel, label=label, **kwargs)
        return self.relay.track(handle)

    def finalize_rebuild(self, success: bool) -> None:
        '''Rebuild endpoint. cleanup/reset the dispatcher rebuild related states'''
        self.set_active_channel(LogChannel.BROWSER)
        if self.nav:
            self.nav.clear_rollup_pending()
        if success:
            self.tracker.clear()

    def get_patch_options(self) -> tuple[BasePatch, ...]:
        '''User-toggleable patch options for the active source.'''
        if not self.active_handler:
            return ()
        return self.active_handler.source.patch_options

    def rebuild_flags_class(self) -> type[SourceRebuildFlags] | None:
        '''The flags class for the active source so callers can build a flag value without the source.'''
        if not self.active_handler:
            return None
        return self.active_handler.source.rebuild_flags

    def compose_build_flags(self, selected: Iterable[SourceRebuildFlags] =()) -> SourceRebuildFlags | None:
        '''Combine the selected patch flags into one build flag value'''
        flags_cls = self.rebuild_flags_class()
        if flags_cls is None:
            return None
        flags = flags_cls(0)
        for flag in selected:
            flags |= flag
        return flags

    def set_metadata_store(self, store: NodeMetadataStore) -> None:
        self._metadata_store = store

    def _relay_tracking_state(self):
        '''Emit counts so UI doesn't need to recalc'''
        self.tracking_update.emit(len(self.tracker.modified_nodes), len(self.tracker.rebuild_queue))

    def __str__(self) -> str:
        return f"Dispatcher(active_handler={self.active_handler})"

    ###----------------------------------- Public ----------------------------------------###

    def load_source(self, source: Path | VfsNode) -> TaskHandle | list[VfsNode] | None:
        '''Route a block file or VfsNode to the appropriate handler.'''
        if isinstance(source, Path):
            handler_class = Registry.get_handler(source)
            from core.contracts import PhysicalHandler
            from core.handlers.iso_container import SourceHandler
            if (
                not handler_class or
                not (isinstance(handler_class, type) and issubclass(handler_class, PhysicalHandler)) or
                not (isinstance(handler_class, type) and issubclass(handler_class, SourceHandler))
            ):
                logger.warning(f'No handler for {source.name}, {handler_class}')
                return
            resolved = resolve_raw_disc_device(source)
            source_stream = BlockDevice(str(resolved), sector_size=2048)
            handler = handler_class(source_stream)
            return self._load_physical(handler)

        if source.children:
            return source.children

        if not self.nav:
            logger.warning(f'No navigator, cannot expand {source.name}.')
            return None

        self.nav.request_expansion(source)
        return [] # node_changed signal populates the tree

    def get_node_data(self, node: VfsNode) -> bytes:
        '''Return the raw bytes of the requested node, unwrapping virtual to the physical source.
        Actual tree navigation/resolution happens in VfsNavigator.'''
        if node.pending_data is not None:
            return node.pending_data
        if node.is_physical:
            handler = self.active_handler
            if handler is None:
                logger.error(f'No physical handler for: {node}')
                return b''
            return handler.get_raw_node(node)
        if self.nav is None:
            logger.error(f'No VfsNavigator initialized. Cannot resolve node: {node}')
            return b''
        return self.nav.unwrap_chain(node)

    def _apply_modifications_with_conflict_prompt(
        self,
        node:         VfsNode,
        new_data:     bytes,
        data_sources: Callable[[VfsNode], bytes]
    ) -> bytes | None:
        '''
        Wraps ModTracker.apply_modification with conflict resolution: on
        NodeConflictError, emits conflict_prompt and blocks for a decision,
        then acts on it using only ModTracker's public API.
        '''
        assert threading.get_ident() == self._main_thread_id, (
            '_apply_modification_with_conflict_prompt relies on a same-thread, '
            'direct signal connection to block for the UI\'s answer.'
        )
        try:
            return self.tracker.apply_modification(node, new_data, data_sources)
        except NodeConflictError as conflict:
            self._pending_conflict_choice = None
            self.conflict_prompt.emit(conflict.node, conflict.others_str, conflict.reason)
            choice = self._pending_conflict_choice
            self._pending_conflict_choice = None
            if choice is None:
                logger.error(f'No conflict resolution received for {node}: doing nothing.')
                return None
            if choice == ConflictChoice.KEEP_OLD:
                logger.info(f'Conflict on {node}: kept {len(conflict.others)} existing modification(s), new edit discarded.')
                return None
            if choice == ConflictChoice.KEEP_NEW:
                logger.info(f'Conflict on {node}: importing new data, discarding pending edits on: {conflict.others}.')
                for _node in conflict.others:
                    self.tracker.revert_node(_node)
                for child in node.children_snapshot:
                    self.vfs.remove_node(child)
                return self.tracker.apply_modification(node, new_data, data_sources)
            logger.warning(f'Conflict on {node}: keeping both, will be flagged during staging.')
            return self.tracker.apply_modification(node, new_data, data_sources, force=True)

    def resolve_conflict_choice(self, node: VfsNode, choice: ConflictChoice) -> None:
        '''Connected to MainWindow.conflict_choice_made. Recorde the choice.'''
        self._pending_conflict_choice = choice

    def apply_edit(
        self,
        node: VfsNode,
        data: Any,
        editor: BaseEditor | None = None,
        on_success: Callable[[], None]    | None = None,
        on_failure: Callable[[str], None] | None = None,
    ) -> None | TaskHandle:
        '''Pushes changes to the tracker.
        If data is not bytes, dispatches to a background worker to decode the payload
        Notifies the editor when finished'''
        if isinstance(data, bytes):
            # Doesn't go through an editor, this is a much more dangerous mutation which
            # can invalidate children and corrupt the tree. With the added complexity of
            # datacenter headers newly imported data should also be parsed upon entry to
            # ensure dependent nodes are also updated to match the new payload.
            previous_content = self._apply_modifications_with_conflict_prompt(node, data, self.get_node_data)
            if not previous_content:
                return
            logger.info(f'Modified: "{node.name}" added to rebuild queue.')
            if on_success:
                on_success()
            return

        handler_class = (
            Registry.get_handler_for_editor(editor) if editor is not None else
            Registry.get_handler(node)
        )
        if not handler_class:
            error_msg = f'No handler registered for "{node.name}" to compile payload.'
            logger.error(error_msg)
            if on_failure:
                on_failure(error_msg)
            return
        # Start task
        task_handle = self._start(
            Actions.decode_editor_data,
            handler_class,
            node,
            data
        )
        task_handle.finished.connect(functools.partial(self._on_decode_done, node, on_success, on_failure))
        return task_handle

    def open_editor(self, node: VfsNode, editor: BaseEditor) -> TaskHandle | None:
        '''
        Start background data preparation for an editor

        Returns a TaskSignal of either:
            Success   - EditorPayload(node, data)
            Exception - (False, str)
        '''
        if not self.nav:
            logger.error('Navigator not initialised')
            return None
        handler_class = (
            Registry.get_handler_for_editor(editor)
            or Registry.get_handler(node)
        )
        if not handler_class:
            logger.warning(f'No handler for {node.name} - Cannot prepare editor data')
            return None
        logger.debug(
            f'open_editor: node={node.name}'
            f'editor={editor.__class__.__name__}'
            f'handler={handler_class.__name__}'
        )
        # Start task
        task_handle = self._start(
            Actions.prepare_editor,
            handler_class,
            node,
            self.nav,
        )
        return task_handle

    def execute_node_action(self, node: VfsNode, action_name: str, **kwargs) -> None:
        '''Route action through Actions.dispatch or the appropriate Action.*custom_action*'''
        if not self.nav:
            logger.error('Navigator not initialised')
            return
        action_def = Registry.get_action(node, action_name)
        if not action_def:
            self.action_complete.emit(ActionResult(
                action_name=action_name,
                node=node,
                status=ActionStatus.FAILURE,
                message=f'No ActionDef registered for "{action_name}" on node: {node}'
            ))
            return

        if action_def.action_type is ActionType.TREE_EXPAND:
            if node.last_expansion_success is not None:  # Already expanded (dedup)
                self.action_complete.emit(ActionResult(
                    action_name=action_name,
                    node=node,
                    status=ActionStatus.FAILURE,
                    message=f'{node.name} has already been expanded previously. Duplicate expansion cancelled.'
                ))
                return
            self.nav.request_expansion(node, lambda success, _node: None)
            return

        if action_def.action_type is ActionType.IMPORT and self.nav and self.nav.package_members(node, PackageIntent.IMPORT):
            self._execute_package_import(node, action_def, **kwargs)
            return

        # Standard Action
        task_handle = self._start(
            Actions.dispatch,
            action_def,
            node,
            self.nav,
            channel=LogChannel.TOAST if action_def.action_type is ActionType.EXPORT else None,
            label=action_def.name,
            **kwargs
        )
        task_handle.finished.connect(self._on_action_complete)

    def _execute_package_import(self, node: VfsNode, action_def: ActionDef, **kwargs) -> None:
        '''Package import helper: resolves unresolved nodes needed for import and start the worker.'''
        if self.vfs is None or not node.target or not self.nav:
            return
        self.vfs.remove_node_children(node)
        handler_class = Registry.get_handler(node)
        def _start_import() -> None:
            task_handle = self._start(
                Actions.package_import,
                node,
                handler_class,
                self.nav,
                self.tracker,
                self.get_node_data,
                channel=LogChannel.TOAST,
                label=action_def.name,
                **kwargs
            )
            task_handle.finished.connect(self._on_action_complete)
        self.nav.ensure_package(node, PackageIntent.IMPORT, _start_import)

    def start_iso_rebuild(self, request: RebuildRequest, output_path: Path) -> TaskHandle | None:
        '''
        Starts the actual rebuild task.
        Routing the logic appropriately for the requests.
        Called by RebuildCoordinator.
        '''
        if not self.active_handler or not self.vfs or not self.nav:
            logger.error('Cannot start rebuild: No ISO loaded.')
            return None
        logger.info(f'Preparing to build {len(request.staged_nodes)} staged files(s)')
        # Use start_dedicated_task (not start_task) so the rebuild runs on a
        # dedicated thread outside the bounded QThreadPool.  The rebuild blocks
        # waiting for expansion tasks that must themselves run on the pool; if
        # the rebuild occupied a pool slot it could exhaust the pool and deadlock.
        task_handle = self.task_coordinator.start_dedicated_task(
            Actions.rebuild_iso,
            self.active_handler,
            self.vfs.root,
            self.nav,
            request.staged_nodes,
            output_path,
            request.build_flags,
            request.patch_targets,
            channel=LogChannel.REBUILD,  # Rebuild is always rebuild page
        )
        return task_handle

    def request_editor_payload(
        self,
        hid: tuple[int, ...],
        callback: Callable[[Any], None]
    ) -> None:
        '''Called by an active editor to request an editor payload for a given node hid.'''
        if not self.nav:
            callback(None)
            return
        task_handle = self._start(
            Actions.fetch_for_editor,
            hid,
            self.nav,
        )
        def _on_finished(success: bool, payload: Any) -> None:
            task_handle.finished.disconnect(_on_finished)
            callback(payload if success else None)
        task_handle.finished.connect(_on_finished)

    def request_raw_data(
        self,
        hid: tuple[int, ...],
        callback: Callable[[Any], None]
    ) -> None:
        '''Called by an active editor to request raw data for a given node hid.'''
        if not self.nav or not self.vfs:
            callback(None)
            return
        data = self.nav.resolve_data_from_hid(hid) # Ensure data is resolved before looking up the node
        node = self.vfs.get_vfs_node_by_id(hid)
        if node is not None and data is not None:
            payload = EditorPayload(node=node, data=data)
            callback(payload)
        else:
            logger.error(f'Failed to resolve data or node for hid: {hid}')
            callback(None)

    ###------------------------------- VFS node resolution passthroughs -------------------------------###

    def resolve_and_unpack_all(self, target_hid: tuple[int, ...], on_success: Callable[[list[VfsNode]], None]) -> None:
        '''Asynchronous entrypoint for unpack recursively untill no more ActionType.TREE_EXPAND actionable nodes remain.'''
        if not self.nav:
            logger.error('Navigator not initialised')
            return
        self.nav.unpack_recursive(target_hid, on_success)

    def resolve_ghost_node(self, target_hid: tuple[int, ...], on_success: Callable[[VfsNode], None]) -> None:
        '''Asynchronous entrypoint for unpacking the VFS until the target hid is reached'''
        if not self.nav:
            logger.error('Navigator not initialised')
            return
        self.nav.resolve_ghost_node(target_hid, on_success)

    def close(self) -> None:
        '''For exiting the dispatch. Refuses to tear down on failed task shutdown.'''
        fully_stopped = self.task_coordinator.shutdown()
        if not fully_stopped:
            logger.error(
                'Dispatcher.close(): a dedicated task did not stop in time. '
                'Aborting shutdown.'
            )
            return
        if self.active_handler:
            self.active_handler.close()
        self.vfs            = None
        self.active_handler = None
        self.nav            = None
        self.tracker.configure_source(None, None)
        self.tracker.clear()
        logger.debug('- File System Reset -')
        self.source_closed.emit()

    ###------------------------------ Helpers --------------------------------###

    def _load_physical(self, handler: SourceHandler) -> TaskHandle | None:
        '''Send ISO loading to a worker thread'''
        if self.active_handler:
            self.active_handler.close()
        self.active_handler = handler
        if not self._metadata_store:
            logger.debug(f'No file metadata loaded... {self._metadata_store}')
        task_handle = self._start(
            Actions.load_source,
            handler,
            channel=LogChannel.BROWSER
        )
        task_handle.finished.connect(self._on_source_loaded)
        return task_handle

    ###------------------------ Callbacks and Signals -----------------------###
    def _on_expand_requested(self, parent: VfsNode, wait_event: threading.Event) -> None:
        '''
        Cross-thread callback for VfsNavigator. Blocking on node.begin_expansion()
        Coordinates expansion on the main thread via QueuedConnection.
        '''
        self.expand_requested.emit(parent, wait_event)

    def _handle_expand_requested(self, node: VfsNode, wait_event: threading.Event) -> None:
        '''
        QueuedConnection slot for expand_requested. Runs on the main thread.
        Starts the actual TREE_EXPAND task for `node` and reports completion
        back to Navigator via complete_expansion(), which releases wait_event
        and drains every waiter queued while this task was in flight.
        '''
        profile = Registry.get_handler_profile(node)
        action_def = profile.primary_expand_action() if profile else None
        if not action_def or action_def.action_type is not ActionType.TREE_EXPAND:
            logger.warning(f'_handle_expand_requested called with non-TREE_EXPAND action: {action_def}')
            if self.nav:
                self.nav.complete_expansion(node, True)
            return
        logger.debug(f'Expanding: {node}')
        task_handle = self._start(
            Actions.dispatch,
            action_def,
            node,
            self.nav,
            channel=self._active_channel
        )
        task_handle.finished.connect(functools.partial(self._on_expansion_task_done, node))

    def _on_expansion_task_done(self, node: VfsNode, success: bool, result: Any) -> None:
        '''
        Completion slot for the TREE_EXPAND task started in
        _handle_expand_requested. Context param (node) bound via
        functools.partial; signal params (success, result) appended by Qt
        when emitted.
        '''
        if threading.get_ident() != self._main_thread_id:
            logger.error('_on_expansion_task_done called from non-main thread')
        self._on_action_complete(success, result)  # inserts children into VFS
        if not success:
            logger.error(f'Expansion failed for {node}: {result}')
        if self.nav:
            # Runs after on_action_complete so waiters see node.children
            self.nav.complete_expansion(node, success)

    def _on_ghost_node_confirmed_missing(self, hid: tuple[int, ...]) -> None:
        '''VfsNavigator callback for invalid metadata entries. Triggering deletion.'''
        self.ghost_node_confirmed_missing.emit(hid)

    def _handle_ghost_node_confirmed_missing(self, hid: tuple[int, ...]) -> None:
        '''
        QueuedConnection slot for ghost_node_confirmed_missing. Runs on the main thread.
        Purges a metadata entry that Navigator has conclusively proven does not exist.
        '''
        if self._metadata_store is None:
            return
        hid_str = '.'.join(map(str, hid))
        logger.warning(f'Purging stale metadata entry for confirmed-missing node: {hid_str}')
        self._metadata_store.delete(hid_str)

    def _handle_extension_request(self, node: VfsNode, auto_save: bool = True) -> None:
        '''
        Fulfill extension request from the VFS when metadata return null (default extension)

        Wastefully reads the entire node into memory on the main thread before returning,
        shouldn't matter too much since this only is triggered on nodes that had no extension enrichment.

        Preferably upgrade the data fetch to a background task.
        '''
        if node.extension:
            return
        if node.is_sentinel:
            logger.debug(f'Skipping extension request, sentinel node has no bytes: {node}')
            return

        source_profile = getattr(self.active_handler, 'source_profile', None)
        if not source_profile:
            return
        header: bytes = self.get_node_data(node)[:0x30]
        if len(header.replace(b'\x00', b'')) < 16:
            logger.debug(f'Header too short: {len(header.replace(b"\\x00", b""))} bytes. {node} applying .bin')
            node.extension = '.bin'
        else:
            node.extension = self.active_handler.source.resolve_extension(node, header) if self.active_handler else '.bin'
        if auto_save and self._metadata_store is not None:
            self._metadata_store.register(node.hierarchical_id_str, extension=node.extension)
            logger.debug(f'Extension request saved: {node.hierarchical_id_str} -> {node.extension}')

    def _on_source_loaded(self, success: bool, result: object) -> None:
        '''Takes the ISO's root+children nodes and intializes:
        VfsManager -> VfsNavigator -> metadata -> and signals completion'''
        if threading.get_ident() != self._main_thread_id:
            raise threading.ThreadError("_on_source_loaded ran off the main thread")
        if not self.active_handler:
            raise RuntimeError('No active handler for the current source.')
        from core.workers import LoadIsoResult
        if not isinstance(result, LoadIsoResult) or not success:
            msg = result.error if isinstance(result, LoadIsoResult) else str(result)
            self.source_loaded.emit(False, msg)
            return
        handler, root = result.handler, result.root
        if not root or not handler:
            msg = 'ISO load succeeded but no root or handler was returned'
            self.source_loaded.emit(False, msg)
            return

        self.vfs = VfsManager(
            root,
            self.active_handler.source.resolve_boundary(root),
            node_enricher=(self._metadata_store.enrich if self._metadata_store else None)
        )
        # Connect signals
        self.tracker.node_modified.connect(self.vfs.update_node)
        self.tracker.node_reverted.connect(self.vfs.update_node)
        self.vfs.request_extension.connect(self._handle_extension_request)

        self.nav = VfsNavigator(
            self.vfs,
            self.get_node_data,
            self._on_expand_requested,
            self._on_ghost_node_confirmed_missing,
            self.active_handler.source,
            links=self._metadata_store
        )
        self.tracker.configure_source(self.active_handler.source, self._metadata_store)

        if handler is not None:
            # I think doing this on mainthread is fine since when this fires it is not possible for there to be any node registration
            build = handler.get_region(root)
            QTimer.singleShot(0, lambda: self.source_verified.emit(build))
        # self._migrate_targets_if_needed()   # Uncomment for building metadata from scratch
        self.source_loaded.emit(True, root)

    def _handle_verify_hash(self) -> None:
        '''
        Background task for manual ISO hashing.
        I'm not fully convinced that this is necesarry with the new ISO ingestion,
        will come down to how the community wants to handle mod distribution.
        For now it's easier to leave the dependency than remove and add it again.
        '''
        if self.active_handler is None:
            return
        verify_handle = self._start(Actions.verify_source, self.active_handler, label='Verify iso', channel=LogChannel.TOAST)
        verify_handle.finished.connect(self._on_source_verified)

    def _on_source_verified(self, success: bool, result: Any) -> None:
        if success and isinstance(result, str):
            self.source_verified.emit(result)

    def _on_action_complete(self, success: bool, result: Any) -> None:
        '''Result handler for Actions.dispatch tasks'''
        if not success or not isinstance(result, ActionResult):
            logger.error(f'Action task failed unexpectedly: {result}')
            return

        if result.status == ActionStatus.FAILURE:
            logger.error(f'Action "{result.action_name}" failed: {result.message}')
            self.action_complete.emit(result)
            return

        action_def = Registry.get_action(result.node, result.action_name)
        if not action_def:
            self.action_complete.emit(result)
            return

        match action_def.action_type:
            case ActionType.IMPORT | ActionType.PATCH:
                # edits get applied via fallback handler, prevents silent failing
                self.apply_edit(result.node, result.payload, editor=None)
                self.relay.log.emit(self._active_channel, f'{action_def.name} applied to {result.node.name}')
            case ActionType.TREE_EXPAND:
                if self.vfs:
                    new_nodes: list[VfsNode] = []
                    if isinstance(result.payload, VfsNode):
                        new_nodes = result.payload.children or [result.payload]
                    elif isinstance(result.payload, list):
                        new_nodes = result.payload
                    else:
                        logger.warning(f'TREE_EXPAND action returned unexpected payload type: {type(result.payload)}')
                    if new_nodes:
                        self.vfs.insert_children(result.node, new_nodes)
                        logger.debug(f'Inserted {len(new_nodes)} nodes into: {result.node}')
            case _:
                pass # PROCESS / DIALOG / EXPORT -- UI handled via action_complete

        self.action_complete.emit(result)

    def _on_decode_done(
        self,
        node: VfsNode,
        on_success: Callable[[], None]    | None,
        on_failure: Callable[[str], None] | None,
        success: bool,
        result: Any,
    ) -> None:
        '''Callback for when handler finishes decoding.
        Context params (node, on_success, on_failure) are bound via functools.partial;
        signal params (success, result) are appended by Qt when emitted.'''
        if threading.get_ident() != self._main_thread_id:
            logger.error("_on_decode_done ran off the main thread")
        if success and isinstance(result, bytes):
            previous_content = self._apply_modifications_with_conflict_prompt(node, result, self.get_node_data)
            if not previous_content:
                return
            logger.info(f'Modified: "{node.name}" added to rebuild queue.')
            if on_success:
                on_success()
        else:
            error_msg = str(result) if not success else f'decode returned {type(result).__name__}'
            logger.error(f'Decode failed: {error_msg}')
            if on_failure:
                on_failure(error_msg)

###------------------------------------- Block Device Helpers -----------------------------------###

def resolve_raw_disc_device(path: Path) -> Path | str:
    '''
    Resolves a mountpoint or drive letter to its raw physical block device path.
    If the path is already a file, it is returned as-is.
    '''
    path = Path(path).resolve()
    if path.is_file():
        return path
    system = platform.system()

    if system == 'Windows':
        drive_letter = path.drive.rstrip('\\')
        if drive_letter:
            return fr'\\.\{drive_letter}'
        raise ValueError(f'Could not resolve Windows drive letter from {path}')

    elif system == 'Linux':
        for dev in ['/dev/sr0', '/dev/cdrom', '/dev/dvd']:
            if Path(dev).exists():
                return dev
        with open('/proc/mounts') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == str(path):
                    return parts[0]
        raise FileNotFoundError(f'No optical raw device found for mountpoint {path}')

    elif system == 'Darwin':
        import subprocess
        result = subprocess.run(['df', str(path)], capture_output=True, text=True, check=False)
        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')
            if len(lines) > 1:
                dev = lines[1].split()[0]
                return dev.replace('/dev/disk', '/dev/rdisk')
        raise FileNotFoundError(f'Could not resolve macOS disk device for {path}')

    return path

###--------------------------------------------- Rebuild ---------------------------------------------------###

@dataclass(frozen=True)
class RebuildRequest:
    '''Everything needed to determine the logic required for the ISO build.'''
    staged_nodes:  list[VfsNode]
    build_flags:   SourceRebuildFlags
    patch_targets: dict[str, list[VfsNode]] | None = None

class RebuildCoordinator(QObject):
    '''
    Single entrypoint to trigger a rebuild.
    Owns the entire staging->confirm->run->complete lifecycle.
    Nothing else touches rebuild state.
    '''
    preparing_build = pyqtSignal()     # Signal to emit when the build is preparing to start
    started  = pyqtSignal(object)      # TaskHandle
    progress = pyqtSignal(int)         # Percentage
    log      = pyqtSignal(str)         # Log message
    finished = pyqtSignal(bool, str)   # Success, Report message

    def __init__(self, dispatcher: Dispatcher, parent_widget: QWidget):
        super().__init__()
        self._dispatcher = dispatcher
        self._parent = parent_widget # Connection for the File dialog
        self._config: RebuildRequest | None = None

    def request_rebuild(self, staged_nodes: list[VfsNode], build_flags: SourceRebuildFlags) -> None:
        '''
        Entry point for both the staging-page flow and direct triggers
        (e.g. the Patches menu). Resolves every active patch's targets
        (asynchronously expanding ghost nodes as needed) before opening the
        save dialog, since handler.rebuild_node needs concrete VfsNodes, not
        HIDs that might not be reachable yet.
        '''
        self.preparing_build.emit()
        self._dispatcher.set_active_channel(LogChannel.REBUILD)
        self.log.emit('Preparing VFS for rebuild...')
        handler = self._dispatcher.active_handler
        source = handler.source if handler else None
        active_patches: list[BaseVirtualPatch] = [
            patch for patch in (source.patches_for(build_flags) if source else [])
            if isinstance(patch, BaseVirtualPatch)
        ]
        self._resolve_patch_targets(staged_nodes, build_flags, active_patches, {})

    def _resolve_patch_targets(
        self,
        staged_nodes:  list[VfsNode],
        build_flags:   SourceRebuildFlags,
        patches:       list[BaseVirtualPatch],
        patch_targets: dict[str, list[VfsNode]],
    ) -> None:
        '''
        Resolve one rule's target parent at a time, chaining through
        resolve_ghost_node's async callback rather than assuming its
        expansion is complete by the next line. Recurses until every active
        rule has been resolved, then proceeds to the save dialog.

        Current limitation: Forced recursive expansion, no targeted expansion yet.
        '''
        if not patches or not self._dispatcher.vfs or not self._dispatcher.nav:
            self._begin(staged_nodes, build_flags, patch_targets)
            return

        patch, *rest = patches

        self.progress.emit(0)
        self.log.emit(f'Resolving patch target for patch {patch.name}, action: {patch.action}...')
        def _on_resolved(leaves: list[VfsNode]) -> None:
            self.progress.emit(100)
            patch_targets.setdefault(patch.action, []).extend(leaves)
            self.log.emit(f'Successfully resolved {len(leaves)} nodes for patch {patch.name}, action: {patch.action}')
            self._resolve_patch_targets(staged_nodes, build_flags, rest, patch_targets)
        self._dispatcher.resolve_and_unpack_all(patch.hid, _on_resolved)

    def _begin(
        self,
        staged_nodes: list[VfsNode],
        build_flags: SourceRebuildFlags,
        patch_targets: dict[str, list[VfsNode]],
    ) -> None:
        self._config = RebuildRequest(staged_nodes, build_flags, patch_targets)
        file_path, _ = QFileDialog.getSaveFileName(self._parent, 'Save Modified ISO', '', 'ISO Files (*.iso)')
        if not file_path:
            self.log.emit('Save dialog cancelled by user.')
            self._config = None
            self._dispatcher.set_active_channel(LogChannel.BROWSER)
            self.finished.emit(False, 'Rebuild cancelled by user.')
            return
        self._dispatcher.set_active_channel(LogChannel.REBUILD)
        handle = self._dispatcher.start_iso_rebuild(self._config, Path(file_path))
        if not handle:
            self._dispatcher.set_active_channel(LogChannel.BROWSER)
            self._config = None
            return
        handle.progress.connect(self.progress.emit)
        handle.log_message.connect(self.log.emit)
        handle.finished.connect(self._on_finished)
        self.started.emit(handle)

    def _on_finished(self, success: bool, result: Any) -> None:
        msg = result.message if isinstance(result, ActionResult) else str(result)
        if not msg:
            msg = 'Rebuild succeeeded.' if success else 'Rebuild failed.'
        self._dispatcher.finalize_rebuild(success)
        self.finished.emit(success, msg)
        self._config = None
