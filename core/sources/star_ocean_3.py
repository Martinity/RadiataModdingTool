'''
source definitions for Star Ocean: Till the End of Time on the PS2
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

VFS_MID = 1
_GEOMETRY = BaseSource.Geometry(sector_size=0x800, iso_9660_pvd=16, pvd_byte_offset=0x9C)
_RUNTIME_REQUIRED_FILES = {'IOPRP271', 'SYSTEM'}
_RUNTIME_EXECUTABLE_CANDIDATES = {
    'SLUS_204', 'SLUS_208', 'SLPM_652', 'SLES_820', 'SCPC_550', 'SLPM_654', 'SLPM_664',
    'SCAJ_200'
}
_FIXED_TOC_LBA = 0x200000 // 0x800
_KNOWN_BUILDS: dict[str, str] = {
    '': '',
}

###-------------------------------------- Flags ---------------------------------------------------###

class SOC3RebuildFlags(BaseSource.Flags):
    '''No rebuild patches are defined yet for this source.'''
    NONE = 0

###------------------------------------ Registration ---------------------------------------###

@Registry.register_source
class SOC3Source(BaseSource):
    '''Star Ocean: Till the End of Time (PS2). ISO9660 + TOC derived virtual filesystem.

    Two roots:
        PHYSICAL    the ISO 9660 root directory
        VFS         the game's own file table - Resolves the same way that Radiata Stories does but
                                                in the current implementation uses a hardcoded offset
    '''
    display_name            = 'Star Ocean: Till the End of Time'
    build_hashes            = _KNOWN_BUILDS
    geometry                 = _GEOMETRY
    total_entries           = 0x1800
    hidden_toc_indices      = frozenset({0})
    rebuild_flags           = SOC3RebuildFlags

    signature               = b'STAROCEANTET'
    toc_seed: int           = 0x13578642 # type: ignore
    required_files          = _RUNTIME_REQUIRED_FILES
    executable_candidates   = _RUNTIME_EXECUTABLE_CANDIDATES

    ### Identification
    def matches(self, handle: BlockDevice) -> bool:
        offset = (self.geometry.iso_9660_pvd * self.geometry.sector_size) + self.signature_offset
        return handle.pread(offset, len(self.signature)) == self.signature

    ### TOC
    def locate_toc(self, root: VfsNode, handle: BlockDevice) -> int:
        '''The TOC sits at offset _FIXED_TOC_LBA but could also be resolved through the executable.'''
        return _FIXED_TOC_LBA

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
        raw = handle.pread_view(offset, self.total_entries * 3 * 4)
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

    def encode_toc(self, entries: list[TocEntry]) -> bytes:
        flat = [0] * (self.total_entries * 3)
        for i, entry in enumerate(entries):
            flat[i] = entry.lba
            flat[self.total_entries + i] = entry.size
            flat[(self.total_entries * 2) + i] = entry.logical_id
        return struct.pack(f'<{self.total_entries * 3}I', *self.scramble_toc(flat))

    ### Tree + Namespaces
    def build_tree(self, handle: BlockDevice, root: VfsNode, toc: list[TocEntry]) -> BaseSource.Tree:
        '''assign the namespaces for the physical and vfs layers'''
        tree = self.Tree(root)
        vfs  = tree.mount(VFS_MID, label='VFS', default=True)
        for entry in toc:
            vfs.append_child(self._toc_node(entry, vfs))
        hidden = sum(1 for child in vfs.children if child.is_hidden)
        logger.info(f'Tree built - {len(vfs.children)} total files - {hidden} hidden files')
        return tree

    def _toc_node(self, entry: TocEntry, parent: VfsNode) -> VfsNode:
        if entry.size == 0: # Sentinels
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
        path = 'ui/assets/soc3_metadata.json'

    class Extensions(BaseSource.Extensions):
        def resolve(self, node: VfsNode, header: bytes) -> str:
            node.extension = lookup_extension(header)
            return node.extension
