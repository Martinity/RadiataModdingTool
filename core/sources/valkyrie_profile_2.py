'''
source definitions for Valkyrie Profile 2: Silmeri on the PS2
'''
from __future__ import annotations

import struct
from typing import TYPE_CHECKING

from core.node import VfsNode
from core.registry import Registry
from core.contracts import BaseSource, TocEntry, RootDirectoryStructure
from core.extension_overrides import lookup_extension

if TYPE_CHECKING:
    from core.native.block_device import BlockDevice

import logging
logger = logging.getLogger(f'radiata.{__name__}')

VFS_MID  = 1
_GEOMETRY = BaseSource.Geometry(sector_size=0x800, iso_9660_pvd=16, pvd_byte_offset=0x9C)
_RUNTIME_REQUIRED_FILES = {'IOPRP300', 'SYSTEM', 'VP2_N', 'VP2_NB~1'}
_RUNTIME_EXECUTABLE_CANDITATES = {'SLUS_214', 'SLPM_664', 'SLES_546'}
_TOC_HOST_FILE = 'VP2_N'  # also the root of vfs 1

_KNOWN_BUILDS: dict[str, str] = {
    '': '',
}

###-------------------------------------- Flags ---------------------------------------------------###

class VP2RebuildFlags(BaseSource.Flags):
    '''No rebuild patches are defined yet for this source.'''
    NONE = 0

###------------------------------------ Registration ----------------------------------------------###

@Registry.register_source
class VP2Source(BaseSource):
    '''Valkyrie Profile 2: Silmeria (PS2). Iso9660 + TOC derived virtual filesystem

    Two roots:
        PHYSICAL    the ISO 9660 root directory
        VFS         the game's own files contained by one of the root directory files
    '''
    display_name        = 'Valkyrie Profile 2: Silmeria'
    build_hashes        = _KNOWN_BUILDS
    geometry             = _GEOMETRY
    total_entries       = 0x0C00
    hidden_toc_indices  = frozenset({0, 1})
    rebuild_flags       = VP2RebuildFlags

    signature           = b'VALKYRIEPROFILE2'
    toc_seed: int       = 0x49287491 # type: ignore
    required_files      = _RUNTIME_REQUIRED_FILES
    executable_candidate = _RUNTIME_EXECUTABLE_CANDITATES

    ### Identification
    def matches(self, handle: BlockDevice) -> bool:
        offset = (self.geometry.iso_9660_pvd * self.geometry.sector_size) + self.signature_offset
        return handle.pread(offset, len(self.signature)) == self.signature

    def resolve_vfs_root(self, root: VfsNode) -> VfsNode:
        '''Return the vfs root (_TOC_HOST_FILE) contained by the root directory'''
        for node in root.children:
            if node.name == _TOC_HOST_FILE:
                return node
        raise ValueError(f'No {_TOC_HOST_FILE} found in the root directory. Cannot resolve VFS.')

    ### TOC
    def locate_toc(self, root: VfsNode, handle: BlockDevice) -> int:
        '''The TOC sits at the start of VP2_N. Returns LBA of VP2_N'''
        for node in root.children:
            if node.name == _TOC_HOST_FILE:
                return node.offset // self.geometry.sector_size
        raise ValueError(f'{_TOC_HOST_FILE} not found, cannot locate the TOC.')

    def scramble_toc(self, flat_toc: list[int]) -> list[int]:
        '''Symmetric XOR for 3 column toc'''
        key = self.toc_seed
        scramble = flat_toc[:]
        for i in range(self.total_entries):
            scramble[0 * self.total_entries + i] ^= key
            key ^= (key << 1) & 0xFFFFFFFF
            scramble[1 * self.total_entries + i] ^= key
            key ^= (~self.toc_seed) & 0xFFFFFFFF
            scramble[2 * self.total_entries + i] ^= key
            key ^= ((key << 2) ^ self.toc_seed) & 0xFFFFFFFF
        return scramble

    def decode_toc(self, handle: BlockDevice, toc_lba: int) -> list[TocEntry]:
        total = self.total_entries
        offset = toc_lba * self.geometry.sector_size
        raw = handle.pread(offset, self.total_entries * 3 * 4)
        flat = self.scramble_toc(list(struct.unpack(f'<{total * 3}I', raw)))
        entries = []
        for i in range(total):
            lba = flat[i]
            entries.append(TocEntry(
                id=i,
                lba=lba,
                size=flat[total + i],
                offset=lba * self.geometry.logical_sector_size,
                logical_id=flat[(self.total_entries * 2) + i],
                name=f'File {i:04d}'
            ))
        return entries

    def encode(self, entries: list[TocEntry]) -> bytes:
        flat = [0] * (self.total_entries * 3)
        for i, entry in enumerate(entries):
            flat[i] = entry.lba
            flat[self.total_entries + i] = entry.size
            flat[(self.total_entries * 2) + i] = entry.logical_id
        scrambled = self.scramble_toc(flat)
        return struct.pack(f'<{self.total_entries * 3}I', *scrambled)

    ### Tree + Namespaces
    def build_tree(self, handle: BlockDevice, root: VfsNode, toc: list[TocEntry]) -> BaseSource.Tree:
        '''assign the namespaces fro the physical and vfs layers'''
        tree = self.Tree(root)
        vfs  = tree.mount(VFS_MID, label='VFS', default=True, host=self.resolve_vfs_root(root))
        for entry in toc:
            vfs.append_child(self._toc_node(entry, vfs))
        hidden = sum(1 for child in vfs.children if child.is_hidden)
        logger.info(f'Tree built - {len(vfs.children)} total files - {hidden} hidden files')
        return tree

    def _toc_node(self, entry: TocEntry, parent: VfsNode) -> VfsNode:
        if entry.size == 0: # Sentinel
            node = VfsNode(name=f'sentinel {entry.id}', offset=-1, size=-1, parent=parent)
            node.is_hidden = True
            node.logical_id = entry.logical_id
            return node
        node = VfsNode(
            name='Unknown',
            category=('',),
            offset=entry.offset,
            size=(entry.size * self.geometry.sector_size),
            parent=parent
        )
        node.is_physical = True
        node.logical_id = entry.logical_id
        if entry.id in self.hidden_toc_indices:
            node.is_hidden = True
        return node

    def read_iso(self, handle: BlockDevice) -> tuple[VfsNode, RootDirectoryStructure, list[VfsNode]]:
        if not self.matches(handle):
            raise ValueError(f'Source does not match the signature expected for source {type(self).__name__!r}')
        root = VfsNode(name='root')
        system_areas = VfsNode()
        # Read descriptor volume for root dir location
        offset  = (self.geometry.iso_9660_pvd * self.geometry.sector_size) + self.geometry.pvd_byte_offset
        pvd_len = handle.pread(offset, 1)[0]
        system_areas.append_child(VfsNode(name='System Area 1', offset=0, size=offset))
        pvd = RootDirectoryStructure.from_bytes(handle.pread(offset, pvd_len), self.geometry.sector_size)
        system_areas.append_child(VfsNode(name='System Area 2', offset=offset + pvd_len, size=pvd.lba - (offset + pvd_len)))
        # Read root dir for files
        bytes_read = 0
        root_dir_view = memoryview(handle.pread(pvd.lba, pvd.file_size))
        logger.debug(f'PVD file size: {pvd.file_size}')
        while bytes_read < pvd.file_size:
            entry_length = root_dir_view[bytes_read]
            bytes_read  += 1
            if not entry_length:
                break
            record_slice = root_dir_view[bytes_read - 1 : bytes_read - 1 + entry_length]
            bytes_read += entry_length - 1
            record = RootDirectoryStructure.from_bytes(record_slice.tobytes(), self.geometry.sector_size)
            if record.file_name in ('.', '..'):  # Skip self-references
                continue
            name, sep, ext = record.file_name.rpartition('.')
            node = VfsNode(
                name=name,
                category=('ISO',),
                extension=sep + ext,
                offset=record.lba,
                size=record.file_size,
                parent=root,
            )
            node.is_physical = True
            root.append_child(node)

        # ISO Integrity Check
        self.validate_filesystem(root.children)

        # Root directory to end of ISO filesystem system areas - for rebuilding
        pos = pvd.lba + pvd.file_size
        for node in sorted(root.children, key=lambda node: node.offset):
            gap = node.offset - pos
            if gap > 0:
                system_areas.append_child(VfsNode(name=f'AreaBefore{node.name}', size=gap, offset=pos))
            pos = max(pos, node.offset + node.size)
        return root, pvd, list(system_areas.children[2:])

    ###--------------------------------- Components -------------------------------------###

    class Metadata(BaseSource.Metadata):
        path = 'ui/assets/vp2_metadata.json'

    class Extensions(BaseSource.Extensions):
        def resolve(self, node: VfsNode, header: bytes) -> str:
            node.extension = lookup_extension(header)
            return node.extension
