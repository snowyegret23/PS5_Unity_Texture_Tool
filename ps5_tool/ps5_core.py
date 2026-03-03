"""Core CLI and processing pipeline for Unity font replacement.

This module contains scanning, parsing, replacement, preview export, and
PS5 swizzle/unswizzle support for Unity font assets.
"""

from __future__ import annotations

import argparse
import atexit
import gc
import inspect
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback as tb_module
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, NoReturn, cast

import UnityPy
from PIL import Image, ImageOps
from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator
try:
    from UnityPy.enums import TextureFormat as _UnityTextureFormatEnum
except Exception:  # pragma: no cover - optional at runtime
    _UnityTextureFormatEnum = None

try:
    import texture2ddecoder
except Exception:  # pragma: no cover - optional dependency
    texture2ddecoder = None

logger = logging.getLogger(__name__)


Language = Literal["ko", "en"]
JsonDict = dict[str, Any]
_REGISTERED_TEMP_DIRS: set[str] = set()
PS5_SWIZZLE_MASK_X = 0x385F0
PS5_SWIZZLE_MASK_Y = 0x07A0F
PS5_SWIZZLE_ROTATE = 90

# Ghidra-grounded format metadata from FUN_0091cd90 runtime table init.
# word0 is the first qword field (e.g. 0x1d0000000a for DXT1/BC1).
# block_pack is packed as: bpb | (block_w << 8) | (block_h << 16) | (depth << 24)
_PS5_GHIDRA_FORMAT_META: dict[int, dict[str, Any]] = {
    4: {"label": "R8B8G8A8", "word0": 0x00000004, "block_pack": 0x1010104},
    10: {"label": "DXT1|BC1", "word0": 0x1D0000000A, "block_pack": 0x1040408},
    12: {"label": "DXT5|BC3", "word0": 0x1D0000000C, "block_pack": 0x1040410},
    24: {"label": "BC6H", "word0": 0x1D00000018, "block_pack": 0x1040410},
    25: {"label": "BC7", "word0": 0x1D00000019, "block_pack": 0x1040410},
    26: {"label": "BC4", "word0": 0x1D0000001A, "block_pack": 0x1040408},
    27: {"label": "BC5", "word0": 0x1D0000001B, "block_pack": 0x1040410},
}

# Ghidra DAT_01b37a60 format flags (index = GPU format id / TextureFormat value in this title)
# Used to derive iVar15 in FUN_003bbdd0: ((flags & 0x6) * 2) + 8.
_PS5_GHIDRA_FORMAT_FLAGS: dict[int, int] = {
    10: 0x0024,  # DXT1|BC1
    12: 0x0000,  # DXT5|BC3
    24: 0x008C,  # BC6H
    25: 0x0094,  # BC7
    26: 0x00A4,  # BC4
    27: 0x0084,  # BC5
}
_PS5_BC_DECODER_BY_FORMAT: dict[int, str] = {
    10: "decode_bc1",
    12: "decode_bc3",
    24: "decode_bc6",
    25: "decode_bc7",
    26: "decode_bc4",
    27: "decode_bc5",
}


def _ps5_unpack_block_pack(block_pack: int) -> tuple[int, int, int, int]:
    packed = int(block_pack) & 0xFFFFFFFF
    bytes_per_block = packed & 0xFF
    block_w = (packed >> 8) & 0xFF
    block_h = (packed >> 16) & 0xFF
    depth = (packed >> 24) & 0xFF
    return bytes_per_block, block_w, block_h, depth


def _ps5_build_bc_formats_from_ghidra() -> dict[int, tuple[int, int, int, str]]:
    out: dict[int, tuple[int, int, int, str]] = {}
    for texture_format, decoder_name in _PS5_BC_DECODER_BY_FORMAT.items():
        meta = _PS5_GHIDRA_FORMAT_META.get(int(texture_format))
        if not meta:
            continue
        bpb, bw, bh, depth = _ps5_unpack_block_pack(int(meta["block_pack"]))
        if depth != 1 or bpb <= 0 or bw <= 0 or bh <= 0:
            continue
        out[int(texture_format)] = (bw, bh, bpb, decoder_name)
    return out


_PS5_BC_FORMATS: dict[int, tuple[int, int, int, str]] = _ps5_build_bc_formats_from_ghidra()

# Swizzle modes for Addrlib v2 (GFX10+) used by PS5.
_PS5_ADDR_SW_256B_S = 1
_PS5_ADDR_SW_256B_D = 2
_PS5_ADDR_SW_4KB_S = 5
_PS5_ADDR_SW_4KB_D = 6
_PS5_ADDR_SW_64KB_S = 9
_PS5_ADDR_SW_64KB_D = 10
_PS5_ADDR_SW_4KB_S_X = 21
_PS5_ADDR_SW_4KB_D_X = 22
_PS5_ADDR_SW_64KB_S_X = 25
_PS5_ADDR_SW_64KB_D_X = 26

_PS5_BC_MODE_INFO: dict[str, tuple[int, str, int, bool]] = {
    "256B_S": (_PS5_ADDR_SW_256B_S, "GFX10_SW_256_S_PATINFO", 8, False),
    "256B_D": (_PS5_ADDR_SW_256B_D, "GFX10_SW_256_D_PATINFO", 8, False),
    "4KB_S": (_PS5_ADDR_SW_4KB_S, "GFX10_SW_4K_S_PATINFO", 12, False),
    "4KB_D": (_PS5_ADDR_SW_4KB_D, "GFX10_SW_4K_D_PATINFO", 12, False),
    "4KB_S_X": (_PS5_ADDR_SW_4KB_S_X, "GFX10_SW_4K_S_X_PATINFO", 12, True),
    "4KB_D_X": (_PS5_ADDR_SW_4KB_D_X, "GFX10_SW_4K_D_X_PATINFO", 12, True),
    "64KB_S": (_PS5_ADDR_SW_64KB_S, "GFX10_SW_64K_S_PATINFO", 16, False),
    "64KB_D": (_PS5_ADDR_SW_64KB_D, "GFX10_SW_64K_D_PATINFO", 16, False),
    "64KB_S_X": (_PS5_ADDR_SW_64KB_S_X, "GFX10_SW_64K_S_X_PATINFO", 16, True),
    "64KB_D_X": (_PS5_ADDR_SW_64KB_D_X, "GFX10_SW_64K_D_X_PATINFO", 16, True),
}
_PS5_BC_FAST_MODE_NAMES = ["4KB_S", "64KB_S", "4KB_D", "256B_S", "64KB_D", "256B_D"]

# Ghidra-verified thin 2D tile dimensions from FUN_003bbdd0 table selection:
#   DAT_01b37a20 (256B), DAT_01b37920 (4KB), DAT_01b379a0 (64KB)
_PS5_GHIDRA_BLOCK256_2D_BITS: dict[int, tuple[int, int]] = {
    1: (4, 4),
    2: (4, 3),
    4: (3, 3),
    8: (3, 2),
    16: (2, 2),
}
_PS5_GHIDRA_BLOCK4K_2D_BITS: dict[int, tuple[int, int]] = {
    1: (6, 6),
    2: (6, 5),
    4: (5, 5),
    8: (5, 4),
    16: (4, 4),
}
_PS5_GHIDRA_BLOCK64K_2D_BITS: dict[int, tuple[int, int]] = {
    1: (8, 8),
    2: (8, 7),
    4: (7, 7),
    8: (7, 6),
    16: (6, 6),
}

# DAT_01b377f0 mode=5 (4KB_S) triplets by elem index (log2(bytes_per_block)).
# Raw triplet order is preserved as observed in Ghidra/file probe.
_PS5_GHIDRA_MODE5_TRIPLETS_BY_BPB: dict[int, tuple[int, int, int]] = {
    1: (0, 6, 6),
    2: (0, 6, 5),
    4: (0, 5, 5),
    8: (0, 5, 4),
    16: (0, 4, 4),
}

# Per-bpe micro-tile dimensions (x_bits, y_bits) determined by brute-force analysis.
# AMD GCN/RDNA thin micro-tile:
#   bpe=1 (Alpha8):  32x16 pixels (512 bytes) – axes transposed (HxW)
#   bpe=4 (RGBA32):   8x4  pixels (128 bytes) – axes NOT transposed (WxH)
#   bpe=2/3: most textures are linear (not swizzled); use conservative defaults.
_PS5_MICRO_TILE_BITS: dict[int, tuple[int, int]] = {
    1: (5, 4),  # 32x16
    2: (4, 3),  # 16x8  (conservative fallback)
    3: (4, 3),  # 16x8  (conservative fallback)
    4: (3, 2),  #  8x4
}
_PS5_MICRO_X_BITS_DEFAULT = 5  # legacy default (8bpp)
_PS5_MICRO_Y_BITS_DEFAULT = 4

# Per-bpe axis transposition rule for non-square textures.
# True = physical layout stores axes transposed (unswizzle at HxW, then rotate 90°).
# False = physical layout preserves metadata axes (unswizzle at WxH, no swap needed).
_PS5_AXIS_TRANSPOSE: dict[int, bool] = {
    1: True,   # Alpha8: always transposed
    2: False,  # conservative – most are linear anyway
    3: False,  # conservative – most are linear anyway
    4: False,  # RGBA32: never transposed
}


def _ps5_get_micro_tile_bits(bytes_per_element: int = 1) -> tuple[int, int]:
    """Return (x_bits, y_bits) for the given bytes-per-element."""
    return _PS5_MICRO_TILE_BITS.get(
        bytes_per_element,
        (_PS5_MICRO_X_BITS_DEFAULT, _PS5_MICRO_Y_BITS_DEFAULT),
    )
# KR: Unity-Runtime-Libraries reports/sdf_font 분석 기준 경계 버전입니다.
# EN: Boundary versions derived from Unity-Runtime-Libraries reports/sdf_font.
_TMP_OLD_ONLY_LAST = (2018, 3, 14)
_TMP_NEW_SCHEMA_FIRST = (2018, 4, 2)
_TMP_CREATION_SETTINGS_KEYS = (
    "m_CreationSettings",
    "m_FontAssetCreationSettings",
    "m_fontAssetCreationEditorSettings",
)
_TMP_DIRTY_FLAG_KEYS = (
    "m_IsFontAssetLookupTablesDirty",
    "IsFontAssetLookupTablesDirty",
)
_TMP_GLYPH_INDEX_LIST_KEYS = (
    "m_GlyphIndexList",
    "m_GlyphIndexes",
)
BUNDLE_SIGNATURES = {"UnityFS", "UnityWeb", "UnityRaw"}
_OLD_LINE_METRIC_KEYS = (
    "LineHeight",
    "Baseline",
    "Ascender",
    "CapHeight",
    "Descender",
    "CenterLine",
    "Scale",
    "SuperscriptOffset",
    "SubscriptOffset",
    "SubSize",
    "Underline",
    "UnderlineThickness",
    "strikethrough",
    "strikethroughThickness",
    "TabWidth",
)
_OLD_LINE_METRIC_SCALE_KEYS = (
    "LineHeight",
    "Baseline",
    "Ascender",
    "CapHeight",
    "Descender",
    "CenterLine",
    "SuperscriptOffset",
    "SubscriptOffset",
    "Underline",
    "UnderlineThickness",
    "strikethrough",
    "strikethroughThickness",
    "TabWidth",
)
_NEW_LINE_METRIC_KEYS = (
    "m_LineHeight",
    "m_AscentLine",
    "m_CapLine",
    "m_MeanLine",
    "m_Baseline",
    "m_DescentLine",
    "m_Scale",
    "m_SuperscriptOffset",
    "m_SuperscriptSize",
    "m_SubscriptOffset",
    "m_SubscriptSize",
    "m_UnderlineOffset",
    "m_UnderlineThickness",
    "m_StrikethroughOffset",
    "m_StrikethroughThickness",
    "m_TabWidth",
)
_NEW_LINE_METRIC_SCALE_KEYS = (
    "m_LineHeight",
    "m_AscentLine",
    "m_CapLine",
    "m_MeanLine",
    "m_Baseline",
    "m_DescentLine",
    "m_SuperscriptOffset",
    "m_SubscriptOffset",
    "m_UnderlineOffset",
    "m_UnderlineThickness",
    "m_StrikethroughOffset",
    "m_StrikethroughThickness",
    "m_TabWidth",
)
_MATERIAL_PADDING_SCALE_KEYS = (
    "_GradientScale",
    "_FaceDilate",
    "_OutlineWidth",
    "_OutlineSoftness",
    "_UnderlayDilate",
    "_UnderlaySoftness",
    "_UnderlayOffsetX",
    "_UnderlayOffsetY",
    "_GlowOffset",
    "_GlowInner",
    "_GlowOuter",
)
LOG_CONSOLE_FORMAT = "%(message)s"
LOG_FILE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_FILE_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
VERBOSE_LOG_FILENAME = "verbose.txt"


def _compose_log_message(*parts: object, sep: str = " ") -> str:
    """KR: 로그 파트를 하나의 문자열로 합칩니다.
    EN: Join variadic log parts into one message string.
    """
    return sep.join(str(part) for part in parts)


def _configure_logging(
    console_level: int = logging.INFO,
    verbose_log_path: str | None = None,
) -> None:
    """KR: 콘솔/파일 로그 핸들러를 구성합니다.
    EN: Configure console and optional verbose file handlers.
    """
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.DEBUG if verbose_log_path else console_level)

    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(logging.Formatter(LOG_CONSOLE_FORMAT))
    root_logger.addHandler(console_handler)

    if verbose_log_path:
        file_handler = logging.FileHandler(
            verbose_log_path,
            mode="w",
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(LOG_FILE_FORMAT, datefmt=LOG_FILE_DATE_FORMAT)
        )
        root_logger.addHandler(file_handler)


def _coerce_log_level(message: str, default_level: int = logging.INFO) -> int:
    """Infer logging level from localized message prefixes."""
    lowered = message.lower()
    if "경고" in message or "warning" in lowered:
        return logging.WARNING
    if (
        "오류" in message
        or "error" in lowered
        or "failed" in lowered
        or "실패" in message
    ):
        return logging.ERROR
    return default_level


def _log_console(
    *parts: object,
    sep: str = " ",
    level: int | None = None,
    include_traceback: bool = False,
) -> None:
    """Print-compatible logging bridge used by legacy call sites."""
    message = _compose_log_message(*parts, sep=sep)
    resolved_level = _coerce_log_level(message) if level is None else level
    if include_traceback:
        logger.log(resolved_level, message, exc_info=True)
        return
    logger.log(resolved_level, message)


def _log_debug(*parts: object, sep: str = " ") -> None:
    """KR: 디버그 레벨 로그를 기록합니다.
    EN: Emit debug-level message.
    """
    logger.debug(_compose_log_message(*parts, sep=sep))


def _log_info(*parts: object, sep: str = " ") -> None:
    """KR: 정보 레벨 로그를 기록합니다.
    EN: Emit info-level message.
    """
    logger.info(_compose_log_message(*parts, sep=sep))


def _log_warning(*parts: object, sep: str = " ") -> None:
    """KR: 경고 레벨 로그를 기록합니다.
    EN: Emit warning-level message.
    """
    logger.warning(_compose_log_message(*parts, sep=sep))


def _log_error(*parts: object, sep: str = " ") -> None:
    """KR: 오류 레벨 로그를 기록합니다.
    EN: Emit error-level message.
    """
    logger.error(_compose_log_message(*parts, sep=sep))


def _log_exception(*parts: object, sep: str = " ") -> None:
    """KR: 예외 Traceback 포함 에러 로그를 기록합니다.
    EN: Emit exception message with traceback.
    """
    logger.exception(_compose_log_message(*parts, sep=sep))


@lru_cache(maxsize=64)
def compute_ps5_swizzle_masks(
    width: int, height: int, bytes_per_element: int = 1,
) -> tuple[int, int]:
    """KR: 텍스처 크기에 맞는 PS5 swizzle 마스크를 계산합니다.
    EN: Compute PS5 swizzle bit-masks for the given texture dimensions.

    Micro-tile size depends on bytes-per-element (bpe):
      bpe=1 → 32×16,  bpe=4 → 8×4, etc.
    Macro-tile bits are interleaved as:
    first-Y, first-X, remaining-Y…, remaining-X… above the micro-tile bits.
    Dimensions must be powers of two and >= micro-tile size.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid dimensions for PS5 swizzle masks: {width}x{height}")
    if width & (width - 1) or height & (height - 1):
        raise ValueError(
            f"PS5 swizzle requires power-of-two dimensions: {width}x{height}"
        )
    micro_x_bits, micro_y_bits = _ps5_get_micro_tile_bits(bytes_per_element)
    micro_w = 1 << micro_x_bits
    micro_h = 1 << micro_y_bits
    if width < micro_w or height < micro_h:
        raise ValueError(
            f"Texture too small for PS5 swizzle micro-tile ({micro_w}x{micro_h}): "
            f"{width}x{height}"
        )
    total_x = width.bit_length() - 1  # log2(width)
    total_y = height.bit_length() - 1  # log2(height)
    macro_x = total_x - micro_x_bits
    macro_y = total_y - micro_y_bits

    mask_x = 0
    mask_y = 0
    pos = 0
    # micro-tile Y bits (bottom)
    for _ in range(micro_y_bits):
        mask_y |= 1 << pos
        pos += 1
    # micro-tile X bits
    for _ in range(micro_x_bits):
        mask_x |= 1 << pos
        pos += 1
    # macro: first Y
    mx_rem = macro_x
    my_rem = macro_y
    if my_rem > 0:
        mask_y |= 1 << pos
        pos += 1
        my_rem -= 1
    # macro: first X
    if mx_rem > 0:
        mask_x |= 1 << pos
        pos += 1
        mx_rem -= 1
    # macro: remaining Y
    for _ in range(my_rem):
        mask_y |= 1 << pos
        pos += 1
    # macro: remaining X
    for _ in range(mx_rem):
        mask_x |= 1 << pos
        pos += 1
    return mask_x, mask_y


def _ps5_dimensions_supported(width: int, height: int, bytes_per_element: int = 1) -> bool:
    if width <= 0 or height <= 0:
        return False
    if width & (width - 1) or height & (height - 1):
        return False
    xbits, ybits = _ps5_get_micro_tile_bits(bytes_per_element)
    micro_w = 1 << xbits
    micro_h = 1 << ybits
    return width >= micro_w and height >= micro_h


def _ps5_is_power_of_two(value: int) -> bool:
    return value > 0 and (value & (value - 1)) == 0


def _ps5_iter_divisor_pairs(total: int) -> Iterable[tuple[int, int]]:
    if total <= 0:
        return
    root = int(math.isqrt(total))
    for d in range(1, root + 1):
        if (total % d) != 0:
            continue
        q = total // d
        yield d, q
        if d != q:
            yield q, d


def _ps5_infer_physical_grid(
    total_elements: int,
    logical_width: int,
    logical_height: int,
    *,
    align_width: int,
    align_height: int,
) -> tuple[int, int]:
    """Infer a likely physical grid from raw element count.

    The PS5 runtime often pads surfaces (especially BC textures) beyond
    logical dimensions.  We infer a plausible physical WxH by searching
    divisor pairs and scoring alignment/padding.
    """
    logical_total = max(0, logical_width) * max(0, logical_height)
    if (
        total_elements <= 0
        or logical_width <= 0
        or logical_height <= 0
        or total_elements < logical_total
    ):
        return logical_width, logical_height
    if total_elements == logical_total:
        return logical_width, logical_height

    best_pair: tuple[int, int] | None = None
    best_score: int | None = None

    for cand_w, cand_h in _ps5_iter_divisor_pairs(total_elements):
        if cand_w < logical_width or cand_h < logical_height:
            continue

        pad_w = cand_w - logical_width
        pad_h = cand_h - logical_height
        pad_area = (cand_w * cand_h) - logical_total

        # Prefer minimum extra area, then prefer width-padding over height-padding.
        score = pad_area * 1000 + pad_h * 32 + pad_w * 4
        if align_width > 1 and (cand_w % align_width) != 0:
            score += 250
        if align_height > 1 and (cand_h % align_height) != 0:
            score += 250
        if _ps5_is_power_of_two(cand_w):
            score -= 32
        if _ps5_is_power_of_two(cand_h):
            score -= 16

        if best_score is None or score < best_score:
            best_score = score
            best_pair = (cand_w, cand_h)

    return best_pair if best_pair is not None else (logical_width, logical_height)


def _ps5_align_up(value: int, align: int) -> int:
    if align <= 1:
        return int(value)
    return ((int(value) + int(align) - 1) // int(align)) * int(align)


def _ps5_physical_grid_candidates_for_mode(
    total_elements: int,
    logical_width: int,
    logical_height: int,
    *,
    bytes_per_block: int,
    mode_name: str,
    align_width: int,
    align_height: int,
) -> list[tuple[int, int]]:
    """Return ordered physical-grid candidates for a given BC swizzle mode.

    Order:
    1) Ghidra tile-table aligned candidate (when available),
    2) generic divisor-based inference fallback,
    3) raw logical grid fallback.
    """
    out: list[tuple[int, int]] = []

    def _push(pair: tuple[int, int]) -> None:
        if pair[0] <= 0 or pair[1] <= 0:
            return
        if pair[0] < logical_width or pair[1] < logical_height:
            return
        if pair[0] * pair[1] > total_elements:
            return
        if pair not in out:
            out.append(pair)

    bits = _ps5_ghidra_mode_tile_bits(mode_name, bytes_per_block)
    if bits is not None:
        tile_w = 1 << bits[0]
        tile_h = 1 << bits[1]
        aligned_w = _ps5_align_up(logical_width, tile_w)
        aligned_h = _ps5_align_up(logical_height, tile_h)
        _push((aligned_w, aligned_h))
        if aligned_w > 0 and (total_elements % aligned_w) == 0:
            aligned_h_from_total = total_elements // aligned_w
            if (
                aligned_h_from_total >= aligned_h
                and (aligned_h_from_total % tile_h) == 0
            ):
                _push((aligned_w, aligned_h_from_total))
        if aligned_h > 0 and (total_elements % aligned_h) == 0:
            aligned_w_from_total = total_elements // aligned_h
            if (
                aligned_w_from_total >= aligned_w
                and (aligned_w_from_total % tile_w) == 0
            ):
                _push((aligned_w_from_total, aligned_h))

    inferred = _ps5_infer_physical_grid(
        total_elements,
        logical_width,
        logical_height,
        align_width=align_width,
        align_height=align_height,
    )
    _push(inferred)
    _push((logical_width, logical_height))
    return out


def _ps5_read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _ps5_extract_block(lines: list[str], decl_prefix: str) -> list[str]:
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith(decl_prefix):
            start = i + 1
            break
    if start is None:
        raise RuntimeError(f"Declaration not found: {decl_prefix}")
    out: list[str] = []
    for line in lines[start:]:
        if line.strip().startswith("};"):
            break
        out.append(line)
    return out


def _ps5_expr_to_mask(expr: str) -> int:
    expr = expr.strip()
    if expr == "0":
        return 0
    total = 0
    for part in expr.split("^"):
        token = part.strip()
        if not token:
            continue
        ch = token[0]
        if ch not in "XYZS" or not token[1:].isdigit():
            raise RuntimeError(f"Unexpected token in swizzle expression: {token}")
        idx = int(token[1:])
        if idx < 0 or idx > 15:
            raise RuntimeError(f"Token bit out of range: {token}")
        chan = "XYZS".index(ch)
        total ^= 1 << (chan * 16 + idx)
    return total


def _ps5_parse_nibble_array(
    lines: list[str], name: str, row_width: int
) -> list[list[int]]:
    block = _ps5_extract_block(lines, f"const UINT_64 {name}")
    rows: list[list[int]] = []
    for line in block:
        if "{" not in line:
            continue
        body = line.split("{", 1)[1].split("}", 1)[0]
        items = [x.strip() for x in body.split(",") if x.strip()]
        if len(items) < row_width:
            continue
        rows.append([_ps5_expr_to_mask(items[i]) for i in range(row_width)])
    return rows


def _ps5_parse_patinfo_array(
    lines: list[str], name: str
) -> list[tuple[int, int, int, int, int]]:
    block = _ps5_extract_block(lines, f"const ADDR_SW_PATINFO {name}")
    rows: list[tuple[int, int, int, int, int]] = []
    pat = re.compile(
        r"\{\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,?\s*\}"
    )
    for line in block:
        m = pat.search(line)
        if m:
            rows.append(tuple(int(m.group(i)) for i in range(1, 6)))
    return rows


@lru_cache(maxsize=1)
def _ps5_resolve_swizzle_pattern_path() -> str | None:
    env_path = os.environ.get("PS5_SWIZZLE_PATTERN_H")
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path))
    repo_root = Path(__file__).resolve().parent
    candidates.append(
        repo_root
        / "TMP_Info"
        / "method1"
        / "pal"
        / "src"
        / "core"
        / "imported"
        / "addrlib"
        / "src"
        / "gfx10"
        / "gfx10SwizzlePattern.h"
    )
    for path in candidates:
        if path.exists():
            return str(path)
    return None


@lru_cache(maxsize=1)
def _ps5_load_bc_pattern_tables() -> dict[str, Any] | None:
    pattern_path = _ps5_resolve_swizzle_pattern_path()
    if not pattern_path:
        return None
    try:
        lines = _ps5_read_lines(Path(pattern_path))
        nib01 = _ps5_parse_nibble_array(lines, "GFX10_SW_PATTERN_NIBBLE01", 8)
        nib2 = _ps5_parse_nibble_array(lines, "GFX10_SW_PATTERN_NIBBLE2", 4)
        nib3 = _ps5_parse_nibble_array(lines, "GFX10_SW_PATTERN_NIBBLE3", 4)
        nib4 = _ps5_parse_nibble_array(lines, "GFX10_SW_PATTERN_NIBBLE4", 4)
        patinfo_tables = {
            mode_name: _ps5_parse_patinfo_array(lines, info[1])
            for mode_name, info in _PS5_BC_MODE_INFO.items()
        }
        return {
            "nib01": nib01,
            "nib2": nib2,
            "nib3": nib3,
            "nib4": nib4,
            "patinfo_tables": patinfo_tables,
        }
    except Exception:
        return None


def _ps5_compute_thin_block_dim(block_bits: int, bytes_per_block: int) -> tuple[int, int]:
    # addrlib2.cpp::ComputeThinBlockDimension (numSamples=1)
    log2_ele = int(math.log2(bytes_per_block))
    log2_num_ele = block_bits - log2_ele
    log2_w = (log2_num_ele + 1) // 2
    w = 1 << log2_w
    h = 1 << (log2_num_ele - log2_w)
    return w, h


def _ps5_parity(value: int) -> int:
    return value.bit_count() & 1


def _ps5_ghidra_mode_tile_bits(mode_name: str, bytes_per_block: int) -> tuple[int, int] | None:
    """Resolve thin-2D tile bit dimensions from Ghidra-derived tables."""
    if mode_name.endswith("_X"):
        # XOR swizzle variants require additional equation bits not reconstructed here.
        return None
    table: dict[int, tuple[int, int]] | None = None
    if mode_name.startswith("256B_"):
        table = _PS5_GHIDRA_BLOCK256_2D_BITS
    elif mode_name.startswith("4KB_"):
        table = _PS5_GHIDRA_BLOCK4K_2D_BITS
    elif mode_name.startswith("64KB_"):
        table = _PS5_GHIDRA_BLOCK64K_2D_BITS
    if table is None:
        return None
    return table.get(int(bytes_per_block))


def _ps5_ghidra_local_order(mode_name: str, bytes_per_block: int) -> str:
    """Select tile-local bit order for fallback BC unswizzle.

    Notes:
    - 256B BC (e.g. DXT1 warning texture) matches simple y-then-x packing.
    - 4KB BC16 benefits from y/x interleaving.
    - 4KB BC8 benefits from one low X bit followed by y/x interleaving.
    """
    if mode_name.startswith("4KB_") or mode_name.startswith("64KB_"):
        if int(bytes_per_block) >= 16:
            return "yxyx"
        if int(bytes_per_block) == 8:
            return "x0_yxyx"
    return "yx"


def _ps5_local_swizzle_index(
    local_x: int,
    local_y: int,
    x_bits: int,
    y_bits: int,
    order: str,
) -> int:
    if order == "yx":
        return local_y + (local_x << y_bits)
    if order == "yxyx":
        out = 0
        bit_pos = 0
        for bit in range(max(x_bits, y_bits)):
            if bit < y_bits:
                out |= ((local_y >> bit) & 1) << bit_pos
                bit_pos += 1
            if bit < x_bits:
                out |= ((local_x >> bit) & 1) << bit_pos
                bit_pos += 1
        return out
    if order == "x0_yxyx":
        if x_bits <= 0:
            return local_y
        out = local_x & 1
        bit_pos = 1
        for bit in range(max(x_bits - 1, y_bits)):
            if bit < y_bits:
                out |= ((local_y >> bit) & 1) << bit_pos
                bit_pos += 1
            if bit < (x_bits - 1):
                out |= ((local_x >> (bit + 1)) & 1) << bit_pos
                bit_pos += 1
        return out
    return local_y + (local_x << y_bits)


def _ps5_mode5_scalar_helper_3c0890(value: int) -> int:
    """Scalar helper at 0x003c0890 (mode=5, bpb=1 path)."""
    v = int(value)
    return ((v << 4) & 0x1F0) ^ ((v << 5) & 0x400)


def _ps5_mode5_scalar_helper_3c08f0(value: int) -> int:
    """Scalar helper at 0x003c08f0 (mode=5, bpb=2/4 paths)."""
    v = int(value)
    return ((v << 4) & 0x70) ^ ((v << 5) & 0x100) ^ ((v << 6) & 0x400)


def _ps5_mode5_scalar_helper_3c09d0(value: int) -> int:
    """Scalar helper at 0x003c09d0 (mode=5 dispatch entries)."""
    v = int(value)
    return ((v << 4) & 0x30) ^ ((v << 6) & 0x100) ^ ((v << 7) & 0x400)


def _ps5_mode5_vector_helper_3c08b0(value: int) -> int:
    """Vector helper at 0x003c08b0 (mode=5, bpb=1 path)."""
    v = int(value)
    return (v & 0x0F) ^ ((v << 5) & 0x200) ^ ((v << 6) & 0x800)


def _ps5_mode5_vector_helper_3c0910(value: int) -> int:
    """Vector helper at 0x003c0910 (mode=5, bpb=2 path)."""
    v = int(value)
    return ((v << 1) & 0x0E) ^ ((v << 4) & 0x80) ^ ((v << 5) & 0x200) ^ ((v << 6) & 0x800)


def _ps5_mode5_vector_helper_3c0970(value: int) -> int:
    """Vector helper at 0x003c0970 (mode=5, bpb=4 path)."""
    v = int(value)
    return ((v << 2) & 0x0C) ^ ((v << 5) & 0x80) ^ ((v << 6) & 0x200) ^ ((v << 7) & 0x800)


def _ps5_mode5_vector_helper_3c09f0(value: int) -> int:
    """Vector helper at 0x003c09f0 (mode=5, bpb=8 path)."""
    v = int(value)
    return (
        ((v << 3) & 0x08)
        ^ ((v << 5) & 0xC0)
        ^ ((v << 6) & 0x200)
        ^ ((v << 7) & 0x800)
    )


def _ps5_mode5_vector_helper_3c0a50(value: int) -> int:
    """Vector helper at 0x003c0a50 (mode=5, bpb=16 path)."""
    v = int(value)
    return ((v << 6) & 0xC0) ^ ((v << 7) & 0x200) ^ ((v << 8) & 0x800)


def _ps5_mode5_local_swizzle_index(
    local_x: int,
    local_y: int,
    bytes_per_block: int,
) -> int | None:
    """Tile-local index for mode=5 derived from Ghidra helper formulas."""
    bpb = int(bytes_per_block)
    if bpb == 1:
        base = _ps5_mode5_scalar_helper_3c0890(local_y)
        mixed = base ^ _ps5_mode5_vector_helper_3c08b0(local_x)
    elif bpb == 2:
        base = _ps5_mode5_scalar_helper_3c08f0(local_y)
        mixed = base ^ _ps5_mode5_vector_helper_3c0910(local_x)
    elif bpb == 4:
        base = _ps5_mode5_scalar_helper_3c08f0(local_y)
        mixed = base ^ _ps5_mode5_vector_helper_3c0970(local_x)
    elif bpb == 8:
        base = _ps5_mode5_scalar_helper_3c09d0(local_y)
        mixed = base ^ _ps5_mode5_vector_helper_3c09f0(local_x)
    elif bpb == 16:
        base = _ps5_mode5_scalar_helper_3c09d0(local_y)
        mixed = base ^ _ps5_mode5_vector_helper_3c0a50(local_x)
    else:
        return None
    return mixed >> int(math.log2(bpb))


def _ps5_build_bc_lut_ghidra_fallback(
    block_w: int,
    block_h: int,
    bytes_per_block: int,
    mode_name: str,
    pipe_bank_xor: int,
) -> tuple[int, ...] | None:
    """Build a BC LUT without external pattern header.

    This fallback is grounded on FUN_003bbdd0 page-class tile dimensions
    (DAT_01b37a20 / DAT_01b37920 / DAT_01b379a0) and uses deterministic
    tile-local bit deposition for non-XOR swizzle variants.
    """
    if pipe_bank_xor != 0:
        return None
    bits = _ps5_ghidra_mode_tile_bits(mode_name, bytes_per_block)
    if bits is None:
        return None
    x_bits, y_bits = bits
    if x_bits <= 0 or y_bits <= 0:
        return None

    tile_w = 1 << x_bits
    tile_h = 1 << y_bits
    if tile_w <= 0 or tile_h <= 0 or block_w <= 0 or block_h <= 0:
        return None

    local_order = _ps5_ghidra_local_order(mode_name, bytes_per_block)
    use_mode5_helper_formula = mode_name == "4KB_S"
    macro_cols = (block_w + tile_w - 1) // tile_w
    tile_elements = tile_w * tile_h
    total = block_w * block_h

    lut: list[int] = [0] * total
    for y in range(block_h):
        macro_y = y // tile_h
        local_y = y & (tile_h - 1)
        row_base = y * block_w
        macro_row_base = macro_y * macro_cols * tile_elements
        for x in range(block_w):
            macro_x = x // tile_w
            local_x = x & (tile_w - 1)
            if use_mode5_helper_formula:
                local_off = _ps5_mode5_local_swizzle_index(
                    local_x,
                    local_y,
                    bytes_per_block,
                )
                if local_off is None:
                    return None
            else:
                local_off = _ps5_local_swizzle_index(
                    local_x,
                    local_y,
                    x_bits,
                    y_bits,
                    local_order,
                )
            if local_off < 0 or local_off >= tile_elements:
                return None
            swizzled_idx = macro_row_base + macro_x * tile_elements + local_off
            if swizzled_idx >= total:
                return None
            lut[row_base + x] = swizzled_idx
    return tuple(lut)


def _ps5_compute_offset(
    pattern_bits: list[int],
    block_bits: int,
    x: int,
    y: int,
    z: int = 0,
    s: int = 0,
) -> int:
    out = 0
    for i in range(block_bits):
        m = pattern_bits[i]
        if m == 0:
            continue
        xmask = m & 0xFFFF
        ymask = (m >> 16) & 0xFFFF
        zmask = (m >> 32) & 0xFFFF
        smask = (m >> 48) & 0xFFFF
        bit = (
            _ps5_parity(x & xmask)
            ^ _ps5_parity(y & ymask)
            ^ _ps5_parity(z & zmask)
            ^ _ps5_parity(s & smask)
        )
        out |= bit << i
    return out


def _ps5_build_full_pattern(
    nib01: list[list[int]],
    nib2: list[list[int]],
    nib3: list[list[int]],
    nib4: list[list[int]],
    patinfo: tuple[int, int, int, int, int],
) -> list[int]:
    _, idx01, idx2, idx3, idx4 = patinfo
    if (
        idx01 >= len(nib01)
        or idx2 >= len(nib2)
        or idx3 >= len(nib3)
        or idx4 >= len(nib4)
    ):
        raise RuntimeError(f"Nibble index out of range: {patinfo}")
    return list(nib01[idx01]) + list(nib2[idx2]) + list(nib3[idx3]) + list(nib4[idx4])


@lru_cache(maxsize=2048)
def _ps5_build_bc_lut_cached(
    block_w: int,
    block_h: int,
    bytes_per_block: int,
    mode_name: str,
    pipe_log2: int,
    pipe_bank_xor: int,
) -> tuple[int, ...] | None:
    tables = _ps5_load_bc_pattern_tables()
    if tables is None:
        return _ps5_build_bc_lut_ghidra_fallback(
            block_w,
            block_h,
            bytes_per_block,
            mode_name,
            pipe_bank_xor,
        )
    mode_info = _PS5_BC_MODE_INFO.get(mode_name)
    if mode_info is None:
        return None
    _, _, block_bits, is_xor_mode = mode_info
    patinfo_rows = tables["patinfo_tables"].get(mode_name, [])
    pat_index = int(math.log2(bytes_per_block))
    if pat_index < 0 or pat_index >= len(patinfo_rows):
        return _ps5_build_bc_lut_ghidra_fallback(
            block_w,
            block_h,
            bytes_per_block,
            mode_name,
            pipe_bank_xor,
        )
    pattern_bits = _ps5_build_full_pattern(
        tables["nib01"],
        tables["nib2"],
        tables["nib3"],
        tables["nib4"],
        patinfo_rows[pat_index],
    )

    total = block_w * block_h
    lut: list[int] = [0] * total

    blk_w, blk_h = _ps5_compute_thin_block_dim(block_bits, bytes_per_block)
    pitch_aligned = ((block_w + blk_w - 1) // blk_w) * blk_w
    pitch_blocks = pitch_aligned // blk_w

    blk_mask = (1 << block_bits) - 1
    pipe_interleave_log2 = 8
    column_bits = 2
    bank_bits_cap = 4
    bank_xor_bits = max(
        0,
        min(
            block_bits - pipe_interleave_log2 - pipe_log2 - column_bits,
            bank_bits_cap,
        ),
    )
    pipe_mask = (1 << pipe_log2) - 1 if pipe_log2 > 0 else 0
    bank_mask = (
        ((1 << bank_xor_bits) - 1) << (pipe_log2 + column_bits)
        if bank_xor_bits > 0
        else 0
    )
    pb_xor_off = 0
    if is_xor_mode:
        pb_xor_off = (
            (pipe_bank_xor & (pipe_mask | bank_mask)) << pipe_interleave_log2
        ) & blk_mask

    elem_log2 = int(math.log2(bytes_per_block))
    for y in range(block_h):
        yb = y // blk_h
        row_base = y * block_w
        for x in range(block_w):
            xb = x // blk_w
            blk_idx = yb * pitch_blocks + xb
            blk_off = _ps5_compute_offset(pattern_bits, block_bits, x, y, 0, 0)
            addr = (blk_idx << block_bits) + (blk_off ^ pb_xor_off)
            swizzled_idx = addr >> elem_log2
            linear_idx = row_base + x
            lut[linear_idx] = swizzled_idx % total

    return tuple(lut)


def _ps5_unswizzle_bc_blocks(
    raw: bytes,
    block_w: int,
    block_h: int,
    bytes_per_block: int,
    lut: tuple[int, ...],
) -> bytes:
    total = block_w * block_h
    src = memoryview(raw[: total * bytes_per_block])
    dst = bytearray(total * bytes_per_block)
    for linear_idx, swizzled_idx in enumerate(lut):
        src_off = swizzled_idx * bytes_per_block
        dst_off = linear_idx * bytes_per_block
        dst[dst_off : dst_off + bytes_per_block] = src[
            src_off : src_off + bytes_per_block
        ]
    return bytes(dst)


def _ps5_decode_bc_to_rgba(
    raw_bytes: bytes,
    pixel_width: int,
    pixel_height: int,
    texture_format: int,
) -> bytes | None:
    if texture2ddecoder is None:
        return None
    bc_info = _PS5_BC_FORMATS.get(texture_format)
    if bc_info is None:
        return None
    _, _, _, decoder_name = bc_info
    decoder = getattr(texture2ddecoder, decoder_name, None)
    if not callable(decoder):
        return None
    try:
        return bytes(decoder(raw_bytes, pixel_width, pixel_height))
    except Exception:
        return None


def _ps5_swap_rb_image(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    r, g, b, a = rgba.split()
    return Image.merge("RGBA", (b, g, r, a))


def _ps5_should_swap_rb_for_bc_preview(texture_format: int) -> bool:
    # KR: PS5 BC 표면은 이 경로에서 BGR 성분 순서로 해석되어 R/B 교환이 필요합니다.
    # EN: PS5 BC surfaces decode as BGR in this path; apply R/B swap consistently.
    return int(texture_format) in _PS5_BC_FORMATS


def _ps5_crop_blocks_top_left(
    block_data: bytes,
    physical_block_w: int,
    logical_block_w: int,
    logical_block_h: int,
    bytes_per_block: int,
) -> bytes:
    if (
        physical_block_w <= 0
        or logical_block_w <= 0
        or logical_block_h <= 0
        or bytes_per_block <= 0
    ):
        return block_data
    logical_size = logical_block_w * logical_block_h * bytes_per_block
    if physical_block_w == logical_block_w:
        return block_data[:logical_size]
    src = memoryview(block_data)
    out = bytearray(logical_size)
    for y in range(logical_block_h):
        src_off = (y * physical_block_w) * bytes_per_block
        dst_off = (y * logical_block_w) * bytes_per_block
        row_bytes = logical_block_w * bytes_per_block
        out[dst_off : dst_off + row_bytes] = src[src_off : src_off + row_bytes]
    return bytes(out)


def _ps5_unswizzle_addrlib_uncompressed_candidate(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
) -> tuple[bytes, float] | None:
    if bytes_per_element not in {2, 4}:
        return None
    total_elements = width * height
    if total_elements <= 0 or total_elements > 2_000_000:
        return None

    logical_bytes = total_elements * bytes_per_element
    usable = data[: (len(data) // bytes_per_element) * bytes_per_element]
    if len(usable) < logical_bytes:
        return None

    physical_total = len(usable) // bytes_per_element
    inferred_w, inferred_h = _ps5_infer_physical_grid(
        physical_total,
        width,
        height,
        align_width=8,
        align_height=8,
    )
    candidates: list[tuple[int, int]] = [(width, height)]
    if (
        inferred_w * inferred_h == physical_total
        and (inferred_w, inferred_h) not in candidates
    ):
        candidates.append((inferred_w, inferred_h))

    for physical_w, physical_h in candidates:
        physical_bytes = physical_w * physical_h * bytes_per_element
        if physical_bytes > len(usable):
            continue
        lut = _ps5_build_bc_lut_cached(
            physical_w,
            physical_h,
            bytes_per_element,
            "4KB_S",
            2,
            0,
        )
        if lut is None:
            continue
        unsw_full = _ps5_unswizzle_bc_blocks(
            usable[:physical_bytes],
            physical_w,
            physical_h,
            bytes_per_element,
            lut,
        )
        unsw_logical = _ps5_crop_blocks_top_left(
            unsw_full,
            physical_w,
            width,
            height,
            bytes_per_element,
        )
        return unsw_logical, 0.0

    return None


def _ps5_pipe_bank_xor_span(
    mode_name: str, bytes_per_block: int, pipe_log2: int
) -> int:
    mode_info = _PS5_BC_MODE_INFO.get(mode_name)
    if mode_info is None:
        return 0
    _, _, block_bits, is_xor_mode = mode_info
    if not is_xor_mode:
        return 1
    pipe_log2 = max(0, int(pipe_log2))
    pipe_mask_bits = pipe_log2
    bank_xor_bits = max(
        0,
        min(
            block_bits - 8 - pipe_log2 - 2,
            4,
        ),
    )
    total_bits = pipe_mask_bits + bank_xor_bits
    if total_bits <= 0:
        return 1
    return 1 << total_bits


def _ps5_iter_pipe_bank_xor_values(
    mode_name: str,
    bytes_per_block: int,
    pipe_log2: int,
    *,
    exhaustive: bool = False,
) -> tuple[int, ...]:
    span = _ps5_pipe_bank_xor_span(mode_name, bytes_per_block, pipe_log2)
    if span <= 1:
        return (0,)
    if exhaustive:
        return tuple(range(span))
    # Keep default path fast, then fallback to exhaustive only when needed.
    quick = tuple(v for v in (0, 1, 2, 3, 4, 7) if v < span)
    return quick if quick else (0,)


def _ps5_unswizzle_bc_best_candidate(
    raw: bytes,
    pixel_width: int,
    pixel_height: int,
    texture_format: int,
    *,
    mode_candidates: Iterable[str] | None = None,
    pipe_log2_candidates: Iterable[int] | None = None,
    exhaustive: bool = False,
    exhaustive_xor: bool = False,
) -> tuple[bytes, str | None, float | None, tuple[int, int], tuple[int, int]] | None:
    bc_info = _PS5_BC_FORMATS.get(texture_format)
    if bc_info is None:
        return None
    block_w_px, block_h_px, bytes_per_block, _ = bc_info
    logical_block_w = (pixel_width + block_w_px - 1) // block_w_px
    logical_block_h = (pixel_height + block_h_px - 1) // block_h_px
    logical_block_total = logical_block_w * logical_block_h
    logical_bytes = logical_block_total * bytes_per_block

    usable = raw[: (len(raw) // bytes_per_block) * bytes_per_block]
    if len(usable) < logical_bytes:
        return None

    physical_total_blocks = len(usable) // bytes_per_block
    align = 16 if bytes_per_block >= 16 else 8

    raw_logical = usable[:logical_bytes]
    raw_rgba = _ps5_decode_bc_to_rgba(
        raw_logical, pixel_width, pixel_height, texture_format
    )
    raw_score = (
        _ps5_roughness_score(raw_rgba, pixel_width, pixel_height, 4)
        if raw_rgba is not None
        else None
    )

    modes = (
        list(mode_candidates)
        if mode_candidates is not None
        else (
            list(_PS5_BC_MODE_INFO.keys())
            if exhaustive
            else list(_PS5_BC_FAST_MODE_NAMES)
        )
    )
    pipe_candidates = (
        tuple(pipe_log2_candidates)
        if pipe_log2_candidates is not None
        else ((0, 1, 2, 3) if exhaustive else (2, 1, 3))
    )

    best_raw = raw_logical
    best_mode: str | None = None
    best_ratio: float | None = None
    best_score: float | None = None

    best_physical = (logical_block_w, logical_block_h)

    for mode_name in modes:
        physical_candidates = _ps5_physical_grid_candidates_for_mode(
            physical_total_blocks,
            logical_block_w,
            logical_block_h,
            bytes_per_block=bytes_per_block,
            mode_name=mode_name,
            align_width=align,
            align_height=align,
        )
        for physical_block_w, physical_block_h in physical_candidates:
            physical_bytes = physical_block_w * physical_block_h * bytes_per_block
            if physical_bytes > len(usable):
                continue
            source_for_layout = usable[:physical_bytes]

            for pipe_log2 in pipe_candidates:
                pipe_bank_xor_values = _ps5_iter_pipe_bank_xor_values(
                    mode_name,
                    bytes_per_block,
                    pipe_log2,
                    exhaustive=exhaustive_xor,
                )
                for pipe_bank_xor in pipe_bank_xor_values:
                    lut = _ps5_build_bc_lut_cached(
                        physical_block_w,
                        physical_block_h,
                        bytes_per_block,
                        mode_name,
                        pipe_log2,
                        pipe_bank_xor,
                    )
                    if lut is None:
                        continue
                    unsw_full = _ps5_unswizzle_bc_blocks(
                        source_for_layout,
                        physical_block_w,
                        physical_block_h,
                        bytes_per_block,
                        lut,
                    )
                    unsw_logical = _ps5_crop_blocks_top_left(
                        unsw_full,
                        physical_block_w,
                        logical_block_w,
                        logical_block_h,
                        bytes_per_block,
                    )

                    if raw_score is None:
                        if best_mode is None:
                            best_raw = unsw_logical
                            best_mode = (
                                f"{mode_name}:p{pipe_log2}:x{pipe_bank_xor}"
                            )
                            best_physical = (physical_block_w, physical_block_h)
                        continue

                    rgba = _ps5_decode_bc_to_rgba(
                        unsw_logical, pixel_width, pixel_height, texture_format
                    )
                    if rgba is None:
                        continue
                    score = _ps5_roughness_score(rgba, pixel_width, pixel_height, 4)
                    ratio = (score / raw_score) if raw_score > 0 else None
                    if best_score is None or score < best_score:
                        best_score = score
                        best_ratio = ratio
                        best_mode = f"{mode_name}:p{pipe_log2}:x{pipe_bank_xor}"
                        best_raw = unsw_logical
                        best_physical = (physical_block_w, physical_block_h)

    return (
        best_raw,
        best_mode,
        best_ratio,
        (logical_block_w, logical_block_h),
        best_physical,
    )


def _ps5_try_mode4k_end_aligned_base_candidate(
    usable: bytes,
    logical_block_w: int,
    logical_block_h: int,
    bytes_per_block: int,
) -> tuple[bytes, str, tuple[int, int]] | None:
    """Try 4KB_S candidate using end-anchored tile-aligned base window.

    This path is for non-square mip layouts where the simple mip-tail model
    can fall back to 256B mode in decompiler-derived reconstruction.
    """
    bits = _ps5_ghidra_mode_tile_bits("4KB_S", bytes_per_block)
    if bits is None:
        return None
    tile_w = 1 << bits[0]
    tile_h = 1 << bits[1]
    if tile_w <= 0 or tile_h <= 0:
        return None

    physical_block_w = _ps5_align_up(logical_block_w, tile_w)
    physical_block_h = _ps5_align_up(logical_block_h, tile_h)
    physical_bytes = physical_block_w * physical_block_h * bytes_per_block
    if physical_bytes <= 0 or physical_bytes > len(usable):
        return None

    offset_bytes = len(usable) - physical_bytes
    source_for_layout = usable[offset_bytes : offset_bytes + physical_bytes]
    lut = _ps5_build_bc_lut_cached(
        physical_block_w,
        physical_block_h,
        bytes_per_block,
        "4KB_S",
        2,
        0,
    )
    if lut is None:
        return None
    unsw_full = _ps5_unswizzle_bc_blocks(
        source_for_layout,
        physical_block_w,
        physical_block_h,
        bytes_per_block,
        lut,
    )
    unsw_logical = _ps5_crop_blocks_top_left(
        unsw_full,
        physical_block_w,
        logical_block_w,
        logical_block_h,
        bytes_per_block,
    )
    return (
        unsw_logical,
        f"4KB_S:p2:x0:o{offset_bytes}",
        (physical_block_w, physical_block_h),
    )


def _ps5_unswizzle_bc_best_candidate_ghidra(
    raw: bytes,
    pixel_width: int,
    pixel_height: int,
    texture_format: int,
    *,
    mip_count: int | None = None,
) -> tuple[bytes, str | None, float | None, tuple[int, int], tuple[int, int]] | None:
    """Deterministically choose the first valid BC variant in fixed order.

    This path intentionally avoids image-quality heuristics (e.g. roughness).
    """
    bc_info = _PS5_BC_FORMATS.get(texture_format)
    if bc_info is None:
        return None
    block_w_px, block_h_px, bytes_per_block, _ = bc_info
    logical_block_w = (pixel_width + block_w_px - 1) // block_w_px
    logical_block_h = (pixel_height + block_h_px - 1) // block_h_px
    logical_block_total = logical_block_w * logical_block_h
    logical_bytes = logical_block_total * bytes_per_block

    usable = raw[: (len(raw) // bytes_per_block) * bytes_per_block]
    if len(usable) < logical_bytes:
        return None
    source_window = usable
    mip0_offset_bytes = 0
    if mip_count is not None and int(mip_count) > 1:
        # KR: FUN_003bbdd0는 level offset을 높은 mip -> 낮은 mip 순으로 누적 저장합니다.
        # KR: 따라서 mip0는 "lower mip tail" 뒤쪽 오프셋에서 시작할 수 있습니다.
        # EN: FUN_003bbdd0 accumulates level offsets from highest mip down.
        # EN: mip0 can therefore begin after a lower-mip tail region.
        lower_tail_sum = 0
        w = max(1, int(pixel_width))
        h = max(1, int(pixel_height))
        levels: list[int] = []
        level_count = max(1, int(mip_count))
        for _ in range(level_count):
            bw = max(1, (w + block_w_px - 1) // block_w_px)
            bh = max(1, (h + block_h_px - 1) // block_h_px)
            levels.append(bw * bh * bytes_per_block)
            w = max(1, w >> 1)
            h = max(1, h >> 1)
        if len(levels) > 1:
            # KR: Ghidra 경로에서 확인된 tail packing 단위(관측치): mip별 256B 정렬, tail 2KB 정렬.
            # EN: Ghidra-grounded packing observed in this title: per-mip 256B align, tail 2KB align.
            for level_bytes in levels[1:]:
                lower_tail_sum += _ps5_align_up(level_bytes, 0x100)
            mip0_offset_bytes = _ps5_align_up(lower_tail_sum, 0x800)
            base_alloc = _ps5_align_up(levels[0], 0x800)
            modeled_total = mip0_offset_bytes + base_alloc
            if modeled_total < len(usable):
                # KR: 비정방/특수 분기(local_a0 경로)에서 lower-tail 모델이 과소추정될 수 있습니다.
                # KR: FUN_003bbdd0의 tail-first 배치 성질을 보존하면서 stream 끝 기준으로 mip0를 재고정합니다.
                # EN: Non-square/special branches (local_a0 path) can exceed the simple lower-tail model.
                # EN: Keep tail-first layout and re-anchor mip0 against stream end.
                mip0_offset_bytes += len(usable) - modeled_total
            if mip0_offset_bytes + base_alloc <= len(usable):
                base_end = mip0_offset_bytes + base_alloc
                source_window = usable[mip0_offset_bytes:base_end]
            elif mip0_offset_bytes + levels[0] <= len(usable):
                base_end = mip0_offset_bytes + levels[0]
                source_window = usable[mip0_offset_bytes:base_end]
            else:
                mip0_offset_bytes = 0
                source_window = usable

    if len(source_window) < logical_bytes:
        return None
    raw_logical = source_window[:logical_bytes]

    def _try_common_mode4k_from_window(
        window: bytes,
        offset_bytes: int,
    ) -> tuple[bytes, str | None, float | None, tuple[int, int], tuple[int, int]] | None:
        bits = _ps5_ghidra_mode_tile_bits("4KB_S", bytes_per_block)
        if bits is None:
            return None
        tile_w = 1 << bits[0]
        tile_h = 1 << bits[1]
        physical_block_w = _ps5_align_up(logical_block_w, tile_w)
        physical_block_h = _ps5_align_up(logical_block_h, tile_h)
        physical_bytes = physical_block_w * physical_block_h * bytes_per_block
        if physical_bytes <= 0 or physical_bytes > len(window):
            return None
        source_for_layout = window[:physical_bytes]
        lut = _ps5_build_bc_lut_cached(
            physical_block_w,
            physical_block_h,
            bytes_per_block,
            "4KB_S",
            2,
            0,
        )
        if lut is None:
            return None
        unsw_full = _ps5_unswizzle_bc_blocks(
            source_for_layout,
            physical_block_w,
            physical_block_h,
            bytes_per_block,
            lut,
        )
        unsw_logical = _ps5_crop_blocks_top_left(
            unsw_full,
            physical_block_w,
            logical_block_w,
            logical_block_h,
            bytes_per_block,
        )
        mode_name = "4KB_S:p2:x0"
        if int(offset_bytes) > 0:
            mode_name += f":o{int(offset_bytes)}"
        return (
            unsw_logical,
            mode_name,
            None,
            (logical_block_w, logical_block_h),
            (physical_block_w, physical_block_h),
        )

    # Common BC rule: prefer 4KB_S:p2:x0 with mip-aware source window.
    common_4kb = _try_common_mode4k_from_window(source_window, mip0_offset_bytes)
    if common_4kb is not None:
        return common_4kb

    # If payload is exactly logical base-level bytes and cannot form the 4KB
    # physical window, treat it as already linear BC payload.
    if len(source_window) == logical_bytes and mip0_offset_bytes == 0:
        return (
            raw_logical,
            "linear_passthrough",
            None,
            (logical_block_w, logical_block_h),
            (logical_block_w, logical_block_h),
        )

    physical_total_blocks = len(source_window) // bytes_per_block
    align = 16 if bytes_per_block >= 16 else 8

    # Ghidra-verified BC path lands on mode=5 (4KB_S) first.
    mode_order: list[str] = ["4KB_S"]
    for mode_name in _PS5_BC_FAST_MODE_NAMES:
        if mode_name not in mode_order:
            mode_order.append(mode_name)
    for mode_name in _PS5_BC_MODE_INFO.keys():
        if mode_name not in mode_order:
            mode_order.append(mode_name)
    pipe_order = (2, 1, 3, 0)

    for mode_name in mode_order:
        physical_candidates = _ps5_physical_grid_candidates_for_mode(
            physical_total_blocks,
            logical_block_w,
            logical_block_h,
            bytes_per_block=bytes_per_block,
            mode_name=mode_name,
            align_width=align,
            align_height=align,
        )
        for physical_block_w, physical_block_h in physical_candidates:
            physical_bytes = physical_block_w * physical_block_h * bytes_per_block
            if physical_bytes > len(source_window):
                continue
            source_for_layout = source_window[:physical_bytes]
            for pipe_log2 in pipe_order:
                for pipe_bank_xor in _ps5_iter_pipe_bank_xor_values(
                    mode_name,
                    bytes_per_block,
                    pipe_log2,
                    exhaustive=True,
                ):
                    lut = _ps5_build_bc_lut_cached(
                        physical_block_w,
                        physical_block_h,
                        bytes_per_block,
                        mode_name,
                        pipe_log2,
                        pipe_bank_xor,
                    )
                    if lut is None:
                        continue
                    unsw_full = _ps5_unswizzle_bc_blocks(
                        source_for_layout,
                        physical_block_w,
                        physical_block_h,
                        bytes_per_block,
                        lut,
                    )
                    unsw_logical = _ps5_crop_blocks_top_left(
                        unsw_full,
                        physical_block_w,
                        logical_block_w,
                        logical_block_h,
                        bytes_per_block,
                    )
                    if (
                        mip_count is not None
                        and int(mip_count) > 1
                        and mode_name.startswith("256B_")
                    ):
                        # KR: 비정방 일부에서 FUN_003bbdd0 local_a0 분기 영향으로
                        # KR: 4KB_S tile-aligned base-at-end 레이아웃이 맞는 케이스가 존재합니다.
                        # EN: Some non-square cases follow a 4KB_S tile-aligned
                        # EN: base-at-end layout in FUN_003bbdd0 local_a0 branch.
                        alt = _ps5_try_mode4k_end_aligned_base_candidate(
                            usable,
                            logical_block_w,
                            logical_block_h,
                            bytes_per_block,
                        )
                        if alt is not None:
                            alt_raw, alt_mode, alt_physical = alt
                            return (
                                alt_raw,
                                alt_mode,
                                None,
                                (logical_block_w, logical_block_h),
                                alt_physical,
                            )
                    return (
                        unsw_logical,
                        (
                            f"{mode_name}:p{pipe_log2}:x{pipe_bank_xor}:o{mip0_offset_bytes}"
                            if mip0_offset_bytes > 0
                            else f"{mode_name}:p{pipe_log2}:x{pipe_bank_xor}"
                        ),
                        None,
                        (logical_block_w, logical_block_h),
                        (physical_block_w, physical_block_h),
                    )

    return (
        raw_logical,
        None,
        None,
        (logical_block_w, logical_block_h),
        (logical_block_w, logical_block_h),
    )


def find_ggm_file(data_path: str) -> str | None:
    """KR: 데이터 폴더에서 globalgamemanagers 계열 파일 경로를 찾습니다.
    EN: Find a globalgamemanagers-like file inside the data folder.
    """
    candidates = ["globalgamemanagers", "globalgamemanagers.assets", "data.unity3d"]
    candidates_resources = ["unity default resources", "unity_builtin_extra"]
    fls: list[str] = []
    # Prefer core globalgamemanagers files first.
    for candidate in candidates:
        ggm_path = os.path.join(data_path, candidate)
        if os.path.exists(ggm_path):
            fls.append(ggm_path)
    for candidate in candidates_resources:
        ggm_path = os.path.join(data_path, "Resources", candidate)
        if os.path.exists(ggm_path):
            fls.append(ggm_path)
    if fls:
        return fls[0]
    return None


def resolve_game_path(path: str, lang: Language = "ko") -> tuple[str, str]:
    """KR: 입력 경로를 게임 루트와 _Data 경로로 정규화합니다.
    EN: Normalize input path to game root and _Data folder path.
    """
    path = os.path.normpath(os.path.abspath(path))

    if path.lower().endswith("_data"):
        data_path = path
        game_path = os.path.dirname(path)
    else:
        game_path = path
        data_folders = [
            d
            for d in os.listdir(path)
            if d.lower().endswith("_data") and os.path.isdir(os.path.join(path, d))
        ]

        if not data_folders:
            if lang == "ko":
                raise FileNotFoundError(f"'{path}'에서 _Data 폴더를 찾을 수 없습니다.")
            raise FileNotFoundError(f"Could not find _Data folder in '{path}'.")

        data_path = os.path.join(game_path, data_folders[0])

    ggm_path = find_ggm_file(data_path)
    if not ggm_path:
        if lang == "ko":
            raise FileNotFoundError(
                f"'{data_path}'에서 globalgamemanagers 파일을 찾을 수 없습니다.\n올바른 Unity 게임 폴더인지 확인해주세요."
            )
        raise FileNotFoundError(
            f"Could not find a globalgamemanagers file in '{data_path}'.\nPlease verify this is a valid Unity game folder."
        )

    return game_path, data_path


def get_data_path(game_path: str, lang: Language = "ko") -> str:
    """KR: 게임 루트에서 _Data 폴더 경로를 반환합니다.
    EN: Return _Data folder path from game root.
    """
    data_folders = [i for i in os.listdir(game_path) if i.lower().endswith("_data")]
    if not data_folders:
        if lang == "ko":
            raise FileNotFoundError(f"'{game_path}'에서 _Data 폴더를 찾을 수 없습니다.")
        raise FileNotFoundError(f"Could not find _Data folder in '{game_path}'.")
    return os.path.join(game_path, data_folders[0])


def get_unity_version(game_path: str, lang: Language = "ko") -> str:
    """KR: 게임 경로에서 Unity 버전을 읽어 반환합니다.
    EN: Read and return Unity version from the game path.
    """
    data_path = get_data_path(game_path, lang=lang)
    candidates = [
        os.path.join(data_path, "globalgamemanagers"),
        os.path.join(data_path, "globalgamemanagers.assets"),
        os.path.join(data_path, "data.unity3d"),
    ]
    existing_candidates = [p for p in candidates if os.path.exists(p)]
    if not existing_candidates:
        if lang == "ko":
            raise FileNotFoundError(
                f"'{data_path}'에서 globalgamemanagers 파일을 찾을 수 없습니다.\n올바른 Unity 게임 폴더인지 확인해주세요."
            )
        raise FileNotFoundError(
            f"Could not find a globalgamemanagers file in '{data_path}'.\nPlease verify this is a valid Unity game folder."
        )

    for candidate in existing_candidates:
        env = None
        try:
            env = UnityPy.load(candidate)

            # 1) Fast path: top-level file may already expose unity_version.
            top_file = getattr(env, "file", None)
            top_version = getattr(top_file, "unity_version", None)
            if top_version:
                return str(top_version)

            # 2) Check loaded files.
            env_files = getattr(env, "files", None)
            if isinstance(env_files, dict):
                for loaded in env_files.values():
                    uv = getattr(loaded, "unity_version", None)
                    if uv:
                        return str(uv)

            # 3) Fallback: inspect parsed objects only when present.
            objs = getattr(env, "objects", None)
            if objs:
                first_obj = objs[0]
                assets_file = getattr(first_obj, "assets_file", None)
                uv = getattr(assets_file, "unity_version", None)
                if uv:
                    return str(uv)
        except Exception:
            continue
        finally:
            env = None
            gc.collect()

    tried = ", ".join(os.path.basename(p) for p in existing_candidates)
    if lang == "ko":
        raise RuntimeError(f"Unity 버전 감지에 실패했습니다. 시도한 파일: {tried}")
    raise RuntimeError(f"Failed to detect Unity version. Tried files: {tried}")


def get_script_dir() -> str:
    """KR: 실행 기준 디렉터리(스크립트/배포 바이너리)를 반환합니다.
    EN: Return runtime directory for script or frozen executable.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def parse_target_files_arg(target_file_args: list[str] | None) -> set[str]:
    """KR: --target-file 인자(반복/콤마 구분)를 파일명 집합으로 정규화합니다.
    EN: Normalize --target-file args (repeatable/comma-separated) into a basename set.
    """
    selected_files: set[str] = set()
    if not target_file_args:
        return selected_files
    for entry in target_file_args:
        for token in str(entry).split(","):
            name = os.path.basename(token.strip())
            if name:
                selected_files.add(name)
    return selected_files


def strip_wrapping_quotes_repeated(value: str) -> str:
    """KR: 앞뒤 따옴표(' 또는 ")를 반복 제거합니다.
    EN: Repeatedly strip wrapping quotes (' or ") from both ends.
    """
    text = str(value).strip()
    while True:
        updated = text.strip().strip('"').strip("'")
        if updated == text:
            return updated
        text = updated


def sanitize_filename_component(
    value: str, fallback: str = "unnamed", max_len: int = 96
) -> str:
    """KR: 파일명 구성요소에서 경로/예약 문자를 안전한 문자로 치환합니다.
    EN: Sanitize filename component by replacing path/reserved characters.
    """
    text = str(value or "").strip()
    invalid_chars = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in invalid_chars else ch for ch in text)
    cleaned = cleaned.strip().strip(".")
    if not cleaned:
        cleaned = fallback
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    return cleaned


def resolve_output_only_path(source_file: str, data_path: str, output_root: str) -> str:
    """KR: output-only 저장 시 원본 data_path 기준 상대 경로를 유지한 출력 경로를 계산합니다.
    EN: Resolve output-only destination path while preserving path relative to data_path.
    """
    source_abs = os.path.abspath(source_file)
    data_abs = os.path.abspath(data_path)
    output_abs = os.path.abspath(output_root)
    try:
        rel_path = os.path.relpath(source_abs, data_abs)
    except ValueError:
        rel_path = os.path.basename(source_abs)
    if rel_path.startswith("..") or os.path.isabs(rel_path):
        rel_path = os.path.basename(source_abs)
    return os.path.join(output_abs, rel_path)


def register_temp_dir_for_cleanup(path: str) -> str:
    """KR: 종료 시 삭제할 임시 디렉터리를 등록하고 정규화 경로를 반환합니다.
    EN: Register a temp directory for cleanup at exit and return normalized path.
    """
    normalized = os.path.abspath(path)
    _REGISTERED_TEMP_DIRS.add(normalized)
    return normalized


def cleanup_registered_temp_dirs() -> None:
    """KR: 등록된 임시 디렉터리를 깊은 경로부터 안전하게 삭제합니다.
    EN: Safely remove registered temp directories from deepest paths first.
    """
    if not _REGISTERED_TEMP_DIRS:
        return
    for temp_dir in sorted(_REGISTERED_TEMP_DIRS, key=len, reverse=True):
        try:
            if os.path.isdir(temp_dir):
                shutil.rmtree(temp_dir)
        except Exception:
            pass
    _REGISTERED_TEMP_DIRS.clear()


atexit.register(cleanup_registered_temp_dirs)


def _close_unitypy_reader(obj: Any) -> None:
    """KR: UnityPy 내부 reader/object를 안전하게 dispose합니다.
    EN: Safely dispose UnityPy internal reader/object resources.
    """
    if obj is None:
        return
    reader = getattr(obj, "reader", None)
    if reader is not None and hasattr(reader, "dispose"):
        try:
            reader.dispose()
        except Exception:
            pass
    if hasattr(obj, "dispose"):
        try:
            obj.dispose()
        except Exception:
            pass


def close_unitypy_env(environment: Any) -> None:
    """KR: Environment에 연결된 UnityPy 파일 리소스를 순회 종료합니다.
    EN: Walk and close UnityPy file resources attached to environment.
    """
    if environment is None:
        return
    stack: list[Any] = []
    files = getattr(environment, "files", None)
    if isinstance(files, dict):
        stack.extend(files.values())
    while stack:
        item = stack.pop()
        _close_unitypy_reader(item)
        sub_files = getattr(item, "files", None)
        if isinstance(sub_files, dict):
            stack.extend(sub_files.values())


def normalize_font_name(name: str) -> str:
    """KR: 확장자/SDF 접미사를 제거해 폰트 기본 이름으로 정규화합니다.
    EN: Normalize font name by removing extension and SDF suffixes.
    """
    for ext in [".ttf", ".otf", ".json", ".png"]:
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
    for suffix in (
        " SDF Atlas",
        " Raster Atlas",
        " Atlas",
        " SDF Material",
        " Raster Material",
        " Material",
        " SDF",
        " Raster",
    ):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def parse_bool_flag(value: Any) -> bool:
    """KR: 문자열/숫자/불리언 입력을 안전하게 bool로 해석합니다.
    EN: Safely interpret string/number/bool values as bool.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


def _read_bundle_signature(
    path: str, bundle_signatures: set[str] | None = None
) -> str | None:
    """KR: 파일 헤더에서 Unity 번들 시그니처를 읽습니다.
    EN: Read Unity bundle signature from file header.
    """
    signatures = bundle_signatures or BUNDLE_SIGNATURES
    try:
        with open(path, "rb") as f:
            header = f.read(16)
    except Exception:
        return None

    for sig in signatures:
        token = (sig + "\x00").encode("ascii")
        if header.startswith(token):
            return sig
    return None


def _safe_metric_scale(game_point_size: Any, replacement_point_size: Any) -> float:
    """KR: 게임 pointSize 대비 교체 pointSize 비율을 계산합니다.
    EN: Compute scaling ratio from game pointSize to replacement pointSize.
    """
    try:
        game_ps = float(game_point_size)
        repl_ps = float(replacement_point_size)
        if game_ps > 0 and repl_ps > 0:
            return repl_ps / game_ps
    except Exception:
        pass
    return 1.0


def _detect_target_texture_swizzle(
    texture_object_lookup: dict[tuple[str, int], Any],
    texture_swizzle_state_cache: dict[str, tuple[str | None, str | None]],
    assets_name: str,
    path_id: int,
) -> tuple[str | None, str | None]:
    """KR: 타겟 Texture2D의 swizzle 판정 결과를 캐시와 함께 반환합니다.
    EN: Return cached swizzle verdict for target Texture2D.
    """
    cache_key = f"{assets_name}|{path_id}"
    if cache_key in texture_swizzle_state_cache:
        return texture_swizzle_state_cache[cache_key]
    texture_obj = texture_object_lookup.get((assets_name, int(path_id)))
    verdict, source = (
        detect_texture_object_ps5_swizzle_detail(texture_obj)
        if texture_obj is not None
        else (None, None)
    )
    texture_swizzle_state_cache[cache_key] = (verdict, source)
    return verdict, source


def _preview_visible_image(image: Image.Image) -> Image.Image:
    """KR: RGBA/LA Atlas를 사람이 보기 쉬운 단일 채널 이미지로 정규화합니다.
    EN: Normalize RGBA/LA atlas into a human-visible single-channel image.
    """
    try:
        if image.mode == "RGBA":
            alpha = image.getchannel("A")
            rgb = image.convert("RGB")
            rgb_bbox = rgb.getbbox()
            alpha_bbox = alpha.getbbox()
            if alpha_bbox and not rgb_bbox:
                return alpha
            return alpha if alpha_bbox else image.convert("L")
        if image.mode == "LA":
            alpha = image.getchannel("A")
            return alpha if alpha.getbbox() else image.getchannel("L")
        if image.mode == "P":
            return image.convert("L")
        if image.mode not in {"L", "RGB"}:
            return image.convert("L")
        return image
    except Exception:
        return image.convert("L")


def _load_target_unswizzled_preview_image(
    texture_object_lookup: dict[tuple[str, int], Any],
    assets_name: str,
    atlas_path_id: int,
    swizzle_verdict: str | None,
    preview_rotate: int = PS5_SWIZZLE_ROTATE,
) -> Image.Image | None:
    """KR: 대상 게임 Atlas(Texture2D)에서 검증용 unswizzle preview 이미지를 생성합니다.
    EN: Build an unswizzled preview image from the target in-game Texture2D atlas.
    """
    texture_obj = texture_object_lookup.get((assets_name, int(atlas_path_id)))
    if texture_obj is None:
        return None
    try:
        texture = texture_obj.parse_as_object()
        width = int(getattr(texture, "m_Width", 0) or 0)
        height = int(getattr(texture, "m_Height", 0) or 0)
        raw_data: bytes | None = None

        get_image_data = getattr(texture, "get_image_data", None)
        if callable(get_image_data):
            try:
                candidate = get_image_data()
                if isinstance(candidate, (bytes, bytearray)):
                    raw_data = bytes(candidate)
            except Exception:
                raw_data = None
        if raw_data is None:
            image_data = getattr(texture, "image_data", None)
            if isinstance(image_data, (bytes, bytearray)):
                raw_data = bytes(image_data)

        if width > 0 and height > 0 and raw_data:
            total_elements = width * height
            bpe: int | None = None
            try:
                texture_format = int(getattr(texture, "m_TextureFormat", -1) or -1)
            except Exception:
                texture_format = -1

            if _texture_format_is_bc(texture_format):
                bc_info = _PS5_BC_FORMATS.get(texture_format)
                if bc_info is not None:
                    block_w_px, block_h_px, bytes_per_block, _ = bc_info
                    logical_block_w = (width + block_w_px - 1) // block_w_px
                    logical_block_h = (height + block_h_px - 1) // block_h_px
                    logical_bytes = (
                        logical_block_w * logical_block_h * bytes_per_block
                    )
                    candidate_raw = raw_data[:logical_bytes]
                    best = None
                    if swizzle_verdict != "likely_linear_input":
                        mip_count = int(getattr(texture, "m_MipCount", 1) or 1)
                        best = _ps5_unswizzle_bc_best_candidate_ghidra(
                            raw_data,
                            width,
                            height,
                            texture_format,
                            mip_count=mip_count,
                        )
                    if best is not None:
                        best_raw, _, _, _, _ = best
                        if swizzle_verdict == "likely_swizzled_input":
                            candidate_raw = best_raw
                    rgba = _ps5_decode_bc_to_rgba(
                        candidate_raw, width, height, texture_format
                    )
                    if rgba is not None:
                        preview_rgba = Image.frombytes("RGBA", (width, height), rgba)
                        if _ps5_should_swap_rb_for_bc_preview(texture_format):
                            preview_rgba = _ps5_swap_rb_image(preview_rgba)
                        # KR: BC preview는 Unity 좌표계와 일치하도록 상하 반전합니다.
                        # EN: Flip BC preview vertically to match Unity coordinates.
                        return ImageOps.flip(preview_rgba)

            bpe_hint = _texture_format_bytes_per_element(texture_format)
            if bpe_hint is not None:
                bpe = bpe_hint
            elif total_elements > 0 and (len(raw_data) % total_elements) == 0:
                derived_bpe = len(raw_data) // total_elements
                if derived_bpe in {1, 2, 3, 4}:
                    bpe = derived_bpe

            if bpe in {1, 2, 3, 4}:
                logical_bytes = width * height * int(bpe)
                usable_data = raw_data[: (len(raw_data) // int(bpe)) * int(bpe)]
                base_data = usable_data[:logical_bytes]
                processed = base_data
                preview_width = width
                preview_height = height
                unsw_variant = "normal"
                if swizzle_verdict == "likely_swizzled_input":
                    try:
                        processed, preview_width, preview_height, unsw_variant, _ = (
                            _ps5_unswizzle_best_variant(
                                usable_data,
                                width,
                                height,
                                int(bpe),
                                allow_axis_swap=True,
                                roughness_guard=True,
                            )
                        )
                    except Exception:
                        processed = base_data
                        preview_width = width
                        preview_height = height
                        unsw_variant = "normal"
                mode_map = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}
                preview_image = Image.frombytes(
                    mode_map[int(bpe)],
                    (preview_width, preview_height),
                    processed,
                )
                if (
                    swizzle_verdict == "likely_swizzled_input"
                    and unsw_variant != "already_linear"
                ):
                    # KR: rotate는 축-스왑(전치) 된 경우에만 적용 (예: Alpha8).
                    # EN: Only apply rotation when axes were swapped (transposing bpe).
                    if unsw_variant == "swapped_axes" and preview_rotate % 360 != 0:
                        preview_image = preview_image.rotate(
                            preview_rotate % 360, expand=True
                        )
                else:
                    # KR: linear(비-swizzle) 텍스쳐는 Unity 좌표계(Y=0 하단)로 저장되므로 상하 반전 보정
                    # EN: Linear (non-swizzled) textures are stored in Unity coordinates (Y=0 at bottom); flip vertically
                    preview_image = ImageOps.flip(preview_image)
                if unsw_variant == "addrlib_4KB_S":
                    # KR: addrlib 비압축 복원 경로는 Y축이 뒤집힌 사례(ui_button)가 있어 보정합니다.
                    # EN: addrlib uncompressed path can be vertically inverted (e.g. ui_button); compensate.
                    preview_image = ImageOps.flip(preview_image)
                return preview_image

        image = getattr(texture, "image", None)
        if isinstance(image, Image.Image):
            preview_image = image
            if swizzle_verdict == "likely_swizzled_input":
                try:
                    preview_image = apply_ps5_unswizzle_to_image(
                        preview_image,
                        rotate=preview_rotate,
                        allow_axis_swap=True,
                        roughness_guard=True,
                    )
                except Exception:
                    pass
            return preview_image
    except Exception:
        return None
    return None


def _save_swizzle_preview(
    image: Image.Image,
    *,
    preview_enabled: bool,
    preview_root: str | None,
    assets_file_name: str,
    assets_name: str,
    atlas_path_id: int,
    font_name: str,
    target_swizzled: bool,
    lang: Language,
) -> None:
    if not (preview_enabled and preview_root):
        return
    try:
        visible = _preview_visible_image(image)
        file_dir = sanitize_filename_component(assets_file_name, fallback="assets_file")
        out_dir = os.path.join(preview_root, file_dir)
        os.makedirs(out_dir, exist_ok=True)
        safe_assets = sanitize_filename_component(assets_name, fallback="assets")
        safe_font = sanitize_filename_component(font_name, fallback="font")
        state_label = "target_swizzled" if target_swizzled else "target_linear"
        out_name = f"{safe_assets}__{atlas_path_id}__{safe_font}__unswizzled__{state_label}.png"
        out_path = os.path.join(out_dir, out_name)
        visible.save(out_path, format="PNG")
        if lang == "ko":
            _log_console(f"  Preview 저장: {out_path}")
        else:
            _log_console(f"  Preview saved: {out_path}")
    except Exception as preview_error:
        if lang == "ko":
            _log_console(f"  경고: preview 저장 실패 ({preview_error})")
        else:
            _log_console(f"  Warning: failed to save preview ({preview_error})")


def _save_glyph_crop_previews(
    image: Image.Image,
    *,
    preview_enabled: bool,
    preview_root: str | None,
    assets_file_name: str,
    assets_name: str,
    atlas_path_id: int,
    font_name: str,
    sdf_data: JsonDict,
    lang: Language,
) -> None:
    if not (preview_enabled and preview_root):
        return
    glyph_table = sdf_data.get("m_GlyphTable")
    char_table = sdf_data.get("m_CharacterTable")
    if not isinstance(glyph_table, list) or not isinstance(char_table, list):
        return
    try:
        visible = _preview_visible_image(image)
        file_dir = sanitize_filename_component(assets_file_name, fallback="assets_file")
        safe_assets = sanitize_filename_component(assets_name, fallback="assets")
        safe_font = sanitize_filename_component(font_name, fallback="font")
        glyph_dir = os.path.join(
            preview_root,
            file_dir,
            f"{safe_assets}__{atlas_path_id}__{safe_font}",
        )
        os.makedirs(glyph_dir, exist_ok=True)

        glyph_rect_by_index: dict[int, tuple[int, int, int, int]] = {}
        for glyph in glyph_table:
            if not isinstance(glyph, dict):
                continue
            try:
                glyph_index = int(glyph.get("m_Index", -1))
            except Exception:
                continue
            rect_raw = glyph.get("m_GlyphRect", {})
            if not isinstance(rect_raw, dict):
                continue
            try:
                gx = int(rect_raw.get("m_X", 0))
                gy = int(rect_raw.get("m_Y", 0))
                gw = int(rect_raw.get("m_Width", 0))
                gh = int(rect_raw.get("m_Height", 0))
            except Exception:
                continue
            if gw <= 0 or gh <= 0:
                continue
            glyph_rect_by_index[glyph_index] = (gx, gy, gw, gh)

        if not glyph_rect_by_index:
            return

        saved = 0
        used_names: set[str] = set()
        for ch in char_table:
            if not isinstance(ch, dict):
                continue
            try:
                codepoint = int(ch.get("m_Unicode", -1))
                glyph_index = int(ch.get("m_GlyphIndex", -1))
            except Exception:
                continue
            if codepoint < 0:
                continue
            rect = glyph_rect_by_index.get(glyph_index)
            if rect is None:
                continue

            x, y, w, h = rect
            # KR: TMP new glyphRect.y는 bottom-origin이므로 top-origin 이미지(PIL) crop 좌표로 변환합니다.
            # EN: TMP new glyphRect.y is bottom-origin; convert to top-origin image(PIL) crop coordinates.
            y = int(round(_tmp_flip_y_between_old_new(y, h, visible.height)))
            x0 = max(0, min(visible.width, x))
            y0 = max(0, min(visible.height, y))
            x1 = max(0, min(visible.width, x + w))
            y1 = max(0, min(visible.height, y + h))
            if x1 <= x0 or y1 <= y0:
                continue

            base = f"U+{codepoint:04X}"
            try:
                ch_text = chr(codepoint)
                if ch_text.isprintable() and not ch_text.isspace():
                    safe_char = sanitize_filename_component(
                        ch_text, fallback="", max_len=8
                    )
                    if safe_char and safe_char != "unnamed":
                        base = f"{base}_{safe_char}"
            except Exception:
                pass

            name = base
            if name in used_names:
                name = f"{name}_g{glyph_index}"
            used_names.add(name)
            out_path = os.path.join(glyph_dir, f"{name}.png")
            visible.crop((x0, y0, x1, y1)).save(out_path, format="PNG")
            saved += 1

        if saved > 0:
            if lang == "ko":
                _log_console(f"  Glyph preview 저장: {saved}개 -> {glyph_dir}")
            else:
                _log_console(f"  Glyph previews saved: {saved} -> {glyph_dir}")
    except Exception as preview_error:
        if lang == "ko":
            _log_console(f"  경고: glyph preview 저장 실패 ({preview_error})")
        else:
            _log_console(f"  Warning: failed to save glyph previews ({preview_error})")


def _image_to_alpha8_bytes(image: Image.Image) -> tuple[bytes, int, int]:
    """KR: Pillow 이미지를 Alpha8 raw bytes로 변환합니다.
    EN: Convert Pillow image into Alpha8 raw bytes.
    """
    if image.mode in {"RGBA", "LA"}:
        alpha = image.getchannel("A")
    elif image.mode == "L":
        alpha = image
    else:
        alpha = image.convert("L")
    return alpha.tobytes(), alpha.width, alpha.height


@lru_cache(maxsize=128)
def _ps5_bit_positions(mask: int) -> tuple[int, ...]:
    return tuple(i for i in range(max(mask.bit_length(), 0)) if (mask >> i) & 1)


@lru_cache(maxsize=128)
def _ps5_axis_tile_size(mask: int) -> int:
    positions = _ps5_bit_positions(mask)
    return 1 << len(positions) if positions else 1


@lru_cache(maxsize=128)
def _ps5_deposit_table(mask: int) -> tuple[int, ...]:
    """KR: 마스크 비트폭(타일 기준) pdep 유사 배치 테이블을 생성합니다.
    EN: Build a pdep-like deposit table using mask bit-width (tile-local axis).
    """
    positions = _ps5_bit_positions(mask)
    axis_size = _ps5_axis_tile_size(mask)
    table: list[int] = [0] * axis_size
    for value in range(axis_size):
        deposited = 0
        for bit_index, dst_bit in enumerate(positions):
            if (value >> bit_index) & 1:
                deposited |= 1 << dst_bit
        table[value] = deposited
    return tuple(table)


def _ps5_validate_texture_shape(
    data: bytes, width: int, height: int, bytes_per_element: int
) -> int:
    if width <= 0 or height <= 0 or bytes_per_element <= 0:
        raise ValueError(
            f"Invalid texture shape for swizzle: width={width}, height={height}, bpe={bytes_per_element}"
        )
    total_elements = width * height
    expected_size = total_elements * bytes_per_element
    if len(data) < expected_size:
        raise ValueError(
            f"Texture data size mismatch: expected_at_least={expected_size}, got={len(data)} "
            f"(w={width}, h={height}, bpe={bytes_per_element})"
        )
    return total_elements


def _ps5_clip_to_base_level(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
) -> tuple[bytes, int]:
    total_elements = _ps5_validate_texture_shape(data, width, height, bytes_per_element)
    expected_size = total_elements * bytes_per_element
    if len(data) > expected_size:
        return data[:expected_size], len(data) - expected_size
    return data, 0


def _texture_format_enum_name(texture_format: int) -> str:
    value = int(texture_format)
    if _UnityTextureFormatEnum is not None:
        try:
            return str(_UnityTextureFormatEnum(value).name)
        except Exception:
            pass
    return f"TextureFormat_{value}"


def _texture_format_ghidra_meta(texture_format: int) -> dict[str, Any] | None:
    value = int(texture_format)
    meta = _PS5_GHIDRA_FORMAT_META.get(value)
    if meta is None:
        return None
    block_pack = int(meta["block_pack"])
    bytes_per_block, block_w, block_h, depth = _ps5_unpack_block_pack(block_pack)
    flags_word = _PS5_GHIDRA_FORMAT_FLAGS.get(value)
    ivar15 = ((flags_word & 0x6) * 2 + 8) if flags_word is not None else None
    mode5_triplet = _PS5_GHIDRA_MODE5_TRIPLETS_BY_BPB.get(bytes_per_block)
    return {
        "label": str(meta["label"]),
        "word0": int(meta["word0"]),
        "block_pack": block_pack,
        "bytes_per_block": bytes_per_block,
        "block_width": block_w,
        "block_height": block_h,
        "block_depth": depth,
        "decoder": _PS5_BC_DECODER_BY_FORMAT.get(value),
        "flags_word": int(flags_word) if flags_word is not None else None,
        "ivar15_shift": int(ivar15) if ivar15 is not None else None,
        "mode5_triplet": list(mode5_triplet) if mode5_triplet is not None else None,
    }


def _texture_format_bytes_per_element(texture_format: int) -> int | None:
    # KR: 가능한 경우 UnityPy enum 이름 기준으로 BPE를 해석합니다.
    # EN: Prefer UnityPy enum names when available to avoid numeric drift by version.
    bpe_by_name = {
        "Alpha8": 1,
        "ARGB4444": 2,
        "RGB24": 3,
        "RGBA32": 4,
        "ARGB32": 4,
        "RGB565": 2,
        "R16": 2,
        "RG16": 2,
        "R8": 1,
    }
    value: int | None = None
    enum_name = _texture_format_enum_name(texture_format)
    if enum_name.startswith("TextureFormat_"):
        enum_name = ""
    if enum_name:
        value = bpe_by_name.get(enum_name)

    # KR: enum 해석 실패 시 최소 숫자 fallback.
    # EN: Minimal numeric fallback for environments without enum resolution.
    if value is None:
        format_to_bpe = {
            1: 1,  # Alpha8
            2: 2,  # ARGB4444
            3: 3,  # RGB24
            4: 4,  # RGBA32
            5: 4,  # ARGB32
            7: 2,  # RGB565
            9: 2,  # R16
            62: 2,  # RG16
            63: 1,  # R8
        }
        value = format_to_bpe.get(int(texture_format), None)

    if value in {1, 2, 3, 4}:
        return value
    return None


def _texture_format_is_bc(texture_format: int) -> bool:
    return int(texture_format) in _PS5_BC_FORMATS


def _texture_format_is_crunched(texture_format: int) -> bool:
    value = int(texture_format)
    if value in {28, 29}:  # DXT1Crunched / DXT5Crunched
        return True
    enum_name = _texture_format_enum_name(value)
    return enum_name in {
        "DXT1Crunched",
        "DXT5Crunched",
        "ETC_RGB4Crunched",
        "ETC2_RGBA8Crunched",
    }


def ps5_unswizzle_bytes(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    mask_x: int | None = None,
    mask_y: int | None = None,
) -> bytes:
    """KR: PS5 swizzled 바이트 배열을 선형 순서로 변환합니다.
    EN: Convert PS5-swizzled bytes into linear row-major bytes.
    mask_x/mask_y가 None이면 width/height에서 자동 계산합니다.
    When mask_x/mask_y are None they are computed from width/height.
    """
    if not _ps5_dimensions_supported(width, height, bytes_per_element):
        clipped, _ = _ps5_clip_to_base_level(data, width, height, bytes_per_element)
        return clipped
    if mask_x is None or mask_y is None:
        mask_x, mask_y = compute_ps5_swizzle_masks(width, height, bytes_per_element)
    data, _ = _ps5_clip_to_base_level(data, width, height, bytes_per_element)
    total_elements = _ps5_validate_texture_shape(data, width, height, bytes_per_element)
    src = memoryview(data)
    dst = bytearray(len(data))
    tile_w = _ps5_axis_tile_size(mask_x)
    tile_h = _ps5_axis_tile_size(mask_y)
    xdep = _ps5_deposit_table(mask_x)
    ydep = _ps5_deposit_table(mask_y)
    macro_cols = (width + tile_w - 1) // tile_w
    tile_elements = tile_w * tile_h

    for y in range(height):
        row_start = y * width
        macro_y = y // tile_h
        local_y = y % tile_h
        row_offset = ydep[local_y]
        for x in range(width):
            macro_x = x // tile_w
            local_x = x % tile_w
            tile_base = ((macro_y * macro_cols) + macro_x) * tile_elements
            src_idx = tile_base + row_offset + xdep[local_x]
            if src_idx < 0 or src_idx >= total_elements:
                raise ValueError(
                    f"PS5 unswizzle index out of range: idx={src_idx}, total={total_elements}, "
                    f"w={width}, h={height}, mask_x={mask_x:#x}, mask_y={mask_y:#x}"
                )
            src_off = src_idx * bytes_per_element
            dst_off = (row_start + x) * bytes_per_element
            dst[dst_off : dst_off + bytes_per_element] = src[
                src_off : src_off + bytes_per_element
            ]

    return bytes(dst)


def ps5_swizzle_bytes(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    mask_x: int | None = None,
    mask_y: int | None = None,
) -> bytes:
    """KR: 선형 순서 바이트 배열을 PS5 swizzle 순서로 변환합니다.
    EN: Convert linear row-major bytes into PS5-swizzled order.
    mask_x/mask_y가 None이면 width/height에서 자동 계산합니다.
    When mask_x/mask_y are None they are computed from width/height.
    """
    if not _ps5_dimensions_supported(width, height, bytes_per_element):
        clipped, _ = _ps5_clip_to_base_level(data, width, height, bytes_per_element)
        return clipped
    if mask_x is None or mask_y is None:
        mask_x, mask_y = compute_ps5_swizzle_masks(width, height, bytes_per_element)
    data, _ = _ps5_clip_to_base_level(data, width, height, bytes_per_element)
    total_elements = _ps5_validate_texture_shape(data, width, height, bytes_per_element)
    src = memoryview(data)
    dst = bytearray(len(data))
    tile_w = _ps5_axis_tile_size(mask_x)
    tile_h = _ps5_axis_tile_size(mask_y)
    xdep = _ps5_deposit_table(mask_x)
    ydep = _ps5_deposit_table(mask_y)
    macro_cols = (width + tile_w - 1) // tile_w
    tile_elements = tile_w * tile_h

    for y in range(height):
        row_start = y * width
        macro_y = y // tile_h
        local_y = y % tile_h
        row_offset = ydep[local_y]
        for x in range(width):
            macro_x = x // tile_w
            local_x = x % tile_w
            tile_base = ((macro_y * macro_cols) + macro_x) * tile_elements
            dst_idx = tile_base + row_offset + xdep[local_x]
            if dst_idx < 0 or dst_idx >= total_elements:
                raise ValueError(
                    f"PS5 swizzle index out of range: idx={dst_idx}, total={total_elements}, "
                    f"w={width}, h={height}, mask_x={mask_x:#x}, mask_y={mask_y:#x}"
                )
            src_off = (row_start + x) * bytes_per_element
            dst_off = dst_idx * bytes_per_element
            dst[dst_off : dst_off + bytes_per_element] = src[
                src_off : src_off + bytes_per_element
            ]

    return bytes(dst)


def _ps5_mode_for_swizzle(image: Image.Image) -> str:
    mode = image.mode
    if mode in {"L", "LA", "RGB", "RGBA"}:
        return mode
    if mode == "P":
        return "L"
    return "RGBA"


def _ps5_prepare_image(image: Image.Image) -> Image.Image:
    mode = _ps5_mode_for_swizzle(image)
    if image.mode == mode:
        return image
    return image.convert(mode)


def _ps5_roughness_score(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
) -> float:
    """KR: 로컬 픽셀 변화량 기반 거칠기 점수를 계산합니다.
    EN: Compute a local variation roughness score.

    Always compares **adjacent** pixels (step=1) to accurately detect swizzle
    vs linear data.  Previous versions used a ``max_axis_samples`` parameter
    that inflated the comparison step (e.g. step=16 for 4096-wide textures),
    which caused dense CJK font atlases at 4096×4096 to be mis-classified as
    'already linear'.

    For performance, a subset of rows (for dx) and columns (for dy) are sampled
    instead of iterating over every pixel.  This keeps accuracy while staying
    fast in pure Python.
    """
    data, _ = _ps5_clip_to_base_level(data, width, height, bytes_per_element)
    _ps5_validate_texture_shape(data, width, height, bytes_per_element)
    view = memoryview(data)
    bpe = bytes_per_element

    # --- Determine which channel to measure ---
    max_sample_lines = 256
    channel_index = 0
    if bpe > 1:
        # Pick channel with highest variance (most information).
        row_step = max(1, height // max_sample_lines)
        col_step = max(1, width // max_sample_lines)
        sums = [0.0] * bpe
        sums_sq = [0.0] * bpe
        sample_count = 0
        for y in range(0, height, row_step):
            row_base = y * width * bpe
            for x in range(0, width, col_step):
                base = row_base + x * bpe
                sample_count += 1
                for ch in range(bpe):
                    value = float(view[base + ch])
                    sums[ch] += value
                    sums_sq[ch] += value * value
        if sample_count > 0:
            best_var = -1.0
            for ch in range(bpe):
                mean = sums[ch] / sample_count
                variance = (sums_sq[ch] / sample_count) - (mean * mean)
                if variance > best_var:
                    best_var = variance
                    channel_index = ch

    # --- Measure dx (horizontal): sample rows, but always compare adjacent pixels ---
    dx_sum = 0.0
    dx_count = 0
    row_step = max(1, height // max_sample_lines)
    if width > 1:
        for y in range(0, height, row_step):
            row_base = y * width * bpe
            for x in range(width - 1):
                left_idx = row_base + x * bpe + channel_index
                right_idx = left_idx + bpe          # step=1, always adjacent
                dx_sum += abs(float(view[right_idx]) - float(view[left_idx]))
                dx_count += 1

    # --- Measure dy (vertical): sample columns, but always compare adjacent pixels ---
    dy_sum = 0.0
    dy_count = 0
    col_step = max(1, width // max_sample_lines)
    if height > 1:
        row_stride = width * bpe
        for x in range(0, width, col_step):
            col_base = x * bpe + channel_index
            for y in range(height - 1):
                up_idx = col_base + y * row_stride
                down_idx = up_idx + row_stride      # step=1, always adjacent
                dy_sum += abs(float(view[down_idx]) - float(view[up_idx]))
                dy_count += 1

    dx = dx_sum / dx_count if dx_count else 0.0
    dy = dy_sum / dy_count if dy_count else 0.0
    return float(dx + dy)


def detect_ps5_swizzle_state(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    mask_x: int | None = None,
    mask_y: int | None = None,
) -> tuple[str, float, float, float, bytes, bytes]:
    """KR: 입력 바이트가 swizzled인지 휴리스틱으로 판별합니다.
    EN: Heuristically detect whether input bytes are likely swizzled.
    """
    data, _ = _ps5_clip_to_base_level(data, width, height, bytes_per_element)
    if not _ps5_dimensions_supported(width, height, bytes_per_element):
        raw_score = _ps5_roughness_score(data, width, height, bytes_per_element)
        return "inconclusive", raw_score, raw_score, raw_score, data, data
    if mask_x is None or mask_y is None:
        mask_x, mask_y = compute_ps5_swizzle_masks(width, height, bytes_per_element)
    raw_score = _ps5_roughness_score(data, width, height, bytes_per_element)
    unswizzled = ps5_unswizzle_bytes(
        data, width, height, bytes_per_element, mask_x=mask_x, mask_y=mask_y
    )
    swizzled = ps5_swizzle_bytes(
        data, width, height, bytes_per_element, mask_x=mask_x, mask_y=mask_y
    )
    unsw_score = _ps5_roughness_score(unswizzled, width, height, bytes_per_element)
    swz_score = _ps5_roughness_score(swizzled, width, height, bytes_per_element)

    if unsw_score < raw_score * 0.92 and unsw_score <= swz_score * 0.98:
        verdict = "likely_swizzled_input"
    elif raw_score <= unsw_score * 0.92 and raw_score <= swz_score * 0.92:
        verdict = "likely_linear_input"
    else:
        verdict = "inconclusive"

    return verdict, raw_score, unsw_score, swz_score, unswizzled, swizzled


def _ps5_unswizzle_best_variant(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    mask_x: int | None = None,
    mask_y: int | None = None,
    allow_axis_swap: bool = False,
    roughness_guard: bool = False,
) -> tuple[bytes, int, int, str, float]:
    """KR: bpe별 축-전치 규칙에 따라 unswizzle 후보를 선택합니다.
    KR: roughness_guard=True이면, unswizzle 결과가 원본보다 거칠 경우 원본을 반환합니다.
    EN: Pick the correct unswizzle variant based on per-bpe axis transposition rules.
    EN: When roughness_guard=True, returns raw data if unswizzle makes it rougher (already linear).

    Axis transposition depends on bpe:
      bpe=1 (Alpha8): always transpose → unswizzle at (H,W), rotate 90° to restore.
      bpe=4 (RGBA32): never transpose → unswizzle at (W,H) directly.
    """
    logical_bytes = width * height * bytes_per_element
    usable = data[: (len(data) // bytes_per_element) * bytes_per_element]
    clipped = usable[:logical_bytes]

    # KR: roughness guard를 위해 원본 roughness를 미리 계산합니다.
    # EN: Pre-compute raw roughness for the safety check.
    raw_score = (
        _ps5_roughness_score(clipped, width, height, bytes_per_element)
        if roughness_guard
        else None
    )

    # KR: bpe별 축 전치 규칙 결정.
    # EN: Determine whether this bpe uses axis transposition.
    should_transpose = _PS5_AXIS_TRANSPOSE.get(bytes_per_element, False)

    if (
        allow_axis_swap
        and should_transpose
        and mask_x is None
        and mask_y is None
        and _ps5_dimensions_supported(height, width, bytes_per_element)
    ):
        # KR: 전치 bpe (예: Alpha8): 종횡비와 무관하게 (H,W)로 unswizzle 후 회전 후보로 취급.
        # EN: Transposing bpe (e.g. Alpha8): always treat (H,W) unswizzle as the primary
        #     candidate regardless of aspect ratio (including square textures).
        try:
            swapped = ps5_unswizzle_bytes(
                clipped,
                height,
                width,
                bytes_per_element,
                mask_x=None,
                mask_y=None,
            )
            best_data = swapped
            best_width = height
            best_height = width
            best_variant = "swapped_axes"
            best_score = _ps5_roughness_score(
                swapped, height, width, bytes_per_element
            )
        except Exception:
            # Fallback to normal
            normal = ps5_unswizzle_bytes(
                clipped, width, height, bytes_per_element,
                mask_x=mask_x, mask_y=mask_y,
            )
            best_data = normal
            best_width = width
            best_height = height
            best_variant = "normal"
            best_score = _ps5_roughness_score(normal, width, height, bytes_per_element)
    else:
        # KR: 비전치 bpe (예: RGBA32) 또는 정사각형: (W,H)로 unswizzle.
        # EN: Non-transposing bpe (e.g. RGBA32) or square texture: unswizzle at (W,H).
        normal = ps5_unswizzle_bytes(
            clipped, width, height, bytes_per_element,
            mask_x=mask_x, mask_y=mask_y,
        )
        best_data = normal
        best_width = width
        best_height = height
        best_variant = "normal"
        best_score = _ps5_roughness_score(normal, width, height, bytes_per_element)

    # KR: 일부 RGBA/LA 텍스처는 addrlib 기반 4KB_S 경로가 더 정확합니다.
    # EN: Some RGBA/LA textures are better reconstructed by addrlib 4KB_S mapping.
    if (
        best_width == width
        and best_height == height
        and bytes_per_element in {2, 4}
    ):
        addrlib_candidate = _ps5_unswizzle_addrlib_uncompressed_candidate(
            usable, width, height, bytes_per_element
        )
        if addrlib_candidate is not None:
            addrlib_data, addrlib_score = addrlib_candidate
            if addrlib_score < (best_score * 0.98):
                best_data = addrlib_data
                best_width = width
                best_height = height
                best_variant = "addrlib_4KB_S"
                best_score = addrlib_score

    # KR: Roughness guard – unswizzle 결과가 원본보다 거칠면, 원본이 이미 linear입니다.
    # EN: Roughness guard – if unswizzle made data rougher, input is already linear.
    if roughness_guard and raw_score is not None and best_score >= raw_score * 0.92:
        return clipped, width, height, "already_linear", raw_score

    return best_data, best_width, best_height, best_variant, best_score


def detect_ps5_swizzle_state_from_image(
    image: Image.Image,
    mask_x: int | None = None,
    mask_y: int | None = None,
    rotate: int = PS5_SWIZZLE_ROTATE,
) -> tuple[str, float, float, float]:
    """KR: Pillow 이미지의 swizzle 상태를 판별합니다.
    EN: Detect swizzle state from a Pillow image.
    """
    prepared = _ps5_prepare_image(image)

    data = prepared.tobytes()
    bytes_per_element = len(prepared.getbands())
    verdict, raw_score, unsw_score, swz_score, _, _ = detect_ps5_swizzle_state(
        data,
        prepared.width,
        prepared.height,
        bytes_per_element,
        mask_x=mask_x,
        mask_y=mask_y,
    )
    return verdict, raw_score, unsw_score, swz_score


def apply_ps5_swizzle_to_image(
    image: Image.Image,
    mask_x: int | None = None,
    mask_y: int | None = None,
    rotate: int = PS5_SWIZZLE_ROTATE,
) -> Image.Image:
    """KR: 선형 이미지에 PS5 swizzle 변환을 적용합니다.
    EN: Apply PS5 swizzle transform to a linear image.
    """
    prepared = _ps5_prepare_image(image)
    bytes_per_element = len(prepared.getbands())
    if not _ps5_dimensions_supported(prepared.width, prepared.height, bytes_per_element):
        return prepared.copy()
    # KR: 전치 bpe (예: Alpha8)에만 역방향 회전을 적용합니다.
    # EN: Only apply inverse rotation for transposing bpe (e.g. Alpha8).
    should_transpose = _PS5_AXIS_TRANSPOSE.get(bytes_per_element, False)
    if should_transpose and rotate % 360 != 0:
        prepared = prepared.rotate((-rotate) % 360, expand=True)
    if not _ps5_dimensions_supported(prepared.width, prepared.height, bytes_per_element):
        return _ps5_prepare_image(image).copy()

    data = prepared.tobytes()
    swizzled = ps5_swizzle_bytes(
        data,
        prepared.width,
        prepared.height,
        bytes_per_element,
        mask_x=mask_x,
        mask_y=mask_y,
    )
    return Image.frombytes(prepared.mode, (prepared.width, prepared.height), swizzled)


def apply_ps5_unswizzle_to_image(
    image: Image.Image,
    mask_x: int | None = None,
    mask_y: int | None = None,
    rotate: int = PS5_SWIZZLE_ROTATE,
    allow_axis_swap: bool = False,
    roughness_guard: bool = False,
) -> Image.Image:
    """KR: swizzled 이미지에 PS5 unswizzle 변환을 적용합니다.
    KR: roughness_guard=True이면, 이미 linear인 입력은 변환하지 않습니다.
    EN: Apply PS5 unswizzle transform to a swizzled image.
    EN: When roughness_guard=True, skips unswizzle if input is already linear.
    """
    prepared = _ps5_prepare_image(image)
    bytes_per_element = len(prepared.getbands())
    if not _ps5_dimensions_supported(prepared.width, prepared.height, bytes_per_element):
        return prepared.copy()
    data = prepared.tobytes()
    unswizzled, out_width, out_height, variant, _ = _ps5_unswizzle_best_variant(
        data,
        prepared.width,
        prepared.height,
        bytes_per_element,
        mask_x=mask_x,
        mask_y=mask_y,
        allow_axis_swap=allow_axis_swap,
        roughness_guard=roughness_guard,
    )
    if variant == "already_linear":
        return prepared.copy()
    output = Image.frombytes(prepared.mode, (out_width, out_height), unswizzled)
    # KR: rotate는 축-스왑(전치) 된 경우에만 적용 (예: Alpha8).
    # EN: Only apply rotation when axes were swapped (transposing bpe).
    if variant == "swapped_axes" and rotate % 360 != 0:
        output = output.rotate(rotate % 360, expand=True)
    return output


def detect_texture_object_ps5_swizzle(
    texture_obj: Any,
    mask_x: int | None = None,
    mask_y: int | None = None,
    rotate: int = PS5_SWIZZLE_ROTATE,
) -> str | None:
    """KR: Texture2D 오브젝트의 swizzle 상태를 판별합니다.
    EN: Detect swizzle state for a Texture2D object.
    """
    verdict, _ = detect_texture_object_ps5_swizzle_detail(
        texture_obj,
        mask_x=mask_x,
        mask_y=mask_y,
        rotate=rotate,
    )
    return verdict


def detect_texture_object_ps5_swizzle_detail(
    texture_obj: Any,
    mask_x: int | None = None,
    mask_y: int | None = None,
    rotate: int = PS5_SWIZZLE_ROTATE,
) -> tuple[str | None, str | None]:
    """KR: Texture2D 오브젝트의 swizzle 상태를 판별합니다.
    KR: 반환값은 (판정값, 판정근거)입니다.
    EN: Detect swizzle state for a Texture2D object.
    EN: Returns (verdict, source).
    """
    try:
        texture = texture_obj.parse_as_object()
        width = int(getattr(texture, "m_Width", 0) or 0)
        height = int(getattr(texture, "m_Height", 0) or 0)
        stream_data = getattr(texture, "m_StreamData", None)
        try:
            stream_size = int(getattr(stream_data, "size", 0) or 0)
        except Exception:
            stream_size = 0
        is_readable = bool(getattr(texture, "m_IsReadable", False))
        try:
            texture_format = int(getattr(texture, "m_TextureFormat", -1) or -1)
        except Exception:
            texture_format = -1

        image_data = getattr(texture, "image_data", None)
        if isinstance(image_data, (bytes, bytearray)):
            image_data_len = len(image_data)
        else:
            image_data_len = 0

        # KR: 포맷/메타데이터 기반 공용 규칙:
        # KR:  - BC: stream+non-readable => swizzled, inline+readable => linear
        # KR:  - Crunched: UnityPy decode 경로 기준 linear 취급
        # KR:  - Uncompressed: stream/inline 메타 + bpe 일치 여부로 판정
        # EN: Format/metadata-based common rules:
        # EN:  - BC: stream+non-readable => swizzled, inline+readable => linear
        # EN:  - Crunched: treat as linear via UnityPy decode path
        # EN:  - Uncompressed: use stream/inline metadata + bpe consistency
        meta_hint: str | None = None
        meta_source: str | None = None
        if width > 0 and height > 0:
            if _texture_format_is_crunched(texture_format):
                return "likely_linear_input", "crunched-unitypy-decode"

            expected_alpha8_size = width * height
            if (
                texture_format == 1
                and stream_size > 0
                and not is_readable
                and stream_size == expected_alpha8_size
            ):
                meta_hint = "likely_swizzled_input"
                meta_source = "meta-alpha8-stream"
            elif (
                texture_format == 1
                and stream_size == 0
                and not is_readable
                and image_data_len == expected_alpha8_size
            ):
                meta_hint = "likely_swizzled_input"
                meta_source = "meta-alpha8-inline-nonread"
            elif stream_size > 0 and not is_readable:
                meta_hint = "likely_swizzled_input"
                meta_source = "meta-stream"
            elif stream_size == 0 and is_readable and image_data_len > 0:
                meta_hint = "likely_linear_input"
                meta_source = "meta-inline"

        # KR: 메타 기준이 확실하면 유사도보다 우선합니다.
        # EN: Prefer metadata verdict when it is available.
        if meta_hint is not None:
            return meta_hint, meta_source or "meta"

        if width > 0 and height > 0:
            raw_data: bytes | None = None
            get_image_data = getattr(texture, "get_image_data", None)
            if callable(get_image_data):
                try:
                    candidate = get_image_data()
                    if isinstance(candidate, (bytes, bytearray)):
                        raw_data = bytes(candidate)
                except Exception:
                    raw_data = None
            if raw_data is None:
                image_data = getattr(texture, "image_data", None)
                if isinstance(image_data, (bytes, bytearray)):
                    raw_data = bytes(image_data)

            if raw_data:
                if _texture_format_is_bc(texture_format):
                    # KR: BC 포맷은 descriptor 비트(타일모드/selector)가 핵심이며
                    # KR: 현재 자산 API에서 직접 노출되지 않으므로, 휴리스틱 점수 판별을 피합니다.
                    # EN: BC formats depend on descriptor bits (tile mode/selectors) not exposed
                    # EN: by current asset APIs; avoid roughness-based heuristics in this branch.
                    if stream_size > 0 and not is_readable:
                        return "likely_swizzled_input", "bc-meta-stream"
                    if stream_size == 0 and is_readable and image_data_len > 0:
                        return "likely_linear_input", "bc-meta-inline"
                    return "inconclusive", "bc-descriptor-unavailable"

                total_elements = width * height
                bytes_per_element: int | None = _texture_format_bytes_per_element(
                    texture_format
                )
                if (
                    bytes_per_element is None
                    and total_elements > 0
                    and (len(raw_data) % total_elements) == 0
                ):
                    derived = len(raw_data) // total_elements
                    if derived in {1, 2, 3, 4}:
                        bytes_per_element = int(derived)
                if bytes_per_element in {1, 2, 3, 4}:
                    expected_base = width * height * int(bytes_per_element)
                    if (
                        stream_size > 0
                        and not is_readable
                        and len(raw_data) >= expected_base
                    ):
                        return "likely_swizzled_input", "raw-meta-stream-bpe"
                    if stream_size == 0 and len(raw_data) >= expected_base:
                        return "likely_linear_input", "raw-meta-inline-bpe"

        image = getattr(texture, "image", None)
        if isinstance(image, Image.Image):
            verdict, _, _, _ = detect_ps5_swizzle_state_from_image(
                image,
                mask_x=mask_x,
                mask_y=mask_y,
                rotate=rotate,
            )
            return verdict, "image"
        return None, None
    except Exception:
        return None, None


def warn_unitypy_version(
    expected_major_minor: tuple[int, int] = (1, 24),
    lang: Language = "ko",
) -> None:
    """KR: UnityPy 버전을 점검하고 권장 버전과 다르면 경고합니다.
    EN: Check UnityPy version and print warning when it differs from recommendation.
    """
    version = getattr(UnityPy, "__version__", "")
    try:
        parts = version.split(".")
        major = int(parts[0])
        minor = int(parts[1])
    except (ValueError, IndexError, AttributeError):
        if lang == "ko":
            _log_console(f"[경고] UnityPy 버전을 확인할 수 없습니다: '{version}'")
        else:
            _log_console(f"[Warning] Could not determine UnityPy version: '{version}'")
        return

    if (major, minor) != expected_major_minor:
        expected = f"{expected_major_minor[0]}.{expected_major_minor[1]}.x"
        if lang == "ko":
            _log_console(
                f"[경고] 현재 UnityPy {version} 사용 중입니다. 권장 검증 버전은 {expected}입니다."
            )
        else:
            _log_console(
                f"[Warning] Using UnityPy {version}. Recommended validated version is {expected}."
            )


def build_replacement_lookup(
    replacements: dict[str, JsonDict],
) -> tuple[dict[tuple[str, str, str, int], str], set[str]]:
    """KR: 교체 JSON을 빠른 조회용 룩업 테이블로 변환합니다.
    EN: Build fast lookup structures from replacement JSON data.
    """
    lookup: dict[tuple[str, str, str, int], str] = {}
    files_to_process: set[str] = set()

    for info in replacements.values():
        replace_to = info.get("Replace_to")
        if not replace_to:
            continue

        file_name_raw = info.get("File")
        assets_name_raw = info.get("assets_name")
        path_id_raw = info.get("Path_ID")
        type_name_raw = info.get("Type")

        if not isinstance(file_name_raw, str) or not file_name_raw:
            continue
        if not isinstance(assets_name_raw, str) or not assets_name_raw:
            continue
        if not isinstance(type_name_raw, str) or not type_name_raw:
            continue
        if path_id_raw is None:
            continue

        try:
            path_id = int(path_id_raw)
        except (TypeError, ValueError):
            continue

        normalized_target = normalize_font_name(str(replace_to))
        lookup[(type_name_raw, file_name_raw, assets_name_raw, path_id)] = (
            normalized_target
        )
        files_to_process.add(file_name_raw)

    return lookup, files_to_process


def debug_parse_enabled() -> bool:
    """KR: 디버그 파싱 로그 활성화 여부를 반환합니다.
    EN: Return whether parse debug logging is enabled.
    """
    return os.environ.get("UFR_DEBUG_PARSE", "").strip() == "1"


def debug_parse_log(message: str) -> None:
    """KR: 디버그 모드일 때만 파싱 로그를 출력합니다.
    EN: Print parsing debug message only when enabled.
    """
    if debug_parse_enabled():
        _log_console(message)


def _log_scan_result_details(
    file_name: str, scanned: dict[str, list[JsonDict]]
) -> None:
    """KR: 스캔 결과를 파일/폰트 단위 DEBUG 로그로 남깁니다.
    EN: Emit file/font-level DEBUG logs for scan results.
    """
    ttf_entries = list(scanned.get("ttf", []))
    sdf_entries = list(scanned.get("sdf", []))
    _log_debug(
        f"[scan_debug] file={file_name} ttf_count={len(ttf_entries)} sdf_count={len(sdf_entries)}"
    )

    for font_entry in ttf_entries:
        assets_name = str(font_entry.get("assets_name", ""))
        font_name = str(font_entry.get("name", ""))
        path_id = font_entry.get("path_id")
        _log_debug(
            f"[scan_debug] type=TTF file={file_name} assets={assets_name} path_id={path_id} name={font_name}"
        )

    for font_entry in sdf_entries:
        assets_name = str(font_entry.get("assets_name", ""))
        font_name = str(font_entry.get("name", ""))
        path_id = font_entry.get("path_id")
        swizzle = font_entry.get("swizzle")
        swizzle_text = f" swizzle={swizzle}" if swizzle is not None else ""
        _log_debug(
            f"[scan_debug] type=SDF file={file_name} assets={assets_name} path_id={path_id} name={font_name}{swizzle_text}"
        )


def _log_replacement_plan_details(
    file_name: str,
    replacement_mapping: dict[str, JsonDict],
) -> None:
    """KR: 파일별 교체 계획을 DEBUG 로그로 기록합니다.
    EN: Emit file-level replacement plan as DEBUG logs.
    """
    if not replacement_mapping:
        _log_debug(f"[replace_plan] file={file_name} targets=0")
        return

    ttf_count = sum(
        1 for item in replacement_mapping.values() if item.get("Type") == "TTF"
    )
    sdf_count = sum(
        1 for item in replacement_mapping.values() if item.get("Type") == "SDF"
    )
    _log_debug(
        f"[replace_plan] file={file_name} targets={len(replacement_mapping)} ttf={ttf_count} sdf={sdf_count}"
    )

    for entry_key in sorted(replacement_mapping.keys()):
        entry = replacement_mapping[entry_key]
        type_name = str(entry.get("Type", ""))
        assets_name = str(entry.get("assets_name", ""))
        path_id = entry.get("Path_ID")
        source_name = str(entry.get("Name", ""))
        replace_to = str(entry.get("Replace_to", ""))
        swizzle = entry.get("swizzle")
        process_swizzle = entry.get("process_swizzle")
        extra_flags = ""
        if swizzle is not None or process_swizzle is not None:
            extra_flags = f" swizzle={swizzle} process_swizzle={process_swizzle}"
        _log_debug(
            f"[replace_plan] type={type_name} file={file_name} assets={assets_name} path_id={path_id} "
            f"name={source_name} replace_to={replace_to}{extra_flags}"
        )


def ensure_int(data: JsonDict | None, keys: Iterable[str]) -> None:
    """KR: 딕셔너리의 지정 키 값을 int로 강제 변환합니다.
    EN: Force-convert specified dictionary keys to integers.
    """
    if not data:
        return
    for key in keys:
        if key in data and data[key] is not None:
            data[key] = int(data[key])


@lru_cache(maxsize=256)
def _parse_unity_version_triplet(version_text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_text or "")
    if not match:
        return None
    try:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    except Exception:
        return None


@lru_cache(maxsize=1)
def _load_tmp_info_unity_field_index() -> dict[tuple[int, int, int], set[str]]:
    """KR: TMP_Info의 Unity 축 스냅샷에서 버전별 최상위 필드 인덱스를 로드합니다.
    EN: Load per-version top-level field index from TMP_Info unity snapshots.
    """
    try:
        path = os.path.join(
            get_script_dir(), "TMP_Info", "02_unity_version_changes.json"
        )
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        snapshots = obj.get("snapshots", []) if isinstance(obj, dict) else []
        index: dict[tuple[int, int, int], set[str]] = {}
        if not isinstance(snapshots, list):
            return {}
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                continue
            if not bool(snapshot.get("has_type", False)):
                continue
            version_text = str(snapshot.get("version", "") or "")
            triplet = _parse_unity_version_triplet(version_text)
            if triplet is None:
                continue
            declared_fields = snapshot.get("declared_fields", [])
            if not isinstance(declared_fields, list):
                continue
            fields: set[str] = set()
            for field in declared_fields:
                if isinstance(field, str) and field:
                    fields.add(field)
                elif isinstance(field, dict):
                    name = field.get("name")
                    if isinstance(name, str) and name:
                        fields.add(name)
            if fields:
                index[triplet] = fields
        return index
    except Exception:
        return {}


@lru_cache(maxsize=256)
def _get_tmp_info_fields_for_unity(unity_version: str | None) -> set[str]:
    """KR: Unity 버전에 가장 가까운 TMP_Info 스냅샷 필드 집합을 반환합니다.
    EN: Return TMP_Info field set from the nearest Unity version snapshot.
    """
    if not unity_version:
        return set()
    triplet = _parse_unity_version_triplet(str(unity_version))
    if triplet is None:
        return set()
    index = _load_tmp_info_unity_field_index()
    if not index:
        return set()
    if triplet in index:
        return set(index[triplet])
    lower_or_equal = [key for key in index.keys() if key <= triplet]
    if lower_or_equal:
        return set(index[max(lower_or_equal)])
    return set(index[min(index.keys())])


def _resolve_creation_settings_key(
    data: JsonDict, unity_version: str | None = None
) -> str | None:
    """KR: 타겟 딕셔너리에서 creation settings 키를 판별합니다.
    EN: Resolve creation-settings key from target dict.
    """
    for key in _TMP_CREATION_SETTINGS_KEYS:
        if isinstance(data.get(key), dict):
            return key
    expected_fields = _get_tmp_info_fields_for_unity(unity_version)
    for key in _TMP_CREATION_SETTINGS_KEYS:
        if key in expected_fields and key in data and isinstance(data.get(key), dict):
            return key
    return None


def _sync_creation_settings_payload(
    creation_settings: JsonDict,
    atlas_width: int,
    atlas_height: int,
    padding: int,
    point_size: int,
) -> None:
    """KR: creation settings 내부 키 패턴을 감지해 atlas/pointSize를 동기화합니다.
    EN: Detect key patterns in creation settings and sync atlas/pointSize values.
    """
    for key in list(creation_settings.keys()):
        normalized = key.replace("_", "").lower()
        if "atlaswidth" in normalized:
            creation_settings[key] = int(atlas_width)
        elif "atlasheight" in normalized:
            creation_settings[key] = int(atlas_height)
        elif normalized.endswith("padding") or normalized == "padding":
            creation_settings[key] = int(padding)
        elif normalized.endswith("pointsize") or normalized == "pointsize":
            creation_settings[key] = int(point_size)
        elif "charactersequence" in normalized:
            creation_settings[key] = ""


def _tmp_version_hint(unity_version: str | None) -> Literal["new", "old"] | None:
    if not unity_version:
        return None
    triplet = _parse_unity_version_triplet(str(unity_version))
    if triplet is None:
        return None
    if triplet <= _TMP_OLD_ONLY_LAST:
        return "old"
    if triplet >= _TMP_NEW_SCHEMA_FIRST:
        return "new"
    return None


def _safe_list_len(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _first_atlas_ref(value: Any) -> JsonDict | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if isinstance(item, dict):
            return cast(JsonDict, item)
    return None


def _atlas_ref_ids(ref: Any) -> tuple[int, int]:
    if not isinstance(ref, dict):
        return 0, 0
    try:
        file_id = int(ref.get("m_FileID", 0) or 0)
    except Exception:
        file_id = 0
    try:
        path_id = int(ref.get("m_PathID", 0) or 0)
    except Exception:
        path_id = 0
    return file_id, path_id


def _has_real_atlas_path(ref: Any) -> bool:
    _, path_id = _atlas_ref_ids(ref)
    return path_id > 0


def _first_valid_atlas_ref(value: Any) -> JsonDict | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if isinstance(item, dict) and _has_real_atlas_path(item):
            return cast(JsonDict, item)
    return None


def _best_atlas_ref(
    data: JsonDict,
    *,
    prefer_new: bool,
) -> JsonDict | None:
    new_any = _first_atlas_ref(data.get("m_AtlasTextures"))
    new_valid = _first_valid_atlas_ref(data.get("m_AtlasTextures"))
    old_any = (
        cast(JsonDict | None, data.get("atlas"))
        if isinstance(data.get("atlas"), dict)
        else None
    )
    old_valid = old_any if _has_real_atlas_path(old_any) else None

    ordered = (
        (new_valid, old_valid, new_any, old_any)
        if prefer_new
        else (old_valid, new_valid, old_any, new_any)
    )
    for ref in ordered:
        if isinstance(ref, dict):
            return ref
    return None


def detect_tmp_version(
    data: JsonDict, unity_version: str | None = None
) -> Literal["new", "old"]:
    """KR: SDF TMP 데이터가 신형/구형 포맷인지 판별합니다.
    EN: Detect whether SDF TMP data uses new or old schema.
    """
    new_glyph_count = _safe_list_len(data.get("m_GlyphTable"))
    old_glyph_count = _safe_list_len(data.get("m_glyphInfoList"))
    has_new_glyphs = new_glyph_count > 0
    has_old_glyphs = old_glyph_count > 0

    has_new_face = isinstance(data.get("m_FaceInfo"), dict)
    has_old_face = isinstance(data.get("m_fontInfo"), dict)
    has_new_atlas = _first_atlas_ref(data.get("m_AtlasTextures")) is not None
    has_old_atlas = isinstance(data.get("atlas"), dict)

    # KR: 두 포맷 키가 동시에 있어도 실제 글리프가 있는 쪽을 우선합니다.
    # EN: When both schema keys exist, prefer the side that has real glyph data.
    if has_new_glyphs != has_old_glyphs:
        return "new" if has_new_glyphs else "old"
    if new_glyph_count != old_glyph_count:
        return "new" if new_glyph_count > old_glyph_count else "old"

    # KR: 글리프가 비슷하면 face/atlas 신호를 비교합니다.
    # EN: When glyph evidence is ambiguous, compare face/atlas signals.
    if has_new_face != has_old_face:
        return "new" if has_new_face else "old"
    if has_new_atlas != has_old_atlas:
        return "new" if has_new_atlas else "old"

    # KR: Unity-Runtime-Libraries 기준 버전 힌트(2018.3.14 / 2018.4.2)를 사용합니다.
    # EN: Use Unity-Runtime-Libraries version boundaries (2018.3.14 / 2018.4.2).
    hint = _tmp_version_hint(unity_version)
    if hint is not None:
        return hint

    # KR: 최종 폴백은 신형 우선입니다.
    # EN: Final fallback prefers new schema.
    if has_new_face or has_new_atlas or "m_CharacterTable" in data:
        return "new"
    if has_old_face or has_old_atlas:
        return "old"

    return "new"


def inspect_tmp_font_schema(
    data: JsonDict,
    unity_version: str | None = None,
) -> dict[str, Any]:
    """KR: TMP 스키마 판별과 glyph/atlas 핵심 메타를 공통 형태로 반환합니다.
    EN: Return unified TMP schema classification and glyph/atlas metadata.
    """
    target_version = detect_tmp_version(data, unity_version=unity_version)

    new_glyph_count = _safe_list_len(data.get("m_GlyphTable"))
    old_glyph_count = _safe_list_len(data.get("m_glyphInfoList"))
    has_new_face = isinstance(data.get("m_FaceInfo"), dict)
    has_old_face = isinstance(data.get("m_fontInfo"), dict)
    new_atlas_ref = _first_atlas_ref(data.get("m_AtlasTextures"))
    old_atlas_ref = (
        cast(JsonDict | None, data.get("atlas"))
        if isinstance(data.get("atlas"), dict)
        else None
    )

    if target_version == "new":
        glyph_count = new_glyph_count if new_glyph_count > 0 else old_glyph_count
        atlas_ref = _best_atlas_ref(data, prefer_new=True)
    else:
        glyph_count = old_glyph_count if old_glyph_count > 0 else new_glyph_count
        atlas_ref = _best_atlas_ref(data, prefer_new=False)

    atlas_file_id, atlas_path_id = _atlas_ref_ids(atlas_ref)

    is_tmp = bool(
        new_glyph_count > 0
        or old_glyph_count > 0
        or has_new_face
        or has_old_face
        or new_atlas_ref is not None
        or old_atlas_ref is not None
    )

    return {
        "version": target_version,
        "is_tmp": is_tmp,
        "glyph_count": int(glyph_count),
        "atlas_file_id": int(atlas_file_id),
        "atlas_path_id": int(atlas_path_id),
    }


def convert_face_info_new_to_old(
    face_info: JsonDict,
    atlas_padding: int = 0,
    atlas_width: int = 0,
    atlas_height: int = 0,
) -> JsonDict:
    """KR: 신형 m_FaceInfo를 구형 m_fontInfo 구조로 변환합니다.
    EN: Convert new m_FaceInfo to old m_fontInfo schema.
    """
    return {
        "Name": face_info.get("m_FamilyName", ""),
        "PointSize": face_info.get("m_PointSize", 0),
        "Scale": face_info.get("m_Scale", 1.0),
        "CharacterCount": 0,
        "LineHeight": face_info.get("m_LineHeight", 0),
        "Baseline": face_info.get("m_Baseline", 0),
        "Ascender": face_info.get("m_AscentLine", 0),
        "CapHeight": face_info.get("m_CapLine", 0),
        "Descender": face_info.get("m_DescentLine", 0),
        "CenterLine": face_info.get("m_MeanLine", 0),
        "SuperscriptOffset": face_info.get("m_SuperscriptOffset", 0),
        "SubscriptOffset": face_info.get("m_SubscriptOffset", 0),
        "SubSize": face_info.get("m_SubscriptSize", 0.5),
        "Underline": face_info.get("m_UnderlineOffset", 0),
        "UnderlineThickness": face_info.get("m_UnderlineThickness", 0),
        "strikethrough": face_info.get("m_StrikethroughOffset", 0),
        "strikethroughThickness": face_info.get("m_StrikethroughThickness", 0),
        "TabWidth": face_info.get("m_TabWidth", 0),
        "Padding": atlas_padding,
        "AtlasWidth": atlas_width,
        "AtlasHeight": atlas_height,
    }


def convert_face_info_old_to_new(font_info: JsonDict) -> JsonDict:
    """KR: 구형 m_fontInfo를 신형 m_FaceInfo 구조로 변환합니다.
    EN: Convert old m_fontInfo to new m_FaceInfo schema.
    """
    return {
        "m_FaceIndex": 0,
        "m_FamilyName": font_info.get("Name", ""),
        "m_StyleName": "regular",
        "m_PointSize": font_info.get("PointSize", 0),
        "m_Scale": font_info.get("Scale", 1.0),
        "m_UnitsPerEM": 0,
        "m_LineHeight": font_info.get("LineHeight", 0),
        "m_AscentLine": font_info.get("Ascender", 0),
        "m_CapLine": font_info.get("CapHeight", 0),
        "m_MeanLine": font_info.get("CenterLine", 0),
        "m_Baseline": font_info.get("Baseline", 0),
        "m_DescentLine": font_info.get("Descender", 0),
        "m_SuperscriptOffset": font_info.get("SuperscriptOffset", 0),
        "m_SuperscriptSize": 0.5,
        "m_SubscriptOffset": font_info.get("SubscriptOffset", 0),
        "m_SubscriptSize": font_info.get("SubSize", 0.5),
        "m_UnderlineOffset": font_info.get("Underline", 0),
        "m_UnderlineThickness": font_info.get("UnderlineThickness", 0),
        "m_StrikethroughOffset": font_info.get("strikethrough", 0),
        "m_StrikethroughThickness": font_info.get("strikethroughThickness", 0),
        "m_TabWidth": font_info.get("TabWidth", 0),
    }


def _new_glyph_rect_to_int(rect: JsonDict) -> tuple[int, int, int, int]:
    """KR: 신형 TMP glyph rect를 정수 좌표/크기로 정규화합니다.
    EN: Normalize new TMP glyph rect to integer coordinates/sizes.
    """
    x = int(round(float(rect.get("m_X", 0))))
    y = int(round(float(rect.get("m_Y", 0))))
    w = max(1, int(round(float(rect.get("m_Width", 0)))))
    h = max(1, int(round(float(rect.get("m_Height", 0)))))
    return x, y, w, h


def _tmp_flip_y_between_old_new(
    y_value: float, glyph_height: float, atlas_height: int | float | None
) -> float:
    """KR: TMP old(top-origin) <-> new(bottom-origin) Y 변환 공식을 적용합니다.
    EN: Apply TMP old(top-origin) <-> new(bottom-origin) Y conversion formula.
    """
    if atlas_height is None:
        return float(y_value)
    try:
        atlas_h = float(atlas_height)
    except Exception:
        return float(y_value)
    if atlas_h <= 0:
        return float(y_value)
    return atlas_h - float(y_value) - float(glyph_height)


def convert_glyphs_new_to_old(
    glyph_table: list[JsonDict],
    char_table: list[JsonDict],
    atlas_height: int | None = None,
) -> list[JsonDict]:
    """KR: 신형 글리프/문자 테이블을 구형 m_glyphInfoList로 변환합니다.
    EN: Convert new glyph/character tables into old m_glyphInfoList.
    """
    glyph_by_index: dict[int, JsonDict] = {}
    for g in glyph_table:
        glyph_by_index[int(g.get("m_Index", 0))] = g
    result: list[JsonDict] = []
    for char in char_table:
        unicode_val = char.get("m_Unicode", 0)
        glyph_idx = char.get("m_GlyphIndex", 0)
        g = glyph_by_index.get(glyph_idx, {})
        metrics = g.get("m_Metrics", {})
        rect = g.get("m_GlyphRect", {})
        rect_h = float(rect.get("m_Height", 0))
        rect_y = _tmp_flip_y_between_old_new(
            float(rect.get("m_Y", 0)),
            rect_h,
            atlas_height,
        )
        result.append(
            {
                "id": int(unicode_val),
                "x": float(rect.get("m_X", 0)),
                "y": rect_y,
                "width": float(metrics.get("m_Width", 0)),
                "height": float(metrics.get("m_Height", 0)),
                "xOffset": float(metrics.get("m_HorizontalBearingX", 0)),
                "yOffset": float(metrics.get("m_HorizontalBearingY", 0)),
                "xAdvance": float(metrics.get("m_HorizontalAdvance", 0)),
                "scale": float(g.get("m_Scale", 1.0)),
            }
        )
    return result


def convert_glyphs_old_to_new(
    glyph_info_list: list[JsonDict],
    atlas_height: int | None = None,
) -> tuple[list[JsonDict], list[JsonDict]]:
    """KR: 구형 m_glyphInfoList를 신형 테이블 구조로 변환합니다.
    EN: Convert old m_glyphInfoList into new glyph/character tables.
    """
    glyph_table: list[JsonDict] = []
    char_table: list[JsonDict] = []
    glyph_idx = 0
    for glyph in glyph_info_list:
        uid = glyph.get("id", 0)
        old_rect_y = float(glyph.get("y", 0))
        glyph_h = float(glyph.get("height", 0))
        new_rect_y = _tmp_flip_y_between_old_new(old_rect_y, glyph_h, atlas_height)
        glyph_table.append(
            {
                "m_Index": glyph_idx,
                "m_Metrics": {
                    "m_Width": glyph.get("width", 0),
                    "m_Height": glyph.get("height", 0),
                    "m_HorizontalBearingX": glyph.get("xOffset", 0),
                    "m_HorizontalBearingY": glyph.get("yOffset", 0),
                    "m_HorizontalAdvance": glyph.get("xAdvance", 0),
                },
                "m_GlyphRect": {
                    "m_X": int(glyph.get("x", 0)),
                    "m_Y": int(round(new_rect_y)),
                    "m_Width": int(glyph.get("width", 0)),
                    "m_Height": int(glyph.get("height", 0)),
                },
                "m_Scale": glyph.get("scale", 1.0),
                "m_AtlasIndex": 0,
                "m_ClassDefinitionType": 0,
            }
        )
        char_table.append(
            {
                "m_ElementType": 1,
                "m_Unicode": int(uid),
                "m_GlyphIndex": glyph_idx,
                "m_Scale": 1.0,
            }
        )
        glyph_idx += 1
    return glyph_table, char_table


def normalize_sdf_data(data: JsonDict, deep_copy: bool = True) -> JsonDict:
    """KR: SDF 교체 데이터를 신형 TMP 형식으로 정규화해 반환합니다.
    KR: deep_copy=True면 입력 데이터를 복사해 원본 변형을 방지합니다.
    EN: Normalize SDF replacement data into the new TMP schema.
    EN: With deep_copy=True, clone input data to avoid mutating the original.
    """
    import copy

    result: JsonDict = copy.deepcopy(data) if deep_copy else data
    version = detect_tmp_version(result)

    if version == "old":
        font_info = result.get("m_fontInfo", {})
        glyph_info_list = result.get("m_glyphInfoList", [])
        atlas_padding = font_info.get("Padding", 0)
        atlas_width = font_info.get("AtlasWidth", 0)
        atlas_height = font_info.get("AtlasHeight", 0)

        # KR: 구형 face/glyph 구조를 신형 TMP 필드로 승격합니다.
        # EN: Upgrade old face/glyph structures to new TMP fields.
        result["m_FaceInfo"] = convert_face_info_old_to_new(font_info)

        try:
            atlas_height_int = int(atlas_height) if atlas_height is not None else None
        except Exception:
            atlas_height_int = None
        glyph_table, char_table = convert_glyphs_old_to_new(
            glyph_info_list,
            atlas_height=atlas_height_int,
        )
        result["m_GlyphTable"] = glyph_table
        result["m_CharacterTable"] = char_table

        # KR: 구형 atlas 참조를 신형 atlas 배열 필드로 보정합니다.
        # EN: Normalize old atlas reference into new atlas-list field.
        if "m_AtlasTextures" not in result or not result["m_AtlasTextures"]:
            atlas_ref = result.get("atlas", {"m_FileID": 0, "m_PathID": 0})
            result["m_AtlasTextures"] = [atlas_ref]
        result.setdefault("m_AtlasWidth", int(atlas_width))
        result.setdefault("m_AtlasHeight", int(atlas_height))
        result.setdefault("m_AtlasPadding", int(atlas_padding))
        result.setdefault("m_AtlasRenderMode", 4118)
        result.setdefault("m_UsedGlyphRects", [])
        result.setdefault("m_FreeGlyphRects", [])

        # KR: 구형 데이터에 누락된 weight table은 기본값으로 채웁니다.
        # EN: Fill missing weight table in old data with a safe default.
        if "m_FontWeightTable" not in result:
            font_weights = result.get("fontWeights", [])
            result["m_FontWeightTable"] = font_weights if font_weights else []

    # KR: 정규화 후 반복 사용을 위해 숫자 타입/기본값을 한 번만 정리합니다.
    # EN: Canonicalize numeric fields/defaults once for repeated reuse.
    try:
        result["m_AtlasWidth"] = int(result.get("m_AtlasWidth", 0) or 0)
        result["m_AtlasHeight"] = int(result.get("m_AtlasHeight", 0) or 0)
        result["m_AtlasPadding"] = int(result.get("m_AtlasPadding", 0) or 0)
    except Exception:
        pass
    result.setdefault("m_AtlasRenderMode", 4118)
    result.setdefault("m_UsedGlyphRects", [])
    result.setdefault("m_FreeGlyphRects", [])
    result.setdefault("m_FontWeightTable", [])

    face_info = result.get("m_FaceInfo")
    if isinstance(face_info, dict):
        ensure_int(face_info, ["m_PointSize", "m_AtlasWidth", "m_AtlasHeight"])

    # KR: Atlas 참조 목록은 공유 변형을 피하기 위해 독립 딕셔너리로 재구성합니다.
    # EN: Rebuild atlas references as standalone dicts to avoid shared mutations.
    atlas_textures_raw = result.get("m_AtlasTextures", [])
    atlas_textures: list[JsonDict] = []
    if isinstance(atlas_textures_raw, list):
        for tex in atlas_textures_raw:
            if isinstance(tex, dict):
                atlas_textures.append(
                    {
                        "m_FileID": int(tex.get("m_FileID", 0) or 0),
                        "m_PathID": int(tex.get("m_PathID", 0) or 0),
                    }
                )
    if not atlas_textures and isinstance(result.get("atlas"), dict):
        atlas_ref = cast(JsonDict, result.get("atlas"))
        atlas_textures.append(
            {
                "m_FileID": int(atlas_ref.get("m_FileID", 0) or 0),
                "m_PathID": int(atlas_ref.get("m_PathID", 0) or 0),
            }
        )
    result["m_AtlasTextures"] = atlas_textures

    glyph_table = result.get("m_GlyphTable")
    if isinstance(glyph_table, list):
        for glyph in glyph_table:
            if not isinstance(glyph, dict):
                continue
            ensure_int(glyph, ["m_Index", "m_AtlasIndex", "m_ClassDefinitionType"])
            glyph["m_ClassDefinitionType"] = 0
            rect = glyph.get("m_GlyphRect")
            if isinstance(rect, dict):
                ensure_int(rect, ["m_X", "m_Y", "m_Width", "m_Height"])

    char_table = result.get("m_CharacterTable")
    if isinstance(char_table, list):
        for char in char_table:
            if isinstance(char, dict):
                ensure_int(char, ["m_Unicode", "m_GlyphIndex", "m_ElementType"])

    for rect_list_name in ["m_UsedGlyphRects", "m_FreeGlyphRects"]:
        rect_list = result.get(rect_list_name)
        if isinstance(rect_list, list):
            for rect in rect_list:
                if isinstance(rect, dict):
                    ensure_int(rect, ["m_X", "m_Y", "m_Width", "m_Height"])

    creation_settings = result.get("m_CreationSettings")
    if isinstance(creation_settings, dict):
        ensure_int(
            creation_settings, ["pointSize", "atlasWidth", "atlasHeight", "padding"]
        )

    return result


def find_assets_files(
    game_path: str,
    lang: Language = "ko",
    target_files: set[str] | None = None,
) -> list[str]:
    """KR: 게임에서 처리 대상 에셋 파일 목록을 수집합니다.
    KR: target_files가 있으면 해당 파일명으로 스캔 대상을 제한합니다.
    EN: Collect candidate asset files from the game.
    EN: If target_files is provided, limit candidates to those basenames.
    """
    data_path = get_data_path(game_path, lang=lang)
    assets_files: list[str] = []
    normalized_targets = (
        {os.path.basename(name) for name in target_files} if target_files else None
    )
    blacklist_exts = {
        ".dll",
        ".manifest",
        ".exe",
        ".txt",
        ".json",
        ".xml",
        ".log",
        ".ini",
        ".cfg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".wav",
        ".mp3",
        ".ogg",
        ".mp4",
        ".avi",
        ".mov",
        ".bak",
        ".info",
        ".config",
    }

    for root, _, files in os.walk(data_path):
        for fn in files:
            if normalized_targets is not None and fn not in normalized_targets:
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext in blacklist_exts:
                continue
            assets_files.append(os.path.join(root, fn))
    assets_files.sort()
    return assets_files


def get_compile_method(datapath: str) -> str:
    """KR: 데이터 폴더의 컴파일 방식을 Mono/Il2cpp로 판별합니다.
    EN: Detect compile method as Mono or Il2cpp.
    """
    if "Managed" in os.listdir(datapath):
        return "Mono"
    else:
        return "Il2cpp"


def _create_generator(
    unity_version: str,
    game_path: str,
    data_path: str,
    compile_method: str,
    lang: Language = "ko",
) -> TypeTreeGenerator:
    """KR: 타입트리 생성기를 구성하고 Mono/Il2cpp 메타데이터를 로드합니다.
    EN: Build typetree generator and load Mono/Il2cpp metadata.
    """
    generator = TypeTreeGenerator(unity_version)
    if compile_method == "Mono":
        managed_dir = os.path.join(data_path, "Managed")
        for fn in os.listdir(managed_dir):
            if not fn.endswith(".dll"):
                continue
            try:
                with open(os.path.join(managed_dir, fn), "rb") as f:
                    generator.load_dll(f.read())
            except Exception as e:
                if lang == "ko":
                    _log_console(f"[generator] DLL 로드 실패: {fn} ({e})")
                else:
                    _log_console(f"[generator] Failed to load DLL: {fn} ({e})")
    else:
        il2cpp_path = os.path.join(game_path, "GameAssembly.dll")
        with open(il2cpp_path, "rb") as f:
            il2cpp = f.read()
        metadata_path = os.path.join(
            data_path, "il2cpp_data", "Metadata", "global-metadata.dat"
        )
        with open(metadata_path, "rb") as f:
            metadata = f.read()
        generator.load_il2cpp(il2cpp, metadata)
    return generator


def _scan_fonts_from_env(
    env: Any,
    file_name: str,
    lang: Language = "ko",
    detect_ps5_swizzle: bool = False,
) -> dict[str, list[JsonDict]]:
    """KR: 로드된 UnityPy env에서 TTF/SDF 폰트 정보를 추출합니다.
    EN: Extract TTF/SDF font entries from a loaded UnityPy env.
    """
    scanned: dict[str, list[JsonDict]] = {"ttf": [], "sdf": []}
    texture_lookup: dict[tuple[str, int], Any] = {}
    texture_swizzle_cache: dict[str, str | None] = {}
    if detect_ps5_swizzle:
        for item in env.objects:
            if item.type.name != "Texture2D":
                continue
            texture_lookup[(item.assets_file.name, int(item.path_id))] = item

    for obj in env.objects:
        try:
            if obj.type.name == "Font":
                font_name = obj.peek_name()
                if not font_name:
                    try:
                        font = obj.parse_as_object()
                        font_name = getattr(font, "m_Name", "") or ""
                    except Exception:
                        font_name = ""
                scanned["ttf"].append(
                    {
                        "file": file_name,
                        "assets_name": obj.assets_file.name,
                        "name": font_name,
                        "path_id": obj.path_id,
                    }
                )
            elif obj.type.name == "MonoBehaviour":
                parse_dict = None
                atlas_file_id = 0
                atlas_path_id = 0
                glyph_count = 0
                try:
                    parse_dict = obj.parse_as_dict()
                    unity_version_hint = getattr(obj.assets_file, "unity_version", None)
                    tmp_info = inspect_tmp_font_schema(
                        parse_dict,
                        unity_version=(
                            str(unity_version_hint) if unity_version_hint else None
                        ),
                    )
                except Exception:
                    if lang == "ko":
                        debug_parse_log(
                            f"[scan_fonts] parse_as_dict 실패: {file_name} | PathID {obj.path_id}"
                        )
                    else:
                        debug_parse_log(
                            f"[scan_fonts] parse_as_dict failed: {file_name} | PathID {obj.path_id}"
                        )
                    continue

                if not tmp_info.get("is_tmp"):
                    continue

                try:
                    if parse_dict is None:
                        parse_dict = obj.parse_as_dict()
                    glyph_count = int(tmp_info.get("glyph_count", 0) or 0)
                    atlas_file_id = int(tmp_info.get("atlas_file_id", 0) or 0)
                    atlas_path_id = int(tmp_info.get("atlas_path_id", 0) or 0)
                    # KR: 외부 참조 stub(FileID!=0, PathID=0)은 실제 교체 대상이 아닙니다.
                    # EN: External stubs (FileID!=0, PathID=0) are not valid replacement targets.
                    if atlas_file_id != 0 and atlas_path_id == 0:
                        continue
                    if glyph_count == 0:
                        continue
                except Exception:
                    if lang == "ko":
                        debug_parse_log(
                            f"[scan_fonts] SDF 필드 검사 실패: {file_name} | PathID {obj.path_id}"
                        )
                    else:
                        debug_parse_log(
                            f"[scan_fonts] SDF field check failed: {file_name} | PathID {obj.path_id}"
                        )
                    continue

                sdf_info: JsonDict = {
                    "file": file_name,
                    "assets_name": obj.assets_file.name,
                    "name": obj.peek_name(),
                    "path_id": obj.path_id,
                }
                if detect_ps5_swizzle:
                    swizzle_state = False
                    if atlas_file_id == 0 and atlas_path_id != 0:
                        cache_key = f"{obj.assets_file.name}|{atlas_path_id}"
                        if cache_key in texture_swizzle_cache:
                            swizzle_verdict = texture_swizzle_cache[cache_key]
                        else:
                            texture_obj = texture_lookup.get(
                                (obj.assets_file.name, atlas_path_id)
                            )
                            swizzle_verdict = (
                                detect_texture_object_ps5_swizzle(texture_obj)
                                if texture_obj is not None
                                else None
                            )
                            texture_swizzle_cache[cache_key] = swizzle_verdict
                        swizzle_state = swizzle_verdict == "likely_swizzled_input"
                    sdf_info["swizzle"] = "True" if swizzle_state else "False"

                scanned["sdf"].append(sdf_info)
        except Exception as e:
            if lang == "ko":
                _log_console(
                    f"[scan_fonts] 오브젝트 처리 실패: {file_name} | PathID {obj.path_id} ({e})"
                )
            else:
                _log_console(
                    f"[scan_fonts] Object processing failed: {file_name} | PathID {obj.path_id} ({e})"
                )
            continue

    return scanned


def _scan_fonts_in_asset_file(
    assets_file: str,
    generator: TypeTreeGenerator,
    lang: Language = "ko",
    detect_ps5_swizzle: bool = False,
) -> tuple[dict[str, list[JsonDict]], str | None]:
    """KR: 단일 에셋 파일을 로드해 폰트 정보를 추출합니다.
    EN: Load one asset file and extract font entries.
    """
    file_name = os.path.basename(assets_file)
    scanned: dict[str, list[JsonDict]] = {"ttf": [], "sdf": []}

    env = None
    try:
        env = UnityPy.load(assets_file)
        env.typetree_generator = generator
    except Exception as e:
        if lang == "ko":
            return scanned, f"UnityPy.load 실패: {assets_file} ({e})"
        return scanned, f"UnityPy.load failed: {assets_file} ({e})"

    try:
        scanned = _scan_fonts_from_env(
            env, file_name, lang=lang, detect_ps5_swizzle=detect_ps5_swizzle
        )
    finally:
        close_unitypy_env(env)
        env = None
        gc.collect()

    return scanned, None


def _scan_fonts_via_worker(
    game_path: str,
    assets_file: str,
    lang: Language = "ko",
    detect_ps5_swizzle: bool = False,
) -> tuple[dict[str, list[JsonDict]], str | None]:
    """KR: 파일 단위 서브프로세스 워커로 스캔해 크래시를 격리합니다.
    EN: Scan using a per-file subprocess worker to isolate hard crashes.
    """
    fd, output_path = tempfile.mkstemp(prefix="scan_worker_", suffix=".json")
    os.close(fd)
    worker_exit_hints = {
        -1073741819: "ACCESS_VIOLATION(0xC0000005)",
        3221225477: "ACCESS_VIOLATION(0xC0000005)",
    }
    try:
        if getattr(sys, "frozen", False):
            cmd = [
                sys.executable,
                "--gamepath",
                game_path,
                "--_scan-file-worker",
                assets_file,
                "--_scan-file-worker-output",
                output_path,
            ]
        else:
            cmd = [
                sys.executable,
                os.path.abspath(__file__),
                "--gamepath",
                game_path,
                "--_scan-file-worker",
                assets_file,
                "--_scan-file-worker-output",
                output_path,
            ]
        if detect_ps5_swizzle:
            cmd.append("--ps5-swizzle")

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            hint = worker_exit_hints.get(int(proc.returncode))
            hint_text = f" [{hint}]" if hint else ""
            if lang == "ko":
                return {
                    "ttf": [],
                    "sdf": [],
                }, f"scan worker 실패 (exit={proc.returncode}{hint_text}): {detail}"
            return {
                "ttf": [],
                "sdf": [],
            }, f"scan worker failed (exit={proc.returncode}{hint_text}): {detail}"

        if not os.path.exists(output_path):
            if lang == "ko":
                return {"ttf": [], "sdf": []}, "scan worker 결과 파일이 없습니다."
            return {"ttf": [], "sdf": []}, "scan worker output file is missing."

        with open(output_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        scanned = {
            "ttf": list(payload.get("ttf", [])) if isinstance(payload, dict) else [],
            "sdf": list(payload.get("sdf", [])) if isinstance(payload, dict) else [],
        }
        worker_error = None
        if isinstance(payload, dict):
            worker_error = payload.get("error")
            if not isinstance(worker_error, str):
                worker_error = None
        return scanned, worker_error
    except Exception as e:
        if lang == "ko":
            return {"ttf": [], "sdf": []}, f"scan worker 실행 실패: {e!r}"
        return {"ttf": [], "sdf": []}, f"failed to run scan worker: {e!r}"
    finally:
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            pass


def scan_fonts(
    game_path: str,
    lang: Language = "ko",
    target_files: set[str] | None = None,
    isolate_files: bool = True,
    scan_jobs: int = 1,
    ps5_swizzle: bool = False,
) -> dict[str, list[JsonDict]]:
    """KR: 게임 에셋을 스캔해 TTF/SDF 폰트 목록을 반환합니다.
    KR: target_files가 있으면 해당 파일만 스캔합니다.
    KR: isolate_files=True면 파일 단위 워커 프로세스로 스캔해 크래시를 격리합니다.
    KR: scan_jobs>1이면 isolate_files 경로에서 워커를 병렬 실행합니다.
    EN: Scan game assets and return TTF/SDF font entries.
    EN: If target_files is provided, only scan those files.
    EN: If isolate_files=True, scan each file via worker subprocess to isolate hard crashes.
    EN: If scan_jobs>1, worker subprocesses are executed in parallel for isolate_files mode.
    """
    data_path = get_data_path(game_path, lang=lang)
    unity_version = get_unity_version(game_path, lang=lang)
    assets_files = find_assets_files(game_path, lang=lang, target_files=target_files)
    compile_method = get_compile_method(data_path)
    generator = _create_generator(
        unity_version, game_path, data_path, compile_method, lang=lang
    )

    fonts: dict[str, list[JsonDict]] = {
        "ttf": [],
        "sdf": [],
    }

    total_files = len(assets_files)
    try:
        scan_jobs = int(scan_jobs)
    except Exception:
        scan_jobs = 1
    if scan_jobs < 1:
        scan_jobs = 1
    if lang == "ko":
        if target_files:
            _log_console(
                f"[scan_fonts] --target-file 기준 스캔 시작: {total_files}개 파일"
            )
        else:
            _log_console(f"[scan_fonts] 전체 스캔 시작: {total_files}개 파일")
    else:
        if target_files:
            _log_console(
                f"[scan_fonts] Starting target-file scan: {total_files} file(s)"
            )
        else:
            _log_console(f"[scan_fonts] Starting full scan: {total_files} file(s)")

    if isolate_files and scan_jobs > 1 and total_files > 1:
        max_workers = min(scan_jobs, total_files)
        if lang == "ko":
            _log_console(f"[scan_fonts] 병렬 워커 모드: {max_workers}개")
        else:
            _log_console(f"[scan_fonts] Parallel worker mode: {max_workers}")

        indexed_results: dict[
            int, tuple[dict[str, list[JsonDict]], str | None, str]
        ] = {}
        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_meta = {
                executor.submit(
                    _scan_fonts_via_worker,
                    game_path,
                    assets_file,
                    lang,
                    ps5_swizzle,
                ): (idx, os.path.basename(assets_file))
                for idx, assets_file in enumerate(assets_files)
            }
            for future in as_completed(future_to_meta):
                idx, fn = future_to_meta[future]
                try:
                    scanned, worker_error = future.result()
                except Exception as e:
                    scanned = {"ttf": [], "sdf": []}
                    worker_error = (
                        f"scan worker 실행 실패: {e!r}"
                        if lang == "ko"
                        else f"failed to run scan worker: {e!r}"
                    )
                indexed_results[idx] = (scanned, worker_error, fn)
                completed += 1
                if lang == "ko":
                    _log_console(f"[scan_fonts] 진행 {completed}/{total_files}: {fn}")
                else:
                    _log_console(
                        f"[scan_fonts] Progress {completed}/{total_files}: {fn}"
                    )

        for idx in range(total_files):
            scanned, worker_error, processed_file_name = indexed_results.get(
                idx, ({"ttf": [], "sdf": []}, None, "")
            )
            if worker_error:
                if lang == "ko":
                    _log_console(f"[scan_fonts] 워커 경고: {worker_error}")
                else:
                    _log_console(f"[scan_fonts] Worker warning: {worker_error}")
            _log_scan_result_details(processed_file_name or f"index_{idx}", scanned)
            fonts["ttf"].extend(scanned.get("ttf", []))
            fonts["sdf"].extend(scanned.get("sdf", []))
    else:
        for idx, assets_file in enumerate(assets_files, start=1):
            fn = os.path.basename(assets_file)
            if lang == "ko":
                _log_console(f"[scan_fonts] 진행 {idx}/{total_files}: {fn}")
            else:
                _log_console(f"[scan_fonts] Progress {idx}/{total_files}: {fn}")

            if isolate_files:
                scanned, worker_error = _scan_fonts_via_worker(
                    game_path,
                    assets_file,
                    lang=lang,
                    detect_ps5_swizzle=ps5_swizzle,
                )
                if worker_error:
                    if lang == "ko":
                        _log_console(f"[scan_fonts] 워커 경고: {worker_error}")
                    else:
                        _log_console(f"[scan_fonts] Worker warning: {worker_error}")
                _log_scan_result_details(fn, scanned)
                fonts["ttf"].extend(scanned.get("ttf", []))
                fonts["sdf"].extend(scanned.get("sdf", []))
                continue

            scanned, load_error = _scan_fonts_in_asset_file(
                assets_file,
                generator,
                lang=lang,
                detect_ps5_swizzle=ps5_swizzle,
            )
            if load_error:
                _log_console(f"[scan_fonts] {load_error}")
                continue
            _log_scan_result_details(fn, scanned)
            fonts["ttf"].extend(scanned.get("ttf", []))
            fonts["sdf"].extend(scanned.get("sdf", []))

    return fonts


def parse_fonts(
    game_path: str,
    lang: Language = "ko",
    target_files: set[str] | None = None,
    scan_jobs: int = 1,
    ps5_swizzle: bool = False,
) -> str:
    """KR: 스캔한 폰트를 JSON으로 저장하고 결과 파일 경로를 반환합니다.
    KR: target_files가 있으면 해당 파일만 파싱합니다.
    EN: Save scanned fonts to JSON and return output file path.
    EN: If target_files is provided, parse only those files.
    """
    # KR: parse 모드는 파일 단위 워커로 스캔해 UnityPy 하드 크래시를 격리합니다.
    # EN: Parse mode scans via per-file workers to isolate hard UnityPy crashes.
    fonts = scan_fonts(
        game_path,
        lang=lang,
        target_files=target_files,
        isolate_files=True,
        scan_jobs=scan_jobs,
        ps5_swizzle=ps5_swizzle,
    )
    game_name = os.path.basename(game_path)
    output_file = os.path.join(get_script_dir(), f"{game_name}.json")

    result: dict[str, JsonDict] = {}

    for font in fonts["ttf"]:
        key = (
            f"{font['file']}|{font['assets_name']}|{font['name']}|TTF|{font['path_id']}"
        )
        result[key] = {
            "File": font["file"],
            "assets_name": font["assets_name"],
            "Path_ID": font["path_id"],
            "Type": "TTF",
            "Name": font["name"],
            "Replace_to": "",
        }

    for font in fonts["sdf"]:
        key = (
            f"{font['file']}|{font['assets_name']}|{font['name']}|SDF|{font['path_id']}"
        )
        if ps5_swizzle:
            swizzle_flag = "True" if parse_bool_flag(font.get("swizzle")) else "False"
            process_swizzle_flag = (
                "True" if parse_bool_flag(font.get("process_swizzle")) else "False"
            )
            entry: JsonDict = {
                "File": font["file"],
                "assets_name": font["assets_name"],
                "Path_ID": font["path_id"],
                "Type": "SDF",
                "Name": font["name"],
                "swizzle": swizzle_flag,
                "process_swizzle": process_swizzle_flag,
                "Replace_to": "",
            }
        else:
            entry = {
                "File": font["file"],
                "assets_name": font["assets_name"],
                "Path_ID": font["path_id"],
                "Type": "SDF",
                "Name": font["name"],
                "Replace_to": "",
            }
        result[key] = entry

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=4, ensure_ascii=False)

    if lang == "ko":
        _log_console(f"폰트 정보가 '{output_file}'에 저장되었습니다.")
        _log_console(f"  - TTF 폰트: {len(fonts['ttf'])}개")
        _log_console(f"  - SDF 폰트: {len(fonts['sdf'])}개")
    else:
        _log_console(f"Font information saved to '{output_file}'.")
        _log_console(f"  - TTF fonts: {len(fonts['ttf'])}")
        _log_console(f"  - SDF fonts: {len(fonts['sdf'])}")
    return output_file


@lru_cache(maxsize=64)
def _load_font_assets_cached(script_dir: str, normalized: str) -> JsonDict:
    """KR: KR_ASSETS에서 폰트 리소스를 읽어 캐시에 저장합니다.
    EN: Load and cache font resources from KR_ASSETS.
    """
    kr_assets = os.path.join(script_dir, "KR_ASSETS")
    raw_name = str(normalized).strip()

    def _dedupe_preserve_order(names: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for item in names:
            key = item.strip()
            if not key:
                continue
            lowered = key.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            ordered.append(key)
        return ordered

    def _strip_render_suffix(name: str) -> str:
        if name.endswith(" SDF"):
            return name[: -len(" SDF")]
        if name.endswith(" Raster"):
            return name[: -len(" Raster")]
        return name

    base_name = _strip_render_suffix(raw_name)
    has_explicit_variant = raw_name.endswith(" SDF") or raw_name.endswith(" Raster")
    if has_explicit_variant:
        explicit_candidates = [raw_name, base_name]
        if raw_name.endswith(" Raster"):
            explicit_candidates.append(f"{base_name} SDF")
        elif raw_name.endswith(" SDF"):
            explicit_candidates.append(f"{base_name} Raster")
        name_candidates = _dedupe_preserve_order(explicit_candidates)
    else:
        name_candidates = _dedupe_preserve_order(
            [raw_name, f"{base_name} SDF", f"{base_name} Raster"]
        )

    font_name_candidates = _dedupe_preserve_order(
        [raw_name, base_name] + name_candidates
    )

    ttf_data = None
    for font_name in font_name_candidates:
        for ext in (".ttf", ".otf"):
            font_path = os.path.join(kr_assets, f"{font_name}{ext}")
            if os.path.exists(font_path):
                with open(font_path, "rb") as f:
                    ttf_data = f.read()
                break
        if ttf_data is not None:
            break

    sdf_data = None
    sdf_data_normalized = None
    sdf_swizzle = False
    sdf_process_swizzle = False
    for name_candidate in name_candidates:
        sdf_json_path = os.path.join(kr_assets, f"{name_candidate}.json")
        if not os.path.exists(sdf_json_path):
            continue
        with open(sdf_json_path, "r", encoding="utf-8") as f:
            sdf_data = json.load(f)
        if isinstance(sdf_data, dict):
            sdf_data_normalized = normalize_sdf_data(sdf_data, deep_copy=True)
            sdf_swizzle = parse_bool_flag(sdf_data.get("swizzle"))
            sdf_process_swizzle = parse_bool_flag(sdf_data.get("process_swizzle"))
        break

    sdf_atlas = None
    for name_candidate in name_candidates:
        sdf_atlas_path = os.path.join(kr_assets, f"{name_candidate} Atlas.png")
        if not os.path.exists(sdf_atlas_path):
            continue
        with open(sdf_atlas_path, "rb") as f:
            sdf_atlas = Image.open(f)
            sdf_atlas.load()
        break

    sdf_material_data = None
    for name_candidate in name_candidates:
        sdf_material_path = os.path.join(kr_assets, f"{name_candidate} Material.json")
        if not os.path.exists(sdf_material_path):
            continue
        with open(sdf_material_path, "r", encoding="utf-8") as f:
            sdf_material_data = json.load(f)
        break

    return {
        "ttf_data": ttf_data,
        "sdf_data": sdf_data,
        "sdf_data_normalized": sdf_data_normalized,
        "sdf_atlas": sdf_atlas,
        "sdf_materials": sdf_material_data,
        "sdf_swizzle": sdf_swizzle,
        "sdf_process_swizzle": sdf_process_swizzle,
    }


def load_font_assets(font_name: str) -> JsonDict:
    """KR: 지정 폰트명의 교체용 리소스(TTF/SDF/Atlas/Material)를 로드합니다.
    EN: Load replacement assets (TTF/SDF/Atlas/Material) for a font name.
    """
    normalized = normalize_font_name(font_name)
    cached_assets = _load_font_assets_cached(get_script_dir(), normalized)
    atlas = cached_assets["sdf_atlas"]
    return {
        "ttf_data": cached_assets["ttf_data"],
        "sdf_data": cached_assets["sdf_data"],
        "sdf_data_normalized": cached_assets.get("sdf_data_normalized"),
        # Reuse cached atlas object to avoid per-replacement image duplication.
        "sdf_atlas": atlas,
        "sdf_materials": cached_assets["sdf_materials"],
        "sdf_swizzle": cached_assets.get("sdf_swizzle"),
        "sdf_process_swizzle": bool(cached_assets.get("sdf_process_swizzle", False)),
    }


def replace_fonts_in_file(
    unity_version: str,
    game_path: str,
    assets_file: str,
    replacements: dict[str, JsonDict],
    replace_ttf: bool = True,
    replace_sdf: bool = True,
    use_game_mat: bool = False,
    use_game_line_metrics: bool = False,
    material_scale_by_padding: bool = True,
    prefer_original_compress: bool = False,
    temp_root_dir: str | None = None,
    generator: TypeTreeGenerator | None = None,
    replacement_lookup: dict[tuple[str, str, str, int], str] | None = None,
    ps5_swizzle: bool = False,
    preview_export: bool = False,
    preview_root: str | None = None,
    lang: Language = "ko",
) -> bool:
    """KR: 단일 assets 파일의 TTF/SDF 폰트를 교체하고 저장합니다.
    KR: 기본 모드는 줄 간격 관련 메트릭(LineHeight/Ascender/Descender 등)을 게임 원본 비율로 보정해
    KR: 교체 pointSize에 맞춰 적용합니다.
    KR: use_game_line_metrics=True면 게임 원본 줄 간격 메트릭을 그대로 사용합니다.
    KR: pointSize는 옵션과 무관하게 교체 폰트 값을 유지합니다.
    KR: material_scale_by_padding=True면 SDF 머티리얼 float를 (게임 padding / 교체 padding) 비율로 보정합니다.
    KR: prefer_original_compress=True면 원본 압축 우선, False면 무압축 계열 우선 저장 전략을 사용합니다.
    KR: ps5_swizzle=True면 대상 Atlas의 swizzle 상태를 판별해 교체 Atlas를 자동 swizzle/unswizzle합니다.
    KR: preview_export=True면 preview 폴더에 Atlas/Glyph crop 미리보기를 저장합니다.
    KR: ps5_swizzle=True일 때는 unswizzle 기준으로 저장합니다.
    KR: temp_root_dir가 지정되면 임시 저장 디렉터리 루트로 사용합니다.
    EN: Replace TTF/SDF fonts in one assets file and save changes.
    EN: By default, line-related metrics (LineHeight/Ascender/Descender, etc.) are adjusted from in-game ratios
    EN: and scaled to match replacement pointSize.
    EN: With use_game_line_metrics=True, original in-game line metrics are used directly.
    EN: pointSize still follows replacement font data regardless of this option.
    EN: If material_scale_by_padding=True, SDF material floats are adjusted by (game padding / replacement padding).
    EN: When prefer_original_compress=True, original compression is tried first; otherwise uncompressed-family is preferred.
    EN: If ps5_swizzle=True, auto-detect target atlas swizzle state and swizzle/unswizzle replacement atlas.
    EN: If preview_export=True, save Atlas/Glyph crop previews into preview folder.
    EN: With ps5_swizzle=True, previews are saved in unswizzled view.
    EN: If temp_root_dir is set, it is used as the root directory for temporary save files.
    """
    fn_without_path = os.path.basename(assets_file)
    data_path = get_data_path(game_path, lang=lang)
    using_custom_temp_root = temp_root_dir is not None
    tmp_root = (
        os.path.abspath(temp_root_dir)
        if using_custom_temp_root
        else os.path.join(data_path, "temp")
    )
    tmp_path = os.path.join(tmp_root, "unity_font_replacer_temp")
    if using_custom_temp_root:
        register_temp_dir_for_cleanup(tmp_path)
    else:
        register_temp_dir_for_cleanup(tmp_root)
    bundle_signatures = BUNDLE_SIGNATURES
    source_bundle_signature = _read_bundle_signature(assets_file, bundle_signatures)

    if not os.path.exists(tmp_root):
        os.makedirs(tmp_root, exist_ok=True)

    if os.path.exists(tmp_path):
        shutil.rmtree(tmp_path)
    os.makedirs(tmp_path, exist_ok=True)

    env = UnityPy.load(assets_file)
    env_file = getattr(env, "file", None)
    if env_file is None:
        files = getattr(env, "files", None)
        if isinstance(files, dict) and len(files) == 1:
            env_file = next(iter(files.values()))
    if env_file is None:
        raise RuntimeError(
            "Could not determine primary UnityPy file object for saving."
        )
    if generator is None:
        compile_method = get_compile_method(data_path)
        generator = _create_generator(
            unity_version, game_path, data_path, compile_method, lang=lang
        )
    env.typetree_generator = generator
    if replacement_lookup is None:
        replacement_lookup, _ = build_replacement_lookup(replacements)
    replacement_meta_lookup: dict[tuple[str, str, str, int], JsonDict] = {}
    preview_target_lookup: dict[tuple[str, str, int], JsonDict] = {}
    for info in replacements.values():
        if not isinstance(info, dict):
            continue
        type_raw = info.get("Type")
        file_raw = info.get("File")
        assets_raw = info.get("assets_name")
        path_raw = info.get("Path_ID")
        if (
            not isinstance(type_raw, str)
            or not isinstance(file_raw, str)
            or not isinstance(assets_raw, str)
        ):
            continue
        try:
            path_id = int(path_raw)
        except (TypeError, ValueError):
            continue
        if type_raw == "SDF":
            preview_target_lookup[(file_raw, assets_raw, path_id)] = info
        if not info.get("Replace_to"):
            continue
        replacement_meta_lookup[(type_raw, file_raw, assets_raw, path_id)] = info

    texture_object_lookup: dict[tuple[str, int], Any] = {}
    texture_swizzle_state_cache: dict[str, tuple[str | None, str | None]] = {}
    for item in env.objects:
        if item.type.name != "Texture2D":
            continue
        texture_object_lookup[(item.assets_file.name, int(item.path_id))] = item

    target_sdf_targets: set[tuple[str, int]] = set()
    target_sdf_pathids: set[int] = set()
    target_sdf_font_by_target: dict[tuple[str, int], str] = {}
    old_line_metric_keys = _OLD_LINE_METRIC_KEYS
    old_line_metric_scale_keys = _OLD_LINE_METRIC_SCALE_KEYS
    new_line_metric_keys = _NEW_LINE_METRIC_KEYS
    new_line_metric_scale_keys = _NEW_LINE_METRIC_SCALE_KEYS
    material_padding_scale_keys = _MATERIAL_PADDING_SCALE_KEYS

    if replace_sdf:
        for key, value in replacement_lookup.items():
            if len(key) == 4 and key[0] == "SDF" and key[1] == fn_without_path:
                assets_key = key[2]
                path_id = key[3]
                target_key = (str(assets_key), int(path_id))
                target_sdf_targets.add(target_key)
                target_sdf_pathids.add(path_id)
                target_sdf_font_by_target.setdefault(target_key, value)
        if preview_export:
            for file_name, assets_name, path_id in preview_target_lookup.keys():
                if file_name != fn_without_path:
                    continue
                target_key = (str(assets_name), int(path_id))
                target_sdf_targets.add(target_key)
                target_sdf_pathids.add(int(path_id))
    matched_sdf_targets = 0
    patched_sdf_targets = 0
    sdf_parse_failure_reasons: list[str] = []

    texture_replacements: dict[str, Any] = {}
    texture_replacement_target_swizzle: dict[str, bool] = {}
    texture_replacement_metadata_size: dict[str, tuple[int, int]] = {}
    texture_replacement_linear_source: dict[str, Image.Image] = {}
    material_replacements: dict[str, JsonDict] = {}
    modified = False

    for obj in env.objects:
        assets_name = obj.assets_file.name
        if obj.type.name == "Font" and replace_ttf:
            font_pathid = obj.path_id
            replacement_font = replacement_lookup.get(
                ("TTF", fn_without_path, assets_name, font_pathid)
            )

            if replacement_font:
                assets = load_font_assets(replacement_font)
                if assets["ttf_data"]:
                    font = obj.parse_as_object()
                    current_ttf_data = bytes(getattr(font, "m_FontData", b""))
                    if current_ttf_data == assets["ttf_data"]:
                        _log_debug(
                            f"[replace_ttf] file={fn_without_path} assets={assets_name} path_id={font_pathid} "
                            f"name={font.m_Name} target={replacement_font} action=skip_same size={len(current_ttf_data)}"
                        )
                        if lang == "ko":
                            _log_console(
                                f"TTF 폰트 동일(건너뜀): {assets_name} | {font.m_Name} | "
                                f"(PathID: {font_pathid} == {replacement_font})"
                            )
                        else:
                            _log_console(
                                f"TTF already same (skip): {assets_name} | {font.m_Name} | "
                                f"(PathID: {font_pathid} == {replacement_font})"
                            )
                        continue
                    if lang == "ko":
                        _log_console(
                            f"TTF 폰트 교체: {assets_name} | {font.m_Name} | (PathID: {font_pathid} -> {replacement_font})"
                        )
                    else:
                        _log_console(
                            f"TTF font replaced: {assets_name} | {font.m_Name} | (PathID: {font_pathid} -> {replacement_font})"
                        )
                    _log_debug(
                        f"[replace_ttf] file={fn_without_path} assets={assets_name} path_id={font_pathid} "
                        f"name={font.m_Name} target={replacement_font} "
                        f"old_size={len(current_ttf_data)} new_size={len(assets['ttf_data'])}"
                    )
                    font.m_FontData = assets["ttf_data"]
                    font.save()
                    modified = True

        if obj.type.name == "MonoBehaviour" and replace_sdf:
            pathid = obj.path_id
            target_key = (assets_name, int(pathid))
            if target_sdf_targets and target_key not in target_sdf_targets:
                continue
            try:
                parse_dict = obj.parse_as_dict()
            except Exception as e:
                reason = f"PathID {obj.path_id} parse_as_dict 실패 [{type(e).__name__}]: {e!r}"
                sdf_parse_failure_reasons.append(reason)
                _log_debug(
                    f"[replace_sdf] file={fn_without_path} assets={assets_name} path_id={obj.path_id} "
                    f"action=parse_as_dict_failed error={type(e).__name__}: {e!r}"
                )
                if lang == "ko":
                    _log_console(f"  경고: {reason}")
                    debug_parse_log(
                        f"[replace_fonts] MonoBehaviour parse_as_dict 실패: {fn_without_path} | {reason}"
                    )
                else:
                    _log_console(
                        f"  Warning: PathID {obj.path_id} parse_as_dict failed [{type(e).__name__}]: {e!r}"
                    )
                    debug_parse_log(
                        f"[replace_fonts] MonoBehaviour parse_as_dict failed: {fn_without_path} | {reason}"
                    )
                continue
            unity_version_hint_raw = getattr(obj.assets_file, "unity_version", None)
            unity_version_hint = str(unity_version_hint_raw or unity_version or "")
            tmp_info = inspect_tmp_font_schema(
                parse_dict,
                unity_version=unity_version_hint or None,
            )
            if not tmp_info.get("is_tmp"):
                continue
            glyph_count = int(tmp_info.get("glyph_count", 0) or 0)
            atlas_file_id = int(tmp_info.get("atlas_file_id", 0) or 0)
            atlas_path_id = int(tmp_info.get("atlas_path_id", 0) or 0)

            # KR: 외부 참조 stub만 제외하고 실제 TMP 폰트만 처리합니다.
            # EN: Skip external stubs and process only concrete TMP font assets.
            if atlas_file_id != 0 and atlas_path_id == 0:
                continue
            if glyph_count == 0:
                continue

            objname = obj.peek_name()
            replacement_font = replacement_lookup.get(
                ("SDF", fn_without_path, assets_name, pathid)
            )
            if replacement_font is None:
                replacement_font = target_sdf_font_by_target.get(target_key)

            preview_target_meta = preview_target_lookup.get(
                (fn_without_path, assets_name, int(pathid))
            )
            if (
                replacement_font is None
                and preview_target_meta is not None
                and preview_export
            ):
                atlas_path_id_preview = int(tmp_info.get("atlas_path_id", 0) or 0)
                if atlas_path_id_preview:
                    target_swizzle_verdict: str | None = None
                    if ps5_swizzle:
                        target_swizzle_verdict, _ = _detect_target_texture_swizzle(
                            texture_object_lookup,
                            texture_swizzle_state_cache,
                            assets_name,
                            int(atlas_path_id_preview),
                        )
                    target_preview_image = _load_target_unswizzled_preview_image(
                        texture_object_lookup,
                        assets_name,
                        int(atlas_path_id_preview),
                        target_swizzle_verdict,
                        preview_rotate=PS5_SWIZZLE_ROTATE if ps5_swizzle else 0,
                    )
                    if isinstance(target_preview_image, Image.Image):
                        _save_swizzle_preview(
                            target_preview_image,
                            preview_enabled=preview_export,
                            preview_root=preview_root,
                            assets_file_name=fn_without_path,
                            assets_name=assets_name,
                            atlas_path_id=int(atlas_path_id_preview),
                            font_name=str(objname),
                            target_swizzled=bool(
                                target_swizzle_verdict == "likely_swizzled_input"
                            ),
                            lang=lang,
                        )
                        preview_sdf_data = normalize_sdf_data(parse_dict)
                        _save_glyph_crop_previews(
                            target_preview_image,
                            preview_enabled=preview_export,
                            preview_root=preview_root,
                            assets_file_name=fn_without_path,
                            assets_name=assets_name,
                            atlas_path_id=int(atlas_path_id_preview),
                            font_name=str(objname),
                            sdf_data=preview_sdf_data,
                            lang=lang,
                        )

            if replacement_font:
                replacement_meta = replacement_meta_lookup.get(
                    ("SDF", fn_without_path, assets_name, int(pathid)),
                    {},
                )
                replacement_process_swizzle = parse_bool_flag(
                    replacement_meta.get("process_swizzle")
                )
                replacement_swizzle_hint = parse_bool_flag(
                    replacement_meta.get("swizzle")
                )
                _log_debug(
                    f"[replace_sdf] file={fn_without_path} assets={assets_name} path_id={pathid} "
                    f"font={objname} target={replacement_font} "
                    f"replacement_swizzle_hint={replacement_swizzle_hint} "
                    f"replacement_process_swizzle={replacement_process_swizzle}"
                )
                matched_sdf_targets += 1
                assets = load_font_assets(replacement_font)
                if assets["sdf_data"] and assets["sdf_atlas"]:
                    if lang == "ko":
                        _log_console(
                            f"SDF 폰트 교체: {assets_name} | {objname} | (PathID: {pathid}) -> {replacement_font}"
                        )
                    else:
                        _log_console(
                            f"SDF font replaced: {assets_name} | {objname} | (PathID: {pathid}) -> {replacement_font}"
                        )
                    source_atlas = assets["sdf_atlas"]
                    source_swizzled = parse_bool_flag(assets.get("sdf_swizzle"))
                    asset_process_swizzle = parse_bool_flag(
                        assets.get("sdf_process_swizzle")
                    )
                    atlas_linear_for_alpha8 = source_atlas
                    if ps5_swizzle and source_swizzled:
                        try:
                            atlas_linear_for_alpha8 = apply_ps5_unswizzle_to_image(
                                source_atlas,
                                allow_axis_swap=True,
                                roughness_guard=True,
                            )
                        except Exception:
                            atlas_linear_for_alpha8 = source_atlas
                    target_swizzle_verdict: str | None = None
                    target_swizzle_source: str | None = None
                    target_is_swizzled: bool | None = None

                    # KR: 입력 JSON이 신형/구형이어도 내부 교체는 신형 TMP 스키마로 통일합니다.
                    # EN: Normalize replacement JSON to the new TMP schema regardless of input format.
                    replace_data = assets.get("sdf_data_normalized")
                    if not isinstance(replace_data, dict):
                        replace_data = normalize_sdf_data(assets["sdf_data"])
                    try:
                        replacement_render_mode = int(
                            replace_data.get("m_AtlasRenderMode", 4118) or 0
                        )
                    except Exception:
                        replacement_render_mode = 4118
                    replacement_is_sdf = (replacement_render_mode & 0x1000) != 0
                    game_padding_for_material = 0.0

                    # KR: GameObject/Script/Material/Atlas 참조는 기존 PathID를 유지해야 런타임 연결이 깨지지 않습니다.
                    # EN: Preserve original GameObject/Script/Material/Atlas references to keep runtime links intact.
                    m_GameObject_FileID = parse_dict["m_GameObject"]["m_FileID"]
                    m_GameObject_PathID = parse_dict["m_GameObject"]["m_PathID"]
                    m_Script_FileID = parse_dict["m_Script"]["m_FileID"]
                    m_Script_PathID = parse_dict["m_Script"]["m_PathID"]
                    has_source_font_ref = isinstance(
                        parse_dict.get("m_SourceFontFile"), dict
                    )
                    if has_source_font_ref:
                        m_SourceFontFile_FileID = int(
                            parse_dict["m_SourceFontFile"].get("m_FileID", 0) or 0
                        )
                        m_SourceFontFile_PathID = int(
                            parse_dict["m_SourceFontFile"].get("m_PathID", 0) or 0
                        )
                    else:
                        m_SourceFontFile_FileID = 0
                        m_SourceFontFile_PathID = 0

                    if parse_dict.get("m_Material") is not None:
                        m_Material_FileID = parse_dict["m_Material"]["m_FileID"]
                        m_Material_PathID = parse_dict["m_Material"]["m_PathID"]
                    else:
                        m_Material_FileID = parse_dict["material"]["m_FileID"]
                        m_Material_PathID = parse_dict["material"]["m_PathID"]

                    target_new_atlas_ref = _first_valid_atlas_ref(
                        parse_dict.get("m_AtlasTextures")
                    ) or _first_atlas_ref(parse_dict.get("m_AtlasTextures"))
                    target_old_atlas_ref = (
                        cast(JsonDict, parse_dict.get("atlas"))
                        if isinstance(parse_dict.get("atlas"), dict)
                        else None
                    )
                    target_has_new_face = isinstance(parse_dict.get("m_FaceInfo"), dict)
                    target_has_new_glyphs = isinstance(
                        parse_dict.get("m_GlyphTable"), list
                    )
                    target_has_new_chars = isinstance(
                        parse_dict.get("m_CharacterTable"), list
                    )
                    target_has_old_face = isinstance(parse_dict.get("m_fontInfo"), dict)
                    target_has_old_glyphs = isinstance(
                        parse_dict.get("m_glyphInfoList"), list
                    )
                    target_creation_settings_key = _resolve_creation_settings_key(
                        parse_dict,
                        unity_version=unity_version_hint or None,
                    )
                    target_creation_settings = (
                        cast(JsonDict, parse_dict.get(target_creation_settings_key))
                        if target_creation_settings_key
                        and isinstance(
                            parse_dict.get(target_creation_settings_key), dict
                        )
                        else None
                    )

                    if target_new_atlas_ref is not None:
                        m_AtlasTextures_FileID, m_AtlasTextures_PathID = _atlas_ref_ids(
                            target_new_atlas_ref
                        )
                    elif target_old_atlas_ref is not None:
                        m_AtlasTextures_FileID, m_AtlasTextures_PathID = _atlas_ref_ids(
                            target_old_atlas_ref
                        )
                    else:
                        m_AtlasTextures_FileID = int(atlas_file_id)
                        m_AtlasTextures_PathID = int(atlas_path_id)

                    if target_has_new_face:
                        game_face_info = parse_dict.get("m_FaceInfo", {})
                        try:
                            game_padding_for_material = float(
                                parse_dict.get(
                                    "m_AtlasPadding",
                                    (
                                        target_creation_settings.get("padding", 0)
                                        if isinstance(target_creation_settings, dict)
                                        else 0
                                    ),
                                )
                            )
                        except Exception:
                            game_padding_for_material = 0.0

                        target_face_info = dict(replace_data["m_FaceInfo"])
                        if isinstance(game_face_info, dict):
                            if use_game_line_metrics:
                                metric_scale = 1.0
                            else:
                                metric_scale = _safe_metric_scale(
                                    game_face_info.get("m_PointSize", 0),
                                    target_face_info.get("m_PointSize", 0),
                                )
                            for metric_key in new_line_metric_keys:
                                if metric_key in game_face_info:
                                    metric_value = game_face_info[metric_key]
                                    if (
                                        metric_key in new_line_metric_scale_keys
                                        and metric_scale != 1.0
                                    ):
                                        try:
                                            metric_value = (
                                                float(metric_value) * metric_scale
                                            )
                                        except Exception:
                                            pass
                                    target_face_info[metric_key] = metric_value
                        ensure_int(
                            target_face_info,
                            ["m_PointSize", "m_AtlasWidth", "m_AtlasHeight"],
                        )
                        parse_dict["m_FaceInfo"] = target_face_info

                    replacement_glyph_table = (
                        replace_data.get("m_GlyphTable", [])
                        if isinstance(replace_data.get("m_GlyphTable", []), list)
                        else []
                    )
                    replacement_character_table = (
                        replace_data.get("m_CharacterTable", [])
                        if isinstance(replace_data.get("m_CharacterTable", []), list)
                        else []
                    )

                    if target_has_new_glyphs:
                        parse_dict["m_GlyphTable"] = replacement_glyph_table
                    if target_has_new_chars:
                        parse_dict["m_CharacterTable"] = replacement_character_table

                    if replacement_glyph_table:
                        replacement_glyph_indexes = [
                            int(g.get("m_Index", 0) or 0)
                            for g in replacement_glyph_table
                            if isinstance(g, dict)
                        ]
                        for glyph_index_key in _TMP_GLYPH_INDEX_LIST_KEYS:
                            if glyph_index_key in parse_dict:
                                parse_dict[glyph_index_key] = list(
                                    replacement_glyph_indexes
                                )

                    if "m_AtlasWidth" in parse_dict:
                        parse_dict["m_AtlasWidth"] = int(
                            replace_data.get(
                                "m_AtlasWidth", parse_dict.get("m_AtlasWidth", 0)
                            )
                            or 0
                        )
                    if "m_AtlasHeight" in parse_dict:
                        parse_dict["m_AtlasHeight"] = int(
                            replace_data.get(
                                "m_AtlasHeight", parse_dict.get("m_AtlasHeight", 0)
                            )
                            or 0
                        )
                    if "m_AtlasPadding" in parse_dict:
                        parse_dict["m_AtlasPadding"] = int(
                            replace_data.get(
                                "m_AtlasPadding", parse_dict.get("m_AtlasPadding", 0)
                            )
                            or 0
                        )
                    if "m_AtlasRenderMode" in parse_dict:
                        parse_dict["m_AtlasRenderMode"] = replace_data.get(
                            "m_AtlasRenderMode",
                            parse_dict.get("m_AtlasRenderMode", 4118),
                        )
                    if "m_UsedGlyphRects" in parse_dict:
                        parse_dict["m_UsedGlyphRects"] = replace_data.get(
                            "m_UsedGlyphRects", parse_dict.get("m_UsedGlyphRects", [])
                        )
                    if "m_FreeGlyphRects" in parse_dict:
                        parse_dict["m_FreeGlyphRects"] = replace_data.get(
                            "m_FreeGlyphRects", parse_dict.get("m_FreeGlyphRects", [])
                        )
                    if "m_FontWeightTable" in parse_dict:
                        parse_dict["m_FontWeightTable"] = replace_data.get(
                            "m_FontWeightTable", parse_dict.get("m_FontWeightTable", [])
                        )

                    if target_has_old_face or target_has_old_glyphs:
                        game_font_info = parse_dict.get("m_fontInfo", {})
                        if game_padding_for_material <= 0:
                            try:
                                game_padding_for_material = float(
                                    game_font_info.get(
                                        "Padding",
                                        (
                                            target_creation_settings.get("padding", 0)
                                            if isinstance(
                                                target_creation_settings, dict
                                            )
                                            else 0
                                        ),
                                    )
                                )
                            except Exception:
                                game_padding_for_material = 0.0

                        old_font_info = convert_face_info_new_to_old(
                            replace_data["m_FaceInfo"],
                            replace_data.get("m_AtlasPadding", 0),
                            replace_data.get("m_AtlasWidth", 0),
                            replace_data.get("m_AtlasHeight", 0),
                        )
                        if isinstance(game_font_info, dict):
                            if use_game_line_metrics:
                                metric_scale = 1.0
                            else:
                                metric_scale = _safe_metric_scale(
                                    game_font_info.get("PointSize", 0),
                                    old_font_info.get("PointSize", 0),
                                )
                            for metric_key in old_line_metric_keys:
                                if metric_key in game_font_info:
                                    metric_value = game_font_info[metric_key]
                                    if (
                                        metric_key in old_line_metric_scale_keys
                                        and metric_scale != 1.0
                                    ):
                                        try:
                                            metric_value = (
                                                float(metric_value) * metric_scale
                                            )
                                        except Exception:
                                            pass
                                    old_font_info[metric_key] = metric_value

                        replacement_atlas = assets.get("sdf_atlas")
                        atlas_height = int(
                            replace_data.get(
                                "m_AtlasHeight",
                                (
                                    replacement_atlas.height
                                    if replacement_atlas is not None
                                    else 0
                                ),
                            )
                        )
                        old_glyph_list = convert_glyphs_new_to_old(
                            replacement_glyph_table,
                            replacement_character_table,
                            atlas_height=atlas_height,
                        )
                        old_font_info["CharacterCount"] = len(old_glyph_list)
                        if target_has_old_face:
                            parse_dict["m_fontInfo"] = old_font_info
                        if target_has_old_glyphs:
                            parse_dict["m_glyphInfoList"] = old_glyph_list

                    if isinstance(target_creation_settings, dict):
                        atlas_width_for_cs = int(
                            parse_dict.get(
                                "m_AtlasWidth", replace_data.get("m_AtlasWidth", 0)
                            )
                            or 0
                        )
                        atlas_height_for_cs = int(
                            parse_dict.get(
                                "m_AtlasHeight", replace_data.get("m_AtlasHeight", 0)
                            )
                            or 0
                        )
                        padding_for_cs = int(
                            parse_dict.get(
                                "m_AtlasPadding", replace_data.get("m_AtlasPadding", 0)
                            )
                            or 0
                        )
                        if target_has_old_face and not use_game_line_metrics:
                            try:
                                padding_for_cs = int(
                                    parse_dict.get("m_fontInfo", {}).get(
                                        "Padding", padding_for_cs
                                    )
                                    or padding_for_cs
                                )
                            except Exception:
                                pass

                        point_size_for_cs = int(
                            replace_data.get("m_FaceInfo", {}).get("m_PointSize", 0)
                            or 0
                        )
                        if target_has_new_face:
                            point_size_for_cs = int(
                                parse_dict.get("m_FaceInfo", {}).get(
                                    "m_PointSize", point_size_for_cs
                                )
                                or point_size_for_cs
                            )
                        elif target_has_old_face:
                            point_size_for_cs = int(
                                parse_dict.get("m_fontInfo", {}).get(
                                    "PointSize", point_size_for_cs
                                )
                                or point_size_for_cs
                            )

                        _sync_creation_settings_payload(
                            target_creation_settings,
                            atlas_width=atlas_width_for_cs,
                            atlas_height=atlas_height_for_cs,
                            padding=padding_for_cs,
                            point_size=point_size_for_cs,
                        )

                    # KR: 신형/구형 필드가 공존하면 신형 face 기준으로 legacy face도 동기화합니다.
                    # EN: If both schemas exist, keep legacy face in sync from new face.
                    if target_has_new_face and target_has_old_face:
                        parse_dict["m_fontInfo"] = convert_face_info_new_to_old(
                            parse_dict["m_FaceInfo"],
                            int(
                                parse_dict.get(
                                    "m_AtlasPadding",
                                    replace_data.get("m_AtlasPadding", 0),
                                )
                                or 0
                            ),
                            int(
                                parse_dict.get(
                                    "m_AtlasWidth", replace_data.get("m_AtlasWidth", 0)
                                )
                                or 0
                            ),
                            int(
                                parse_dict.get(
                                    "m_AtlasHeight",
                                    replace_data.get("m_AtlasHeight", 0),
                                )
                                or 0
                            ),
                        )

                    for dirty_key in _TMP_DIRTY_FLAG_KEYS:
                        if dirty_key in parse_dict:
                            parse_dict[dirty_key] = True

                    # KR: 포맷 분기 후 공통 참조를 원래 값으로 되돌립니다.
                    # EN: Restore shared references to original values after schema-specific patching.
                    parse_dict["m_GameObject"]["m_FileID"] = m_GameObject_FileID
                    parse_dict["m_GameObject"]["m_PathID"] = m_GameObject_PathID
                    parse_dict["m_Script"]["m_FileID"] = m_Script_FileID
                    parse_dict["m_Script"]["m_PathID"] = m_Script_PathID

                    if parse_dict.get("m_Material") is not None:
                        parse_dict["m_Material"]["m_FileID"] = m_Material_FileID
                        parse_dict["m_Material"]["m_PathID"] = m_Material_PathID
                    else:
                        parse_dict["material"]["m_FileID"] = m_Material_FileID
                        parse_dict["material"]["m_PathID"] = m_Material_PathID

                    if has_source_font_ref and isinstance(
                        parse_dict.get("m_SourceFontFile"), dict
                    ):
                        parse_dict["m_SourceFontFile"][
                            "m_FileID"
                        ] = m_SourceFontFile_FileID
                        parse_dict["m_SourceFontFile"][
                            "m_PathID"
                        ] = m_SourceFontFile_PathID

                    current_new_atlas_ref = _first_valid_atlas_ref(
                        parse_dict.get("m_AtlasTextures")
                    ) or _first_atlas_ref(parse_dict.get("m_AtlasTextures"))
                    if current_new_atlas_ref is not None:
                        current_new_atlas_ref["m_FileID"] = m_AtlasTextures_FileID
                        current_new_atlas_ref["m_PathID"] = m_AtlasTextures_PathID
                    if isinstance(parse_dict.get("atlas"), dict):
                        parse_dict["atlas"]["m_FileID"] = m_AtlasTextures_FileID
                        parse_dict["atlas"]["m_PathID"] = m_AtlasTextures_PathID

                    desired_swizzle_state = source_swizzled
                    if ps5_swizzle:
                        target_swizzle_verdict, target_swizzle_source = (
                            _detect_target_texture_swizzle(
                                texture_object_lookup,
                                texture_swizzle_state_cache,
                                assets_name,
                                int(m_AtlasTextures_PathID),
                            )
                        )
                        if target_swizzle_verdict == "likely_swizzled_input":
                            target_is_swizzled = True
                        elif target_swizzle_verdict == "likely_linear_input":
                            target_is_swizzled = False
                        elif replacement_swizzle_hint:
                            target_is_swizzled = True

                        if target_is_swizzled is not None:
                            desired_swizzle_state = target_is_swizzled
                    if replacement_process_swizzle or asset_process_swizzle:
                        desired_swizzle_state = True

                    if ps5_swizzle:
                        if target_swizzle_verdict == "likely_swizzled_input":
                            if lang == "ko":
                                reason = (
                                    f" (근거: {target_swizzle_source})"
                                    if target_swizzle_source
                                    else ""
                                )
                                _log_console(
                                    f"  PS5 swizzle 감지: 대상 Atlas가 swizzled 상태로 판별되었습니다.{reason}"
                                )
                            else:
                                reason = (
                                    f" (source: {target_swizzle_source})"
                                    if target_swizzle_source
                                    else ""
                                )
                                _log_console(
                                    f"  PS5 swizzle detect: target atlas is likely swizzled.{reason}"
                                )
                        elif target_swizzle_verdict == "likely_linear_input":
                            if lang == "ko":
                                reason = (
                                    f" (근거: {target_swizzle_source})"
                                    if target_swizzle_source
                                    else ""
                                )
                                _log_console(
                                    f"  PS5 swizzle 감지: 대상 Atlas가 선형(linear) 상태로 판별되었습니다.{reason}"
                                )
                            else:
                                reason = (
                                    f" (source: {target_swizzle_source})"
                                    if target_swizzle_source
                                    else ""
                                )
                                _log_console(
                                    f"  PS5 swizzle detect: target atlas is likely linear.{reason}"
                                )
                        elif replacement_swizzle_hint:
                            if lang == "ko":
                                _log_console(
                                    "  PS5 swizzle 힌트: JSON swizzle=yes 값을 기준으로 swizzle 적용합니다."
                                )
                            else:
                                _log_console(
                                    "  PS5 swizzle hint: applying swizzle based on JSON swizzle=yes."
                                )
                        elif lang == "ko":
                            _log_console(
                                "  PS5 swizzle 감지: inconclusive, 교체 Atlas 원본 상태를 유지합니다."
                            )
                        else:
                            _log_console(
                                "  PS5 swizzle detect: inconclusive, keeping replacement atlas state."
                            )
                    elif replacement_process_swizzle:
                        if lang == "ko":
                            _log_console(
                                "  process_swizzle=True: 교체 Atlas를 swizzle 상태로 변환합니다."
                            )
                        else:
                            _log_console(
                                "  process_swizzle=True: converting replacement atlas to swizzled state."
                            )
                    _log_debug(
                        f"[replace_sdf] file={fn_without_path} assets={assets_name} path_id={pathid} "
                        f"source_swizzled={source_swizzled} target_swizzle_verdict={target_swizzle_verdict} "
                        f"target_swizzle_source={target_swizzle_source} desired_swizzle={desired_swizzle_state}"
                    )

                    atlas_metadata_width = int(source_atlas.width)
                    atlas_metadata_height = int(source_atlas.height)
                    atlas_for_write = source_atlas
                    if desired_swizzle_state != source_swizzled:
                        try:
                            if desired_swizzle_state:
                                atlas_for_write = apply_ps5_swizzle_to_image(
                                    source_atlas
                                )
                            else:
                                atlas_for_write = apply_ps5_unswizzle_to_image(
                                    source_atlas
                                )
                        except Exception as swizzle_error:
                            atlas_for_write = source_atlas
                            if lang == "ko":
                                _log_console(
                                    f"  경고: PS5 swizzle 변환 실패, 원본 Atlas를 사용합니다. ({swizzle_error})"
                                )
                            else:
                                _log_console(
                                    f"  Warning: PS5 swizzle transform failed; using original atlas. ({swizzle_error})"
                                )

                    if preview_export:
                        preview_image = atlas_for_write
                        if ps5_swizzle and desired_swizzle_state:
                            try:
                                preview_image = apply_ps5_unswizzle_to_image(
                                    atlas_for_write
                                )
                            except Exception as preview_unswizzle_error:
                                preview_image = atlas_for_write
                                if lang == "ko":
                                    _log_console(
                                        "  경고: preview unswizzle 실패, 저장 상태 Atlas 그대로 미리보기를 저장합니다. "
                                        f"({preview_unswizzle_error})"
                                    )
                                else:
                                    _log_console(
                                        "  Warning: preview unswizzle failed; saving preview from stored atlas state. "
                                        f"({preview_unswizzle_error})"
                                    )
                        _save_swizzle_preview(
                            preview_image,
                            preview_enabled=preview_export,
                            preview_root=preview_root,
                            assets_file_name=fn_without_path,
                            assets_name=assets_name,
                            atlas_path_id=int(m_AtlasTextures_PathID),
                            font_name=str(objname),
                            target_swizzled=bool(desired_swizzle_state),
                            lang=lang,
                        )
                        if isinstance(replace_data, dict):
                            _save_glyph_crop_previews(
                                preview_image,
                                preview_enabled=preview_export,
                                preview_root=preview_root,
                                assets_file_name=fn_without_path,
                                assets_name=assets_name,
                                atlas_path_id=int(m_AtlasTextures_PathID),
                                font_name=str(objname),
                                sdf_data=replace_data,
                                lang=lang,
                            )

                    texture_key = f"{assets_name}|{m_AtlasTextures_PathID}"
                    texture_replacements[texture_key] = atlas_for_write
                    texture_replacement_target_swizzle[texture_key] = bool(
                        desired_swizzle_state
                    )
                    texture_replacement_linear_source[texture_key] = (
                        atlas_linear_for_alpha8
                    )
                    texture_replacement_metadata_size[texture_key] = (
                        atlas_metadata_width,
                        atlas_metadata_height,
                    )
                    if m_Material_FileID == 0 and m_Material_PathID != 0:
                        gradient_scale = None
                        apply_replacement_material = not use_game_mat
                        float_overrides: dict[str, float] = {}
                        material_padding_ratio = 1.0
                        material_data = assets.get("sdf_materials")
                        if (not replacement_is_sdf) and use_game_mat:
                            if lang == "ko":
                                _log_console(
                                    "  경고: Raster 폰트에 --use-game-material 사용 시 박스 아티팩트가 생길 수 있습니다."
                                )
                            else:
                                _log_console(
                                    "  Warning: using --use-game-material with Raster fonts may cause box artifacts."
                                )
                        try:
                            replacement_padding = float(
                                replace_data.get("m_AtlasPadding", 0)
                            )
                        except Exception:
                            replacement_padding = 0.0
                        if (
                            replacement_is_sdf
                            and material_scale_by_padding
                            and game_padding_for_material > 0
                            and replacement_padding > 0
                        ):
                            material_padding_ratio = (
                                game_padding_for_material / replacement_padding
                            )
                            if material_padding_ratio <= 0:
                                material_padding_ratio = 1.0
                        if material_data and apply_replacement_material:
                            material_props = material_data.get("m_SavedProperties", {})
                            float_properties = material_props.get("m_Floats", [])
                            for prop in float_properties:
                                if not isinstance(prop, (list, tuple)) or len(prop) < 2:
                                    continue
                                key = str(prop[0])
                                try:
                                    value = float(prop[1])
                                except (TypeError, ValueError):
                                    continue
                                float_overrides[key] = value
                            if material_padding_ratio != 1.0:
                                for key in material_padding_scale_keys:
                                    if key in float_overrides:
                                        float_overrides[key] = float(
                                            float_overrides[key]
                                            * material_padding_ratio
                                        )
                            gradient_scale = float_overrides.get("_GradientScale")
                        if apply_replacement_material and not replacement_is_sdf:
                            # KR: Raster atlas를 SDF 머티리얼로 렌더링할 때 박스 아티팩트를 줄이기 위해
                            # KR: dilate/outline/underlay/glow 계열을 0으로 리셋합니다.
                            # EN: Reduce box artifacts when raster atlases are sampled by SDF materials by
                            # EN: resetting dilate/outline/underlay/glow-like params to 0.
                            float_overrides["_GradientScale"] = 1.0
                            for key in material_padding_scale_keys:
                                if key == "_GradientScale":
                                    continue
                                float_overrides[key] = 0.0
                            gradient_scale = 1.0
                            if lang == "ko":
                                _log_console(
                                    "  Raster 모드 감지: Material SDF 효과값을 0으로 보정합니다."
                                )
                            else:
                                _log_console(
                                    "  Raster mode detected: neutralizing SDF material effect floats."
                                )
                        if (
                            material_scale_by_padding
                            and apply_replacement_material
                            and material_padding_ratio != 1.0
                        ):
                            if lang == "ko":
                                _log_console(
                                    f"  Material padding 비율 보정 적용: {game_padding_for_material:.2f}/{replacement_padding:.2f} "
                                    f"(x{material_padding_ratio:.3f})"
                                )
                            else:
                                _log_console(
                                    f"  Applied material padding ratio: {game_padding_for_material:.2f}/{replacement_padding:.2f} "
                                    f"(x{material_padding_ratio:.3f})"
                                )
                        material_replacements[f"{assets_name}|{m_Material_PathID}"] = {
                            "w": atlas_metadata_width,
                            "h": atlas_metadata_height,
                            "gs": gradient_scale,
                            "float_overrides": float_overrides,
                        }
                    obj.patch(parse_dict)
                    patched_sdf_targets += 1
                    modified = True
                else:
                    missing_parts: list[str] = []
                    if assets.get("sdf_data") is None:
                        missing_parts.append("json")
                    if assets.get("sdf_atlas") is None:
                        missing_parts.append("atlas")
                    if lang == "ko":
                        _log_console(
                            f"  경고: 교체 리소스 누락으로 SDF 적용 건너뜀: {replacement_font} "
                            f"(누락: {', '.join(missing_parts) if missing_parts else 'unknown'})"
                        )
                    else:
                        _log_console(
                            f"  Warning: skipping SDF patch due to missing replacement assets: {replacement_font} "
                            f"(missing: {', '.join(missing_parts) if missing_parts else 'unknown'})"
                        )

    for obj in env.objects:
        assets_name = obj.assets_file.name
        if obj.type.name == "Texture2D":
            replacement_key = f"{assets_name}|{obj.path_id}"
            if replacement_key in texture_replacements:
                parse_dict = obj.parse_as_object()
                if lang == "ko":
                    _log_console(
                        f"텍스처 교체: {obj.peek_name()} (PathID: {obj.path_id})"
                    )
                else:
                    _log_console(
                        f"Texture replaced: {obj.peek_name()} (PathID: {obj.path_id})"
                    )
                replacement_image = texture_replacements[replacement_key]
                target_swizzled_state = texture_replacement_target_swizzle.get(
                    replacement_key
                )
                replacement_linear_source = texture_replacement_linear_source.get(
                    replacement_key
                )
                metadata_w, metadata_h = texture_replacement_metadata_size.get(
                    replacement_key, (0, 0)
                )
                applied_raw_alpha8 = False
                try:
                    texture_format = int(
                        getattr(parse_dict, "m_TextureFormat", -1) or -1
                    )
                except Exception:
                    texture_format = -1
                _log_debug(
                    f"[replace_texture] file={fn_without_path} assets={assets_name} path_id={obj.path_id} "
                    f"name={obj.peek_name()} texture_format={texture_format} metadata={metadata_w}x{metadata_h}"
                )
                if (
                    ps5_swizzle
                    and texture_format == 1
                    and isinstance(replacement_image, Image.Image)
                ):
                    try:
                        alpha_source = (
                            replacement_linear_source
                            if isinstance(replacement_linear_source, Image.Image)
                            else replacement_image
                        )
                        # KR: Alpha8은 반드시 bpe=1 경로로 인코딩해야 합니다.
                        # KR: RGBA 기준 swizzle 후 알파만 추출하면 바이트 순서가 깨질 수 있습니다.
                        # EN: Alpha8 must be encoded via bpe=1 path.
                        # EN: Swizzling as RGBA then extracting alpha can corrupt byte order.
                        if target_swizzled_state is True:
                            alpha_linear, aw, ah = _image_to_alpha8_bytes(alpha_source)
                            alpha_linear_img = Image.frombytes(
                                "L", (int(aw), int(ah)), alpha_linear
                            )
                            alpha_swizzled_img = apply_ps5_swizzle_to_image(
                                alpha_linear_img
                            )
                            alpha_raw, aw, ah = _image_to_alpha8_bytes(
                                alpha_swizzled_img
                            )
                        elif target_swizzled_state is False:
                            # KR: linear(비-swizzled) 타겟은 Unity 저장 좌표계 보정을 위해 상하 반전을 적용합니다.
                            # EN: Linear (non-swizzled) targets are vertically flipped for Unity storage coordinates.
                            alpha_raw, aw, ah = _image_to_alpha8_bytes(
                                ImageOps.flip(alpha_source)
                            )
                        else:
                            alpha_raw, aw, ah = _image_to_alpha8_bytes(alpha_source)
                        parse_dict.m_Width = int(metadata_w if metadata_w > 0 else aw)
                        parse_dict.m_Height = int(metadata_h if metadata_h > 0 else ah)
                        if hasattr(parse_dict, "m_CompleteImageSize"):
                            parse_dict.m_CompleteImageSize = int(len(alpha_raw))
                        parse_dict.image_data = alpha_raw
                        stream_data = getattr(parse_dict, "m_StreamData", None)
                        if stream_data is not None:
                            try:
                                stream_data.offset = 0
                                stream_data.size = 0
                                stream_data.path = ""
                            except Exception:
                                pass
                        applied_raw_alpha8 = True
                        _log_debug(
                            f"[replace_texture] file={fn_without_path} assets={assets_name} path_id={obj.path_id} "
                            f"action=alpha8_raw_injection target_swizzled={target_swizzled_state} "
                            f"raw_size={len(alpha_raw)} width={aw} height={ah}"
                        )
                        if lang == "ko":
                            if target_swizzled_state is True:
                                _log_console(
                                    "  Alpha8 raw 주입 적용: swizzled 바이트를 image_data에 직접 기록합니다."
                                )
                            elif target_swizzled_state is False:
                                _log_console(
                                    "  Alpha8 raw 주입 적용: linear 바이트(상하 반전 보정)를 image_data에 직접 기록합니다."
                                )
                            else:
                                _log_console(
                                    "  Alpha8 raw 주입 적용: 판정 불명(inconclusive) 상태로 image_data에 직접 기록합니다."
                                )
                        else:
                            if target_swizzled_state is True:
                                _log_console(
                                    "  Applied Alpha8 raw injection: writing swizzled bytes directly to image_data."
                                )
                            elif target_swizzled_state is False:
                                _log_console(
                                    "  Applied Alpha8 raw injection: writing linear bytes (with vertical-flip compensation) to image_data."
                                )
                            else:
                                _log_console(
                                    "  Applied Alpha8 raw injection: writing bytes directly to image_data (target state inconclusive)."
                                )
                    except Exception as raw_inject_error:
                        if lang == "ko":
                            _log_console(
                                f"  경고: Alpha8 raw 주입 실패, 일반 image 저장으로 폴백합니다. ({raw_inject_error})"
                            )
                        else:
                            _log_console(
                                f"  Warning: Alpha8 raw injection failed; falling back to image save. ({raw_inject_error})"
                            )
                if not applied_raw_alpha8:
                    parse_dict.image = replacement_image
                parse_dict.save()
                modified = True
        if obj.type.name == "Material":
            if f"{assets_name}|{obj.path_id}" in material_replacements:
                parse_dict = obj.parse_as_object()

                mat_info = material_replacements[f"{assets_name}|{obj.path_id}"]
                float_overrides = mat_info.get("float_overrides", {})
                for i in range(len(parse_dict.m_SavedProperties.m_Floats)):
                    prop_name = parse_dict.m_SavedProperties.m_Floats[i][0]
                    if prop_name in float_overrides:
                        parse_dict.m_SavedProperties.m_Floats[i] = (
                            prop_name,
                            float(float_overrides[prop_name]),
                        )
                    elif prop_name == "_TextureHeight":
                        parse_dict.m_SavedProperties.m_Floats[i] = (
                            "_TextureHeight",
                            float(mat_info["h"]),
                        )
                    elif prop_name == "_TextureWidth":
                        parse_dict.m_SavedProperties.m_Floats[i] = (
                            "_TextureWidth",
                            float(mat_info["w"]),
                        )
                    elif prop_name == "_GradientScale" and mat_info["gs"] is not None:
                        parse_dict.m_SavedProperties.m_Floats[i] = (
                            "_GradientScale",
                            float(mat_info["gs"]),
                        )
                parse_dict.save()

    if modified:
        if lang == "ko":
            _log_console(f"'{fn_without_path}' 저장 중...")
        else:
            _log_console(f"Saving '{fn_without_path}'...")

        save_success = False
        last_save_failure_reason: str | None = None

        def _save_env_file(
            packer: Any = None,
            save_path: str | None = None,
            use_save_to: bool = False,
        ) -> bytes | int:
            """KR: 지정 packer로 기본 파일 객체의 save/save_to를 호출합니다.
            KR: save_path가 주어지면 save_to()로 파일에 직접 기록하여 메모리를 절약합니다.
            KR: 반환값은 bytes(legacy) 또는 저장된 파일 크기(int)입니다.
            EN: Call save/save_to on the primary file object with an optional packer.
            EN: If save_path is provided, it writes via save_to() to reduce memory usage.
            EN: Returns bytes (legacy path) or written file size as int (save_to path).
            """
            # KR: use_save_to=True 이고 save_to()가 존재하면 파일에 직접 저장합니다.
            # EN: When use_save_to=True and save_to() exists, save directly to file.
            save_to_fn = getattr(env_file, "save_to", None)
            if use_save_to and save_path and callable(save_to_fn):
                try:
                    supports_packer = (
                        "packer" in inspect.signature(save_to_fn).parameters
                    )
                except (TypeError, ValueError):
                    supports_packer = False
                if packer is None or not supports_packer:
                    return save_to_fn(save_path)
                return save_to_fn(save_path, packer=packer)

            # KR: 기존 bytes 반환 방식 폴백
            # EN: Fallback to legacy bytes-returning save()
            save_fn = getattr(env_file, "save", None)
            if not callable(save_fn):
                raise AttributeError(
                    "UnityPy environment file object has no callable save()."
                )
            typed_save = cast(Callable[..., bytes], save_fn)
            # KR: save() 시그니처를 기준으로 packer 지원 여부를 판별해 내부 TypeError를 가리지 않도록 합니다.
            # EN: Detect packer support from save() signature so we don't swallow internal TypeError.
            try:
                supports_packer = "packer" in inspect.signature(typed_save).parameters
            except (TypeError, ValueError):
                supports_packer = False

            if packer is None or not supports_packer:
                return typed_save()
            return typed_save(packer=packer)

        def _validate_saved_file(saved_path: str) -> tuple[bool, str | None]:
            """KR: 저장 결과 파일이 Unity bundle로 다시 열리는지 검증합니다.
            EN: Validate saved output by attempting to reload from file path.
            """
            signature = source_bundle_signature or getattr(env_file, "signature", None)
            if signature not in bundle_signatures:
                return True, None
            saved_signature = _read_bundle_signature(saved_path, bundle_signatures)
            if saved_signature != signature:
                reason = (
                    f"번들 시그니처 불일치 (기대: {signature}, 결과: {saved_signature or 'None'})"
                    if lang == "ko"
                    else f"bundle signature mismatch (expected: {signature}, got: {saved_signature or 'None'})"
                )
                if lang == "ko":
                    _log_console(f"  저장 검증 실패: {reason}")
                else:
                    _log_console(f"  Save validation failed: {reason}")
                return False, reason
            try:
                if getattr(sys, "frozen", False):
                    cmd = [sys.executable, "--_validate-bundle", saved_path]
                else:
                    cmd = [
                        sys.executable,
                        os.path.abspath(__file__),
                        "--_validate-bundle",
                        saved_path,
                    ]
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=1800,
                )
                if proc.returncode == 0:
                    return True, None
                detail = (proc.stderr or proc.stdout or "").strip()
                reason = (
                    f"worker exit={proc.returncode}: {detail}"
                    if detail
                    else f"worker exit={proc.returncode}"
                )
                if lang == "ko":
                    _log_console(f"  저장 검증 실패 [{reason}]")
                else:
                    _log_console(f"  Save validation failed [{reason}]")
                return False, reason
            except Exception as e:
                reason = (
                    f"검증 워커 실행 실패: {e!r}"
                    if lang == "ko"
                    else f"failed to run validation worker: {e!r}"
                )
                if lang == "ko":
                    _log_console(f"  저장 검증 워커 실행 실패: {e!r}")
                else:
                    _log_console(f"  Failed to run save validation worker: {e!r}")
                return False, reason

        def _try_save(packer_label: Any, log_label: str) -> bool:
            """KR: 단일 저장 전략을 시도하고 성공 여부를 반환합니다.
            EN: Try one save strategy and return success status.
            """
            nonlocal save_success, last_save_failure_reason
            tmp_file = os.path.join(tmp_path, fn_without_path)
            has_save_to = callable(getattr(env_file, "save_to", None))
            saved_blob: bytes | None = None
            try:
                use_stream_fallback = False
                if has_save_to and source_bundle_signature in bundle_signatures:
                    # KR: 번들은 안정성을 위해 legacy save()를 우선 시도하고, 메모리 부족 시에만 save_to로 폴백합니다.
                    # EN: For bundles, prefer legacy save() for stability; fall back to save_to on MemoryError.
                    try:
                        saved_blob = _save_env_file(packer_label, use_save_to=False)
                    except MemoryError:
                        use_stream_fallback = True
                        if lang == "ko":
                            _log_console(
                                "  메모리 부족으로 스트리밍 저장(save_to)으로 폴백합니다..."
                            )
                        else:
                            _log_console(
                                "  Falling back to streaming save_to due to MemoryError..."
                            )

                    if not use_stream_fallback:
                        with open(tmp_file, "wb") as f:
                            f.write(cast(bytes, saved_blob))
                        saved_blob = None
                    else:
                        _save_env_file(
                            packer_label, save_path=tmp_file, use_save_to=True
                        )
                elif has_save_to:
                    # KR: save_to()로 파일에 직접 저장 — bytes 중간 변수 없음 (메모리 절약)
                    # EN: save_to() writes directly to file — no intermediate bytes blob (memory-efficient)
                    _save_env_file(packer_label, save_path=tmp_file, use_save_to=True)
                else:
                    # KR: 기존 bytes 반환 방식 폴백
                    # EN: Legacy bytes-returning fallback
                    saved_blob = _save_env_file(packer_label, use_save_to=False)
                    with open(tmp_file, "wb") as f:
                        f.write(cast(bytes, saved_blob))
                    # Release large in-memory blob before optional validation to lower peak memory.
                    saved_blob = None
                gc.collect()
                is_valid, validation_reason = _validate_saved_file(tmp_file)
                if not is_valid:
                    try:
                        saved_size = os.path.getsize(tmp_file)
                    except Exception:
                        saved_size = 0
                    if saved_size > 0:
                        if lang == "ko":
                            _log_console(
                                "  경고: 저장 검증에 실패했지만 무검증 저장으로 계속 진행합니다."
                            )
                            if validation_reason:
                                _log_console(f"  검증 실패 원인: {validation_reason}")
                        else:
                            _log_console(
                                "  Warning: save validation failed, continuing with unvalidated save."
                            )
                            if validation_reason:
                                _log_console(
                                    f"  Validation failure reason: {validation_reason}"
                                )
                        save_success = True
                        return True
                    last_save_failure_reason = (
                        validation_reason or "validation failed (empty output file)"
                    )
                    try:
                        if os.path.exists(tmp_file):
                            os.remove(tmp_file)
                    except Exception:
                        pass
                    return False
                save_success = True
                return True
            except Exception as e:
                last_save_failure_reason = (
                    f"method {log_label} [{type(e).__name__}]: {e!r}"
                )
                if lang == "ko":
                    _log_console(
                        f"  저장 방법 {log_label} 실패 [{type(e).__name__}]: {e!r}"
                    )
                else:
                    _log_console(
                        f"  Save method {log_label} failed [{type(e).__name__}]: {e!r}"
                    )
                if debug_parse_enabled():
                    tb_module.print_exc()
                try:
                    if os.path.exists(tmp_file):
                        os.remove(tmp_file)
                except Exception:
                    pass
                return False
            finally:
                saved_blob = None
                gc.collect()

        dataflags = getattr(env_file, "dataflags", None)
        safe_none_packer = (int(dataflags), 0) if dataflags is not None else "none"
        legacy_none_packer = (
            ((int(dataflags) & ~0x3F), 0) if dataflags is not None else None
        )

        if prefer_original_compress:
            # KR: 옵션이 있으면 원본 압축 우선으로 저장합니다.
            # EN: With option enabled, keep original compression as first choice.
            if not _try_save("original", "1"):
                if lang == "ko":
                    _log_console("  lz4 압축 모드로 재시도...")
                else:
                    _log_console("  Retrying with lz4 packer...")
                if not _try_save("lz4", "2"):
                    if lang == "ko":
                        _log_console("  비압축 계열 모드로 재시도...")
                    else:
                        _log_console("  Retrying with uncompressed-style packer...")
                    if (
                        not _try_save(safe_none_packer, "3")
                        and legacy_none_packer is not None
                    ):
                        if lang == "ko":
                            _log_console("  레거시 비트마스크 모드로 재시도...")
                        else:
                            _log_console("  Retrying with legacy bitmask packer...")
                        _try_save(legacy_none_packer, "4")
        else:
            # KR: 기본은 무압축 계열 우선으로 저장해 시간을 줄이고, 실패 시 압축 모드로 폴백합니다.
            # EN: Default prefers uncompressed-family save for speed, then falls back to compressed modes.
            if not _try_save(safe_none_packer, "1"):
                if legacy_none_packer is not None:
                    if lang == "ko":
                        _log_console("  레거시 비트마스크 무압축 모드로 재시도...")
                    else:
                        _log_console(
                            "  Retrying with legacy bitmask uncompressed packer..."
                        )
                    if _try_save(legacy_none_packer, "2"):
                        pass
                    else:
                        if lang == "ko":
                            _log_console("  원본 압축 모드로 재시도...")
                        else:
                            _log_console("  Retrying with original compression...")
                        if not _try_save("original", "3"):
                            if lang == "ko":
                                _log_console("  lz4 압축 모드로 재시도...")
                            else:
                                _log_console("  Retrying with lz4 packer...")
                            _try_save("lz4", "4")
                else:
                    if lang == "ko":
                        _log_console("  원본 압축 모드로 재시도...")
                    else:
                        _log_console("  Retrying with original compression...")
                    if not _try_save("original", "2"):
                        if lang == "ko":
                            _log_console("  lz4 압축 모드로 재시도...")
                        else:
                            _log_console("  Retrying with lz4 packer...")
                        _try_save("lz4", "3")

        close_unitypy_env(env)
        gc.collect()

        if save_success:
            saved_file_path = os.path.join(tmp_path, fn_without_path)
            if os.path.exists(saved_file_path):
                saved_size = os.path.getsize(saved_file_path)
                shutil.move(saved_file_path, assets_file)
                _log_debug(
                    f"[save] file={fn_without_path} output={assets_file} temp={saved_file_path} bytes={saved_size}"
                )
                if lang == "ko":
                    _log_console(f"  저장 완료 (크기: {saved_size} bytes)")
                else:
                    _log_console(f"  Save complete (size: {saved_size} bytes)")
            else:
                _log_debug(
                    f"[save] file={fn_without_path} output={assets_file} temp={saved_file_path} missing_after_save=True"
                )
                if lang == "ko":
                    _log_console("  경고: 저장된 파일을 찾을 수 없습니다")
                else:
                    _log_console("  Warning: saved file was not found")
                last_save_failure_reason = "saved file was not found after save phase"
                save_success = False

        if not save_success:
            _log_debug(
                f"[save] file={fn_without_path} output={assets_file} failed=True reason={last_save_failure_reason}"
            )
            if lang == "ko":
                _log_console("  오류: 파일 저장에 실패했습니다.")
                if last_save_failure_reason:
                    _log_console(f"  실패 원인: {last_save_failure_reason}")
            else:
                _log_console("  Error: failed to save file.")
                if last_save_failure_reason:
                    _log_console(f"  Failure reason: {last_save_failure_reason}")
    elif replace_sdf and target_sdf_targets and not preview_export:
        if lang == "ko":
            _log_console(
                f"  경고: SDF 대상 {len(target_sdf_targets)}건 중 매칭 {matched_sdf_targets}건, 적용 {patched_sdf_targets}건"
            )
            if sdf_parse_failure_reasons:
                _log_console(f"  파싱 오류: {sdf_parse_failure_reasons[-1]}")
        else:
            _log_console(
                f"  Warning: SDF targets={len(target_sdf_targets)}, matched={matched_sdf_targets}, patched={patched_sdf_targets}"
            )
            if sdf_parse_failure_reasons:
                _log_console(f"  Parse error: {sdf_parse_failure_reasons[-1]}")

    if os.path.exists(tmp_path):
        shutil.rmtree(tmp_path)
    if not using_custom_temp_root and os.path.isdir(tmp_root):
        try:
            os.rmdir(tmp_root)
        except OSError:
            pass

    return save_success if modified else False


def create_batch_replacements(
    game_path: str,
    font_name: str,
    replace_ttf: bool = True,
    replace_sdf: bool = True,
    target_files: set[str] | None = None,
    scan_jobs: int = 1,
    lang: Language = "ko",
    ps5_swizzle: bool = False,
) -> dict[str, JsonDict]:
    """KR: 게임 내 모든 폰트를 지정 폰트로 치환하는 배치 매핑을 생성합니다.
    KR: target_files가 있으면 해당 파일만 대상으로 매핑을 생성합니다.
    EN: Create batch replacement mapping for all fonts in a game.
    EN: If target_files is provided, build mapping only for those files.
    """
    fonts = scan_fonts(
        game_path,
        lang=lang,
        target_files=target_files,
        scan_jobs=scan_jobs,
        ps5_swizzle=ps5_swizzle,
    )
    replacements: dict[str, JsonDict] = {}

    if replace_ttf:
        for font in fonts["ttf"]:
            key = f"{font['file']}|TTF|{font['path_id']}"
            replacements[key] = {
                "Name": font["name"],
                "assets_name": font["assets_name"],
                "Path_ID": font["path_id"],
                "Type": "TTF",
                "File": font["file"],
                "Replace_to": font_name,
            }

    if replace_sdf:
        for font in fonts["sdf"]:
            key = f"{font['file']}|SDF|{font['path_id']}"
            if ps5_swizzle:
                swizzle_flag = (
                    "True" if parse_bool_flag(font.get("swizzle")) else "False"
                )
                process_swizzle_flag = (
                    "True" if parse_bool_flag(font.get("process_swizzle")) else "False"
                )
                entry: JsonDict = {
                    "File": font["file"],
                    "assets_name": font["assets_name"],
                    "Path_ID": font["path_id"],
                    "Type": "SDF",
                    "Name": font["name"],
                    "swizzle": swizzle_flag,
                    "process_swizzle": process_swizzle_flag,
                    "Replace_to": font_name,
                }
            else:
                entry = {
                    "File": font["file"],
                    "assets_name": font["assets_name"],
                    "Path_ID": font["path_id"],
                    "Type": "SDF",
                    "Name": font["name"],
                    "Replace_to": font_name,
                }
            replacements[key] = entry

    return replacements


def create_preview_export_targets(
    game_path: str,
    target_files: set[str] | None = None,
    scan_jobs: int = 1,
    lang: Language = "ko",
    ps5_swizzle: bool = False,
) -> dict[str, JsonDict]:
    """KR: preview-export 전용 SDF 대상 매핑(Replace_to 비어 있음)을 생성합니다.
    KR: scan_jobs/target_files 조건을 그대로 반영합니다.
    EN: Build preview-export-only SDF mapping (Replace_to left empty).
    EN: scan_jobs/target_files are applied as-is.
    """
    fonts = scan_fonts(
        game_path,
        lang=lang,
        target_files=target_files,
        scan_jobs=scan_jobs,
        ps5_swizzle=ps5_swizzle,
    )
    targets: dict[str, JsonDict] = {}
    for font in fonts["sdf"]:
        key = f"{font['file']}|PREVIEW|{font['path_id']}"
        entry: JsonDict = {
            "File": font["file"],
            "assets_name": font["assets_name"],
            "Path_ID": font["path_id"],
            "Type": "SDF",
            "Name": font["name"],
            "Replace_to": "",
        }
        if ps5_swizzle:
            entry["swizzle"] = (
                "True" if parse_bool_flag(font.get("swizzle")) else "False"
            )
            entry["process_swizzle"] = (
                "True" if parse_bool_flag(font.get("process_swizzle")) else "False"
            )
        targets[key] = entry
    return targets


def exit_with_error(message: str, lang: Language = "ko") -> NoReturn:
    """KR: 로컬라이즈된 오류 메시지를 출력하고 종료합니다.
    EN: Print localized error message and terminate the process.
    """
    if lang == "ko":
        _log_console(f"오류: {message}")
    else:
        _log_console(f"Error: {message}")
    if lang == "ko":
        input("\n엔터를 눌러 종료...")
    else:
        input("\nPress Enter to exit...")
    sys.exit(1)


def exit_with_error_en(message: str) -> NoReturn:
    """KR: 영문 오류 메시지를 출력하고 종료합니다.
    EN: Print English error message and terminate the process.
    """
    exit_with_error(message, lang="en")


def run_validation_worker(bundle_path: str, lang: Language = "ko") -> int:
    """KR: 저장 검증 전용 워커입니다. bundle_path를 UnityPy로 로드해 성공/실패 코드만 반환합니다.
    EN: Validation worker that loads bundle_path with UnityPy and returns a status code.
    """
    try:
        if not os.path.exists(bundle_path):
            if lang == "ko":
                _log_console("[validate] 검증 실패: 저장 파일이 존재하지 않습니다.")
            else:
                _log_console("[validate] Validation failed: saved file does not exist.")
            return 2

        env = UnityPy.load(bundle_path)
        files = getattr(env, "files", None)
        if not isinstance(files, dict) or len(files) == 0:
            if lang == "ko":
                _log_console(
                    "[validate] 검증 실패: UnityPy.load 결과에 파일이 없습니다."
                )
            else:
                _log_console(
                    "[validate] Validation failed: UnityPy.load returned no files."
                )
            return 2

        # KR: 실제 오브젝트가 없으면 저장 결과가 비정상일 가능성이 높습니다.
        # EN: Empty object list usually indicates an invalid or incomplete save result.
        if not getattr(env, "objects", None):
            if lang == "ko":
                _log_console("[validate] 검증 실패: 로드된 오브젝트가 없습니다.")
            else:
                _log_console(
                    "[validate] Validation failed: loaded object list is empty."
                )
            return 2

        return 0
    except Exception as e:
        if lang == "ko":
            _log_console(f"[validate] 검증 실패: {e!r}")
        else:
            _log_console(f"[validate] Validation failed: {e!r}")
        if debug_parse_enabled():
            tb_module.print_exc()
        return 2


def run_scan_file_worker(
    game_path: str,
    assets_file: str,
    output_path: str,
    lang: Language = "ko",
    detect_ps5_swizzle: bool = False,
) -> int:
    """KR: 단일 파일 파싱 워커입니다. 결과를 JSON 파일로 저장합니다.
    EN: Single-file scan worker. Writes results to a JSON file.
    """
    try:
        game_path, data_path = resolve_game_path(game_path, lang=lang)
        unity_version = get_unity_version(game_path, lang=lang)
        compile_method = get_compile_method(data_path)
        generator = _create_generator(
            unity_version, game_path, data_path, compile_method, lang=lang
        )
        scanned, load_error = _scan_fonts_in_asset_file(
            assets_file,
            generator,
            lang=lang,
            detect_ps5_swizzle=detect_ps5_swizzle,
        )
        payload: JsonDict = {
            "ttf": scanned.get("ttf", []),
            "sdf": scanned.get("sdf", []),
            "error": load_error,
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return 0
    except Exception as e:
        if lang == "ko":
            _log_console(f"[scan_worker] 실패: {e!r}")
        else:
            _log_console(f"[scan_worker] failed: {e!r}")
        if debug_parse_enabled():
            tb_module.print_exc()
        return 2


def main_cli(lang: Language = "ko") -> None:
    """KR: 언어별 공통 CLI 진입점입니다.
    EN: Shared CLI entrypoint parameterized by language.
    """
    is_ko = lang == "ko"

    if is_ko:
        description = "Unity 게임의 폰트를 한글 폰트로 교체합니다."
        epilog = """
예시:
  %(prog)s --gamepath "C:/path/to/game" --parse
  %(prog)s --gamepath "C:/path/to/game" --preview-export
  %(prog)s --gamepath "C:/path/to/game" --mulmaru
  %(prog)s --gamepath "C:/path/to/game" --nanumgothic --sdfonly
  %(prog)s --gamepath "C:/path/to/game" --list font_map.json
        """
        gamepath_help = "게임의 루트 경로 (예: C:/path/to/game)"
        parse_help = "폰트 정보를 JSON으로 출력"
        mulmaru_help = "모든 폰트를 Mulmaru로 일괄 교체"
        nanum_help = "모든 폰트를 NanumGothic으로 일괄 교체"
        sdf_help = "SDF 폰트만 교체"
        ttf_help = "TTF 폰트만 교체"
        list_help = "JSON 파일을 읽어서 폰트 교체"
        target_file_help = "지정한 파일명만 교체 대상에 포함 (여러 번 사용 가능)"
        game_mat_help = "SDF 교체 시 게임 원본 Material 파라미터를 유지 (기본: 교체 Material 보정 적용)"
        game_line_metrics_help = "SDF 교체 시 게임 원본 줄 간격 메트릭 사용 (기본: 교체 폰트 메트릭 보정 적용)"
        original_compress_help = (
            "저장 시 원본 압축 모드를 우선 사용 (기본: 무압축 계열 우선)"
        )
        temp_dir_help = "임시 저장 폴더 루트 경로 (가능하면 빠른 SSD/NVMe 권장)"
        output_only_help = (
            "원본 파일은 유지하고, 수정된 파일만 지정 폴더에 원본 상대 경로로 저장"
        )
        preview_help = "모든 SDF 폰트 Atlas/Glyph crop 미리보기를 preview 폴더에 저장 (--ps5-swizzle와 함께면 unswizzle 기준)"
        scan_jobs_help = "폰트 스캔 병렬 워커 수 (기본: 1, parse/일괄교체 스캔에 적용, 별칭: --max-workers)"
        split_save_force_help = (
            "대형 SDF 다건 교체에서 one-shot을 건너뛰고 SDF 1개씩 강제 분할 저장"
        )
        oneshot_save_force_help = (
            "대형 SDF 다건 교체에서도 분할 저장 폴백 없이 one-shot 저장만 시도"
        )
        ps5_swizzle_help = "PS5 swizzle 자동 판별/변환 모드 (mask_x=0x385F0, mask_y=0x07A0F, rotate=90 보정)"
        verbose_help = "콘솔 로그는 유지하고, 상세 DEBUG 로그(파일/폰트/경로/버전)를 verbose.txt에 저장"
    else:
        description = "Replace Unity game fonts with Korean fonts."
        epilog = """
Examples:
  %(prog)s --gamepath "C:/path/to/game" --parse
  %(prog)s --gamepath "C:/path/to/game" --preview-export
  %(prog)s --gamepath "C:/path/to/game" --mulmaru
  %(prog)s --gamepath "C:/path/to/game" --nanumgothic --sdfonly
  %(prog)s --gamepath "C:/path/to/game" --list font_map.json
        """
        gamepath_help = "Game root path (e.g. C:/path/to/game)"
        parse_help = "Export font info to JSON"
        mulmaru_help = "Replace all fonts with Mulmaru"
        nanum_help = "Replace all fonts with NanumGothic"
        sdf_help = "Replace SDF fonts only"
        ttf_help = "Replace TTF fonts only"
        list_help = "Replace fonts using a JSON file"
        target_file_help = (
            "Limit replacement targets to specific file name(s) (repeatable)"
        )
        game_mat_help = "Use original in-game Material parameters for SDF replacement (default: adjusted replacement material)"
        game_line_metrics_help = "Use original in-game line metrics for SDF replacement (default: adjusted replacement font metrics)"
        original_compress_help = "Prefer original compression mode on save (default: uncompressed-family first)"
        temp_dir_help = "Root path for temporary save files (fast SSD/NVMe recommended)"
        output_only_help = "Keep originals untouched and write modified files only to this folder (preserve relative paths)"
        preview_help = "Export preview PNGs (Atlas + glyph crops) for all SDF fonts into preview folder (unswizzled when used with --ps5-swizzle)"
        scan_jobs_help = "Number of parallel scan workers (default: 1, used for parse/bulk scan paths, alias: --max-workers)"
        split_save_force_help = "Skip one-shot and force one-by-one SDF split save for large multi-SDF replacements"
        oneshot_save_force_help = "Force one-shot save even for large multi-SDF targets (disable split-save fallback)"
        ps5_swizzle_help = "Enable PS5 swizzle detect/transform mode (mask_x=0x385F0, mask_y=0x07A0F, rotate=90 compensation)"
        verbose_help = "Keep concise console logs and save detailed DEBUG logs (file/font/path/version) to verbose.txt"

    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    parser.add_argument("--gamepath", type=str, help=gamepath_help)
    parser.add_argument("--parse", action="store_true", help=parse_help)
    parser.add_argument("--mulmaru", action="store_true", help=mulmaru_help)
    parser.add_argument("--nanumgothic", action="store_true", help=nanum_help)
    parser.add_argument("--sdfonly", action="store_true", help=sdf_help)
    parser.add_argument("--ttfonly", action="store_true", help=ttf_help)
    parser.add_argument("--list", type=str, metavar="JSON_FILE", help=list_help)
    parser.add_argument(
        "--target-file", action="append", metavar="FILE_NAME", help=target_file_help
    )
    parser.add_argument("--use-game-material", action="store_true", help=game_mat_help)
    parser.add_argument("--use-game-mat", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--use-game-line-metrics", action="store_true", help=game_line_metrics_help
    )
    parser.add_argument(
        "--use-game-line-matrics", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--material-scale-by-padding", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--original-compress", action="store_true", help=original_compress_help
    )
    parser.add_argument("--temp-dir", type=str, metavar="PATH", help=temp_dir_help)
    parser.add_argument(
        "--output-only", type=str, metavar="PATH", help=output_only_help
    )
    parser.add_argument("--preview-export", action="store_true", help=preview_help)
    parser.add_argument("--preview", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--scan-jobs",
        "--max-workers",
        dest="scan_jobs",
        type=int,
        default=1,
        metavar="N",
        help=scan_jobs_help,
    )
    parser.add_argument(
        "--split-save-force", action="store_true", help=split_save_force_help
    )
    parser.add_argument(
        "--oneshot-save-force", action="store_true", help=oneshot_save_force_help
    )
    parser.add_argument("--ps5-swizzle", action="store_true", help=ps5_swizzle_help)
    parser.add_argument("--verbose", action="store_true", help=verbose_help)
    parser.add_argument(
        "--_validate-bundle", type=str, metavar="BUNDLE_PATH", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--_scan-file-worker",
        type=str,
        metavar="ASSET_FILE_PATH",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_scan-file-worker-output",
        type=str,
        metavar="OUTPUT_JSON_PATH",
        help=argparse.SUPPRESS,
    )

    args = parser.parse_args()
    if isinstance(args.gamepath, str):
        args.gamepath = strip_wrapping_quotes_repeated(args.gamepath)
    if isinstance(args.list, str):
        args.list = strip_wrapping_quotes_repeated(args.list)
    if isinstance(args.output_only, str):
        args.output_only = strip_wrapping_quotes_repeated(args.output_only)

    verbose_path: str | None = None
    if args.verbose:
        verbose_path = os.path.join(get_script_dir(), VERBOSE_LOG_FILENAME)
    _configure_logging(
        console_level=logging.INFO,
        verbose_log_path=verbose_path,
    )
    if verbose_path:
        if is_ko:
            _log_info(f"[verbose] 상세 로그를 '{verbose_path}'에 저장합니다.")
        else:
            _log_info(f"[verbose] Writing detailed logs to '{verbose_path}'.")
    _log_debug(
        f"[runtime] cwd={os.getcwd()} script_dir={get_script_dir()} args={vars(args)}"
    )

    # KR: 이전 옵션(--use-game-mat) 호환을 위해 새 옵션에 병합합니다.
    # EN: Merge legacy flag (--use-game-mat) into the new option for compatibility.
    args.use_game_material = bool(
        getattr(args, "use_game_material", False)
        or getattr(args, "use_game_mat", False)
    )
    # KR: 오타/레거시 옵션(--use-game-line-matrics)도 동일 동작으로 병합합니다.
    # EN: Merge typo/legacy option (--use-game-line-matrics) into the canonical flag.
    args.use_game_line_metrics = bool(
        getattr(args, "use_game_line_metrics", False)
        or getattr(args, "use_game_line_matrics", False)
    )
    # KR: 레거시 옵션(--preview)도 새 옵션(--preview-export)으로 병합합니다.
    # EN: Merge legacy --preview into the canonical --preview-export flag.
    args.preview_export = bool(
        getattr(args, "preview_export", False) or getattr(args, "preview", False)
    )
    selected_files = parse_target_files_arg(getattr(args, "target_file", None))
    if args.target_file and not selected_files:
        if is_ko:
            exit_with_error("--target-file 값이 비어 있습니다.", lang=lang)
        else:
            exit_with_error("--target-file values are empty.", lang=lang)

    if args.split_save_force and args.oneshot_save_force:
        if is_ko:
            exit_with_error(
                "--split-save-force와 --oneshot-save-force를 동시에 사용할 수 없습니다.",
                lang=lang,
            )
        else:
            exit_with_error(
                "Cannot use --split-save-force and --oneshot-save-force at the same time.",
                lang=lang,
            )

    # KR: 기본은 split-save 폴백을 활성화합니다.
    # EN: Split-save fallback is enabled by default.
    args.split_save = not args.oneshot_save_force
    if args.scan_jobs < 1:
        if is_ko:
            exit_with_error("--scan-jobs는 1 이상의 정수여야 합니다.", lang=lang)
        else:
            exit_with_error(
                "--scan-jobs must be an integer greater than or equal to 1.", lang=lang
            )

    if args._scan_file_worker:
        if not args.gamepath:
            if is_ko:
                _log_console("[scan_worker] 오류: --gamepath가 필요합니다.")
            else:
                _log_console("[scan_worker] Error: --gamepath is required.")
            raise SystemExit(2)
        if not args._scan_file_worker_output:
            if is_ko:
                _log_console(
                    "[scan_worker] 오류: --_scan-file-worker-output 경로가 필요합니다."
                )
            else:
                _log_console(
                    "[scan_worker] Error: --_scan-file-worker-output path is required."
                )
            raise SystemExit(2)
        raise SystemExit(
            run_scan_file_worker(
                args.gamepath,
                args._scan_file_worker,
                args._scan_file_worker_output,
                lang=lang,
                detect_ps5_swizzle=args.ps5_swizzle,
            )
        )

    if args.temp_dir:
        args.temp_dir = os.path.abspath(str(args.temp_dir))
        try:
            os.makedirs(args.temp_dir, exist_ok=True)
        except Exception as e:
            if is_ko:
                exit_with_error(
                    f"임시 폴더를 만들 수 없습니다: {args.temp_dir} ({e})", lang=lang
                )
            else:
                exit_with_error(
                    f"Failed to create temp directory: {args.temp_dir} ({e})", lang=lang
                )
        if is_ko:
            _log_console(f"임시 저장 경로: {args.temp_dir}")
        else:
            _log_console(f"Temp save path: {args.temp_dir}")
        register_temp_dir_for_cleanup(
            os.path.join(args.temp_dir, "unity_font_replacer_temp")
        )

    output_only_root: str | None = None
    if args.output_only:
        output_only_root = os.path.abspath(str(args.output_only))
        try:
            os.makedirs(output_only_root, exist_ok=True)
        except Exception as e:
            if is_ko:
                exit_with_error(
                    f"출력 폴더를 만들 수 없습니다: {output_only_root} ({e})", lang=lang
                )
            else:
                exit_with_error(
                    f"Failed to create output folder: {output_only_root} ({e})",
                    lang=lang,
                )
        if is_ko:
            _log_console(
                f"출력 전용 모드: 수정 파일을 '{output_only_root}'에 저장합니다."
            )
        else:
            _log_console(
                f"Output-only mode: writing modified files to '{output_only_root}'."
            )

    preview_root: str | None = None
    if args.preview_export:
        preview_root = os.path.join(get_script_dir(), "preview")
        try:
            os.makedirs(preview_root, exist_ok=True)
        except Exception as e:
            if is_ko:
                exit_with_error(
                    f"preview 폴더를 만들 수 없습니다: {preview_root} ({e})", lang=lang
                )
            else:
                exit_with_error(
                    f"Failed to create preview folder: {preview_root} ({e})", lang=lang
                )
        if is_ko:
            _log_console(f"Preview 모드: '{preview_root}'에 미리보기를 저장합니다.")
        else:
            _log_console(f"Preview mode: saving previews to '{preview_root}'.")
        if args.ps5_swizzle:
            if is_ko:
                _log_console(
                    "  PS5 swizzle 활성화: preview를 unswizzle 기준으로 저장합니다."
                )
            else:
                _log_console(
                    "  PS5 swizzle enabled: saving previews in unswizzled view."
                )

    if args.use_game_line_metrics:
        if is_ko:
            _log_console("줄 간격 메트릭 모드: 게임 원본 줄 간격 메트릭을 사용합니다.")
        else:
            _log_console("Line metrics mode: using original in-game line metrics.")
    else:
        if is_ko:
            _log_console(
                "줄 간격 메트릭 모드: 교체 폰트 메트릭 보정을 기본 적용합니다."
            )
        else:
            _log_console(
                "Line metrics mode: using adjusted replacement font metrics by default."
            )

    if args.use_game_material:
        if is_ko:
            _log_console("Material 모드: 게임 원본 Material 파라미터를 사용합니다.")
        else:
            _log_console("Material mode: using original in-game Material parameters.")
    else:
        if is_ko:
            _log_console(
                "Material 모드: 교체 Material 보정(패딩 비율)을 기본 적용합니다."
            )
        else:
            _log_console(
                "Material mode: using adjusted replacement material by default (padding ratio)."
            )
    if args.ps5_swizzle:
        if is_ko:
            _log_console(
                "PS5 swizzle 모드: 대상 Atlas swizzle을 자동 판별해 교체 Atlas를 변환합니다 "
                f"(마스크는 텍스처 크기에 따라 자동 계산, rotate={PS5_SWIZZLE_ROTATE})."
            )
        else:
            _log_console(
                "PS5 swizzle mode: auto-detecting target atlas swizzle state and transforming replacement atlas "
                f"(masks computed per texture size, rotate={PS5_SWIZZLE_ROTATE})."
            )
    else:
        if is_ko:
            _log_console("PS5 swizzle 모드: 비활성화")
        else:
            _log_console("PS5 swizzle mode: disabled")

    if args._validate_bundle:
        raise SystemExit(run_validation_worker(args._validate_bundle, lang=lang))

    import struct

    py_bits = struct.calcsize("P") * 8
    _log_console(f"Python {sys.version} ({py_bits}-bit)")

    warn_unitypy_version(lang=lang)

    input_path = strip_wrapping_quotes_repeated(args.gamepath) if args.gamepath else ""
    _log_debug(f"[runtime] requested_gamepath={input_path!r}")
    if not input_path:
        while True:
            if is_ko:
                entered_path = input("게임 경로를 입력하세요: ").strip()
            else:
                entered_path = input("Enter game path: ").strip()
            input_path = strip_wrapping_quotes_repeated(entered_path)
            if not input_path:
                if is_ko:
                    _log_console("게임 경로가 필요합니다. 다시 입력해주세요.")
                else:
                    _log_console("Game path is required. Please try again.")
                continue
            if not os.path.isdir(input_path):
                if is_ko:
                    _log_console(
                        f"'{input_path}'는 유효한 디렉토리가 아닙니다. 다시 입력해주세요."
                    )
                else:
                    _log_console(
                        f"'{input_path}' is not a valid directory. Please try again."
                    )
                continue
            try:
                game_path, data_path = resolve_game_path(input_path, lang=lang)
            except FileNotFoundError as e:
                if is_ko:
                    _log_console(f"{e}\n다시 입력해주세요.")
                else:
                    _log_console(f"{e}\nPlease try again.")
                continue
            break
    else:
        if not os.path.isdir(input_path):
            if is_ko:
                exit_with_error(
                    f"'{input_path}'는 유효한 디렉토리가 아닙니다.", lang=lang
                )
            else:
                exit_with_error(f"'{input_path}' is not a valid directory.", lang=lang)
        try:
            game_path, data_path = resolve_game_path(input_path, lang=lang)
        except FileNotFoundError as e:
            exit_with_error(str(e), lang=lang)

    compile_method = get_compile_method(data_path)
    if is_ko:
        _log_console(f"게임 경로: {game_path}")
        _log_console(f"데이터 경로: {data_path}")
        _log_console(f"컴파일 방식: {compile_method}")
        _log_console(f"스캔 워커 수: {args.scan_jobs}")
    else:
        _log_console(f"Game path: {game_path}")
        _log_console(f"Data path: {data_path}")
        _log_console(f"Compile method: {compile_method}")
        _log_console(f"Scan workers: {args.scan_jobs}")
    _log_debug(
        f"[runtime] input_path={input_path} game_path={game_path} data_path={data_path} "
        f"compile_method={compile_method} scan_jobs={args.scan_jobs} "
        f"ps5_swizzle={args.ps5_swizzle} preview_export={args.preview_export}"
    )
    detected_unity_version = get_unity_version(game_path, lang=lang)
    _log_debug(f"[runtime] unity_version={detected_unity_version}")

    if selected_files:
        target_text = ", ".join(sorted(selected_files))
        if is_ko:
            _log_console(f"--target-file 적용: {target_text}")
        else:
            _log_console(f"Applied --target-file: {target_text}")
        _log_debug(f"[runtime] target_files={target_text}")

    default_temp_root = register_temp_dir_for_cleanup(os.path.join(data_path, "temp"))
    if os.path.exists(default_temp_root):
        shutil.rmtree(default_temp_root)

    replace_ttf = not args.sdfonly
    replace_sdf = not args.ttfonly
    if args.sdfonly and args.ttfonly:
        if is_ko:
            exit_with_error(
                "--sdfonly와 --ttfonly를 동시에 사용할 수 없습니다.", lang=lang
            )
        else:
            exit_with_error(
                "Cannot use --sdfonly and --ttfonly at the same time.", lang=lang
            )

    replacements: dict[str, JsonDict] | None = None
    mode: str | None = None
    interactive_session = False
    if args.parse:
        mode = "parse"
    elif args.mulmaru:
        mode = "mulmaru"
    elif args.nanumgothic:
        mode = "nanumgothic"
    elif args.list:
        mode = "list"
    elif args.preview_export:
        mode = "preview_export"
    else:
        interactive_session = True
        if is_ko:
            while True:
                _log_console("작업을 선택하세요:")
                _log_console("  1. 폰트 정보 추출 (JSON 파일 생성)")
                _log_console("  2. JSON 파일로 폰트 교체")
                _log_console("  3. Mulmaru(물마루체)로 일괄 교체")
                _log_console("  4. NanumGothic(나눔고딕)으로 일괄 교체")
                _log_console()
                choice = input("선택 (1-4): ").strip()
                if choice in {"1", "2", "3", "4"}:
                    break
                _log_console("잘못된 선택입니다. 다시 입력해주세요.")
        else:
            while True:
                _log_console("Select a task:")
                _log_console("  1. Export font info (create JSON)")
                _log_console("  2. Replace fonts using JSON")
                _log_console("  3. Bulk replace with Mulmaru")
                _log_console("  4. Bulk replace with NanumGothic")
                _log_console()
                choice = input("Choose (1-4): ").strip()
                if choice in {"1", "2", "3", "4"}:
                    break
                _log_console("Invalid selection. Please try again.")

        if choice == "1":
            mode = "parse"
        elif choice == "2":
            mode = "list"
            while True:
                if is_ko:
                    entered = input("JSON 파일 경로를 입력하세요: ").strip()
                else:
                    entered = input("Enter JSON file path: ").strip()
                entered = strip_wrapping_quotes_repeated(entered)
                if not entered:
                    if is_ko:
                        _log_console("JSON 파일 경로가 필요합니다. 다시 입력해주세요.")
                    else:
                        _log_console("JSON file path is required. Please try again.")
                    continue
                if os.path.exists(entered):
                    args.list = entered
                    break
                if is_ko:
                    _log_console(f"파일을 찾을 수 없습니다: '{entered}'")
                else:
                    _log_console(f"File not found: '{entered}'")
        elif choice == "3":
            mode = "mulmaru"
        elif choice == "4":
            mode = "nanumgothic"
    _log_debug(
        f"[runtime] mode={mode} interactive={interactive_session} "
        f"replace_ttf={replace_ttf} replace_sdf={replace_sdf}"
    )

    if compile_method == "Il2cpp" and not os.path.exists(
        os.path.join(data_path, "Managed")
    ):
        binary_path = os.path.join(game_path, "GameAssembly.dll")
        metadata_path = os.path.join(
            data_path, "il2cpp_data", "Metadata", "global-metadata.dat"
        )
        if not os.path.exists(binary_path) or not os.path.exists(metadata_path):
            if is_ko:
                exit_with_error(
                    "Il2cpp 게임의 경우 'Managed' 폴더 또는 'GameAssembly.dll'과 'global-metadata.dat' 파일이 필요합니다.\n올바른 Unity 게임 폴더인지 확인해주세요.",
                    lang=lang,
                )
            else:
                exit_with_error(
                    "For Il2cpp games, the 'Managed' folder or 'GameAssembly.dll' and 'global-metadata.dat' files are required.\nPlease check that this is a valid Unity game folder.",
                    lang=lang,
                )

        dumper_path = os.path.join(get_script_dir(), "Il2CppDumper", "Il2CppDumper.exe")
        target_path = os.path.join(data_path, "Managed_")
        os.makedirs(target_path, exist_ok=True)
        command = [
            os.path.abspath(dumper_path),
            os.path.abspath(binary_path),
            os.path.abspath(metadata_path),
            os.path.abspath(target_path),
        ]
        if is_ko:
            _log_console("Il2cpp 게임을 위한 Managed 폴더를 생성합니다...")
        else:
            _log_console("Creating Managed folder for Il2cpp game...")
        _log_console(os.path.abspath(target_path))

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                startupinfo=startupinfo,
                encoding="utf-8",
            )
            if process.returncode == 0:
                _log_console(process.stdout)
                shutil.move(
                    os.path.join(data_path, "Managed_", "DummyDll"),
                    os.path.join(data_path, "Managed"),
                )
                shutil.rmtree(os.path.join(data_path, "Managed_"))
                if is_ko:
                    _log_console("더미 DLL 생성에 성공했습니다!")
                else:
                    _log_console("Dummy DLL generated successfully!")
                compile_method = get_compile_method(data_path)
                if is_ko:
                    _log_console(f"컴파일 방식 재감지: {compile_method}")
                else:
                    _log_console(f"Compile method re-detected: {compile_method}")
            else:
                _log_console(process.stderr)
                if is_ko:
                    exit_with_error("Il2cpp 더미 DLL 생성 실패", lang=lang)
                else:
                    exit_with_error("Failed to generate Il2cpp dummy DLL", lang=lang)
        except Exception as e:
            if is_ko:
                exit_with_error(f"Il2CppDumper 실행 중 예외 발생: {e}", lang=lang)
            else:
                exit_with_error(f"Exception while running Il2CppDumper: {e}", lang=lang)

    if mode == "parse":
        parse_fonts(
            game_path,
            lang=lang,
            target_files=selected_files if selected_files else None,
            scan_jobs=args.scan_jobs,
            ps5_swizzle=args.ps5_swizzle,
        )
        if is_ko:
            input("\n엔터를 눌러 종료...")
        else:
            input("\nPress Enter to exit...")
        return

    if mode == "preview_export":
        if is_ko:
            _log_console(
                "Preview export 모드: 모든 SDF 폰트 Atlas/Glyph crop 미리보기를 추출합니다..."
            )
        else:
            _log_console(
                "Preview export mode: exporting Atlas/Glyph crop previews for all SDF fonts..."
            )
        replacements = create_preview_export_targets(
            game_path,
            target_files=selected_files if selected_files else None,
            scan_jobs=args.scan_jobs,
            lang=lang,
            ps5_swizzle=args.ps5_swizzle,
        )
        if not replacements:
            if is_ko:
                _log_console("Preview 대상 SDF 폰트를 찾지 못했습니다.")
                input("\n엔터를 눌러 종료...")
            else:
                _log_console("No SDF fonts found for preview export.")
                input("\nPress Enter to exit...")
            return
        if is_ko:
            _log_console(f"Preview 대상 SDF 폰트: {len(replacements)}개")
        else:
            _log_console(f"Preview target SDF fonts: {len(replacements)}")
    elif mode == "mulmaru":
        if is_ko:
            _log_console("Mulmaru 폰트로 일괄 교체합니다...")
        else:
            _log_console("Bulk replacing with Mulmaru...")
        replacements = create_batch_replacements(
            game_path,
            "Mulmaru",
            replace_ttf,
            replace_sdf,
            target_files=selected_files if selected_files else None,
            scan_jobs=args.scan_jobs,
            lang=lang,
            ps5_swizzle=args.ps5_swizzle,
        )
        ttf_count = sum(1 for v in replacements.values() if v["Type"] == "TTF")
        sdf_count = sum(1 for v in replacements.values() if v["Type"] == "SDF")
        if is_ko:
            _log_console(f"발견된 폰트: TTF {ttf_count}개, SDF {sdf_count}개")
        else:
            _log_console(f"Found fonts: TTF {ttf_count}, SDF {sdf_count}")
    elif mode == "nanumgothic":
        if is_ko:
            _log_console("NanumGothic 폰트로 일괄 교체합니다...")
        else:
            _log_console("Bulk replacing with NanumGothic...")
        replacements = create_batch_replacements(
            game_path,
            "NanumGothic",
            replace_ttf,
            replace_sdf,
            target_files=selected_files if selected_files else None,
            scan_jobs=args.scan_jobs,
            lang=lang,
            ps5_swizzle=args.ps5_swizzle,
        )
        ttf_count = sum(1 for v in replacements.values() if v["Type"] == "TTF")
        sdf_count = sum(1 for v in replacements.values() if v["Type"] == "SDF")
        if is_ko:
            _log_console(f"발견된 폰트: TTF {ttf_count}개, SDF {sdf_count}개")
        else:
            _log_console(f"Found fonts: TTF {ttf_count}, SDF {sdf_count}")
    elif mode == "list":
        if isinstance(args.list, str):
            args.list = strip_wrapping_quotes_repeated(args.list)

        if interactive_session:
            while not args.list or not os.path.exists(args.list):
                if args.list:
                    if is_ko:
                        _log_console(f"'{args.list}' 파일을 찾을 수 없습니다.")
                    else:
                        _log_console(f"File not found: '{args.list}'")
                if is_ko:
                    entered = input("JSON 파일 경로를 다시 입력하세요: ").strip()
                else:
                    entered = input("Re-enter JSON file path: ").strip()
                args.list = strip_wrapping_quotes_repeated(entered)

        if not args.list or not os.path.exists(args.list):
            if is_ko:
                exit_with_error(f"'{args.list}' 파일을 찾을 수 없습니다.", lang=lang)
            else:
                exit_with_error(f"File not found: '{args.list}'", lang=lang)

        if is_ko:
            _log_console(f"'{args.list}' 파일을 읽어서 교체합니다...")
        else:
            _log_console(f"Replacing using '{args.list}'...")
        with open(args.list, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            if is_ko:
                exit_with_error("JSON 루트는 객체(dict)여야 합니다.", lang=lang)
            else:
                exit_with_error("JSON root must be an object (dict).", lang=lang)
        replacements = cast(dict[str, JsonDict], loaded)

    if replacements is None:
        if is_ko:
            exit_with_error("교체 정보가 생성되지 않았습니다.", lang=lang)
        else:
            exit_with_error("Replacement mapping was not generated.", lang=lang)

    if selected_files:
        replacements = {
            key: value
            for key, value in replacements.items()
            if isinstance(value, dict)
            and os.path.basename(str(value.get("File", ""))) in selected_files
        }

        if not replacements:
            target_text = ", ".join(sorted(selected_files))
            if is_ko:
                exit_with_error(
                    f"--target-file 조건에 맞는 교체 대상이 없습니다: {target_text}",
                    lang=lang,
                )
            else:
                exit_with_error(
                    f"No replacement targets matched --target-file: {target_text}",
                    lang=lang,
                )

    unity_version = detected_unity_version
    generator = _create_generator(
        unity_version, game_path, data_path, compile_method, lang=lang
    )
    replacement_lookup, files_to_process = build_replacement_lookup(replacements)
    _log_debug(
        f"[runtime] replacement_entries={len(replacements)} "
        f"lookup_entries={len(replacement_lookup)} files_to_process={len(files_to_process)}"
    )
    preview_files_to_process: set[str] = set()
    if args.preview_export:
        preview_files_to_process = {
            os.path.basename(str(value.get("File", "")))
            for value in replacements.values()
            if isinstance(value, dict) and str(value.get("Type", "")) == "SDF"
        }
        preview_files_to_process.discard("")
    process_files = set(files_to_process) | preview_files_to_process
    _log_debug(
        f"[runtime] process_files={len(process_files)} "
        f"preview_only_files={len(preview_files_to_process)}"
    )
    assets_files = find_assets_files(
        game_path,
        lang=lang,
        target_files=process_files if process_files else None,
    )
    _log_debug(f"[runtime] matched_asset_files={len(assets_files)}")

    modified_count = 0
    for assets_file in assets_files:
        fn = os.path.basename(assets_file)
        if fn in process_files:
            working_assets_file = assets_file
            if output_only_root and mode != "preview_export":
                working_assets_file = resolve_output_only_path(
                    assets_file, data_path, output_only_root
                )
                working_dir = os.path.dirname(working_assets_file)
                if working_dir and not os.path.exists(working_dir):
                    os.makedirs(working_dir, exist_ok=True)
                shutil.copy2(assets_file, working_assets_file)
                if is_ko:
                    rel_out = os.path.relpath(working_assets_file, output_only_root)
                    _log_console(f"  출력 대상 준비: {rel_out}")
                else:
                    rel_out = os.path.relpath(working_assets_file, output_only_root)
                    _log_console(f"  Prepared output target: {rel_out}")
            if is_ko:
                _log_console(f"\n처리 중: {fn}")
            else:
                _log_console(f"\nProcessing: {fn}")
            # KR: 기본은 split-save 폴백을 사용하고, --oneshot-save-force일 때만 비활성화합니다.
            # EN: Split-save fallback is enabled by default and disabled only by --oneshot-save-force.
            file_replacements = {
                key: value
                for key, value in replacements.items()
                if isinstance(value, dict)
                and value.get("File") == fn
                and value.get("Replace_to")
            }
            file_ttf_replacements = {
                key: value
                for key, value in file_replacements.items()
                if value.get("Type") == "TTF"
            }
            file_sdf_replacements = {
                key: value
                for key, value in file_replacements.items()
                if value.get("Type") == "SDF"
            }
            _log_replacement_plan_details(fn, file_replacements)

            file_modified = False
            use_split_sdf_save = (
                args.split_save and replace_sdf and len(file_sdf_replacements) > 1
            )

            if use_split_sdf_save:
                if is_ko:
                    _log_console(
                        f"  SDF 대상 {len(file_sdf_replacements)}건: one-shot 실패 시 적응형 분할 저장으로 폴백합니다..."
                    )
                else:
                    _log_console(
                        f"  {len(file_sdf_replacements)} SDF targets: will fall back to adaptive split save if one-shot fails..."
                    )

                # KR: 먼저 한 번에 저장을 시도하고, 실패 시에만 적응형 분할 저장으로 폴백합니다.
                # EN: Try one-shot save first, then fall back to adaptive split save on failure.
                file_lookup, _ = build_replacement_lookup(file_replacements)
                one_shot_ok = False
                if args.split_save_force:
                    if is_ko:
                        _log_console(
                            "  --split-save-force 활성화: one-shot을 건너뛰고 SDF 1개씩 강제 분할 저장을 시작합니다..."
                        )
                    else:
                        _log_console(
                            "  --split-save-force enabled: skipping one-shot and forcing one-by-one SDF split save..."
                        )
                else:
                    try:
                        one_shot_ok = replace_fonts_in_file(
                            unity_version,
                            game_path,
                            working_assets_file,
                            file_replacements,
                            replace_ttf=replace_ttf,
                            replace_sdf=replace_sdf,
                            use_game_mat=args.use_game_material,
                            use_game_line_metrics=args.use_game_line_metrics,
                            material_scale_by_padding=not args.use_game_material,
                            prefer_original_compress=args.original_compress,
                            temp_root_dir=args.temp_dir,
                            generator=generator,
                            replacement_lookup=file_lookup,
                            ps5_swizzle=args.ps5_swizzle,
                            preview_export=args.preview_export,
                            preview_root=preview_root,
                            lang=lang,
                        )
                    except MemoryError as e:
                        if is_ko:
                            _log_console(f"  one-shot 저장 실패 [MemoryError]: {e!r}")
                            _log_console("  적응형 분할 저장으로 폴백합니다...")
                        else:
                            _log_console(f"  One-shot save failed [MemoryError]: {e!r}")
                            _log_console("  Falling back to adaptive split save...")
                    except Exception as e:
                        if is_ko:
                            _log_console(
                                f"  one-shot 저장 실패 [{type(e).__name__}]: {e!r}"
                            )
                            _log_console("  적응형 분할 저장으로 폴백합니다...")
                        else:
                            _log_console(
                                f"  One-shot save failed [{type(e).__name__}]: {e!r}"
                            )
                            _log_console("  Falling back to adaptive split save...")

                if one_shot_ok:
                    file_modified = True
                else:
                    split_stopped = False
                    if replace_ttf and file_ttf_replacements:
                        file_ttf_lookup, _ = build_replacement_lookup(
                            file_ttf_replacements
                        )
                        try:
                            if replace_fonts_in_file(
                                unity_version,
                                game_path,
                                working_assets_file,
                                file_ttf_replacements,
                                replace_ttf=True,
                                replace_sdf=False,
                                use_game_mat=args.use_game_material,
                                use_game_line_metrics=args.use_game_line_metrics,
                                material_scale_by_padding=not args.use_game_material,
                                prefer_original_compress=args.original_compress,
                                temp_root_dir=args.temp_dir,
                                generator=generator,
                                replacement_lookup=file_ttf_lookup,
                                ps5_swizzle=args.ps5_swizzle,
                                preview_export=args.preview_export,
                                preview_root=preview_root,
                                lang=lang,
                            ):
                                file_modified = True
                        except Exception as e:
                            if is_ko:
                                _log_console(
                                    f"  TTF 분할 저장 실패 [{type(e).__name__}]: {e!r}"
                                )
                            else:
                                _log_console(
                                    f"  TTF split save failed [{type(e).__name__}]: {e!r}"
                                )
                            split_stopped = True

                    if replace_sdf and not split_stopped:
                        sdf_items = list(file_sdf_replacements.items())
                        sdf_total = len(sdf_items)
                        if sdf_total > 0:
                            if args.split_save_force:
                                batch_size = 1
                            else:
                                batch_size = min(sdf_total, max(1, sdf_total // 2))

                            idx = 0
                            while idx < sdf_total:
                                current_batch = min(batch_size, sdf_total - idx)
                                batch_dict = dict(sdf_items[idx : idx + current_batch])
                                batch_lookup, _ = build_replacement_lookup(batch_dict)

                                try:
                                    ok = replace_fonts_in_file(
                                        unity_version,
                                        game_path,
                                        working_assets_file,
                                        batch_dict,
                                        replace_ttf=False,
                                        replace_sdf=True,
                                        use_game_mat=args.use_game_material,
                                        use_game_line_metrics=args.use_game_line_metrics,
                                        material_scale_by_padding=not args.use_game_material,
                                        prefer_original_compress=args.original_compress,
                                        temp_root_dir=args.temp_dir,
                                        generator=generator,
                                        replacement_lookup=batch_lookup,
                                        ps5_swizzle=args.ps5_swizzle,
                                        preview_export=args.preview_export,
                                        preview_root=preview_root,
                                        lang=lang,
                                    )
                                except Exception as e:
                                    ok = False
                                    if is_ko:
                                        _log_console(
                                            f"  SDF 배치 저장 실패 [{type(e).__name__}]: {e!r}"
                                        )
                                    else:
                                        _log_console(
                                            f"  SDF batch save failed [{type(e).__name__}]: {e!r}"
                                        )

                                if ok:
                                    file_modified = True
                                    idx += current_batch
                                    if idx < sdf_total:
                                        if args.split_save_force:
                                            if is_ko:
                                                _log_console(
                                                    f"  SDF 배치 진행: {idx}/{sdf_total} (다음 배치: 1, 강제)"
                                                )
                                            else:
                                                _log_console(
                                                    f"  SDF batch progress: {idx}/{sdf_total} (next batch: 1, forced)"
                                                )
                                        else:
                                            # KR: 성공하면 배치를 키워 쓰기 횟수를 줄입니다.
                                            # EN: Grow batch size after success to reduce write count.
                                            batch_size = min(
                                                sdf_total - idx,
                                                max(
                                                    current_batch + 1, current_batch * 2
                                                ),
                                            )
                                            if is_ko:
                                                _log_console(
                                                    f"  SDF 배치 진행: {idx}/{sdf_total} (다음 배치: {batch_size})"
                                                )
                                            else:
                                                _log_console(
                                                    f"  SDF batch progress: {idx}/{sdf_total} (next batch: {batch_size})"
                                                )
                                else:
                                    if is_ko:
                                        _log_console(
                                            "  SDF 배치 저장 실패: 내부 저장 단계가 False를 반환했습니다. 위 오류 로그를 확인하세요."
                                        )
                                    else:
                                        _log_console(
                                            "  SDF batch save failed: internal save stage returned False. Check previous error logs."
                                        )
                                    if current_batch <= 1:
                                        split_stopped = True
                                        if is_ko:
                                            _log_console(
                                                "  SDF 분할 저장 중단: 배치 1개에서도 저장 실패"
                                            )
                                        else:
                                            _log_console(
                                                "  Stopping SDF split save: failed even with batch size 1"
                                            )
                                        break

                                    batch_size = max(1, current_batch // 2)
                                    gc.collect()
                                    if is_ko:
                                        _log_console(
                                            f"  SDF 배치 크기를 {batch_size}로 줄여 재시도합니다..."
                                        )
                                    else:
                                        _log_console(
                                            f"  Reducing SDF batch size to {batch_size} and retrying..."
                                        )
            else:
                if (
                    replace_sdf
                    and len(file_sdf_replacements) > 1
                    and not args.split_save
                ):
                    if is_ko:
                        _log_console(
                            "  참고: --oneshot-save-force로 split-save 폴백이 비활성화되어 메모리 피크가 증가할 수 있습니다."
                        )
                    else:
                        _log_console(
                            "  Note: --oneshot-save-force disables split-save fallback and may increase memory peak."
                        )
                try:
                    if replace_fonts_in_file(
                        unity_version,
                        game_path,
                        working_assets_file,
                        replacements,
                        replace_ttf,
                        replace_sdf,
                        use_game_mat=args.use_game_material,
                        use_game_line_metrics=args.use_game_line_metrics,
                        material_scale_by_padding=not args.use_game_material,
                        prefer_original_compress=args.original_compress,
                        temp_root_dir=args.temp_dir,
                        generator=generator,
                        replacement_lookup=replacement_lookup,
                        ps5_swizzle=args.ps5_swizzle,
                        preview_export=args.preview_export,
                        preview_root=preview_root,
                        lang=lang,
                    ):
                        file_modified = True
                except Exception as e:
                    if is_ko:
                        _log_console(f"  파일 처리 실패 [{type(e).__name__}]: {e!r}")
                    else:
                        _log_console(
                            f"  File processing failed [{type(e).__name__}]: {e!r}"
                        )

            if file_modified:
                modified_count += 1

    if mode == "preview_export":
        if is_ko:
            _log_console(
                f"\n완료! preview export 처리 파일: {len(process_files)}개 (원본 수정 없음)"
            )
            input("\n엔터를 눌러 종료...")
        else:
            _log_console(
                f"\nDone! Preview-export processed {len(process_files)} file(s) (no source modifications)."
            )
            input("\nPress Enter to exit...")
    else:
        if is_ko:
            _log_console(f"\n완료! {modified_count}개의 파일이 수정되었습니다.")
            input("\n엔터를 눌러 종료...")
        else:
            _log_console(f"\nDone! Modified {modified_count} file(s).")
            input("\nPress Enter to exit...")


def main() -> None:
    """KR: 한국어 CLI 진입점입니다.
    EN: Korean CLI entrypoint.
    """
    main_cli(lang="ko")


def main_en() -> None:
    """KR: 영어 CLI 진입점입니다.
    EN: English CLI entrypoint.
    """
    main_cli(lang="en")


def run_main_ko() -> None:
    """KR: 한국어 실행 진입점을 예외 처리와 함께 실행합니다.
    EN: Run Korean entrypoint with top-level exception handling.
    """
    try:
        main()
    except Exception as e:
        _log_exception(f"\n예상치 못한 오류가 발생했습니다: {e}")
        input("\n엔터를 눌러 종료...")
        sys.exit(1)
    finally:
        logging.shutdown()
        cleanup_registered_temp_dirs()


def run_main_en() -> None:
    """KR: 영어 실행 진입점을 예외 처리와 함께 실행합니다.
    EN: Run English entrypoint with top-level exception handling.
    """
    try:
        main_en()
    except Exception as e:
        _log_exception(f"\nAn unexpected error occurred: {e}")
        input("\nPress Enter to exit...")
        sys.exit(1)
    finally:
        logging.shutdown()
        cleanup_registered_temp_dirs()


if __name__ == "__main__":
    try:
        run_main_ko()
    except Exception as e:
        _log_exception(f"\n예상치 못한 오류가 발생했습니다: {e}")
        input("\n엔터를 눌러 종료...")
        sys.exit(1)
