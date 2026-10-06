'''
source definitions for Radiata Stories on the PS2
'''
from __future__ import annotations

import struct
from dataclasses import dataclass

from core.native.block_device import BlockDevice
from core.node import VfsNode
from core.registry import Registry
from core.contracts import (
    SourceGeometry, TocEntry, RebuildContext, SourceRebuildFlags, ConflictFinding,
    PackageIntent, PackageMember, BasePhysicalPatch, BaseVirtualPatch, BaseSource, LinkLookup
)
from core.extension_overrides import lookup_extension

_GEOMETRY = SourceGeometry(sector_size=0x800, iso_9660_pvd=16, pvd_byte_offset=0x9C)
_RUNTIME_REQUIRED_FILES = {'IOPRP300', 'SYSTEM', 'VP2_N', 'VP2_NB~1'}
_RUNTIME_EXECUTABLE_CANDITATES = {'SLUS_214', 'SLPM_664', 'SLES-546'}

###--------------------------------- Boundary / Extensions -------------------------------------###

@dataclass
class BoundaryResolver:
    def resolve(self, root: VfsNode) -> VfsNode:
        for child in root.children:
            if child.is_boundary:
                return child
        for child in root.children:
            if child.name == 'VP2_N':
                child.is_boundary = True
                return child
        raise ValueError('Boundary not found')

@dataclass
class ExtensionResolver:
    def resolve(self, node: VfsNode, header: bytes) -> str:
        node.extension = lookup_extension(header)
        return node.extension

###----------------------------------- TOC Codec ---------------------------------------###

@dataclass
class TocCodec:
    seed:          int
    total_entries: int

    def locate(self, root: VfsNode, handle: BlockDevice) -> int:
        for node in root.children:
            if node.name == 'VP2_N':
                return node.offset
        raise ValueError('TOC not found')

    def _scramble(self, flat_toc: list[int]) -> list[int]:
        '''Symmetric XOR for 3 column toc'''
        key = self.seed
        scramble = flat_toc[:]
        for i in range(self.total_entries):
            scramble[0 * self.total_entries + i] ^= key
            key ^= (key << 1) & 0xFFFFFFFF
            scramble[1 * self.total_entries + i] ^= key
            key ^= (~self.seed) & 0xFFFFFFFF
            scramble[2 * self.total_entries + i] ^= key
            key ^= ((key << 2) ^ self.seed) & 0xFFFFFFFF
        return scramble

    def decode(self, handle, geometry: SourceGeometry, toc_lba: int) -> list[TocEntry]:
        offset = toc_lba * geometry.logical_sector_size
        print(hex(offset))
        raw = handle.pread(offset, self.total_entries * 3 * 4)
        flat = list(struct.unpack(f'<{self.total_entries * 3}I', raw))
        for i in flat[:20]: print(hex(i))
        flat = self._scramble(flat)
        for i in flat[:20]: print(hex(i))
        entries = []
        for i in range(self.total_entries):
            lba = flat[i]
            size = flat[self.total_entries + i]
            logical_id = flat[(self.total_entries * 2) + i]
            entries.append(TocEntry(
                id=i,
                lba=lba,
                size=size,
                offset=lba * geometry.logical_sector_size,
                logical_id=logical_id,
                name=f'File {i:04d}'
            ))
        for entry in entries:
            print(hex(entry.size), hex(entry.offset), hex(entry.logical_id))
        return entries

    def encode(self, entries: list[TocEntry]):
        flat = [0] * (self.total_entries * 3)
        for i, entry in enumerate(entries):
            flat[i] = entry.lba
            flat[self.total_entries + i] = entry.size
            flat[(self.total_entries * 2) + i] = entry.logical_id
        scrambled = self._scramble(flat)
        return struct.pack(f'<{self.total_entries * 3}I', *scrambled)

###---------------------------------------- Signature ---------------------------------------------###

@dataclass
class SignatureValidator:
    magic:        bytes
    magic_offset: int

    def matches(self, handle, geometry: SourceGeometry) -> bool:
        offset = (geometry.iso_9660_pvd * geometry.sector_size) + self.magic_offset
        found = handle.pread(offset, len(self.magic))
        return found == self.magic

    def validate_filesystem(self, root_children) -> None:
        found_names = {child.name for child in root_children}
        has_system = _RUNTIME_REQUIRED_FILES.issubset(found_names)
        has_executable = bool(_RUNTIME_EXECUTABLE_CANDITATES.intersection(found_names))
        if not (has_system and has_executable):
            raise ValueError(f'Invalid ISO: must contain {_RUNTIME_REQUIRED_FILES}, and one of {_RUNTIME_EXECUTABLE_CANDITATES}: have {found_names}')


###---------------------------------------- Registrations ---------------------------------------------###
class RebuildFlags(SourceRebuildFlags):
    NONE              = 0

_KNOWN_BUILDS: dict[str, str] = {
    '': '',
}

@Registry.register_source
class VP2Source(BaseSource):
    '''Valkyrie Profile 2: Silmeria (PS2): ISO9660'''
    display_name        = 'Valkyrie Profile 2: Silmeria'
    metadata_path       = 'ui/assets/vp2_metadata.json'
    build_hashes        = _KNOWN_BUILDS
    geometry            = _GEOMETRY
    toc_total_entries   = 0x0C00
    hidden_toc_indices  = frozenset()
    runtime_file_names  = frozenset(_RUNTIME_REQUIRED_FILES | _RUNTIME_EXECUTABLE_CANDITATES)
    rebuild_flags       = RebuildFlags
    patches             = ()
    builder             = None

    _SIGNATURE          = b'VALKYRIEPROFILE2'
    _SIGNATURE_OFFSET   = 0x28
    _TOC_SEED           = 0x49287491

    ### Identification
    def matches(self, handle: BlockDevice) -> bool:
        offset = (self.geometry.iso_9660_pvd * self.geometry.sector_size) + self._SIGNATURE_OFFSET
        return handle.pread(offset, len(self._SIGNATURE)) == self._SIGNATURE

    def validate_filesystem(self, root_children: list[VfsNode]) -> None:
        found_names = {child.name for child in root_children}
        has_system  = _RUNTIME_REQUIRED_FILES.issubset(found_names)
        has_executable = bool(_RUNTIME_EXECUTABLE_CANDITATES.intersection(found_names))
        if not (has_system and has_executable):
            raise ValueError(f'Invalid filesystem: missing {_RUNTIME_REQUIRED_FILES - found_names}')

    ### TOC
    def locate_toc(self, root: VfsNode, handle: BlockDevice) -> int:
        return 0x200000 // 0x800

    def scramble_toc(self, flat_toc: list[int]) -> list[int]:
        '''Symmetric XOR for 3 column toc'''
        total = self.toc_total_entries
        key = self._TOC_SEED
        scramble = flat_toc[:]
        for i in range(total):
            scramble[0 * total + i] ^= key
            key ^= (key << 1) & 0xFFFFFFFF
            scramble[1 * total + i] ^= key
            key ^= (~self._TOC_SEED) & 0xFFFFFFFF
            scramble[2 * total + i] ^= key
            key ^= ((key << 2) ^ self._TOC_SEED) & 0xFFFFFFFF
        return scramble

    def decode_toc(self, handle: BlockDevice, toc_lba: int) -> list[TocEntry]:
        total = self.toc_total_entries
        offset = toc_lba * self.geometry.sector_size
        raw = handle.pread_view(offset, total * 3 * 4)
        flat = self.scramble_toc(list(struct.unpack(f'<{total * 3}I', raw)))
        entries = []
        for i in range(total):
            lba = flat[i]
            entries.append(TocEntry(
                id=i,
                lba=lba,
                size=flat[total + i],
                offset=lba * self.geometry.logical_sector_size,
                logical_id=flat[(total * 2) + i],
                name=f'File {i:04d}'
            ))
        return entries

    def encode_toc(self, entries: list[TocEntry]) -> bytes:
        total = self.toc_total_entries
        flat = [0] * (total * 3)
        for i, entry in enumerate(entries):
            flat[i] = entry.lba
            flat[total + i] = entry.size
            flat[(total * 2) + i] = entry.logical_id
        return struct.pack(f'<{total * 3}I', *self.scramble_toc(flat))

    ### Extensions
    def resolve_extension(self, node: VfsNode, header: bytes) -> str:
        node.extension = lookup_extension(header)
        return node.extension

    ### Packages / conflicts / metadata
    def members_for(self, node: VfsNode, intent: PackageIntent, links: LinkLookup) -> tuple[PackageMember, ...]:
        '''A "Kods" container needs its datacenter header (and, when importing, the entity pack sub headers).'''
        return ()

    def find_conflicts(self, incoming: VfsNode, pending: frozenset[VfsNode], links: LinkLookup) -> list[ConflictFinding]:
        hid  = incoming.hierarchical_id
        header_hid = links.link_of(hid)
        findings: list[ConflictFinding] = []
        for other in pending:
            other_hid = other.hierarchical_id
            if header_hid is not None and other_hid == header_hid:
                findings.append(ConflictFinding(other, f'{incoming} depends on header from {other} which has pending modifications'))
            elif links.link_of(other_hid) == hid:
                findings.append(ConflictFinding(other, f'{other} depends on header from {incoming} which has pending modifications'))
        return findings

    def build_metadata(self, store):
        pass
