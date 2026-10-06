'''
Filesystem metadata. Contains three supporting components

VfsNode (File data):
    Pure data container representing individual file or directory entries in the
    VFS hierarchy. Manages node attributes, thread-safe parent-child relationships,
    async expansion state, and pending data payloads.

VfsManager (Relational data):
    Central relational manager providing O(1) HID lookup tables for VFS and ISO
    nodes. Handles structural node mutations (insertions, removals, sentinels) and
    emits Qt signals to synchronize the UI model.

ModTracker (Mutation tracking):
    Tracks modified nodes, staged rebuild queues, and original file backups.
    Detects hierarchical conflicts and delegates filesystem conflicts to the current source profile.
'''
from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum, auto
from typing import NamedTuple, Callable, TYPE_CHECKING, Iterator, Sequence
from PyQt6.QtCore import pyqtSignal, QObject
if TYPE_CHECKING:
    from core.handlers.compression_container import CompressorHandler
    from core.contracts import BaseSource, LinkLookup

import logging
logger = logging.getLogger(f'radiata.{__name__}')


###--------------------------------------------- VFS Node ------------------------------------------------------###

class NodeStatus(Enum):
    '''Hold modification data'''
    UNMODIFIED = auto()
    MODIFIED = auto()
    STAGED = auto()

class VfsNode:
    '''Pure Data Container. All files whether iso, kods, or raw are all nodes. '''
    def __init__(
        self,
        name:      str = 'Undefined',
        category:  tuple[str, ...] = ('Unknown',),
        offset:    int = 0,
        size:      int = 0,
        header:    bytes = b'',
        extension: str | None = None,
        parent:    VfsNode | None = None,
        hid:       tuple[int, ...] = (),
        target:    tuple[int, ...] | None = None,
    ):
        self.name     = name                                # semantic name from overrides
        self.category = category                            # semantic category derived from disk index
        self.parent   = parent                              # parent node (None = Root)
        self.children: list[VfsNode] = []                   # children node(s)

        # offset and size are set synchronously at init but are lock protected after since they are mutable from background threads
        self._offset = offset                               # Relative offset into parent
        self._size   = size                                 # Size of node in bytes (VirtualFile=disk[offset:offset+size])
        self.target: tuple[int,...] | None = target         # Header HID for unpacking datacenter

        self.extension = extension                          # extension from override saved in radi_metadata
        self.parent_header: CompressorHandler.SlzHeader | bytes | None = None    # Original header of the parent node used for rebuilding children
        self.logical_id: int | None = None                  # Logical ID from the TOC used for ISO rebuilding

        self._id_path: tuple[int, ...] = hid                # hierarchical id (root, sub, subsub)

        self.status = NodeStatus.UNMODIFIED                 # node state
        self._pending_data: bytes | None = None             # cached data

        self.is_physical   = False                          # Has physical address
        self.is_hidden     = False                          # Hide node in UI (file system related or null nodes by default)
        self.is_boundary   = False                          # The entrypoint node for the VFS, as specified by the source profile

        self._lock                   = threading.RLock()     # Structural lock for parent-child mutations/access
        self.expansion_pending: bool = False                 # True while an expansion is in-flight
        self._expansion_lock         = threading.Lock()      # Expansion lock for expansion state
        self.last_expansion_success: bool | None = None      # Outcome of the last expansion
        self._expansion_event: threading.Event | None = None # Set when expansion completes

        self._row: int = 0                                   # Cached row index within parent

    def append_child(self, child: VfsNode):
        '''
        Called to add a child node to this node.
        Keeps separate HID increments for ISO and VFS nodes, split by boundary flag.
        '''
        with self._lock:
            self.children.append(child)
            child.parent = self
            child._row = len(self.children) - 1
            if self.is_boundary:
                child._id_path = (child._row,)
            else:
                child._id_path = self._id_path + (child._row,)

    def make_sentinel(self):
        '''
        Called to wipe this node's data replacing it with an empty sentinel slot.
        Children are dropped from the VfsNode object but not yet removed from VfsManager.
        HIDs for all nodes existing pre-post mutation are kept identical.
        '''
        with self._lock:
            self.name          = f'sentinel_{self._row}'
            self.size          = 0
            self.offset        = -1
            self.category      = ('Unknown',)
            self.extension     = None
            self.parent_header = None
            self.is_physical   = False
            self.is_hidden     = True
            self._pending_data = None
            self.status        = NodeStatus.UNMODIFIED
            self.children      = []
        # Wake and reset thread state for async expansion on the node
        with self._expansion_lock:
            self.expansion_pending = False
            self.last_expansion_success = None
            if self._expansion_event:
                self._expansion_event.set()
                self._expansion_event = None

    @property
    def children_snapshot(self) -> list[VfsNode]:
        '''Get a thread-safe snapshot of the children nodes'''
        with self._lock:
            return self.children.copy()

    @property
    def pending_data(self) -> bytes | None:
        '''Get the pending data for this node'''
        with self._lock:
            return self._pending_data

    @pending_data.setter
    def pending_data(self, value: bytes | None) -> None:
        '''Set the pending data for this node'''
        with self._lock:
            self._pending_data = value

    @property
    def is_sentinel(self) -> bool:
        with self._lock:
            return self._pending_data is None and (self._offset == -1 or self._size < 0)

    @property
    def size(self) -> int:
        with self._lock:
            return self._size

    @size.setter
    def size(self, value: int) -> None:
        with self._lock:
            self._size = value

    @property
    def offset(self) -> int:
        with self._lock:
            return self._offset

    @offset.setter
    def offset(self, value: int) -> None:
        with self._lock:
            self._offset = value

    @property
    def hierarchical_id(self) -> tuple[int, ...]:
        '''Return tuple id'''
        return self._id_path

    @property
    def hierarchical_id_str(self) -> str:
        '''Return human readable id'''
        return '.'.join(map(str, self._id_path)) if self._id_path else '0'

    @property
    def visible_children(self) -> list[VfsNode]:
        return [child for child in self.children if not getattr(child, 'is_hidden', False)]

    def row(self) -> int:
        '''Keep track of the children-parent links for tree view'''
        if self.parent is None:
            return 0
        with self._lock:
            return self._row

    def begin_expansion(self) -> tuple[bool, threading.Event]:
        '''Mark expansion in progress. Return wait event.
        If an expansion is already pending and an event exists, return the existing
        event so that a second waiter blocks on the same event the running task will set.'''
        with self._expansion_lock:
            if self.expansion_pending and self._expansion_event is not None:
                return False, self._expansion_event
            self._expansion_event = threading.Event()
            self.expansion_pending = True
            return True, self._expansion_event

    def finish_expansion(self, success: bool) -> None:
        '''Release expansion ownership, record the outcome and wake any waiting threads.
        Always called from the same caller as begin_expansion.'''
        with self._expansion_lock:
            self.expansion_pending = False
            self.last_expansion_success = success
            event = self._expansion_event
            self._expansion_event = None
        if event:
            event.set()

    def clear_pending(self):
        with self._lock:
            self.pending_data = None
            self.status = NodeStatus.UNMODIFIED

    @property
    def depth(self) -> int:
        '''Depth in the respective iso/vfs tree.'''
        return len(self._id_path)

    def walk_to_physical(self) -> Iterator[VfsNode]:
        '''
        Yield self, then each ancestor in turn, stopping before crossing out
        of this node's own filesystem (when yielded node.is_physical). If somehow we
        miss a node.is_physical in the chain it is bounded to the tree.
        '''
        node: VfsNode | None = self
        while node is not None:
            yield node
            if node.is_physical:
                return
            node = node.parent

    def nearest_physical_ancestor(self) -> VfsNode | None:
        '''The nearest node flagged is_physical, or None if the chain never reaches one.'''
        last: VfsNode | None = None
        for last in self.walk_to_physical():
            pass
        if last is None or not last.is_physical:
            logger.warning(f'{self}: no is_physical ancestor found, tree may be corrupt.')
            return None
        return last

    def chain_to_physical_source(self) -> list[VfsNode]:
        '''Return an ordered list of node to reach the source.'''
        chain = list(self.walk_to_physical())
        if not chain or not chain[-1].is_physical:
            logger.warning(f'{self}: no is_physical ancestor found, tree may be corrupt.')
            return []
        chain.reverse()
        return chain

    def __repr__(self) -> str:
        return f'{self.name}{self.extension} ({self.hierarchical_id_str})'

###------------------------------------------------------- VFS Manager -----------------------------------------------------###

class HidSnapshot(NamedTuple):
    '''Result of a lock-protect VFS snapshot'''
    resolved: list[VfsNode]             # nodes already in the VFS
    unresolved: list[tuple[int, ...]]   # HIDs whose parent need expansion

class VfsManager(QObject):
    '''
    Holds O(N) node lookup tables for VFS and ISO nodes.

    Registration is valid for both VFS and ISO nodes during init.
    After which, the only way to register new ISO nodes is to add a child to root.
    '''
    insert_start      = pyqtSignal(VfsNode, int, int) # (parent, first_row, last_row)
    insert_finished   = pyqtSignal()
    remove_start      = pyqtSignal(VfsNode, int, int)
    remove_finished   = pyqtSignal()
    node_dataChanged  = pyqtSignal(VfsNode)

    request_extension = pyqtSignal(VfsNode)           # Request extension for a node that has '.bin' extension

    def __init__(
        self,
        root:          VfsNode,
        vfs_root:      VfsNode,
        node_enricher: Callable[[VfsNode], None] | None = None,
    ) -> None:
        super().__init__()
        self.root        = root
        self.vfs_root    = self._validate_vfs_boundary(vfs_root)
        self.enrich_node = node_enricher
        self._lock       = threading.RLock()

        self.vfs_nodes_by_id:  dict[tuple[int, ...], VfsNode] = {}  # VFS-only hid lookup map
        self.iso_nodes_by_id:  dict[tuple[int, ...], VfsNode] = {}  # ISO-only hid lookup map, only mutated during init
        self.physical_offsets: dict[VfsNode, int] = {}              # Physical disk map
        self._register_recursive(self.root, self.iso_nodes_by_id)   # Register physical nodes with VFS initilization

    @staticmethod
    def _validate_vfs_boundary(vfs_root: VfsNode) -> VfsNode:
        if not vfs_root.is_boundary:
            raise ValueError(f'VfsManager requires an already resolved VFS boundary node: got {vfs_root!r}')
        return vfs_root

    ###--------------------- Registration ----------------------###

    def _register_recursive(self, node: VfsNode, target_dict: dict[tuple[int, ...], VfsNode]) -> None:
        '''
        Register node and all its children.
        Node is registered in the target_dict (vfs or iso).

        After initialization, must be called inside _lock.
        '''
        target_dict[node.hierarchical_id] = node
        self.physical_offsets[node] = node.offset

        next_dict = self.vfs_nodes_by_id if node is self.vfs_root else target_dict
        for child in node.children:
            self._register_recursive(child, next_dict)

    def insert_children(self, parent: VfsNode, new_children: list[VfsNode]) -> None:
        '''
        Update the VFS and signal to the tree model.

        Enforces all iso level nodes to be registered at initialization, not lazily via insert_children.
        Because of the registration enforcement all iso level nodes are depth 1 only.
        Not really a problem for anything unless the project expands to splitting the kernel image or main ELF.
        Could also be a problem if some kind of custom ISO level structure is modded in.
        '''
        if not new_children:
            return
        assert parent is not self.vfs_root, (
            'vfs_entry\'s children must be populated at initialization, not lazily via insert_children'
        )
        with self._lock:
            base_idx = len(parent.children)
            self.insert_start.emit(parent, base_idx, base_idx + len(new_children) - 1)
            for i, child in enumerate(new_children): # Add the nodes to the file system / tree
                child.parent = parent
                child._id_path = parent._id_path + (base_idx + i,)
                child._row = base_idx + i
                child.is_hidden = bool(parent.is_hidden or not child.size or child.offset == -1)
                if self.enrich_node:
                    self.enrich_node(child)
                if not child.extension and not child.is_hidden and not child.is_sentinel:
                    self.request_extension.emit(child)
                parent.children.append(child)
                self._register_recursive(child, self.vfs_nodes_by_id)
            self.insert_finished.emit()

    # def remove_child(self, node: VfsNode, on_remove: Callable[[VfsNode], None] | None = None) -> None:
    #     '''Replace node's own data with a sentinel in place. All children are dropped.'''
    #     if node is self.vfs_root or node.is_boundary:
    #         logger.warning('VFS boundary node is crucial, aborting data clear.')
    #         return
    #     if on_remove:
    #         on_remove(node)
    #     with self._lock:
    #         self.remove_node(node)

    # def remove_children(self, parent: VfsNode, on_remove: Callable[[VfsNode], None] | None = None) -> None:
    #     '''Recursively fully delete children from parent'''
    #     with self._lock:
    #         direct_children = parent.children_snapshot
    #         if not direct_children:
    #             return
    #         if on_remove:
    #             for child in direct_children:
    #                 on_remove(child)
    #         for child in direct_children:
    #             self.remove_node(child)

    def update_node(self, node: VfsNode) -> None:
        '''Signal to the UI that a node's data has changed, redraw.'''
        self.node_dataChanged.emit(node)

    def enrich_initial_tree(self) -> None:
        '''
        Walk the VFS after initialization enriching nodes with metadata.
        No need for extra metadata on ISO level nodes since the root
        directory + system.cnf supplies enough context.
        '''
        if not self.enrich_node:
            return
        logger.warning(f'Enriching initial tree, the children of {self.vfs_root}')
        for child in self.vfs_root.children:
            logger.warning(f'Enriching child: {child.name}, size: {child.size}, offset {child.offset}')
            self.enrich_node(child)
            if child.is_boundary:
                continue
            if not child.extension or child.extension == '.bin':
                self.request_extension.emit(child)
        logger.debug('VfsManager.enrich_initial_tree: complete')

    ###--------------------- Remove -----------------------###

    def remove_slotted_node(self, node: VfsNode) -> None:
        '''
        Sentinels this node and delets all children from the tree.
        For keeping the indexes consistent while still clearing data.
        '''
        if node in (self.root, self.vfs_root) or 'System' in node.category:
            logger.warning(f'Cannot perform slotted removal on crucial system node: {node}')
            return
        with self._lock:
            with node._lock:
                num_children = len(node.children)
                if num_children > 0:
                    self.remove_start.emit(node, 0, num_children - 1)
                    for child in node.children_snapshot:
                        self._unlink_descendant_recursive(child)
                node.make_sentinel()
                if num_children > 0:
                    self.remove_finished.emit()
            self.physical_offsets.pop(node, None)
            self.node_dataChanged.emit(node)

    def remove_node_children(self, node: VfsNode) -> None:
        '''
        Deleted this node's children, and reset this node's expansion state.
        For importing a parent node that already has children so that
        expansion can be run again against the new data.
        '''
        if node is self.root or node is self.vfs_root:
            logger.warning(f'Cannot perform children removal on root node: {node}')
            return
        with self._lock, node._lock:
            num_children = len(node.children)
            if num_children == 0:
                return
            self.remove_start.emit(node, 0, num_children - 1)
            for child in node.children:
                self._unlink_descendant_recursive(child)
            # This is a temporary node reset. When a new raw payload is imported for a node
            # the dispatcher should run the new payload against registered handlers to
            # see if they contain a primary_expansion_action and if so the payload should
            # be verified before commiting. Based on this the node reseting will most likely
            # need a new soft-reset function that keeps the data required by it's parents
            # but drops everything that could be required of for children
            node.children.clear()
            with node._expansion_lock:
                node.expansion_pending = False
                node.last_expansion_success = None
                if node._expansion_event:
                    node._expansion_event.set()
                    node._expansion_event = None
            self.remove_finished.emit()

    def remove_node(self, node: VfsNode) -> None:
        '''
        Removes this node and all children.
        WARNING: deleting a node like this will recalculate the indexes and
        can causes issues with indexed lookups. Use remove_slotted_node for
        keeping indexing consistent.
        '''
        if node in (self.root, self.vfs_root) or 'System' in node.category:
            logger.warning(f'Cannot perform node removal for crucial node: {node}')
            return
        parent=node.parent
        if parent is None:
            logger.warning(f'Cannot perform targeted removal on parentless node: {node}')
            return
        with self._lock, parent._lock, node._lock:
            if node not in parent.children:
                return
            row = parent.children.index(node)
            self.remove_start.emit(parent, row, row)
            self._unlink_descendant_recursive(node)
            parent.children.pop(row)
            node.parent = None
            for i in range(row, len(parent.children)):
                child = parent.children[i]
                old_hid = child.hierarchical_id
                child._row = i
                new_hid = (i,) if parent.is_boundary else parent._id_path + (i,)
                self._rekey_shifted_node(child, new_hid)
            self.remove_finished.emit()

    def _rekey_shifted_node(self, node: VfsNode, new_id_path: tuple[int, ...]) -> None:
        '''
        Ensure that the iso and vfs hid's stay consistent and keep the required indexes.
        Shifts the node first then cascades down.
        '''
        old_hid = node.hierarchical_id
        if old_hid != new_id_path:
            node._id_path = new_id_path
            if old_hid in self.vfs_nodes_by_id:
                self.vfs_nodes_by_id.pop(old_hid)
                self.vfs_nodes_by_id[new_id_path] = node
            elif old_hid in self.iso_nodes_by_id:
                self.iso_nodes_by_id.pop(old_hid)
                self.iso_nodes_by_id[new_id_path] = node
        for i, child in enumerate(node.children):
            child_path = (i,) if node.is_boundary else new_id_path + (i,)
            self._rekey_shifted_node(child, child_path)

    def _unlink_descendant_recursive(self, node: VfsNode) -> None:
        '''
        Helper to deeply purge descendant nodes from anything held by VfsManager.
        Calls node.children directly from inside node._lock, can't use children_snapshot here
        as that introduces a race condiiton.
        '''
        for child in node.children:
            self._unlink_descendant_recursive(child)
        self.vfs_nodes_by_id.pop(node.hierarchical_id, None)
        self.iso_nodes_by_id.pop(node.hierarchical_id, None)
        self.physical_offsets.pop(node, None)

    ###--------------------- Lookup -----------------------###

    def get_offset(self, node: VfsNode) -> int:
        '''Get physical disk offsets'''
        return self.physical_offsets.get(node, 0)

    def get_vfs_node_by_id(self, hid: tuple[int, ...]) -> VfsNode | None:
        '''Node lookup for known vfs registered nodes.'''
        with self._lock:
            return self.vfs_nodes_by_id.get(hid)

    def get_iso_node_by_id(self, hid: tuple[int, ...]) -> VfsNode | None:
        '''Node lookup for known iso registered nodes.'''
        with self._lock:
            return self.iso_nodes_by_id.get(hid)

    ###-------------------- Navigator API ------------------###

    def snapshot_hids(self, hids: list[tuple[int,...]]) -> HidSnapshot:
        '''Lock-protected snapshot. Return nodes already in the VFS'''
        resolved:   list[VfsNode] =[]
        unresolved: list[tuple[int,...]] = []

        with self._lock:
            for hid in hids:
                node = self.vfs_nodes_by_id.get(hid)
                if node:
                    resolved.append(node)
                else:
                    unresolved.append(hid)

        return HidSnapshot(resolved, unresolved)

    def find_nearest_ancestor(self, hid: tuple[int,...]) -> VfsNode | None:
        '''Return the nearest ancestor for an HID, the node that needs expanding'''
        with self._lock:
            best: VfsNode | None = None
            for depth in range(1, len(hid)):
                ancestor = self.vfs_nodes_by_id.get(hid[:depth])
                if ancestor:
                    best = ancestor
            return best

    def _find_child_by_path(self, parent: VfsNode, target: tuple[int,...]) -> VfsNode | None:
        '''Scan children for match. Must be called from _lock.'''
        for child in parent.children:
            if child.hierarchical_id == target:
                return child
        return None

###------------------------------------ Status Tracker ---------------------------------------###

class ConflictInfo(NamedTuple):
    other:  VfsNode # the other node in conflict
    reason: str     # explanation of the cause of conflict

class NodeConflictError(Exception):
    '''
    Raised by ModTracker.apply_modification/mark_modified when a modification would
    conflict with an existing pending edit. Should be caught by the dispatcher.

    ModTracker only reports conflicts, the dispatcher is responsible for bridging the conflict to the
    UI for resolution. Then applying the desired outcome through the ModTracker API.
    '''

    def __init__(self, node: VfsNode, conflicts: list[ConflictInfo]) -> None:
        assert conflicts, 'NodeConflictError requires at least one conflict'
        self.node       = node      # the node the caller was trying to modify
        self.conflicts  = conflicts # every ConflictInfo that collides with this modification
        super().__init__(self.reason)

    @property
    def others(self) -> list[VfsNode]:
        '''Every 'other' node this modifiaction collides with'''
        return [c.other for c in self.conflicts]

    @property
    def others_str(self) -> str:
        return ', '.join(o.__repr__() for o in self.others)

    @property
    def reason(self) -> str:
        '''Every conflict's reason, combined into one display-ready string.'''
        return '\n'.join(c.reason for c in self.conflicts)

    def __repr__(self) -> str:
        others = [o.hierarchical_id for o in self.others]
        return f'NodeConflictError(node={self.node.hierarchical_id_str}, other={others})'

@dataclass(eq=False)
class ModGroup:
    '''
    One atomic pending file change.
    Contains a group of members so that packages are treated as a single unit.
    Package conflicts are checked by group.
    The first member is the primary node (user acted).
    '''
    id:      int
    members: tuple[VfsNode, ...]
    staged:  bool = False

    @property
    def primary(self) -> VfsNode:
        return self.members[0]

    @property
    def name(self) -> str:
        extra = len(self.members) - 1
        return self.primary.name if not extra else f'{self.primary.name} (+{extra} linked)'

    @property
    def size(self) -> int:
        return sum(node.size for node in self.members)

    def __iter__(self) -> Iterator[VfsNode]:
        return iter(self.members)

    def __len__(self) -> int:
        return len(self.members)

    def __repr__(self) -> str:
        return f'ModGroup(id={self.id}, members={self.members}, staged={self.staged})'

class ModTracker(QObject):
    '''Pure state tracker for modified nodes, organized in ModGroups.'''
    node_modified = pyqtSignal(VfsNode)
    node_reverted = pyqtSignal(VfsNode)
    state_changed = pyqtSignal(int, int)              # (unstaged_count, staged_count) node counts not group counts
    conflict_detected = pyqtSignal(VfsNode, str)      # (VfsNode, reason)
    conflict_resolved = pyqtSignal(VfsNode)           # (VfsNode)

    def __init__(self) -> None:
        super().__init__()
        self.modified_nodes: set[VfsNode] = set()
        self.rebuild_queue:  set[VfsNode] = set()
        self._originals:     dict[VfsNode, bytes] = {}
        self.conflicts:      set[VfsNode] = set()
        self._groups:        dict[int, ModGroup] = {}
        self._group_of:      dict[VfsNode, ModGroup] = {}
        self._next_group:    int = 0
        self._source:        BaseSource | None = None
        self._links:         LinkLookup | None = None
        self._lock = threading.RLock()

    def configure_source(self, source: BaseSource | None, links: LinkLookup | None) -> None:
        with self._lock:
            self._source = source
            self._links = links

    def _emit_state(self):
        self.state_changed.emit(len(self.modified_nodes), len(self.rebuild_queue))


    ###----------------------------------- Group Management -------------------------------------###

    def group_of(self, node: VfsNode) -> ModGroup | None:
        with self._lock:
            return self._group_of.get(node)

    def unstaged_groups(self) -> list[ModGroup]:
        with self._lock:
            return sorted((group for group in self._groups.values() if not group.staged),
                          key=lambda group: group.name)

    def staged_groups(self) -> list[ModGroup]:
        with self._lock:
            return sorted((group for group in self._groups.values() if group.staged),
                          key=lambda group: group.name)

    def staged_nodes(self) -> list[VfsNode]:
        with self._lock:
            return [node for group in self.staged_groups() for node in group.members]

    ###----------------------------------- Modification -------------------------------------###

    def apply_modification(
        self,
        node:         VfsNode,
        new_data:     bytes,
        data_sources: Callable[[VfsNode], bytes],
        *,
        force:        bool = False,
    ) -> bytes:
        return self.apply_group([(node, new_data)], data_sources, force=force)[node]

    def apply_group(
        self,
        changes:      Sequence[tuple[VfsNode, bytes]],
        data_sources: Callable[[VfsNode], bytes],
        *,
        force:        bool = False,
    ) -> dict[VfsNode, bytes]:
        '''
        Apply a group of modifications as a ModGroup, this ensures that members never
        conflict with each other.
        Returns node and original data.
        '''
        if not changes:
            return {}
        applied: dict[VfsNode, bytes] = {}
        with self._lock:
            group_nodes = frozenset(n for n, _ in changes)
            if len(group_nodes) != len(changes):
                raise ValueError('apply_group: duplicate nodes in one group')
            found: list[tuple[VfsNode, list[ConflictInfo]]] = []
            for node, _ in changes:
                conflicts = self._conflicts_for(node, group_nodes, check_package=True)
                if conflicts:
                    found.append((node, conflicts))
            if found and not force:
                raise NodeConflictError(*found[0])
            conflicted = {node for node, _ in found}
            for node, conflicts in found:
                combined_reason = '; '.join(c.reason for c in conflicts)
                logger.warning(f'Conflict on {node} forced through: {combined_reason}')
                if node not in self.conflicts:
                    self.conflicts.add(node)
                    self.conflict_detected.emit(node, combined_reason)
            for node, _ in changes:
                if node not in conflicted:
                    self.resolve_conflict(node)
            #
            for old in {self._group_of[node] for node in group_nodes if node in self._group_of}:
                remaining = tuple(node for node in old.members if node not in group_nodes)
                for node in old.members:
                    self._group_of.pop(node, None)
                if remaining:
                    shrunk = ModGroup(old.id, remaining, old.staged)
                    self._groups[old.id] = shrunk
                    for node in remaining:
                        self._group_of[node] = shrunk
                else:
                    self._groups.pop(old.id, None)

            group = ModGroup(self._next_group, tuple(node for node, _ in changes))
            self._next_group += 1
            self._groups[group.id] = group
            for node, new_data in changes:
                previous = node.pending_data
                if node not in self._originals:
                    self._originals[node] = previous if previous is not None else data_sources(node)
                if previous is None:
                    previous = self._originals[node]
                self.rebuild_queue.discard(node)
                node.pending_data = new_data
                node.size         = len(new_data)
                node.status       = NodeStatus.MODIFIED
                self.modified_nodes.add(node)
                self._group_of[node] = group
                applied[node] = previous
        for node, _ in changes:
            self.node_modified.emit(node)
        self._emit_state()
        return applied

    def mark_modified(self, node: VfsNode, new_data: bytes, original_data: bytes, *, force: bool = False) -> None:
        '''Back compatible temp entrypoint for callers that already have original data.'''
        self.apply_modification(node, new_data, data_sources=lambda n: original_data, force=force)

    def get_original(self, node: VfsNode) -> bytes:
        with self._lock:
            return self._originals.get(node, b'')

    def has_original(self, node: VfsNode) -> bool:
        with self._lock:
            return node in self._originals

    ###-------------------------------------- Staging - Group ---------------------------------------###
    #
    def stage_group(self, group: ModGroup) -> list[ConflictInfo]:
        '''Move from cache to staging area'''
        with self._lock:
            if self._groups.get(group.id) is not group or group.staged:
                return []
            conflicts = self.group_conflicts(group)
            if conflicts:
                return conflicts
            for node in group.members:
                self.modified_nodes.discard(node)
                self.rebuild_queue.add(node)
                node.status = NodeStatus.STAGED
            group.staged = True
        self._emit_state()
        return []

    def unstage_group(self, group: ModGroup) -> None:
        '''Move from staging back to cache'''
        with self._lock:
            if self._groups.get(group.id) is not group or not group.staged:
                return
            for node in group.members:
                self.rebuild_queue.discard(node)
                self.modified_nodes.add(node)
                node.status = NodeStatus.MODIFIED
            group.staged = False
        self._emit_state()

    def stage_all(self) -> dict[ModGroup, list[ConflictInfo]]:
        '''Stage every group in the queue, returns conflicts if any.'''
        refused: dict[ModGroup, list[ConflictInfo]] = {}
        for group in self.unstaged_groups():
            conflicts = self.stage_group(group)
            if conflicts:
                refused[group] = conflicts
        return refused

    def unstage_all(self) -> None:
        for group in self.staged_groups():
            self.unstage_group(group)

    def revert_group(self, group: ModGroup) -> None:
        '''Discard a whole group's changes'''
        with self._lock:
            if self._groups.get(group.id) is not group:
                return
            members = list(group.members)
            self._drop_group(group)
            for node in members:
                self._revert_locked(node)
            self._refresh_conflict_flags()
        for node in members:
            self.node_reverted.emit(node)
            logger.info(f'Reverted changes for node: {node}')
        self._emit_state()

    def revert_all(self) -> None:
        for group in self.unstaged_groups() + self.staged_groups():
            self.revert_group(group)

    ###-------------------------------------- Staging - Node -----------------------------------###

    def stage_node(self, node: VfsNode) -> None:
        group = self.group_of(node)
        if group is not None:
            self.stage_group(group)

    def unstage_node(self, node: VfsNode) -> None:
        group = self.group_of(node)
        if group is not None:
            self.unstage_group(group)

    def revert_node(self, node: VfsNode) -> None:
        '''Discard changes and revert the group's members if applicable.'''
        group = self.group_of(node)
        if group is not None:
            self.revert_group(group)

    def _drop_group(self, group: ModGroup) -> None:
        self._groups.pop(group.id, None)
        for node in group.members:
            if self._group_of.get(node) is group:
                del self._group_of[node]

    def _revert_locked(self, node: VfsNode) -> None:
        self.modified_nodes.discard(node)
        self.rebuild_queue.discard(node)
        if node in self._originals:
            node.size = len(self._originals[node])
        self._originals.pop(node, None)
        node.clear_pending()
        node.status = NodeStatus.UNMODIFIED
        self.resolve_conflict(node)

    def clear(self) -> None:
        '''Clear state when closing an ISO'''
        with self._lock:
            for node in self.modified_nodes | self.rebuild_queue | set(self._originals):
                node.clear_pending()
            self.modified_nodes.clear()
            self.rebuild_queue.clear()
            self._originals.clear()
            self._group_of.clear()
            self._groups.clear()
            self.conflicts.clear()
        self._emit_state()

    def clear_subtree(self, node: VfsNode) -> None:
        '''Clears all state tracking for a node and all it's children.'''
        outside: list[VfsNode] = []
        with self._lock:
            stack = [node]
            subtree: set[VfsNode] = set()
            while stack:
                current = stack.pop()
                subtree.add(current)
                stack.extend(current.children)
            for group in {self._group_of[node] for node in subtree if node in self._group_of}:
                self._drop_group(group)
                outside.extend(node for node in group.members if node not in subtree)
            for n in subtree:
                if n in self.modified_nodes or n in self.rebuild_queue or n in self._originals:
                    n.clear_pending()
                self.modified_nodes.discard(n)
                self.rebuild_queue.discard(n)
                self._originals.pop(n, None)
                self.resolve_conflict(n)
            for node in outside:
                self._revert_locked(node)
            self._refresh_conflict_flags()
        for node in outside:
            self.node_reverted.emit(node)
        self._emit_state()

    ###---------------------------- Conflict -----------------------------###

    def _pending_excluding(self, node: VfsNode, exclude: frozenset[VfsNode] | None) -> frozenset[VfsNode]:
        '''Pending edits other than node and exclude'''
        if exclude is None:
            group   = self._group_of.get(node)
            exclude = frozenset(group.members) if group is not None else frozenset()
        return frozenset((self.modified_nodes | self.rebuild_queue) - exclude - {node})

    def find_hierarchical_conflicts(self, incoming_node: VfsNode, exclude: frozenset[VfsNode] | None = None) -> list[ConflictInfo]:
        '''Detect and report ancestor/descendant data corruption cases. Pure tree datatype shape.'''
        pending = self._pending_excluding(incoming_node, exclude)
        conflicts: list[ConflictInfo] = []
        # Child modification may not conflict if the pending data is the source that is edited.
        # This depends on how I have the rebuild setup but currently because of leaf -> source
        # rebuilding the child is overwritten by the parent.
        for ancestor in incoming_node.walk_to_physical():
            if ancestor is incoming_node:
                continue
            if ancestor in pending:
                conflicts.append(ConflictInfo(
                    ancestor,
                    (f'Parent node {ancestor} has pending modifications. '
                     f'Modifications to child {incoming_node} may be '
                     'overwritten by the parent modifications during rebuild.')
                ))
        for descendant in pending:
            if incoming_node in descendant.walk_to_physical():
                conflicts.append(ConflictInfo(
                    descendant,
                    (f'Child node {descendant} has pending modifications. '
                     f'Modifying parent {incoming_node} may overwrite '
                     'the child\'s pending data.')
                ))
        return conflicts

    def find_source_conflicts(self, incoming_node: VfsNode, exclude: frozenset[VfsNode] | None = None) -> list[ConflictInfo]:
        '''Filesystem conflicts as defined by the active source profile.'''
        if self._source is None or self._links is None:
            return []
        pending = self._pending_excluding(incoming_node, exclude)
        if not pending:
            return []
        return [ConflictInfo(f.other, f.reason) for f in self._source.find_conflicts(incoming_node, pending, self._links)]

    def _conflicts_for(
        self,
        node:          VfsNode,
        exclude:       frozenset[VfsNode] | None,
        check_package: bool = False
    ) -> list[ConflictInfo]:
        '''All conflicts for a node outside of the current group.'''
        found: list[ConflictInfo] = []
        if check_package:
            group = self._group_of.get(node)
            if group is not None and exclude is not None:
                for peer in group.members:
                    if peer is not node and peer not in exclude:
                        found.append(ConflictInfo(
                            peer,
                            (f'{node} was modified together with {peer} as one package. '
                             f'Editing it alone may break the package integrity.')
                        ))
        found += self.find_hierarchical_conflicts(node, exclude=exclude)
        found += self.find_source_conflicts(node, exclude=exclude)
        unique: dict[VfsNode, ConflictInfo] = {}
        for conflict in found:
            unique.setdefault(conflict.other, conflict)
        return list(unique.values())

    def group_conflicts(self, groups: ModGroup) -> list[ConflictInfo]:
        '''Conflicts between a group and anything outside of it.'''
        with self._lock:
            members = frozenset(groups.members)
            unique: dict[VfsNode, ConflictInfo] = {}
            for node in groups.members:
                for conflict in self._conflicts_for(node, members):
                    unique.setdefault(conflict.other, conflict)
            return list(unique.values())

    def _refresh_conflict_flags(self) -> None:
        '''Drop conflict flags whose conflicts no longer exist'''
        for node in list(self.conflicts):
            if node not in self.modified_nodes and node not in self.rebuild_queue or not self._conflicts_for(node, None):
                self.resolve_conflict(node)

    def has_conflict(self, node: VfsNode) -> bool:
        '''Whether node currently has any collisions with any pending edits outside of its group'''
        with self._lock:
            return bool(self._conflicts_for(node, None))

    def find_conflicts(self) -> list[tuple[VfsNode, ConflictInfo]]:
        '''Tracker-wide scan for any conflicts in the pending modification lists.'''
        with self._lock:
            results: list[tuple[VfsNode, ConflictInfo]] = []
            for node in self.modified_nodes | self.rebuild_queue:
                for conflict in self._conflicts_for(node, None):
                    results.append((node, conflict))
            return results

    def resolve_conflict(self, node: VfsNode) -> None:
        '''Clears the conflict state for node safely within the lock.'''
        with self._lock:
            if node in self.conflicts:
                self.conflicts.remove(node)
                logger.info(f'Conflict resolved for {node}')
                self.conflict_resolved.emit(node)
