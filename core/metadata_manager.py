'''
Contains all logic for interfacing with metadata json\'s
NodeMeta            - Translates JSON <-> Application
NodeMetadataStore   - Updates node metadata, updates metadata with new entries
Metadata Imports section - Standard metadata to build a new metadata.json from scratch programmatically **Non-Essential**

Extensions are added from source to the database for speed optimization on missing extension enrichment, thus non-essential.
'''
from __future__ import annotations

import json
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator, Protocol, runtime_checkable
from PyQt6.QtCore import QTimer, QObject, pyqtSignal

if TYPE_CHECKING:
    from core.node import VfsNode

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###--------------------------------- Metadata --------------------------------------###

@dataclass(frozen=True)
class NodeMeta:
    title:       str
    description: str
    tags:        tuple[str, ...]
    target_hid:  tuple[int, ...] | None = None
    extension:   str | None = None

    @staticmethod
    def from_dict(d: dict) -> NodeMeta:
        cat = d.get('category', ['Unknown'])
        if isinstance(cat, str):
            cat = [cat] if cat else ['Unknown']

        raw_target = d.get('target')
        target: tuple[int, ...] | None = None
        if raw_target:
            try:
                if isinstance(raw_target, list) and raw_target and isinstance(raw_target[0], list):
                    target = tuple(int(i) for i in raw_target[0])
                else:
                    target = tuple(int(i) for i in raw_target)
            except (TypeError, ValueError) as e:
                logger.debug(f'NodeMeta.from_dict: invalid target {raw_target!r}: {e}')

        return NodeMeta(
            title=d.get('title', ''),
            description=d.get('description', ''),
            tags=tuple(d.get('tags', [])),
            target_hid=target,
            extension=d.get('extension', None),
        )

    def to_dict(self) -> dict:
        d: dict = {
            'title':       self.title,
            'description': self.description,
            'tags':        list(self.tags),
            'extension':   self.extension,
        }
        if self.target_hid:
            d['target'] = list(self.target_hid)
        return d

###--------------------------------------- Store ----------------------------------------------###

class NodeMetadataStore(QObject):
    '''Owns the metadata database.'''
    SAVE_DEBOUNCE_MS = 2000
    entry_registered = pyqtSignal(str)  # hid str
    entry_updated    = pyqtSignal(str)  # hid str
    entry_removed    = pyqtSignal(str)  # hid str
    bulk_updated     = pyqtSignal(int)  # updated count

    def __init__(
        self,
        json_path: Path,
        *,
        auto_save: bool = False,
        parent:    QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._path      = json_path
        self._auto_save = auto_save
        self._db: dict[str, NodeMeta] = {}
        self._lock = threading.RLock()
        self._dirty = False

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(self.SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self._save_to_disk)

    def load(self) -> None:
        '''Parse json into a flat dict[str, NodeMeta]'''
        if not self._path.exists():
            logger.info(f'metadata file not found at {self._path} - Starting anew')
            return
        try:
            raw: dict[str, dict] = json.loads(self._path.read_text(encoding='utf-8'))
            parsed: dict[str, NodeMeta] = {}
            errors = 0
            for hid, entry in raw.items():
                try:
                    parsed[hid] = NodeMeta.from_dict(entry)
                except Exception as e:
                    logger.debug(f'Metadata parse error for "{hid}": {e}')
                    errors += 1
            with self._lock:
                self._db = parsed
            logger.info(
                f'Loaded {len(parsed)} metadata from {self._path.name}'
                + (f' ({errors} skipped)' if errors else '')
            )
        except Exception as e:
            logger.error(f'Failed to load metadata database: {e}', exc_info=True)

    def reload(self, json_path: Path) -> None:
        '''Reload the current active store with a new path for switching sources.'''
        self._save_timer.stop()
        with self._lock:
            if self._dirty:
                logger.warning(f'reload({json_path.name}): discarding unsaved changes to {self._path.name}')
            self._path  = json_path
            self._db    = {}
            self._dirty = False
        self.load()

    def enrich(self, node: VfsNode) -> None:
        '''
        Stamps metadata onto a VfsNode instance.
        Used during runtime when new nodes are added to the filesystem.
        '''
        with self._lock: # snapshot the db entry
            meta = self._db.get(node.hierarchical_id_str)
        if meta is None:
            return
        if meta.title:
            node.name = meta.title
        if meta.tags and meta.tags != ('Unknown',):
            node.category = meta.tags
        if meta.target_hid:
            node.target = meta.target_hid
        if meta.extension:
            node.extension = meta.extension

    ### Lookup
    def link_of(self, hid: tuple[int, ...]) -> tuple[int, ...] | None:
        '''Link for a node'''
        with self._lock:
            meta = self._db.get('.'.join(map(str, hid)))
        return meta.target_hid if meta else None

    def get(self, hid: str) -> NodeMeta | None:
        with self._lock:
            return self._db.get(hid)

    def __contains__(self, hid: str) -> bool:
        with self._lock:
            return hid in self._db

    def __len__(self) -> int:
        with self._lock:
            return len(self._db)

    ### runtime registration
    def _merge_entry(
        self,
        hid: str,
        *,
        title:       str | None = None,
        description: str | None = None,
        tags: list[str] | tuple[str, ...] | None = None,
        target: tuple[int, ...] | None = None,
        extension:   str | None = None,
    ) -> None:
        '''
        Used to merge new metadata with existing entries at runtime.
        Only called from register()
        '''
        existing = self._db.get(hid)
        if tags is not None:
            existing_tags = existing.tags if existing else()
            merged_tags = tuple(dict.fromkeys((*existing_tags, *tags)))
            if merged_tags != ('Unknown',) and 'Unknown' in merged_tags:
                merged_tags = tuple(t for t in merged_tags if t != 'Unknown')
            new_tags = merged_tags
        else:
            new_tags = existing.tags if existing else ('Unknown',)

        self._db[hid] = NodeMeta(
            title       = title       if title       is not None else (existing.title       if existing else ''),
            description = description if description is not None else (existing.description if existing else ''),
            tags        = new_tags,
            target_hid  = target      if target      is not None else (existing.target_hid  if existing else None),
            extension   = extension   if extension   is not None else (existing.extension   if existing is not None else None),
        )

    def register(
            self,
            hid: str,
            *,
            title:       str | None = None,
            description: str | None = None,
            tags:   list[str]| None = None,
            target: tuple[int, ...] | None = None,
            extension:   str | None = None,
        ) -> None:
        '''
        Create entries and update fields
        Update logic for fields:
        title and description   - overwrite
        tags                    - merge
        target                  - overwrite
        extension               - overwrite
        '''
        with self._lock:
            is_new   = hid not in self._db
            self._merge_entry(hid, title=title, description=description, tags=tags, target=target, extension=extension)
            self._dirty = True
        # Update search model
        if is_new:
            self.entry_registered.emit(hid)
        else:
            self.entry_updated.emit(hid)
        # Trigger save debounce
        if self._auto_save:
            self._save_timer.start(self.SAVE_DEBOUNCE_MS)

    def register_many(self, entries: Iterator[tuple[str, dict[str, Any]]]) -> int:
        count = 0
        with self._lock:
            for hid, fields in entries:
                self._merge_entry(
                    hid,
                    title=fields.get('title'),
                    description=fields.get('description'),
                    tags=fields.get('tags'),
                    target=fields.get('target'),
                    extension=fields.get('extension'),
                )
                count += 1
            if count:
                self._dirty = True
        if count:
            self.bulk_updated.emit(count)
            if self._auto_save:
                self._save_timer.start(self.SAVE_DEBOUNCE_MS)
        return count

    def delete(self, hid: str) -> bool:
        '''Remove a single entry by hid. Triggered by the VfsNavigator when it discovers
        invalid expansions targets.'''
        with self._lock:
            existed = self._db.pop(hid, None) is not None
            if existed:
                self._dirty = True
        if existed:
            self.entry_removed.emit(hid)
            if self._auto_save:
                self._save_timer.start(self.SAVE_DEBOUNCE_MS)
        else:
            logger.debug(f'delete({hid!r}): no such entry.')
        return existed

    ### Persistence for expansion
    def save(self) -> None:
        '''Current metadata back to disk'''
        self._save_timer.stop()
        self._save_to_disk()

    def _save_to_disk(self) -> None:
        logger.debug('Attempting to save updated metadata to disk...')
        with self._lock:
            if not self._dirty:
                logger.debug('No new entries.')
                return
            snapshot = {k: v.to_dict() for k, v in self._db.items()}
            self._dirty = False
        try:
            sorted_ss = dict(sorted(snapshot.items(), key=self._sort_key))
            self._path.write_text(
                json.dumps(sorted_ss, indent=2, ensure_ascii=False),
                encoding='utf-8'
            )
            logger.debug(f'Metadata updated - {len(snapshot)} total entries -> {self._path.name}')
        except Exception as e:
            logger.error(f'Failed to save metadata: {e}', exc_info=True)
            with self._lock:
                self._dirty = True

    def _sort_key(self, kv: tuple[str, Any]) -> tuple:
        parts  = kv[0].split('.')
        sorted = []
        for p in parts:
            try:
                sorted.append(int(p))
            except ValueError:
                logger.warning('Could not cast key to int')
        return tuple(sorted)

    def dump_metadata(self, output_path: Path) -> int:
        '''Write the full metadata json to disk'''
        target_path = output_path or self._path
        with self._lock:
            snapshot = {k: v.to_dict() for k, v in self._db.items()}
        sorted_snapshot = dict(sorted(snapshot.items(), key=self._sort_key))
        target_path.write_text(
            json.dumps(sorted_snapshot, indent=2, ensure_ascii=False),
            encoding='utf-8'
        )
        logger.info(f'Dumped {len(sorted_snapshot)} metadata entries -> {target_path}')
        return len(sorted_snapshot)

    ### Metadata building from scratch
    def ingest_static_sources(self, sources: list[StaticMetadataSource]) -> int:
        '''Register static entries from a list of sources'''
        def _all_entries() -> Iterator[tuple[str, dict[str, Any]]]:
            for source in sources:
                if not hasattr(source, 'iter_entries'):
                    logger.warning(f'{source.__name__} has no iter_entries(). --Skipped--')
                    continue
                yield from source.iter_entries()
        count = self.register_many(_all_entries())
        logger.info(f'Ingested {count} metadata entries from {len(sources)} source(s).')
        return count

###--------------------------------- Protocol ------------------------------------###

@runtime_checkable
class StaticMetadataSource(Protocol):
    '''Import protocol'''
    @classmethod
    def iter_entries(cls) -> Iterator[tuple[str, dict[str, Any]]]: ...
