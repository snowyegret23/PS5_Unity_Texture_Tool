from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import UnityPy
from PIL import Image, ImageOps
from UnityPy.export import Texture2DConverter

from . import ps5_core as core
from .constants import DEFAULT_BC_SWIZZLE_MODES, TEXTURE_FORMAT_CATALOG
from .models import OperationResult, TexturePreviewSettings, TextureRecord


CRUNCHED_FORMATS = {28, 29}
BC_FORMATS = set(getattr(core, "_PS5_BC_FORMATS", {}).keys())
IMAGE_SUFFIXES = {".png", ".dds", ".tga", ".jpg", ".jpeg", ".webp", ".bmp"}
_BC_MODE_RE = re.compile(r"^(?P<mode>[^:]+):p(?P<pipe>\d+):x(?P<xor>\d+)(?::o(?P<offset>\d+))?$")


def _sanitize_name(text: str, fallback: str = "texture") -> str:
    t = re.sub(r"[^\w\-.]+", "_", text or "").strip("._")
    return t or fallback


def _texture_category(texture_format: int) -> str:
    fmt = int(texture_format)
    if fmt in CRUNCHED_FORMATS:
        return "crunched"
    if fmt in BC_FORMATS:
        return "block_compressed"
    info = TEXTURE_FORMAT_CATALOG.get(fmt)
    if info:
        return str(info.get("category", "unknown"))
    return "unknown"


def _guess_bpe(texture_format: int, raw_size: int, width: int, height: int) -> int | None:
    info = TEXTURE_FORMAT_CATALOG.get(int(texture_format))
    if info and isinstance(info.get("bytes_per_pixel"), int):
        return int(info["bytes_per_pixel"])
    bpe = core._texture_format_bytes_per_element(int(texture_format))
    if bpe in {1, 2, 3, 4}:
        return int(bpe)
    total = int(width) * int(height)
    if total > 0 and raw_size > 0 and raw_size % total == 0:
        g = raw_size // total
        if g in {1, 2, 3, 4}:
            return int(g)
    return None


def _read_raw_data(texture_obj: Any) -> bytes | None:
    texture = texture_obj.parse_as_object()
    get_image_data = getattr(texture, "get_image_data", None)
    if callable(get_image_data):
        try:
            candidate = get_image_data()
            if isinstance(candidate, (bytes, bytearray)):
                return bytes(candidate)
        except Exception:
            pass
    image_data = getattr(texture, "image_data", None)
    if isinstance(image_data, (bytes, bytearray)):
        return bytes(image_data)
    return None


def _resolve_assets_name(texture_obj: Any, texture: Any | None = None) -> str:
    candidates: list[Any] = []
    candidates.append(getattr(texture_obj, "assets_file", None))
    if texture is not None:
        candidates.append(getattr(texture, "assets_file", None))
    for af in candidates:
        if af is None:
            continue
        name = getattr(af, "name", None)
        if isinstance(name, str) and name.strip():
            return str(name).strip()
        cab = getattr(af, "cab_file", None)
        if isinstance(cab, str) and cab.strip():
            return str(cab).strip()
    return "unknown_assets"


def _to_rgba_preview(image: Image.Image) -> Image.Image:
    if image.mode != "RGBA":
        return image.convert("RGBA")
    return image


def _parse_bc_mode_used(mode_used: str | None) -> tuple[str, int, int, int] | None:
    if not mode_used:
        return None
    m = _BC_MODE_RE.match(str(mode_used).strip())
    if not m:
        return None
    return (
        str(m.group("mode")),
        int(m.group("pipe")),
        int(m.group("xor")),
        int(m.group("offset") or 0),
    )


def _swizzle_bc_blocks(
    linear_block_data: bytes,
    block_w: int,
    block_h: int,
    bytes_per_block: int,
    lut: tuple[int, ...],
) -> bytes:
    total = int(block_w) * int(block_h)
    src = memoryview(linear_block_data[: total * int(bytes_per_block)])
    dst = bytearray(total * int(bytes_per_block))
    for linear_idx, swizzled_idx in enumerate(lut):
        src_off = int(linear_idx) * int(bytes_per_block)
        dst_off = int(swizzled_idx) * int(bytes_per_block)
        dst[dst_off : dst_off + int(bytes_per_block)] = src[src_off : src_off + int(bytes_per_block)]
    return bytes(dst)


def _patch_blocks_top_left(
    full_block_data: bytes,
    physical_block_w: int,
    logical_block_w: int,
    logical_block_h: int,
    bytes_per_block: int,
    logical_patch: bytes,
) -> bytes:
    out = bytearray(full_block_data)
    src = memoryview(logical_patch)
    row_bytes = int(logical_block_w) * int(bytes_per_block)
    for y in range(int(logical_block_h)):
        dst_off = y * int(physical_block_w) * int(bytes_per_block)
        src_off = y * int(logical_block_w) * int(bytes_per_block)
        out[dst_off : dst_off + row_bytes] = src[src_off : src_off + row_bytes]
    return bytes(out)


def _align_up(value: int, align: int) -> int:
    a = max(1, int(align))
    return ((int(value) + a - 1) // a) * a


def _mode_for_bpe(bpe: int) -> str | None:
    return {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}.get(int(bpe))


def _bc_payload_sizes(
    texture_format: int,
    width: int,
    height: int,
) -> tuple[int, int] | None:
    bc = core._PS5_BC_FORMATS.get(int(texture_format))
    if bc is None:
        return None
    block_w_px, block_h_px, bytes_per_block, _ = bc
    logical_bw = (int(width) + int(block_w_px) - 1) // int(block_w_px)
    logical_bh = (int(height) + int(block_h_px) - 1) // int(block_h_px)
    logical_bytes = logical_bw * logical_bh * int(bytes_per_block)

    bits = core._ps5_ghidra_mode_tile_bits("4KB_S", int(bytes_per_block))
    if bits is None:
        return logical_bytes, logical_bytes
    tile_bw = 1 << int(bits[0])
    tile_bh = 1 << int(bits[1])
    physical_bw = _align_up(logical_bw, tile_bw)
    physical_bh = _align_up(logical_bh, tile_bh)
    canonical_4kb_bytes = physical_bw * physical_bh * int(bytes_per_block)
    return logical_bytes, canonical_4kb_bytes


def _rule_based_swizzle_default(
    *,
    texture_format: int,
    category: str,
    width: int,
    height: int,
    is_streamed: bool,
    is_readable: bool,
    raw_size: int,
) -> tuple[bool, str]:
    """Deterministic rule set for preview defaults (no image-score heuristics)."""
    fmt = int(texture_format)
    if fmt in BC_FORMATS or category == "block_compressed":
        sizes = _bc_payload_sizes(fmt, int(width), int(height))
        if sizes is not None:
            logical_bytes, canonical_4kb_bytes = sizes
            # Common BC rule:
            # - If payload can contain the canonical 4KB tiled base, use unswizzle.
            # - If payload is exactly logical size (no 4KB pad/extra tail), treat as linear.
            if int(raw_size) >= int(canonical_4kb_bytes):
                return True, "rule:bc-4kb-canonical"
            if int(raw_size) == int(logical_bytes):
                return False, "rule:bc-inline-linear"
            if int(raw_size) > int(logical_bytes):
                return True, "rule:bc-extra-tail"
            return False, "rule:bc-insufficient-payload"
        return bool(is_streamed), ("rule:bc-streamed-fallback" if is_streamed else "rule:bc-inline-fallback")

    if category == "uncompressed":
        if is_streamed:
            return True, "rule:unc-streamed"
        return False, "rule:unc-inline-linear"

    return False, "rule:no-unswizzle"


class TextureBackend:
    def __init__(self) -> None:
        self.bundle_path: Path | None = None
        self.env: Any | None = None
        self.records: list[TextureRecord] = []
        self._object_map: dict[int, Any] = {}

    def load_bundle(self, bundle_path: str | Path) -> list[TextureRecord]:
        path = Path(bundle_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        self.env = UnityPy.load(str(path))
        self.bundle_path = path
        self.records = []
        self._object_map = {}

        for obj in self.env.objects:
            if obj.type.name != "Texture2D":
                continue
            tex = obj.parse_as_object()
            width = int(getattr(tex, "m_Width", 0) or 0)
            height = int(getattr(tex, "m_Height", 0) or 0)
            if width <= 0 or height <= 0:
                # Skip placeholder/invalid Texture2D entries (e.g. 0x0).
                continue
            texture_format = int(getattr(tex, "m_TextureFormat", -1) or -1)
            texture_format_name = str(core._texture_format_enum_name(texture_format))
            mip_count = int(getattr(tex, "m_MipCount", 1) or 1)
            is_readable = bool(getattr(tex, "m_IsReadable", False))
            stream_data = getattr(tex, "m_StreamData", None)
            stream_size = int(getattr(stream_data, "size", 0) or 0) if stream_data is not None else 0
            raw = _read_raw_data(obj)
            category = _texture_category(texture_format)
            swz_default, swz_source = _rule_based_swizzle_default(
                texture_format=texture_format,
                category=category,
                width=width,
                height=height,
                is_streamed=(stream_size > 0),
                is_readable=is_readable,
                raw_size=(len(raw) if raw else 0),
            )
            verdict = "swizzled_by_rule" if swz_default else "linear_by_rule"
            source = swz_source
            assets_name = _resolve_assets_name(obj, tex)
            record = TextureRecord(
                bundle_path=str(path),
                assets_name=assets_name,
                path_id=int(obj.path_id),
                name=str(getattr(tex, "m_Name", "") or ""),
                width=width,
                height=height,
                mip_count=mip_count,
                texture_format=texture_format,
                texture_format_name=texture_format_name,
                category=category,
                is_streamed=stream_size > 0,
                stream_size=stream_size,
                is_readable=is_readable,
                raw_size=len(raw) if raw else 0,
                auto_swizzle_verdict=verdict,
                auto_swizzle_source=source,
            )
            self.records.append(record)
            self._object_map[record.path_id] = obj

        self.records.sort(key=lambda r: (r.path_id, r.name.lower()))
        return list(self.records)

    def get_record(self, path_id: int) -> TextureRecord | None:
        for r in self.records:
            if r.path_id == int(path_id):
                return r
        return None

    def _build_import_settings(self, record: TextureRecord, settings: TexturePreviewSettings | None) -> TexturePreviewSettings:
        if settings is not None:
            return settings
        return self.default_settings_for(record)

    def _apply_inverse_preview_ops(self, image: Image.Image, settings: TexturePreviewSettings) -> Image.Image:
        out = image.convert("RGBA")
        # Reverse preview post-process order: flip then R/B swap.
        if settings.flip_tb:
            out = ImageOps.flip(out)
        if settings.swap_rb:
            out = core._ps5_swap_rb_image(out)
        return out

    def _write_texture_raw_inline(self, tex: Any, raw_bytes: bytes, *, keep_format: int) -> None:
        tex.image_data = bytes(raw_bytes)
        tex.m_CompleteImageSize = int(len(raw_bytes))
        tex.m_TextureFormat = int(keep_format)
        if getattr(tex, "m_StreamData", None) is not None:
            tex.m_StreamData.path = ""
            tex.m_StreamData.offset = 0
            tex.m_StreamData.size = 0

    def _import_bc_preserve_layout(
        self,
        target_obj: Any,
        tex: Any,
        replacement: Image.Image,
        record: TextureRecord,
        settings: TexturePreviewSettings,
    ) -> bool:
        texture_format = int(record.texture_format)
        bc_info = core._PS5_BC_FORMATS.get(texture_format)
        if bc_info is None:
            return False

        width = int(record.width)
        height = int(record.height)
        mip_count = int(record.mip_count)
        block_w_px, block_h_px, bytes_per_block, _ = bc_info
        logical_bw = (width + int(block_w_px) - 1) // int(block_w_px)
        logical_bh = (height + int(block_h_px) - 1) // int(block_h_px)
        logical_bytes = logical_bw * logical_bh * int(bytes_per_block)

        # BC import note:
        # - Keep replacement pixels as user-edited preview image.
        # - Let Texture2DConverter handle vertical convention (flip=True).
        # - Do NOT reverse R/B here; empirical roundtrip shows this degrades color.
        work = replacement.convert("RGBA")
        platform = tex.object_reader.platform if getattr(tex, "object_reader", None) is not None else 0
        platform_blob = getattr(tex, "m_PlatformBlob", None)
        try:
            encoded, encoded_format = Texture2DConverter.image_to_texture2d(
                work,
                texture_format,
                platform,
                platform_blob,
                True,
            )
        except Exception:
            return False
        if int(encoded_format) != texture_format:
            return False
        if len(encoded) < logical_bytes:
            return False
        linear_blocks = bytes(encoded[:logical_bytes])

        current_raw = _read_raw_data(target_obj)
        if not current_raw:
            current_raw = linear_blocks
        raw_mut = bytearray(current_raw)

        if not settings.swizzle_enabled:
            # Keep payload length; patch base-level logical prefix.
            if len(raw_mut) < logical_bytes:
                raw_mut.extend(b"\x00" * (logical_bytes - len(raw_mut)))
            raw_mut[:logical_bytes] = linear_blocks
            self._write_texture_raw_inline(tex, bytes(raw_mut), keep_format=texture_format)
            return True

        best = core._ps5_unswizzle_bc_best_candidate_ghidra(
            current_raw,
            width,
            height,
            texture_format,
            mip_count=mip_count,
        )
        if best is None:
            return False
        _, mode_used, _, _, physical = best
        if not isinstance(mode_used, str):
            return False
        parsed = _parse_bc_mode_used(mode_used)
        if parsed is None:
            if mode_used == "linear_passthrough":
                if len(raw_mut) < logical_bytes:
                    raw_mut.extend(b"\x00" * (logical_bytes - len(raw_mut)))
                raw_mut[:logical_bytes] = linear_blocks
                self._write_texture_raw_inline(tex, bytes(raw_mut), keep_format=texture_format)
                return True
            return False

        mode_name, pipe_log2, pipe_bank_xor, offset = parsed
        physical_bw = int(physical[0])
        physical_bh = int(physical[1])
        physical_bytes = physical_bw * physical_bh * int(bytes_per_block)
        if physical_bytes <= 0:
            return False
        if len(raw_mut) < offset + physical_bytes:
            raw_mut.extend(b"\x00" * (offset + physical_bytes - len(raw_mut)))
        source_window = bytes(raw_mut[offset : offset + physical_bytes])

        lut = core._ps5_build_bc_lut_cached(
            physical_bw,
            physical_bh,
            int(bytes_per_block),
            mode_name,
            int(pipe_log2),
            int(pipe_bank_xor),
        )
        if lut is None:
            return False

        unsw_full = core._ps5_unswizzle_bc_blocks(
            source_window,
            physical_bw,
            physical_bh,
            int(bytes_per_block),
            lut,
        )
        patched_unsw = _patch_blocks_top_left(
            unsw_full,
            physical_bw,
            logical_bw,
            logical_bh,
            int(bytes_per_block),
            linear_blocks,
        )
        swizzled_window = _swizzle_bc_blocks(
            patched_unsw,
            physical_bw,
            physical_bh,
            int(bytes_per_block),
            lut,
        )
        raw_mut[offset : offset + physical_bytes] = swizzled_window
        self._write_texture_raw_inline(tex, bytes(raw_mut), keep_format=texture_format)
        return True

    def _import_uncompressed_preserve_layout(
        self,
        target_obj: Any,
        tex: Any,
        replacement: Image.Image,
        record: TextureRecord,
        settings: TexturePreviewSettings,
    ) -> bool:
        texture_format = int(record.texture_format)
        width = int(record.width)
        height = int(record.height)
        bpe = _guess_bpe(texture_format, int(record.raw_size), width, height)
        if bpe not in {1, 2, 3, 4}:
            return False
        mode = _mode_for_bpe(int(bpe))
        if mode is None:
            return False

        # Undo preview post-ops first (flip/swap), then reconstruct the storage variant.
        work = self._apply_inverse_preview_ops(replacement, settings)
        mode_img = work.convert(mode)
        current_raw = _read_raw_data(target_obj) or b""
        usable = current_raw[: (len(current_raw) // int(bpe)) * int(bpe)]
        logical_bytes = int(width) * int(height) * int(bpe)

        variant = "raw_linear"
        best_unswizzled = b""
        if settings.swizzle_enabled:
            if len(usable) < logical_bytes:
                return False
            best_unswizzled, out_w, out_h, variant, _ = core._ps5_unswizzle_best_variant(
                usable[:logical_bytes],
                width,
                height,
                int(bpe),
                allow_axis_swap=True,
                roughness_guard=bool(settings.roughness_guard),
            )
            if variant == "swapped_axes":
                rot = int(getattr(core, "PS5_SWIZZLE_ROTATE", 0)) % 360
                if rot != 0:
                    mode_img = mode_img.rotate((-rot) % 360, expand=True)
                if mode_img.width != out_w or mode_img.height != out_h:
                    mode_img = mode_img.resize((out_w, out_h), Image.Resampling.LANCZOS)
            elif variant in {"normal", "already_linear", "addrlib_4KB_S"}:
                mode_img = ImageOps.flip(mode_img)
                if mode_img.width != width or mode_img.height != height:
                    mode_img = mode_img.resize((width, height), Image.Resampling.LANCZOS)
            else:
                # Unknown variant from core: fall back to default swizzle path.
                mode_img = mode_img.resize((width, height), Image.Resampling.LANCZOS)
                variant = "normal"
        else:
            if mode_img.width != width or mode_img.height != height:
                mode_img = mode_img.resize((width, height), Image.Resampling.LANCZOS)

        linear_variant = mode_img.tobytes()
        raw_mut = bytearray(current_raw)

        if not settings.swizzle_enabled:
            if len(raw_mut) < logical_bytes:
                raw_mut.extend(b"\x00" * (logical_bytes - len(raw_mut)))
            raw_mut[:logical_bytes] = linear_variant[:logical_bytes]
            self._write_texture_raw_inline(tex, bytes(raw_mut), keep_format=texture_format)
            return True

        if variant == "already_linear":
            rebuilt = linear_variant[:logical_bytes]
            if len(raw_mut) < logical_bytes:
                raw_mut.extend(b"\x00" * (logical_bytes - len(raw_mut)))
            raw_mut[:logical_bytes] = rebuilt
            self._write_texture_raw_inline(tex, bytes(raw_mut), keep_format=texture_format)
            return True

        if variant == "normal":
            rebuilt = core.ps5_swizzle_bytes(
                linear_variant,
                width,
                height,
                int(bpe),
            )
            if len(raw_mut) < logical_bytes:
                raw_mut.extend(b"\x00" * (logical_bytes - len(raw_mut)))
            raw_mut[:logical_bytes] = rebuilt[:logical_bytes]
            self._write_texture_raw_inline(tex, bytes(raw_mut), keep_format=texture_format)
            return True

        if variant == "swapped_axes":
            rebuilt = core.ps5_swizzle_bytes(
                linear_variant,
                mode_img.width,
                mode_img.height,
                int(bpe),
            )
            if len(raw_mut) < logical_bytes:
                raw_mut.extend(b"\x00" * (logical_bytes - len(raw_mut)))
            raw_mut[:logical_bytes] = rebuilt[:logical_bytes]
            self._write_texture_raw_inline(tex, bytes(raw_mut), keep_format=texture_format)
            return True

        if variant == "addrlib_4KB_S":
            # Preserve physical addrlib layout when payload has aligned physical grid.
            physical_total = len(usable) // int(bpe)
            if physical_total <= 0:
                return False
            inferred = core._ps5_infer_physical_grid(
                physical_total,
                width,
                height,
                align_width=8,
                align_height=8,
            )
            candidates: list[tuple[int, int]] = [(width, height)]
            if inferred[0] * inferred[1] == physical_total and inferred not in candidates:
                candidates.append(inferred)

            selected: tuple[int, int] | None = None
            selected_lut: tuple[int, ...] | None = None
            selected_unsw: bytes | None = None
            for pw, ph in candidates:
                phys_bytes = int(pw) * int(ph) * int(bpe)
                if phys_bytes > len(usable):
                    continue
                lut = core._ps5_build_bc_lut_cached(int(pw), int(ph), int(bpe), "4KB_S", 2, 0)
                if lut is None:
                    continue
                unsw_full = core._ps5_unswizzle_bc_blocks(
                    usable[:phys_bytes],
                    int(pw),
                    int(ph),
                    int(bpe),
                    lut,
                )
                crop = core._ps5_crop_blocks_top_left(
                    unsw_full,
                    int(pw),
                    width,
                    height,
                    int(bpe),
                )
                if best_unswizzled and crop != best_unswizzled:
                    continue
                selected = (int(pw), int(ph))
                selected_lut = lut
                selected_unsw = unsw_full
                break

            if selected is None or selected_lut is None or selected_unsw is None:
                # Fallback to plain swizzle at logical dimensions.
                rebuilt = core.ps5_swizzle_bytes(linear_variant, width, height, int(bpe))
                if len(raw_mut) < logical_bytes:
                    raw_mut.extend(b"\x00" * (logical_bytes - len(raw_mut)))
                raw_mut[:logical_bytes] = rebuilt[:logical_bytes]
                self._write_texture_raw_inline(tex, bytes(raw_mut), keep_format=texture_format)
                return True

            pw, ph = selected
            phys_bytes = int(pw) * int(ph) * int(bpe)
            patched_unsw = _patch_blocks_top_left(
                selected_unsw,
                int(pw),
                width,
                height,
                int(bpe),
                linear_variant[:logical_bytes],
            )
            rebuilt_full = _swizzle_bc_blocks(
                patched_unsw,
                int(pw),
                int(ph),
                int(bpe),
                selected_lut,
            )
            if len(raw_mut) < phys_bytes:
                raw_mut.extend(b"\x00" * (phys_bytes - len(raw_mut)))
            raw_mut[:phys_bytes] = rebuilt_full[:phys_bytes]
            self._write_texture_raw_inline(tex, bytes(raw_mut), keep_format=texture_format)
            return True

        return False

    def _import_crunched_preserve_format(
        self,
        tex: Any,
        replacement: Image.Image,
        record: TextureRecord,
        settings: TexturePreviewSettings,
    ) -> bool:
        texture_format = int(record.texture_format)
        work = self._apply_inverse_preview_ops(replacement, settings)
        platform = tex.object_reader.platform if getattr(tex, "object_reader", None) is not None else 0
        platform_blob = getattr(tex, "m_PlatformBlob", None)
        try:
            encoded, encoded_format = Texture2DConverter.image_to_texture2d(
                work,
                texture_format,
                platform,
                platform_blob,
                True,
            )
        except Exception:
            return False
        if int(encoded_format) != texture_format:
            return False
        if not encoded:
            return False
        self._write_texture_raw_inline(tex, bytes(encoded), keep_format=texture_format)
        return True

    def default_settings_for(self, record: TextureRecord) -> TexturePreviewSettings:
        swz, _ = _rule_based_swizzle_default(
            texture_format=record.texture_format,
            category=record.category,
            width=record.width,
            height=record.height,
            is_streamed=record.is_streamed,
            is_readable=record.is_readable,
            raw_size=record.raw_size,
        )
        is_bc = record.category == "block_compressed"
        return TexturePreviewSettings(
            swizzle_enabled=swz,
            # Keep BC preview post-fixes always enabled by default.
            # Even when unswizzle is disabled (linear passthrough), BC decode
            # preview usually still needs channel/order normalization.
            swap_rb=is_bc,
            flip_tb=is_bc,
            bc_mode="auto",
            pipe_log2=2,
            pipe_bank_xor=0,
            mip_offset_override=None,
            use_mip_count=True,
            roughness_guard=True,
        )

    def build_preview(self, path_id: int, settings: TexturePreviewSettings) -> tuple[Image.Image, dict[str, Any]]:
        if self.env is None or self.bundle_path is None:
            raise RuntimeError("Bundle is not loaded")
        obj = self._object_map.get(int(path_id))
        if obj is None:
            raise KeyError(f"path_id not found: {path_id}")
        tex = obj.parse_as_object()
        width = int(getattr(tex, "m_Width", 0) or 0)
        height = int(getattr(tex, "m_Height", 0) or 0)
        mip_count = int(getattr(tex, "m_MipCount", 1) or 1)
        texture_format = int(getattr(tex, "m_TextureFormat", -1) or -1)
        category = _texture_category(texture_format)
        raw = _read_raw_data(obj)
        if width <= 0 or height <= 0:
            raise ValueError(f"Invalid texture dimensions: {width}x{height}")
        if not raw:
            raise ValueError("Texture has no readable raw payload")

        meta: dict[str, Any] = {
            "path_id": int(path_id),
            "assets_name": _resolve_assets_name(obj, tex),
            "name": str(getattr(tex, "m_Name", "") or ""),
            "width": width,
            "height": height,
            "mip_count": mip_count,
            "texture_format": texture_format,
            "texture_format_name": str(core._texture_format_enum_name(texture_format)),
            "category": category,
            "used_settings": settings.to_dict(),
        }

        if category == "crunched":
            image = tex.image
            if image is None:
                raise ValueError("UnityPy failed to decode crunched texture")
            image = _to_rgba_preview(image)
            meta["decode_path"] = "crunched_unitypy_decode"
            return image, meta

        if category == "block_compressed":
            image, bc_meta = self._build_bc_preview(raw, width, height, texture_format, mip_count, settings)
            meta.update(bc_meta)
            return image, meta

        image, unc_meta = self._build_uncompressed_preview(raw, width, height, texture_format, settings)
        meta.update(unc_meta)
        return image, meta

    def _build_bc_preview(
        self,
        raw: bytes,
        width: int,
        height: int,
        texture_format: int,
        mip_count: int,
        settings: TexturePreviewSettings,
    ) -> tuple[Image.Image, dict[str, Any]]:
        bc_info = core._PS5_BC_FORMATS.get(int(texture_format))
        if bc_info is None:
            raise ValueError(f"Unsupported BC format: {texture_format}")
        block_w_px, block_h_px, bytes_per_block, _ = bc_info
        logical_bw = (width + block_w_px - 1) // block_w_px
        logical_bh = (height + block_h_px - 1) // block_h_px
        logical_bytes = logical_bw * logical_bh * bytes_per_block
        usable = raw[: (len(raw) // bytes_per_block) * bytes_per_block]
        if len(usable) < logical_bytes:
            raise ValueError(f"Raw BC payload too small: have={len(usable)} need={logical_bytes}")

        mode_used = "raw_linear"
        physical_grid = [logical_bw, logical_bh]
        if not settings.swizzle_enabled:
            blocks = usable[:logical_bytes]
        elif settings.bc_mode == "auto":
            best = core._ps5_unswizzle_bc_best_candidate_ghidra(
                usable,
                width,
                height,
                texture_format,
                mip_count=(mip_count if settings.use_mip_count else None),
            )
            if best is None:
                raise ValueError("Auto BC unswizzle failed")
            blocks, mode_used, _, _, physical = best
            physical_grid = [int(physical[0]), int(physical[1])]
        else:
            mode_name = str(settings.bc_mode)
            if mode_name not in DEFAULT_BC_SWIZZLE_MODES:
                raise ValueError(f"Invalid BC mode: {mode_name}")

            total_blocks = len(usable) // bytes_per_block
            candidates = core._ps5_physical_grid_candidates_for_mode(
                total_blocks,
                logical_bw,
                logical_bh,
                bytes_per_block=bytes_per_block,
                mode_name=mode_name,
                align_width=(16 if bytes_per_block >= 16 else 8),
                align_height=(16 if bytes_per_block >= 16 else 8),
            )
            if not candidates:
                raise ValueError(f"No physical-grid candidate for mode={mode_name}")
            physical_w, physical_h = candidates[0]
            physical_grid = [int(physical_w), int(physical_h)]
            physical_bytes = physical_w * physical_h * bytes_per_block

            offset = settings.mip_offset_override
            if offset is None:
                auto = core._ps5_unswizzle_bc_best_candidate_ghidra(
                    usable,
                    width,
                    height,
                    texture_format,
                    mip_count=(mip_count if settings.use_mip_count else None),
                )
                if auto is not None and isinstance(auto[1], str):
                    m = re.search(r":o(\d+)$", auto[1])
                    if m:
                        offset = int(m.group(1))
            if offset is None:
                offset = 0
            if offset < 0 or offset + physical_bytes > len(usable):
                raise ValueError(
                    f"Invalid manual BC offset={offset} for physical_bytes={physical_bytes}, stream={len(usable)}"
                )
            source = usable[offset : offset + physical_bytes]
            lut = core._ps5_build_bc_lut_cached(
                physical_w,
                physical_h,
                bytes_per_block,
                mode_name,
                int(settings.pipe_log2),
                int(settings.pipe_bank_xor),
            )
            if lut is None:
                raise ValueError("Failed to build BC LUT for manual settings")
            unsw_full = core._ps5_unswizzle_bc_blocks(
                source,
                physical_w,
                physical_h,
                bytes_per_block,
                lut,
            )
            blocks = core._ps5_crop_blocks_top_left(
                unsw_full,
                physical_w,
                logical_bw,
                logical_bh,
                bytes_per_block,
            )
            mode_used = f"{mode_name}:p{settings.pipe_log2}:x{settings.pipe_bank_xor}:o{offset}"

        rgba = core._ps5_decode_bc_to_rgba(blocks, width, height, texture_format)
        if rgba is None:
            raise ValueError("BC decode failed")
        image = Image.frombytes("RGBA", (width, height), rgba)
        if settings.swap_rb:
            image = core._ps5_swap_rb_image(image)
        if settings.flip_tb:
            image = ImageOps.flip(image)
        return image, {"decode_path": "bc_unswizzle", "mode_used": mode_used, "physical_grid": physical_grid}

    def _build_uncompressed_preview(
        self,
        raw: bytes,
        width: int,
        height: int,
        texture_format: int,
        settings: TexturePreviewSettings,
    ) -> tuple[Image.Image, dict[str, Any]]:
        bpe = _guess_bpe(texture_format, len(raw), width, height)
        if bpe not in {1, 2, 3, 4}:
            tex = self._object_map.get(-1)  # never used; quiet linter shape
            raise ValueError(f"Unsupported uncompressed bytes-per-pixel: {bpe}")

        usable = raw[: (len(raw) // int(bpe)) * int(bpe)]
        mode_map = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}
        out_variant = "raw_linear"
        if settings.swizzle_enabled:
            processed, out_w, out_h, variant, _ = core._ps5_unswizzle_best_variant(
                usable,
                width,
                height,
                int(bpe),
                allow_axis_swap=True,
                roughness_guard=bool(settings.roughness_guard),
            )
            image = Image.frombytes(mode_map[int(bpe)], (out_w, out_h), processed)
            out_variant = str(variant)
            if variant == "swapped_axes" and int(getattr(core, "PS5_SWIZZLE_ROTATE", 0)) % 360 != 0:
                image = image.rotate(int(getattr(core, "PS5_SWIZZLE_ROTATE", 0)) % 360, expand=True)
            elif variant in {"already_linear", "normal"}:
                image = ImageOps.flip(image)
            if variant == "addrlib_4KB_S":
                image = ImageOps.flip(image)
        else:
            expected = width * height * int(bpe)
            data = usable[:expected]
            image = Image.frombytes(mode_map[int(bpe)], (width, height), data)
        image = _to_rgba_preview(image)
        if settings.swap_rb:
            image = core._ps5_swap_rb_image(image)
        if settings.flip_tb:
            image = ImageOps.flip(image)
        return image, {"decode_path": "uncompressed", "variant": out_variant, "bytes_per_pixel": int(bpe)}

    def extract_single(
        self,
        path_id: int,
        out_dir: str | Path,
        settings: TexturePreviewSettings,
    ) -> OperationResult:
        try:
            image, meta = self.build_preview(path_id, settings)
        except Exception as exc:
            return OperationResult(False, f"preview failed: {exc}")

        out_root = Path(out_dir).expanduser().resolve()
        out_root.mkdir(parents=True, exist_ok=True)
        safe_assets = _sanitize_name(str(meta.get("assets_name") or "unknown_assets"), fallback="unknown_assets")
        safe_name = _sanitize_name(str(meta.get("name") or f"pathid_{path_id}"))
        base_name = f"{safe_assets}__pid{int(path_id):06d}__{safe_name}"
        png_name = f"{base_name}.png"
        png_path = out_root / png_name
        image.save(png_path)
        meta_path = out_root / f"{base_name}.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return OperationResult(True, "ok", str(png_path), meta=meta)

    def extract_batch(
        self,
        out_dir: str | Path,
        settings_lookup: dict[int, TexturePreviewSettings] | None = None,
        only_path_ids: set[int] | None = None,
    ) -> tuple[list[OperationResult], Path]:
        out_root = Path(out_dir).expanduser().resolve()
        out_root.mkdir(parents=True, exist_ok=True)
        results: list[OperationResult] = []
        manifest: list[dict[str, Any]] = []
        for record in self.records:
            if only_path_ids and int(record.path_id) not in only_path_ids:
                continue
            st = settings_lookup.get(record.path_id) if settings_lookup else None
            if st is None:
                st = self.default_settings_for(record)
            result = self.extract_single(record.path_id, out_root, st)
            results.append(result)
            manifest.append(
                {
                    "path_id": record.path_id,
                    "name": record.name,
                    "status": "ok" if result.ok else "fail",
                    "message": result.message,
                    "output_path": result.output_path,
                    "settings": st.to_dict(),
                }
            )
        manifest_path = out_root / "extract_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return results, manifest_path

    def import_single(
        self,
        path_id: int,
        image_path: str | Path,
        output_bundle_path: str | Path,
        settings: TexturePreviewSettings | None = None,
        allow_resize: bool = True,
        allow_noop: bool = True,
    ) -> OperationResult:
        if self.bundle_path is None:
            return OperationResult(False, "bundle is not loaded")
        image_file = Path(image_path).expanduser().resolve()
        if not image_file.exists():
            return OperationResult(False, f"image not found: {image_file}")
        out_path = Path(output_bundle_path).expanduser().resolve()
        try:
            env = UnityPy.load(str(self.bundle_path))
            target_obj = None
            for obj in env.objects:
                if obj.type.name == "Texture2D" and int(obj.path_id) == int(path_id):
                    target_obj = obj
                    break
            if target_obj is None:
                return OperationResult(False, f"path_id not found: {path_id}")
            tex = target_obj.read()
            target_w = int(getattr(tex, "m_Width", 0) or 0)
            target_h = int(getattr(tex, "m_Height", 0) or 0)
            replacement = Image.open(image_file).convert("RGBA")
            if replacement.size != (target_w, target_h):
                if not allow_resize:
                    return OperationResult(
                        False,
                        f"size mismatch: src={replacement.size}, target={(target_w, target_h)}",
                    )
                replacement = replacement.resize((target_w, target_h), Image.Resampling.LANCZOS)
            record = self.get_record(int(path_id))
            if record is None:
                texture_format = int(getattr(tex, "m_TextureFormat", -1) or -1)
                texture_format_name = str(core._texture_format_enum_name(texture_format))
                record = TextureRecord(
                    bundle_path=str(self.bundle_path),
                    assets_name=_resolve_assets_name(target_obj, tex),
                    path_id=int(path_id),
                    name=str(getattr(tex, "m_Name", "") or ""),
                    width=target_w,
                    height=target_h,
                    mip_count=int(getattr(tex, "m_MipCount", 1) or 1),
                    texture_format=texture_format,
                    texture_format_name=texture_format_name,
                    category=_texture_category(texture_format),
                    is_streamed=bool(int(getattr(getattr(tex, "m_StreamData", None), "size", 0) or 0) > 0),
                    stream_size=int(getattr(getattr(tex, "m_StreamData", None), "size", 0) or 0),
                    is_readable=bool(getattr(tex, "m_IsReadable", False)),
                    raw_size=len(_read_raw_data(target_obj) or b""),
                    auto_swizzle_verdict=None,
                    auto_swizzle_source=None,
                )
            st = self._build_import_settings(record, settings)
            warnings: list[str] = []
            # No-op optimization:
            # If user imports the exact same preview image for this texture/settings,
            # keep original bundle bytes as-is (avoid unnecessary re-encode drift).
            if allow_noop:
                try:
                    current_preview, _ = self.build_preview(int(path_id), st)
                    current_rgba = current_preview.convert("RGBA")
                    incoming_rgba = replacement.convert("RGBA")
                    if current_rgba.size == incoming_rgba.size and current_rgba.tobytes() == incoming_rgba.tobytes():
                        out_path.parent.mkdir(parents=True, exist_ok=True)
                        out_path.write_bytes(Path(self.bundle_path).read_bytes())
                        return OperationResult(
                            True,
                            "ok (no-op: input equals current preview)",
                            str(out_path),
                            meta={"no_op": True},
                        )
                except Exception:
                    pass
            applied = False
            if record.category == "block_compressed":
                applied = self._import_bc_preserve_layout(target_obj, tex, replacement, record, st)
            elif record.category == "uncompressed":
                applied = self._import_uncompressed_preserve_layout(target_obj, tex, replacement, record, st)
            elif record.category == "crunched":
                applied = self._import_crunched_preserve_format(tex, replacement, record, st)
            if not applied:
                if record.category == "crunched":
                    warnings.append(
                        "Crunched format-preserving encode unavailable; fallback import may downgrade format 28/29 -> 10/12."
                    )
                # Fallback path: UnityPy re-encode. Keep inverse preview ops so edited
                # preview images can be re-applied with minimal visual drift.
                if record.category == "block_compressed":
                    fallback = replacement
                else:
                    fallback = self._apply_inverse_preview_ops(replacement, st)
                tex.image = fallback
            tex.save()
            if record.category == "crunched":
                after_fmt = int(getattr(tex, "m_TextureFormat", -1) or -1)
                if after_fmt != int(record.texture_format):
                    warnings.append(
                        f"Crunched texture format changed: {record.texture_format} -> {after_fmt}."
                    )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(env.file.save())
            meta: dict[str, Any] = {}
            if warnings:
                meta["warnings"] = warnings
            return OperationResult(True, "ok", str(out_path), meta=meta)
        except Exception as exc:
            return OperationResult(False, f"import failed: {exc}")

    def import_batch(
        self,
        image_dir: str | Path,
        output_bundle_path: str | Path,
        settings_lookup: dict[int, TexturePreviewSettings] | None = None,
        allow_resize: bool = True,
        allow_noop: bool = True,
    ) -> OperationResult:
        if self.bundle_path is None:
            return OperationResult(False, "bundle is not loaded")
        src_dir = Path(image_dir).expanduser().resolve()
        if not src_dir.exists():
            return OperationResult(False, f"import folder not found: {src_dir}")

        files = [p for p in src_dir.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES]
        if not files:
            return OperationResult(False, "no importable image files found")

        try:
            env = UnityPy.load(str(self.bundle_path))
            by_pid: dict[int, Any] = {}
            by_name: dict[str, Any] = {}
            for obj in env.objects:
                if obj.type.name != "Texture2D":
                    continue
                by_pid[int(obj.path_id)] = obj
                try:
                    name = str(obj.read().m_Name or "")
                except Exception:
                    name = ""
                if name:
                    by_name[name] = obj

            applied: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            warnings: list[dict[str, Any]] = []
            for file in files:
                stem = file.stem
                target_obj = None

                m = re.match(r"^(\d{1,20})_", stem)
                if m:
                    try:
                        pid = int(m.group(1))
                        target_obj = by_pid.get(pid)
                    except Exception:
                        target_obj = None
                if target_obj is None:
                    target_obj = by_name.get(stem)
                if target_obj is None:
                    skipped.append({"file": str(file), "reason": "no_matching_texture"})
                    continue

                tex = target_obj.read()
                target_w = int(getattr(tex, "m_Width", 0) or 0)
                target_h = int(getattr(tex, "m_Height", 0) or 0)
                replacement = Image.open(file).convert("RGBA")
                if replacement.size != (target_w, target_h):
                    if not allow_resize:
                        skipped.append(
                            {
                                "file": str(file),
                                "path_id": int(target_obj.path_id),
                                "name": str(getattr(tex, "m_Name", "") or ""),
                                "reason": f"size_mismatch:{replacement.size}->{(target_w, target_h)}",
                            }
                        )
                        continue
                    replacement = replacement.resize((target_w, target_h), Image.Resampling.LANCZOS)
                pid = int(target_obj.path_id)
                record = self.get_record(pid)
                if record is None:
                    texture_format = int(getattr(tex, "m_TextureFormat", -1) or -1)
                    texture_format_name = str(core._texture_format_enum_name(texture_format))
                    record = TextureRecord(
                        bundle_path=str(self.bundle_path),
                        assets_name=_resolve_assets_name(target_obj, tex),
                        path_id=pid,
                        name=str(getattr(tex, "m_Name", "") or ""),
                        width=target_w,
                        height=target_h,
                        mip_count=int(getattr(tex, "m_MipCount", 1) or 1),
                        texture_format=texture_format,
                        texture_format_name=texture_format_name,
                        category=_texture_category(texture_format),
                        is_streamed=bool(int(getattr(getattr(tex, "m_StreamData", None), "size", 0) or 0) > 0),
                        stream_size=int(getattr(getattr(tex, "m_StreamData", None), "size", 0) or 0),
                        is_readable=bool(getattr(tex, "m_IsReadable", False)),
                        raw_size=len(_read_raw_data(target_obj) or b""),
                        auto_swizzle_verdict=None,
                        auto_swizzle_source=None,
                    )
                st = (settings_lookup or {}).get(pid)
                st = self._build_import_settings(record, st)
                # Batch no-op optimization for unchanged textures.
                if allow_noop:
                    try:
                        current_preview, _ = self.build_preview(pid, st)
                        current_rgba = current_preview.convert("RGBA")
                        incoming_rgba = replacement.convert("RGBA")
                        if current_rgba.size == incoming_rgba.size and current_rgba.tobytes() == incoming_rgba.tobytes():
                            skipped.append(
                                {
                                    "file": str(file),
                                    "path_id": pid,
                                    "name": str(getattr(tex, "m_Name", "") or ""),
                                    "reason": "no_op_same_as_current_preview",
                                }
                            )
                            continue
                    except Exception:
                        pass
                applied_ok = False
                if record.category == "block_compressed":
                    applied_ok = self._import_bc_preserve_layout(target_obj, tex, replacement, record, st)
                elif record.category == "uncompressed":
                    applied_ok = self._import_uncompressed_preserve_layout(target_obj, tex, replacement, record, st)
                elif record.category == "crunched":
                    applied_ok = self._import_crunched_preserve_format(tex, replacement, record, st)
                if not applied_ok:
                    if record.category == "crunched":
                        warnings.append(
                            {
                                "file": str(file),
                                "path_id": pid,
                                "name": str(getattr(tex, "m_Name", "") or ""),
                                "warning": "crunched_format_preserve_failed_fallback_used (may downgrade 28/29 -> 10/12)",
                            }
                        )
                    if record.category == "block_compressed":
                        tex.image = replacement
                    else:
                        tex.image = self._apply_inverse_preview_ops(replacement, st)
                tex.save()
                if record.category == "crunched":
                    after_fmt = int(getattr(tex, "m_TextureFormat", -1) or -1)
                    if after_fmt != int(record.texture_format):
                        warnings.append(
                            {
                                "file": str(file),
                                "path_id": pid,
                                "name": str(getattr(tex, "m_Name", "") or ""),
                                "warning": f"crunched_format_changed:{record.texture_format}->{after_fmt}",
                            }
                        )
                applied.append(
                    {
                        "file": str(file),
                        "path_id": int(target_obj.path_id),
                        "name": str(getattr(tex, "m_Name", "") or ""),
                    }
                )

            out_path = Path(output_bundle_path).expanduser().resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(env.file.save())
            meta = {
                "applied": applied,
                "skipped": skipped,
                "warnings": warnings,
                "applied_count": len(applied),
                "skipped_count": len(skipped),
                "warning_count": len(warnings),
            }
            return OperationResult(True, "ok", str(out_path), meta=meta)
        except Exception as exc:
            return OperationResult(False, f"batch import failed: {exc}")
