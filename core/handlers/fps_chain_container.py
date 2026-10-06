'''ContainerHandler for unpacking what I think the game calls "spf" tbh I am not sure tho
I did not need to analyse any MIPS to reverse this format at this surface level'''
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from core.contracts import ContainerHandler
from core.workers import ActionDef, ActionType
from core.node import VfsNode
from core.extension_overrides import lookup_extension
from core.registry import Registry

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###------------------------------- Chain handler -----------------------------------###

'''
fps payload [28:32] offset to FIS payload + header size (0x40)
[20:24] 6 = fis is contained?
'''
@Registry.register(
    name='Chain Handler',
    extensions=('.fps','.fas', '.rmac', '.tgil', '.xbdc', '.dnal', '.lctp', '.idom', '.ndnc', '.pcdc'),
    supported_actions=(
        ActionDef('Deconstruct Chain', ActionType.TREE_EXPAND),
        ActionDef('Properties', ActionType.DIALOG)
))
class FpsChainHandler(ContainerHandler):
    @dataclass(frozen=True, slots=True)
    class ChainHeader:
        magic: bytes
        payload_size: int
        offset_from_last_file: int
        next_file_offset: int

    def __init__(self, source: bytes, parent: VfsNode):
        super().__init__(source)
        self.handler_parent = parent
        self.data = memoryview(source)

    def get_raw_node(self, node: VfsNode) -> bytes:
        return bytes(self.data[node.offset : node.offset + node.size])

    def get_file_tree(self) -> VfsNode:
        root = VfsNode(
            name=f"{getattr(self.handler_parent, 'name', 'Chain')}_contents",
            parent=self.handler_parent,
        )
        max_size = len(self.data)
        pos = 0
        idx = 0
        while pos < max_size:
            raw_header = bytes(self.data[pos:pos+16])
            header_obj = self.parse_header(raw_header)
            ext: str = lookup_extension(header_obj.magic)
            ext = f'{ext}-Segment'

            node = VfsNode(
                name=str(idx),
                category=self.handler_parent.category,
                offset=pos,
                size=header_obj.payload_size + 16,
                extension=ext,
                parent=root
            )
            node.parent_header = raw_header
            root.append_child(node)

            if not header_obj.next_file_offset:
                break
            pos += header_obj.payload_size + 16
            idx += 1

        logger.debug(f'Successfully unpacked {len(root.children)} nodes from chain')
        return root

    def rebuild_node(self, node: VfsNode, staged_nodes: list[VfsNode]) -> bytes:
        if not self.task_handle:
            raise RuntimeError(f'No background task manager active for {self}')
        staged_set = set(staged_nodes)
        previous_size = 0
        new_node = bytearray()
        for i, child in enumerate(node.children): # Calculate new header and copy payload
            if not isinstance(child.parent_header, bytes):
                raise TypeError(f'Expected bytes for parent header, got {type(child.parent_header)}')
            self.task_handle.checkpoint()
            is_last = (i == len(node.children) - 1)
            payload = (
                child.pending_data[16:]
                if child in staged_set and child.pending_data is not None
                else bytes(self.data[child.offset + 16 : child.offset + child.size])
            )
            new_node += self._build_header(child.parent_header[:4], payload, previous_size, is_last)
            new_node += payload

            previous_size = len(payload)
        self.task_handle.log_message.emit(f'{node.hierarchical_id} New spf chain built . Original size:{node.size} New size:{len(new_node)}')
        return bytes(new_node)

    def get_properties(self, node: VfsNode):
        return 'Not yet Implemented'

    def execute_action(self, node: VfsNode, action_name: str, **kwargs) -> Any:
        if action_name == 'Deconstruct Chain':
            return self.get_file_tree()
        elif action_name == 'Properties':
            return self.get_properties(node)
        return None

    def parse_header(self, header: bytes) -> ChainHeader:
        return self.ChainHeader(
            magic=header[:4],
            payload_size=int.from_bytes(header[4:8], 'little'),
            offset_from_last_file=int.from_bytes(header[8:12], 'little'),
            next_file_offset=int.from_bytes(header[12:16], 'little'),
        )

    def _build_header(self, magic: bytes, payload: bytes, previous_size: int, is_last: bool) -> bytes:
        new_header = bytearray(magic)
        new_header += len(payload).to_bytes(4, 'little')
        new_header += previous_size.to_bytes(4, 'little')
        next_offset = 0 if is_last else (len(payload) + 16)
        new_header += next_offset.to_bytes(4, 'little')

        return bytes(new_header)
