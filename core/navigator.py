"""
Core FS Navigation.

Handles multi-threaded tree expansions, deep FS traversals, and
thread-isolated node roll-ups for rebuilding the FS.

Traversal API (Physical to Virtual):
    unwrap_chain            - Reads raw bytes from the physical layer up to the target node.
        |_ build_chain      - Constructs the path from a virtual node down to its physical root.
        |_ walk_chain       - Traverses container handlers to extract the target's bytes.

Expansion API (Concurrency-Safe Discovery):
    request_expansion       - Core orchestrator for async, multi-threaded node expansion.
    complete_expansion      - Signals the conclusion of an in-flight expansion task.
    resolve_ghost_node      - Recursively expands ancestors to discover a specific target HID.
    resolve_data_from_hid   - Resolves a target HID directly to raw bytes, triggering expansions as needed.
    unpack_recursive        - Deeply expands a target and all of its children recursively.

Package API
    package_members         - Ask the assembler which nodes a package's handler needs.
    ensure_package          - None-blocking: expand until every member is reachable.
    resolve_package         - Blocking: collect members + bytes into a ResolvedPackage.
    open_handler            - Construct the handler for a package.

Rebuild API (Thread-Isolated Operations):
    rollup_nodes            - Bottom-up VFS rebuild (deepest children up to physical parents).
    precompute_packages     - Rolls up nodes that depend on package members.
    clear_rollup_pending    - Flushes cached rebuild data. Executed safely on the main thread
                              after the dedicated rebuild thread concludes and unblocks the UI.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Callable, TYPE_CHECKING, Iterator
from contextlib import contextmanager
from core.node import VfsNode, VfsManager
from core.registry import Registry
from core.contracts import ContainerHandler, LeafHandler, ResolvedPackage, PackageError, as_package_result, BaseSource
if TYPE_CHECKING:
    from core.workers import TaskHandle
    from core.contracts import BaseHandler

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###----------------------------------- Exceptions ----------------------------------------###

class ExpansionTimeoutError(RuntimeError):
    """Raised when a VFS node expansion request times out.

    This is a hard error: it means the rebuild thread waited the full
    EXPANSION_TIMEOUT for an expansion task to complete and it never did.
    Letting the rebuild continue with stale/empty data would produce a
    silently corrupt output, so we raise instead.
    """
    pass

###----------------------------------- Navigator ----------------------------------------###

class VfsNavigator:
    '''Handles all tree traveling logic'''
    EXPANSION_TIMEOUT = 30.0  # seconds; raised from 1.0 — real disk+decompress can be slow
    MAX_RESOLUTION_DEPTH = 20
    def __init__(
            self,
            vfs:                 VfsManager,
            data_reader:         Callable[[VfsNode], bytes],
            expansion_callback:  Callable[[VfsNode, threading.Event], None],
            ghost_node_callback: Callable[[tuple[int, ...]], None] | None = None,
            source_profile:      BaseSource | None = None,
            links:               BaseSource.Packages.LinkLookup | None = None,
        ):
        self.vfs  = vfs
        self.read = data_reader
        self.expansion_callback  = expansion_callback
        self.ghost_node_callback = ghost_node_callback
        self._source_profile     = source_profile
        self._links     = links
        self._rollup_touched: set[VfsNode] = set()

        self._expand_waiters: dict[tuple[int, ...], list[Callable[[bool, VfsNode], None]]] = {}
        self._expand_waiters_lock = threading.Lock()

    ###--------------------- Expansion Entry/End points ----------------------###

    def request_expansion(
        self,
        node:    VfsNode,
        on_done: Callable[[bool, VfsNode], None] | None = None,
    ) -> threading.Event:
        '''
        The only path anything should use to start or join a node expansion.
        Safe to call concurrently, from any thread, for the same node.
        Exactly one TREE_EXPAND task is ever in flight per node, regardless
        of how many callers ask at once or from which thread.

        - Node doesn't need expansion (already has children, or has no
          registered TREE_EXPAND action): on_done fires synchronously with
          (True, node); returns an already-set Event.
        - An expansion is already in-flight for this node (started by this
          call or a concurrent one): on_done is queued and fires once that
          expansion completes. No new task is started — this call is a pure
          join, not an attempt.
        - Otherwise, this call becomes the owner: it invokes self.expansion_callback(node, event).
          complete_expansion() must be called exactly once when that work finishes.

        Returns the node's expansion Event so blocking callers can `event.wait(timeout=...)`
        themselves in addition to, or instead of, passing on_done.
        '''
        with self._expand_waiters_lock:
            if not self._needs_expansion(node):
                event = threading.Event()
                event.set()
                is_owner = False
                already_resolved = True
            else:
                is_owner, event = node.begin_expansion()
                already_resolved = False
                if on_done:
                    self._expand_waiters.setdefault(node.hierarchical_id, []).append(on_done)
        if already_resolved:
            if on_done:
                on_done(True, node)
            return  event
        if not is_owner:
            logger.debug(f'Expansion already in-flight for {node}; queued waiter.')
            return event
        logger.debug(f'Claimed expansion for {node}')
        self.expansion_callback(node, event)
        return event

    def complete_expansion(self, node: VfsNode, success: bool) -> None:
        '''
        Called exactly once by whoever implements expansion_callback, when
        the underlying expansion work for `node` finishes (success or
        failure). Releases the node's ownership and drains every waiter
        queued via request_expansion(), in the order they arrived.
        '''
        with self._expand_waiters_lock:
            node.finish_expansion(success)
            waiters = self._expand_waiters.pop(node.hierarchical_id, [])
        for on_done in waiters:
            on_done(success, node)

    ###--------------------- Ghost node missing -----------------------###

    def _report_confirmed_missing(self, hid: tuple[int, ...], reason: str) -> None:
        '''The entrypoint to cleaning a false record from the metadata database'''
        logger.warning(f'Confirmed non-existent HID {hid}: {reason}')
        if self.ghost_node_callback:
            self.ghost_node_callback(hid)

    def _evaluate_stalled(self, ancestor: VfsNode, target_hid: tuple[int, ...]) -> bool:
        if ancestor.last_expansion_success is None:
            return False
        logger.error(f'Expansion stalled at {ancestor.hierarchical_id_str}. Failed to reach target: {target_hid}')
        if ancestor.last_expansion_success is True:
            self._report_confirmed_missing(
                target_hid, f'Ancestor {ancestor.hierarchical_id} expanded successfully, but target never appeared.'
            )
        else:
            logger.warning(
                f'Stalled at {ancestor.hierarchical_id}. Previous expansion: {ancestor.last_expansion_success}. '
                'Not purging metadata due to inconclusive expansion result.'
            )
        return True

    ###--------------------- Public API -----------------------###

    def unwrap_chain(self, node: VfsNode) -> bytes:
        '''For traveling through the VFS from physical layer to depths'''
        chain = node.chain_to_physical_source()
        if not chain:
            logger.error(f'No physical source for node: {node}. Data will be zeroed!')
            return b''
        return self._walk_chain(chain)

    def resolve_data_from_hid(self, target: tuple[int, ...] | None) -> bytes | None:
        '''
        Resolve HID to raw bytes. snapshot -> expand missing -> re-read. In order of hids indexes.
        Returning pending data first if exists.
        '''
        if not target:
            return None

        logger.debug(f'Resolving HID: {target}.')
        for _ in range(self.MAX_RESOLUTION_DEPTH):
            snapshot = self.vfs.snapshot_hids([target])  # Get VFS snapshot
            if not snapshot.unresolved:  # target is already expanded
                break
            ancestor = self.vfs.find_nearest_ancestor(target)
            if not ancestor:
                break
            if self._evaluate_stalled(ancestor, target):
                break
            self._expand_pending(snapshot.unresolved)
        else:
            raise ExpansionTimeoutError(
                f'Resolution of {target} did not resolve within {self.MAX_RESOLUTION_DEPTH} attempts. '
                f'Aborted to prevent an unbounded loop.'
            )

        node = self.vfs.get_vfs_node_by_id(target)
        if not node:
            logger.warning(f'Could not resolve HID: {target}')
            return None
        return self.read(node)

    def resolve_ghost_node(self, target_hid: tuple[int, ...], on_success: Callable[[VfsNode], None]) -> None:
        '''
        Resolve a target hid to a real VfsNode, meaning that a failed on_success will not expand (see _drill_down_to).
        on_success is synchronous for already reachable targets, otherwise async
        '''
        if not self.vfs:
            logger.warning(f'resolve_ghost_node: No VFS available')
            return
        self._drill_down_to(target_hid, on_success)

    def unpack_recursive(self, target_hid: tuple[int, ...], on_success: Callable[[list[VfsNode]], None]) -> None:
        '''Resolve target hid then expand all children recursively until no TREE_EXPAND actions remain.
        on_success does not continue to expand on failure, see _drill_down_to'''
        self.resolve_ghost_node(target_hid, lambda node: self._deep_unpack_layer([node], [], on_success))

    ###---------------------------------- Packages ----------------------------------###

    def package_members(self, node: VfsNode, intent: BaseSource.Packages.Intent = BaseSource.Packages.Intent.ACCESS) -> tuple[BaseSource.Packages.Member, ...]:
        '''What the active source says node's handler needs.'''
        if self._source_profile is None or self._links is None:
            return ()
        return self._source_profile.packages.members(node, intent, self._links)

    def ensure_package(self, node: VfsNode, intent: BaseSource.Packages.Intent, on_ready: Callable[[], None]) -> None:
        '''Ensure the package for the given node is ready, calling on_ready when done.'''
        pending = [m.hid for m in self.package_members(node, intent) if m.required]
        def _next(_: VfsNode | None = None) -> None:
            while pending and self.vfs.get_vfs_node_by_id(pending[0]) is not None:
                pending.pop(0)
            if not pending:
                on_ready()
                return
            self.resolve_ghost_node(pending[0], _next)
        _next()

    def resolve_package(self, node: VfsNode, intent: BaseSource.Packages.Intent = BaseSource.Packages.Intent.ACCESS) -> ResolvedPackage | None:
        '''Collect all the package's required members and their bytes, blocking until ready'''
        specs = self.package_members(node, intent)
        if not specs:
            return None
        members: dict[str, VfsNode] = {}
        data:    dict[str, bytes] = {}
        for spec in specs:
            if spec.required:
                member = self.vfs.get_vfs_node_by_id(spec.hid)
                raw    = self.resolve_data_from_hid(spec.hid)
            else:  # check the bytes so the VFS is not consulted (save nonexistant metadata HIDs)
                member = self.vfs.get_vfs_node_by_id(spec.hid)
                raw    = self.read(member) if member is not None else None
            if raw is None or member is None:
                if spec.required:
                    raise PackageError(f'{node}: required package member {spec.role} at {spec.hid}')
                logger.debug(f'{node}: optional package member "{spec.role}" at {spec.hid} not materialized - skipped')
                continue
            members[spec.role] = member
            data[spec.role]    = raw
        return ResolvedPackage(node, members, data)

    @contextmanager
    def open_handler(
        self,
        handler_class: type[BaseHandler],
        node:          VfsNode,
        task_handle:   TaskHandle,
        raw_bytes:     bytes,
        intent:        BaseSource.Packages.Intent = BaseSource.Packages.Intent.ACCESS,
    ) -> Iterator[BaseHandler]:
        '''Construct a handler injecting task_handle and package data.'''
        if not issubclass(handler_class, (ContainerHandler, LeafHandler)):
            raise TypeError(f'{handler_class.__name__} must be ContainerHandler or LeafHandler')
        package = self.resolve_package(node, intent)
        with handler_class(raw_bytes, node.parent) as handler:
            handler.task_handle = task_handle
            handler.package     = package
            if package is not None:
                handler.validate_package(node, package)
            yield handler

    def rollup_nodes(self, staged_nodes: list[VfsNode], task_handle: TaskHandle) -> list[VfsNode]:
        '''For Rebuilding the VFS from deepest layer to physical (children -> parent)'''
        current_queue: set[VfsNode] = set(staged_nodes)
        while not all(node.is_physical for node in current_queue): # deepest -> physical layer
            task_handle.checkpoint()
            max_depth:     int = max(len(node.hierarchical_id) for node in current_queue)
            deepest_nodes: list[VfsNode] = [n for n in current_queue if len(n.hierarchical_id) == max_depth]
            parent_map:    dict[VfsNode, list[VfsNode]] = {}

            for node in deepest_nodes: # build the parent-child map
                if node.parent is None:
                    task_handle.log_message.emit(f'Cannot roll up {node.name}; No parent node.')
                    continue
                parent_map.setdefault(node.parent, []).append(node)

            for parent, modified_children in parent_map.items(): # build the parents
                if parent.pending_data and parent not in modified_children:
                    current_queue.add(parent)
                    task_handle.log_message.emit(f'{parent.hierarchical_id} has pre-computed payload - skipping rebuild')
                    continue
                handler_class = Registry.get_handler(parent)
                if not handler_class:
                    task_handle.log_message.emit(f'No handler found for {parent.hierarchical_id}')
                    continue
                if not issubclass(handler_class, ContainerHandler):
                    logger.error(f'Subcontract {handler_class.__name__} must be ContainerHandler for virtual tree navigation.')
                    continue
                parent_bytes = self.read(parent)
                with self.open_handler(handler_class, parent, task_handle, parent_bytes) as handler:
                    result = as_package_result(handler.rebuild_node(parent, modified_children))
                    parent.pending_data = result.payload
                    self._rollup_touched.add(parent)
                    for role, data in (result.companions or {}).items():
                        member = handler.package.members.get(role) if handler.package else None
                        if member is None:
                            task_handle.log_message.emit(
                                f'{parent} returned package member {role} but its package has no such member'
                            )
                            continue
                        member.pending_data = data
                        self._rollup_touched.add(member)
                        current_queue.add(member)
                        task_handle.log_message.emit(
                            f'Package member queued: {parent} -> {role} {member.hierarchical_id}'
                        )
                    current_queue.add(parent)

            for node in deepest_nodes: # update the queue
                current_queue.discard(node)

        task_handle.log_message.emit('Virtual node roll-up complete')
        return list(current_queue)

    def precompute_packages(self, staged_nodes: list[VfsNode], task_handle: TaskHandle) -> list[VfsNode]:
        '''Cache payload/headers that are not located sequentially on disk'''
        nonseq_nodes: set[VfsNode] = set()
        staged_sorted = sorted(staged_nodes, key=lambda node: node.hierarchical_id)
        task_handle.log_message.emit(f'{len(staged_sorted)} nodes to precompute and all their children.')
        for node in staged_sorted:
            current_node = node
            while current_node is not None:
                if current_node.parent and self.package_members(current_node):
                    task_handle.log_message.emit(f'Sending {node} to cached roll-up')
                    nonseq_nodes.add(node)
                    break
                current_node = current_node.parent if current_node.is_physical is False else None

        if not nonseq_nodes:
            task_handle.log_message.emit('No nonsequential files. Nothing to precompute.')
            return []

        task_handle.log_message.emit(f'Sending {len(nonseq_nodes)} nodes to cached roll-up')
        return self.rollup_nodes(list(nonseq_nodes), task_handle)

    def clear_rollup_pending(self) -> None:
        '''Clear pending_data cached on parents/targets during the last rollup.'''
        for node in self._rollup_touched:
            node.clear_pending()
        self._rollup_touched.clear()

    ###---------------------- helpers -------------------------###

    def _expand_pending(self, unresolved: list[tuple[int,...]]) -> None:
        '''
        Find nearest ancestor for each unresolved HID and block this thread
        until each expansion completes or times out. Routes through
        request_expansion(), so this is safe to call even if a UI-driven
        expansion of the same ancestor is already in flight on another thread.
        '''
        ancestors_needed: dict[tuple[int, ...], VfsNode] = {}
        for hid in unresolved: # Deduplicate
            ancestor = self.vfs.find_nearest_ancestor(hid)
            if not ancestor:
                logger.warning(f'No registered ancestor found for HID {hid}')
                continue
            ancestors_needed.setdefault(ancestor.hierarchical_id, ancestor)

        for ancestor in ancestors_needed.values():
            event = self.request_expansion(ancestor)
            if not event.wait(timeout=self.EXPANSION_TIMEOUT):
                raise ExpansionTimeoutError(
                    f'Timeout ({self.EXPANSION_TIMEOUT}s) expanding {ancestor}: Aborted'
                )

    def _drill_down_to(
        self,
        target_hid: tuple[int, ...],
        on_success: Callable[[VfsNode], None],
    ) -> None:
        '''
        Recursively expand ancestors until target_hid becomes reachable.
        Bounded by tree depth and _evaluate_stalled which will trigger metadata cleaning
        on confirmed false entries.
        '''
        node = self.vfs.get_vfs_node_by_id(target_hid)
        if node:
            on_success(node)
            return

        ancestor = self.vfs.find_nearest_ancestor(target_hid)
        if not ancestor:
            logger.error(f'Cannot resolve {target_hid}: No ancestor exists.')
            return

        if self._evaluate_stalled(ancestor, target_hid):
            return

        logger.debug(f'Drilling down to {target_hid} from ancestor {ancestor.hierarchical_id_str}')
        def _continue(success: bool, expanded_node: VfsNode) -> None:
            if not success:
                logger.error(f'Failed to drill down to {target_hid}. Failed at {expanded_node.name} {expanded_node.hierarchical_id}.')
                return
            self._drill_down_to(target_hid, on_success)

        self.request_expansion(ancestor, _continue)

    def _deep_unpack_layer(
        self,
        queue: list[VfsNode] | deque[VfsNode],
        expanded: list[VfsNode],
        on_success: Callable[[list[VfsNode]], None],
    ) -> None:
        '''
        Consumes `queue` synchronously as long as each node resolves
        immediately via request_expansion's fast path. If an expansion goes
        async, stops. The queued callback re-enters this method once
        complete_expansion() fires for that node, from whichever thread that
        happens on.
        '''
        if not isinstance(queue, deque):  # cast list to deque for O(1) pop perf
            queue = deque(queue)
        while queue:
            node = queue.popleft()
            state_lock = threading.Lock()
            state = {'waiting': True, 'settled': False}

            def _continue(success: bool, expanded_node: VfsNode) -> None:
                if success and expanded_node.children:
                    queue.extend(expanded_node.children)
                else:
                    expanded.append(expanded_node)
                with state_lock:
                    still_waiting = state['waiting']
                    if still_waiting:
                        state['settled'] = True
                if not still_waiting:  # Async
                    self._deep_unpack_layer(queue, expanded, on_success)
            self.request_expansion(node, _continue)
            with state_lock:
                state['waiting'] = False
                handled_synchronously = state['settled']
            if not handled_synchronously:
                return  # _continue's on owning thread, halt this thread
        on_success(expanded)

    ###-------------------- Chain Traversal --------------------###

    def _walk_chain(self, chain: list[VfsNode]) -> bytes:
        '''helper to walk the path from the physical source to virtual requested file'''
        current_bytes = self.read(chain[0])
        for i in range(1, len(chain)):
            container     = chain[i -1]
            target        = chain[i]
            handler_class = Registry.get_handler(container)
            logger.debug(f'Currently at {container.hierarchical_id} searching until {target.hierarchical_id}')
            if not handler_class:
                logger.warning(f'No handler for {container.name}')
                return b''
            if not issubclass(handler_class, ContainerHandler):
                logger.error(f'Subcontract {handler_class.__name__} must be ContainerHandler for virtual tree navigation.')
                return b''
            with handler_class(current_bytes, container) as handler:
                current_bytes = handler.get_raw_node(target)
        return current_bytes

    ###---------------- Expansion Detection ---------------###

    def _needs_expansion(self, node: VfsNode) -> bool:
        '''True if node is a container that has not been expanded and registered'''
        if node.children:
            return False
        profile = Registry.get_handler_profile(node)
        if not profile:
            return False
        return issubclass(profile.handler_class, (ContainerHandler))
