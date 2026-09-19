'''
Contains all background task defining logic.

- ActionType and ActionDef are used when registering profiles as well as for routing logic to distinct UI outcomes

- Tasks section manages threading and associated signals

- Actions routes background tasks to the appropriate logic.

'''
from __future__ import annotations

import threading
import itertools
import inspect
import time
from pathlib import Path
from enum import auto, Enum, Flag
from dataclasses import dataclass
from typing import Callable, Any, TYPE_CHECKING, NamedTuple
from PyQt6.QtCore import pyqtSignal, QObject, pyqtSlot, QRunnable, QThreadPool, Qt
from core.contracts import LeafHandler, ContainerHandler
from core.native.block_device import BlockDevice
if TYPE_CHECKING:
    from core.node import VfsNode, ModTracker
    from core.contracts import BaseHandler, PhysicalHandler
    from core.handlers.iso_container import IsoHandler
    from core.handlers.kods_container import KodsHandler
    from core.navigator import VfsNavigator
    from core.dispatcher import Dispatcher

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###------------------------------------- Action Types -------------------------------------###

class LogChannel(Enum):
    '''
    Designates which log/progress widget the given tasks' output belongs to. Set at task creation
    (TaskCoordinator.start_*) and carried on the TaskHandle itself, consumers read off the handle.

    BROWSER  - FileBrowserPage log console
    REBUILD  - IsoRebuildPage log console
    TOAST    - MainWindows's Toast, bottom right notification
    ACTION   - TODO EditorPage logging [[UNUSED]]
    '''
    BROWSER = auto()
    REBUILD = auto()
    TOAST   = auto()
    ACTION  = auto()

class IsoRebuildFlags(Flag):
    '''
    Which patches need to be applied to the filesystems before or during the ISO rebuild.
    Patches should be designed as independent and combinable.
    Adding a new patch should be as simple as adding a new flag and logic(Navigation + Overwrite).

    SLIMMED           - IsoHandler, patch out non-essential runtime data to save 1GB
    CUTSCENE_SKIPPER  - EvdHandler, scan and patch all story events with the appropriate near instant termination
    '''
    NONE              = 0
    SLIMMED           = auto()
    CUTSCENE_SKIPPER  = auto()

class ActionType(Enum):
    '''
    Defines how the disatcher and Action.dispatch handle results

    TREE_EXPAND  - execute_action returns a VfsNode — dispatcher inserts children
    PROCESS      - execute_action returns node data in Any format — stored as payload
    DIALOG       - execute_action returns a display string — shown in metadata panel or dialog
    EXPORT       - write node data to disk
    IMPORT       - read file from disk into node
    PATCH        - auto applies returned payload to node.pending_data
    '''
    TREE_EXPAND = 'tree_expand'
    PROCESS     = 'process'
    DIALOG      = 'dialog'
    EXPORT      = 'export'
    IMPORT      = 'import'
    PATCH       = 'patch'

@dataclass(frozen=True)
class ActionDef:
    '''Describes a single action.
    name is the key passed to execute_action and the context menu label'''
    name:        str
    action_type: ActionType

###---------------------------------------- Results ----------------------------------------###

class ActionStatus(Enum):
    SUCCESS = auto()
    FAILURE = auto()

@dataclass
class ActionResult:
    '''Generic action result. Carries the action name, node, status, and optional payload/message.'''
    action_name: str
    node:        VfsNode
    status:      ActionStatus
    payload:     Any = None  # result of the action (bytes, str... etc depending on ActionType)
    message:     str = ''

@dataclass
class EditorPayload:
    '''Result structured for editors. Carries node, data'''
    node: VfsNode
    data: Any

class LoadIsoResult(NamedTuple):
    '''Payload for _on_iso_loaded. PhysicalHandler and ISO handling are always dedicated logically.'''
    success: bool
    handler: IsoHandler | None = None
    root:    VfsNode         | None = None
    error:   str             | None = None

def validate_task_signature(fn, args: tuple, kwargs: dict) -> None:
    signature = inspect.signature(fn)
    try: signature.bind(*args, **kwargs)
    except TypeError as e:
        pos_details = [f"  [{i}] {type(arg).__name__}: {repr(arg)[:40]}" for i, arg in enumerate(args)]
        kw_details = [f"  {k} ({type(v).__name__}): {repr(v)[:40]}" for k, v in kwargs.items()]
        diagnostic = (
            f"\nTarget Function : {fn.__module__}.{fn.__qualname__}"
            f"\nExpected Sig    : {fn.__name__}{signature}"
            f"\nPositional Args ({len(args)}):\n" + ("\n".join(pos_details) if pos_details else "  (none)") +
            f"\nKeyword Args    ({len(kwargs)}):\n" + ("\n".join(kw_details) if kw_details else "  (none)") +
            f"\nError Details   : {e}"
        )
        logger.error(diagnostic)
        raise TypeError(diagnostic) from e

###---------------------------------------- Tasks ------------------------------------------###

class GenericTask(QRunnable):
    '''Generic background worker'''
    def __init__(self, handle: TaskHandle, function: Callable, *args, **kwargs) -> None:
        super().__init__()
        self.handle  = handle
        self.fn      = function
        self.args    = args
        self.kwargs  = dict(kwargs)

    @pyqtSlot()
    def run(self) -> None:
        '''Execute the function, catch errors, and advance the state machine'''
        current_thread = threading.current_thread()
        logger.debug(
            f'<Thread: {current_thread.name} (ID: {current_thread.ident})> '
            f'Started execution of GenericTask for Task #{self.handle.task_id} ({self.handle.task_name})'
        )
        self.handle.mark_running()
        try:
            validate_task_signature(self.fn, self.args, self.kwargs)
            result = self.fn(*self.args, **self.kwargs)
            self.handle.complete(result)
        except InterruptedError as e:
            logger.warning(
                f'<Thread: {current_thread.name} Task #{self.handle.task_id}> '
                f'({self.handle.task_name}) aborted: {e}'
            )
            self.handle.fail('Cancelled by user')
        except Exception as e:
            logger.error(
                f'<Thread: {current_thread.name} Background Task #{self.handle.task_id}> '
                f'({self.handle.task_name}) failed', exc_info=True
            )
            self.handle.fail(e)

class TaskCoordinator(QObject):
    def __init__(self):
        super().__init__()
        self.thread_pool = QThreadPool()
        self.thread_pool.setMaxThreadCount(4)
        # Retention map: keeps the Python TaskHandle wrapper alive until its
        # terminal `finished` signal has been delivered, even when call sites
        # discard the returned handle.  Keyed by id(handle) so the cleanup
        # lambda captures only an int — no direct reference to the handle —
        # which avoids a permanent retention cycle.
        self._active_handles: dict[int, TaskHandle] = {}

    def _retain_handle(self, handle: TaskHandle) -> None:
        '''Store *handle* and schedule its removal when finished fires.'''
        handle_id = id(handle)
        self._active_handles[handle_id] = handle
        # DirectConnection to ensure that finished can be triggerd immediately regardless of main thread event loop
        handle.finished.connect(
            lambda *_: self._active_handles.pop(handle_id, None),
            Qt.ConnectionType.DirectConnection # type: ignore this valid
        )

    def start_task(
        self,
        function: Callable,
        *args,
        channel: LogChannel = LogChannel.BROWSER,
        label:   str        = '',
        **kwargs
    ) -> TaskHandle:
        '''
        Spin up thread. Link handle.
        channel and label are consumed here tagging the TaskHandle, never passing to actions.
        '''
        task_name = function.__name__ # type: ignore function is a named function
        handle = TaskHandle(task_name, channel=channel, label=label)

        logger.debug(
            f'<TaskCoordinator: Queuing ThreadPool Task #{handle.task_id} '
            f'({task_name}) | Total Active Handles: {len(self._active_handles) + 1}'
        )

        kwargs['task_handle'] = handle

        self._retain_handle(handle)
        worker = GenericTask(handle, function, *args, **kwargs)
        self.thread_pool.start(worker)
        return handle

    def start_dedicated_task(
        self,
        function: Callable,
        *args,
        channel: LogChannel = LogChannel.BROWSER,
        label: str = '',
        **kwargs
    ) -> TaskHandle:
        '''Run *function* on a dedicated daemon thread, NOT on the bounded QThreadPool.

        Use this for long-running coordinator tasks (e.g. ISO rebuild) that may
        block waiting for QThreadPool tasks to finish.  Running such a task on the
        pool itself risks exhausting all pool threads, causing a deadlock where the
        blocked pool threads wait for expansion tasks that can never be scheduled.

        The returned TaskHandle behaves identically to one from start_task — its
        signals are emitted cross-thread and delivered to the main thread via Qt's
        queued-connection mechanism.
        '''
        task_name = function.__name__ # type: ignore  function is a named funtion
        handle = TaskHandle(task_name, channel=channel, label=label)
        kwargs['task_handle'] = handle

        logger.debug(
            f'<TaskCoordinator: Allocation Dedicated Thread for Task #{handle.task_id} ({task_name})>'
        )
        self._retain_handle(handle)

        def _run() -> None:
            # Handle tasks cancelled during spin-up
            current_thread = threading.current_thread()
            logger.debug(
                f'<Thread: {current_thread.name} (ID: {current_thread.ident}) '
                f'Spawned dedicated worker for Task #{handle.task_id} ({task_name})'
            )
            if handle.is_cancelling():
                logger.warning(f'<Thread: {current_thread.name} Task #{handle.task_id}> cancelled before starting')
                handle.finished.emit(False, 'Task cancelled before starting.')
                return
            # Run the task
            handle.mark_running()
            try:
                result = function(*args, **kwargs)
                handle.complete(result)
            except InterruptedError as e:
                # Route cancelling to cancelled
                logger.warning(
                    f'<Thread: {current_thread.name} Task #{handle.task_id}> aborted at checkpoint: {e}'
                )
                handle.complete('Cancelled by user')
            except Exception as e:
                logger.error(
                    f'<Thread: {current_thread.name} Task #{handle.task_id}> failed', exc_info=True
                )
                handle.fail(e)

        thread = threading.Thread(target=_run, daemon=True, name=f'dedicated-{task_name}')
        thread.start()
        return handle

    def shutdown(self, drain_timeout: float = 10.0, poll_interval: float = 0.02) -> bool:
        '''
        Cancel any task held by the retention map, returning True if all were cancelled.
        drain_timeout needs to cover the longest gap between two checkpoint() calls in any
        dedicated task.
        '''
        open_handles = list(self._active_handles.values())
        if open_handles:  # Only log if there are active tasks
            logger.info(
                'TaskCoordinator: Cancelling pending tasks...'
                f'Unfinished Active Task IDs still in memory: {[h.task_id for h in open_handles]}'
            )
        for handle in open_handles:
            logger.debug(f'TaskCoordinator Force-Cancelling: Task #{handle.task_id}')
            handle.cancel()
        # QThreadPool handles
        self.thread_pool.clear()
        if not self.thread_pool.waitForDone(2000):
            logger.warning('TaskCoordinator: Some threads did not finish in time.')
        open_ids = {id(h) for h in open_handles}
        deadline = time.monotonic() + drain_timeout
        while open_ids & self._active_handles.keys():
            if time.monotonic() > deadline:
                still_pending = [h.task_id for h in open_handles if id(h) in self._active_handles]
                logger.warning(f'TaskCoordinator: Shutdown deadline exceeded. Still pending: {still_pending}')
                return False
            time.sleep(poll_interval)
        return True

class TaskHandle(QObject):
    '''State machine and handle for background tasks, thread-safe'''
    _id_generator = itertools.count(1)

    state_changed = pyqtSignal(str)           # State name
    progress      = pyqtSignal(int)           # (percentage)
    finished      = pyqtSignal(bool, object)  # (success, payload)
    log_message   = pyqtSignal(str)           # log output

    _VALID_TRANSITIONS: dict[str, set[str]] = {
        'pending':    {'running', 'cancelled'},
        'running':    {'cancelling', 'completed', 'failed'},
        'cancelling': {'cancelled'},
        'completed':  set(),
        'failed':     set(),
        'cancelled':  set()
    }

    def __init__(self, task_name: str, channel: LogChannel = LogChannel.BROWSER, label: str = ''):
        super().__init__()
        self.task_id    = next(self._id_generator)
        self.task_name  = task_name
        self.channel    = channel
        self.label      = label    # <-- may be redundant since the task_name could be used instaed.
        self._state     = 'pending'
        self._lock      = threading.Lock()
        self._cancel_token = threading.Event()
        self._start_time: float | None = None
        self._end_time: float | None = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def _transition(self, target: str) -> None:
        '''Transition to the specified state, updating end time if completed/failed/cancelled'''
        current_thread = threading.current_thread()
        with self._lock:
            valid = self._VALID_TRANSITIONS.get(self._state, set())
            if target not in valid:
                logger.warning(
                    f'<Task #{self.task_id} ({self.task_name})> '
                    f'Invalid transition attempted {self._state}->{target}'
                )
                return
            self._state = target
            log_entry = None
            # Start timer when moving to running
            if target == 'running':
                self._start_time = time.perf_counter()

            # End timer and generate completion message when entering terminal states
            elif target in ('completed', 'failed', 'cancelled') and self._start_time is not None and self._end_time is None:
                self._end_time = time.perf_counter()
                dur = self._end_time - self._start_time
                if (mins := dur // 60):
                    secs = dur % 60
                    dur_str = f'{int(mins)}m {secs:.2f}s'
                else:
                    dur_str = f'{dur:.2f}s'
                log_entry = f'Task #{self.task_id} ({self.task_name}) Thread: "{current_thread.name}" -> {target} in {dur_str}'
        self.state_changed.emit(target)
        if log_entry:
            logger.debug(log_entry)
        else:
            logger.debug(
                f'<Task #{self.task_id} ({self.task_name}) Thread: "{current_thread.name}"> -> {target}'
            )

    ### from main thread
    def cancel(self) -> None:
        '''Cancel the current task from the main thread'''
        current_thread = threading.current_thread()
        with self._lock:
            if self._state not in ('pending', 'running'):
                logger.debug(
                    f'<Task #{self.task_id}> Cancel skipped. Already in terminal state "{self._state}"'
                )
                return
            target = 'cancelled' if self._state == 'pending' else 'cancelling'
            self._cancel_token.set()
            self._state = target
        self.state_changed.emit(target)
        logger.debug(
            f'<Task #{self.task_id} ({self.task_name})> '
            f'CANCEL requested for Thread "{current_thread.name}" -> {target}'
        )

    ### from background thread
    def is_cancelling(self) -> bool:
        return self._cancel_token.is_set()

    def checkpoint(self) -> None:
        if self.is_cancelling():
            logger.debug(f'<Task #{self.task_id} ({self.task_name}) caught cancel flag at a checkpoint.')
            raise InterruptedError(
                f'<Task #{self.task_id} ({self.task_name})> cancelled at checkpoint.'
            )

    def mark_running(self) -> None:
        self._transition('running')

    def complete(self, result: object) -> None:
        if self.is_cancelling():
            self._transition('cancelled')
            self.finished.emit(False, f'Task #{self.task_id} ({self.task_name}) cancelled.')
        else:
            self._transition('completed')
            self.finished.emit(True, result)

    def fail(self, error: Exception | str) -> None:
        if self.is_cancelling():
            self._transition('cancelled')
            self.finished.emit(False, f'Task #{self.task_id} ({self.task_name}) cancelled.')
        else:
            self._transition('failed')
            self.finished.emit(False, str(error))

###-------------------------------------- Rebuild Patches ------------------------------------###

REBUILD_PATCH_ACTIONS: dict[IsoRebuildFlags, str] = {
    IsoRebuildFlags.CUTSCENE_SKIPPER: 'Skip cutscenes',
}

###---------------------------------------- Actions --------------------------------------###

class Actions:
    """All background task entrypoint logic in one place."""

    @staticmethod
    def _apply_rebuild_patches(
        patch_targets: dict[str, list[VfsNode]],
        navigator:        VfsNavigator,
        task_handle:      TaskHandle,
    ) -> list[VfsNode]:
        '''Apply all rebuild patches to the given nodes, returning the list of touched nodes.
        Uses the same Actions.dispatch pipeline generic node actions use.'''
        if not patch_targets:
            return []
        from core.registry import Registry
        touched: list[VfsNode] = []
        for action_name, nodes in patch_targets.items():
            for node in nodes:
                # Filter out container nodes, nodes waiting to be expanded, and sentinel nodes
                # Container nodes may need to be included if some fs patch requires them to be modified
                if node.children or getattr(node, 'expansion_pending', False) or 'sentinel' in node.name:
                    continue

                task_handle.checkpoint()
                action_def = Registry.get_action(node, action_name)
                if not action_def:
                    task_handle.log_message.emit(f'Warning: No action found for {action_name} on {node}')
                    continue
                task_handle.log_message.emit(f'Applying patch {action_name} to {node}')
                result = Actions.dispatch(action_def, node, navigator, task_handle)
                if result.status is ActionStatus.SUCCESS:
                    if isinstance(result.payload, (bytes, bytearray)):
                        node.pending_data = bytes(result.payload)
                        touched.append(node)
                    else:
                        task_handle.log_message.emit(
                            f'{action_name} on {node} did not return bytes payload  - Skipping'
                        )
                else:
                    task_handle.log_message.emit(f'{action_name} failed on {node}: {result.message}')
        return touched

    @staticmethod
    def propogate_virtual_mutations(
        nodes:       list[VfsNode],
        navigator:   VfsNavigator,
        task_handle: TaskHandle
    ) -> bool:
        '''
        Initiate a virtual roll-up/rebuild for one toc entry. Deadcode from pcsx2 live testing.
        Kept since this has a decent chance of having some use, however it is very primitive and
        should be adjusted if reimplemented in the future.
        '''
        result = navigator.rollup_nodes(nodes, task_handle)
        if 0 < len(result) > 2:  # rollup on a single virtual mutation should never result in more than a physical node + datacenter rebuild
            return False
        task_handle.checkpoint()
        return True

    ### Editor
    @staticmethod
    def prepare_editor(
        handler_class: type[BaseHandler],
        node:          VfsNode,
        navigator:     VfsNavigator,
        task_handle:   TaskHandle
    ) -> EditorPayload:
        '''
        Unwraps the node and calls handler.prepare_editor_data() on a background thread.

        Returns EditorPayload(node, data)
        '''
        task_handle.log_message.emit(f'Preparing editor data for "{node.name}"...')
        task_handle.checkpoint()
        raw_bytes = node.pending_data or navigator.unwrap_chain(node)
        if not raw_bytes:
            raise ValueError(f'unwrap_chain returned empty bytes for "{node.name}"')
        task_handle.checkpoint()
        header_bytes = navigator.resolve_data_from_hid(node.target)
        if not issubclass(handler_class, (ContainerHandler, LeafHandler)):
            raise TypeError(
                f'{handler_class.__name__} must be ContainerHandler or LeafHandler.'
            )
        with handler_class(raw_bytes, node.parent) as handler:
            handler.task_handle = task_handle
            handler.datacenter_header = header_bytes
            result = handler.prepare_editor_data(node, raw_bytes)
        logger.debug(f'prepare_editor: {node.name} -> {type(result).__name__} from {handler_class.__name__}')
        return EditorPayload(node=node, data=result)

    @staticmethod
    def decode_editor_data(
        handler_class: type[BaseHandler],
        node:          VfsNode,
        payload:       Any,
        task_handle:   TaskHandle
    ) -> bytes:
        '''
        Takes a complex UI payload and routes it through the node's handler
        converting it back to raw bytes on a background thread
        '''
        task_handle.log_message.emit(f'Decoding data for {node.name}')
        task_handle.checkpoint()
        if not issubclass(handler_class, (ContainerHandler, LeafHandler)):
            raise TypeError(f'{handler_class.__name__} must be ContainerHandler or LeafHandler.')
        with handler_class(b'', node.parent) as handler:
            handler.task_handle = task_handle
            result = handler.decode_editor_data(node, payload)
        if not isinstance(result, bytes):
            raise TypeError(f'Handler {handler_class.__name__} returned {type(result).__name__}, expected bytes')
        task_handle.log_message.emit(f'Editor payload for {node.name} encoded to bytes')
        return result

    @staticmethod
    def fetch_for_editor(
        hid: tuple[int, ...],
        navigator: VfsNavigator,
        expansion_callback: Callable[[VfsNode, threading.Event], None],
        task_handle: TaskHandle
    ) -> EditorPayload:
        '''
        Resolves a node for the given HID, and attempts to call it's prepare_editor_data() method.
        '''
        logger.debug(f'Fetching editor payload for HID {hid}')  # logger to hit dev console
        raw_bytes = navigator.resolve_data_from_hid(hid) # resolve raw bytes/register the node in VFS
        if not raw_bytes:
            raise ValueError(f'Could not resolve raw bytes for HID: {hid}. Ensure it exists.')
        node = navigator.vfs.get_vfs_node_by_id(hid) # resolve node
        if not node:
            raise ValueError(f'Could not find node for HID: {hid}. Ensure it got expanded.')
        from core.registry import Registry
        handler_class = Registry.get_handler(node) # resolve handler
        if not handler_class:
            raise ValueError(f'No handler registered for node: {node}')
        task_handle.checkpoint()
        # Datacenter verification
        header_bytes = None
        if hasattr(node, 'target') and node.target:
            header_bytes = navigator.resolve_data_from_hid(node.target)
            if not header_bytes:
                raise ValueError(f'Could not resolve header bytes for target: {node.target}. Ensure it exists.')
        if not issubclass(handler_class, (ContainerHandler, LeafHandler)):
            raise TypeError(
                f'{handler_class.__name__} must be ContainerHandler or LeafHandler'
            )
        # Start prepare_editor_data and return the EditorPayload
        with handler_class(raw_bytes, node.parent) as handler:
            handler.tesk_handle = task_handle
            if header_bytes:
                handler.datacenter_header = header_bytes
            result = handler.prepare_editor_data(node, raw_bytes)
        logger.debug(f'fetch_for_editor: {hid} -> {type(result).__name__} from {handler_class.__name__}')
        return EditorPayload(node=node, data=result)

    ### Entry point for all node actions
    @staticmethod
    def dispatch(
        action_def:  ActionDef,
        node:        VfsNode,
        navigator:   VfsNavigator,
        task_handle: TaskHandle,
        **kwargs,
    ) -> ActionResult:
        '''
        Route an action based on ActionDef.action_type.
        Dispatcher calls this for node actions. It should never need to know what to execute
        '''
        from core.registry import Registry
        match action_def.action_type:
            case ActionType.TREE_EXPAND | ActionType.PROCESS | ActionType.DIALOG | ActionType.PATCH:
                handler_class = Registry.get_handler(node)
                if not handler_class:
                    return ActionResult(
                        action_name=action_def.name,
                        node=node,
                        status=ActionStatus.FAILURE,
                        message=f'No handler registered for {node.name}'
                    )
                return Actions.run_handler_action(
                    handler_class,
                    node,
                    action_def.name,
                    navigator,
                    task_handle,
                    **kwargs,
                )
            case ActionType.EXPORT:
                file_path: Path | None = kwargs.get('file_path')
                if not file_path:
                    return ActionResult(
                        action_name=action_def.name,
                        node=node,
                        status=ActionStatus.FAILURE,
                        message='No output path provided',
                    )
                handler_class = Registry.get_handler(node)
                profile = Registry.get_handler_profile(node)
                has_custom_export = (
                    handler_class
                    and profile
                    and profile.get_action(action_def.name) is not None
                )
                if has_custom_export:  # Custom Export - PNG, WAV... etc
                    return Actions.run_handler_action(
                        handler_class,
                        node,
                        action_def.name,
                        navigator,
                        task_handle,
                        **kwargs
                    )
                # Fallback - Raw Bytes
                return Actions.export_node(
                    node, file_path, navigator, task_handle, action_name=action_def.name
                )
            case ActionType.IMPORT:
                file_path = kwargs.get('file_path')
                if not file_path:
                    return ActionResult(
                        action_name=action_def.name,
                        node=node,
                        status=ActionStatus.FAILURE,
                        message='No import path provided',
                    )
                return Actions.import_node(
                    node, file_path, task_handle, action_name=action_def.name
                )
            case _:
                pass

    ### ISO Specific actions
    @staticmethod
    def load_iso(
        handler:       IsoHandler,
        task_handle:   TaskHandle,
    ) -> object:
        '''Read the TOC and split the disk into it's physical files'''
        try:
            root = handler.get_file_tree()
            return LoadIsoResult(True, handler, root)
        except ValueError as e:
            return str(e)

    @staticmethod
    def rebuild_iso(
        handler:       IsoHandler,
        root_node:     VfsNode,
        navigator:     VfsNavigator,
        staged_nodes:  list[VfsNode],
        output_path:   Path,
        build_flags:   IsoRebuildFlags,
        patch_targets: dict[str, list[VfsNode]],
        task_handle:  TaskHandle,
    ) -> ActionResult:
        from core.navigator import ExpansionTimeoutError
        try:
            task_handle.log_message.emit('Starting ISO build sequence...')
            task_handle.progress.emit(0)
            if build_flags is not IsoRebuildFlags.NONE:
                applied = ', '.join(
                    flag.name for flag in IsoRebuildFlags
                    if flag is not IsoRebuildFlags.NONE and (build_flags & flag) and flag.name
                )
                task_handle.log_message.emit(f'Patch(es) to be applied: {applied}')
            task_handle.log_message.emit('Starting Pass 0   -   Applying patches...')
            patched_nodes = Actions._apply_rebuild_patches(patch_targets, navigator, task_handle)
            if patched_nodes :
                staged_nodes = list(staged_nodes) + patched_nodes
                task_handle.log_message.emit(f'Pass 0 complete   -   Patched {len(patched_nodes)} nodes')
            task_handle.log_message.emit('Starting Pass 1   -   Precomputing Datacenter...')

            extra_targets: list[VfsNode] = navigator.precompute_datacenter(staged_nodes, task_handle)
            all_staged:    list[VfsNode] = list(staged_nodes) + extra_targets
            task_handle.log_message.emit(f'Pass 1 complete   -   {len(extra_targets)} datacenter target(s) cached and queued')

            task_handle.progress.emit(0)
            task_handle.log_message.emit('Starting Pass 2   -   Performing VFS rollup...')

            physical_staged_nodes = navigator.rollup_nodes(all_staged, task_handle)
            task_handle.progress.emit(0)
            task_handle.log_message.emit('Writing sectors to disk...')
            success = handler.rebuild_node(
                root_node,
                physical_staged_nodes,
                output_path,
                build_flags,
                task_handle,
            )

            if success:
                task_handle.log_message.emit('ISO Build Successful.')
                task_handle.progress.emit(100)
                return ActionResult(
                    action_name='Build ISO',
                    node=root_node,
                    status=ActionStatus.SUCCESS,
                )
            raise ValueError('iso_container.rebuild_node returned false')
        except ExpansionTimeoutError:
            # Re-raise so the dedicated rebuild thread's exception handler calls
            # handle.fail(), which emits finished(False, ...) and causes the UI
            # to show a hard failure rather than silently proceeding with stale data.
            raise
        except Exception as e:
            logger.error(f'Rebuild failed: {e}', exc_info=True)
            return ActionResult(
                action_name='Build ISO',
                node=root_node,
                status=ActionStatus.FAILURE,
                message=str(e)
            )

    @staticmethod
    def verify_iso(
        handler:      IsoHandler,
        task_handle:  TaskHandle,
    ) -> str:
        task_handle.progress.emit(0)
        task_handle.log_message.emit('Verifying ISO...')
        result = handler.verify_iso_integrity(task_handle)
        task_handle.log_message.emit(f'ISO verified {result}')
        task_handle.progress.emit(100)
        return result

    ### IO specific actions
    @staticmethod
    def export_node(
        node:         VfsNode,
        output_path:  Path,
        navigator:    VfsNavigator,
        task_handle:  TaskHandle,
        action_name:  str = 'Export'
    ) -> ActionResult:
        try:
            task_handle.log_message.emit(f'Exporting {node.name}{node.extension}...')
            data = navigator.unwrap_chain(node)
            if not data:
                raise ValueError('Resolved data is empty')
            total_size = len(data)
            chunk_size = 1024 * 1024 * 10
            with open(output_path, 'wb') as f:
                for i in range(0, total_size, chunk_size):
                    task_handle.checkpoint()
                    f.write(data[i : i + chunk_size])

            return ActionResult(
                action_name=action_name,
                node=node,
                status=ActionStatus.SUCCESS,
                message=f'Exported: {output_path.name}'
            )
        except Exception as e:
            logger.error(f'Export failed: {e}', exc_info=True)
            return ActionResult(
                action_name=action_name,
                node=node,
                status=ActionStatus.FAILURE,
                message=str(e)
            )

    @staticmethod
    def import_node(
            node:        VfsNode,
            import_path: Path,
            task_handle: TaskHandle,
            action_name: str = 'Import'
    ) -> ActionResult:
        try:
            task_handle.log_message.emit(f'Importing {import_path.name}...')
            data = import_path.read_bytes()
            return ActionResult(
                action_name=action_name,
                node=node,
                status=ActionStatus.SUCCESS,
                payload=data
            )
        except Exception as e:
            logger.error(f'Import failed: {e}', exc_info=True)
            return ActionResult(
                action_name=action_name,
                node=node,
                status=ActionStatus.FAILURE,
                message=str(e),
            )

    @staticmethod
    def complex_import(
        node:          VfsNode,
        target_node:   VfsNode,
        handler_class: type[KodsHandler],
        tracker:       ModTracker,
        resolver:      Callable[[VfsNode], bytes],
        inner_nodes:   list[VfsNode],
        file_path:     Path,
        task_handle:   TaskHandle,
    ) -> ActionResult:
        '''
        Import a new raw payload for a datacenter node. This means that the Kods imported is actually
        missing it's header.

        In the future depending on how we end up resolving these imports we should add some dialog or
        checks to see if the datacenter header can be or is uploaded.
        '''
        try:
            task_handle.log_message.emit(f'Importing {file_path.name} as datacenter file...')
            # Build the new headers for the payload
            new_payload = file_path.read_bytes()
            orig_payload = resolver(node)
            orig_header = resolver(target_node)
            child_headers = {(node.hierarchical_id[-1] % 10) - 1: resolver(node) for node in inner_nodes if node.size > 8}
            if not node.parent:
                raise ValueError(f'No parent for node {node}')
            with handler_class(orig_payload, node.parent) as handler:
                new_header_outer, new_headers_inner = handler.complex_import(node, orig_header, new_payload, child_headers)
            task_handle.checkpoint()
            # Apply the computed headers to the appropriate nodes
            tracker.mark_modified(node, new_payload, orig_payload)
            tracker.mark_modified(target_node, new_header_outer, orig_header)
            for inner_node in inner_nodes:
                slot_index = (inner_node.hierarchical_id[-1] % 10) - 1
                if slot_index in new_headers_inner:
                    tracker.mark_modified(inner_node, new_headers_inner[slot_index], resolver(inner_node))
            return ActionResult(
                action_name='Complex Import',
                node=node,
                status=ActionStatus.SUCCESS,
                payload=new_payload
            )
        except Exception as e:
            logger.error(f'Complex Import failed: {e}', exc_info=True)
            return ActionResult(
                action_name='Complex Import',
                node=node,
                status=ActionStatus.FAILURE,
                message=str(e),
            )

    ### Handler actions
    @staticmethod
    def run_handler_action(
        handler_class: type[BaseHandler],
        node:          VfsNode,
        action_name:   str,
        navigator:     VfsNavigator,
        task_handle:   TaskHandle,
        **kwargs,
    ) -> ActionResult:
        '''
        Find requested data,
        open a handle,
        inject handle with dependencies,
        execute action with handle
        '''
        if action_name != 'Properties':
            task_handle.log_message.emit(f'Starting "{action_name}" on node: {node}...')
        try:
            node_bytes   = navigator.unwrap_chain(node)
            header_bytes = navigator.resolve_data_from_hid(node.target)
            if not issubclass(handler_class, (ContainerHandler, LeafHandler)):
                raise TypeError(f'{handler_class.__name__} must be ContainerHandler or LeafHandler.')
            with handler_class(node_bytes, node.parent) as handler:
                handler.task_handle = task_handle
                setattr(handler, 'datacenter_header', header_bytes)
                payload = handler.execute_action(node, action_name, **kwargs)
            if action_name != 'Properties':
                task_handle.log_message.emit(f'Finished "{action_name}" on node: {node}.')
            return ActionResult(
                action_name=action_name,
                node=node,
                status=ActionStatus.SUCCESS,
                payload=payload
            )
        except Exception as e:
            logger.error(f'Action "{action_name}" failed on {node.name}: {e}', exc_info=True)
            return ActionResult(
                action_name=action_name,
                node=node,
                status=ActionStatus.FAILURE,
                message=str(e)
            )
