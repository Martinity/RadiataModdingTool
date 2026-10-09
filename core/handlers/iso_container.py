"""
PhysicalHandler ISO related processing. Extraction, rebuilding, TOC parsing, disk verification

Something to look more closely at in the future is the assignment of logical IDs to nodes.
Currently I am aware of overlays getting ID 0, but for other types of new files research is needed.
"""

from __future__ import annotations

import logging
from pathlib import Path
import xxhash
from core.node import VfsNode
from core.registry import Registry, FormatResolver
from core.workers import TaskHandle
from core.native.block_device import BlockDevice
from core.contracts import BaseSource, PhysicalHandler, TocEntry, RootDirectoryStructure, SourceRebuildFlags

logger = logging.getLogger(f'radiata.{__name__}')

###------------------------------ ISO HANDLER ------------------------------------###


@Registry.register(name='ISO Handler', extensions=('.iso',))
class SourceHandler(PhysicalHandler):
    """
    Responsible for managing and keeping the ISOfs and Vfs consistent and in sync.
    Injected with IsoVariant and RebuildContext based on the source that is loaded.
    Defined in patch_registry.
    """
    def __init__(self, handle: BlockDevice, parent=None, source: BaseSource | None = None, resolver: FormatResolver = Registry):
        """Initialize iso properties"""
        super().__init__(handle, parent_node=parent)
        logger.info(f'SourceHandler initialized for {handle.name}')
        self.source:         BaseSource = source or resolver.detect_source(handle)
        self.geometry:       BaseSource.Geometry = self.source.geometry
        self.toc:            list[TocEntry]  = []
        self.toc_lba:        int = -1
        self.pvd:            RootDirectoryStructure | None = None
        self.cnf:            tuple[int, int] | None = None
        self.system_areas:   VfsNode = VfsNode()

    def get_raw_node(self, node: VfsNode) -> bytes:
        """Public call for the raw data of a physical node"""
        if node.pending_data is not None:
            return node.pending_data
        return self.handle.pread(node.offset, node.size)

    ###------------------------------------ Extract ISO ------------------------------------###

    def _get_iso_dir(self) -> VfsNode:
        '''
        Returns the root node of the ISO directory tree containing all files.
        '''
        if not self.source.matches(self.handle):
            raise ValueError(
                f'Source does not match the signature expected for source {self.source.__class__.__name__!r}'
            )

        self.root = VfsNode(name='root')
        root = self.root
        # Read descriptor volume for root dir location
        offset = (self.geometry.iso_9660_pvd * self.geometry.sector_size) + self.geometry.pvd_byte_offset
        pvd_len = self.handle.pread(offset, 1)[0]
        self.system_areas.append_child(VfsNode(name='System Area 1', offset=0, size=offset))
        self.pvd = RootDirectoryStructure.from_bytes(self.handle.pread(offset, pvd_len), self.geometry.sector_size)
        self.system_areas.append_child(VfsNode(name='System Area 2', offset=offset + pvd_len, size=self.pvd.lba))
        # Read root dir for files
        bytes_read = 0
        root_dir_view = memoryview(self.handle.pread(self.pvd.lba, self.pvd.file_size))
        logger.debug(f'PVD file size: {self.pvd.file_size}')
        while bytes_read < self.pvd.file_size:
            entry_length = root_dir_view[bytes_read]
            bytes_read  += 1
            if not entry_length:
                break
            record_slice = root_dir_view[bytes_read - 1 : bytes_read - 1 + entry_length]
            bytes_read += entry_length - 1
            record = RootDirectoryStructure.from_bytes(record_slice.tobytes(), self.geometry.sector_size)
            if record.file_name in ('.', '..'): # Skip self-references
                continue
            name, sep, ext = record.file_name.rpartition('.')
            node = VfsNode(
                name=name,
                category=('ISO',),
                extension=sep + ext,
                offset=record.lba,
                size=record.file_size,
                parent=root,
                target=None,
            )
            node.is_physical = True
            root.append_child(node)
            if ext == 'CNF':
                self.cnf = (record.lba * self.geometry.sector_size, record.file_size)

        # ISO Integrity Check
        self.source.validate_filesystem(root.children)

        # Root directory to end of ISO filesystem system areas - for rebuilding
        physical_files = sorted(root.children, key=lambda n: n.offset)
        pos = self.pvd.lba + self.pvd.file_size
        for node in physical_files:
            gap = node.offset - pos
            if gap > 0:
                gap_node = VfsNode(
                    name=f'AreaBefore{node.name}',
                    size=gap,
                    offset=pos,
                )
                self.system_areas.append_child(gap_node)
            pos = max(pos, node.offset + node.size)

        return root

    def _get_vfs_dir(self, toc: list[TocEntry]) -> VfsNode:
        '''
        Returns the root node of the VFS (the toc/gameassets)
        '''
        logger.debug('Building VFS tree from TOC')
        root = self.source.resolve_boundary(self.root)

        for entry in toc:
            disk_index = entry.id
            # Sentinel nodes
            if entry.size == 0:
                sentinel = VfsNode(
                    name=f'sentinel {disk_index}',
                    offset=-1,
                    size=-1,
                    parent=root,
                )
                sentinel.is_hidden = True
                sentinel.logical_id = entry.logical_id
                root.append_child(sentinel)
                continue
            # Valid nodes
            node = VfsNode(
                name='Unknown',
                category=('',),
                offset=entry.offset,
                size=(entry.size * self.geometry.sector_size),
                parent=root,
                target=None,
            )
            node.is_physical = True
            node.logical_id = entry.logical_id
            root.append_child(node)
            if disk_index in self.source.hidden_toc_indices:  # Hide file system nodes
                node.is_hidden = True
        logger.info(f'Tree built - {len(root.children)} total files - {sum(1 for valid in root.children if valid.is_hidden is True)} hidden files')
        return root

    def get_file_tree(self) -> VfsNode:
        """
        Return the entire ISO file tree required for runtime.

        The final source_root entry is always the vfs_root node.
        This is crucial to ensure the VfsManager tracks relational data correctly.

        Dynamically locate the TOC offset and process the TOC data.
        """
        source_root = self._get_iso_dir()
        self.toc_lba = self.source.locate_toc(source_root, self.handle)
        self.toc = self.source.decode_toc(self.handle, self.toc_lba)
        self._get_vfs_dir(self.toc)
        return source_root

    ###----------------------------------- Build ISO ------------------------------------------###

    def rebuild_node(
        self,
        root:         VfsNode,
        staged_nodes: list[VfsNode],
        output_path:  Path,
        build_flags:  SourceRebuildFlags,
        task_handle:  TaskHandle,
    ) -> bool:
        '''
        Rebuilds the ISO using DiskLayoutPlanner sequentially.
        IsoRebuildFlags.SLIMMED shifts the TOC location hardcode sector.

        Because the tool currently doesn't support mutable file systems the way the first
        three areas are built is redundant and can be collapsed into one copy. I wrote it
        this way to be easier to expand to adding/removing files in the future.
        '''
        src_path = self.handle.path
        if output_path.resolve() == src_path.resolve():
            raise ValueError('Cannot overwrite source ISO')
        if self.pvd is None:
            raise ValueError('PVD not found')
        vfs_root = self.source.resolve_boundary(root)
        try:
            builder = self.source.create_builder(
                source=self.source,
                handle=self.handle,
                source_root=root,
                vfs_root=vfs_root,
                staged=frozenset(staged_nodes),
                flags=build_flags,
                toc=self.toc,
                pvd=self.pvd,
                gap_nodes=list(self.system_areas.children[2:]),
                log=task_handle.log_message.emit
            )
            ### Write
            with open(output_path, 'wb') as dst:
                task_handle.log_message.emit('Starting sequential write...')
                new_size = builder.build(dst, task_handle)
            task_handle.log_message.emit(f'Wrote {new_size:,} bytes to {output_path.name}')
            return True

        except Exception as e:
            logger.error(f'Rebuild failed: {e}', exc_info=True)
            if output_path.exists() and output_path != src_path:
                try:
                    output_path.unlink()
                    logger.info(f'Removed partial output: {output_path.name}')
                except OSError as unlink_error:
                    logger.error(f'Failed to remove partial output: {unlink_error}', exc_info=True)
            return False

    ###---------------------------------- Utility -------------------------------------------###

    def get_region(self, root: VfsNode) -> str:
        """Return the str name of the region inferred from the ISO level files"""
        iso_file_names = {child.name for child in root.children}
        if 'SLPM_658' in iso_file_names:
            return 'Region: JPN'
        elif 'SLUS_212' in iso_file_names:
            return 'Region: USA'
        return 'Region: Unknown'

    def verify_source_integrity(self, task_handle: TaskHandle) -> str:
        '''
        Hash the ISO with xxhash and compare against known hashes.
        BlockDevice natively handles the pre-allocation of a zero-copy buffer.
        '''
        file_size = getattr(self.source, 'size', 0)
        hasher = xxhash.xxh128()
        chunk_size = (24 * 1024 * 1024) # 24MB chunks seemed to load the fastest I have tested
        bytes_read = 0
        while (chunk := self.handle.pread_view(size=chunk_size, offset=bytes_read)):
            task_handle.checkpoint()
            hasher.update(chunk)
            bytes_read += len(chunk)
            task_handle.progress.emit(int(bytes_read / file_size * 100))
        digest = hasher.hexdigest()
        build = self.source.build_hashes.get(digest, f'Modified/Unknown: {digest}')
        return 'Build: ' + build
