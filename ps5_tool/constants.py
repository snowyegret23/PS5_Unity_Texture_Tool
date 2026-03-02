"""Human-readable texture format constants (no reverse-engineering naming)."""

from __future__ import annotations

from typing import Any


TEXTURE_FORMAT_CATALOG: dict[int, dict[str, Any]] = {
    1: {
        "name": "Alpha8",
        "category": "uncompressed",
        "bytes_per_pixel": 1,
        "channels": "A",
    },
    3: {
        "name": "RGB24",
        "category": "uncompressed",
        "bytes_per_pixel": 3,
        "channels": "RGB",
    },
    4: {
        "name": "RGBA32",
        "category": "uncompressed",
        "bytes_per_pixel": 4,
        "channels": "RGBA",
    },
    10: {
        "name": "DXT1",
        "category": "block_compressed",
        "block_codec": "BC1",
        "bytes_per_block": 8,
        "block_size": [4, 4],
    },
    12: {
        "name": "DXT5",
        "category": "block_compressed",
        "block_codec": "BC3",
        "bytes_per_block": 16,
        "block_size": [4, 4],
    },
    25: {
        "name": "BC7",
        "category": "block_compressed",
        "block_codec": "BC7",
        "bytes_per_block": 16,
        "block_size": [4, 4],
    },
    28: {
        "name": "DXT1Crunched",
        "category": "crunched",
        "decoded_codec": "BC1",
    },
    29: {
        "name": "DXT5Crunched",
        "category": "crunched",
        "decoded_codec": "BC3",
    },
}

DEFAULT_BC_SWIZZLE_MODES: list[str] = [
    "auto",
    "4KB_S",
    "64KB_S",
    "4KB_D",
    "256B_S",
    "64KB_D",
    "256B_D",
]

DEFAULT_PIPE_VALUES: list[int] = [2, 1, 3, 0]

