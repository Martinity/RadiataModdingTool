'''Holds all models for the filebrowser. Takes nodes and converts their data into file browser tree displayable formats.
VfsTreeModel - QAbstractItemModel, the hierarchical tree
SearchModel - QAbstractListModel, the search list. List due to recursive hierarchical searching chugging UI
TreeProxyModel - QSortFilterProxyModel, only gets applied to the tree. Search is hardcoded to avoid hidden nodes'''
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from PyQt6.QtCore import QAbstractItemModel, QModelIndex, Qt, QSortFilterProxyModel, QAbstractListModel, QTimer
from PyQt6.QtGui import QColor
from core.node import VfsManager, Address
from core.metadata_manager import key_to_address, address_to_key
from utilities import human_size
if TYPE_CHECKING:
    from core.node import VfsNode
    from core.metadata_manager import NodeMetadataStore

import logging
logger = logging.getLogger(f'radiata.{__name__}')

class VfsTreeModel(QAbstractItemModel):
    '''Responsible for all tree gui data/graphics'''
    def __init__(self, vfs_manager: VfsManager, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.vfs_manager = vfs_manager
        self.root_node = self.vfs_manager.root
        self.columns = ["ID", "File Name", "Size"]

        # Catch VfsManager signals for updating tree
        self.vfs_manager.insert_start.connect(self._on_insert_start)
        self.vfs_manager.insert_finished.connect(self._on_insert_finished)
        self.vfs_manager.remove_start.connect(self._on_remove_start)
        self.vfs_manager.remove_finished.connect(self._on_remove_finished)
        self.vfs_manager.node_dataChanged.connect(self._on_node_dataChanged)

    ###---------------------------------------- Qt API --------------------------------------###

    def columnCount(self, parent=QModelIndex()) -> int:
        '''Draw the columns'''
        return len(self.columns)

    def rowCount(self, parent=QModelIndex()) -> int:
        """Returns the number of children under a specific parent."""
        if parent.column() > 0:
            return 0

        parent_node = self.get_node(parent)
        return len(parent_node.children)

    def index(self, row: int, column: int, parent=QModelIndex()) -> QModelIndex:
        """Creates a QModelIndex pointing to a specific child."""
        if not self.hasIndex(row, column, parent):
            return QModelIndex()

        parent_node = self.get_node(parent)
        if row >= len(parent_node.children):
            return QModelIndex()

        child_node = parent_node.children[row]
        return self.createIndex(row, column, child_node) if child_node is not None else QModelIndex()

    def parent(self, index: QModelIndex) -> QModelIndex:
        """Finds the parent of a given index."""
        if not index.isValid():
            return QModelIndex()

        child_node: VfsNode = index.internalPointer()
        parent_node: VfsNode | None = child_node.parent

        # If the parent is the root node, we return an empty index (top level)
        if parent_node == self.root_node or parent_node is None:
            return QModelIndex()

        # Otherwise, create an index for the parent based on ITS row
        return self.createIndex(parent_node.row(), 0, parent_node)

    # Draw data
    def get_node(self, index: QModelIndex) -> VfsNode:
        """Helper to extract a VfsNode from a Qt index."""
        if index.isValid():
            node = index.internalPointer()
            if node:
                return node
        return self.root_node

    def data(self, index: QModelIndex, role: Qt.ItemDataRole):
        if not index.isValid():
            return None

        node: VfsNode = index.internalPointer()

        # What text to display in the UI cells
        if role == Qt.ItemDataRole.DisplayRole:
            col = index.column()
            if col == 0:
                return address_to_key(node.address)
            if col == 1:
                return node.name + node.extension if node.extension else node.name
            if col == 2:
                return human_size(node.size)

        if role == Qt.ItemDataRole.UserRole:
            return node

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        '''Enable selection and interaction with tree'''
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def headerData(self, section: int, orientation: Qt.Orientation, role: Qt.ItemDataRole):
        """Draws the column headers at the top of the TreeView."""
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section]
        return None

    def set_root(self, new_root_node: VfsNode):
        '''Build the entire tree'''
        self.beginResetModel()
        self.root_node = new_root_node
        self.endResetModel()

    def _on_insert_start(self, parent: VfsNode, first: int, last: int) -> None:
        parent_index = self.index_for_node(parent)
        self.beginInsertRows(parent_index, first, last)

    def _on_insert_finished(self):
        self.endInsertRows()

    def _on_remove_start(self, parent: VfsNode, first: int, last: int) -> None:
        parent_index = self.index_for_node(parent)
        self.beginRemoveRows(parent_index, first, last)

    def _on_remove_finished(self) -> None:
        self.endRemoveRows()

    def _on_node_dataChanged(self, node: VfsNode) -> None:
        '''Trigger a UI repaint for a specific node.'''
        top_left = self.index_for_node(node)
        if not top_left.isValid():
            return
        bottom_right = self.index(node.row(), self.columnCount() - 1, top_left.parent())
        self.dataChanged.emit(top_left, bottom_right)

    def index_for_node(self, target_node: VfsNode) -> QModelIndex:
        '''Get the QModelIndex for a node'''
        if target_node is None or target_node == self.root_node:
            return QModelIndex()
        return self.createIndex(target_node.row(), 0, target_node)

###---------------------------------------------- Category Proxy -------------------------------------------###

class TreeProxyModel(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.show_hidden  = False
        # self.setRecursiveFilteringEnabled(True)
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        '''Custom sorting logic for columns'''
        left_node: VfsNode = self.source_model.data(left, Qt.ItemDataRole.UserRole)
        right_node: VfsNode = self.source_model.data(right, Qt.ItemDataRole.UserRole)
        if not left_node or not right_node:
            return super().lessThan(left, right)
        col = left.column()
        if col == 0:
            return left_node.address < right_node.address
        if col == 2:
            return left_node.size < right_node.size
        return super().lessThan(left, right)

    def set_show_hidden(self, show: bool):
        '''refreshes view with the hidden toggle'''
        self.show_hidden = show
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        '''Computed without an index() Qt roundtrip to save the interfacing time.'''
        if self.show_hidden:
            return True
        parent = self.source_model.get_node(source_parent)
        kids = parent.children
        return source_row < len(kids) and not kids[source_row].is_hidden

    @property
    def source_model(self) -> QAbstractItemModel:
        source_model = self.sourceModel()
        assert source_model is not None
        return source_model

### -------------------------------------- Flat Search Model -------------------------------------------###

_RANK_EXACT_NAME  = 100
_RANK_ADDRESS         = 90
_RANK_NAME_PREFIX = 80
_RANK_EXTENSION   = 70
_RANK_NAME        = 60
_RANK_TAG         = 40
_RANK_DESCRIPTION = 10

@dataclass
class _SearchEntry:
    '''Stores: node*, name, address, ext, tokens, base_rank, tags. Calculates score.'''
    node:       VfsNode | None
    address:    str
    name_lower: str
    ext_lower:  str
    desc_lower: str
    tags_lower: tuple[str, ...]
    tokens:     frozenset[str]
    base_rank:  int

    @property
    def is_resolved(self) -> bool:
        return self.node is not None

    def _matches_address(self, query: str) -> bool:
        if ':' in query:
            return self.address.startswith(query)
        return self.address.partition(':')[2].startswith(query)

    def score(self, query: str) -> int:
        '''return score for query. Higher is more relevant'''
        if not query:
            return 0

        if ':' in query: # Filter Search
            prefix, _, val = query.partition(':')
            val = val.lower().strip()
            if not val:
                return 0
            if prefix in ('ext', 'extension'): # Extension filter
                clean_val = val.lower().strip()
                clean_ext = self.ext_lower.lstrip('.')
                return _RANK_EXTENSION + self.base_rank if clean_ext and clean_val == clean_ext else 0
            if prefix in ('tag', 'tags'): # Tag filter
                return _RANK_TAG + self.base_rank if any(val in t for t in self.tags_lower) else 0
            if prefix in ('desc', 'description'): # Description filter
                return _RANK_DESCRIPTION + self.base_rank if val in self.desc_lower else 0
            if prefix == 'name': # Name filter
                if val == self.name_lower:
                    return _RANK_EXACT_NAME + self.base_rank
                if self.name_lower.startswith(val):
                    return _RANK_NAME_PREFIX + self.base_rank
                if val in self.name_lower:
                    return _RANK_NAME + self.base_rank
                return 0
            if prefix == 'address': # Address filter
                return _RANK_ADDRESS if self._matches_address(val) else 0

        # Global Search
        if query == self.name_lower: # Name exact
            return _RANK_EXACT_NAME + self.base_rank
        if self.name_lower.startswith(query): # Name startswith
            return _RANK_NAME_PREFIX + self.base_rank
        if query in self.name_lower: # Name contains
            return _RANK_NAME + self.base_rank
        if self._matches_address(query): # Address match
            return _RANK_ADDRESS
        for token in self.tokens:
            if query == token: # Exact token match
                return _RANK_TAG + self.base_rank if self.base_rank > 0 else _RANK_DESCRIPTION
            if token.startswith(query): # Partial token match
                return _RANK_DESCRIPTION + self.base_rank
        return 0

@dataclass
class _QueryResult:
    entry: _SearchEntry
    score: int

class FlatSearchModel(QAbstractListModel):
    '''Flat list of search results. Built from metadata store'''
    TagsRole     = Qt.ItemDataRole.UserRole + 1
    ResolvedRole = Qt.ItemDataRole.UserRole + 2

    def __init__(self,  vfs: VfsManager, metadata_store: NodeMetadataStore, parent=None,) -> None:
        super().__init__(parent)
        self._vfs         = vfs
        self._store       = metadata_store
        self._index:      list[_SearchEntry] = []
        self._results:    list[_QueryResult] = []
        self._query       = ''
        self._address_to_idx: dict[str, int] = {}
        self._built       = False  # deferred: index is built on first set_query call

        metadata_store.entry_registered.connect(self._on_entry_registered)
        metadata_store.entry_updated.connect(self._on_entry_updated)
        self._pending_inserts: list[tuple[VfsNode, int, int]] = []
        vfs.insert_start.connect(self._on_insert_start)
        vfs.insert_finished.connect(self._on_insert_finished)

        # Debounce timer: recompute runs once ~120ms after the last set_query call
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(120)
        self._debounce_timer.timeout.connect(self._recompute_results)

    ### Qt API
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._results)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        '''Returns different type of data depending on requested Role'''
        if not index.isValid() or index.row() >= len(self._results):
            return None
        result = self._results[index.row()]
        entry  = result.entry

        if role == Qt.ItemDataRole.DisplayRole: # Name
            if entry.node:
                return f'{entry.node.name}{entry.node.extension} ({entry.address})'
            else:
                meta    = self._store.get(entry.address)
                title   = meta.title if (meta and meta.title) else f'Unresolved ({entry.address})'
                ext_str = meta.extension if meta and meta.extension else ''
                return f'{title}{ext_str} ({entry.address})'
        if role == Qt.ItemDataRole.UserRole: # Node
            return entry
        if role == self.TagsRole: # Tags
            return entry.tags_lower
        if role == self.ResolvedRole:
            return entry.is_resolved
        if role == Qt.ItemDataRole.ToolTipRole: # ToolTip
            meta = self._store.get(entry.address)
            parts = [entry.address]
            if meta and meta.description:
                parts.append(meta.description)
            if not entry.is_resolved:
                parts.append('𔓎 Not yet loaded by VFS. Click to expand to file. 𔓎')
            return '\n'.join(parts)
        if role == Qt.ItemDataRole.ForegroundRole:
            if not entry.is_resolved:
                return QColor('#888888')
            if result.score < _RANK_TAG:
                return QColor('#AAAAAA')
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    ### Query
    def _ensure_built(self) -> None:
        '''Lazily build the search index on first use. Skips entries already added by signal handlers.'''
        if self._built:
            return
        self._built = True
        self._build_from_store()
        self._upgrade_from_vfs(self._vfs.root)
        logger.debug(f'FlatSearchModel: lazy build complete ({len(self._index)} entries)')

    def set_query(self, query: str) -> None:
        if not self._built:
            self._ensure_built()
        q = query.lower().strip()
        if q == self._query:
            return
        self._query = q
        self._debounce_timer.start()  # (re)starts the 120ms countdown; fires _recompute_results once

    def result_count(self) -> int:
        return len(self._results)

    def _recompute_results(self) -> None:
        self.beginResetModel()
        if not self._query:
            self._results = []
        else:
            scored = [
                _QueryResult(entry=e, score=e.score(self._query))
                for e in self._index
            ]
            self._results = sorted(
                (r for r in scored if r.score > 0),
                key=lambda r: -r.score,
            )
        self.endResetModel()

    ### Index Population
    def _build_from_store(self) -> None:
        '''Index every descriptor entry. Skips entries already in _address_to_idx
        (added by _on_entry_registered before the lazy build ran).'''
        store_items = self._store._db.items()
        added = 0
        for key, meta in store_items:
            if key in self._address_to_idx:
                continue  # already added by a signal handler before the lazy build
            entry = self._build_uninstantiated_entry(key, meta)
            self._address_to_idx[key] = len(self._index)
            self._index.append(entry)
            added += 1
        logger.debug(f'FlatSearchModel: _build_from_store added {added} entries (total {len(self._index)})')

    def _upgrade_from_vfs(self, node: VfsNode) -> None:
        '''Match metadata entries to VFS entries to mark current instance'''
        for child in node.children:
            if child.is_hidden:
                continue
            self._upgrade_or_append(child)
            if child.children:
                self._upgrade_from_vfs(child)

    def _on_insert_start(self, parent: VfsNode, first: int, last: int) -> None:
        self._pending_inserts.append((parent, first, last))

    def _on_insert_finished(self) -> None:
        if not self._pending_inserts:
            return
        parent, first, last = self._pending_inserts.pop()
        self._on_vfs_nodes_inserted(parent, first, last)

    def _on_vfs_nodes_inserted(self, parent: VfsNode, first: int, last: int) -> None:
        '''Called when new nodes are added to the VFS'''
        new_children = parent.children[first : last + 1]
        changed = False
        for child in new_children:
            if child.is_hidden:
                continue
            self._upgrade_or_append(child)
            changed = True
        if changed and self._query:
            self._recompute_results()

    def _on_entry_registered(self, key: str) -> None:
        '''Called when new entries are added to the metadata store'''
        meta = self._store.get(key)
        if meta is None:
            return
        if key in self._address_to_idx:
            self._on_entry_updated(key)
            return
        node = self._vfs.get_node(key_to_address(key))
        entry = (
            self._build_entry(node)
            if node and not node.is_hidden
            else self._build_uninstantiated_entry(key, meta)
        )
        self.beginResetModel()
        self._address_to_idx[key] = len(self._index)
        self._index.append(entry)
        self.endResetModel()
        if self._query:
            self._recompute_results()

    def _on_entry_updated(self, key: str) -> None:
        '''Called when a metadata store entry is modified'''
        idx = self._address_to_idx.get(key)
        if idx is None:
            return
        existing = self._index[idx]
        meta = self._store.get(key)
        if meta is None:
            return
        self._index[idx] = (
            self._build_entry(existing.node)
            if existing.node
            else self._build_uninstantiated_entry(key, meta)
        )
        if self._query:
            self._recompute_results()

    ### Entry helpers
    def _upgrade_or_append(self, node: VfsNode) -> None:
        '''Upgrade or append an entry'''
        key = address_to_key(node.address)
        if key in self._address_to_idx: # Upgrade
            idx = self._address_to_idx[key]
            if not self._index[idx].is_resolved:
                self._index[idx] = self._build_entry(node)
        else: # New Entry
            self._address_to_idx[key] = len(self._index)
            self._index.append(self._build_entry(node))

    def _build_entry(self, node: VfsNode) -> _SearchEntry:
        '''Build an instantiated entry from live VFS'''
        key:        str = address_to_key(node.address)
        meta            = self._store.get(key)
        name_lower: str = node.name.lower()
        ext_lower:  str = node.extension.lower() if node.extension else (meta.extension.lower() if meta and meta.extension else '')
        desc_lower: str = ''
        tags_lower: tuple[str, ...] = ()
        base_rank  = 0

        tokens: set[str] = set(name_lower.split())
        tokens.add(key)
        tokens.update(c.lower() for c in node.category)
        if ext_lower:
            tokens.add(ext_lower.lstrip('.'))

        if meta:
            if meta.title:
                tokens.update(meta.title.lower().split())
                base_rank += 20
            if meta.tags:
                tags_lower = tuple(t.lower() for t in meta.tags)
                tokens.update(tags_lower)
                base_rank += 10 * len(meta.tags)
            if meta.description:
                desc_lower = meta.description.lower()
                tokens.update(w for w in desc_lower.split() if len(w) > 2)
        return _SearchEntry(
            node=node,
            name_lower=name_lower,
            ext_lower=ext_lower,
            address=key,
            desc_lower=desc_lower,
            tags_lower=tags_lower,
            tokens=frozenset(tokens),
            base_rank=base_rank,
        )

    def _build_uninstantiated_entry(self, key: str, meta: Any) -> _SearchEntry:
        '''Build an for a metadata entry, a node not yet in the VFS'''
        name_lower = (meta.title if meta.title else f'Unresolved ({key})').lower()
        tags_lower = tuple(t.lower() for t in meta.tags)
        ext_lower  = meta.extension.lower() if meta and meta.extension else ''
        desc_lower = ''
        base_rank  = 0

        tokens: set[str] = set(name_lower.split())
        tokens.add(key)
        tokens.update(tags_lower)
        if ext_lower:
            tokens.add(ext_lower.lstrip('.'))

        if meta.title:
            base_rank += 20
        if meta.tags:
            base_rank += 10 * len(meta.tags)
        if meta.description:
            desc_lower = meta.description.lower()
            tokens.update(w for w in desc_lower.split() if len(w) > 2)
        return _SearchEntry(
            node=None,
            name_lower=name_lower,
            ext_lower=ext_lower,
            address=key,
            desc_lower=desc_lower,
            tags_lower=tags_lower,
            tokens=frozenset(tokens),
            base_rank=base_rank,
        )

    def rebuild_index(self) -> None:
        '''Rebuild the entire index. Used when mass metadata updates occur'''
        self.beginResetModel()
        self._index.clear()
        self._address_to_idx.clear()
        self._results.clear()
        self._built = True  # cleared above so _build_from_store will add all entries cleanly
        self._build_from_store()
        self._upgrade_from_vfs(self._vfs.root)
        self.endResetModel()
        if self._query:
            self._recompute_results()
