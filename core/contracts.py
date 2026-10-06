'''
Contracts for handlers, editors, and sources.

Contract map:

    Handlers   BaseHandler > PhysicalHandler / ContainerHandler / LeafHandler
    Editors    BaseEditor  > BaseViewer
    Sources    BaseSource  > BaseSignatureValidator / BaseTocCodec / BaseBoundaryResolver / BaseExtensionResolver / BasePackageAssembler / BaseConflictDetector / BaseMetadataBuilder / BasePatch / BaseSourceBuilder
               BasePatch   > BaseDirectPatch / BaseDelegatedPatch
               BaseSourceBuilder
'''
from __future__ import annotations

import io
import abc
import array
import inspect
import struct
import sys
from dataclasses import dataclass, field
from enum import Flag, Enum, auto
from pathlib import Path
from typing import (
    Any, Callable, NamedTuple, TYPE_CHECKING, runtime_checkable, BinaryIO, Protocol,
    ClassVar, Sequence
)
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import pyqtSignal
from core.node import VfsNode
if TYPE_CHECKING:
    from core.workers import TaskHandle, EditorPayload
    from core.native.block_device import BlockDevice
    from core.metadata_manager import NodeMetadataStore
    from core.dispatcher import Dispatcher

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###-------------------------------------------- Special Return Structs --------------------------------------------------###

class PackageError(RuntimeError):
    '''Raised when a package fails to build or validate'''

@dataclass(frozen=True)
class ResolvedPackage:
    '''Requirements for a package to complete resolution'''
    primary: VfsNode
    members: dict[str, VfsNode]
    data:    dict[str, bytes]

class PackageResult(NamedTuple):
    '''Structured return value for handlers that mutate linked nodes (ex. kods w/datacenter)'''
    payload:     bytes
    companions: dict[str, bytes] | None = None

def as_package_result(result: bytes | PackageResult) -> PackageResult:
    '''Normalize handler results to PackageResult'''
    return result if isinstance(result, PackageResult) else PackageResult(result)

###---------------------------------------------- Base Handler contract ------------------------------------------------###

class BaseHandler(abc.ABC):
    '''
    The architectural blueprint for data interpretation.

    A Handler acts as a translator between raw binary data and the VfsNode
    hierarchy. It is responsible for 'unpacking' a format's internal structure
    and 'repacking' modifications back into a valid binary stream.

    Lifecycle:
        1. Instantiated by a Worker or Navigator with source bytes.
        2. get_file_tree() maps internal entries to VfsNodes.
        3. execute_action() performs specific logic (e.g., decompression).
    '''
    def __init__(self, parent_node: VfsNode | None = None) -> None:
        '''Initialize the root handle and provide generic resource management'''
        self.parent_node = parent_node
        self.handle:      io.IOBase | BlockDevice | None = None
        self.owns_handle: bool = False
        self.task_handle: TaskHandle | None = None
        self.package:     ResolvedPackage | None = None

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__} identity="{self.get_identity()}">'

    def get_identity(self) -> str:
        '''Reads the stamp from the registration'''
        return getattr(self.__class__, '_plugin_name', 'Unknown Handler')

    def close(self):
        '''Close the stream if owned by this instance'''
        if self.owns_handle and self.handle and not getattr(self.handle, 'closed', False):
            try:
                self.handle.close()
            except Exception as e:
                logger.error(f'Error while closing handler: {e}')
            finally:
                self.owns_handle = False
                self.handle = None
                logger.debug('Closed handler resources successfully.')

    def execute_action(
        self,
        node:         VfsNode,
        action_name:  str,
        **kwargs,
    ) -> Any:
        '''Execute custom actions registered with the Registry'''
        if action_name == 'Properties':
            logger.info(f'Properties not implemented for {self.get_identity()}')
            return None
        logger.warning(f'{self.__class__.__name__} has not implemented action: {action_name}')
        return None

    def prepare_editor_data(self, node: VfsNode, raw_bytes: bytes) -> Any:
        '''Processed data return directly to the editor.
        Use this for things such as audio decoding, swizzling...etc
        to keep entensive data processing off Main thread and prevent UI from freezing.
        '''
        return raw_bytes

    def decode_editor_data(self, node: VfsNode, payload: Any, **kwargs) -> bytes:
        '''Process the modified node data back into raw bytes'''
        if isinstance(payload, bytes):
            return payload
        raise NotImplementedError(
            f'{self.__class__.__name__} must implement decode_editor_data to handle decoding non-bytes payloads'
        )

    def validate_package(self, node: VfsNode, package: ResolvedPackage) -> None:
        '''Raise (ValueError) to reject a packgage before any processing'''

    def import_payload(self, node: VfsNode, new_payload: bytes, package: ResolvedPackage) -> PackageResult:
        '''Import a new payload into the package'''
        raise NotImplementedError(
            f'{self.__class__.__name__} must implement import_payload to handle importing new payloads'
        )

    @abc.abstractmethod
    def get_file_tree(self) -> VfsNode:
        '''Return the root VfsNode representing this format's internal structure.'''

    @abc.abstractmethod
    def rebuild_node(self, node: VfsNode, staged_nodes: list[VfsNode]) -> bytes | PackageResult:
        '''Return rebuilt container bytes incorporating pending edits. + any dependant node bytes'''

    @abc.abstractmethod
    def get_raw_node(self, node: VfsNode) -> bytes:
        '''Return raw original bytes for a node, bypassing any pending edits.'''

###------------------------------------------ Specialized Handler Contracts ----------------------------------------###

class PhysicalHandler(BaseHandler):
    '''Specialized ISO/BlockDevice handler
    Currently only used for disk images if new sources are added the BlockDevice needs to match the device alignment.
    BlockDevice is stateless and thread-safe.
    - Source must be a BlockDevice
    - Manages file-handles directly
    - Scanning and maintaining top-level tree structures
    '''
    def __init__(self, source: BlockDevice, parent_node: VfsNode | None = None) -> None:
        super().__init__(parent_node)
        self.source: BlockDevice = source
        self.handle: BlockDevice = self.source
        self.owns_handle = True


    def release_handle(self) -> None:
        '''
        Release the init handle after get_file_tree().
        Subsequent physical node access must open private handles with source_path
        '''
        if self.handle and not self.handle.closed:
            self.handle.close()
        self.handle = None  # type: ignore dereference for safety
        self.owns_handle = False

    @abc.abstractmethod
    def rebuild_node(
        self,
        root:         VfsNode,
        staged_nodes: list[VfsNode],
        output_path:  Path,
        build_flags:  SourceRebuildFlags,
        task_handle:  TaskHandle,
    ) -> bool:  # type: ignore
        '''Rebuild and write a collection of nodes to disk. Returns True on success'''

class ContainerHandler(BaseHandler):
    '''Used for virtual archives (e.g. SLZ, Kods)
    Class exists purely as a typing landmark so VfsNavigator can distinguish handler purposes
    without parsing extensions or magic
    - Source must be memory-based (bytes or stream)
    - Maintains relationships and alignments between nodes
    '''
    def __init__(self, source_data: bytes, parent_node: VfsNode | None = None) -> None:
        super().__init__(parent_node)
        self.handle: io.BytesIO = io.BytesIO(source_data)
        self.owns_handle        = True

class LeafHandler(BaseHandler):
    '''Used for individual, isolated file objects (e.g. IO)
    Provides minimal/passthrough stubs of all abstract methods so subclasses
    only need to implement execute_action for their specific logic.
    - Source must be memory-based (bytes or stream)
    - Never sees any relational data
    - Purely used for tasks where the node is the input for specific logic (e.g. parsing a JPG)
    '''
    def __init__(self, source_data: bytes, parent_node: VfsNode | None = None) -> None:
        super().__init__(parent_node)
        self.handle: io.BytesIO = io.BytesIO(source_data)
        self.owns_handle = True

    def get_file_tree(self) -> VfsNode:
        '''Leaf nodes do not contain children'''
        return VfsNode(name='raw_data')

    def get_raw_node(self, node: VfsNode) -> bytes:
        self.handle.seek(0)
        return self.handle.read()

    def rebuild_node(self, node: VfsNode, staged_nodes: list[VfsNode]) -> bytes:
        if node in staged_nodes and node.pending_data is not None and self.task_handle:
            self.task_handle.log_message.emit(f'Node has been modified. Original size:{node.size} New size:{len(node.pending_data)}')
            return node.pending_data
        self.handle.seek(0)
        return self.handle.read()

###---------------------------------------------- Editor contract ----------------------------------------------###

class _ABCMetaQtMeta(type(QWidget), abc.ABCMeta): # type: ignore
    '''Merge PyQt6 widget metaclass with ABC metaclass'''

class BaseEditor(QWidget, metaclass=_ABCMetaQtMeta): # type: ignore
    '''
    Minimum required implementation for a mutable editor:

        _populate_ui(data)            render the payload in your widget
        current_data() -> Any         return the current widget state for saving
                                        (needed for non-bytes types data)

    Minimum required implementation for a read-only editor:

        Inherit from BaseViewer instead
        _populate_ui(data)            render the payload in your widget

    Optional:
        undo() / redo()               implement and emit undo_state_changed
        confirm_changes_applied()     called on save success
        show_error(message)           override the error display for custom UIs
        cleanup()                     release resources (timers, sinks, handles)
    '''
    undo_state_changed = pyqtSignal(bool, bool) # (can_undo, can_redo)
    dataChanged = pyqtSignal(bool)              # Data changed bool
    is_mutable = True

    def __init__(self, parent: QWidget | None = None, data_resolver: Dispatcher | None = None):
        super().__init__(parent)
        self.current_node:      VfsNode | None = None
        self._is_dirty:         bool = False
        self._original_payload: Any = None
        self._pending_data:     Any = None
        self._data_resolver:    Dispatcher | None = data_resolver

    def __repr__(self) -> str:
        node_name = self.current_node.name if self.current_node else "None"
        return f"<{self.__class__.__name__} node='{node_name}' dirty={self.is_dirty()}>"

    def __str__(self) -> str:
        return f"{self.__class__.__name__} for {self.current_node or 'no node'}"

    ### Data handling
    def begin_loading(self, node: VfsNode) -> None:
        '''Called when editor is open for data loading feedback, while waiting for BG thread'''
        self.current_node = node

    def receive_data(self, result: Any) -> None:
        '''
        Called when the BG thread is done data processing
        (default) if result is bytes, stores as original data and call _populate_ui(result).
        Override for handlers that return non-bytes results.
        '''
        self._original_payload = result
        if isinstance(result, bytes):
            self.set_dirty(False)
            self._populate_ui(result)
        else:
            self.show_error(
                f'Receive data got {type(result).__name__}, expected bytes. '
                f'Override receive_data or ensure handler.prepare_editor_data returns bytes.'
            )

    @abc.abstractmethod
    def _populate_ui(self, data: Any) -> None:
        '''Populate the editor with return from associated handler.prepare_editor_data'''

    def undo(self) -> None:
        pass

    def redo(self) -> None:
        pass

    def receive_request(self, payload: Any) -> None:
        '''Callback for a request from the data resolver
        payload = None if the request could not be resolved
        payload = EditorPayload if the request was resolved successfully'''
        if payload is None:
            logger.error('request_payload failed to resolve an EditorPayload.')
            return

    ### Lifecycle
    def cleanup(self) -> None:
        '''Editor Destructor'''
        self.current_node      = None
        self._original_payload = None
        self._data_resolver    = None
        self.set_dirty(False)

    ### Data access
    def request_payload(self, hid: tuple[int, ...]) -> None:
        from core.dispatcher import Dispatcher
        if not isinstance(self._data_resolver, Dispatcher):
            raise TypeError(f'Ensure to set _data_resolver during recieve_data before calling request_payload')
        self._data_resolver.request_editor_payload(hid, callback=self.receive_request)

    def request_raw_data(self, hid: tuple[int, ...]) -> None:
        if not self._data_resolver:
            raise TypeError(f'Ensure to set _data_resolver during recieve_data before calling request_raw_data')
        self._data_resolver.request_raw_data(hid, callback=self.receive_request)

    def current_data(self) -> Any:
        '''Return the live state'''
        return self._original_payload

    def snapshot(self) -> None:
        '''Freeze the state'''
        self._pending_data = self.current_data()

    ### Mutability management
    def confirm_changes_applied(self) -> None:
        '''Called by dispatcher after handler successfully applied decoded editor data to node'''
        if self._pending_data is not None:
            self._original_payload = self._pending_data
            self._pending_data = None
        self.set_dirty(False)

    def reject_changes_applied(self, reason: str) -> None:
        '''Called by dispatcher when handler failed to decode editor data'''
        self._pending_data = None
        logger.error(f'{self.__class__.__name__} save rejected: {reason}')

    def discard_changes(self) -> None:
        '''Reverts the node data back to original state'''
        if self.is_dirty() and self.current_node:
            self._pending_data = None
            self._populate_ui(self._original_payload)
            self.set_dirty(False)

    def set_dirty(self, state: bool):
        '''Track node changes'''
        if self._is_dirty == state: # Prevent inf recursion
            return
        self._is_dirty = state
        self.dataChanged.emit(state)

    def is_dirty(self) -> bool:
        '''Get node status'''
        return self._is_dirty

    def show_error(self, message: str) -> None:
        '''Called when prepare_editor fails. Override for custom UI output.'''
        logger.error(f'{self.__class__.__name__}: {message}')

###----------------------------------- Read Only Editors ----------------------------------###

class BaseViewer(BaseEditor):
    '''Convenience base for Read Only Editors.
    provides no-ops for all mutable logic'''
    is_mutable = False

    def set_dirty(self, state: bool):
        pass


###--------------------------------------------------------------------------------------###
###------------------------------------- Sources ----------------------------------------###
###--------------------------------------------------------------------------------------###

###------------------------------- Shared Source Data ------------------------------------###

@dataclass(frozen=True)
class SourceGeometry:
    '''Static per-source layout constants.'''
    sector_size:        int = 0x800
    sector_header_size: int = 0
    sector_ecc_size:    int = 0
    iso_9660_pvd:       int = 16
    pvd_byte_offset:    int = 0x9C

    @property
    def logical_sector_size(self) -> int:
        return self.sector_size - self.sector_header_size - self.sector_ecc_size

    def scratch_alignment(self) -> int:
        '''Scratch/staging buffer alignemnt.'''
        return self.logical_sector_size

@dataclass(slots=True, frozen=True)
class TocEntry:
    '''Canonical TOC entry shape.'''
    id:         int
    lba:        int
    size:       int
    offset:     int
    logical_id: int
    name:       str

class SourceRebuildFlags(Flag):
    '''Contract every source profile's rebuild flags must satisfy
    Every source must define a minimum of, NONE = 0, the no-patch flag.'''

def get_member(flags: SourceRebuildFlags, name: str) -> SourceRebuildFlags | None:
    '''Lookup a flag by name for a source without importing specific flags'''
    return type(flags).__members__.get(name)

###----------------------------------- Package & Conflict Detection ---------------------------------------###

class PackageIntent(Enum):
    '''What actions a package needs to perform.'''
    ACCESS = auto()
    IMPORT = auto()

@dataclass(frozen=True, slots=True)
class PackageMember:
    '''A single required member (node) of a package.'''
    role:     str
    hid:      tuple[int, ...]
    required: bool = True

@runtime_checkable
class LinkLookup(Protocol):
    '''Resolves links for a package, satisfied by NodeMetadataStore.'''
    def link_of(self, hid: tuple[int, ...]) -> tuple[int, ...] | None: ...

@dataclass(frozen=True, slots=True)
class ConflictFinding:
    '''One source-defined filesystem conflict, resolved by ModTracker.'''
    other:  VfsNode
    reason: str

###-------------------------------------- ISO Structs ---------------------------------------###

def pack_both_endian_32(val):
    '''LBA and File Size root directory structs'''
    return struct.pack('<I', val) + struct.pack('>I', val)

def pack_both_endian_16(val):
    '''Volume Sequence Numbers for root directory structs'''
    return struct.pack('<H', val) + struct.pack('>H', val)

@dataclass(frozen=True)
class RootDirectoryStructure:
    entry_length:           int
    extended_attribute:     int
    lba:                    int
    file_size:              int
    date:                   bytes
    flag:                   int
    interleave_gap:         int
    volume_sequence_number: int
    filename_length:        int
    file_name:              str

    @classmethod
    def from_bytes(cls, record_data: bytes, sector_size: int = 0x800) -> RootDirectoryStructure:
        # Unpack the entry
        entry_length = record_data[0]
        ext_attr     = record_data[1]
        lba          = struct.unpack('<I', record_data[2:6])[0] * sector_size
        file_size    = struct.unpack('<I', record_data[10:14])[0]
        date_bytes   = record_data[18:25]
        flags        = record_data[25]
        gap_size     = record_data[27]
        vol_seq      = struct.unpack('<H', record_data[28:30])[0]
        name_len     = record_data[32]
        raw_name     = record_data[33 : 33 + name_len]
        if raw_name == b'\x00':
            file_name = '.'
        elif raw_name == b'\x01':
            file_name = '..'
        else:
            file_name = raw_name.decode('ascii', errors='replace').split(';')[0]
        return cls(
            entry_length=entry_length,
            extended_attribute=ext_attr,
            lba=lba,
            file_size=file_size,
            date=date_bytes,
            flag=flags,
            interleave_gap=gap_size,
            volume_sequence_number=vol_seq,
            filename_length=name_len,
            file_name=file_name,
        )

###------------------------------------ Source Regions/Layouts -----------------------------------------###

class DiskRegion(abc.ABC):
    '''One contiguous region of the final disk image.'''
    start_offset: int = 0
    padding:      int = 0
    @property
    @abc.abstractmethod
    def size(self) -> int:
        '''Byte size of the region'''
    def on_placed(self, cursor: int) -> None:
        '''Called by the planner once start_offset is known. Override when size dependson logic.'''
    def label(self) -> str:
        '''Human-readable identification for logging/progress.'''
        return self.__class__.__name__
    def write_to(self, dst: BinaryIO) -> int:
        '''Write contents directly to the destination stream, return number of bytes written.'''
        return 0

@dataclass(slots=True)
class RawCopyRegion(DiskRegion):
    '''A raw copy of a region from the source disk image.'''
    source_offset: int
    length:        int
    name:          str
    src_handle:    BlockDevice
    @property
    def size(self) -> int:
        return self.length
    def label(self) -> str:
        return self.name or f'RawCopy@{self.source_offset:#x}'
    def write_to(self, dst: BinaryIO) -> int:
        chunk_size = 32 * 1024 * 1024 # 32MB chunks
        bytes_left = self.length
        current_offset = self.source_offset
        while bytes_left > 0:
            read_size = min(chunk_size, bytes_left)
            dst.write(self.src_handle.pread_view(current_offset, read_size))
            bytes_left -= read_size
            current_offset += read_size
        return self.length

@dataclass(slots=True)
class StagedDataRegion(DiskRegion):
    '''Modified data region that has been staged for writing to the disk image.'''
    node:        VfsNode
    sector_size: int
    @property
    def size(self) -> int:
        if self.node.pending_data is None: return 0
        raw = len(self.node.pending_data)
        return raw + ((-raw) & (self.sector_size - 1))
    def label(self) -> str:
        return f'Staged:{self.node.name}'
    def write_to(self, dst: BinaryIO) -> int:
        if self.node.pending_data is None: return 0
        dst.write(self.node.pending_data)
        padding = self.size - len(self.node.pending_data)
        if padding:
            dst.write(b'\x00' * padding)
        return self.size

@dataclass(slots=True)
class ZeroFillRegion(DiskRegion):
    '''Padding region of a fixed length filled with zero bytes.'''
    length: int
    name:   str = 'padding'
    @property
    def size(self) -> int:
        return self.length
    def label(self) -> str:
        return self.name
    def write_to(self, dst: BinaryIO) -> int:
        dst.write(b'\x00' * self.length)
        return self.length

@dataclass(slots=True)
class AlignRegion(DiskRegion):
    '''Zero padding up to the next multiple of `alignement`'''
    alignment: int
    name:      str = 'alignment_padding'
    _size:     int = field(init=False, repr=False, default=0)
    @property
    def size(self) -> int:
        return self._size
    def on_placed(self, cursor: int) -> None:
        self._size = (-cursor) & (self.alignment - 1)
    def label(self) -> str:
        return self.name
    def write_to(self, dst: BinaryIO) -> int:
        dst.write(b'\x00' * self._size)
        return self._size

@dataclass(slots=True)
class PadToLbaRegion(DiskRegion):
    '''Zero padding up to a fixed LBA'''
    target_lba:  int
    sector_size: int
    name:        str = 'lba_padding'
    _size:       int = field(init=False, repr=False, default=0)
    @property
    def size(self) -> int:
        return self._size
    def on_placed(self, cursor: int) -> None:
        target = self.target_lba * self.sector_size
        if cursor > target:
            raise ValueError(f'Layout overran fixed offset for {self.name!r}: cursor={cursor} target={target}')
        self._size = target - cursor
    def label(self) -> str:
        return self.name
    def write_to(self, dst: BinaryIO) -> int:
        dst.write(b'\x00' * self._size)
        return self._size

@dataclass(slots=True)
class SentinelRegion(DiskRegion):
    '''A genuinely empty region used as a sentinel pointer.'''
    node: VfsNode
    @property
    def size(self) -> int:
        return 0
    def label(self) -> str:
        return f'Sentinel:{self.node.name}'
    def write_to(self, dst: BinaryIO) -> int:
        return 0

@dataclass(slots=True)
class TocRegion(DiskRegion):
    '''A final scrambled TOC region.'''
    total_entries: int
    sector_size:   int
    scramble_fn:   Callable[[list[int]], list[int]]
    entries:       list[tuple[VfsNode, DiskRegion | None]] = field(default_factory=list)
                      # ^^ Node, Self-reference or sentinel ^^
    @property
    def size(self) -> int:
        return self.total_entries * 3 * 4
    def label(self) -> str:
        return 'TOC'
    def write_to(self, dst: BinaryIO) -> int:
        flat = [0] * (self.total_entries * 3)
        for i, (node, region) in enumerate(self.entries[:self.total_entries]):
            if region is None or isinstance(region, SentinelRegion):
                flat[i] = -1
                flat[self.total_entries + i] = 0
            else:
                flat[i] = region.start_offset // self.sector_size
                flat[self.total_entries + i] = region.size // self.sector_size
            flat[2 * self.total_entries + i] = getattr(node, 'logical_id', 0)
        scrambled = self.scramble_fn(flat)
        toc_array = array.array('I', (x & 0xFFFFFFFF for x in scrambled))
        if sys.byteorder != 'little':
            toc_array.byteswap()
        raw = toc_array.tobytes()
        dst.write(raw)
        return len(raw)

@dataclass(slots=True)
class RootDirectoryRegion(DiskRegion):
    '''Represents the root directory region of an ISO filesystem.'''
    fixed_size:            int
    sector_size:           int = 0x800
    original_bytes:        bytes = b''
    front_section_entries: list[tuple[VfsNode, DiskRegion]] = field(default_factory=list)
    keep_unmatched:        bool = False
    @property
    def size(self) -> int:
        return self.fixed_size
    def label(self) -> str:
        return 'RootDirectory'
    def write_to(self, dst: BinaryIO) -> int:
        if not self.original_bytes:
            raise ValueError('original_bytes must be set before writing')
        output = bytearray()
        bytes_read = 0
        while bytes_read < self.fixed_size:
            entry_length = self.original_bytes[bytes_read]
            if not entry_length:
                output.append(0)
                bytes_read += 1
                continue
            record_slice = bytearray(self.original_bytes[bytes_read : bytes_read + entry_length])
            record = RootDirectoryStructure.from_bytes(bytes(record_slice), self.sector_size)
            bytes_read += entry_length

            if record.file_name not in ('.', '..'):
                name, sep, ext = record.file_name.rpartition('.')
                match = next(((n, r) for n, r in self.front_section_entries if n.name == name and n.extension == sep + ext), None)
                if match:
                    node, region = match
                    new_lba =  region.start_offset // self.sector_size
                    new_size = len(node.pending_data) if node.pending_data is not None else node.size
                    record_slice[2:10] = pack_both_endian_32(new_lba)
                    record_slice[10:18] = pack_both_endian_32(new_size)
                    output.extend(record_slice)
                elif self.keep_unmatched:
                    output.extend(record_slice)
            else:
                output.extend(record_slice)
        if len(output) < self.fixed_size:
            output.extend(b'\x00' * (self.fixed_size - len(output)))
        elif len(output) > self.fixed_size:
            output = output[:self.fixed_size]
        dst.write(output)
        return len(output)

@dataclass(slots=True)
class BinaryPatchRegion(DiskRegion):
    '''
    Special case for a region that needs to be mutated during the rebuild process.
    '''
    node:            VfsNode
    src_handle:      BlockDevice
    sector_size:     int
    patch_targets:   list[PatchSite] = field(default_factory=list)
    rebuild_context: RebuildContext | None = None
    @property
    def size(self) -> int:
        if self.node.pending_data is not None:
            return len(self.node.pending_data)
        return self.node.size
    def label(self) -> str:
        return f'Patched: {self.node}'
    def write_to(self, dst: BinaryIO) -> int:
        if self.node.pending_data is not None:
            data = bytearray(self.node.pending_data)
        else:
            data = bytearray(self.node.size)
            if self.node.size != self.src_handle.readinto(data, self.node.offset):
                raise ValueError(f'Expected {self.node.size} bytes, got {len(data)}')
        if self.rebuild_context is not None:
            for site in self.patch_targets:
                value = site.patch.compute_value(self.rebuild_context)
                site.patch.apply(data, site.relative_offset, value)
        dst.write(data)
        return len(data)

###----------------------------------- Patches ---------------------------------------###

@dataclass(frozen=True)
class RebuildContext:
    '''What a rebuild has already resolved that a patch may need'''
    values:    dict[str, Any] = field(default_factory=dict)
    regions:   dict[str, DiskRegion] = field(default_factory=dict)
    sector_size: int = 0x800
    def __getitem__(self, key: str) -> Any:
        return self.values[key]
    def lba_of(self, name: str) -> int:
        '''Return the LBA of the given region in the layout.'''
        try:
            return self.regions[name].start_offset // self.sector_size
        except KeyError:
            raise KeyError(f'No region named {name!r} in this layout. Known regions: {", ".join(self.regions.keys())}')

class BasePatch(abc.ABC):
    '''Overarching base class for all patches.
    BasePhysicalPatch  patches bytes of a physical file
    BaseVirtualPatch   patches bytes of a virtual file
    '''
    name:         ClassVar[str] = ''
    description:  ClassVar[str] = ''
    flag:         ClassVar[SourceRebuildFlags]
    is_physical:  ClassVar[bool]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if 'spec_id' not in cls.__dict__:
            return
        if not hasattr(cls, 'flag'):
            raise TypeError(f'{cls.__name__} must define a flag')
        if cls.flag.value and not cls.name:
            raise TypeError(f'{cls.__name__}: user selectable patches must define a name')

    @property
    def is_virtual(self) -> bool:
        return not self.is_physical

    @property
    def is_user_selectable(self) -> bool:
        '''Currently only return False on NONE (0) flag...'''
        return bool(self.flag.value)

    def is_active(self, flags: SourceRebuildFlags) -> bool:
        '''Whether this patch applies for the requested rebuild flags.'''
        return not self.flag.value or bool(flags & self.flag)

class BasePhysicalPatch(BasePatch):
    '''
    Patches bytes of a physical file in place during the rebuild process itself (PhysicalHandler).

    Must implement:
        candidate_selector(all_nodes)       filter which nodes this patch applies to
        locate(raw_data)                    find the relative offset of the patch site, or None when it is not present
        compute_value(context)              calculate the payload to inject
        apply(data, offset, value)          apply the computed value to the bytearray at offset
    '''
    is_physical: ClassVar[bool] = True

    @abc.abstractmethod
    def candidate_selector(self, all_nodes: list[VfsNode]) -> list[VfsNode]:
        '''Filter which nodes this patch applies to.'''

    @abc.abstractmethod
    def locate(self, raw_data: bytes) -> int | None:
        '''Find the relative offset of the patch site, or None when it is not present.'''

    @abc.abstractmethod
    def compute_value(self, context: RebuildContext) -> int:
        '''Calculate the payload to inject.'''

    @abc.abstractmethod
    def apply(self, data: bytearray, offset: int, value: int) -> None:
        '''Apply the computed value to the bytearray at offset.'''

class BaseVirtualPatch(BasePatch):
    '''
    Delegated virtual patch. Runs through a Leaf or Container handler `action` on node `hid`.

    Must implement:
        action      the handler action name
        hid         hierarchical id of the node to apply the patch to
    '''
    is_physical: ClassVar[bool] = False
    action:      ClassVar[str]
    hid:         ClassVar[tuple[int, ...]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if 'spec_id' in cls.__dict__ and not (hasattr(cls, 'action') and hasattr(cls, 'hid')):
            raise TypeError(f'{cls.__name__}: delegated patches must define both `action` and `hid`.')

Patches = tuple[BasePatch, ...]

def selectable_patches(patches: Sequence[BasePatch]) -> list[BasePatch]:
    '''Patches the user can toggle (in declaration order).'''
    return [patch for patch in patches if patch.is_user_selectable]

@dataclass(frozen=True)
class PatchSite:
    '''One resolved location for a direct PatchTarget's spec, provided by PatchLocator.'''
    node:            VfsNode
    relative_offset: int
    patch:            BasePhysicalPatch

class PatchLocator:
    '''Runs every physical patch against the physical nodes to find where it can be applied.'''
    def locate_all(
        self,
        handle:    BlockDevice,
        all_nodes: list[VfsNode],
        patches:   Sequence[BasePatch]
    ) -> list[PatchSite]:
        sites: list[PatchSite] = []
        for patch in patches:
            if not isinstance(patch, BasePhysicalPatch):
                continue
            for node in patch.candidate_selector(all_nodes):
                raw = handle.pread(node.offset, node.size)
                offset = patch.locate(raw)
                if offset is not None:
                    sites.append(PatchSite(node, offset, patch))
        return sites

###------------------------------ Rebuild Planner ------------------------------------###

class DiskLayoutPlanner:
    '''
    Pass1: accumulate regions in disk order
    resolve_offsets(): assign every region's start_offset by cumulative sum.
    pass2: write_all() emits every region's bytes sequentially
    '''
    def __init__(self) -> None:
        self.regions:     list[DiskRegion] = []
        self._alignments: dict[int, int] = {}  # (id, alignment)
        self._resolved:   bool = False

    def add(self, region: DiskRegion, alignment: int | None = None) -> DiskRegion:
        '''Append and return the region so callers can hold a reference for cross-referencing'''
        if self._resolved:
            raise RuntimeError('Cannot add regions after resolve_offsets() has been called')
        if alignment is not None:
            if alignment <= 0 or alignment & (alignment - 1):
                raise ValueError(f'alignment must be a power of 2, got {alignment}')
            self._alignments[id(region)] = alignment
        self.regions.append(region)
        return region

    def resolve_offsets(self) -> None:
        '''The lba_map concept collapsed into a simple cumulative offset calculation'''
        cursor = 0
        for region in self.regions:
            region.start_offset = cursor
            region.on_placed(cursor)
            cursor += region.size
            alignment = self._alignments.get(id(region))
            region.padding = (-cursor) & (alignment - 1) if alignment else 0
            cursor += region.padding
        self._resolved = True

    def total_size(self) -> int:
        return sum(region.size + region.padding for region in self.regions)

    def write_all(self, dst: BinaryIO, task_handle: TaskHandle, progress_every: int = 1) -> int:
        '''one pass - strict sequential write'''
        if not self._resolved:
            raise RuntimeError('resolve_offsets() must be called before write_all().')
        total_bytes = self.total_size()
        bytes_written = 0
        total_regions = len(self.regions)
        for i, region in enumerate(self.regions):
            task_handle.checkpoint()
            written = region.write_to(dst)
            if written != region.size:
                raise ValueError(f'Expected to write {region.size} bytes, but wrote {written}')
            bytes_written += written
            if region.padding:
                dst.write(b'\x00' * region.padding)
                bytes_written += region.padding
            if i % progress_every == 0 or i == total_regions - 1:
                pct = int((bytes_written / total_bytes) * 100) if total_bytes else 100
                task_handle.progress.emit(pct)
        return bytes_written

###-------------------------------------- Source Layout --------------------------------------##

class BaseSourceBuilder(abc.ABC):
    '''
    Plans and writes on a rebuilt source. A source implements one subclass; SourceHandler constructs it.

    Must Implement:
        build_layout()              add every region front to back with add_region()

    Entrypoint (do not override):
        build(dist, task_handle)    build_layout() > resolve_offsets() > write_all(), return bytes_written

    Optional Implement:
        source                      the BaseSource being built
        source_root                 the root directory of the ISO being built
        vfs_root                    the root directory of the VFS being built
        staged                      the set of nodes that have been staged
        flags                       the flags for the source rebuild
        toc, pvd, gap_nodes, geomtry, handle
        patch_sites                 the list of patch sites for the source rebuild
        sites_for(node)             the patch sites for a given node
        lba_of(name)                LBA of a given region name
        rebuild_context             the rebuild context for BinaryPatchRegion so patches can read named regions
        log(message)                progress message to the UI
    '''
    def __init__(
        self,
        *,
        source:      BaseSource,
        handle:      BlockDevice,
        source_root: VfsNode,
        vfs_root:    VfsNode,
        staged:      frozenset[VfsNode],
        flags:       SourceRebuildFlags,
        toc:         list[TocEntry],
        patch_sites: list[PatchSite],
        pvd:         RootDirectoryStructure,
        gap_nodes:   list[VfsNode],
        log:         Callable[[str], None] | None = None,
    ) -> None:
        self.source      = source
        self.geometry    = source.geometry
        self.handle      = handle
        self.source_root = source_root
        self.vfs_root    = vfs_root
        self.staged      = staged
        self.flags       = flags
        self.toc         = toc
        self.pvd         = pvd
        self.gap_nodes   = gap_nodes
        self.log         = log or (lambda message: None)

        self.planner: DiskLayoutPlanner = DiskLayoutPlanner()
        self.named:   dict[str, DiskRegion] = {}
        self.rebuild_context = RebuildContext(regions=self.named, sector_size=self.geometry.sector_size)

        self.patch_sites = [site for site in patch_sites if site.patch.is_active(flags)]
        self._sites_by_node: dict[VfsNode, list[PatchSite]] = {}
        for site in self.patch_sites:
            self._sites_by_node.setdefault(site.node, []).append(site)

    ### Contract
    @abc.abstractmethod
    def build_layout(self) -> None:
        '''Subclasses must implement this method to build the layout, by calling self.add_region() sequentially.'''

    ### Entrypoint
    def build(self, dst: BinaryIO, task_handle: TaskHandle, progress_every: int = 1) -> int:
        '''Plan, resolve and write a new source. Returns the number of bytes written.'''
        self.build_layout()
        self.log(f'Resolving phyiscal disk layout offsets for {len(self.planner.regions)} regions')
        self.planner.resolve_offsets()
        self.log('Starting sequential write...')
        return self.planner.write_all(dst, task_handle, progress_every)

    ### Helpers for build_layout
    def add_region(self, region: DiskRegion, name: str | None = None, alignment: int | None = None) -> DiskRegion:
        '''Append a region. Give it a name and look it up later (lab_of, patches)'''
        if name is not None:
            if name in self.named:
                raise ValueError(f"Layout already defines a region for name {name!r}")
            self.named[name] = region
        return self.planner.add(region, alignment=alignment)

    def lba_of(self, name: str) -> int:
        return self.rebuild_context.lba_of(name)

    def sites_for(self, node: VfsNode) -> list[PatchSite]:
        return self._sites_by_node.get(node, [])

    def has_flag(self, flag: SourceRebuildFlags | str) -> bool:
        '''True if the rebuild was requested with the flag'''
        if isinstance(flag, str):
            member = get_member(self.flags, flag)
            return member is not None and bool(self.flags & member)
        return bool(self.flags & flag)

###------------------------------------- Source -------------------------------------###

class BaseSource(abc.ABC):
    display_name:       ClassVar[str]
    toc_total_entries:  ClassVar[int]
    rebuild_flags:      ClassVar[type[SourceRebuildFlags]]
    builder:            ClassVar[type[BaseSourceBuilder] | None]
    geometry:           ClassVar[SourceGeometry] = SourceGeometry()
    hidden_toc_indices: ClassVar[frozenset[int]] = frozenset()
    runtime_file_names: ClassVar[frozenset[str]] = frozenset()
    patches:            ClassVar[tuple[BasePatch, ...]] = ()
    build_hashes:       ClassVar[dict[str, str]] = {}
    metadata_path:      ClassVar[str] = ''

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__} source_id={getattr(self, "source_id", '?')}>'

    ### Identificaiton
    @abc.abstractmethod
    def matches(self, handle: BlockDevice) -> bool:
        '''Returns True if the signature matches'''

    @abc.abstractmethod
    def validate_filesystem(self, root_children: list[VfsNode]) -> None:
        '''Raise ValueError if the initial filesystem is invalid'''

    ### Table of contents
    @abc.abstractmethod
    def locate_toc(self, root: VfsNode, handle: BlockDevice) -> int:
        '''Return the LBA of the TOC'''

    @abc.abstractmethod
    def decode_toc(self, handle: BlockDevice, toc_lba: int) -> list[TocEntry]:
        '''Read and parse the TOC into TocEntry objects'''

    @abc.abstractmethod
    def encode_toc(self, entries: list[TocEntry]) -> bytes:
        '''Serialize the TOC entries into a byte string'''

    def scramble_toc(self, flat_toc: list[int]) -> list[int]:
        '''Process the toc into valid data. Only necesarry if the TOC is not plain bytes'''
        return flat_toc

    ### Tree
    def resolve_boundary(self, root: VfsNode) -> VfsNode:
        '''Return the node that represents the boundary between the VFS and the ISO.
        Currently only supports a single boundary node.'''
        if root.children and root.children[-1].is_boundary:
            return root.children[-1]
        for child in root.children:
            if child.is_boundary:
                return child
        boundary = VfsNode()
        boundary.is_boundary = True
        root.append_child(boundary)
        return boundary

    def resolve_extension(self, node: VfsNode, header: bytes) ->  str:
        '''Return the node's extension from its header bytes.'''
        return '.bin'

    ### Packages / Conflicts / Metadata
    def members_for(self, node: VfsNode, intent: PackageIntent, links: LinkLookup) -> tuple[PackageMember, ...]:
        '''Return the members (nodes) that make up a package, or () if not a package.'''
        return ()

    def find_conflicts(self, incoming: VfsNode, pending: frozenset[VfsNode], links: LinkLookup) -> list[ConflictFinding]:
        '''Returns the conflicts between the incoming node and the pending nodes.'''
        return []

    def build_metadata(self, store: NodeMetadataStore) -> int:
        '''Build a metadata store from scratch.'''
        return 0

    ### Patches
    @property
    def patch_options(self) -> tuple[BasePatch, ...]:
        '''User toggleable patches.'''
        return tuple(selectable_patches(self.patches))

    def patches_for(self, flags: SourceRebuildFlags) ->  list[BasePatch]:
        '''Patches active for a rebuild with these flags. (NONE is always active)'''
        return [patch for patch in self.patches if patch.is_active(flags)]

    ### Registration check
    @classmethod
    def validate_definition(cls) -> None:
        '''Raise early (at startup, from Registry.register_source) if the source is incompletely defined.'''
        name = cls.__name__
        if not isinstance(cls.hidden_toc_indices, frozenset):
            raise TypeError(
                f"[{cls.__name__}] 'hidden_toc_indices' must be a frozenset "
                f"to prevent cross-rebuild state mutation."
            )
        if not isinstance(cls.runtime_file_names, frozenset):
            raise TypeError(
                f"[{cls.__name__}] 'runtime_file_names' must be a frozenset "
                f"to prevent cross-rebuild state mutation."
            )
        for attr in ('display_name', 'toc_total_entries', 'rebuild_flags', 'builder'):
            if not hasattr(cls, attr):
                raise TypeError(f'{name} must define `{attr}`.')
        if inspect.isabstract(cls):
            raise TypeError(f'{name} is abstract; implement {sorted(cls.__abstractmethods__)}.')
        none_flag = cls.rebuild_flags.__members__.get('NONE')
        if none_flag is None or none_flag.value != 0:
            raise TypeError(f'{name}.rebuild_flags must define NONE = 0 (the no-patch flag).')
        # if not (isinstance(cls.builder, type) and issubclass(cls.builder, BaseSourceBuilder)):
        #     raise TypeError(f'{name}.builder must be a BaseSourceBuilder subclass (the class, not an instance).')
        # if inspect.isabstract(cls.builder):
        #     raise TypeError(f'{name}.builder {cls.builder.__name__} is abstract; implement {sorted(cls.builder.__abstractmethods__)}.')
        seen: set[str] = set()
        for patch in cls.patches:
            if not isinstance(patch, BasePatch):
                raise TypeError(f'{name}.patches must hold BasePatch instances, got {patch!r}.')
            if inspect.isabstract(type(patch)):
                raise TypeError(f'Patch {type(patch).__name__} is abstract; implement {sorted(type(patch).__abstractmethods__)}.')
            if not isinstance(patch.flag, cls.rebuild_flags):
                raise TypeError(f'Patch {patch.name!r} flag {patch.flag!r} is not a member of {cls.rebuild_flags.__name__}.')
            if patch.name in seen:
                raise ValueError(f'{cls.__name__} has more than one patch with name, {patch.name!r}.')
            seen.add(patch.name)
