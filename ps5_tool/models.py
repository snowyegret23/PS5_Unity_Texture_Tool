from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class TextureRecord:
    bundle_path: str
    assets_name: str
    path_id: int
    name: str
    width: int
    height: int
    mip_count: int
    texture_format: int
    texture_format_name: str
    category: str
    is_streamed: bool
    stream_size: int
    is_readable: bool
    raw_size: int
    auto_swizzle_verdict: str | None
    auto_swizzle_source: str | None


@dataclass(slots=True)
class TexturePreviewSettings:
    swizzle_enabled: bool = True
    swap_rb: bool = True
    flip_tb: bool = True
    bc_mode: str = "auto"
    pipe_log2: int = 2
    pipe_bank_xor: int = 0
    mip_offset_override: int | None = None
    use_mip_count: bool = True
    roughness_guard: bool = True

    def to_dict(self) -> dict:
        return {
            "swizzle_enabled": self.swizzle_enabled,
            "swap_rb": self.swap_rb,
            "flip_tb": self.flip_tb,
            "bc_mode": self.bc_mode,
            "pipe_log2": self.pipe_log2,
            "pipe_bank_xor": self.pipe_bank_xor,
            "mip_offset_override": self.mip_offset_override,
            "use_mip_count": self.use_mip_count,
            "roughness_guard": self.roughness_guard,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "TexturePreviewSettings":
        if not isinstance(data, dict):
            return cls()
        return cls(
            swizzle_enabled=bool(data.get("swizzle_enabled", True)),
            swap_rb=bool(data.get("swap_rb", True)),
            flip_tb=bool(data.get("flip_tb", True)),
            bc_mode=str(data.get("bc_mode", "auto")),
            pipe_log2=int(data.get("pipe_log2", 2)),
            pipe_bank_xor=int(data.get("pipe_bank_xor", 0)),
            mip_offset_override=(
                int(data["mip_offset_override"])
                if data.get("mip_offset_override") not in (None, "", "auto")
                else None
            ),
            use_mip_count=bool(data.get("use_mip_count", True)),
            roughness_guard=bool(data.get("roughness_guard", True)),
        )


@dataclass(slots=True)
class OperationResult:
    ok: bool
    message: str
    output_path: str | None = None
    meta: dict = field(default_factory=dict)
