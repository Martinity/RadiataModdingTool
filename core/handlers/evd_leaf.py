'''
Handler for EVD script files, and everything needed to read and write them.

EVD is the game's cutscene bytecode. This module is the whole pipeline for it,
bottom to top, because it is all one concern -- turning EVD bytes into text a
person can edit and back again -- and splitting it across modules only hid the
lower half from the handler discovery that error-checks everything else.

Three sections, in the order they load:

  1. the format            opcode tables, the assembler and disassembler, the
                           EVDCODE/EVDSRC/EVDASM text forms. Generated, and
                           delimited by markers; hand edits there are lost.
  2. the API layer         what the handler and the editor actually call:
                           compile/decompile, the CodeLine model, the command
                           index and symbol tables, editing helpers.
  3. the handler           EVDHandler itself: decompile on the worker thread,
                           compile back on save.

Section 2 builds its tables at import, so section 1 has to come first. Search
for the banner comments to jump between them.
'''
from __future__ import annotations

import json
import math
import re
import struct
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Iterator, NamedTuple

from core import asset_symbols
from core.registry import Registry
from core.node import VfsNode
from core.contracts import LeafHandler
from core.workers import ActionDef, ActionType
from utilities import get_resource_path

import logging
logger = logging.getLogger(f'radiata.{__name__}')


###=========================================================================================###
###                                     1. THE FORMAT                                       ###
###=========================================================================================###
# BEGIN GENERATED -- do not edit by hand; see scripts/vendor_evd_tool.py.
#
# Derived from a 23443-line format tool, 486 top-level statements.
# Kept 357 of them; 118 were unreachable from this project.

EVD_MAGIC = b"EVD\x01"

ROTATE_OPTION_MODE_ACTIONS: dict[tuple[int, int], str] = {
    (0, 0): "rotate_target_vector",
    (0, 1): "rotate_target_character",
    (0, 2): "rotate_target_map_object",
    (0, 3): "rotate_target_stand_position_vector",
    (0, 4): "rotate_postprocess_only",
    (0, 5): "rotate_postprocess_only",
    (0, 6): "rotate_capture_state_3",
    (0, 7): "rotate_target_character_differ",
    (0, 8): "rotate_target_posture",
    (0, 9): "rotate_target_character",
    (0, 10): "rotate_target_map_object",
    (0, 11): "rotate_target_stand_position_vector",
    (0, 12): "rotate_postprocess_only",
    (0, 13): "rotate_postprocess_only",
    (0, 14): "rotate_postprocess_only",
    (0, 15): "rotate_target_current_character",
    (1, 0): "option_target_vector",
    (1, 1): "option_target_character",
    (1, 2): "option_target_map_object",
    (1, 8): "option_head_angle",
    (1, 15): "option_capture_reset",
}

AXES = ("x", "y", "z")

CHARACTER_VARIANT_SPECS = (("character_number", 0, 0xFFFF), ("character_variant", 16, 0xFF))

CHARACTER_TYPE_SPECS = (("character_number", 0, 0xFFFF), ("character_type", 16, 0xFF))

AUTO_RATE_CONTROL_SPECS = (
    ("color_rate", 1, 0x01),
    ("color_flag2", 2, 0x01),
    ("color_mode", 3, 0x03),
    ("transparent", 6, 0x01),
    ("transparent_flag7", 7, 0x01),
    ("transparent_mode", 8, 0x03),
    ("scale", 11, 0x01),
    ("scale_flag4", 12, 0x01),
    ("palette", 14, 0x01),
    ("palette_flag7", 15, 0x01),
    ("visibility", 17, 0x01),
    ("visibility_flag2", 18, 0x01),
)

CONDITION_SPECS = (("cond_base", 0, 0x7F), ("invert", 7, 0x01))

PARENT_WORD_SPECS = (
    ("parent_character", 0, 0xFFFF),
    ("parent_variant", 16, 0xFF),
    ("parent_raw_high", 24, 0xFF),
)

EXPR_OPERATION_NAMES = {
    0: "copy",
    1: "add",
    2: "sub",
    3: "mul",
    4: "div",
    5: "mod",
    6: "and",
    7: "or",
}

TEXT_OUTPUT_MODE_NAMES = {
    0: "sjis_text",
    1: "event_value_number",
    7: "clear_text",
}

EYE_MOVE_SELECTOR_ACTIONS = {
    0: "set_type_0",
    1: "set_type_1",
    2: "set_type_2",
    3: "manual_vector",
    4: "set_type_3",
    5: "set_type_4",
}

def masked_vector_fields(mask: int, payload: list[int]) -> tuple[list[str], int]:
    fields = []
    cursor = 0
    for axis_index, axis in enumerate(AXES):
        if mask & (1 << axis_index):
            if cursor >= len(payload):
                break
            fields.append(f"{axis}:{format_f32(u32_to_f32(payload[cursor]))}")
            cursor += 1
    return fields, cursor

def parse_axis_float_words(text: str, line_no: int, field_name: str) -> dict[str, int]:
    values: dict[str, int] = {}
    if not text:
        return values
    for item in text.split(","):
        if ":" not in item:
            raise ValueError(f"line {line_no}: {field_name} item {item!r} must be axis:value")
        axis, value = item.split(":", 1)
        if axis not in AXES:
            raise ValueError(f"line {line_no}: {field_name} axis {axis!r} must be x, y, or z")
        if axis in values:
            raise ValueError(f"line {line_no}: duplicate {field_name} axis {axis}")
        values[axis] = f32_to_u32(float(value))
    return values

def append_masked_axis_words(words: list[int], mask: int, fields: dict[str, str], field_name: str, line_no: int) -> None:
    axis_words = parse_axis_float_words(fields.get(field_name, ""), line_no, field_name)
    for axis_index, axis in enumerate(AXES):
        if mask & (1 << axis_index):
            if axis not in axis_words:
                raise ValueError(f"line {line_no}: {field_name} missing {axis}")
            words.append(axis_words[axis])
        elif axis in axis_words:
            raise ValueError(f"line {line_no}: {field_name} includes {axis} but mask bit is clear")

def parse_vec3_words(text: str, line_no: int, field_name: str) -> list[int]:
    values = text.split(",") if text else []
    if len(values) != 3:
        raise ValueError(f"line {line_no}: {field_name} expects three comma-separated floats")
    return [f32_to_u32(float(value)) for value in values]

def parse_float_words(text: str, count: int, line_no: int, field_name: str) -> list[int]:
    values = text.split(",") if text else []
    if len(values) != count:
        raise ValueError(f"line {line_no}: {field_name} expects {count} comma-separated floats")
    return [f32_to_u32(float(value)) for value in values]

def format_vec3_words(words: list[int]) -> str:
    return ",".join(format_f32(u32_to_f32(word)) for word in words)

def format_control_bit_names(bits: list[str]) -> str:
    return ",".join(bits) if bits else "none"

def decode_character_data_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    mode = arg >> 6
    cursor = 0
    fields = [f"explicit_char={explicit_char}", f"mode={mode}"]
    if explicit_char:
        if cursor >= len(words):
            return fields, False
        fields.append(f"character=0x{words[cursor]:08X}")
        cursor += 1
    if cursor >= len(words) or mode not in (0, 1, 2):
        return fields, False

    control = words[cursor]
    cursor += 1
    # The control word is a presence bitmask: each modeled bit is implied by
    # its payload field being present. Only print it when it carries bits
    # outside the model.
    modeled_mask = 0x7FF if mode == 2 else 0x3FF
    if control & ~modeled_mask:
        fields.append(f"control=0x{control:08X}")

    if control & 0x01:
        if cursor >= len(words):
            return fields, False
        word = words[cursor]
        cursor += 1
        fields.append(f"modeling_word=0x{word:08X}")
    if control & 0x02:
        if cursor >= len(words):
            return fields, False
        word = words[cursor]
        cursor += 1
        fields.append(f"action_word=0x{word:08X}")

    animation_fields: list[str] = []
    for slot in range(8):
        if control & (1 << (slot + 2)):
            if cursor >= len(words):
                return fields, False
            word = words[cursor]
            cursor += 1
            animation_fields.append(f"{slot}:0x{word:08X}")
    if animation_fields:
        fields.append(f"animation_words={'|'.join(animation_fields)}")

    if mode == 2 and control & 0x400:
        if cursor >= len(words):
            return fields, False
        word = words[cursor]
        cursor += 1
        fields.append(f"algorithm_word=0x{word:08X}")

    return fields, cursor == len(words)

def parse_character_data_animation_words(text: str, line_no: int) -> dict[int, int]:
    values: dict[int, int] = {}
    if not text:
        return values
    for item in text.split("|"):
        if ":" not in item:
            raise ValueError(f"line {line_no}: character_data animation_words item {item!r} must be slot:word")
        slot_text, word_text = item.split(":", 1)
        slot = parse_hex_int(slot_text)
        if not 0 <= slot < 8:
            raise ValueError(f"line {line_no}: character_data animation slot {slot} out of range")
        if slot in values:
            raise ValueError(f"line {line_no}: duplicate character_data animation slot {slot}")
        values[slot] = parse_hex_int(word_text) & 0xFFFFFFFF
    return values

def build_character_data_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"explicit_char"}, line_no, "character_data")
    explicit_char = parse_hex_int(fields["explicit_char"])
    if explicit_char not in (0, 1):
        raise ValueError(f"line {line_no}: character_data explicit_char must be 0 or 1")
    if "words" in fields and "control" not in fields:
        arg = parse_hex_int(fields.get("arg", str(explicit_char)))
        if (arg & 0x01) != explicit_char:
            raise ValueError(f"line {line_no}: character_data arg does not match explicit_char")
        return arg, parse_optional_word_list(fields)

    require_fields(fields, {"mode"}, line_no, "character_data")
    mode = parse_hex_int(fields["mode"])
    if mode not in (0, 1, 2):
        raise ValueError(f"line {line_no}: character_data mode must be 0, 1, or 2")
    default_arg = explicit_char | (mode << 6)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if (arg & 0x01) != explicit_char or (arg >> 6) != mode:
        raise ValueError(f"line {line_no}: character_data arg does not match explicit_char/mode")

    animation_words = parse_character_data_animation_words(fields.get("animation_words", ""), line_no)
    if "control" in fields:
        control = parse_hex_int(fields["control"]) & 0xFFFFFFFF
    else:
        # Derive the presence bitmask from which payload fields are present.
        control = 0
        if "modeling_word" in fields:
            control |= 0x01
        if "action_word" in fields:
            control |= 0x02
        for slot in animation_words:
            control |= 1 << (slot + 2)
        if mode == 2 and "algorithm_word" in fields:
            control |= 0x400
    words: list[int] = []
    if explicit_char:
        pass  # packed word is resolved from its named parts below
        words.append(resolve_packed_word(fields, line_no, "character_data", "character", CHARACTER_VARIANT_SPECS))
    words.append(control)

    if control & 0x01:
        require_fields(fields, {"modeling_word"}, line_no, "character_data")
        words.append(parse_hex_int(fields["modeling_word"]) & 0xFFFFFFFF)
    elif "modeling_word" in fields:
        raise ValueError(f"line {line_no}: character_data modeling_word present but control bit 0 is clear")
    if control & 0x02:
        require_fields(fields, {"action_word"}, line_no, "character_data")
        words.append(parse_hex_int(fields["action_word"]) & 0xFFFFFFFF)
    elif "action_word" in fields:
        raise ValueError(f"line {line_no}: character_data action_word present but control bit 1 is clear")

    for slot in range(8):
        bit = 1 << (slot + 2)
        if control & bit:
            if slot not in animation_words:
                raise ValueError(f"line {line_no}: character_data missing animation_words slot {slot}")
            words.append(animation_words[slot])
        elif slot in animation_words:
            raise ValueError(f"line {line_no}: character_data animation slot {slot} present but control bit is clear")

    if mode == 2 and control & 0x400:
        require_fields(fields, {"algorithm_word"}, line_no, "character_data")
        words.append(parse_hex_int(fields["algorithm_word"]) & 0xFFFFFFFF)
    elif "algorithm_word" in fields:
        raise ValueError(f"line {line_no}: character_data algorithm_word present but mode/control do not consume it")
    return arg, words

def decode_character_delete_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    flag7 = (arg >> 7) & 0x01
    cursor = 0
    fields = [f"explicit_char={explicit_char}", f"flag7={flag7}"]
    if explicit_char:
        if cursor >= len(words):
            return fields, False
        fields.append(f"character=0x{words[cursor]:08X}")
        cursor += 1
    if cursor >= len(words):
        return fields, False
    control = words[cursor]
    cursor += 1
    # control = delete bit | detach mask << 1; fully derivable from the
    # named fields, so the packed word is never printed.
    if control & 0x01:
        fields.append("delete=1")
    if control >> 1:
        fields.append(f"detach_data_mask=0x{control >> 1:08X}")
    return fields, cursor == len(words)

def build_character_delete_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"explicit_char"}, line_no, "character_delete_data")
    explicit_char = parse_hex_int(fields["explicit_char"])
    if "words" in fields and "control" not in fields:
        # Truncated decode: only explicit_char and the verbatim tail are present.
        arg = parse_hex_int(fields.get("arg", str(explicit_char)))
        if (arg & 0x01) != explicit_char:
            raise ValueError(f"line {line_no}: character_delete_data arg does not match explicit_char")
        return arg, parse_optional_word_list(fields)
    require_fields(fields, {"flag7"}, line_no, "character_delete_data")
    flag7 = parse_hex_int(fields["flag7"])
    if explicit_char not in (0, 1) or flag7 not in (0, 1):
        raise ValueError(f"line {line_no}: character_delete_data explicit_char/flag7 must be 0 or 1")
    default_arg = explicit_char | (flag7 << 7)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if (arg & 0x81) != default_arg:
        raise ValueError(f"line {line_no}: character_delete_data arg does not match explicit_char/flag7")

    if "control" in fields:
        control = parse_hex_int(fields["control"]) & 0xFFFFFFFF
    else:
        control = (parse_hex_int(fields.get("delete", "0")) & 0x01) | (
            parse_hex_int(fields.get("detach_data_mask", "0")) << 1
        )
        if control >> 32:
            raise ValueError(f"line {line_no}: character_delete_data detach_data_mask out of range")
    if "delete" in fields and parse_hex_int(fields["delete"]) != (control & 0x01):
        raise ValueError(f"line {line_no}: character_delete_data delete does not match control")
    if "detach_data_mask" in fields and parse_hex_int(fields["detach_data_mask"]) != (control >> 1):
        raise ValueError(f"line {line_no}: character_delete_data detach_data_mask does not match control")
    words: list[int] = []
    if explicit_char:
        require_fields(fields, {"character"}, line_no, "character_delete_data")
        words.append(parse_hex_int(fields["character"]) & 0xFFFFFFFF)
    elif "character" in fields:
        raise ValueError(f"line {line_no}: character_delete_data character present but explicit_char is clear")
    words.append(control)
    return arg, words

def decode_character_event_leave_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    count = arg & 0x1F
    post_mode = (arg >> 5) & 0x03
    release = (arg >> 7) & 0x01
    fields = [
        f"mode=0x{arg:02X}",
        f"count={count}",
        f"release={release}",
        f"post_mode={post_mode}",
    ]
    if count != len(words):
        return fields, False
    if release and count == 0:
        fields.append("action=enter_all")
        return fields, True
    fields.append(f"action={'release_enter_character' if release else 'add_enter_character'}")
    if words:
        fields.append("character_pairs=" + words_to_csv(words))
        fields.append(
            "characters="
            + "|".join(
                f"0x{word & 0xFFFF:04X}:0x{(word >> 16) & 0xFFFF:04X}"
                for word in words
            )
        )
    return fields, True

def build_character_event_leave_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    if "mode" in fields:
        arg = parse_hex_int(fields["mode"])
    else:
        require_fields(fields, {"count", "release", "post_mode"}, line_no, "character_event_leave")
        count = parse_hex_int(fields["count"])
        release = parse_hex_int(fields["release"])
        post_mode = parse_hex_int(fields["post_mode"])
        if not 0 <= count <= 0x1F or release not in (0, 1) or not 0 <= post_mode <= 0x03:
            raise ValueError(f"line {line_no}: character_event_leave count/release/post_mode out of range")
        arg = count | (post_mode << 5) | (release << 7)
    if not 0 <= arg <= 0xFF:
        raise ValueError(f"line {line_no}: character_event_leave mode out of range")
    count = arg & 0x1F
    if "words" in fields:
        words = parse_optional_word_list(fields)
    else:
        text = fields.get("character_pairs", "")
        words = parse_word_list(text) if text else []
    if len(words) != count and "words" not in fields:
        # A raw words= list (data regions, truncated commands) keeps its real
        # length; the header word count comes from the list, not the arg field.
        raise ValueError(f"line {line_no}: character_event_leave count {count} does not match {len(words)} character pair word(s)")
    if "release" in fields and parse_hex_int(fields["release"]) != ((arg >> 7) & 0x01):
        raise ValueError(f"line {line_no}: character_event_leave release does not match mode")
    if "post_mode" in fields and parse_hex_int(fields["post_mode"]) != ((arg >> 5) & 0x03):
        raise ValueError(f"line {line_no}: character_event_leave post_mode does not match mode")
    if "count" in fields and parse_hex_int(fields["count"]) != count:
        raise ValueError(f"line {line_no}: character_event_leave count does not match mode")
    return arg, words

def decode_character_collision_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    fields, complete = decode_character_collision_fields_inner(arg, words)
    if not complete and not any(field.startswith("control=") for field in fields):
        # Truncated lines fall back to raw words=; keep the control word
        # visible so the raw escape rebuilds the exact payload.
        index = (arg & 0x01) + 1
        if index <= len(words):
            fields.insert(min(index, len(fields)), f"control=0x{words[index - 1]:08X}")
    return fields, complete

def decode_character_collision_fields_inner(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    cursor = 0
    fields = [f"explicit_char={explicit_char}"]
    if explicit_char:
        if cursor >= len(words):
            return fields, False
        fields.append(f"character=0x{words[cursor]:08X}")
        cursor += 1
    if cursor >= len(words):
        return fields, False
    control = words[cursor]
    cursor += 1
    control_names: list[str] = []
    for bit, name in (
        (0x001, "float_array"),
        (0x002, "float_80"),
        (0x004, "float_84"),
        (0x008, "float_88"),
        (0x010, "float_8c"),
        (0x020, "float_0c"),
        (0x040, "float_10"),
        (0x080, "dynamic_halfword"),
        (0x100, "skip_word"),
        (0x200, "halfword_triplet"),
        (0x1000, "collision_scale"),
    ):
        if control & bit:
            control_names.append(name)
    # The control word is a presence bitmask plus the 2-bit byte0b mode at
    # bits 10-11; print it only when it carries bits outside the model.
    if control & ~0x1FFF:
        fields.append(f"control=0x{control:08X}")
        fields.append(f"control_bits={format_control_bit_names(control_names)}")

    if control & 0x001:
        if cursor >= len(words):
            return fields, False
        array_word = words[cursor]
        cursor += 1
        count = array_word & 0x07
        fields.append(f"float_array_word=0x{array_word:08X}")
        fields.append(f"float_array_count={count}")
        if cursor + count > len(words):
            return fields, False
        if count:
            fields.append("float_array_values=" + ",".join(format_f32(u32_to_f32(word)) for word in words[cursor : cursor + count]))
            cursor += count
    for bit, name in (
        (0x002, "float_80"),
        (0x004, "float_84"),
        (0x008, "float_88"),
        (0x010, "float_8c"),
        (0x020, "float_0c"),
        (0x040, "float_10"),
    ):
        if control & bit:
            if cursor >= len(words):
                return fields, False
            fields.append(f"{name}={format_f32(u32_to_f32(words[cursor]))}")
            cursor += 1
    if control & 0x080:
        if cursor >= len(words):
            return fields, False
        fields.append(f"dynamic_halfword_word=0x{words[cursor]:08X}")
        cursor += 1
    if control & 0x100:
        if cursor >= len(words):
            return fields, False
        fields.append(f"skip_word=0x{words[cursor]:08X}")
        cursor += 1
    if control & 0x200:
        if cursor + 3 > len(words):
            return fields, False
        fields.append("halfword_triplet_words=" + words_to_csv(words[cursor : cursor + 3]))
        cursor += 3
    # Control bits 10-11 select a three-way action on one flag in the character's
    # collision record (Command_31 at 0x002EF768): 1 clears it, 2 sets it, and
    # leaving the bits at 0 leaves the flag as it was. No mapped overlay reads the
    # flag back, so the name says where it is written, not what it later does.
    collision_flag_mode = (control >> 10) & 0x03
    if collision_flag_mode in (1, 2):
        fields.append(f"collision_flag={'off' if collision_flag_mode == 1 else 'on'}")
    elif collision_flag_mode:
        fields.append(f"collision_flag_mode={collision_flag_mode}")
    if control & 0x1000:
        if cursor + 2 > len(words):
            return fields, False
        fields.append("collision_scale_words=" + words_to_csv(words[cursor : cursor + 2]))
        lo = sign_extend(words[cursor] & 0xFFFF, 16) / 100.0
        hi = sign_extend((words[cursor] >> 16) & 0xFFFF, 16) / 100.0
        third = sign_extend(words[cursor + 1] & 0xFFFF, 16) / 100.0
        fields.append(f"collision_scale={format_f32(lo)},{format_f32(hi)},{format_f32(third)}")
        cursor += 2
    return fields, cursor == len(words)

def build_character_collision_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"explicit_char"}, line_no, "character_collision_setup")
    explicit_char = parse_hex_int(fields["explicit_char"])
    if explicit_char not in (0, 1):
        raise ValueError(f"line {line_no}: character_collision_setup explicit_char must be 0 or 1")
    if "words" in fields and "control" not in fields:
        arg = parse_hex_int(fields.get("arg", str(explicit_char)))
        if (arg & 0x01) != explicit_char:
            raise ValueError(f"line {line_no}: character_collision_setup arg does not match explicit_char")
        return arg, parse_optional_word_list(fields)

    arg = parse_hex_int(fields.get("arg", str(explicit_char)))
    if (arg & 0x01) != explicit_char:
        raise ValueError(f"line {line_no}: character_collision_setup arg does not match explicit_char")
    if "control" in fields:
        control = parse_hex_int(fields["control"]) & 0xFFFFFFFF
    else:
        # Derive the presence bitmask from which payload fields are present.
        control = 0
        for key, bit in (
            ("float_array_word", 0x001),
            ("float_80", 0x002),
            ("float_84", 0x004),
            ("float_88", 0x008),
            ("float_8c", 0x010),
            ("float_0c", 0x020),
            ("float_10", 0x040),
            ("dynamic_halfword_word", 0x080),
            ("skip_word", 0x100),
            ("halfword_triplet_words", 0x200),
            ("collision_scale_words", 0x1000),
        ):
            if key in fields:
                control |= bit
        if "collision_flag" in fields:
            flag_text = fields["collision_flag"].strip().lower()
            if flag_text not in ("on", "off"):
                raise ValueError(
                    f"line {line_no}: character_collision_setup collision_flag must be on or off"
                )
            flag_mode = 2 if flag_text == "on" else 1
        else:
            flag_mode = parse_hex_int(
                fields.get("collision_flag_mode", fields.get("byte0b_bit6_mode", "0"))
            )
        control |= (flag_mode & 0x03) << 10
    words: list[int] = []
    if explicit_char:
        require_fields(fields, {"character"}, line_no, "character_collision_setup")
        words.append(parse_hex_int(fields["character"]) & 0xFFFFFFFF)
    words.append(control)

    if control & 0x001:
        require_fields(fields, {"float_array_word"}, line_no, "character_collision_setup")
        array_word = parse_hex_int(fields["float_array_word"]) & 0xFFFFFFFF
        count = array_word & 0x07
        words.append(array_word)
        values_text = fields.get("float_array_values", "")
        values = parse_float_words(values_text, count, line_no, "float_array_values") if count else []
        words.extend(values)
    elif "float_array_word" in fields or "float_array_values" in fields:
        raise ValueError(f"line {line_no}: character_collision_setup float array fields present but control bit 0 is clear")

    for bit, name in (
        (0x002, "float_80"),
        (0x004, "float_84"),
        (0x008, "float_88"),
        (0x010, "float_8c"),
        (0x020, "float_0c"),
        (0x040, "float_10"),
    ):
        if control & bit:
            require_fields(fields, {name}, line_no, "character_collision_setup")
            words.append(f32_to_u32(float(fields[name])))
        elif name in fields:
            raise ValueError(f"line {line_no}: character_collision_setup {name} present but control bit is clear")
    if control & 0x080:
        require_fields(fields, {"dynamic_halfword_word"}, line_no, "character_collision_setup")
        words.append(parse_hex_int(fields["dynamic_halfword_word"]) & 0xFFFFFFFF)
    elif "dynamic_halfword_word" in fields:
        raise ValueError(f"line {line_no}: character_collision_setup dynamic_halfword_word present but control bit is clear")
    if control & 0x100:
        require_fields(fields, {"skip_word"}, line_no, "character_collision_setup")
        words.append(parse_hex_int(fields["skip_word"]) & 0xFFFFFFFF)
    elif "skip_word" in fields:
        raise ValueError(f"line {line_no}: character_collision_setup skip_word present but control bit is clear")
    if control & 0x200:
        require_fields(fields, {"halfword_triplet_words"}, line_no, "character_collision_setup")
        triplet = parse_word_list(fields["halfword_triplet_words"])
        if len(triplet) != 3:
            raise ValueError(f"line {line_no}: character_collision_setup halfword_triplet_words expects three words")
        words.extend(triplet)
    elif "halfword_triplet_words" in fields:
        raise ValueError(f"line {line_no}: character_collision_setup halfword_triplet_words present but control bit is clear")
    if control & 0x1000:
        require_fields(fields, {"collision_scale_words"}, line_no, "character_collision_setup")
        scale_words = parse_word_list(fields["collision_scale_words"])
        if len(scale_words) != 2:
            raise ValueError(f"line {line_no}: character_collision_setup collision_scale_words expects two words")
        words.extend(scale_words)
    elif "collision_scale_words" in fields:
        raise ValueError(f"line {line_no}: character_collision_setup collision_scale_words present but control bit is clear")
    return arg, words

def decode_character_anim_signal_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    name_source = (arg >> 1) & 0x03
    result_op = (arg >> 2) & 0x07
    cursor = 0
    fields = [f"explicit_char={explicit_char}", f"name_source={name_source}", f"result_op={result_op}"]
    if explicit_char:
        if cursor >= len(words):
            return fields, False
        fields.append(f"character=0x{words[cursor]:08X}")
        cursor += 1
    if cursor >= len(words):
        return fields, False
    control = words[cursor]
    cursor += 1
    fields.extend(
        [
            f"event_value=0x{control & 0xFFFF:04X}",
            f"signal_selector=0x{(control >> 16) & 0xFF:02X}",
            f"signal_raw_byte3=0x{(control >> 24) & 0xFF:02X}",
        ]
    )
    if name_source == 1:
        if cursor + 4 > len(words):
            return fields, False
        name_words = words[cursor:cursor + 4]
        cursor += 4
        text = words_to_sjis_text(name_words)
        if text is not None:
            fields.append(f"signal_name={json.dumps(text, ensure_ascii=False)}")
        else:
            fields.append(fixed_name_field("signal_name_words", name_words))
    if cursor != len(words):
        fields.append(f"trailing={words_to_csv(words[cursor:])}")
        return fields, False
    return fields, True

def build_character_anim_signal_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"explicit_char"}, line_no, "character_anim_signal")
    explicit_char = parse_hex_int(fields["explicit_char"])
    name_source = parse_hex_int(fields.get("name_source", "0"))
    result_op = parse_hex_int(fields.get("result_op", "0"))
    arg = parse_hex_int(fields.get("arg", str(explicit_char | (name_source << 1) | (result_op << 2))))
    if "words" in fields:
        # Truncated decode: the words= tail carries the full raw payload.
        return arg, parse_optional_word_list(fields)
    if explicit_char not in (0, 1) or not 0 <= name_source <= 3 or not 0 <= result_op <= 7:
        raise ValueError(f"line {line_no}: character_anim_signal fields out of range")
    if (arg & 0x01) != explicit_char or ((arg >> 1) & 0x03) != name_source or ((arg >> 2) & 0x07) != result_op:
        raise ValueError(f"line {line_no}: character_anim_signal arg does not match explicit_char/name_source/result_op")
    if "words" in fields and not any(name in fields for name in ("event_value", "signal_name", "signal_name_words")):
        return arg, parse_optional_word_list(fields)

    words: list[int] = []
    if explicit_char:
        require_fields(fields, {"character"}, line_no, "character_anim_signal")
        words.append(parse_hex_int(fields["character"]) & 0xFFFFFFFF)
    require_fields(fields, {"event_value", "signal_selector"}, line_no, "character_anim_signal")
    event_value = parse_hex_int(fields["event_value"])
    signal_selector = parse_hex_int(fields["signal_selector"])
    raw_byte3 = parse_hex_int(fields.get("signal_raw_byte3", "0"))
    if not 0 <= event_value <= 0xFFFF or not 0 <= signal_selector <= 0xFF or not 0 <= raw_byte3 <= 0xFF:
        raise ValueError(f"line {line_no}: character_anim_signal control fields out of range")
    words.append(event_value | (signal_selector << 16) | (raw_byte3 << 24))
    if name_source == 1:
        if "signal_name" in fields:
            words.extend(fixed_name_words_from_text(fields, "signal_name", line_no))
        else:
            if "signal_name" not in fields:
                require_fields(fields, {"signal_name_words"}, line_no, "character_anim_signal")
            name_words = (resolve_fixed_name_words(fields, "signal_name_words", line_no) or [])
            if len(name_words) != 4:
                raise ValueError(f"line {line_no}: character_anim_signal signal_name_words expects four words")
            words.extend(name_words)
    if "trailing" in fields:
        words.extend(parse_word_list(fields["trailing"]))
    return arg, words

def decode_character_equipment_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    item_branch = (arg >> 1) & 0x01
    display_mode = arg >> 6
    cursor = 0
    fields = [
        f"explicit_char={explicit_char}",
        f"item_branch={item_branch}",
        f"display_mode={display_mode}",
    ]
    if explicit_char:
        if cursor >= len(words):
            return fields, False
        fields.append(f"character=0x{words[cursor]:08X}")
        cursor += 1
    if cursor >= len(words):
        return fields, False
    item_control = words[cursor]
    cursor += 1
    fields.append(f"item=0x{item_control & 0xFFFF:04X}")
    if (item_control >> 16) & 0xFF:
        fields.append(f"item_high_byte=0x{(item_control >> 16) & 0xFF:02X}")
    if (item_control >> 24) & 0xFF:
        fields.append(f"display_arg=0x{(item_control >> 24) & 0xFF:02X}")
    return fields, cursor == len(words)

def build_character_equipment_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"explicit_char"}, line_no, "character_equipment")
    explicit_char = parse_hex_int(fields["explicit_char"])
    if explicit_char not in (0, 1):
        raise ValueError(f"line {line_no}: character_equipment explicit_char must be 0 or 1")
    if "words" in fields and "item_control" not in fields:
        item_branch = parse_hex_int(fields.get("flag1", fields.get("item_branch", "0")))
        flag2 = parse_hex_int(fields.get("flag2", "0"))
        flag3 = parse_hex_int(fields.get("flag3", "0"))
        flag4 = parse_hex_int(fields.get("flag4", "0"))
        default_arg = explicit_char | (item_branch << 1) | (flag2 << 2) | (flag3 << 3) | (flag4 << 4)
        arg = parse_hex_int(fields.get("arg", str(default_arg)))
        if (arg & 0x01) != explicit_char:
            raise ValueError(f"line {line_no}: character_equipment arg does not match explicit_char")
        return arg, parse_optional_word_list(fields)

    require_fields(fields, {"item_branch", "display_mode"}, line_no, "character_equipment")
    item_branch = parse_hex_int(fields["item_branch"])
    display_mode = parse_hex_int(fields["display_mode"])
    if item_branch not in (0, 1) or not 0 <= display_mode <= 3:
        raise ValueError(f"line {line_no}: character_equipment item_branch/display_mode out of range")
    default_arg = explicit_char | (item_branch << 1) | (display_mode << 6)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if (arg & 0xC3) != default_arg:
        raise ValueError(f"line {line_no}: character_equipment arg does not match explicit_char/item_branch/display_mode")
    words: list[int] = []
    if explicit_char:
        pass  # packed word is resolved from its named parts below
        words.append(resolve_packed_word(fields, line_no, "character_equipment", "character", CHARACTER_VARIANT_SPECS))
    if "item_control" in fields:
        item_control = parse_hex_int(fields["item_control"]) & 0xFFFFFFFF
    else:
        require_fields(fields, {"item"}, line_no, "character_equipment")
        item_control = (
            (parse_hex_int(fields["item"]) & 0xFFFF)
            | ((parse_hex_int(fields.get("item_high_byte", "0")) & 0xFF) << 16)
            | ((parse_hex_int(fields.get("display_arg", "0")) & 0xFF) << 24)
        )
    if "item" in fields and parse_hex_int(fields["item"]) != (item_control & 0xFFFF):
        raise ValueError(f"line {line_no}: character_equipment item does not match item_control")
    if "item_high_byte" in fields and parse_hex_int(fields["item_high_byte"]) != ((item_control >> 16) & 0xFF):
        raise ValueError(f"line {line_no}: character_equipment item_high_byte does not match item_control")
    if "display_arg" in fields and parse_hex_int(fields["display_arg"]) != ((item_control >> 24) & 0xFF):
        raise ValueError(f"line {line_no}: character_equipment display_arg does not match item_control")
    words.append(item_control)
    return arg, words

def decode_special_effect_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char0 = arg & 0x01
    explicit_char1 = (arg >> 1) & 0x01
    abort = (arg >> 7) & 0x01
    cursor = 0
    fields = [
        f"explicit_char0={explicit_char0}",
        f"explicit_char1={explicit_char1}",
        f"abort={abort}",
    ]
    if cursor >= len(words):
        return fields, False
    effect_word = words[cursor]
    cursor += 1
    fields.extend(
        [
            f"effect_word=0x{effect_word:08X}",
            f"effect_id=0x{effect_word & 0xFFFF:04X}",
            f"effect_flags=0x{(effect_word >> 16) & 0xFFFF:04X}",
        ]
    )
    if explicit_char0:
        if cursor >= len(words):
            return fields, False
        fields.append(f"character0=0x{words[cursor]:08X}")
        cursor += 1
    if explicit_char1:
        if cursor >= len(words):
            return fields, False
        fields.append(f"character1=0x{words[cursor]:08X}")
        cursor += 1
    if not abort:
        if cursor >= len(words):
            return fields, False
        fields.append(f"execute_word=0x{words[cursor]:08X}")
        cursor += 1
    return fields, cursor == len(words)

def build_special_effect_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"explicit_char0", "explicit_char1"}, line_no, "special_effect")
    explicit_char0 = parse_hex_int(fields["explicit_char0"])
    explicit_char1 = parse_hex_int(fields["explicit_char1"])
    if explicit_char0 not in (0, 1) or explicit_char1 not in (0, 1):
        raise ValueError(f"line {line_no}: special_effect explicit flags must be 0 or 1")
    if "words" in fields:
        # Truncated decode: the words= tail carries the full raw payload and
        # any named payload fields on the line are informational duplicates.
        abort = parse_hex_int(fields.get("abort", "0"))
        default_arg = explicit_char0 | (explicit_char1 << 1) | (abort << 7)
        arg = parse_hex_int(fields.get("arg", str(default_arg)))
        if (arg & 0x83) != default_arg:
            raise ValueError(f"line {line_no}: special_effect arg does not match explicit flags")
        return arg, parse_optional_word_list(fields)

    require_fields(fields, {"abort", "effect_word"}, line_no, "special_effect")
    abort = parse_hex_int(fields["abort"])
    if abort not in (0, 1):
        raise ValueError(f"line {line_no}: special_effect abort must be 0 or 1")
    default_arg = explicit_char0 | (explicit_char1 << 1) | (abort << 7)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if (arg & 0x83) != default_arg:
        raise ValueError(f"line {line_no}: special_effect arg does not match explicit flags/abort")
    words = [parse_hex_int(fields["effect_word"]) & 0xFFFFFFFF]
    if "effect_id" in fields and parse_hex_int(fields["effect_id"]) != (words[0] & 0xFFFF):
        raise ValueError(f"line {line_no}: special_effect effect_id does not match effect_word")
    if "effect_flags" in fields and parse_hex_int(fields["effect_flags"]) != ((words[0] >> 16) & 0xFFFF):
        raise ValueError(f"line {line_no}: special_effect effect_flags does not match effect_word")
    if explicit_char0:
        require_fields(fields, {"character0"}, line_no, "special_effect")
        words.append(parse_hex_int(fields["character0"]) & 0xFFFFFFFF)
    elif "character0" in fields:
        raise ValueError(f"line {line_no}: special_effect character0 present but explicit_char0 is clear")
    if explicit_char1:
        require_fields(fields, {"character1"}, line_no, "special_effect")
        words.append(parse_hex_int(fields["character1"]) & 0xFFFFFFFF)
    elif "character1" in fields:
        raise ValueError(f"line {line_no}: special_effect character1 present but explicit_char1 is clear")
    if not abort:
        require_fields(fields, {"execute_word"}, line_no, "special_effect")
        words.append(parse_hex_int(fields["execute_word"]) & 0xFFFFFFFF)
    elif "execute_word" in fields:
        raise ValueError(f"line {line_no}: special_effect execute_word present on abort path")
    return arg, words

def decode_background_visibility_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    mode = (arg >> 6) & 0x03
    flag0 = arg & 0x01
    flag1 = (arg >> 1) & 0x01
    shadow = (arg >> 4) & 0x03
    fields = [
        f"mode={mode}",
        f"flag0={flag0}",
        f"flag1={flag1}",
        f"shadow={shadow}",
    ]
    if mode == 1:
        # The handler takes the name as a char* and reads to its NUL, so the
        # payload length is whatever the command header declares. Shipped scripts
        # always use four words, but shorter payloads are valid.
        if not words:
            return fields, False
        # Every shipped script uses the four-word slot, which is the only length
        # the text spelling can be rebuilt at; longer payloads stay as words.
        fields.append(
            fixed_name_field("name_words", words)
            if len(words) == 4
            else "name_words=" + words_to_csv(words)
        )
        return fields, True
    if words:
        return fields, False
    return fields, True

def build_background_visibility_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"mode", "flag0"}, line_no, "background_visibility")
    mode = parse_hex_int(fields["mode"])
    flag0 = parse_hex_int(fields["flag0"])
    flag1 = parse_hex_int(fields.get("flag1", "0"))
    shadow = parse_hex_int(fields.get("shadow", "0"))
    if not 0 <= mode <= 0x03 or flag0 not in (0, 1) or flag1 not in (0, 1) or not 0 <= shadow <= 0x03:
        raise ValueError(f"line {line_no}: background_visibility mode/flags out of range")
    default_arg = flag0 | (flag1 << 1) | (shadow << 4) | (mode << 6)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if (arg & 0xF3) != default_arg:
        raise ValueError(f"line {line_no}: background_visibility arg does not match mode/flag0/flag1/shadow")
    if "words" in fields:
        return arg, parse_optional_word_list(fields)
    if mode == 1:
        if "name" not in fields:
            require_fields(fields, {"name_words"}, line_no, "background_visibility")
        name_words = (resolve_fixed_name_words(fields, "name_words", line_no) or [])
        if not name_words:
            raise ValueError(f"line {line_no}: background_visibility name_words must not be empty")
        return arg, name_words
    if "name_words" in fields:
        raise ValueError(f"line {line_no}: background_visibility name_words is only used by mode 1")
    return arg, []

BG_ANIM_CONTROL_BITS: tuple[tuple[str, int], ...] = (
    ("include_children", 0x0002),
    ("children_share_range", 0x0004),
    ("restart_if_playing", 0x0008),
    ("frames_are_keys", 0x0010),
    ("sync_to_character", 0x8000),
)

BG_ANIM_CONTROL_KNOWN_MASK = 0x801E

def decode_background_play_animation_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    has_float0 = arg & 0x01
    has_float1 = (arg >> 1) & 0x01
    has_float2_or_char = (arg >> 2) & 0x01
    name_source = (arg >> 3) & 0x03
    char_ref_stream = (arg >> 5) & 0x01
    fields = [
        f"name_source={name_source}",
        f"has_float0={has_float0}",
        f"has_float1={has_float1}",
        f"has_float2_or_char={has_float2_or_char}",
        f"char_ref_stream={char_ref_stream}",
    ]
    if not words:
        fields.append("words=")
        return fields, False
    cursor = 0
    control = words[cursor]
    cursor += 1
    control_low = control & 0xFFFF
    control_index = len(fields)

    def truncated(tail: list[int]) -> tuple[list[str], bool]:
        fields.insert(control_index, f"control=0x{control:08X}")
        fields.append(f"words={words_to_csv(tail)}")
        return fields, False

    if name_source == 1:
        if cursor + 4 > len(words):
            return truncated(words[cursor:])
        fields.append(fixed_name_field("name_words", words[cursor:cursor + 4]))
        cursor += 4
    if has_float0:
        if cursor >= len(words):
            return truncated(words[cursor:])
        float0_word = words[cursor]
        float0_text = format_f32(u32_to_f32(float0_word))
        if f32_to_u32(float(float0_text)) == float0_word:
            fields.append(f"float0={float0_text}")
        else:
            fields.append(f"float0_word=0x{float0_word:08X}")
        cursor += 1
    if has_float1:
        if cursor >= len(words):
            return truncated(words[cursor:])
        float1_word = words[cursor]
        float1_text = format_f32(u32_to_f32(float1_word))
        if f32_to_u32(float(float1_text)) == float1_word:
            fields.append(f"float1={float1_text}")
        else:
            fields.append(f"float1_word=0x{float1_word:08X}")
        cursor += 1
    if has_float2_or_char:
        if control_low & 0x8000:
            if char_ref_stream:
                if cursor >= len(words):
                    return truncated(words[cursor:])
                char_ref_word = words[cursor]
                fields.append(f"char_ref_word=0x{char_ref_word:08X}")
                fields.append(f"char_ref_low=0x{char_ref_word & 0xFFFF:04X}")
                fields.append(f"char_ref_variant=0x{(char_ref_word >> 16) & 0xFF:02X}")
                raw_high = (char_ref_word >> 24) & 0xFF
                if raw_high:
                    fields.append(f"char_ref_raw_high=0x{raw_high:02X}")
                cursor += 1
        else:
            if cursor >= len(words):
                return truncated(words[cursor:])
            float2_word = words[cursor]
            float2_text = format_f32(u32_to_f32(float2_word))
            if f32_to_u32(float(float2_text)) == float2_word:
                fields.append(f"float2={float2_text}")
            else:
                fields.append(f"float2_word=0x{float2_word:08X}")
            cursor += 1
    if cursor < len(words):
        fields.append(f"trailing={words_to_csv(words[cursor:])}")
    # Control word model (CBackGround::PlayAnimation_sub1/sub2 + the stepper in
    # BackGroundProcess): named bits plus a repeat count in the high half.
    extra: list[str] = []
    if control_low & ~BG_ANIM_CONTROL_KNOWN_MASK:
        extra.append(f"control=0x{control:08X}")
    else:
        for bit_name, bit in BG_ANIM_CONTROL_BITS:
            if control_low & bit:
                extra.append(f"{bit_name}=1")
        repeat = (control >> 16) & 0xFFFF
        if repeat == 0xFFFF:
            extra.append("repeat=forever")
        elif repeat:
            extra.append(f"repeat={repeat}")
    fields[control_index:control_index] = extra
    return fields, True

def build_background_play_animation_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    if "words" in fields and "control" not in fields:
        arg = parse_hex_int(fields.get("arg", "0"))
        return arg, parse_optional_word_list(fields)
    require_fields(fields, {"name_source"}, line_no, "background_play_animation")
    name_source = parse_hex_int(fields["name_source"])
    has_float0 = parse_hex_int(fields.get("has_float0", "0"))
    has_float1 = parse_hex_int(fields.get("has_float1", "0"))
    has_float2_or_char = parse_hex_int(fields.get("has_float2_or_char", "0"))
    char_ref_stream = parse_hex_int(fields.get("char_ref_stream", "0"))
    if not 0 <= name_source <= 0x03 or any(value not in (0, 1) for value in (has_float0, has_float1, has_float2_or_char, char_ref_stream)):
        raise ValueError(f"line {line_no}: background_play_animation arg fields out of range")
    if "control" in fields:
        control = parse_hex_int(fields["control"]) & 0xFFFFFFFF
    else:
        # Derive the control word from the named bit fields and repeat count.
        control = 0
        for bit_name, bit in BG_ANIM_CONTROL_BITS:
            if bit_name in fields and parse_hex_int(fields[bit_name]):
                control |= bit
        repeat_text = fields.get("repeat", "0")
        repeat = 0xFFFF if repeat_text == "forever" else parse_hex_int(repeat_text)
        if not 0 <= repeat <= 0xFFFF:
            raise ValueError(f"line {line_no}: background_play_animation repeat out of range")
        control |= repeat << 16
    control_low = control & 0xFFFF
    if "control_low" in fields and parse_hex_int(fields["control_low"]) != control_low:
        raise ValueError(f"line {line_no}: background_play_animation control_low does not match control")
    if "control_high" in fields and parse_hex_int(fields["control_high"]) != ((control >> 16) & 0xFFFF):
        raise ValueError(f"line {line_no}: background_play_animation control_high does not match control")
    words = [control]
    if name_source == 1:
        if "name" not in fields:
            require_fields(fields, {"name_words"}, line_no, "background_play_animation")
        name_words = (resolve_fixed_name_words(fields, "name_words", line_no) or [])
        if len(name_words) != 4:
            raise ValueError(f"line {line_no}: background_play_animation name_words expects four words")
        words.extend(name_words)
    elif "name_words" in fields:
        raise ValueError(f"line {line_no}: background_play_animation name_words is only used by name_source 1")
    for field_name, present in (("float0", has_float0), ("float1", has_float1)):
        if present:
            word_name = f"{field_name}_word"
            if word_name in fields:
                word = parse_hex_int(fields[word_name]) & 0xFFFFFFFF
            else:
                require_fields(fields, {field_name}, line_no, "background_play_animation")
                word = f32_to_u32(float(fields[field_name]))
            if field_name in fields and f32_to_u32(float(fields[field_name])) != word:
                raise ValueError(f"line {line_no}: background_play_animation {field_name} does not match {word_name}")
            words.append(word)
    if has_float2_or_char:
        if control_low & 0x8000:
            if char_ref_stream:
                require_fields(fields, {"char_ref_word"}, line_no, "background_play_animation")
                char_ref_word = parse_hex_int(fields["char_ref_word"]) & 0xFFFFFFFF
                if "char_ref_low" in fields and parse_hex_int(fields["char_ref_low"]) != (char_ref_word & 0xFFFF):
                    raise ValueError(f"line {line_no}: background_play_animation char_ref_low does not match char_ref_word")
                if "char_ref_variant" in fields and parse_hex_int(fields["char_ref_variant"]) != ((char_ref_word >> 16) & 0xFF):
                    raise ValueError(f"line {line_no}: background_play_animation char_ref_variant does not match char_ref_word")
                words.append(char_ref_word)
        else:
            if "float2_word" in fields:
                float2_word = parse_hex_int(fields["float2_word"]) & 0xFFFFFFFF
            else:
                require_fields(fields, {"float2"}, line_no, "background_play_animation")
                float2_word = f32_to_u32(float(fields["float2"]))
            if "float2" in fields and f32_to_u32(float(fields["float2"])) != float2_word:
                raise ValueError(f"line {line_no}: background_play_animation float2 does not match float2_word")
            words.append(float2_word)
    default_arg = has_float0 | (has_float1 << 1) | (has_float2_or_char << 2) | (name_source << 3) | (char_ref_stream << 5)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    # Bits 6-7 are outside the structured fields; an explicit arg= preserves them.
    if (arg & 0x3F) != default_arg:
        raise ValueError(f"line {line_no}: background_play_animation arg does not match structured fields")
    words.extend(parse_optional_word_list(fields, "trailing"))
    if "words" in fields:
        words.extend(parse_optional_word_list(fields))
    return arg, words

def build_person_allow_attribute_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    """Command_c3: SetAllowAttribute(person, arg>>6, ((arg>>2)&3)-1, ((arg>>4)&3)-1)."""
    named = {"selector", "allow_a", "allow_b", "explicit_char", "character"}
    if not named & set(fields):
        return parse_hex_int(fields.get("arg", "0")), parse_optional_word_list(fields)
    explicit = parse_hex_int(fields.get("explicit_char", "1" if "character" in fields else "0"))
    selector = parse_hex_int(fields.get("selector", "0"))
    allow_a = parse_hex_int(fields.get("allow_a", "0"))
    allow_b = parse_hex_int(fields.get("allow_b", "0"))
    if explicit not in (0, 1) or not all(0 <= value <= 3 for value in (selector, allow_a, allow_b)):
        raise ValueError(f"line {line_no}: person_allow_attribute fields out of range")
    default_arg = explicit | (allow_a << 2) | (allow_b << 4) | (selector << 6)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if (arg & 0x01) != explicit or ((arg >> 2) & 0x03) != allow_a or ((arg >> 4) & 0x03) != allow_b or ((arg >> 6) & 0x03) != selector:
        raise ValueError(f"line {line_no}: person_allow_attribute arg does not match fields")
    words: list[int] = []
    if explicit:
        require_fields(fields, {"character"}, line_no, "person_allow_attribute")
        words.append(parse_hex_int(fields["character"]) & 0xFFFFFFFF)
    elif "character" in fields:
        raise ValueError(f"line {line_no}: person_allow_attribute character requires explicit_char=1")
    words.extend(parse_optional_word_list(fields))
    return arg, words

def build_chara_put_attach_life_flag_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    """Command_e0: AttachLifeFlag(mgr, charno, subno, flag_word & 0xFFFF)."""
    if "life_flag" not in fields:
        return parse_hex_int(fields.get("arg", "0")), parse_optional_word_list(fields)
    explicit = parse_hex_int(fields.get("explicit_char", "1" if "character" in fields else "0"))
    flag = parse_hex_int(fields["life_flag"])
    raw_high = parse_hex_int(fields.get("raw_high", "0"))
    if explicit not in (0, 1) or not 0 <= flag <= 0xFFFF or not 0 <= raw_high <= 0xFFFF:
        raise ValueError(f"line {line_no}: chara_put_attach_life_flag fields out of range")
    arg = parse_hex_int(fields.get("arg", str(explicit)))
    if (arg & 0x01) != explicit:
        raise ValueError(f"line {line_no}: chara_put_attach_life_flag arg does not match explicit_char")
    words: list[int] = []
    if explicit:
        require_fields(fields, {"character"}, line_no, "chara_put_attach_life_flag")
        words.append(parse_hex_int(fields["character"]) & 0xFFFFFFFF)
    elif "character" in fields:
        raise ValueError(f"line {line_no}: chara_put_attach_life_flag character requires explicit_char=1")
    words.append(flag | (raw_high << 16))
    words.extend(parse_optional_word_list(fields))
    return arg, words

def build_packing_file_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    """Command_1e: arg bit 0 clear loads a packing file id; set releases slot arg>>4."""
    if "release" not in fields and "file" not in fields and "slot" not in fields:
        return parse_hex_int(fields.get("arg", "0")), parse_optional_word_list(fields)
    release = parse_hex_int(fields.get("release", "0" if "file" in fields else "1"))
    if release not in (0, 1):
        raise ValueError(f"line {line_no}: packing_file_load_or_release release must be 0 or 1")
    if release:
        slot = parse_hex_int(fields.get("slot", "0"))
        if not 0 <= slot <= 0x0F:
            raise ValueError(f"line {line_no}: packing_file_load_or_release slot out of range")
        default_arg = 0x01 | (slot << 4)
        arg = parse_hex_int(fields.get("arg", str(default_arg)))
        if (arg & 0x01) != 1 or (arg >> 4) != slot:
            raise ValueError(f"line {line_no}: packing_file_load_or_release arg does not match slot")
        return arg, parse_optional_word_list(fields)
    require_fields(fields, {"file"}, line_no, "packing_file_load_or_release")
    file_id = parse_hex_int(fields["file"])
    raw_high = parse_hex_int(fields.get("raw_high", "0"))
    if not 0 <= file_id <= 0xFFFF or not 0 <= raw_high <= 0xFFFF:
        raise ValueError(f"line {line_no}: packing_file_load_or_release file/raw_high out of range")
    arg = parse_hex_int(fields.get("arg", "0"))
    if arg & 0x01:
        raise ValueError(f"line {line_no}: packing_file_load_or_release arg bit 0 conflicts with file=")
    return arg, [file_id | (raw_high << 16)] + parse_optional_word_list(fields)

def build_sound_field_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    """Command_78: stores arg & 0x0F to gpRadiSound+0xE0; no operands."""
    if "value" not in fields:
        return parse_hex_int(fields.get("arg", "0")), parse_optional_word_list(fields)
    value = parse_hex_int(fields["value"])
    if not 0 <= value <= 0x0F:
        raise ValueError(f"line {line_no}: sound_field_e0_set value out of range")
    arg = parse_hex_int(fields.get("arg", str(value)))
    if (arg & 0x0F) != value:
        raise ValueError(f"line {line_no}: sound_field_e0_set arg does not match value")
    return arg, parse_optional_word_list(fields)

def build_battle_character_control_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    """Command_a0: 14-entry jump table on arg & 0x3F; arg bit 7 is the boolean input."""
    if "mode" not in fields:
        return parse_hex_int(fields.get("arg", "0")), parse_optional_word_list(fields)
    mode = parse_hex_int(fields["mode"])
    bit7 = parse_hex_int(fields.get("bit7", "0"))
    if not 0 <= mode <= 0x3F or bit7 not in (0, 1):
        raise ValueError(f"line {line_no}: battle_character_fall_or_plugin_control fields out of range")
    default_arg = mode | (bit7 << 7)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if (arg & 0x3F) != mode or ((arg >> 7) & 0x01) != bit7:
        raise ValueError(f"line {line_no}: battle_character_fall_or_plugin_control arg does not match fields")
    return arg, parse_optional_word_list(fields)

PARAM_HOLDER_ACTIONS: dict[int, str] = {
    0: "build_group",
    1: "read_element",
    2: "find_stand",
    3: "read_element_3",
    4: "build_children",
    5: "read_element_5",
}

PARAM_HOLDER_ACTION_CODES: dict[str, int] = {name: mode for mode, name in PARAM_HOLDER_ACTIONS.items()}

def build_character_script_param_holder_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    """Command_d2: build/read the per-character script param holder."""
    named = {"action", "selector", "selector_from_event", "to_event", "character"}
    if not named & set(fields):
        return parse_hex_int(fields.get("arg", "0")), parse_optional_word_list(fields)
    require_fields(fields, {"action", "to_event"}, line_no, "character_script_param_holder")
    mode = PARAM_HOLDER_ACTION_CODES.get(fields["action"])
    if mode is None:
        raise ValueError(f"line {line_no}: character_script_param_holder unknown action {fields['action']!r}")
    if "selector_from_event" in fields:
        sel = parse_hex_int(fields["selector_from_event"])
        sel_from_event = 1
    else:
        require_fields(fields, {"selector"}, line_no, "character_script_param_holder")
        sel = parse_hex_int(fields["selector"])
        sel_from_event = 0
    dest = parse_hex_int(fields["to_event"])
    if not 0 <= sel <= 0xFFFF or not 0 <= dest <= 0xFFFF:
        raise ValueError(f"line {line_no}: character_script_param_holder selector/to_event out of range")
    explicit = 1 if "character" in fields else 0
    arg = parse_hex_int(fields.get("arg", str(explicit | (sel_from_event << 1) | (mode << 2))))
    if arg != (explicit | (sel_from_event << 1) | (mode << 2)):
        raise ValueError(f"line {line_no}: character_script_param_holder arg does not match fields")
    words: list[int] = []
    if explicit:
        words.append(parse_hex_int(fields["character"]) & 0xFFFFFFFF)
    words.append(sel | (dest << 16))
    return arg, words

def build_battle_copy_character_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    """Command_d3: CopyCharacter; the copy result goes to event value 0x11."""
    if "character" not in fields:
        return parse_hex_int(fields.get("arg", "0")), parse_optional_word_list(fields)
    arg = parse_hex_int(fields.get("arg", "1"))
    if arg != 1:
        raise ValueError(f"line {line_no}: battle_copy_character character= requires arg bit 0")
    return arg, [parse_hex_int(fields["character"]) & 0xFFFFFFFF]

def build_battle_volty_distance_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    """Command_d4: Script_CalcVoltyDistance(chrA, chrB, float) into the float slots."""
    named = {"character_a", "character_b", "distance"}
    if not named & set(fields):
        return parse_hex_int(fields.get("arg", "0")), parse_optional_word_list(fields)
    require_fields(fields, {"distance"}, line_no, "battle_volty_distance")
    explicit_a = 1 if "character_a" in fields else 0
    explicit_b = 1 if "character_b" in fields else 0
    arg = parse_hex_int(fields.get("arg", str(explicit_a | (explicit_b << 1))))
    if arg != (explicit_a | (explicit_b << 1)):
        raise ValueError(f"line {line_no}: battle_volty_distance arg does not match fields")
    words: list[int] = []
    if explicit_a:
        words.append(parse_hex_int(fields["character_a"]) & 0xFFFFFFFF)
    if explicit_b:
        words.append(parse_hex_int(fields["character_b"]) & 0xFFFFFFFF)
    words.append(f32_to_u32(float(fields["distance"])))
    return arg, words

def build_character_collision_scale_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    """Command_d1: scale a character's object from another's collision size."""
    named = {"character", "scale_character", "name", "name_source", "explicit_char"}
    if not named & set(fields):
        return parse_hex_int(fields.get("arg", "0")), parse_optional_word_list(fields)
    explicit = parse_hex_int(fields.get("explicit_char", "1" if "character" in fields else "0"))
    name_source = parse_hex_int(fields.get("name_source", "1" if "name" in fields else "0"))
    scale_from_stream = 1 if "scale_character" in fields else 0
    if name_source == 1 and scale_from_stream:
        raise ValueError(f"line {line_no}: character_collision_control_d1 inline name and scale_character overlap in the stream")
    default_arg = explicit | (name_source << 1) | (scale_from_stream << 3)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if arg != default_arg:
        raise ValueError(f"line {line_no}: character_collision_control_d1 arg does not match fields")
    words: list[int] = []
    if explicit:
        require_fields(fields, {"character"}, line_no, "character_collision_control_d1")
        words.append(parse_hex_int(fields["character"]) & 0xFFFFFFFF)
    if name_source == 1:
        require_fields(fields, {"name"}, line_no, "character_collision_control_d1")
        words.extend(fixed_name_words_from_text(fields, "name", line_no))
    if scale_from_stream:
        words.append(parse_hex_int(fields["scale_character"]) & 0xFFFFFFFF)
    return arg, words

NEW_FORM_BUILDERS: dict[str, Any] = {
    "person_allow_attribute": (0xC3, build_person_allow_attribute_words),
    "chara_put_attach_life_flag": (0xE0, build_chara_put_attach_life_flag_words),
    "packing_file_load_or_release": (0x1E, build_packing_file_words),
    "sound_field_e0_set": (0x78, build_sound_field_words),
    "battle_character_fall_or_plugin_control": (0xA0, build_battle_character_control_words),
    "character_script_param_holder": (0xD2, build_character_script_param_holder_words),
    "battle_copy_character": (0xD3, build_battle_copy_character_words),
    "battle_volty_distance": (0xD4, build_battle_volty_distance_words),
    "character_collision_control_d1": (0xD1, build_character_collision_scale_words),
}

EXPR_HEAD_OPS: dict[str, int] = {
    "set_value": 0,
    "add_value": 1,
    "sub_value": 2,
    "mul_value": 3,
    "div_value": 4,
    "mod_value": 5,
    # `and_value` / `or_value` name the machine instruction, which means nothing
    # to someone who has not written C. The printed names say what happens to
    # the target instead; both spellings still compile.
    "keep_only_bits": 6,
    "turn_on_bits": 7,
    "and_value": 6,
    "or_value": 7,
}

EXPR_OP_HEADS: dict[int, str] = {
    0: "set_value",
    1: "add_value",
    2: "sub_value",
    3: "mul_value",
    4: "div_value",
    5: "mod_value",
    6: "keep_only_bits",
    7: "turn_on_bits",
}

EXPR_FLAG_HEAD_OPS: dict[str, int] = {
    "set_character_flag": 7,
    "clear_character_flag": 6,
}

CHARA_PROPERTY_NAMES: dict[int, str] = {
    0x00: "hp",
    0x01: "hp_max",
    0x03: "evasion",
    0x06: "money",
    0x0A: "guard_counter",
    0x0B: "damage_counter",
    0x0C: "ailment_state",
    # CharaParaCode_029 (0x0029EED0) reads and writes one byte at
    # CCharacterPerson+0x82. A scan of every byte, halfword and word access in
    # Main.elf, step0_00, step1_02 and step2_00 finds no other reader: the only
    # other reference is ReleaseScheduleList zeroing it. Scripts own it end to
    # end, setting bit 0 with `or_value value=1`, clearing it with
    # `and_value value=254`, and testing it with `value_from_property=0x1D`.
    0x1D: "script_flag",
    0x21: "schedule_percent",
    0x2B: "item_count",
    0x32: "battle_target",
    0x33: "magic_charge",
    0x36: "magic_level",
    0x37: "owner_character",
    0x38: "equipped_skill",
    0x39: "using_item",
    0x3B: "fake_death",
    0x3D: "move_progress",
    0x3F: "volty",
    0x42: "last_stolen_item",
    0x44: "experience",
    0x46: "eternal_tactics",
    0x47: "formation",
    0x48: "friend_list",
}

CHARA_PROPERTY_CODES: dict[str, int] = {name: code for code, name in CHARA_PROPERTY_NAMES.items()}

CHARACTER_SLOT_NAMES: dict[int, str] = {
    0x26AC: "current",
    0x26AD: "party1",
    0x26AE: "party2",
    0x26AF: "party3",
    0x26B0: "party4",
    0x26B1: "party5",
    # 0x26E1+N reads SCR_DATA+0x34+4N, which SetEventValueForScript shows is
    # event value 1000+N: "whichever character that value is holding".
    **{0x26E1 + index: f"value{1000 + index}" for index in range(10)},
}

CHARACTER_SLOT_CODES: dict[str, int] = {name: code for code, name in CHARACTER_SLOT_NAMES.items()}

def render_expr_character(subject: int) -> str:
    return CHARACTER_SLOT_NAMES.get(subject, f"0x{subject:X}")

def parse_expr_character(text: str, line_no: int) -> int:
    if text.lower() in CHARACTER_SLOT_CODES:
        return CHARACTER_SLOT_CODES[text.lower()]
    value = parse_hex_int(text)
    if not 0 <= value <= 0xFFFFFF:
        raise ValueError(f"line {line_no}: character id out of range")
    return value

EXPR_COMPONENT_NAMES: dict[int, str] = {0: "x", 1: "y", 2: "z"}

EXPR_COMPONENT_CODES: dict[str, int] = {name: code for code, name in EXPR_COMPONENT_NAMES.items()}

def parse_expr_component(text: str, line_no: int) -> int:
    code = EXPR_COMPONENT_CODES.get(text.lower())
    if code is None:
        code = parse_hex_int(text)
    if not 0 <= code <= 0xFF:
        raise ValueError(f"line {line_no}: set_value component out of range")
    return code

def single_bit_index(value: int) -> int | None:
    """Which bit a value sets, or None when it is not exactly one bit."""
    return value.bit_length() - 1 if value and not value & (value - 1) else None

def expr_character_flag_head(
    op: int, target_from: int, value_from: int, lhs: int, rhs: int
) -> tuple[str, int] | None:
    """Recognise "switch one flag of a character property on or off".

    `or` with a single bit turns that flag on; `and` with a byte that has
    exactly one bit missing turns it off. Anything else is a real bitwise
    operation and keeps the `or_value` / `and_value` spelling.
    """
    if target_from != 0x2 or value_from != 0xF:
        return None
    if op == 7:
        bit = single_bit_index(rhs)
        return ("set_character_flag", bit) if bit is not None and bit < 8 else None
    if op == 6 and rhs <= 0xFF:
        bit = single_bit_index((~rhs) & 0xFF)
        return ("clear_character_flag", bit) if bit is not None else None
    return None

def format_expr_friendly(words: list[int], flags: int) -> str | None:
    """Readable spelling for a Command_14 line, or None when the shape is odd."""
    control, lhs, rhs = words
    op = control & 0x07
    target_from = (control >> 4) & 0x0F
    value_from = (control >> 8) & 0x0F
    if control >> 12 or control & 0x08:
        return None
    # A character property used as a byte of eight independent flags is by far
    # the commonest use of the bitwise operations, and "or 1" / "and 254" is a
    # terrible way to write "switch flag 0 on / off". Spell those two out.
    flag_head = expr_character_flag_head(op, target_from, value_from, lhs, rhs)
    if flag_head is not None:
        head, bit = flag_head
        return (
            f"  {head} character={render_expr_character(lhs & 0xFFFFFF)} "
            f"property={CHARA_PROPERTY_NAMES.get(lhs >> 24, f'0x{lhs >> 24:02X}')} "
            f"flag={bit}" + source_flags_suffix(flags)
        )
    parts = [EXPR_OP_HEADS[op]]
    if target_from == 0x0 and lhs >> 24 == 0:
        # Flag words pack a multi-bit field: low16 = first flag id, bits
        # 16-23 = extra bit count; consecutive flags hold the value LSB-first
        # (proven at 0x002EA0A8/0x002EA3D8 in Command_14).
        parts.append(f"flag={lhs & 0xFFFF}")
        if lhs >> 16:
            parts.append(f"flag_bits={((lhs >> 16) & 0xFF) + 1}")
    elif target_from == 0x1 and lhs >> 16 == 0:
        parts.append(f"event_value={lhs}")
    elif target_from == 0x2:
        prop = lhs >> 24
        parts.append(f"character={render_expr_character(lhs & 0xFFFFFF)}")
        parts.append(f"property={CHARA_PROPERTY_NAMES.get(prop, f'0x{prop:02X}')}")
    elif target_from == 0x3:
        parts.append(f"system_param=0x{lhs:X}")
    elif target_from == 0x5 and (lhs >> 16) & 0xFF == 0 and lhs & 0xFFFF in (0x3F3, 0x3F5):
        # SCR_DATA float slots: id 0x3F3 = plain float vector at +0x60,
        # id 0x3F5 = angle vector at +0x70 stored as radians (input degrees).
        slot_key = "float_slot" if lhs & 0xFFFF == 0x3F3 else "angle_slot"
        parts.append(f"{slot_key}={EXPR_COMPONENT_NAMES.get(lhs >> 24, str(lhs >> 24))}")
    else:
        return None
    if value_from == 0xF:
        value = rhs if rhs < 0x80000000 else rhs - (1 << 32)
        parts.append(f"value={value}")
    elif value_from == 0x0 and rhs >> 24 == 0:
        parts.append(f"value_from_flag={rhs & 0xFFFF}")
        if rhs >> 16:
            parts.append(f"value_from_flag_bits={((rhs >> 16) & 0xFF) + 1}")
    elif value_from == 0x1 and rhs >> 16 == 0:
        parts.append(f"value_from_event={rhs}")
    elif value_from == 0x2:
        prop = rhs >> 24
        parts.append(f"value_from_character={render_expr_character(rhs & 0xFFFFFF)}")
        parts.append(f"value_from_property={CHARA_PROPERTY_NAMES.get(prop, f'0x{prop:02X}')}")
    elif value_from == 0x3:
        parts.append(f"value_from_system=0x{rhs:X}")
    elif value_from == 0x5 and (rhs >> 16) & 0xFF == 0:
        # Stand-position component read: low16 = stand id, byte3 = axis.
        parts.append(f"value_from_stand=0x{rhs & 0xFFFF:X}")
        parts.append(f"stand_component={EXPR_COMPONENT_NAMES.get(rhs >> 24, str(rhs >> 24))}")
    else:
        return None
    return "  " + " ".join(parts) + source_flags_suffix(flags)

def build_expr_friendly_words(fields: dict[str, str], line_no: int) -> list[int]:
    """Assemble [control, lhs, rhs] from the friendly set_value fields."""
    op = parse_hex_int(fields.get("op", "0"))
    if not 0 <= op <= 7:
        raise ValueError(f"line {line_no}: set_value op out of range")
    if "flag" in fields:
        target_from, lhs = 0x0, parse_hex_int(fields["flag"])
        if "flag_bits" in fields:
            bits = parse_hex_int(fields["flag_bits"])
            if not 1 <= bits <= 0x100 or lhs >> 16:
                raise ValueError(f"line {line_no}: set_value flag/flag_bits out of range")
            lhs |= (bits - 1) << 16
    elif "event_value" in fields:
        target_from, lhs = 0x1, parse_hex_int(fields["event_value"])
    elif "property" in fields:
        prop_text = fields["property"]
        prop = CHARA_PROPERTY_CODES.get(prop_text.lower(), None)
        if prop is None:
            prop = parse_hex_int(prop_text)
        if not 0 <= prop <= 0xFF:
            raise ValueError(f"line {line_no}: set_value property out of range")
        subject = parse_expr_character(fields.get("character", "current"), line_no)
        target_from, lhs = 0x2, (prop << 24) | subject
    elif "system_param" in fields:
        target_from, lhs = 0x3, parse_hex_int(fields["system_param"])
    elif "float_slot" in fields:
        target_from, lhs = 0x5, 0x3F3 | (parse_expr_component(fields["float_slot"], line_no) << 24)
    elif "angle_slot" in fields:
        target_from, lhs = 0x5, 0x3F5 | (parse_expr_component(fields["angle_slot"], line_no) << 24)
    else:
        raise ValueError(f"line {line_no}: set_value needs a target (flag=, event_value=, property=, system_param=, float_slot=, or angle_slot=)")
    if not 0 <= lhs <= 0xFFFFFFFF:
        raise ValueError(f"line {line_no}: set_value target id out of range")
    if "value_from_flag" in fields:
        value_from, rhs = 0x0, parse_hex_int(fields["value_from_flag"])
        if "value_from_flag_bits" in fields:
            bits = parse_hex_int(fields["value_from_flag_bits"])
            if not 1 <= bits <= 0x100 or rhs >> 16:
                raise ValueError(f"line {line_no}: set_value value_from_flag/value_from_flag_bits out of range")
            rhs |= (bits - 1) << 16
    elif "value_from_event" in fields:
        value_from, rhs = 0x1, parse_hex_int(fields["value_from_event"])
    elif "value_from_character" in fields or "value_from_property" in fields:
        prop_text = fields.get("value_from_property", "0")
        prop = CHARA_PROPERTY_CODES.get(prop_text.lower(), None)
        if prop is None:
            prop = parse_hex_int(prop_text)
        subject = parse_expr_character(fields.get("value_from_character", "current"), line_no)
        value_from, rhs = 0x2, (prop << 24) | subject
    elif "value_from_system" in fields:
        value_from, rhs = 0x3, parse_hex_int(fields["value_from_system"])
    elif "value_from_stand" in fields:
        component = parse_expr_component(fields.get("stand_component", "x"), line_no)
        value_from, rhs = 0x5, (parse_hex_int(fields["value_from_stand"]) & 0xFFFF) | (component << 24)
    elif "rhs" in fields:  # `value=` arrives here through the expr field aliases
        value_from, rhs = 0xF, parse_hex_int(fields["rhs"]) & 0xFFFFFFFF
    else:
        raise ValueError(f"line {line_no}: set_value needs a value (value=, value_from_flag=, value_from_event=, value_from_character=, or value_from_system=)")
    control = op | (target_from << 4) | (value_from << 8)
    if "control" in fields and parse_hex_int(fields["control"]) != control:
        raise ValueError(f"line {line_no}: set_value control does not match named fields")
    return [control, lhs & 0xFFFFFFFF, rhs & 0xFFFFFFFF]

BRANCH_COMPARE_FIELDS: dict[int, str] = {
    0: "is",        # eq
    1: "at_least",  # ge
    2: "over",      # gt
    3: "not",       # ne
    4: "under",     # lt
    5: "at_most",   # le
}

BRANCH_COMPARE_SELECTORS: dict[str, int] = {name: sel for sel, name in BRANCH_COMPARE_FIELDS.items()}

BRANCH_BLOCK_HEADS = {"if_value", "if_flag", "unless_value", "unless_flag"}

NEGATED_COMPARATORS = {
    "is": "not",
    "not": "is",
    "at_least": "under",
    "under": "at_least",
    "over": "at_most",
    "at_most": "over",
}

BRANCH_FRIENDLY_HEADS = ("if_flag", "if_value", "unless_flag", "unless_value")

def format_branch_friendly(condition: int, words: list[int], target_text: str, flags: int) -> str | None:
    """Readable spelling for a Command_02 branch, or None when the shape is odd."""
    if condition == 0 and len(words) == 1:
        return f"  jump goto={target_text}" + source_flags_suffix(flags)
    base = condition & 0x7F
    invert = condition >> 7
    if base not in (1, 2) or len(words) != 3:
        return None
    word0, word1 = words[1], words[2]
    byte3 = word0 >> 24
    selector = (byte3 >> 1) & 0x07
    # byte 3 packs the compare selector in bits 1-3; any other bit set means an
    # event-value comparison or an unusual control byte - keep the exact form.
    if byte3 != (selector << 1) or selector not in BRANCH_COMPARE_FIELDS:
        return None
    value = word1 if word1 < 0x80000000 else word1 - (1 << 32)
    compare_field = f"{BRANCH_COMPARE_FIELDS[selector]}={value}"
    if base == 2:
        if (word0 >> 12) & 0xFFF:
            return None
        head = "unless_value" if invert else "if_value"
        return f"  {head} event_value={word0 & 0x0FFF} {compare_field} goto={target_text}" + source_flags_suffix(flags)
    if (word0 >> 20) & 0x0F:
        return None
    count = ((word0 >> 16) & 0x0F) + 1
    head = "unless_flag" if invert else "if_flag"
    count_field = f" count={count}" if count != 1 else ""
    return f"  {head} flag={word0 & 0xFFFF}{count_field} {compare_field} goto={target_text}" + source_flags_suffix(flags)

def build_branch_friendly_words(head: str, fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    """Return (condition byte, [word0, word1]) for an if_/unless_ line."""
    selectors = [name for name in BRANCH_COMPARE_SELECTORS if name in fields]
    if len(selectors) != 1:
        raise ValueError(
            f"line {line_no}: {head} needs exactly one comparison "
            f"(is=, not=, at_least=, at_most=, over=, under=)"
        )
    selector = BRANCH_COMPARE_SELECTORS[selectors[0]]
    value = parse_hex_int(fields[selectors[0]]) & 0xFFFFFFFF
    byte3 = selector << 1
    if head.endswith("_value"):
        require_fields(fields, {"event_value"}, line_no, head)
        event_value = parse_hex_int(fields["event_value"])
        if not 0 <= event_value <= 0x0FFF:
            raise ValueError(f"line {line_no}: {head} event_value out of range")
        word0 = event_value | (byte3 << 24)
        base = 2
    else:
        require_fields(fields, {"flag"}, line_no, head)
        flag = parse_hex_int(fields["flag"])
        count = parse_hex_int(fields.get("count", "1"))
        if not 0 <= flag <= 0xFFFF or not 1 <= count <= 16:
            raise ValueError(f"line {line_no}: {head} flag/count out of range")
        word0 = flag | ((count - 1) << 16) | (byte3 << 24)
        base = 1
    condition = base | (0x80 if head.startswith("unless_") else 0)
    return condition, [word0, value]

RAW_ESCAPE_FORMS: dict[str, tuple[int, str]] = {
    "bgm_control": (0x72, "value"),
    "camera_select": (0x50, "camera"),
    "set_radiata_time": (0xC5, "byte0"),
    "load_script_file": (0x06, "file"),
    "load_background": (0x40, "id"),
    "load_texture": (0x60, "texture"),
    "load_paf": (0x61, "paf"),
    "primitive_priority": (0x66, "priority"),
    "text_message_layout": (0x8E, "x"),
    "expr": (0x14, "control"),
    # Arg-only forms: their structured shape has no payload words at all, so
    # any words= list means a raw data region ("__none__" never matches).
    "camera_flags": (0x5A, "__none__"),
    "play_bgm": (0x71, "__none__"),
    "setting_map": (0x42, "id"),
    "delete_background": (0x43, "__none__"),
}

def decode_background_stop_animation_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    name_source = arg & 0x03
    stop_mode = (arg >> 4) & 0x0F
    fields = [f"name_source={name_source}", f"stop_mode={stop_mode}"]
    if name_source == 1 and len(words) == 4:
        text = words_to_sjis_text(words)
        if text is not None:
            fields.append(f"name={json.dumps(text, ensure_ascii=False)}")
            return fields, True
    if name_source == 1:
        fields.append(fixed_name_field("name_words", words))
        return fields, False
    if words:
        fields.append(f"words={words_to_csv(words)}")
        return fields, False
    return fields, name_source in (0, 2)

def build_background_stop_animation_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    if "arg" in fields:
        arg = parse_hex_int(fields["arg"])
        name_source = parse_hex_int(fields.get("name_source", str(arg & 0x03)))
        stop_mode = parse_hex_int(fields.get("stop_mode", str((arg >> 4) & 0x0F)))
    else:
        require_fields(fields, {"name_source", "stop_mode"}, line_no, "background_stop_animation")
        name_source = parse_hex_int(fields["name_source"])
        stop_mode = parse_hex_int(fields["stop_mode"])
        arg = name_source | (stop_mode << 4)
    if not 0 <= name_source <= 3 or not 0 <= stop_mode <= 0x0F:
        raise ValueError(f"line {line_no}: background_stop_animation fields out of range")
    if (arg & 0xF3) != (name_source | (stop_mode << 4)):
        raise ValueError(f"line {line_no}: background_stop_animation arg does not match name_source/stop_mode")
    if "words" in fields and "name" not in fields and "name_words" not in fields:
        return arg, parse_optional_word_list(fields)
    if name_source == 1:
        if "name" in fields:
            return arg, fixed_name_words_from_text(
                fields, "name", line_no, label="background_stop_animation name"
            )
        if "name_words" in fields:
            name_words = (resolve_fixed_name_words(fields, "name_words", line_no) or [])
            if len(name_words) != 4:
                raise ValueError(f"line {line_no}: background_stop_animation name_words expects four words")
            return arg, name_words
        raise ValueError(f"line {line_no}: background_stop_animation name_source 1 requires name= or name_words=")
    return arg, parse_optional_word_list(fields)

def decode_background_auto_rate_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    name_source = (arg >> 1) & 0x03
    flag3 = (arg >> 3) & 0x01
    event_duration = (arg >> 4) & 0x01
    fields = [
        f"mode=0x{arg:02X}",
        f"name_source={name_source}",
        f"flag3={flag3}",
        f"event_duration={event_duration}",
    ]
    if len(words) < 2:
        return fields, False
    control = words[0]
    duration_word = words[1]
    cursor = 2
    b0 = control & 0xFF
    b1 = (control >> 8) & 0xFF
    b2 = (control >> 16) & 0xFF
    b3 = (control >> 24) & 0xFF
    fields.append(f"control=0x{control:08X}")
    fields.append(f"control_bytes=0x{b0:02X},0x{b1:02X},0x{b2:02X},0x{b3:02X}")
    fields.append(f"duration_word=0x{duration_word:08X}")
    if event_duration:
        fields.append(f"duration_event=0x{duration_word & 0xFFFF:04X}")
        fields.append(f"duration_raw_high=0x{(duration_word >> 16) & 0xFFFF:04X}")
    else:
        fields.append(f"duration={duration_word}")
    if name_source == 1:
        if cursor + 4 > len(words):
            return fields, False
        name_words = words[cursor:cursor + 4]
        cursor += 4
        text = words_to_sjis_text(name_words)
        if text is not None:
            fields.append(f"target_name={json.dumps(text, ensure_ascii=False)}")
        else:
            fields.append(fixed_name_field("target_name_words", name_words))

    if sign_extend(duration_word, 32) >= 0 or event_duration:
        if b0 & 0x02:
            fields.append("color_rate=1")
            fields.append(f"color_mode={(b0 >> 3) & 0x03}")
            fields.append(f"color_flag2={(b0 >> 2) & 0x01}")
            if b0 & 0x01:
                if cursor + 3 > len(words):
                    return fields, False
                fields.append(f"color_start_vec={format_vec3_words(words[cursor:cursor + 3])}")
                cursor += 3
            if cursor + 3 > len(words):
                return fields, False
            fields.append(f"color_end_vec={format_vec3_words(words[cursor:cursor + 3])}")
            cursor += 3
        if b0 & 0x40:
            fields.append("transparent=1")
            fields.append(f"transparent_mode={b1 & 0x03}")
            fields.append(f"transparent_flag7={(b0 >> 7) & 0x01}")
            if b0 & 0x20:
                if cursor >= len(words):
                    return fields, False
                fields.append(f"transparent_from={format_f32(u32_to_f32(words[cursor]))}")
                cursor += 1
            else:
                fields.append("transparent_from=-1")
            if cursor >= len(words):
                return fields, False
            fields.append(f"transparent_to={format_f32(u32_to_f32(words[cursor]))}")
            cursor += 1
        if b1 & 0x08:
            fields.append("scale=1")
            fields.append(f"scale_flag4={(b1 >> 4) & 0x01}")
            if b1 & 0x04:
                if cursor + 3 > len(words):
                    return fields, False
                fields.append(f"scale_start_vec={format_vec3_words(words[cursor:cursor + 3])}")
                cursor += 3
            if cursor + 3 > len(words):
                return fields, False
            fields.append(f"scale_end_vec={format_vec3_words(words[cursor:cursor + 3])}")
            cursor += 3
        if b1 & 0x40:
            fields.append("palette=1")
            fields.append(f"palette_flag7={(b1 >> 7) & 0x01}")
            if b1 & 0x20:
                if cursor >= len(words):
                    return fields, False
                fields.append(f"palette_from={format_f32(u32_to_f32(words[cursor]))}")
                cursor += 1
            else:
                fields.append("palette_from=-1")
            if cursor + 2 > len(words):
                return fields, False
            fields.append(f"palette_to={format_f32(u32_to_f32(words[cursor]))}")
            fields.append(f"palette_id=0x{words[cursor + 1]:08X}")
            cursor += 2
        if b2 & 0x02:
            fields.append("visibility=1")
            fields.append(f"visibility_flag2={(b2 >> 2) & 0x01}")
            if b2 & 0x01:
                if cursor >= len(words):
                    return fields, False
                fields.append(f"visibility_from={format_f32(u32_to_f32(words[cursor]))}")
                cursor += 1
            else:
                fields.append("visibility_from=-1")
            if cursor >= len(words):
                return fields, False
            fields.append(f"visibility_to={format_f32(u32_to_f32(words[cursor]))}")
            cursor += 1
    if cursor < len(words):
        fields.append(f"trailing={words_to_csv(words[cursor:])}")
        return fields, False
    return fields, True

def build_background_auto_rate_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    if "mode" in fields:
        arg = parse_hex_int(fields["mode"])
        name_source = parse_hex_int(fields.get("name_source", str((arg >> 1) & 0x03)))
        flag3 = parse_hex_int(fields.get("flag3", str((arg >> 3) & 0x01)))
        event_duration = parse_hex_int(fields.get("event_duration", str((arg >> 4) & 0x01)))
    else:
        require_fields(fields, {"name_source", "flag3", "event_duration"}, line_no, "background_auto_rate_anim")
        name_source = parse_hex_int(fields["name_source"])
        flag3 = parse_hex_int(fields["flag3"])
        event_duration = parse_hex_int(fields["event_duration"])
        arg = (name_source << 1) | (flag3 << 3) | (event_duration << 4)
    if not 0 <= arg <= 0xFF or not 0 <= name_source <= 3 or flag3 not in (0, 1) or event_duration not in (0, 1):
        raise ValueError(f"line {line_no}: background_auto_rate_anim mode fields out of range")
    if (arg & 0x1E) != ((name_source << 1) | (flag3 << 3) | (event_duration << 4)):
        raise ValueError(f"line {line_no}: background_auto_rate_anim mode does not match name_source/flag3/event_duration")
    if "words" in fields and "control" not in fields:
        return arg, parse_optional_word_list(fields)
    require_fields(fields, {"duration_word"}, line_no, "background_auto_rate_anim")
    words = [
        resolve_packed_word(fields, line_no, "background_auto_rate_anim", "control", AUTO_RATE_CONTROL_SPECS),
        parse_hex_int(fields["duration_word"]) & 0xFFFFFFFF,
    ]
    if name_source == 1:
        if "target_name" in fields:
            words.extend(fixed_name_words_from_text(fields, "target_name", line_no))
        else:
            if "target_name" not in fields:
                require_fields(fields, {"target_name_words"}, line_no, "background_auto_rate_anim")
            name_words = (resolve_fixed_name_words(fields, "target_name_words", line_no) or [])
            if len(name_words) != 4:
                raise ValueError(f"line {line_no}: background_auto_rate_anim target_name_words expects four words")
            words.extend(name_words)
    if "start_vec" in fields:
        words.extend(parse_vec3_words(fields["start_vec"], line_no, "start_vec"))
    if "end_vec" in fields:
        words.extend(parse_vec3_words(fields["end_vec"], line_no, "end_vec"))
    if "value_float" in fields:
        words.append(f32_to_u32(float(fields["value_float"])))
    b0 = words[0] & 0xFF
    b1 = (words[0] >> 8) & 0xFF
    b2 = (words[0] >> 16) & 0xFF
    is_play = sign_extend(words[1], 32) >= 0 or event_duration
    if is_play:
        if b0 & 0x02 and "end_vec" not in fields:
            if b0 & 0x01:
                require_fields(fields, {"color_start_vec"}, line_no, "background_auto_rate_anim")
                words.extend(parse_vec3_words(fields["color_start_vec"], line_no, "color_start_vec"))
            require_fields(fields, {"color_end_vec"}, line_no, "background_auto_rate_anim")
            words.extend(parse_vec3_words(fields["color_end_vec"], line_no, "color_end_vec"))
        if b0 & 0x40 and "value_float" not in fields:
            if b0 & 0x20:
                require_fields(fields, {"transparent_from"}, line_no, "background_auto_rate_anim")
                words.append(f32_to_u32(float(fields["transparent_from"])))
            require_fields(fields, {"transparent_to"}, line_no, "background_auto_rate_anim")
            words.append(f32_to_u32(float(fields["transparent_to"])))
        if b1 & 0x08 and "end_vec" not in fields:
            if b1 & 0x04:
                require_fields(fields, {"scale_start_vec"}, line_no, "background_auto_rate_anim")
                words.extend(parse_vec3_words(fields["scale_start_vec"], line_no, "scale_start_vec"))
            require_fields(fields, {"scale_end_vec"}, line_no, "background_auto_rate_anim")
            words.extend(parse_vec3_words(fields["scale_end_vec"], line_no, "scale_end_vec"))
        if b1 & 0x40:
            if b1 & 0x20:
                require_fields(fields, {"palette_from"}, line_no, "background_auto_rate_anim")
                words.append(f32_to_u32(float(fields["palette_from"])))
            require_fields(fields, {"palette_to", "palette_id"}, line_no, "background_auto_rate_anim")
            words.append(f32_to_u32(float(fields["palette_to"])))
            words.append(parse_hex_int(fields["palette_id"]) & 0xFFFFFFFF)
        if b2 & 0x02:
            if b2 & 0x01:
                require_fields(fields, {"visibility_from"}, line_no, "background_auto_rate_anim")
                words.append(f32_to_u32(float(fields["visibility_from"])))
            require_fields(fields, {"visibility_to"}, line_no, "background_auto_rate_anim")
            words.append(f32_to_u32(float(fields["visibility_to"])))
    if "trailing" in fields:
        words.extend(parse_word_list(fields["trailing"]))
    return arg, words

def decode_character_animation_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    cursor = 0
    # Option bits are presence flags for the optional payload words; only the
    # set ones are printed (the payload fields imply them on compile), and
    # character= implies explicit_char.
    fields = []
    if sub_anim_mode := arg >> 6:
        fields.append(f"sub_anim_mode={sub_anim_mode}")
    if explicit_char:
        if cursor >= len(words):
            fields.insert(0, f"explicit_char={explicit_char}")
            return fields, False
        fields.append(f"character=0x{words[cursor]:08X}")
        cursor += 1
    if cursor + 2 > len(words):
        fields.insert(0, f"explicit_char={explicit_char}")
        return fields, False

    anim_group = words[cursor]
    anim_word = words[cursor + 1]
    cursor += 2
    # Word1 is the animation reference (proven in Command_23 at 0x002F2548 and
    # GetAbstractionAnimationNumber at 0x002E53A0): low 24 bits = animation id
    # (0x26DE+n selects script-argument slot n), byte 3 = variant (0xFF = take
    # it from the slot). Word0 stays packed under anim_group.
    fields.append(f"anim_group=0x{anim_group:08X}")
    fields.append(f"anim_word=0x{anim_word & 0xFFFFFF:X}")
    if anim_word >> 24:
        fields.append(f"animation_variant=0x{(anim_word >> 24) & 0xFF:02X}")

    for bit, field_name in (
        (0x02, "optional_float0"),
        (0x04, "optional_float1"),
        (0x08, "optional_float2"),
        (0x10, "play_speed"),
    ):
        if arg & bit:
            if cursor >= len(words):
                return fields, False
            fields.append(f"{field_name}={format_f32(u32_to_f32(words[cursor]))}")
            cursor += 1
    if arg & 0x20:
        if cursor >= len(words):
            return fields, False
        fields.append(f"extra_anim_word=0x{words[cursor]:08X}")
        cursor += 1
    return fields, cursor == len(words)

def build_character_animation_words(fields: dict[str, str], line_no: int) -> list[int]:
    # Option bits default from payload-field presence; the explicit spellings
    # remain accepted.
    explicit_char = parse_hex_int(fields.get("explicit_char", "1" if "character" in fields else "0"))
    speed0 = parse_hex_int(fields.get("speed0", "1" if "optional_float0" in fields else "0"))
    speed1 = parse_hex_int(fields.get("speed1", "1" if "optional_float1" in fields else "0"))
    blend = parse_hex_int(fields.get("blend", "1" if "optional_float2" in fields else "0"))
    speed2 = parse_hex_int(fields.get("speed2", "1" if "play_speed" in fields else "0"))
    extra_word = parse_hex_int(fields.get("extra_word", "1" if "extra_anim_word" in fields else "0"))
    sub_anim_mode = parse_hex_int(fields.get("sub_anim_mode", "0"))
    if explicit_char not in (0, 1):
        raise ValueError(f"line {line_no}: character_animation explicit_char must be 0 or 1")
    if any(value not in (0, 1) for value in (speed0, speed1, blend, speed2, extra_word)):
        raise ValueError(f"line {line_no}: character_animation option flags must be 0 or 1")
    if not 0 <= sub_anim_mode <= 3:
        raise ValueError(f"line {line_no}: character_animation sub_anim_mode out of range")

    if "words" in fields:
        # Truncated decode: the words= tail carries the full raw payload and
        # any named payload fields on the line are informational duplicates.
        return parse_optional_word_list(fields)

    words: list[int] = []
    if explicit_char:
        require_fields(fields, {"character"}, line_no, "character_animation")
        words.append(parse_hex_int(fields["character"]) & 0xFFFFFFFF)
    require_fields(fields, {"anim_group", "anim_word"}, line_no, "character_animation")
    anim_group = parse_hex_int(fields["anim_group"]) & 0xFFFFFFFF
    anim_word = parse_hex_int(fields["anim_word"]) & 0xFFFFFFFF
    if "animation_variant" in fields:
        if anim_word >> 24:
            raise ValueError(f"line {line_no}: character_animation animation_variant conflicts with anim_word byte 3")
        anim_word |= (parse_hex_int(fields["animation_variant"]) & 0xFF) << 24
    words.extend([anim_group, anim_word])

    if "request_low16" in fields and parse_hex_int(fields["request_low16"]) != (anim_group & 0xFFFF):
        raise ValueError(f"line {line_no}: character_animation request_low16 does not match anim_group")
    if "request_low_byte" in fields and parse_hex_int(fields["request_low_byte"]) != (anim_group & 0xFF):
        raise ValueError(f"line {line_no}: character_animation request_low_byte does not match anim_group")
    if "request_flags" in fields and parse_hex_int(fields["request_flags"]) != ((anim_group >> 16) & 0xFF):
        raise ValueError(f"line {line_no}: character_animation request_flags does not match anim_group")
    if "anim_id" in fields and parse_hex_int(fields["anim_id"]) != (anim_word & 0xFFFF):
        raise ValueError(f"line {line_no}: character_animation anim_id does not match anim_word")
    if "anim_high" in fields and parse_hex_int(fields["anim_high"]) != ((anim_word >> 24) & 0xFF):
        raise ValueError(f"line {line_no}: character_animation anim_high does not match anim_word")

    for enabled, field_name in (
        (speed0, "optional_float0"),
        (speed1, "optional_float1"),
        (blend, "optional_float2"),
        (speed2, "play_speed"),
    ):
        if enabled:
            require_fields(fields, {field_name}, line_no, "character_animation")
            words.append(f32_to_u32(float(fields[field_name])))
        elif field_name in fields:
            raise ValueError(f"line {line_no}: character_animation {field_name} present but flag is clear")
    if extra_word:
        require_fields(fields, {"extra_anim_word"}, line_no, "character_animation")
        words.append(parse_hex_int(fields["extra_anim_word"]) & 0xFFFFFFFF)
    elif "extra_anim_word" in fields:
        raise ValueError(f"line {line_no}: character_animation extra_anim_word present but flag is clear")
    return words

def decode_rotate_option_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    cursor = 0
    fields = [f"explicit_char={explicit_char}"]
    if explicit_char:
        if cursor >= len(words):
            return fields, False
        fields.append(f"character=0x{words[cursor]:08X}")
        cursor += 1
    if cursor >= len(words):
        return fields, False

    control = words[cursor]
    cursor += 1
    mode = control & 0x0F
    option = (control >> 9) & 0x01
    vector_mask = (control >> 6) & 0x07
    duration = (control >> 16) & 0xFFFF
    duration_text = "-1" if duration == 0xFFFF else str(duration)
    action = ROTATE_OPTION_MODE_ACTIONS.get((option, mode), "unknown")
    # Every control bit has a named field; zero-valued option bits are
    # suppressed and the packed word itself is only printed when it carries
    # bits outside the decoded model (14-15) or the action is unknown.
    fields.append(f"mode={mode}")
    fields.append(f"action={action}")
    if action == "unknown":
        fields.append(f"option={option}")
    if (arg >> 1) & 0x01:
        fields.append(f"target_char_from_stream={(arg >> 1) & 0x01}")
    if (arg >> 2) & 0x03:
        fields.append(f"name_source={(arg >> 2) & 0x03}")
    if (control >> 4) & 0x03:
        fields.append(f"postprocess_mode={(control >> 4) & 0x03}")
    if (control >> 10) & 0x01:
        fields.append("position_offset=1")
    if (control >> 11) & 0x01:
        fields.append("posture_offset=1")
    if (control >> 13) & 0x01:
        fields.append("bit13=1")
    fields.append(f"duration={duration_text}")
    if (control >> 14) & 0x03 or action == "unknown":
        fields.append(f"vector_mask={vector_mask}")
        fields.append(f"control=0x{control:08X}")

    initial_fields, consumed = masked_vector_fields(vector_mask, words[cursor:])
    if consumed != bin(vector_mask).count("1"):
        return fields, False
    if initial_fields:
        fields.append(f"initial_vec={','.join(initial_fields)}")
    cursor += consumed

    if control & 0x1000:
        if cursor + 2 > len(words):
            return fields, False
        fields.append(
            "speed_values="
            f"{format_f32(u32_to_f32(words[cursor]))},{format_f32(u32_to_f32(words[cursor + 1]))}"
        )
        cursor += 2

    name_source = (arg >> 2) & 0x03
    target_from_stream = (arg >> 1) & 0x01

    def take_name_payload() -> bool:
        nonlocal cursor
        if name_source != 1:
            return True
        if cursor + 4 > len(words):
            return False
        fields.append(fixed_name_field("target_name_words", words[cursor:cursor + 4]))
        cursor += 4
        return True

    def take_position_offset() -> bool:
        nonlocal cursor
        if not (control & 0x400):
            return True
        if cursor + 3 > len(words):
            return False
        fields.append(f"position_offset_vec={format_vec3_words(words[cursor:cursor + 3])}")
        cursor += 3
        return True

    if not option:
        if mode == 0:
            if cursor + 3 > len(words):
                return fields, False
            fields.append(f"target_vector={format_vec3_words(words[cursor:cursor + 3])}")
            cursor += 3
        elif mode in (1, 2, 9, 10):
            if not take_name_payload() or not take_position_offset():
                return fields, False
            if mode in (1, 9) and target_from_stream:
                if cursor >= len(words):
                    return fields, False
                fields.append(f"target_character_pair=0x{words[cursor]:08X}")
                cursor += 1
        elif mode in (3, 11):
            if cursor >= len(words):
                return fields, False
            fields.append(f"stand=0x{words[cursor] & 0xFFFF:04X}")
            cursor += 1
            if not take_position_offset():
                return fields, False
        elif mode == 8:
            posture_fields, consumed = masked_vector_fields(vector_mask, words[cursor:])
            if consumed != bin(vector_mask).count("1"):
                return fields, False
            if posture_fields:
                fields.append(f"target_posture_vec={','.join(posture_fields)}")
            cursor += consumed
        elif mode in (4, 5, 6, 7, 12, 13, 14, 15):
            pass
        else:
            return fields, False
    else:
        if mode == 0:
            if cursor + 3 > len(words):
                return fields, False
            fields.append(f"target_vector={format_vec3_words(words[cursor:cursor + 3])}")
            cursor += 3
        elif mode in (1, 2):
            if not take_name_payload() or not take_position_offset():
                return fields, False
            if mode == 1 and target_from_stream:
                if cursor >= len(words):
                    return fields, False
                fields.append(f"target_character_pair=0x{words[cursor]:08X}")
                cursor += 1
        elif mode == 8:
            head_fields, consumed = masked_vector_fields(vector_mask, words[cursor:])
            if consumed != bin(vector_mask).count("1"):
                return fields, False
            if head_fields:
                fields.append(f"head_angle_add={','.join(head_fields)}")
            cursor += consumed
        elif mode == 15:
            pass
        else:
            return fields, False

    return fields, cursor == len(words)

ROTATE_ACTION_OPTIONS: dict[tuple[str, int], int] = {
    (action, mode): option for (option, mode), action in ROTATE_OPTION_MODE_ACTIONS.items()
}

def derive_rotate_option_control(fields: dict[str, str], line_no: int) -> int:
    """Rebuild the Command_2D control word from the named fields."""
    require_fields(fields, {"mode"}, line_no, "character_rotate_option")
    mode = parse_hex_int(fields["mode"])
    if "option" in fields:
        option = parse_hex_int(fields["option"])
    else:
        require_fields(fields, {"action"}, line_no, "character_rotate_option")
        option = ROTATE_ACTION_OPTIONS.get((fields["action"], mode))
        if option is None:
            raise ValueError(f"line {line_no}: character_rotate_option unknown action {fields['action']} for mode {mode}")
    if "vector_mask" in fields:
        vector_mask = parse_hex_int(fields["vector_mask"])
    else:
        vector_mask = 0
        for axis, bit in (("x", 1), ("y", 2), ("z", 4)):
            if f"{axis}:" in fields.get("initial_vec", ""):
                vector_mask |= bit
    postprocess = parse_hex_int(fields.get("postprocess_mode", "0"))
    position_offset = parse_hex_int(fields.get("position_offset", "0"))
    posture_offset = parse_hex_int(fields.get("posture_offset", "0"))
    speed_limit = parse_hex_int(fields.get("speed_limit", "1" if "speed_values" in fields else "0"))
    bit13 = parse_hex_int(fields.get("bit13", "0"))
    duration_text = fields.get("duration", fields.get("duration_text", "0"))
    duration = 0xFFFF if duration_text == "-1" else parse_hex_int(duration_text)
    if not 0 <= mode <= 0x0F or option not in (0, 1) or not 0 <= vector_mask <= 7 or not 0 <= postprocess <= 3:
        raise ValueError(f"line {line_no}: character_rotate_option fields out of range")
    if position_offset not in (0, 1) or posture_offset not in (0, 1) or speed_limit not in (0, 1) or bit13 not in (0, 1):
        raise ValueError(f"line {line_no}: character_rotate_option flag fields must be 0 or 1")
    if not 0 <= duration <= 0xFFFF:
        raise ValueError(f"line {line_no}: character_rotate_option duration out of range")
    return (
        mode
        | (postprocess << 4)
        | (vector_mask << 6)
        | (option << 9)
        | (position_offset << 10)
        | (posture_offset << 11)
        | (speed_limit << 12)
        | (bit13 << 13)
        | (duration << 16)
    )

def build_rotate_option_words(fields: dict[str, str], line_no: int) -> list[int]:
    require_fields(fields, {"explicit_char"}, line_no, "character_rotate_option")
    explicit_char = parse_hex_int(fields["explicit_char"])
    if explicit_char not in (0, 1):
        raise ValueError(f"line {line_no}: character_rotate_option explicit_char must be 0 or 1")
    if "control" in fields:
        control = parse_hex_int(fields["control"])
    else:
        control = derive_rotate_option_control(fields, line_no)
    if not 0 <= control <= 0xFFFFFFFF:
        raise ValueError(f"line {line_no}: character_rotate_option control out of range")

    words: list[int] = []
    if explicit_char:
        require_fields(fields, {"character"}, line_no, "character_rotate_option")
        character = parse_hex_int(fields["character"])
        if not 0 <= character <= 0xFFFFFFFF:
            raise ValueError(f"line {line_no}: character_rotate_option character out of range")
        words.append(character)
    words.append(control)

    mode = control & 0x0F
    option = (control >> 9) & 0x01
    vector_mask = (control >> 6) & 0x07
    append_masked_axis_words(words, vector_mask, fields, "initial_vec", line_no)

    if control & 0x1000:
        require_fields(fields, {"speed_values"}, line_no, "character_rotate_option")
        words.extend(parse_float_words(fields["speed_values"], 2, line_no, "speed_values"))

    name_source = parse_hex_int(fields.get("name_source", str((parse_hex_int(fields.get("arg", str(explicit_char))) >> 2) & 0x03)))
    target_from_stream = parse_hex_int(fields.get("target_char_from_stream", str((parse_hex_int(fields.get("arg", str(explicit_char))) >> 1) & 0x01)))

    def append_name_payload() -> None:
        if name_source == 1:
            if "target_name" not in fields:
                require_fields(fields, {"target_name_words"}, line_no, "character_rotate_option")
            name_words = (resolve_fixed_name_words(fields, "target_name_words", line_no) or [])
            if len(name_words) != 4:
                raise ValueError(f"line {line_no}: target_name_words expects four words")
            words.extend(name_words)

    def append_position_offset() -> None:
        if control & 0x400:
            require_fields(fields, {"position_offset_vec"}, line_no, "character_rotate_option")
            words.extend(parse_vec3_words(fields["position_offset_vec"], line_no, "position_offset_vec"))

    if not option:
        if mode == 0:
            require_fields(fields, {"target_vector"}, line_no, "character_rotate_option")
            words.extend(parse_vec3_words(fields["target_vector"], line_no, "target_vector"))
        elif mode in (1, 2, 9, 10):
            append_name_payload()
            append_position_offset()
            if mode in (1, 9) and target_from_stream:
                require_fields(fields, {"target_character_pair"}, line_no, "character_rotate_option")
                words.append(parse_hex_int(fields["target_character_pair"]) & 0xFFFFFFFF)
        elif mode in (3, 11):
            require_fields(fields, {"stand"}, line_no, "character_rotate_option")
            words.append(parse_hex_int(fields["stand"]) & 0xFFFFFFFF)
            append_position_offset()
        elif mode == 8:
            append_masked_axis_words(words, vector_mask, fields, "target_posture_vec", line_no)
        elif mode in (4, 5, 6, 7, 12, 13, 14, 15):
            pass
        else:
            raise ValueError(f"line {line_no}: unsupported rotate character_rotate_option mode {mode}")
    else:
        if mode == 0:
            require_fields(fields, {"target_vector"}, line_no, "character_rotate_option")
            words.extend(parse_vec3_words(fields["target_vector"], line_no, "target_vector"))
        elif mode in (1, 2):
            append_name_payload()
            append_position_offset()
            if mode == 1 and target_from_stream:
                require_fields(fields, {"target_character_pair"}, line_no, "character_rotate_option")
                words.append(parse_hex_int(fields["target_character_pair"]) & 0xFFFFFFFF)
        elif mode == 8:
            append_masked_axis_words(words, vector_mask, fields, "head_angle_add", line_no)
        elif mode == 15:
            pass
        else:
            raise ValueError(f"line {line_no}: unsupported option character_rotate_option mode {mode}")
    return words

def rgba8_text(word: int) -> str:
    return ",".join(str((word >> shift) & 0xFF) for shift in (0, 8, 16, 24))

def decode_camera_color_anim_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    mode = arg >> 4
    low = arg & 0x0F
    fields = [f"mode={mode}", f"low=0x{low:X}"]
    cursor = 0
    if 0 <= mode <= 7:
        has_first = 1 if low & 0x08 else 0
        fields.extend(
            [
                "kind=camera_move_etc2",
                f"target_slot={low}",
                f"has_first_float={has_first}",
            ]
        )
        if has_first:
            if cursor >= len(words):
                return fields, False
            fields.append(f"float0={format_f32(u32_to_f32(words[cursor]))}")
            cursor += 1
        else:
            fields.append("float0=-1")
        if cursor + 2 > len(words):
            return fields, False
        fields.append(f"float1={format_f32(u32_to_f32(words[cursor]))}")
        fields.append(f"float2={format_f32(u32_to_f32(words[cursor + 1]))}")
        cursor += 2
        return fields, cursor == len(words)
    if mode in (0x0E, 0x0F):
        has_start = 1 if low & 0x08 else 0
        fields.extend(
            [
                f"kind={'fog_color' if mode == 0x0E else 'ambient_color'}",
                f"blend_flag={(low >> 1) & 0x01}",
                f"has_start_color={has_start}",
            ]
        )
        if has_start:
            if cursor >= len(words):
                return fields, False
            fields.append(f"start_color_word=0x{words[cursor]:08X}")
            fields.append(f"start_rgba8={rgba8_text(words[cursor])}")
            cursor += 1
        if cursor + 2 > len(words):
            return fields, False
        fields.append(f"end_color_word=0x{words[cursor]:08X}")
        fields.append(f"end_rgba8={rgba8_text(words[cursor])}")
        fields.append(f"duration={format_f32(u32_to_f32(words[cursor + 1]))}")
        cursor += 2
        return fields, cursor == len(words)
    return fields, False

def build_camera_color_anim_words(fields: dict[str, str], line_no: int) -> list[int]:
    require_fields(fields, {"mode", "low"}, line_no, "camera_color_anim")
    mode = parse_hex_int(fields["mode"])
    low = parse_hex_int(fields["low"])
    if not 0 <= mode <= 0x0F or not 0 <= low <= 0x0F:
        raise ValueError(f"line {line_no}: camera_color_anim mode/low out of range")

    words: list[int] = []
    if 0 <= mode <= 7:
        if low & 0x08:
            require_fields(fields, {"float0"}, line_no, "camera_color_anim")
            words.append(f32_to_u32(float(fields["float0"])))
        require_fields(fields, {"float1", "float2"}, line_no, "camera_color_anim")
        words.extend([f32_to_u32(float(fields["float1"])), f32_to_u32(float(fields["float2"]))])
        return words

    if mode in (0x0E, 0x0F):
        if low & 0x08:
            require_fields(fields, {"start_color_word"}, line_no, "camera_color_anim")
            words.append(parse_hex_int(fields["start_color_word"]) & 0xFFFFFFFF)
        require_fields(fields, {"end_color_word", "duration"}, line_no, "camera_color_anim")
        words.append(parse_hex_int(fields["end_color_word"]) & 0xFFFFFFFF)
        words.append(f32_to_u32(float(fields["duration"])))
        return words

    raise ValueError(f"line {line_no}: unsupported camera_color_anim mode {mode}")

def decode_camera_move_etc_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    flag0 = arg & 0x01
    flag1 = (arg >> 1) & 0x01
    target_slot = (arg >> 2) & 0x03
    source = (arg >> 5) & 0x07
    cursor = 0
    fields = [
        f"flag0={flag0}",
        f"flag1={flag1}",
        f"target_slot={target_slot}",
        f"source={source}",
    ]
    if cursor >= len(words):
        return fields, False
    fields.append(f"duration={format_f32(u32_to_f32(words[cursor]))}")
    cursor += 1
    if target_slot:
        if cursor + 3 > len(words):
            return fields, False
        fields.append(f"target_offset_vec={format_vec3_words(words[cursor:cursor + 3])}")
        cursor += 3
    if source == 2:
        fields.append("source_kind=direct_vector")
        if cursor + 3 > len(words):
            return fields, False
        fields.append(f"source_vec={format_vec3_words(words[cursor:cursor + 3])}")
        cursor += 3
        return fields, cursor == len(words)
    if source == 0:
        fields.append("source_kind=packed_direct_point")
        if cursor >= len(words):
            return fields, False
        fields.append(f"source_point_word=0x{words[cursor]:08X}")
        cursor += 1
        return fields, cursor == len(words)
    if source == 3:
        fields.append("source_kind=character_or_name")
        if cursor >= len(words):
            return fields, False
        control = words[cursor]
        cursor += 1
        explicit_char = control & 0x01
        name_source = (control >> 1) & 0x03
        fields.append(f"source_control=0x{control:08X}")
        fields.append(f"source_explicit_char={explicit_char}")
        fields.append(f"source_name_source={name_source}")
        if explicit_char:
            if cursor >= len(words):
                return fields, False
            character_word = words[cursor]
            cursor += 1
            fields.append(f"source_character=0x{character_word & 0xFFFF:04X}")
            fields.append(f"source_character_type=0x{(character_word >> 16) & 0xFF:02X}")
            raw_byte3 = (character_word >> 24) & 0xFF
            if raw_byte3:
                fields.append(f"source_character_raw_byte3=0x{raw_byte3:02X}")
        if name_source == 1:
            if cursor + 4 > len(words):
                return fields, False
            name_words = words[cursor:cursor + 4]
            cursor += 4
            text = words_to_sjis_text(name_words)
            if text is not None:
                fields.append(f"source_name={json.dumps(text, ensure_ascii=False)}")
            else:
                fields.append(fixed_name_field("source_name_words", name_words))
        if cursor != len(words):
            fields.append(f"trailing={words_to_csv(words[cursor:])}")
            return fields, False
        return fields, True
    return fields, False

def build_camera_move_etc_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"flag0", "flag1", "target_slot", "source"}, line_no, "camera_move_etc")
    flag0 = parse_hex_int(fields["flag0"])
    flag1 = parse_hex_int(fields["flag1"])
    target_slot = parse_hex_int(fields["target_slot"])
    source = parse_hex_int(fields["source"])
    if flag0 not in (0, 1) or flag1 not in (0, 1) or not 0 <= target_slot <= 0x03 or not 0 <= source <= 0x07:
        raise ValueError(f"line {line_no}: camera_move_etc fields out of range")
    default_arg = flag0 | (flag1 << 1) | (target_slot << 2) | (source << 5)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if (arg & 0xEF) != default_arg:
        raise ValueError(f"line {line_no}: camera_move_etc arg does not match fields")
    if "words" in fields:
        return arg, parse_optional_word_list(fields)

    require_fields(fields, {"duration"}, line_no, "camera_move_etc")
    words = [f32_to_u32(float(fields["duration"]))]
    if target_slot:
        require_fields(fields, {"target_offset_vec"}, line_no, "camera_move_etc")
        words.extend(parse_vec3_words(fields["target_offset_vec"], line_no, "target_offset_vec"))
    elif "target_offset_vec" in fields:
        raise ValueError(f"line {line_no}: camera_move_etc target_offset_vec present but target_slot is zero")
    if source == 0:
        require_fields(fields, {"source_point_word"}, line_no, "camera_move_etc")
        words.append(parse_hex_int(fields["source_point_word"]) & 0xFFFFFFFF)
    elif source == 2:
        require_fields(fields, {"source_vec"}, line_no, "camera_move_etc")
        words.extend(parse_vec3_words(fields["source_vec"], line_no, "source_vec"))
    elif source == 3:
        if "source_control" in fields:
            source_control = parse_hex_int(fields["source_control"])
            explicit_char = source_control & 0x01
            name_source = (source_control >> 1) & 0x03
        else:
            require_fields(fields, {"source_explicit_char", "source_name_source"}, line_no, "camera_move_etc")
            explicit_char = parse_hex_int(fields["source_explicit_char"])
            name_source = parse_hex_int(fields["source_name_source"])
            if explicit_char not in (0, 1) or not 0 <= name_source <= 3:
                raise ValueError(f"line {line_no}: camera_move_etc source 3 fields out of range")
            source_control = explicit_char | (name_source << 1)
        words.append(source_control & 0xFFFFFFFF)
        if explicit_char:
            require_fields(fields, {"source_character"}, line_no, "camera_move_etc")
            character = parse_hex_int(fields["source_character"])
            character_type = parse_hex_int(fields.get("source_character_type", "0"))
            raw_byte3 = parse_hex_int(fields.get("source_character_raw_byte3", "0"))
            if not 0 <= character <= 0xFFFF or not 0 <= character_type <= 0xFF or not 0 <= raw_byte3 <= 0xFF:
                raise ValueError(f"line {line_no}: camera_move_etc source character fields out of range")
            words.append(character | (character_type << 16) | (raw_byte3 << 24))
        if name_source == 1:
            if "source_name" in fields:
                words.extend(fixed_name_words_from_text(fields, "source_name", line_no))
            else:
                if "source_name" not in fields:
                    require_fields(fields, {"source_name_words"}, line_no, "camera_move_etc")
                name_words = (resolve_fixed_name_words(fields, "source_name_words", line_no) or [])
                if len(name_words) != 4:
                    raise ValueError(f"line {line_no}: camera_move_etc source_name_words expects four words")
                words.extend(name_words)
        if "trailing" in fields:
            words.extend(parse_word_list(fields["trailing"]))
    else:
        raise ValueError(f"line {line_no}: camera_move_etc structured fields currently support source=0/2/3 only; use words= for other sources")
    return arg, words

def decode_camera_move_existing_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    move_mode = arg & 0x0F
    target_slot = arg >> 4
    fields = [f"move_mode={move_mode}", f"target_slot={target_slot}"]
    if len(words) % 2:
        fields.append(f"words={words_to_csv(words)}")
        return fields, False
    points = []
    for index in range(0, len(words), 2):
        word0 = words[index]
        word1 = words[index + 1]
        x = sign_extend(word0 & 0xFFFF, 16)
        y = sign_extend((word0 >> 16) & 0xFFFF, 16)
        z = sign_extend(word1 & 0xFFFF, 16)
        duration = (word1 >> 16) & 0xFFFF
        points.append(f"{x},{y},{z},{duration}")
    fields.append("points=" + "|".join(points))
    return fields, True

def parse_camera_existing_points(text: str, line_no: int) -> list[int]:
    words: list[int] = []
    if not text:
        return words
    for item in text.split("|"):
        values = item.split(",")
        if len(values) != 4:
            raise ValueError(f"line {line_no}: camera_move_existing point {item!r} expects x,y,z,duration")
        x, y, z = (int(values[i], 0) for i in range(3))
        duration = int(values[3], 0)
        if not -(1 << 15) <= x < (1 << 15) or not -(1 << 15) <= y < (1 << 15) or not -(1 << 15) <= z < (1 << 15):
            raise ValueError(f"line {line_no}: camera_move_existing point coordinates out of s16 range")
        if not 0 <= duration <= 0xFFFF:
            raise ValueError(f"line {line_no}: camera_move_existing point duration out of u16 range")
        words.append((x & 0xFFFF) | ((y & 0xFFFF) << 16))
        words.append((z & 0xFFFF) | (duration << 16))
    return words

def build_camera_move_existing_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"move_mode", "target_slot"}, line_no, "camera_move_existing")
    move_mode = parse_hex_int(fields["move_mode"])
    target_slot = parse_hex_int(fields["target_slot"])
    default_arg = move_mode | (target_slot << 4)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if not 0 <= move_mode <= 0x0F or not 0 <= target_slot <= 0x0F or arg != default_arg:
        raise ValueError(f"line {line_no}: camera_move_existing arg does not match fields")
    if "words" in fields and "points" not in fields:
        return arg, parse_optional_word_list(fields)
    require_fields(fields, {"points"}, line_no, "camera_move_existing")
    words = parse_camera_existing_points(fields["points"], line_no)
    if "words" in fields:
        words.extend(parse_optional_word_list(fields))
    return arg, words

def decode_camera_capture_target_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    target_mode = (arg >> 1) & 0x03
    posture_flag = (arg >> 5) & 0x01
    fields = [
        f"explicit_char={explicit_char}",
        f"target_mode={target_mode}",
        f"posture={posture_flag}",
    ]
    if not words:
        return fields, False
    cursor = 0
    control = words[cursor]
    cursor += 1
    fields.extend(
        [
            f"capture_control=0x{control:08X}",
            f"enable_capture={(control >> 0) & 0x01}",
            f"has_position_vec={(control >> 1) & 0x01}",
            f"position_flag={(control >> 2) & 0x01}",
            f"has_posture_vec={(control >> 3) & 0x01}",
            f"has_angle_pair={(control >> 4) & 0x01}",
            f"has_coeff_pair={(control >> 5) & 0x01}",
        ]
    )
    if not (control & 0x01):
        if cursor >= len(words):
            fields.append(f"words={words_to_csv(words[cursor:])}")
            return fields, False
        capture_rate_word = words[cursor]
        cursor += 1
        fields.append(f"capture_rate_word=0x{capture_rate_word:08X}")
        fields.append(f"capture_rate={format_f32(u32_to_f32(capture_rate_word))}")
        if capture_rate_word != 0:
            if explicit_char:
                if cursor >= len(words):
                    fields.append(f"words={words_to_csv(words[cursor:])}")
                    return fields, False
                fields.append(f"character=0x{words[cursor]:08X}")
                cursor += 1
            if target_mode == 1:
                if cursor + 4 > len(words):
                    fields.append(f"words={words_to_csv(words[cursor:])}")
                    return fields, False
                fields.append(fixed_name_field("target_name_words", words[cursor:cursor + 4]))
                cursor += 4
    if control & 0x02:
        if cursor + 3 > len(words):
            fields.append(f"words={words_to_csv(words[cursor:])}")
            return fields, False
        fields.append(f"position_vec={format_vec3_words(words[cursor:cursor + 3])}")
        cursor += 3
    if control & 0x08:
        if cursor + 3 > len(words):
            fields.append(f"words={words_to_csv(words[cursor:])}")
            return fields, False
        fields.append(f"posture_vec={format_vec3_words(words[cursor:cursor + 3])}")
        cursor += 3
    if control & 0x10:
        if cursor >= len(words):
            fields.append(f"words={words_to_csv(words[cursor:])}")
            return fields, False
        angle_pair = words[cursor]
        cursor += 1
        fields.append(f"angle_pair_word=0x{angle_pair:08X}")
        fields.append(f"angle_x_raw=0x{angle_pair & 0xFFFF:04X}")
        fields.append(f"angle_y_raw=0x{(angle_pair >> 16) & 0xFFFF:04X}")
    if control & 0x20:
        if cursor + 2 > len(words):
            fields.append(f"words={words_to_csv(words[cursor:])}")
            return fields, False
        fields.append(f"coeff_pair={format_f32(u32_to_f32(words[cursor]))},{format_f32(u32_to_f32(words[cursor + 1]))}")
        cursor += 2
    if cursor < len(words):
        fields.append(f"trailing={words_to_csv(words[cursor:])}")
    return fields, True

def build_camera_capture_target_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"explicit_char", "target_mode", "posture"}, line_no, "camera_capture_target")
    explicit_char = parse_hex_int(fields["explicit_char"])
    target_mode = parse_hex_int(fields["target_mode"])
    posture = parse_hex_int(fields["posture"])
    default_arg = explicit_char | (target_mode << 1) | (posture << 5)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if explicit_char not in (0, 1) or not 0 <= target_mode <= 0x03 or posture not in (0, 1):
        raise ValueError(f"line {line_no}: camera_capture_target fields out of range")
    if (arg & 0x27) != default_arg:
        raise ValueError(f"line {line_no}: camera_capture_target arg does not match fields")
    if "words" in fields and "capture_control" not in fields:
        return arg, parse_optional_word_list(fields)
    require_fields(fields, {"capture_control"}, line_no, "camera_capture_target")
    control = parse_hex_int(fields["capture_control"]) & 0xFFFFFFFF
    for field_name, bit in (
        ("enable_capture", 0),
        ("has_position_vec", 1),
        ("position_flag", 2),
        ("has_posture_vec", 3),
        ("has_angle_pair", 4),
        ("has_coeff_pair", 5),
    ):
        if field_name in fields and parse_hex_int(fields[field_name]) != ((control >> bit) & 0x01):
            raise ValueError(f"line {line_no}: camera_capture_target {field_name} does not match capture_control")
    words = [control]
    # A truncated command (declared word count ends mid-shape) decompiles to the
    # fields that fit plus a verbatim `words=` tail. When that tail marker is
    # present, a missing conditional field means "stream ended here" rather
    # than an authoring error, and the remaining payload comes from the tail.
    truncated = False

    def absent(name: str) -> bool:
        nonlocal truncated
        if name in fields:
            return False
        if "words" in fields:
            truncated = True
            return True
        require_fields(fields, {name}, line_no, "camera_capture_target")
        return True
    if not (control & 0x01) and not absent("capture_rate_word"):
        capture_rate_word = parse_hex_int(fields["capture_rate_word"]) & 0xFFFFFFFF
        if "capture_rate" in fields and f32_to_u32(float(fields["capture_rate"])) != capture_rate_word:
            raise ValueError(f"line {line_no}: camera_capture_target capture_rate does not match capture_rate_word")
        words.append(capture_rate_word)
        if capture_rate_word != 0:
            if explicit_char and not absent("character"):
                words.append(parse_hex_int(fields["character"]) & 0xFFFFFFFF)
            if not truncated and target_mode == 1 and not absent("target_name_words"):
                target_name_words = (resolve_fixed_name_words(fields, "target_name_words", line_no) or [])
                if len(target_name_words) != 4:
                    raise ValueError(f"line {line_no}: camera_capture_target target_name_words expects four words")
                words.extend(target_name_words)
    if not truncated and control & 0x02 and not absent("position_vec"):
        words.extend(parse_vec3_words(fields["position_vec"], line_no, "position_vec"))
    if not truncated and control & 0x08 and not absent("posture_vec"):
        words.extend(parse_vec3_words(fields["posture_vec"], line_no, "posture_vec"))
    if not truncated and control & 0x10 and not absent("angle_pair_word"):
        angle_pair = parse_hex_int(fields["angle_pair_word"]) & 0xFFFFFFFF
        if "angle_x_raw" in fields and parse_hex_int(fields["angle_x_raw"]) != (angle_pair & 0xFFFF):
            raise ValueError(f"line {line_no}: camera_capture_target angle_x_raw does not match angle_pair_word")
        if "angle_y_raw" in fields and parse_hex_int(fields["angle_y_raw"]) != ((angle_pair >> 16) & 0xFFFF):
            raise ValueError(f"line {line_no}: camera_capture_target angle_y_raw does not match angle_pair_word")
        words.append(angle_pair)
    if not truncated and control & 0x20 and not absent("coeff_pair"):
        words.extend(parse_float_words(fields["coeff_pair"], 2, line_no, "coeff_pair"))
    words.extend(parse_optional_word_list(fields, "trailing"))
    if "words" in fields:
        words.extend(parse_optional_word_list(fields))
    return arg, words

def decode_map_change_check_fields(arg: int, flags: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    has_map_word = (arg >> 1) & 0x01
    map_from_event = (arg >> 2) & 0x01
    change_map = 1 if flags & 0x80 else 0
    fields = [f"explicit_char={explicit_char}"]
    cursor = 0
    if explicit_char:
        if cursor >= len(words):
            fields.append(f"arg=0x{arg:02X}")
            fields.append(f"words={words_to_csv(words)}")
            return fields, False
        fields.append(f"character=0x{words[cursor]:08X}")
        cursor += 1
    if has_map_word:
        if cursor >= len(words):
            fields.append(f"arg=0x{arg:02X}")
            fields.append(f"words={words_to_csv(words)}")
            return fields, False
        map_word = words[cursor]
        cursor += 1
        raw_high = (map_word >> 16) & 0xFFFF
        if map_from_event:
            fields.append("map_source=event_value")
            fields.append(f"event_value=0x{map_word & 0xFFFF:04X}")
        else:
            fields.append("map_source=direct")
            fields.append(f"map=0x{map_word & 0xFFFF:04X}")
        if raw_high:
            fields.append(f"map_word_high=0x{raw_high:04X}")
    else:
        fields.append("map_source=none")
    fields.append(f"change_map={change_map}")
    if cursor < len(words):
        fields.append(f"trailing={words_to_csv(words[cursor:])}")
    return fields, True

def build_map_change_check_words(fields: dict[str, str], line_no: int) -> tuple[int, int, list[int]]:
    require_fields(fields, {"explicit_char"}, line_no, "map_change_check")
    explicit_char = parse_hex_int(fields["explicit_char"])
    if explicit_char not in (0, 1):
        raise ValueError(f"line {line_no}: map_change_check explicit_char must be 0 or 1")
    if "words" in fields and "map_source" not in fields:
        arg = parse_hex_int(fields.get("arg", str(explicit_char)))
        flags = parse_hex_int(fields.get("flags", "0x80"))
        if (arg & 0x01) != explicit_char:
            raise ValueError(f"line {line_no}: map_change_check arg does not match explicit_char")
        return arg, flags, parse_optional_word_list(fields)

    words: list[int] = []
    if explicit_char:
        require_fields(fields, {"character"}, line_no, "map_change_check")
        character = parse_hex_int(fields["character"])
        if not 0 <= character <= 0xFFFFFFFF:
            raise ValueError(f"line {line_no}: map_change_check character out of range")
        words.append(character)

    map_source = fields.get("map_source", "none")
    if map_source not in ("none", "direct", "event_value"):
        raise ValueError(f"line {line_no}: map_change_check map_source must be none, direct, or event_value")
    if map_source == "direct":
        require_fields(fields, {"map"}, line_no, "map_change_check")
        map_value = parse_hex_int(fields["map"])
        if not 0 <= map_value <= 0xFFFF:
            raise ValueError(f"line {line_no}: map_change_check map out of u16 range")
        map_word = map_value
    elif map_source == "event_value":
        require_fields(fields, {"event_value"}, line_no, "map_change_check")
        event_value = parse_hex_int(fields["event_value"])
        if not 0 <= event_value <= 0xFFFF:
            raise ValueError(f"line {line_no}: map_change_check event_value out of u16 range")
        map_word = event_value
    else:
        map_word = 0
    if map_source != "none":
        map_word_high = parse_hex_int(fields.get("map_word_high", "0"))
        if not 0 <= map_word_high <= 0xFFFF:
            raise ValueError(f"line {line_no}: map_change_check map_word_high out of u16 range")
        words.append(map_word | (map_word_high << 16))

    default_arg = explicit_char
    if map_source != "none":
        default_arg |= 0x02
    if map_source == "event_value":
        default_arg |= 0x04
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if arg != default_arg:
        raise ValueError(f"line {line_no}: map_change_check arg does not match structured fields")

    change_map = parse_hex_int(fields.get("change_map", "0"))
    if change_map not in (0, 1):
        raise ValueError(f"line {line_no}: map_change_check change_map must be 0 or 1")
    default_flags = 0x80 if change_map else 0
    flags = parse_hex_int(fields.get("flags", str(default_flags)))
    if (flags & 0x80) != default_flags:
        raise ValueError(f"line {line_no}: map_change_check flags do not match change_map")

    words.extend(parse_optional_word_list(fields, "trailing"))
    if "words" in fields:
        words.extend(parse_optional_word_list(fields))
    return arg, flags, words

def decode_camera_transform_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    mode = arg & 0x03
    cursor = 0
    fields = [
        f"mode={mode}",
        f"flag2={(arg >> 2) & 0x01}",
        f"flag3={(arg >> 3) & 0x01}",
        f"flag4={(arg >> 4) & 0x01}",
    ]
    if mode == 1:
        if cursor + 3 > len(words):
            return fields, False
        fields.append(f"initial_vec={format_vec3_words(words[cursor:cursor + 3])}")
        cursor += 3
    elif mode == 2:
        if cursor >= len(words):
            return fields, False
        control = words[cursor]
        cursor += 1
        control_flags = control & 0xFFFF
        duration = (control >> 16) & 0xFFFF
        fields.extend(
            [
                f"control=0x{control:08X}",
                f"control_flags=0x{control_flags:04X}",
                f"duration={duration}",
                f"rotate={(control_flags >> 0) & 0x01}",
                f"capture_before_rotate={(control_flags >> 1) & 0x01}",
                f"field20={(control_flags >> 2) & 0x01}",
                f"field3c={(control_flags >> 3) & 0x01}",
                f"distance_scale={(control_flags >> 4) & 0x01}",
            ]
        )
        if control_flags & 0x01:
            if cursor >= len(words):
                return fields, False
            fields.append(f"rotate_value={format_f32(u32_to_f32(words[cursor]))}")
            cursor += 1
        if control_flags & 0x04:
            if cursor >= len(words):
                return fields, False
            fields.append(f"field20_value={format_f32(u32_to_f32(words[cursor]))}")
            cursor += 1
        if control_flags & 0x08:
            if cursor >= len(words):
                return fields, False
            fields.append(f"field3c_value={format_f32(u32_to_f32(words[cursor]))}")
            cursor += 1
        if control_flags & 0x10:
            if cursor >= len(words):
                return fields, False
            fields.append(f"distance_value={format_f32(u32_to_f32(words[cursor]))}")
            cursor += 1
    elif mode not in (0, 3):
        return fields, False

    if arg & 0x04:
        if cursor + 3 > len(words):
            return fields, False
        fields.append(f"post_vec={format_vec3_words(words[cursor:cursor + 3])}")
        cursor += 3
    if arg & 0x08:
        if cursor >= len(words):
            return fields, False
        fields.append(f"field58_value={format_f32(u32_to_f32(words[cursor]))}")
        cursor += 1
    if arg & 0x10:
        if cursor + 3 > len(words):
            return fields, False
        fields.append(f"abs_vec={format_vec3_words(words[cursor:cursor + 3])}")
        cursor += 3
    return fields, cursor == len(words)

def build_camera_transform_words(fields: dict[str, str], line_no: int) -> list[int]:
    require_fields(fields, {"mode", "flag2", "flag3", "flag4"}, line_no, "camera_transform_param")
    mode = parse_hex_int(fields["mode"])
    flag2 = parse_hex_int(fields["flag2"])
    flag3 = parse_hex_int(fields["flag3"])
    flag4 = parse_hex_int(fields["flag4"])
    if not 0 <= mode <= 3 or any(value not in (0, 1) for value in (flag2, flag3, flag4)):
        raise ValueError(f"line {line_no}: camera_transform_param fields out of range")

    words: list[int] = []
    if mode == 1:
        require_fields(fields, {"initial_vec"}, line_no, "camera_transform_param")
        words.extend(parse_vec3_words(fields["initial_vec"], line_no, "initial_vec"))
    elif mode == 2:
        require_fields(fields, {"control"}, line_no, "camera_transform_param")
        control = parse_hex_int(fields["control"]) & 0xFFFFFFFF
        words.append(control)
        control_flags = control & 0xFFFF
        duration = (control >> 16) & 0xFFFF
        if "control_flags" in fields and parse_hex_int(fields["control_flags"]) != control_flags:
            raise ValueError(f"line {line_no}: camera_transform_param control_flags does not match control")
        if "duration" in fields and parse_hex_int(fields["duration"]) != duration:
            raise ValueError(f"line {line_no}: camera_transform_param duration does not match control")
        flag_aliases = {
            "rotate": 0x01,
            "capture_before_rotate": 0x02,
            "field20": 0x04,
            "field3c": 0x08,
            "distance_scale": 0x10,
        }
        for name, bit in flag_aliases.items():
            if name in fields and parse_hex_int(fields[name]) != (1 if control_flags & bit else 0):
                raise ValueError(f"line {line_no}: camera_transform_param {name} does not match control")
        for bit, field_name in (
            (0x01, "rotate_value"),
            (0x04, "field20_value"),
            (0x08, "field3c_value"),
            (0x10, "distance_value"),
        ):
            if control_flags & bit:
                require_fields(fields, {field_name}, line_no, "camera_transform_param")
                words.append(f32_to_u32(float(fields[field_name])))
            elif field_name in fields:
                raise ValueError(f"line {line_no}: camera_transform_param {field_name} present but control bit is clear")

    if flag2:
        require_fields(fields, {"post_vec"}, line_no, "camera_transform_param")
        words.extend(parse_vec3_words(fields["post_vec"], line_no, "post_vec"))
    elif "post_vec" in fields:
        raise ValueError(f"line {line_no}: camera_transform_param post_vec present but flag2 is clear")
    if flag3:
        require_fields(fields, {"field58_value"}, line_no, "camera_transform_param")
        words.append(f32_to_u32(float(fields["field58_value"])))
    elif "field58_value" in fields:
        raise ValueError(f"line {line_no}: camera_transform_param field58_value present but flag3 is clear")
    if flag4:
        require_fields(fields, {"abs_vec"}, line_no, "camera_transform_param")
        words.extend(parse_vec3_words(fields["abs_vec"], line_no, "abs_vec"))
    elif "abs_vec" in fields:
        raise ValueError(f"line {line_no}: camera_transform_param abs_vec present but flag4 is clear")
    return words

def decode_character_move_position_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    mode = (arg >> 1) & 0x03
    coord = (arg >> 3) & 0x03
    source = (arg >> 5) & 0x07
    cursor = 0
    fields = [
        f"explicit_char={explicit_char}",
        f"mode={mode}",
        f"coord={coord}",
        f"source={source}",
    ]
    if explicit_char:
        if cursor >= len(words):
            return fields, False
        fields.append(f"character=0x{words[cursor]:08X}")
        cursor += 1
    if cursor >= len(words):
        return fields, False
    control = words[cursor]
    cursor += 1
    duration = control & 0xFFFF
    control_high = (control >> 16) & 0xFFFF
    # Modeled bits get named fields (zeroes suppressed); the packed word is
    # only printed when the high half carries bits outside the model.
    fields.append(f"duration={duration}")
    if control_high & 0x01:
        fields.append("terrain_y=1")
    if (control_high >> 1) & 0x01:
        fields.append("move_flag1=1")
    if (control_high >> 2) & 0x01:
        fields.append("duration_as_speed=1")
    fields.append(f"action={'snap' if duration == 0 else 'move'}")
    if control_high >> 3:
        fields.append(f"control=0x{control:08X}")
        fields.append(f"control_high=0x{control_high:04X}")

    if source == 4:
        if cursor + 3 > len(words):
            return fields, False
        fields.append(f"inline_vec={format_vec3_words(words[cursor:cursor + 3])}")
        cursor += 3
    elif source == 3:
        if cursor >= len(words):
            return fields, False
        fields.append(f"stand=0x{words[cursor] & 0xFFFFFFFF:08X}")
        cursor += 1
    elif source in (0, 1):
        if cursor >= len(words):
            return fields, False
        target_word = words[cursor]
        cursor += 1
        fields.append(f"target_word=0x{target_word:08X}")
        if mode == 1:
            if cursor + 4 > len(words):
                return fields, False
            name_words = words[cursor:cursor + 4]
            cursor += 4
            text = words_to_sjis_text(name_words)
            if text is not None:
                fields.append(f"target_name={json.dumps(text, ensure_ascii=False)}")
            else:
                fields.append(fixed_name_field("target_name_words", name_words))
    else:
        return fields, False

    if coord:
        if cursor + 3 > len(words):
            return fields, False
        fields.append(f"offset_vec={format_vec3_words(words[cursor:cursor + 3])}")
        cursor += 3
    return fields, cursor == len(words)

def build_character_move_position_words(fields: dict[str, str], line_no: int) -> list[int]:
    require_fields(fields, {"explicit_char", "mode", "coord", "source"}, line_no, "character_move_position")
    if "words" in fields and "control" not in fields and "duration" not in fields:
        # Truncated decode: the words= tail carries the full raw payload.
        return parse_optional_word_list(fields)
    explicit_char = parse_hex_int(fields["explicit_char"])
    mode = parse_hex_int(fields["mode"])
    coord = parse_hex_int(fields["coord"])
    source = parse_hex_int(fields["source"])
    if explicit_char not in (0, 1) or not 0 <= mode <= 3 or not 0 <= coord <= 3 or not 0 <= source <= 7:
        raise ValueError(f"line {line_no}: character_move_position fields out of range")

    words: list[int] = []
    if explicit_char:
        require_fields(fields, {"character"}, line_no, "character_move_position")
        words.append(parse_hex_int(fields["character"]) & 0xFFFFFFFF)
    if "control" in fields:
        control = parse_hex_int(fields["control"]) & 0xFFFFFFFF
    else:
        # Derive the packed word from the named fields (friendly form).
        require_fields(fields, {"duration"}, line_no, "character_move_position")
        control = parse_hex_int(fields["duration"]) & 0xFFFF
        for name, bit in (("terrain_y", 16), ("move_flag1", 17), ("duration_as_speed", 18)):
            if name in fields:
                control |= (parse_hex_int(fields[name]) & 0x01) << bit
    words.append(control)
    duration = control & 0xFFFF
    control_high = (control >> 16) & 0xFFFF
    if "duration" in fields and parse_hex_int(fields["duration"]) != duration:
        raise ValueError(f"line {line_no}: character_move_position duration does not match control")
    if "control_high" in fields and parse_hex_int(fields["control_high"]) != control_high:
        raise ValueError(f"line {line_no}: character_move_position control_high does not match control")
    for name, bit in (("terrain_y", 0), ("move_flag1", 1), ("duration_as_speed", 2)):
        if name in fields and parse_hex_int(fields[name]) != ((control_high >> bit) & 0x01):
            raise ValueError(f"line {line_no}: character_move_position {name} does not match control")

    if source == 4:
        require_fields(fields, {"inline_vec"}, line_no, "character_move_position")
        words.extend(parse_vec3_words(fields["inline_vec"], line_no, "inline_vec"))
    elif source == 3:
        require_fields(fields, {"stand"}, line_no, "character_move_position")
        words.append(parse_hex_int(fields["stand"]) & 0xFFFFFFFF)
    elif source in (0, 1):
        require_fields(fields, {"target_word"}, line_no, "character_move_position")
        target_word = parse_hex_int(fields["target_word"]) & 0xFFFFFFFF
        if "target_number" in fields and parse_hex_int(fields["target_number"]) != (target_word & 0xFFFF):
            raise ValueError(f"line {line_no}: character_move_position target_number does not match target_word")
        if "target_variant" in fields and parse_hex_int(fields["target_variant"]) != ((target_word >> 16) & 0xFF):
            raise ValueError(f"line {line_no}: character_move_position target_variant does not match target_word")
        words.append(target_word)
        if mode == 1:
            if "target_name" in fields:
                words.extend(fixed_name_words_from_text(fields, "target_name", line_no))
            else:
                if "target_name" not in fields:
                    require_fields(fields, {"target_name_words"}, line_no, "character_move_position")
                name_words = (resolve_fixed_name_words(fields, "target_name_words", line_no) or [])
                if len(name_words) != 4:
                    raise ValueError(f"line {line_no}: character_move_position target_name_words expects four words")
                words.extend(name_words)
        elif "target_name" in fields or "target_name_words" in fields:
            raise ValueError(f"line {line_no}: character_move_position target_name is only consumed by mode 1")
    else:
        raise ValueError(f"line {line_no}: character_move_position source {source} still requires words= fallback")

    if coord:
        require_fields(fields, {"offset_vec"}, line_no, "character_move_position")
        words.extend(parse_vec3_words(fields["offset_vec"], line_no, "offset_vec"))
    elif "offset_vec" in fields:
        raise ValueError(f"line {line_no}: character_move_position offset_vec present but coord is zero")
    return words

def pack_s16_pair(low: int, high: int, line_no: int, field_name: str) -> int:
    if not -0x8000 <= low <= 0x7FFF or not -0x8000 <= high <= 0x7FFF:
        raise ValueError(f"line {line_no}: {field_name} values must fit signed 16-bit")
    return (low & 0xFFFF) | ((high & 0xFFFF) << 16)

def format_character_move_point_entry(words: list[int]) -> str:
    word0, word1, word2 = words
    x_or_stand = sign_extend(word0 & 0xFFFF, 16)
    y = sign_extend((word0 >> 16) & 0xFFFF, 16)
    z = sign_extend(word1 & 0xFFFF, 16)
    position_source_flags = (word1 >> 16) & 0xFFFF
    duration_ms = word2 & 0xFFFF
    control = (word2 >> 16) & 0xFFFF
    setpoint_arg0 = control & 0xFF
    setpoint_arg1 = (control >> 8) & 0x3F
    setpoint_arg2 = (control >> 14) & 0x03
    if position_source_flags & 0x01:
        return (
            f"stand:{word0 & 0xFFFF},position_source_flags=0x{position_source_flags:04X},duration_ms={duration_ms},"
            f"setpoint_arg0=0x{setpoint_arg0:02X},setpoint_arg1={setpoint_arg1},setpoint_arg2={setpoint_arg2}"
        )
    return (
        f"inline:{x_or_stand},{y},{z},position_source_flags=0x{position_source_flags:04X},duration_ms={duration_ms},"
        f"setpoint_arg0=0x{setpoint_arg0:02X},setpoint_arg1={setpoint_arg1},setpoint_arg2={setpoint_arg2}"
    )

def decode_character_move_points_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    fields = [f"explicit_char={explicit_char}", f"buffer_mode={(arg >> 1) & 0x0F}"]
    if len(words) < explicit_char or (len(words) - explicit_char) % 3:
        return fields, False
    cursor = 0
    if explicit_char:
        fields.append(f"character=0x{words[0]:08X}")
        cursor = 1
    point_count = (len(words) - cursor) // 3
    fields.append(f"point_count={point_count}")
    fields.append(
        "points="
        + "|".join(
            format_character_move_point_entry(words[index : index + 3])
            for index in range(cursor, len(words), 3)
        )
    )
    return fields, True

def parse_character_move_point_entry(entry: str, line_no: int) -> list[int]:
    if ":" not in entry:
        raise ValueError(f"line {line_no}: character_move_points point {entry!r} must start with inline: or stand:")
    kind, rest = entry.split(":", 1)
    parts = rest.split(",") if rest else []
    if kind == "inline":
        if len(parts) < 3:
            raise ValueError(f"line {line_no}: inline character_move_points point expects x,y,z")
        x = parse_hex_int(parts[0])
        y = parse_hex_int(parts[1])
        z = parse_hex_int(parts[2])
        options = parse_key_values(parts[3:], line_no, "character_move_points point")
        position_source_flags = parse_hex_int(options.get("position_source_flags", "0"))
        word0 = pack_s16_pair(x, y, line_no, "character_move_points inline vector")
        if not -0x8000 <= z <= 0x7FFF:
            raise ValueError(f"line {line_no}: character_move_points inline z must fit signed 16-bit")
        word1 = (z & 0xFFFF) | ((position_source_flags & 0xFFFF) << 16)
    elif kind == "stand":
        if not parts:
            raise ValueError(f"line {line_no}: stand character_move_points point expects stand id")
        stand = parse_hex_int(parts[0])
        options = parse_key_values(parts[1:], line_no, "character_move_points point")
        position_source_flags = parse_hex_int(options.get("position_source_flags", "1"))
        if not 0 <= stand <= 0xFFFF:
            raise ValueError(f"line {line_no}: character_move_points stand id must fit unsigned 16-bit")
        word0 = stand
        word1 = (position_source_flags & 0xFFFF) << 16
    else:
        raise ValueError(f"line {line_no}: character_move_points point kind {kind!r} must be inline or stand")

    duration_ms = parse_hex_int(options.get("duration_ms", "0"))
    setpoint_arg0 = parse_hex_int(options.get("setpoint_arg0", "0"))
    setpoint_arg1 = parse_hex_int(options.get("setpoint_arg1", "0"))
    setpoint_arg2 = parse_hex_int(options.get("setpoint_arg2", "0"))
    if not 0 <= duration_ms <= 0xFFFF:
        raise ValueError(f"line {line_no}: character_move_points duration_ms must fit unsigned 16-bit")
    if not 0 <= setpoint_arg0 <= 0xFF or not 0 <= setpoint_arg1 <= 0x3F or not 0 <= setpoint_arg2 <= 0x03:
        raise ValueError(f"line {line_no}: character_move_points SetPointData args out of packed range")
    control = setpoint_arg0 | (setpoint_arg1 << 8) | (setpoint_arg2 << 14)
    return [word0, word1, duration_ms | (control << 16)]

def build_character_move_points_words(fields: dict[str, str], line_no: int) -> list[int]:
    require_fields(fields, {"explicit_char", "buffer_mode", "points"}, line_no, "character_move_points")
    explicit_char = parse_hex_int(fields["explicit_char"])
    if explicit_char not in (0, 1):
        raise ValueError(f"line {line_no}: character_move_points explicit_char must be 0 or 1")
    words: list[int] = []
    if explicit_char:
        require_fields(fields, {"character"}, line_no, "character_move_points")
        words.append(parse_hex_int(fields["character"]) & 0xFFFFFFFF)
    entries = [entry for entry in fields["points"].split("|") if entry]
    if "point_count" in fields and parse_hex_int(fields["point_count"]) != len(entries):
        raise ValueError(f"line {line_no}: character_move_points point_count does not match points")
    for entry in entries:
        words.extend(parse_character_move_point_entry(entry, line_no))
    return words

def build_character_move_pause_words(fields: dict[str, str], line_no: int) -> list[int]:
    require_fields(fields, {"explicit_char"}, line_no, "character_move_pause")
    explicit_char = parse_hex_int(fields["explicit_char"])
    if explicit_char not in (0, 1):
        raise ValueError(f"line {line_no}: character_move_pause explicit_char must be 0 or 1")
    if "words" in fields:
        # The handler only reads a character word on the explicit form, but real
        # scripts carry extra declared words the dispatcher skips; keep them
        # verbatim so the command round-trips.
        return parse_optional_word_list(fields)
    if explicit_char:
        require_fields(fields, {"character"}, line_no, "character_move_pause")
        character = parse_hex_int(fields["character"])
        if not 0 <= character <= 0xFFFFFFFF:
            raise ValueError(f"line {line_no}: character_move_pause character out of range")
        return [character]
    return []

def decode_primitive_anim_slot_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    group = arg & 0x07
    delete_slot = (arg >> 6) & 0x01
    all_slots = (arg >> 7) & 0x01
    fields = [f"group={group}", f"all_slots={all_slots}", f"delete_slot={delete_slot}"]
    if all_slots:
        return fields, len(words) == 0
    if len(words) != 1:
        return fields, False
    word = words[0]
    start_slot = word & 0xFFFF
    count = (word >> 16) & 0xFFFF
    end_slot = 0x32 if count == 0 else min(start_slot + count, 0x32)
    fields.extend(
        [
            f"start_slot={start_slot}",
            f"count={count}",
            f"end_slot={end_slot}",
        ]
    )
    return fields, True

def build_primitive_anim_slot_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    if "delete" in fields and "all_slots" not in fields and "delete_slot" not in fields:
        fields = dict(fields)
        fields["all_slots"] = fields["delete"]
    require_fields(fields, {"group"}, line_no, "primitive_anim_slot")
    group = parse_hex_int(fields["group"])
    all_slots = parse_hex_int(fields.get("all_slots", "0"))
    delete_slot = parse_hex_int(fields.get("delete_slot", "0"))
    if not 0 <= group <= 0x07 or all_slots not in (0, 1) or delete_slot not in (0, 1):
        raise ValueError(f"line {line_no}: primitive_anim_slot group/all_slots/delete_slot out of range")
    default_arg = group | (delete_slot << 6) | (all_slots << 7)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if (arg & 0xC7) != default_arg:
        raise ValueError(f"line {line_no}: primitive_anim_slot arg does not match group/all_slots/delete_slot")
    if "words" in fields:
        return arg, parse_optional_word_list(fields)
    if all_slots:
        return arg, []
    require_fields(fields, {"start_slot", "count"}, line_no, "primitive_anim_slot")
    start_slot = parse_hex_int(fields["start_slot"])
    count = parse_hex_int(fields["count"])
    if not 0 <= start_slot <= 0xFFFF or not 0 <= count <= 0xFFFF:
        raise ValueError(f"line {line_no}: primitive_anim_slot start_slot/count out of range")
    return arg, [start_slot | (count << 16)]

def decode_primitive_play_paf_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    slot = arg & 0x03
    paf_mode = (arg >> 2) & 0x1F
    has_offset = (arg >> 7) & 0x01
    fields = [f"slot={slot}", f"paf_mode={paf_mode}", f"has_offset={has_offset}"]
    if len(words) < 1:
        return fields, False
    word0 = words[0]
    fields.extend(
        [
            f"paf_id=0x{word0 & 0xFFFF:04X}",
            f"sequence_index=0x{(word0 >> 16) & 0xFFFF:04X}",
        ]
    )
    cursor = 1
    if has_offset:
        if len(words) < 2:
            return fields, False
        offset_word = words[1]
        fields.append(f"offset={sign_extend(offset_word & 0xFFFF, 16)}")
        fields.append(f"offset_raw_high=0x{(offset_word >> 16) & 0xFFFF:04X}")
        cursor = 2
    return fields, cursor == len(words)

def build_primitive_play_paf_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"slot", "paf_mode"}, line_no, "primitive_play_paf")
    slot = parse_hex_int(fields["slot"])
    paf_mode = parse_hex_int(fields["paf_mode"])
    has_offset = parse_hex_int(fields.get("has_offset", "1" if "offset" in fields else "0"))
    if not 0 <= slot <= 0x03 or not 0 <= paf_mode <= 0x1F or has_offset not in (0, 1):
        raise ValueError(f"line {line_no}: primitive_play_paf slot/paf_mode/has_offset out of range")
    default_arg = slot | (paf_mode << 2) | (has_offset << 7)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if arg != default_arg:
        raise ValueError(f"line {line_no}: primitive_play_paf arg does not match slot/paf_mode/has_offset")
    if "words" in fields:
        return arg, parse_optional_word_list(fields)
    require_fields(fields, {"paf_id", "sequence_index"}, line_no, "primitive_play_paf")
    paf_id = parse_hex_int(fields["paf_id"])
    sequence_index = parse_hex_int(fields["sequence_index"])
    if not 0 <= paf_id <= 0xFFFF or not 0 <= sequence_index <= 0xFFFF:
        raise ValueError(f"line {line_no}: primitive_play_paf paf_id/sequence_index out of range")
    words = [paf_id | (sequence_index << 16)]
    if has_offset:
        require_fields(fields, {"offset"}, line_no, "primitive_play_paf")
        offset = parse_hex_int(fields["offset"])
        raw_high = parse_hex_int(fields.get("offset_raw_high", "0"))
        if not -0x8000 <= offset <= 0x7FFF or not 0 <= raw_high <= 0xFFFF:
            raise ValueError(f"line {line_no}: primitive_play_paf offset fields out of range")
        words.append((offset & 0xFFFF) | (raw_high << 16))
    elif "offset" in fields or "offset_raw_high" in fields:
        raise ValueError(f"line {line_no}: primitive_play_paf offset present but has_offset is 0")
    return arg, words

def decode_primitive_stop_paf_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    slot = arg & 0x03
    paf_mode = (arg >> 2) & 0x1F
    fields = [f"slot={slot}", f"paf_mode={paf_mode}"]
    if len(words) != 1:
        return fields, False
    word = words[0]
    raw_paf = word & 0xFFFF
    fields.append(f"paf_id={-1 if raw_paf == 0 else raw_paf}")
    fields.append(f"paf_raw=0x{raw_paf:04X}")
    fields.append(f"raw_high=0x{(word >> 16) & 0xFFFF:04X}")
    return fields, True

def build_primitive_stop_paf_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"slot", "paf_mode"}, line_no, "primitive_stop_paf")
    slot = parse_hex_int(fields["slot"])
    paf_mode = parse_hex_int(fields["paf_mode"])
    if not 0 <= slot <= 0x03 or not 0 <= paf_mode <= 0x1F:
        raise ValueError(f"line {line_no}: primitive_stop_paf slot/paf_mode out of range")
    default_arg = slot | (paf_mode << 2)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    # Bit 7 is outside the structured fields; an explicit arg= preserves it.
    if (arg & 0x7F) != default_arg:
        raise ValueError(f"line {line_no}: primitive_stop_paf arg does not match slot/paf_mode")
    if "words" in fields:
        return arg, parse_optional_word_list(fields)
    if "paf_raw" in fields:
        raw_paf = parse_hex_int(fields["paf_raw"])
    else:
        require_fields(fields, {"paf_id"}, line_no, "primitive_stop_paf")
        paf_id = parse_hex_int(fields["paf_id"])
        raw_paf = 0 if paf_id == -1 else paf_id
    raw_high = parse_hex_int(fields.get("raw_high", "0"))
    if not 0 <= raw_paf <= 0xFFFF or not 0 <= raw_high <= 0xFFFF:
        raise ValueError(f"line {line_no}: primitive_stop_paf paf/raw_high out of range")
    return arg, [raw_paf | (raw_high << 16)]

def decode_primitive_move_sprtg_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    slot = arg & 0x03
    move_kind = arg >> 4
    unused_arg_bits = (arg >> 2) & 0x03
    fields = [f"slot={slot}", f"move_kind={move_kind}", f"call_id={6 + move_kind}"]
    if unused_arg_bits:
        fields.append(f"unused_arg_bits=0x{unused_arg_bits:X}")
    if len(words) != 4 or move_kind > 4:
        fields.append(f"words={words_to_csv(words)}")
        return fields, False
    word0, word1, word2, word3 = words
    fields.extend(
        [
            f"primitive_index=0x{word0 & 0xFFFF:04X}",
            f"call_arg0=0x{(word0 >> 16) & 0xFFFF:04X}",
            f"float0={format_f32(u32_to_f32(word1))}",
            f"float1={format_f32(u32_to_f32(word2))}",
            f"call_arg3=0x{word3 & 0xFFFF:04X}",
            f"call_arg4={sign_extend((word3 >> 16) & 0xFFFF, 16)}",
        ]
    )
    return fields, True

def build_primitive_move_sprtg_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    if "slot_flags" in fields and "slot" not in fields and "move_kind" not in fields:
        slot_flags = parse_hex_int(fields["slot_flags"])
        if not 0 <= slot_flags <= 0x1F:
            raise ValueError(f"line {line_no}: primitive_move_sprtg slot_flags out of range")
        arg = parse_hex_int(fields.get("arg", str(slot_flags)))
        if (arg & 0x1F) != slot_flags:
            raise ValueError(f"line {line_no}: primitive_move_sprtg arg does not match slot_flags")
        return arg, parse_optional_word_list(fields)
    require_fields(fields, {"slot", "move_kind"}, line_no, "primitive_move_sprtg")
    slot = parse_hex_int(fields["slot"])
    move_kind = parse_hex_int(fields["move_kind"])
    unused_arg_bits = parse_hex_int(fields.get("unused_arg_bits", "0"))
    if not 0 <= slot <= 0x03 or not 0 <= move_kind <= 0x0F or not 0 <= unused_arg_bits <= 0x03:
        raise ValueError(f"line {line_no}: primitive_move_sprtg slot/move_kind/unused_arg_bits out of range")
    default_arg = slot | (unused_arg_bits << 2) | (move_kind << 4)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if arg != default_arg:
        raise ValueError(f"line {line_no}: primitive_move_sprtg arg does not match slot/move_kind/unused_arg_bits")
    if "words" in fields and "primitive_index" not in fields:
        return arg, parse_optional_word_list(fields)
    require_fields(fields, {"primitive_index", "call_arg0", "float0", "float1", "call_arg3", "call_arg4"}, line_no, "primitive_move_sprtg")
    primitive_index = parse_hex_int(fields["primitive_index"])
    call_arg0 = parse_hex_int(fields["call_arg0"])
    call_arg3 = parse_hex_int(fields["call_arg3"])
    call_arg4 = parse_hex_int(fields["call_arg4"])
    if not 0 <= primitive_index <= 0xFFFF or not 0 <= call_arg0 <= 0xFFFF or not 0 <= call_arg3 <= 0xFFFF:
        raise ValueError(f"line {line_no}: primitive_move_sprtg halfword fields out of range")
    if not -0x8000 <= call_arg4 <= 0x7FFF:
        raise ValueError(f"line {line_no}: primitive_move_sprtg call_arg4 out of signed 16-bit range")
    return arg, [
        primitive_index | (call_arg0 << 16),
        f32_to_u32(float(fields["float0"])),
        f32_to_u32(float(fields["float1"])),
        call_arg3 | ((call_arg4 & 0xFFFF) << 16),
    ]

def decode_position_vibration_vector_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    mode = arg & 0x0F
    target_slot = (arg >> 4) & 0x0F
    start_slot0 = arg & 0x01
    start_slot1 = (arg >> 1) & 0x01
    fields = [
        f"mode={mode}",
        f"target_slot={target_slot}",
        f"start_slot0={start_slot0}",
        f"start_slot1={start_slot1}",
    ]
    if len(words) != 8:
        fields.append(f"words={words_to_csv(words)}")
        return fields, False
    control = words[0]
    target_mode = (control >> 8) & 0x03
    fields.extend(
        [
            f"control=0x{control:08X}",
            f"control_low8=0x{control & 0xFF:02X}",
            f"control_target_mode={target_mode}",
            f"control_high=0x{control >> 16:04X}",
        ]
    )
    if target_mode == 0:
        fields.append(f"direct_float={format_f32(u32_to_f32(words[1]))}")
    else:
        fields.append(f"target_word=0x{words[1]:08X}")
    fields.append("params=" + ",".join(format_f32(u32_to_f32(word)) for word in words[2:8]))
    return fields, True

def build_position_vibration_vector_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"mode", "target_slot"}, line_no, "position_vibration_vector")
    mode = parse_hex_int(fields["mode"])
    target_slot = parse_hex_int(fields["target_slot"])
    if not 0 <= mode <= 0x0F or not 0 <= target_slot <= 0x0F:
        raise ValueError(f"line {line_no}: position_vibration_vector mode/target_slot out of range")
    default_arg = mode | (target_slot << 4)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if (arg & 0x0F) != mode or ((arg >> 4) & 0x0F) != target_slot:
        raise ValueError(f"line {line_no}: position_vibration_vector arg does not match fields")
    if "words" in fields and "control" not in fields:
        return arg, parse_optional_word_list(fields)

    require_fields(fields, {"control", "params"}, line_no, "position_vibration_vector")
    control = parse_hex_int(fields["control"]) & 0xFFFFFFFF
    target_mode = (control >> 8) & 0x03
    if "control_low8" in fields and parse_hex_int(fields["control_low8"]) != (control & 0xFF):
        raise ValueError(f"line {line_no}: position_vibration_vector control_low8 does not match control")
    if "control_target_mode" in fields and parse_hex_int(fields["control_target_mode"]) != target_mode:
        raise ValueError(f"line {line_no}: position_vibration_vector control_target_mode does not match control")
    if "control_high" in fields and parse_hex_int(fields["control_high"]) != (control >> 16):
        raise ValueError(f"line {line_no}: position_vibration_vector control_high does not match control")
    words = [control]
    if target_mode == 0:
        require_fields(fields, {"direct_float"}, line_no, "position_vibration_vector")
        words.append(f32_to_u32(float(fields["direct_float"])))
    else:
        require_fields(fields, {"target_word"}, line_no, "position_vibration_vector")
        words.append(parse_hex_int(fields["target_word"]) & 0xFFFFFFFF)
    params = parse_float_words(fields["params"], 6, line_no, "params")
    words.extend(params)
    return arg, words

def decode_talk_bustup_display_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    stream_float = (arg >> 2) & 0x01
    fields = [
        f"explicit_char={explicit_char}",
        f"flag1={(arg >> 1) & 0x01}",
        f"stream_float={stream_float}",
        f"mode={(arg >> 4) & 0x03}",
        f"upper_arg={(arg >> 6) & 0x03}",
    ]
    cursor = 0
    if explicit_char:
        if cursor >= len(words):
            return fields, False
        character_word = words[cursor]
        cursor += 1
        fields.append(f"character=0x{character_word:08X}")
        fields.append(f"character_number=0x{character_word & 0xFFFF:04X}")
        fields.append(f"character_variant=0x{(character_word >> 16) & 0xFF:02X}")
    if stream_float:
        if cursor >= len(words):
            return fields, False
        fields.append(f"display_time={format_f32(u32_to_f32(words[cursor]))}")
        cursor += 1
    return fields, cursor == len(words)

def build_talk_bustup_display_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(
        fields,
        {"explicit_char", "flag1", "stream_float", "mode", "upper_arg"},
        line_no,
        "talk_bustup_display",
    )
    explicit_char = parse_hex_int(fields["explicit_char"])
    flag1 = parse_hex_int(fields["flag1"])
    stream_float = parse_hex_int(fields["stream_float"])
    mode = parse_hex_int(fields["mode"])
    upper_arg = parse_hex_int(fields["upper_arg"])
    if (
        explicit_char not in (0, 1)
        or flag1 not in (0, 1)
        or stream_float not in (0, 1)
        or not 0 <= mode <= 0x03
        or not 0 <= upper_arg <= 0x03
    ):
        raise ValueError(f"line {line_no}: talk_bustup_display fields out of range")
    default_arg = explicit_char | (flag1 << 1) | (stream_float << 2) | (mode << 4) | (upper_arg << 6)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if (arg & 0xF7) != default_arg:
        raise ValueError(f"line {line_no}: talk_bustup_display arg does not match decoded fields")
    if "words" in fields:
        return arg, parse_optional_word_list(fields)
    words: list[int] = []
    if explicit_char:
        pass  # packed word is resolved from its named parts below
        character = resolve_packed_word(fields, line_no, "talk_bustup_display", "character", CHARACTER_VARIANT_SPECS)
        if not 0 <= character <= 0xFFFFFFFF:
            raise ValueError(f"line {line_no}: talk_bustup_display character out of range")
        words.append(character)
    elif "character" in fields:
        raise ValueError(f"line {line_no}: talk_bustup_display character present but explicit_char is 0")
    if stream_float:
        require_fields(fields, {"display_time"}, line_no, "talk_bustup_display")
        words.append(f32_to_u32(float(fields["display_time"])))
    elif "display_time" in fields:
        raise ValueError(f"line {line_no}: talk_bustup_display display_time present but stream_float is 0")
    return arg, words

SCENE_SAVE_ENV_MASK_BITS: tuple[tuple[int, str], ...] = (
    (0x001, "radi_time_enable"),
    (0x002, "map_animation_camera_halt"),
    (0x004, "ambient"),
    (0x008, "light_gf"),
    (0x010, "character_halt"),
    (0x020, "character_disp"),
    (0x040, "map_disp"),
    (0x080, "camera_system"),
    (0x100, "window_status"),
)

def format_scene_save_env_mask(mask: int) -> str:
    names = [name for bit, name in SCENE_SAVE_ENV_MASK_BITS if mask & bit]
    extra = mask & ~0x1FF
    if extra:
        names.append(f"extra_0x{extra:X}")
    return ",".join(names) if names else "none"

def decode_scene_save_env_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    pop = arg & 0x01
    fields = [f"pop={pop}"]
    if not words:
        return fields, False
    mask = words[0]
    fields.append(f"mask=0x{mask:08X}")
    fields.append(f"mask_bits={format_scene_save_env_mask(mask)}")
    if pop:
        return fields, len(words) == 1
    if len(words) < 2:
        return fields, False
    control = words[1]
    fields.extend(
        [
            f"control=0x{control:08X}",
            f"radi_time_enable={control & 0x03}",
            f"camera_halt_mode={(control >> 2) & 0x03}",
            f"map_animation_mode={(control >> 4) & 0x03}",
            f"light_arg0={(control >> 6) & 0x03}",
            f"light_arg1={(control >> 8) & 0x03}",
            f"character_halt_arg0={(control >> 10) & 0xFF}",
            f"character_halt_arg1={(control >> 18) & 0xFF}",
            f"character_disp={(control >> 26) & 0x03}",
            f"map_disp={(control >> 28) & 0x03}",
            f"window_status_mode={(control >> 30) & 0x03}",
        ]
    )
    return fields, len(words) == 2

def build_scene_save_env_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"pop"}, line_no, "scene_save_env")
    pop = parse_hex_int(fields["pop"])
    if pop not in (0, 1):
        raise ValueError(f"line {line_no}: scene_save_env pop must be 0 or 1")
    arg = parse_hex_int(fields.get("arg", str(pop)))
    if (arg & 0x01) != pop:
        raise ValueError(f"line {line_no}: scene_save_env arg does not match pop")
    if "words" in fields:
        return arg, parse_optional_word_list(fields)
    if "mask" not in fields:
        if any(name in fields for name in ("control", "mask_bits")):
            raise ValueError(f"line {line_no}: scene_save_env control/mask_bits present without mask")
        return arg, []
    require_fields(fields, {"mask"}, line_no, "scene_save_env")
    mask = parse_hex_int(fields["mask"])
    if not 0 <= mask <= 0xFFFFFFFF:
        raise ValueError(f"line {line_no}: scene_save_env mask out of range")
    if pop:
        if "control" in fields:
            raise ValueError(f"line {line_no}: scene_save_env pop form does not consume control")
        return arg, [mask]
    require_fields(fields, {"control"}, line_no, "scene_save_env")
    control = parse_hex_int(fields["control"])
    if not 0 <= control <= 0xFFFFFFFF:
        raise ValueError(f"line {line_no}: scene_save_env control out of range")
    return arg, [mask, control]

def decode_landscape_visibility_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    hidden = (arg >> 7) & 0x01
    fields = [
        f"slot={arg & 0x03}",
        f"visible={0 if hidden else 1}",
        f"hidden={hidden}",
    ]
    if len(words) != 1:
        return fields, False
    word = words[0]
    fields.append(f"group={word & 0xFF}")
    # Command_48 (0x002F41E0) reads the operand with `lbu`, so only its low byte
    # reaches CRadiLandscape::GetGroupHeader; the rest never leaves the stream.
    if (word >> 8) & 0xFFFFFF:
        fields.append(f"unused_high=0x{(word >> 8) & 0xFFFFFF:06X}")
    return fields, True

def build_landscape_visibility_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    if "uses_event" in fields and "slot" not in fields:
        # Backward-compatible spelling from the first conservative pass. The
        # MIPS later showed this bit is the hidden flag, not an event selector.
        fields = dict(fields)
        fields["hidden"] = fields["uses_event"]
        fields["slot"] = str(parse_hex_int(fields.get("arg", "0")) & 0x03)
    require_fields(fields, {"slot"}, line_no, "landscape_visibility")
    slot = parse_hex_int(fields["slot"])
    if "hidden" in fields:
        hidden = parse_hex_int(fields["hidden"])
        if "visible" in fields and parse_hex_int(fields["visible"]) != (0 if hidden else 1):
            raise ValueError(f"line {line_no}: landscape_visibility visible does not match hidden")
    else:
        hidden = 0 if parse_hex_int(fields.get("visible", "1")) else 1
    if not 0 <= slot <= 0x03 or hidden not in (0, 1):
        raise ValueError(f"line {line_no}: landscape_visibility slot/hidden out of range")
    default_arg = slot | (hidden << 7)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if (arg & 0x83) != default_arg:
        raise ValueError(f"line {line_no}: landscape_visibility arg does not match slot/hidden")
    if "words" in fields:
        return arg, parse_optional_word_list(fields)
    require_fields(fields, {"group"}, line_no, "landscape_visibility")
    group = parse_hex_int(fields["group"])
    raw_high = parse_hex_int(fields.get("unused_high", fields.get("raw_high", "0")))
    if not 0 <= group <= 0xFF or not 0 <= raw_high <= 0xFFFFFF:
        raise ValueError(f"line {line_no}: landscape_visibility group/raw_high out of range")
    return arg, [group | (raw_high << 8)]

def decode_background_runtime_field_fields(control: int, words: list[int]) -> tuple[list[str], bool]:
    # The control word is a presence bitmask for the three modeled fields;
    # print it only when it carries bits outside the model (Command_4c only
    # consumes bits 0x02/0x04/0x08).
    fields = [] if not control & ~0x0E else [f"control=0x{control:08X}"]
    index = 0
    complete = True
    if control & 0x02:
        if index + 2 <= len(words):
            value = words[index] | (words[index + 1] << 32)
            fields.append(f"field10_u64=0x{value:016X}")
            index += 2
        else:
            complete = False
    if control & 0x04:
        if index < len(words):
            fields.append(f"scaled_120_source={words[index]}")
            index += 1
        else:
            complete = False
    if control & 0x08:
        if index < len(words):
            fields.append(f"radi_180=0x{words[index] & 0xFFFF:04X}")
            fields.append(f"radi_180_raw_high=0x{(words[index] >> 16) & 0xFFFF:04X}")
            index += 1
        else:
            complete = False
    unknown_control = control & ~0x0E
    if unknown_control:
        fields.append(f"unknown_control=0x{unknown_control:08X}")
        complete = False
    if index != len(words):
        fields.append(f"trailing={words_to_csv(words[index:])}")
        complete = False
    if not complete and not unknown_control:
        fields.insert(0, f"control=0x{control:08X}")
    return fields, complete

def build_background_runtime_field_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    if "control" in fields:
        control = parse_hex_int(fields["control"])
    else:
        # Derive the presence bitmask from which payload fields are present.
        control = 0
        if "field10_u64" in fields:
            control |= 0x02
        if "scaled_120_source" in fields:
            control |= 0x04
        if "radi_180" in fields:
            control |= 0x08
    if "words" in fields:
        return control, parse_optional_word_list(fields)
    words: list[int] = []
    if control & 0x02:
        require_fields(fields, {"field10_u64"}, line_no, "background_runtime_field")
        value = parse_hex_int(fields["field10_u64"])
        if not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError(f"line {line_no}: background_runtime_field field10_u64 out of range")
        words.extend([value & 0xFFFFFFFF, (value >> 32) & 0xFFFFFFFF])
    if control & 0x04:
        require_fields(fields, {"scaled_120_source"}, line_no, "background_runtime_field")
        words.append(parse_hex_int(fields["scaled_120_source"]))
    if control & 0x08:
        require_fields(fields, {"radi_180"}, line_no, "background_runtime_field")
        radi_180 = parse_hex_int(fields["radi_180"])
        raw_high = parse_hex_int(fields.get("radi_180_raw_high", "0"))
        if not 0 <= radi_180 <= 0xFFFF or not 0 <= raw_high <= 0xFFFF:
            raise ValueError(f"line {line_no}: background_runtime_field radi_180/raw_high out of range")
        words.append(radi_180 | (raw_high << 16))
    unknown_control = control & ~0x0E
    if unknown_control:
        if "trailing" not in fields:
            raise ValueError(
                f"line {line_no}: background_runtime_field unknown control bits require words=... or trailing=..."
            )
        words.extend(parse_word_list(fields["trailing"]))
    return control, words

FADE_COLOR_PRESETS: dict[int, str] = {0: "black", 1: "white", 0xF: "current"}

FADE_COLOR_PRESET_CODES: dict[str, int] = {name: code for code, name in FADE_COLOR_PRESETS.items()}

def decode_fade_control_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    mode = (arg >> 4) & 0x0F
    fade_flags = arg & 0x0F
    # mode 0 and a zero low nibble are the defaults; the handler never reads
    # the low nibble at all.
    fields = []
    if mode:
        fields.append(f"mode={mode}")
    if fade_flags:
        fields.append(f"fade_flags=0x{fade_flags:X}")
    if not words:
        return fields, False
    word0 = words[0]
    fade_id = word0 & 0xFFFF
    control = (word0 >> 16) & 0xFFFF
    fields.append(f"duration={fade_id}")
    # Mode 0 color-fade control bits are fully traced (Command_67 at
    # 0x002F6C90, CRadiColorFade::SetParam/Run): bit0 = fade to the colour
    # (out) vs from it (in), bit1 = hold the overlay after finishing,
    # bit2 = explicit colour word follows, bits 8-11 = curve index,
    # bits 12-15 = colour preset. Bit4 is always set in scripts and never
    # read. Anything outside that model keeps the raw control= spelling.
    preset = (control >> 12) & 0x0F
    if (
        mode == 0
        and not control & 0x00E8
        and (control & 0x0004 or preset in FADE_COLOR_PRESETS)
    ):
        fields.append(f"direction={'out' if control & 0x0001 else 'in'}")
        if control & 0x0002:
            fields.append("hold=1")
        if not control & 0x0010:
            # Bit 4 is never read by the engine but is set in 99% of scripts;
            # only the rare cleared form is spelled out.
            fields.append("bit4=0")
        curve = (control >> 8) & 0x0F
        if curve:
            fields.append(f"curve={curve}")
        if not control & 0x0004:
            if preset:
                fields.append(f"color={FADE_COLOR_PRESETS[preset]}")
        elif preset:
            fields.append(f"color_preset_bits=0x{preset:X}")
    else:
        fields.append(f"control=0x{control:04X}")
    index = 1
    complete = True
    if mode == 1 and (control & 0x0001):
        if index < len(words):
            value_word = words[index]
            fields.append(f"dissolve_value=0x{value_word & 0xFFFF:04X}")
            raw_high = (value_word >> 16) & 0xFFFF
            if raw_high:
                fields.append(f"dissolve_raw_high=0x{raw_high:04X}")
            index += 1
        else:
            complete = False
    elif mode == 0 and (control & 0x0004):
        if index < len(words):
            fields.append(f"color_word=0x{words[index]:08X}")
            index += 1
        else:
            complete = False
    if index != len(words):
        fields.append(f"extra={words_to_csv(words[index:])}")
        complete = False
    return fields, complete

def build_fade_control_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    mode = parse_hex_int(fields.get("mode", "0"))
    fade_flags = parse_hex_int(fields.get("fade_flags", "0"))
    # `duration=` is the proven name (word0 low16 becomes the float frame
    # count in Command_67); `id=` is the legacy spelling of the same field.
    if "duration" in fields:
        fade_id = parse_hex_int(fields["duration"])
        if "id" in fields and parse_hex_int(fields["id"]) != fade_id:
            raise ValueError(f"line {line_no}: fade_control id does not match duration")
    elif "id" in fields:
        fade_id = parse_hex_int(fields["id"])
    else:
        fade_id = 60  # default fade length in frames when neither spelling is given
    if "control" in fields:
        control = parse_hex_int(fields["control"])
    else:
        # Derive the mode-0 control word from the named fade fields.
        require_fields(fields, {"direction"}, line_no, "fade_control")
        direction = fields["direction"]
        if direction not in ("out", "in"):
            raise ValueError(f"line {line_no}: fade_control direction must be 'out' or 'in'")
        control = 1 if direction == "out" else 0
        if "hold" in fields and parse_hex_int(fields["hold"]):
            control |= 0x0002
        if parse_hex_int(fields.get("bit4", "1")):
            control |= 0x0010
        curve = parse_hex_int(fields.get("curve", "0"))
        if not 0 <= curve <= 0x0F:
            raise ValueError(f"line {line_no}: fade_control curve out of range")
        control |= curve << 8
        if "color_word" in fields:
            control |= 0x0004
            control |= (parse_hex_int(fields.get("color_preset_bits", "0")) & 0x0F) << 12
        else:
            preset_name = fields.get("color", "black")
            preset = FADE_COLOR_PRESET_CODES.get(preset_name)
            if preset is None:
                raise ValueError(f"line {line_no}: fade_control color must be black, white, or current")
            control |= preset << 12
    arg = parse_hex_int(fields.get("arg", str((mode << 4) | fade_flags)))
    if not 0 <= mode <= 0x0F or not 0 <= fade_flags <= 0x0F or arg != ((mode << 4) | fade_flags):
        raise ValueError(f"line {line_no}: fade_control arg does not match mode/fade_flags")
    if not 0 <= fade_id <= 0xFFFF or not 0 <= control <= 0xFFFF:
        raise ValueError(f"line {line_no}: fade_control id/control out of range")
    words = [fade_id | (control << 16)]
    if mode == 1 and (control & 0x0001):
        if "extra" in fields and "dissolve_value" not in fields:
            words.extend(parse_word_list(fields["extra"]))
            return arg, words
        require_fields(fields, {"dissolve_value"}, line_no, "fade_control")
        value = parse_hex_int(fields["dissolve_value"])
        raw_high = parse_hex_int(fields.get("dissolve_raw_high", "0"))
        if not 0 <= value <= 0xFFFF or not 0 <= raw_high <= 0xFFFF:
            raise ValueError(f"line {line_no}: fade_control dissolve_value/raw_high out of range")
        words.append(value | (raw_high << 16))
    elif mode == 0 and (control & 0x0004):
        if "extra" in fields and "color_word" not in fields:
            words.extend(parse_word_list(fields["extra"]))
            return arg, words
        require_fields(fields, {"color_word"}, line_no, "fade_control")
        words.append(parse_hex_int(fields["color_word"]) & 0xFFFFFFFF)
    if "extra" in fields:
        words.extend(parse_word_list(fields["extra"]))
    return arg, words

def decode_global_visual_state_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    global_db = arg & 0x01
    object_visual = (arg >> 1) & 0x01
    fields = [f"global_db={global_db}", f"object_visual={object_visual}"]
    index = 0
    complete = True
    if global_db:
        if index + 2 <= len(words):
            fields.append(f"global_db_float={format_f32(u32_to_f32(words[index]))}")
            fields.append(f"global_db_reserved=0x{words[index + 1]:08X}")
            index += 2
        else:
            complete = False
    if object_visual:
        fields.append(f"object_visual_words={words_to_csv(words[index:])}")
        complete = False
        index = len(words)
    if index != len(words):
        fields.append(f"trailing={words_to_csv(words[index:])}")
        complete = False
    return fields, complete

def build_global_visual_state_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"global_db", "object_visual"}, line_no, "global_visual_state")
    global_db = parse_hex_int(fields["global_db"])
    object_visual = parse_hex_int(fields["object_visual"])
    default_arg = global_db | (object_visual << 1)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if global_db not in (0, 1) or object_visual not in (0, 1):
        raise ValueError(f"line {line_no}: global_visual_state fields out of range")
    if (arg & 0x03) != default_arg:
        raise ValueError(f"line {line_no}: global_visual_state arg does not match fields")
    if "words" in fields and not any(name in fields for name in ("global_db_float", "object_visual_words")):
        return arg, parse_optional_word_list(fields)
    words: list[int] = []
    if global_db:
        require_fields(fields, {"global_db_float", "global_db_reserved"}, line_no, "global_visual_state")
        words.append(f32_to_u32(float(fields["global_db_float"])))
        words.append(parse_hex_int(fields["global_db_reserved"]) & 0xFFFFFFFF)
    if object_visual:
        if "object_visual_words" in fields:
            words.extend(parse_word_list(fields["object_visual_words"]))
        elif "words" in fields:
            words.extend(parse_optional_word_list(fields))
        else:
            raise ValueError(f"line {line_no}: global_visual_state object_visual branch requires object_visual_words= or words=")
    if "trailing" in fields:
        words.extend(parse_word_list(fields["trailing"]))
    return arg, words

def decode_time_schedule_value_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    slot_select = (arg & 0x03) - 1
    operation = (arg >> 2) & 0x03
    validate_time = (arg >> 4) & 0x01
    fields = [
        f"mode=0x{arg:02X}",
        f"slot_select={slot_select}",
        f"operation={operation}",
        f"validate_time={validate_time}",
    ]
    if len(words) != 1:
        return fields, False
    word = words[0]

    def sentinel(value: int, max_value: int) -> int:
        return -1 if value == max_value else value

    fields.extend(
        [
            f"packed_time=0x{word:08X}",
            f"part0={sentinel(word & 0x3F, 0x3F)}",
            f"part1={sentinel((word >> 6) & 0x3F, 0x3F)}",
            f"part2={sentinel((word >> 12) & 0x3F, 0x3F)}",
            f"part3={sentinel((word >> 18) & 0x1F, 0x1F)}",
            f"part4={sentinel((word >> 23) & 0x1FF, 0x1FF)}",
        ]
    )
    return fields, True

def build_time_schedule_value_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    if "mode" in fields:
        arg = parse_hex_int(fields["mode"])
        slot_select = (arg & 0x03) - 1
        operation = (arg >> 2) & 0x03
        validate_time = (arg >> 4) & 0x01
    else:
        require_fields(fields, {"slot_select", "operation", "validate_time"}, line_no, "time_schedule_value")
        slot_select = parse_hex_int(fields["slot_select"])
        operation = parse_hex_int(fields["operation"])
        validate_time = parse_hex_int(fields["validate_time"])
        if not -1 <= slot_select <= 2 or not 0 <= operation <= 3 or validate_time not in (0, 1):
            raise ValueError(f"line {line_no}: time_schedule_value mode fields out of range")
        arg = (slot_select + 1) | (operation << 2) | (validate_time << 4)
    if not 0 <= arg <= 0xFF:
        raise ValueError(f"line {line_no}: time_schedule_value mode out of range")
    if (arg & 0x1F) != ((slot_select + 1) | (operation << 2) | (validate_time << 4)):
        raise ValueError(f"line {line_no}: time_schedule_value mode does not match slot_select/operation/validate_time")
    if "words" in fields and "packed_time" not in fields:
        return arg, parse_optional_word_list(fields)
    if "packed_time" in fields:
        return arg, [parse_hex_int(fields["packed_time"]) & 0xFFFFFFFF]

    require_fields(fields, {"part0", "part1", "part2", "part3", "part4"}, line_no, "time_schedule_value")

    def encode_part(name: str, bits: int) -> int:
        value = parse_hex_int(fields[name])
        max_value = (1 << bits) - 1
        if value == -1:
            return max_value
        if not 0 <= value < max_value:
            raise ValueError(f"line {line_no}: time_schedule_value {name} out of range")
        return value

    part0 = encode_part("part0", 6)
    part1 = encode_part("part1", 6)
    part2 = encode_part("part2", 6)
    part3 = encode_part("part3", 5)
    part4 = encode_part("part4", 9)
    return arg, [part0 | (part1 << 6) | (part2 << 12) | (part3 << 18) | (part4 << 23)]

def decode_battle_acquisition_setup_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    """Command_16 configures the next battle (proven via the step1 overlay):

    word0 low16 = battle map id (0xFFFF = keep current), high16 = battle BGM
    (CRadiScene::GameModeChangeProc_Battle / CBattleMain::State_LoadMapStart).
    The count bytes are CBtlFinishCheck condition preset ids (0-0x25 jump
    table in SetConditions) with one parameter word per active check; the
    check that ends the battle is recorded as slot<<8|id at 0x3B2820.
    tail0 low16 = battle script file (LoadScriptFile(id|0x8000), 0 -> 1).
    The manager word's low16 lands at app+0x84C where a nonzero value makes
    CBackGround::SettingMap skip the map-init script; its high bits and the
    final word (0x3B2824/26) are written but never read by any overlay.
    Option bit 1 enables a dropped-item remap in CBtlAcquisition::Liquidate.
    """
    fields = [f"arg=0x{arg:02X}"] if arg else []
    if len(words) < 6:
        fields.append(f"words={words_to_csv(words)}")
        return fields, False
    setup_word = words[0]
    control = words[1]
    count_word = words[2]
    counts = (count_word & 0xFF, (count_word >> 8) & 0xFF, (count_word >> 16) & 0xFF)
    count_word_high = (count_word >> 24) & 0xFF
    fields.append(f"battle_map=0x{setup_word & 0xFFFF:04X}")
    fields.append(f"battle_bgm=0x{(setup_word >> 16) & 0xFFFF:04X}")
    if control & ~0x3:
        fields.append(f"control=0x{control:08X}")
    else:
        if control & 0x01:
            fields.append("option_bit0=1")
        if control & 0x02:
            fields.append("option_bit1=1")
    cursor = 3
    value_count = sum(1 for value in counts if value)
    if cursor + value_count + 3 > len(words):
        fields.append(f"words={words_to_csv(words)}")
        return fields, False
    for index, count in enumerate(counts, start=1):
        if count:
            fields.append(f"finish_check{index}={count}")
            fields.append(f"finish_param{index}=0x{words[cursor]:08X}")
            cursor += 1
    if count_word_high:
        fields.append(f"count_word_high=0x{count_word_high:02X}")
    tail0_word = words[cursor]
    manager_word = words[cursor + 1]
    final_word = words[cursor + 2]
    cursor += 3
    fields.append(f"battle_script=0x{tail0_word & 0xFFFF:04X}")
    if tail0_word >> 16:
        fields.append(f"tail0_high16=0x{(tail0_word >> 16) & 0xFFFF:04X}")
    if manager_word & 0xFFFF:
        fields.append(f"battle_event_file=0x{manager_word & 0xFFFF:04X}")
    if (manager_word >> 16) & 0x7FFF:
        fields.append(f"battle_event_script=0x{(manager_word >> 16) & 0x7FFF:04X}")
    if (manager_word >> 31) & 0x01:
        fields.append("manager_flag31=1")
    if final_word & 0xFFFF:
        fields.append(f"unused_2824=0x{final_word & 0xFFFF:04X}")
    if (final_word >> 16) & 0xFFFF:
        fields.append(f"unused_2826=0x{(final_word >> 16) & 0xFFFF:04X}")
    if cursor < len(words):
        fields.append(f"trailing={words_to_csv(words[cursor:])}")
    return fields, True

def build_battle_acquisition_setup_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    arg = parse_hex_int(fields.get("arg", "0"))
    named = {"battle_map", "setup_low16", "battle_bgm", "battle_script", "tail0_word"}
    if "words" in fields and not (named & set(fields)):
        return arg, parse_optional_word_list(fields)

    def field2(new_name: str, old_name: str, default: str | None = None) -> int:
        text = fields.get(new_name, fields.get(old_name, default))
        if text is None:
            require_fields(fields, {new_name}, line_no, "battle_acquisition_setup")
        return parse_hex_int(text)

    def field3(new_name: str, *older: str, default: str | None = None) -> int:
        """Like field2 but with more than one superseded spelling.

        `suppress_map_init` and `unused_84e` were named before the pair was
        traced; scripts written with them still compile.
        """
        for name in (new_name, *older[:-1]):
            if name in fields:
                return parse_hex_int(fields[name])
        text = older[-1] if older else default
        if text is None:
            require_fields(fields, {new_name}, line_no, "battle_acquisition_setup")
        return parse_hex_int(text)

    battle_map = field2("battle_map", "setup_low16")
    battle_bgm = field2("battle_bgm", "setup_high16")
    if "control" in fields:
        control = parse_hex_int(fields["control"])
    else:
        control = (field2("option_bit0", "control_bit0", "0") & 0x01) | (
            (field2("option_bit1", "control_bit1", "0") & 0x01) << 1
        )
    if not 0 <= battle_map <= 0xFFFF or not 0 <= battle_bgm <= 0xFFFF or not 0 <= control <= 0xFFFFFFFF:
        raise ValueError(f"line {line_no}: battle_acquisition_setup map/bgm/control fields out of range")

    counts: list[int] = []
    params: list[int] = []
    if "count_values" in fields or "count0" in fields:
        # Legacy spelling: three count bytes plus a csv of parameter words.
        counts = [field2("count0", "count0", "0"), field2("count1", "count1", "0"), field2("count2", "count2", "0")]
        count_values = parse_word_list(fields.get("count_values", ""))
        if len(count_values) != sum(1 for value in counts if value):
            raise ValueError(f"line {line_no}: battle_acquisition_setup count_values length does not match nonzero count bytes")
        params = list(count_values)
    else:
        pending = []
        for index in range(1, 4):
            count = parse_hex_int(fields.get(f"finish_check{index}", "0"))
            counts.append(count)
            if count:
                require_fields(fields, {f"finish_param{index}"}, line_no, "battle_acquisition_setup")
                pending.append(parse_hex_int(fields[f"finish_param{index}"]) & 0xFFFFFFFF)
            elif f"finish_param{index}" in fields:
                raise ValueError(f"line {line_no}: battle_acquisition_setup finish_param{index} present but finish_check{index} is zero")
        params = pending
    count_word_high = parse_hex_int(fields.get("count_word_high", "0"))
    if not all(0 <= value <= 0xFF for value in (*counts, count_word_high)):
        raise ValueError(f"line {line_no}: battle_acquisition_setup count byte fields out of range")

    if "tail0_word" in fields:
        tail0_word = parse_hex_int(fields["tail0_word"]) & 0xFFFFFFFF
    else:
        tail0_word = (field2("battle_script", "tail0_low16") & 0xFFFF) | (
            (parse_hex_int(fields.get("tail0_high16", "0")) & 0xFFFF) << 16
        )
    if "tail0_low16" in fields and parse_hex_int(fields["tail0_low16"]) != (tail0_word & 0xFFFF):
        raise ValueError(f"line {line_no}: battle_acquisition_setup tail0_low16 does not match tail0_word")
    manager_low16 = field3("battle_event_file", "suppress_map_init", "manager_low16", "0")
    manager_high15 = field3("battle_event_script", "unused_84e", "manager_high15", "0")
    manager_flag31 = parse_hex_int(fields.get("manager_flag31", "0"))
    final_low16 = field2("unused_2824", "final_low16", "0")
    final_high16 = field2("unused_2826", "final_high16", "0")
    if not 0 <= manager_low16 <= 0xFFFF or not 0 <= manager_high15 <= 0x7FFF or manager_flag31 not in (0, 1):
        raise ValueError(f"line {line_no}: battle_acquisition_setup manager fields out of range")
    if not 0 <= final_low16 <= 0xFFFF or not 0 <= final_high16 <= 0xFFFF:
        raise ValueError(f"line {line_no}: battle_acquisition_setup final fields out of range")
    words = [
        battle_map | (battle_bgm << 16),
        control & 0xFFFFFFFF,
        counts[0] | (counts[1] << 8) | (counts[2] << 16) | (count_word_high << 24),
        *params,
        tail0_word,
        manager_low16 | (manager_high15 << 16) | (manager_flag31 << 31),
        final_low16 | (final_high16 << 16),
    ]
    words.extend(parse_optional_word_list(fields, "trailing"))
    if "words" in fields:
        words.extend(parse_optional_word_list(fields))
    return arg, words

def decode_stand_context_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    # Argument bit 0 is the gate for the context value: Command_1c (0x002E8310)
    # takes the operand's low bits and writes them into bits 0-9 of the global
    # halfword at +0x84A, preserving bits 10-15. Bit 1 gates the stand-position
    # lookup that uses the operand's high half.
    context = arg & 0x01
    stand = (arg >> 1) & 0x01
    position = (arg >> 2) & 0x01
    posture = (arg >> 3) & 0x01
    fields = [
        f"context={context}",
        f"stand={stand}",
        f"position={position}",
        f"posture={posture}",
    ]
    cursor = 0
    if arg & 0x03:
        if cursor >= len(words):
            fields.append(f"words={words_to_csv(words)}")
            return fields, False
        stand_word = words[cursor]
        cursor += 1
        fields.extend(
            [
                f"stand_word=0x{stand_word:08X}",
                f"context_low10=0x{stand_word & 0x03FF:03X}",
                f"stand_position=0x{(stand_word >> 16) & 0xFFFF:04X}",
            ]
        )
    if position:
        if cursor + 3 > len(words):
            fields.append(f"words={words_to_csv(words)}")
            return fields, False
        fields.append(f"position_vec_words={words_to_csv(words[cursor:cursor + 3])}")
        cursor += 3
    if posture:
        if cursor + 3 > len(words):
            fields.append(f"words={words_to_csv(words)}")
            return fields, False
        fields.append(f"posture_vec_words={words_to_csv(words[cursor:cursor + 3])}")
        cursor += 3
    if cursor < len(words):
        fields.append(f"trailing={words_to_csv(words[cursor:])}")
    return fields, True

def build_stand_context_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"stand", "position", "posture"}, line_no, "stand_context")
    if "context" not in fields and "field0" not in fields:
        require_fields(fields, {"context"}, line_no, "stand_context")
    field0 = parse_hex_int(fields.get("context", fields.get("field0", "0")))
    stand = parse_hex_int(fields["stand"])
    position = parse_hex_int(fields["position"])
    posture = parse_hex_int(fields["posture"])
    if any(value not in (0, 1) for value in (field0, stand, position, posture)):
        raise ValueError(f"line {line_no}: stand_context selector fields must be 0 or 1")
    default_arg = field0 | (stand << 1) | (position << 2) | (posture << 3)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if (arg & 0x0F) != default_arg:
        raise ValueError(f"line {line_no}: stand_context arg does not match selector fields")
    if "words" in fields and "stand_word" not in fields and "position_vec_words" not in fields and "posture_vec_words" not in fields:
        return arg, parse_optional_word_list(fields)

    words: list[int] = []
    if arg & 0x03:
        if "stand_word" in fields:
            stand_word = parse_hex_int(fields["stand_word"]) & 0xFFFFFFFF
        else:
            require_fields(fields, {"context_low10", "stand_position"}, line_no, "stand_context")
            context_low10 = parse_hex_int(fields["context_low10"])
            stand_position = parse_hex_int(fields["stand_position"])
            if not 0 <= context_low10 <= 0x03FF or not 0 <= stand_position <= 0xFFFF:
                raise ValueError(f"line {line_no}: stand_context stand fields out of range")
            stand_word = context_low10 | (stand_position << 16)
        if "context_low10" in fields and parse_hex_int(fields["context_low10"]) != (stand_word & 0x03FF):
            raise ValueError(f"line {line_no}: stand_context context_low10 does not match stand_word")
        if "stand_position" in fields and parse_hex_int(fields["stand_position"]) != ((stand_word >> 16) & 0xFFFF):
            raise ValueError(f"line {line_no}: stand_context stand_position does not match stand_word")
        words.append(stand_word)
    if position:
        require_fields(fields, {"position_vec_words"}, line_no, "stand_context")
        position_words = parse_word_list(fields["position_vec_words"])
        if len(position_words) != 3:
            raise ValueError(f"line {line_no}: stand_context position_vec_words expects three words")
        words.extend(word & 0xFFFFFFFF for word in position_words)
    elif "position_vec_words" in fields:
        raise ValueError(f"line {line_no}: stand_context position_vec_words present but position flag is clear")
    if posture:
        require_fields(fields, {"posture_vec_words"}, line_no, "stand_context")
        posture_words = parse_word_list(fields["posture_vec_words"])
        if len(posture_words) != 3:
            raise ValueError(f"line {line_no}: stand_context posture_vec_words expects three words")
        words.extend(word & 0xFFFFFFFF for word in posture_words)
    elif "posture_vec_words" in fields:
        raise ValueError(f"line {line_no}: stand_context posture_vec_words present but posture flag is clear")
    words.extend(parse_optional_word_list(fields, "trailing"))
    return arg, words

def format_clock_component(value: int, keep_sentinel: int) -> str:
    """Radiata clock fields use an all-ones sentinel for "keep current"."""
    return "keep" if value == keep_sentinel else str(value)

def decode_script_defaults_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    default_char = arg & 0x01
    default_object = (arg >> 1) & 0x01
    event_char = (arg >> 7) & 0x01
    fields = [
        f"default_char={default_char}",
        f"default_object={default_object}",
        f"event_char={event_char}",
    ]
    cursor = 0
    if default_char:
        if cursor >= len(words):
            fields.append(f"words={words_to_csv(words)}")
            return fields, False
        char_word = words[cursor]
        cursor += 1
        fields.append(f"{'event_value' if event_char else 'character'}=0x{char_word & 0xFFFF:04X}")
        if (char_word >> 16) & 0xFF:
            fields.append(f"character_variant=0x{(char_word >> 16) & 0xFF:02X}")
        if (char_word >> 24) & 0xFF:
            fields.append(f"character_raw_high=0x{(char_word >> 24) & 0xFF:02X}")
    if default_object:
        if cursor >= len(words):
            fields.append(f"words={words_to_csv(words)}")
            return fields, False
        object_words = words[cursor:]
        fields.append(f"object_name_null={1 if object_words[0] == 0 else 0}")
        fields.append(fixed_name_field("object_name_words", object_words))
        cursor = len(words)
    if cursor < len(words):
        fields.append(f"trailing={words_to_csv(words[cursor:])}")
    return fields, True

def build_script_defaults_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"default_char", "default_object", "event_char"}, line_no, "script_defaults")
    default_char = parse_hex_int(fields["default_char"])
    default_object = parse_hex_int(fields["default_object"])
    event_char = parse_hex_int(fields["event_char"])
    if default_char not in (0, 1) or default_object not in (0, 1) or event_char not in (0, 1):
        raise ValueError(f"line {line_no}: script_defaults fields must be 0 or 1")
    default_arg = default_char | (default_object << 1) | (event_char << 7)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if (arg & 0x83) != default_arg:
        raise ValueError(f"line {line_no}: script_defaults arg does not match fields")
    if "words" in fields and "char_word" not in fields and "object_name_words" not in fields:
        return arg, parse_optional_word_list(fields)

    words: list[int] = []
    if default_char:
        if "char_word" in fields:
            char_word = parse_hex_int(fields["char_word"]) & 0xFFFFFFFF
        else:
            key = "event_value" if event_char else "character"
            require_fields(fields, {key}, line_no, "script_defaults")
            character = parse_hex_int(fields[key])
            character_variant = parse_hex_int(fields.get("character_variant", "0"))
            character_raw_high = parse_hex_int(fields.get("character_raw_high", "0"))
            if not 0 <= character <= 0xFFFF or not 0 <= character_variant <= 0xFF or not 0 <= character_raw_high <= 0xFF:
                raise ValueError(f"line {line_no}: script_defaults character fields out of range")
            char_word = character | (character_variant << 16) | (character_raw_high << 24)
        if "character" in fields and not event_char and parse_hex_int(fields["character"]) != (char_word & 0xFFFF):
            raise ValueError(f"line {line_no}: script_defaults character does not match char_word")
        if "event_value" in fields and event_char and parse_hex_int(fields["event_value"]) != (char_word & 0xFFFF):
            raise ValueError(f"line {line_no}: script_defaults event_value does not match char_word")
        if "character_variant" in fields and parse_hex_int(fields["character_variant"]) != ((char_word >> 16) & 0xFF):
            raise ValueError(f"line {line_no}: script_defaults character_variant does not match char_word")
        if "character_raw_high" in fields and parse_hex_int(fields["character_raw_high"]) != ((char_word >> 24) & 0xFF):
            raise ValueError(f"line {line_no}: script_defaults character_raw_high does not match char_word")
        words.append(char_word)
    elif "char_word" in fields or "character" in fields or "event_value" in fields:
        raise ValueError(f"line {line_no}: script_defaults character fields present but default_char is clear")

    if default_object:
        if "object_name" not in fields:
            require_fields(fields, {"object_name_words"}, line_no, "script_defaults")
        object_words = (resolve_fixed_name_words(fields, "object_name_words", line_no) or [])
        if not object_words:
            raise ValueError(f"line {line_no}: script_defaults object_name_words cannot be empty when default_object is set")
        if "object_name_null" in fields and parse_hex_int(fields["object_name_null"]) != (1 if object_words[0] == 0 else 0):
            raise ValueError(f"line {line_no}: script_defaults object_name_null does not match object_name_words")
        words.extend(word & 0xFFFFFFFF for word in object_words)
    elif "object_name_words" in fields:
        raise ValueError(f"line {line_no}: script_defaults object_name_words present but default_object is clear")
    words.extend(parse_optional_word_list(fields, "trailing"))
    return arg, words

def decode_camera_mode_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    mode = arg & 0x0F
    flag4 = (arg >> 4) & 0x01
    flag5 = (arg >> 5) & 0x01
    flag6 = (arg >> 6) & 0x01
    flag7 = (arg >> 7) & 0x01
    fields = [
        f"mode={mode}",
        f"flag4={flag4}",
        f"flag5={flag5}",
        f"flag6={flag6}",
        f"flag7={flag7}",
    ]
    if mode == 3:
        if len(words) != 1:
            fields.append(f"words={words_to_csv(words)}")
            return fields, False
        rail_time_word = words[0]
        fields.extend(
            [
                "action=move_camera_rail",
                f"rail_forward={flag4}",
                # The float text is the readable form; keep the packed word only
                # when the text cannot reproduce it exactly.
                *(
                    []
                    if f32_to_u32(float(format_f32(u32_to_f32(rail_time_word)))) == rail_time_word
                    else [f"rail_time_word=0x{rail_time_word:08X}"]
                ),
                f"rail_time={format_f32(u32_to_f32(rail_time_word))}",
            ]
        )
        return fields, True
    if words:
        fields.append(f"words={words_to_csv(words)}")
        return fields, False
    return fields, True

def build_camera_mode_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"mode", "flag4", "flag5", "flag6", "flag7"}, line_no, "camera_mode")
    mode = parse_hex_int(fields["mode"])
    flag4 = parse_hex_int(fields["flag4"])
    flag5 = parse_hex_int(fields["flag5"])
    flag6 = parse_hex_int(fields["flag6"])
    flag7 = parse_hex_int(fields["flag7"])
    if not 0 <= mode <= 0x0F or any(value not in (0, 1) for value in (flag4, flag5, flag6, flag7)):
        raise ValueError(f"line {line_no}: camera_mode fields out of range")
    default_arg = mode | (flag4 << 4) | (flag5 << 5) | (flag6 << 6) | (flag7 << 7)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if arg != default_arg:
        raise ValueError(f"line {line_no}: camera_mode arg does not match mode/flags")
    if "words" in fields and "rail_time_word" not in fields:
        return arg, parse_optional_word_list(fields)
    if mode == 3:
        if "rail_time_word" in fields:
            rail_time_word = parse_hex_int(fields["rail_time_word"]) & 0xFFFFFFFF
        else:
            require_fields(fields, {"rail_time"}, line_no, "camera_mode")
            rail_time_word = f32_to_u32(float(fields["rail_time"]))
        if "rail_forward" in fields and parse_hex_int(fields["rail_forward"]) != flag4:
            raise ValueError(f"line {line_no}: camera_mode rail_forward does not match flag4")
        if "rail_time" in fields and f32_to_u32(float(fields["rail_time"])) != rail_time_word:
            raise ValueError(f"line {line_no}: camera_mode rail_time does not match rail_time_word")
        return arg, [rail_time_word]
    if any(name in fields for name in ("rail_forward", "rail_time_word", "rail_time")):
        raise ValueError(f"line {line_no}: camera_mode rail fields are only valid for mode 3")
    return arg, []

def decode_character_virtual_24_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    upper_mode = (arg >> 6) & 0x03
    fields = [f"explicit_char={explicit_char}", f"upper_mode={upper_mode}"]
    cursor = 0
    if explicit_char:
        if cursor >= len(words):
            fields.append(f"words={words_to_csv(words)}")
            return fields, False
        character = words[cursor]
        cursor += 1
        fields.append(f"character=0x{character:08X}")
    if cursor >= len(words):
        fields.append(f"words={words_to_csv(words)}")
        return fields, False
    control = words[cursor]
    cursor += 1
    mode = control & 0x03
    extra_float = (control >> 2) & 0x01
    byte91_mode = (control >> 3) & 0x03
    bytec4_mode = (control >> 5) & 0x03
    # The control word is fully modeled; print it only when it carries bits
    # outside the model, and suppress zero-valued optional fields.
    ignored = control & ~0x7F
    if ignored:
        fields.append(f"control=0x{control:08X}")
    fields.append(f"mode={mode}")
    if extra_float:
        fields.append(f"extra_float={extra_float}")
    if byte91_mode:
        fields.append(f"byte91_mode={byte91_mode}")
    if bytec4_mode:
        fields.append(f"bytec4_mode={bytec4_mode}")
    if ignored:
        fields.append(f"ignored_control_bits=0x{ignored:08X}")
    if mode in (1, 2):
        if cursor >= len(words):
            fields.append(f"words={words_to_csv(words)}")
            return fields, False
        mode_float_word = words[cursor]
        cursor += 1
        fields.append(f"mode_float_word=0x{mode_float_word:08X}")
        fields.append(f"mode_float={format_f32(u32_to_f32(mode_float_word))}")
    if extra_float:
        if cursor >= len(words):
            fields.append(f"words={words_to_csv(words)}")
            return fields, False
        extra_float_word = words[cursor]
        cursor += 1
        fields.append(f"extra_float_word=0x{extra_float_word:08X}")
        fields.append(f"extra_float_value={format_f32(u32_to_f32(extra_float_word))}")
    if cursor < len(words):
        fields.append(f"trailing={words_to_csv(words[cursor:])}")
    return fields, True

def build_character_virtual_24_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"explicit_char"}, line_no, "character_virtual_24")
    explicit_char = parse_hex_int(fields["explicit_char"])
    if explicit_char not in (0, 1):
        raise ValueError(f"line {line_no}: character_virtual_24 explicit_char must be 0 or 1")
    upper_mode = parse_hex_int(fields.get("upper_mode", "0"))
    if not 0 <= upper_mode <= 0x03:
        raise ValueError(f"line {line_no}: character_virtual_24 upper_mode out of range")
    default_arg = explicit_char | (upper_mode << 6)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if (arg & 0xC1) != default_arg:
        raise ValueError(f"line {line_no}: character_virtual_24 arg does not match explicit_char/upper_mode")
    if "words" in fields and "control" not in fields:
        return arg, parse_optional_word_list(fields)

    words: list[int] = []
    if explicit_char:
        pass  # packed word is resolved from its named parts below
        words.append(resolve_packed_word(fields, line_no, "character_virtual_24", "character", CHARACTER_VARIANT_SPECS))
    elif "character" in fields:
        raise ValueError(f"line {line_no}: character_virtual_24 character present but explicit_char is clear")
    if "control" in fields:
        control = parse_hex_int(fields["control"]) & 0xFFFFFFFF
    else:
        # Derive from the named fields (zero-suppressed on decode).
        control = (
            (parse_hex_int(fields.get("mode", "0")) & 0x03)
            | ((parse_hex_int(fields.get("extra_float", "0")) & 0x01) << 2)
            | ((parse_hex_int(fields.get("byte91_mode", "0")) & 0x03) << 3)
            | ((parse_hex_int(fields.get("bytec4_mode", "0")) & 0x03) << 5)
        )
    mode = control & 0x03
    extra_float = (control >> 2) & 0x01
    if "mode" in fields and parse_hex_int(fields["mode"]) != mode:
        raise ValueError(f"line {line_no}: character_virtual_24 mode does not match control")
    if "extra_float" in fields and parse_hex_int(fields["extra_float"]) != extra_float:
        raise ValueError(f"line {line_no}: character_virtual_24 extra_float does not match control")
    if "byte91_mode" in fields and parse_hex_int(fields["byte91_mode"]) != ((control >> 3) & 0x03):
        raise ValueError(f"line {line_no}: character_virtual_24 byte91_mode does not match control")
    if "bytec4_mode" in fields and parse_hex_int(fields["bytec4_mode"]) != ((control >> 5) & 0x03):
        raise ValueError(f"line {line_no}: character_virtual_24 bytec4_mode does not match control")
    if "ignored_control_bits" in fields and parse_hex_int(fields["ignored_control_bits"]) != (control & ~0x7F):
        raise ValueError(f"line {line_no}: character_virtual_24 ignored_control_bits does not match control")
    words.append(control)
    if mode in (1, 2):
        if "mode_float_word" in fields:
            mode_float_word = parse_hex_int(fields["mode_float_word"]) & 0xFFFFFFFF
        else:
            require_fields(fields, {"mode_float"}, line_no, "character_virtual_24")
            mode_float_word = f32_to_u32(float(fields["mode_float"]))
        if "mode_float" in fields and f32_to_u32(float(fields["mode_float"])) != mode_float_word:
            raise ValueError(f"line {line_no}: character_virtual_24 mode_float does not match mode_float_word")
        words.append(mode_float_word)
    elif "mode_float_word" in fields or "mode_float" in fields:
        raise ValueError(f"line {line_no}: character_virtual_24 mode float fields require mode 1 or 2")
    if extra_float:
        if "extra_float_word" in fields:
            extra_float_word = parse_hex_int(fields["extra_float_word"]) & 0xFFFFFFFF
        else:
            require_fields(fields, {"extra_float_value"}, line_no, "character_virtual_24")
            extra_float_word = f32_to_u32(float(fields["extra_float_value"]))
        if "extra_float_value" in fields and f32_to_u32(float(fields["extra_float_value"])) != extra_float_word:
            raise ValueError(f"line {line_no}: character_virtual_24 extra_float_value does not match extra_float_word")
        words.append(extra_float_word)
    elif "extra_float_word" in fields or "extra_float_value" in fields:
        raise ValueError(f"line {line_no}: character_virtual_24 extra float fields require extra_float bit")
    words.extend(parse_optional_word_list(fields, "trailing"))
    return arg, words

def decode_person_field_update_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    bit332_20 = (arg >> 7) & 0x01
    fields = [f"explicit_char={explicit_char}", f"bit332_20={bit332_20}"]
    cursor = 0
    character_variant = 0
    if explicit_char:
        if cursor >= len(words):
            fields.append(f"words={words_to_csv(words)}")
            return fields, False
        character = words[cursor]
        cursor += 1
        character_variant = (character >> 16) & 0xFF
        fields.extend(
            [
                f"character=0x{character:08X}",
                f"character_number=0x{character & 0xFFFF:04X}",
                f"character_variant=0x{character_variant:02X}",
            ]
        )
    if character_variant == 0 and cursor < len(words):
        if cursor + 2 > len(words):
            fields.append(f"words={words_to_csv(words)}")
            return fields, False
        word0 = words[cursor]
        word1 = words[cursor + 1]
        cursor += 2
        fields.extend(
            [
                f"update_word0=0x{word0:08X}",
                f"field34a=0x{word0 & 0xFFFF:04X}",
                f"field34c=0x{(word0 >> 16) & 0xFFFF:04X}",
                f"update_word1=0x{word1:08X}",
                f"field34e=0x{word1 & 0xFFFF:04X}",
                f"field83=0x{(word1 >> 16) & 0xFF:02X}",
                f"field83_raw_high=0x{(word1 >> 24) & 0xFF:02X}",
            ]
        )
    if cursor < len(words):
        fields.append(f"trailing={words_to_csv(words[cursor:])}")
    return fields, True

def build_person_field_update_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"explicit_char"}, line_no, "person_field_update")
    explicit_char = parse_hex_int(fields["explicit_char"])
    if explicit_char not in (0, 1):
        raise ValueError(f"line {line_no}: person_field_update explicit_char must be 0 or 1")
    bit332_20 = parse_hex_int(fields.get("bit332_20", "0"))
    if bit332_20 not in (0, 1):
        raise ValueError(f"line {line_no}: person_field_update bit332_20 must be 0 or 1")
    default_arg = explicit_char | (bit332_20 << 7)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if (arg & 0x81) != default_arg:
        raise ValueError(f"line {line_no}: person_field_update arg does not match explicit_char/bit332_20")
    if "words" in fields and "update_word0" not in fields and "field34a" not in fields:
        return arg, parse_optional_word_list(fields)

    words: list[int] = []
    character_variant = 0
    if explicit_char:
        require_fields(fields, {"character"}, line_no, "person_field_update")
        character = parse_hex_int(fields["character"]) & 0xFFFFFFFF
        character_variant = (character >> 16) & 0xFF
        if "character_number" in fields and parse_hex_int(fields["character_number"]) != (character & 0xFFFF):
            raise ValueError(f"line {line_no}: person_field_update character_number does not match character")
        if "character_variant" in fields and parse_hex_int(fields["character_variant"]) != character_variant:
            raise ValueError(f"line {line_no}: person_field_update character_variant does not match character")
        words.append(character)
    elif "character" in fields:
        raise ValueError(f"line {line_no}: person_field_update character present but explicit_char is clear")

    has_update = "update_word0" in fields or "field34a" in fields or "field34c" in fields or "update_word1" in fields or "field34e" in fields or "field83" in fields
    if has_update:
        if character_variant != 0:
            raise ValueError(f"line {line_no}: person_field_update update fields require character_variant 0")
        if "update_word0" in fields:
            word0 = parse_hex_int(fields["update_word0"]) & 0xFFFFFFFF
        else:
            require_fields(fields, {"field34a", "field34c"}, line_no, "person_field_update")
            field34a = parse_hex_int(fields["field34a"])
            field34c = parse_hex_int(fields["field34c"])
            if not 0 <= field34a <= 0xFFFF or not 0 <= field34c <= 0xFFFF:
                raise ValueError(f"line {line_no}: person_field_update field34a/field34c out of range")
            word0 = field34a | (field34c << 16)
        if "update_word1" in fields:
            word1 = parse_hex_int(fields["update_word1"]) & 0xFFFFFFFF
        else:
            require_fields(fields, {"field34e", "field83"}, line_no, "person_field_update")
            field34e = parse_hex_int(fields["field34e"])
            field83 = parse_hex_int(fields["field83"])
            field83_raw_high = parse_hex_int(fields.get("field83_raw_high", "0"))
            if not 0 <= field34e <= 0xFFFF or not 0 <= field83 <= 0xFF or not 0 <= field83_raw_high <= 0xFF:
                raise ValueError(f"line {line_no}: person_field_update field34e/field83 fields out of range")
            word1 = field34e | (field83 << 16) | (field83_raw_high << 24)
        checks = {
            "field34a": word0 & 0xFFFF,
            "field34c": (word0 >> 16) & 0xFFFF,
            "field34e": word1 & 0xFFFF,
            "field83": (word1 >> 16) & 0xFF,
            "field83_raw_high": (word1 >> 24) & 0xFF,
        }
        for name, expected in checks.items():
            if name in fields and parse_hex_int(fields[name]) != expected:
                raise ValueError(f"line {line_no}: person_field_update {name} does not match packed update word")
        words.extend([word0, word1])
    words.extend(parse_optional_word_list(fields, "trailing"))
    return arg, words

SINGLE_MANAGER_ENTRY_TYPES = {
    1: 0x0001,
    2: 0x0020,
    3: 0x0010,
    4: 0x0080,
}

def decode_character_single_manager_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    manager_mode = (arg >> 1) & 0x07
    manager_flag14_bit0 = (arg >> 4) & 0x01
    fields = [
        f"explicit_char={explicit_char}",
        f"manager_mode={manager_mode}",
        f"manager_flag14_bit0={manager_flag14_bit0}",
    ]
    cursor = 0
    if explicit_char:
        if cursor >= len(words):
            fields.append(f"words={words_to_csv(words)}")
            return fields, False
        character = words[cursor]
        cursor += 1
        fields.extend(
            [
                f"character=0x{character:08X}",
                f"character_number=0x{character & 0xFFFF:04X}",
                f"character_variant=0x{(character >> 16) & 0xFF:02X}",
            ]
        )
    if manager_mode != 0:
        fields.append(f"words={words_to_csv(words[cursor:])}")
        return fields, False
    if cursor >= len(words):
        fields.append(f"words={words_to_csv(words[cursor:])}")
        return fields, False
    mask = words[cursor]
    cursor += 1
    fields.append(f"mask=0x{mask:08X}")
    entry_bits: list[str] = []
    for bit in range(32):
        if not (mask & (1 << bit)):
            continue
        entry_bits.append(str(bit))
        if bit > 4:
            fields.append(f"words={words_to_csv(words[cursor:])}")
            return fields, False
        if cursor >= len(words):
            fields.append(f"words={words_to_csv(words[cursor:])}")
            return fields, False
        entry_word = words[cursor]
        cursor += 1
        fields.append(f"entry{bit}=0x{entry_word:08X}")
        if bit in SINGLE_MANAGER_ENTRY_TYPES:
            fields.append(f"entry{bit}_manager_type=0x{SINGLE_MANAGER_ENTRY_TYPES[bit]:04X}")
    fields.append("mask_bits=" + ",".join(entry_bits) if entry_bits else "mask_bits=none")
    if cursor < len(words):
        fields.append(f"trailing={words_to_csv(words[cursor:])}")
    return fields, True

def build_character_single_manager_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"explicit_char", "manager_mode"}, line_no, "character_single_manager")
    explicit_char = parse_hex_int(fields["explicit_char"])
    manager_mode = parse_hex_int(fields["manager_mode"])
    arg_field = parse_hex_int(fields["arg"]) if "arg" in fields else None
    manager_flag14_bit0 = parse_hex_int(
        fields.get("manager_flag14_bit0", str((arg_field >> 4) & 0x01 if arg_field is not None else 0))
    )
    if explicit_char not in (0, 1) or not 0 <= manager_mode <= 0x07 or manager_flag14_bit0 not in (0, 1):
        raise ValueError(f"line {line_no}: character_single_manager arg fields out of range")
    default_arg = explicit_char | (manager_mode << 1) | (manager_flag14_bit0 << 4)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    # Bits 5-7 are outside the structured fields; an explicit arg= preserves them.
    if (arg & 0x1F) != default_arg:
        raise ValueError(f"line {line_no}: character_single_manager arg does not match fields")
    # Incomplete decodes carry a words= tail holding the UNCONSUMED remainder;
    # named fields (character, mask, entries) carry the consumed words, so the
    # rebuild is named fields in handler order followed by the tail.
    truncated = "words" in fields

    words: list[int] = []
    if explicit_char:
        if truncated and "character" not in fields:
            words.extend(parse_optional_word_list(fields))
            return arg, words
        require_fields(fields, {"character"}, line_no, "character_single_manager")
        character = parse_hex_int(fields["character"]) & 0xFFFFFFFF
        if "character_number" in fields and parse_hex_int(fields["character_number"]) != (character & 0xFFFF):
            raise ValueError(f"line {line_no}: character_single_manager character_number does not match character")
        if "character_variant" in fields and parse_hex_int(fields["character_variant"]) != ((character >> 16) & 0xFF):
            raise ValueError(f"line {line_no}: character_single_manager character_variant does not match character")
        words.append(character)
    elif "character" in fields:
        raise ValueError(f"line {line_no}: character_single_manager character present but explicit_char is clear")
    if manager_mode != 0:
        if truncated:
            words.extend(parse_optional_word_list(fields))
            return arg, words
        raise ValueError(f"line {line_no}: character_single_manager structured fields only cover manager_mode 0")
    if truncated and "mask" not in fields:
        words.extend(parse_optional_word_list(fields))
        return arg, words
    require_fields(fields, {"mask"}, line_no, "character_single_manager")
    mask = parse_hex_int(fields["mask"]) & 0xFFFFFFFF
    words.append(mask)
    for bit in range(32):
        if not (mask & (1 << bit)):
            continue
        entry_name = f"entry{bit}"
        if bit > 4 or entry_name not in fields:
            if truncated:
                break
            if bit > 4:
                raise ValueError(f"line {line_no}: character_single_manager mask bit {bit} is not structurally decoded")
            require_fields(fields, {entry_name}, line_no, "character_single_manager")
        entry_word = parse_hex_int(fields[entry_name]) & 0xFFFFFFFF
        type_name = f"entry{bit}_manager_type"
        if type_name in fields and bit in SINGLE_MANAGER_ENTRY_TYPES and parse_hex_int(fields[type_name]) != SINGLE_MANAGER_ENTRY_TYPES[bit]:
            raise ValueError(f"line {line_no}: character_single_manager {type_name} does not match handler type")
        words.append(entry_word)
    if truncated:
        words.extend(parse_optional_word_list(fields))
    words.extend(parse_optional_word_list(fields, "trailing"))
    return arg, words

def decode_script_stop_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    mode = arg & 0x0F
    fields = [f"mode=0x{arg:02X}"]
    if not words:
        fields.append("words=")
        return fields, False
    first = words[0]
    condition = (first >> 24) & 0xFF
    fields.append(f"script_id=0x{first & 0xFFFF:04X}")
    fields.append(f"condition=0x{condition:02X}")
    raw_mid = (first >> 16) & 0xFF
    if raw_mid:
        fields.append(f"raw_mid=0x{raw_mid:02X}")
    index = 1
    complete = True
    if condition:
        if len(words) >= 3:
            condition_words = words[1:3]
            condition_args = source_condition_args_field(condition, condition_words).strip()
            if condition_args:
                fields.append(condition_args)
            details = source_condition_details_field(condition, condition_words)
            if details:
                fields.append(details.strip())
            index = 3
        else:
            fields.append(f"words={words_to_csv(words[index:])}")
            return fields, False
    if mode in (2, 4):
        if arg & 0x10:
            if index < len(words):
                fields.append(f"character=0x{words[index]:08X}")
                index += 1
            else:
                complete = False
        else:
            fields.append("character=default")
    if index != len(words):
        fields.append(f"words={words_to_csv(words[index:])}")
        complete = False
    return fields, complete

def build_script_stop_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"mode"}, line_no, "script_stop")
    arg = parse_hex_int(fields["mode"])
    if "words" in fields and "script_id" not in fields:
        return arg, parse_optional_word_list(fields)
    require_fields(fields, {"script_id"}, line_no, "script_stop")
    script_id = parse_hex_int(fields["script_id"])
    condition = resolve_packed_word(
        fields, line_no, "script_stop", "condition", CONDITION_SPECS, required=False
    )
    raw_mid = parse_hex_int(fields.get("raw_mid", "0"))
    if not 0 <= arg <= 0xFF or not 0 <= script_id <= 0xFFFF or not 0 <= condition <= 0xFF or not 0 <= raw_mid <= 0xFF:
        raise ValueError(f"line {line_no}: script_stop mode/script_id/condition out of range")
    words = [(condition << 24) | (raw_mid << 16) | script_id]
    if "words" in fields and not (set(fields) - {"mode", "script_id", "condition", "raw_mid", "words", "flags", "arg"}):
        # Truncated decode: the declared word count ended inside the condition
        # payload; the words= tail carries the remaining raw words verbatim.
        words.extend(parse_optional_word_list(fields))
        return arg, words
    if condition:
        condition_words = build_condition_words_from_source(fields, condition, line_no, "script_stop")
        if len(condition_words) != 2:
            raise ValueError(f"line {line_no}: script_stop condition requires two condition_args words")
        words.extend(condition_words)
    mode = arg & 0x0F
    if mode in (2, 4) and (arg & 0x10):
        require_fields(fields, {"character"}, line_no, "script_stop")
        words.append(parse_hex_int(fields["character"]))
    if "words" in fields:
        words.extend(parse_optional_word_list(fields))
    return arg, words

SCRIPT_START_INHERIT_BITS: tuple[tuple[str, int], ...] = (
    ("inherit_character", 0x01),
    ("inherit_object_name", 0x02),
    ("inherit_values", 0x04),
    ("inherit_floats", 0x08),
)

def decode_script_start_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    mode = arg & 0x0F
    default_char = (arg >> 4) & 0x01
    fields = [f"mode={mode}", f"default_char={default_char}"]
    if not words:
        return fields, False
    first = words[0]
    condition = (first >> 24) & 0xFF
    control = (first >> 16) & 0xFF
    fields.append(f"script_id=0x{first & 0xFFFF:04X}")
    if condition:
        fields.append(f"condition=0x{condition:02X}")
    # Control byte bits (proven at 0x002EB67C..0x002EB728 in Command_04): the
    # new script inherits pieces of the caller's SCR_DATA. 0xFF = everything.
    if control == 0xFF:
        fields.append("inherit=all")
    elif control < 0x10:
        for name, bit in SCRIPT_START_INHERIT_BITS:
            if control & bit:
                fields.append(f"{name}=1")
    else:
        fields.append(f"control=0x{control:02X}")
    index = 1
    complete = True
    if condition:
        if len(words) >= 3:
            condition_words = words[1:3]
            condition_args = source_condition_args_field(condition, condition_words).strip()
            if condition_args:
                fields.append(condition_args)
            details = source_condition_details_field(condition, condition_words)
            if details:
                fields.append(details.strip())
            index = 3
        else:
            complete = False
    if default_char:
        if index < len(words):
            character = words[index]
            fields.append(f"character=0x{character:08X}")
            fields.append(f"character_number=0x{character & 0xFFFF:04X}")
            fields.append(f"character_type=0x{(character >> 16) & 0xFF:02X}")
            raw_byte3 = (character >> 24) & 0xFF
            if raw_byte3:
                fields.append(f"character_raw_byte3=0x{raw_byte3:02X}")
            index += 1
        else:
            complete = False
    if index != len(words):
        fields.append(f"words={words_to_csv(words[index:])}")
        complete = False
    return fields, complete

def build_script_start_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"mode", "default_char"}, line_no, "script_start")
    mode = parse_hex_int(fields["mode"])
    default_char = parse_hex_int(fields["default_char"])
    default_arg = mode | (default_char << 4)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if "words" in fields and "script_id" not in fields:
        return arg, parse_optional_word_list(fields)
    require_fields(fields, {"script_id"}, line_no, "script_start")
    script_id = parse_hex_int(fields["script_id"])
    condition = resolve_packed_word(
        fields, line_no, "script_start", "condition", CONDITION_SPECS, required=False
    )
    if "control" in fields:
        control = parse_hex_int(fields["control"])
    elif fields.get("inherit", "") == "all":
        control = 0xFF
    else:
        if "inherit" in fields:
            raise ValueError(f"line {line_no}: script_start inherit= only accepts 'all'")
        control = 0
        for name, bit in SCRIPT_START_INHERIT_BITS:
            if name in fields and parse_hex_int(fields[name]):
                control |= bit
    if not 0 <= mode <= 0x0F or default_char not in (0, 1):
        raise ValueError(f"line {line_no}: script_start mode/default_char out of range")
    if (arg & 0x0F) != mode or ((arg >> 4) & 0x01) != default_char:
        raise ValueError(f"line {line_no}: script_start arg does not match mode/default_char")
    if not 0 <= script_id <= 0xFFFF or not 0 <= condition <= 0xFF or not 0 <= control <= 0xFF:
        raise ValueError(f"line {line_no}: script_start script_id/condition/control out of range")
    words = [(condition << 24) | (control << 16) | script_id]
    if condition:
        condition_words = build_condition_words_from_source(fields, condition, line_no, "script_start")
        if len(condition_words) != 2:
            raise ValueError(f"line {line_no}: script_start condition requires two condition_args words")
        words.extend(condition_words)
    if default_char:
        pass  # packed word is resolved from its named parts below
        words.append(resolve_packed_word(fields, line_no, "script_start", "character", CHARACTER_TYPE_SPECS))
    if "words" in fields:
        words.extend(parse_optional_word_list(fields))
    return arg, words

def decode_marker_seek_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    selector = arg & 0x07
    advance_if_lower = (arg >> 3) & 0x01
    marker_type = (arg >> 5) & 0x07
    fields = [f"selector={selector}", f"advance_if_lower={advance_if_lower}", f"marker_type={marker_type}"]
    complete = True
    if selector in (0, 1, 2):
        if len(words) != 1:
            complete = False
        elif selector == 1:
            word = words[0]
            fields.append(f"first_flag=0x{word & 0xFFFF:04X}")
            fields.append(f"flag_count={((word >> 16) & 0xFFFF) + 1}")
            fields.append(f"selector_word=0x{word:08X}")
        elif selector == 2:
            # Command_0d passes this word straight to GetEventValueForScript
            # (0x002EB090), so it is an event value id, not a packed selector.
            fields.append(f"event_value={words[0]}")
        else:
            # selector 0 uses the word as the marker index itself (0x002EB030).
            fields.append(f"marker_index={words[0]}")
    elif selector == 3:
        if words:
            complete = False
    else:
        complete = False
    if not complete:
        fields.append(source_words_field(words))
    return fields, complete

def build_marker_seek_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"selector", "advance_if_lower", "marker_type"}, line_no, "marker_seek")
    selector = parse_hex_int(fields["selector"])
    advance_if_lower = parse_hex_int(fields["advance_if_lower"])
    marker_type = parse_hex_int(fields["marker_type"])
    if not 0 <= selector <= 7 or advance_if_lower not in (0, 1) or not 0 <= marker_type <= 7:
        raise ValueError(f"line {line_no}: marker_seek selector/advance_if_lower/marker_type out of range")
    default_arg = selector | (advance_if_lower << 3) | (marker_type << 5)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    # Bit 4 is outside the structured fields; an explicit arg= preserves it.
    if (arg & 0xEF) != default_arg:
        raise ValueError(f"line {line_no}: marker_seek arg does not match fields")
    if "words" in fields:
        return arg, parse_optional_word_list(fields)
    words: list[int] = []
    if selector == 1 and "selector_word" not in fields:
        require_fields(fields, {"first_flag", "flag_count"}, line_no, "marker_seek")
        first_flag = parse_hex_int(fields["first_flag"])
        flag_count = parse_hex_int(fields["flag_count"])
        if not 1 <= flag_count <= 0x10000:
            raise ValueError(f"line {line_no}: marker_seek flag_count out of range")
        words.append(first_flag | ((flag_count - 1) << 16))
    elif selector in (0, 1, 2):
        for key in ("selector_word", "event_value", "marker_index"):
            if key in fields:
                words.append(parse_hex_int(fields[key]) & 0xFFFFFFFF)
                break
        else:
            require_fields(fields, {"selector_word"}, line_no, "marker_seek")
    elif selector != 3:
        raise ValueError(f"line {line_no}: marker_seek selector requires raw words")
    return arg, words

def decode_position_vibration_param_fields(words: list[int]) -> tuple[list[str], bool]:
    if not words:
        return [source_words_field(words)], False
    enable = words[0]
    fields = [f"enable=0x{enable:08X}"]
    if enable == 0:
        if len(words) != 1:
            fields.append(source_words_field(words[1:]))
            return fields, False
        return fields, True
    if len(words) != 8:
        fields.append(source_words_field(words[1:]))
        return fields, False
    attr = words[1]
    attr_mode = (attr >> 8) & 0x03
    fields.append(f"attr=0x{attr:08X}")
    fields.append(f"attr_mode={attr_mode}")
    fields.append("params=" + ",".join(format_f32(u32_to_f32(word)) for word in words[2:]))
    return fields, True

def build_position_vibration_param_words(fields: dict[str, str], line_no: int) -> list[int]:
    require_fields(fields, {"enable"}, line_no, "position_vibration_param")
    enable = parse_hex_int(fields["enable"])
    if "words" in fields:
        return [enable, *parse_optional_word_list(fields)]
    if enable == 0:
        return [enable]
    require_fields(fields, {"attr", "params"}, line_no, "position_vibration_param")
    params = parse_float_words(fields["params"], 6, line_no, "params")
    return [enable, parse_hex_int(fields["attr"]) & 0xFFFFFFFF, *params]

def decode_script_start_stack_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    mode = arg & 0x0F
    explicit_char = (arg >> 4) & 0x01
    fields = [f"mode={mode}", f"explicit_char={explicit_char}"]
    if not words:
        return fields, False
    first = words[0]
    condition = (first >> 24) & 0xFF
    raw_mid = (first >> 16) & 0xFF
    fields.append(f"script_id=0x{first & 0xFFFF:04X}")
    fields.append(f"condition=0x{condition:02X}")
    if raw_mid:
        fields.append(f"raw_mid=0x{raw_mid:02X}")
    index = 1
    complete = True
    if condition:
        if len(words) >= 3:
            condition_words = words[1:3]
            condition_args = source_condition_args_field(condition, condition_words).strip()
            if condition_args:
                fields.append(condition_args)
            details = source_condition_details_field(condition, condition_words)
            if details:
                fields.append(details.strip())
            index = 3
        else:
            complete = False
    if explicit_char:
        if index < len(words):
            character = words[index]
            fields.append(f"character=0x{character:08X}")
            fields.append(f"character_number=0x{character & 0xFFFF:04X}")
            fields.append(f"character_type=0x{(character >> 16) & 0xFF:02X}")
            raw_byte3 = (character >> 24) & 0xFF
            if raw_byte3:
                fields.append(f"character_raw_byte3=0x{raw_byte3:02X}")
            index += 1
        else:
            complete = False
    if index != len(words):
        fields.append(f"words={words_to_csv(words[index:])}")
        complete = False
    return fields, complete

def build_script_start_stack_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"mode", "explicit_char"}, line_no, "script_start_stack")
    mode = parse_hex_int(fields["mode"])
    explicit_char = parse_hex_int(fields["explicit_char"])
    default_arg = mode | (explicit_char << 4)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if "words" in fields and "script_id" not in fields:
        return arg, parse_optional_word_list(fields)
    require_fields(fields, {"script_id"}, line_no, "script_start_stack")
    script_id = parse_hex_int(fields["script_id"])
    condition = resolve_packed_word(
        fields, line_no, "script_start_stack", "condition", CONDITION_SPECS, required=False
    )
    raw_mid = parse_hex_int(fields.get("raw_mid", "0"))
    if not 0 <= mode <= 0x0F or explicit_char not in (0, 1):
        raise ValueError(f"line {line_no}: script_start_stack mode/explicit_char out of range")
    if (arg & 0x0F) != mode or ((arg >> 4) & 0x01) != explicit_char:
        raise ValueError(f"line {line_no}: script_start_stack arg does not match mode/explicit_char")
    if not 0 <= script_id <= 0xFFFF or not 0 <= condition <= 0xFF or not 0 <= raw_mid <= 0xFF:
        raise ValueError(f"line {line_no}: script_start_stack script_id/condition/raw_mid out of range")
    words = [(condition << 24) | (raw_mid << 16) | script_id]
    if condition:
        condition_words = build_condition_words_from_source(fields, condition, line_no, "script_start_stack")
        if len(condition_words) != 2:
            raise ValueError(f"line {line_no}: script_start_stack condition requires two condition_args words")
        words.extend(condition_words)
    if explicit_char:
        require_fields(fields, {"character"}, line_no, "script_start_stack")
        words.append(parse_hex_int(fields["character"]) & 0xFFFFFFFF)
    if "words" in fields:
        words.extend(parse_optional_word_list(fields))
    return arg, words

def decode_character_movement_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    mode = (arg >> 4) & 0x0F
    arg_mid = (arg >> 1) & 0x07
    fields = [f"explicit_char={explicit_char}", f"mode={mode}"]
    if arg_mid:
        fields.append(f"arg_mid=0x{arg_mid:X}")

    cursor = 0
    if explicit_char:
        if cursor >= len(words):
            return fields, False
        character = words[cursor]
        fields.append(f"character=0x{character:08X}")
        fields.append(f"character_number=0x{character & 0xFFFF:04X}")
        fields.append(f"character_type=0x{(character >> 16) & 0xFF:02X}")
        raw_byte3 = (character >> 24) & 0xFF
        if raw_byte3:
            fields.append(f"character_raw_byte3=0x{raw_byte3:02X}")
        cursor += 1
    else:
        fields.append("character=default")
    if mode == 4:
        if cursor >= len(words):
            return fields, False
        control = words[cursor]
        cursor += 1
        slot = control & 0xFF
        low_flags = (control >> 16) & 0xFF
        high_flags = (control >> 24) & 0xFF
        submode = low_flags & 0x03
        attach_source = (low_flags >> 2) & 0x03
        target_word_count = 0
        if slot not in (0, 0xFF) and submode < 2:
            fixed_count = 0
            if attach_source == 1:
                fixed_count += 4
            if low_flags & 0x10:
                fixed_count += 3
            if low_flags & 0x20:
                fixed_count += 1
            if low_flags & 0x40:
                fixed_count += 1
            if low_flags & 0x80:
                fixed_count += 1
            if high_flags & 0x04:
                fixed_count += 1
            if high_flags & 0x01:
                fixed_count += 1
            if high_flags & 0x08:
                fixed_count += 1
            if high_flags & 0x02:
                fixed_count += 3
            target_word_count = len(words) - cursor - fixed_count
            if target_word_count < 0:
                return fields, False
        fields.extend(
            [
                "action=mode4_control",
                f"mode4_control=0x{control:08X}",
                f"mode4_slot=0x{slot:02X}",
                f"mode4_low_flags=0x{low_flags:02X}",
                f"mode4_high_flags=0x{high_flags:02X}",
                f"mode4_submode={submode}",
                f"mode4_attach_source={attach_source}",
            ]
        )
        if target_word_count:
            fields.append(f"target_words={words_to_csv(words[cursor:cursor + target_word_count])}")
            cursor += target_word_count
        if attach_source == 1:
            if cursor + 4 > len(words):
                return fields, False
            fields.append(f"attach_words={words_to_csv(words[cursor:cursor + 4])}")
            cursor += 4
        elif attach_source == 2:
            fields.append("attach_source_name=script_context")
        if low_flags & 0x10:
            if cursor + 3 > len(words):
                return fields, False
            fields.append(f"vec60={format_vec3_words(words[cursor:cursor + 3])}")
            cursor += 3
        if low_flags & 0x20:
            if cursor >= len(words):
                return fields, False
            fields.append(f"float130={format_f32(u32_to_f32(words[cursor]))}")
            cursor += 1
        if low_flags & 0x40:
            if cursor >= len(words):
                return fields, False
            fields.append(f"float134={format_f32(u32_to_f32(words[cursor]))}")
            cursor += 1
        if low_flags & 0x80:
            if cursor >= len(words):
                return fields, False
            fields.append(f"float13c={format_f32(u32_to_f32(words[cursor]))}")
            cursor += 1
        if high_flags & 0x04:
            if cursor >= len(words):
                return fields, False
            fields.append(f"float140={format_f32(u32_to_f32(words[cursor]))}")
            cursor += 1
        if high_flags & 0x01:
            if cursor >= len(words):
                return fields, False
            fields.append(f"float148={format_f32(u32_to_f32(words[cursor]))}")
            cursor += 1
        if high_flags & 0x08:
            if cursor >= len(words):
                return fields, False
            fields.append(f"float14c={format_f32(u32_to_f32(words[cursor]))}")
            cursor += 1
        if high_flags & 0x02:
            if cursor + 3 > len(words):
                return fields, False
            fields.append(f"vec110={format_vec3_words(words[cursor:cursor + 3])}")
            cursor += 3
        high_mode = (high_flags >> 4) & 0x03
        tail_mode = (high_flags >> 6) & 0x03
        if high_mode:
            fields.append(f"mode4_high_mode={high_mode}")
        if tail_mode:
            fields.append(f"mode4_tail_mode={tail_mode}")
        return fields, cursor == len(words)
    if mode == 3:
        if cursor + 4 > len(words):
            return fields, False
        fields.extend(
            [
                "action=throw_position_scalar",
                "posvib_attr=0x00000000",
                f"throw_params={format_vec3_words(words[cursor:cursor + 4])}",
            ]
        )
        cursor += 4
        return fields, cursor == len(words)
    if mode == 5:
        if cursor + 4 > len(words):
            return fields, False
        control = words[cursor]
        cursor += 1
        fields.extend(
            [
                "action=mode5_vector_control",
                f"mode5_control=0x{control:08X}",
                f"mode5_flags=0x{control & 0xFF:02X}",
                f"base_vec={format_vec3_words(words[cursor:cursor + 3])}",
            ]
        )
        cursor += 3
        if control & 0x01:
            if cursor + 3 > len(words):
                return fields, False
            fields.append(f"optional_vec0={format_vec3_words(words[cursor:cursor + 3])}")
            cursor += 3
        if control & 0x02:
            if cursor + 3 > len(words):
                return fields, False
            fields.append(f"optional_vec1={format_vec3_words(words[cursor:cursor + 3])}")
            cursor += 3
        if control & 0x04:
            if cursor + 3 > len(words):
                return fields, False
            fields.append(f"optional_vec2={format_vec3_words(words[cursor:cursor + 3])}")
            cursor += 3
        if control & 0x08:
            if cursor >= len(words):
                return fields, False
            fields.append(f"rate={format_f32(u32_to_f32(words[cursor]))}")
            cursor += 1
        return fields, cursor == len(words)
    if mode != 0:
        return fields, False
    if cursor >= len(words):
        return fields, False
    control = words[cursor]
    cursor += 1
    duration_source = control & 0x03
    control_mid = (control >> 2) & 0x3FFF
    duration_raw = (control >> 16) & 0xFFFF
    source_names = {
        0: "direct",
        1: "event_value",
        2: "schedule_percent",
        3: "reserved",
    }
    fields.extend(
        [
            "action=move_start",
            f"move_control=0x{control:08X}",
            f"duration_source={duration_source}",
            f"duration_source_name={source_names[duration_source]}",
            f"duration_value=0x{duration_raw:04X}",
        ]
    )
    if control_mid:
        fields.append(f"move_control_mid=0x{control_mid:04X}")
    if cursor != len(words):
        fields.append(f"words={words_to_csv(words[cursor:])}")
        return fields, False
    return fields, True

def build_character_movement_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"explicit_char", "mode"}, line_no, "character_movement")
    explicit_char = parse_hex_int(fields["explicit_char"])
    mode = parse_hex_int(fields["mode"])
    default_arg = explicit_char | (mode << 4)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if explicit_char not in (0, 1) or not 0 <= mode <= 0x0F:
        raise ValueError(f"line {line_no}: character_movement explicit_char/mode out of range")
    if (arg & 0x01) != explicit_char or ((arg >> 4) & 0x0F) != mode:
        raise ValueError(f"line {line_no}: character_movement arg does not match explicit_char/mode")
    if (
        "words" in fields
        and "move_control" not in fields
        and "throw_params" not in fields
        and "mode4_control" not in fields
        and "mode5_control" not in fields
    ):
        return arg, parse_optional_word_list(fields)

    words: list[int] = []
    if explicit_char:
        pass  # packed word is resolved from its named parts below
        words.append(resolve_packed_word(fields, line_no, "character_movement", "character", CHARACTER_TYPE_SPECS))
    elif "character" in fields and fields["character"] != "default":
        raise ValueError(f"line {line_no}: character_movement non-default character requires explicit_char=1")

    if mode == 3:
        require_fields(fields, {"throw_params"}, line_no, "character_movement")
        if "posvib_attr" in fields and parse_hex_int(fields["posvib_attr"]) != 0:
            raise ValueError(f"line {line_no}: character_movement mode 3 posvib_attr is a zeroed local struct")
        words.extend(parse_float_words(fields["throw_params"], 4, line_no, "throw_params"))
        return arg, words
    if mode == 4:
        require_fields(fields, {"mode4_control"}, line_no, "character_movement")
        control = parse_hex_int(fields["mode4_control"]) & 0xFFFFFFFF
        if "mode4_slot" in fields and parse_hex_int(fields["mode4_slot"]) != (control & 0xFF):
            raise ValueError(f"line {line_no}: character_movement mode4_slot does not match mode4_control")
        if "mode4_low_flags" in fields and parse_hex_int(fields["mode4_low_flags"]) != ((control >> 16) & 0xFF):
            raise ValueError(f"line {line_no}: character_movement mode4_low_flags does not match mode4_control")
        if "mode4_high_flags" in fields and parse_hex_int(fields["mode4_high_flags"]) != ((control >> 24) & 0xFF):
            raise ValueError(f"line {line_no}: character_movement mode4_high_flags does not match mode4_control")
        words.append(control)
        low_flags = (control >> 16) & 0xFF
        high_flags = (control >> 24) & 0xFF
        if "target_words" in fields:
            # The decoder derives the target word count from the payload
            # length; zero targets is a valid shape and omits the field.
            words.extend(parse_word_list(fields["target_words"]))
        attach_source = (low_flags >> 2) & 0x03
        if attach_source == 1:
            require_fields(fields, {"attach_words"}, line_no, "character_movement")
            attach_words = parse_word_list(fields["attach_words"])
            if len(attach_words) != 4:
                raise ValueError(f"line {line_no}: character_movement attach_words expects four words")
            words.extend(attach_words)
        if low_flags & 0x10:
            require_fields(fields, {"vec60"}, line_no, "character_movement")
            words.extend(parse_vec3_words(fields["vec60"], line_no, "vec60"))
        if low_flags & 0x20:
            require_fields(fields, {"float130"}, line_no, "character_movement")
            words.append(f32_to_u32(float(fields["float130"])))
        if low_flags & 0x40:
            require_fields(fields, {"float134"}, line_no, "character_movement")
            words.append(f32_to_u32(float(fields["float134"])))
        if low_flags & 0x80:
            require_fields(fields, {"float13c"}, line_no, "character_movement")
            words.append(f32_to_u32(float(fields["float13c"])))
        if high_flags & 0x04:
            require_fields(fields, {"float140"}, line_no, "character_movement")
            words.append(f32_to_u32(float(fields["float140"])))
        if high_flags & 0x01:
            require_fields(fields, {"float148"}, line_no, "character_movement")
            words.append(f32_to_u32(float(fields["float148"])))
        if high_flags & 0x08:
            require_fields(fields, {"float14c"}, line_no, "character_movement")
            words.append(f32_to_u32(float(fields["float14c"])))
        if high_flags & 0x02:
            require_fields(fields, {"vec110"}, line_no, "character_movement")
            words.extend(parse_vec3_words(fields["vec110"], line_no, "vec110"))
        return arg, words
    if mode == 5:
        require_fields(fields, {"mode5_control", "base_vec"}, line_no, "character_movement")
        control = parse_hex_int(fields["mode5_control"]) & 0xFFFFFFFF
        if "mode5_flags" in fields and parse_hex_int(fields["mode5_flags"]) != (control & 0xFF):
            raise ValueError(f"line {line_no}: character_movement mode5_flags does not match mode5_control")
        words.append(control)
        words.extend(parse_vec3_words(fields["base_vec"], line_no, "base_vec"))
        for bit, name in ((0x01, "optional_vec0"), (0x02, "optional_vec1"), (0x04, "optional_vec2")):
            if control & bit:
                require_fields(fields, {name}, line_no, "character_movement")
                words.extend(parse_vec3_words(fields[name], line_no, name))
        if control & 0x08:
            require_fields(fields, {"rate"}, line_no, "character_movement")
            words.append(f32_to_u32(float(fields["rate"])))
        return arg, words
    if mode != 0:
        require_fields(fields, {"words"}, line_no, "character_movement")
        if explicit_char:
            words = words[:1] + parse_optional_word_list(fields)
            return arg, words
        return arg, parse_optional_word_list(fields)

    require_fields(fields, {"move_control"}, line_no, "character_movement")
    control = parse_hex_int(fields["move_control"]) & 0xFFFFFFFF
    if "duration_source" in fields and parse_hex_int(fields["duration_source"]) != (control & 0x03):
        raise ValueError(f"line {line_no}: character_movement duration_source does not match move_control")
    if "duration_value" in fields and parse_hex_int(fields["duration_value"]) != ((control >> 16) & 0xFFFF):
        raise ValueError(f"line {line_no}: character_movement duration_value does not match move_control")
    if "move_control_mid" in fields and parse_hex_int(fields["move_control_mid"]) != ((control >> 2) & 0x3FFF):
        raise ValueError(f"line {line_no}: character_movement move_control_mid does not match move_control")
    words.append(control)
    if "words" in fields:
        words.extend(parse_optional_word_list(fields))
    return arg, words

def decode_battle_character_entry_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    fields = [f"explicit_char={explicit_char}"]
    cursor = 0
    if explicit_char:
        if cursor >= len(words):
            return fields, False
        character = words[cursor]
        fields.append(f"character=0x{character:08X}")
        fields.append(f"character_number=0x{character & 0xFFFF:04X}")
        fields.append(f"character_type=0x{(character >> 16) & 0xFF:02X}")
        raw_byte3 = (character >> 24) & 0xFF
        if raw_byte3:
            fields.append(f"character_raw_byte3=0x{raw_byte3:02X}")
        cursor += 1
    else:
        fields.append("character=default")
    if cursor + 3 > len(words):
        return fields, False
    word0 = words[cursor]
    word1 = words[cursor + 1]
    word2 = words[cursor + 2]
    cursor += 3
    fields.extend(
        [
            f"entry_word0=0x{word0:08X}",
            f"entry_half0=0x{word0 & 0xFFFF:04X}",
            f"entry_half1={sign_extend((word0 >> 16) & 0xFFFF, 16)}",
            f"entry_word1=0x{word1:08X}",
            f"entry_half2=0x{word1 & 0xFFFF:04X}",
            f"entry_byte3=0x{(word1 >> 16) & 0xFF:02X}",
        ]
    )
    entry_reserved = (word1 >> 24) & 0xFF
    if entry_reserved:
        fields.append(f"entry_reserved=0x{entry_reserved:02X}")
    # Word 2 lands at RADI_BTL_ORDER+0x10, whose byte 0 CBtlCharacter reads as
    # two nibbles: ChangeTeamID masks it with 0x0F, ChangeLeader extracts bits
    # 4-7 and tests them for non-zero. The upper three bytes have no reader
    # specific enough to name, so they stay a remainder.
    team = word2 & 0x0F
    leader = (word2 >> 4) & 0x0F
    if leader <= 1:
        fields.append(f"team={team}")
        fields.append(f"leader={leader}")
        if word2 >> 8:
            fields.append(f"entry_word2_high=0x{word2 >> 8:06X}")
    else:
        fields.append(f"entry_word2=0x{word2:08X}")
    if cursor != len(words):
        fields.append(f"words={words_to_csv(words[cursor:])}")
        return fields, False
    return fields, True

def build_battle_character_entry_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"explicit_char"}, line_no, "battle_character_entry")
    explicit_char = parse_hex_int(fields["explicit_char"])
    arg = parse_hex_int(fields.get("arg", str(explicit_char)))
    if explicit_char not in (0, 1) or (arg & 0x01) != explicit_char:
        raise ValueError(f"line {line_no}: battle_character_entry arg does not match explicit_char")
    if "words" in fields and "entry_word0" not in fields:
        return arg, parse_optional_word_list(fields)

    words: list[int] = []
    if explicit_char:
        pass  # packed word is resolved from its named parts below
        words.append(resolve_packed_word(fields, line_no, "battle_character_entry", "character", CHARACTER_TYPE_SPECS))
    elif "character" in fields and fields["character"] != "default":
        raise ValueError(f"line {line_no}: battle_character_entry non-default character requires explicit_char=1")

    require_fields(fields, {"entry_word0", "entry_word1"}, line_no, "battle_character_entry")
    word0 = parse_hex_int(fields["entry_word0"]) & 0xFFFFFFFF
    word1 = parse_hex_int(fields["entry_word1"]) & 0xFFFFFFFF
    if "entry_word2" in fields:
        word2 = parse_hex_int(fields["entry_word2"]) & 0xFFFFFFFF
    else:
        require_fields(fields, {"team", "leader"}, line_no, "battle_character_entry")
        team = parse_hex_int(fields["team"])
        leader = parse_hex_int(fields["leader"])
        if not 0 <= team <= 0x0F or leader not in (0, 1):
            raise ValueError(f"line {line_no}: battle_character_entry team/leader out of range")
        high = parse_hex_int(fields.get("entry_word2_high", "0"))
        if not 0 <= high <= 0xFFFFFF:
            raise ValueError(f"line {line_no}: battle_character_entry entry_word2_high out of range")
        word2 = team | (leader << 4) | (high << 8)
    if "entry_half0" in fields and parse_hex_int(fields["entry_half0"]) != (word0 & 0xFFFF):
        raise ValueError(f"line {line_no}: battle_character_entry entry_half0 does not match entry_word0")
    if "entry_half1" in fields and parse_hex_int(fields["entry_half1"]) != sign_extend((word0 >> 16) & 0xFFFF, 16):
        raise ValueError(f"line {line_no}: battle_character_entry entry_half1 does not match entry_word0")
    if "entry_half2" in fields and parse_hex_int(fields["entry_half2"]) != (word1 & 0xFFFF):
        raise ValueError(f"line {line_no}: battle_character_entry entry_half2 does not match entry_word1")
    if "entry_byte3" in fields and parse_hex_int(fields["entry_byte3"]) != ((word1 >> 16) & 0xFF):
        raise ValueError(f"line {line_no}: battle_character_entry entry_byte3 does not match entry_word1")
    if "entry_reserved" in fields and parse_hex_int(fields["entry_reserved"]) != ((word1 >> 24) & 0xFF):
        raise ValueError(f"line {line_no}: battle_character_entry entry_reserved does not match entry_word1")
    words.extend([word0, word1, word2])
    if "words" in fields:
        words.extend(parse_optional_word_list(fields))
    return arg, words

def decode_character_sub_anim_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    handler_mode = arg >> 6
    action = "virtual_anim_sub_control" if handler_mode == 0 else "stop_sub_animation"
    fields = [f"explicit_char={explicit_char}", f"handler_mode={handler_mode}", f"action={action}"]
    cursor = 0
    if explicit_char:
        if cursor >= len(words):
            return fields, False
        character = words[cursor]
        fields.append(f"character=0x{character:08X}")
        fields.append(f"character_number=0x{character & 0xFFFF:04X}")
        fields.append(f"character_type=0x{(character >> 16) & 0xFF:02X}")
        raw_byte3 = (character >> 24) & 0xFF
        if raw_byte3:
            fields.append(f"character_raw_byte3=0x{raw_byte3:02X}")
        cursor += 1
    else:
        fields.append("character=default")
    if cursor != len(words):
        fields.append(f"words={words_to_csv(words[cursor:])}")
        return fields, False
    return fields, True

def build_character_sub_anim_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"explicit_char"}, line_no, "character_sub_anim")
    explicit_char = parse_hex_int(fields["explicit_char"])
    handler_mode = parse_hex_int(fields.get("handler_mode", "0"))
    default_arg = explicit_char | (handler_mode << 6)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if explicit_char not in (0, 1) or not 0 <= handler_mode <= 0x03:
        raise ValueError(f"line {line_no}: character_sub_anim explicit_char/handler_mode out of range")
    if (arg & 0x01) != explicit_char or (arg >> 6) != handler_mode:
        raise ValueError(f"line {line_no}: character_sub_anim arg does not match explicit_char/handler_mode")
    if "words" in fields and "character" not in fields:
        return arg, parse_optional_word_list(fields)
    words: list[int] = []
    if explicit_char:
        pass  # packed word is resolved from its named parts below
        words.append(resolve_packed_word(fields, line_no, "character_sub_anim", "character", CHARACTER_TYPE_SPECS))
    elif "character" in fields and fields["character"] != "default":
        raise ValueError(f"line {line_no}: character_sub_anim non-default character requires explicit_char=1")
    if "words" in fields:
        words.extend(parse_optional_word_list(fields))
    return arg, words

def decode_character_attach_parent_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    parent_from_stream = (arg >> 1) & 0x01
    name_source = (arg >> 2) & 0x03
    background = (arg >> 4) & 0x01
    cursor = 0
    fields = [
        f"explicit_char={explicit_char}",
        f"background={background}",
        f"name_source={name_source}",
    ]
    if background:
        fields.append(f"background_attach_flag={(arg >> 5) & 0x01}")
    else:
        fields.append(f"parent_source={'stream' if parent_from_stream else 'context'}")
        fields.append(f"attach_mode={arg >> 5}")
    if explicit_char:
        if cursor >= len(words):
            fields.append(f"arg=0x{arg:02X}")
            fields.append(f"words={words_to_csv(words)}")
            return fields, False
        fields.append(f"character=0x{words[cursor]:08X}")
        cursor += 1
    if not background and parent_from_stream:
        if cursor >= len(words):
            fields.append(f"arg=0x{arg:02X}")
            fields.append(f"words={words_to_csv(words)}")
            return fields, False
        parent_word = words[cursor]
        cursor += 1
        fields.append(f"parent_word=0x{parent_word:08X}")
        fields.append(f"parent_character=0x{parent_word & 0xFFFF:04X}")
        fields.append(f"parent_variant=0x{(parent_word >> 16) & 0xFF:02X}")
        parent_high = (parent_word >> 24) & 0xFF
        if parent_high:
            fields.append(f"parent_raw_high=0x{parent_high:02X}")
    if name_source == 1:
        if cursor + 4 > len(words):
            fields.append(f"arg=0x{arg:02X}")
            fields.append(f"words={words_to_csv(words)}")
            return fields, False
        fields.append(fixed_name_field("name_words", words[cursor:cursor + 4]))
        cursor += 4
    if cursor < len(words):
        fields.append(f"trailing={words_to_csv(words[cursor:])}")
    return fields, True

def build_character_attach_parent_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"explicit_char"}, line_no, "character_attach_parent")
    explicit_char = parse_hex_int(fields["explicit_char"])
    if explicit_char not in (0, 1):
        raise ValueError(f"line {line_no}: character_attach_parent explicit_char must be 0 or 1")
    if "words" in fields and "background" not in fields:
        target_source = parse_hex_int(fields.get("target_source", str((parse_hex_int(fields.get("arg", str(explicit_char))) >> 1) & 0x03)))
        arg = parse_hex_int(fields.get("arg", str(explicit_char | (target_source << 1))))
        if not 0 <= target_source <= 0x03 or (arg & 0x07) != (explicit_char | (target_source << 1)):
            raise ValueError(f"line {line_no}: character_attach_parent arg does not match explicit_char/target_source")
        return arg, parse_optional_word_list(fields)

    require_fields(fields, {"background", "name_source"}, line_no, "character_attach_parent")
    background = parse_hex_int(fields["background"])
    name_source = parse_hex_int(fields["name_source"])
    if background not in (0, 1) or not 0 <= name_source <= 0x03:
        raise ValueError(f"line {line_no}: character_attach_parent background/name_source out of range")
    words: list[int] = []
    if explicit_char:
        require_fields(fields, {"character"}, line_no, "character_attach_parent")
        character = parse_hex_int(fields["character"])
        if not 0 <= character <= 0xFFFFFFFF:
            raise ValueError(f"line {line_no}: character_attach_parent character out of range")
        words.append(character)

    if background:
        background_attach_flag = parse_hex_int(fields.get("background_attach_flag", "0"))
        if background_attach_flag not in (0, 1):
            raise ValueError(f"line {line_no}: character_attach_parent background_attach_flag must be 0 or 1")
        arg = explicit_char | (name_source << 2) | 0x10 | (background_attach_flag << 5)
    else:
        parent_source = fields.get("parent_source", "context")
        if parent_source not in ("context", "stream"):
            raise ValueError(f"line {line_no}: character_attach_parent parent_source must be context or stream")
        if parent_source == "stream":
            parent_word = resolve_packed_word(
                fields, line_no, "character_attach_parent", "parent_word", PARENT_WORD_SPECS
            )
            if not 0 <= parent_word <= 0xFFFFFFFF:
                raise ValueError(f"line {line_no}: character_attach_parent parent_word out of range")
            words.append(parent_word)
        attach_mode = parse_hex_int(fields.get("attach_mode", "0"))
        if not 0 <= attach_mode <= 0x07:
            raise ValueError(f"line {line_no}: character_attach_parent attach_mode out of range")
        arg = explicit_char | ((1 if parent_source == "stream" else 0) << 1) | (name_source << 2) | (attach_mode << 5)

    if name_source == 1:
        if "name" not in fields:
            require_fields(fields, {"name_words"}, line_no, "character_attach_parent")
        name_words = (resolve_fixed_name_words(fields, "name_words", line_no) or [])
        if len(name_words) != 4:
            raise ValueError(f"line {line_no}: character_attach_parent name_words expects four words")
        words.extend(name_words)
    elif "name_words" in fields:
        raise ValueError(f"line {line_no}: character_attach_parent name_words is only used by name_source 1")

    explicit_arg = parse_hex_int(fields.get("arg", str(arg)))
    if explicit_arg != arg:
        raise ValueError(f"line {line_no}: character_attach_parent arg does not match structured fields")
    words.extend(parse_optional_word_list(fields, "trailing"))
    if "words" in fields:
        words.extend(parse_optional_word_list(fields))
    return arg, words

def decode_window_message_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    fields = [f"mode=0x{arg:02X}"]
    if not words:
        return fields, False
    first = words[0]
    message_id = first & 0xFFFF
    message_group = (first >> 16) & 0xFFFF
    fields.append(f"message_id=0x{message_id:04X}")
    fields.append(f"message_group=0x{message_group:04X}")
    if arg & 0x01:
        fields.append(f"subdispatch={1 if message_group == 7 else 0}")
    params = words[1:]
    if params:
        fields.append(f"params={words_to_csv(params)}")
    return fields, True

def build_window_message_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"mode"}, line_no, "window_message")
    arg = parse_hex_int(fields["mode"])
    if "words" in fields and "message_id" not in fields:
        return arg, parse_optional_word_list(fields)
    require_fields(fields, {"message_id", "message_group"}, line_no, "window_message")
    message_id = parse_hex_int(fields["message_id"])
    message_group = parse_hex_int(fields["message_group"])
    if not 0 <= arg <= 0xFF or not 0 <= message_id <= 0xFFFF or not 0 <= message_group <= 0xFFFF:
        raise ValueError(f"line {line_no}: window_message mode/message fields out of range")
    words = [(message_group << 16) | message_id]
    if "params" in fields:
        words.extend(parse_word_list(fields["params"]))
    if "words" in fields:
        words.extend(parse_optional_word_list(fields))
    return arg, words

def decode_play_sound_effect_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    mode = (arg >> 6) & 0x03
    submode = (arg >> 1) & 0x03
    fields = [f"mode={mode}", f"submode={submode}", f"explicit_char={arg & 0x01}"]
    if mode == 0 and len(words) == 4:
        control = (words[0] >> 16) & 0xFFFF
        control_index_raw = control & 0x1F
        control_index = control_index_raw - 0x20 if control_index_raw >= 0x10 else control_index_raw
        # control_high = index | flag_bits << 5; print it only when it carries
        # bits outside that model. Zero-valued optional params are suppressed.
        if control >> 8:
            fields.append(f"control_high=0x{control:04X}")
        if sign_extend(words[0] & 0xFFFF, 16):
            fields.append(f"playse_stack0={sign_extend(words[0] & 0xFFFF, 16)}")
        fields.append(f"se_index={control_index}")
        if (control >> 5) & 0x07:
            fields.append(f"se_flags={(control >> 5) & 0x07}")
        fields.append(f"sound_id=0x{words[1] & 0xFFFFFFFF:08X}")
        if sign_extend((words[2] >> 16) & 0xFFFF, 16):
            fields.append(f"playse_arg5={sign_extend((words[2] >> 16) & 0xFFFF, 16)}")
        # word2 low16 / word3 low16 are the PlaySe volume (0-127) and
        # pan (0x40 centre) slots: every engine-internal caller passes
        # the 0x70/0x40 idiom there and the corpus distributions match.
        fields.append(f"volume={sign_extend(words[2] & 0xFFFF, 16)}")
        fields.append(f"pan={sign_extend(words[3] & 0xFFFF, 16)}")
        return fields, True
    if mode == 1 and len(words) in (4, 8):
        control = (words[0] >> 16) & 0xFFFF
        fields.extend(
            [
            ]
        )
        if sign_extend(words[0] & 0xFFFF, 16):
            fields.append(f"control_low_s16={sign_extend(words[0] & 0xFFFF, 16)}")
        fields.append(f"control_high=0x{control:04X}")
        fields.append(f"sound_id=0x{words[1]:08X}")
        fields.append(f"volume={words[2] & 0xFFFF}")
        if words[2] >> 16:
            fields.append(f"playse_arg5={(words[2] >> 16) & 0xFFFF}")
        fields.append(f"character=0x{words[3]:08X}")
        if submode == 1 and len(words) == 8:
            fields.append(fixed_name_field("target_name_words", words[4:8]))
        elif len(words) != 4:
            fields.append(f"trailing={words_to_csv(words[4:])}")
        return fields, True
    if mode == 2 and len(words) >= 3:
        fields.extend(
            [
            ]
        )
        if sign_extend(words[0] & 0xFFFF, 16):
            fields.append(f"control_low_s16={sign_extend(words[0] & 0xFFFF, 16)}")
        fields.append(f"control_high=0x{(words[0] >> 16) & 0xFFFF:04X}")
        fields.append(f"sound_id=0x{words[1]:08X}")
        fields.append(f"volume={words[2] & 0xFFFF}")
        if words[2] >> 16:
            fields.append(f"playse_arg5={(words[2] >> 16) & 0xFFFF}")
        if submode == 1 and len(words) >= 7:
            fields.append(fixed_name_field("target_name_words", words[3:7]))
            if len(words) > 7:
                fields.append(f"trailing={words_to_csv(words[7:])}")
        elif len(words) > 3:
            fields.append(f"trailing={words_to_csv(words[3:])}")
        return fields, True
    return fields, False

def build_play_sound_effect_words(fields: dict[str, str], line_no: int) -> list[int]:
    require_fields(fields, {"mode"}, line_no, "play_sound_effect")
    mode = parse_hex_int(fields["mode"])
    if mode in (1, 2) and ("control_word" in fields or "control_high" in fields):
        if "control_word" in fields:
            control_word = parse_hex_int(fields["control_word"]) & 0xFFFFFFFF
        else:
            control_word = (parse_hex_int(fields.get("control_low_s16", "0")) & 0xFFFF) | (
                (parse_hex_int(fields["control_high"]) & 0xFFFF) << 16
            )
        words = [control_word]
        if "control_low_s16" in fields and parse_hex_int(fields["control_low_s16"]) != sign_extend(control_word & 0xFFFF, 16):
            raise ValueError(f"line {line_no}: play_sound_effect control_low_s16 does not match control_word")
        if "control_high" in fields and parse_hex_int(fields["control_high"]) != ((control_word >> 16) & 0xFFFF):
            raise ValueError(f"line {line_no}: play_sound_effect control_high does not match control_word")
        require_fields(fields, {"sound_id"}, line_no, "play_sound_effect")
        words.append(parse_hex_int(fields["sound_id"]) & 0xFFFFFFFF)
        if "volume_or_arg" in fields:
            words.append(parse_hex_int(fields["volume_or_arg"]) & 0xFFFFFFFF)
        else:
            require_fields(fields, {"volume"}, line_no, "play_sound_effect")
            words.append(
                (parse_hex_int(fields["volume"]) & 0xFFFF)
                | ((parse_hex_int(fields.get("playse_arg5", "0")) & 0xFFFF) << 16)
            )
        if mode == 1:
            require_fields(fields, {"character"}, line_no, "play_sound_effect")
            words.append(parse_hex_int(fields["character"]) & 0xFFFFFFFF)
        if "target_name_words" in fields or "target_name" in fields:
            target_name_words = (resolve_fixed_name_words(fields, "target_name_words", line_no) or [])
            if len(target_name_words) != 4:
                raise ValueError(f"line {line_no}: play_sound_effect target_name_words expects four words")
            words.extend(target_name_words)
        words.extend(parse_optional_word_list(fields, "trailing"))
        if "words" in fields:
            words.extend(parse_optional_word_list(fields))
        return words

    require_fields(fields, {"sound_id"}, line_no, "play_sound_effect")
    if mode != 0:
        raise ValueError(f"line {line_no}: play_sound_effect mode {mode} requires control_word= form")
    if "control_high" in fields:
        control_high = parse_hex_int(fields["control_high"])
    else:
        # Derive from control_index + control_flag_bits (index is 5-bit
        # two's complement: -16..-1 encode as 0x10..0x1F).
        index_text = fields.get("se_index", fields.get("control_index"))
        if index_text is None:
            require_fields(fields, {"se_index"}, line_no, "play_sound_effect")
        index = parse_hex_int(index_text)
        if not -16 <= index <= 15:
            raise ValueError(f"line {line_no}: play_sound_effect se_index out of range")
        flags_text = fields.get("se_flags", fields.get("control_flag_bits", "0"))
        control_high = (index & 0x1F) | ((parse_hex_int(flags_text) & 0x07) << 5)
    stack0 = parse_hex_int(fields.get("playse_stack0", "0"))
    sound_id = parse_hex_int(fields["sound_id"])
    arg5 = parse_hex_int(fields.get("playse_arg5", "0"))
    # `volume`/`pan` are the proven names; `playse_arg6`/`playse_arg7` are the
    # legacy spellings of the same halfwords.
    arg6_text = fields.get("volume", fields.get("playse_arg6"))
    arg7_text = fields.get("pan", fields.get("playse_arg7"))
    if arg6_text is None or arg7_text is None:
        require_fields(fields, {"volume", "pan"}, line_no, "play_sound_effect")
    arg6 = parse_hex_int(arg6_text)
    arg7 = parse_hex_int(arg7_text)
    if not 0 <= control_high <= 0xFFFF:
        raise ValueError(f"line {line_no}: play_sound_effect control_high must fit unsigned 16-bit")
    if not -0x8000 <= stack0 <= 0x7FFF or not -0x8000 <= arg5 <= 0x7FFF or not -0x8000 <= arg6 <= 0x7FFF or not -0x8000 <= arg7 <= 0x7FFF:
        raise ValueError(f"line {line_no}: play_sound_effect signed halfword fields out of range")
    if not 0 <= sound_id <= 0xFFFFFFFF:
        raise ValueError(f"line {line_no}: play_sound_effect sound_id out of range")
    if "control_index" in fields:
        control_index = parse_hex_int(fields["control_index"])
        expected_index = (control_high & 0x1F) - 0x20 if (control_high & 0x1F) >= 0x10 else (control_high & 0x1F)
        if control_index != expected_index:
            raise ValueError(f"line {line_no}: play_sound_effect control_index does not match control_high")
    if "control_flag_bits" in fields and parse_hex_int(fields["control_flag_bits"]) != ((control_high >> 5) & 0x07):
        raise ValueError(f"line {line_no}: play_sound_effect control_flag_bits does not match control_high")
    return [
        (stack0 & 0xFFFF) | (control_high << 16),
        sound_id & 0xFFFFFFFF,
        (arg6 & 0xFFFF) | ((arg5 & 0xFFFF) << 16),
        arg7 & 0xFFFF,
    ]

def decode_stop_sound_effect_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    mode = arg & 0x0F
    stop_mode = (arg >> 4) & 0x0F
    explicit_flag = arg & 0x01
    fields = [f"mode={mode}", f"stop_mode=0x{stop_mode:X}", f"explicit_flag={explicit_flag}"]
    if len(words) != 1:
        return fields, False
    word = words[0]
    fields.extend(
        [
            f"sound_id=0x{word & 0xFFFF:04X}",
            f"bank=0x{(word >> 16) & 0xFF:02X}",
            f"selector=0x{(word >> 24) & 0xFF:02X}",
        ]
    )
    return fields, True

def build_stop_sound_effect_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"mode"}, line_no, "stop_sound_effect")
    mode = parse_hex_int(fields["mode"])
    stop_mode = parse_hex_int(fields.get("stop_mode", "0"))
    explicit_flag = parse_hex_int(fields.get("explicit_flag", str(mode & 0x01)))
    default_arg = mode | (stop_mode << 4)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if "words" in fields:
        # Truncated decode: rebuild from the verbatim words= tail.
        return arg, parse_optional_word_list(fields)
    require_fields(fields, {"sound_id", "bank", "selector"}, line_no, "stop_sound_effect")
    sound_id = parse_hex_int(fields["sound_id"])
    bank = parse_hex_int(fields["bank"])
    selector = parse_hex_int(fields["selector"])
    if not 0 <= mode <= 0x0F or not 0 <= stop_mode <= 0x0F or explicit_flag not in (0, 1):
        raise ValueError(f"line {line_no}: stop_sound_effect mode fields out of range")
    if (arg & 0x0F) != mode or ((arg >> 4) & 0x0F) != stop_mode or (arg & 0x01) != explicit_flag:
        raise ValueError(f"line {line_no}: stop_sound_effect arg does not match mode/stop_mode/explicit_flag")
    if not 0 <= sound_id <= 0xFFFF or not 0 <= bank <= 0xFF or not 0 <= selector <= 0xFF:
        raise ValueError(f"line {line_no}: stop_sound_effect sound_id/bank/selector out of range")
    return arg, [sound_id | (bank << 16) | (selector << 24)]

def decode_load_sound_resource_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    mode = arg & 0x1F
    fields = [f"mode={mode}"]
    if len(words) != 1:
        return fields, False
    resource_id = words[0]
    fields.append(f"resource_id=0x{resource_id:08X}")
    if mode == 31:
        fields.append(f"table_offset=0x{resource_id * 1000:08X}")
    return fields, True

def build_load_sound_resource_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"mode"}, line_no, "load_sound_resource")
    mode = parse_hex_int(fields["mode"])
    arg = parse_hex_int(fields.get("arg", str(mode)))
    if "words" in fields and "resource_id" not in fields:
        return arg, parse_optional_word_list(fields)
    require_fields(fields, {"resource_id"}, line_no, "load_sound_resource")
    resource_id = parse_hex_int(fields["resource_id"])
    if not 0 <= mode <= 0x1F or (arg & 0x1F) != mode:
        raise ValueError(f"line {line_no}: load_sound_resource arg does not match mode")
    if not 0 <= resource_id <= 0xFFFFFFFF:
        raise ValueError(f"line {line_no}: load_sound_resource resource_id out of range")
    if "table_offset" in fields:
        expected = (resource_id * 1000) & 0xFFFFFFFF
        if parse_hex_int(fields["table_offset"]) != expected:
            raise ValueError(f"line {line_no}: load_sound_resource table_offset does not match resource_id")
    return arg, [resource_id]

def decode_sound_listener_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    mode = arg & 0x07
    target_source = (arg >> 3) & 0x03
    manager_listener = (arg >> 5) & 0x01
    positive_distance = (arg >> 6) & 0x01
    fields = [
        f"mode={mode}",
        f"target_source={target_source}",
        f"manager_listener={manager_listener}",
        f"positive_distance={positive_distance}",
    ]
    if mode == 4 and not words:
        fields.append("target=camera")
        return fields, True
    if mode == 5 and len(words) >= 3:
        fields.append(f"position={format_vec3_words(words[:3])}")
        if len(words) > 3:
            fields.append(f"tail={words_to_csv(words[3:])}")
        return fields, True
    if mode == 2 and len(words) == 1:
        word = words[0]
        fields.append(f"character=0x{word & 0xFFFF:04X}")
        fields.append(f"character_type=0x{(word >> 16) & 0xFF:02X}")
        fields.append(f"raw_byte3=0x{(word >> 24) & 0xFF:02X}")
        return fields, True
    return fields, False

def build_sound_listener_words(fields: dict[str, str], line_no: int) -> tuple[int, list[int]]:
    require_fields(fields, {"mode"}, line_no, "sound_listener")
    mode = parse_hex_int(fields["mode"])
    target_source = parse_hex_int(fields.get("target_source", "0"))
    manager_listener = parse_hex_int(fields.get("manager_listener", "0"))
    positive_distance = parse_hex_int(fields.get("positive_distance", "0"))
    default_arg = mode | (target_source << 3) | (manager_listener << 5) | (positive_distance << 6)
    arg = parse_hex_int(fields.get("arg", str(default_arg)))
    if "words" in fields and not any(name in fields for name in ("position", "character")):
        return arg, parse_optional_word_list(fields)
    if not 0 <= mode <= 0x07 or not 0 <= target_source <= 0x03 or manager_listener not in (0, 1) or positive_distance not in (0, 1):
        raise ValueError(f"line {line_no}: sound_listener mode fields out of range")
    if (arg & 0x7F) != default_arg:
        raise ValueError(f"line {line_no}: sound_listener arg does not match mode/target_source/manager_listener/positive_distance")
    if mode == 4:
        return arg, []
    if mode == 5:
        require_fields(fields, {"position"}, line_no, "sound_listener")
        words = parse_vec3_words(fields["position"], line_no, "position")
        if "tail" in fields:
            words.extend(parse_word_list(fields["tail"]))
        return arg, words
    if mode == 2:
        require_fields(fields, {"character"}, line_no, "sound_listener")
        character = parse_hex_int(fields["character"])
        character_type = parse_hex_int(fields.get("character_type", "0"))
        raw_byte3 = parse_hex_int(fields.get("raw_byte3", "0"))
        if not 0 <= character <= 0xFFFF or not 0 <= character_type <= 0xFF or not 0 <= raw_byte3 <= 0xFF:
            raise ValueError(f"line {line_no}: sound_listener character fields out of range")
        return arg, [character | (character_type << 16) | (raw_byte3 << 24)]
    return arg, parse_optional_word_list(fields)

def decode_personal_inventory_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    mode = (arg >> 1) & 0x03
    flag5 = (arg >> 5) & 0x01
    event40 = (arg >> 6) & 0x01
    event80 = (arg >> 7) & 0x01
    fields = [
        f"explicit_char={explicit_char}",
        f"mode={mode}",
        f"flag5={flag5}",
        f"event40={event40}",
        f"event80={event80}",
    ]
    cursor = 0
    if explicit_char:
        if cursor >= len(words):
            return fields, False
        fields.append(f"character=0x{words[cursor]:08X}")
        cursor += 1
    if mode == 3 and not event40 and not event80 and not flag5:
        fields.extend(
            [
                "action=delete_all_possession_items",
                "delete_index=0",
            ]
        )
        return fields, cursor == len(words)
    if mode not in (0, 1) or event40 or event80:
        return fields, False
    if cursor >= len(words):
        return fields, False
    item_word = words[cursor]
    cursor += 1
    # Command_19 modes 0/1: low16 resolves through GetAbstractionItemNumber,
    # high16 is the signed count passed to SetPossessionItem.
    fields.extend(
            [
                f"item_id=0x{item_word & 0xFFFF:04X}",
                f"quantity={sign_extend((item_word >> 16) & 0xFFFF, 16)}",
            ]
        )
    if flag5:
        if cursor >= len(words):
            return fields, False
        flags_word = words[cursor]
        cursor += 1
        fields.append(f"post_set_flags=0x{flags_word & 0xFFFF:04X}")
    return fields, cursor == len(words)

def build_personal_inventory_words(fields: dict[str, str], line_no: int) -> list[int]:
    require_fields(fields, {"explicit_char", "mode", "flag5", "event40", "event80"}, line_no, "personal_inventory")
    explicit_char = parse_hex_int(fields["explicit_char"])
    mode = parse_hex_int(fields["mode"])
    flag5 = parse_hex_int(fields["flag5"])
    event40 = parse_hex_int(fields["event40"])
    event80 = parse_hex_int(fields["event80"])
    if explicit_char not in (0, 1) or mode not in (0, 1, 2, 3) or any(value not in (0, 1) for value in (flag5, event40, event80)):
        raise ValueError(f"line {line_no}: personal_inventory fields out of range")
    if event40 or event80:
        raise ValueError(f"line {line_no}: named personal_inventory fields do not support event-value item/count operands yet")
    words: list[int] = []
    if explicit_char:
        require_fields(fields, {"character"}, line_no, "personal_inventory")
        words.append(parse_hex_int(fields["character"]) & 0xFFFFFFFF)
    if mode == 3:
        if flag5:
            raise ValueError(f"line {line_no}: personal_inventory mode 3 does not use post-set flags")
        if "delete_index" in fields and parse_hex_int(fields["delete_index"]) != 0:
            raise ValueError(f"line {line_no}: personal_inventory mode 3 deletes possession index 0 repeatedly")
        return words
    if mode == 2:
        require_fields(fields, {"words"}, line_no, "personal_inventory")
        words.extend(parse_optional_word_list(fields))
        return words
    if "item_word" in fields:
        item_word = parse_hex_int(fields["item_word"]) & 0xFFFFFFFF
        low_field = fields.get("operand_low16", fields.get("item_id"))
        high_field = fields.get("operand_high16", fields.get("quantity"))
        if low_field is not None and parse_hex_int(low_field) != (item_word & 0xFFFF):
            raise ValueError(f"line {line_no}: personal_inventory operand_low16 does not match item_word")
        if high_field is not None and parse_hex_int(high_field) != sign_extend((item_word >> 16) & 0xFFFF, 16):
            raise ValueError(f"line {line_no}: personal_inventory operand_high16 does not match item_word")
    else:
        if "operand_low16" not in fields and "item_id" in fields:
            fields["operand_low16"] = fields["item_id"]
        if "operand_high16" not in fields and "quantity" in fields:
            fields["operand_high16"] = fields["quantity"]
        require_fields(fields, {"operand_low16", "operand_high16"}, line_no, "personal_inventory")
        operand_low16 = parse_hex_int(fields["operand_low16"])
        operand_high16 = parse_hex_int(fields["operand_high16"])
        if not 0 <= operand_low16 <= 0xFFFF or not -0x8000 <= operand_high16 <= 0x7FFF:
            raise ValueError(f"line {line_no}: personal_inventory operand fields out of range")
        item_word = operand_low16 | ((operand_high16 & 0xFFFF) << 16)
    words.append(item_word)
    if flag5:
        require_fields(fields, {"post_set_flags"}, line_no, "personal_inventory")
        post_set_flags = parse_hex_int(fields["post_set_flags"])
        if not 0 <= post_set_flags <= 0xFFFF:
            raise ValueError(f"line {line_no}: personal_inventory post_set_flags must fit unsigned 16-bit")
        words.append(post_set_flags)
    return words

def decode_trigger_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    if not words:
        return [], False
    trigger_word = words[0]
    action = arg >> 6
    trigger_type = trigger_word & 0xFF
    raw_mid = (trigger_word >> 8) & 0xFF
    trigger_flags = (trigger_word >> 16) & 0xFFFF
    fields = [
        f"action={action}",
        f"type=0x{trigger_type:02X}",
    ]
    if raw_mid:
        # Bits 8-15 of the trigger word are never read (Command_03 at
        # 0x002EB960 stores only byte 0 and the high halfword); preserved raw.
        fields.append(f"raw_mid=0x{raw_mid:02X}")
    fields.append(f"trigger_flags=0x{trigger_flags:04X}")
    payload = words[1:]
    if not payload:
        return fields, True
    if trigger_type == 0x06 and len(payload) == 1:
        fields.append(f"character_word=0x{payload[0]:08X}")
        return fields, True
    if trigger_type in (0x01, 0x07, 0x0C) and len(payload) == 1:
        fields.append(f"trigger_value=0x{payload[0]:08X}")
        return fields, True
    if trigger_type == 0x0B and trigger_flags == 0x0002 and len(payload) == 1:
        fields.append(f"trigger_value=0x{payload[0]:08X}")
        return fields, True
    if trigger_type == 0x0A and len(payload) == 4:
        fields.append(fixed_name_field("name_words", payload))
        return fields, True
    return fields, False

def build_trigger_words(fields: dict[str, str], line_no: int) -> list[int]:
    require_fields(fields, {"action", "type", "trigger_flags"}, line_no, "trigger")
    trigger_type = parse_hex_int(fields["type"])
    raw_mid = parse_hex_int(fields.get("raw_mid", "0"))
    trigger_flags = parse_hex_int(fields.get("trigger_flags", "0"))
    if not 0 <= trigger_type <= 0xFF or not 0 <= raw_mid <= 0xFF or not 0 <= trigger_flags <= 0xFFFF:
        raise ValueError(f"line {line_no}: trigger type/raw_mid/trigger_flags out of range")
    trigger_word = trigger_type | (raw_mid << 8) | (trigger_flags << 16)
    if "payload" in fields:
        return [trigger_word, *parse_optional_word_list(fields, "payload")]
    if "character_word" in fields:
        return [trigger_word, parse_hex_int(fields["character_word"]) & 0xFFFFFFFF]
    if "trigger_value" in fields:
        return [trigger_word, parse_hex_int(fields["trigger_value"]) & 0xFFFFFFFF]
    if "name" in fields or "name_words" in fields:
        return [trigger_word, *(resolve_fixed_name_words(fields, "name_words", line_no) or [])]
    return [trigger_word]

def decode_auto_rate_fields(arg: int, words: list[int]) -> tuple[list[str], bool]:
    explicit_char = arg & 0x01
    cursor = 0
    fields = [
        f"explicit_char={explicit_char}",
        f"mode={(arg >> 1) & 0x03}",
        f"with_child={(arg >> 3) & 0x01}",
        f"event_duration={(arg >> 4) & 0x01}",
    ]
    if explicit_char:
        if cursor >= len(words):
            return fields, False
        fields.append(f"character=0x{words[cursor]:08X}")
        cursor += 1
    if cursor + 2 > len(words):
        return fields, False
    control = words[cursor]
    duration_word = words[cursor + 1]
    cursor += 2
    b0 = control & 0xFF
    b1 = (control >> 8) & 0xFF
    b2 = (control >> 16) & 0xFF
    b3 = (control >> 24) & 0xFF
    duration_text = f"event:0x{duration_word & 0xFFFF:04X}" if arg & 0x10 else str(sign_extend(duration_word, 32))
    fields.append(f"duration={duration_text}")
    if arg & 0x10 and duration_word >> 16:
        fields.append(f"duration_word=0x{duration_word:08X}")
    fields.append(f"action={'stop' if sign_extend(duration_word, 32) < 0 and not (arg & 0x10) else 'play'}")
    if (b3 >> 5) & 0x01:
        fields.append("target_child=1")
    if b3 & ~0x20:
        # Byte 3's remaining bits feed the CCharaAutoRateAnim option flags
        # (bit4 -> flag bit 6, bit6 -> bit 25, bit7 -> bits 7/16); preserved
        # as one packed field until those flags are named.
        fields.append(f"option_bits=0x{b3 & ~0x20:02X}")

    name_source = (arg >> 1) & 0x03
    if name_source == 1:
        if cursor + 4 > len(words):
            return fields, False
        fields.append(fixed_name_field("target_name_words", words[cursor:cursor + 4]))
        cursor += 4

    if sign_extend(duration_word, 32) >= 0 or (arg & 0x10):
        if b0 & 0x02:
            fields.append(f"color_rate=1")
            fields.append(f"color_mode={(b0 >> 3) & 0x03}")
            fields.append(f"color_flag2={(b0 >> 2) & 0x01}")
            if b0 & 0x01:
                if cursor + 3 > len(words):
                    return fields, False
                fields.append(f"color_start_vec={format_vec3_words(words[cursor:cursor + 3])}")
                cursor += 3
            if cursor + 3 > len(words):
                return fields, False
            fields.append(f"color_end_vec={format_vec3_words(words[cursor:cursor + 3])}")
            cursor += 3
        if b0 & 0x40:
            fields.append("transparent=1")
            fields.append(f"transparent_mode={b1 & 0x03}")
            fields.append(f"transparent_flag7={(b0 >> 7) & 0x01}")
            if b0 & 0x20:
                if cursor >= len(words):
                    return fields, False
                fields.append(f"transparent_from={format_f32(u32_to_f32(words[cursor]))}")
                cursor += 1
            else:
                fields.append("transparent_from=-1")
            if cursor >= len(words):
                return fields, False
            fields.append(f"transparent_to={format_f32(u32_to_f32(words[cursor]))}")
            cursor += 1
        if b1 & 0x08:
            fields.append("scale=1")
            fields.append(f"scale_flag4={(b1 >> 4) & 0x01}")
            if b1 & 0x04:
                if cursor + 3 > len(words):
                    return fields, False
                fields.append(f"scale_start_vec={format_vec3_words(words[cursor:cursor + 3])}")
                cursor += 3
            if cursor + 3 > len(words):
                return fields, False
            fields.append(f"scale_end_vec={format_vec3_words(words[cursor:cursor + 3])}")
            cursor += 3
        if b1 & 0x40:
            fields.append("palette=1")
            fields.append(f"palette_flag7={(b1 >> 7) & 0x01}")
            if b1 & 0x20:
                if cursor >= len(words):
                    return fields, False
                fields.append(f"palette_from={format_f32(u32_to_f32(words[cursor]))}")
                cursor += 1
            else:
                fields.append("palette_from=-1")
            if cursor + 2 > len(words):
                return fields, False
            fields.append(f"palette_to={format_f32(u32_to_f32(words[cursor]))}")
            fields.append(f"palette_id=0x{words[cursor + 1]:08X}")
            cursor += 2
        if b2 & 0x02:
            fields.append("visibility=1")
            fields.append(f"visibility_flag2={(b2 >> 2) & 0x01}")
            if b2 & 0x01:
                if cursor >= len(words):
                    return fields, False
                fields.append(f"visibility_from={format_f32(u32_to_f32(words[cursor]))}")
                cursor += 1
            else:
                fields.append("visibility_from=-1")
            if cursor >= len(words):
                return fields, False
            fields.append(f"visibility_to={format_f32(u32_to_f32(words[cursor]))}")
            cursor += 1
    return fields, cursor == len(words)

def build_auto_rate_words(fields: dict[str, str], line_no: int) -> list[int]:
    require_fields(fields, {"explicit_char"}, line_no, "character_auto_rate_anim")
    if "words" in fields:
        # Truncated decode: the words= tail carries the full raw payload and
        # any named payload fields on the line are informational duplicates.
        return parse_optional_word_list(fields)
    explicit_char = parse_hex_int(fields["explicit_char"])
    words: list[int] = []
    if explicit_char:
        require_fields(fields, {"character"}, line_no, "character_auto_rate_anim")
        words.append(parse_hex_int(fields["character"]) & 0xFFFFFFFF)
    control = resolve_packed_word(fields, line_no, "character_auto_rate_anim", "control", AUTO_RATE_CONTROL_SPECS, required=False)
    if "control" not in fields:
        # Presence-derived bits: each optional payload implies its gate bit,
        # and byte 3 packs target_child plus the preserved option bits.
        if "color_start_vec" in fields:
            control |= 0x01
        if fields.get("transparent_from", "-1") != "-1":
            control |= 0x20
        if "scale_start_vec" in fields:
            control |= 0x04 << 8
        if fields.get("palette_from", "-1") != "-1":
            control |= 0x20 << 8
        if fields.get("visibility_from", "-1") != "-1":
            control |= 0x01 << 16
        control |= (parse_hex_int(fields.get("target_child", "0")) & 0x01) << 29
        control |= (parse_hex_int(fields.get("option_bits", "0")) & 0xDF) << 24
    if "duration_word" in fields:
        duration_word = parse_hex_int(fields["duration_word"]) & 0xFFFFFFFF
    else:
        require_fields(fields, {"duration"}, line_no, "character_auto_rate_anim")
        duration_text = fields["duration"]
        if duration_text.startswith("event:"):
            duration_word = parse_hex_int(duration_text[6:]) & 0xFFFFFFFF
        else:
            duration_word = int(duration_text, 0) & 0xFFFFFFFF
    words.extend([control, duration_word])
    b0 = control & 0xFF
    b1 = (control >> 8) & 0xFF
    b2 = (control >> 16) & 0xFF

    mode = parse_hex_int(fields.get("mode", "0"))
    if mode == 1:
        if "target_name" not in fields:
            require_fields(fields, {"target_name_words"}, line_no, "character_auto_rate_anim")
        name_words = (resolve_fixed_name_words(fields, "target_name_words", line_no) or [])
        if len(name_words) != 4:
            raise ValueError(f"line {line_no}: target_name_words expects four words")
        words.extend(name_words)

    is_play = sign_extend(duration_word, 32) >= 0 or parse_hex_int(fields.get("event_duration", "0"))
    if is_play:
        if b0 & 0x02:
            if b0 & 0x01:
                require_fields(fields, {"color_start_vec"}, line_no, "character_auto_rate_anim")
                words.extend(parse_vec3_words(fields["color_start_vec"], line_no, "color_start_vec"))
            require_fields(fields, {"color_end_vec"}, line_no, "character_auto_rate_anim")
            words.extend(parse_vec3_words(fields["color_end_vec"], line_no, "color_end_vec"))
        if b0 & 0x40:
            if b0 & 0x20:
                require_fields(fields, {"transparent_from"}, line_no, "character_auto_rate_anim")
                words.append(f32_to_u32(float(fields["transparent_from"])))
            require_fields(fields, {"transparent_to"}, line_no, "character_auto_rate_anim")
            words.append(f32_to_u32(float(fields["transparent_to"])))
        if b1 & 0x08:
            if b1 & 0x04:
                require_fields(fields, {"scale_start_vec"}, line_no, "character_auto_rate_anim")
                words.extend(parse_vec3_words(fields["scale_start_vec"], line_no, "scale_start_vec"))
            require_fields(fields, {"scale_end_vec"}, line_no, "character_auto_rate_anim")
            words.extend(parse_vec3_words(fields["scale_end_vec"], line_no, "scale_end_vec"))
        if b1 & 0x40:
            if b1 & 0x20:
                require_fields(fields, {"palette_from"}, line_no, "character_auto_rate_anim")
                words.append(f32_to_u32(float(fields["palette_from"])))
            require_fields(fields, {"palette_to", "palette_id"}, line_no, "character_auto_rate_anim")
            words.append(f32_to_u32(float(fields["palette_to"])))
            words.append(parse_hex_int(fields["palette_id"]) & 0xFFFFFFFF)
        if b2 & 0x02:
            if b2 & 0x01:
                require_fields(fields, {"visibility_from"}, line_no, "character_auto_rate_anim")
                words.append(f32_to_u32(float(fields["visibility_from"])))
            require_fields(fields, {"visibility_to"}, line_no, "character_auto_rate_anim")
            words.append(f32_to_u32(float(fields["visibility_to"])))
    return words

OPCODE_NOTES: dict[int, dict[str, str]] = {
    0x00: {
        "name": "end_script",
        "evidence": "Command_00 clears the SCR_DATA slot with memset(0xE0) and returns 1.",
    },
    0x02: {
        "name": "conditional_relative_jump",
        "evidence": "Command_02 reads one branch word, optionally reads an 8-byte condition payload and calls CheckCondition, then writes SCR_DATA+0x0C and returns 2.",
    },
    0x03: {
        "name": "various_trigger",
        "evidence": "Command_03 builds an RVTC_DATA record, can register it in SCR_DATA+0x80 slots, call CVariousTrigger::Checking, or clear matching trigger slots.",
    },
    0x04: {
        "name": "script_start_or_inline_call",
        "evidence": "Command_04 may call CheckCondition, SetScriptNumber, SetDefaultCharacter, SetDefaultObjectName, memcpy, and StepProcess.",
    },
    0x05: {
        "name": "conditional_end_script",
        "evidence": "Command_05 optionally calls CheckCondition and, if the condition passes or no condition is present, calls Command_00.",
    },
    0x06: {
        "name": "load_script_file",
        "evidence": "Command_06 reads one operand, adjusts its high bit from handler_arg_byte bit 0, and calls CRadiScript::LoadScriptFile.",
    },
    0x01: {
        "name": "script_start_with_stack_data",
        "evidence": "Command_01 may call CheckCondition, allocate/copy stack script data, call Command_00, and start a script slot with SetScriptNumber.",
    },
    0x0A: {
        "name": "change_game_mode_and_flags",
        "evidence": "Command_0a calls CRadiScene::ChangeGameMode and then sets a run of config/event flags with CRadiApp::SetConfigEventFlag.",
    },
    0x0B: {
        "name": "script_stop",
        "evidence": "Command_0b may call CheckCondition, resolves a character abstraction, searches for a script slot with SearchScriptSlot, then calls StopScript.",
    },
    0x0D: {
        "name": "marker_seek",
        "evidence": "Command_0d resolves a marker selector from direct data, config flags, event values, or Radiata time, scans marker records with GetMarkerAddress, then writes SCR_DATA+0x0C to jump to the selected marker.",
    },
    0x0F: {
        "name": "nop_0f",
        "evidence": "Command_0f immediately returns 0.",
    },
    0x10: {
        "name": "set_config_event_flags",
        "evidence": "Command_10 reads one u32 operand, uses low16 as the first flag id and high bits as bool values, then calls CRadiApp::SetConfigEventFlag.",
    },
    0x11: {
        "name": "scene_save_env_push_pop",
        "evidence": "Command_11 creates CRadiSceneSaveEnv as needed and pushes/pops radi time, map animation, camera halt, ambient, light, character halt/disp, map disp, camera system, and window status.",
    },
    0x12: {
        "name": "time_schedule_event_value_control",
        "evidence": "Command_12 can call CRadiApp::SetRadiataTime, SetEventValueForScript, GetEventValueForScript, GetAbstractionCharacterNumber, and CCharacterPerson::TraverseScheduleList.",
    },
    0x13: {
        "name": "radiata_time_enable_control",
        "evidence": "Command_13 updates Radi time bits and calls CRadiApp::SetRadiataTimeEnable.",
    },
    0x14: {
        "name": "eval_int_expression",
        "evidence": "Command_14 evaluates two typed operands, applies an arithmetic/logical operation, and can write the result to flags, event values, character data, item data, or script-local floats.",
    },
    0x15: {
        "name": "set_script_defaults",
        "evidence": "Command_15 calls SetDefaultCharacter and SetDefaultObjectName; one branch resolves the character id through GetEventValueForScript.",
    },
    0x16: {
        "name": "battle_result_drop_setup",
        "evidence": "Command_16 configures the next battle: word0 = battle map|bgm (read by GameModeChangeProc_Battle/State_LoadMapStart), the count bytes are CBtlFinishCheck condition preset ids with one parameter word each, tail0 low16 = battle script file (LoadScriptFile(id|0x8000)), the manager word's low16 at app+0x84C makes SettingMap skip the map-init script, and it registers the default character with CBtlAcquisition::AddDroppedItem.",
    },
    0x17: {
        "name": "battle_result_character_entry",
        "evidence": "Command_17 resolves a character id, builds a 0x14-byte record, and inserts it into the same global battle/acquisition command buffer used by Command_16.",
    },
    0x18: {
        "name": "party_membership_control",
        "evidence": "Command_18 resolves a character abstraction and calls CRadiApp::ReleaseParty or CRadiApp::AddParty, with another branch editing party slots directly.",
    },
    0x19: {
        "name": "personal_item_inventory",
        "evidence": "Command_19 resolves a personal data record and calls CPersonalData item inventory methods such as SetPossessionItem, GetOneItemNum, GetItemInfo, and DeletePossessionItem.",
    },
    0x1A: {
        "name": "character_equipment_control",
        "evidence": "Command_1a resolves character and item abstractions, then calls CPersonalData item lookup and CCharaEquipmentManager methods such as SetEquipedItemByIndex, EquipAllType, DispEpuipment, and ReflectArmorModelingType.",
    },
    0x1B: {
        "name": "window_message_dispatch",
        "evidence": "Command_1b branches through many message/window cases, resolves character/item/event abstractions, may AttachMessageData, and repeatedly calls CRadiWindowManager::SendMessage.",
    },
    0x1C: {
        "name": "stand_position_to_current_context",
        "evidence": "Command_1c reads stand-position data, calls GetAbstractionStandPosNumber when needed, and writes position/posture fields into the current global script context.",
    },
    0x20: {
        "name": "character_data_load_control",
        "evidence": "Command_20 resolves/creates characters and calls CCharacterManager/CRadiDataCenter load and release methods for modeling, action, animation, and algorithm data.",
    },
    0x21: {
        "name": "character_delete_or_detach_data",
        "evidence": "Command_21 resolves a character and calls CCharacterManager::DeleteCharacter or CCharacterManager::DetachData_Main depending on command flags.",
    },
    0x23: {
        "name": "character_animation_control",
        "evidence": "Command_23 resolves a character id, gets its animation/motion objects, and calls CMotionSwitchData/CCharacterAnim methods to switch or set animation.",
    },
    0x24: {
        "name": "character_virtual_command_24",
        "evidence": "Command_24 resolves a character with GetCharacterID/GetCharacterClass2, calls one character virtual method, and touches a character sub-manager; the virtual target needs a separate trace before a higher-level name.",
    },
    0x25: {
        "name": "character_sub_animation_control",
        "evidence": "Command_25 resolves a character and calls CCharacterAnim sub-animation control, including CCharacterAnim::StopSubAnimation in one traced branch.",
    },
    0x26: {
        "name": "character_or_background_attach_parent",
        "evidence": "Command_26 resolves one or two characters, searches SSF names, and calls CCharacterManager::AttachParent, CBackGround::AttachParent, or CCharacterCollision::SetCollisionAttribute.",
    },
    0x27: {
        "name": "character_or_background_detach_parent",
        "evidence": "Command_27 resolves a character and calls either CCharacterManager::DetachParent or CBackGround::DetachParent depending on character/background state.",
    },
    0x28: {
        "name": "character_move_point_buffer",
        "evidence": "Command_28 resolves a character move object, calls CCharacterMove::AllocBuffer, resolves stand positions, and calls CCharacterMove::SetPointData.",
    },
    0x29: {
        "name": "character_move_pause",
        "evidence": "Command_29 resolves a character, gets its CCharacterMove object, and calls CCharacterMove::MovePause(int).",
    },
    0x2A: {
        "name": "character_movement_control",
        "evidence": "Command_2a resolves a character move object and calls movement/connect/throw/vibration/fall/rotation helpers such as MoveStart, VibrationPosition, ThrowPosition, StraightMove, SetConnectTarget*, and FallStart.",
    },
    0x2B: {
        "name": "character_precreate_animation_control",
        "evidence": "Command_2b resolves a character and calls CCharacterAnim::PrecreateAnimationCtrl(unsigned int,int).",
    },
    0x1F: {
        "name": "set_pause_float",
        "evidence": "Command_1f reads one float operand and stores it to a global pause-related float if CRadiApp::WhetherCanPause() is non-negative.",
    },
    0x2E: {
        "name": "nop_2e",
        "evidence": "Command_2e immediately returns 0.",
    },
    0x22: {
        "name": "character_attach_render_setup",
        "evidence": "Command_22 resolves a character, may call CCharacterManager::AttachData, toggles no-render flags, and calls InhibitionCharacter.",
    },
    0x2D: {
        "name": "character_rotate_or_option_target",
        "evidence": "Command_2d resolves a character and calls CCharacterRotate/CCharacterOption target methods such as SetTargetVector, SetTargetCharacter, SetTargetMapObject, SetTargetPosture, SetMovePosture, and SetHeadAngle.",
    },
    0x2F: {
        "name": "character_attribute_or_collision",
        "evidence": "Command_2f resolves a character and calls CCharacterManager::SetCharaCollisionAttribute and CCharacterManager::SetCharacterAttribute.",
    },
    0x30: {
        "name": "character_move_position",
        "evidence": "Command_30 resolves a character, decodes a position through GetPosisionCode30_38, and either sets CCharacter position or calls CCharacterMove::MovePosition.",
    },
    0x31: {
        "name": "character_collision_setup",
        "evidence": "Command_31 resolves a character, writes collision-related fields, then calls GetCharacterCollisionClass and CCharacterCollision::SetupCollisionData.",
    },
    0x32: {
        "name": "character_auto_rate_visual_animation",
        "evidence": "Command_32 resolves a character and calls CCharaAutoRateAnim play/stop methods for color, transparency, scale, palette morph, and visibility animations.",
    },
    0x33: {
        "name": "character_eye_control",
        "evidence": "Command_33 resolves a character option object and calls CEyeControl methods including SetEyeBallNo, SetEyeMoveType, and SetEyeMoveManual.",
    },
    0x34: {
        "name": "character_expression_control",
        "evidence": "Command_34 resolves a character option object and calls CExpressionControl::BlinkControll and CExpressionControl::MouthControll.",
    },
    0x35: {
        "name": "character_single_manager_command",
        "evidence": "Command_35 resolves a character, gets a character sub-manager, creates/deletes CSingleManager data, and dispatches several virtual constructors or append methods.",
    },
    0x36: {
        "name": "character_animation_signal_query",
        "evidence": "Command_36 resolves a character/SSF object and can call CCharacterAnim::GetPlayingAnimationSignal, GetEventValueForScript, and SetEventValueForScript.",
    },
    0x39: {
        "name": "character_event_leave_manager",
        "evidence": "Command_39 gets CCharaEventLeaveManager and calls EnterAll, AddEnterCharacter, or ReleaseEnterCharacter.",
    },
    0x3B: {
        "name": "strong_motion_blend_for_dynamics",
        "evidence": "Command_3b resolves a character and calls CSsfHandler2::StrongMotionBlendForDynamics(float) on its SSF handler.",
    },
    0x40: {
        "name": "background_load_data",
        "evidence": "Command_40 reads a background id or event-value-derived id and calls CBackGround::LoadData.",
    },
    0x42: {
        "name": "background_setting_map",
        "evidence": "Command_42 calls CBackGround::SettingMap and records a background/map command entry in the shared command queue.",
    },
    0x43: {
        "name": "background_delete_data",
        "evidence": "Command_43 calls CBackGround::DeleteData with handler_arg_byte&3.",
    },
    0x44: {
        "name": "background_change_map_with_drop_sort",
        "evidence": "Command_44 calls CBackGround::ChangeMap, queues a background/map command entry, and calls CBtlAcquisition::SortDroppedItem when the character sub-manager exists.",
    },
    0x45: {
        "name": "background_play_animation",
        "evidence": "Command_45 calls CBackGround::PlayAnimation with the control word's low half as the animation id. An inline name occupies a fixed four-word slot (the cursor always advances 16 bytes past it), then up to three floats follow, defaulting to 0, 0, 1.0; control bit 15 replaces the third float with a character reference.",
    },
    0x46: {
        "name": "background_stop_animation",
        "evidence": "Command_46 reads a name payload and calls CBackGround::StopAnimation.",
    },
    0x47: {
        "name": "background_display_visibility",
        "evidence": "Command_47 branches on handler_arg_byte and calls CBackGround::SetBgDisp, SetBgVisibility, or SetLightShadowEnable.",
    },
    0x48: {
        "name": "landscape_group_visibility",
        "evidence": "Command_48 calls CRadiLandscape::GetGroupHeader and CHierarchicalObject::SetVisibility.",
    },
    0x4A: {
        "name": "position_vibration_param",
        "evidence": "Command_4a reads a CVibrationVector attribute word plus six float parameters and calls CVibrationVector::SetParam; a zero enable word clears the active global position vibration state.",
    },
    0x4C: {
        "name": "background_runtime_field_update",
        "evidence": "Command_4c validates gpBackGround state, then writes background/runtime fields including a 64-bit value, a scaled float, and Radi+0x180.",
    },
    0x4D: {
        "name": "background_auto_rate_animation",
        "evidence": "Command_4d calls CAutoRateManager play/stop methods for color, transparency, scale, palette morph, and visibility animations.",
    },
    0x50: {
        "name": "camera_select_or_target",
        "evidence": "Command_50 reads two halfwords from the command stream and calls CRadiCameraSystem::SelectCamera and/or SetCameraTarget, then marks camera/display instant mode.",
    },
    0x51: {
        "name": "camera_system_mode_control",
        "evidence": "Command_51 branches on handler_arg_byte&0x0F, mutates CRadiCameraSystem/CRLHCamera state, and calls MoveCameraRail or SetParamFromCamera in traced modes.",
    },
    0x52: {
        "name": "camera_transform_param_control",
        "evidence": "Command_52 branches on handler_arg_byte and control bits, calls CCharacterCapture::Capture, CRLHCamera AddAcceleration/SetRotate, and CAutoRateBase::SetParam for camera parameters.",
    },
    0x54: {
        "name": "camera_move_etc",
        "evidence": "Command_54 resolves camera objects, character/SSF targets, stand positions, and calls CRadiCameraSystem::MoveCameraEtc.",
    },
    0x55: {
        "name": "camera_capture_target_control",
        "evidence": "Command_55 configures the camera capture target, position/posture enable bits, offsets, Euler posture, and camera target coefficients through CCharacterCapture and CRadiCameraSystem.",
    },
    0x56: {
        "name": "camera_move_etc_from_existing_info",
        "evidence": "Command_56 is a short wrapper that calls CRadiCameraSystem::MoveCameraEtc using the current camera system pointer and an existing RCM_MVINFO pointer from script state.",
    },
    0x57: {
        "name": "position_vibration_vector",
        "evidence": "Command_57 allocates CVibrationVector data and calls CVibrationVector::SetParam with position-vibration attributes before dispatching virtual attach/start calls.",
    },
    0x58: {
        "name": "position_vibration_clear",
        "evidence": "Command_58 deletes CSingleManager elements and calls CHierarchicalObject::MakePivotMatrix in two vibration/vector cleanup branches.",
    },
    0x59: {
        "name": "camera_or_color_animation",
        "evidence": "Command_59 branches on handler_arg_byte>>4; modes 0-7 call CRadiCameraSystem::MoveCameraEtc2, mode 0xE calls PlayFogColorAnim, and mode 0xF calls CBackGround::PlayAmbientColorAnim.",
    },
    0x5A: {
        "name": "camera_flag_toggles",
        "evidence": "Command_5a writes three boolean bits from handler_arg_byte into CRadiCameraSystem fields and its child object.",
    },
    0x60: {
        "name": "load_texture_file",
        "evidence": "Command_60 reads a texture/file word, splits id fields, and calls LoadTextureFile.",
    },
    0x61: {
        "name": "load_paf_file",
        "evidence": "Command_61 reads one PAF id operand and calls CRadiDataCenter::LoadPafFile.",
    },
    0x62: {
        "name": "sprite_config_bitfield",
        "evidence": "Command_62 reads a flag word and consumes variable operands to update a CSpriteG_Base slot: coordinates, texture page, colors/rects, floats, and virtual sprite methods.",
    },
    0x63: {
        "name": "primitive_animation_slot_control",
        "evidence": "Command_63 selects a primitive animation slot, calls a slot virtual function, and can call CPrimitiveAnimation::DeleteSlot.",
    },
    0x64: {
        "name": "primitive_play_paf_sequence",
        "evidence": "Command_64 calls CPrimitiveAnimation::GetPafAddress, PlayPafSequence, and SetOffset.",
    },
    0x65: {
        "name": "primitive_stop_paf_sequence",
        "evidence": "Command_65 calls CPrimitiveAnimation::GetPafAddress and CPrimitiveAnimation::StopPafSequence.",
    },
    0x66: {
        "name": "primitive_set_priority",
        "evidence": "Command_66 selects a primitive helper slot from handler_arg_byte&3 and calls CPrimitiveHelper::SetPriority.",
    },
    0x67: {
        "name": "fade_control",
        "evidence": "Command_67 branches on handler_arg_byte>>4 and calls CRadiFade::SetColorFade, SetDissolves, or SetBattleInFade.",
    },
    0x68: {
        "name": "global_object_visual_state",
        "evidence": "Command_68 writes through gpGlobalDB/gpObjectManager and Radi fields, consuming bit-selected floats/words for visual state; no named calls are made in the traced body.",
    },
    0x69: {
        "name": "primitive_helper_byte_control",
        "evidence": "Command_69 selects a helper slot from handler_arg_byte&7 and stores handler_arg_byte>>3 to helper offset +0x0C.",
    },
    0x6A: {
        "name": "primitive_move_sprtg",
        "evidence": "Command_6a repeatedly calls CPrimitiveAnimation::MovePrimitiveSPRTG for selected primitive helper slots.",
    },
    0x70: {
        "name": "set_bgm",
        "evidence": "Command_70 builds a RADIBGM_INF structure from two operands and calls CRadiSound::SetBgm.",
    },
    0x71: {
        "name": "play_bgm",
        "evidence": "Command_71 calls CRadiSound::PlayBgm with handler_arg_byte & 3.",
    },
    0x72: {
        "name": "stop_or_pause_bgm",
        "evidence": "Command_72 reads one operand and calls CRadiSound::StopBgm or CRadiSound::PauseBgm depending on handler_arg_byte bit 0x80.",
    },
    0x73: {
        "name": "set_bgm_volume",
        "evidence": "Command_73 reads one operand and calls CRadiSound::SetBgmVolume(int,unsigned int,unsigned int).",
    },
    0x74: {
        "name": "load_sound_file_or_voice",
        "evidence": "Command_74 branches on handler_arg_byte&0x1F and calls CRadiSound::LoadEventSeFile or CRadiSound::LoadBattleVoiceFile; one mode writes a computed value to CRadiSound+0x74.",
    },
    0x75: {
        "name": "play_sound_effect",
        "evidence": "Command_75 can resolve a character/SSF object and calls CRadiSound::PlaySe; it also calls GetSEBankFromChrNum and CSsfHandler::SearchName in traced branches.",
    },
    0x76: {
        "name": "stop_sound_effect",
        "evidence": "Command_76 resolves optional character abstractions and calls CSoundManager::FadeoutAllSE or CRadiSound::StopSe.",
    },
    0x7C: {
        "name": "play_movie",
        "evidence": "Command_7c reads movie id, signed halfword parameters, and one extra word, then calls CMovieManager::PlayMovie.",
    },
    0x7D: {
        "name": "stop_movie",
        "evidence": "Command_7d calls CMovieManager::StopMovie.",
    },
    0x8E: {
        "name": "text_message_layout",
        "evidence": "Command_8e writes position/layout fields on the global CTextMessage object from three u32 operands.",
    },
    0x8F: {
        "name": "text_message_output",
        "evidence": "Command_8f uses handler_arg_byte>>5 to output SJIS text, output an event value as text, or clear text id 0xFA via CTextMessage methods.",
    },
    0x8A: {
        "name": "window_message_mode_control",
        "evidence": "Command_8a sends several CRadiWindowManager::SendMessage calls and branches on a CRadiApp field at +0x458 and handler_arg_byte flags.",
    },
    0x8B: {
        "name": "talk_bustup_display",
        "evidence": "Command_8b resolves a default or explicit character through GetAbstractionCharacterNumber, gets CCharacterManager::GetCharacterClass2, then calls CTalkBustupTotalControl::BustupDisp.",
    },
    0x79: {
        "name": "sound_listener_point",
        "evidence": "Command_79 resolves optional characters and calls CRadiSound listener-point methods for character, map object, camera, direct vector, or CSoundManager::SetListener.",
    },
    0x7A: {
        "name": "sound_effect_stack",
        "evidence": "Command_7a calls CRadiSound::PushAllSe when arg bit 0 is clear and CRadiSound::PopAllSe when arg bit 0 is set.",
    },
    0x83: {
        "name": "play_vibration",
        "evidence": "Command_83 reads one operand, splits it into bytes/halfwords, and calls CVibPlayer::PlayVibration.",
    },
    0x82: {
        "name": "stop_vibration_sequence",
        "evidence": "Command_82 calls CVibPlayer::StopVibSequence.",
    },
    0xC0: {
        "name": "character_person_schedule_list",
        "evidence": "Command_c0 resolves a character/person and calls CCharacterPersonManager::SetScheduleListNumber.",
    },
    0xC1: {
        "name": "map_change_or_map_inout_check",
        "evidence": "Command_c1 resolves a character/person, can call GetEventValueForScript, CBackGround::LoadData, CBackGround::ChangeMap, and CCharacterPerson::CheckMapInOut.",
    },
    0xC4: {
        "name": "character_person_field_update",
        "evidence": "Command_c4 resolves a character/person, can translate through GetAbstractionCharacterNumber, and writes schedule/person fields such as offsets 0x34A/0x34C/0x34E/0x83/0x332 on the resolved record.",
    },
    0xC5: {
        "name": "set_radiata_time",
        "evidence": "Command_c5 reads time fields from the command stream and calls CRadiApp::SetRadiataTime.",
    },
    0xD5: {
        "name": "special_effect_execute_or_abort",
        "evidence": "Command_d5 builds special-effect parameters, resolves up to two character abstractions, and calls CSpecialEffectManager::ExecuteSpecialEffect_Main or AbortSpecialEffect.",
    },
    0x89: {
        "name": "talk_rmf_start",
        "evidence": "Command_89 calls CRadiScript::AttachMessageData, may call CTalk::RmfExit, then calls CTalk::RmfStart.",
    },
    0xF0: {
        "name": "special_schedule_percent_or_battle_person_command",
        "evidence": "Command_f0 is a special opcode>=0xF0 handler that branches on the command word high nibble; an observed branch resolves a character/person and calls CCharacterPerson::SetSchedulePercent.",
    },
    0xFF: {
        "name": "nop_ff",
        "evidence": "Command_ff immediately returns 0.",
    },
    # Installed handlers not reached by the sampled raw corpus, traced from the
    # debug overlay (handler calls plus direct disassembly for the small ones).
    0x0E: {
        "name": "script_callback_notify",
        "evidence": "Command_0e saves the cursor to SCR_DATA+0x30 and the two argument bytes to SCR_DATA+0x24/+0x25, then calls the callback pointer stored at SCR_DATA+0x2C (step_arg, extra_word_count, handler_arg, cursor) when it is set.",
    },
    0x1D: {
        "name": "set_global_float_266c",
        "evidence": "Command_1d reads one float operand and stores it to the unnamed global at 0x003B266C, next to the pause-gated global Command_1f writes.",
    },
    0x1E: {
        "name": "packing_file_load_or_release",
        "evidence": "Command_1e with arg bit 0 clear reads a halfword id and calls CRadiDataCenter::LoadPackingFile; with bit 0 set it calls CFileObject::ReleaseFile on data-center slot (arg>>4) when below 8.",
    },
    0x2C: {
        "name": "character_persistence_control",
        "evidence": "Command_2c resolves a character and calls CSsfHandler2::SetPersistenceAll(bool) or CCharacter::SetPersistenceWithChild(bool, name) with the boolean taken from arg bit 7.",
    },
    0x37: {
        "name": "character_effect_tail_or_mosaic",
        "evidence": "Command_37 resolves a character (arg bit 0 = explicit selector) and branches on word0: low16 = effect id, byte2 = mode (1 tail, 2 mosaic, 0xFF flag toggle), byte3 = sub mode (1 create, 2 delete, 3 clear-points/params). Inline attach-object names use the fixed four-word slot.",
    },
    0x38: {
        "name": "character_particle_control",
        "evidence": "Command_38 resolves a character, calls CSsfHandler::SearchParticle, and decodes a position with CRadiScript::GetPosisionCode30_38.",
    },
    0x3A: {
        "name": "character_capture_setup",
        "evidence": "Command_3a resolves a character, allocates CCharacterCapture data, and calls Initialize, SetPositionOffset, CQuaternion::Euler, SetTargetCharacter, EnableSetPosition, and EnableSetPosture.",
    },
    0x41: {
        "name": "person_all_check_map_inout",
        "evidence": "Command_41 takes no operands: it gets character sub-manager 0x11 (CCharacterPersonManager) and calls AllCheckMapInOut(1, true).",
    },
    0x49: {
        "name": "landscape_set_position",
        "evidence": "Command_49 reads a group byte and an x,y,z float vector, resolves the landscape from gpBackGround (slot arg&3), then calls CRadiLandscape::GetGroupHeader and CRadiLandscape::SetPosition.",
    },
    0x4B: {
        "name": "background_animation_speed",
        "evidence": "Command_4b resolves an animation name (mode arg&3: none, a fixed four-word inline slot, or SCR_DATA+0x14), reads one float, and calls CBackGround::SetAnimationSpeed(name, arg>>4, speed).",
    },
    0x4E: {
        "name": "background_animation_query_to_event_value",
        "evidence": "Command_4e resolves an object/SSF name, can call CBackGround::GetPlayingAnimationFrame, CVector::GetEuler, and CQuaternion::ToEuler, and reads/writes script event values with the result.",
    },
    0x4F: {
        "name": "map_object_disp_control",
        "evidence": "Command_4f calls CBackGround::ResetMapObjectDisp, CBackGround::SetMapObjectDispByCharacter after resolving a character abstraction, or CBackGround::SetMapObjectDispByCamera.",
    },
    0x53: {
        "name": "camera_animation_play",
        "evidence": "Command_53 reads up to three floats gated by arg bits 0-2 (defaults 0, 0, 1.0) and calls CRadiCameraSystem::PlayCameraAnimation(arg&0xE0, f0, f1, f2).",
    },
    0x78: {
        "name": "sound_field_e0_set",
        "evidence": "Command_78 takes no operands and stores arg&0x0F to gpRadiSound+0xE0.",
    },
    0x80: {
        "name": "load_vibration_data",
        "evidence": "Command_80 reads one halfword id and calls CRadiDataCenter::LoadVibrationData.",
    },
    0x81: {
        "name": "vibration_sequence_play",
        "evidence": "Command_81 resolves a character abstraction, locates act data through CCharacterManager::_get_act_data and GetKodsAddress, then calls CVibPlayer::AttachData and CVibPlayer::PlayVibSequence.",
    },
    0x88: {
        "name": "talk_load_data",
        "evidence": "Command_88 reads one halfword id and calls CTalk::LoadData(gpTalk, 1, id).",
    },
    0xA0: {
        "name": "battle_character_fall_or_plugin_control",
        "evidence": "Command_a0 acts on the script's default character through a 14-entry jump table on arg&0x3F: fall start (mode 1, raw float operands), combo branch (mode 3), plugin activation 0x501/0x502 (modes 4/5), flag-bit writes from arg bit 7 (modes 7/8/9), attack-param/collision attach or Action::DoDamageForVolty into event value 0x11 (mode 0xB), SetStsInvincible (mode 0xC), and MagicChargeCountDownStart (mode 0xD). Battle paths gate on config flag 0xDC.",
    },
    0xA1: {
        "name": "nop_a1",
        "evidence": "Command_a1 immediately returns 0.",
    },
    0xC2: {
        "name": "nop_c2",
        "evidence": "Command_c2 immediately returns 0.",
    },
    0xC3: {
        "name": "person_allow_attribute",
        "evidence": "Command_c3 resolves a character/person abstraction and calls CCharacterPerson::SetAllowAttribute(int, int, int).",
    },
    0xC6: {
        "name": "person_field_control_c6",
        "evidence": "Command_c6 resolves character/person abstractions and updates person record fields directly; it makes no other named engine calls in the traced body.",
    },
    0xC7: {
        "name": "person_include_character",
        "evidence": "Command_c7 resolves a character and calls CCharacterPerson::SetIncludeCharacter(unsigned int, unsigned int, unsigned char, unsigned short).",
    },
    0xD0: {
        "name": "battle_character_make_or_delete",
        "evidence": "Command_d0 makes or reconfigures a battle character: arg bits 0/1/3 gate main/owner/notify character selector words, payload bytes 0-5 are plugin ids for groups 1-6 (0xFF skips), byte 6 a group-4 plugin value (arg bit 2 = relative), byte 7 an s16 field (0xFF -> -2), and an arg-bit-4 word packs team/leader. Builds RADI_BTL_ORDER for MakeBtlCharacter or applies ChangeLeader/ChangeTeamID to an existing one.",
    },
    0xD1: {
        "name": "character_collision_control_d1",
        "evidence": "Command_d1 resolves a character abstraction and an SSF name and operates on the class from CCharacterManager::GetCharacterCollisionClass.",
    },
    0xD2: {
        "name": "character_script_param_holder",
        "evidence": "Command_d2 builds or reads a per-character script param holder (r_chrScriptParamHolder.h): mode arg>>2 selects building a battle-team member list (sieves), picking a free stand position (GetStandPosition + Ground_CanBeArranged, shuffled or sorted by distance to target), listing child characters, or reading one element; the result count/element is always written to the event value named by the selector word's high half.",
    },
    0xD3: {
        "name": "battle_copy_character",
        "evidence": "Command_d3 resolves a character, calls CCharacterManager::CopyCharacter, and writes a result with SetEventValueForScript.",
    },
    0xD4: {
        "name": "battle_volty_distance",
        "evidence": "Command_d4 resolves character abstractions, calls CbtlEtc::Script_CalcVoltyDistance, and calls CRadiApp::ProcessTaskReady.",
    },
    0xD6: {
        "name": "battle_character_send_message",
        "evidence": "Command_d6 resolves a character and calls CBtlCharacter::SendMessage with a constructed CBtlChrPluginMsgParam.",
    },
    0xE0: {
        "name": "chara_put_attach_life_flag",
        "evidence": "Command_e0 resolves a character abstraction and calls CCharaPutManager::AttachLifeFlag(int, int, unsigned int).",
    },
    0xE1: {
        "name": "chara_put_query_flag_numbers",
        "evidence": "Command_e1 calls CCharaPutManager::GetGroupTotalFlagNumber, GetGroupLiveFlagNumber, or GetGroupDefaultLiveFlagNumber and stores the result with SetEventValueForScript.",
    },
    0xE2: {
        "name": "chara_put_set_live_flags",
        "evidence": "Command_e2 reads a group halfword and a value (optionally through GetEventValueForScript) and calls CCharaPutManager::SetGroupLiveFlagNumber.",
    },
}

SPECIAL_SOURCE_OPCODES = {0x00, 0x02, 0x05, 0x0F, 0x10, 0x2E, 0xFF}

SOURCE_OPCODE_ALIASES: dict[str, int] = {
    str(note["name"]).lower(): opcode
    for opcode, note in OPCODE_NOTES.items()
    if "name" in note and opcode not in SPECIAL_SOURCE_OPCODES
}

SOURCE_OPCODE_ALIASES_BY_OPCODE: dict[int, str] = {
    opcode: str(note["name"]).lower()
    for opcode, note in OPCODE_NOTES.items()
    if "name" in note and opcode not in SPECIAL_SOURCE_OPCODES
}

STRUCTURED_SOURCE_FORMS = {
    # The 0xF0 marker sugar. `build_special_f0_friendly` compiles all four, but
    # they reach the builder through the special-opcode path rather than the
    # form table, so without listing them here they read as heads no compiler
    # path claims -- and the index that publishes them looks like it is
    # advertising commands that cannot be written.
    "anim_frame_trigger",
    "anim_script_end",
    "marker",
    "set_schedule_percent",
    ".marker_table",
    "jump",
    "if_flag",
    "if_value",
    "unless_flag",
    "unless_value",
    "background_auto_rate_anim",
    "background_change_map",
    "battle_character_fall_or_plugin_control",
    "chara_put_attach_life_flag",
    "packing_file_load_or_release",
    "person_allow_attribute",
    "sound_field_e0_set",
    "background_play_animation",
    "background_runtime_field",
    "background_stop_animation",
    "background_visibility",
    "battle_acquisition_setup",
    "battle_character_entry",
    "bgm_control",
    "branch",
    "camera_capture_target",
    "camera_color_anim",
    "camera_flags",
    "camera_mode",
    "camera_move_etc",
    "camera_move_existing",
    "camera_select",
    "camera_transform_param",
    "character_anim_signal",
    "character_animation",
    "character_attach_parent",
    "character_attach_render",
    "character_attribute",
    "character_auto_rate_anim",
    "character_collision_setup",
    "character_data",
    "character_delete_data",
    "character_detach_parent",
    "character_equipment",
    "character_event_leave",
    "character_expression",
    "character_eye_control",
    "character_move_pause",
    "character_move_points",
    "character_move_position",
    "character_movement",
    "character_precreate_anim",
    "character_rotate_option",
    "character_single_manager",
    "character_sub_anim",
    "character_virtual_24",
    "change_game_mode",
    "conditional_end",
    "delete_background",
    "end_script",
    "expr",
    "fade_control",
    "global_visual_state",
    "landscape_visibility",
    "load_background",
    "load_paf",
    "load_script_file",
    "load_sound_resource",
    "load_texture",
    "map_change_check",
    "marker_seek",
    "nop",
    "nop_2e",
    "party_member",
    "person_field_update",
    "person_schedule_list",
    "personal_inventory",
    "play_bgm",
    "play_movie",
    "play_sound_effect",
    "play_vibration",
    "position_vibration_clear",
    "position_vibration_param",
    "position_vibration_vector",
    "primitive_anim_slot",
    "primitive_helper_byte",
    "primitive_move_sprtg",
    "primitive_play_paf",
    "primitive_priority",
    "primitive_stop_paf",
    "radiata_time_enable",
    "return_zero",
    "scene_save_env",
    "script_defaults",
    "script_start",
    "script_start_stack",
    "script_stop",
    "set_bgm",
    "set_bgm_volume",
    "set_flags",
    "set_radiata_time",
    "setting_map",
    "sound_effect_stack",
    "sound_listener",
    "special_effect",
    "special_f0",
    "sprite_config",
    "stand_context",
    "stop_movie",
    "stop_sound_effect",
    "strong_motion_blend",
    "talk_bustup_display",
    "talk_rmf",
    "text_message_layout",
    "text_output",
    "time_schedule_value",
    "trigger",
    "vibration_stop",
    "window_message",
    "window_message_mode",
}

def u32(data: bytes, offset: int) -> int:
    if offset + 4 > len(data):
        return 0
    return int.from_bytes(data[offset : offset + 4], "little")

def words_to_bytes(words: list[int]) -> bytes:
    return pack_u32_words(words)

def bytes_to_u32_words(data: bytes) -> list[int]:
    if len(data) % 4:
        raise ValueError("byte data must be 4-byte aligned to pack as command words")
    return [u32(data, index) for index in range(0, len(data), 4)]

def sjis_text_to_words(text: str) -> list[int]:
    raw = text.encode("shift_jis") + b"\x00"
    raw += b"\x00" * ((4 - (len(raw) % 4)) % 4)
    return bytes_to_u32_words(raw)

def words_to_sjis_text(words: list[int]) -> str | None:
    raw = words_to_bytes(words)
    body = raw.rstrip(b"\x00")
    if not body or b"\x00" in body:
        return None
    try:
        text = body.decode("shift_jis")
    except UnicodeDecodeError:
        return None
    if text.encode("shift_jis") != body:
        return None
    return text

def fixed_name_text_and_fill(words: list[int]) -> tuple[str, int] | None:
    """Split a fixed name slot into its text and the byte filling the rest.

    The engine reads these slots as NUL-terminated strings, so whatever follows
    the terminator never reaches it. The original build tool left uninitialised
    memory there, most often 0xCC, which is why so many slots are not simply
    zero-padded. Returns None when the leftover is not one repeated byte, since
    only a uniform filler can be restored from a single field.
    """
    raw = words_to_bytes(words)
    end = raw.find(b"\x00")
    if end <= 0:
        return None
    body, tail = raw[:end], raw[end + 1:]
    fill = tail[0] if tail else 0x00
    if any(byte != fill for byte in tail):
        return None
    try:
        text = body.decode("shift_jis")
    except UnicodeDecodeError:
        return None
    if text.encode("shift_jis") != body:
        return None
    return text, fill

def fixed_name_field(key: str, words: list[int]) -> str:
    """Render a fixed name slot as text when that is byte-exact, falling back
    to the raw word list for slots that cannot be rebuilt from text."""
    found = fixed_name_text_and_fill(words)
    if found is not None:
        text, fill = found
        try:
            if fixed_sjis_name_to_words(text, 0, key, len(words), fill) == [
                word & 0xFFFFFFFF for word in words
            ]:
                base = key[:-6]
                rendered = f"{base}={json.dumps(text, ensure_ascii=False)}"
                if fill:
                    rendered += f" {base}_fill=0x{fill:02X}"
                return rendered
        except ValueError:
            pass
    return f"{key}={words_to_csv(words)}"

def resolve_fixed_name_words(fields: dict[str, str], key: str, line_no: int, count: int = 4) -> list[int] | None:
    """Accept either the text spelling (`name="..."`) or the raw word list."""
    base = key[:-6]
    if base in fields:
        return fixed_sjis_name_to_words(
            parse_source_string(fields[base], line_no, base),
            line_no,
            base,
            count,
            fixed_name_fill_byte(fields, base, line_no),
        )
    if key in fields:
        return parse_word_list(fields[key])
    return None

def fixed_name_words_from_text(
    fields: dict[str, str], base: str, line_no: int, count: int = 4, label: str | None = None
) -> list[int]:
    """Encode a `name="..."` field, honouring its matching `_fill` byte."""
    label = label or base
    return fixed_sjis_name_to_words(
        parse_source_string(fields[base], line_no, label),
        line_no,
        label,
        count,
        fixed_name_fill_byte(fields, base, line_no),
    )

def fixed_name_fill_byte(fields: dict[str, str], base: str, line_no: int) -> int:
    """The byte filling a name slot after its terminator; zero unless stated."""
    fill = parse_hex_int(fields.get(f"{base}_fill", "0"))
    if not 0 <= fill <= 0xFF:
        raise ValueError(f"line {line_no}: {base}_fill must be a single byte")
    return fill

def fixed_sjis_name_to_words(
    text: str, line_no: int, field: str, word_count: int = 4, fill: int = 0x00
) -> list[int]:
    raw = text.encode("shift_jis") + b"\x00"
    max_size = word_count * 4
    if len(raw) > max_size:
        raise ValueError(f"line {line_no}: {field} is too long for {max_size} bytes including terminator")
    raw += bytes([fill & 0xFF]) * (max_size - len(raw))
    return bytes_to_u32_words(raw)

def parse_source_string(value: str, line_no: int, field: str) -> str:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"line {line_no}: {field} must be a JSON-style quoted string") from exc
    if not isinstance(parsed, str):
        raise ValueError(f"line {line_no}: {field} must decode to a string")
    return parsed

def strip_source_comment(raw_line: str) -> str:
    in_string = False
    escape = False
    for index, char in enumerate(raw_line):
        if escape:
            escape = False
            continue
        if char == "\\" and in_string:
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if char == ";" and not in_string:
            return raw_line[:index].strip()
    return raw_line.strip()

def split_source_parts(line: str, line_no: int) -> list[str]:
    parts: list[str] = []
    start: int | None = None
    in_string = False
    escape = False
    for index, char in enumerate(line):
        if start is None and not char.isspace():
            start = index
        if start is None:
            continue
        if escape:
            escape = False
            continue
        if char == "\\" and in_string:
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if char.isspace() and not in_string:
            parts.append(line[start:index])
            start = None
    if in_string:
        raise ValueError(f"line {line_no}: unterminated quoted string")
    if start is not None:
        parts.append(line[start:])
    return parts

def locate_evd(data: bytes) -> int:
    offset = data.find(EVD_MAGIC)
    if offset < 0:
        raise ValueError("not an EVD file or container")
    return offset

def decode_command_at(payload: bytes, offset: int) -> dict[str, Any]:
    if offset < 0 or offset + 4 > len(payload):
        raise ValueError(f"command offset 0x{offset:X} outside payload")
    word = u32(payload, offset)
    opcode = word & 0xFF
    extra_word_count = (word >> 8) & 0xFF
    arg_byte = (word >> 16) & 0xFF
    high_byte = (word >> 24) & 0xFF
    end = offset + 4 if opcode >= 0xF0 else offset + 4 + extra_word_count * 4
    command: dict[str, Any] = {
        "offset": offset,
        "end_offset": min(end, len(payload)),
        "word": word,
        "opcode": opcode,
        "known": OPCODE_NOTES.get(opcode),
        "extra_word_count": extra_word_count,
        "arg_byte": arg_byte,
        "high_byte": high_byte,
    }
    if end > len(payload):
        command["truncated"] = True
        return command
    args_u32 = [] if opcode >= 0xF0 else [u32(payload, offset + 4 + i * 4) for i in range(extra_word_count)]
    command["args_u32"] = args_u32
    details = command_details(payload, offset, opcode, arg_byte, args_u32)
    if details:
        command["details"] = details
    return command

def sign_extend(value: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    return (value ^ sign) - sign

def u32_to_f32(value: int) -> float:
    return struct.unpack("<f", value.to_bytes(4, "little"))[0]

def f32_to_u32(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", value))[0]

def format_f32(value: float) -> str:
    return format(value, ".9g")

def s16_pair_from_word(word: int) -> tuple[int, int]:
    return sign_extend(word & 0xFFFF, 16), sign_extend((word >> 16) & 0xFFFF, 16)

def u16_pair_from_word(word: int) -> tuple[int, int]:
    return word & 0xFFFF, (word >> 16) & 0xFFFF

def sprite_config_fields(control: int, payload: list[int]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    cursor = 0

    def take_word(kind: str, bit: int) -> int | None:
        nonlocal cursor
        if not control & bit:
            return None
        if cursor >= len(payload):
            fields.append({"kind": kind, "bit": bit, "missing": 1})
            return None
        word = payload[cursor]
        cursor += 1
        return word

    word = take_word("status_bits", 0x001)
    if word is not None:
        fields.append({"kind": "status_bits", "bit": 0x001, "word": word})
    word = take_word("field_10", 0x002)
    if word is not None:
        fields.append({"kind": "field_10", "bit": 0x002, "word": word})
    word = take_word("xy_s16", 0x004)
    if word is not None:
        x, y = s16_pair_from_word(word)
        fields.append({"kind": "xy_s16", "bit": 0x004, "word": word, "x": x, "y": y})
    word = take_word("texture_page", 0x008)
    if word is not None:
        tpage_mode = (word >> 8) & 0x0F
        mapped_tpage = {0: -1, 1: 0x1B, 2: 0x24, 3: 0x2C}.get(tpage_mode)
        fields.append(
            {
                "kind": "texture_page",
                "bit": 0x008,
                "word": word,
                "tex_low": word & 0xFF,
                "tpage_mode": tpage_mode,
                "mapped_tpage": mapped_tpage,
            }
        )
    for kind, bit in (("uv_u16", 0x010), ("rect0_u16", 0x020), ("rect1_u16", 0x040)):
        word = take_word(kind, bit)
        if word is not None:
            a, b = u16_pair_from_word(word)
            fields.append({"kind": kind, "bit": bit, "word": word, "a": a, "b": b})
    word = take_word("color_word", 0x080)
    if word is not None:
        fields.append({"kind": "color_word", "bit": 0x080, "word": word})
    if control & 0x100:
        if cursor + 1 >= len(payload):
            fields.append({"kind": "scale_f32", "bit": 0x100, "missing": 1})
            cursor = len(payload)
        else:
            word0 = payload[cursor]
            word1 = payload[cursor + 1]
            cursor += 2
            fields.append(
                {
                    "kind": "scale_f32",
                    "bit": 0x100,
                    "word0": word0,
                    "word1": word1,
                    "x": u32_to_f32(word0),
                    "y": u32_to_f32(word1),
                }
            )
    word = take_word("rotate_f32", 0x200)
    if word is not None:
        fields.append({"kind": "rotate_f32", "bit": 0x200, "word": word, "value": u32_to_f32(word)})
    if control & 0xC00:
        word = take_word("blend_alpha_param", 0xC00)
        if word is not None:
            mode = word & 0xFF
            texture_param = {0: 0x44, 1: 0x48, 2: 0x42}.get(mode)
            fields.append(
                {
                    "kind": "blend_alpha_param",
                    "bit": 0xC00,
                    "word": word,
                    "mode": mode,
                    "texture_param": texture_param,
                    "alpha": (word >> 16) & 0xFF,
                }
            )
    if cursor < len(payload):
        fields.append({"kind": "trailing_words", "words": payload[cursor:]})
    return fields

COMPARE_NAMES = {
    0: "eq",
    1: "ge",
    2: "gt",
    3: "ne",
    4: "lt",
    5: "le",
}

COMPARE_SELECTORS_BY_NAME = {name: selector for selector, name in COMPARE_NAMES.items()}

CONDITION_0B_PROPERTY_OFFSETS = {
    0: 0,
    1: 1,
    2: 2,
    3: 4,
    4: 5,
    5: 6,
    6: 8,
}

def condition_compare_fields(control_byte: int) -> dict[str, int | str]:
    selector = (control_byte >> 1) & 0x07
    return {
        "compare": COMPARE_NAMES.get(selector, f"sel{selector}"),
        "compare_selector": selector,
        "compare_from_event_value": (control_byte >> 6) & 0x01,
    }

def condition_payload_details(condition_id: int, words: list[int]) -> dict[str, Any]:
    if not condition_id or len(words) < 2:
        return {}
    base_id = condition_id & 0x7F
    inverted = 1 if condition_id & 0x80 else 0
    word0 = words[0]
    word1 = words[1]
    byte2 = (word0 >> 16) & 0xFF
    byte3 = (word0 >> 24) & 0xFF
    details: dict[str, Any] = {
        "condition_base_id": base_id,
        "condition_inverted": inverted,
    }
    if base_id in {0x01, 0x02, 0x04, 0x05, 0x07, 0x10, 0x11, 0x12, 0x13}:
        details["compare_control_byte"] = byte3
        details.update(condition_compare_fields(byte3))
        details["compare_value"] = sign_extend(word1, 32)
    if base_id == 0x01:
        details.update(
            {
                "condition_kind": "config_event_flag_mask_compare",
                "first_flag": word0 & 0xFFFF,
                "flag_count": (byte2 & 0x0F) + 1,
            }
        )
    elif base_id == 0x02:
        details.update(
            {
                "condition_kind": "script_event_value_compare",
                "event_value_id": word0 & 0x0FFF,
            }
        )
    elif base_id == 0x03:
        details.update(
            {
                "condition_kind": "config_event_flag_mask_direct",
                "required_mask": word0 & 0xFFFF,
                "mask_mode": (word0 >> 16) & 0xFF,
                "requires_all": byte2 & 0x01,
            }
        )
        if word1:
            details["condition_word1"] = word1
    elif base_id == 0x04:
        time_control = word0 & 0xFF
        component_mask = time_control & 0x0F
        components = []
        if component_mask & 0x01:
            components.append("seconds")
        if component_mask & 0x02:
            components.append("minutes")
        if component_mask & 0x04:
            components.append("hours")
        if component_mask & 0x08:
            components.append("days")
        details.update(
            {
                "condition_kind": "radiata_time_compare",
                "time_control": time_control,
                "time_source_selector": (time_control >> 6) & 0x03,
                "time_component_mask": component_mask,
                "time_components": ",".join(components) if components else "none",
            }
        )
        condition_word0_mid = (word0 >> 8) & 0xFFFF
        if condition_word0_mid:
            details["condition_word0_mid"] = condition_word0_mid
    elif base_id == 0x05:
        details.update(
            {
                "condition_kind": "runtime_player_value_compare",
                "runtime_source": "field_124_float_as_int" if word0 & 0x01 else "field_110_int",
            }
        )
    elif base_id == 0x07:
        details.update(
            {
                "condition_kind": "character_table_property_compare",
                "character_word": word0 & 0xFFFF,
                "character_source": byte2,
                "property_selector": byte2,
            }
        )
    elif base_id == 0x08:
        details.update(
            {
                "condition_kind": "character_id_category_direct",
                "character_word": word0 & 0xFFFF,
                "character_source": byte2,
                "category_selector": byte3,
            }
        )
        if word1:
            details["condition_word1"] = word1
    elif base_id == 0x0A:
        control_byte = (word1 >> 24) & 0xFF
        details.update(
            {
                "condition_kind": "script_lookup_direct",
                "character_word": word0 & 0xFFFF,
                "character_source": byte2,
                "selector": byte3,
                "lookup_script_id": word1 & 0xFFFF,
                "lookup_script_raw_mid": (word1 >> 16) & 0xFF,
                "script_lookup_control": control_byte,
                "script_id_from_event": (control_byte >> 5) & 0x01,
                "uses_character_filter": (control_byte >> 6) & 0x01,
                "uses_selector": (control_byte >> 7) & 0x01,
                "lookup_mode": "address" if control_byte & 0x10 else "slot_scan",
            }
        )
    elif base_id == 0x0B:
        property_selector = byte3 & 0x0F
        compare_selector = (byte3 >> 5) & 0x03
        details.update(
            {
                "condition_kind": "character_runtime_property_compare",
                "character_word": word0 & 0xFFFF,
                "character_source": byte2,
                "property_selector": property_selector,
                "property_offset": CONDITION_0B_PROPERTY_OFFSETS.get(property_selector, -1),
                "scan_all_characters": (byte3 >> 4) & 0x01,
                "compare_control_byte": byte3,
                "compare": COMPARE_NAMES.get(compare_selector, f"sel{compare_selector}"),
                "compare_selector": compare_selector,
                "compare_from_event_value": (byte3 >> 7) & 0x01,
                "compare_value": sign_extend(word1, 32),
            }
        )
    elif base_id == 0x10:
        details.update(
            {
                "condition_kind": "script_or_character_state_direct",
                "character_raw_word": word0 & 0xFFFF,
                "character_word": word0 & 0x3FFF,
                "character_source": byte2,
                "state_selector": byte3,
                "state_flags": (word1 >> 24) & 0xFF,
            }
        )
    elif base_id == 0x0C:
        details.update(
            {
                "condition_kind": "character_item_state_direct",
                "character_word": word0 & 0xFFFF,
                "character_source": byte2,
                "item_or_state_selector": byte3,
                "item_or_state_word": word1 & 0xFFFF,
            }
        )
        if word1 & 0xFFFF0000:
            details["condition_word1"] = word1
    elif base_id == 0x0D:
        details.update(
            {
                "condition_kind": "character_status_direct",
                "character_word": word0 & 0xFFFF,
                "character_source": byte2,
                "status_selector": byte3,
            }
        )
        if word1:
            details["condition_word1"] = word1
    elif base_id == 0x0E:
        details.update(
            {
                "condition_kind": "character_object_state_direct",
                "character_word": word0 & 0xFFFF,
                "character_source": byte2,
                "object_state_selector": byte3,
            }
        )
        if word1:
            details["condition_word1"] = word1
    elif base_id == 0x0F:
        details.update(
            {
                "condition_kind": "global_config_word_direct",
                "expected_word": word0 & 0xFFFF,
            }
        )
    return details

def build_condition_words_from_source(fields: dict[str, str], condition_id: int, line_no: int, directive: str) -> list[int]:
    if "condition_args" in fields:
        return parse_optional_word_list(fields, "condition_args")
    if not condition_id:
        return []
    base_id = condition_id & 0x7F
    if base_id == 0x0F:
        require_fields(fields, {"expected_word"}, line_no, directive)
        expected_word = parse_hex_int(fields["expected_word"])
        if not 0 <= expected_word <= 0xFFFF:
            raise ValueError(f"line {line_no}: {directive} condition 0F expected_word out of range")
        return [expected_word, parse_hex_int(fields.get("condition_word1", "0")) & 0xFFFFFFFF]
    if base_id == 0x0A:
        require_fields(fields, {"lookup_script_id", "script_lookup_control"}, line_no, directive)
        script_id = parse_hex_int(fields["lookup_script_id"])
        control_byte = parse_hex_int(fields["script_lookup_control"])
        script_raw_mid = parse_hex_int(fields.get("lookup_script_raw_mid", "0"))
        character_word = parse_hex_int(fields.get("character_word", "0"))
        character_source = parse_hex_int(fields.get("character_source", "0"))
        selector = parse_hex_int(fields.get("selector", "0"))
        if not 0 <= script_id <= 0xFFFF or not 0 <= script_raw_mid <= 0xFF or not 0 <= control_byte <= 0xFF:
            raise ValueError(f"line {line_no}: {directive} condition 0A script fields out of range")
        if not 0 <= character_word <= 0xFFFF or not 0 <= character_source <= 0xFF or not 0 <= selector <= 0xFF:
            raise ValueError(f"line {line_no}: {directive} condition 0A character/selector fields out of range")
        if "script_id_from_event" in fields and parse_hex_int(fields["script_id_from_event"]) != ((control_byte >> 5) & 0x01):
            raise ValueError(f"line {line_no}: {directive} script_id_from_event does not match script_lookup_control")
        if "uses_character_filter" in fields and parse_hex_int(fields["uses_character_filter"]) != ((control_byte >> 6) & 0x01):
            raise ValueError(f"line {line_no}: {directive} uses_character_filter does not match script_lookup_control")
        if "uses_selector" in fields and parse_hex_int(fields["uses_selector"]) != ((control_byte >> 7) & 0x01):
            raise ValueError(f"line {line_no}: {directive} uses_selector does not match script_lookup_control")
        if "lookup_mode" in fields:
            expected_mode = "address" if control_byte & 0x10 else "slot_scan"
            if fields["lookup_mode"] != expected_mode:
                raise ValueError(f"line {line_no}: {directive} lookup_mode does not match script_lookup_control")
        return [
            character_word | (character_source << 16) | (selector << 24),
            script_id | (script_raw_mid << 16) | (control_byte << 24),
        ]
    if base_id == 0x0B:
        require_fields(fields, {"character_word", "property_selector", "compare", "compare_from_event", "value"}, line_no, directive)
        character_word = parse_hex_int(fields["character_word"])
        character_source = parse_hex_int(fields.get("character_source", "0"))
        property_selector = parse_hex_int(fields["property_selector"])
        scan_all = parse_hex_int(fields.get("scan_all_characters", "0"))
        compare_text = fields["compare"]
        compare_selector = COMPARE_SELECTORS_BY_NAME.get(compare_text)
        if compare_selector is None:
            compare_selector = parse_hex_int(compare_text)
        compare_from_event = parse_hex_int(fields["compare_from_event"])
        compare_value = parse_hex_int(fields["value"])
        control_byte = property_selector | (scan_all << 4) | (compare_selector << 5) | (compare_from_event << 7)
        if "compare_control" in fields and parse_hex_int(fields["compare_control"]) != control_byte:
            raise ValueError(f"line {line_no}: {directive} compare_control does not match condition 0B fields")
        if "property_offset" in fields and parse_hex_int(fields["property_offset"]) != CONDITION_0B_PROPERTY_OFFSETS.get(property_selector, -1):
            raise ValueError(f"line {line_no}: {directive} property_offset does not match property_selector")
        if not 0 <= character_word <= 0xFFFF or not 0 <= character_source <= 0xFF:
            raise ValueError(f"line {line_no}: {directive} condition 0B character fields out of range")
        if property_selector not in CONDITION_0B_PROPERTY_OFFSETS or scan_all not in (0, 1):
            raise ValueError(f"line {line_no}: {directive} condition 0B selector fields out of range")
        if not 0 <= compare_selector <= 0x03 or compare_from_event not in (0, 1):
            raise ValueError(f"line {line_no}: {directive} condition 0B compare fields out of range")
        return [character_word | (character_source << 16) | (control_byte << 24), compare_value & 0xFFFFFFFF]
    if base_id == 0x03:
        require_fields(fields, {"required_mask", "mask_mode"}, line_no, directive)
        required_mask = parse_hex_int(fields["required_mask"])
        mask_mode = parse_hex_int(fields["mask_mode"])
        if "requires_all" in fields and parse_hex_int(fields["requires_all"]) != (mask_mode & 0x01):
            raise ValueError(f"line {line_no}: {directive} requires_all does not match mask_mode")
        if not 0 <= required_mask <= 0xFFFF or not 0 <= mask_mode <= 0xFF:
            raise ValueError(f"line {line_no}: {directive} condition 03 fields out of range")
        return [required_mask | (mask_mode << 16), parse_hex_int(fields.get("condition_word1", "0")) & 0xFFFFFFFF]
    if base_id == 0x04:
        require_fields(fields, {"time_control", "compare", "compare_from_event", "value"}, line_no, directive)
        time_control = parse_hex_int(fields["time_control"])
        compare_text = fields["compare"]
        compare_selector = COMPARE_SELECTORS_BY_NAME.get(compare_text)
        if compare_selector is None:
            compare_selector = parse_hex_int(compare_text)
        compare_from_event = parse_hex_int(fields["compare_from_event"])
        compare_value = parse_hex_int(fields["value"])
        compare_byte = parse_hex_int(fields["compare_control"]) if "compare_control" in fields else (compare_selector << 1) | (compare_from_event << 6)
        if ((compare_byte >> 1) & 0x07) != compare_selector or ((compare_byte >> 6) & 0x01) != compare_from_event:
            raise ValueError(f"line {line_no}: {directive} compare_control does not match compare/compare_from_event")
        if "time_source_selector" in fields and parse_hex_int(fields["time_source_selector"]) != ((time_control >> 6) & 0x03):
            raise ValueError(f"line {line_no}: {directive} time_source_selector does not match time_control")
        if "time_component_mask" in fields and parse_hex_int(fields["time_component_mask"]) != (time_control & 0x0F):
            raise ValueError(f"line {line_no}: {directive} time_component_mask does not match time_control")
        condition_word0_mid = parse_hex_int(fields.get("condition_word0_mid", "0"))
        if not 0 <= time_control <= 0xFF or not 0 <= condition_word0_mid <= 0xFFFF or not 0 <= compare_byte <= 0xFF:
            raise ValueError(f"line {line_no}: {directive} condition 04 fields out of range")
        return [time_control | (condition_word0_mid << 8) | (compare_byte << 24), compare_value & 0xFFFFFFFF]
    if base_id == 0x05:
        require_fields(fields, {"runtime_source", "compare", "compare_from_event", "value"}, line_no, directive)
        runtime_source = fields["runtime_source"]
        if runtime_source == "field_110_int":
            runtime_word = 0
        elif runtime_source == "field_124_float_as_int":
            runtime_word = 1
        else:
            runtime_word = parse_hex_int(runtime_source)
        compare_text = fields["compare"]
        compare_selector = COMPARE_SELECTORS_BY_NAME.get(compare_text)
        if compare_selector is None:
            compare_selector = parse_hex_int(compare_text)
        compare_from_event = parse_hex_int(fields["compare_from_event"])
        compare_byte = parse_hex_int(fields["compare_control"]) if "compare_control" in fields else (compare_selector << 1) | (compare_from_event << 6)
        if ((compare_byte >> 1) & 0x07) != compare_selector or ((compare_byte >> 6) & 0x01) != compare_from_event:
            raise ValueError(f"line {line_no}: {directive} compare_control does not match compare/compare_from_event")
        if not 0 <= runtime_word <= 0xFFFFFF or not 0 <= compare_byte <= 0xFF:
            raise ValueError(f"line {line_no}: {directive} condition 05 fields out of range")
        return [runtime_word | (compare_byte << 24), parse_hex_int(fields["value"]) & 0xFFFFFFFF]
    if base_id == 0x08:
        require_fields(fields, {"character_word", "category_selector"}, line_no, directive)
        character_word = parse_hex_int(fields["character_word"])
        character_source = parse_hex_int(fields.get("character_source", "0"))
        category_selector = parse_hex_int(fields["category_selector"])
        if not 0 <= character_word <= 0xFFFF or not 0 <= character_source <= 0xFF or not 0 <= category_selector <= 0xFF:
            raise ValueError(f"line {line_no}: {directive} condition 08 fields out of range")
        return [character_word | (character_source << 16) | (category_selector << 24), parse_hex_int(fields.get("condition_word1", "0")) & 0xFFFFFFFF]
    if base_id == 0x0C:
        require_fields(fields, {"character_word", "item_or_state_word"}, line_no, directive)
        character_word = parse_hex_int(fields["character_word"])
        character_source = parse_hex_int(fields.get("character_source", "0"))
        item_or_state_selector = parse_hex_int(fields.get("item_or_state_selector", "0"))
        item_or_state_word = parse_hex_int(fields["item_or_state_word"])
        if not 0 <= character_word <= 0xFFFF or not 0 <= character_source <= 0xFF or not 0 <= item_or_state_selector <= 0xFF or not 0 <= item_or_state_word <= 0xFFFF:
            raise ValueError(f"line {line_no}: {directive} condition 0C fields out of range")
        return [
            character_word | (character_source << 16) | (item_or_state_selector << 24),
            parse_hex_int(fields.get("condition_word1", str(item_or_state_word))) & 0xFFFFFFFF,
        ]
    if base_id == 0x0D:
        require_fields(fields, {"character_word", "status_selector"}, line_no, directive)
        character_word = parse_hex_int(fields["character_word"])
        character_source = parse_hex_int(fields.get("character_source", "0"))
        status_selector = parse_hex_int(fields["status_selector"])
        if not 0 <= character_word <= 0xFFFF or not 0 <= character_source <= 0xFF or not 0 <= status_selector <= 0xFF:
            raise ValueError(f"line {line_no}: {directive} condition 0D fields out of range")
        return [character_word | (character_source << 16) | (status_selector << 24), parse_hex_int(fields.get("condition_word1", "0")) & 0xFFFFFFFF]
    if base_id == 0x0E:
        require_fields(fields, {"character_word", "object_state_selector"}, line_no, directive)
        character_word = parse_hex_int(fields["character_word"])
        character_source = parse_hex_int(fields.get("character_source", "0"))
        object_state_selector = parse_hex_int(fields["object_state_selector"])
        if not 0 <= character_word <= 0xFFFF or not 0 <= character_source <= 0xFF or not 0 <= object_state_selector <= 0xFF:
            raise ValueError(f"line {line_no}: {directive} condition 0E fields out of range")
        return [character_word | (character_source << 16) | (object_state_selector << 24), parse_hex_int(fields.get("condition_word1", "0")) & 0xFFFFFFFF]
    if base_id == 0x10:
        require_fields(fields, {"character_source", "state_selector", "value"}, line_no, directive)
        character_word = parse_hex_int(fields.get("character_raw_word", fields.get("character_word", "0")))
        character_source = parse_hex_int(fields["character_source"])
        state_selector = parse_hex_int(fields["state_selector"])
        compare_value = parse_hex_int(fields["value"])
        if not 0 <= character_word <= 0xFFFF or not 0 <= character_source <= 0xFF or not 0 <= state_selector <= 0xFF:
            raise ValueError(f"line {line_no}: {directive} condition 10 fields out of range")
        if "state_flags" in fields and parse_hex_int(fields["state_flags"]) != ((compare_value >> 24) & 0xFF):
            raise ValueError(f"line {line_no}: {directive} state_flags does not match value")
        return [character_word | (character_source << 16) | (state_selector << 24), compare_value & 0xFFFFFFFF]
    if base_id not in {0x01, 0x02, 0x07}:
        return []
    require_fields(fields, {"compare", "compare_from_event", "value"}, line_no, directive)
    compare_text = fields["compare"]
    compare_selector = COMPARE_SELECTORS_BY_NAME.get(compare_text)
    if compare_selector is None:
        compare_selector = parse_hex_int(compare_text)
    compare_from_event = parse_hex_int(fields["compare_from_event"])
    compare_value = parse_hex_int(fields["value"])
    if not 0 <= compare_selector <= 0x07 or compare_from_event not in (0, 1):
        raise ValueError(f"line {line_no}: {directive} compare fields out of range")
    compare_byte = parse_hex_int(fields["compare_control"]) if "compare_control" in fields else (compare_selector << 1) | (compare_from_event << 6)
    if ((compare_byte >> 1) & 0x07) != compare_selector or ((compare_byte >> 6) & 0x01) != compare_from_event:
        raise ValueError(f"line {line_no}: {directive} compare_control does not match compare/compare_from_event")
    if base_id == 0x01:
        require_fields(fields, {"first_flag", "flag_count"}, line_no, directive)
        first_flag = parse_hex_int(fields["first_flag"])
        flag_count = parse_hex_int(fields["flag_count"])
        if not 0 <= first_flag <= 0xFFFF or not 1 <= flag_count <= 0x10:
            raise ValueError(f"line {line_no}: {directive} condition 01 flag fields out of range")
        return [first_flag | ((flag_count - 1) << 16) | (compare_byte << 24), compare_value & 0xFFFFFFFF]
    if base_id == 0x02:
        require_fields(fields, {"event_value"}, line_no, directive)
        event_value = parse_hex_int(fields["event_value"])
        if not 0 <= event_value <= 0x0FFF:
            raise ValueError(f"line {line_no}: {directive} event_value out of range")
        return [event_value | (compare_byte << 24), compare_value & 0xFFFFFFFF]
    require_fields(fields, {"character_word", "property_selector"}, line_no, directive)
    character_word = parse_hex_int(fields["character_word"])
    property_selector = parse_hex_int(fields["property_selector"])
    if not 0 <= character_word <= 0xFFFF or not 0 <= property_selector <= 0xFF:
        raise ValueError(f"line {line_no}: {directive} condition 07 fields out of range")
    if "character_source" in fields and parse_hex_int(fields["character_source"]) != property_selector:
        raise ValueError(f"line {line_no}: {directive} character_source does not match property_selector")
    return [character_word | (property_selector << 16) | (compare_byte << 24), compare_value & 0xFFFFFFFF]

def command_details(payload: bytes, offset: int, opcode: int, arg_byte: int, args_u32: list[int]) -> dict[str, Any]:
    if opcode == 0x03 and args_u32:
        trigger_word = args_u32[0]
        details: dict[str, Any] = {
            "trigger_type": trigger_word & 0xFF,
            "trigger_raw_mid": (trigger_word >> 8) & 0xFF,
            "trigger_subarg": arg_byte,
            "trigger_flags": (trigger_word >> 16) & 0xFFFF,
            "mode": arg_byte >> 6,
        }
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x04:
        details = {
            "script_mode": arg_byte & 0x0F,
            "has_default_character": bool(arg_byte & 0x10),
            "payload_u32": args_u32,
        }
        if args_u32:
            details["script_word_0"] = args_u32[0]
            details["condition_id"] = (args_u32[0] >> 24) & 0xFF
        return details
    if opcode == 0x01:
        details = {
            "script_mode": arg_byte & 0x0F,
            "uses_explicit_character": bool(arg_byte & 0x10),
            "payload_u32": args_u32,
        }
        if args_u32:
            details["script_word_0"] = args_u32[0]
            details["condition_id"] = (args_u32[0] >> 24) & 0xFF
        return details
    if opcode == 0x0A:
        return {
            "game_mode": arg_byte & 0x3F,
            "payload_u32": args_u32,
        }
    if opcode == 0x05:
        details = {
            "condition_id": arg_byte & 0xFF,
            "payload_u32": args_u32,
        }
        if arg_byte and len(args_u32) >= 2:
            details.update(condition_payload_details(arg_byte & 0xFF, args_u32[:2]))
        return details
    if opcode == 0x14:
        details = {
            "arg_flags": arg_byte,
            "payload_u32": args_u32,
        }
        if len(args_u32) >= 3:
            control = args_u32[0]
            lhs = args_u32[1]
            rhs = args_u32[2]
            op = control & 0x07
            details["control_word"] = control
            details["operation"] = op
            details["operation_name"] = EXPR_OPERATION_NAMES.get(op, "unknown")
            details["control_mid_nibble"] = (control >> 4) & 0x0F
            details["store_type"] = (control >> 8) & 0x0F
            details["left_operand_word"] = lhs
            details["left_operand_type"] = (lhs >> 24) & 0x0F
            details["left_operand_tag"] = (lhs >> 24) & 0xFF
            details["right_operand_word"] = rhs
            details["right_operand_type"] = (rhs >> 24) & 0x0F
            details["right_operand_tag"] = (rhs >> 24) & 0xFF
        return details
    if opcode == 0x06:
        details = {
            "force_high_bit": bool(arg_byte & 0x01),
        }
        if args_u32:
            details["script_file_id"] = args_u32[0] & 0x7FFF
        return details
    if opcode == 0x0B:
        details = {
            "stop_mode": arg_byte,
            "payload_u32": args_u32,
        }
        if args_u32:
            details["script_word_0"] = args_u32[0]
        return details
    if opcode == 0x15:
        fields, complete = decode_script_defaults_fields(arg_byte, args_u32)
        details = {
            "sets_default_character": bool(arg_byte & 0x01),
            "sets_default_object_name": bool(arg_byte & 0x02),
            "character_from_event_value": bool(arg_byte & 0x80),
            "decoded_fields": fields,
            "decoded_complete": complete,
        }
        if args_u32:
            details["first_word"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x16:
        details = {
            "arg_flags": arg_byte,
            "payload_u32": args_u32,
        }
        if args_u32:
            details["battle_result_word_0"] = args_u32[0]
        return details
    if opcode == 0x17:
        details = {
            "has_explicit_character": bool(arg_byte & 0x01),
            "arg_flags": arg_byte,
        }
        if args_u32:
            details["character_word"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x12:
        details = {
            "time_schedule_mode": arg_byte,
            "payload_u32": args_u32,
        }
        if args_u32:
            details["first_word"] = args_u32[0]
        return details
    if opcode == 0x13:
        return {
            "time_mode_low2": arg_byte & 0x03,
            "updates_radi_time_bit": bool(arg_byte & 0x20),
            "time_enable_mode_bits_6_7": (arg_byte >> 6) & 0x03,
            "payload_u32": args_u32,
        }
    if opcode == 0x11:
        details = {
            "pop_mode": bool(arg_byte & 0x01),
            "payload_u32": args_u32,
        }
        if args_u32:
            details["save_env_flags"] = args_u32[0]
        return details
    if opcode == 0x19:
        details = {
            "has_explicit_character": bool(arg_byte & 0x01),
            "inventory_mode": (arg_byte >> 1) & 0x03,
            "flag_20": bool(arg_byte & 0x20),
            "uses_event_value_bit_40": bool(arg_byte & 0x40),
            "uses_event_value_bit_80": bool(arg_byte & 0x80),
        }
        if args_u32:
            details["first_word_low16"] = args_u32[0] & 0xFFFF
            details["first_word_high16"] = (args_u32[0] >> 16) & 0xFFFF
        return details
    if opcode == 0x18:
        details = {
            "party_mode": arg_byte & 0x03,
            "arg_flag_07": bool(arg_byte & 0x80),
        }
        if args_u32:
            details["character_word"] = args_u32[0]
        return details
    if opcode == 0x1A:
        details = {
            "has_explicit_character": bool(arg_byte & 0x01),
            "arg_flag_01": bool(arg_byte & 0x02),
            "arg_flag_02": bool(arg_byte & 0x04),
            "arg_flag_03": bool(arg_byte & 0x08),
            "arg_flag_04": bool(arg_byte & 0x10),
        }
        fields, complete = decode_character_equipment_fields(arg_byte, args_u32)
        details["decoded_fields"] = fields
        details["decoded_complete"] = complete
        if args_u32:
            details["character_word"] = args_u32[0]
        if len(args_u32) > 1:
            details["item_word"] = args_u32[1]
        if len(args_u32) > 2:
            details["payload_u32"] = args_u32[2:]
        return details
    if opcode == 0x1B:
        return {
            "window_mode": arg_byte,
            "payload_u32": args_u32,
        }
    if opcode == 0x1C:
        fields, complete = decode_stand_context_fields(arg_byte, args_u32)
        details = {
            "context_flags": arg_byte,
            "sets_field_0": bool(arg_byte & 0x01),
            "sets_stand_position": bool(arg_byte & 0x02),
            "sets_position_vector": bool(arg_byte & 0x04),
            "sets_posture_vector": bool(arg_byte & 0x08),
            "decoded_fields": fields,
            "decoded_complete": complete,
        }
        if args_u32:
            details["first_word"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x20:
        details = {
            "has_explicit_character": bool(arg_byte & 0x01),
            "mode": arg_byte >> 6,
            "arg_flags": arg_byte,
        }
        fields, complete = decode_character_data_fields(arg_byte, args_u32)
        details["decoded_fields"] = fields
        details["decoded_complete"] = complete
        if args_u32:
            details["character_word"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x21:
        details = {
            "has_explicit_character": bool(arg_byte & 0x01),
            "arg_flag_07": bool(arg_byte & 0x80),
        }
        fields, complete = decode_character_delete_fields(arg_byte, args_u32)
        details["decoded_fields"] = fields
        details["decoded_complete"] = complete
        control_index = 1 if arg_byte & 0x01 else 0
        if arg_byte & 0x01 and args_u32:
            details["character_word"] = args_u32[0]
        if control_index < len(args_u32):
            control = args_u32[control_index]
            details["control_word"] = control
            details["delete_character_bit_0"] = bool(control & 0x01)
            details["detach_data_mask"] = control >> 1
        if control_index + 1 < len(args_u32):
            details["payload_u32"] = args_u32[control_index + 1 :]
        return details
    if opcode == 0x22:
        details = {
            "has_explicit_character": bool(arg_byte & 0x01),
            "sub_manager_flag_bit": bool(arg_byte & 0x02),
            "character_byte_0a_bit1_value": 0 if (arg_byte & 0x04) else 1,
            "use_child_no_render_path": bool(arg_byte & 0x40),
            "no_render_value": bool(arg_byte & 0x80),
        }
        if args_u32:
            details["character_word"] = args_u32[0]
        return details
    if opcode == 0x23:
        explicit_char = arg_byte & 0x01
        control_index = 1 if explicit_char else 0
        details = {
            "explicit_char": explicit_char,
            "has_speed0": 1 if arg_byte & 0x02 else 0,
            "has_speed1": 1 if arg_byte & 0x04 else 0,
            "has_blend": 1 if arg_byte & 0x08 else 0,
            "has_speed2": 1 if arg_byte & 0x10 else 0,
            "has_extra_word": 1 if arg_byte & 0x20 else 0,
            "sub_animation_path": 1 if arg_byte >> 6 else 0,
        }
        if control_index + 1 < len(args_u32):
            anim_group = args_u32[control_index]
            anim_word = args_u32[control_index + 1]
            details["animation_group"] = anim_group
            details["animation_request_low16"] = anim_group & 0xFFFF
            details["animation_request_low_byte"] = anim_group & 0xFF
            details["animation_request_flags_byte"] = (anim_group >> 16) & 0xFF
            details["animation_word"] = anim_word
            details["animation_number"] = anim_word & 0xFFFF
            details["animation_high_byte"] = (anim_word >> 24) & 0xFF
            cursor = control_index + 2
            optional_values = []
            for bit, name in (
                (0x02, "optional_float0"),
                (0x04, "optional_float1"),
                (0x08, "optional_float2"),
                (0x10, "play_speed"),
            ):
                if arg_byte & bit and cursor < len(args_u32):
                    optional_values.append((name, args_u32[cursor], u32_to_f32(args_u32[cursor])))
                    cursor += 1
            if optional_values:
                details["optional_float_operands"] = optional_values
            if arg_byte & 0x20 and cursor < len(args_u32):
                details["extra_animation_word"] = args_u32[cursor]
            details["payload_u32"] = args_u32[control_index + 2 :]
        return details
    if opcode == 0x24:
        return {
            "has_explicit_character": bool(arg_byte & 0x01),
            "arg_flags": arg_byte,
            "payload_u32": args_u32,
        }
    if opcode == 0x02 and args_u32:
        branch_word = args_u32[0]
        condition_id = (branch_word >> 24) & 0xFF
        rel_words = sign_extend(branch_word & 0x00FFFFFF, 24)
        branch_base = offset + 8
        details: dict[str, Any] = {
            "condition_id": condition_id,
            "relative_words": rel_words,
        }
        if condition_id:
            branch_base += 8
            details["condition_payload_u32"] = args_u32[1:3]
            details.update(condition_payload_details(condition_id, args_u32[1:3]))
            if condition_id == 0x02 and len(args_u32) >= 3:
                control = args_u32[1]
                details["condition_02"] = {
                    "event_value_id": control & 0x0FFF,
                    "compare_selector": (control >> 24) & 0x07,
                    "compare_value": sign_extend(args_u32[2], 32),
                }
        details["branch_target_offset"] = branch_base + rel_words * 4
        return details
    if opcode == 0x2D:
        explicit_char = arg_byte & 0x01
        control_index = 1 if explicit_char else 0
        details = {
            "arg_byte": arg_byte,
            "explicit_char": explicit_char,
            "target_character_from_stream": 1 if arg_byte & 0x02 else 0,
            "name_payload_source": (arg_byte >> 2) & 0x03,
        }
        if control_index < len(args_u32):
            control_word = args_u32[control_index]
            mode = control_word & 0x0F
            option_path = 1 if control_word & 0x200 else 0
            details["control_word"] = control_word
            details["mode"] = mode
            details["mode_action"] = ROTATE_OPTION_MODE_ACTIONS.get((option_path, mode), "unknown")
            details["postprocess_mode"] = (control_word >> 4) & 0x03
            details["vector_component_mask"] = (control_word >> 6) & 0x07
            details["uses_character_option"] = option_path
            details["has_position_offset"] = 1 if control_word & 0x400 else 0
            details["has_posture_offset"] = 1 if control_word & 0x800 else 0
            details["has_speed_limit"] = 1 if control_word & 0x1000 else 0
            details["control_bit13"] = 1 if control_word & 0x2000 else 0
            duration = (control_word >> 16) & 0xFFFF
            details["duration"] = -1 if duration == 0xFFFF else duration
            payload = args_u32[control_index + 1 :]
            vector_fields, consumed = masked_vector_fields((control_word >> 6) & 0x07, payload)
            if vector_fields:
                details["initial_vector"] = vector_fields
                details["initial_vector_words"] = payload[:consumed]
            if control_word & 0x1000 and len(payload) >= consumed + 2:
                details["speed_limit_words"] = payload[consumed : consumed + 2]
                details["speed_limit_values"] = [
                    u32_to_f32(payload[consumed]),
                    u32_to_f32(payload[consumed + 1]),
                ]
            details["payload_u32"] = payload
        return details
    if opcode == 0x27:
        details = {
            "has_explicit_character": bool(arg_byte & 0x01),
            "detach_flag_low": bool(arg_byte & 0x10),
            "character_parent_detach_flag_high": bool(arg_byte & 0x40),
            "arg_flag_07_preserved": bool(arg_byte & 0x80),
        }
        if args_u32:
            details["character_word"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x26:
        details = {
            "has_explicit_character": bool(arg_byte & 0x01),
            "target_source_bits_1_2": (arg_byte >> 1) & 0x03,
            "arg_flags": arg_byte,
        }
        if args_u32:
            details["attach_word_0"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x28:
        details = {
            "has_explicit_character": bool(arg_byte & 0x01),
            "arg_flags": arg_byte,
        }
        if args_u32:
            details["buffer_control_word"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x25:
        return {
            "has_explicit_character": bool(arg_byte & 0x01),
            "arg_flags": arg_byte,
            "payload_u32": args_u32,
        }
    if opcode == 0x29:
        return {
            "has_explicit_character": bool(arg_byte & 0x01),
            "pause_arg": arg_byte >> 1,
            "payload_u32": args_u32,
        }
    if opcode == 0x2A:
        details = {
            "has_explicit_character": bool(arg_byte & 0x01),
            "movement_mode": (arg_byte >> 1) & 0x0F,
            "arg_flags": arg_byte,
        }
        if args_u32:
            details["movement_control_word"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x2B:
        explicit_char = bool(arg_byte & 0x01)
        control_index = 1 if explicit_char else 0
        details = {
            "has_explicit_character": explicit_char,
            "precreate_mode": arg_byte >> 6,
        }
        if explicit_char and args_u32:
            details["character_word"] = args_u32[0]
        if control_index < len(args_u32):
            control = args_u32[control_index]
            details["precreate_control_word"] = control
            details["precreate_anim_id"] = control & 0xFF
            details["precreate_control_raw_high24"] = control >> 8
        return details
    if opcode == 0x2F:
        explicit_char = arg_byte & 0x01
        control_index = 1 if explicit_char else 0
        details = {
            "has_explicit_character": bool(explicit_char),
            "set_character_attribute_2_value": 0 if (arg_byte & 0x02) else 1,
        }
        if explicit_char and args_u32:
            details["character_word"] = args_u32[0]
        if control_index < len(args_u32):
            details["collision_attribute"] = args_u32[control_index]
        if control_index + 1 < len(args_u32):
            details["collision_value"] = args_u32[control_index + 1]
        return details
    if opcode == 0x30:
        explicit_char = arg_byte & 0x01
        control_index = 1 if explicit_char else 0
        source = (arg_byte >> 5) & 0x07
        details = {
            "has_explicit_character": bool(explicit_char),
            "position_mode_bits_1_2": (arg_byte >> 1) & 0x03,
            "position_coord_selector_bits_3_4": (arg_byte >> 3) & 0x03,
            "position_source_bits_5_7": source,
        }
        if explicit_char and args_u32:
            details["character_word"] = args_u32[0]
        if control_index < len(args_u32):
            control = args_u32[control_index]
            details["position_control_word"] = control
            details["move_duration"] = control & 0xFFFF
            details["move_control_high16"] = (control >> 16) & 0xFFFF
            details["snap_position"] = 1 if (control & 0xFFFF) == 0 else 0
            details["move_position"] = 0 if (control & 0xFFFF) == 0 else 1
            details["position_payload_u32"] = args_u32[control_index + 1 :]
            if source == 4 and len(args_u32) >= control_index + 4:
                vector_words = args_u32[control_index + 1 : control_index + 4]
                details["inline_vector_words"] = vector_words
                details["inline_vector"] = [u32_to_f32(word) for word in vector_words]
        return details
    if opcode == 0x31:
        details = {
            "has_explicit_character": bool(arg_byte & 0x01),
            "arg_flags": arg_byte,
        }
        fields, complete = decode_character_collision_fields(arg_byte, args_u32)
        details["decoded_fields"] = fields
        details["decoded_complete"] = complete
        if args_u32:
            details["collision_word_0"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x32:
        explicit_char = arg_byte & 0x01
        control_index = 1 if explicit_char else 0
        uses_event_value_duration = 1 if arg_byte & 0x10 else 0
        details = {
            "explicit_char": explicit_char,
            "target_name_source": (arg_byte >> 1) & 0x03,
            "uses_event_value_duration": uses_event_value_duration,
            "with_child": 1 if arg_byte & 0x08 else 0,
        }
        if control_index < len(args_u32):
            control = args_u32[control_index]
            details["control_word"] = control
            details["control_byte0"] = control & 0xFF
            details["control_byte1"] = (control >> 8) & 0xFF
            details["control_byte2"] = (control >> 16) & 0xFF
            details["control_byte3"] = (control >> 24) & 0xFF
            details["control_byte0_transparency_color"] = control & 0xFF
            details["control_byte1_scale_palette"] = (control >> 8) & 0xFF
            details["control_byte2_visibility"] = (control >> 16) & 0xFF
            details["control_byte3_target_child"] = (control >> 24) & 0xFF
            duration_word = args_u32[control_index + 1] if control_index + 1 < len(args_u32) else None
            details["duration_word"] = duration_word
            if duration_word is not None:
                if uses_event_value_duration:
                    details["duration_event_value_id"] = duration_word & 0xFFFF
                    details["duration_action"] = "dynamic"
                else:
                    duration_value = sign_extend(duration_word, 32)
                    details["duration_value"] = duration_value
                    details["duration_action"] = "stop" if duration_value < 0 else "play"
            details["payload_u32"] = args_u32[control_index + 2 :]
        return details
    if opcode == 0x33:
        explicit_char = bool(arg_byte & 0x01)
        control_index = 1 if explicit_char else 0
        details = {
            "has_explicit_character": explicit_char,
            "sets_eye_ball": bool(arg_byte & 0x02),
            "sets_eye_move": bool(arg_byte & 0x04),
        }
        if explicit_char and args_u32:
            details["character_word"] = args_u32[0]
        if control_index < len(args_u32):
            control = args_u32[control_index]
            selector = (control >> 8) & 0xFF
            details["eye_control_word"] = control
            details["eye_ball_byte"] = control & 0xFF
            details["eye_ball_no_bits_0_1"] = control & 0x03
            details["eye_move_selector_byte_1"] = selector
            details["eye_move_action"] = EYE_MOVE_SELECTOR_ACTIONS.get(selector, "ignored")
            details["manual_x_s8_byte_2"] = sign_extend((control >> 16) & 0xFF, 8)
            details["manual_y_s8_byte_3"] = sign_extend((control >> 24) & 0xFF, 8)
        if control_index + 1 < len(args_u32):
            details["manual_time_float_word"] = args_u32[control_index + 1]
            details["manual_time_float"] = u32_to_f32(args_u32[control_index + 1])
        return details
    if opcode == 0x34:
        explicit_char = bool(arg_byte & 0x01)
        control_index = 1 if explicit_char else 0
        details = {"has_explicit_character": explicit_char}
        if explicit_char and args_u32:
            details["character_word"] = args_u32[0]
        if control_index + 1 < len(args_u32):
            word0 = args_u32[control_index]
            word1 = args_u32[control_index + 1]
            expression_byte = word0 & 0xFF
            blink_control = (word0 >> 8) & 0xFF
            mouth_control = (word0 >> 16) & 0xFF
            blink_half_steps = (word1 >> 16) & 0xFF
            details["expression_word_0"] = word0
            details["expression_word_1"] = word1
            details["expression_store_byte"] = expression_byte
            details["expression_store_enabled"] = expression_byte != 0xFF
            details["blink_control_byte"] = blink_control
            details["blink_enabled"] = blink_control != 0xFF
            details["mouth_control_byte"] = mouth_control
            details["mouth_enabled"] = mouth_control != 0xFF
            details["mouth_arg_byte_0"] = word1 & 0xFF
            details["mouth_arg_byte_1"] = (word1 >> 8) & 0xFF
            details["blink_half_steps"] = blink_half_steps
            details["blink_duration_units"] = blink_half_steps * 0.5
            details["expression_word_0_high_byte"] = (word0 >> 24) & 0xFF
            details["expression_word_1_high_byte"] = (word1 >> 24) & 0xFF
        return details
    if opcode == 0x35:
        return {
            "has_explicit_character": bool(arg_byte & 0x01),
            "manager_mode_bits": (arg_byte >> 1) & 0x07,
            "arg_flags": arg_byte,
            "payload_u32": args_u32,
        }
    if opcode == 0x36:
        details = {
            "has_explicit_character": bool(arg_byte & 0x01),
            "arg_flags": arg_byte,
        }
        if args_u32:
            details["animation_signal_word"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x3B and args_u32:
        explicit_char = bool(arg_byte & 0x01)
        control_index = 1 if explicit_char else 0
        details = {"has_explicit_character": explicit_char}
        if explicit_char:
            details["character_word"] = args_u32[0]
        if control_index < len(args_u32):
            details["blend_float_word"] = args_u32[control_index]
            details["blend_float"] = u32_to_f32(args_u32[control_index])
        return details
    if opcode == 0xC0:
        explicit_char = bool(arg_byte & 0x01)
        control_index = 1 if explicit_char else 0
        details = {"has_explicit_character": explicit_char}
        if explicit_char and args_u32:
            details["character_word"] = args_u32[0]
        if control_index < len(args_u32):
            control = args_u32[control_index]
            details["schedule_control_word"] = control
            details["schedule_low16"] = control & 0xFFFF
            details["schedule_arg_byte"] = (control >> 16) & 0xFF
            details["schedule_raw_high_byte"] = (control >> 24) & 0xFF
        return details
    if opcode == 0x39:
        details = {
            "manager_mode": arg_byte,
            "count": arg_byte & 0x1F,
            "post_mode_bits_5_6": (arg_byte >> 5) & 0x03,
            "release_mode_bit_7": bool(arg_byte & 0x80),
        }
        fields, complete = decode_character_event_leave_fields(arg_byte, args_u32)
        details["decoded_fields"] = fields
        details["decoded_complete"] = complete
        if args_u32:
            details["character_pairs"] = args_u32
        if args_u32:
            details["first_word_low16"] = args_u32[0] & 0xFFFF
            details["first_word_high16"] = (args_u32[0] >> 16) & 0xFFFF
        return details
    if opcode == 0x40:
        details = {
            "background_slot": arg_byte & 0x03,
            "uses_event_value_id": bool(arg_byte & 0x80),
        }
        if args_u32:
            details["background_id_or_event_value_id"] = args_u32[0] & 0xFFFF
        return details
    if opcode == 0x42:
        return {
            "setting_map_id": arg_byte & 0x0F,
            "payload_u32": args_u32,
        }
    if opcode == 0x43:
        return {
            "background_slot": arg_byte & 0x03,
            "payload_u32": args_u32,
        }
    if opcode == 0x44:
        details = {
            "change_map_mode": arg_byte & 0x0F,
            "arg_flags": arg_byte,
        }
        if args_u32:
            details["map_word_0"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x45:
        details = {
            "has_explicit_character": bool(arg_byte & 0x01),
            "arg_flags": arg_byte,
        }
        if args_u32:
            details["animation_word_0"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x46:
        return {
            "stop_animation_flags": arg_byte,
            "payload_u32": args_u32,
        }
    if opcode == 0x47:
        mode = (arg_byte >> 6) & 0x03
        flag0 = arg_byte & 0x01
        flag1 = (arg_byte >> 1) & 0x01
        shadow = (arg_byte >> 4) & 0x03
        details = {
            "background_mode": mode,
            "set_bg_disp_enabled": flag0 if mode in (0, 3) else None,
            "set_bg_visibility_alpha": 0.0 if flag0 else 1.0,
            "set_bg_visibility_bool": bool(flag1),
            "light_shadow_mode": shadow,
            "light_shadow_enable_arg": shadow - 1 if shadow else None,
            "unused_arg_bits_2_3": (arg_byte >> 2) & 0x03,
        }
        if mode == 1 and len(args_u32) == 4:
            details["direct_name_words"] = args_u32
        elif args_u32:
            details["payload_u32"] = args_u32
        return details
    if opcode == 0x48:
        details = {
            "arg_flags": arg_byte,
            "uses_event_value_or_alt": bool(arg_byte & 0x80),
        }
        if args_u32:
            details["landscape_word_0"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x4C:
        details = {"arg_flags": arg_byte}
        if args_u32:
            details["control_word"] = args_u32[0]
            details["updates_64bit_field"] = bool(args_u32[0] & 0x02)
            details["updates_scaled_float"] = bool(args_u32[0] & 0x04)
            details["updates_radi_halfword"] = bool(args_u32[0] & 0x08)
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x4D:
        details = {
            "auto_rate_mode": arg_byte,
            "payload_u32": args_u32,
        }
        if args_u32:
            details["target_or_control_word"] = args_u32[0]
        return details
    if opcode == 0x50:
        details = {
            "select_camera_slot": arg_byte & 0x03,
            "set_target_slot": (arg_byte >> 2) & 0x03,
        }
        if args_u32:
            word = args_u32[0]
            details["select_camera_id_low16"] = word & 0xFFFF
            details["target_id_high16"] = (word >> 16) & 0xFFFF
            details["select_camera_enabled"] = (word & 0xFFFF) != 0xFFFF
            details["set_target_enabled"] = ((word >> 16) & 0xFFFF) != 0xFFFF
        return details
    if opcode == 0x51:
        fields, complete = decode_camera_mode_fields(arg_byte, args_u32)
        return {
            "camera_mode": arg_byte & 0x0F,
            "arg_flag_04": bool(arg_byte & 0x10),
            "arg_flag_05": bool(arg_byte & 0x20),
            "arg_flag_06": bool(arg_byte & 0x40),
            "arg_flag_07": bool(arg_byte & 0x80),
            "decoded_fields": fields,
            "decoded_complete": complete,
            "payload_u32": args_u32,
        }
    if opcode == 0x52:
        mode = arg_byte & 0x03
        details = {
            "mode": mode,
            "initial_camera_vector": 1 if mode == 1 else 0,
            "control_word_path": 1 if mode == 2 else 0,
            "post_camera_vector": 1 if arg_byte & 0x04 else 0,
            "field58_float": 1 if arg_byte & 0x08 else 0,
            "position_abs_vector": 1 if arg_byte & 0x10 else 0,
        }
        if mode == 2 and args_u32:
            details["control_word"] = args_u32[0]
            details["control_flags_low16"] = args_u32[0] & 0xFFFF
            details["control_duration_high16"] = (args_u32[0] >> 16) & 0xFFFF
            details["payload_u32"] = args_u32[1:]
        else:
            details["payload_u32"] = args_u32
        return details
    if opcode == 0x54:
        details = {
            "arg_flag_00": bool(arg_byte & 0x01),
            "arg_flag_01": bool(arg_byte & 0x02),
            "target_slot_bits_2_3": (arg_byte >> 2) & 0x03,
            "source_bits_5_7": (arg_byte >> 5) & 0x07,
        }
        fields, complete = decode_camera_move_etc_fields(arg_byte, args_u32)
        details["decoded_fields"] = fields
        details["decoded_complete"] = complete
        if args_u32:
            details["camera_move_word_0"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x55:
        details = {
            "camera_capture_flags": arg_byte,
            "has_explicit_character": bool(arg_byte & 0x01),
            "target_mode_bits_1_2": (arg_byte >> 1) & 0x03,
            "posture_flag_5": bool(arg_byte & 0x20),
        }
        if args_u32:
            details["capture_control_word"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x56:
        return {
            "target_camera_slot": arg_byte >> 4,
            "move_mode": arg_byte & 0x0F,
            "payload_u32": args_u32,
        }
    if opcode == 0x57:
        return {
            "vibration_mode": arg_byte & 0x0F,
            "target_slot": (arg_byte >> 4) & 0x0F,
            "payload_u32": args_u32,
        }
    if opcode == 0x58:
        return {
            "clear_mode": arg_byte & 0x0F,
            "payload_u32": args_u32,
        }
    if opcode == 0x8E and len(args_u32) >= 3:
        xy = args_u32[0]
        word00 = args_u32[1]
        word08 = args_u32[2]
        return {
            "x_s16": sign_extend(xy & 0xFFFF, 16),
            "y_s16": sign_extend((xy >> 16) & 0xFFFF, 16),
            "writes_x_to_offsets": ["0x14", "0x18"],
            "writes_y_to_offsets": ["0x16", "0x1A"],
            "sets_offset_0e": 1,
            "text_message_word_00": word00,
            "text_message_word_00_bytes": [
                word00 & 0xFF,
                (word00 >> 8) & 0xFF,
                (word00 >> 16) & 0xFF,
                (word00 >> 24) & 0xFF,
            ],
            "text_message_word_08": word08,
            "text_message_word_08_low16": word08 & 0xFFFF,
            "text_message_word_08_high16": (word08 >> 16) & 0xFFFF,
            "text_message_word_08_bytes": [
                word08 & 0xFF,
                (word08 >> 8) & 0xFF,
                (word08 >> 16) & 0xFF,
                (word08 >> 24) & 0xFF,
            ],
        }
    if opcode == 0x89:
        details = {
            "attach_message_mode": (arg_byte >> 1) & 0x03,
            "rmf_start_flag_0": (arg_byte >> 6) & 0x01,
            "rmf_start_flag_1": (arg_byte >> 7) & 0x01,
        }
        if arg_byte & 1 and args_u32:
            details["message_character_id"] = args_u32[0] & 0xFFFF
            details["message_character_variant"] = (args_u32[0] >> 16) & 0xFF
        if args_u32:
            details["rmf_message_id"] = args_u32[-1] & 0xFFFF
        return details
    if opcode == 0x8F:
        mode = arg_byte >> 5
        details = {
            "mode": mode,
            "mode_name": TEXT_OUTPUT_MODE_NAMES.get(mode, "unknown"),
            "text_message_setup": {
                "byte_0c": 1,
                "byte_0d": 0xFA,
                "word_04": 0x20000000,
                "half_10": 0x1000,
                "half_12": 0x1000,
                "byte_0f": 0,
                "word_30": 0,
            },
        }
        if mode == 1 and args_u32:
            details["event_value_id"] = args_u32[0] & 0xFFFF
            details["number_text_width"] = (args_u32[0] >> 16) & 0xFF
            details["number_text_raw_high"] = (args_u32[0] >> 24) & 0xFF
        elif mode == 7:
            details["clear_text_id"] = 0xFA
        return details
    if opcode == 0x59:
        mode = arg_byte >> 4
        details = {"mode": mode}
        if mode <= 7:
            details["target_camera_slot"] = arg_byte & 0x0F
            details["has_first_float"] = 1 if arg_byte & 0x08 else 0
        elif mode == 0x0E:
            details["animation_kind"] = "fog_color"
            details["blend_flag"] = (arg_byte >> 1) & 0x01
            details["has_start_color"] = 1 if arg_byte & 0x08 else 0
        elif mode == 0x0F:
            details["animation_kind"] = "ambient_color"
            details["blend_flag"] = (arg_byte >> 1) & 0x01
            details["has_start_color"] = 1 if arg_byte & 0x08 else 0
        return details
    if opcode == 0x5A:
        return {
            "camera_flag_0": arg_byte & 0x01,
            "camera_flag_1": (arg_byte >> 1) & 0x01,
            "camera_flag_2": (arg_byte >> 2) & 0x01,
        }
    if opcode == 0x60:
        details = {
            "texture_mode": arg_byte & 0x1F,
        }
        if args_u32:
            word = args_u32[0]
            details["texture_id_low16_signed_adjusted"] = sign_extend(word & 0xFFFF, 16)
            details["texture_group_byte_2"] = (word >> 16) & 0xFF
        return details
    if opcode == 0x61:
        details = {
            "paf_load_mode": arg_byte & 0x1F,
        }
        if args_u32:
            details["paf_id_signed16"] = sign_extend(args_u32[0] & 0xFFFF, 16)
        return details
    if opcode == 0x62:
        details = {
            "arg_flags": arg_byte,
            "payload_u32": args_u32,
        }
        if args_u32:
            control = args_u32[0]
            details["control_word"] = control
            details["sprite_index"] = (control >> 24) & 0xFF
            details["control_mask_low12"] = control & 0x0FFF
            details["payload_words"] = args_u32[1:]
            details["sprite_fields"] = sprite_config_fields(control, args_u32[1:])
        return details
    if opcode == 0x67:
        details = {
            "fade_mode": (arg_byte >> 4) & 0x0F,
            "arg_low_flags": arg_byte & 0x0F,
        }
        if args_u32:
            details["fade_word_0"] = args_u32[0]
            details["fade_id_low16"] = args_u32[0] & 0xFFFF
            details["fade_flags_high16"] = (args_u32[0] >> 16) & 0xFFFF
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x63:
        details = {
            "delete_slot": bool(arg_byte & 0x80),
            "arg_flags": arg_byte,
        }
        if args_u32:
            details["slot_range_low16"] = args_u32[0] & 0xFFFF
            details["slot_range_high16"] = (args_u32[0] >> 16) & 0xFFFF
        return details
    if opcode == 0x64:
        details = {
            "primitive_slot": arg_byte & 0x07,
            "arg_flags": arg_byte,
        }
        if args_u32:
            details["paf_sequence_word"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x65:
        details = {
            "primitive_slot": arg_byte & 0x03,
            "paf_mode": (arg_byte >> 2) & 0x1F,
        }
        if args_u32:
            details["paf_sequence_id"] = args_u32[0] & 0xFFFF
        return details
    if opcode == 0x66:
        details = {
            "primitive_slot": arg_byte & 0x03,
        }
        if args_u32:
            details["priority"] = args_u32[0]
        return details
    if opcode == 0x68:
        details = {
            "updates_global_db_field": bool(arg_byte & 0x01),
            "updates_object_manager_visual_state": bool(arg_byte & 0x02),
            "arg_flags": arg_byte,
        }
        if args_u32:
            details["first_word"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x69:
        return {
            "primitive_slot": arg_byte & 0x07,
            "stored_byte": arg_byte >> 3,
            "payload_u32": args_u32,
        }
    if opcode == 0x6A:
        details = {
            "primitive_slot_flags": arg_byte & 0x1F,
            "arg_flags": arg_byte,
        }
        if args_u32:
            details["move_control_word"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x75:
        details = {
            "sound_mode": (arg_byte >> 6) & 0x03,
            "arg_low_flags": arg_byte & 0x3F,
        }
        if args_u32:
            details["sound_word_0"] = args_u32[0]
            details["sound_id_low16"] = args_u32[0] & 0xFFFF
        if len(args_u32) > 1:
            details["sound_word_1"] = args_u32[1]
        if len(args_u32) > 2:
            details["payload_u32"] = args_u32[2:]
        return details
    if opcode == 0x76:
        details = {
            "sound_stop_mode": arg_byte,
            "payload_u32": args_u32,
        }
        if args_u32:
            details["sound_word_0"] = args_u32[0]
        return details
    if opcode == 0x7C:
        details = {
            "movie_flags": arg_byte,
        }
        if args_u32:
            details["movie_id"] = args_u32[0] & 0xFFFF
        if len(args_u32) > 1:
            word = args_u32[1]
            details["movie_param_s16_0"] = sign_extend(word & 0xFFFF, 16)
            details["movie_param_s16_1"] = sign_extend((word >> 16) & 0xFFFF, 16)
        if len(args_u32) > 2:
            details["movie_extra_word"] = args_u32[2]
        return details
    if opcode == 0x7D:
        return {
            "movie_stop_flags": arg_byte,
            "payload_u32": args_u32,
        }
    if opcode == 0x72:
        details = {
            "sound_mode": arg_byte & 0x03,
            "pause_bgm": bool(arg_byte & 0x80),
        }
        if args_u32:
            details["fade_or_time_low16"] = args_u32[0] & 0xFFFF
        return details
    if opcode == 0x73:
        details = {
            "bgm_volume_slot": arg_byte & 0x03,
        }
        if args_u32:
            details["volume_low16"] = args_u32[0] & 0xFFFF
            details["time_or_curve_high16"] = (args_u32[0] >> 16) & 0xFFFF
        return details
    if opcode == 0x74:
        details = {
            "sound_load_mode": arg_byte & 0x1F,
        }
        if args_u32:
            details["sound_file_word"] = args_u32[0]
        return details
    if opcode == 0x8A:
        return {
            "window_mode": arg_byte,
            "arg_flag_07": bool(arg_byte & 0x80),
            "payload_u32": args_u32,
        }
    if opcode == 0x79:
        details = {
            "listener_mode": arg_byte & 0x07,
            "arg_flags": arg_byte,
        }
        if args_u32:
            details["listener_word_0"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0x83:
        details = {"arg_flags": arg_byte}
        if args_u32:
            word = args_u32[0]
            details["vibration_word"] = word
            details["duration_or_id_high16"] = (word >> 16) & 0xFFFF
            details["strength_byte_0"] = word & 0xFF
            details["pattern_byte_1"] = (word >> 8) & 0xFF
        return details
    if opcode == 0x82:
        return {
            "vibration_stop_flags": arg_byte,
            "payload_u32": args_u32,
        }
    if opcode == 0xC0:
        details = {
            "has_explicit_character": bool(arg_byte & 0x01),
            "arg_flags": arg_byte,
        }
        if args_u32:
            details["character_word"] = args_u32[0]
        if len(args_u32) > 1:
            details["schedule_payload_u32"] = args_u32[1:]
        return details
    if opcode == 0xC1:
        details = {
            "has_explicit_character": bool(arg_byte & 0x01),
            "arg_flags": arg_byte,
        }
        if args_u32:
            details["map_word_0"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0xC4:
        details = {
            "has_explicit_character": bool(arg_byte & 0x01),
            "arg_flags": arg_byte,
        }
        if args_u32:
            details["character_word"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0xC5:
        details = {
            "arg_flags": arg_byte,
        }
        if args_u32:
            word = args_u32[0]
            details["hour_or_mode_byte_0"] = word & 0xFF
            details["minute_or_mode_byte_1"] = (word >> 8) & 0xFF
            details["time_value_u16_2"] = (word >> 16) & 0xFFFF
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0xD5:
        fields, complete = decode_special_effect_fields(arg_byte, args_u32)
        details = {
            "has_explicit_first_character": bool(arg_byte & 0x01),
            "has_explicit_second_character": bool(arg_byte & 0x02),
            "abort_mode": bool(arg_byte & 0x80),
            "arg_flags": arg_byte,
            "decoded_fields": fields,
            "decoded_complete": complete,
        }
        if args_u32:
            details["effect_word_0"] = args_u32[0]
        if len(args_u32) > 1:
            details["payload_u32"] = args_u32[1:]
        return details
    if opcode == 0xF0:
        return {
            "special_mode_high_nibble": (u32(payload, offset) >> 28) & 0x0F,
            "note": "opcode >= 0xF0 consumes only the header in StepProcess default advancement",
        }
    return {}

def bytes_to_hex(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)

def words_to_csv(words: list[int]) -> str:
    return ",".join(f"0x{word:08X}" for word in words)

def label_name(offset: int) -> str:
    return f"loc_{offset:04X}"

def parse_hex_int(text: str) -> int:
    if text.lower().startswith("loc_"):
        return int(text[4:], 16)
    return int(text, 0)

def parse_hex_byte(text: str) -> int:
    value = int(text, 16)
    if not 0 <= value <= 0xFF:
        raise ValueError(f"byte value out of range: {text}")
    return value

def parse_key_values(parts: list[str], line_no: int, directive: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            raise ValueError(f"line {line_no}: malformed {directive} argument {part}")
        key, value = part.split("=", 1)
        if key in values:
            raise ValueError(f"line {line_no}: duplicate {directive} argument {key}")
        values[key] = value
    # `yield=1` is the readable spelling of a clear StepProcess continue bit
    # (flags byte 0x00): the script pauses for a frame after this command.
    if "yield" in values:
        if "flags" in values:
            raise ValueError(f"line {line_no}: {directive} cannot carry both yield= and flags=")
        if parse_hex_int(values.pop("yield")) != 1:
            raise ValueError(f"line {line_no}: {directive} yield= must be 1 when present")
        values["flags"] = "0x00"
    return values

def parse_word_list(text: str) -> list[int]:
    if text == "":
        return []
    return [parse_hex_int(value) for value in text.split(",") if value]

def parse_optional_word_list(fields: dict[str, str], name: str = "words") -> list[int]:
    if name not in fields:
        return []
    return parse_word_list(fields[name])

def pack_u32_words(words: list[int]) -> bytes:
    return b"".join((word & 0xFFFFFFFF).to_bytes(4, "little") for word in words)

def build_command_header(opcode: int, word_count: int, arg: int = 0, flags: int = 0) -> int:
    if not 0 <= opcode <= 0xFF:
        raise ValueError(f"opcode out of range: 0x{opcode:X}")
    if not 0 <= word_count <= 0xFF:
        raise ValueError(f"word count out of range: {word_count}")
    if not 0 <= arg <= 0xFF:
        raise ValueError(f"handler argument out of range: 0x{arg:X}")
    if not 0 <= flags <= 0xFF:
        raise ValueError(f"header flags out of range: 0x{flags:X}")
    return opcode | (word_count << 8) | (arg << 16) | (flags << 24)

def require_fields(fields: dict[str, str], required: set[str], line_no: int, directive: str) -> None:
    missing_fields = sorted(required - set(fields))
    if missing_fields:
        raise ValueError(f"line {line_no}: {directive} missing {', '.join(missing_fields)}")

def require_header_opcode(header: int, opcode: int, line_no: int, directive: str) -> None:
    actual_opcode = header & 0xFF
    if actual_opcode != opcode:
        raise ValueError(f"line {line_no}: {directive} header opcode 0x{actual_opcode:02X} is not 0x{opcode:02X}")

def resolve_packed_word(
    fields: dict[str, str],
    line_no: int,
    directive: str,
    packed_name: str,
    specs: tuple[tuple[str, int, int], ...],
    *,
    required: bool = True,
    default: int = 0,
) -> int:
    """Resolve a packed u32 from its raw hex form or from its named parts.

    Each entry of ``specs`` is ``(field_name, shift, mask)``. A named part may
    stand in for ``packed_name`` when the raw word is absent, or accompany it as
    a guard. Supplying both with conflicting values is an error rather than a
    silently ignored edit.
    """
    present: dict[str, int] = {}
    for name, _, mask in specs:
        if name in fields:
            value = parse_hex_int(fields[name])
            if value & ~mask:
                raise ValueError(f"line {line_no}: {directive} {name} does not fit in mask 0x{mask:X}")
            present[name] = value

    if packed_name in fields:
        word = parse_hex_int(fields[packed_name]) & 0xFFFFFFFF
    elif present:
        word = default & 0xFFFFFFFF
        for name, shift, mask in specs:
            if name in present:
                word = (word & ~((mask << shift) & 0xFFFFFFFF)) | ((present[name] & mask) << shift)
    elif required:
        raise ValueError(f"line {line_no}: {directive} missing {packed_name}")
    else:
        return default & 0xFFFFFFFF

    for name, shift, mask in specs:
        if name in present and present[name] != ((word >> shift) & mask):
            raise ValueError(f"line {line_no}: {directive} {name} does not match {packed_name}")
    return word

def resolve_named_choice(
    fields: dict[str, str],
    line_no: int,
    directive: str,
    value_name: str,
    name_field: str,
    table: dict[int, str],
    *,
    required: bool = True,
    default: int = 0,
) -> int:
    """Resolve a small enum from its numeric field or its friendly name field."""
    if value_name in fields:
        value = parse_hex_int(fields[value_name])
    elif name_field in fields:
        wanted = fields[name_field]
        matches = sorted(key for key, text in table.items() if text == wanted)
        if not matches:
            known = ", ".join(sorted(set(table.values())))
            raise ValueError(f"line {line_no}: {directive} {name_field} {wanted!r} is not one of {known}")
        value = matches[0]
    elif required:
        raise ValueError(f"line {line_no}: {directive} missing {value_name}")
    else:
        return default

    if name_field in fields:
        expected = table.get(value, "unknown")
        if fields[name_field] != expected:
            raise ValueError(
                f"line {line_no}: {directive} {name_field} {fields[name_field]!r} does not match {value_name} ({expected})"
            )
    return value

def parse_source_target(text: str, labels: dict[str, int], line_no: int) -> int:
    if text in labels:
        return labels[text]
    try:
        return parse_hex_int(text)
    except ValueError as exc:
        raise ValueError(f"line {line_no}: unknown label or offset {text}") from exc

def compile_source_branch(offset: int, header: int, target: int, condition_id: int, condition_words: list[int], line_no: int, strict: bool = True) -> bytes:
    if target % 4:
        raise ValueError(f"line {line_no}: branch target must be word-aligned")
    branch_base = offset + (16 if condition_id else 8)
    relative_bytes = target - branch_base
    if relative_bytes % 4:
        raise ValueError(f"line {line_no}: branch target is not a word offset from base")
    relative_words = relative_bytes // 4
    if not -(1 << 23) <= relative_words < (1 << 23):
        raise ValueError(f"line {line_no}: branch target is outside signed 24-bit range")
    if strict and condition_id and len(condition_words) != 2:
        raise ValueError(f"line {line_no}: conditional branch expects exactly two condition_args words")
    if strict and not condition_id and condition_words:
        raise ValueError(f"line {line_no}: unconditional branch cannot have condition_args")
    branch_word = ((condition_id & 0xFF) << 24) | (relative_words & 0x00FFFFFF)
    return header.to_bytes(4, "little") + branch_word.to_bytes(4, "little") + pack_u32_words(condition_words)

def decode_special_f0_fields(word: int) -> list[str]:
    return [
        f"raw=0x{word & 0xFFFFFFFF:08X}",
        f"mode={(word >> 28) & 0x07}",
        f"key=0x{(word >> 8) & 0xFFFFFF:06X}",
    ]

def format_special_f0_line(word: int) -> str:
    """Friendly spelling for an opcode-0xF0 word, or the raw special_f0 form.

    Character/animation EVDs use these as markers: mode nibble 3 carries a
    signed 20-bit animation frame in bits 8-27 (CCharacterAnim::ChangeFrame_sub
    at 0x002A4A20 matches it against the playing frame and runs the commands at
    that marker), mode 7 is the end sentinel (frame = infinity; Command_f0 also
    sets the SCR_DATA status bit 0x20 when executed), and mode 2 calls
    CCharacterPerson::SetSchedulePercent(bits 8-27). Bit 31 is the standard
    StepProcess keep-going bit.
    """
    word &= 0xFFFFFFFF
    mode = (word >> 28) & 0x07
    yield_tail = "" if word >> 31 else " yield=1"
    if word == 0xF00000F0:
        return "  anim_script_end"
    if mode == 3:
        frame = (word >> 8) & 0xFFFFF
        signed = frame if frame < 0x7FFFF else frame - 0x100000
        return f"  anim_frame_trigger frame={signed}{yield_tail}"
    if mode == 2:
        return f"  set_schedule_percent percent={(word >> 8) & 0xFFFFF}{yield_tail}"
    if mode == 0:
        # A labelled point in the script. Command_f0 only handles modes 2 and 7,
        # so running one does nothing; its purpose is to be jumped to. The
        # marker table at header+8 holds word offsets that GetMarkerAddress
        # turns into addresses, and seek_marker jumps there. The number is the
        # author's own label: across the corpus it often matches the entry's
        # position in the table but not reliably, so it is not the index.
        return f"  marker id={(word >> 8) & 0xFFFFF}{yield_tail}"
    return f"  special_f0 {' '.join(decode_special_f0_fields(word))}"

def build_special_f0_friendly(directive: str, fields: dict[str, str], line_no: int) -> int:
    flags = parse_hex_int(fields.get("flags", "0x80"))
    bit31 = 1 if flags & 0x80 else 0
    if directive == "anim_script_end":
        return 0xF00000F0
    if directive == "marker":
        require_fields(fields, {"id"}, line_no, "marker")
        marker_id = parse_hex_int(fields["id"])
        if not 0 <= marker_id <= 0xFFFFF:
            raise ValueError(f"line {line_no}: marker id out of range")
        return 0xF0 | (marker_id << 8) | (bit31 << 31)
    if directive == "anim_frame_trigger":
        require_fields(fields, {"frame"}, line_no, "anim_frame_trigger")
        frame = int(fields["frame"], 0)
        if not -0x80001 <= frame <= 0x7FFFE:
            raise ValueError(f"line {line_no}: anim_frame_trigger frame out of range")
        return 0xF0 | ((frame & 0xFFFFF) << 8) | (0x3 << 28) | (bit31 << 31)
    require_fields(fields, {"percent"}, line_no, "set_schedule_percent")
    percent = parse_hex_int(fields["percent"])
    if not 0 <= percent <= 0xFFFFF:
        raise ValueError(f"line {line_no}: set_schedule_percent percent out of range")
    return 0xF0 | (percent << 8) | (0x2 << 28) | (bit31 << 31)

def build_special_f0_word(fields: dict[str, str], line_no: int) -> int:
    require_fields(fields, {"raw"}, line_no, "special_f0")
    raw = parse_hex_int(fields["raw"]) & 0xFFFFFFFF
    if (raw & 0xFF) != 0xF0:
        raise ValueError(f"line {line_no}: special_f0 raw opcode low byte must be 0xF0")
    if "mode" in fields and parse_hex_int(fields["mode"]) != ((raw >> 28) & 0x07):
        raise ValueError(f"line {line_no}: special_f0 mode does not match raw")
    if "key" in fields and parse_hex_int(fields["key"]) != ((raw >> 8) & 0xFFFFFF):
        raise ValueError(f"line {line_no}: special_f0 key does not match raw")
    return raw

def source_value_equivalent(written: str, decoded: str) -> bool:
    """Compare a hand-written field value with the value the decoder prints."""
    if written == decoded:
        return True

    def unquote(text: str) -> str:
        if len(text) > 1 and text[0] == '"' and text[-1] == '"':
            return text[1:-1]
        return text

    left, right = unquote(written), unquote(decoded)
    if left == right:
        return True
    try:
        if int(left, 0) == int(right, 0):
            return True
    except ValueError:
        pass
    try:
        if float(left) == float(right):
            return True
    except ValueError:
        pass
    if "," in left and "," in right:
        left_parts, right_parts = left.split(","), right.split(",")
        if len(left_parts) == len(right_parts):
            return all(
                source_value_equivalent(a.strip(), b.strip())
                for a, b in zip(left_parts, right_parts)
            )
    # axis:value pairs compare by axis name plus numeric value, so that a written
    # `y:1.0` matches an assembled `y:1`.
    if ":" in left and ":" in right:
        left_axis, _, left_value = left.partition(":")
        right_axis, _, right_value = right.partition(":")
        if left_axis.strip() == right_axis.strip():
            return source_value_equivalent(left_value.strip(), right_value.strip())
    # Named sentinels the decoders print for special numeric values.
    sentinels = {"off": (0,), "keep": (0xFF, 0xFFFF), "resync": (0xFFFF,), "forever": (0xFFFF,)}
    for name, other in ((left, right), (right, left)):
        try:
            number = int(other, 0)
        except ValueError:
            continue
        if name in sentinels and number in sentinels[name]:
            return True
        if name == "on" and number > 0 and (number & (number + 1)) == 0:
            # `on` covers any all-bits-set mask (1, 3, 7, ... per flag count).
            return True
    return False

def verify_built_command(data: bytes, parts: list[str], line_no: int) -> None:
    """Reject written fields that the builder did not actually encode.

    Every structured command is decoded straight back after it is built and the
    written fields are compared with what the decoder prints. A field that the
    builder ignores would silently survive into a different command, so treat any
    mismatch as an error rather than letting the edit disappear.
    """
    directive = parts[0]
    if directive.startswith(".") or len(data) < 4 or len(data) % 4:
        return
    try:
        fields = parse_key_values(parts[1:], line_no, directive)
    except ValueError:
        return
    # Branch targets are label references; the rebuilt command can only show an
    # absolute offset, so the branch framing logic validates those. Every other
    # field on the line still has to survive the round trip, otherwise readable
    # spellings like `kind=` could be edited with no effect on the output.
    fields = {key: value for key, value in fields.items() if key not in ("target", "goto")}
    if not fields:
        return
    if set(fields) & RAW_PAYLOAD_FIELD_NAMES:
        return
    try:
        command = decode_command_at(data, 0)
        if command.get("truncated") or int(command["end_offset"]) != len(data):
            return
        rendered = format_source_command(command, {})
    except Exception:  # noqa: BLE001 - verification must never mask a real build
        return
    decoded_parts = rendered.split()
    if not decoded_parts or decoded_parts[0] != directive:
        return
    try:
        decoded = parse_key_values(split_source_parts(rendered.strip(), line_no)[1:], line_no, directive)
    except ValueError:
        return
    for key, value in fields.items():
        if key not in decoded:
            continue
        if not source_value_equivalent(value, decoded[key]):
            raise ValueError(
                f"line {line_no}: {directive} {key}={value} is not encoded by this command "
                f"(it assembles as {key}={decoded[key]}); adjust the field it derives from"
            )

def compile_evd_source(text: str) -> bytes:
    """Compile a small label-based event source language into a raw EVD file.

    This is intentionally a bridge toward higher-level event authoring, not a
    replacement for lossless EVDASM. It only auto-builds layouts whose command
    size and operands are already proven by the traced handlers.
    """

    source_items: list[dict[str, Any]] = []
    labels: dict[str, int] = {}
    header_value = 3
    header_extra = 0
    header_extra_set = False
    marker_table_offset: int | None = None
    current = 0x0C

    for line_no, raw_line in enumerate(text.splitlines(), 1):
        line = strip_source_comment(raw_line)
        if not line:
            continue
        if line.endswith(":"):
            name = line[:-1]
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
                raise ValueError(f"line {line_no}: invalid label name {name}")
            labels[name] = current
            continue
        parts = split_source_parts(line, line_no)
        head = parts[0].lower()
        directive = resolve_form_name(head)
        parts[0] = directive
        if not directive.startswith("."):
            # Readable character convention: `character=default` marks the
            # script's default character (explicit_char=0), and a plain
            # `character=` implies explicit_char=1.
            has_explicit = any(part.startswith("explicit_char=") for part in parts[1:])
            char_index = next((i for i, part in enumerate(parts) if part == "character=default"), None)
            if char_index is not None:
                parts.pop(char_index)
                if not has_explicit:
                    parts.append("explicit_char=0")
            elif not has_explicit and any(part.startswith("character=") for part in parts[1:]):
                parts.append("explicit_char=1")
        if directive in HIGH_LEVEL_COMMANDS or directive in HIGH_LEVEL_NAME_ALIASES:
            # High-level authoring names (set_flag, clear_flag, wait_for, ...)
            # are valid EVDSRC statements too: lower them to their form line.
            # The capture set is strictly the authoring parameters (plus the
            # flags byte for yield=), so decompiled lines carrying engine
            # fields always fall through to the form builders.
            kwargs: dict[str, str] = {}
            parseable = True
            for part in parts[1:]:
                key, sep, value = part.partition("=")
                if not sep:
                    parseable = False
                    break
                if key == "yield" and value == "1":
                    key, value = "flags", "0x00"
                kwargs[key] = value
            if parseable:
                spec = HIGH_LEVEL_COMMANDS[directive]
                allowed = set(spec["params"]) | set(spec.get("companions", {})) | {"flags"}
                if set(kwargs) <= allowed:
                    lowered = build_high_level_command(directive, [], kwargs)
                    parts = split_source_parts(lowered.strip(), line_no)
                    head = parts[0].lower()
                    directive = resolve_form_name(head)
                    parts[0] = directive
        if directive == "expr" and head in EXPR_FLAG_HEAD_OPS:
            # `set_character_flag ... flag=N` is `or` with bit N; the clear form
            # is `and` with that one bit missing from a byte of ones.
            flag_index = next(
                (int(part.split("=", 1)[1], 0) for part in parts[1:] if part.startswith("flag=")),
                None,
            )
            if flag_index is None or not 0 <= flag_index <= 7:
                raise ValueError(f"line {line_no}: {head} needs flag=0..7")
            parts = [part for part in parts if not part.startswith("flag=")]
            op = EXPR_FLAG_HEAD_OPS[head]
            parts.append(f"op={op}")
            parts.append(f"value={(1 << flag_index) if op == 7 else (~(1 << flag_index)) & 0xFF}")
        elif directive == "expr" and head in EXPR_HEAD_OPS and not any(part.startswith("op=") for part in parts[1:]):
            # The head names the operation: add_value, sub_value, ...
            parts.append(f"op={EXPR_HEAD_OPS[head]}")
        if directive in PARAMETER_ALIASES_REVERSE:
            for index in range(1, len(parts)):
                key, sep, value = parts[index].partition("=")
                if sep:
                    parts[index] = resolve_parameter_name(directive, key) + sep + value
        if not directive.startswith("."):
            parts = resolve_symbol_name_values(directive, parts, line_no)
        if directive in (".evdsrc", ".evd_source"):
            continue
        if directive == ".header":
            if len(parts) != 2:
                raise ValueError(f"line {line_no}: .header expects one value")
            header_value = parse_hex_int(parts[1])
            if not 0 <= header_value <= 0xFFFFFFFF:
                raise ValueError(f"line {line_no}: .header value out of range")
            continue
        if directive == ".header_extra":
            if len(parts) != 2:
                raise ValueError(f"line {line_no}: .header_extra expects one value")
            header_extra = parse_hex_int(parts[1])
            header_extra_set = True
            if not 0 <= header_extra <= 0xFFFFFFFF:
                raise ValueError(f"line {line_no}: .header_extra value out of range")
            continue
        if directive == ".entry":
            # Source entries are labels for people/readability. CRadiScript uses
            # the command stream itself; EVD files in the corpus start commands
            # at 0x0C.
            continue
        if directive == ".org":
            if len(parts) != 2:
                raise ValueError(f"line {line_no}: .org expects one offset")
            new_current = parse_hex_int(parts[1])
            if new_current < 0x0C:
                raise ValueError(f"line {line_no}: .org cannot move before the EVD header")
            if new_current < current:
                raise ValueError(f"line {line_no}: .org cannot move backwards")
            current = new_current
            continue
        if directive == ".align":
            if len(parts) != 2:
                raise ValueError(f"line {line_no}: .align expects one byte alignment")
            align = parse_hex_int(parts[1])
            if align <= 0 or align & (align - 1):
                raise ValueError(f"line {line_no}: .align must be a positive power of two")
            current = (current + align - 1) & -align
            continue

        size = 0
        if directive in RAW_ESCAPE_FORMS:
            raw_fields = parse_key_values(parts[1:], line_no, directive)
            if "words" in raw_fields and RAW_ESCAPE_FORMS[directive][1] not in raw_fields:
                # Raw form (data regions, truncated commands): the words= list
                # keeps its real length regardless of the form's fixed shape.
                size = 4 * (1 + len(parse_optional_word_list(raw_fields)))
                source_items.append({"line_no": line_no, "offset": current, "parts": parts, "directive": directive})
                current += size
                continue
        if directive == ".bytes":
            if len(parts) < 2:
                raise ValueError(f"line {line_no}: .bytes expects byte values")
            size = len(parts) - 1
        elif directive == ".word":
            if len(parts) < 2:
                raise ValueError(f"line {line_no}: .word expects word values")
            size = 4 * (len(parts) - 1)
        elif directive in ("special_f0", "anim_frame_trigger", "set_schedule_percent", "anim_script_end", "marker"):
            size = 4
        elif directive == ".marker_table":
            if current % 4:
                raise ValueError(f"line {line_no}: .marker_table must start on a 4-byte boundary")
            if marker_table_offset is not None:
                raise ValueError(f"line {line_no}: only one .marker_table is supported")
            marker_table_offset = current
            marker_table_word_offset = current // 4
            if header_extra_set and header_extra != marker_table_word_offset:
                raise ValueError(
                    f"line {line_no}: .header_extra 0x{header_extra:08X} does not match "
                    f".marker_table word offset 0x{marker_table_word_offset:08X}"
                )
            header_extra = marker_table_word_offset
            size = 4 + 4 * (len(parts) - 1)
        elif directive == ".cmd":
            fields = parse_key_values(parts[1:], line_no, ".cmd")
            require_fields(fields, {"op"}, line_no, ".cmd")
            size = 4 * (1 + len(parse_optional_word_list(fields)))
        elif directive == "character_expression":
            fields = parse_key_values(parts[1:], line_no, "character_expression")
            require_fields(fields, {"explicit_char"}, line_no, "character_expression")
            if "words" in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
            else:
                size = 16 if parse_hex_int(fields["explicit_char"]) else 12
        elif directive == "character_eye_control":
            fields = parse_key_values(parts[1:], line_no, "character_eye_control")
            require_fields(fields, {"explicit_char"}, line_no, "character_eye_control")
            if "words" in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
            else:
                base_words = 2 if parse_hex_int(fields["explicit_char"]) else 1
                has_time = "manual_time_word" in fields or "manual_time" in fields
                size = 4 * (1 + base_words + (1 if has_time else 0))
        elif directive == "character_attach_render":
            fields = parse_key_values(parts[1:], line_no, "character_attach_render")
            if "words" in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
            else:
                explicit_char = parse_hex_int(fields.get("explicit_char", "1" if "character" in fields else "0"))
                size = 8 if explicit_char else 4
        elif directive == "strong_motion_blend":
            fields = parse_key_values(parts[1:], line_no, "strong_motion_blend")
            require_fields(fields, {"explicit_char"}, line_no, "strong_motion_blend")
            if "words" in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
            else:
                size = 12 if parse_hex_int(fields["explicit_char"]) else 8
        elif directive == "character_precreate_anim":
            fields = parse_key_values(parts[1:], line_no, "character_precreate_anim")
            require_fields(fields, {"explicit_char"}, line_no, "character_precreate_anim")
            if "words" in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
            else:
                size = 12 if parse_hex_int(fields["explicit_char"]) else 8
        elif directive == "character_detach_parent":
            fields = parse_key_values(parts[1:], line_no, "character_detach_parent")
            if "words" in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
            else:
                require_fields(fields, {"explicit_char"}, line_no, "character_detach_parent")
                size = 8 if parse_hex_int(fields["explicit_char"]) else 4
        elif directive == "person_schedule_list":
            fields = parse_key_values(parts[1:], line_no, "person_schedule_list")
            require_fields(fields, {"explicit_char"}, line_no, "person_schedule_list")
            if "words" in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
            else:
                size = 12 if parse_hex_int(fields["explicit_char"]) else 8
        elif directive == "character_attribute":
            fields = parse_key_values(parts[1:], line_no, "character_attribute")
            require_fields(fields, {"explicit_char"}, line_no, "character_attribute")
            if "words" in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
            else:
                size = 16 if parse_hex_int(fields["explicit_char"]) else 12
        elif directive == "character_auto_rate_anim":
            fields = parse_key_values(parts[1:], line_no, "character_auto_rate_anim")
            if "words" in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
            else:
                size = 4 * (1 + len(build_auto_rate_words(fields, line_no)))
        elif directive == "character_animation":
            fields = parse_key_values(parts[1:], line_no, "character_animation")
            if "words" in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
            else:
                size = 4 * (1 + len(build_character_animation_words(fields, line_no)))
        elif directive == "character_rotate_option":
            fields = parse_key_values(parts[1:], line_no, "character_rotate_option")
            if "words" in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
            else:
                size = 4 * (1 + len(build_rotate_option_words(fields, line_no)))
        elif directive == "camera_color_anim":
            fields = parse_key_values(parts[1:], line_no, "camera_color_anim")
            if "words" in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
            else:
                size = 4 * (1 + len(build_camera_color_anim_words(fields, line_no)))
        elif directive == "camera_move_etc":
            fields = parse_key_values(parts[1:], line_no, "camera_move_etc")
            _arg, words = build_camera_move_etc_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "camera_move_existing":
            fields = parse_key_values(parts[1:], line_no, "camera_move_existing")
            _arg, words = build_camera_move_existing_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "camera_capture_target":
            fields = parse_key_values(parts[1:], line_no, "camera_capture_target")
            _arg, words = build_camera_capture_target_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "marker_seek":
            fields = parse_key_values(parts[1:], line_no, "marker_seek")
            _arg, words = build_marker_seek_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "camera_transform_param":
            fields = parse_key_values(parts[1:], line_no, "camera_transform_param")
            if "words" in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
            else:
                size = 4 * (1 + len(build_camera_transform_words(fields, line_no)))
        elif directive == "character_move_position":
            fields = parse_key_values(parts[1:], line_no, "character_move_position")
            if "words" in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
            else:
                size = 4 * (1 + len(build_character_move_position_words(fields, line_no)))
        elif directive == "character_move_points":
            fields = parse_key_values(parts[1:], line_no, "character_move_points")
            if "words" in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
            else:
                size = 4 * (1 + len(build_character_move_points_words(fields, line_no)))
        elif directive == "character_move_pause":
            fields = parse_key_values(parts[1:], line_no, "character_move_pause")
            size = 4 * (1 + len(build_character_move_pause_words(fields, line_no)))
        elif directive == "character_data":
            fields = parse_key_values(parts[1:], line_no, "character_data")
            _arg, words = build_character_data_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "character_delete_data":
            fields = parse_key_values(parts[1:], line_no, "character_delete_data")
            _arg, words = build_character_delete_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "character_event_leave":
            fields = parse_key_values(parts[1:], line_no, "character_event_leave")
            _arg, words = build_character_event_leave_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "character_equipment":
            fields = parse_key_values(parts[1:], line_no, "character_equipment")
            _arg, words = build_character_equipment_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "character_collision_setup":
            fields = parse_key_values(parts[1:], line_no, "character_collision_setup")
            _arg, words = build_character_collision_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "character_anim_signal":
            fields = parse_key_values(parts[1:], line_no, "character_anim_signal")
            _arg, words = build_character_anim_signal_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "special_effect":
            fields = parse_key_values(parts[1:], line_no, "special_effect")
            _arg, words = build_special_effect_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "background_visibility":
            fields = parse_key_values(parts[1:], line_no, "background_visibility")
            _arg, words = build_background_visibility_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "position_vibration_param":
            fields = parse_key_values(parts[1:], line_no, "position_vibration_param")
            words = build_position_vibration_param_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "script_start_stack":
            fields = parse_key_values(parts[1:], line_no, "script_start_stack")
            _arg, words = build_script_start_stack_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "battle_character_entry":
            fields = parse_key_values(parts[1:], line_no, "battle_character_entry")
            _arg, words = build_battle_character_entry_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "character_sub_anim":
            fields = parse_key_values(parts[1:], line_no, "character_sub_anim")
            _arg, words = build_character_sub_anim_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "character_movement":
            fields = parse_key_values(parts[1:], line_no, "character_movement")
            _arg, words = build_character_movement_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive in (
            "change_game_mode",
            "radiata_time_enable",
            "character_attach_render",
            "character_detach_parent",
            "background_change_map",
            "position_vibration_clear",
        ):
            size = 4 * (1 + len(parse_optional_word_list(parse_key_values(parts[1:], line_no, directive))))
        elif directive == "position_vibration_vector":
            fields = parse_key_values(parts[1:], line_no, "position_vibration_vector")
            _arg, words = build_position_vibration_vector_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "character_single_manager":
            fields = parse_key_values(parts[1:], line_no, "character_single_manager")
            _arg, words = build_character_single_manager_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "person_field_update":
            fields = parse_key_values(parts[1:], line_no, "person_field_update")
            _arg, words = build_person_field_update_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "camera_mode":
            fields = parse_key_values(parts[1:], line_no, "camera_mode")
            _arg, words = build_camera_mode_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "script_defaults":
            fields = parse_key_values(parts[1:], line_no, "script_defaults")
            _arg, words = build_script_defaults_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "character_virtual_24":
            fields = parse_key_values(parts[1:], line_no, "character_virtual_24")
            _arg, words = build_character_virtual_24_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "stand_context":
            fields = parse_key_values(parts[1:], line_no, "stand_context")
            _arg, words = build_stand_context_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "battle_acquisition_setup":
            fields = parse_key_values(parts[1:], line_no, "battle_acquisition_setup")
            _arg, words = build_battle_acquisition_setup_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "background_play_animation":
            fields = parse_key_values(parts[1:], line_no, "background_play_animation")
            _arg, words = build_background_play_animation_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "character_attach_parent":
            fields = parse_key_values(parts[1:], line_no, "character_attach_parent")
            _arg, words = build_character_attach_parent_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "map_change_check":
            fields = parse_key_values(parts[1:], line_no, "map_change_check")
            _arg, _flags, words = build_map_change_check_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "background_auto_rate_anim":
            fields = parse_key_values(parts[1:], line_no, "background_auto_rate_anim")
            _arg, words = build_background_auto_rate_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "script_stop":
            fields = parse_key_values(parts[1:], line_no, "script_stop")
            _arg, words = build_script_stop_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "script_start":
            fields = parse_key_values(parts[1:], line_no, "script_start")
            _arg, words = build_script_start_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "scene_save_env":
            fields = parse_key_values(parts[1:], line_no, "scene_save_env")
            _arg, words = build_scene_save_env_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "background_stop_animation":
            fields = parse_key_values(parts[1:], line_no, "background_stop_animation")
            _arg, words = build_background_stop_animation_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "personal_inventory":
            fields = parse_key_values(parts[1:], line_no, "personal_inventory")
            if "words" in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
            else:
                size = 4 * (1 + len(build_personal_inventory_words(fields, line_no)))
        elif directive == "background_runtime_field":
            fields = parse_key_values(parts[1:], line_no, "background_runtime_field")
            _control, words = build_background_runtime_field_words(fields, line_no)
            size = 8 + 4 * len(words)
        elif directive == "landscape_visibility":
            fields = parse_key_values(parts[1:], line_no, "landscape_visibility")
            _arg, words = build_landscape_visibility_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "party_member":
            size = 8
        elif directive == "talk_rmf":
            size = 8
        elif directive == "fade_control":
            fields = parse_key_values(parts[1:], line_no, "fade_control")
            _arg, words = build_fade_control_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "trigger":
            fields = parse_key_values(parts[1:], line_no, "trigger")
            size = 4 * (1 + len(build_trigger_words(fields, line_no)))
        elif directive == "expr":
            size = 16
        elif directive in ("set_bgm", "play_movie"):
            fields = parse_key_values(parts[1:], line_no, directive)
            if "words" in fields and ("info0" if directive == "set_bgm" else "movie") not in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
            else:
                size = 12 if directive == "set_bgm" else 16
        elif directive == "play_sound_effect":
            fields = parse_key_values(parts[1:], line_no, "play_sound_effect")
            if "words" in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
            else:
                size = 4 * (1 + len(build_play_sound_effect_words(fields, line_no)))
        elif directive == "sound_listener":
            fields = parse_key_values(parts[1:], line_no, "sound_listener")
            _arg, words = build_sound_listener_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "sound_effect_stack":
            fields = parse_key_values(parts[1:], line_no, "sound_effect_stack")
            size = 4 * (1 + len(parse_optional_word_list(fields)))
        elif directive == "load_sound_resource":
            fields = parse_key_values(parts[1:], line_no, "load_sound_resource")
            _arg, words = build_load_sound_resource_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "stop_sound_effect":
            fields = parse_key_values(parts[1:], line_no, "stop_sound_effect")
            _arg, words = build_stop_sound_effect_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "play_vibration":
            fields = parse_key_values(parts[1:], line_no, "play_vibration")
            if "words" in fields and "strength" not in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
            else:
                size = 8
        elif directive == "sprite_config":
            fields = parse_key_values(parts[1:], line_no, "sprite_config")
            if "words" in fields:
                size = 8 + 4 * len(parse_optional_word_list(fields))
            else:
                if "control" in fields:
                    control = parse_hex_int(fields["control"])
                else:
                    control = derive_sprite_control(fields, line_no)
                size = 8 + 4 * len(build_sprite_config_payload(control, fields, line_no))
        elif directive == "global_visual_state":
            fields = parse_key_values(parts[1:], line_no, "global_visual_state")
            _arg, words = build_global_visual_state_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive in (
            "primitive_helper_byte",
            "window_message_mode",
        ):
            size = 4 * (1 + len(parse_optional_word_list(parse_key_values(parts[1:], line_no, directive))))
        elif directive == "primitive_move_sprtg":
            fields = parse_key_values(parts[1:], line_no, "primitive_move_sprtg")
            _arg, words = build_primitive_move_sprtg_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "window_message":
            fields = parse_key_values(parts[1:], line_no, "window_message")
            _arg, words = build_window_message_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "primitive_anim_slot":
            fields = parse_key_values(parts[1:], line_no, "primitive_anim_slot")
            _arg, words = build_primitive_anim_slot_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "talk_bustup_display":
            fields = parse_key_values(parts[1:], line_no, "talk_bustup_display")
            _arg, words = build_talk_bustup_display_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "primitive_play_paf":
            fields = parse_key_values(parts[1:], line_no, "primitive_play_paf")
            _arg, words = build_primitive_play_paf_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "primitive_stop_paf":
            fields = parse_key_values(parts[1:], line_no, "primitive_stop_paf")
            _arg, words = build_primitive_stop_paf_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive == "text_output":
            fields = parse_key_values(parts[1:], line_no, "text_output")
            if "text" in fields:
                text_value = parse_source_string(fields["text"], line_no, "text_output text")
                size = 4 * (1 + len(sjis_text_to_words(text_value)))
            elif "event_value" in fields or "width" in fields or "raw_high" in fields:
                size = 8
            elif "clear_id" in fields:
                size = 4
            else:
                size = 4 * (1 + len(parse_optional_word_list(fields)))
        elif directive == "set_bgm_volume":
            fields = parse_key_values(parts[1:], line_no, "set_bgm_volume")
            size = 8 if "volume" in fields else 4 * (1 + len(parse_optional_word_list(fields)))
        elif directive in (
            "bgm_control",
            "text_message_layout",
            "camera_select",
            "set_radiata_time",
            "load_script_file",
            "load_background",
            "load_texture",
            "load_paf",
            "primitive_priority",
        ):
            size = 8 if directive != "text_message_layout" else 16
        elif directive in ("play_bgm", "stop_movie", "camera_flags", "setting_map", "delete_background", "vibration_stop"):
            size = 4
        elif directive == "time_schedule_value":
            fields = parse_key_values(parts[1:], line_no, "time_schedule_value")
            _arg, words = build_time_schedule_value_words(fields, line_no)
            size = 4 * (1 + len(words))
        elif directive in NEW_FORM_BUILDERS:
            fields = parse_key_values(parts[1:], line_no, directive)
            _arg, words = NEW_FORM_BUILDERS[directive][1](fields, line_no)
            size = 4 * (1 + len(words))
        elif directive in SOURCE_OPCODE_ALIASES:
            size = 4 * (1 + len(parse_optional_word_list(parse_key_values(parts[1:], line_no, directive))))
        elif directive == "end_script":
            size = 4 * (1 + len(parse_optional_word_list(parse_key_values(parts[1:], line_no, "end_script"))))
        elif directive == "return_zero":
            size = 4
        elif directive == "nop":
            size = 4
        elif directive == "nop_2e":
            size = 4
        elif directive == "set_flags":
            size = 8
        elif directive == "conditional_end":
            fields = parse_key_values(parts[1:], line_no, "conditional_end")
            condition = resolve_packed_word(
                fields, line_no, "conditional_end", "condition", CONDITION_SPECS, required=False
            )
            if "condition_args" in fields:
                size = 4 * (1 + len(parse_optional_word_list(fields, "condition_args")))
            else:
                size = 12 if condition else 4
        elif directive == "jump":
            size = 8
        elif directive in BRANCH_FRIENDLY_HEADS:
            size = 16
        elif directive == "branch":
            fields = parse_key_values(parts[1:], line_no, "branch")
            condition = resolve_packed_word(
                fields, line_no, "branch", "condition", CONDITION_SPECS, required=False
            )
            if "condition_args" in fields:
                size = 4 * (2 + len(parse_optional_word_list(fields, "condition_args")))
            else:
                size = 16 if condition else 8
        else:
            raise ValueError(f"line {line_no}: unknown source directive {parts[0]}")

        source_items.append({"line_no": line_no, "offset": current, "parts": parts, "directive": directive})
        current += size

    if current < 0x0C:
        raise ValueError("compiled source ended before EVD command area")
    output = bytearray(EVD_MAGIC + header_value.to_bytes(4, "little") + header_extra.to_bytes(4, "little"))
    if len(output) < current:
        output.extend(b"\x00" * (current - len(output)))

    for item in source_items:
        line_no = int(item["line_no"])
        offset = int(item["offset"])
        parts = list(item["parts"])
        directive = str(item["directive"])
        data: bytes

        if directive in RAW_ESCAPE_FORMS:
            raw_fields = parse_key_values(parts[1:], line_no, directive)
            if "words" in raw_fields and RAW_ESCAPE_FORMS[directive][1] not in raw_fields:
                words = parse_optional_word_list(raw_fields)
                arg = parse_hex_int(raw_fields.get("arg", "0"))
                flags = parse_hex_int(raw_fields.get("flags", "0x80"))
                header = build_command_header(RAW_ESCAPE_FORMS[directive][0], len(words), arg, flags)
                data = header.to_bytes(4, "little") + pack_u32_words(words)
                output[offset:offset + len(data)] = data
                continue
        if directive == ".bytes":
            data = bytes(parse_hex_byte(part) for part in parts[1:])
        elif directive == ".word":
            data = pack_u32_words([parse_hex_int(part) for part in parts[1:]])
        elif directive == "special_f0":
            fields = parse_key_values(parts[1:], line_no, "special_f0")
            data = build_special_f0_word(fields, line_no).to_bytes(4, "little")
        elif directive in ("anim_frame_trigger", "set_schedule_percent", "anim_script_end", "marker"):
            fields = parse_key_values(parts[1:], line_no, directive)
            data = build_special_f0_friendly(directive, fields, line_no).to_bytes(4, "little")
        elif directive == ".marker_table":
            marker_labels = parts[1:]
            if len(marker_labels) > 0xFFFF:
                raise ValueError(f"line {line_no}: .marker_table supports at most 65535 entries")
            data = len(marker_labels).to_bytes(2, "little") + b"\x00\x00"
            marker_words: list[int] = []
            for label in marker_labels:
                target = parse_source_target(label, labels, line_no)
                if target % 4:
                    raise ValueError(f"line {line_no}: .marker_table target {label} is not 4-byte aligned")
                marker_words.append(target // 4)
            data += pack_u32_words(marker_words)
        elif directive == ".cmd":
            fields = parse_key_values(parts[1:], line_no, ".cmd")
            opcode = parse_hex_int(fields["op"])
            words = parse_optional_word_list(fields)
            arg = parse_hex_int(fields.get("arg", "0"))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(opcode, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "trigger":
            fields = parse_key_values(parts[1:], line_no, "trigger")
            require_fields(fields, {"action", "type", "trigger_flags"}, line_no, "trigger")
            action = parse_hex_int(fields["action"])
            words = build_trigger_words(fields, line_no)
            default_arg = action << 6
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= action <= 3 or (arg >> 6) != action:
                raise ValueError(f"line {line_no}: trigger arg does not match action")
            header = build_command_header(0x03, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "expr":
            fields = parse_key_values(parts[1:], line_no, "expr")
            if "lhs" not in fields and any(key in fields for key in ("flag", "event_value", "property", "system_param")):
                friendly_words = build_expr_friendly_words(fields, line_no)
                arg = parse_hex_int(fields.get("arg", "0"))
                flags = parse_hex_int(fields.get("flags", "0x80"))
                header = build_command_header(0x14, 3, arg, flags)
                data = header.to_bytes(4, "little") + pack_u32_words(friendly_words)
                output[offset:offset + len(data)] = data
                continue
            require_fields(fields, {"lhs", "rhs"}, line_no, "expr")
            if "op" not in fields and "op_name" in fields:
                fields["op"] = str(
                    resolve_named_choice(fields, line_no, "expr", "op", "op_name", EXPR_OPERATION_NAMES)
                )
            control = resolve_packed_word(
                fields,
                line_no,
                "expr",
                "control",
                (("op", 0, 0x07), ("control_mid", 4, 0x0F), ("store_type", 8, 0x0F)),
            )
            op = resolve_named_choice(
                fields, line_no, "expr", "op", "op_name", EXPR_OPERATION_NAMES,
                required=False, default=control & 0x07,
            )
            lhs = resolve_packed_word(
                fields, line_no, "expr", "lhs", (("lhs_type", 24, 0x0F), ("lhs_tag", 24, 0xFF))
            )
            rhs = resolve_packed_word(
                fields, line_no, "expr", "rhs", (("rhs_type", 24, 0x0F), ("rhs_tag", 24, 0xFF))
            )
            arg = parse_hex_int(fields.get("arg", "0"))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if op != (control & 0x07):
                raise ValueError(f"line {line_no}: expr op does not match control low bits")
            header = build_command_header(0x14, 3, arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words([control, lhs, rhs])
        elif directive == "script_start_stack":
            fields = parse_key_values(parts[1:], line_no, "script_start_stack")
            arg, words = build_script_start_stack_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x01, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "script_start":
            fields = parse_key_values(parts[1:], line_no, "script_start")
            arg, words = build_script_start_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x04, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "change_game_mode":
            fields = parse_key_values(parts[1:], line_no, "change_game_mode")
            require_fields(fields, {"mode"}, line_no, "change_game_mode")
            mode = parse_hex_int(fields["mode"])
            words = parse_optional_word_list(fields)
            arg = parse_hex_int(fields.get("arg", str(mode)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= mode <= 0x3F or (arg & 0x3F) != mode:
                raise ValueError(f"line {line_no}: change_game_mode arg does not match mode")
            header = build_command_header(0x0A, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "script_stop":
            fields = parse_key_values(parts[1:], line_no, "script_stop")
            mode, words = build_script_stop_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x0B, len(words), mode, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "scene_save_env":
            fields = parse_key_values(parts[1:], line_no, "scene_save_env")
            arg, words = build_scene_save_env_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x11, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "time_schedule_value":
            fields = parse_key_values(parts[1:], line_no, "time_schedule_value")
            arg, words = build_time_schedule_value_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x12, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "radiata_time_enable":
            fields = parse_key_values(parts[1:], line_no, "radiata_time_enable")
            require_fields(fields, {"mode", "update_bit", "time_enable"}, line_no, "radiata_time_enable")
            mode = parse_hex_int(fields["mode"])
            update_bit = parse_hex_int(fields["update_bit"])
            time_enable = parse_hex_int(fields["time_enable"])
            words = parse_optional_word_list(fields)
            default_arg = mode | (update_bit << 5) | (time_enable << 6)
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= mode <= 0x03 or update_bit not in (0, 1) or not 0 <= time_enable <= 0x03:
                raise ValueError(f"line {line_no}: radiata_time_enable fields out of range")
            if (arg & 0x03) != mode or ((arg >> 5) & 0x01) != update_bit or ((arg >> 6) & 0x03) != time_enable:
                raise ValueError(f"line {line_no}: radiata_time_enable arg does not match fields")
            header = build_command_header(0x13, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "script_defaults":
            fields = parse_key_values(parts[1:], line_no, "script_defaults")
            arg, words = build_script_defaults_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x15, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "battle_acquisition_setup":
            fields = parse_key_values(parts[1:], line_no, "battle_acquisition_setup")
            arg, words = build_battle_acquisition_setup_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x16, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "battle_character_entry":
            fields = parse_key_values(parts[1:], line_no, "battle_character_entry")
            arg, words = build_battle_character_entry_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x17, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "party_member":
            fields = parse_key_values(parts[1:], line_no, "party_member")
            require_fields(fields, {"mode", "flag7", "character", "raw_high"}, line_no, "party_member")
            mode = parse_hex_int(fields["mode"])
            flag7 = parse_hex_int(fields["flag7"])
            character = parse_hex_int(fields["character"])
            raw_high = parse_hex_int(fields["raw_high"])
            default_arg = mode | (flag7 << 7)
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= mode <= 0x03 or flag7 not in (0, 1) or (arg & 0x03) != mode or ((arg >> 7) & 0x01) != flag7:
                raise ValueError(f"line {line_no}: party_member arg does not match mode/flag7")
            if not 0 <= character <= 0xFFFF or not 0 <= raw_high <= 0xFFFF:
                raise ValueError(f"line {line_no}: party_member character/raw_high out of range")
            word = character | (raw_high << 16)
            header = build_command_header(0x18, 1, arg, flags)
            data = header.to_bytes(4, "little") + word.to_bytes(4, "little")
        elif directive == "personal_inventory":
            fields = parse_key_values(parts[1:], line_no, "personal_inventory")
            require_fields(fields, {"explicit_char", "mode", "flag5", "event40", "event80"}, line_no, "personal_inventory")
            explicit_char = parse_hex_int(fields["explicit_char"])
            mode = parse_hex_int(fields["mode"])
            flag5 = parse_hex_int(fields["flag5"])
            event40 = parse_hex_int(fields["event40"])
            event80 = parse_hex_int(fields["event80"])
            if "words" in fields:
                words = parse_optional_word_list(fields)
            else:
                words = build_personal_inventory_words(fields, line_no)
            default_arg = explicit_char | (mode << 1) | (flag5 << 5) | (event40 << 6) | (event80 << 7)
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if explicit_char not in (0, 1) or not 0 <= mode <= 0x03 or any(value not in (0, 1) for value in (flag5, event40, event80)):
                raise ValueError(f"line {line_no}: personal_inventory fields out of range")
            if (arg & 0xE7) != default_arg:
                raise ValueError(f"line {line_no}: personal_inventory arg does not match fields")
            header = build_command_header(0x19, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_equipment":
            fields = parse_key_values(parts[1:], line_no, "character_equipment")
            arg, words = build_character_equipment_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x1A, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_data":
            fields = parse_key_values(parts[1:], line_no, "character_data")
            arg, words = build_character_data_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x20, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_delete_data":
            fields = parse_key_values(parts[1:], line_no, "character_delete_data")
            arg, words = build_character_delete_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x21, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_attach_render":
            fields = parse_key_values(parts[1:], line_no, "character_attach_render")
            # Semantic spellings map onto the raw flagN arg bits; explicit_char
            # defaults from character= presence.
            explicit_char = parse_hex_int(fields.get("explicit_char", "1" if "character" in fields else "0"))
            flag1 = parse_hex_int(fields.get("flag1", fields.get("sub_manager_flag", "0")))
            flag2 = parse_hex_int(fields.get("flag2", fields.get("no_render_bit", "0")))
            flag6 = parse_hex_int(fields.get("flag6", fields.get("no_render_with_child", "0")))
            flag7 = parse_hex_int(fields.get("flag7", fields.get("no_render", "0")))
            if "words" in fields:
                words = parse_optional_word_list(fields)
            elif explicit_char:
                require_fields(fields, {"character"}, line_no, "character_attach_render")
                character = parse_hex_int(fields["character"])
                if not 0 <= character <= 0xFFFFFFFF:
                    raise ValueError(f"line {line_no}: character_attach_render character out of range")
                words = [character]
            else:
                words = []
            default_arg = explicit_char | (flag1 << 1) | (flag2 << 2) | (flag6 << 6) | (flag7 << 7)
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if any(value not in (0, 1) for value in (explicit_char, flag1, flag2, flag6, flag7)) or (arg & 0xC7) != default_arg:
                raise ValueError(f"line {line_no}: character_attach_render arg does not match fields")
            header = build_command_header(0x22, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_attribute":
            fields = parse_key_values(parts[1:], line_no, "character_attribute")
            require_fields(fields, {"explicit_char"}, line_no, "character_attribute")
            explicit_char = parse_hex_int(fields["explicit_char"])
            if "words" in fields:
                words = parse_optional_word_list(fields)
            else:
                require_fields(fields, {"collision_attr", "collision_value"}, line_no, "character_attribute")
                words = []
                if explicit_char:
                    require_fields(fields, {"character"}, line_no, "character_attribute")
                    character = parse_hex_int(fields["character"])
                    if not 0 <= character <= 0xFFFFFFFF:
                        raise ValueError(f"line {line_no}: character_attribute character out of range")
                    words.append(character)
                collision_attr = parse_hex_int(fields["collision_attr"])
                collision_value = parse_hex_int(fields["collision_value"])
                if not 0 <= collision_attr <= 0xFFFFFFFF or not 0 <= collision_value <= 0xFFFFFFFF:
                    raise ValueError(f"line {line_no}: character_attribute collision fields out of range")
                words.extend([collision_attr, collision_value])
            arg = parse_hex_int(fields.get("arg", str(explicit_char)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if "set_attr2" in fields and parse_hex_int(fields["set_attr2"]) != (0 if (arg & 0x02) else 1):
                raise ValueError(f"line {line_no}: character_attribute set_attr2 does not match arg bit1")
            if explicit_char not in (0, 1) or (arg & 0x01) != explicit_char:
                raise ValueError(f"line {line_no}: character_attribute arg does not match explicit_char")
            header = build_command_header(0x2F, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_expression":
            fields = parse_key_values(parts[1:], line_no, "character_expression")
            require_fields(fields, {"explicit_char"}, line_no, "character_expression")
            explicit_char = parse_hex_int(fields["explicit_char"])
            if explicit_char not in (0, 1):
                raise ValueError(f"line {line_no}: character_expression explicit_char out of range")
            if "words" in fields:
                words = parse_optional_word_list(fields)
            else:
                require_fields(fields, {"expression", "blink", "mouth"}, line_no, "character_expression")
                words = []
                if explicit_char:
                    require_fields(fields, {"character"}, line_no, "character_expression")
                    character = parse_hex_int(fields["character"])
                    if not 0 <= character <= 0xFFFFFFFF:
                        raise ValueError(f"line {line_no}: character_expression character out of range")
                    words.append(character)
                # blink_interval is the proven name; blink_half_steps is the
                # legacy spelling. Optional bytes default to 0.
                interval_text = fields.get("blink_interval", fields.get("blink_half_steps", "0"))
                byte_fields = {
                    "expression": parse_hex_int(fields["expression"]),
                    "blink": parse_hex_int(fields["blink"]),
                    "mouth": parse_hex_int(fields["mouth"]),
                    "mouth_arg0": parse_hex_int(
                        fields.get("mouth_arg0", "0xFF" if parse_hex_int(fields["mouth"]) == 0xFF else "0")
                    ),
                    "mouth_arg1": parse_hex_int(
                        fields.get("mouth_arg1", "0xFF" if parse_hex_int(fields["mouth"]) == 0xFF else "0")
                    ),
                    "blink_interval": parse_hex_int(interval_text),
                    "raw0_high": parse_hex_int(fields.get("raw0_high", "0")),
                    "raw1_high": parse_hex_int(fields.get("raw1_high", "0")),
                }
                if any(not 0 <= value <= 0xFF for value in byte_fields.values()):
                    raise ValueError(f"line {line_no}: character_expression byte fields out of range")
                word0 = (
                    byte_fields["expression"]
                    | (byte_fields["blink"] << 8)
                    | (byte_fields["mouth"] << 16)
                    | (byte_fields["raw0_high"] << 24)
                )
                word1 = (
                    byte_fields["mouth_arg0"]
                    | (byte_fields["mouth_arg1"] << 8)
                    | (byte_fields["blink_interval"] << 16)
                    | (byte_fields["raw1_high"] << 24)
                )
                words.extend([word0, word1])
            arg = parse_hex_int(fields.get("arg", str(explicit_char)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if (arg & 0x01) != explicit_char:
                raise ValueError(f"line {line_no}: character_expression arg does not match explicit_char")
            header = build_command_header(0x34, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "strong_motion_blend":
            fields = parse_key_values(parts[1:], line_no, "strong_motion_blend")
            require_fields(fields, {"explicit_char"}, line_no, "strong_motion_blend")
            explicit_char = parse_hex_int(fields["explicit_char"])
            if explicit_char not in (0, 1):
                raise ValueError(f"line {line_no}: strong_motion_blend explicit_char out of range")
            if "words" in fields:
                words = parse_optional_word_list(fields)
            else:
                words = []
                if explicit_char:
                    require_fields(fields, {"character"}, line_no, "strong_motion_blend")
                    character = parse_hex_int(fields["character"])
                    if not 0 <= character <= 0xFFFFFFFF:
                        raise ValueError(f"line {line_no}: strong_motion_blend character out of range")
                    words.append(character)
                if "blend_word" in fields:
                    blend_word = parse_hex_int(fields["blend_word"])
                else:
                    require_fields(fields, {"blend"}, line_no, "strong_motion_blend")
                    blend_word = f32_to_u32(float(fields["blend"]))
                if not 0 <= blend_word <= 0xFFFFFFFF:
                    raise ValueError(f"line {line_no}: strong_motion_blend blend_word out of range")
                words.append(blend_word)
            arg = parse_hex_int(fields.get("arg", str(explicit_char)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if (arg & 0x01) != explicit_char:
                raise ValueError(f"line {line_no}: strong_motion_blend arg does not match explicit_char")
            header = build_command_header(0x3B, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_precreate_anim":
            fields = parse_key_values(parts[1:], line_no, "character_precreate_anim")
            require_fields(fields, {"explicit_char"}, line_no, "character_precreate_anim")
            explicit_char = parse_hex_int(fields["explicit_char"])
            if explicit_char not in (0, 1):
                raise ValueError(f"line {line_no}: character_precreate_anim explicit_char out of range")
            if "words" in fields:
                words = parse_optional_word_list(fields)
            else:
                require_fields(fields, {"anim_id"}, line_no, "character_precreate_anim")
                words = []
                if explicit_char:
                    require_fields(fields, {"character"}, line_no, "character_precreate_anim")
                    character = parse_hex_int(fields["character"])
                    if not 0 <= character <= 0xFFFFFFFF:
                        raise ValueError(f"line {line_no}: character_precreate_anim character out of range")
                    words.append(character)
                anim_id = parse_hex_int(fields["anim_id"])
                raw_high = parse_hex_int(fields.get("raw_high", "0"))
                if not 0 <= anim_id <= 0xFF or not 0 <= raw_high <= 0xFFFFFF:
                    raise ValueError(f"line {line_no}: character_precreate_anim fields out of range")
                words.append(anim_id | (raw_high << 8))
            mode = parse_hex_int(fields.get("mode", "0"))
            arg = parse_hex_int(fields.get("arg", str(explicit_char | (mode << 6))))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= mode <= 0x03 or (arg & 0x01) != explicit_char or (arg >> 6) != mode:
                raise ValueError(f"line {line_no}: character_precreate_anim arg does not match explicit_char/mode")
            header = build_command_header(0x2B, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "person_schedule_list":
            fields = parse_key_values(parts[1:], line_no, "person_schedule_list")
            require_fields(fields, {"explicit_char"}, line_no, "person_schedule_list")
            explicit_char = parse_hex_int(fields["explicit_char"])
            if explicit_char not in (0, 1):
                raise ValueError(f"line {line_no}: person_schedule_list explicit_char out of range")
            if "words" in fields:
                words = parse_optional_word_list(fields)
            else:
                # `schedule_list` is the proven name (the word's low16 goes to
                # CCharacterPersonManager::SetScheduleListNumber); the legacy
                # `schedule_low16` spelling stays accepted.
                schedule_text = fields.get("schedule_list", fields.get("schedule_low16"))
                if schedule_text is None:
                    require_fields(fields, {"schedule_list"}, line_no, "person_schedule_list")
                words = []
                if explicit_char:
                    require_fields(fields, {"character"}, line_no, "person_schedule_list")
                    character = parse_hex_int(fields["character"])
                    if not 0 <= character <= 0xFFFFFFFF:
                        raise ValueError(f"line {line_no}: person_schedule_list character out of range")
                    words.append(character)
                schedule_low16 = parse_hex_int(schedule_text)
                schedule_arg_byte = parse_hex_int(fields.get("schedule_arg_byte", "0"))
                raw_high = parse_hex_int(fields.get("raw_high", "0"))
                if not 0 <= schedule_low16 <= 0xFFFF or not 0 <= schedule_arg_byte <= 0xFF or not 0 <= raw_high <= 0xFF:
                    raise ValueError(f"line {line_no}: person_schedule_list fields out of range")
                words.append(schedule_low16 | (schedule_arg_byte << 16) | (raw_high << 24))
            arg = parse_hex_int(fields.get("arg", str(explicit_char)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if (arg & 0x01) != explicit_char:
                raise ValueError(f"line {line_no}: person_schedule_list arg does not match explicit_char")
            header = build_command_header(0xC0, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_animation":
            fields = parse_key_values(parts[1:], line_no, "character_animation")
            explicit_char = parse_hex_int(fields.get("explicit_char", "1" if "character" in fields else "0"))
            if explicit_char not in (0, 1):
                raise ValueError(f"line {line_no}: character_animation explicit_char must be 0 or 1")
            if "words" in fields:
                words = parse_optional_word_list(fields)
            else:
                words = build_character_animation_words(fields, line_no)
            default_arg = (
                explicit_char
                | (parse_hex_int(fields.get("speed0", "1" if "optional_float0" in fields else "0")) << 1)
                | (parse_hex_int(fields.get("speed1", "1" if "optional_float1" in fields else "0")) << 2)
                | (parse_hex_int(fields.get("blend", "1" if "optional_float2" in fields else "0")) << 3)
                | (parse_hex_int(fields.get("speed2", "1" if "play_speed" in fields else "0")) << 4)
                | (parse_hex_int(fields.get("extra_word", "1" if "extra_anim_word" in fields else "0")) << 5)
                | (parse_hex_int(fields.get("sub_anim_mode", "0")) << 6)
            )
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if (arg & 0x01) != explicit_char:
                raise ValueError(f"line {line_no}: character_animation arg does not match explicit_char")
            if "words" not in fields and (arg & 0xFF) != default_arg:
                raise ValueError(f"line {line_no}: character_animation arg does not match option fields")
            control_index = 1 if explicit_char else 0
            if "anim_group" in fields:
                if control_index >= len(words):
                    raise ValueError(f"line {line_no}: character_animation anim_group field has no matching word")
                anim_group = parse_hex_int(fields["anim_group"])
                if words[control_index] != anim_group:
                    raise ValueError(f"line {line_no}: character_animation anim_group does not match words payload")
            if "anim_word" in fields:
                if control_index + 1 >= len(words):
                    raise ValueError(f"line {line_no}: character_animation anim_word field has no matching word")
                anim_word = parse_hex_int(fields["anim_word"])
                if "animation_variant" in fields and anim_word >> 24 == 0:
                    anim_word |= (parse_hex_int(fields["animation_variant"]) & 0xFF) << 24
                if words[control_index + 1] != anim_word:
                    raise ValueError(f"line {line_no}: character_animation anim_word does not match words payload")
            header = build_command_header(0x23, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_move_points":
            fields = parse_key_values(parts[1:], line_no, "character_move_points")
            require_fields(fields, {"explicit_char", "buffer_mode"}, line_no, "character_move_points")
            explicit_char = parse_hex_int(fields["explicit_char"])
            buffer_mode = parse_hex_int(fields["buffer_mode"])
            if "words" in fields:
                words = parse_optional_word_list(fields)
            else:
                words = build_character_move_points_words(fields, line_no)
            default_arg = explicit_char | (buffer_mode << 1)
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if explicit_char not in (0, 1) or not 0 <= buffer_mode <= 0x0F or (arg & 0x1F) != default_arg:
                raise ValueError(f"line {line_no}: character_move_points arg does not match explicit_char/buffer_mode")
            if ("words" not in fields) and (len(words) < explicit_char or (len(words) - explicit_char) % 3):
                # Raw words= lists (data regions) keep their real length.
                raise ValueError(f"line {line_no}: character_move_points word count must be explicit_char + 3 * point_count")
            header = build_command_header(0x28, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_collision_setup":
            fields = parse_key_values(parts[1:], line_no, "character_collision_setup")
            arg, words = build_character_collision_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x31, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_anim_signal":
            fields = parse_key_values(parts[1:], line_no, "character_anim_signal")
            arg, words = build_character_anim_signal_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x36, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_sub_anim":
            fields = parse_key_values(parts[1:], line_no, "character_sub_anim")
            arg, words = build_character_sub_anim_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x25, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "map_change_check":
            fields = parse_key_values(parts[1:], line_no, "map_change_check")
            arg, flags, words = build_map_change_check_words(fields, line_no)
            header = build_command_header(0xC1, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_virtual_24":
            fields = parse_key_values(parts[1:], line_no, "character_virtual_24")
            arg, words = build_character_virtual_24_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x24, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "person_field_update":
            fields = parse_key_values(parts[1:], line_no, "person_field_update")
            arg, words = build_person_field_update_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0xC4, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_expression":
            opcode = {
                "character_expression": 0x34,
            }[directive]
            fields = parse_key_values(parts[1:], line_no, directive)
            require_fields(fields, {"explicit_char"}, line_no, directive)
            explicit_char = parse_hex_int(fields["explicit_char"])
            words = parse_optional_word_list(fields)
            arg = parse_hex_int(fields.get("arg", str(explicit_char)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if explicit_char not in (0, 1) or (arg & 0x01) != explicit_char:
                raise ValueError(f"line {line_no}: {directive} arg does not match explicit_char")
            header = build_command_header(opcode, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_attach_parent":
            fields = parse_key_values(parts[1:], line_no, "character_attach_parent")
            arg, words = build_character_attach_parent_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x26, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_detach_parent":
            fields = parse_key_values(parts[1:], line_no, "character_detach_parent")
            require_fields(fields, {"explicit_char", "flag4", "flag6", "flag7"}, line_no, "character_detach_parent")
            explicit_char = parse_hex_int(fields["explicit_char"])
            flag4 = parse_hex_int(fields["flag4"])
            flag6 = parse_hex_int(fields["flag6"])
            flag7 = parse_hex_int(fields["flag7"])
            if "words" in fields:
                words = parse_optional_word_list(fields)
            elif explicit_char:
                require_fields(fields, {"character"}, line_no, "character_detach_parent")
                character = parse_hex_int(fields["character"])
                if not 0 <= character <= 0xFFFFFFFF:
                    raise ValueError(f"line {line_no}: character_detach_parent character out of range")
                words = [character]
            else:
                words = []
            default_arg = explicit_char | (flag4 << 4) | (flag6 << 6) | (flag7 << 7)
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if any(value not in (0, 1) for value in (explicit_char, flag4, flag6, flag7)) or (arg & 0xD1) != default_arg:
                raise ValueError(f"line {line_no}: character_detach_parent arg does not match fields")
            header = build_command_header(0x27, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_move_pause":
            fields = parse_key_values(parts[1:], line_no, "character_move_pause")
            require_fields(fields, {"explicit_char"}, line_no, "character_move_pause")
            explicit_char = parse_hex_int(fields["explicit_char"])
            pause_text = fields.get("pause_mode", fields.get("pause_arg"))
            if pause_text is None:
                require_fields(fields, {"pause_mode"}, line_no, "character_move_pause")
            pause_arg = parse_hex_int(pause_text)
            words = build_character_move_pause_words(fields, line_no)
            default_arg = explicit_char | (pause_arg << 1)
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if explicit_char not in (0, 1) or not 0 <= pause_arg <= 0x03 or (arg & 0x07) != default_arg:
                raise ValueError(f"line {line_no}: character_move_pause arg does not match fields")
            header = build_command_header(0x29, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_movement":
            fields = parse_key_values(parts[1:], line_no, "character_movement")
            arg, words = build_character_movement_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x2A, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_rotate_option":
            fields = parse_key_values(parts[1:], line_no, "character_rotate_option")
            explicit_char = parse_hex_int(fields.get("explicit_char", "0"))
            if explicit_char not in (0, 1):
                raise ValueError(f"line {line_no}: character_rotate_option explicit_char must be 0 or 1")
            if "words" in fields:
                words = parse_optional_word_list(fields)
            else:
                words = build_rotate_option_words(fields, line_no)
            arg = parse_hex_int(fields.get("arg", str(explicit_char)))
            if (arg & 0x01) != explicit_char:
                raise ValueError(f"line {line_no}: character_rotate_option arg does not match explicit_char")
            control_index = 1 if explicit_char else 0
            if "target_char_from_stream" in fields and ((arg >> 1) & 0x01) != parse_hex_int(fields["target_char_from_stream"]):
                raise ValueError(f"line {line_no}: character_rotate_option arg does not match target_char_from_stream")
            if "name_source" in fields and ((arg >> 2) & 0x03) != parse_hex_int(fields["name_source"]):
                raise ValueError(f"line {line_no}: character_rotate_option arg does not match name_source")
            if "control" in fields:
                if control_index >= len(words):
                    raise ValueError(f"line {line_no}: character_rotate_option control field has no matching word")
                control = parse_hex_int(fields["control"])
                if words[control_index] != control:
                    raise ValueError(f"line {line_no}: character_rotate_option control does not match words payload")
            if "mode" in fields:
                if control_index >= len(words):
                    raise ValueError(f"line {line_no}: character_rotate_option mode field has no matching control word")
                mode = parse_hex_int(fields["mode"])
                if mode != (words[control_index] & 0x0F):
                    raise ValueError(f"line {line_no}: character_rotate_option mode does not match control word")
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x2D, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_move_position":
            fields = parse_key_values(parts[1:], line_no, "character_move_position")
            require_fields(fields, {"explicit_char", "mode", "coord", "source"}, line_no, "character_move_position")
            explicit_char = parse_hex_int(fields["explicit_char"])
            mode = parse_hex_int(fields["mode"])
            coord = parse_hex_int(fields["coord"])
            source = parse_hex_int(fields["source"])
            if "words" in fields:
                words = parse_optional_word_list(fields)
            else:
                words = build_character_move_position_words(fields, line_no)
            default_arg = explicit_char | (mode << 1) | (coord << 3) | (source << 5)
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if explicit_char not in (0, 1) or not 0 <= mode <= 0x03 or not 0 <= coord <= 0x03 or not 0 <= source <= 0x07 or arg != default_arg:
                raise ValueError(f"line {line_no}: character_move_position arg does not match fields")
            header = build_command_header(0x30, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_auto_rate_anim":
            fields = parse_key_values(parts[1:], line_no, "character_auto_rate_anim")
            require_fields(fields, {"explicit_char", "mode", "with_child", "event_duration"}, line_no, "character_auto_rate_anim")
            explicit_char = parse_hex_int(fields["explicit_char"])
            mode = parse_hex_int(fields["mode"])
            with_child = parse_hex_int(fields["with_child"])
            event_duration = parse_hex_int(fields["event_duration"])
            if "words" in fields:
                words = parse_optional_word_list(fields)
            else:
                words = build_auto_rate_words(fields, line_no)
            default_arg = explicit_char | (mode << 1) | (with_child << 3) | (event_duration << 4)
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if explicit_char not in (0, 1) or not 0 <= mode <= 0x03 or with_child not in (0, 1) or event_duration not in (0, 1) or (arg & 0x1F) != default_arg:
                raise ValueError(f"line {line_no}: character_auto_rate_anim arg does not match fields")
            control_index = 1 if explicit_char else 0
            if "control" in fields:
                if control_index >= len(words):
                    raise ValueError(f"line {line_no}: character_auto_rate_anim control field has no matching word")
                control = parse_hex_int(fields["control"])
                if words[control_index] != control:
                    raise ValueError(f"line {line_no}: character_auto_rate_anim control does not match words payload")
            header = build_command_header(0x32, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_eye_control":
            fields = parse_key_values(parts[1:], line_no, "character_eye_control")
            require_fields(fields, {"explicit_char", "eye_ball", "eye_move"}, line_no, "character_eye_control")
            explicit_char = parse_hex_int(fields["explicit_char"])
            eye_ball = parse_hex_int(fields["eye_ball"])
            eye_move = parse_hex_int(fields["eye_move"])
            if "words" in fields:
                words = parse_optional_word_list(fields)
            else:
                require_fields(fields, {"move_selector"}, line_no, "character_eye_control")
                words = []
                if explicit_char:
                    require_fields(fields, {"character"}, line_no, "character_eye_control")
                    character = parse_hex_int(fields["character"])
                    if not 0 <= character <= 0xFFFFFFFF:
                        raise ValueError(f"line {line_no}: character_eye_control character out of range")
                    words.append(character)
                eye_ball_byte = parse_hex_int(fields.get("eye_ball_byte", "0"))
                move_selector = parse_hex_int(fields["move_selector"])
                manual_x = parse_hex_int(fields.get("manual_x_s8", "0"))
                manual_y = parse_hex_int(fields.get("manual_y_s8", "0"))
                if not 0 <= eye_ball_byte <= 0xFF or not 0 <= move_selector <= 0xFF:
                    raise ValueError(f"line {line_no}: character_eye_control byte fields out of range")
                if not -128 <= manual_x <= 127 or not -128 <= manual_y <= 127:
                    raise ValueError(f"line {line_no}: character_eye_control manual vector fields out of signed-byte range")
                if "eye_ball_no" in fields and parse_hex_int(fields["eye_ball_no"]) != (eye_ball_byte & 0x03):
                    raise ValueError(f"line {line_no}: character_eye_control eye_ball_no does not match eye_ball_byte low bits")
                control = eye_ball_byte | (move_selector << 8) | ((manual_x & 0xFF) << 16) | ((manual_y & 0xFF) << 24)
                words.append(control)
                if "manual_time_word" in fields:
                    manual_time_word = parse_hex_int(fields["manual_time_word"])
                    if not 0 <= manual_time_word <= 0xFFFFFFFF:
                        raise ValueError(f"line {line_no}: character_eye_control manual_time_word out of range")
                    words.append(manual_time_word)
                elif "manual_time" in fields:
                    words.append(f32_to_u32(float(fields["manual_time"])))
            default_arg = explicit_char | (eye_ball << 1) | (eye_move << 2)
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if any(value not in (0, 1) for value in (explicit_char, eye_ball, eye_move)) or (arg & 0x07) != default_arg:
                raise ValueError(f"line {line_no}: character_eye_control arg does not match fields")
            header = build_command_header(0x33, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_single_manager":
            fields = parse_key_values(parts[1:], line_no, "character_single_manager")
            arg, words = build_character_single_manager_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x35, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "character_event_leave":
            fields = parse_key_values(parts[1:], line_no, "character_event_leave")
            arg, words = build_character_event_leave_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x39, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "special_effect":
            fields = parse_key_values(parts[1:], line_no, "special_effect")
            arg, words = build_special_effect_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0xD5, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "stand_context":
            fields = parse_key_values(parts[1:], line_no, "stand_context")
            arg, words = build_stand_context_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x1C, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "camera_mode":
            fields = parse_key_values(parts[1:], line_no, "camera_mode")
            arg, words = build_camera_mode_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x51, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "camera_transform_param":
            fields = parse_key_values(parts[1:], line_no, "camera_transform_param")
            require_fields(fields, {"mode", "flag2", "flag3", "flag4"}, line_no, "camera_transform_param")
            mode = parse_hex_int(fields["mode"])
            flag2 = parse_hex_int(fields["flag2"])
            flag3 = parse_hex_int(fields["flag3"])
            flag4 = parse_hex_int(fields["flag4"])
            if "words" in fields:
                words = parse_optional_word_list(fields)
            else:
                words = build_camera_transform_words(fields, line_no)
            default_arg = mode | (flag2 << 2) | (flag3 << 3) | (flag4 << 4)
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= mode <= 0x03 or any(value not in (0, 1) for value in (flag2, flag3, flag4)):
                raise ValueError(f"line {line_no}: camera_transform_param fields out of range")
            if (arg & 0x1F) != default_arg:
                raise ValueError(f"line {line_no}: camera_transform_param arg does not match fields")
            if "control" in fields:
                if mode != 2:
                    raise ValueError(f"line {line_no}: camera_transform_param control is only valid for mode 2")
                if not words:
                    raise ValueError(f"line {line_no}: camera_transform_param control field has no matching word")
                control = parse_hex_int(fields["control"])
                if words[0] != control:
                    raise ValueError(f"line {line_no}: camera_transform_param control does not match words payload")
            header = build_command_header(0x52, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "camera_color_anim":
            fields = parse_key_values(parts[1:], line_no, "camera_color_anim")
            require_fields(fields, {"mode", "low"}, line_no, "camera_color_anim")
            mode = parse_hex_int(fields["mode"])
            low = parse_hex_int(fields["low"])
            if "words" in fields:
                words = parse_optional_word_list(fields)
            else:
                words = build_camera_color_anim_words(fields, line_no)
            default_arg = (mode << 4) | low
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= mode <= 0x0F or not 0 <= low <= 0x0F or arg != default_arg:
                raise ValueError(f"line {line_no}: camera_color_anim arg does not match mode/low")
            if "has_first_float" in fields and (1 if low & 0x08 else 0) != parse_hex_int(fields["has_first_float"]):
                raise ValueError(f"line {line_no}: camera_color_anim low does not match has_first_float")
            if "has_start_color" in fields and (1 if low & 0x08 else 0) != parse_hex_int(fields["has_start_color"]):
                raise ValueError(f"line {line_no}: camera_color_anim low does not match has_start_color")
            if "blend_flag" in fields and ((low >> 1) & 0x01) != parse_hex_int(fields["blend_flag"]):
                raise ValueError(f"line {line_no}: camera_color_anim low does not match blend_flag")
            header = build_command_header(0x59, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "camera_move_existing":
            fields = parse_key_values(parts[1:], line_no, "camera_move_existing")
            arg, words = build_camera_move_existing_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x56, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "camera_capture_target":
            fields = parse_key_values(parts[1:], line_no, "camera_capture_target")
            arg, words = build_camera_capture_target_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x55, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "marker_seek":
            fields = parse_key_values(parts[1:], line_no, "marker_seek")
            arg, words = build_marker_seek_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x0D, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "camera_move_etc":
            fields = parse_key_values(parts[1:], line_no, "camera_move_etc")
            arg, words = build_camera_move_etc_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x54, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "fade_control":
            fields = parse_key_values(parts[1:], line_no, "fade_control")
            arg, words = build_fade_control_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x67, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "position_vibration_vector":
            fields = parse_key_values(parts[1:], line_no, "position_vibration_vector")
            arg, words = build_position_vibration_vector_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x57, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "position_vibration_param":
            fields = parse_key_values(parts[1:], line_no, "position_vibration_param")
            words = build_position_vibration_param_words(fields, line_no)
            arg = parse_hex_int(fields.get("arg", "0"))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x4A, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "position_vibration_clear":
            fields = parse_key_values(parts[1:], line_no, "position_vibration_clear")
            require_fields(fields, {"mode"}, line_no, "position_vibration_clear")
            mode = parse_hex_int(fields["mode"])
            words = parse_optional_word_list(fields)
            arg = parse_hex_int(fields.get("arg", str(mode)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= mode <= 0x0F or (arg & 0x0F) != mode:
                raise ValueError(f"line {line_no}: position_vibration_clear arg does not match mode")
            header = build_command_header(0x58, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "set_bgm":
            fields = parse_key_values(parts[1:], line_no, "set_bgm")
            arg = parse_hex_int(fields.get("arg", "0"))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if "words" in fields and "info0" not in fields:
                # Raw form (data regions, truncated commands).
                words = parse_optional_word_list(fields)
                header = build_command_header(0x70, len(words), arg, flags)
                data = header.to_bytes(4, "little") + pack_u32_words(words)
            else:
                require_fields(fields, {"info0", "info1"}, line_no, "set_bgm")
                info0 = parse_hex_int(fields["info0"])
                info1 = parse_hex_int(fields["info1"])
                header = build_command_header(0x70, 2, arg, flags)
                data = header.to_bytes(4, "little") + pack_u32_words([info0, info1])
        elif directive == "play_bgm":
            fields = parse_key_values(parts[1:], line_no, "play_bgm")
            require_fields(fields, {"mode"}, line_no, "play_bgm")
            mode = parse_hex_int(fields["mode"])
            arg = parse_hex_int(fields.get("arg", str(mode)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= mode <= 0x03 or (arg & 0x03) != mode:
                raise ValueError(f"line {line_no}: play_bgm mode must be 0..3")
            data = build_command_header(0x71, 0, arg, flags).to_bytes(4, "little")
        elif directive == "bgm_control":
            fields = parse_key_values(parts[1:], line_no, "bgm_control")
            require_fields(fields, {"mode", "pause", "value"}, line_no, "bgm_control")
            mode = parse_hex_int(fields["mode"])
            pause = parse_hex_int(fields["pause"])
            default_arg = mode | (pause << 7)
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            value = parse_hex_int(fields["value"])
            raw_high = parse_hex_int(fields.get("unused_high", fields.get("raw_high", "0")))
            if not 0 <= mode <= 0x03 or pause not in (0, 1) or (arg & 0x03) != mode or int(bool(arg & 0x80)) != pause:
                raise ValueError(f"line {line_no}: bgm_control mode/pause out of range")
            if not 0 <= value <= 0xFFFF or not 0 <= raw_high <= 0xFFFF:
                raise ValueError(f"line {line_no}: bgm_control value/raw_high out of range")
            header = build_command_header(0x72, 1, arg, flags)
            data = header.to_bytes(4, "little") + ((value | (raw_high << 16)).to_bytes(4, "little"))
        elif directive == "set_bgm_volume":
            fields = parse_key_values(parts[1:], line_no, "set_bgm_volume")
            if "volume" not in fields:
                # Truncated command (declared word count 0): rebuild raw.
                words = parse_optional_word_list(fields)
                arg = parse_hex_int(fields.get("arg", "0"))
                flags = parse_hex_int(fields.get("flags", "0x80"))
                header = build_command_header(0x73, len(words), arg, flags)
                data = header.to_bytes(4, "little") + pack_u32_words(words)
                output[offset:offset + len(data)] = data
                continue
            require_fields(fields, {"slot", "volume", "time"}, line_no, "set_bgm_volume")
            slot = parse_hex_int(fields["slot"])
            arg = parse_hex_int(fields.get("arg", str(slot)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            volume = parse_hex_int(fields["volume"])
            time = parse_hex_int(fields["time"])
            if not 0 <= slot <= 0x03 or (arg & 0x03) != slot or not 0 <= volume <= 0xFFFF or not 0 <= time <= 0xFFFF:
                raise ValueError(f"line {line_no}: set_bgm_volume fields out of range")
            header = build_command_header(0x73, 1, arg, flags)
            data = header.to_bytes(4, "little") + ((volume | (time << 16)).to_bytes(4, "little"))
        elif directive == "play_movie":
            fields = parse_key_values(parts[1:], line_no, "play_movie")
            if "words" in fields and "movie" not in fields:
                # Raw form (data regions, truncated commands).
                words = parse_optional_word_list(fields)
                arg = parse_hex_int(fields.get("arg", "0"))
                flags = parse_hex_int(fields.get("flags", "0x80"))
                header = build_command_header(0x7C, len(words), arg, flags)
                data = header.to_bytes(4, "little") + pack_u32_words(words)
                output[offset:offset + len(data)] = data
                continue
            require_fields(fields, {"movie", "param0", "param1", "extra"}, line_no, "play_movie")
            movie = parse_hex_int(fields["movie"])
            param0 = parse_hex_int(fields["param0"])
            param1 = parse_hex_int(fields["param1"])
            extra = parse_hex_int(fields["extra"])
            arg = parse_hex_int(fields.get("arg", "0"))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= movie <= 0xFFFF:
                raise ValueError(f"line {line_no}: movie id out of range")
            if not -(1 << 15) <= param0 < (1 << 15) or not -(1 << 15) <= param1 < (1 << 15):
                raise ValueError(f"line {line_no}: movie params must fit signed 16-bit")
            word1 = (param0 & 0xFFFF) | ((param1 & 0xFFFF) << 16)
            header = build_command_header(0x7C, 3, arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words([movie, word1, extra])
        elif directive == "stop_movie":
            fields = parse_key_values(parts[1:], line_no, "stop_movie")
            arg = parse_hex_int(fields.get("arg", "0"))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            data = build_command_header(0x7D, 0, arg, flags).to_bytes(4, "little")
        elif directive == "load_sound_resource":
            fields = parse_key_values(parts[1:], line_no, "load_sound_resource")
            arg, words = build_load_sound_resource_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x74, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "play_sound_effect":
            fields = parse_key_values(parts[1:], line_no, "play_sound_effect")
            require_fields(fields, {"mode"}, line_no, "play_sound_effect")
            mode = parse_hex_int(fields["mode"])
            if "words" in fields:
                words = parse_optional_word_list(fields)
            else:
                words = build_play_sound_effect_words(fields, line_no)
            submode = parse_hex_int(fields.get("submode", "0"))
            explicit_char = parse_hex_int(fields.get("explicit_char", str(parse_hex_int(fields.get("arg", "0")) & 0x01)))
            arg = parse_hex_int(fields.get("arg", str((mode << 6) | (submode << 1) | explicit_char)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= mode <= 0x03 or not 0 <= submode <= 0x03 or explicit_char not in (0, 1):
                raise ValueError(f"line {line_no}: play_sound_effect mode/submode/explicit_char out of range")
            if (arg >> 6) != mode or ((arg >> 1) & 0x03) != submode or (arg & 0x01) != explicit_char:
                raise ValueError(f"line {line_no}: play_sound_effect arg does not match mode/submode/explicit_char")
            header = build_command_header(0x75, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "stop_sound_effect":
            fields = parse_key_values(parts[1:], line_no, "stop_sound_effect")
            arg, words = build_stop_sound_effect_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x76, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "sound_listener":
            fields = parse_key_values(parts[1:], line_no, "sound_listener")
            arg, words = build_sound_listener_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x79, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "sound_effect_stack":
            fields = parse_key_values(parts[1:], line_no, "sound_effect_stack")
            arg = parse_hex_int(fields.get("arg", fields.get("mode", "0")))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            words = parse_optional_word_list(fields)
            if "mode" in fields:
                mode = parse_hex_int(fields["mode"])
                if mode not in (0, 1) or (arg & 0x01) != mode:
                    raise ValueError(f"line {line_no}: sound_effect_stack arg does not match mode")
            elif "words" not in fields:
                require_fields(fields, {"mode"}, line_no, "sound_effect_stack")
            header = build_command_header(0x7A, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "vibration_stop":
            fields = parse_key_values(parts[1:], line_no, "vibration_stop")
            arg = parse_hex_int(fields.get("arg", "0"))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            data = build_command_header(0x82, 0, arg, flags).to_bytes(4, "little")
        elif directive == "play_vibration":
            fields = parse_key_values(parts[1:], line_no, "play_vibration")
            if "words" in fields and "strength" not in fields:
                # Raw form (data regions, truncated commands).
                words = parse_optional_word_list(fields)
                arg = parse_hex_int(fields.get("arg", "0"))
                flags = parse_hex_int(fields.get("flags", "0x80"))
                header = build_command_header(0x83, len(words), arg, flags)
                data = header.to_bytes(4, "little") + pack_u32_words(words)
                output[offset:offset + len(data)] = data
                continue
            require_fields(fields, {"strength", "pattern", "duration"}, line_no, "play_vibration")
            strength = parse_hex_int(fields["strength"])
            pattern = parse_hex_int(fields["pattern"])
            duration = parse_hex_int(fields["duration"])
            arg = parse_hex_int(fields.get("arg", "0"))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= strength <= 0xFF or not 0 <= pattern <= 0xFF or not 0 <= duration <= 0xFFFF:
                raise ValueError(f"line {line_no}: play_vibration fields out of range")
            word = strength | (pattern << 8) | (duration << 16)
            header = build_command_header(0x83, 1, arg, flags)
            data = header.to_bytes(4, "little") + word.to_bytes(4, "little")
        elif directive == "camera_select":
            fields = parse_key_values(parts[1:], line_no, "camera_select")
            require_fields(fields, {"select_slot", "target_slot", "camera", "target"}, line_no, "camera_select")
            select_slot = parse_hex_int(fields["select_slot"])
            target_slot = parse_hex_int(fields["target_slot"])
            camera = parse_hex_int(fields["camera"])
            target = parse_hex_int(fields["target"])
            flags = parse_hex_int(fields.get("flags", "0x80"))
            default_arg = select_slot | (target_slot << 2)
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            if not 0 <= select_slot <= 0x03 or not 0 <= target_slot <= 0x03:
                raise ValueError(f"line {line_no}: camera_select slots out of range")
            if (arg & 0x03) != select_slot or ((arg >> 2) & 0x03) != target_slot:
                raise ValueError(f"line {line_no}: camera_select arg does not match slots")
            if not 0 <= camera <= 0xFFFF or not 0 <= target <= 0xFFFF:
                raise ValueError(f"line {line_no}: camera_select ids out of range")
            header = build_command_header(0x50, 1, arg, flags)
            data = header.to_bytes(4, "little") + ((camera | (target << 16)).to_bytes(4, "little"))
        elif directive == "camera_flags":
            fields = parse_key_values(parts[1:], line_no, "camera_flags")
            require_fields(fields, {"flag0", "flag1", "flag2"}, line_no, "camera_flags")
            flag0 = parse_hex_int(fields["flag0"])
            flag1 = parse_hex_int(fields["flag1"])
            flag2 = parse_hex_int(fields["flag2"])
            default_arg = flag0 | (flag1 << 1) | (flag2 << 2)
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if flag0 not in (0, 1) or flag1 not in (0, 1) or flag2 not in (0, 1):
                raise ValueError(f"line {line_no}: camera_flags values must be 0 or 1")
            if (arg & 0x07) != default_arg:
                raise ValueError(f"line {line_no}: camera_flags arg does not match flags")
            data = build_command_header(0x5A, 0, arg, flags).to_bytes(4, "little")
        elif directive == "text_message_layout":
            fields = parse_key_values(parts[1:], line_no, "text_message_layout")
            require_fields(fields, {"x", "y"}, line_no, "text_message_layout")
            x = parse_hex_int(fields["x"])
            y = parse_hex_int(fields["y"])
            # color/priority are the proven names; word00/word08 stay accepted.
            color_text = fields.get("color", fields.get("word00"))
            priority_text = fields.get("priority", fields.get("word08"))
            if color_text is None or priority_text is None:
                require_fields(fields, {"color", "priority"}, line_no, "text_message_layout")
            word00 = parse_hex_int(color_text)
            word08 = parse_hex_int(priority_text)
            arg = parse_hex_int(fields.get("arg", "0"))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not -(1 << 15) <= x < (1 << 15) or not -(1 << 15) <= y < (1 << 15):
                raise ValueError(f"line {line_no}: text_message_layout x/y must fit signed 16-bit")
            xy = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
            header = build_command_header(0x8E, 3, arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words([xy, word00, word08])
        elif directive == "set_radiata_time":
            fields = parse_key_values(parts[1:], line_no, "set_radiata_time")
            arg = parse_hex_int(fields.get("arg", "0"))
            flags = parse_hex_int(fields.get("flags", "0x80"))

            def clock_field(new_name: str, old_name: str, sentinel: int) -> int:
                text = fields.get(new_name, fields.get(old_name))
                if text is None:
                    require_fields(fields, {new_name}, line_no, "set_radiata_time")
                return sentinel if text == "keep" else parse_hex_int(text)

            byte0 = clock_field("minute", "byte0", 0xFF)
            byte1 = clock_field("hour", "byte1", 0xFF)
            day_text = fields.get("day", fields.get("value"))
            if day_text is None:
                require_fields(fields, {"day"}, line_no, "set_radiata_time")
            value = 0xFFFF if day_text == "resync" else parse_hex_int(day_text)
            if not 0 <= byte0 <= 0xFF or not 0 <= byte1 <= 0xFF or not 0 <= value <= 0xFFFF:
                raise ValueError(f"line {line_no}: set_radiata_time fields out of range")
            word = byte0 | (byte1 << 8) | (value << 16)
            header = build_command_header(0xC5, 1, arg, flags)
            data = header.to_bytes(4, "little") + word.to_bytes(4, "little")
        elif directive == "load_script_file":
            fields = parse_key_values(parts[1:], line_no, "load_script_file")
            require_fields(fields, {"force_high_bit", "file"}, line_no, "load_script_file")
            force_high_bit = parse_hex_int(fields["force_high_bit"])
            file_id = parse_hex_int(fields["file"])
            # The engine discards these; they exist so an odd file round-trips.
            raw_bit15 = parse_hex_int(fields.get("unused_bit15", fields.get("raw_bit15", "0")))
            raw_high = parse_hex_int(fields.get("unused_high", fields.get("raw_high", "0")))
            arg = parse_hex_int(fields.get("arg", str(force_high_bit)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if force_high_bit not in (0, 1) or (arg & 0x01) != force_high_bit:
                raise ValueError(f"line {line_no}: load_script_file arg does not match force_high_bit")
            if not 0 <= file_id <= 0x7FFF or raw_bit15 not in (0, 1) or not 0 <= raw_high <= 0xFFFF:
                raise ValueError(f"line {line_no}: load_script_file fields out of range")
            word = file_id | (raw_bit15 << 15) | (raw_high << 16)
            header = build_command_header(0x06, 1, arg, flags)
            data = header.to_bytes(4, "little") + word.to_bytes(4, "little")
        elif directive == "load_background":
            fields = parse_key_values(parts[1:], line_no, "load_background")
            require_fields(fields, {"slot", "event_value", "id"}, line_no, "load_background")
            slot = parse_hex_int(fields["slot"])
            event_value = parse_hex_int(fields["event_value"])
            bg_id = parse_hex_int(fields["id"])
            # Discarded by Command_40; accepted so an odd file round-trips.
            raw_high = parse_hex_int(fields.get("unused_high", fields.get("raw_high", "0")))
            default_arg = slot | (event_value << 7)
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= slot <= 0x03 or event_value not in (0, 1):
                raise ValueError(f"line {line_no}: load_background slot/event_value out of range")
            if (arg & 0x03) != slot or int(bool(arg & 0x80)) != event_value:
                raise ValueError(f"line {line_no}: load_background arg does not match slot/event_value")
            if not 0 <= bg_id <= 0xFFFF or not 0 <= raw_high <= 0xFFFF:
                raise ValueError(f"line {line_no}: load_background id/raw_high out of range")
            word = bg_id | (raw_high << 16)
            header = build_command_header(0x40, 1, arg, flags)
            data = header.to_bytes(4, "little") + word.to_bytes(4, "little")
        elif directive == "setting_map":
            fields = parse_key_values(parts[1:], line_no, "setting_map")
            require_fields(fields, {"id"}, line_no, "setting_map")
            setting_map_id = parse_hex_int(fields["id"])
            arg = parse_hex_int(fields.get("arg", str(setting_map_id)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= setting_map_id <= 0x0F or (arg & 0x0F) != setting_map_id:
                raise ValueError(f"line {line_no}: setting_map arg does not match id")
            data = build_command_header(0x42, 0, arg, flags).to_bytes(4, "little")
        elif directive == "delete_background":
            fields = parse_key_values(parts[1:], line_no, "delete_background")
            require_fields(fields, {"slot"}, line_no, "delete_background")
            slot = parse_hex_int(fields["slot"])
            arg = parse_hex_int(fields.get("arg", str(slot)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= slot <= 0x03 or (arg & 0x03) != slot:
                raise ValueError(f"line {line_no}: delete_background arg does not match slot")
            data = build_command_header(0x43, 0, arg, flags).to_bytes(4, "little")
        elif directive == "background_change_map":
            fields = parse_key_values(parts[1:], line_no, "background_change_map")
            require_fields(fields, {"mode"}, line_no, "background_change_map")
            mode = parse_hex_int(fields["mode"])
            words = parse_optional_word_list(fields)
            arg = parse_hex_int(fields.get("arg", str(mode)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= mode <= 0x0F or (arg & 0x0F) != mode:
                raise ValueError(f"line {line_no}: background_change_map arg does not match mode")
            header = build_command_header(0x44, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "background_play_animation":
            fields = parse_key_values(parts[1:], line_no, "background_play_animation")
            arg, words = build_background_play_animation_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x45, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "background_stop_animation":
            fields = parse_key_values(parts[1:], line_no, "background_stop_animation")
            arg, words = build_background_stop_animation_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x46, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "background_visibility":
            fields = parse_key_values(parts[1:], line_no, "background_visibility")
            arg, words = build_background_visibility_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x47, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "landscape_visibility":
            fields = parse_key_values(parts[1:], line_no, "landscape_visibility")
            arg, words = build_landscape_visibility_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x48, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "background_runtime_field":
            fields = parse_key_values(parts[1:], line_no, "background_runtime_field")
            control, words = build_background_runtime_field_words(fields, line_no)
            arg = parse_hex_int(fields.get("arg", "0"))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x4C, 1 + len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words([control, *words])
        elif directive == "background_auto_rate_anim":
            fields = parse_key_values(parts[1:], line_no, "background_auto_rate_anim")
            arg, words = build_background_auto_rate_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x4D, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "load_texture":
            fields = parse_key_values(parts[1:], line_no, "load_texture")
            require_fields(fields, {"mode", "texture", "group", "raw_byte3"}, line_no, "load_texture")
            mode = parse_hex_int(fields["mode"])
            texture = parse_hex_int(fields["texture"])
            group = parse_hex_int(fields["group"])
            raw_byte3 = parse_hex_int(fields["raw_byte3"])
            arg = parse_hex_int(fields.get("arg", str(mode)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= mode <= 0x1F or (arg & 0x1F) != mode:
                raise ValueError(f"line {line_no}: load_texture arg does not match mode")
            if not -(1 << 15) <= texture < (1 << 15) or not 0 <= group <= 0xFF or not 0 <= raw_byte3 <= 0xFF:
                raise ValueError(f"line {line_no}: load_texture fields out of range")
            word = (texture & 0xFFFF) | (group << 16) | (raw_byte3 << 24)
            header = build_command_header(0x60, 1, arg, flags)
            data = header.to_bytes(4, "little") + word.to_bytes(4, "little")
        elif directive == "load_paf":
            fields = parse_key_values(parts[1:], line_no, "load_paf")
            require_fields(fields, {"mode", "paf", "raw_high"}, line_no, "load_paf")
            mode = parse_hex_int(fields["mode"])
            paf = parse_hex_int(fields["paf"])
            raw_high = parse_hex_int(fields["raw_high"])
            arg = parse_hex_int(fields.get("arg", str(mode)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= mode <= 0x1F or (arg & 0x1F) != mode:
                raise ValueError(f"line {line_no}: load_paf arg does not match mode")
            if not -(1 << 15) <= paf < (1 << 15) or not 0 <= raw_high <= 0xFFFF:
                raise ValueError(f"line {line_no}: load_paf fields out of range")
            word = (paf & 0xFFFF) | (raw_high << 16)
            header = build_command_header(0x61, 1, arg, flags)
            data = header.to_bytes(4, "little") + word.to_bytes(4, "little")
        elif directive == "primitive_priority":
            fields = parse_key_values(parts[1:], line_no, "primitive_priority")
            require_fields(fields, {"slot", "priority"}, line_no, "primitive_priority")
            slot = parse_hex_int(fields["slot"])
            priority = parse_hex_int(fields["priority"])
            arg = parse_hex_int(fields.get("arg", str(slot)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= slot <= 0x03 or (arg & 0x03) != slot:
                raise ValueError(f"line {line_no}: primitive_priority arg does not match slot")
            header = build_command_header(0x66, 1, arg, flags)
            data = header.to_bytes(4, "little") + priority.to_bytes(4, "little")
        elif directive == "sprite_config":
            fields = parse_key_values(parts[1:], line_no, "sprite_config")
            if "control" in fields:
                control = parse_hex_int(fields["control"])
            else:
                control = derive_sprite_control(fields, line_no)
            if "sprite_index" in fields and parse_hex_int(fields["sprite_index"]) != ((control >> 24) & 0xFF):
                raise ValueError(f"line {line_no}: sprite_config sprite_index does not match control")
            if "words" in fields:
                words = parse_optional_word_list(fields)
            else:
                words = build_sprite_config_payload(control, fields, line_no)
            arg = parse_hex_int(fields.get("arg", "0"))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x62, 1 + len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words([control, *words])
        elif directive == "primitive_anim_slot":
            fields = parse_key_values(parts[1:], line_no, "primitive_anim_slot")
            arg, words = build_primitive_anim_slot_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x63, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "primitive_play_paf":
            fields = parse_key_values(parts[1:], line_no, "primitive_play_paf")
            arg, words = build_primitive_play_paf_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x64, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "primitive_stop_paf":
            fields = parse_key_values(parts[1:], line_no, "primitive_stop_paf")
            arg, words = build_primitive_stop_paf_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x65, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "global_visual_state":
            fields = parse_key_values(parts[1:], line_no, "global_visual_state")
            arg, words = build_global_visual_state_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x68, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "primitive_helper_byte":
            fields = parse_key_values(parts[1:], line_no, "primitive_helper_byte")
            require_fields(fields, {"slot", "value"}, line_no, "primitive_helper_byte")
            slot = parse_hex_int(fields["slot"])
            value = parse_hex_int(fields["value"])
            words = parse_optional_word_list(fields)
            default_arg = slot | (value << 3)
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= slot <= 0x07 or not 0 <= value <= 0x1F or arg != default_arg:
                raise ValueError(f"line {line_no}: primitive_helper_byte arg does not match slot/value")
            header = build_command_header(0x69, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "primitive_move_sprtg":
            fields = parse_key_values(parts[1:], line_no, "primitive_move_sprtg")
            arg, words = build_primitive_move_sprtg_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x6A, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "text_output":
            fields = parse_key_values(parts[1:], line_no, "text_output")
            require_fields(fields, {"mode"}, line_no, "text_output")
            mode = parse_hex_int(fields["mode"])
            default_arg = mode << 5
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= mode <= 0x07 or (arg >> 5) != mode:
                raise ValueError(f"line {line_no}: text_output arg does not match mode")
            if "text" in fields:
                if mode != 0:
                    raise ValueError(f"line {line_no}: text_output text= is only valid for mode 0")
                if "words" in fields:
                    raise ValueError(f"line {line_no}: text_output cannot mix text= and words=")
                words = sjis_text_to_words(parse_source_string(fields["text"], line_no, "text_output text"))
            elif "event_value" in fields or "width" in fields or "raw_high" in fields:
                require_fields(fields, {"event_value", "width", "raw_high"}, line_no, "text_output")
                if mode != 1:
                    raise ValueError(f"line {line_no}: text_output event_value fields are only valid for mode 1")
                if "words" in fields:
                    raise ValueError(f"line {line_no}: text_output cannot mix event_value fields and words=")
                event_value = parse_hex_int(fields["event_value"])
                width = parse_hex_int(fields["width"])
                raw_high = parse_hex_int(fields["raw_high"])
                if not 0 <= event_value <= 0xFFFF or not 0 <= width <= 0xFF or not 0 <= raw_high <= 0xFF:
                    raise ValueError(f"line {line_no}: text_output event_value/width/raw_high out of range")
                words = [event_value | (width << 16) | (raw_high << 24)]
            elif "clear_id" in fields:
                clear_id = parse_hex_int(fields["clear_id"])
                if mode != 7 or clear_id != 0xFA:
                    raise ValueError(f"line {line_no}: text_output clear_id is only valid as 0xFA for mode 7")
                if "words" in fields:
                    raise ValueError(f"line {line_no}: text_output cannot mix clear_id= and words=")
                words = []
            else:
                words = parse_optional_word_list(fields)
            header = build_command_header(0x8F, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "window_message":
            fields = parse_key_values(parts[1:], line_no, "window_message")
            arg, words = build_window_message_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x1B, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "window_message_mode":
            fields = parse_key_values(parts[1:], line_no, "window_message_mode")
            words = parse_optional_word_list(fields)
            arg = parse_hex_int(fields.get("arg", "0"))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x8A, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "talk_bustup_display":
            fields = parse_key_values(parts[1:], line_no, "talk_bustup_display")
            arg, words = build_talk_bustup_display_words(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(0x8B, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "talk_rmf":
            fields = parse_key_values(parts[1:], line_no, "talk_rmf")
            fields.setdefault("raw_high", "0")
            require_fields(fields, {"attach_mode", "flag0", "flag1", "message"}, line_no, "talk_rmf")
            attach_mode = parse_hex_int(fields["attach_mode"])
            flag0 = parse_hex_int(fields["flag0"])
            flag1 = parse_hex_int(fields["flag1"])
            message = parse_hex_int(fields["message"])
            raw_high = parse_hex_int(fields["raw_high"])
            default_arg = (attach_mode << 1) | (flag0 << 6) | (flag1 << 7)
            arg = parse_hex_int(fields.get("arg", str(default_arg)))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 0 <= attach_mode <= 0x03 or flag0 not in (0, 1) or flag1 not in (0, 1):
                raise ValueError(f"line {line_no}: talk_rmf fields out of range")
            if arg & 0x01 or (arg & 0xC6) != default_arg:
                raise ValueError(f"line {line_no}: talk_rmf arg does not match attach_mode/flags or uses unsupported explicit-character branch")
            if not 0 <= message <= 0xFFFF or not 0 <= raw_high <= 0xFFFF:
                raise ValueError(f"line {line_no}: talk_rmf message/raw_high out of range")
            header = build_command_header(0x89, 1, arg, flags)
            data = header.to_bytes(4, "little") + (message | (raw_high << 16)).to_bytes(4, "little")
        elif directive in NEW_FORM_BUILDERS:
            fields = parse_key_values(parts[1:], line_no, directive)
            opcode, builder = NEW_FORM_BUILDERS[directive]
            arg, words = builder(fields, line_no)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            header = build_command_header(opcode, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive in SOURCE_OPCODE_ALIASES:
            fields = parse_key_values(parts[1:], line_no, directive)
            words = parse_optional_word_list(fields)
            arg = parse_hex_int(fields.get("arg", "0"))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            opcode = SOURCE_OPCODE_ALIASES[directive]
            header = build_command_header(opcode, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "end_script":
            fields = parse_key_values(parts[1:], line_no, "end_script")
            flags = parse_hex_int(fields.get("flags", "0x80"))
            arg = parse_hex_int(fields.get("arg", "0"))
            words = parse_optional_word_list(fields)
            header = build_command_header(0x00, len(words), arg, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(words)
        elif directive == "return_zero":
            fields = parse_key_values(parts[1:], line_no, "return_zero")
            header = parse_hex_int(fields.get("header", "0xFFFFFFFF"))
            require_header_opcode(header, 0xFF, line_no, "return_zero")
            data = header.to_bytes(4, "little")
        elif directive == "nop":
            fields = parse_key_values(parts[1:], line_no, "nop")
            flags = parse_hex_int(fields.get("flags", "0x80"))
            data = build_command_header(0x0F, 0, 0, flags).to_bytes(4, "little")
        elif directive == "nop_2e":
            fields = parse_key_values(parts[1:], line_no, "nop_2e")
            flags = parse_hex_int(fields.get("flags", "0x80"))
            data = build_command_header(0x2E, 0, 0, flags).to_bytes(4, "little")
        elif directive == "set_flags":
            fields = parse_key_values(parts[1:], line_no, "set_flags")
            require_fields(fields, {"first", "count", "values"}, line_no, "set_flags")
            first_flag = parse_hex_int(fields["first"])
            count = parse_hex_int(fields["count"])
            values_text = fields["values"]
            if values_text == "on":
                values = (1 << count) - 1
            elif values_text == "off":
                values = 0
            else:
                values = parse_hex_int(values_text)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            if not 1 <= count <= 16:
                raise ValueError(f"line {line_no}: set_flags count must be 1..16")
            if not 0 <= first_flag <= 0xFFFF or not 0 <= values <= 0xFFFF:
                raise ValueError(f"line {line_no}: set_flags fields out of range")
            header = build_command_header(0x10, 1, count - 1, flags)
            word = first_flag | (values << 16)
            data = header.to_bytes(4, "little") + word.to_bytes(4, "little")
        elif directive == "conditional_end":
            fields = parse_key_values(parts[1:], line_no, "conditional_end")
            condition = resolve_packed_word(
                fields, line_no, "conditional_end", "condition", CONDITION_SPECS, required=False
            )
            flags = parse_hex_int(fields.get("flags", "0x80"))
            condition_words = build_condition_words_from_source(fields, condition, line_no, "conditional_end")
            if "condition_args" not in fields:
                # A raw condition_args= list (data regions) keeps its length.
                if condition and len(condition_words) != 2:
                    raise ValueError(f"line {line_no}: conditional_end expects exactly two condition words")
                if not condition and condition_words:
                    raise ValueError(f"line {line_no}: unconditional conditional_end cannot have condition words")
            header = build_command_header(0x05, len(condition_words), condition, flags)
            data = header.to_bytes(4, "little") + pack_u32_words(condition_words)
        elif directive == "jump":
            fields = parse_key_values(parts[1:], line_no, "jump")
            require_fields(fields, {"goto"}, line_no, "jump")
            flags = parse_hex_int(fields.get("flags", "0x80"))
            target = parse_source_target(fields["goto"], labels, line_no)
            header = build_command_header(0x02, 1, 0, flags)
            data = compile_source_branch(offset, header, target, 0, [], line_no)
        elif directive in BRANCH_FRIENDLY_HEADS:
            fields = parse_key_values(parts[1:], line_no, directive)
            require_fields(fields, {"goto"}, line_no, directive)
            flags = parse_hex_int(fields.get("flags", "0x80"))
            condition, condition_words = build_branch_friendly_words(directive, fields, line_no)
            target = parse_source_target(fields["goto"], labels, line_no)
            header = build_command_header(0x02, 1 + len(condition_words), 0, flags)
            data = compile_source_branch(offset, header, target, condition, condition_words, line_no)
        elif directive == "branch":
            fields = parse_key_values(parts[1:], line_no, "branch")
            require_fields(fields, {"target"}, line_no, "branch")
            condition = resolve_packed_word(
                fields, line_no, "branch", "condition", CONDITION_SPECS, required=False
            )
            arg = parse_hex_int(fields.get("arg", "0"))
            flags = parse_hex_int(fields.get("flags", "0x80"))
            condition_words = build_condition_words_from_source(fields, condition, line_no, "branch")
            target = parse_source_target(fields["target"], labels, line_no)
            header = build_command_header(0x02, 1 + len(condition_words), arg, flags)
            data = compile_source_branch(
                offset, header, target, condition, condition_words, line_no,
                strict="condition_args" not in fields,
            )
        else:
            raise AssertionError(f"unhandled source directive {directive}")

        verify_built_command(data, parts, line_no)
        output[offset : offset + len(data)] = data

    return bytes(output)

def source_words_field(words: list[int]) -> str:
    if not words:
        return "words="
    return "words=" + words_to_csv(words)

def source_words_suffix(words: list[int]) -> str:
    if not words:
        return ""
    return " " + source_words_field(words)

def truncated_words_suffix(words: list[int]) -> str:
    """Tail for an incomplete decode: always carries the words= marker.

    A bare `words=` on an otherwise structured line tells the builder the
    declared word count ended mid-shape, so missing payload fields mean
    "stream ended here" rather than an authoring error.
    """
    return " " + source_words_field(words)

def source_condition_args_field(condition: int, words: list[int]) -> str:
    if not words:
        return ""
    if (condition & 0x7F) in {0x01, 0x02, 0x03, 0x04, 0x05, 0x07, 0x08, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10}:
        # Known ids normally decode to named fields; fall back to the raw
        # payload when the self-check in source_condition_details_field fails
        # (data regions misread as conditions carry out-of-range selectors).
        if source_condition_details_field(condition, words):
            return ""
    return " condition_args=" + words_to_csv(words)

def source_condition_details_field(condition: int, words: list[int]) -> str:
    details = condition_payload_details(condition, words)
    if not details:
        return ""
    parts = [
        f"cond_base=0x{int(details['condition_base_id']):02X}",
        f"invert={int(details['condition_inverted'])}",
    ]
    kind = details.get("condition_kind")
    if kind:
        parts.append(f"kind={kind}")
    if "compare" in details:
        parts.append(f"compare={details['compare']}")
        parts.append(f"compare_from_event={int(details['compare_from_event_value'])}")
    if "compare_control_byte" in details:
        parts.append(f"compare_control=0x{int(details['compare_control_byte']):02X}")
    if "compare_value" in details:
        parts.append(f"value={int(details['compare_value'])}")
    for key in (
        "event_value_id",
        "first_flag",
        "flag_count",
        "required_mask",
        "mask_mode",
        "requires_all",
        "time_control",
        "time_source_selector",
        "time_component_mask",
        "time_components",
        "condition_word0_mid",
        "condition_word1",
        "runtime_source",
        "character_raw_word",
        "character_word",
        "character_source",
        "property_selector",
        "property_offset",
        "scan_all_characters",
        "category_selector",
        "selector",
        "lookup_script_id",
        "lookup_script_raw_mid",
        "script_lookup_control",
        "script_id_from_event",
        "uses_character_filter",
        "uses_selector",
        "lookup_mode",
        "state_selector",
        "item_or_state_word",
        "item_or_state_selector",
        "status_selector",
        "object_state_selector",
        "expected_word",
    ):
        if key not in details:
            continue
        value = details[key]
        # Zero-valued optional operands are suppressed (the builder defaults
        # them), and pure bit-duplicates of script_lookup_control are dropped.
        if key in CONDITION_ZERO_SUPPRESSED_KEYS and value == 0:
            continue
        if key in CONDITION_DERIVED_KEYS:
            continue
        out_key = "event_value" if key == "event_value_id" else key
        if isinstance(value, int):
            if key.endswith("word") or key in {"required_mask", "expected_word"}:
                parts.append(f"{out_key}=0x{value:04X}")
            else:
                parts.append(f"{out_key}={value}")
        else:
            parts.append(f"{out_key}={value}")
    text = " " + " ".join(parts)
    # Self-check: emit named condition fields only when they rebuild the exact
    # payload words; otherwise the caller emits raw condition_args= instead.
    try:
        check_fields = parse_key_values(text.split(), 0, "condition_selfcheck")
        rebuilt = build_condition_words_from_source(check_fields, condition, 0, "condition_selfcheck")
    except Exception:
        return ""
    if rebuilt != list(words):
        return ""
    return text

CONDITION_ZERO_SUPPRESSED_KEYS = {
    "lookup_script_raw_mid",
    "character_word",
    "character_source",
    "selector",
    "scan_all_characters",
    "condition_word0_mid",
}

CONDITION_DERIVED_KEYS = {
    "script_id_from_event",
    "uses_character_filter",
    "uses_selector",
}

def source_sprite_fields_field(control: int, payload: list[int]) -> str:
    parts: list[str] = []
    for field in sprite_config_fields(control, payload):
        kind = field["kind"]
        if field.get("missing"):
            parts.append(f"{kind}=<missing>")
            continue
        if kind == "status_bits":
            parts.append(f"status_bits=0x{int(field['word']):08X}")
        elif kind == "field_10":
            parts.append(f"time_base=0x{int(field['word']):08X}")
        elif kind == "xy_s16":
            parts.append(f"xy={int(field['x'])},{int(field['y'])}")
        elif kind == "texture_page":
            mapped = field.get("mapped_tpage")
            mapped_text = "none" if mapped is None else f"0x{int(mapped) & 0xFFFFFFFF:X}" if int(mapped) >= 0 else "-1"
            parts.append(
                f"texture_page=low:{int(field['tex_low'])},mode:{int(field['tpage_mode'])},mapped:{mapped_text}"
            )
        elif kind in {"uv_u16", "rect0_u16", "rect1_u16"}:
            friendly = {"uv_u16": "uv", "rect0_u16": "corner_offset", "rect1_u16": "size"}[kind]
            parts.append(f"{friendly}={int(field['a'])},{int(field['b'])}")
        elif kind == "color_word":
            parts.append(f"color=0x{int(field['word']):08X}")
        elif kind == "scale_f32":
            parts.append(f"scale={format_f32(float(field['x']))},{format_f32(float(field['y']))}")
        elif kind == "rotate_f32":
            parts.append(f"rotate={format_f32(float(field['value']))}")
        elif kind == "blend_alpha_param":
            texture_param = field.get("texture_param")
            texture_text = "none" if texture_param is None else f"0x{int(texture_param):02X}"
            parts.append(
                f"blend_alpha=mode:{int(field['mode'])},texture:{texture_text},alpha:{int(field['alpha'])}"
            )
        elif kind == "trailing_words":
            words = field.get("words", [])
            if isinstance(words, list) and words:
                parts.append("trailing=" + words_to_csv(words))
    return " " + " ".join(parts) if parts else ""

def parse_int_pair(text: str, line_no: int, field_name: str) -> tuple[int, int]:
    values = text.split(",") if text else []
    if len(values) != 2:
        raise ValueError(f"line {line_no}: {field_name} expects two comma-separated integers")
    return int(values[0], 0), int(values[1], 0)

def parse_colon_fields(text: str, line_no: int, field_name: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not text:
        return values
    for item in text.split(","):
        if ":" not in item:
            raise ValueError(f"line {line_no}: {field_name} item {item!r} must be key:value")
        key, value = item.split(":", 1)
        if key in values:
            raise ValueError(f"line {line_no}: duplicate {field_name} key {key}")
        values[key] = value
    return values

SPRITE_FIELD_RENAMES: dict[str, str] = {
    "time_base": "field10",
    "uv": "uv_u16",
    "corner_offset": "rect0_u16",
    "size": "rect1_u16",
}

def derive_sprite_control(fields: dict[str, str], line_no: int) -> int:
    """Rebuild the sprite control word from sprite_index plus field presence."""
    require_fields(fields, {"sprite_index"}, line_no, "sprite_config")
    index = parse_hex_int(fields["sprite_index"])
    if not 0 <= index <= 0xFF:
        raise ValueError(f"line {line_no}: sprite_config sprite_index out of range")
    control = index << 24
    for bit, names in (
        (0x001, ("status_bits",)),
        (0x002, ("field10", "time_base")),
        (0x004, ("xy",)),
        (0x008, ("texture_page",)),
        (0x010, ("uv_u16", "uv")),
        (0x020, ("rect0_u16", "corner_offset")),
        (0x040, ("rect1_u16", "size")),
        (0x080, ("color",)),
        (0x100, ("scale",)),
        (0x200, ("rotate",)),
    ):
        if any(name in fields for name in names):
            control |= bit
    if "blend_alpha" in fields:
        raise ValueError(f"line {line_no}: sprite_config blend_alpha requires an explicit control= word (bits 0x400/0x800 are ambiguous)")
    return control

def build_sprite_config_payload(control: int, fields: dict[str, str], line_no: int) -> list[int]:
    for new_name, old_name in SPRITE_FIELD_RENAMES.items():
        if new_name in fields and old_name not in fields:
            fields = {**fields, old_name: fields[new_name]}
    words: list[int] = []

    def require_word(bit: int, name: str) -> None:
        if control & bit:
            require_fields(fields, {name}, line_no, "sprite_config")
            words.append(parse_hex_int(fields[name]) & 0xFFFFFFFF)
        elif name in fields:
            raise ValueError(f"line {line_no}: sprite_config {name} present but control bit 0x{bit:03X} is clear")

    require_word(0x001, "status_bits")
    require_word(0x002, "field10")
    if control & 0x004:
        require_fields(fields, {"xy"}, line_no, "sprite_config")
        x, y = parse_int_pair(fields["xy"], line_no, "xy")
        if not -(1 << 15) <= x < (1 << 15) or not -(1 << 15) <= y < (1 << 15):
            raise ValueError(f"line {line_no}: sprite_config xy values out of s16 range")
        words.append((x & 0xFFFF) | ((y & 0xFFFF) << 16))
    elif "xy" in fields:
        raise ValueError(f"line {line_no}: sprite_config xy present but control bit 0x004 is clear")

    if control & 0x008:
        require_fields(fields, {"texture_page"}, line_no, "sprite_config")
        parts = parse_colon_fields(fields["texture_page"], line_no, "texture_page")
        require_fields(parts, {"low", "mode"}, line_no, "texture_page")
        low = parse_hex_int(parts["low"])
        mode = parse_hex_int(parts["mode"])
        if not 0 <= low <= 0xFF or not 0 <= mode <= 0x0F:
            raise ValueError(f"line {line_no}: sprite_config texture_page values out of range")
        words.append(low | (mode << 8))
    elif "texture_page" in fields:
        raise ValueError(f"line {line_no}: sprite_config texture_page present but control bit 0x008 is clear")

    for bit, name in ((0x010, "uv_u16"), (0x020, "rect0_u16"), (0x040, "rect1_u16")):
        if control & bit:
            require_fields(fields, {name}, line_no, "sprite_config")
            a, b = parse_int_pair(fields[name], line_no, name)
            if not 0 <= a <= 0xFFFF or not 0 <= b <= 0xFFFF:
                raise ValueError(f"line {line_no}: sprite_config {name} values out of u16 range")
            words.append(a | (b << 16))
        elif name in fields:
            raise ValueError(f"line {line_no}: sprite_config {name} present but control bit 0x{bit:03X} is clear")

    require_word(0x080, "color")
    if control & 0x100:
        require_fields(fields, {"scale"}, line_no, "sprite_config")
        words.extend(parse_float_words(fields["scale"], 2, line_no, "scale"))
    elif "scale" in fields:
        raise ValueError(f"line {line_no}: sprite_config scale present but control bit 0x100 is clear")
    if control & 0x200:
        require_fields(fields, {"rotate"}, line_no, "sprite_config")
        words.append(f32_to_u32(float(fields["rotate"])))
    elif "rotate" in fields:
        raise ValueError(f"line {line_no}: sprite_config rotate present but control bit 0x200 is clear")
    if control & 0xC00:
        require_fields(fields, {"blend_alpha"}, line_no, "sprite_config")
        parts = parse_colon_fields(fields["blend_alpha"], line_no, "blend_alpha")
        require_fields(parts, {"mode", "alpha"}, line_no, "blend_alpha")
        mode = parse_hex_int(parts["mode"])
        alpha = parse_hex_int(parts["alpha"])
        if not 0 <= mode <= 0xFF or not 0 <= alpha <= 0xFF:
            raise ValueError(f"line {line_no}: sprite_config blend_alpha values out of range")
        words.append(mode | (alpha << 16))
    elif "blend_alpha" in fields:
        raise ValueError(f"line {line_no}: sprite_config blend_alpha present but control bits 0xC00 are clear")
    if "trailing" in fields:
        words.extend(parse_word_list(fields["trailing"]))
    return words

def source_flags_suffix(flags: int) -> str:
    # Bit 31 of the command word is the StepProcess "keep going" bit: set, the
    # next command runs in the same frame; clear, the script yields until the
    # next game update. The overwhelmingly common 0x80 is therefore the implied
    # default, a plain 0x00 prints as the readable `yield=1`, and any other
    # value stays an explicit flags= byte.
    if flags == 0x80:
        return ""
    if flags == 0x00:
        return " yield=1"
    return f" flags=0x{flags:02X}"

def source_arg_suffix(arg: int) -> str:
    return f" arg=0x{arg:02X}" if arg else ""

_SELFCHECK_PROLOGUE = ".header 3\n.header_extra 0x00000000\n.entry start\n\nstart:\n"

_selfcheck_cache: dict[tuple[str, bytes], bool] = {}

def source_line_selfcheck(line: str, expected: bytes) -> bool:
    """True when a single source line recompiles to exactly the given bytes."""
    key = (line, expected)
    cached = _selfcheck_cache.get(key)
    if cached is not None:
        return cached
    try:
        rebuilt = bytes(compile_evd_source(_SELFCHECK_PROLOGUE + "  " + line + "\n"))[0x0C:]
        ok = rebuilt == expected
    except Exception:
        ok = False
    _selfcheck_cache[key] = ok
    return ok

def raw_source_command(command: dict[str, Any], expected: bytes | None = None) -> str:
    """Verbatim raw form of a command: alias (or .cmd) with arg/flags/words.

    When `expected` is given, the alias spelling is self-checked and the
    unambiguous `.cmd` spelling is used if a strict compile branch would
    reject or reshape it.
    """
    opcode = int(command["opcode"])
    arg = int(command["arg_byte"])
    flags = int(command["high_byte"])
    words = [int(word) for word in command.get("args_u32", [])]
    suffix = []
    if arg:
        suffix.append(f"arg=0x{arg:02X}")
    if flags != 0x80:
        suffix.append(f"flags=0x{flags:02X}")
    suffix.append(source_words_field(words))
    alias = SOURCE_OPCODE_ALIASES_BY_OPCODE.get(opcode)
    if alias:
        line = "  " + " ".join([alias] + suffix)
        if expected is None or source_line_selfcheck(line.strip(), expected):
            return line
    return "  " + " ".join([".cmd", f"op=0x{opcode:02X}"] + suffix)

def format_source_command(command: dict[str, Any], labels: dict[int, str]) -> str:
    opcode = int(command["opcode"])
    arg = int(command["arg_byte"])
    flags = int(command["high_byte"])
    words = list(command.get("args_u32", []))
    offset = int(command["offset"])

    if opcode == 0x00 and not words:
        # Data words misread as op-0x00 headers can carry a nonzero arg byte;
        # preserve it so the region round-trips exactly.
        suffix = f"{source_arg_suffix(arg)}{source_flags_suffix(flags)}"
        return f"  end_script{suffix}"
    if opcode == 0x0F and not words:
        suffix = "" if flags == 0x80 else f" flags=0x{flags:02X}"
        return f"  nop{suffix}"
    if opcode == 0x2E and not words:
        suffix = source_flags_suffix(flags)
        return f"  nop_2e{suffix}"
    if opcode == 0xFF:
        return f"  return_zero header=0x{int(command['word']):08X}"
    if opcode == 0x10 and len(words) == 1:
        word = words[0]
        flag_count = (arg & 0x0F) + 1
        first_flag = word & 0xFFFF
        values = (word >> 16) & 0xFFFF
        suffix = source_flags_suffix(flags)
        if flag_count == 1 and values in (0, 1):
            head = "set_flag" if values else "clear_flag"
            return f"  {head} flag={first_flag}{suffix}"
        if values == (1 << flag_count) - 1:
            return f"  set_flags first={first_flag} count={flag_count} values=on{suffix}"
        if values == 0:
            return f"  set_flags first={first_flag} count={flag_count} values=off{suffix}"
        return f"  set_flags first={first_flag} count={flag_count} values=0x{values:04X}{suffix}"
    if opcode == 0x05:
        condition = arg
        suffix = source_flags_suffix(flags)
        return (
            f"  conditional_end condition=0x{condition:02X}"
            f"{source_condition_args_field(condition, words)}{source_condition_details_field(condition, words)}{suffix}"
        )
    if opcode == 0x02 and words:
        details = command.get("details", {})
        target = details.get("branch_target_offset")
        condition = int(details.get("condition_id", arg))
        # Only emit the structured branch form for sane shapes; data regions
        # misread as branches (negative targets, odd word counts) fall through
        # to the raw alias form, which round-trips verbatim.
        sane = (
            isinstance(target, int)
            and target >= 0x0C
            and len(words) == (3 if condition else 1)
        )
        if sane:
            target_text = labels.get(target, f"0x{target:X}")
            if not arg:
                friendly = format_branch_friendly(condition, words, target_text, flags)
                if friendly:
                    return friendly
            condition_words = words[1:3] if condition else []
            arg_suffix = f" arg=0x{arg:02X}" if arg else ""
            suffix = source_flags_suffix(flags)
            return (
                f"  branch target={target_text} condition=0x{condition:02X}"
                f"{source_condition_args_field(condition, condition_words)}"
                f"{source_condition_details_field(condition, condition_words)}{arg_suffix}{suffix}"
            )
    if opcode == 0x01:
        mode = arg & 0x0F
        explicit_char = (arg >> 4) & 0x01
        default_arg = mode | (explicit_char << 4)
        fields, complete = decode_script_start_stack_fields(arg, words)
        if complete:
            return (
                f"  script_start_stack {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return f"  .cmd op=0x01 arg=0x{arg:02X}{source_flags_suffix(flags)} words={words_to_csv(words)}"
    if opcode == 0x04:
        fields, complete = decode_script_start_fields(arg, words)
        mode = arg & 0x0F
        default_char = (arg >> 4) & 0x01
        default_arg = mode | (default_char << 4)
        if complete:
            return (
                f"  script_start {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return f"  .cmd op=0x04 arg=0x{arg:02X}{source_flags_suffix(flags)} words={words_to_csv(words)}"
    if opcode == 0x0A:
        mode = arg & 0x3F
        return (
            f"  change_game_mode mode={mode}"
            f"{source_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != mode else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x0B:
        fields, complete = decode_script_stop_fields(arg, words)
        if complete:
            return f"  script_stop {' '.join(fields)}{source_flags_suffix(flags)}"
        # Incomplete decodes already carry their words= marker inside fields.
        return f"  script_stop {' '.join(fields)}{source_flags_suffix(flags)}"
    if opcode == 0x0D:
        fields, complete = decode_marker_seek_fields(arg, words)
        if complete:
            return f"  marker_seek {' '.join(fields)}{source_flags_suffix(flags)}"
        return f"  marker_seek {' '.join(fields)}{source_arg_suffix(arg)}{source_flags_suffix(flags)}"
    if opcode == 0x11:
        pop = arg & 0x01
        fields, complete = decode_scene_save_env_fields(arg, words)
        if complete:
            return (
                f"  scene_save_env {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != pop else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  scene_save_env pop={pop}{source_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != pop else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x12:
        fields, complete = decode_time_schedule_value_fields(arg, words)
        if complete:
            return f"  time_schedule_value {' '.join(fields)}{source_flags_suffix(flags)}"
        return f"  time_schedule_value {' '.join(fields)}{truncated_words_suffix(words)}{source_flags_suffix(flags)}"
    if opcode == 0x13:
        mode = arg & 0x03
        update_bit = (arg >> 5) & 0x01
        time_enable = (arg >> 6) & 0x03
        default_arg = mode | (update_bit << 5) | (time_enable << 6)
        return (
            f"  radiata_time_enable mode={mode} update_bit={update_bit} time_enable={time_enable}"
            f"{source_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x15:
        default_char = arg & 0x01
        default_object = (arg >> 1) & 0x01
        event_char = (arg >> 7) & 0x01
        default_arg = default_char | (default_object << 1) | (event_char << 7)
        fields, complete = decode_script_defaults_fields(arg, words)
        if complete:
            return (
                f"  script_defaults {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  script_defaults default_char={default_char} default_object={default_object} "
            f"event_char={event_char}{source_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x16:
        fields, complete = decode_battle_acquisition_setup_fields(arg, words)
        if complete:
            if arg == 0 and fields and fields[0] == "arg=0x00":
                fields = fields[1:]
            return f"  battle_acquisition_setup {' '.join(fields)}{source_flags_suffix(flags)}"
        return f"  battle_acquisition_setup{source_words_suffix(words)}{source_arg_suffix(arg)}{source_flags_suffix(flags)}"
    if opcode == 0x17:
        explicit_char = arg & 0x01
        default_arg = explicit_char
        fields, complete = decode_battle_character_entry_fields(arg, words)
        if complete:
            return (
                f"  battle_character_entry {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  battle_character_entry explicit_char={explicit_char}"
            f"{source_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x18 and len(words) == 1:
        word = words[0]
        mode = arg & 0x03
        flag7 = (arg >> 7) & 0x01
        default_arg = mode | (flag7 << 7)
        raw_high_text = f" raw_high=0x{(word >> 16) & 0xFFFF:04X}" if word >> 16 else ""
        return (
            f"  party_member mode={mode} flag7={flag7} character=0x{word & 0xFFFF:04X}"
            f"{raw_high_text}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x19:
        default_arg = arg & 0xE7
        fields, complete = decode_personal_inventory_fields(arg, words)
        if complete:
            return (
                f"  personal_inventory {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  personal_inventory {' '.join(fields)}{truncated_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x1A:
        fields, complete = decode_character_equipment_fields(arg, words)
        default_arg = (arg & 0x03) | ((arg >> 6) << 6)
        if complete:
            return (
                f"  character_equipment {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  character_equipment {' '.join(fields)}{truncated_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x1C:
        default_arg = arg & 0x0F
        fields, complete = decode_stand_context_fields(arg, words)
        if complete:
            return (
                f"  stand_context {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  stand_context field0={arg & 0x01} stand={(arg >> 1) & 0x01} "
            f"position={(arg >> 2) & 0x01} posture={(arg >> 3) & 0x01}"
            f"{source_words_suffix(words)}{source_arg_suffix(arg) if arg != default_arg else ''}"
            f"{source_flags_suffix(flags)}"
        )
    if opcode == 0x20:
        explicit_char = arg & 0x01
        fields, complete = decode_character_data_fields(arg, words)
        if complete:
            default_arg = explicit_char | ((arg >> 6) << 6)
            return (
                f"  character_data {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  character_data explicit_char={explicit_char}{truncated_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != explicit_char else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x21:
        fields, complete = decode_character_delete_fields(arg, words)
        default_arg = (arg & 0x01) | (((arg >> 7) & 0x01) << 7)
        if complete:
            return (
                f"  character_delete_data {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        explicit_char = arg & 0x01
        return (
            f"  character_delete_data explicit_char={explicit_char}{truncated_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != explicit_char else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x22:
        default_arg = arg & 0xC7
        if len(words) <= 1:
            # Semantic spellings of the arg bits, zero-suppressed; the raw
            # flagN names remain accepted on input.
            pieces = []
            if len(words) == 1:
                pieces.append(f"character=0x{words[0]:08X}")
            if (arg >> 1) & 0x01:
                pieces.append("sub_manager_flag=1")
            if (arg >> 2) & 0x01:
                pieces.append("no_render_bit=1")
            if (arg >> 6) & 0x01:
                pieces.append("no_render_with_child=1")
            if (arg >> 7) & 0x01:
                pieces.append("no_render=1")
            body = (" " + " ".join(pieces)) if pieces else ""
            return (
                f"  character_attach_render{body}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}"
                f"{source_flags_suffix(flags)}"
            )
        return (
            f"  character_attach_render explicit_char={arg & 0x01} flag1={(arg >> 1) & 0x01} "
            f"flag2={(arg >> 2) & 0x01} flag6={(arg >> 6) & 0x01} flag7={(arg >> 7) & 0x01}"
            f"{source_words_suffix(words)}{source_arg_suffix(arg) if arg != default_arg else ''}"
            f"{source_flags_suffix(flags)}"
        )
    if opcode == 0x34:
        explicit_char = arg & 0x01
        control_index = 1 if explicit_char else 0
        if control_index + 1 < len(words):
            word0 = words[control_index]
            word1 = words[control_index + 1]
            character = f" character=0x{words[0]:08X}" if explicit_char else ""
            # 0xFF = leave unchanged for expression/blink/mouth (Command_34 at
            # 0x002EEA38 skips each 0xFF byte); zero-valued optional bytes are
            # suppressed. blink_interval is halved by the engine before
            # BlinkControll.
            pieces = [
                f"expression=0x{word0 & 0xFF:02X}",
                f"blink=0x{(word0 >> 8) & 0xFF:02X}",
                f"mouth=0x{(word0 >> 16) & 0xFF:02X}",
            ]
            if (word0 >> 24) & 0xFF:
                pieces.append(f"raw0_high=0x{(word0 >> 24) & 0xFF:02X}")
            mouth_default = 0xFF if (word0 >> 16) & 0xFF == 0xFF else 0
            if word1 & 0xFF != mouth_default:
                pieces.append(f"mouth_arg0=0x{word1 & 0xFF:02X}")
            if (word1 >> 8) & 0xFF != mouth_default:
                pieces.append(f"mouth_arg1=0x{(word1 >> 8) & 0xFF:02X}")
            if (word1 >> 16) & 0xFF:
                pieces.append(f"blink_interval=0x{(word1 >> 16) & 0xFF:02X}")
            if (word1 >> 24) & 0xFF:
                pieces.append(f"raw1_high=0x{(word1 >> 24) & 0xFF:02X}")
            return (
                f"  character_expression explicit_char={explicit_char}{character} "
                f"{' '.join(pieces)}"
                f"{source_arg_suffix(arg) if arg != explicit_char else ''}{source_flags_suffix(flags)}"
            )
    if opcode == 0x3B:
        explicit_char = arg & 0x01
        control_index = 1 if explicit_char else 0
        if control_index < len(words):
            blend_word = words[control_index]
            character = f" character=0x{words[0]:08X}" if explicit_char else ""
            blend_text = format_f32(u32_to_f32(blend_word))
            if f32_to_u32(float(blend_text)) == blend_word:
                blend_field = f"blend={blend_text}"
            else:
                blend_field = f"blend_word=0x{blend_word:08X}"
            return (
                f"  strong_motion_blend explicit_char={explicit_char}{character} "
                f"{blend_field}"
                f"{source_arg_suffix(arg) if arg != explicit_char else ''}{source_flags_suffix(flags)}"
            )
    if opcode == 0x2B:
        explicit_char = arg & 0x01
        control_index = 1 if explicit_char else 0
        if control_index + 1 == len(words):
            control = words[control_index]
            character = f" character=0x{words[0]:08X}" if explicit_char else ""
            default_arg = explicit_char | ((arg >> 6) << 6)
            raw_high_text = f" raw_high=0x{control >> 8:06X}" if control >> 8 else ""
            return (
                f"  character_precreate_anim explicit_char={explicit_char} mode={arg >> 6}{character} "
                f"anim_id=0x{control & 0xFF:02X}{raw_high_text}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
    if opcode == 0x28:
        fields, complete = decode_character_move_points_fields(arg, words)
        default_arg = arg & 0x1F
        if complete:
            return (
                f"  character_move_points {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  character_move_points {' '.join(fields)}{truncated_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0xC0:
        explicit_char = arg & 0x01
        control_index = 1 if explicit_char else 0
        if control_index < len(words):
            control = words[control_index]
            character = f" character=0x{words[0]:08X}" if explicit_char else ""
            raw_high_text = f" raw_high=0x{(control >> 24) & 0xFF:02X}" if control >> 24 else ""
            arg_byte_text = f" schedule_arg_byte=0x{(control >> 16) & 0xFF:02X}" if (control >> 16) & 0xFF else ""
            return (
                f"  person_schedule_list explicit_char={explicit_char}{character} "
                f"schedule_list=0x{control & 0xFFFF:04X}{arg_byte_text}{raw_high_text}"
                f"{source_arg_suffix(arg) if arg != explicit_char else ''}{source_flags_suffix(flags)}"
            )
    if opcode == 0x25:
        explicit_char = arg & 0x01
        handler_mode = arg >> 6
        default_arg = explicit_char | (handler_mode << 6)
        fields, complete = decode_character_sub_anim_fields(arg, words)
        if complete:
            return (
                f"  character_sub_anim {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  character_sub_anim explicit_char={explicit_char} handler_mode={handler_mode}"
            f"{source_words_suffix(words)}{source_arg_suffix(arg) if arg != default_arg else ''}"
            f"{source_flags_suffix(flags)}"
        )
    if opcode == 0xC1:
        fields, complete = decode_map_change_check_fields(arg, flags, words)
        default_flags = 0x80 if flags & 0x80 else 0
        if complete:
            return f"  map_change_check {' '.join(fields)}{source_flags_suffix(flags) if flags != default_flags else ''}"
        return (
            f"  map_change_check {' '.join(fields)}"
            f"{source_arg_suffix(arg) if arg != (arg & 0x01) else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x24:
        fields, complete = decode_character_virtual_24_fields(arg, words)
        default_arg = (arg & 0x01) | (((arg >> 6) & 0x03) << 6)
        if complete:
            return (
                f"  character_virtual_24 {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  character_virtual_24 explicit_char={arg & 0x01}{source_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != (arg & 0x01) else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0xC4:
        fields, complete = decode_person_field_update_fields(arg, words)
        default_arg = (arg & 0x01) | (((arg >> 7) & 0x01) << 7)
        if complete:
            return (
                f"  person_field_update {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  person_field_update explicit_char={arg & 0x01}{source_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != (arg & 0x01) else ''}{source_flags_suffix(flags)}"
        )
    if opcode in (0x23, 0x31, 0x34, 0x36):
        name = {
            0x23: "character_animation",
            0x31: "character_collision_setup",
            0x34: "character_expression",
            0x36: "character_anim_signal",
        }[opcode]
        explicit_char = arg & 0x01
        extra = ""
        if opcode == 0x23:
            fields, complete = decode_character_animation_fields(arg, words)
            default_arg = (
                explicit_char
                | (((arg >> 1) & 0x01) << 1)
                | (((arg >> 2) & 0x01) << 2)
                | (((arg >> 3) & 0x01) << 3)
                | (((arg >> 4) & 0x01) << 4)
                | (((arg >> 5) & 0x01) << 5)
                | ((arg >> 6) << 6)
            )
            if complete:
                return (
                    f"  character_animation {' '.join(fields)}"
                    f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
                )
            return (
                f"  character_animation {' '.join(fields)}{truncated_words_suffix(words)}"
                f"{source_arg_suffix(arg) if arg != explicit_char else ''}{source_flags_suffix(flags)}"
            )
        if opcode == 0x31:
            fields, complete = decode_character_collision_fields(arg, words)
            if complete:
                return (
                    f"  character_collision_setup {' '.join(fields)}"
                    f"{source_arg_suffix(arg) if arg != explicit_char else ''}{source_flags_suffix(flags)}"
                )
            return (
                f"  character_collision_setup {' '.join(fields)}{truncated_words_suffix(words)}"
                f"{source_arg_suffix(arg) if arg != explicit_char else ''}{source_flags_suffix(flags)}"
            )
        if opcode == 0x36:
            fields, complete = decode_character_anim_signal_fields(arg, words)
            default_arg = arg & 0x1F
            if complete:
                return (
                    f"  character_anim_signal {' '.join(fields)}"
                    f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
                )
            return (
                f"  character_anim_signal {' '.join(fields)}{truncated_words_suffix(words)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  {name} explicit_char={explicit_char}{extra}{source_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != explicit_char else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x26:
        fields, complete = decode_character_attach_parent_fields(arg, words)
        default_arg = arg
        if complete:
            return (
                f"  character_attach_parent {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  character_attach_parent {' '.join(fields)}"
            f"{source_arg_suffix(arg) if arg != (arg & 0x07) else ''}"
            f"{source_flags_suffix(flags)}"
        )
    if opcode == 0x27:
        default_arg = arg & 0xD1
        if len(words) == 1:
            words_text = (
                f" character=0x{words[0]:08X}"
                f" detach_flag_low={(arg >> 4) & 0x01}"
                f" character_parent_detach_flag_high={(arg >> 6) & 0x01}"
                f" flag7_preserved={(arg >> 7) & 0x01}"
            )
        else:
            words_text = source_words_suffix(words) if words else ""
        return (
            f"  character_detach_parent explicit_char={arg & 0x01} flag4={(arg >> 4) & 0x01} "
            f"flag6={(arg >> 6) & 0x01} flag7={(arg >> 7) & 0x01}"
            f"{words_text}{source_arg_suffix(arg) if arg != default_arg else ''}"
            f"{source_flags_suffix(flags)}"
        )
    if opcode == 0x29:
        payload_text = ""
        if arg & 0x01:
            if len(words) == 1:
                payload_text = f" character=0x{words[0]:08X}"
            else:
                payload_text = source_words_suffix(words)
        elif words:
            payload_text = source_words_suffix(words)
        return (
            f"  character_move_pause explicit_char={arg & 0x01} pause_mode={(arg >> 1) & 0x03}"
            f"{payload_text}{source_arg_suffix(arg) if arg != ((arg & 0x01) | (((arg >> 1) & 0x03) << 1)) else ''}"
            f"{source_flags_suffix(flags)}"
        )
    if opcode == 0x2A:
        explicit_char = arg & 0x01
        mode = (arg >> 4) & 0x0F
        default_arg = explicit_char | (mode << 4)
        fields, complete = decode_character_movement_fields(arg, words)
        if complete:
            return (
                f"  character_movement {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  character_movement explicit_char={explicit_char} mode={mode}"
            f"{source_words_suffix(words)}{source_arg_suffix(arg) if arg != default_arg else ''}"
            f"{source_flags_suffix(flags)}"
        )
    if opcode == 0x2D:
        fields, complete = decode_rotate_option_fields(arg, words)
        explicit_char = arg & 0x01
        if complete:
            return (
                f"  character_rotate_option {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != explicit_char else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  character_rotate_option {' '.join(fields)}{truncated_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != explicit_char else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x2F:
        explicit_char = arg & 0x01
        control_index = 1 if explicit_char else 0
        if control_index + 1 < len(words):
            character = f" character=0x{words[0]:08X}" if explicit_char else ""
            return (
                f"  character_attribute explicit_char={explicit_char}{character} "
                f"collision_attr=0x{words[control_index]:08X} "
                f"collision_value=0x{words[control_index + 1]:08X} set_attr2={0 if (arg & 0x02) else 1}"
                f"{source_arg_suffix(arg) if arg != explicit_char else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  character_attribute explicit_char={explicit_char}{source_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != explicit_char else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x30:
        fields, complete = decode_character_move_position_fields(arg, words)
        default_arg = arg & 0xFF
        if complete:
            return (
                f"  character_move_position {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  character_move_position {' '.join(fields)}{truncated_words_suffix(words)}"
            f"{source_flags_suffix(flags)}"
        )
    if opcode == 0x32:
        fields, complete = decode_auto_rate_fields(arg, words)
        default_arg = arg & 0x1F
        if complete:
            return (
                f"  character_auto_rate_anim {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  character_auto_rate_anim {' '.join(fields)}{truncated_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x33:
        default_arg = arg & 0x07
        explicit_char = arg & 0x01
        control_index = 1 if explicit_char else 0
        if control_index < len(words):
            control = words[control_index]
            move_selector = (control >> 8) & 0xFF
            character = f" character=0x{words[0]:08X}" if explicit_char else ""
            manual_time = ""
            if control_index + 1 < len(words):
                manual_time_word = words[control_index + 1]
                time_text = format_f32(u32_to_f32(manual_time_word))
                if f32_to_u32(float(time_text)) == manual_time_word:
                    manual_time = f" manual_time={time_text}"
                else:
                    manual_time = f" manual_time_word=0x{manual_time_word:08X}"
            pieces = [f"explicit_char={explicit_char}", f"eye_ball={(arg >> 1) & 0x01}", f"eye_move={(arg >> 2) & 0x01}"]
            if character:
                pieces.append(character.strip())
            if control & 0xFF:
                pieces.append(f"eye_ball_byte=0x{control & 0xFF:02X}")
            pieces.append(f"move_selector=0x{move_selector:02X}")
            pieces.append(f"move_action={EYE_MOVE_SELECTOR_ACTIONS.get(move_selector, 'ignored')}")
            if sign_extend((control >> 16) & 0xFF, 8):
                pieces.append(f"manual_x_s8={sign_extend((control >> 16) & 0xFF, 8)}")
            if sign_extend((control >> 24) & 0xFF, 8):
                pieces.append(f"manual_y_s8={sign_extend((control >> 24) & 0xFF, 8)}")
            return (
                f"  character_eye_control {' '.join(pieces)}{manual_time}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  character_eye_control explicit_char={explicit_char} eye_ball={(arg >> 1) & 0x01} "
            f"eye_move={(arg >> 2) & 0x01}{source_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x35:
        fields, complete = decode_character_single_manager_fields(arg, words)
        default_arg = (arg & 0x01) | (((arg >> 1) & 0x07) << 1) | (((arg >> 4) & 0x01) << 4)
        if complete:
            return (
                f"  character_single_manager {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  character_single_manager {' '.join(fields)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x39:
        fields, complete = decode_character_event_leave_fields(arg, words)
        if complete:
            return f"  character_event_leave {' '.join(fields)}{source_flags_suffix(flags)}"
        return f"  character_event_leave {' '.join(fields)}{truncated_words_suffix(words)}{source_flags_suffix(flags)}"
    if opcode == 0xD5:
        fields, complete = decode_special_effect_fields(arg, words)
        default_arg = (arg & 0x03) | (arg & 0x80)
        if complete:
            return (
                f"  special_effect {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  special_effect {' '.join(fields)}{truncated_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x51:
        fields, complete = decode_camera_mode_fields(arg, words)
        if complete:
            return f"  camera_mode {' '.join(fields)}{source_flags_suffix(flags)}"
        return (
            f"  camera_mode mode={arg & 0x0F} flag4={(arg >> 4) & 0x01} "
            f"flag5={(arg >> 5) & 0x01} flag6={(arg >> 6) & 0x01} flag7={(arg >> 7) & 0x01}"
            f"{source_words_suffix(words)}{source_flags_suffix(flags)}"
        )
    if opcode == 0x52:
        default_arg = arg & 0x1F
        fields, complete = decode_camera_transform_fields(arg, words)
        if complete:
            return (
                f"  camera_transform_param {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  camera_transform_param {' '.join(fields)}"
            f"{source_words_suffix(words)}{source_arg_suffix(arg) if arg != default_arg else ''}"
            f"{source_flags_suffix(flags)}"
        )
    if opcode == 0x55:
        default_arg = arg & 0x27
        fields, complete = decode_camera_capture_target_fields(arg, words)
        if complete:
            return (
                f"  camera_capture_target {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  camera_capture_target {' '.join(fields)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x54:
        default_arg = (arg & 0x03) | (arg & 0x0C) | (arg & 0xE0)
        fields, complete = decode_camera_move_etc_fields(arg, words)
        if complete:
            return (
                f"  camera_move_etc {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  camera_move_etc {' '.join(fields)}"
            f"{source_words_suffix(words)}{source_arg_suffix(arg) if arg != default_arg else ''}"
            f"{source_flags_suffix(flags)}"
        )
    if opcode == 0x56:
        fields, complete = decode_camera_move_existing_fields(arg, words)
        if complete:
            return f"  camera_move_existing {' '.join(fields)}{source_flags_suffix(flags)}"
        return (
            f"  camera_move_existing move_mode={arg & 0x0F} target_slot={arg >> 4}"
            f"{source_words_suffix(words)}{source_flags_suffix(flags)}"
        )
    if opcode == 0x59:
        fields, complete = decode_camera_color_anim_fields(arg, words)
        if complete:
            return f"  camera_color_anim {' '.join(fields)}{source_flags_suffix(flags)}"
        return f"  camera_color_anim {' '.join(fields)}{truncated_words_suffix(words)}{source_flags_suffix(flags)}"
    if opcode == 0x67 and words:
        fields, complete = decode_fade_control_fields(arg, words)
        if complete:
            return f"  fade_control {' '.join(fields)}{source_flags_suffix(flags)}"
        return f"  fade_control {' '.join(fields)}{source_flags_suffix(flags)}"
    if opcode == 0x57:
        fields, complete = decode_position_vibration_vector_fields(arg, words)
        default_arg = (arg & 0x0F) | (((arg >> 4) & 0x0F) << 4)
        if complete:
            return (
                f"  position_vibration_vector {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  position_vibration_vector {' '.join(fields)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x58:
        mode = arg & 0x0F
        return (
            f"  position_vibration_clear mode={mode}{source_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != mode else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x03 and words:
        fields, complete = decode_trigger_fields(arg, words)
        action = arg >> 6
        default_arg = action << 6
        if complete:
            return (
                f"  trigger {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        payload = words_to_csv(words[1:])
        payload_suffix = f" payload={payload}" if payload else ""
        return (
            f"  trigger {' '.join(fields)}{payload_suffix}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x14 and len(words) == 3:
        if not arg:
            friendly = format_expr_friendly(words, flags)
            if friendly:
                return friendly
        op = words[0] & 0x07
        return (
            f"  expr op={op} op_name={EXPR_OPERATION_NAMES.get(op, 'unknown')} "
            f"store_type=0x{(words[0] >> 8) & 0x0F:X} control_mid=0x{(words[0] >> 4) & 0x0F:X} "
            f"lhs_type=0x{(words[1] >> 24) & 0x0F:X} lhs_tag=0x{(words[1] >> 24) & 0xFF:02X} "
            f"rhs_type=0x{(words[2] >> 24) & 0x0F:X} rhs_tag=0x{(words[2] >> 24) & 0xFF:02X} "
            f"control=0x{words[0]:08X} "
            f"lhs=0x{words[1]:08X} rhs=0x{words[2]:08X}"
            f"{source_arg_suffix(arg)}{source_flags_suffix(flags)}"
        )
    if opcode == 0x70 and len(words) == 2:
        return (
            f"  set_bgm info0=0x{words[0]:08X} info1=0x{words[1]:08X}"
            f"{source_arg_suffix(arg)}{source_flags_suffix(flags)}"
        )
    if opcode == 0x71 and not words:
        mode = arg & 0x03
        return f"  play_bgm mode={mode}{source_arg_suffix(arg) if arg != mode else ''}{source_flags_suffix(flags)}"
    if opcode == 0x72 and len(words) == 1:
        word = words[0]
        mode = arg & 0x03
        pause = int(bool(arg & 0x80))
        default_arg = mode | (pause << 7)
        # Command_72 masks the operand with 0xFFFF in both delay slots before
        # StopBgm/PauseBgm, so the high half never reaches the engine.
        unused = f" unused_high=0x{(word >> 16) & 0xFFFF:04X}" if word >> 16 else ""
        return (
            f"  bgm_control mode={mode} pause={pause} "
            f"value=0x{word & 0xFFFF:04X}{unused}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x73 and len(words) == 1:
        word = words[0]
        slot = arg & 0x03
        return (
            f"  set_bgm_volume slot={slot} volume=0x{word & 0xFFFF:04X} "
            f"time=0x{(word >> 16) & 0xFFFF:04X}"
            f"{source_arg_suffix(arg) if arg != slot else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x7C and len(words) == 3:
        word = words[1]
        return (
            f"  play_movie movie=0x{words[0] & 0xFFFF:04X} "
            f"param0={sign_extend(word & 0xFFFF, 16)} "
            f"param1={sign_extend((word >> 16) & 0xFFFF, 16)} "
            f"extra=0x{words[2]:08X}{source_arg_suffix(arg)}{source_flags_suffix(flags)}"
        )
    if opcode == 0x7D and not words:
        return f"  stop_movie{source_arg_suffix(arg)}{source_flags_suffix(flags)}"
    if opcode == 0x74:
        fields, complete = decode_load_sound_resource_fields(arg, words)
        mode = arg & 0x1F
        if complete:
            return (
                f"  load_sound_resource {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != mode else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  load_sound_resource {' '.join(fields)}{truncated_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != mode else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x75:
        fields, complete = decode_play_sound_effect_fields(arg, words)
        default_arg = ((arg >> 6) & 0x03) << 6
        if complete:
            return (
                f"  play_sound_effect {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  play_sound_effect {' '.join(fields)}{truncated_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x76:
        fields, complete = decode_stop_sound_effect_fields(arg, words)
        default_arg = (arg & 0x0F) | (arg & 0xF0)
        if complete:
            return (
                f"  stop_sound_effect {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  stop_sound_effect {' '.join(fields)}{truncated_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x79:
        fields, complete = decode_sound_listener_fields(arg, words)
        default_arg = arg & 0x7F
        if complete:
            return (
                f"  sound_listener {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  sound_listener {' '.join(fields)}{truncated_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x7A and not words:
        mode = arg & 0x01
        return f"  sound_effect_stack mode={mode}{source_arg_suffix(arg) if arg != mode else ''}{source_flags_suffix(flags)}"
    if opcode == 0x82 and not words:
        return f"  vibration_stop{source_arg_suffix(arg)}{source_flags_suffix(flags)}"
    if opcode == 0x83 and len(words) == 1:
        word = words[0]
        return (
            f"  play_vibration strength=0x{word & 0xFF:02X} pattern=0x{(word >> 8) & 0xFF:02X} "
            f"duration=0x{(word >> 16) & 0xFFFF:04X}{source_arg_suffix(arg)}{source_flags_suffix(flags)}"
        )
    if opcode == 0x50 and len(words) == 1:
        word = words[0]
        default_arg = (arg & 0x03) | (arg & 0x0C)
        return (
            f"  camera_select select_slot={arg & 0x03} target_slot={(arg >> 2) & 0x03} "
            f"camera=0x{word & 0xFFFF:04X} target=0x{(word >> 16) & 0xFFFF:04X}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x5A and not words:
        default_arg = arg & 0x07
        return (
            f"  camera_flags flag0={arg & 0x01} flag1={(arg >> 1) & 0x01} "
            f"flag2={(arg >> 2) & 0x01}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x8E and len(words) == 3:
        # Word1 is the RGBA text colour and word2 the draw priority: PutText
        # feeds slot+0x00 to CPrimitiveHelper::CreateSPRT as the sprite colour
        # and TextOut passes slot+0x08 to SetPriority (shadow layer draws at
        # priority + 1).
        xy = words[0]
        return (
            f"  text_message_layout x={sign_extend(xy & 0xFFFF, 16)} "
            f"y={sign_extend((xy >> 16) & 0xFFFF, 16)} "
            f"color=0x{words[1]:08X} "
            f"priority={words[2]}"
            f"{source_arg_suffix(arg)}{source_flags_suffix(flags)}"
        )
    if opcode == 0xC5 and len(words) == 1:
        word = words[0]
        return (
            f"  set_radiata_time minute={format_clock_component(word & 0xFF, 0xFF)} "
            f"hour={format_clock_component((word >> 8) & 0xFF, 0xFF)} day={'resync' if (word >> 16) & 0xFFFF == 0xFFFF else (word >> 16) & 0xFFFF}"
            f"{source_arg_suffix(arg)}{source_flags_suffix(flags)}"
        )
    if opcode == 0x06 and len(words) == 1:
        word = words[0]
        force_high_bit = arg & 0x01
        # Command_06 masks the operand with 0x7FFF unconditionally (the `andi`
        # sits in the branch delay slot), so everything above bit 14 is thrown
        # away before LoadScriptFile sees it, and bit 15 is re-derived from the
        # argument byte. The corpus never sets them; print them only if some
        # file does, so they stay round-trippable without adding noise.
        ignored = ""
        if (word >> 15) & 0x01:
            ignored += f" unused_bit15={(word >> 15) & 0x01}"
        if (word >> 16) & 0xFFFF:
            ignored += f" unused_high=0x{(word >> 16) & 0xFFFF:04X}"
        return (
            f"  load_script_file force_high_bit={force_high_bit} file=0x{word & 0x7FFF:04X}"
            f"{ignored}"
            f"{source_arg_suffix(arg) if arg != force_high_bit else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x40 and len(words) == 1:
        word = words[0]
        slot = arg & 0x03
        event_value = int(bool(arg & 0x80))
        default_arg = slot | (event_value << 7)
        # Command_40 masks the operand with 0xFFFF in the delay slot, before
        # both the event-value lookup and CBackGround::LoadData, so the high
        # half is discarded. One corpus line sets it; it is kept verbatim there.
        unused = f" unused_high=0x{(word >> 16) & 0xFFFF:04X}" if word >> 16 else ""
        return (
            f"  load_background slot={slot} event_value={event_value} id=0x{word & 0xFFFF:04X}"
            f"{unused}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x42 and not words:
        setting_map_id = arg & 0x0F
        return (
            f"  setting_map id={setting_map_id}"
            f"{source_arg_suffix(arg) if arg != setting_map_id else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x43 and not words:
        slot = arg & 0x03
        return (
            f"  delete_background slot={slot}"
            f"{source_arg_suffix(arg) if arg != slot else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x44:
        mode = arg & 0x0F
        return (
            f"  background_change_map mode={mode}{source_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != mode else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x45:
        fields, complete = decode_background_play_animation_fields(arg, words)
        default_arg = arg & 0x3F
        if complete:
            return (
                f"  background_play_animation {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  background_play_animation {' '.join(fields)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x46:
        fields, complete = decode_background_stop_animation_fields(arg, words)
        default_arg = (arg & 0x03) | (arg & 0xF0)
        if complete:
            return (
                f"  background_stop_animation {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  background_stop_animation {' '.join(fields)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x47:
        fields, complete = decode_background_visibility_fields(arg, words)
        default_arg = (arg & 0x01) | (((arg >> 1) & 0x01) << 1) | (((arg >> 4) & 0x03) << 4) | (((arg >> 6) & 0x03) << 6)
        if complete:
            return (
                f"  background_visibility {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  background_visibility {' '.join(fields)}{truncated_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x48:
        fields, complete = decode_landscape_visibility_fields(arg, words)
        default_arg = (arg & 0x03) | (arg & 0x80)
        if complete:
            return (
                f"  landscape_visibility {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  landscape_visibility {' '.join(fields)}{truncated_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x4A:
        fields, _complete = decode_position_vibration_param_fields(words)
        return f"  position_vibration_param {' '.join(fields)}{source_arg_suffix(arg)}{source_flags_suffix(flags)}"
    if opcode == 0x4C and words:
        fields, complete = decode_background_runtime_field_fields(words[0], words[1:])
        if complete:
            return (
                f"  background_runtime_field {' '.join(fields)}"
                f"{source_arg_suffix(arg)}{source_flags_suffix(flags)}"
            )
        return (
            f"  background_runtime_field {' '.join(fields)}{source_words_suffix(words[1:])}"
            f"{source_arg_suffix(arg)}{source_flags_suffix(flags)}"
        )
    if opcode == 0x4D:
        fields, complete = decode_background_auto_rate_fields(arg, words)
        if complete:
            return f"  background_auto_rate_anim {' '.join(fields)}{source_flags_suffix(flags)}"
        return f"  background_auto_rate_anim {' '.join(fields)}{truncated_words_suffix(words)}{source_flags_suffix(flags)}"
    if opcode == 0x60 and len(words) == 1:
        word = words[0]
        mode = arg & 0x1F
        return (
            f"  load_texture mode={mode} texture={sign_extend(word & 0xFFFF, 16)} "
            f"group=0x{(word >> 16) & 0xFF:02X} raw_byte3=0x{(word >> 24) & 0xFF:02X}"
            f"{source_arg_suffix(arg) if arg != mode else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x61 and len(words) == 1:
        word = words[0]
        mode = arg & 0x1F
        return (
            f"  load_paf mode={mode} paf={sign_extend(word & 0xFFFF, 16)} "
            f"raw_high=0x{(word >> 16) & 0xFFFF:04X}"
            f"{source_arg_suffix(arg) if arg != mode else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x66 and len(words) == 1:
        slot = arg & 0x03
        return (
            f"  primitive_priority slot={slot} priority=0x{words[0]:08X}"
            f"{source_arg_suffix(arg) if arg != slot else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x62 and words:
        control = words[0]
        sprite_index = (control >> 24) & 0xFF
        mask = control & 0x0FFF
        if not control & 0x00FFFC00:
            # Fully modeled: the mask is implied by which fields are present.
            text = (
                f"  sprite_config sprite_index={sprite_index}"
                f"{source_sprite_fields_field(control, words[1:])}"
                f"{source_arg_suffix(arg)}{source_flags_suffix(flags)}"
            )
        else:
            text = (
                f"  sprite_config control=0x{control:08X} sprite_index={sprite_index} mask=0x{mask:03X}"
                f"{source_sprite_fields_field(control, words[1:])}"
                f"{source_arg_suffix(arg)}{source_flags_suffix(flags)}"
            )
        # Self-check: only emit the structured form when it rebuilds the exact
        # payload; garbage data regions fall through to the raw alias form.
        try:
            check_fields = parse_key_values(text.split()[1:], 0, "sprite_config")
            rebuilt = build_sprite_config_payload(control, check_fields, 0)
        except Exception:
            rebuilt = None
        if rebuilt == words[1:]:
            return text
    if opcode == 0x63:
        fields, complete = decode_primitive_anim_slot_fields(arg, words)
        default_arg = arg & 0xC7
        if complete:
            return (
                f"  primitive_anim_slot {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  primitive_anim_slot {' '.join(fields)}{truncated_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x64:
        fields, complete = decode_primitive_play_paf_fields(arg, words)
        default_arg = (arg & 0x03) | (arg & 0x7C) | (arg & 0x80)
        if complete:
            return (
                f"  primitive_play_paf {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        slot = arg & 0x03
        return (
            f"  primitive_play_paf {' '.join(fields)}{truncated_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x65:
        fields, complete = decode_primitive_stop_paf_fields(arg, words)
        default_arg = (arg & 0x03) | (arg & 0x7C)
        if complete:
            return (
                f"  primitive_stop_paf {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  primitive_stop_paf {' '.join(fields)}{truncated_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x68:
        default_arg = arg & 0x03
        fields, complete = decode_global_visual_state_fields(arg, words)
        if complete:
            return (
                f"  global_visual_state {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  global_visual_state {' '.join(fields)}{truncated_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x69:
        slot = arg & 0x07
        value = arg >> 3
        return (
            f"  primitive_helper_byte slot={slot} value={value}{source_words_suffix(words)}"
            f"{source_flags_suffix(flags)}"
        )
    if opcode == 0x6A:
        fields, complete = decode_primitive_move_sprtg_fields(arg, words)
        default_arg = (arg & 0x03) | (((arg >> 2) & 0x03) << 2) | ((arg >> 4) << 4)
        if complete:
            return (
                f"  primitive_move_sprtg {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  primitive_move_sprtg {' '.join(fields)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x8F:
        mode = arg >> 5
        default_arg = mode << 5
        if mode == 0:
            text = words_to_sjis_text(words)
            if text is not None:
                return (
                    f"  text_output mode=0 mode_name=sjis_text text={json.dumps(text, ensure_ascii=False)}"
                    f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
                )
        if mode == 1 and len(words) == 1:
            word = words[0]
            return (
                f"  text_output mode=1 mode_name=event_value_number event_value=0x{word & 0xFFFF:04X} "
                f"width=0x{(word >> 16) & 0xFF:02X} raw_high=0x{(word >> 24) & 0xFF:02X}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        if mode == 7 and not words:
            return (
                f"  text_output mode=7 mode_name=clear_text clear_id=0x00FA"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
            )
        return (
            f"  text_output mode={mode} mode_name={TEXT_OUTPUT_MODE_NAMES.get(mode, 'unknown')}"
            f"{source_words_suffix(words)}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x1B:
        fields, complete = decode_window_message_fields(arg, words)
        if complete:
            return f"  window_message {' '.join(fields)}{source_flags_suffix(flags)}"
        return f"  window_message {' '.join(fields)}{truncated_words_suffix(words)}{source_flags_suffix(flags)}"
    if opcode == 0x8A:
        return f"  window_message_mode{source_words_suffix(words)}{source_arg_suffix(arg)}{source_flags_suffix(flags)}"
    if opcode == 0x8B:
        default_arg = arg & 0xF7
        fields, complete = decode_talk_bustup_display_fields(arg, words)
        if complete:
            return (
                f"  talk_bustup_display {' '.join(fields)}"
                f"{source_arg_suffix(arg) if arg != default_arg else ''}"
                f"{source_flags_suffix(flags)}"
            )
        return (
            f"  talk_bustup_display {' '.join(fields)}"
            f"{source_words_suffix(words)}{source_arg_suffix(arg) if arg != default_arg else ''}"
            f"{source_flags_suffix(flags)}"
        )
    if opcode == 0x89 and len(words) == 1 and not (arg & 0x01):
        word = words[0]
        attach_mode = (arg >> 1) & 0x03
        flag0 = (arg >> 6) & 0x01
        flag1 = (arg >> 7) & 0x01
        default_arg = (attach_mode << 1) | (flag0 << 6) | (flag1 << 7)
        raw_high = (word >> 16) & 0xFFFF
        raw_high_text = f" raw_high=0x{raw_high:04X}" if raw_high else ""
        return (
            f"  talk_rmf attach_mode={attach_mode} flag0={flag0} flag1={flag1} "
            f"message=0x{word & 0xFFFF:04X}{raw_high_text}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0xF0:
        return format_special_f0_line(int(command["word"]))
    if opcode >= 0xF0:
        return f"  .word 0x{int(command['word']):08X}"

    if opcode == 0xD2:
        explicit = arg & 0x01
        sel_from_event = (arg >> 1) & 0x01
        mode = arg >> 2
        if mode in PARAM_HOLDER_ACTIONS and len(words) == explicit + 1:
            pieces = []
            if explicit:
                pieces.append(f"character=0x{words[0]:08X}")
            selector_word = words[explicit]
            pieces.append(f"action={PARAM_HOLDER_ACTIONS[mode]}")
            sel_key = "selector_from_event" if sel_from_event else "selector"
            pieces.append(f"{sel_key}={selector_word & 0xFFFF}")
            pieces.append(f"to_event={(selector_word >> 16) & 0xFFFF}")
            return f"  character_script_param_holder {' '.join(pieces)}{source_flags_suffix(flags)}"
    if opcode == 0xD3:
        if arg == 1 and len(words) == 1:
            return f"  battle_copy_character character=0x{words[0]:08X}{source_flags_suffix(flags)}"
        if arg == 0 and not words:
            return f"  battle_copy_character{source_flags_suffix(flags)}"
    if opcode == 0xD4:
        explicit_a = arg & 0x01
        explicit_b = (arg >> 1) & 0x01
        if arg <= 3 and len(words) == explicit_a + explicit_b + 1:
            pieces = []
            cursor = 0
            if explicit_a:
                pieces.append(f"character_a=0x{words[cursor]:08X}")
                cursor += 1
            if explicit_b:
                pieces.append(f"character_b=0x{words[cursor]:08X}")
                cursor += 1
            pieces.append(f"distance={format_f32(u32_to_f32(words[cursor]))}")
            return f"  battle_volty_distance {' '.join(pieces)}{source_flags_suffix(flags)}"
    if opcode == 0xD1:
        explicit = arg & 0x01
        name_source = (arg >> 1) & 0x03
        scale_from_stream = (arg >> 3) & 0x01
        expected = explicit + (4 if name_source == 1 else 0) + (1 if scale_from_stream else 0)
        if (
            arg == (explicit | (name_source << 1) | (scale_from_stream << 3))
            and not (name_source == 1 and scale_from_stream)
            and len(words) == expected
        ):
            pieces = []
            cursor = 0
            if explicit:
                pieces.append(f"explicit_char=1 character=0x{words[cursor]:08X}")
                cursor += 1
            if name_source == 1:
                text = words_to_sjis_text(words[cursor:cursor + 4])
                cursor += 4
                if text is None:
                    pieces = None
                else:
                    pieces.append(f"name={json.dumps(text, ensure_ascii=False)}")
            elif name_source:
                pieces.append(f"name_source={name_source}")
            if pieces is not None:
                if scale_from_stream:
                    pieces.append(f"scale_character=0x{words[cursor]:08X}")
                body = " ".join(pieces) if pieces else "name_source=0"
                return f"  character_collision_control_d1 {body}{source_flags_suffix(flags)}"
    if opcode == 0xC3 and len(words) >= (arg & 0x01):
        explicit = arg & 0x01
        selector = (arg >> 6) & 0x03
        allow_a = (arg >> 2) & 0x03
        allow_b = (arg >> 4) & 0x03
        default_arg = explicit | (allow_a << 2) | (allow_b << 4) | (selector << 6)
        pieces = [f"explicit_char={explicit}", f"selector={selector}", f"allow_a={allow_a}", f"allow_b={allow_b}"]
        rest = words
        if explicit:
            pieces.append(f"character=0x{words[0]:08X}")
            rest = words[1:]
        return (
            f"  person_allow_attribute {' '.join(pieces)}"
            f"{source_words_suffix(rest) if rest else ''}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0xE0 and len(words) >= 1 + (arg & 0x01):
        explicit = arg & 0x01
        flag_word = words[1] if explicit else words[0]
        pieces = [f"explicit_char={explicit}"]
        if explicit:
            pieces.append(f"character=0x{words[0]:08X}")
        pieces.append(f"life_flag=0x{flag_word & 0xFFFF:04X}")
        if flag_word >> 16:
            pieces.append(f"raw_high=0x{flag_word >> 16:04X}")
        rest = words[(2 if explicit else 1):]
        return (
            f"  chara_put_attach_life_flag {' '.join(pieces)}"
            f"{source_words_suffix(rest) if rest else ''}"
            f"{source_arg_suffix(arg) if arg != explicit else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x1E and (arg & 0x01) and not words:
        slot = arg >> 4
        default_arg = 0x01 | (slot << 4)
        return (
            f"  packing_file_load_or_release release=1 slot={slot}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x1E and not (arg & 0x01) and len(words) >= 1:
        pieces = [f"release=0 file=0x{words[0] & 0xFFFF:04X}"]
        if words[0] >> 16:
            pieces.append(f"raw_high=0x{words[0] >> 16:04X}")
        rest = words[1:]
        return (
            f"  packing_file_load_or_release {' '.join(pieces)}"
            f"{source_words_suffix(rest) if rest else ''}"
            f"{source_arg_suffix(arg) if arg else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0x78 and not words:
        value = arg & 0x0F
        return (
            f"  sound_field_e0_set value={value}"
            f"{source_arg_suffix(arg) if arg != value else ''}{source_flags_suffix(flags)}"
        )
    if opcode == 0xA0:
        mode = arg & 0x3F
        bit7 = (arg >> 7) & 0x01
        default_arg = mode | (bit7 << 7)
        return (
            f"  battle_character_fall_or_plugin_control mode={mode} bit7={bit7}"
            f"{source_words_suffix(words) if words else ''}"
            f"{source_arg_suffix(arg) if arg != default_arg else ''}{source_flags_suffix(flags)}"
        )

    alias = SOURCE_OPCODE_ALIASES_BY_OPCODE.get(opcode)
    if alias:
        fields = [alias]
    else:
        fields = [".cmd", f"op=0x{opcode:02X}"]
    if arg:
        fields.append(f"arg=0x{arg:02X}")
    if flags != 0x80:
        fields.append(f"flags=0x{flags:02X}")
    # Always carry the words= marker (even empty) so forms with a structured
    # compile branch can tell this raw line from a truncated structured one.
    fields.append(source_words_field(words))
    return "  " + " ".join(fields)

def decode_marker_table_source(data: bytes, header_extra: int) -> dict[str, Any] | None:
    if not header_extra:
        return None
    offset = header_extra * 4
    if offset < 0x0C or offset + 4 > len(data) or offset % 4:
        return None
    count = int.from_bytes(data[offset : offset + 2], "little")
    size = 4 + count * 4
    if offset + size > len(data):
        return None
    targets: list[int] = []
    for index in range(count):
        target = u32(data, offset + 4 + index * 4) * 4
        if target < 0x0C or target >= len(data) or target % 4:
            return None
        targets.append(target)
    return {"offset": offset, "size": size, "targets": targets}

def decompile_evd_source(
    data: bytes,
    start: int = 0x0C,
    concise: bool = False,
    symbols: dict[str, dict[int, str]] | None = None,
) -> str:
    evd_offset = locate_evd(data)
    if evd_offset != 0:
        raise ValueError("source decompiler currently expects a raw unpacked EVD payload")
    if len(data) < start:
        raise ValueError(f"EVD is shorter than command start 0x{start:X}")
    header_value = u32(data, 0x04) if len(data) >= 8 else 0
    header_extra = u32(data, 0x08) if len(data) >= 12 else 0
    marker_table = decode_marker_table_source(data, header_extra)

    commands: list[dict[str, Any]] = []
    raw_chunks: list[tuple[int, bytes]] = []
    cursor = start
    while cursor < len(data):
        if marker_table and cursor == marker_table["offset"]:
            cursor += int(marker_table["size"])
            continue
        if cursor % 4 or cursor + 4 > len(data):
            raw_chunks.append((cursor, data[cursor:]))
            break
        command = decode_command_at(data, cursor)
        if command.get("truncated"):
            raw_chunks.append((cursor, data[cursor:]))
            break
        commands.append(command)
        next_cursor = int(command["end_offset"])
        if next_cursor <= cursor:
            raw_chunks.append((cursor, data[cursor : cursor + 4]))
            cursor += 4
        else:
            cursor = next_cursor

    command_offsets = {int(command["offset"]) for command in commands}
    label_offsets = {start}
    if marker_table:
        label_offsets.update(int(target) for target in marker_table["targets"])
    for command in commands:
        if int(command["opcode"]) != 0x02:
            continue
        target = command.get("details", {}).get("branch_target_offset")
        if isinstance(target, int) and target in command_offsets:
            label_offsets.add(target)
    labels = {offset: label_name(offset) for offset in sorted(label_offsets)}

    records: list[tuple[int, str]] = []
    for command in commands:
        offset = int(command["offset"])
        if offset in labels:
            records.append((offset, f"{labels[offset]}:"))
        rendered = format_source_command(command, labels)
        # Self-check every offset-independent line: it must recompile to the
        # exact original bytes, otherwise fall back to the raw alias form.
        # Branch/target lines are offset-dependent and validated by their own
        # framing logic instead.
        if concise:
            rendered = concise_source_command(rendered, data[offset : int(command["end_offset"])])
        engine_form = rendered.split()[0] if rendered.split() else ""
        rendered = apply_parameter_aliases(engine_form, rendered)
        friendly_form = FRIENDLY_FORM_NAMES.get(engine_form)
        if friendly_form:
            rendered = rendered.replace(engine_form, friendly_form, 1)
        # Self-check every offset-independent line AFTER alias renaming (the
        # compiler consumes the aliased spelling): it must recompile to the
        # exact original bytes, otherwise fall back to the raw alias form.
        # Branch/target lines are offset-dependent and validated by their own
        # framing logic instead.
        stripped = rendered.strip()
        if not stripped.startswith(".") and "target=" not in stripped and "goto=" not in stripped:
            raw_bytes = bytes(data[offset : int(command["end_offset"])])
            simplified = simplify_character_fields(stripped)
            if simplified != stripped and source_line_selfcheck(simplified, raw_bytes):
                rendered = f"  {simplified}"
            elif not source_line_selfcheck(stripped, raw_bytes):
                rendered = raw_source_command(command, raw_bytes)
        rendered = decimalize_id_fields(rendered)
        if symbols:
            rendered = annotate_source_line(rendered, symbols)
        records.append((offset, rendered))
    for offset, chunk in raw_chunks:
        if offset in labels:
            records.append((offset, f"{labels[offset]}:"))
        for piece_offset, piece in ((offset + index, chunk[index : index + 16]) for index in range(0, len(chunk), 16)):
            records.append((piece_offset, "  .bytes " + bytes_to_hex(piece)))
    if marker_table:
        marker_offset = int(marker_table["offset"])
        marker_labels = [labels.get(int(target), label_name(int(target))) for target in marker_table["targets"]]
        records.append((marker_offset, "  .marker_table " + " ".join(marker_labels)))

    lines = [
        "; EVDSRC v1",
        "; Conservative source output. Known commands use names; unsupported bytes stay raw.",
        f".header 0x{header_value:08X}",
        f".header_extra 0x{header_extra:08X}",
        f".entry {labels.get(start, label_name(start))}",
        "",
    ]
    previous_was_label = False
    for _, line in sorted(records, key=lambda item: (item[0], 0 if item[1].endswith(":") else 1, item[1])):
        if line.endswith(":"):
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(line)
            previous_was_label = True
            continue
        lines.append(line)
        previous_was_label = False
    if previous_was_label:
        lines.append("  ; empty label")
    return "\n".join(lines).rstrip() + "\n"

class EVDCodeParser:
    def __init__(self, text: str):
        self.tokens = self.tokenize(text)
        self.index = 0
        self.label_counter = 0
        self.header_value = "3"
        self.header_extra: str | None = None
        self.entry_label = "start"
        self.output: list[str] = []
        # The EVDCODE line each lowered EVDSRC line came from. Without it a
        # compile error reports a line number in the intermediate text, which
        # points at nothing an author can see.
        self.line_map: list[int] = []
        self.current_line = 1
        self.source_line_of: dict[int, int] = {}

    @classmethod
    def scratch(cls) -> "EVDCodeParser":
        """A parser with no input, for running one emitter in isolation.

        Macro recognition emits a candidate and compares bytes, which needs the
        emitters but not the tokenizer. Everything an emitter touches is set
        here, so adding parser state cannot silently break recognition: a
        missing field would raise inside the emitter and be read as "this macro
        does not match".
        """
        parser = cls.__new__(cls)
        parser.tokens = [("eof", "", 0)]
        parser.index = 0
        parser.label_counter = 0
        parser.header_value = "3"
        parser.header_extra = None
        parser.entry_label = "start"
        parser.output = []
        parser.line_map = []
        parser.current_line = 1
        parser.source_line_of = {}
        return parser

    @staticmethod
    def tokenize(text: str) -> list[tuple[str, str, int]]:
        token_re = re.compile(
            r"""
            (?P<space>\s+)
          | (?P<comment>//[^\n]*)
          | (?P<string>"(?:\\.|[^"\\])*")
          | (?P<number>0x[0-9A-Fa-f]+|-?\d+(?:\.\d+)?)
          | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
          | (?P<symbol>==|!=|[{}(),=!;])
          | (?P<other>.)
            """,
            re.VERBOSE,
        )
        tokens: list[tuple[str, str, int]] = []
        # Track the line number incrementally: counting newlines from the
        # start of the text per token is quadratic on large scripts.
        line_no = 1
        cursor = 0
        for match in token_re.finditer(text):
            kind = match.lastgroup or "other"
            value = match.group()
            line_no += text.count("\n", cursor, match.start())
            cursor = match.start()
            if kind in ("space", "comment"):
                continue
            if kind == "other":
                raise ValueError(f"line {line_no}: unexpected character {value!r}")
            tokens.append((kind, value, line_no))
        line_no += text.count("\n", cursor)
        tokens.append(("eof", "", line_no))
        return tokens

    def peek(self) -> tuple[str, str, int]:
        return self.tokens[self.index]

    def match(self, value: str) -> bool:
        if self.peek()[1] == value:
            self.index += 1
            return True
        return False

    def expect_value(self, value: str) -> tuple[str, str, int]:
        token = self.peek()
        if token[1] != value:
            raise ValueError(f"line {token[2]}: expected {value!r}, got {token[1]!r}")
        self.index += 1
        return token

    def expect_kind(self, kind: str) -> tuple[str, str, int]:
        token = self.peek()
        if token[0] != kind:
            raise ValueError(f"line {token[2]}: expected {kind}, got {token[1]!r}")
        self.index += 1
        return token

    def new_label(self, prefix: str) -> str:
        self.label_counter += 1
        return f"_{prefix}_{self.label_counter:04d}"

    def parse(self) -> str:
        self.expect_value("event")
        name = self.expect_kind("ident")[1]
        self.expect_value("{")
        self.output = []
        self.line_map = []
        self.parse_block_body()
        self.expect_value("}")
        if self.peek()[0] != "eof":
            token = self.peek()
            raise ValueError(f"line {token[2]}: unexpected token after event block: {token[1]!r}")
        if not any(line.strip().split(" ", 1)[0] == "end_script" for line in self.output):
            self.emit("end_script")
        header = [
            "; EVDCODE lowered to EVDSRC v1",
            f"; event {name}",
            f".header {self.header_value}",
        ]
        if self.header_extra is not None:
            header.append(f".header_extra {self.header_extra}")
        elif not any(line.strip().startswith(".marker_table") for line in self.output):
            # No marker table to derive it from; keep the explicit default.
            header.append(".header_extra 0x00000000")
        header.extend([
            f".entry {self.entry_label}",
            "",
            "start:",
        ])
        self.source_line_of = {
            len(header) + index + 1: line for index, line in enumerate(self.line_map)
        }
        return "\n".join(header + self.output).rstrip() + "\n"

    def parse_block_body(self) -> None:
        while self.peek()[0] != "eof" and self.peek()[1] != "}":
            self.parse_statement()

    def parse_statement(self) -> None:
        token = self.peek()
        self.current_line = token[2]
        if token[1] == "if":
            self.parse_if()
            self.match(";")
            return
        if token[1] == "choose":
            self.parse_choose()
            self.match(";")
            return
        if token[0] == "ident":
            self.parse_call_statement()
            self.match(";")
            return
        raise ValueError(f"line {token[2]}: expected statement, got {token[1]!r}")

    def parse_choose(self) -> None:
        """choose (lineId, ...) { option { ... } option { ... } }

        Lowers to the shipped-game choice choreography: a choice-mode window,
        a wait loop polling event value 18 (where the engine stores the picked
        option), a branch table, then one block per option. Unlike the
        original community tooling, every option block ends with an explicit
        jump past the remaining options, so blocks can never fall through
        into each other.
        """
        self.expect_value("choose")
        self.expect_value("(")
        args, kwargs = self.parse_call_args()
        self.expect_value(")")
        if len(args) != 1:
            token = self.peek()
            raise ValueError(f"line {token[2]}: choose() expects one RMF line id")
        line_id = parse_hex_int(args[0])
        box_colour = parse_hex_int(kwargs.pop("box_colour", kwargs.pop("boxColour", kwargs.pop("boxColor", "0"))))
        box_image = parse_hex_int(kwargs.pop("box_image", kwargs.pop("boxImage", "0")))
        sfx = parse_hex_int(kwargs.pop("sfx", "6"))
        if kwargs:
            token = self.peek()
            raise ValueError(f"line {token[2]}: choose() unknown argument(s): {', '.join(sorted(kwargs))}")
        if not 0 <= line_id <= 0xFFFF or not 0 <= box_colour <= 0xFF or not 0 <= box_image <= 0xFF or not 0 <= sfx <= 0xFF:
            token = self.peek()
            raise ValueError(f"line {token[2]}: choose() argument out of range")
        self.emit(
            "window_command mode=0x01 window=0x0009 message=0x0007 subdispatch=1 "
            f"params=0x{((line_id & 0xFFFF) << 16) | 0x0002:08X},0x00000064,0x00000000,"
            f"0x{0x00030000 | (box_image << 8) | box_colour:08X},0x{sfx << 16:08X},"
            "0x0000FEC0,0x00000000,0x00000140"
        )
        wait_label = self.new_label("choose_wait")
        self.emit_label(wait_label)
        self.emit("nop flags=0x00")
        self.emit("window_command mode=0x00 window=0x0009 message=0x0005 flags=0x80")
        self.emit(f"if_value event_value=18 is=0 goto={wait_label}")
        self.emit("window_command mode=0x00 window=0x0009 message=0x0002")
        # Collect option bodies first so the branch table can precede them.
        self.expect_value("{")
        option_bodies: list[tuple[list[str], list[int]]] = []
        while self.peek()[1] == "option":
            self.expect_value("option")
            self.expect_value("{")
            saved, saved_map = self.output, self.line_map
            self.output, self.line_map = [], []
            self.parse_block_body()
            option_bodies.append((self.output, self.line_map))
            self.output, self.line_map = saved, saved_map
            self.expect_value("}")
            self.match(";")
        self.expect_value("}")
        if not option_bodies:
            token = self.peek()
            raise ValueError(f"line {token[2]}: choose() needs at least one option {{ ... }} block")
        end_label = self.new_label("choose_end")
        option_labels = [self.new_label(f"choice_{index + 1}") for index in range(len(option_bodies))]
        for index, label in enumerate(option_labels):
            self.emit(f"if_value event_value=18 is={index + 1} goto={label}")
        self.emit(f"jump goto={end_label}")
        for label, (body, body_map) in zip(option_labels, option_bodies):
            self.emit_label(label)
            self.output.extend(body)
            self.line_map.extend(body_map)
            self.emit(f"jump goto={end_label}")
        self.emit_label(end_label)

    def parse_if(self) -> None:
        self.expect_value("if")
        condition_fields = self.parse_condition_expression()
        then_label = self.new_label("then")
        else_label = self.new_label("else")
        end_label = self.new_label("endif")
        self.emit(f"branch target={then_label} {' '.join(condition_fields)}")
        self.emit(f"branch target={else_label} condition=0")
        self.emit_label(then_label)
        self.expect_value("{")
        self.parse_block_body()
        self.expect_value("}")
        has_else = self.peek()[1] == "else"
        if has_else:
            self.emit(f"branch target={end_label} condition=0")
        self.emit_label(else_label)
        if has_else:
            self.expect_value("else")
            self.expect_value("{")
            self.parse_block_body()
            self.expect_value("}")
            self.emit_label(end_label)

    def parse_condition_expression(self) -> list[str]:
        invert = False
        if self.match("!"):
            invert = True
        name = self.expect_kind("ident")[1]
        self.expect_value("(")
        args, kwargs = self.parse_call_args()
        self.expect_value(")")
        if name == "flag":
            if len(args) != 1 or kwargs:
                token = self.peek()
                raise ValueError(f"line {token[2]}: flag() condition expects one positional flag id")
            flag_id = parse_hex_int(args[0])
            if not 0 <= flag_id <= 0xFFFF:
                token = self.peek()
                raise ValueError(f"line {token[2]}: flag id out of range")
            condition = 0x81 if invert else 0x01
            return [
                f"condition=0x{condition:02X}",
                "compare=eq",
                "compare_from_event=0",
                "value=1",
                f"first_flag=0x{flag_id:04X}",
                "flag_count=1",
            ]
        if name in ("recruit", "party"):
            if len(args) != 1:
                token = self.peek()
                raise ValueError(f"line {token[2]}: {name}() condition expects one character id")
            character = parse_hex_int(args[0])
            if not 0 <= character <= 0xFFFF:
                token = self.peek()
                raise ValueError(f"line {token[2]}: character id out of range")
            condition = 0x87 if invert and name == "recruit" else 0x07 if name == "recruit" else 0x88 if invert else 0x08
            if name == "recruit":
                friend_book = kwargs.pop("friendBook", kwargs.pop("friend_book", "false"))
                if kwargs:
                    token = self.peek()
                    raise ValueError(f"line {token[2]}: recruit() unknown argument(s): {', '.join(sorted(kwargs))}")
                property_selector = 0x48 if self.parse_bool(friend_book) else 0x0D
                return [
                    f"condition=0x{condition:02X}",
                    "compare=eq",
                    "compare_from_event=0",
                    "value=0",
                    f"character_word=0x{character:04X}",
                    f"property_selector=0x{property_selector:02X}",
                ]
            if kwargs:
                token = self.peek()
                raise ValueError(f"line {token[2]}: party() does not accept named arguments")
            return [
                f"condition=0x{condition:02X}",
                f"character_word=0x{character:04X}",
                "character_source=0",
                "category_selector=1",
            ]
        if name == "condition":
            if args:
                token = self.peek()
                raise ValueError(f"line {token[2]}: condition() only accepts named fields")
            if "condition" not in kwargs:
                token = self.peek()
                raise ValueError(f"line {token[2]}: condition() requires condition=...")
            fields = [f"{key}={value}" for key, value in kwargs.items()]
            if invert:
                raw_condition = parse_hex_int(kwargs["condition"])
                fields = [f"condition=0x{(raw_condition ^ 0x80) & 0xFF:02X}" if item.startswith("condition=") else item for item in fields]
            return fields
        token = self.peek()
        raise ValueError(f"line {token[2]}: unsupported condition {name}(); use flag(id) or condition(...)")

    def parse_call_statement(self) -> None:
        name = self.expect_kind("ident")[1]
        self.expect_value("(")
        args, kwargs = self.parse_call_args()
        self.expect_value(")")
        if name in BRANCH_BLOCK_HEADS and self.peek()[1] == "{":
            self.parse_branch_block(name, args, kwargs)
            return
        self.emit_call(name, args, kwargs)

    def parse_branch_block(self, name: str, args: list, kwargs: dict) -> None:
        """Structured branch block: `if_value(cond) { A } [else { B }]`.

        Lowers to the single inverted-comparator branch shape used by the
        game's own scripts: branch-past-the-block when the condition fails,
        so recompiled blocks are byte-identical to the goto form.
        """
        token = self.peek()
        if args:
            raise ValueError(f"line {token[2]}: {name} block takes key=value arguments only")
        if "goto" in kwargs:
            raise ValueError(f"line {token[2]}: {name} block cannot also carry goto=")
        negated = dict(kwargs)
        cmp_keys = [key for key in kwargs if key in BRANCH_COMPARE_SELECTORS]
        if len(cmp_keys) != 1:
            raise ValueError(f"line {token[2]}: {name} block needs exactly one comparator argument")
        cmp_key = cmp_keys[0]
        value = negated.pop(cmp_key)
        negated[NEGATED_COMPARATORS[cmp_key]] = value
        skip_label = self.new_label("endif")
        pairs = " ".join(f"{key}={val}" for key, val in negated.items())
        self.emit(f"{name} {pairs} goto={skip_label}")
        self.expect_value("{")
        self.parse_block_body()
        self.expect_value("}")
        if self.peek()[1] == "else":
            self.expect_value("else")
            end_label = self.new_label("endif")
            self.emit(f"branch target={end_label} condition=0")
            self.emit_label(skip_label)
            self.expect_value("{")
            self.parse_block_body()
            self.expect_value("}")
            self.emit_label(end_label)
        else:
            self.emit_label(skip_label)

    def parse_call_args(self) -> tuple[list[str], dict[str, str]]:
        args: list[str] = []
        kwargs: dict[str, str] = {}
        if self.peek()[1] == ")":
            return args, kwargs
        while True:
            if self.peek()[0] == "ident" and self.tokens[self.index + 1][1] == "=":
                key = self.expect_kind("ident")[1]
                self.expect_value("=")
                kwargs[key] = self.parse_value()
            else:
                if kwargs:
                    token = self.peek()
                    raise ValueError(f"line {token[2]}: positional arguments cannot follow named arguments")
                args.append(self.parse_value())
            if not self.match(","):
                break
        return args, kwargs

    def parse_value(self) -> str:
        token = self.peek()
        if token[0] in ("number", "string"):
            self.index += 1
            return token[1]
        if token[0] == "ident":
            self.index += 1
            return token[1]
        raise ValueError(f"line {token[2]}: expected value, got {token[1]!r}")

    def emit(self, text: str) -> None:
        self.output.append(f"  {text}")
        self.line_map.append(self.current_line)

    def emit_label(self, label: str) -> None:
        self.output.append("")
        self.output.append(f"{label}:")
        self.line_map.extend([self.current_line, self.current_line])

    def emit_call(self, name: str, args: list[str], kwargs: dict[str, str]) -> None:
        directive = self.normalize_call_name(name)
        if directive == "header":
            if len(args) != 1 or kwargs:
                raise ValueError("header() expects one value")
            self.header_value = args[0]
            return
        if directive == "header_extra":
            if len(args) != 1 or kwargs:
                raise ValueError("headerExtra() expects one value")
            self.header_extra = args[0]
            return
        if directive == "entry":
            if len(args) != 1 or kwargs:
                raise ValueError("entry() expects one label")
            self.entry_label = self.source_label_arg(args[0])
            return
        if directive == "label":
            if len(args) != 1 or kwargs:
                raise ValueError("label() expects one label")
            self.emit_label(self.source_label_arg(args[0]))
            return
        if directive == "bytes":
            if kwargs:
                raise ValueError("bytes() only accepts positional byte values")
            if len(args) == 1 and args[0].startswith('"'):
                self.emit(".bytes " + parse_source_string(args[0], self.peek()[2], "bytes"))
            else:
                self.emit(".bytes " + " ".join(args))
            return
        if directive == "word":
            if not args or kwargs:
                raise ValueError("word() expects one or more positional words")
            self.emit(".word " + " ".join(args))
            return
        if directive == "cmd":
            if args:
                raise ValueError("cmd() only accepts named arguments")
            fields = " ".join(f"{key}={self.source_field_value(key, value)}" for key, value in kwargs.items())
            self.emit(f".cmd{(' ' + fields) if fields else ''}")
            return
        if directive == "marker_table":
            if not args or kwargs:
                raise ValueError("markerTable() expects one or more labels")
            self.emit(".marker_table " + " ".join(self.source_label_arg(arg) for arg in args))
            return
        if directive in ("msg", "message"):
            if len(args) != 1 or kwargs:
                raise ValueError(f"{name}() expects one string argument")
            parse_source_string(args[0], self.peek()[2], f"{name} text")
            self.emit(f"text_output mode=0 text={args[0]}")
            return
        if directive in ("display_text", "screen_text"):
            if len(args) != 1:
                raise ValueError(f"{name}() expects text as its first argument")
            parse_source_string(args[0], self.peek()[2], f"{name} text")
            x = kwargs.pop("x", "0")
            y = kwargs.pop("y", "0")
            word00 = kwargs.pop("word00", "0x80808080")
            word08 = kwargs.pop("word08", "0x00000000")
            flags = kwargs.pop("flags", "0x80")
            if kwargs:
                raise ValueError(f"{name}() unknown argument(s): {', '.join(sorted(kwargs))}")
            self.emit(f"text_message_layout x={x} y={y} word00={word00} word08={word08} flags={flags}")
            self.emit(f"text_output mode=0 text={args[0]} flags={flags}")
            return
        if directive == "wait":
            if len(args) != 1 or kwargs:
                raise ValueError("wait() expects one duration argument")
            duration_word = f32_to_u32(float(args[0]))
            self.emit(f"trigger action=0 type=0x01 raw_mid=0x00 trigger_flags=0x0000 trigger_value=0x{duration_word:08X}")
            return
        if directive == "talk_wait":
            if args or kwargs:
                raise ValueError("talkWait() takes no arguments")
            self.emit("trigger action=0 type=0x04 raw_mid=0x00 trigger_flags=0x0000")
            return
        if directive == "talk":
            if len(args) != 1:
                raise ValueError("talk() expects one RMF line id")
            wait = self.parse_bool(kwargs.pop("wait", "true"))
            if kwargs:
                raise ValueError(f"talk() unknown argument(s): {', '.join(sorted(kwargs))}")
            line_id = parse_hex_int(args[0])
            if not 0 <= line_id <= 0xFFFF:
                raise ValueError("talk() line id out of range")
            self.emit(f"talk_rmf attach_mode=2 flag0=0 flag1=0 message=0x{line_id:04X} raw_high=0x0000 flags=0x80")
            if wait:
                self.emit("trigger action=0 type=0x04 raw_mid=0x00 trigger_flags=0x0000")
            return
        if directive == "textbox":
            if len(args) != 1:
                raise ValueError("textbox() expects one RMF line id")
            line_id = parse_hex_int(args[0])
            box_colour = parse_hex_int(kwargs.pop("box_colour", kwargs.pop("boxColour", kwargs.pop("boxColor", "0"))))
            box_image = parse_hex_int(kwargs.pop("box_image", kwargs.pop("boxImage", "0")))
            sfx = parse_hex_int(kwargs.pop("sfx", "6"))
            if kwargs:
                raise ValueError(f"textbox() unknown argument(s): {', '.join(sorted(kwargs))}")
            if not 0 <= line_id <= 0xFFFF or not 0 <= box_colour <= 0xFF or not 0 <= box_image <= 0xFF or not 0 <= sfx <= 0xFF:
                raise ValueError("textbox() argument out of range")
            self.emit(
                "window_command mode=0x01 window=0x0009 message=0x0007 subdispatch=1 "
                f"params=0x{((line_id & 0xFFFF) << 16) | 0x0002:08X},0x00000064,0x00000000,"
                f"0x{0x00010000 | (box_image << 8) | box_colour:08X},0x{sfx << 16:08X},"
                "0x0000FEC0,0x00000000,0x00000140"
            )
            wait_label = self.new_label("textbox_wait")
            self.emit_label(wait_label)
            self.emit("nop flags=0x00")
            self.emit("window_command mode=0x00 window=0x0009 message=0x0005 flags=0x80")
            self.emit(
                f"branch target={wait_label} condition=0x02 compare=eq compare_from_event=0 "
                "value=0 event_value=18 flags=0x80"
            )
            self.emit("window_command mode=0x00 window=0x0009 message=0x0002")
            close_label = self.new_label("textbox_close")
            self.emit_label(close_label)
            self.emit("nop flags=0x00")
            self.emit(
                f"branch target={close_label} condition=0x02 compare=eq compare_from_event=0 "
                "value=0 event_value=18"
            )
            return
        if directive in ("fade_out", "hide_screen"):
            if args:
                raise ValueError(f"{name}() only accepts named arguments")
            wait = self.parse_bool(kwargs.pop("wait", "true"))
            white = self.parse_bool(kwargs.pop("white", "false"))
            if kwargs:
                raise ValueError(f"{name}() unknown argument(s): {', '.join(sorted(kwargs))}")
            control = "0x1013" if white else "0x0013"
            self.emit(f"fade_control mode=0 fade_flags=0x0 id=0x003C control={control} flags=0x80")
            if wait:
                self.emit("trigger action=0 type=0x03 raw_mid=0x00 trigger_flags=0x0000")
            return
        if directive in ("fade_in", "show_screen"):
            if args:
                raise ValueError(f"{name}() only accepts named arguments")
            wait = self.parse_bool(kwargs.pop("wait", "true"))
            # Control bits 12-15 pick the preset colour, exactly as they do for
            # the matching fade_out; 1 is the near-white preset.
            white = self.parse_bool(kwargs.pop("white", "false"))
            if kwargs:
                raise ValueError(f"{name}() unknown argument(s): {', '.join(sorted(kwargs))}")
            control = "0x1010" if white else "0x0010"
            self.emit(f"fade_control mode=0 fade_flags=0x0 id=0x003C control={control} flags=0x80")
            if wait:
                self.emit("trigger action=0 type=0x03 raw_mid=0x00 trigger_flags=0x0000")
            return
        if directive in ("play_movie", "play_fmv"):
            if not args and kwargs:
                # a decompiled `play_movie ...` line (structured or raw data
                # region), not an authoring call
                fields = " ".join(f"{key}={self.source_field_value(key, value)}" for key, value in kwargs.items())
                self.emit(f"play_movie {fields}")
                return
            if len(args) != 1:
                raise ValueError(f"{name}() expects one movie id")
            wait = self.parse_bool(kwargs.pop("wait", "true"))
            extra = kwargs.pop("extra", "0x00000009")
            flags = kwargs.pop("flags", "0x80")
            if kwargs:
                raise ValueError(f"{name}() unknown argument(s): {', '.join(sorted(kwargs))}")
            movie = parse_hex_int(args[0])
            if not 0 <= movie <= 0xFFFF:
                raise ValueError(f"{name}() movie id out of range")
            self.emit(f"play_movie movie=0x{movie:04X} param0=0 param1=0 extra={extra} flags={flags}")
            if wait:
                self.emit("trigger action=0 type=0x0B raw_mid=0x00 trigger_flags=0x0000")
            return
        if directive in ("play_music", "change_music"):
            if not args and kwargs:
                # a decompiled `play_music mode=... flags=...` line, not an authoring call
                fields = " ".join(f"{key}={self.source_field_value(key, value)}" for key, value in kwargs.items())
                self.emit(f"play_bgm{(' ' + fields) if fields else ''}")
                return
            if len(args) != 1 or kwargs:
                raise ValueError(f"{name}() expects one music id")
            music = parse_hex_int(args[0])
            if not 0 <= music <= 0xFFFF:
                raise ValueError(f"{name}() music id out of range")
            self.emit(f"set_bgm info0=0x{0x61A80000 | music:08X} info1=0x00000000 arg=0x02 flags=0x80")
            self.emit("play_bgm mode=2 flags=0x80")
            return
        if directive == "load_sound_effects":
            if len(args) != 1 or kwargs:
                raise ValueError(f"{name}() expects one sound resource file id")
            resource_id = parse_hex_int(args[0])
            if not 0 <= resource_id <= 0xFFFFFFFF:
                raise ValueError(f"{name}() resource id out of range")
            self.emit(f"load_sound_resource mode=9 resource_id=0x{resource_id:08X} flags=0x80")
            return
        if directive in ("play_se", "play_sound"):
            if not args and kwargs:
                fields = " ".join(f"{key}={self.source_field_value(key, value)}" for key, value in kwargs.items())
                self.emit(f"play_sound_effect{(' ' + fields) if fields else ''}")
                return
            if len(args) != 1:
                raise ValueError(f"{name}() expects one sound id")
            sound_id = parse_hex_int(args[0])
            arg5 = parse_hex_int(kwargs.pop("arg5", "0"))
            arg6 = parse_hex_int(kwargs.pop("arg6", "80"))
            arg7 = parse_hex_int(kwargs.pop("arg7", "64"))
            control_high = kwargs.pop("control_high", "0x0020")
            flags = kwargs.pop("flags", "0x80")
            if kwargs:
                raise ValueError(f"{name}() unknown argument(s): {', '.join(sorted(kwargs))}")
            if not 0 <= sound_id <= 0xFFFFFFFF:
                raise ValueError(f"{name}() sound id out of range")
            self.emit(
                "play_sound_effect mode=0 submode=0 explicit_char=0 "
                f"control_high={control_high} playse_stack0=0 control_index=0 control_flag_bits=1 "
                f"sound_id=0x{sound_id:08X} playse_arg5={arg5} playse_arg6={arg6} playse_arg7={arg7} flags={flags}"
            )
            return
        if directive == "set_flag":
            self.emit_flag_set(args, kwargs, 1, name)
            return
        if directive == "clear_flag":
            self.emit_flag_set(args, kwargs, 0, name)
            return
        if directive in ("init_char", "load_char", "place_char", "move_char", "remove_char", "spawn_char", "give_item"):
            self.emit_stage_macro(directive, name, args, kwargs)
            return
        if directive == "end":
            if args or kwargs:
                raise ValueError("end() takes no arguments")
            self.emit("end_script")
            return
        if directive == "raw":
            if len(args) != 1 or kwargs:
                raise ValueError("raw() expects one quoted EVDSRC line")
            self.emit(parse_source_string(args[0], self.peek()[2], "raw line"))
            return
        if (
            directive in HIGH_LEVEL_COMMANDS or directive in HIGH_LEVEL_NAME_ALIASES
        ) and high_level_call_matches(directive, kwargs):
            values = {key: self.source_field_value(key, value) for key, value in kwargs.items()}
            positional = [self.source_field_value("", value) for value in args]
            self.emit(build_high_level_command(directive, positional, values))
            return
        if args:
            raise ValueError(f"{name}() only supports named arguments; use raw(...) for custom EVDSRC")
        fields = " ".join(f"{key}={self.source_field_value(key, value)}" for key, value in kwargs.items())
        self.emit(f"{directive}{(' ' + fields) if fields else ''}")

    @staticmethod
    def parse_bool(value: str) -> bool:
        lowered = value.lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"expected boolean value, got {value!r}")

    @staticmethod
    def source_label_arg(value: str) -> str:
        if value.startswith('"'):
            return parse_source_string(value, 0, "label")
        return value

    @staticmethod
    def source_field_value(key: str, value: str) -> str:
        if key in {"words", "params", "values"} and value.startswith('"'):
            return parse_source_string(value, 0, key)
        if value.startswith('"'):
            parsed = parse_source_string(value, 0, key)
            if key != "text" and parsed == "":
                return parsed
            if key != "text" and ("," in parsed or ":" in parsed):
                return parsed
            if re.fullmatch(r"[-+0-9A-Fa-fxX.,]+", parsed):
                return parsed
        return value

    @staticmethod
    def normalize_call_name(name: str) -> str:
        chars: list[str] = []
        for index, char in enumerate(name):
            if char.isupper() and index:
                chars.append("_")
            chars.append(char.lower())
        return "".join(chars)

    def macro_character_word(self, value: str, scene: str) -> int:
        """Pack a character id (or unique name) with a 16-bit scene-instance id."""
        text = value
        if text.startswith('"'):
            text = parse_source_string(text, self.peek()[2], "character")
        char = symbol_reverse_tables().get("character", {}).get(text.strip().lower())
        if char is None:
            char = parse_hex_int(text)
        scene_id = parse_hex_int(scene)
        if not 0 <= char <= 0xFFFF or not 0 <= scene_id <= 0xFFFF:
            raise ValueError("character id and scene id must fit 16 bits")
        return char | (scene_id << 16)

    def emit_stage_macro(self, directive: str, name: str, args: list[str], kwargs: dict[str, str]) -> None:
        """Composite scene-staging recipes, ported from the community RadiataEvent
        tooling but emitted through the validated EVDSRC forms."""
        if directive == "give_item":
            if len(args) != 1 or kwargs:
                raise ValueError(f"{name}() expects one item id or name")
            text = args[0]
            if text.startswith('"'):
                text = parse_source_string(text, self.peek()[2], "item")
            item = symbol_reverse_tables().get("item", {}).get(text.strip().lower())
            if item is None:
                item = parse_hex_int(text)
            if not 0 <= item <= 0xFFFF:
                raise ValueError("give_item() item id out of range")
            self.emit("window_command mode=0x00 window=0x0018 message=0x0001 yield=1")
            self.emit(f"window_command mode=0x01 window=0x0018 message=0x0007 subdispatch=1 params=0x{0x00010000 | item:08X}")
            open_label = self.new_label("item_open")
            self.emit_label(open_label)
            self.emit("nop flags=0x00")
            self.emit("window_command mode=0x00 window=0x0018 message=0x0005")
            self.emit(f"if_value event_value=18 is=-1 goto={open_label} yield=1")
            self.emit("window_command mode=0x00 window=0x0018 message=0x000B")
            shown_label = self.new_label("item_shown")
            self.emit_label(shown_label)
            self.emit("nop flags=0x00")
            self.emit("window_command mode=0x00 window=0x0018 message=0x0005")
            self.emit(f"unless_value event_value=18 is=1 goto={shown_label} yield=1")
            self.emit("window_command mode=0x00 window=0x0018 message=0x000C")
            close_label = self.new_label("item_close")
            self.emit_label(close_label)
            self.emit("nop flags=0x00")
            self.emit("window_command mode=0x00 window=0x0018 message=0x0005")
            self.emit(f"unless_value event_value=18 is=-1 goto={close_label} yield=1")
            self.emit("window_command mode=0x00 window=0x0018 message=0x0002")
            return
        if not args:
            raise ValueError(f"{name}() expects the character as its first argument")
        scene = kwargs.pop("scene", kwargs.pop("sceneId", "0"))
        word = self.macro_character_word(args[0], scene)
        char_text = f"0x{word:08X}"
        if directive == "remove_char":
            if len(args) != 1 or kwargs:
                raise ValueError(f"{name}() expects only the character (and scene=)")
            self.emit(
                f"delete_character explicit_char=1 flag7=0 character={char_text} "
                "control=0x00000001 delete=1 detach_data_mask=0x00000000"
            )
            return
        if directive in ("init_char", "spawn_char"):
            animation = parse_hex_int(kwargs.pop("animation", "1"))
            model = parse_hex_int(kwargs.pop("model", "0xFF"))
            has_animation = self.parse_bool(
                kwargs.pop("has_animation", kwargs.pop("hasAnimation", "true"))
            )
            self.emit(
                f"set_person_schedule explicit_char=1 character={char_text} "
                "schedule_low16=0x0000 schedule_arg_byte=0x05 raw_high=0x00"
            )
            self.emit(
                f"detach_character_from_parent explicit_char=1 flag4=0 flag6=0 flag7=0 character={char_text} "
                "detach_flag_low=0 character_parent_detach_flag_high=0 flag7_preserved=0"
            )
            if has_animation:
                self.emit(
                    f"load_character explicit_char=1 mode=0 character={char_text} "
                    "control=0x00000021 "
                    f"modeling=0x{0x00010100 | model:08X} "
                    f"animations=3:0x{0x00010000 | (0x1400 | animation):08X}"
                )
            else:
                self.emit(
                    f"load_character explicit_char=1 mode=0 character={char_text} "
                    "control=0x00000001 "
                    f"modeling=0x{0x00010100 | model:08X}"
                )
            if directive == "init_char":
                if kwargs:
                    raise ValueError(f"{name}() unknown argument(s): {', '.join(sorted(kwargs))}")
                return
        if directive in ("load_char", "spawn_char"):
            self.emit(
                f"set_event_leave mode=0x01 count=1 release=0 post_mode=0 action=add_enter_character "
                f"character_pairs={char_text}"
            )
            self.emit(
                f"set_character_render explicit_char=1 flag1=0 flag2=0 flag6=0 flag7=0 character={char_text} "
                "sub_manager_flag=0 byte0a_bit1=1 no_render_with_child=0 no_render=0"
            )
            self.emit(f"precreate_character_animation explicit_char=1 mode=1 character={char_text} anim_id=0x00 raw_high=0x000000")
            self.emit(
                f"rotate_character explicit_char=1 character={char_text} mode=8 action=option_head_angle option=1 "
                "target_char_from_stream=0 name_source=0 postprocess_mode=0 vector_mask=3 position_offset=0 "
                "posture_offset=0 speed_limit=0 bit13=0 control=0x000102C8 initial_vec=x:0,y:0 head_angle_add=x:0,y:0"
            )
            self.emit(
                f"set_character_expression explicit_char=1 character={char_text} expression=0x00 blink=0x07 "
                "mouth=0x16 raw0_high=0x00 mouth_arg0=0xFF mouth_arg1=0xFF blink_half_steps=0x00 raw1_high=0x00"
            )
            self.emit(
                f"character_auto_rate_visual_animation arg=0x01 "
                f"words={char_text},0x6000014A,0x00000000,0x3F800000,0x3F800000,0x3F800000,0x3F800000"
            )
            self.emit(
                f"set_character_eyes explicit_char=1 eye_ball=0 eye_move=1 character={char_text} eye_ball_byte=0x00 "
                "eye_ball_no=0x0 move_selector=0x03 move_action=manual_vector manual_x_s8=0 manual_y_s8=0 "
                "manual_time_word=0x00000000 manual_time=0"
            )
            self.emit(
                f"set_character_eyes explicit_char=1 eye_ball=0 eye_move=1 character={char_text} eye_ball_byte=0x00 "
                "eye_ball_no=0x0 move_selector=0x05 move_action=set_type_4 manual_x_s8=0 manual_y_s8=0"
            )
            self.emit(f"pause_character_move explicit_char=1 pause_arg=2 character={char_text}")
            self.emit(
                f"play_character_sub_animation explicit_char=1 handler_mode=0 action=virtual_anim_sub_control "
                f"character={char_text}"
            )
            self.emit(
                f"character_auto_rate_visual_animation arg=0x03 "
                f"words={char_text},0xA0020000,0x00000000,0x44414853,0x0000574F,0x00000000,0x00000000,0x3F800000"
            )
            self.emit(
                f"play_character_animation explicit_char=1 speed0=0 speed1=0 blend=0 speed2=0 has_extra_animation=0 "
                f"sub_anim_mode=0 character={char_text} animation_group=0x00000001 animation=0x00000001"
            )
            if directive == "load_char":
                if kwargs:
                    raise ValueError(f"{name}() unknown argument(s): {', '.join(sorted(kwargs))}")
                return
        if directive in ("place_char", "spawn_char"):
            if directive == "place_char" and len(args) < 4:
                raise ValueError(f"{name}() expects character, x, y, z")
            floats = args[1:4] if len(args) >= 4 else ["0", "0", "0"]
            x, y, z = (format_f32(u32_to_f32(f32_to_u32(float(v)))) for v in floats)
            rotations = []
            for key, index in (("rotX", 4), ("rotY", 5), ("rotZ", 6)):
                # rotX / rotx / rot_x all mean the same thing; snake_case is the
                # spelling the tool prints, the others stay accepted.
                snake = f"rot_{key[-1].lower()}"
                text = kwargs.pop(key, kwargs.pop(key.lower(), kwargs.pop(snake, None)))
                if text is None and len(args) > index:
                    text = args[index]
                degrees = min(max(float(text or "0"), 0.0), 360.0)
                rotations.append(format_f32(u32_to_f32(f32_to_u32(degrees / 360.0 * 2.0 * math.pi))))
            if kwargs:
                raise ValueError(f"{name}() unknown argument(s): {', '.join(sorted(kwargs))}")
            self.emit(
                f"set_character_render explicit_char=1 flag1=0 flag2=0 flag6=1 flag7=0 character={char_text} "
                "sub_manager_flag=0 byte0a_bit1=1 no_render_with_child=1 no_render=0"
            )
            self.emit(f"set_motion_blend explicit_char=1 character={char_text} blend_word=0x41000000 blend=8")
            self.emit(
                f"set_character_position explicit_char=1 mode=0 coord=0 source=4 character={char_text} "
                f"control=0x00010000 duration=0 control_high=0x0001 terrain_y=1 move_flag1=0 duration_as_speed=0 "
                f"action=snap inline_vec={x},{y},{z}"
            )
            self.emit(
                f"rotate_character explicit_char=1 character={char_text} mode=8 action=rotate_target_posture option=0 "
                "target_char_from_stream=0 name_source=0 postprocess_mode=0 vector_mask=7 position_offset=0 "
                "posture_offset=0 speed_limit=0 bit13=0 control=0x000101C8 initial_vec=x:0,y:0,z:0 "
                f"target_posture_vec=x:{rotations[0]},y:{rotations[1]},z:{rotations[2]}"
            )
            self.emit(
                f"set_character_attribute explicit_char=1 character={char_text} collision_attr=0x00000004 "
                "collision_value=0x00000000 set_attr2=1 yield=1"
            )
            return
        if directive == "move_char":
            if len(args) < 4:
                raise ValueError(f"{name}() expects character, x, y, z")
            wait = self.parse_bool(kwargs.pop("wait", "true"))
            duration = parse_hex_int(kwargs.pop("duration", "1000"))
            if kwargs:
                raise ValueError(f"{name}() unknown argument(s): {', '.join(sorted(kwargs))}")
            x, y, z = (int(float(v)) for v in args[1:4])
            self.emit(
                f"move_character_along_points explicit_char=1 buffer_mode=1 character={char_text} point_count=1 "
                f"points=inline:{x},{y},{z},position_source_flags=0x0000,duration_ms={duration},"
                "setpoint_arg0=0x20,setpoint_arg1=3,setpoint_arg2=0"
            )
            self.emit(
                f"control_character_movement explicit_char=1 mode=0 character={char_text} action=move_start "
                "move_control=0x00000000 duration_source=0 duration_source_name=direct duration_value=0x0000"
            )
            if wait:
                self.emit(
                    f"trigger action=0 trigger_type=0x06 raw_mid=0x00 trigger_flags=0x0002 character={char_text} yield=1"
                )
            return

    def emit_flag_set(self, args: list[str], kwargs: dict[str, str], value: int, name: str) -> None:
        kwargs = dict(kwargs)
        flag_text = kwargs.pop("flag", None)
        yield_flags = ""
        if kwargs.pop("yield", None) == "1" or kwargs.pop("flags", None) == "0x00":
            yield_flags = " flags=0x00"
        one_source = (flag_text is not None and not args) or (flag_text is None and len(args) == 1)
        if kwargs or not one_source:
            raise ValueError(f"{name}() expects one flag id (positional or flag=)")
        flag_id = parse_hex_int(flag_text if flag_text is not None else args[0])
        if not 0 <= flag_id <= 0xFFFF:
            raise ValueError(f"{name}() flag id out of range")
        self.emit(f"set_flags first=0x{flag_id:04X} count=1 values=0x{value:04X}{yield_flags}")

def compile_evd_code_to_source(text: str) -> str:
    return EVDCodeParser(text).parse()

def compile_evd_code(text: str) -> bytes:
    """Compile EVDCODE, reporting errors against the line the author wrote.

    The lowered EVDSRC is an intermediate the author never sees, so an error
    from that layer has to be translated back through the line map before it is
    worth showing. An untranslatable number is dropped rather than left
    pointing at a line that does not exist.
    """
    parser = EVDCodeParser(text)
    source_text = parser.parse()
    try:
        return compile_evd_source(source_text)
    except ValueError as exc:
        match = re.match(r"line (\d+): (.*)", str(exc), re.S)
        if not match:
            raise
        lowered_line = int(match.group(1))
        author_line = parser.source_line_of.get(lowered_line)
        if author_line is None:
            raise ValueError(match.group(2)) from exc
        raise ValueError(f"line {author_line}: {match.group(2)}") from exc

def split_source_tokens(line: str) -> list[str]:
    tokens: list[str] = []
    start: int | None = None
    in_string = False
    escaped = False
    for index, char in enumerate(line):
        if start is None:
            if char.isspace():
                continue
            start = index
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char.isspace():
            tokens.append(line[start:index])
            start = None
    if in_string:
        raise ValueError("unterminated quoted string in EVDSRC line")
    if start is not None:
        tokens.append(line[start:])
    return tokens

def code_quote(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)

def code_value_needs_quote(key: str, value: str) -> bool:
    if key in {"words", "params", "values"}:
        return True
    return not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*|0x[0-9A-Fa-f]+|-?\d+(?:\.\d+)?|\"(?:\\.|[^\"\\])*\"", value)

def source_call_to_code(directive: str, parts: list[str]) -> str:
    if directive == ".bytes":
        return f"    bytes({code_quote(' '.join(parts))})"
    if directive == ".word":
        return f"    word({', '.join(parts)})"
    if directive == ".marker_table":
        return f"    markerTable({', '.join(parts)})"
    if directive == ".cmd":
        directive = "cmd"
    fields: list[str] = []
    for part in parts:
        if "=" not in part:
            fields.append(part)
            continue
        key, value = part.split("=", 1)
        if code_value_needs_quote(key, value):
            value = code_quote(value)
        fields.append(f"{key}={value}")
    return f"    {directive}({', '.join(fields)})"

def parse_source_items(source_text: str) -> list[dict]:
    """Parse EVDSRC text into structured items for code rendering."""
    items: list[dict] = []
    for line_no, line in enumerate(source_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        note = ""
        if "  ; " in stripped:
            stripped, _, annotation = stripped.partition("  ; ")
            stripped = stripped.strip()
            note = f"  // {annotation}"
        if stripped.endswith(":") and " " not in stripped:
            items.append({"kind": "label", "name": stripped[:-1], "note": note, "line_no": line_no})
            continue
        parts = split_source_tokens(stripped)
        if not parts:
            continue
        items.append({"kind": "cmd", "head": parts[0], "parts": parts[1:], "note": note, "line_no": line_no})
    return items

def source_item_label_refs(items: list[dict]) -> dict[str, int]:
    """Count references to each label across goto=/target=/entry/marker lines."""
    refs: dict[str, int] = {}

    def bump(name: str) -> None:
        refs[name] = refs.get(name, 0) + 1

    for item in items:
        if item["kind"] != "cmd":
            continue
        if item["head"] in (".entry", ".marker_table"):
            for part in item["parts"]:
                bump(part)
            continue
        for part in item["parts"]:
            if "=" in part:
                key, value = part.split("=", 1)
                if key in ("goto", "target"):
                    bump(value)
    return refs

def source_item_field(item: dict, key: str) -> str | None:
    for part in item["parts"]:
        if part.startswith(key + "="):
            return part.split("=", 1)[1]
    return None

STAGE_MACRO_NAMES = {
    "init_char": "init_char",
    "load_char": "load_char",
    "place_char": "place_char",
    "move_char": "move_char",
    "remove_char": "remove_char",
    "spawn_char": "spawn_char",
    "give_item": "give_item",
}

def stage_macro_source_lines(directive: str, args: list[str], kwargs: dict[str, str]) -> list[str] | None:
    """Run the staging-macro emitter against a scratch parser and return the
    EVDSRC lines it produces, or None when the arguments are rejected."""
    parser = EVDCodeParser.scratch()
    try:
        parser.emit_stage_macro(directive, directive, list(args), dict(kwargs))
    except Exception:
        return None
    return parser.output

def compile_source_window(lines: list[str]) -> bytes | None:
    """Compile a run of EVDSRC statement lines in isolation and return the
    command-region bytes (relative branches make this position-independent)."""
    body = "\n".join(line if line.strip().endswith(":") else line for line in lines if line.strip())
    text = "; EVDSRC v1\n.header 3\n.entry __w\n__w:\n" + body + "\n  end_script\n"
    try:
        data = compile_evd_source(text)
    except Exception:
        return None
    return data[0x0C: len(data) - 4]

def source_items_window_lines(window: list[dict]) -> list[str] | None:
    lines: list[str] = []
    for item in window:
        if item["kind"] == "label":
            lines.append(f"{item['name']}:")
        elif item["kind"] == "cmd":
            lines.append("  " + " ".join([item["head"], *item["parts"]]))
        else:
            return None
    return lines

def _macro_char_call(word_text: str) -> tuple[str, dict[str, str]] | None:
    try:
        word = parse_hex_int(word_text)
    except Exception:
        return None
    kwargs: dict[str, str] = {}
    scene = (word >> 16) & 0xFFFF
    if scene:
        kwargs["scene"] = str(scene)
    return str(word & 0xFFFF), kwargs

def _extract_stage_macro_call(directive: str, items: list[dict], index: int) -> tuple[list[str], dict[str, str]] | None:
    def field(offset: int, key: str) -> str | None:
        if index + offset >= len(items) or items[index + offset]["kind"] != "cmd":
            return None
        return source_item_field(items[index + offset], key)

    if directive == "remove_char":
        char = field(0, "character")
        if char is None:
            return None
        packed = _macro_char_call(char)
        return ([packed[0]], packed[1]) if packed else None

    if directive == "give_item":
        params = field(1, "params")
        if params is None:
            return None
        try:
            word = parse_hex_int(params)
        except Exception:
            return None
        if word >> 16 != 0x0001:
            return None
        return [str(word & 0xFFFF)], {}

    if directive in ("init_char", "spawn_char"):
        char = field(0, "character")
        if char is None:
            return None
        packed = _macro_char_call(char)
        if not packed:
            return None
        args, kwargs = [packed[0]], dict(packed[1])
        modeling = field(2, "modeling")
        if modeling is None:
            return None
        model_word = parse_hex_int(modeling)
        if model_word & 0xFFFFFF00 != 0x00010100:
            return None
        model = model_word & 0xFF
        if model != 0xFF:
            kwargs["model"] = f"0x{model:02X}"
        animations = field(2, "animations")
        if animations is None:
            kwargs["has_animation"] = "false"
        else:
            if not animations.startswith("3:"):
                return None
            anim_word = parse_hex_int(animations[2:])
            if anim_word & 0xFFFFFF00 != 0x00011400:
                return None
            anim = anim_word & 0xFF
            if anim != 1:
                kwargs["animation"] = str(anim)
        if directive == "init_char":
            return args, kwargs
        # spawn = init(3) + load(12) + place(5); pull xyz/rotations from the
        # position and rotate lines of the place segment.
        place_pos = index + 3 + 12 + 2
        vec = field(place_pos - index, "inline_vec")
        posture = field(place_pos - index + 1, "target_posture_vec")
        if vec is None or posture is None:
            return None
        coords = vec.split(",")
        if len(coords) != 3:
            return None
        args.extend(coords)
        rot_kwargs = _rotation_kwargs(posture)
        if rot_kwargs is None:
            return None
        kwargs.update(rot_kwargs)
        return args, kwargs

    if directive == "load_char":
        char = field(0, "character_pairs")
        if char is None:
            return None
        packed = _macro_char_call(char)
        return ([packed[0]], packed[1]) if packed else None

    if directive == "place_char":
        char = field(0, "character")
        vec = field(2, "inline_vec")
        posture = field(3, "target_posture_vec")
        if char is None or vec is None or posture is None:
            return None
        packed = _macro_char_call(char)
        if not packed:
            return None
        coords = vec.split(",")
        if len(coords) != 3:
            return None
        rot_kwargs = _rotation_kwargs(posture)
        if rot_kwargs is None:
            return None
        kwargs = dict(packed[1])
        kwargs.update(rot_kwargs)
        return [packed[0], *coords], kwargs

    if directive == "move_char":
        char = field(0, "character")
        points = field(0, "points")
        if char is None or points is None or not points.startswith("inline:"):
            return None
        packed = _macro_char_call(char)
        if not packed:
            return None
        pieces = points[len("inline:"):].split(",")
        if len(pieces) < 4:
            return None
        coords = pieces[:3]
        kwargs = dict(packed[1])
        for piece in pieces[3:]:
            if piece.startswith("duration_ms="):
                duration = piece.split("=", 1)[1]
                if duration != "1000":
                    kwargs["duration"] = duration
        has_wait = False
        nxt = index + 2
        if nxt < len(items) and items[nxt]["kind"] == "cmd" and items[nxt]["head"] == "trigger":
            has_wait = source_item_field(items[nxt], "trigger_type") == "0x06"
        if not has_wait:
            kwargs["wait"] = "false"
        return [packed[0], *coords], kwargs

    return None

def _rotation_kwargs(posture: str) -> dict[str, str] | None:
    kwargs: dict[str, str] = {}
    for part, key in zip(posture.split(","), ("rot_x", "rot_y", "rot_z")):
        if ":" not in part:
            return None
        value = part.split(":", 1)[1]
        try:
            radians = float(value)
        except ValueError:
            return None
        degrees = math.degrees(radians)
        if degrees:
            kwargs[key] = format_f32(u32_to_f32(f32_to_u32(degrees)))
    return kwargs

STAGE_MACRO_TRIGGERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("set_person_schedule", ("spawn_char", "init_char")),
    ("set_event_leave", ("load_char",)),
    ("set_character_render", ("place_char",)),
    ("move_character_along_points", ("move_char",)),
    ("delete_character", ("remove_char",)),
    ("window_command", ("give_item",)),
)

def _parse_choose_header_params(params_text: str) -> tuple[int, dict[str, str]] | None:
    """Match the 8-word choose window header and extract line id + kwargs."""
    try:
        words = [parse_hex_int(part) for part in params_text.split(",")]
    except Exception:
        return None
    if len(words) != 8:
        return None
    if (
        words[0] & 0xFFFF != 0x0002
        or words[1] != 0x64
        or words[2] != 0
        or words[3] >> 16 != 0x0003
        or words[4] & 0xFFFF
        or words[4] >> 24
        or words[5] != 0xFEC0
        or words[6] != 0
        or words[7] != 0x140
    ):
        return None
    line_id = words[0] >> 16
    box_image = (words[3] >> 8) & 0xFF
    box_colour = words[3] & 0xFF
    sfx = (words[4] >> 16) & 0xFF
    kwargs: dict[str, str] = {}
    if box_colour:
        kwargs["box_colour"] = f"0x{box_colour:02X}"
    if box_image:
        kwargs["box_image"] = f"0x{box_image:02X}"
    if sfx != 6:
        kwargs["sfx"] = str(sfx)
    return line_id, kwargs

def _is_cmd(item: dict, head: str, *fields: str) -> bool:
    if item.get("kind") != "cmd" or item.get("head") != head:
        return False
    return all(part in item["parts"] for part in fields)

def resugar_choose_blocks(items: list[dict], refs: dict[str, int]) -> list[dict]:
    """Fold the choice-window choreography back into choose/option blocks.

    Structural recognition of the exact scaffold parse_choose emits: header
    window, wait loop on event value 18, close window, branch table, option
    bodies each ending with a jump to a shared end label. The caller verifies
    the final result byte-for-byte and falls back when it differs.
    """
    out: list[dict] = []
    index = 0
    while index < len(items):
        replaced = _try_choose_at(items, index, refs)
        if replaced is not None:
            out.append(replaced[0])
            index = replaced[1]
        else:
            out.append(items[index])
            index += 1
    return out

def _try_choose_at(items: list[dict], index: int, refs: dict[str, int]):
    item = items[index]
    if not _is_cmd(item, "window_command", "mode=0x01", "window=0x0009", "message=0x0007"):
        return None
    params = source_item_field(item, "params")
    if params is None:
        return None
    header = _parse_choose_header_params(params)
    if header is None:
        return None
    line_id, kwargs = header
    i = index + 1
    # wait loop: label / nop / poll window / backward branch / close window
    if i + 4 >= len(items) or items[i]["kind"] != "label":
        return None
    wait_label = items[i]["name"]
    if refs.get(wait_label, 0) != 1:
        return None
    if not _is_cmd(items[i + 1], "nop", "flags=0x00"):
        return None
    if not _is_cmd(items[i + 2], "window_command", "mode=0x00", "window=0x0009", "message=0x0005"):
        return None
    branch = items[i + 3]
    if not _is_cmd(branch, "if_value", "event_value=18", "is=0", f"goto={wait_label}"):
        return None
    if not _is_cmd(items[i + 4], "window_command", "mode=0x00", "window=0x0009", "message=0x0002"):
        return None
    i += 5
    # branch table: if_value event_value=18 is=K goto=Lk, then jump goto=end
    option_labels: list[str] = []
    while i < len(items) and items[i].get("kind") == "cmd" and items[i]["head"] == "if_value":
        expected = f"is={len(option_labels) + 1}"
        entry = items[i]
        if "event_value=18" not in entry["parts"] or expected not in entry["parts"]:
            return None
        target = source_item_field(entry, "goto")
        if target is None or refs.get(target, 0) != 1 or len(entry["parts"]) != 3:
            return None
        option_labels.append(target)
        i += 1
    if not option_labels:
        return None
    if i >= len(items) or items[i].get("kind") != "cmd" or items[i]["head"] != "jump":
        return None
    end_label = source_item_field(items[i], "goto")
    if end_label is None or len(items[i]["parts"]) != 1:
        return None
    if refs.get(end_label, 0) != len(option_labels) + 1:
        return None
    i += 1
    bodies: list[list[dict]] = []
    for position, label in enumerate(option_labels):
        if i >= len(items) or items[i]["kind"] != "label" or items[i]["name"] != label:
            return None
        i += 1
        body: list[dict] = []
        while i < len(items):
            entry = items[i]
            if entry.get("kind") == "cmd" and entry["head"] == "jump" and source_item_field(entry, "goto") == end_label and len(entry["parts"]) == 1:
                break
            if entry.get("kind") == "label" and entry["name"] in option_labels[position + 1:] + [end_label]:
                return None
            body.append(entry)
            i += 1
        else:
            return None
        bodies.append(body)
        i += 1  # consume the jump
    if i >= len(items) or items[i]["kind"] != "label" or items[i]["name"] != end_label:
        return None
    i += 1
    choose_item = {
        "kind": "choose",
        "line_id": f"0x{line_id:04X}",
        "kwargs": kwargs,
        "bodies": bodies,
        "note": item["note"],
    }
    return choose_item, i

def resugar_stage_macros(items: list[dict], refs: dict[str, int]) -> list[dict]:
    """Recognize staging-macro sequences and fold them into macro calls.

    Purely additive and byte-safe: a window is replaced only when re-running
    the macro emitter with the extracted arguments compiles to exactly the
    same bytes as the observed lines.
    """

    def self_contained(window: list[dict]) -> bool:
        defined = {entry["name"] for entry in window if entry["kind"] == "label"}
        if not defined:
            return True
        inner = source_item_label_refs([e for e in window if e["kind"] == "cmd"])
        return all(refs.get(name, 0) == inner.get(name, 0) for name in defined)

    out: list[dict] = []
    index = 0
    while index < len(items):
        item = items[index]
        replaced = False
        if item["kind"] == "cmd":
            for trigger_head, directives in STAGE_MACRO_TRIGGERS:
                if item["head"] != trigger_head:
                    continue
                for directive in directives:
                    call = _extract_stage_macro_call(directive, items, index)
                    if call is None:
                        continue
                    template = stage_macro_source_lines(directive, call[0], call[1])
                    if not template:
                        continue
                    count = sum(1 for line in template if line.strip())
                    window = items[index: index + count]
                    if len(window) != count or not self_contained(window):
                        continue
                    window_lines = source_items_window_lines(window)
                    if window_lines is None:
                        continue
                    expected = compile_source_window(template)
                    actual = compile_source_window(window_lines)
                    if expected is None or expected != actual:
                        continue
                    rendered_args = list(call[0]) + [f"{k}={v}" for k, v in call[1].items()]
                    out.append({
                        "kind": "macro",
                        "name": STAGE_MACRO_NAMES[directive],
                        "args": rendered_args,
                        "note": item["note"],
                    })
                    index += count
                    replaced = True
                    break
                if replaced:
                    break
        if not replaced:
            out.append(item)
            index += 1
    return out

def resugar_branch_blocks(items: list[dict], refs: dict[str, int]) -> list[dict]:
    """Turn single-use forward branches into structured if/else block items.

    Only fires when the recompiled shape is byte-identical: the branch keeps
    its head with the comparator negated, the skip/end labels are removed
    (each had exactly one reference), and every label defined inside a block
    is only referenced from inside that block.
    """

    def label_index(scope: list[dict], name: str, start: int) -> int:
        for index in range(start, len(scope)):
            entry = scope[index]
            if entry["kind"] == "label" and entry["name"] == name:
                return index
        return -1

    def region_is_self_contained(region: list[dict]) -> bool:
        defined = {entry["name"] for entry in region if entry["kind"] == "label"}
        if not defined:
            return True
        inner_refs = source_item_label_refs([e for e in region if e["kind"] == "cmd"])
        for name in defined:
            if refs.get(name, 0) != inner_refs.get(name, 0):
                return False
        return True

    def structure(scope: list[dict]) -> list[dict]:
        out: list[dict] = []
        index = 0
        while index < len(scope):
            item = scope[index]
            if item["kind"] == "choose":
                item = dict(item)
                item["bodies"] = [structure(body) for body in item["bodies"]]
                out.append(item)
                index += 1
                continue
            if item["kind"] != "cmd" or item["head"] not in BRANCH_BLOCK_HEADS:
                out.append(item)
                index += 1
                continue
            target = source_item_field(item, "goto")
            cmp_parts = [p for p in item["parts"] if p.split("=", 1)[0] in BRANCH_COMPARE_SELECTORS]
            if target is None or refs.get(target, 0) != 1 or len(cmp_parts) != 1:
                out.append(item)
                index += 1
                continue
            skip_index = label_index(scope, target, index + 1)
            if skip_index < 0:
                out.append(item)
                index += 1
                continue
            body = scope[index + 1: skip_index]
            else_body = None
            consumed_end = skip_index
            # else form: the body ends with an unadorned jump to a later
            # single-use label; the skip label separates the two bodies.
            if body and body[-1]["kind"] == "cmd" and body[-1]["head"] == "jump":
                jump = body[-1]
                end_target = source_item_field(jump, "goto")
                if (
                    end_target is not None
                    and refs.get(end_target, 0) == 1
                    and len(jump["parts"]) == 1
                    and not jump["note"]
                ):
                    end_index = label_index(scope, end_target, skip_index + 1)
                    if end_index >= 0:
                        candidate_else = scope[skip_index + 1: end_index]
                        if region_is_self_contained(body[:-1]) and region_is_self_contained(candidate_else):
                            body = body[:-1]
                            else_body = candidate_else
                            consumed_end = end_index
            if else_body is None and not region_is_self_contained(body):
                out.append(item)
                index += 1
                continue
            cmp_key, cmp_value = cmp_parts[0].split("=", 1)
            block = {
                "kind": "block",
                "head": item["head"],
                "parts": [p for p in item["parts"] if p != "goto=" + target and p not in cmp_parts],
                "cmp_key": NEGATED_COMPARATORS[cmp_key],
                "cmp_value": cmp_value,
                "note": item["note"],
                "body": structure(body),
                "else_body": structure(else_body) if else_body is not None else None,
            }
            out.append(block)
            index = consumed_end + 1
        return out

    return structure(items)

def render_code_items(items: list[dict], depth: int) -> list[str]:
    pad = "    " * depth
    lines: list[str] = []
    for item in items:
        if item["kind"] == "label":
            lines.append(f"{pad}label({item['name']}){item['note']}")
            continue
        if item["kind"] == "choose":
            call_args = [item["line_id"]] + [f"{k}={v}" for k, v in item["kwargs"].items()]
            lines.append(f"{pad}choose({', '.join(call_args)}) {{{item['note']}")
            for body in item["bodies"]:
                lines.append(f"{pad}    option {{")
                lines.extend(render_code_items(body, depth + 2))
                lines.append(f"{pad}    }}")
            lines.append(f"{pad}}}")
            continue
        if item["kind"] == "macro":
            lines.append(f"{pad}{item['name']}({', '.join(item['args'])}){item['note']}")
            continue
        if item["kind"] == "block":
            fields = []
            for part in item["parts"]:
                if "=" in part:
                    key, value = part.split("=", 1)
                    if code_value_needs_quote(key, value):
                        value = code_quote(value)
                    fields.append(f"{key}={value}")
                else:
                    fields.append(part)
            fields.append(f"{item['cmp_key']}={item['cmp_value']}")
            lines.append(f"{pad}{item['head']}({', '.join(fields)}) {{{item['note']}")
            lines.extend(render_code_items(item["body"], depth + 1))
            if item["else_body"] is not None:
                lines.append(f"{pad}}} else {{")
                lines.extend(render_code_items(item["else_body"], depth + 1))
            lines.append(f"{pad}}}")
            continue
        head = item["head"]
        parts = item["parts"]
        if head == ".header":
            lines.append(f"{pad}header({parts[0]}){item['note']}")
        elif head == ".header_extra":
            lines.append(f"{pad}headerExtra({parts[0]}){item['note']}")
        elif head == ".entry":
            lines.append(f"{pad}entry({parts[0]}){item['note']}")
        else:
            rendered = source_call_to_code(head, parts).strip()
            lines.append(f"{pad}{rendered}{item['note']}")
    return lines

def evd_source_to_code(source_text: str, event_name: str = "Main", sugar_choose: bool = True) -> str:
    items = parse_source_items(source_text)
    refs = source_item_label_refs(items)
    items = resugar_stage_macros(items, refs)
    if sugar_choose:
        items = resugar_choose_blocks(items, refs)
    items = resugar_branch_blocks(items, refs)
    lines = [f"event {event_name} {{"]
    lines.extend(render_code_items(items, 1))
    lines.append("}")
    return "\n".join(lines) + "\n"

def decompile_evd_code(
    data: bytes,
    start: int = 0x0C,
    event_name: str = "Main",
    symbols: dict[str, dict[int, str]] | None = None,
) -> str:
    source_text = decompile_evd_source(data, start, False, symbols)
    code = evd_source_to_code(source_text, event_name)
    # choose recognition is structural rather than per-window byte-verified,
    # so confirm the whole file and drop that pass when it changes bytes.
    try:
        if compile_evd_code(code) == data:
            return code
    except Exception:
        pass
    return evd_source_to_code(source_text, event_name, sugar_choose=False)

RAW_PAYLOAD_FIELD_NAMES = {"words", "payload", "extra", "condition_args"}

SOURCE_FIELD_PROLOGUE = ".header 3\n.header_extra 0x00000000\n.entry start\n\nstart:\n"

PARAMETER_GLOSSARY: dict[str, str] = {
    "name": (
        "Object or animation name this line acts on, as text. The engine reads it as a "
        "NUL-terminated string; `name_fill` covers the bytes after the terminator."
    ),
    "action": (
        "Which action this command performs, read from its mode or control bits. Each "
        "name is one branch of the handler, so changing it changes what the line does."
    ),
    "flag": "Config/event flag id this line reads or writes.",
    "kind": (
        "Readable name for the variant this line selects, derived from its selector "
        "byte. It is cross-checked on compile, so it cannot disagree with the selector."
    ),
    "name_fill": (
        "Byte filling the name slot after its terminator. The engine stops at the "
        "terminator so this never changes behaviour, but the original tool left "
        "0xCC there and it has to be preserved. Omit it for a zero-filled slot."
    ),
    "flags": (
        "Command header flag byte. Bit 0x80 is the keep-going bit: set (the "
        "hidden default), the next command runs in the same frame; clear "
        "(printed as `yield=1`), the script pauses until the next game update. "
        "Other bits are rare and stay as an explicit flags= byte."
    ),
    "arg": (
        "Raw handler argument byte from the command header. It is printed only when "
        "it differs from the value the named fields already imply, so seeing it means "
        "the command uses an unusual argument encoding."
    ),
    "explicit_char": (
        "`1` when the command carries its own character selector word, `0` when it "
        "acts on the script's current character. It corresponds to bit 0 of the "
        "handler argument and decides whether a `character=` word is present."
    ),
    "character": (
        "The packed 32-bit character selector word. Its low half is the character id "
        "and byte 2 is a secondary class/variant input; see `character_number` and "
        "`character_variant`."
    ),
    "character_number": "Low 16 bits of the character selector: the character/actor id.",
    "character_variant": (
        "Byte 2 of the character selector: a secondary character/class input passed "
        "alongside the id."
    ),
    "character_type": (
        "Byte 2 of the character selector, where the handler treats it as a type "
        "selector rather than a variant."
    ),
    "character_word": "A packed character selector word used by this command in addition to the main one.",
    "mode": (
        "Handler path selector taken from the argument byte. The meaning is specific "
        "to each command; see the opcode notes for the paths it chooses."
    ),
    "control": (
        "Bitmask word choosing which optional payload words follow, in the order the "
        "handler tests them. Changing it changes how many words the command carries."
    ),
    "condition": (
        "Condition selector byte. The low 7 bits pick the condition (`cond_base`) and "
        "bit 7 inverts the result (`invert`). `0` means unconditional."
    ),
    "cond_base": "Low 7 bits of `condition`: which condition is evaluated.",
    "invert": "Bit 7 of `condition`: when `1` the condition result is inverted.",
    "event_value": "Script event-value id that the condition or command reads.",
    "value": "The value the condition compares against.",
    "slot": "Index into a runtime table owned by this command's subsystem.",
    "target_slot": "Destination index into a runtime table owned by this command's subsystem.",
    "duration": (
        "Human-readable rendering of the command's duration word. Edit the underlying "
        "`duration_word` when it is listed as an input."
    ),
    "raw_high": "Upper half of a packed word, preserved verbatim because its meaning is not yet traced.",
    "name_source": "Selects where a name operand comes from, such as an inline four-word string.",
    "selector": "Sub-selector byte choosing which variant of the command's behaviour runs.",
    "words": "Raw fallback payload, used when a command shape has no named form yet.",
    "id": "Identifier for the thing this command acts on.",
    "group": "Which group the target belongs to.",
    "mask": "Bitmask selecting which parts of the command apply.",
    "stand": "Stand-position operand, resolved through GetAbstractionStandPosNumber.",
    "target": "What the command acts on or moves towards.",
    "item": "Item operand, resolved through GetAbstractionItemNumber.",
    "count": "How many entries this command carries.",
    "source": "Where the command reads its operand from.",
    "parent": "The object being attached to.",
    "expected": "Value the condition compares against.",
    "low": "Lower part of the matching packed value.",
    "default_char": "Character used when the line does not name one explicitly.",
    "event_duration": "Set when the duration is an event value rather than a literal time.",
    "blend": "Set when a blend value is included on this line.",
    "scale": "Set when scale is animated by this line; the scale values follow.",
    "transparent": "Set when transparency is animated by this line; the transparency values follow.",
    "palette": "Set when the palette is animated by this line; the palette values follow.",
    "visibility": "Set when visibility is animated by this line; the visibility values follow.",
    "rotate": "Set when rotation is included on this line.",
    "posture": "Set when a posture operand is included on this line.",
    "background": "Set when the command targets the background rather than a character.",
    "change_map": "Set when the command also changes the map.",
    "first_flag": "Id of the first event flag the condition reads.",
    "map": "Which map the command loads or switches to.",
    "rate": "How fast the change is applied.",
    "delete": "Set when the command deletes rather than creates.",
    "paf": "Primitive animation file operand.",
    "file": "File id the command loads.",
    "texture": "Texture operand for this command.",
    "parent_character": "Low half of the parent selector: the parent's character id.",
    "parent_variant": "Byte 2 of the parent selector: a secondary class input for the parent.",
    "item_or_state": "Item or object-state operand, depending on the selector on this line.",
    "trailing": "Words left after the decoded operands, preserved verbatim.",
    "yield": "Pause the script here for one game frame; the next command runs on the following update. Lines without it keep running immediately.",
    "goto": "Label to jump to when the condition holds (or always, for `jump`).",
    "is": "Jump when the tested value equals this.",
    "not": "Jump when the tested value differs from this.",
    "at_least": "Jump when the tested value is greater than or equal to this.",
    "at_most": "Jump when the tested value is less than or equal to this.",
    "over": "Jump when the tested value is strictly greater than this.",
    "under": "Jump when the tested value is strictly less than this.",
}

FRIENDLY_FORM_NAMES: dict[str, str] = {
    "background_auto_rate_anim": "animate_background_material",
    "battle_character_fall_or_plugin_control": "control_battle_character",
    "chara_put_attach_life_flag": "attach_life_flag",
    "packing_file_load_or_release": "manage_packing_file",
    "person_allow_attribute": "set_person_allow",
    "sound_field_e0_set": "set_sound_field",
    "background_change_map": "change_map",
    "background_play_animation": "play_background_animation",
    "background_runtime_field": "set_background_field",
    "background_stop_animation": "stop_background_animation",
    "background_visibility": "set_background_visibility",
    "battle_acquisition_setup": "configure_battle",
    "battle_character_entry": "add_battle_character",
    "bgm_control": "stop_or_pause_music",
    "camera_capture_target": "set_camera_target",
    "camera_color_anim": (
        "control_camera_animation"
    ),
    "camera_flags": "set_camera_flags",
    "camera_mode": "set_camera_mode",
    "camera_move_etc": "move_camera",
    "camera_move_existing": "move_current_camera",
    "camera_select": (
        "control_camera_select"
    ),
    "camera_transform_param": "set_camera_transform",
    "character_anim_signal": "get_animation_signal",
    "character_animation": "play_character_animation",
    "character_attach_parent": "attach_character_to_parent",
    "character_attach_render": (
        "control_character_render"
    ),
    "character_attribute": "set_character_attribute",
    "character_auto_rate_anim": "animate_character_material",
    "character_collision_setup": "setup_character_collision",
    "character_data": "load_character",
    "character_delete_data": "delete_character",
    "character_detach_parent": "detach_character_from_parent",
    "character_equipment": "set_character_equipment",
    "character_event_leave": "set_event_leave",
    "character_expression": "set_character_expression",
    "character_eye_control": "set_character_eyes",
    "character_move_pause": "pause_character_move",
    "character_move_points": "move_character_along_points",
    "character_move_position": "set_character_position",
    "character_movement": "control_character_movement",
    "character_precreate_anim": "precreate_character_animation",
    "character_rotate_option": "rotate_character",
    "character_single_manager": "manage_character_subobject",
    "character_sub_anim": (
        "control_character_sub_animation"
    ),
    "character_virtual_24": "call_character_virtual",
    "conditional_end": "end_if",
    "expr": "set_value",
    "fade_control": "fade_screen",
    "global_visual_state": "set_global_visual_state",
    "landscape_visibility": "set_landscape_visibility",
    "load_sound_resource": "load_sound_file",
    "map_change_check": "check_map_change",
    "marker_seek": "seek_marker",
    "party_member": "change_party",
    "person_field_update": "set_person_field",
    "person_schedule_list": "set_person_schedule",
    "personal_inventory": "change_inventory",
    "play_bgm": "play_music",
    "play_sound_effect": "play_sound",
    "position_vibration_clear": "clear_position_vibration",
    "position_vibration_param": "set_position_vibration_params",
    "position_vibration_vector": "set_position_vibration",
    "primitive_anim_slot": "set_primitive_slot",
    "primitive_helper_byte": "set_primitive_helper",
    "primitive_move_sprtg": "move_primitive",
    "primitive_play_paf": "play_primitive_animation",
    "primitive_priority": "set_primitive_priority",
    "primitive_stop_paf": "stop_primitive_animation",
    "radiata_time_enable": "enable_game_clock",
    "scene_save_env": "push_scene_state",
    "script_defaults": "set_script_defaults",
    "script_start": "start_script",
    "script_start_stack": "start_script_stacked",
    "script_stop": "stop_script",
    "set_bgm": "select_music",
    "set_bgm_volume": "set_music_volume",
    "set_radiata_time": "set_game_clock",
    "setting_map": "setup_map",
    "sound_effect_stack": "push_or_pop_sounds",
    "sound_listener": "set_sound_listener",
    "special_effect": (
        "control_special_effect"
    ),
    "sprite_config": "set_sprite",
    "stand_context": "set_stand_position_context",
    "stop_sound_effect": "stop_sound",
    "strong_motion_blend": "set_motion_blend",
    "talk_bustup_display": (
        "control_portrait"
    ),
    # Not a text command. It sends a message to one of the game's windows, and a
    # textbox is only the most common thing to send one to.
    "window_message": "window_command",
    # No friendly alias for talk_rmf on purpose. It was spelled `say`, but three
    # commands put words on screen — this one, window_message (0x1B, the textbox
    # through CRadiWindowManager::SendMessage) and print_text (0x8F, raw SJIS
    # through CTextMessage) — and `say` gave no clue which. The engine name says
    # which system runs: a message id through AttachMessageData and
    # CTalk::RmfStart.
    "text_message_layout": "set_text_layout",
    "text_output": "print_text",
    "time_schedule_value": "set_time_schedule",
    "vibration_stop": "stop_vibration",
    "window_message_mode": "set_window_mode",
}

PARAMETER_ALIASES: dict[str, dict[str, str]] = {
    # Command_8b is hide/create/erase, not "show". `(arg>>6)` is the action
    # BustupDisp branches on (0 hide, 1 create, 2 erase); `(arg>>1)&1` indexes
    # the portrait pointer slot at controller+0x3C+slot*4 inside Create; and the
    # optional float is consumed only by action 2, as EraseStart(2.0, value),
    # defaulting to 5.0 when absent.
    "talk_bustup_display": {
        "upper_arg": "action",
        "flag1": "portrait_slot",
        "mode": "portrait_variant",
        "stream_float": "has_erase_duration",
        "display_time": "erase_duration",
    },
    # CVibPlayer::PlayVibration masks this argument with `& 1` and stores that
    # one bit beside the strength byte; there is no pattern table. Only 0 and 1
    # occur in the shipped corpus.
    "play_vibration": {"pattern": "motor_flag"},
    # The day selector is one-based: the handler passes `value - 1` to
    # SetRadiataTime, with 0 meaning "make no day call" and 0xFFFF "derive it".
    "set_radiata_time": {"day": "day_1based"},
    # Argument bit 4 gates an error path for the packed value's top component.
    # It is a range check, not a duration.
    "time_schedule_value": {"validate_time": "range_check"},
    # Command_14 is a read-modify-write, not an expression: `target` names what is
    # written, `value` supplies the operand, and `control_mid`/`store_type` are the
    # source domain of each (0 config flag, 1 script event value, 2 character
    # property, 0xF immediate literal). Proven from the recovered pfnParamTbl
    # tables plus the hand-labelled snippets.
    "expr": {
        "lhs": "target",
        "rhs": "value",
        "lhs_tag": "target_property",
        "rhs_tag": "value_property",
        "control_mid": "target_from",
        "store_type": "value_from",
        "op_name": "operation",
    },
    "character_data": {
        "modeling_word": "modeling",
        "action_word": "action",
        "algorithm_word": "algorithm",
        "animation_words": "animations",
        "modeling_id": "modeling_selector",
        "modeling_count": "modeling_load_mode",
        "action_id": "action_data_id",
    },
    "character_animation": {
        "anim_word": "animation",
        "anim_group": "animation_group",
        "anim_id": "animation_id",
        "extra_anim_word": "extra_animation",
        "optional_float0": "anim_float_a",
        "optional_float1": "anim_float_b",
        "optional_float2": "anim_float_c",
    },
    "set_flags": {"first": "first_flag", "count": "flag_count", "values": "flag_values"},
    "talk_rmf": {"message": "message_id"},
    "text_output": {"clear_id": "clear_message_id"},
    "character_attach_parent": {"parent_word": "parent"},
    "character_sub_anim": {"anim_word": "animation"},
    "character_precreate_anim": {"anim_word": "animation"},
    "trigger": {"type": "trigger_type"},
    
    "background_auto_rate_anim": {"duration": "duration_text", "duration_word": "duration"},
    "character_rotate_option": {"duration": "duration_text", "duration_word": "duration"},
}

PARAMETER_ALIASES.update(
    {
        "battle_acquisition_setup": {"tail0_word": "tail0"},
        "branch": {
            "character_raw_word": "character_raw",
            "character_word": "character",
            "expected_word": "expected",
            "item_or_state_word": "item_or_state",
        },
        "camera_color_anim": {"end_color_word": "end_color"},
        "camera_move_etc": {"source_point_word": "source_point"},
        "character_collision_setup": {"float_array_word": "float_array"},
        "character_move_position": {"target_word": "target"},
        "personal_inventory": {"item_word": "item"},
        "play_sound_effect": {"control_word": "control"},
        "position_vibration_vector": {"target_word": "target"},
        "script_start_stack": {"expected_word": "expected"},
        "special_effect": {"effect_word": "effect", "execute_word": "execute"},
    }
)

PARAMETER_ALIASES["character_animation"]["extra_word"] = "has_extra_animation"

PARAMETER_ALIASES["trigger"]["character_word"] = "character"

PARAMETER_ALIASES["window_message"] = {"message_id": "window", "message_group": "message"}

PARAMETER_ALIASES_REVERSE: dict[str, dict[str, str]] = {
    form: {friendly: engine for engine, friendly in mapping.items()}
    for form, mapping in PARAMETER_ALIASES.items()
}

UNTRACED_PARAMETERS: dict[str, set[str]] = {
    "character_rotate_option": {"bit13"},
    "trigger": {"raw_mid"},
    "talk_rmf": {"raw_high"},
    "character_data": {"modeling_variant"},
}

def apply_parameter_aliases(directive: str, line: str) -> str:
    """Rewrite a decompiled line to use the friendly parameter names."""
    mapping = PARAMETER_ALIASES.get(directive)
    if not mapping:
        return line
    # Rename in place rather than re-rendering the line: some commands emit
    # fields with an empty value (`count_values=`), and a parse/re-render round
    # trip would drop them.
    pattern = "|".join(re.escape(name) for name in sorted(mapping, key=len, reverse=True))
    return re.sub(
        rf"(?<=\s)({pattern})=",
        lambda match: mapping[match.group(1)] + "=",
        line,
    )

def resolve_parameter_name(directive: str, name: str) -> str:
    """Map a friendly parameter name back to the engine name the builders expect."""
    return PARAMETER_ALIASES_REVERSE.get(directive, {}).get(name, name)

RADIATA_EVENT_ALIASES: dict[str, str] = {
    "change_time": "set_radiata_time",
    "fade_music": "set_bgm_volume",
    "anim_map_object": "background_play_animation",
    "remove_text": "primitive_anim_slot",
    "play_image": "primitive_helper_byte",
    "remove_image": "primitive_move_sprtg",
    "run_code": "battle_acquisition_setup",
    "load_voice_file": "load_sound_resource",
}

FORM_NAME_ALIASES: dict[str, str] = {
    friendly: engine for engine, friendly in FRIENDLY_FORM_NAMES.items()
}

FORM_NAME_ALIASES["set_character_render"] = "character_attach_render"

FORM_NAME_ALIASES["play_character_sub_animation"] = "character_sub_anim"

FORM_NAME_ALIASES["animate_camera_color"] = "camera_color_anim"

FORM_NAME_ALIASES["select_camera"] = "camera_select"

FORM_NAME_ALIASES["show_portrait"] = "talk_bustup_display"

FORM_NAME_ALIASES["play_special_effect"] = "special_effect"

FORM_NAME_ALIASES["control_character_render"] = "character_attach_render"

FORM_NAME_ALIASES["control_character_sub_animation"] = "character_sub_anim"

FORM_NAME_ALIASES["control_camera_animation"] = "camera_color_anim"

FORM_NAME_ALIASES["control_camera_select"] = "camera_select"

FORM_NAME_ALIASES["control_portrait"] = "talk_bustup_display"

FORM_NAME_ALIASES["control_special_effect"] = "special_effect"

FORM_NAME_ALIASES.update(RADIATA_EVENT_ALIASES)

FORM_NAME_ALIASES["setup_battle_rewards"] = "battle_acquisition_setup"

FORM_NAME_ALIASES["say"] = "talk_rmf"

FORM_NAME_ALIASES.update({head: "expr" for head in EXPR_HEAD_OPS})

FORM_NAME_ALIASES.update({head: "expr" for head in EXPR_FLAG_HEAD_OPS})

def resolve_form_name(directive: str) -> str:
    """Map a friendly command name onto the engine form the compiler implements."""
    return FORM_NAME_ALIASES.get(directive, directive)

FORM_PARAMETER_NOTES: dict[str, dict[str, str]] = {

    "character_data": {
        "mode": (
            "Which load path runs, from the top two bits of the argument byte. `2` uses "
            "the CRadiDataCenter load/release path; other values use the live "
            "CCharacterManager path."
        ),
        "control": (
            "Bitmask of which data types to act on: `0x0001` modeling, `0x0002` action, "
            "`0x0004`..`0x0200` animation slots, and `0x0400` algorithm on the data-center "
            "path. One payload word follows per set bit, in that order."
        ),
        "control_bits": "Names of the bits set in `control`.",
        "modeling_word": (
            "Modeling data word. Low byte is the modeling selector (`0xFF` is a sentinel "
            "that looks the value up from character data `+0x77`), byte 1 is a second "
            "modeling argument, and on the data-center path the signed high half selects "
            "load or release."
        ),
        "modeling_id": "Low byte of `modeling_word`: the modeling selector.",
        "modeling_variant": "Byte 1 of `modeling_word`, passed on to the load call.",
        "modeling_count": "Signed high half of `modeling_word`: the load/release selector.",
        "action_word": (
            "Action data word. Low half is the action id; on the data-center path the "
            "signed high half selects load or release."
        ),
        "action_id": "Low half of `action_word`: the action data id.",
        "animation_words": "Animation slots as `slot:word` pairs, one per set animation bit in `control`.",
        "algorithm_word": "Algorithm data word; data-center path only (`control` bit `0x0400`).",
    },
    "expr": {
        "op": "How `value` combines with the target: 0 set, 1 add, 2 sub, 3 mul, 4 div, 5 mod, 6 and, 7 or. Usually implied by the head: set_value, add_value, sub_value, mul_value, div_value, mod_value, and_value, or_value.",
        "operation": "Friendly spelling of `op`. Set either one.",
        "flag": "Config/event flag id this line writes (the friendly target form).",
        "event_value": "Script event value id this line writes (the friendly target form).",
        "property": "Named character property this line writes: hp, hp_max, money, experience, volty, friend_list, equipped_skill, evasion, and the battle-only names; unknown codes stay as hex.",
        "character": "Whose property it is: current, party1..party5, or a character id.",
        "system_param": "System/config parameter word (target domain 3).",
        "value_from_flag": "Read the value from this config/event flag instead of a literal.",
        "value_from_event": "Read the value from this script event value instead of a literal.",
        "value_from_character": "Read the value from a character property: whose character.",
        "value_from_property": "Read the value from a character property: which property.",
        "value_from_system": "Read the value from a system/config parameter word.",
        "control": "Packs `op`, `target_from` and `value_from`.",
        "target": (
            "What gets written. Its top byte is the property code and the low 24 bits "
            "are the subject, such as the character id."
        ),
        "target_property": (
            "Property code in the top byte of `target`, indexing the domain's parameter "
            "table. Known character codes: 0x00 HP, 0x01 HP Max, 0x03 evasion, "
            "0x06 Money, 0x38 Equipped Skill, 0x3F Volty, 0x44 Experience, "
            "0x48 Friend List. Battle-only codes (no effect outside battle): "
            "0x0A guard counter, 0x0B damage counter, 0x0C status-ailment state, "
            "0x32 battle target, 0x33 magic charge count, 0x36 magic level, "
            "0x37 owner character, 0x39 using-item number, 0x3B fake death, "
            "0x42 last stolen item, 0x46 active eternal tactics, 0x47 formation. "
            "Other reads: 0x21 schedule percent, 0x2B item count, "
            "0x3D move progress percent."
        ),
        "target_from": (
            "Which domain `target` addresses: 0 config/system event flag, 1 script event "
            "value, 2 character property, 5 item/other."
        ),
        "value": "The operand combined with the target.",
        "value_from": (
            "Where `value` comes from, using the same domain codes as `target_from`. "
            "0xF means it is an immediate literal, which is 82% of all uses."
        ),
        "value_property": "Property code in the top byte of `value`, when `value_from` names a property domain.",
    },
    "trigger": {
        "action": (
            "What to do with the trigger record, from the top two bits of the argument "
            "byte: `0`/`1` store it in a free wait slot, `2` test it now, `3` clear "
            "matching slots."
        ),
        "type": "Trigger type byte, choosing what is waited on (`0x01` timer, `0x03` fade, `0x04` dialogue, `0x0B` movie).",
        "trigger_value": "Payload for trigger types `1`, `7`, `0x0C`, and some `0x0B` cases; for a timer this is the duration as a float.",
        "character_word": "Character operand for trigger type `6`, resolved through GetAbstractionCharacterNumber.",
        "name_words": "Inline four-word name payload used by trigger type `0x0A`.",
        "trigger_flags": "Upper half of the first operand, copied into the record.",
    },
    "set_flags": {
        "first": "Id of the first event flag written.",
        "count": "How many consecutive flags are written, 1..16.",
        "values": "Bitmask of the values written, one bit per flag starting at `first`.",
    },
    "text_output": {
        "mode": "`0` writes Shift-JIS text, `1` writes an event value as a number, `7` clears a previously written line.",
        "mode_name": "Friendly spelling of `mode`.",
        "text": "The Shift-JIS string, for `mode=0`.",
        "clear_id": "Which previously written line to clear, for `mode=7`.",
    },
    "talk_rmf": {
        "message": "RMF message id: the dialogue line to show.",
        "attach_mode": "How the line attaches to its speaker.",
    },
    # Entries below were added by the parameter-reference audit: every parameter
    # the generic patterns could not describe gets either a traced meaning or an
    # honest structural sentence. Do not invent semantics here; upgrade wording
    # only when a handler trace proves it.
    "camera_color_anim": {
        "end_color": "Packed RGBA8 end colour word; `end_rgba8` beside it shows the four channels.",
    },
    "character_virtual_24": {
        "mode_float_word": "Raw word behind `mode_float`, consumed by the mode path; its exact role in the virtual call is untraced.",
    },
    "personal_inventory": {
        "event40": "Bit 6 of the argument byte: the handler reads a script event value on this path.",
        "event80": "Bit 7 of the argument byte: the handler writes its result to a script event value.",
    },
    "character_movement": {
        "mode4_submode": "Sub-path selector inside mode 4, unpacked from `mode4_control`.",
        "posvib_attr": "POSVIB_ATTR word passed to the vibration/throw movement calls.",
    },
    "character_detach_parent": {
        "flag7_preserved": "Bit 7 of the argument byte, kept verbatim; the traced handler body never consumes it.",
    },
    "character_move_pause": {
        "pause_arg": "Bits 1-2 of the argument byte, passed as the CCharacterMove::MovePause argument.",
    },
    "background_play_animation": {
        "char_ref_stream": "Bit 5 of the argument byte: when the control word has bit 15 set, an explicit character-reference word follows in place of the third float.",
        "control": "Packed control word; the low 16 bits are the animation id passed to PlayAnimation, and bit 15 switches the third optional slot from a float to a character reference.",
        "name_words": "Fixed four-word name slot: the game always advances 16 bytes past the name, so it must be NUL-padded to exactly four words and 15 characters at most.",
    },
    "character_animation": {
        "animation": "Packed animation word; its low half is `animation_id` and its high bits are kept in `anim_high`.",
        "animation_group": "Animation group word passed to the motion-switch call.",
        "extra_animation": "Extra animation word, present when `has_extra_animation` is set.",
        "play_speed": "Playback-speed value for the animation switch.",
        "request_low_byte": "Low byte of `request_low16`, split out the way the handler splits it.",
        "speed0": "Argument-byte gate: when set, optional float `anim_float_a` follows.",
        "speed1": "Argument-byte gate: when set, optional float `anim_float_b` follows.",
        "speed2": "Argument-byte gate: when set, optional float `anim_float_c` follows.",
        "anim_float_a": "Optional animation float, present when `speed0` is set.",
        "anim_float_b": "Optional animation float, present when `speed1` is set.",
        "anim_float_c": "Optional animation float, present when `speed2` is set.",
    },
    "play_sound_effect": {
        "submode": "Secondary path selector from the argument byte.",
        "playse_stack0": "Word passed through to the PlaySe call unchanged; untraced beyond that.",
        "volume_or_arg": "Operand passed as either a volume or a call argument depending on the path; untraced in detail.",
        "control_low_s16": "Signed low half of the control word, as the handler sign-extends it.",
    },
    "special_effect": {
        "effect": "Packed effect word: low half is `effect_id`, high half is `effect_flags`.",
        "execute": "Final word passed to ExecuteSpecialEffect_Main.",
        "abort": "Set when the command calls AbortSpecialEffect instead of ExecuteSpecialEffect_Main.",
        "character0": "First character selector word, present when `explicit_char0` is set.",
        "character1": "Second character selector word, present when `explicit_char1` is set.",
        "explicit_char0": "Whether the first character selector word is present.",
        "explicit_char1": "Whether the second character selector word is present.",
    },
    "play_vibration": {
        "strength": "Vibration strength passed to CVibPlayer::PlayVibration.",
        "pattern": "Vibration pattern selector passed to CVibPlayer::PlayVibration.",
    },
    "play_movie": {
        "movie": "Movie id: the low halfword of the first operand, passed to CMovieManager::PlayMovie.",
        "param0": "First signed halfword parameter passed to PlayMovie.",
        "param1": "Second signed halfword parameter passed to PlayMovie.",
        "extra": "Third operand word, passed to PlayMovie unchanged.",
    },
    "scene_save_env": {
        "pop": "0 pushes (saves) the selected scene state, 1 pops (restores) it.",
        "character_disp": "Whether character display state is in the saved set; unpacked from `mask`.",
        "map_disp": "Whether map display state is in the saved set; unpacked from `mask`.",
    },
    "character_rotate_option": {
        "option": "Bit 9 of the control word; with `mode` it selects the row in the rotate-action table.",
        "speed_limit": "Whether the speed-values pair is present (control bit 0x1000).",
        "target_char_from_stream": "Whether the target character comes from the stream rather than the current context.",
        "target_character_pair": "Packed target character word for the target-character rotate actions.",
    },
    "marker_seek": {
        "selector_word": "Packed selector word naming which marker key to search for; `selector_source` beside it names where the value comes from.",
        "advance_if_lower": "Scan-direction flag decoded from the selector word; preserved exactly.",
    },
    "camera_select": {
        "camera": "Camera id for CRadiCameraSystem::SelectCamera; 0xFFFF leaves the camera unchanged.",
    },
    "set_bgm": {
        "info0": "First word of the RADIBGM_INF record passed to CRadiSound::SetBgm.",
        "info1": "Second word of the RADIBGM_INF record passed to CRadiSound::SetBgm.",
    },
    "set_bgm_volume": {
        "volume": "Volume target passed to CRadiSound::SetBgmVolume; 0x3FFF is the full-volume value seen in samples.",
        "time": "Ramp duration passed to SetBgmVolume, which is how music fades work.",
    },
    "bgm_control": {
        "pause": "Argument-byte bit 7: 1 calls CRadiSound::PauseBgm, 0 calls StopBgm.",
    },
    "stop_sound_effect": {
        "bank": "Sound bank byte passed to CRadiSound::StopSe.",
    },
    "background_visibility": {
        "shadow": "Selects the CBackGround::SetLightShadowEnable path of Command_47.",
    },
    "camera_mode": {
        "rail_forward": "Direction flag for CRadiCameraSystem::MoveCameraRail.",
        "rail_time_word": "Raw duration word for the rail move; `rail_time` shows it decoded.",
    },
    "camera_capture_target": {
        "capture_rate_word": "Raw float word behind `capture_rate`.",
        "enable_capture": "Whether capture is enabled on this line; unpacked from `capture_control`.",
    },
    "camera_transform_param": {
        "capture_before_rotate": "Control bit preserved from the command; its exact effect on the transform is untraced.",
        "distance_scale": "Control bit preserved from the command; its exact effect on the transform is untraced.",
    },
    "character_attribute": {
        "collision_attr": "Collision attribute selector passed to SetCharaCollisionAttribute; `collision_value` is its value pair.",
        "set_attr2": "Value passed to SetCharacterAttribute for attribute 2: 1 when argument-byte bit 1 is clear, 0 when it is set.",
    },
    "character_equipment": {
        "item_branch": "Bit 1 of the argument byte: takes the inventory/equipment-index branch that uses byte 2 of `item_control`.",
        "item_high_byte": "Byte 2 of `item_control`, used by the equipment-index branch; 0x100 is ORed in when the character is in the party.",
        "display_arg": "Byte 3 of `item_control`, passed to the display/equip-all calls when `display_mode` is nonzero.",
    },
    "character_expression": {
        "expression": "Expression selector byte passed to the expression control object.",
        "blink": "Blink control byte passed to CExpressionControl::BlinkControll.",
        "mouth": "Mouth control byte passed to CExpressionControl::MouthControll.",
        "blink_half_steps": "Blink timing operand; the decoder reads it in half steps.",
    },
    "character_eye_control": {
        "eye_ball": "Whether the eye-ball path (CEyeControl::SetEyeBallNo) runs.",
        "eye_ball_byte": "Eye-ball number byte passed to CEyeControl::SetEyeBallNo (low 2 bits); suppressed when zero.",
        "eye_move": "Whether the eye-move path (SetEyeMoveType/SetEyeMoveManual) runs.",
        "manual_x_s8": "Signed x component for SetEyeMoveManual.",
        "manual_y_s8": "Signed y component for SetEyeMoveManual.",
        "manual_time_word": "Raw duration word for SetEyeMoveManual; `manual_time` shows it decoded.",
    },
    "character_move_position": {
        "coord": "Coordinate/path selector bits from the argument byte; the handler branches on them.",
        "terrain_y": "Control bit the decoder reads as snap-y-to-terrain; preserved exactly.",
        "duration_as_speed": "Control bit the decoder reads as duration-means-speed; preserved exactly.",
        "target_number": "Low 16 bits of `target`: the target id.",
        "target_variant": "Byte 2 of `target`: its secondary class input.",
    },
    "character_attach_render": {
        "no_render": "No-render boolean applied through the SSF handler or character byte; argument-byte bit 7 when data is already attached.",
    },
    "character_event_leave": {
        "release": "Selects ReleaseEnterCharacter rather than AddEnterCharacter.",
    },
    "set_radiata_time": {
        "byte0": "First time byte passed to CRadiApp::SetRadiataTime.",
        "byte1": "Second time byte passed to CRadiApp::SetRadiataTime.",
    },
    "global_visual_state": {
        "global_db": "Whether the gpGlobalDB field group is written.",
        "object_visual": "Whether the object-manager visual field group is written.",
        "global_db_reserved": "Raw word written alongside the global-DB float, preserved verbatim.",
    },
    "landscape_visibility": {
        "visible": "Visibility boolean passed to CHierarchicalObject::SetVisibility.",
        "hidden": "Hidden bit toggled on the landscape group header.",
    },
    "strong_motion_blend": {
        "blend": "Blend strength float passed to StrongMotionBlendForDynamics.",
        "blend_word": "Raw hex spelling of `blend`, used when the float does not round-trip exactly.",
    },
    "person_field_update": {
        "bit332_20": "Bit 0x20 of person-record byte +0x332, set or cleared by this line.",
    },
    "person_schedule_list": {
        "schedule_arg_byte": "Byte operand passed to SetScheduleListNumber alongside the schedule id.",
    },
    "position_vibration_vector": {
        "start_slot0": "Start/attach flag for the first slot, decoded from the mode bits.",
        "start_slot1": "Start/attach flag for the second slot, decoded from the mode bits.",
        "control_low8": "Low byte of `control`, shown for the handler's byte-wise test.",
    },
    "position_vibration_param": {
        "attr": "POSVIB attribute word passed to CVibrationVector::SetParam.",
        "enable": "Enable flag passed to CVibrationVector::SetParam.",
    },
    "primitive_priority": {
        "priority": "Priority value passed to CPrimitiveHelper::SetPriority.",
    },
    "primitive_anim_slot": {
        "all_slots": "Whether the command acts on every slot in the group rather than the start/count range.",
    },
    # Command_17 (0x002E9930) memsets a 20-byte record, fills it, and pushes it
    # into the battle order array at 0x3B2834. CBattleMain::State_LoadPartyStart
    # hands each one to CBtlCharacterManager::MakeBtlCharacter(const
    # RADI_BTL_ORDER*, CCharacter*), so the record is the game's own
    # RADI_BTL_ORDER. CBtlCharacter::Initialize (0x003C19F0) copies every field
    # straight through, which fixes the layout but not yet what each field means.
    "battle_character_entry": {
        "entry_word0": (
            "First payload word of the battle order (RADI_BTL_ORDER): its low half lands "
            "at record +0x08 and its high half at +0x0A, which the battle character reads "
            "back as a signed value."
        ),
        "entry_word1": (
            "Second payload word: low half to record +0x0C, byte 2 to +0x0E, byte 3 to +0x0F."
        ),
        "entry_word2": "Third payload word, stored whole at record +0x10. Raw form of `team`/`leader`.",
        "team": (
            "Which side this character fights on. It is the low nibble of record +0x10, "
            "which `CBtlCharacter::ChangeTeamID` reads with `& 0x0F`. Shipped battles use "
            "0 and 1."
        ),
        "leader": (
            "Whether this character leads its team. It is bits 4-7 of record +0x10, which "
            "`CBtlCharacter::ChangeLeader` extracts and tests for non-zero."
        ),
        "entry_word2_high": (
            "The upper three bytes of the third payload word, which have no reader "
            "specific enough to name yet."
        ),
        "entry_half0": "Low half of `entry_word0`; record +0x08.",
        "entry_half1": "High half of `entry_word0`; record +0x0A, read back as a signed value.",
        "entry_half2": "Low half of `entry_word1`; record +0x0C.",
        "entry_byte3": "Byte 2 of `entry_word1`; record +0x0E.",
    },
    "character_single_manager": {
        "manager_flag14_bit0": (
            "Argument-byte bit 4, stored as bit 0 of the sub-manager's flag byte "
            "(CSingleManager+0x14). No mapped code reads that bit back, so the name says "
            "where it is written rather than what it does."
        ),
        "mask": (
            "Which sub-object slots this line sets up: one bit per slot, and each set bit "
            "consumes one entry from the stream. Bits 0-4 are the ones scripts use."
        ),
    },
    "sound_listener": {
        "manager_listener": "Whether CSoundManager::SetListener is called (listener-control bit).",
        "positive_distance": "Distance-sign control bit from the argument byte.",
    },
    "sprite_config": {
        "color": "Packed colour word written to the sprite slot.",
    },
    "stand_context": {
        "context": (
            "Whether this line sets the stand-position context value. Argument bit 0: "
            "Command_1c writes the operand's low bits into bits 0-9 of a global halfword, "
            "leaving its upper bits alone."
        ),
        "context_low10": "The context value itself: the low 10 bits of `stand_word`.",
        "position": "Whether the position fields are written into the current context.",
        "stand_position": "Stand-position id resolved through GetAbstractionStandPosNumber.",
        "stand_word": "Packed stand-position word; the annotated parts beside it decode it.",
    },
    "text_message_layout": {
        "x": "Signed x layout position, copied to text-message fields +0x14/+0x18.",
        "y": "Signed y layout position, copied to text-message fields +0x16/+0x1A.",
        "color": "RGBA text colour (0x80 = full intensity); PutText passes CTextMessage +0x00 to CreateSPRT as each glyph's colour.",
        "priority": "Draw priority passed to CPrimitiveHelper::SetPriority; the shadow layer draws at priority + 1.",
    },
    "time_schedule_value": {
        "operation": "Sub-operation selector within the mode path.",
        "slot_select": "Slot selector; -1 means the current/default slot.",
        "part0": "Component 0 of `packed_time`.",
        "part1": "Component 1 of `packed_time`.",
        "part2": "Component 2 of `packed_time`.",
        "part3": "Component 3 of `packed_time`.",
        "part4": "Component 4 of `packed_time`.",
    },
    "battle_acquisition_setup": {
        "count_values": "Raw count payload words, preserved verbatim.",
        "tail0": "Packed tail word 0; `tail0_low16` beside it shows its low half.",
    },
    "script_defaults": {
        "char_word": "Packed default-character word; `character` and `character_variant` beside it decode it.",
        "default_char": (
            "Argument-byte bit 0: whether a character selector word follows, which then "
            "becomes this script's default character. It is a gate, not a character id — "
            "`default_char=1` means \"a character is given below\", not character 1."
        ),
        "default_object": (
            "Argument-byte bit 1: whether a default object name follows, setting the name "
            "later commands act on when they are not given one."
        ),
        "event_char": (
            "Argument-byte bit 7: read the default character from an event value instead "
            "of taking it literally. It changes what the following word means, which is "
            "why the line then prints `event_value=` rather than `character=`."
        ),
    },
    "setting_map": {
        "id": (
            "Low nibble of the argument byte, passed straight to CBackGround::SettingMap. "
            "A 0-15 setup code, not a map or location id; shipped scripts only use 2, 3 "
            "and 11."
        ),
    },
    "script_start": {
        "script_id": (
            "Which script *inside the currently loaded event file* to start — the handler "
            "calls SetScriptNumber, not LoadScriptFile. In file 516, `script_id=5` means "
            "516_05. Use `load_script_file` first to switch files."
        ),
    },
    "script_start_stack": {
        "script_id": (
            "Which script *inside the currently loaded event file* to start, pushed onto "
            "the script stack. In file 516, `script_id=5` means 516_05, not event 5."
        ),
    },
    "load_script_file": {
        "force_high_bit": (
            "Argument bit 0, which forces bit 15 of the file id on. Command_06 masks the "
            "operand with 0x7FFF first, so this bit is the only way to set it."
        ),
        "file": "Which event file to load: the operand's low 15 bits.",
        "unused_bit15": "Discarded by the engine; kept only so an odd file still round-trips.",
        "unused_high": "Discarded by the engine; kept only so an odd file still round-trips.",
        "script_id": (
            "Which event file to load, as a global file id; this is the one script operand "
            "that names a file rather than a script inside the current one."
        ),
    },
    "character_collision_setup": {
        "float_array": "Raw word introducing the float-array block: `float_array_count` floats follow.",
        "float_array_values": "The collision float array written to the record.",
        "collision_flag": (
            "`on` sets, `off` clears one flag in the character's collision record; "
            "leave it out to leave the flag as it was. No mapped code reads the flag "
            "back, so the name says where it is written, not what it does."
        ),
        "collision_flag_mode": "Raw form of `collision_flag` for the unused mode 3.",
    },
    "talk_bustup_display": {
        "upper_arg": "High bits of the argument byte, preserved verbatim.",
    },
    "special_f0": {
        "raw": "The entire command word, which is the whole payload for opcodes >= 0xF0; the compile authority for this line.",
        "key": "Derived selector unpacked from `raw`, cross-checked when compiling.",
    },
    "window_message": {
        "window": (
            "Which window this line talks to. It becomes the first argument of "
            "`CRadiWindowManager::SendMessage(window_id, message, param)`, so it names "
            "a window such as the message box, the character-name display, the obi, the "
            "field-map name, or a battle window — not a piece of text."
        ),
        "message": (
            "What the window is being told to do; the second `SendMessage` argument. "
            "`7` is the only value the handler intercepts: it builds a parameter block "
            "from the words that follow and sends its own sequence instead."
        ),
        "subdispatch": (
            "Argument bit 0. It must be set for the `message=7` parameter-block path; "
            "without it the line forwards `window` and `message` straight through."
        ),
    },
    "person_allow_attribute": {
        "selector": "First CCharacterPerson::SetAllowAttribute argument, from arg bits 6-7.",
        "allow_a": "Arg bits 2-3; the handler passes this value minus 1, so 0 passes -1 (leave unchanged).",
        "allow_b": "Arg bits 4-5; the handler passes this value minus 1, so 0 passes -1 (leave unchanged).",
    },
    "chara_put_attach_life_flag": {
        "life_flag": "Life-flag id passed to CCharaPutManager::AttachLifeFlag (low half of the flag word).",
    },
    "packing_file_load_or_release": {
        "release": "1 releases data-center slot `slot` (arg bit 0 set); 0 loads packing file `file`.",
        "slot": "Data-center slot to release, from arg bits 4-7; the handler ignores slots above 7.",
        "file": "Packing file id passed to CRadiDataCenter::LoadPackingFile (low half of the operand word).",
    },
    "sound_field_e0_set": {
        "value": "Arg bits 0-3, stored to gpRadiSound+0xE0; the command has no operand words.",
    },
    "battle_character_fall_or_plugin_control": {
        "mode": "Jump-table mode from arg bits 0-5; see the Command_a0 branch trace for the per-mode payloads.",
        "bit7": "Arg bit 7: the boolean input of modes 7/8/9 (flag-bit writes) and 0xC (SetStsInvincible).",
    },
}

SOURCE_FIELD_TOKEN = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')

def source_field_tokens(line: str) -> tuple[str, list[tuple[str, str]]]:
    """Split a decompiled EVDSRC line into its directive and ordered fields."""
    stripped = line.strip()
    if not stripped or stripped.startswith(";") or stripped.startswith(".") or stripped.endswith(":"):
        return "", []
    head, _, rest = stripped.partition(" ")
    return head, [(m.group(1), m.group(2)) for m in SOURCE_FIELD_TOKEN.finditer(rest)]

def render_source_fields(directive: str, fields: list[tuple[str, str]]) -> str:
    return directive + "".join(f" {name}={value}" for name, value in fields)

def concise_source_command(line: str, command_bytes: bytes) -> str:
    """Drop fields that the command re-derives, keeping the assembled bytes identical.

    The decompiler prints a packed word and each named piece of it. That is useful
    for auditing but noisy to read, so this removes every field the compiler can
    rebuild. Fields are tried left to right, which drops the packed word and keeps
    the named pieces, and each removal is verified by reassembling the command.
    """
    directive, fields = source_field_tokens(line)
    if not directive or not fields or directive == "branch":
        return line
    indent = line[: len(line) - len(line.lstrip())]
    tail = 4  # the end_script terminator the probe appends

    def assembles(trial: list[tuple[str, str]]) -> bool:
        if not trial:
            return False
        try:
            out = compile_evd_source(
                SOURCE_FIELD_PROLOGUE + "  " + render_source_fields(directive, trial) + "\n  end_script\n"
            )
        except Exception:  # noqa: BLE001 - a failed trial simply means "cannot drop"
            return False
        return len(out) == 0x0C + len(command_bytes) + tail and out[0x0C : 0x0C + len(command_bytes)] == command_bytes

    if not assembles(fields):
        return line
    kept = list(fields)
    for name, _ in list(fields):
        trial = [item for item in kept if item[0] != name]
        if assembles(trial):
            kept = trial
    return indent + render_source_fields(directive, kept)

HIGH_LEVEL_COMMANDS: dict[str, dict[str, Any]] = {
    "load_character": {
        "form": "character_data",
        "control": "control",
        "defaults": {"mode": "0", "flags": "0x80"},
        "positional": ["character"],
        "params": {
            "character": {"emit": "character_number", "implies": {"explicit_char": "1"}},
            "model": {
                "bit": 0x0001,
                "packs": ("modeling", (("model", 0, 0xFF), ("variant", 8, 0xFF), ("load_mode", 16, 0xFFFF))),
            },
            "action": {"bit": 0x0002, "emit": "action"},
            "algorithm": {"bit": 0x0400, "emit": "algorithm"},
        },
        "companions": {"variant": "model", "load_mode": "model"},
    },
    "delete_character": {
        "form": "character_delete_data",
        "control": "control",
        "defaults": {"flag7": "0", "detach_data_mask": "0x00000000", "flags": "0x80"},
        "positional": ["character"],
        "params": {
            "character": {"emit": "character", "implies": {"explicit_char": "1"}},
            "delete": {"bit": 0x0001, "emit": "delete"},
        },
    },
    "set_flag": {
        "form": "set_flags",
        "defaults": {"flag_count": "1", "flag_values": "0x0001", "flags": "0x80"},
        "positional": ["flag"],
        "params": {"flag": {"emit": "first_flag"}},
    },
    "clear_flag": {
        "form": "set_flags",
        "defaults": {"flag_count": "1", "flag_values": "0x0000", "flags": "0x80"},
        "positional": ["flag"],
        "params": {"flag": {"emit": "first_flag"}},
    },
    "print_text": {
        "form": "text_output",
        "defaults": {"mode": "0", "flags": "0x80"},
        "positional": ["text"],
        "params": {"text": {"emit": "text"}},
    },
    "talk_rmf": {
        "form": "talk_rmf",
        "defaults": {"attach_mode": "2", "flag0": "0", "flag1": "0", "raw_high": "0x0000", "flags": "0x80"},
        "positional": ["message"],
        "params": {"message": {"emit": "message_id"}},
    },
    "load_background": {
        "form": "load_background",
        "defaults": {"slot": "0", "event_value": "0", "raw_high": "0x0000", "flags": "0x80"},
        "positional": ["background"],
        "params": {"background": {"emit": "id"}},
    },
    "delete_background": {
        "form": "delete_background",
        "positional": ["slot"],
        "params": {"slot": {"emit": "slot"}},
    },
    "select_camera": {
        "form": "camera_select",
        "defaults": {"select_slot": "1", "target_slot": "1", "target": "0x0001", "flags": "0x80"},
        "positional": ["camera"],
        "params": {"camera": {"emit": "camera"}, "target": {"emit": "target"}},
    },
    "set_music_volume": {
        "form": "set_bgm_volume",
        "defaults": {"slot": "2", "time": "0x0078", "flags": "0x80"},
        "positional": ["volume"],
        "params": {"volume": {"emit": "volume"}, "time": {"emit": "time"}},
    },
    "load_sound_file": {
        "form": "load_sound_resource",
        "defaults": {"mode": "9", "flags": "0x80"},
        "positional": ["resource"],
        "params": {"resource": {"emit": "resource_id"}},
    },
    "pause_character_move": {
        "form": "character_move_pause",
        "defaults": {"pause_arg": "2", "flags": "0x80"},
        "positional": ["character"],
        "params": {"character": {"emit": "character", "implies": {"explicit_char": "1"}}},
    },
    "set_motion_blend": {
        "form": "strong_motion_blend",
        "defaults": {"flags": "0x80"},
        "positional": ["character", "blend"],
        "params": {
            "character": {"emit": "character", "implies": {"explicit_char": "1"}},
            "blend": {"emit": "blend"},
        },
    },
    "set_character_attribute": {
        "form": "character_attribute",
        "defaults": {"flags": "0x80"},
        "positional": ["character"],
        "params": {
            "character": {"emit": "character", "implies": {"explicit_char": "1"}},
            "attribute": {"emit": "collision_attr"},
            "value": {"emit": "collision_value"},
        },
    },
    "precreate_character_animation": {
        "form": "character_precreate_anim",
        "defaults": {"mode": "1", "raw_high": "0x000000", "flags": "0x80"},
        "positional": ["character", "animation"],
        "params": {
            "character": {"emit": "character", "implies": {"explicit_char": "1"}},
            "animation": {"emit": "anim_id"},
        },
    },
    "set_camera_mode": {
        "form": "camera_mode",
        "defaults": {"flag4": "0", "flag5": "0", "flag6": "0", "flag7": "0", "rail_time": "0", "flags": "0x80"},
        "positional": ["mode"],
        "params": {"mode": {"emit": "mode"}},
    },
    "change_party": {
        "form": "party_member",
        "defaults": {"mode": "0", "flag7": "1", "raw_high": "0x0000", "flags": "0x80"},
        "positional": ["character"],
        "params": {"character": {"emit": "character"}},
    },
    "set_window_mode": {
        "form": "window_message_mode",
        "positional": ["arg"],
        "params": {"arg": {"emit": "arg"}},
    },
    # Flat command: no gating, every field is always present. The defaults are the
    # values the corpus always uses; 0xFF on blink/mouth appears to mean "leave
    # alone", which is inferred from usage rather than proven from the handler.
    "set_character_expression": {
        "form": "character_expression",
        "defaults": {
            "expression": "0x00",
            "blink": "0xFF",
            "mouth": "0xFF",
            "blink_half_steps": "0x00",
            "raw0_high": "0x00",
            "raw1_high": "0x00",
            "flags": "0x80",
        },
        "positional": ["character", "expression"],
        "params": {
            "character": {"emit": "character", "implies": {"explicit_char": "1"}},
            "expression": {"emit": "expression"},
            "blink": {"emit": "blink"},
            "mouth": {"emit": "mouth"},
            # The mouth arguments are only meaningful when a mouth shape is being
            # set; left out, the underlying form derives them from mouth (0xFF
            # means "leave unchanged", anything else means "no argument"). Giving
            # them a fixed default here would emit the wrong pair for every line
            # that names a real mouth shape.
            "mouth_arg0": {"emit": "mouth_arg0"},
            "mouth_arg1": {"emit": "mouth_arg1"},
        },
    },
    "set_character_render": {
        "form": "character_attach_render",
        "defaults": {"flag1": "0", "flag2": "0", "flag6": "0", "flag7": "0", "flags": "0x80"},
        "positional": ["character"],
        "params": {
            "character": {"emit": "character", "implies": {"explicit_char": "1"}},
            "flag1": {"emit": "flag1"},
            "flag2": {"emit": "flag2"},
            "flag6": {"emit": "flag6"},
            "flag7": {"emit": "flag7"},
        },
    },
    "detach_character_from_parent": {
        "form": "character_detach_parent",
        "defaults": {"flag4": "0", "flag6": "0", "flag7": "0", "flags": "0x80"},
        "positional": ["character"],
        "params": {"character": {"emit": "character", "implies": {"explicit_char": "1"}}},
    },
    "play_character_sub_animation": {
        "form": "character_sub_anim",
        "defaults": {"flags": "0x80"},
        "positional": ["character"],
        "params": {"character": {"emit": "character_number", "implies": {"explicit_char": "1"}}},
    },
    # move_selector 3 is the only value that carries a time word, so supplying a
    # time selects it. Derived from the corpus: 1573 lines with selector 3 all have
    # manual_time_word, and no line with another selector does.
    "set_character_eyes": {
        "form": "character_eye_control",
        "defaults": {
            "eye_ball": "0",
            "eye_move": "1",
            "eye_ball_byte": "0x00",
            "move_selector": "0x05",
            "manual_x_s8": "0",
            "manual_y_s8": "0",
            "flags": "0x80",
        },
        "positional": ["character"],
        "params": {
            "character": {"emit": "character", "implies": {"explicit_char": "1"}},
            "x": {"emit": "manual_x_s8"},
            "y": {"emit": "manual_y_s8"},
            "time": {"emit": "manual_time_word", "implies": {"move_selector": "0x03"}},
            "selector": {"emit": "move_selector"},
        },
    },
    # character_rotate_option selects its behaviour with `mode = control & 0x0F` and
    # `option = (control >> 9) & 1`, indexing ROTATE_OPTION_MODE_ACTIONS, so each
    # action gets its own command. Bits 6-8 hold one axis mask shared by every vector
    # on the line, derived here from the target vector; bit 0x1000 gates speed_values.
    "rotate_character_to_posture": {
        "form": "character_rotate_option",
        "control": "control",
        "control_base": 0x00010008,  # duration 1, mode 8, option 0
        "defaults": {"flags": "0x80"},
        "positional": ["character", "posture"],
        "params": {
            "character": {"emit": "character", "implies": {"explicit_char": "1"}},
            "posture": {"emit": "target_posture_vec", "axis_mask": 6},
            "speed": {"bit": 0x1000, "emit": "speed_values"},
        },
        "axes_follow": {"initial_vec": "posture"},
    },
    # Remaining rotate actions. Each carries the control value observed for it in the
    # corpus, including that value's axis mask, with initial_vec written to match.
    "set_character_head_angle": {
        "form": "character_rotate_option",
        "control": "control",
        "control_base": 0x000102C8,  # mode 8, option 1, axis mask 3
        "defaults": {"initial_vec": "x:0,y:0", "flags": "0x80"},
        "positional": ["character", "angle"],
        "params": {
            "character": {"emit": "character", "implies": {"explicit_char": "1"}},
            "angle": {"emit": "head_angle_add"},
            "speed": {"bit": 0x1000, "emit": "speed_values"},
        },
    },
    "rotate_character_to_character": {
        "form": "character_rotate_option",
        "control": "control",
        "control_base": 0x00010091,  # mode 1, option 0, axis mask 2
        "defaults": {"initial_vec": "y:0", "flags": "0x80"},
        "positional": ["character", "target"],
        "params": {
            "character": {"emit": "character", "implies": {"explicit_char": "1"}},
            "target": {
                "emit": "target_character_pair",
                # bit 0 is explicit_char, bit 1 says the target follows in the stream
                "implies": {"target_char_from_stream": "1", "arg": "0x03"},
            },
            "speed": {"bit": 0x1000, "emit": "speed_values"},
        },
    },
    "rotate_character_to_stand_position": {
        "form": "character_rotate_option",
        "control": "control",
        "control_base": 0x000101CB,  # mode 11, option 0, axis mask 7
        "defaults": {"initial_vec": "x:0,y:0,z:0", "flags": "0x80"},
        "positional": ["character", "stand"],
        "params": {
            "character": {"emit": "character", "implies": {"explicit_char": "1"}},
            "stand": {"emit": "stand"},
            "speed": {"bit": 0x1000, "emit": "speed_values"},
        },
    },
    "reset_character_rotate_capture": {
        "form": "character_rotate_option",
        "control": "control",
        "control_base": 0x000102DF,  # mode 15, option 1, axis mask 3
        "defaults": {"initial_vec": "x:0,y:0", "flags": "0x80"},
        "positional": ["character"],
        "params": {"character": {"emit": "character", "implies": {"explicit_char": "1"}}},
    },
    "rotate_character_to_current": {
        "form": "character_rotate_option",
        "control": "control",
        "control_base": 0x0001009F,  # mode 15, option 0, axis mask 2
        "defaults": {"initial_vec": "y:0", "flags": "0x80"},
        "positional": ["character"],
        "params": {
            "character": {"emit": "character", "implies": {"explicit_char": "1"}},
            "speed": {"bit": 0x1000, "emit": "speed_values"},
        },
    },
    "rotate_character_to_vector": {
        "form": "character_rotate_option",
        "control": "control",
        "control_base": 0x000102C0,  # mode 0, option 1, axis mask 3
        "defaults": {"initial_vec": "x:0,y:0", "flags": "0x80"},
        "positional": ["character", "vector"],
        "params": {
            "character": {"emit": "character", "implies": {"explicit_char": "1"}},
            "vector": {"emit": "target_vector"},
            "speed": {"bit": 0x1000, "emit": "speed_values"},
        },
    },
    # Modes 2, 3, 4, 6 and 7 share one payload shape and cover essentially the whole
    # corpus. Mode 14 takes duration/end_color instead (10 lines) and the rare
    # float0 variant (7 lines) are both left to the raw form.
    "animate_camera_color": {
        "form": "camera_color_anim",
        "defaults": {"low": "0x00000000", "flags": "0x80"},
        "positional": ["mode", "float1", "float2"],
        "params": {
            "mode": {"emit": "mode"},
            "float1": {"emit": "float1"},
            "float2": {"emit": "float2"},
            "low": {"emit": "low"},
        },
    },
    # Each animated property has an enable bit and a "has start value" bit in the
    # control word, and the payload words follow in handler test order. Supplying a
    # `_to` sets the enable bit; supplying a `_from` sets both, because the start
    # value is only read when the enable bit is also set.
    "animate_character_material": {
        "form": "character_auto_rate_anim",
        "control": "control",
        "defaults": {
            "duration_word": "0x00000000",
            "event_duration": "0",
            "mode": "0",
            "with_child": "0",
            "flags": "0x80",
        },
        "positional": ["character"],
        "params": {
            "character": {"emit": "character", "implies": {"explicit_char": "1"}},
            "duration": {"emit": "duration_word"},
            "color_to": {"bit": 0x00002, "emit": "color_end_vec"},
            "color_from": {"bit": 0x00003, "emit": "color_start_vec"},
            "transparent_to": {"bit": 0x00040, "emit": "transparent_to"},
            "transparent_from": {"bit": 0x00060, "emit": "transparent_from"},
            "scale_to": {"bit": 0x00800, "emit": "scale_end_vec"},
            "scale_from": {"bit": 0x00C00, "emit": "scale_start_vec"},
            "palette_to": {"bit": 0x04000, "emit": "palette_to"},
            "palette_from": {"bit": 0x06000, "emit": "palette_from"},
            "palette_id": {"emit": "palette_id"},
            "visibility_to": {"bit": 0x20000, "emit": "visibility_to"},
            "visibility_from": {"bit": 0x30000, "emit": "visibility_from"},
        },
        "companions": {"palette_id": "palette_to"},
    },
    # Corpus shows mode 1 throughout, with two vectors and three flags. The rare
    # field58_value variant (20 of 1839 lines) is left to the raw form.
    "set_camera_transform": {
        "form": "camera_transform_param",
        "defaults": {"mode": "1", "flag2": "0", "flag3": "0", "flag4": "0", "flags": "0x80"},
        "positional": ["initial", "post"],
        "params": {
            "initial": {"emit": "initial_vec"},
            "post": {"emit": "post_vec", "implies": {"flag2": "1"}},
            "flag2": {"emit": "flag2"},
            "flag3": {"emit": "flag3"},
            "flag4": {"emit": "flag4"},
        },
    },
    "set_text_layout": {
        "form": "text_message_layout",
        "defaults": {"word00": "0x80808080", "word08": "0x00000000", "flags": "0x80"},
        "positional": ["x", "y"],
        "params": {
            "x": {"emit": "x"},
            "y": {"emit": "y"},
            "word00": {"emit": "word00"},
            "word08": {"emit": "word08"},
        },
    },
    "play_vibration": {
        "form": "play_vibration",
        "defaults": {"strength": "0x00", "pattern": "0x00", "duration": "0x003C", "flags": "0x80"},
        "positional": ["strength", "pattern"],
        "params": {
            "strength": {"emit": "strength"},
            "pattern": {"emit": "pattern"},
            "duration": {"emit": "duration"},
        },
    },
    "stop_vibration": {"form": "vibration_stop", "params": {}},
    "load_texture": {
        "form": "load_texture",
        "defaults": {"mode": "5", "group": "0x00", "raw_byte3": "0x00", "flags": "0x80"},
        "positional": ["texture"],
        "params": {"texture": {"emit": "texture"}, "group": {"emit": "group"}},
    },
    "load_paf": {
        "form": "load_paf",
        "defaults": {"mode": "5", "raw_high": "0x0000", "flags": "0x80"},
        "positional": ["paf"],
        "params": {"paf": {"emit": "paf"}},
    },
    "stop_sound": {
        "form": "stop_sound_effect",
        "defaults": {"mode": "0", "stop_mode": "0x4", "bank": "0x00", "selector": "0x00", "flags": "0x80"},
        "positional": ["sound"],
        "params": {
            "sound": {"emit": "sound_id"},
            "bank": {"emit": "bank"},
            "selector": {"emit": "selector"},
        },
    },
    "set_sound_listener": {
        "form": "sound_listener",
        "defaults": {
            "target_source": "0",
            "manager_listener": "0",
            "positive_distance": "0",
            "target": "camera",
            "flags": "0x80",
        },
        "positional": ["mode"],
        "params": {"mode": {"emit": "mode"}, "target": {"emit": "target"}},
    },
    # control_high/control_index/control_flag_bits select the sound bank and slot;
    # the values here are the ones event scripts use for a plain one-shot effect.
    "play_sound_file": {
        "form": "play_sound_effect",
        "defaults": {
            "mode": "0",
            "submode": "0",
            "explicit_char": "0",
            "control_high": "0x0029",
            "playse_stack0": "0",
            "control_index": "9",
            "control_flag_bits": "1",
            "playse_arg5": "0",
            "playse_arg6": "80",
            "playse_arg7": "64",
            "flags": "0x80",
        },
        "positional": ["sound"],
        "params": {
            "sound": {"emit": "sound_id"},
            "volume": {"emit": "playse_arg6"},
            "pan": {"emit": "playse_arg7"},
        },
    },
    "wait_for": {
        "form": "trigger",
        # A wait must yield: the keep-going bit stays clear so the script
        # pauses here until the trigger fires.
        "defaults": {"action": "0", "raw_mid": "0x00", "trigger_flags": "0x0000", "flags": "0x00"},
        "positional": ["trigger_type"],
        "params": {
            "trigger_type": {"emit": "trigger_type"},
            "value": {"emit": "trigger_value"},
        },
    },
    "fade_screen": {
        "form": "fade_control",
        "defaults": {"mode": "0", "fade_flags": "0x0", "flags": "0x80"},
        "positional": ["control"],
        "params": {"control": {"emit": "control"}, "duration": {"emit": "duration"}, "id": {"emit": "id"}},
    },
    "set_game_clock": {
        "form": "set_radiata_time",
        "defaults": {"byte0": "0", "value": "0xFFFF", "flags": "0x00"},
        "positional": ["hour"],
        "params": {"hour": {"emit": "byte1"}, "value": {"emit": "value"}},
    },
    "change_game_mode": {
        "form": "change_game_mode",
        "positional": ["mode"],
        "params": {"mode": {"emit": "mode"}},
    },
    "stop_or_pause_music": {
        "form": "bgm_control",
        "defaults": {"pause": "0", "value": "0x000F", "raw_high": "0x0000", "flags": "0x80"},
        "positional": ["mode"],
        "params": {
            "mode": {"emit": "mode"},
            "pause": {"emit": "pause"},
            "value": {"emit": "value"},
        },
    },
    "stop_background_animation": {
        "form": "background_stop_animation",
        "defaults": {"name_source": "1", "stop_mode": "2", "flags": "0x80"},
        "positional": ["name"],
        "params": {"name": {"emit": "name"}, "stop_mode": {"emit": "stop_mode"}},
    },
    "setup_character_collision": {
        "form": "character_collision_setup",
        # No control default: the underlying form derives the presence mask from
        # the fields that are actually supplied, and a fixed default here would
        # bake a collision_flag action into every call that did not ask for one.
        "defaults": {"flags": "0x80"},
        "positional": ["character"],
        "params": {
            "character": {"emit": "character", "implies": {"explicit_char": "1"}},
            "collision_flag": {"emit": "collision_flag"},
            "control": {"emit": "control"},
        },
    },
    "call_character_virtual": {
        "form": "character_virtual_24",
        "defaults": {"control": "0x00000008", "flags": "0x80"},
        "positional": ["character"],
        "params": {
            "character": {"emit": "character_number", "implies": {"explicit_char": "1"}},
            "control": {"emit": "control"},
        },
    },
    "set_camera_flags": {
        "form": "camera_flags",
        "defaults": {"flag0": "0", "flag1": "0", "flag2": "0", "flags": "0x80"},
        "params": {
            "flag0": {"emit": "flag0"},
            "flag1": {"emit": "flag1"},
            "flag2": {"emit": "flag2"},
        },
    },
    # The builder already accepts item_id/quantity in place of the packed
    # operand halves, so this wrapper only has to supply the flag defaults.
    # `quantity` is the signed high half of the operand.
    "change_inventory": {
        "form": "personal_inventory",
        "defaults": {
            "mode": "0",
            "flag5": "0",
            "event40": "0",
            "event80": "0",
            "flags": "0x80",
        },
        "positional": ["character", "item"],
        "params": {
            "character": {"emit": "character", "implies": {"explicit_char": "1"}},
            "item": {"emit": "item_id"},
            "quantity": {"emit": "quantity"},
        },
    },
    # `source` picks which operand form follows, so it is derived from which of
    # at/stand/target the author supplied rather than written by hand. The values
    # come from the corpus: 4 is a literal position, 3 a stand id, 1 a target.
    "set_character_position": {
        "form": "character_move_position",
        "defaults": {
            "mode": "0",
            "coord": "0",
            "source": "4",
            "control": "0x00000000",
            "flags": "0x80",
        },
        "positional": ["character", "at"],
        "params": {
            "character": {"emit": "character", "implies": {"explicit_char": "1"}},
            "at": {"emit": "inline_vec", "implies": {"source": "4"}},
            "stand": {"emit": "stand", "implies": {"source": "3"}},
            "target": {"emit": "target", "implies": {"source": "1"}},
            "offset": {"emit": "offset_vec", "implies": {"coord": "2"}},
        },
    },
    # The optional floats are gated by their own flag fields rather than by a
    # control mask, so supplying one sets its gate through `implies`.
    "play_character_animation": {
        "form": "character_animation",
        "defaults": {
            "speed0": "0",
            "speed1": "0",
            "blend": "0",
            "speed2": "0",
            "has_extra_animation": "0",
            "sub_anim_mode": "0",
            "animation_group": "0x00000000",
            "flags": "0x80",
        },
        "positional": ["character", "animation"],
        "params": {
            "character": {"emit": "character", "implies": {"explicit_char": "1"}},
            "animation": {"emit": "animation"},
            "group": {"emit": "animation_group"},
            "float_a": {"emit": "anim_float_a", "implies": {"speed0": "1"}},
            "float_b": {"emit": "anim_float_b", "implies": {"speed1": "1"}},
            "float_c": {"emit": "anim_float_c", "implies": {"blend": "1"}},
            "speed": {"emit": "play_speed", "implies": {"speed2": "1"}},
            "extra": {"emit": "extra_animation", "implies": {"has_extra_animation": "1"}},
        },
    },
}

HIGH_LEVEL_NAME_ALIASES: dict[str, str] = {"say": "talk_rmf"}

def high_level_call_matches(name: str, kwargs: dict[str, str]) -> bool:
    """Whether a call should use the authoring sugar rather than the raw form.

    A high-level name can also be the friendly alias of the underlying form, so a
    decompiled line ends up here too. Those carry engine fields the sugar does not
    define, and must fall through to the passthrough so round-trips stay exact.
    """
    spec = HIGH_LEVEL_COMMANDS[HIGH_LEVEL_NAME_ALIASES.get(name, name)]
    # Only the authoring parameters count. The defaults are engine field names, and
    # accepting those here would let a decompiled line be captured by the sugar.
    known = set(spec["params"]) | set(spec.get("companions", {}))
    return set(kwargs) <= known

def build_high_level_command(name: str, args: list[str], kwargs: dict[str, str]) -> str:
    """Lower one high-level authoring call to a single EVDSRC line.

    The control mask is derived from which parameters the author supplied, so
    adding or removing a value cannot leave the mask out of step with the payload.
    """
    name = HIGH_LEVEL_NAME_ALIASES.get(name, name)
    spec = HIGH_LEVEL_COMMANDS[name]
    params: dict[str, Any] = spec["params"]
    supplied = dict(kwargs)
    for index, positional in enumerate(spec.get("positional", [])):
        if index < len(args):
            if positional in supplied:
                raise ValueError(f"{name}() got {positional} both positionally and by name")
            supplied[positional] = args[index]
    if len(args) > len(spec.get("positional", [])):
        raise ValueError(f"{name}() takes at most {len(spec.get('positional', []))} positional argument(s)")

    companions = spec.get("companions", {})
    unknown = sorted(set(supplied) - set(params) - set(companions) - set(spec.get("defaults", {})))
    if unknown:
        known = ", ".join(sorted(set(params) | set(companions)))
        raise ValueError(f"{name}() unknown parameter(s): {', '.join(unknown)}. Known: {known}")
    for companion, owner in companions.items():
        if companion in supplied and owner not in supplied:
            raise ValueError(f"{name}() {companion} needs {owner} as well")

    fields: dict[str, str] = dict(spec.get("defaults", {}))
    # Some commands carry a fixed base in the control word that identifies which
    # action they perform; supplied parameters then OR their gate bits onto it.
    control = int(spec.get("control_base", 0))
    for param, rule in params.items():
        if param not in supplied:
            continue
        if supplied[param] == "default" and rule.get("implies", {}).get("explicit_char") == "1":
            # `character=default` selects the script's default character: no
            # selector word is emitted and explicit_char stays 0.
            fields["explicit_char"] = "0"
            continue
        if "bit" in rule:
            control |= rule["bit"]
        if "axis_mask" in rule:
            # The control word records which axes a vector carries, so derive it
            # from the value rather than making the caller state it twice.
            shift = int(rule["axis_mask"])
            axes = 0
            for index, axis in enumerate(AXES):
                if re.search(rf"(?:^|,)\s*{axis}\s*:", supplied[param]):
                    axes |= 1 << index
            control |= axes << shift
        fields.update(rule.get("implies", {}))
        if "packs" in rule:
            target, layout = rule["packs"]
            word = 0
            for part, shift, mask in layout:
                raw = supplied.get(part, "0") if part != param else supplied[param]
                value = parse_hex_int(raw)
                if value & ~mask:
                    raise ValueError(f"{name}() {part}={raw} does not fit in mask 0x{mask:X}")
                word |= (value & mask) << shift
            fields[target] = f"0x{word:08X}"
        elif "emit" in rule:
            fields[rule["emit"]] = supplied[param]
    for key, value in supplied.items():
        if key in spec.get("defaults", {}) and key not in params:
            fields[key] = value
    if spec.get("control") is not None:
        fields[spec["control"]] = f"0x{control:08X}"

    # Some commands share one axis mask across several vectors, so a companion
    # vector has to carry exactly the axes the mask names. Generate it as zeros
    # matching the vector the mask was derived from.
    for target, source in spec.get("axes_follow", {}).items():
        if source not in supplied:
            continue
        axes = [axis for axis in AXES if re.search(rf"(?:^|,)\s*{axis}\s*:", supplied[source])]
        fields[target] = ",".join(f"{axis}:0" for axis in axes)

    rendered = " ".join(f"{key}={value}" for key, value in fields.items())
    return f"{spec['form']} {rendered}".rstrip()

for _form, _notes in {
    "fade_control": {
        "direction": (
            "`out` fades the screen to the colour, `in` fades from it. Control bit 0 "
            "picks which end of the colour pair carries the alpha byte."
        ),
        "hold": (
            "Keep the finished fade on screen instead of releasing it (control bit 1). "
            "A fade-to-black needs this or the screen uncovers itself."
        ),
        "color": (
            "Fade colour: `black`, `white`, `current` (the global fade colour), or an "
            "explicit 0xAARRGGBB word."
        ),
        "curve": "Interpolation curve index; the engine scales the duration through a per-curve table.",
    },
    "play_sound_effect": {
        "volume": "Playback volume, 0-127. Shipped scripts overwhelmingly use 127.",
        "pan": "Stereo position; 64 is centre and is what almost every line uses.",
    },
    "background_play_animation": {
        "include_children": "Also play the animation on the object's child nodes.",
        "children_share_range": "Children play the same frame range as the parent rather than their own.",
        "restart_if_playing": "Restart from the beginning if this animation is already running.",
        "frames_are_keys": "Treat the frame numbers as key indices rather than frame counts.",
        "repeat": "How many extra times to loop: `0` plays once, `forever` loops until stopped.",
    },
    "battle_acquisition_setup": {
        "battle_map": "Map the battle loads.",
        "battle_bgm": "Music the battle plays.",
        "battle_script": "Event script the battle runs, loaded as `id | 0x8000`.",
        "battle_event_file": (
            "Event file the battle loads. Command_16 writes it to app+0x84C, where "
            "CGame's battle path passes it to CRadiScript::LoadScriptFile; a non-zero "
            "value also makes CBackGround::SettingMap skip the map's own init script."
        ),
        "battle_event_script": (
            "Script number inside `battle_event_file` to run. Written to app+0x84E and "
            "passed to CRadiScript::SetScriptNumber as the script selector, which stashes "
            "it in the new slot while the file is still loading. Together the two name one "
            "script the way the files do: file 500 script 3 is 500_03."
        ),
        "finish_check1": "Battle-end condition preset checked by `CBtlFinishCheck`.",
        "finish_check2": "Second battle-end condition preset.",
        "finish_check3": "Third battle-end condition preset.",
        "finish_param1": "Operand for `finish_check1`; its meaning depends on the preset.",
        "finish_param2": "Operand for `finish_check2`.",
        "finish_param3": "Operand for `finish_check3`.",
        "unused_2824": (
            "Written to the global at 0x3B2824 and never read: a scan of every module finds "
            "exactly one access to that address, this handler's store. Preserved verbatim."
        ),
        "unused_2826": (
            "Written to the global at 0x3B2826 and never read: a scan of every module finds "
            "exactly one access to that address, this handler's store. Preserved verbatim."
        ),
    },
    "set_radiata_time": {
        "minute": "Minute passed to `CRadiApp::SetRadiataTime`.",
        "hour": "Hour passed to `CRadiApp::SetRadiataTime`.",
        "day": "Day passed to `CRadiApp::SetRadiataTime`.",
    },
    "script_start": {
        "inherit_character": "The started script uses this script's default character instead of its own.",
        "inherit_values": "The started script inherits this script's event values.",
    },
    "sprite_config": {
        "xy": "Top-left corner of the sprite, as `x,y` screen coordinates.",
        "size": "Sprite size as `width,height`.",
        "time_base": "Timing base word for the sprite's animation.",
        "texture_page": "Which texture page and palette the sprite draws from.",
        "blend_alpha": "Blend parameters applied when the sprite is drawn.",
    },
    "expr": {
        "stand_component": "Which component of a stand position the value comes from.",
        "value_from_stand": "Read the operand from a stand position instead of a literal.",
    },
    "character_rotate_option": {
        "target_vector": "Direction the character turns toward.",
        "head_angle_add": "Extra head angle added on top of the body rotation, as `x:..,y:..`.",
        "speed_values": "Rotation speed operands, in the order the handler consumes them.",
    },
    "character_eye_control": {
        "move_action": "Which eye-movement mode `CEyeControl` is put into.",
    },
    "character_event_leave": {
        "characters": "The characters this line adds or removes, as `id:variant` pairs.",
        "character_pairs": "Raw form of `characters`, one packed word per pair.",
    },
    "person_schedule_list": {
        "schedule_list": "Schedule list number passed to `SetScheduleListNumber`.",
    },
    "sound_listener": {
        "position": "Listener position operand.",
        "tail": "Remaining operand words for this line, kept in order.",
    },
    "personal_inventory": {
        "quantity": "How many of the item to add or remove.",
    },
    "character_animation": {
        "animation_variant": "Variant selector within the chosen animation.",
    },
    "character_movement": {
        "optional_vec0": "Optional vector operand, present when its control bit is set.",
        "vec110": "Vector written to the movement record at +0x110.",
    },
    "background_auto_rate_anim": {
        "control_bytes": "The control word split into its four bytes, for readability.",
    },
    "branch": {
        "condition_args": "The condition's operand words, kept verbatim when no structured form applies.",
        "time_components": "Human-readable breakdown of the compared time value.",
    },
    "window_message": {
        "params": "The remaining operand words for this message, kept in order.",
    },
    "position_vibration_vector": {
        "params": "The remaining operand words for this line, kept in order.",
    },
    "camera_color_anim": {
        "kind": "Which colour this line animates: fog or ambient.",
    },
    "character_move_points": {
        "points": "The waypoints the character walks through, in order.",
    },
    "camera_move_existing": {
        "points": "The waypoints the camera moves through, in order.",
    },
}.items():
    FORM_PARAMETER_NOTES.setdefault(_form, {}).update(_notes)

del _form, _notes

for _form, _notes in {
    "talk_bustup_display": {
        "action": (
            "What to do with the portrait, from `(arg >> 6)`. "
            "`CTalkBustupTotalControl::BustupDisp` branches on exactly three values: "
            "0 hides it (`CTalkBustup::SetVisiblity(false)`), 1 creates or "
            "reinitialises it (`Create`), 2 erases it (`EraseStart`). Any other value "
            "does nothing."
        ),
        "portrait_slot": (
            "Which of the two portrait slots this acts on, from `(arg >> 1) & 1`. "
            "`Create` indexes the pointer array at `controller + 0x3C + slot * 4` with it."
        ),
        "portrait_variant": (
            "Presentation variant, from `(arg >> 4) & 3`, forwarded to `Create`, "
            "`CTalkBustup::Init` and `ReInit`. Which variant each value selects is not "
            "established -- only that it is not the hide/create/erase action."
        ),
        "erase_duration": (
            "Seconds the erase takes. Consumed only on action 2, as "
            "`CTalkBustup::EraseStart(2.0, value)`; the handler supplies 5.0 when the "
            "line omits it."
        ),
        "has_erase_duration": "Argument bit 2: whether an `erase_duration` float follows on this line.",
    },
    "play_vibration": {
        "motor_flag": (
            "One bit, from bits 8-15 of the word. `CVibPlayer::PlayVibration` masks the "
            "argument with `& 1` and stores that bit beside the strength byte in the "
            "vibration record; there is no pattern lookup, and the other seven bits are "
            "discarded. Shipped scripts only ever write 0 or 1."
        ),
        "duration": (
            "How long the vibration runs, from bits 16-31 of the word. This is the value "
            "itself, not a rendering of another field."
        ),
    },
    "set_radiata_time": {
        "hour": (
            "Hour to set. Command_c5 normalises it modulo 24 and stores it in the app "
            "clock at +0x848; it is not passed to CRadiApp::SetRadiataTime, which only "
            "the day path calls. 0xFF derives the current hour from game time first."
        ),
        "minute": (
            "Minute to set. Command_c5 normalises it modulo 60 and stores it in the app "
            "clock at +0x849; it is not passed to CRadiApp::SetRadiataTime, which only "
            "the day path calls. 0xFF derives the current minute from game time first."
        ),
        "day_1based": (
            "Day to set, counted from 1: the handler passes `value - 1` to "
            "`CRadiApp::SetRadiataTime`. Two values are sentinels -- 0 makes no day call "
            "at all, and 0xFFFF (printed `resync`) derives the day from accumulated "
            "game time."
        ),
    },
    # The shared `duration` glossary line says it is a rendering of a
    # `duration_word` and to edit that instead. On these forms `duration` is
    # itself the authoritative input and no `duration_word` is published, so the
    # advice points at a field that does not exist.
    "fade_control": {
        "duration": "How long the fade takes. This is the value itself, not a rendering of another field.",
    },
    "camera_move_etc": {
        "duration": "How long the camera move takes. This is the value itself, not a rendering of another field.",
    },
    "camera_transform_param": {
        "duration": "How long the transform takes. This is the value itself, not a rendering of another field.",
    },
    "time_schedule_value": {
        "packed_time": (
            "A packed time/calendar tuple, not a duration: five components of 6, 6, 6, 5 "
            "and 9 bits from the low end up, each forwarded to `CRadiApp::SetRadiataTime`. "
            "A component of all ones is the leave-unchanged sentinel and reaches the call "
            "as -1."
        ),
        "part0": "Bits 0-5 of `packed_time`; 0x3F leaves that component unchanged.",
        "part1": "Bits 6-11 of `packed_time`; 0x3F leaves that component unchanged.",
        "part2": "Bits 12-17 of `packed_time`; 0x3F leaves that component unchanged.",
        "part3": "Bits 18-22 of `packed_time`; 0x1F leaves that component unchanged.",
        "part4": "Bits 23-31 of `packed_time`; 0x1FF leaves that component unchanged.",
        "range_check": (
            "Argument bit 4. When set and `part4` is not its sentinel, the handler raises "
            "a script error and forces that component to -1. It gates a range check on the "
            "top component; it is not a duration."
        ),
        "operation": (
            "Argument bits 2-3, choosing what the command does: 0 sets the time from "
            "`packed_time`, 1 writes the selected script time value out to an event value, "
            "2 reads an event value into the selected script time slot."
        ),
    },
}.items():
    FORM_PARAMETER_NOTES.setdefault(_form, {}).update(_notes)

del _form, _notes

for _form, _notes in {
    "anim_frame_trigger": {
        "frame": (
            "Signed 20-bit animation frame from bits 8-27. Inert to the script stepper: CCharacterAnim::ChangeFrame_sub walks the marker table and fires the commands at this marker when the playing animation reaches this frame."
        ),
    },
    "set_schedule_percent": {
        "percent": (
            "Schedule percentage, `(word >> 8) & 0xFFFFF`, passed straight to CCharacterPerson::SetSchedulePercent by Command_f0."
        ),
    },
    "background_auto_rate_anim": {
        "duration_event": (
            "Low half of the duration word, read as a script event-value id when `event_duration` selects an event-derived duration instead of a literal time."
        ),
    },
    "battle_volty_distance": {
        "character_a": (
            "First character selector resolved by Command_d4, present when the line supplies one rather than using the script's default."
        ),
        "character_b": (
            "Second character selector, the other end of the distance being measured."
        ),
        "distance": (
            "Float passed to CbtlEtc::Script_CalcVoltyDistance alongside the two characters. The handler adds it to the computed difference, so it reads as an offset applied to the measured distance rather than the distance itself."
        ),
    },
    "character_collision_control_d1": {
        "scale_character": (
            "A character selector read from the stream and used as the scale source. It occupies the same stream slot as an inline object name, so a line carries one or the other, never both."
        ),
    },
    "character_script_param_holder": {
        "to_event": (
            "Script event-value id the result is written to, taken from the high half of the selector word."
        ),
    },
    "character_movement": {
        "move_control_mid": (
            "Middle 16 bits of the movement control word, split out for readability. Which of its bits the handler acts on is not established; it is preserved verbatim."
        ),
        "optional_vec2": (
            "A three-component vector present only when its control bit is set; it follows the earlier optional vectors in the stream."
        ),
        "throw_params": (
            "Four words of throw parameters passed to CCharacterMove::ThrowPosition. The individual fields are not separated out."
        ),
        "vec60": (
            "Three-component vector written to the movement record at +0x60. Its purpose is not established, so it is carried through as stored."
        ),
    },
    "conditional_end": {
        "compare": (
            "Comparison the condition applies, shared with `branch`: `eq`, `ne`, `ge`, `le`, `gt` or `lt`."
        ),
        "condition_args": (
            "The condition's two payload words, kept verbatim when no structured form applies to this condition id."
        ),
        "time_components": (
            "Human-readable breakdown of the compared clock value; derived from the condition payload rather than set on its own."
        ),
    },
    "background_play_animation": {
        "sync_to_character": (
            "Control bit 0x8000: the animation is synchronised to a character rather than played free-running."
        ),
    },
    "primitive_play_paf": {
        "offset": (
            "Signed 16-bit playback offset from the low half of the offset word, passed to CPrimitiveAnimation::SetOffset. Present only when the control bit that announces it is set."
        ),
    },
    "return_zero": {
        "header": (
            "The command's raw 32-bit header word. The handler returns immediately without reading operands, so the header is all there is to preserve."
        ),
    },
    "background_runtime_field": {
        "field10_u64": (
            "A 64-bit value written to a background runtime field. No reader has been traced, so it is preserved verbatim rather than named."
        ),
        "radi_180": (
            "Low half of the word written to the background runtime field at +0x180; `radi_180_raw_high` carries the upper half, whose meaning is not established."
        ),
    },
    "camera_capture_target": {
        "coeff_pair": (
            "Two floats present when control bit 5 is set, passed to the camera capture coefficient call as a pair."
        ),
    },
    "position_vibration_param": {
        "params": (
            "The raw parameter words handed to CVibrationVector::SetParam. The overload taken depends on how many are present; the individual fields are not separated out."
        ),
    },
    "script_defaults": {
        "object_name_null": (
            "Set when the line's object-name slot is all zeros, meaning no default object name. Derived from the name words, not set on its own."
        ),
    },
    "script_start": {
        "inherit_floats": (
            "Control bit 3: the started script inherits the caller's float and angle slots, copied from SCR_DATA +0x60 and +0x70."
        ),
    },
    "script_stop": {
        "condition_args": (
            "The condition's payload words, kept verbatim when no structured form applies to this condition id."
        ),
    },
    "trigger": {
        "payload": (
            "The trigger's operand words after the type and flags, kept verbatim; what each word means depends on the trigger type."
        ),
    },
}.items():
    FORM_PARAMETER_NOTES.setdefault(_form, {}).update(_notes)

del _form, _notes

PARAMETER_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^flag(\d)$", "Boolean option {0} taken from the handler's argument byte."),
    (r"^float(\d)$", "Float parameter {0} consumed by this command."),
    (r"^(.*)_from$", "Start value for `{0}`; the animation runs from here to the matching `_to`."),
    (r"^(.*)_to$", "End value for `{0}`; the animation runs from the matching `_from` to here."),
    (r"^(.*)_vec$", "Three-component vector (x, y, z) for `{0}`."),
    (r"^(.*)_mask$", "Bitmask selecting which parts of `{0}` apply."),
    (r"^(.*)_bits$", "Names of the bits set in the matching packed value."),
    (r"^(.*)_text$", "Human-readable rendering of `{0}`. Edit the value it derives from."),
    (r"^(.*)_words$", "Raw payload words for `{0}`, kept verbatim."),
    (r"^(.*)_count$", "How many `{0}` entries follow on this line."),
    (r"^(.*)_id$", "Identifier selecting which `{0}` to use."),
    (r"^(.*)_mode$", "Mode selector for `{0}`; the handler branches on it."),
    (r"^(.*)_control$", "Bitmask controlling which optional `{0}` values follow."),
    (r"^(.*)_slot$", "Index into the runtime table of `{0}`."),
    (r"^slot(\d*)$", "Index into a runtime table owned by this command's subsystem."),
    (r"^(?:has|uses)_(.*)$", "Whether `{0}` is present on this line."),
    (r"^(.*)_name$", "Name operand for `{0}`."),
    (
        r"^(.*)_fill$",
        "Byte filling the `{0}` slot after its terminator. The engine stops at the "
        "terminator so this never changes behaviour, but the original tool left 0xCC "
        "there and it has to be preserved. Omit it for a zero-filled slot.",
    ),
    (r"^(.*)_float(\d*)$", "Float parameter for `{0}`."),
    (r"^(.*)_high$", "Upper part of the matching packed value for `{0}`."),
    (r"^(.*)_low$", "Lower part of the matching packed value for `{0}`."),
    (r"^(.*)_offset$", "Offset applied to `{0}`."),
    (r"^(.*)_rate$", "Rate at which `{0}` changes."),
    (r"^(.*)_time$", "Duration, in the units this command uses, for `{0}`."),
    (r"^(.*)_index$", "Index selecting which `{0}` to act on."),
    (r"^(.*)_type$", "Which kind of `{0}` this is; the handler branches on it."),
    (r"^(.*)_source$", "Where `{0}` is read from."),
    (r"^(.*)_flags$", "Boolean options for `{0}`, packed one per bit."),
    (r"^(.*)_from_event$", "Whether `{0}` is read from an event value instead of being a literal on this line."),
    (r"^(.*)_selector$", "Selects which `{0}` the handler acts on."),
    (r"^(.*)_half(\d)$", "Halfword {1} of the packed `{0}` entry."),
    (r"^(.*)_word(\d)$", "Word {1} of the packed `{0}` entry."),
    (r"^(.*)_byte(\d)$", "Byte {1} of the packed `{0}` entry."),
    (r"^(.*)_flag(\d+)$", "Enable bit {1} for `{0}`."),
    (r"^(.*)_child$", "Whether child objects are included with `{0}`."),
    (r"^with_(.*)$", "Whether `{0}` is included."),
    (r"^requires_(.*)$", "Whether every `{0}` must match rather than any one of them."),
    (r"^scan_(.*)$", "Whether the handler scans all `{0}` rather than a single one."),
    (r"^(.*)_flag$", "Boolean option controlling `{0}`."),
    (r"^(.*)_low16$", "Low halfword of the packed `{0}` value."),
    (r"^(.*)_high1[0-9]$", "High part of the packed `{0}` value."),
    (r"^(.*)_arg(\d)$", "Argument {1} passed to the engine call for `{0}`."),
    (r"^entry(\d)$", "Entry {0} of the packed record this command builds."),
    (r"^(.*)_value$", "Value used for `{0}`."),
    (r"^(.*)_op$", "Operation applied to `{0}`."),
    (r"^(.*)_kind$", "Which kind of `{0}` this line uses."),
    (r"^(.*)_enable$", "Whether `{0}` is enabled."),
    (r"^source_(.*)$", "Source-side `{0}`, as opposed to the destination."),
    (r"^float\d+\w*$", "Float parameter consumed by this command."),
    (r"^float_([0-9a-f]{2})$", "Float written to the engine record at offset +0x{0}; the field's meaning is not yet named."),
    (r"^field([0-9a-f]+)$", "Value written to the engine record at offset +0x{0}; the field's meaning is not yet named."),
    (r"^word([0-9a-f]{2})$", "Word copied to the engine record at offset +0x{0}."),
    (r"^(.*)_slot(\d)$", "Slot flag/index {1} for `{0}`."),
    (r"^(.*)_low(\d+)$", "Low {1} bits of the packed `{0}` value."),
    (r"^(.*)_high(\d+)$", "High bits of the packed `{0}` value."),
    (r"^(.*)_s(8|16)$", "Signed {1}-bit value for `{0}`."),
    (r"^count(\d)$", "Count byte {0} of the packed count word."),
    (r"^part(\d)$", "Component {0} of the matching packed value."),
    (r"^byte(\d)$", "Byte {0} passed to the engine call."),
    (r"^info(\d)$", "Word {0} of the record this command passes to the engine."),
    (r"^param(\d)$", "Parameter {0} passed to the engine call."),
    (r"^(.*)_word$", "Packed word for `{0}`; the annotated parts beside it decode it."),
    (r"^(.*)_byte$", "Byte-sized operand for `{0}`."),
    (r"^(.*)_arg$", "Value passed to the engine call for `{0}`."),
    (r"^(.*)_bit\d*$", "Single bit of `{0}`."),
    (r"^(?:raw|unknown)_.*$", "Raw field preserved verbatim; its meaning is not yet traced."),
    (r"^.*_raw(?:_.*)?$", "Raw field preserved verbatim; its meaning is not yet traced."),
    (r"^bit\d+$", "Single bit whose meaning is not yet traced; preserved so the command round-trips."),
    (r"^byte[0-9a-f]+_bit\d+$", "Single bit whose meaning is not yet traced; preserved so the command round-trips."),
)

def pattern_parameter_description(name: str) -> str:
    """Structural description for parameters with no hand-written entry.

    These are honest about being generic: they describe the shape of the field
    from its name and the conventions the decoder follows, not a proven meaning.
    """
    for pattern, template in PARAMETER_PATTERNS:
        match = re.match(pattern, name)
        if match:
            groups = [group.replace("_", " ") if group else "" for group in match.groups()]
            try:
                return template.format(*groups) if groups else template
            except (IndexError, KeyError):
                return template
    return ""

PARAMETER_SYMBOL_DOMAINS: dict[str, str] = {
    "character": "character",
    "character_number": "character",
    "character_raw": "character",
    "character_a": "character",
    "character_b": "character",
    "scale_character": "character",
    "parent_character": "character",
    "source_character": "character",
    "item": "item",
    "item_id": "item",
    "first_flag": "flag",
    "flag": "flag",
    "value_from_flag": "flag",
    "battle_map": "location",
    "battle_bgm": "bgm",
    "battle_script": "event",
    "battle_event_file": "event",
}

FORM_SYMBOL_DOMAINS: dict[str, dict[str, str]] = {
    # The event domain names whole script FILES, so it belongs on the command
    # that loads one and nowhere else. `script_start` and `script_start_stack`
    # take a script number inside the file that is already loaded — Command_04
    # calls SetScriptNumber, only Command_06 calls LoadScriptFile — so a file
    # name there is confidently wrong. The corpus agrees: stacked starts only
    # ever use 1..24, and file 516's own scripts are 516_01..516_06.
    # It is deliberately NOT attached to talk_rmf's message_id either: RMF
    # dialogue lines are a separate id space.
    "load_script_file": {"script_id": "event"},
    "load_background": {"id": "location"},
    "background_change_map": {"id": "location"},
    "personal_inventory": {"item_id": "item"},
    "character_equipment": {"item": "item"},
    "set_flags": {"first_flag": "flag"},
}

CHARACTER_ABSTRACTION_CODES: dict[int, str] = {
    0x26AC: "current character",
    0x26AD: "party slot 1",
    0x26AE: "party slot 2",
    0x26AF: "party slot 3",
    0x26B0: "party slot 4",
    0x26B1: "party slot 5",
    **{
        0x26E1 + index: f"character in event value {1000 + index}"
        for index in range(10)
    },
}

def symbol_for(domain: str, value: int, tables: dict[str, dict[int, str]]) -> str | None:
    if domain == "character" and value in CHARACTER_ABSTRACTION_CODES:
        return CHARACTER_ABSTRACTION_CODES[value]
    return tables.get(domain, {}).get(value)

def simplify_character_fields(line: str) -> str:
    """Readable spelling of the explicit-character convention.

    `explicit_char=1` next to a `character=` field is implied and dropped;
    `explicit_char=0` (use the script's default character) becomes
    `character=default`. The compile normalization reverses both.
    """
    tokens = line.split()
    if not tokens or tokens[0].startswith("."):
        return line
    has_char = any(t.startswith("character=") for t in tokens[1:])
    if "explicit_char=1" in tokens and has_char:
        tokens = [t for t in tokens if t != "explicit_char=1"]
        return " ".join(tokens)
    if "explicit_char=0" in tokens and not has_char:
        tokens = ["character=default" if t == "explicit_char=0" else t for t in tokens]
        return " ".join(tokens)
    return line

DECIMAL_ONLY_ID_FIELDS: frozenset[tuple[str, str]] = frozenset({
    ("script_start", "script_id"),
    ("script_start_stack", "script_id"),
    ("script_start", "lookup_script_id"),
    ("script_stop", "script_id"),
})

def decimalize_id_fields(line: str) -> str:
    """Print identity ids (characters, items, flags, maps, BGM, scripts) in
    decimal instead of hex. Packed words with bits above the id half keep the
    hex spelling; both spellings compile identically."""
    directive, fields = source_field_tokens(line)
    if not directive or not fields or ";" in line:
        return line
    engine = resolve_form_name(directive)
    overrides = FORM_SYMBOL_DOMAINS.get(engine, {})
    for name, value in fields:
        engine_name = resolve_parameter_name(engine, name)
        domain = overrides.get(name) or overrides.get(engine_name)
        domain = domain or PARAMETER_SYMBOL_DOMAINS.get(name) or PARAMETER_SYMBOL_DOMAINS.get(engine_name)
        # Counting ids with no name table of their own still read better in
        # decimal: `script_id=5` says "script 5", `script_id=0x0005` does not.
        if not domain and (engine, name) not in DECIMAL_ONLY_ID_FIELDS:
            continue
        if not value.startswith("0x"):
            continue
        try:
            number = parse_hex_int(value)
        except ValueError:
            continue
        if not 0 <= number <= 0xFFFF:
            continue
        line = line.replace(f"{name}={value}", f"{name}={number}", 1)
    return line

def annotate_source_line(line: str, tables: dict[str, dict[int, str]]) -> str:
    """Append a trailing comment naming the ids on this line.

    The annotation is an EVDSRC comment, so it is stripped before assembly and
    cannot affect the bytes. Ids stay authoritative; names are never parsed back.
    """
    directive, fields = source_field_tokens(line)
    if not directive or not fields or ";" in line:
        return line
    engine = resolve_form_name(directive)
    overrides = FORM_SYMBOL_DOMAINS.get(engine, {})
    notes: list[str] = []
    for name, value in fields:
        engine_name = resolve_parameter_name(engine, name)
        domain = overrides.get(name) or overrides.get(engine_name)
        domain = domain or PARAMETER_SYMBOL_DOMAINS.get(name) or PARAMETER_SYMBOL_DOMAINS.get(engine_name)
        if not domain or value.startswith('"'):
            continue
        try:
            number = parse_hex_int(value)
        except ValueError:
            continue
        found = symbol_for(domain, number & 0xFFFFFF, tables)
        if found:
            notes.append(f"{name}={found}")
    if not notes:
        return line
    return f"{line}  ; {', '.join(notes)}"

_symbol_reverse_cache: dict[str, dict[str, int]] | None = None

def symbol_reverse_tables() -> dict[str, dict[str, int]]:
    """name -> id per domain, keeping only names that map to exactly one id.

    Lets id operands be written by name (`character=Jack`). A name shared by
    several ids is excluded so it can never silently pick the wrong one; those
    must stay numeric.
    """
    global _symbol_reverse_cache
    if _symbol_reverse_cache is not None:
        return _symbol_reverse_cache
    reverse: dict[str, dict[str, int]] = {}
    for domain, table in asset_symbols.all_names().items():
        counts: dict[str, list[int]] = {}
        for number, name in table.items():
            counts.setdefault(name.strip().lower(), []).append(number)
        reverse[domain] = {
            name: numbers[0] for name, numbers in counts.items() if len(numbers) == 1
        }
    reverse.setdefault("character", {}).update(
        {name: code for name, code in CHARACTER_SLOT_CODES.items()}
    )
    _symbol_reverse_cache = reverse
    return reverse

def resolve_symbol_name_values(directive: str, parts: list[str], line_no: int) -> list[str]:
    """Rewrite `key=Name` values to their numeric ids for known id domains."""
    engine = resolve_form_name(directive)
    overrides = FORM_SYMBOL_DOMAINS.get(engine, {})
    out = list(parts)
    for index in range(1, len(out)):
        key, sep, value = out[index].partition("=")
        if not sep or not value:
            continue
        if value[0] in "0123456789-" or (value[0] == "0" and value[1:2] in ("x", "X")):
            continue
        engine_name = resolve_parameter_name(engine, key)
        domain = overrides.get(key) or overrides.get(engine_name)
        domain = domain or PARAMETER_SYMBOL_DOMAINS.get(key) or PARAMETER_SYMBOL_DOMAINS.get(engine_name)
        if not domain:
            continue
        text = value
        if text.startswith('"') and text.endswith('"') and len(text) >= 2:
            text = text[1:-1]
        number = symbol_reverse_tables().get(domain, {}).get(text.strip().lower())
        if number is not None:
            out[index] = f"{key}=0x{number:04X}"
    return out

def parameter_description(directive: str, name: str) -> str:
    """Description for one parameter, preferring the form-specific wording."""
    directive = resolve_form_name(directive)
    engine_name = resolve_parameter_name(directive, name)
    if engine_name in UNTRACED_PARAMETERS.get(directive, set()):
        return (
            "Not yet traced. The handler consumes it but its meaning was never established, "
            "so it is preserved verbatim rather than given an invented name."
        )
    for key in (name, engine_name):
        described = FORM_PARAMETER_NOTES.get(directive, {}).get(key) or PARAMETER_GLOSSARY.get(key, "")
        if described:
            return described
    return pattern_parameter_description(name)

def source_head_is_compilable(name: str) -> bool:
    """Whether some compiler path claims this head."""
    resolved = resolve_form_name(name)
    return (
        resolved in STRUCTURED_SOURCE_FORMS
        or name in HIGH_LEVEL_COMMANDS
        or resolved in RAW_ESCAPE_FORMS
        or name in SOURCE_OPCODE_ALIASES
    )

# END GENERATED


###=========================================================================================###
###                                    2. THE API LAYER                                     ###
###=========================================================================================###
COMMAND_REGION_OFFSET = 0x0C

###------------------------------------------- Categories -------------------------------------------###

CATEGORY_TERMINAL     = 'end'
CATEGORY_JUMP         = 'jump'
CATEGORY_SCRIPT_START = 'script_start'
CATEGORY_MARKER_SEEK  = 'marker_seek'
CATEGORY_EXPRESSION   = 'calc'
CATEGORY_HIGH         = 'high'
CATEGORY_NORMAL       = 'normal'
CATEGORY_STRUCTURE    = 'structure'  # event/label/entry/header/braces: emits no command
CATEGORY_MACRO        = 'macro'      # authoring shorthand that lowers to several commands

# The opcodes that do something other than act on the scene, and so are worth
# telling apart by colour. Everything else is CATEGORY_NORMAL. These come from
# the format reference, sections 4 and 8; there are only six because control
# flow in this format is only six opcodes wide.
_OPCODE_CATEGORIES: dict[int, str] = {
    0x00: CATEGORY_TERMINAL,      # end script
    0x01: CATEGORY_SCRIPT_START,  # stacked start
    0x02: CATEGORY_JUMP,          # every branch and jump
    0x04: CATEGORY_SCRIPT_START,  # start script
    0x0D: CATEGORY_MARKER_SEEK,   # marker seek
    0x14: CATEGORY_EXPRESSION,    # read-modify-write across mutable state
}

# Opcode families from the format reference, section 9. Only used for grouping
# in the palette and for a second colour axis in the code view; the categories
# above stay the authority for anything that changes behaviour.
_OPCODE_FAMILIES: tuple[tuple[int, int, str], ...] = (
    (0x00, 0x1F, 'Script control, flags, values, windows'),
    (0x20, 0x3F, 'Characters'),
    (0x40, 0x5F, 'Background, map, camera'),
    (0x60, 0x7F, 'Primitives, textures, sound, movies'),
    (0x80, 0x8F, 'Dialogue, text, windows'),
    (0x90, 0xEF, 'Person, schedule, battle, effects'),
    (0xF0, 0xFF, 'Markers'),
)


def opcode_family(opcode: int | None) -> str:
    if opcode is None:
        return 'Structure'
    for low, high, name in _OPCODE_FAMILIES:
        if low <= opcode <= high:
            return name
    return 'Other'

###------------------------------------------- Diagnostics -------------------------------------------###

_LINE_PREFIX = re.compile(r'^line (\d+): (.*)$', re.S)
# "character_sub_anim character_number does not match character" -- the compiler
# names the derived field first and the input it disagrees with last.
_MISMATCH = re.compile(r'\b(\w+) does not match (\w+)\b')


class EvdCompileError(ValueError):
    '''A rejected EVDCODE source, with the author's line number when there is one.'''

    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(f'line {line}: {message}' if line else message)
        self.message = message
        self.line = line

    @classmethod
    def from_exception(cls, exc: Exception) -> 'EvdCompileError':
        if isinstance(exc, cls):
            return exc
        match = _LINE_PREFIX.match(str(exc))
        if match:
            return cls(match.group(2), int(match.group(1)))
        return cls(str(exc))

    @property
    def conflicting_field(self) -> str | None:
        '''The derived parameter this error blames, when it blames one.

        A derived parameter is cross-checked against the inputs it comes from,
        so editing an input alone is rejected rather than ignored. The name here
        is what the parameter editor drops before retrying.
        '''
        match = _MISMATCH.search(self.message)
        return match.group(1) if match else None

###--------------------------------------------- Symbols ---------------------------------------------###

class SymbolTables:
    '''Id-to-name tables for characters, items, locations, BGM, skills, events and flags.

    Names are advisory: the decompiler appends them as `// field=name` comments
    and the compiler ignores them, so a missing table costs readability only.
    '''
    DOMAINS = asset_symbols.CATEGORIES

    def __init__(self) -> None:
        # The tables are not ours: they are the game's asset id lists, shared
        # with anything else that names an id. We merge every category here
        # because a script can reference all of them; a handler that needs one
        # should ask `core.asset_symbols` for that one instead of coming here.
        self._tables: dict[str, dict[int, str]] = asset_symbols.all_names()

    def __bool__(self) -> bool:
        return bool(self._tables)

    @property
    def tables(self) -> dict[str, dict[int, str]] | None:
        '''The mapping the format layer wants, or None when nothing loaded.'''
        return self._tables or None

    def lookup(self, domain: str, value: int) -> str | None:
        if not self._tables:
            return None
        return symbol_for(domain, value, self._tables)

    def search(self, domain: str, text: str) -> list[tuple[int, str]]:
        '''Ids in `domain` whose name contains `text`, case-insensitively.'''
        return asset_symbols.search(domain, text)


SYMBOLS = SymbolTables()
###------------------------------------------ Command index ------------------------------------------###

@dataclass(frozen=True)
class ParamInfo:
    '''One named parameter of one command.

    `role` is the whole reason this metadata exists. An `input` is yours to set.
    A `derived` value is recomputed from the inputs it comes from and is
    cross-checked on compile, so changing one on its own is an error rather than
    a silent no-op -- the editor greys it and drops it when its input moves.
    '''
    name:    str
    role:    str  # 'input' | 'derived'
    meaning: str
    # How much the meaning is worth: 'traced' from this form's disassembly,
    # 'glossary' from the shared vocabulary, 'template' generated from the
    # parameter's own name, 'untraced' explicitly unknown, 'none' undescribed.
    # A structurally authoritative field is not necessarily an understood one.
    evidence: str = 'none'

    @property
    def is_input(self) -> bool:
        return self.role == 'input'

    @property
    def is_traced(self) -> bool:
        return self.evidence in ('traced', 'glossary')


@dataclass(frozen=True)
class CommandInfo:
    '''Everything needed to complete, document and validate one EVDCODE command.'''
    name:       str
    engine:     str
    summary:    str
    example:    str
    opcode:     int | None
    raw:        bool
    evidence:   str
    parameters: tuple[ParamInfo, ...]
    shorthand:  tuple[str, ...]              # every parameter the shorthand accepts
    positional: tuple[str, ...] = ()         # the ones it takes in order, without names

    @cached_property
    def by_name(self) -> dict[str, ParamInfo]:
        return {p.name: p for p in self.parameters}

    @property
    def inputs(self) -> tuple[ParamInfo, ...]:
        return tuple(p for p in self.parameters if p.is_input)

    @property
    def family(self) -> str:
        return opcode_family(self.opcode)

    def role_of(self, param: str) -> str | None:
        info = self.by_name.get(param)
        return info.role if info else None

    def evidence_of(self, param: str) -> str:
        info = self.by_name.get(param)
        return info.evidence if info else 'none'

    def meaning_of(self, param: str) -> str:
        '''What a parameter means, falling back to the shared vocabulary.

        A command's own list only holds the parameters the corpus classifier saw
        on it, so a legal-but-unused spelling (`not=` on `if_value`, where every
        shipped script writes `is=`) is missing from it while still being
        documented format-wide.
        '''
        info = self.by_name.get(param)
        if info is not None and info.meaning:
            return info.meaning
        shared = parameter_description(self.name, param)
        return shared or 'Not in the command index; passed through to the compiler as written.'


@dataclass(frozen=True)
class StructureInfo:
    '''A structural construct or authoring macro: `event`, `label`, `if_value`, `spawn_char`...'''
    name:      str
    summary:   str
    signature: str
    snippet:   str


class CommandIndex:
    '''The command index shipped as an asset, generated from the format layer.

    One file feeds this: `evd_command_index.json`, carrying every command with
    its parameters and their input/derived roles, built from the same corpus
    classification the format reference is written from. Because it is
    generated, re-running that command is the whole update story.
    '''

    def __init__(self) -> None:
        index = self._load_json('ui/assets/evd_command_index.json', {})
        self._commands: dict[str, CommandInfo] = {}
        for name, entry in (index.get('commands') or {}).items():
            self._commands[name] = CommandInfo(
                name=name,
                engine=entry.get('engine', name),
                summary=entry.get('summary', ''),
                example=entry.get('example', ''),
                opcode=entry.get('opcode'),
                raw=bool(entry.get('raw')),
                evidence=entry.get('evidence', ''),
                parameters=tuple(
                    ParamInfo(p.get('name', ''), p.get('role', 'input'), p.get('meaning', ''),
                              p.get('evidence', 'none'))
                    for p in entry.get('parameters', ())
                ),
                shorthand=tuple((entry.get('shorthand') or {}).get('params', ())),
                positional=tuple((entry.get('shorthand') or {}).get('positional', ())),
            )

        self._aliases: dict[str, str] = dict(index.get('aliases') or {})
        self._structure: dict[str, StructureInfo] = {
            name: StructureInfo(
                name=name,
                summary=entry.get('summary', ''),
                signature=entry.get('signature', name),
                snippet=entry.get('snippet', name),
            )
            for name, entry in (index.get('structure') or {}).items()
        }
        self._directives: dict[str, str] = dict(index.get('directives') or {})

        if not self._commands:
            logger.warning('EVD command index is empty; parameter editing falls back to free text.')

    @staticmethod
    def _load_json(relative: str, fallback):
        path = get_resource_path(relative)
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f'Could not load {path}: {e}')
            return fallback

    def __bool__(self) -> bool:
        return bool(self._commands)

    def __contains__(self, name: str) -> bool:
        return self.canonical(name) in self._commands

    def canonical(self, name: str) -> str:
        '''Resolve an accepted spelling to the one the decompiler prints.'''
        return self._aliases.get(name, name)

    def get(self, name: str) -> CommandInfo | None:
        return self._commands.get(self.canonical(name))

    def structure(self, name: str) -> StructureInfo | None:
        return self._structure.get(name)

    @property
    def command_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._commands))

    @property
    def structure_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._structure))

    @property
    def directives(self) -> dict[str, str]:
        return dict(self._directives)

    def category(self, name: str) -> str:
        '''Control-flow category of a command name, for colouring.'''
        if name in _STRUCTURE_KEYWORDS:
            return CATEGORY_STRUCTURE
        info = self.get(name)
        if info is not None and info.opcode is not None:
            return self.category_of_opcode(info.opcode)
        if info is not None or name in self._structure:
            return CATEGORY_MACRO  # lowers to several commands, so it has no single opcode
        return CATEGORY_NORMAL

    @staticmethod
    def category_of_opcode(opcode: int) -> str:
        if opcode >= 0xF0:
            return CATEGORY_HIGH
        return _OPCODE_CATEGORIES.get(opcode, CATEGORY_NORMAL)

    def palette_entries(self) -> list[tuple[str, str, str]]:
        '''(name, family, summary) for everything droppable into a script.

        Structural constructs and macros come first under their own family so
        the common authoring moves are not buried in 135 alphabetical commands.
        '''
        entries: list[tuple[str, str, str]] = [
            (info.name, 'Structure', info.summary)
            for info in sorted(self._structure.values(), key=lambda i: i.name)
        ]
        entries.extend(
            (info.name, info.family, info.summary)
            for info in sorted(self._commands.values(), key=lambda i: (i.opcode if i.opcode is not None else 0x100, i.name))
            if info.name not in self._structure
        )
        return entries


_STRUCTURE_KEYWORDS = frozenset({
    'event', 'label', 'entry', 'header', 'headerExtra', 'header_extra',
    'markerTable', 'marker_table', 'option', 'bytes', 'cmd',
})

COMMANDS = CommandIndex()

###-------------------------------------------- Text forms -------------------------------------------###

_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def event_name_for(name: str) -> str:
    '''An EVDCODE event name has to be an identifier; EVD files are called `516_01`.'''
    cleaned = re.sub(r'[^A-Za-z0-9_]', '_', name.strip())
    if not cleaned:
        return 'Main'
    return cleaned if _IDENTIFIER.match(cleaned) else f'Event_{cleaned}'


def decompile_code(data: bytes, event_name: str = 'Main', annotate: bool = True) -> str:
    '''Raw EVD bytes to block-structured EVDCODE.

    the format layer verifies its own output here: it recompiles the sugared form and
    falls back to the unsugared one when the bytes differ, so what comes back
    always reassembles to `data`.
    '''
    return decompile_evd_code(
        data,
        COMMAND_REGION_OFFSET,
        event_name_for(event_name),
        SYMBOLS.tables if annotate else None,
    )


def decompile_source(data: bytes, annotate: bool = True) -> str:
    '''Raw EVD bytes to flat EVDSRC, the intermediate EVDCODE lowers to.'''
    return decompile_evd_source(
        data, COMMAND_REGION_OFFSET, False, SYMBOLS.tables if annotate else None
    )


def compile_code(text: str) -> bytes:
    '''EVDCODE to raw EVD bytes. Raises EvdCompileError with the author's line.'''
    try:
        return compile_evd_code(text)
    except Exception as e:
        raise EvdCompileError.from_exception(e) from e


def compile_code_to_source(text: str) -> str:
    '''The EVDSRC an EVDCODE file lowers to, for inspection.'''
    try:
        return compile_evd_code_to_source(text)
    except Exception as e:
        raise EvdCompileError.from_exception(e) from e


def validate_code(text: str) -> EvdCompileError | None:
    '''None when `text` assembles, otherwise the first error.'''
    try:
        compile_code(text)
    except EvdCompileError as e:
        return e
    return None


@dataclass(frozen=True)
class Assembly:
    '''One compile of an EVDCODE file, plus where each line landed.'''
    data:    bytes | None = None
    offsets: dict[int, int] = field(default_factory=dict)  # EVDCODE line -> byte offset
    error:   EvdCompileError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def assemble(text: str) -> Assembly:
    '''Compile `text` and report the byte offset every line landed at.

    The offsets matter because labels in this format *are* offsets: the
    decompiler names a jump target `loc_02BC` after the byte it sits at. Once
    lines move, the names stay and the addresses shift, so the only way to find
    where `loc_02BC` actually is now is to assemble and look.

    They are recovered by walking the assembled command region and lining it up
    with the lowered EVDSRC, whose statements are emitted in that same order.
    If the two do not line up -- a `.bytes` region assembles to something the
    command walk cannot read -- no offsets are reported rather than wrong ones.
    '''
    try:
        parser = EVDCodeParser(text)
        lowered = parser.parse()
        data = compile_evd_source(lowered)
    except Exception:
        # Report it through compile_code, which maps the failure back onto the
        # line the author wrote instead of a line of the lowered intermediate.
        return Assembly(error=validate_code(text) or EvdCompileError('script did not assemble'))

    statements = [
        item for item in parse_source_items(lowered)
        if item['kind'] == 'cmd' and item['head'] not in _NON_EMITTING_DIRECTIVES
    ]
    walked = _walk_statement_offsets(data, statements)
    if walked is None:
        return Assembly(data=data)

    by_line: dict[int, int] = dict(_header_line_offsets(text, data))
    for item, offset in zip(statements, walked):
        author_line = parser.source_line_of.get(item['line_no'])
        if author_line is not None:
            by_line.setdefault(author_line, offset)

    # A label, a brace and a directive emit nothing, so they take the address of
    # the next thing that does -- which for a label is exactly its value.
    offsets: dict[int, int] = {}
    next_offset = len(data)
    for number in range(len(text.splitlines()), 0, -1):
        if number in by_line:
            next_offset = by_line[number]
        offsets[number] = next_offset
    return Assembly(data=data, offsets=offsets)


_NON_EMITTING_DIRECTIVES = frozenset({
    '.header', '.header_extra', '.entry', '.marker_table', '.org', '.align',
})

_RE_HEAD = re.compile(r'^\s*(?:event\s+\S+\s*\{|([A-Za-z_][A-Za-z0-9_]*)\s*\()')


def _header_line_offsets(text: str, data: bytes) -> dict[int, int]:
    '''Addresses for the lines that are file structure rather than commands.

    These are not commands, but they are bytes, and they are the first bytes:
    the magic at +0x00 and the two header words at +0x04 and +0x08. Without
    them the address column would begin partway down the file at the first
    command, when the file itself begins at 0000.
    '''
    fixed = {'header': 0x04, 'headerExtra': 0x08, 'header_extra': 0x08}
    table = u32(data, 0x08) * 4 if len(data) >= 12 else 0
    offsets: dict[int, int] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        match = _RE_HEAD.match(raw)
        if not match:
            continue
        head = match.group(1)
        if head is None:                       # `event Name {` -- the magic word
            offsets.setdefault(number, 0x00)
        elif head in fixed:
            offsets[number] = fixed[head]
        elif head in ('markerTable', 'marker_table') and table:
            offsets[number] = table
    return offsets


_CURSOR_DIRECTIVES = frozenset({'.org', '.align'})


def _walk_statement_offsets(data: bytes, statements: list[dict]) -> list[int] | None:
    '''Where each lowered statement landed, or None if the walk lost its place.

    The statements are in emission order, so walking them against the assembled
    bytes gives each one its address. It has to be a joint walk rather than a
    plain command walk: a `.bytes` region is raw data that decodes as garbage
    commands, and the marker table sits inside the command region without being
    one. Anything unexpected returns None -- no addresses beats wrong ones.
    '''
    header_extra = u32(data, 0x08) if len(data) >= 12 else 0
    marker_table = decode_marker_table_source(data, header_extra)
    offsets: list[int] = []
    cursor = COMMAND_REGION_OFFSET
    for item in statements:
        if marker_table and cursor == marker_table['offset']:
            cursor += int(marker_table['size'])
        if cursor >= len(data):
            return None
        head = item['head']
        if head in _CURSOR_DIRECTIVES:
            return None  # moves the cursor by an amount only the compiler knows
        offsets.append(cursor)
        if head == '.bytes':
            cursor += len(item['parts'])
            continue
        if head == '.word':
            cursor += 4 * len(item['parts'])
            continue
        if cursor % 4 or cursor + 4 > len(data):
            return None
        try:
            command = decode_command_at(data, cursor)
        except ValueError:
            return None
        end = int(command['end_offset'])
        if command.get('truncated') or end <= cursor:
            return None
        cursor = end
    return offsets


def label_for_offset(offset: int) -> str:
    '''The name the decompiler would give a label at `offset`.'''
    return label_name(offset)

###--------------------------------------------- Line model ------------------------------------------###

KIND_BLANK     = 'blank'
KIND_COMMENT   = 'comment'
KIND_EVENT     = 'event'      # `event Main {`
KIND_OPTION    = 'option'     # `option {` inside a choose
KIND_CLOSE     = 'close'      # `}`
KIND_ELSE      = 'else'       # `} else {`
KIND_LABEL     = 'label'      # `label(loc_000C)`
KIND_DIRECTIVE = 'directive'  # `header(...)`, `entry(...)`, `headerExtra(...)`
KIND_COMMAND   = 'command'    # any call that emits a command
KIND_UNKNOWN   = 'unknown'

# `markerTable` is the EVDCODE spelling the decompiler prints for the
# `.marker_table` directive; both are accepted on input.
_DIRECTIVE_HEADS = frozenset({'header', 'headerExtra', 'header_extra', 'entry',
                              'markerTable', 'marker_table'})

_RE_ELSE   = re.compile(r'^\}\s*else\s*\{$')
_RE_CLOSE  = re.compile(r'^\}$')
_RE_EVENT  = re.compile(r'^event\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{$')
_RE_OPTION = re.compile(r'^option\s*\{$')
_RE_CALL   = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*(\{)?$', re.S)


@dataclass(frozen=True)
class Arg:
    '''One argument of a call. `key` is empty for a positional argument.'''
    key:   str
    value: str

    def __str__(self) -> str:
        return f'{self.key}={self.value}' if self.key else self.value


@dataclass
class CodeLine:
    '''One line of EVDCODE, and everything the views need to render or edit it.

    `text` is the line exactly as it appears. Lines the user has not touched are
    written back verbatim, so decompiler output survives an unrelated edit
    untouched rather than being re-rendered through this model.
    '''
    number:  int                       # 1-based, matches compiler diagnostics
    text:    str
    indent:  int
    kind:    str
    head:    str = ''
    args:    tuple[Arg, ...] = ()
    comment: str = ''                  # including the leading '//'
    opens:   bool = False
    closes:  bool = False
    depth:   int = 0                   # nesting depth, from the brace walk
    block_end: int | None = None       # line number of the matching '}', for openers

    @property
    def is_call(self) -> bool:
        return self.kind in (KIND_COMMAND, KIND_LABEL, KIND_DIRECTIVE)

    @property
    def info(self) -> CommandInfo | None:
        return COMMANDS.get(self.head) if self.head else None

    @property
    def opcode(self) -> int | None:
        info = self.info
        return info.opcode if info else None

    @property
    def category(self) -> str:
        if self.kind in (KIND_EVENT, KIND_OPTION, KIND_CLOSE, KIND_ELSE, KIND_LABEL, KIND_DIRECTIVE):
            return CATEGORY_STRUCTURE
        if self.kind != KIND_COMMAND:
            return CATEGORY_NORMAL
        return COMMANDS.category(self.head)

    @property
    def target_label(self) -> str | None:
        '''Where this line jumps, when it jumps somewhere.'''
        for arg in self.args:
            if arg.key in ('goto', 'target'):
                return arg.value
        return None

    def arg(self, key: str) -> str | None:
        for a in self.args:
            if a.key == key:
                return a.value
        return None

    def with_args(self, args: tuple[Arg, ...], comment: str | None = None) -> 'CodeLine':
        '''A copy re-rendered from `args`; only for lines the user actually edited.'''
        rendered = render_call(
            self.indent, self.head, args,
            self.comment if comment is None else comment,
            self.opens,
        )
        return parse_line(rendered, self.number)


def example_to_call(example: str, indent: int = 0) -> str:
    '''Turn a documented EVDSRC example (`head a=1 b=2`) into an EVDCODE call.

    Every command in the index carries an example lifted from a shipped script,
    which is what makes a freshly inserted command assemble instead of arriving
    as an empty payload the author has to reverse engineer. The conversion is
    the decompiler's own, because it is not a join: EVDCODE separates arguments
    with commas, so any value that contains one (`xy=-320,-240`) has to be
    quoted on the way across.
    '''
    parts = split_source_tokens(example.strip())
    if not parts:
        return f'{" " * indent}{example.strip()}'
    return ' ' * indent + source_call_to_code(parts[0], parts[1:]).strip()


# Structural signatures are written to be read, not compiled: bodies are `...`
# and operands are the names of what goes there. Substituting concrete values
# turns them into something that assembles the moment it is inserted.
_TEMPLATE_BODY = 'nop()'
_TEMPLATE_OPERANDS = {
    'character': '1', 'item': '1', 'x': '0', 'y': '0', 'z': '0',
    # A head angle is pitch and yaw only; a posture is a full rotation. Their
    # masks are checked against the axes present, so the two cannot share one.
    'angle': '"x:0,y:0"', 'posture': '"x:0,y:0,z:0"', 'vector': '"0,0,0"',
    'speed': '1', 'sound': '0', 'trigger_type': '0', 'value': '0', 'stand': '0',
    'pan': '0', 'volume': '0',
}
_TEMPLATE_OPERAND_RE = re.compile(
    r'(?<=[(,\s])(' + '|'.join(_TEMPLATE_OPERANDS) + r')(?=[,)\s])'
)


def command_template(name: str, indent: int = 4, label: str = 'loc_new') -> str:
    '''Insertable starting text for `name`, or '' when there is nothing to insert.

    A documented command becomes its own example, a real line lifted from a
    shipped script, so it assembles as inserted. A structural construct becomes
    its signature with the placeholders filled in. A few (`option`, `raw`) only
    mean something once the author supplies the missing part and are returned as
    stubs that do not yet compile.
    '''
    pad = ' ' * indent
    structure = COMMANDS.structure(name)
    if structure is not None:
        # The block form wins over the command example where a name has both.
        # `if_value`'s example is the jump spelling, and its `goto=loc_090C`
        # names a label from the script it was lifted from -- which in another
        # script silently compiles to the raw offset 0x090C instead.
        text = structure.signature.replace('...', _TEMPLATE_BODY).replace('loc_name', label)
        text = _TEMPLATE_OPERAND_RE.sub(lambda m: _TEMPLATE_OPERANDS[m.group(1)], text)
        return '\n'.join(pad + _reindent(part) for part in text.splitlines())

    info = COMMANDS.get(name)
    if info is None:
        return ''
    if info.example:
        return example_to_call(info.example, indent)
    if info.positional:
        # A shorthand spelling that no shipped script uses has no example to
        # borrow, but its signature says what it takes and in what order.
        return f'{pad}{name}({", ".join(_TEMPLATE_OPERANDS.get(p, "0") for p in info.positional)})'
    return f'{pad}{name}()'


def _reindent(part: str) -> str:
    '''Signatures indent nested lines by two spaces; EVDCODE files use four.'''
    stripped = part.lstrip(' ')
    return ' ' * ((len(part) - len(stripped)) * 2) + stripped


def unique_label(lines: list[CodeLine], stem: str = 'loc_new') -> str:
    '''A label name not already defined in `lines`.'''
    taken = {name for name, _ in iter_labels(lines)}
    if stem not in taken:
        return stem
    index = 1
    while f'{stem}{index}' in taken:
        index += 1
    return f'{stem}{index}'


def render_call(indent: int, head: str, args: tuple[Arg, ...] | list[Arg], comment: str, opens: bool) -> str:
    body = ', '.join(str(a) for a in args)
    line = f'{" " * indent}{head}({body})'
    if opens:
        line += ' {'
    if comment:
        line += f'  {comment}'
    return line


def comment_start(line: str) -> int:
    '''Index of the trailing `//`, or -1. A `//` inside a quoted string is text.'''
    in_string = False
    escaped = False
    for i, ch in enumerate(line):
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == '/' and line[i + 1:i + 2] == '/':
            return i
    return -1


def split_comment(line: str) -> tuple[str, str]:
    '''Split off a trailing `// ...`, ignoring `//` inside a quoted string.'''
    index = comment_start(line)
    if index < 0:
        return line.rstrip(), ''
    return line[:index].rstrip(), line[index:].rstrip()


def split_args(text: str) -> tuple[Arg, ...]:
    '''Split a call's argument list on top-level commas, respecting quotes.'''
    args: list[Arg] = []
    depth = 0
    in_string = False
    escaped = False
    current = ''
    for ch in text:
        if in_string:
            current += ch
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            current += ch
            continue
        if ch in '([':
            depth += 1
        elif ch in ')]':
            depth -= 1
        if ch == ',' and depth <= 0:
            if current.strip():
                args.append(_make_arg(current))
            current = ''
            continue
        current += ch
    if current.strip():
        args.append(_make_arg(current))
    return tuple(args)


def _make_arg(raw: str) -> Arg:
    token = raw.strip()
    key, sep, value = token.partition('=')
    if not sep:
        return Arg('', token)
    return Arg(key.strip(), value.strip())


def parse_line(text: str, number: int) -> CodeLine:
    '''Parse one EVDCODE line. Never raises: anything unrecognised stays KIND_UNKNOWN.'''
    indent = len(text) - len(text.lstrip(' '))
    code, comment = split_comment(text)
    stripped = code.strip()

    if not stripped:
        kind = KIND_COMMENT if comment else KIND_BLANK
        return CodeLine(number, text.rstrip(), indent, kind, comment=comment)

    if _RE_ELSE.match(stripped):
        return CodeLine(number, text.rstrip(), indent, KIND_ELSE, head='else',
                        comment=comment, opens=True, closes=True)
    if _RE_CLOSE.match(stripped):
        return CodeLine(number, text.rstrip(), indent, KIND_CLOSE, head='}',
                        comment=comment, closes=True)

    match = _RE_EVENT.match(stripped)
    if match:
        return CodeLine(number, text.rstrip(), indent, KIND_EVENT, head='event',
                        args=(Arg('', match.group(1)),), comment=comment, opens=True)
    if _RE_OPTION.match(stripped):
        return CodeLine(number, text.rstrip(), indent, KIND_OPTION, head='option',
                        comment=comment, opens=True)

    match = _RE_CALL.match(stripped)
    if match:
        head, body, brace = match.group(1), match.group(2), match.group(3)
        if head == 'label':
            kind = KIND_LABEL
        elif head in _DIRECTIVE_HEADS:
            kind = KIND_DIRECTIVE
        else:
            kind = KIND_COMMAND
        return CodeLine(number, text.rstrip(), indent, kind, head=head,
                        args=split_args(body), comment=comment, opens=bool(brace))

    return CodeLine(number, text.rstrip(), indent, KIND_UNKNOWN, comment=comment)


def parse_code(text: str) -> list[CodeLine]:
    '''Parse a whole EVDCODE file into one CodeLine per text line.

    Also resolves nesting: every line carries its depth, and every opener the
    line number of its matching `}`, which is what lets the structure view move
    or delete a block as a unit instead of stranding its body.
    '''
    lines = [parse_line(raw, number) for number, raw in enumerate(text.splitlines(), start=1)]
    open_stack: list[CodeLine] = []
    depth = 0
    for line in lines:
        if line.kind == KIND_ELSE:
            # `} else {` closes the if body and opens the else body at the same
            # depth, so it neither indents nor pairs as a new opener.
            if open_stack:
                depth = max(0, depth - 1)
            line.depth = depth
            depth += 1
            continue
        if line.closes:
            depth = max(0, depth - 1)
            line.depth = depth
            if open_stack:
                open_stack.pop().block_end = line.number
            continue
        line.depth = depth
        if line.opens:
            open_stack.append(line)
            depth += 1
    for orphan in open_stack:
        orphan.block_end = None
    return lines


def block_range(lines: list[CodeLine], number: int) -> tuple[int, int]:
    '''The 1-based inclusive line span a line owns: itself, or itself plus its block.'''
    index = number - 1
    if not (0 <= index < len(lines)):
        return number, number
    line = lines[index]
    if line.opens and line.block_end:
        return line.number, line.block_end
    return line.number, line.number


def render_code(lines: list[CodeLine]) -> str:
    return '\n'.join(line.text for line in lines) + '\n'


def foldable(lines: list[CodeLine]) -> dict[int, int]:
    '''Opener line -> its closing line, for every block that can be collapsed.

    A block is worth folding only if it has something inside it; `event Main {`
    counts, so the whole script can be collapsed to one line.
    '''
    return {line.number: line.block_end for line in lines
            if line.opens and line.block_end and line.block_end > line.number + 1}


def hidden_lines(lines: list[CodeLine], folded: set[int]) -> set[int]:
    '''Line numbers inside a collapsed block, including nested ones.

    A fold inside a fold contributes nothing extra -- its lines are already
    hidden by the outer one -- so unfolding the outer block restores whatever
    fold state the inner blocks were left in.
    '''
    spans = foldable(lines)
    hidden: set[int] = set()
    for opener in folded:
        end = spans.get(opener)
        if end:
            hidden.update(range(opener + 1, end + 1))
    return hidden


def fold_summary(lines: list[CodeLine], opener: int) -> str:
    '''What a collapsed block shows in place of its body: ` ... }`.'''
    end = foldable(lines).get(opener)
    if not end:
        return ''
    return f' ... }}   ({end - opener - 1} lines)'


def iter_labels(lines: list[CodeLine]) -> Iterator[tuple[str, int]]:
    '''(name, line number) for every `label(...)` in the file.'''
    for line in lines:
        if line.kind == KIND_LABEL and line.args:
            yield line.args[0].value, line.number


_LOOKS_LIKE_LABEL = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


BRANCH_OPCODE = 0x02
_LABEL_DIRECTIVES = frozenset({'entry', 'markerTable', 'marker_table'})


def iter_label_targets(lines: list[CodeLine]) -> Iterator[tuple[CodeLine, str]]:
    '''(line, label name) for everything that names a jump target.

    `goto=` is always a label, but `target=` is overloaded: on a branch it is the
    destination, while on `set_sound_listener` it is an enum naming what to
    follow. Only a branch's `target=` counts.
    '''
    for line in lines:
        if line.kind == KIND_DIRECTIVE and line.head in _LABEL_DIRECTIVES:
            for arg in line.args:
                yield line, arg.value
            continue
        branch = line.opcode == BRANCH_OPCODE
        for arg in line.args:
            if arg.key == 'goto' or (branch and arg.key == 'target'):
                yield line, arg.value


def undefined_label_problems(lines: list[CodeLine]) -> list[EvdCompileError]:
    '''Jump targets that name a label the script does not define.

    The compiler does not catch these. `loc_0910` parses as the raw byte offset
    0x0910 when no such label exists, so a branch left pointing at a label that
    was deleted -- or renamed, or never existed in this script -- assembles
    quietly into a jump to whatever now sits at that offset. Moving and deleting
    lines is most of what this editor does, which is exactly what strands them.
    '''
    defined = {name for name, _ in iter_labels(lines)}
    problems: list[EvdCompileError] = []
    for line, name in iter_label_targets(lines):
        if name in defined or not _LOOKS_LIKE_LABEL.match(name):
            continue  # a plain number is a deliberate raw offset, not a mistake
        try:
            offset = parse_hex_int(name)
        except ValueError:
            problems.append(EvdCompileError(
                f'{line.head} targets {name}, which is not a label in this script', line.number))
            continue
        problems.append(EvdCompileError(
            f'{line.head} targets {name}, which is not a label in this script; '
            f'it will assemble as the raw byte offset 0x{offset:04X}', line.number))
    return problems


def label_references(lines: list[CodeLine]) -> dict[str, int]:
    '''How many lines jump to each label, so unreferenced ones can be flagged.'''
    counts: dict[str, int] = {}
    for line in lines:
        for arg in line.args:
            if arg.key in ('goto', 'target'):
                counts[arg.value] = counts.get(arg.value, 0) + 1
            elif line.kind == KIND_DIRECTIVE and line.head in _LABEL_DIRECTIVES:
                counts[arg.value] = counts.get(arg.value, 0) + 1
    return counts

###-------------------------------------------- Line editing -----------------------------------------###

###------------------------------------------ Packed operands ----------------------------------------###

# A character selector packs an id and a variant into one word. The format layer's own
# spec tuples are reused so the layouts here cannot drift from the ones the
# compiler enforces.
_PACKED_SPECS: dict[str, tuple[tuple[str, int, int], ...]] = {
    'parent': PARENT_WORD_SPECS,
}


def packed_spec(info: 'CommandInfo | None', key: str) -> tuple[tuple[str, int, int], ...] | None:
    '''How `key` splits into named parts, or None when it is a plain value.

    `character` splits two ways depending on the command: byte 2 is a variant on
    most, and a type selector on the handful that read it as one. The command's
    own parameter list says which.
    '''
    if key == 'character':
        if info is not None and 'character_type' in info.by_name:
            return CHARACTER_TYPE_SPECS
        return CHARACTER_VARIANT_SPECS
    return _PACKED_SPECS.get(key)


def split_packed(word: int, specs: tuple[tuple[str, int, int], ...]) -> dict[str, int]:
    return {name: (word >> shift) & mask for name, shift, mask in specs}


def compose_packed(word: int, values: dict[str, int],
                   specs: tuple[tuple[str, int, int], ...]) -> int:
    '''`word` with the named parts in `values` written into their fields.

    Only the fields named are touched, so bits the specs do not cover -- the top
    byte of a character selector, which no handler has been shown to read --
    survive rather than being zeroed by an edit that never mentioned them.
    '''
    for name, shift, mask in specs:
        if name in values:
            word = (word & ~((mask << shift) & 0xFFFFFFFF)) | ((values[name] & mask) << shift)
    return word & 0xFFFFFFFF


###------------------------------------------ Named id operands --------------------------------------###

# Domains with good enough coverage to offer as a list. Flags are deliberately
# out: names exist for 175 of 8,191, so a list would hide far more than it shows.
PICKABLE_DOMAINS = ('character', 'item', 'bgm', 'location')

# A packed word is not an id, so it never gets a list even though the decompiler
# annotates it -- its id half is a separate field and that is what gets picked.
_NOT_PICKABLE = frozenset({'character'})


def symbol_domain(info: 'CommandInfo | None', key: str) -> str | None:
    '''Which id table `key` draws from, or None if it is a plain number.

    The mapping is the format layer's own, the same one that decides which fields get
    a `// name` comment on decompile, so a field offers a list exactly when the
    decompiler would have named it.
    '''
    if key in _NOT_PICKABLE:
        return None
    engine = resolve_form_name(info.name) if info is not None else ''
    engine_key = resolve_parameter_name(engine, key) if engine else key
    overrides = FORM_SYMBOL_DOMAINS.get(engine, {})
    domain = (overrides.get(key) or overrides.get(engine_key)
              or PARAMETER_SYMBOL_DOMAINS.get(key)
              or PARAMETER_SYMBOL_DOMAINS.get(engine_key))
    return domain if domain in PICKABLE_DOMAINS else None


def is_writable_head(name: str) -> bool:
    """Whether the compiler accepts `name` as a command head.

    The index can publish a command under the dispatch table's handler name,
    which is not always a spelling an author can write -- `nop_ff` is the
    handler for 0xFF, `return_zero` is the command. Offering one the compiler
    rejects is worse than the coverage gap it fills.
    """
    return source_head_is_compilable(name)


def domain_choices(domain: str) -> list[tuple[int, str]]:
    '''Every (id, name) in `domain`, lowest id first.

    Character includes the abstraction codes -- "current character" and the
    party slots -- which are not real ids but are the most common operands in
    the format, and are what an author reaches for first.
    '''
    entries = dict(SYMBOLS._tables.get(domain, {}))
    if domain == 'character':
        entries.update(CHARACTER_ABSTRACTION_CODES)
    return sorted(entries.items())


###------------------------------------------ Value occurrences --------------------------------------###

_TOKEN = re.compile(r'0x[0-9A-Fa-f]+|-?\d+(?:\.\d+)?|"(?:[^"\\]|\\.)*"|[A-Za-z_][A-Za-z0-9_]*')


def value_key(text: str) -> str | None:
    '''A comparable identity for a value, or None if it is not one.

    Numbers compare numerically, so `1000` and `0x3E8` are the same value --
    which matters here because the decompiler prints flags in decimal and
    masks in hex, and an author tracing an event value through a script should
    not have to notice which spelling a line happened to use.
    '''
    token = text.strip()
    if not token:
        return None
    number = parse_number(token)
    if number is not None:
        return f'#{number}'
    if token.startswith('"'):
        return f'={token}'
    return None


def token_at(text: str, column: int) -> tuple[str, int, int] | None:
    '''The token containing `column`, as (text, start, end).'''
    for match in _TOKEN.finditer(text):
        if match.start() <= column <= match.end():
            return match.group(), match.start(), match.end()
    return None


def value_occurrences(line: CodeLine, key: str) -> list[tuple[int, int]]:
    '''Spans in `line.text` whose value matches `key`.

    Only argument values count. A parameter *name* that happens to read as a
    number is not a value, and neither is a head or a label, so tracing `1000`
    never lights up something that merely contains it.
    '''
    spans: list[tuple[int, int]] = []
    if not line.is_call:
        return spans
    open_paren = line.text.find('(')
    if open_paren < 0:
        return spans
    limit = comment_start(line.text)
    if limit < 0:
        limit = len(line.text)
    for match in _TOKEN.finditer(line.text, open_paren, limit):
        before = line.text[:match.start()].rstrip()
        if before.endswith('='):                       # a value
            pass
        elif before.endswith(('(', ',')):              # a positional value
            pass
        else:
            continue
        if line.text[match.end():match.end() + 1] == '=':
            continue                                   # actually a parameter name
        if value_key(match.group()) == key:
            spans.append((match.start(), match.end()))
    return spans


def parse_number(text: str) -> int | None:
    '''Parse a field value the way the compiler does, or None if it is not a number.'''
    try:
        return parse_hex_int(text.strip())
    except (ValueError, AttributeError):
        return None


_MAX_DERIVED_RETRIES = 8
_ANNOTATION_ENTRY = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$')


def prune_annotation(comment: str, changed_keys: set[str]) -> str:
    '''Drop the decompiler's `// field=Name` notes for fields that just changed.

    The names are looked up from the ids, so leaving them after an edit would
    label the new id with the old name. Anything that is not a generated
    annotation -- a comment the user wrote -- is left exactly as it is.
    '''
    if not comment.startswith('//') or not changed_keys:
        return comment
    entries = [part.strip() for part in comment[2:].split(',')]
    parsed = [_ANNOTATION_ENTRY.match(entry) for entry in entries]
    if not all(parsed):
        return comment
    kept = [entry for entry, match in zip(entries, parsed) if match.group(1) not in changed_keys]  # type: ignore[union-attr]
    return f'// {", ".join(kept)}' if kept else ''


@dataclass
class EditResult:
    '''Outcome of applying a parameter edit to one line.'''
    text:    str = ''
    dropped: tuple[str, ...] = ()       # derived fields the compiler made us recompute
    error:   EvdCompileError | None = None
    changed_line: str = ''
    line:    int = 0                    # the line the edit targeted
    offsets: dict[int, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def error_text(self) -> str:
        '''The error, pointing at the edited line when the compiler did not.

        Some rejections come out of a bare conversion rather than a checked
        field, so they carry no line of their own. Naming the line that was
        being edited is still true and is the only place the user can look.
        '''
        if self.error is None:
            return ''
        if self.error.line or not self.line:
            return str(self.error)
        return f'line {self.line}: {self.error.message}'


def apply_line_edit(lines: list[CodeLine], number: int, args: tuple[Arg, ...],
                    comment: str | None = None) -> EditResult:
    '''Replace line `number`'s arguments and re-validate the whole script.

    Editing an input leaves the parameters derived from it stale, and the
    compiler rejects that rather than ignoring it. Rather than guessing a
    dependency graph, this drops exactly the field each rejection names and
    retries: the compiler is the only thing that actually knows.
    '''
    index = number - 1
    if not (0 <= index < len(lines)):
        return EditResult(error=EvdCompileError(f'no line {number} to edit'), line=number)

    original = lines[index]
    candidate = list(args)
    dropped: list[str] = []

    explicit_comment = comment
    before = {a.key: a.value for a in original.args}
    changed = {a.key for a in args if a.key and before.get(a.key) != a.value}
    changed |= {key for key in before if key and key not in {a.key for a in args}}

    for _ in range(_MAX_DERIVED_RETRIES):
        note = (explicit_comment if explicit_comment is not None
                else prune_annotation(original.comment, changed | set(dropped)))
        edited = original.with_args(tuple(candidate), note)
        text = render_code(lines[:index] + [edited] + lines[index + 1:])
        # assemble rather than validate: the offsets come out of the same
        # compile the check needs, so the addresses stay live for free.
        result = assemble(text)
        error = result.error
        if error is None:
            return EditResult(text=text, dropped=tuple(dropped), changed_line=edited.text,
                              line=number, offsets=result.offsets)
        stale = error.conflicting_field
        blames_elsewhere = error.line is not None and error.line != number
        if stale is None or blames_elsewhere or not any(a.key == stale for a in candidate):
            return EditResult(error=error, line=number)
        candidate = [a for a in candidate if a.key != stale]
        dropped.append(stale)

    return EditResult(line=number, error=EvdCompileError(
        'could not reconcile the derived parameters on this line', number
    ))


def apply_text_edit(lines: list[CodeLine], number: int, text_line: str) -> EditResult:
    '''Replace one line with raw text and re-validate.'''
    index = number - 1
    if not (0 <= index < len(lines)):
        return EditResult(error=EvdCompileError(f'no line {number} to edit'), line=number)
    replacement = parse_line(text_line, number)
    text = render_code(lines[:index] + [replacement] + lines[index + 1:])
    result = assemble(text)
    return EditResult(text='' if result.error else text, error=result.error,
                      changed_line=replacement.text, line=number, offsets=result.offsets)

###--------------------------------------------- Statistics ------------------------------------------###

@dataclass(frozen=True)
class ScriptStats:
    '''Headline numbers for the toolbar.'''
    byte_size:     int
    line_count:    int
    command_count: int
    label_count:   int
    block_count:   int
    header_flags:  int
    marker_count:  int
    raw_commands:  int = 0    # commands with no structured form, carrying raw words

    def summary(self) -> str:
        parts = [
            f'{self.command_count} commands',
            f'{self.label_count} labels',
            f'{self.byte_size} bytes',
        ]
        if self.block_count:
            parts.insert(1, f'{self.block_count} blocks')
        if self.marker_count:
            parts.append(f'{self.marker_count} markers')
        if self.raw_commands:
            parts.append(f'{self.raw_commands} raw')
        return ', '.join(parts)


def script_stats(data: bytes, lines: list[CodeLine]) -> ScriptStats:
    header_flags = u32(data, 0x04) if len(data) >= 8 else 0
    header_extra = u32(data, 0x08) if len(data) >= 12 else 0
    marker_table = decode_marker_table_source(data, header_extra) if header_extra else None
    return ScriptStats(
        byte_size=len(data),
        line_count=len(lines),
        command_count=sum(1 for line in lines if line.kind == KIND_COMMAND),
        label_count=sum(1 for line in lines if line.kind == KIND_LABEL),
        block_count=sum(1 for line in lines if line.opens and line.kind == KIND_COMMAND),
        header_flags=header_flags,
        marker_count=len(marker_table['targets']) if marker_table else 0,
        raw_commands=sum(
            1 for line in lines
            if line.kind == KIND_COMMAND and (line.info.raw if line.info else False)
        ),
    )


###=========================================================================================###
###                                     3. THE HANDLER                                      ###
###=========================================================================================###
class EvdError(RuntimeError):
    '''Raised when an EVD cannot be presented as a script at all.'''


@dataclass(frozen=True)
class EvdEditorPayload:
    '''What the worker thread hands the editor.

    `code` is the single source of truth: `lines` is a parse of it, and both
    views edit it. `source` is the flat EVDSRC the block form lowers to, kept
    only so the debug panel can show what the compiler actually sees.
    '''
    name:     str
    raw:      bytes
    code:     str
    lines:    tuple[CodeLine, ...]
    stats:    ScriptStats
    offsets:  dict[int, int]            # EVDCODE line -> byte offset in the assembled file
    source:   str = ''
    warning:  str = ''

    @property
    def line_count(self) -> int:
        return len(self.lines)


class EvdSavePayload(NamedTuple):
    '''What EvdEditor.current_data() returns and decode_editor_data receives.'''
    code: str


###---------------------------------------------------- Handler -----------------------------------------------------###

@Registry.register(
    'EVD Script Handler',
    extensions=('.evd',),
    supported_actions=(
        ActionDef(name='Skip cutscenes', action_type=ActionType.PATCH),
        ActionDef('Properties', ActionType.DIALOG),
    ))
class EVDHandler(LeafHandler):
    '''Leaf handler for EVD script files.'''

    def __init__(self, source: bytes, parent: VfsNode | None = None) -> None:
        super().__init__(source)
        self._raw = source

    ###------------------------------------- Editor pipeline -------------------------------------###

    def prepare_editor_data(self, node: VfsNode, raw_bytes: bytes) -> EvdEditorPayload:
        '''Decompile to EVDCODE. Runs on a worker thread; see documentation.md.'''
        data = raw_bytes or self._raw
        if not data.startswith(EVD_MAGIC):
            raise EvdError(f'{node.name} is not an EVD script (expected magic {EVD_MAGIC!r})')
        try:
            code = decompile_code(data, node.name)
        except Exception as e:
            raise EvdError(f'Could not decompile {node.name}: {e}') from e

        lines = parse_code(code)
        # Assembled here on the worker thread rather than on first edit: the
        # check that it round-trips and the byte offset of every line come out
        # of the same compile, and the editor needs the offsets to draw its
        # address column the moment the script opens.
        warning = ''
        built = assemble(code)
        if not built.ok:
            warning = f'Decompiled script does not compile: {built.error}'
            logger.error(f'{node.name}: {warning}')
        elif built.data != data:
            # The decompiler already proved this round-trips, so a failure here
            # is a real defect rather than an unsupported script.
            warning = 'Decompiled script does not reassemble to the original bytes; saving would change the file.'
            logger.error(f'{node.name}: {warning}')
        elif not built.offsets:
            logger.warning(f'{node.name}: line addresses unavailable; the address column will be blank')

        try:
            source = decompile_source(data)
        except Exception as e:
            source = f'; EVDSRC unavailable: {e}'

        return EvdEditorPayload(
            name=node.name,
            raw=data,
            code=code,
            lines=tuple(lines),
            stats=script_stats(data, lines),
            offsets=built.offsets,
            source=source,
            warning=warning,
        )

    def decode_editor_data(self, node: VfsNode, payload: EvdSavePayload, **kwargs) -> bytes:
        '''Compile the edited EVDCODE back to raw bytes.'''
        if not isinstance(payload, EvdSavePayload):
            raise ValueError('Invalid payload: expected EvdSavePayload')
        data = compile_code(payload.code)
        logger.info(f'{node.name}: compiled {len(payload.code.splitlines())} EVDCODE lines to {len(data)} bytes')
        return data

    ###---------------------------------------- Actions ------------------------------------------###

    def execute_action(self, node: VfsNode, action_name: str, **kwargs):
        if action_name == 'Skip cutscenes':
            return self.skip_cutscenes(node)
        if action_name == 'Properties':
            return self.properties()
        return None

    def properties(self) -> str:
        data = self._raw
        if not data.startswith(EVD_MAGIC):
            return 'error: not an EVD script'
        try:
            lines = parse_code(decompile_code(data))
        except Exception as e:
            return f'error: {e}'
        stats = script_stats(data, lines)
        return (
            f'header flags: {stats.header_flags}\n'
            f'marker table entries: {stats.marker_count}\n'
            f'commands: {stats.command_count}\n'
            f'blocks: {stats.block_count}\n'
            f'labels: {stats.label_count}\n'
            f'commands with no structured form: {stats.raw_commands}\n'
            f'size: {stats.byte_size} bytes'
        )

    def skip_cutscenes(self, node: VfsNode) -> bytes:
        keep_heads = {
            'event', 'set_value', 'set_flag', 'clear_flag', 'if_value', 'if_flag',
            'jump', 'set_game_clock', 'change_inventory', 'trigger', 'branch',
            'nop', 'change_map', 'stop_script', 'start_script', 'unless_value',
            'change_game_mode_and_flags', 'end_script', 'configure_battle',
            'add_battle_character', 'bytes', 'header', 'headerExtra', 'header_extra',
            'entry', 'label', 'set_background_visibility', 'load_background', 'start_script_stacked',
            'play_fmv', 'play_movie', 'stop_movie'
        }
        if not self._raw.startswith(EVD_MAGIC):
            raise EvdError(f'{node.name} is not an EVD script (expected magic {EVD_MAGIC!r})')
        try:
            code = decompile_code(self._raw, node.name)
        except Exception as e:
            raise EvdError(f'Failed to decompile EVD code: {e}')

        lines = parse_code(code)
        out: list[str] = []
        for line in lines:
            if line.kind != KIND_COMMAND or line.opens or line.closes:
                out.append(line.text)
                continue
            if line.head in keep_heads:
                out.append(line.text)
                continue
            # logger.debug(f'Skipping {line.text.strip()}')
            # out.append(' ' * line.indent + 'nop()')

        return compile_code('\n'.join(out) + '\n')
