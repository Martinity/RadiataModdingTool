import struct
import json
import re
from pathlib import Path
from typing import Any, Optional, Tuple
from Components.Iso import HEADERS

###---------------------------- Packing ----------------------------###

def start_kods_packing(input_dir: Path, output_path: Path, original_kods: Path) -> None:
    # Validations
    if not input_dir.is_dir():
        raise ValueError(f'Input must be a directory: {input_dir}')
    if not original_kods.is_file():
        raise ValueError(f'Original .kods not found: {original_kods}')
    with open(original_kods, 'rb') as f:
        raw_original = f.read()
    if not raw_original[:4] == b'Kods':
        raise ValueError('Original file is missing kods header')
    original_name = original_kods.name
    # Determine original parameters
    params = _parse_header(raw_original)
    original_length = len(raw_original)
    # Check if original's last offset entry is sentinel
    last_pos = params['table_base'] + ((params['num_offsets'] - 1) * params['alignment'])
    last_raw = struct.unpack_from(params['format'], raw_original, last_pos)[0]
    last_is_sentinel = (last_raw == params['sentinel'])
    # Extended table based on original to keep metadata and files contiguous
    extended_raw: Optional[bytes] = None
    if params['extended_table']:
        ext_start = params["table_base"] + params['num_offsets'] * params["alignment"]
        ext_size = params['num_offsets'] * params["alignment"]
        extended_raw = raw_original[ext_start : ext_start + ext_size]
        print(f"Template from original: {params['num_offsets']} slots + extended table ({ext_size} bytes)")
    # Information needed for a successful packing
    data_blocks = _prepare_data_blocks(input_dir, params['num_offsets'] - 1)
    aliases = _load_aliases(input_dir)
    raw_tail = _load_tail(input_dir)
    best_params = _analyze_kods_outcomes(data_blocks, params)
    kods_header = _create_kods_header(best_params)
    offsets, final_blocks = _calculate_offsets(data_blocks, best_params, aliases, last_is_sentinel)
    # The packing and reporting
    packed_length = _pack_kods(output_path, kods_header, best_params, final_blocks, offsets, extended_raw, raw_tail, original_name, original_length)
    _report_sectors(original_length, packed_length)
    print('Final Parameters:',best_params)


def _prepare_data_blocks(input_dir: Path, num_entries: int) -> list:
    # Gather bins for rebuild into an organised list
    file_map: dict[int, bytes] = {}
    pattern = re.compile(r'_([0-9A-Fa-f]{4})\.\w+$', re.IGNORECASE)
    for p in input_dir.iterdir():
        m = pattern.search(p.name)
        if m:
            idx = int(m.group(1), 16)
            if idx < num_entries:
                file_map[idx] = p.read_bytes()

    return [file_map.get(i, None) for i in range(num_entries)]


def _load_tail(input_dir: Path):
    # Check for XXXX_tail.bin file
    tail_file = list(input_dir.glob('*_tail.*'))
    if tail_file:
        tail = tail_file[0].read_bytes()
        print(f'Found tail will append: {tail_file[0].name} ({len(tail)} bytes)')
        return tail
    return None


def _load_aliases(input_dir: Path) -> dict:
    aliases_path = input_dir / '_aliases.json'
    if aliases_path.exists():
        with open(aliases_path) as f:
            return {int(k): v for k, v in json.load(f).items()}
    return {}


def _analyze_kods_outcomes(data_blocks: list, original_params: dict) -> dict[str,Any]:
    # Get data
    unique_blocks = {}
    total_unique_size = 0
    for block in data_blocks:
        if block is not None and block and block not in unique_blocks:
            unique_blocks[block] = True
            total_unique_size += len(block)
    # Generate new params based on certain original params to avoid corruption
    num_entries = len(data_blocks) + 1
    entry_type = original_params['entry_type']
    alignment = 2 if entry_type else 4
    format = '<H' if entry_type else '<I'
    sentinel = 0xFFFF if entry_type else 0xFFFFFFFF

    table_size = original_params['table_base']+ (num_entries * alignment * (2 if original_params['extended_table'] else 1))

    print(f"Analyzing {len(data_blocks)} files ({total_unique_size / 1024:.2f} KB unique data)...")

    best_parameters = None
    # Find best shift
    test_shifts = sorted(list(set([original_params['shift'], 2, 4, 11] + list(range(2, 13)))))
    for shift in test_shifts:
        denominator = 2 ** shift
        # No padding between table and data — offsets are relative to table_end
        table_padding = 0

        current_offset = table_padding
        possible = True
        for block in data_blocks:
            if block is None or not block: continue
            if (current_offset >> shift) >= sentinel:
                possible = False
            current_offset += (len(block) + denominator - 1) & ~(denominator - 1)
        if possible:
            best_parameters = {
                'shift': shift,
                'entry_type': entry_type,
                'alignment': alignment,
                'extended_table': original_params['extended_table'],
                'denominator': denominator,
                'padding': table_padding,
                'num_offsets': num_entries,
                'format': format,
                'sentinel': 0xFFFF if entry_type else 0xFFFFFFFF,
                'reason': f"Safe {original_params['format']} table with shift {shift}"
            }
        if shift >= original_params['shift']:
            break

    if not best_parameters:
        raise ValueError('Archive data is too large for the required table size!')

    return best_parameters


def _create_kods_header(params: dict) -> int:
    kods_header = (
            (params['num_offsets'] & 0xFFFF) |
            ((params['shift'] & 0xF) << 16) |
            ((params['entry_type'] & 0x3) << 20) |
            (int(params['extended_table']) << 29) |
            (0 << 30) |
            (0 << 31) # runtime flag
        )
    return kods_header


def _calculate_offsets(data_blocks: list[Optional[bytes]], params: dict,
                       aliases: dict = None, last_is_sentinel: bool = False) -> Tuple[list[int], list[bytes]]:
    aliases = aliases or {}
    pos = params['padding']
    shifted_offsets = []
    final_data_blobs = []

    for i, block in enumerate(data_blocks):
        if i in aliases:
            shifted_offsets.append(None)  # placeholder, fix up after
            continue
        if block is None: # NULL / missing entry
            shifted_offsets.append(params['sentinel'])
            continue
        if len(block) == 0: # zero-length valid entry (Bug 3 fix)
            shifted_offsets.append(pos >> params['shift'])
            continue
        # Normal entry — no content dedup (Bug 2 fix)
        shifted_offsets.append(pos >> params['shift'])
        final_data_blobs.append(block)

        pos += len(block)
        padding = (params['denominator'] - (pos % params['denominator'])) % params['denominator']
        pos += padding

        # Add padding to the block itself to keep the file pointer math simple
        final_data_blobs[-1] += (b'\x00' * padding)

    # Fix up alias offsets to point to their target's offset
    for alias_idx, target_idx in aliases.items():
        if alias_idx < len(shifted_offsets):
            shifted_offsets[alias_idx] = shifted_offsets[target_idx]

    # End marker: sentinel if original had sentinel, otherwise computed end position
    if last_is_sentinel:
        shifted_offsets.append(params['sentinel'])
    else:
        shifted_offsets.append(pos >> params['shift'])
    return shifted_offsets, final_data_blobs


def _pack_kods(output_path: Path, kods_header: int, params: dict, data_blocks: list[bytes], offsets: list[int], extended_raw: Optional[bytes], raw_tail: Optional[bytes], original_name: str, original_length: int = 0) -> int:
    out_file = output_path / original_name
    with open(out_file, 'wb') as f:
        f.write(b'Kods') # write header
        f.write(struct.pack('<I', kods_header))
        for offset in offsets: # write standard table
            f.write(struct.pack(params['format'], offset))

        if params['extended_table'] and extended_raw: # write extended table
            expected = params["num_offsets"] * params["alignment"]
            to_write = extended_raw[:expected] + b"\x00" * (expected - len(extended_raw))
            f.write(to_write[:expected])
            print(f"Preserved extended table from original ({expected} bytes)")

        f.write(b'\x00' * params['padding'])
        for block in data_blocks:
            f.write(block)

        if raw_tail: # Append tail after Kods archive
            f.write(raw_tail)
            print(f"Appended tail after data ({len(raw_tail)} bytes)")

        # Pad to original size if available, otherwise to sector boundary
        current = f.tell()
        if original_length and current < original_length:
            f.write(b'\x00' * (original_length - current))
        else:
            sector_size = 0x800
            sector_padding = (sector_size - (current % sector_size)) % sector_size
            if sector_padding:
                f.write(b'\x00' * sector_padding)

    print(f'Repacked .kods to {output_path}.\nStatistics; Number of offsets:{params["num_offsets"]} Entry type:{params["entry_type"]}')

    with open(out_file, 'rb') as f:
        return len(f.read())


def _report_sectors(original_length: int, packed_length: int) -> None:
    sector_size = 0x800

    original_sectors = (original_length + sector_size - 1) // sector_size
    packed_sectors = (packed_length + sector_size - 1) // sector_size
    sector_diff = original_sectors - packed_sectors
    # Information on whether will need to change the TOC and Kods efficiency
    if sector_diff > 0:
        print(f'SECTOR SIZE CHANGED! Saved {sector_diff} sectors ({sector_diff * 2 // 1024} bytes)')
    elif sector_diff < 0:
        print(f'SECTOR SIZE CHANGED! Grew by {abs(sector_diff)} sectors ({abs(sector_diff) * 2 // 1024})')
    else:
        print('Maintained sector size of original.')

###---------------------------- Unpack -----------------------------###

def start_kods_unpacking(kods_path: Path, output_path: Path) -> dict:
    # Check inputs
    with open(kods_path, 'rb') as f:
        raw_kods = f.read()
    if raw_kods[:4] != b'Kods':
        raise ValueError('Header mismatch')
    # Perform unpacking
    params = _parse_header(raw_kods)
    offsets = _get_offsets(raw_kods, params)
    stats = extract_kods(raw_kods, output_path, kods_path.stem, params, offsets)

    if params['runtime_flag']:
        stats["runtime_warning"] = "WARNING: Runtime flag set, offsets might be runtime RAM pointers."

    return stats


def _get_offsets(raw_kods: bytes, params: dict) -> list[int]:
    if params['extended_table']: # Get size of table
        table_size = params['table_base'] + (params['num_offsets'] * params['alignment'] * 2)
    else:
        table_size = params['table_base'] + (params['num_offsets'] * params['alignment'])

    params['table_end'] = table_size
    params['null_entries'] = set()
    params['null_count'] = 0
    offsets = []
    for index in range(params['num_offsets']): # Calculate payload offsets
        pos = params['table_base'] + (index * params['alignment'])
        raw_offset = struct.unpack_from(params['format'], raw_kods, pos)[0]
        if raw_offset == params['sentinel']: # Empty payload
            offsets.append(-1)
            params['null_entries'].add(index)
            params['null_count'] += 1
        elif raw_offset == 0: # Zero-offset entry (treated as empty per game code)
            offsets.append(table_size)  # resolves to data region start
            params['null_entries'].add(index)
        else:
            absolute_offset = table_size + (raw_offset << params['shift'])
            offsets.append(absolute_offset)
    return offsets


def extract_kods(raw_kods: bytes, output_path: Path, kods_name: str, params: dict, offsets: list[int]) -> dict:
    output_path.mkdir(parents=True, exist_ok=True)
    extracted = 0
    aliases_skipped = 0
    seen = {} # map (start, end) to first entry index
    aliases = {} # alias_index -> target_index (for round-trip)
    tail_info = None

    for i in range(len(offsets) - 1):
        start = offsets[i]
        if start == -1: continue # Skip sentinel entries
        # Find next valid end
        end = -1
        for j in range(i + 1, len(offsets)):
            if offsets[j] != -1:
                end = offsets[j]
                break

        # Bug 1 fix: when no forward end found, compute from file data
        if end == -1:
            data_end = len(raw_kods)
            # Strip trailing null bytes (sector padding)
            while data_end > start + 1 and raw_kods[data_end - 1] == 0:
                data_end -= 1
            if data_end > start:
                # Re-align to shift boundary relative to data region start
                denom = 1 << params['shift']
                table_end = params.get('table_end', start)
                relative = data_end - table_end
                aligned = relative + (denom - (relative % denom)) % denom
                end = table_end + aligned
                end = min(end, len(raw_kods))
            else:
                end = start  # empty

        # Bug 3 fix: write zero-length file for valid zero-length entries
        if start >= end:
            ext = '.bin'
            out_name = f'{kods_name}_{i:04X}{ext}'
            (output_path / out_name).write_bytes(b'')
            extracted += 1
            continue

        # Alias tracking: same (start, end) range = alias
        if (start, end) in seen:
            aliases[str(i)] = seen[(start, end)]
            aliases_skipped += 1
            continue

        # Save Entry
        segment = raw_kods[start:end]
        if segment.strip(b'\x00'):
            ext = '.bin'
            for magic, suffix in sorted(HEADERS.items(), key=lambda x: -len(x[0])):
                if segment.startswith(magic): # scan matching header largest to smallest
                    ext = suffix
                    break
            out_name = f'{kods_name}_{i:04X}{ext}'
            (output_path / out_name).write_bytes(segment)
            seen[(start, end)] = i
            extracted += 1

    # Save aliases map for round-trip repacking
    if aliases:
        aliases_path = output_path / '_aliases.json'
        with open(aliases_path, 'w') as f:
            json.dump(aliases, f)

    # Save any post Kods archive data as XXXX_tail.bin
    last_offset = offsets[-1]
    if last_offset > 0 and last_offset < len(raw_kods):
        tail = raw_kods[last_offset:]
        if tail.strip(b'\x00'):
            ext = '.bin'
            for magic, suffix in sorted(HEADERS.items(), key=lambda x: -len(x[0])):
                if tail.startswith(magic): # scan matching header largest to smallest
                    ext = suffix
                    break
            tail_path = output_path / f'{kods_name}_tail{ext}'
            tail_path.write_bytes(tail)
            tail_info = f'{tail_path.name} ({len(tail)} bytes)'

    return {
        "extracted": extracted,
        "aliases_skipped": aliases_skipped,
        "tail": tail_info,
    }

###------------------------ Utility ---------------------------###

def _parse_header(raw_kods: bytes) -> dict[str,Any]:
    header = struct.unpack_from('<I', raw_kods, 4)[0]
    entry_type = (header >> 20) & 0x3
    if entry_type not in (0,1):
        raise ValueError(f'Unsupported entry_type:{entry_type}')

    return {
            'num_offsets': header & 0xFFFF,
            'shift': (header >> 16) & 0xF,
            'entry_type': entry_type,
            'extended_table': (header >> 29) & 1,
            'external_payload': (header >> 30) & 1,
            'runtime_flag': (header >> 31) & 1,
            'alignment': 2 if entry_type else 4,
            'format': '<H' if entry_type else '<I',
            'sentinel': 0xFFFF if entry_type else 0xFFFFFFFF,
            'table_base': 8,
            }
