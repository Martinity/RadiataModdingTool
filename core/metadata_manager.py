'''
Contains all logic for interfacing with metadata jsons
NodeMeta            - Translates JSON <-> Application
NodeMetadataStore   - Updates node metadata, updates metadata with new entries
Keys                - Every entry is keyed by the node's Address, written "mid:hid"

Building a database from scratch is BaseSource.Metadata.build(store)'s job.
Extensions are added from source to the database for speed optimization on missing extension enrichment, thus non-essential.
'''
from __future__ import annotations

import json
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator, Protocol, runtime_checkable, Mapping, Sequence
from PyQt6.QtCore import QTimer, QObject, pyqtSignal
from core.node import Address, Hid, Mid

if TYPE_CHECKING:
    from core.node import VfsNode

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###--------------------------------- Keys ----------------------------------###

def parse_hid(hid: str) -> Hid:
    '''"3.4.0" -> (3, 4, 0)'''
    if not hid:
        return ()
    parts = hid.split('.')
    if not all(part.isascii() and part.isdigit() for part in parts):
        raise ValueError(f'{hid!r} is not dotted integers')
    return tuple(int(part) for part in parts)

def address_to_key(address: Address) -> str:
    '''Address -> database key'''
    mid, hid = address
    return f'{mid}:{".".join(map(str, hid))}'

def key_to_address(key: str) -> Address:
    '''Database Key -> Address'''
    head, sep, tail = key.partition(':')
    if not sep:
        raise ValueError(f'{key!r} has no namespace')
    if not (head.isascii() and head.isdigit()):
        raise ValueError(f'{key!r}: the namespace must be an integer')
    return int(head), parse_hid(tail)

def is_address(value: Any) -> bool:
    '''True for a well formed (mid, hid)'''
    def _index(i: Any) -> bool:
        return isinstance(i, int) and not isinstance(i, bool) and i >= 0
    return (
        isinstance(value, tuple) and len(value) == 2
        and _index(value[0])
        and isinstance(value[1], tuple) and all(_index(i) for i in value[1])
    )

def sort_key(kv: tuple[str, Any]) -> tuple[Mid, Hid]:
    '''Order database entries by Mid, then Hid'''
    try:
        return key_to_address(kv[0])
    except ValueError:
        logger.warning(f'Could not parse metadata key {kv[0]!r}')
        return (2**31, ())

###--------------------------------- Metadata --------------------------------------###

@dataclass(frozen=True)
class NodeMeta:
    title:       str
    description: str
    tags:        tuple[str, ...]
    links:        tuple[Address, ...] = ()
    extension:   str | None = None

    @staticmethod
    def from_dict(d: dict) -> NodeMeta:
        raw_links = d.get('links') or []
        if not isinstance(raw_links, list) or not all(isinstance(item, str) for item in raw_links):
            raise ValueError(f'Links must be "mid:hid" strings, got {raw_links!r}')

        return NodeMeta(
            title=d.get('title', ''),
            description=d.get('description', ''),
            tags=tuple(d.get('tags', [])),
            links=tuple(key_to_address(link) for link in raw_links),
            extension=d.get('extension', None),
        )

    def to_dict(self) -> dict:
        d: dict = {
            'title':       self.title,
            'description': self.description,
            'tags':        list(self.tags),
            'extension':   self.extension,
        }
        if self.links:
            d['links'] = [address_to_key(link) for link in self.links]
        return d

###--------------------------------------- Store ----------------------------------------------###

class NodeMetadataStore(QObject):
    '''Owns the metadata database. Entries are keyed by address_to_key(node.address).
    Ingestion/Lookup accepts either a string "mid:hid" or the Address itself.'''
    SAVE_DEBOUNCE_MS = 2000
    entry_registered = pyqtSignal(str)  # key str
    entry_updated    = pyqtSignal(str)  # key str
    entry_removed    = pyqtSignal(str)  # key str
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

    @staticmethod
    def _key(key: str | Address) -> str:
        return key if isinstance(key, str) else address_to_key(key)

    def load(self) -> None:
        '''Parse json into a flat dict[str, NodeMeta]'''
        if not self._path.exists():
            logger.info(f'metadata file not found at {self._path} - Starting anew')
            return
        try:
            raw: dict[str, dict] = json.loads(self._path.read_text(encoding='utf-8'))
            parsed: dict[str, NodeMeta] = {}
            errors = 0
            for key, entry in raw.items():
                try:
                    key_to_address(key)  # Validate the key
                    parsed[key] = NodeMeta.from_dict(entry)
                except Exception as e:
                    logger.debug(f'Metadata parse error for "{key}": {e}')
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
            meta = self._db.get(address_to_key(node.address))
        if meta is None:
            return
        if meta.title:
            node.name = meta.title
        if meta.tags and meta.tags != ('Unknown',):
            node.category = meta.tags
        if meta.links:
            node.links = meta.links
        if meta.extension:
            node.extension = meta.extension

    ### Lookup
    def links_of(self, address: Address) -> tuple[Address, ...]:
        '''The package members required for a node at address. Satisfies BaseSource.Package.LinksLookup.'''
        with self._lock:
            meta = self._db.get(address_to_key(address))
        return meta.links if meta else ()

    def get(self, key: str | Address) -> NodeMeta | None:
        with self._lock:
            return self._db.get(self._key(key))

    def __contains__(self, key: str | Address) -> bool:
        with self._lock:
            return self._key(key) in self._db

    def __len__(self) -> int:
        with self._lock:
            return len(self._db)

    ### runtime registration
    def _merge_entry(
        self,
        key: str,
        *,
        title:       str | None = None,
        description: str | None = None,
        tags:        list[str] | tuple[str, ...] | None = None,
        links:       Sequence[Address] | None = None,
        extension:   str | None = None,
    ) -> None:
        '''
        Used to merge new metadata with existing entries at runtime.
        Only called from register() and register_many()
        '''
        existing = self._db.get(key)
        if tags is not None:
            existing_tags = existing.tags if existing else()
            merged_tags = tuple(dict.fromkeys((*existing_tags, *tags)))
            if merged_tags != ('Unknown',) and 'Unknown' in merged_tags:
                merged_tags = tuple(t for t in merged_tags if t != 'Unknown')
            new_tags = merged_tags
        else:
            new_tags = existing.tags if existing else ('Unknown',)

        self._db[key] = NodeMeta(
            title       = title       if title       is not None else (existing.title       if existing else ''),
            description = description if description is not None else (existing.description if existing else ''),
            tags        = new_tags,
            links       = tuple(links)if links       is not None else (existing.links       if existing else ()),
            extension   = extension   if extension   is not None else (existing.extension   if existing is not None else None),
        )

    def register(
            self,
            key: str | Address,
            *,
            title:       str | None = None,
            description: str | None = None,
            tags:   list[str]| None = None,
            links:  Sequence[Address] | None = None,
            extension:   str | None = None,
        ) -> None:
        '''
        Create entries and update fields
        Update logic for fields:
        title and description   - overwrite
        tags                    - merge
        links                   - overwrite (emtpy sequence clears existing)
        extension               - overwrite
        '''
        key = self._key(key)
        with self._lock:
            is_new = key not in self._db
            self._merge_entry(key, title=title, description=description, tags=tags, links=links, extension=extension)
            self._dirty = True
        # Update search model
        if is_new:
            self.entry_registered.emit(key)
        else:
            self.entry_updated.emit(key)
        # Trigger save debounce
        if self._auto_save:
            self._save_timer.start(self.SAVE_DEBOUNCE_MS)

    def register_many(self, entries: Iterator[tuple[str | Address, dict[str, Any]]]) -> int:
        count = 0
        with self._lock:
            for key, fields in entries:
                self._merge_entry(
                    self._key(key),
                    title=fields.get('title'),
                    description=fields.get('description'),
                    tags=fields.get('tags'),
                    links=fields.get('links'),
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

    def delete(self, key: str | Address) -> bool:
        '''Remove a single entry by key or address. Triggered by the VfsNavigator when it discovers
        invalid expansions targets.'''
        key = self._key(key)
        with self._lock:
            existed = self._db.pop(key, None) is not None
            if existed:
                self._dirty = True
        if existed:
            self.entry_removed.emit(key)
            if self._auto_save:
                self._save_timer.start(self.SAVE_DEBOUNCE_MS)
        else:
            logger.debug(f'delete({key!r}): no such entry.')
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
            sorted_ss = dict(sorted(snapshot.items(), key=sort_key))
            self._path.write_text(
                json.dumps(sorted_ss, indent=2, ensure_ascii=False),
                encoding='utf-8'
            )
            logger.debug(f'Metadata updated - {len(snapshot)} total entries -> {self._path.name}')
        except Exception as e:
            logger.error(f'Failed to save metadata: {e}', exc_info=True)
            with self._lock:
                self._dirty = True

    def dump_metadata(self, output_path: Path) -> int:
        '''Write the full metadata json to disk'''
        target_path = output_path or self._path
        with self._lock:
            snapshot = {k: v.to_dict() for k, v in self._db.items()}
        sorted_snapshot = dict(sorted(snapshot.items(), key=sort_key))
        target_path.write_text(
            json.dumps(sorted_snapshot, indent=2, ensure_ascii=False),
            encoding='utf-8'
        )
        logger.info(f'Dumped {len(sorted_snapshot)} metadata entries -> {target_path}')
        return len(sorted_snapshot)

###--------------------------------- Protocol ------------------------------------###

@runtime_checkable
class StaticMetadataSource(Protocol):
    '''Import protocol for BaseSource.Metadata.static_sources.
    Yields (Address, fields)'''
    @classmethod
    def iter_entries(cls) -> Iterator[tuple[str | Address, dict[str, Any]]]: ...
