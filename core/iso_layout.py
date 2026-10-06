'''
Reusable building blocks for ISO-based source builders.

contracts.py only defines the contract (BaseSourceBuilder, regions, planner). This module holds
IsoSourceBuilder: a BaseSourceBuilder with the emit_* steps most ISO sources share. A source
subclasses it and composes the steps it needs, in disk order, with the parameters it needs.
Nothing here is applied implicitly, and no source should import another source's builder.

    class MyBuilder(IsoSourceBuilder):
        toc_region_name = 'toc'
        def build_layout(self) -> None:
            self.emit_prologue()
            self.emit_runtime_files(require_patch=True)
            self.emit_toc_placement(fixed_lba=494979, slim_flag='SLIMMED')
            self.emit_toc_table()
            self.emit_vfs_payload(self_reference_index=0)

Tree reminder: self.source_root is the PHYSICAL tree (executable, SYSTEM, ... plus the boundary node),
self.vfs_root is the boundary node whose children are the TOC derived virtual files.
'''
from __future__ import annotations

from core.contracts import (
    BaseSourceBuilder, RawCopyRegion, StagedDataRegion, AlignRegion, PadToLbaRegion, SentinelRegion,
    TocRegion, RootDirectoryRegion, BinaryPatchRegion,
)

_SENTINEL_LBAS = frozenset({-1, 0xFFFFFFFF})  # decode() reads unsigned, so a stored -1 comes back as 0xFFFFFFFF

class IsoSourceBuilder(BaseSourceBuilder):
    '''
    BaseSourceBuilder plus the common ISO layout steps. Still abstract: implement build_layout().

    Class attributes (override per source):
        root_dir_name     name the root directory region is registered under
        toc_region_name   name the TOC region is registered under (patches look it up with lba_of)
        toc_padding_name  name of the padding between the ISO filesystem and the TOC

    Steps (call from build_layout in disk order):
        emit_prologue()             system area, PVD record, area up to the root directory, root directory
        emit_runtime_files()        physical ISO files and the gaps between them (needs emit_prologue first)
        emit_toc_placement()        padding between the ISO filesystem and the TOC
        emit_toc_table()            the scrambled TOC (entries are filled by emit_vfs_payload)
        emit_vfs_payload()          the virtual file system data, one sector aligned region per node
    '''
    root_dir_name:    str = 'root_dir'
    toc_region_name:  str = 'toc'
    toc_padding_name: str = 'toc_padding'

    def emit_prologue(self, *, keep_unmatched_records: bool = False) -> None:
        '''System area, PVD record, area up to the root directory, then the root directory itself.'''
        geo = self.geometry
        sys_area_1_len = (geo.iso_9660_pvd * geo.sector_size) + geo.pvd_byte_offset
        self.add_region(RawCopyRegion(0, sys_area_1_len, 'System Area', self.handle))
        self.add_region(RawCopyRegion(sys_area_1_len, self.pvd.entry_length, 'PVD Record', self.handle))
        pvd_end = sys_area_1_len + self.pvd.entry_length
        self.add_region(RawCopyRegion(pvd_end, self.pvd.lba, 'System Area 2', self.handle))
        self.add_region(RootDirectoryRegion(
            fixed_size=self.pvd.file_size,
            sector_size=geo.sector_size,
            original_bytes=self.handle.pread(self.pvd.lba, self.pvd.file_size),
            keep_unmatched=keep_unmatched_records,
        ), name=self.root_dir_name)

    def emit_runtime_files(self, *, only_runtime: bool = True, require_patch: bool = False) -> None:
        '''The physical ISO files (and the gaps between them) in disk order, each registered in the root
        directory. Files with direct patch sites become BinaryPatchRegions. These are copied at their
        original size without alignment because the gap regions already cover the padding.

        only_runtime:  only emit files named in source.runtime_file_names (drops everything else)
        require_patch: fail the build if no file received a patch. Only enable this for sources that
                       declare a direct patch for the executable that is always active.
        '''
        root_dir = self.named.get(self.root_dir_name)
        if not isinstance(root_dir, RootDirectoryRegion):
            raise ValueError(f'emit_runtime_files needs emit_prologue() first (no region named {self.root_dir_name!r})')
        nodes = [child for child in self.source_root.children if child.is_physical or not child.is_boundary]
        nodes += self.gap_nodes
        nodes.sort(key=lambda node: node.offset)
        suffixes = tuple(self.source.runtime_file_names)
        patched = False
        for node in nodes:
            if only_runtime and not node.name.endswith(suffixes):
                continue
            if node.name.startswith('AreaBefore'):
                self.add_region(RawCopyRegion(node.offset, node.size, node.name, self.handle))
                continue
            sites = self.sites_for(node)
            if sites:
                region = BinaryPatchRegion(
                    node, self.handle, self.geometry.sector_size,
                    patch_targets=sites, rebuild_context=self.rebuild_context,
                )
                patched = True
            elif node in self.staged and node.pending_data is not None:
                region = StagedDataRegion(node, self.geometry.sector_size)
            else:
                region = RawCopyRegion(node.offset, node.size, node.name, self.handle)
            self.add_region(region)
            root_dir.front_section_entries.append((node, region))
        if require_patch and not patched:
            raise ValueError('No ISO file received a patch; the executable could not be patched')

    def emit_toc_placement(self, *, fixed_lba: int, slim_flag: str | None = None) -> None:
        '''Padding between the ISO filesystem and the TOC.
        Normally pads to fixed_lba (the executable expects the TOC there). If slim_flag names a member of
        the source's SourceRebuildFlags and it is set, it only aligns to a sector so the TOC follows
        immediately, and the executable patch carries the new LBA.'''
        sector = self.geometry.sector_size
        if slim_flag and self.has_flag(slim_flag):
            self.add_region(AlignRegion(sector, 'TOC_alignment_padding'), name=self.toc_padding_name)
        else:
            self.add_region(PadToLbaRegion(fixed_lba, sector, 'TOC_offset_Padding'), name=self.toc_padding_name)

    def emit_toc_table(self) -> None:
        '''The scrambled TOC, using the source's toc_total_entries and scramble_toc. Registered as
        toc_region_name. Entries are filled by emit_vfs_payload, so this step must come before it.'''
        self.add_region(TocRegion(
            total_entries=self.source.toc_total_entries,  # changing this means the executable must be patched too
            sector_size=self.geometry.sector_size,
            scramble_fn=self.source.scramble_toc,
        ), name=self.toc_region_name)

    def emit_vfs_payload(self, *, self_reference_index: int | None = 0) -> None:
        '''The virtual file system's data, one sector-aligned region per node, recorded in the TOC.
        self_reference_index: TOC slot that describes the TOC itself and gets no region (None for no such slot).'''
        toc_region = self.named.get(self.toc_region_name)
        if not isinstance(toc_region, TocRegion):
            raise ValueError(f'emit_vfs_payload needs emit_toc_table() first (no TOC region named {self.toc_region_name!r})')
        sector = self.geometry.sector_size
        for idx, child in enumerate(self.vfs_root.children):
            if idx == self_reference_index:
                toc_region.entries.append((child, None))
                continue
            orig_lba = self.toc[idx].lba if idx < len(self.toc) else 0
            if (child.size == -1 and child not in self.staged) or orig_lba in _SENTINEL_LBAS:
                toc_region.entries.append((child, SentinelRegion(child)))
                continue
            if child in self.staged and child.pending_data is not None:
                region = StagedDataRegion(child, sector)
            else:
                region = RawCopyRegion(child.offset, child.size, child.name, self.handle)
            toc_region.entries.append((child, region))
            self.add_region(region, alignment=sector)
