'''
ContainerHandler for handling all kods format and datacenter

Has two builds versions: raw headerless import (complex_ prefixed) and rebuild from children.
I would like to do another pass on this code and consolidate some of the logic that is only
slightly different between the two paths into a more unified helper. For now this will do.
'''
from __future__ import annotations

import struct
from io import BytesIO
from dataclasses import dataclass
from typing import Any

from core.contracts import ContainerHandler, PackageResult, ResolvedPackage
from core.extension_overrides import lookup_extension
from core.registry import Registry
from core.node import VfsNode
from core.workers import ActionDef, ActionType

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###-------------------------------------------- Roles ------------------------------------------------###

HEADER_ROLE         = 'kods.header'
SLOT_HEADER_PREFIX  = 'kods.slot_header:'

def slot_header_role(slot_index: int) -> str:
    return f'{SLOT_HEADER_PREFIX}{slot_index}'

def slot_index_of(role: str) -> int | None:
    if not role.startswith(SLOT_HEADER_PREFIX):
        return None
    try:
        return int(role.removeprefix(SLOT_HEADER_PREFIX))
    except ValueError:
        return None

###-------------------------------------------- KodsHandler -------------------------------------------###

@Registry.register(
    name='Kods Archiver',
    extensions=('.kods',),
    supported_actions=(
        ActionDef('Unpack',     ActionType.TREE_EXPAND),
        ActionDef('Properties', ActionType.DIALOG),
    ))
class KodsHandler(ContainerHandler):
    '''Wrapper for Kods archiver class'''
    def __init__(self, source: bytes, parent: VfsNode) -> None:
        super().__init__(source)
        self.handler_parent     = parent
        self.payload_view       = memoryview(self.handle.read())
        self.archiver           = KodsArchiver(self.payload_view)

    @property
    def datacenter_header(self) -> bytes | None:
        return self.package.data.get(HEADER_ROLE) if self.package else None

    def validate_package(self, node: VfsNode, package: ResolvedPackage) -> None:
        header = package.data.get(HEADER_ROLE)
        if header and header[:4] != b'Kods':
            raise ValueError(f'{node}: package header member is not a Kods header')

    def get_file_tree(self) -> VfsNode:
        '''System unifies all unpacks to one header thus can unpack generically'''
        root = VfsNode(
            name=f'{getattr(self.handler_parent, "name", "KODS")} contents',
            parent=self.handler_parent,
        )
        is_internal = not bool(self.datacenter_header)
        if not self.datacenter_header and self.payload_view[:4] != b'Kods':
            logger.error('No valid headers found for unpacking')
            return root

        if self.datacenter_header:
            header_view = memoryview(self.datacenter_header)
            header_obj = self.archiver.parse_header(header_view, False)
        else:
            header_obj = self.archiver.parse_header(self.payload_view, True)
            header_view = self.payload_view[:header_obj.payload_offset]
        master_map = self.archiver.get_kods_map(header_view, is_internal)
        for meta in master_map:
            if not meta.is_valid or meta.size == 0 or meta.size == -1:
                dummy_node = VfsNode(
                    name=f'sentinel {meta.node_index:03d}',
                    offset=meta.offset,
                    size=0,
                    parent=root
                )
                dummy_node.is_hidden = True
                root.append_child(dummy_node)
                continue

            header = bytes(self.payload_view[meta.offset : meta.offset + 8])
            ext: str = lookup_extension(header)
            name = f'{meta.node_index:04d}' if is_internal else f'Entry {meta.node_index:02d}'

            node = VfsNode(
                name=name,
                offset=meta.offset,
                size=meta.size,
                header=header,
                extension=ext,
                parent=root,
            )
            root.append_child(node)

        logger.debug(f'Unpacked {len(root.children)} sections from {root.name}')
        return root

    def get_raw_node(self, node: VfsNode) -> bytes:
        return self.payload_view[node.offset : node.offset + node.size].tobytes()

    ###------------------------------------- Rebuild ---------------------------------------------------###

    def rebuild_node(self, node: VfsNode, staged_nodes: list[VfsNode]) -> PackageResult:
        '''Routes to the correct rebuild strategy based on node state'''
        if not self.task_handle:
            raise RuntimeError(f'No active Task Handle for {self.__class__.__name__}')
        is_internal = not bool(self.datacenter_header)
        # Check original structure
        self.header_view  = (
            memoryview(self.datacenter_header)
            if self.datacenter_header
            else self.payload_view
        )
        self.header_obj = self.archiver.parse_header(self.header_view, is_internal)
        # Build the payload and header
        payload, header = self.build_container(node.children)
        if self.header_obj.is_internal:
            payload = header + payload
            header = None
        # Sector align payload
        padding = (-len(payload)) & (0x800 - 1)
        payload += b'\x00' * padding
        self.task_handle.log_message.emit(f'{node.hierarchical_id} Rebuilt Kods Archive. Original size:{node.size} New size:{len(payload)}')
        return PackageResult(payload, {HEADER_ROLE: header} if header else None)

    def build_container(self, children: list[VfsNode]) -> tuple[bytes, bytes]:
        '''
        Build a new payload + header from all children.

        Reuses the original header's slot layout (entry count, stride, mode,
        second-table presence) but recomputes the offset table and shift to
        match the new payload (the children).
        '''
        orig_offsets = self.archiver._get_offsets(self.header_view, self.header_obj)
        slots = self.archiver.classify_offsets(orig_offsets)

        # Resolve bytes for every 'normal' slot once.
        primary_data: dict[int, bytes] = {}
        for slot in slots:
            if slot.kind != 'normal':
                continue
            child = children[slot.index] if slot.index < len(children) else None
            if child is None:
                continue
            primary_data[slot.index] = child.pending_data if child.pending_data is not None else self.get_raw_node(child)

        # Preserve original physical ordering so whichever slot ran to EOF stays last.
        write_order = sorted(primary_data.keys(), key=lambda i: orig_offsets[i])
        shift = self._resolve_shift([primary_data[i] for i in write_order], self.header_obj)
        align_mask = (1 << shift) - 1

        new_payload = BytesIO()
        new_offset_of: dict[int, int] = {}
        for i in write_order:
            new_offset_of[i] = len(new_payload.getbuffer()) >> shift
            new_payload.write(primary_data[i])
            padding = (-len(new_payload.getbuffer())) & align_mask
            new_payload.write(padding * b'\x00')

        # Resolve every slot's definition and offset
        offsets: list[int] = []
        for slot in slots:
            if slot.kind == 'sentinel':
                offsets.append(-1)
            elif slot.kind == 'alias':
                offsets.append(new_offset_of.get(slot.alias_of, -1))
            else:
                offsets.append(new_offset_of.get(slot.index, -1))

        # Write the new header
        header_source = (
            self.datacenter_header
            if self.datacenter_header and not self.header_obj.is_internal
            else bytes(self.payload_view)
        )
        new_header = BytesIO()
        magic, control_word = struct.unpack_from('<II', header_source, 0)
        control_word = (control_word & ~(0x0F << 16)) | ((shift & 0x0F) << 16)
        new_header.write(struct.pack('<II', magic, control_word))
        new_header.write(header_source[8:self.header_obj.header_size])
        for offset in offsets: # Offsets
            try:
                new_header.write(
                    offset.to_bytes(self.header_obj.stride, 'little')
                    if offset != -1
                    else self.header_obj.sentinel.to_bytes(self.header_obj.stride, 'little')
                )
            except Exception as e:
                raise ValueError(f'Child container could not fit in the alloted size. {e}')
        if self.header_obj.has_second_table: # Secondary table
            second_table_start = self.header_obj.header_size + (self.header_obj.num_entries * self.header_obj.stride)
            new_header.write(header_source[second_table_start:self.header_obj.payload_offset])

        return new_payload.getvalue(), new_header.getvalue()

    @staticmethod
    def _offset_fits(offset: int, shift: int, sentinel: int) -> bool:
        '''Whether offset can fit in the shift alloted size.'''
        return (offset >> shift) < sentinel

    def _resolve_shift(self, raw_slots: list[bytes | None], header_obj: KodsArchiver.KodsHeader) -> int:
        '''Find the smallest shift that fits every offset.'''
        for shift in range(header_obj.shift, 16):
            align_mask = (1 << shift) - 1
            total      = 0
            fits       = True
            for data in raw_slots:
                if data is None:
                    continue
                if not self._offset_fits(total, shift, header_obj.sentinel):
                    fits = False
                    break
                total += len(data)
                total += (-total) & align_mask
            if fits:
                return shift
        raise ValueError('Child container could not fit in any alloted size.')

    def _complex_build_header(self, node: VfsNode, orig_main_header: bytes, new_payload: bytes) -> bytes:
        '''
        Focuses on building a new offset table for a file that is imported
        without a header, or transformation data. Enforces that the new
        header is structurally similar to the original. This could be limiting
        but the current philosophy is to ensure that the data is usable by the
        engine after a rebuild, and when the structure changes without any transformation
        data there is no way to verify the new structures. Ultimately, structural
        insurance should be on the modder themselves but the scene currently lacks
        tools to build against so I'm limiting it here.

        Headers are built based on a dedicated-slot convention, where a slot is i
        and a segment is derived as offset[i]:offset[i+1], and
        size is offset[i+1] - offset[i]. i+1 where i=total_entries is clamped to
        the previous entry.
        Segments are matched original->new payload via 4-byte magic.
        Extra data that is not matched from the original is not discarded, it is left
        as unreachable.
        '''
        header_view = memoryview(orig_main_header)
        header_obj = self.archiver.parse_header(header_view, is_internal=False)
        num_entries = header_obj.num_entries
        min_header_len = header_obj.header_size + num_entries * header_obj.stride
        if len(orig_main_header) < min_header_len:
            raise ValueError(
                f'{node}\'s original header has {num_entries} entries '
                f' but is only {len(orig_main_header)} bytes. Aborting header build.'
            )
        orig_offsets = self.archiver._get_offsets(header_view, header_obj)
        orig_payload = self.payload_view
        new_payload_size = len(new_payload)

        def _is_zero_size(i: int) -> bool:
            '''Verify if the slot is an alias in the original header.'''
            if orig_offsets[i] == -1:
                return False
            if i + 1 < num_entries:
                return orig_offsets[i + 1] == orig_offsets[i]
            return orig_offsets[i] == node.size

        is_run_member = [_is_zero_size(i) for i in range(num_entries)]

        # Grab an identifying magic for every standalone REAL slot, keyed by index
        slot_magic: dict[int, bytes] = {}
        for i in range(num_entries):
            if orig_offsets[i] == -1 or is_run_member[i]:
                continue
            next_offset = next(
                (orig_offsets[j] for j in range(i + 1, num_entries) if orig_offsets[j] != -1),
                None,
            )
            size = (next_offset if next_offset is not None else node.size) - orig_offsets[i]
            if size < 4:
                continue
            slot_magic[i] = bytes(orig_payload[orig_offsets[i] : orig_offsets[i] + 4])

        # Scan new_payload once, bucketing every position a given magic appears at.
        positions_by_magic: dict[bytes, list[int]] = {}
        wanted_magics = set(slot_magic.values())
        pos, payload_len = 0, len(new_payload)
        while pos <= payload_len - 4:
            candidate = new_payload[pos : pos + 4]
            if candidate in wanted_magics:
                positions_by_magic.setdefault(candidate, []).append(pos)
            pos += 4

        # Hand out found positions to slots in order
        slot_offsets: dict[int, int] = {}
        for index in sorted(slot_magic):
            bucket = positions_by_magic.get(slot_magic[index])
            if bucket:
                slot_offsets[index] = bucket.pop(0)

        logger.debug(
            f'Complex import magic scan found {len(slot_offsets)} segments '
            f'out of {len(slot_magic)} expected.'
        )

        # Resolve slot definitions and offsets
        offsets: list[int] = [0] * num_entries
        next_value = new_payload_size
        for i in range(num_entries - 1, -1, -1):
            if orig_offsets[i] == -1: # Sentinel
                offsets[i] = -1
                continue
            if is_run_member[i]: # Alias
                offsets[i] = next_value
                continue
            if i in slot_offsets: # Real
                offsets[i] = slot_offsets[i]
                next_value = offsets[i]
            else: # Original real was not found -> Alias
                logger.warning(f'{node} matching segment {i} not found in new payload. Treating as zero-size alias.')
                offsets[i] = next_value

        # Resolve shift
        shift = header_obj.shift
        max_offset = max((off for off in offsets if off != -1), default=0)
        while not self._offset_fits(max_offset, shift, header_obj.sentinel) and shift < 16:
            shift += 1
        if not self._offset_fits(max_offset, shift, header_obj.sentinel):
            raise ValueError('New payload offsets exceed container sentinel capacity.')

        # Write the header
        new_header = BytesIO()
        magic, control_word = struct.unpack_from('<II', orig_main_header, 0)
        control_word = (control_word & ~(0x0F << 16)) | ((shift & 0x0F) << 16)
        new_header.write(struct.pack('<II', magic, control_word))
        new_header.write(orig_main_header[8:header_obj.header_size])
        for off in offsets:
            preshifted = (off >> shift) if off != -1 else header_obj.sentinel
            new_header.write(preshifted.to_bytes(header_obj.stride, 'little'))
        if header_obj.has_second_table:
            second_table_start = header_obj.header_size + (header_obj.num_entries * header_obj.stride)
            payload_offset = header_obj.header_size + (header_obj.num_entries * header_obj.stride * 2)
            new_header.write(orig_main_header[second_table_start:payload_offset])

        return new_header.getvalue()

    def _slot_map(self, header: bytes, payload_length: int) -> dict[int, tuple[int, int]]:
        '''
        Take a header and payload and return valid slots {slot_index: (offset, size)}.
        Can't use KodsArchiver as is since it is coupled to the packed kods at construction...
        '''
        header_obj = self.archiver.parse_header(memoryview(header), is_internal=False)
        offsets = self.archiver._get_offsets(memoryview(header), header_obj)
        valid = [(i, o) for i, o in enumerate(offsets) if o != -1]
        result: dict[int, tuple[int, int]] = {}
        for pos, (i, o) in enumerate(valid):
            next_o = valid[pos + 1][1] if pos + 1 < len(valid) else payload_length
            result[i] = (o, next_o - o)
        return result

    def complex_import(
        self,
        node:             VfsNode,
        orig_main_header: bytes,
        new_payload:      bytes,
        orig_sub_headers: dict[int, bytes] | None = None,
    ) -> tuple[bytes, dict[int, bytes]]:
        '''
        Rebuild the main datacenter header against the new_payload, the rebuild the sub-
        datacenter headers, if provided, againsts the main datacenter headers new AND original
        slot segment. Sub-datacenter headers initialize a dummy KodsHandler and reenter here.

        Returns (new_main_header, {slot_index: new_sub_header, ...}).
        '''
        new_main_header = self._complex_build_header(node, orig_main_header, new_payload)
        if not orig_sub_headers: # non-entity pack or is itself a sub-datacenter-header
            return new_main_header, {}

        orig_slots = self._slot_map(orig_main_header, len(self.payload_view))
        new_slots  = self._slot_map(new_main_header, len(new_payload))

        new_children: dict[int, bytes] = {}
        for slot_index, slot_orig_main_header in orig_sub_headers.items():
            orig_slot = orig_slots.get(slot_index)
            new_slot  = new_slots.get(slot_index)
            # Verify the slots
            if orig_slot is None or orig_slot[1] <= 0:
                logger.warning(f'{node}: slot {slot_index} has no valid original segment, skipping.')
                continue
            if new_slot is None or new_slot[1] <= 0:
                logger.warning(f'{node}: slot {slot_index} has no valid segment after outer rebuild, skipping.')
                continue

            # Send valid slots to a sub-header rebuild
            orig_offset, orig_size = orig_slot
            new_offset, new_size = new_slot
            orig_segment = self.payload_view[orig_offset : orig_offset + orig_size].tobytes()
            new_segment  = new_payload[new_offset : new_offset + new_size]
            dummy_node = VfsNode(name=f'slot {slot_index:02d}', offset=0, size=len(orig_segment), extension='.Kods')
            if node.parent is None:
                raise TypeError(f'No parent for {node}')
            with KodsHandler(orig_segment, node.parent) as slot_handler:
                new_child_header, _ = slot_handler.complex_import(dummy_node, slot_orig_main_header, new_segment)
                new_children[slot_index] = new_child_header

        return new_main_header, new_children

    def import_payload(self, node: VfsNode, new_payload: bytes, package: ResolvedPackage) -> PackageResult:
        main_header = package.data.get(HEADER_ROLE)
        if not main_header:
            raise ValueError(f'{node}: no header in package to import against')
        sub_headers: dict[int, bytes] = {}
        for role, data in package.data.items():
            slot = slot_index_of(role)
            if slot is not None and len(data) > 8:  # Skip empty or small headers
                sub_headers[slot] = data
        new_main, new_subs = self.complex_import(node, main_header, new_payload, sub_headers)
        companions = {HEADER_ROLE: new_main}
        companions.update({slot_header_role(i): header for i, header in new_subs.items()})
        return PackageResult(new_payload, companions)

    ### ------------------ Properties and Execute actions ------------------------ ###

    def get_properties(self) -> str:
        is_internal = not bool(self.datacenter_header)
        if self.datacenter_header:
            header_view = memoryview(self.datacenter_header)
        else:
            header_obj = self.archiver.parse_header(self.payload_view, is_internal=True)
            header_view = self.payload_view[:header_obj.header_size] # Captures only up to the offset table

        lines = []
        p = self.archiver.parse_header(
            header_view,
            is_internal=is_internal
        )
        mode_str = (
            "32bit aligned"
            if not p.mode
            else "16bit aligned"
        )
        lines.extend([
            f"Number of Entries: {p.num_entries}",
            f"Compression shift: {p.shift}",
            f"Entry mode: {mode_str}",
            f"Secondary table present: {p.has_second_table}",
        ])
        if p.payload_offset:
            lines.append(
                f"Size of Pre-Payload data: {p.payload_offset} bytes"
            )
        lines.append("")
        return "\n".join(lines)

    def execute_action(self, node: VfsNode, action_name: str, **kwargs) -> Any:
        if action_name == 'Unpack':
            return self.get_file_tree()
        elif action_name == 'Properties':
            return self.get_properties()
        return None

###----------------------------------------------- Archiver ----------------------------------------------------###

class KodsArchiver:
    '''Archiver class for all kods archive related processing.'''
    @dataclass(slots=True)
    class KodsHeader:
        num_entries:      int
        shift:            int
        mode:             bool
        stride:           int
        has_second_table: bool
        bit30:            bool
        sentinel:         int
        format:           str
        is_internal:      bool
        header_size:      int = 0
        payload_offset:   int = 0

    @dataclass(slots=True)
    class FileNodeMeta:
        ''''Represents a mapped file from any header source'''
        node_index:   int
        offset:       int  # relative offset into payload
        size:         int
        is_valid:     bool

    def __init__(self, payload: bytes | memoryview | bytearray):
        self.payload_view   = memoryview(payload)
        self.payload_length = len(self.payload_view) # Includes header size if internal header present

    ###---------------------------------------- Unpack ----------------------------------------------###

    def get_kods_map(self, header: memoryview, is_internal: bool) -> list[KodsArchiver.FileNodeMeta]:
        '''Generate a single offset map into the payload from from the provided header'''
        # Get offsets, and shifts
        header_obj = self.parse_header(header, is_internal)
        offsets = self._get_offsets(header, header_obj)
        kods_map: list[self.FileNodeMeta] = []
        for i, offset in enumerate(offsets): # Get Basic Segment metadata (missing size)
            is_valid = offset != -1
            if is_valid and is_internal:
                is_valid = offset < self.payload_length
            node_metadata = self.FileNodeMeta(i, offset, 0, is_valid)
            kods_map.append(node_metadata)

        valid_nodes = [node for node in kods_map if node.is_valid]
        valid_nodes.append(self.FileNodeMeta(-1, self.payload_length, -1, False)) # EOF sentinel

        for current_node, next_node in zip(valid_nodes, valid_nodes[1:]): # Calculate valid node sizes
            if current_node.offset == next_node.offset:
                # TODO ALIAS...
                continue
            if next_node.offset == -1:
                current_node.size = -1
            else:
                current_node.size = next_node.offset - current_node.offset
        return kods_map

    ###-------------------------------------------- Unpack Helpers --------------------------------------------###

    def parse_header(self, header_view: memoryview, is_internal: bool) -> KodsHeader:
        '''Return dataclass with all header definitions'''
        if len(header_view) < 8:
            return self.KodsHeader(num_entries=0,shift=0,mode=False,stride=0,has_second_table=False,bit30=False,sentinel=0,format='None',is_internal=True)

        magic, control_word = struct.unpack_from("<II", header_view, 0)
        if magic != 0x73646F4B: # 'Kods'
            return self.KodsHeader(num_entries=0,shift=0,mode=False,stride=0,has_second_table=False,bit30=False,sentinel=0,format='None',is_internal=True)

        # Bit Field Extraction
        num_entries = control_word & 0xFFFF              # Bits 0-15
        shift = (control_word >> 16) & 0x0F              # Bits 16-19
        mode = (control_word >> 20) & 0x01               # Bits 20
        has_second_table = (control_word >> 29) & 0x01   # Bit 29
        bit30 = (control_word >> 30) & 0x01              # Bit 30

        # Data alignement
        data_format = "<H" if mode else "<I"
        stride = 2 if mode else 4
        sentinel = 0xFFFF if mode else 0xFFFFFFFF
        header_size = 8
        table_count = 2 if has_second_table else 1
        payload_offset =  header_size + (num_entries * stride * table_count) if is_internal else 0

        return self.KodsHeader(
            num_entries,
            shift,
            bool(mode),
            stride,
            bool(has_second_table),
            bool(bit30),
            sentinel,
            data_format,
            is_internal,
            header_size,
            payload_offset
        )

    @dataclass(slots=True)
    class OffsetSlot:
        '''Describes a single offset slot.'''
        index:    int
        kind:     str                 # 'normal' | 'alias' | 'sentinel'
        alias_of: int | None = None

    def classify_offsets(self, offsets: list[int]) -> list[KodsArchiver.OffsetSlot]:
        '''Classify every table index as sentinel, normal, or alias-of-another-index.'''
        groups: dict[int, list[int]] = {}
        for i, offset in enumerate(offsets):
            if offset == -1:
                continue
            groups.setdefault(offset, []).append(i)

        primary_of: dict[int, int] = {i: max(idxs) for idxs in groups.values() for i in idxs}
        slots: list[KodsArchiver.OffsetSlot] = []
        for i, offset in enumerate(offsets):
            if offset == -1:
                slots.append(self.OffsetSlot(i, 'sentinel'))
            elif primary_of[i] == i:
                slots.append(self.OffsetSlot(i, 'normal'))
            else:
                slots.append(self.OffsetSlot(i, 'alias', alias_of=primary_of[i]))
        return slots

    def original_sizes(self, slots: list[KodsArchiver.OffsetSlot], offsets: list[int], payload_length: int) -> dict[int, int]:
        '''Size, in the original payload, of every 'normal' slot: the gap to the next
        distinct physical offset, or to EOF for whichever slot is physically last.
        Alias and sentinel slots have no size of their own.'''
        normals = sorted((s.index for s in slots if s.kind == 'normal'), key=lambda i: offsets[i])
        sizes: dict[int, int] = {}
        for pos, i in enumerate(normals):
            next_offset = offsets[normals[pos + 1]] if pos + 1 < len(normals) else payload_length
            sizes[i] = next_offset - offsets[i]
        return sizes

    def _get_offsets(self, header_view: memoryview, header_obj: KodsArchiver.KodsHeader) -> list[int]:
        '''Return list of offsets for files inside the Kods container'''
        offsets = []
        for i in range(header_obj.num_entries):
            offset_pos = header_obj.header_size + (i * header_obj.stride)
            raw_offset = struct.unpack_from(header_obj.format, header_view, offset_pos)[0]
            # Collect offset data
            if raw_offset == header_obj.sentinel:
                offset = -1
            elif raw_offset == 0:
                offset = header_obj.payload_offset
            else:
                offset = header_obj.payload_offset + (raw_offset << header_obj.shift)
            offsets.append(offset)
        return offsets
