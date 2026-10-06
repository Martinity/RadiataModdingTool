'''FIS texture handler and viewer for Radiata Stories.

Decodes PS2 PSMT8 / PSMT4 textures directly to QImage — no PIL dependency.
Registers both a handler (for VFS parsing) and an editor widget (for display),
so the workspace automatically opens the viewer when a FIS node is selected.
'''
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from PyQt6.QtGui import QImage, qRgba, qRed, qBlue, qGreen, qAlpha

from core.contracts import LeafHandler
from core.registry import Registry
from core.workers import ActionDef, ActionType
from core.node import VfsNode

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###--------------------------------- Constants ---------------------------------------###

FIS_MAGIC        = b'FIS\x00'
FIS_FLAG_SPECIAL = 0x2000

_PAL_8BPP = {0x13, 0x1B}
_PAL_4BPP = {0x14, 0x24, 0x2C}

_PSM_NAMES = {
    0x00: 'PSMCT32', 0x01: 'PSMCT24', 0x02: 'PSMCT16',
    0x13: 'PSMT8',   0x14: 'PSMT4',   0x1B: 'PSMT8H',
    0x24: 'PSMT4HL', 0x2C: 'PSMT4HH',
}

###------------------------------ FIS metadata -------------------------------------###

@dataclass(frozen=True)
class FISInfo:
    '''All parsed metadata for a single FIS texture.'''
    name:                 str
    flags:                int
    psm:                  int
    psm_name:             str
    bpp:                  int
    width:                int
    height:               int
    raw_width:            int
    raw_height:           int
    dimension_mode:       str
    swizzled:             bool
    padded_4bpp_clut:     bool
    palette_offset:       int | None
    palette_storage_size: int
    image_offset:         int
    image_size:           int

    @property
    def special_layout(self) -> bool:
        return bool(self.flags & FIS_FLAG_SPECIAL)

    def summary(self) -> str:
        return (
            f'Name:         {self.name!r}\n'
            f'PSM:          {self.psm_name} ({self.bpp}bpp)\n'
            f'Dimensions:   {self.width}×{self.height}\n'
            f'Swizzled:     {self.swizzled}\n'
            f'Padded CLUT:  {self.padded_4bpp_clut}\n'
            f'CLUT size:    {self.palette_storage_size}'
        )

class FisEditorPayload(NamedTuple):
    '''Structured result to pass to the editor'''
    image:      QImage    # Displayable image
    info:       FISInfo   # Str Statistics
    raw_bytes:  bytes     # for PNG export via editor button (don't re-decode)

###------------------------------ Binary Helpers -------------------------------------###

def _u16(data: bytes, off: int) -> int:
    return struct.unpack_from('<H', data, off)[0]

def _u32(data: bytes, off: int) -> int:
    return struct.unpack_from('<I', data, off)[0]

def _ps2_alpha(a: int) -> int:
    '''Convert PS2 alpha 7-bit(0-128) to 8-bit(0-255)'''
    return min(255, a * 2)

###----------------------------- CLUT Helpers ------------------------------------------###

def _read_palette(data: bytes, offset: int, count: int) -> list[tuple[int,int,int,int]]:
    return [
        (*struct.unpack_from('3B', data, offset + i * 4), _ps2_alpha(data[offset + i * 4 + 3]))
        for i in range(count)
    ]

def _read_4bpp_palette(data: bytes, offset: int, padded: bool) -> list[tuple[int,int,int,int]]:
    palette = []
    for i in range(16):
        pos = offset + i * 4
        if padded and i >= 8:
            pos += 0x20
        r, g, b, a = struct.unpack_from('4B', data, pos)
        palette.append((r, g, b, _ps2_alpha(a)))
    return palette

def _clut_interleave(pal: list) -> list:
    '''PS2 PSMT8 CLUT interleave, symmetrical (encode == decode).'''
    out = list(pal)
    for base in range(0, 256, 32):
        for i in range(8):
            out[base + 8 + i], out[base + 16 + i] = pal[base + 16 + i], pal[base + 8 + i]
    return out

###-------------------------------- Swizzling -------------------------------------###

def _unswizzle_psmt8(src: bytes, width: int, height: int) -> bytearray:
    '''Unswizzle PSMT8 textures.'''
    dst = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            bl = (y & ~0xF) * width + (x & ~0xF) * 2
            ss = (((y + 2) >> 2) & 1) * 4
            py = (((y & ~3) >> 1) + (y & 1)) & 7
            cl = py * width * 2 + ((x + ss) & 7) * 4
            bn = ((y >> 1) & 1) + ((x >> 2) & 2)
            si = bl + cl + bn
            li = y * width + x
            if si < len(src) and li < len(dst):
                dst[li] = src[si]
    return dst

def _unpack_4bpp(data: bytes, width: int, height: int) -> list[int]:
    '''Converts 4bpp packed bytes to list of nibbles'''
    expected = (width * height) // 2
    out = []
    for byte in data[:expected]:
        out.append(byte & 0x0F)
        out.append((byte >> 4) & 0x0F)
    return out

def _swizzle_psmt8(src: bytes | bytearray, width: int, height: int) -> bytearray:
    '''Swizzle PSMT8 textures (inverse of unswizzle).'''
    dst = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            bl = (y & ~0xF) * width + (x & ~0xF) * 2
            ss = (((y + 2) >> 2) & 1) * 4
            py = (((y & ~3) >> 1) + (y & 1)) & 7
            cl = py * width * 2 + ((x + ss) & 7) * 4
            bn = ((y >> 1) & 1) + ((x >> 2) & 2)
            si = bl + cl + bn
            li = y * width + x
            if si < len(dst) and li < len(src):
                # Write linear src index back into swizzled dst index
                dst[si] = src[li]
    return dst

def _pack_4bpp(indices: bytes, width: int, height: int) -> bytearray:
    '''Converts list of nibbles back to 4bpp packed bytes'''
    expected = (width * height) // 2
    out = bytearray(expected)
    for i in range(expected):
        low = indices[i * 2] & 0x0F
        high = indices[i * 2 + 1] & 0x0F
        out[i] = low | (high << 4)
    return out

def _expected_size(w: int, h: int, bpp: int) -> int | None:
    return {8: w * h, 4: (w * h) // 2, 16: w * h * 2, 32: w * h * 4}.get(bpp)

def _bpp(psm: int) -> int:
    if psm in _PAL_8BPP: return 8
    if psm in _PAL_4BPP: return 4
    if psm == 0x02:      return 16
    return 32

###----------------------------------- FIS parsing ------------------------------------------###

def parse_fis(data: bytes, *, swizzled: bool | None = None, padded_4bpp: bool | None = None) -> FISInfo:
    '''Parse FIS header and determine texture layout'''
    if len(data) < 0x30 or data[:4] != FIS_MAGIC:
        raise ValueError('Invalid FIS header')

    flags               = _u16(data, 0x10)
    name                = data[0x14:0x18].decode('ascii', errors='replace').strip('\0')
    pre_image_size      = _u32(data, 0x1C)
    pal_storage_size    = _u32(data, 0x20)

    if pal_storage_size == 0: # PSMCT32 Direct-Color check
        if len(data) < 0x108:
            raise ValueError('Invalid PSMCT32 FIS payload (too small)')
        # Extract dimensions from the 64-bit TRXREG at offset 0xC0
        trxreg = struct.unpack_from('<Q', data, 0xC0)[0]
        w = trxreg & 0xFFF
        h = (trxreg >> 32) & 0xFFF
        return FISInfo(
            name=name,
            flags=flags,
            psm=0x00,
            psm_name='PSMCT32',
            bpp=32,
            width=w,
            height=h,
            raw_width=w,
            raw_height=h,
            dimension_mode='trxreg_direct',
            swizzled=False,
            padded_4bpp_clut=False,
            palette_offset=None,
            palette_storage_size=0,
            image_offset=0x100,
            image_size=w * h * 4,
        )

    img_hdr_off         = pre_image_size + 0x10
    if img_hdr_off + 0x10 > len(data):
        raise ValueError('Invalid FIS payload')

    raw_w      = _u16(data, img_hdr_off + 0x00)
    raw_h      = _u16(data, img_hdr_off + 0x02)
    image_size = _u32(data, img_hdr_off + 0x04)
    setup_off  = _u32(data, img_hdr_off + 0x08)
    psm        = _u16(data, img_hdr_off + 0x0C)
    tw, th       = data[img_hdr_off + 0x0E], data[img_hdr_off + 0x0F]

    bpp_val    = _bpp(psm)

    def matches(w: int, h: int) -> bool:
        exp = _expected_size(w, h, bpp_val)
        return exp is not None and exp == image_size

    # Decode dimensions
    if flags & FIS_FLAG_SPECIAL:
        if psm in _PAL_8BPP:
            w, h, dim_mode = raw_w * 2, raw_h * 2, 'special_8bpp_x2'
        elif psm in _PAL_4BPP:
            w, h, dim_mode = raw_w * 2, raw_h * 4, 'special_4bpp_x2x4'
        else:
            w, h, dim_mode = raw_w, raw_h, 'special_unknown'
    else:
        w, h, dim_mode = raw_w, raw_h, 'stored'
        if not matches(w, h) and 0 <= tw <= 15 and 0 <= th <= 15:
            tw_w, tw_h = 1 << tw, 1 << th
            if matches(tw_w, tw_h):
                w, h, dim_mode = tw_w, tw_h, 'tw_th'

    # Fallback tw/th
    if not matches(w, h) and 0 <= tw <= 15 and 0 <= th <= 15:
        tw_w, tw_h = 1 << tw, 1 << th
        if matches(tw_w, tw_h):
            w, h, dim_mode = tw_w, tw_h, f'{dim_mode}_fallback_tw_th'

    # Palette location
    pal_off: int | None = None
    if pal_storage_size:
        computed = img_hdr_off - pal_storage_size
        if computed >= 0:
            pal_off = computed

    if swizzled is None:
        swizzled = bool(flags & FIS_FLAG_SPECIAL)
    if padded_4bpp is None:
        padded_4bpp = (
            bpp_val == 4 and pal_off is not None
            and _auto_padded_4bpp(data, pal_off, pal_storage_size)
        )

    return FISInfo(
        name=name,
        flags=flags,
        psm=psm,
        psm_name=_PSM_NAMES.get(psm, f'UNKNOWN_{psm:#04x}'),
        bpp=bpp_val,
        width=w,
        height=h,
        raw_width=raw_w,
        raw_height=raw_h,
        dimension_mode=dim_mode,
        swizzled=bool(swizzled),
        padded_4bpp_clut=bool(padded_4bpp),
        palette_offset=pal_off,
        palette_storage_size=pal_storage_size,
        image_offset=setup_off + 0x90,
        image_size=image_size,
    )

def _auto_padded_4bpp(data: bytes, pal_off: int, pal_size: int) -> bool:
    if pal_size < 0x60 or len(data) < pal_off + 0x60:
        return False
    def mostly_zero(chunk: bytes) -> bool:
        return chunk.count(0) >= len(chunk) * 0.75
    return (
        not mostly_zero(data[pal_off:pal_off + 0x20])
        and mostly_zero(data[pal_off + 0x20:pal_off + 0x40])
        and not mostly_zero(data[pal_off + 0x40:pal_off + 0x60])
    )

###------------------------------- FIS decoding ------------------------------------------###

def decode_fis(
    data: bytes,
    *,
    swizzled: bool | None = None,
    padded_4bpp: bool | None = None,
) -> tuple[QImage, FISInfo]:
    '''Decode a FIS texture into a QImage (Format_RGBA8888) and FISInfo.'''
    info = parse_fis(data, swizzled=swizzled, padded_4bpp=padded_4bpp)
    end = info.image_offset + info.image_size
    if end > len(data):
        raise ValueError(f'Truncated FIS: need 0x{end:X} bytes, have 0x{len(data):X}')

    w, h = info.width, info.height

    if info.psm == 0x00 and info.bpp == 32: # PSCMCT32 Direct-Color loading
        pixel_data = bytearray(data[info.image_offset: info.image_offset + info.image_size])
        # Scale PS2 alpha (0-128) to standard (0-255)
        for i in range(3, len(pixel_data), 4):
            pixel_data[i] = _ps2_alpha(pixel_data[i])
        img = QImage(bytes(pixel_data), w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
        return img, info

    if info.bpp == 8:  # 8-bit per pixel
        if info.palette_offset is None or len(data) < info.palette_offset + 0x400:
            raise ValueError('Insufficient 8bpp CLUT data')
        palette    = _clut_interleave(_read_palette(data, info.palette_offset, 256))
        pixel_data = bytearray(data[info.image_offset: info.image_offset + info.image_size])
        if info.swizzled:
            pixel_data = _unswizzle_psmt8(pixel_data, w, h)
        indices = bytes(pixel_data)

    elif info.bpp == 4: # 4-bit per pixel
        footprint = 0x60 if info.padded_4bpp_clut else 0x40
        if info.palette_offset is None or len(data) < info.palette_offset + footprint:
            raise ValueError('Insufficient 4bpp CLUT data')
        palette    = _read_4bpp_palette(data, info.palette_offset, padded=info.padded_4bpp_clut)
        pixel_data = bytearray(data[info.image_offset: info.image_offset + info.image_size])
        if info.swizzled:
            pixel_data = _unswizzle_psmt8(pixel_data, w // 2, h)
        indices = bytes(_unpack_4bpp(pixel_data, w, h))

    else: # Unknown format
        raise NotImplementedError(
            f'FIS decode only supports PSMT8/PSMT4; got {info.psm_name}'
        )
    qt_palette = [qRgba(r, g, b, a) for r, g, b, a in palette]
    img = QImage(indices, w, h, w, QImage.Format.Format_Indexed8).copy()
    img.setColorTable(qt_palette)
    return img, info

###------------------------------------- Handler -----------------------------------------###
@Registry.register(
    name='FIS Handler',
    extensions=('.fis',),
    supported_actions=(
        ActionDef('Properties', ActionType.DIALOG),
        ActionDef('Export as PNG', ActionType.EXPORT),
))
class FisHandler(LeafHandler):
    '''Leaf handler for FIS textures. The editor widget owns decoding + display.'''
    def __init__(self, source: bytes, parent: VfsNode | None = None) -> None:
        super().__init__(source)
        self.handler_parent = parent
        self._raw = source if isinstance(source, bytes) else bytes(source)

    def prepare_editor_data(self, node: VfsNode, raw_bytes: bytes) -> Any:
        img, info = decode_fis(raw_bytes)
        return FisEditorPayload(image=img, info=info, raw_bytes=raw_bytes)

    def decode_editor_data(self, node: VfsNode, payload: Any, **kwargs) -> bytes:
        '''Transforms QImage and modified CLUT back into raw FIS bytes'''
        if isinstance(payload, bytes):
            return payload

        if isinstance(payload, FisEditorPayload):
            img, original_bytes = payload.image, payload.raw_bytes
        elif isinstance(payload, tuple) or len(payload) == 2:
            img, original_bytes = payload
        else:
            raise TypeError(f"{self.__class__.__name__} expects a tuple of (QImage, original_bytes)")

        info = parse_fis(original_bytes)
        w, h = info.width, info.height

        # 1. Safely extract indices respecting QImage stride padding!
        bpl = img.bytesPerLine()
        ptr = img.bits()
        ptr.setsize(bpl * h)
        raw_bits = bytes(ptr)

        out_bytes = bytearray(original_bytes)
        if info.psm == 0x00 and info.bpp == 32: # PSMCT32 Direct-Color
            pixel_data = bytearray()
            for y in range(h):
                pixel_data.extend(raw_bits[y * bpl : y * bpl + w * 4])

            # Scale standard Alpha (0-255) back to PS2 Alpha (0-128)
            for i in range(3, len(pixel_data), 4):
                pixel_data[i] = pixel_data[i] >> 1

            start = info.image_offset
            end = start + info.image_size
            out_bytes[start:end] = pixel_data[:info.image_size]

        else: # PSMT8/PSMT4
            indices = bytearray()
            for y in range(h):
                indices.extend(raw_bits[y * bpl : y * bpl + w])

            # 2. Pack and Re-swizzle indices
            if info.bpp == 8:
                pixel_data = bytearray(indices)
                if info.swizzled:
                    pixel_data = _swizzle_psmt8(pixel_data, w, h)

            elif info.bpp == 4:
                pixel_data = _pack_4bpp(bytes(indices), w, h)
                if info.swizzled:
                    # 4bpp swizzling maps over packed memory, so width is halved
                    pixel_data = _swizzle_psmt8(pixel_data, w // 2, h)
            else:
                raise NotImplementedError(f"Repacking not supported for {info.psm_name}")

            # 3. Splice the modified pixel block back into the exact location
            start = info.image_offset
            end = start + info.image_size
            out_bytes[start:end] = pixel_data[:info.image_size]

            # 4. Serialize CLUT changes back to the binary
            if info.palette_offset is not None:
                qt_palette = img.colorTable()
                pal_off = info.palette_offset

                if info.bpp == 8:
                    # Re-interleave the palette array and scale Qt Alpha (0-255) back to PS2 (0-128)
                    pal = [(qRed(c), qGreen(c), qBlue(c), qAlpha(c) >> 1) for c in qt_palette]
                    pal += [(0, 0, 0, 0)] * (256 - len(pal)) # Ensure 256 colors
                    pal = _clut_interleave(pal)

                    max_pal_bytes = len(out_bytes) - pal_off
                    entries_safe = min(256, max_pal_bytes // 4)
                    for i, (r, g, b, a) in enumerate(pal[:entries_safe]):
                        struct.pack_into('4B', out_bytes, pal_off + i * 4, r, g, b, a)

                elif info.bpp == 4:
                    for i, c in enumerate(qt_palette[:16]):
                        r, g, b, a = qRed(c), qGreen(c), qBlue(c), qAlpha(c) >> 1
                        pos = pal_off + i * 4
                        if info.padded_4bpp_clut and i >= 8:
                            pos += 0x20
                        struct.pack_into('4B', out_bytes, pos, r, g, b, a)

        if node.size != len(out_bytes):
            logger.warning('Size of FIS texture has changed dispite texture resizing not implemented.')
        logger.debug(f'Original length: {node.size} New Length: {len(out_bytes)}')
        return bytes(out_bytes)

    def execute_action(
        self,
        node:        VfsNode,
        action_name: str,
        **kwargs,
    ) -> Any:
        if not self.task_handle:
            raise RuntimeError(f'No active task handle for {self.__class__.__name__}')
        if action_name == 'Properties':
            try:
                return parse_fis(self._raw).summary()
            except Exception as e:
                return f'Parse error: {e}'
        elif action_name == 'Export as PNG':
            file_path: Path | None = kwargs.get('file_path')
            if not file_path:
                return 'No output path provided'
            try:
                logger.info(f'Decoding {node.name} for PNG export...')
                img, info = decode_fis(self._raw)
                if not img.save(str(file_path), 'PNG'):
                    raise RuntimeError('QImage.save() returned False')
                logger.info(f'Exported {info.width}×{info.height} {info.psm_name} to {file_path.name}')
                return f'Saved to {file_path.name}'
            except Exception as e:
                logger.error(f'FIS PNG export failed: {e}', exc_info=True)
                raise
        return None
