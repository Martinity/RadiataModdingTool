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
from core.contracts import BaseSource, PhysicalHandler, SourceRebuildFlags, OpenedSource

logger = logging.getLogger(f'radiata.{__name__}')

###------------------------------ ISO HANDLER ------------------------------------###


@Registry.register(name='ISO Handler', extensions=('.iso',))
class SourceHandler(PhysicalHandler):
    """
    Responsible for managing and keeping the ISOfs and Vfs consistent and in sync.
    Injected with IsoVariant and RebuildContext based on the source that is loaded.
    Defined in patch_registry.
    """
    def __init__(self, handle: BlockDevice, parent=None, source: BaseSource | None = None, resolver = Registry):
        """Initialize iso properties"""
        super().__init__(handle, parent_node=parent)
        logger.info(f'SourceHandler initialized for {handle.name}')
        self.source:   BaseSource = source or resolver.detect_source(handle)
        self.opened: OpenedSource | None = None
        self.geometry: BaseSource.Geometry = self.source.geometry

    def get_raw_node(self, node: VfsNode) -> bytes:
        """Public call for the raw data of a physical node"""
        if node.pending_data is not None:
            return node.pending_data
        return self.handle.pread(node.offset, node.size)

    ###------------------------------------ Extract ISO ------------------------------------###

    def get_file_tree(self) -> VfsNode:
        """Return the first depth of ."""
        if self.opened is None:
            self.opened = self.source.open_tree(self.handle)
        return self.opened.tree.root

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
        Applies patches for physical regions if source specified.

        Because the tool currently doesn't support mutable file systems the way the first
        three areas are built is redundant and can be collapsed into one copy. I wrote it
        this way to be easier to expand to adding/removing files in the future.
        '''
        if self.opened is None:
            raise RuntimeError('get_file_tree must run before rebuild_node.')
        src_path = self.handle.path
        if output_path.resolve() == src_path.resolve():
            raise ValueError('Cannot overwrite source.')
        try:
            with open(output_path, 'wb') as dst:
                new_size = self.source.rebuild(
                    dst,
                    task_handle,
                    handle=self.handle,
                    tree=self.opened.tree,
                    toc=self.opened.toc,
                    pvd=self.opened.pvd,
                    gap_nodes=self.opened.gap_nodes,
                    staged=frozenset(staged_nodes),
                    flags=build_flags,
                    log=task_handle.log_message.emit,
                )
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
        file_size = getattr(self.handle, 'size', 0)
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
