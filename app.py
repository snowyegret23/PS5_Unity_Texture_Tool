from __future__ import annotations

import base64
import io
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import flet as ft

from ps5_tool.backend import TextureBackend
from ps5_tool.config_store import ConfigStore
from ps5_tool.constants import DEFAULT_BC_SWIZZLE_MODES
from ps5_tool.models import TexturePreviewSettings, TextureRecord


def _runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_ROOT = _runtime_root()
CONFIG_PATH = APP_ROOT / "config.json"
PREVIEW_CACHE_DIR = APP_ROOT / ".preview_cache"
MODIFIED_DIR = APP_ROOT / "modified"
MAIN_PANEL_HEIGHT = 820
BOTTOM_PANEL_HEIGHT = 180
PREVIEW_BOX_HEIGHT = 360
BLANK_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO2pB9sAAAAASUVORK5CYII="
)


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _bool_text(value: bool) -> str:
    return "yes" if value else "no"


def _texture_line(r: TextureRecord) -> str:
    return (
        f"[{r.path_id}] {r.name or '(unnamed)'} | "
        f"{r.texture_format_name} ({r.width}x{r.height}, mip={r.mip_count}) | "
        f"cat={r.category} stream={_bool_text(r.is_streamed)} swz={r.auto_swizzle_verdict or 'n/a'}"
    )


def _default_output_bundle_path(bundle_path: str | Path) -> Path:
    src = Path(bundle_path)
    MODIFIED_DIR.mkdir(parents=True, exist_ok=True)
    if src.suffix:
        return MODIFIED_DIR / src.name
    return MODIFIED_DIR / f"{src.name}.bundle"


def main(page: ft.Page) -> None:
    page.title = "PS5 Unity Texture Tool"
    page.theme_mode = ft.ThemeMode.DARK
    try:
        page.window.width = 1840
        page.window.height = 1080
        page.window.min_width = 1600
        page.window.min_height = 920
    except Exception:
        pass
    page.padding = 8
    page.scroll = ft.ScrollMode.HIDDEN

    PREVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    MODIFIED_DIR.mkdir(parents=True, exist_ok=True)

    config = ConfigStore(CONFIG_PATH)
    backend = TextureBackend()

    records: list[TextureRecord] = []
    selected_path_id: int | None = None
    suspend_live_preview = False

    # --- Controls: paths
    bundle_path_tf = ft.TextField(label="Bundle File", expand=True, dense=True)
    extract_dir_tf = ft.TextField(label="Extract Output Folder", expand=True, dense=True)
    import_image_tf = ft.TextField(label="Import Image (single)", expand=True, dense=True)
    import_dir_tf = ft.TextField(label="Import Folder", expand=True, dense=True)
    output_bundle_tf = ft.TextField(
        label="Output Bundle Path (auto: modified folder)",
        expand=True,
        dense=True,
        read_only=True,
    )
    search_tf = ft.TextField(label="Search texture name / path_id", dense=True, expand=True)

    recent_bundle_dd = ft.Dropdown(
        label="Recent Bundles",
        options=[ft.dropdown.Option(x) for x in config.data.get("recent_bundle_paths", [])],
        dense=True,
        expand=True,
    )

    # --- Controls: settings
    swizzle_enabled_sw = ft.Switch(label="Input is swizzled (apply unswizzle)", value=True)
    swap_rb_sw = ft.Switch(label="Swap R/B (preview)", value=True)
    flip_tb_sw = ft.Switch(label="Flip Top/Bottom (preview)", value=True)
    use_mip_sw = ft.Switch(label="Use mip_count for BC auto mode", value=True)
    roughness_guard_sw = ft.Switch(label="Roughness guard (uncompressed)", value=True)

    bc_mode_dd = ft.Dropdown(
        label="BC Mode",
        options=[ft.dropdown.Option(x) for x in DEFAULT_BC_SWIZZLE_MODES],
        value="auto",
        dense=True,
        width=180,
    )
    pipe_dd = ft.Dropdown(
        label="Pipe log2",
        options=[ft.dropdown.Option(str(x)) for x in [2, 1, 3, 0]],
        value="2",
        dense=True,
        width=120,
    )
    xor_tf = ft.TextField(label="Pipe/Bank XOR", value="0", dense=True, width=140)
    mip_offset_tf = ft.TextField(label="Mip0 Offset Override (empty=auto)", value="", dense=True, width=260)

    # --- Controls: output
    preview_image = ft.Image(src=BLANK_PNG_BYTES, fit=ft.BoxFit.CONTAIN, expand=True)
    preview_meta_tf = ft.TextField(
        label="Preview Metadata",
        multiline=True,
        min_lines=8,
        max_lines=8,
        read_only=True,
        height=136,
    )
    log_tf = ft.TextField(
        label="Log",
        multiline=True,
        min_lines=8,
        max_lines=8,
        read_only=True,
        height=136,
    )
    progress = ft.ProgressBar(value=0, width=120, bar_height=4, opacity=0.25)

    texture_list_view = ft.ListView(expand=True, spacing=0, padding=0, auto_scroll=False)

    # --- Pickers
    bundle_picker = ft.FilePicker()
    extract_dir_picker = ft.FilePicker()
    import_image_picker = ft.FilePicker()
    import_dir_picker = ft.FilePicker()

    def log(msg: str) -> None:
        line = f"[{_now()}] {msg}"
        log_tf.value = (log_tf.value + "\n" + line).strip()
        page.update()

    def set_busy(is_busy: bool) -> None:
        progress.value = None if is_busy else 0
        progress.opacity = 1.0 if is_busy else 0.25
        page.update()

    def log_result_warnings(meta: dict[str, Any] | None) -> None:
        if not isinstance(meta, dict):
            return
        items = meta.get("warnings")
        if not isinstance(items, list) or not items:
            return
        for w in items:
            if isinstance(w, dict):
                pid = w.get("path_id")
                name = str(w.get("name") or "").strip()
                file = str(w.get("file") or "").strip()
                text = str(w.get("warning") or "").strip()
                parts: list[str] = []
                if pid is not None and str(pid) != "":
                    parts.append(f"path_id={pid}")
                if name:
                    parts.append(name)
                if file:
                    parts.append(file)
                prefix = " | ".join(parts)
                if prefix and text:
                    log(f"Warning: {prefix} - {text}")
                elif text:
                    log(f"Warning: {text}")
                elif prefix:
                    log(f"Warning: {prefix}")
            else:
                log(f"Warning: {w}")

    def selected_record() -> TextureRecord | None:
        nonlocal selected_path_id
        if selected_path_id is None:
            return None
        return backend.get_record(selected_path_id)

    def ensure_output_bundle_path() -> str:
        if backend.bundle_path is not None:
            p = _default_output_bundle_path(backend.bundle_path)
        else:
            p = MODIFIED_DIR / "output.bundle"
        output_bundle_tf.value = str(p)
        page.update()
        return str(p)

    def settings_from_controls() -> TexturePreviewSettings:
        xor_value = 0
        try:
            xor_value = int((xor_tf.value or "0").strip())
        except Exception:
            xor_value = 0
        mip_off = None
        text = (mip_offset_tf.value or "").strip()
        if text:
            try:
                mip_off = int(text)
            except Exception:
                mip_off = None
        pipe_log2 = 2
        try:
            pipe_log2 = int(pipe_dd.value or "2")
        except Exception:
            pipe_log2 = 2
        return TexturePreviewSettings(
            swizzle_enabled=bool(swizzle_enabled_sw.value),
            swap_rb=bool(swap_rb_sw.value),
            flip_tb=bool(flip_tb_sw.value),
            bc_mode=str(bc_mode_dd.value or "auto"),
            pipe_log2=pipe_log2,
            pipe_bank_xor=xor_value,
            mip_offset_override=mip_off,
            use_mip_count=bool(use_mip_sw.value),
            roughness_guard=bool(roughness_guard_sw.value),
        )

    def apply_settings_to_controls(st: TexturePreviewSettings) -> None:
        nonlocal suspend_live_preview
        suspend_live_preview = True
        swizzle_enabled_sw.value = bool(st.swizzle_enabled)
        swap_rb_sw.value = bool(st.swap_rb)
        flip_tb_sw.value = bool(st.flip_tb)
        bc_mode_dd.value = st.bc_mode if st.bc_mode in DEFAULT_BC_SWIZZLE_MODES else "auto"
        pipe_dd.value = str(st.pipe_log2)
        xor_tf.value = str(st.pipe_bank_xor)
        mip_offset_tf.value = "" if st.mip_offset_override is None else str(st.mip_offset_override)
        use_mip_sw.value = bool(st.use_mip_count)
        roughness_guard_sw.value = bool(st.roughness_guard)
        suspend_live_preview = False

    def live_preview_on_setting_change(_e: ft.ControlEvent | None = None) -> None:
        if suspend_live_preview:
            return
        if selected_record() is None:
            return
        do_preview()

    def combined_settings_for_record(record: TextureRecord) -> TexturePreviewSettings:
        default_auto = backend.default_settings_for(record)
        global_st = TexturePreviewSettings.from_dict(config.get_global_settings())
        # Keep auto swizzle defaults unless user explicitly overrode texture settings.
        base = global_st
        base.swizzle_enabled = default_auto.swizzle_enabled
        base.swap_rb = default_auto.swap_rb
        base.flip_tb = default_auto.flip_tb
        override = config.get_override(record.bundle_path, record.path_id)
        if isinstance(override, dict) and bool(override.get("__manual_override", False)):
            merged = base.to_dict()
            merged.update(override)
            return TexturePreviewSettings.from_dict(merged)
        return base

    def save_current_override() -> None:
        rec = selected_record()
        if rec is None:
            return
        st = settings_from_controls()
        payload = st.to_dict()
        payload["__manual_override"] = True
        config.set_override(rec.bundle_path, rec.path_id, payload)
        log(f"Saved override for path_id={rec.path_id}")

    def save_global_settings() -> None:
        st = settings_from_controls()
        config.set_global_settings(st.to_dict())
        log("Saved global preview settings")

    def clear_current_override() -> None:
        rec = selected_record()
        if rec is None:
            return
        config.clear_override(rec.bundle_path, rec.path_id)
        apply_settings_to_controls(combined_settings_for_record(rec))
        page.update()
        log(f"Cleared override for path_id={rec.path_id}")

    def refresh_texture_list() -> None:
        query = (search_tf.value or "").strip().lower()
        texture_list_view.controls.clear()
        for rec in records:
            if query:
                by_name = query in rec.name.lower()
                by_pid = query in str(rec.path_id)
                if not by_name and not by_pid:
                    continue
            tile = ft.ListTile(
                dense=True,
                title=ft.Text(f"[{rec.path_id}] {rec.name or '(unnamed)'}", size=13),
                subtitle=ft.Text(
                    f"{rec.texture_format_name} | {rec.width}x{rec.height} | mip={rec.mip_count} | "
                    f"cat={rec.category} | swz={rec.auto_swizzle_verdict or 'n/a'}",
                    size=11,
                ),
                data=rec.path_id,
            )

            def _make_click(pid: int):
                def _on_click(_e: ft.ControlEvent) -> None:
                    nonlocal selected_path_id
                    selected_path_id = int(pid)
                    rec2 = backend.get_record(selected_path_id)
                    if rec2 is not None:
                        st = combined_settings_for_record(rec2)
                        apply_settings_to_controls(st)
                        page.update()
                        preview_meta_tf.value = json.dumps(
                            {
                                "selected": {
                                    "assets_name": rec2.assets_name,
                                    "path_id": rec2.path_id,
                                    "name": rec2.name,
                                    "format": rec2.texture_format_name,
                                    "size": [rec2.width, rec2.height],
                                    "mip_count": rec2.mip_count,
                                    "category": rec2.category,
                                    "auto_swizzle": rec2.auto_swizzle_verdict,
                                    "auto_swizzle_source": rec2.auto_swizzle_source,
                                    "raw_size": rec2.raw_size,
                                    "stream_size": rec2.stream_size,
                                }
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    log(f"Selected path_id={pid}")
                    do_preview()

                return _on_click

            tile.on_click = _make_click(rec.path_id)
            texture_list_view.controls.append(tile)
        page.update()

    def do_load_bundle(bundle_path: str) -> None:
        nonlocal records, selected_path_id
        if not bundle_path:
            log("Bundle path is empty")
            return
        try:
            set_busy(True)
            records = backend.load_bundle(bundle_path)
            selected_path_id = None
            config.add_recent_bundle(bundle_path)
            recent_bundle_dd.options = [ft.dropdown.Option(x) for x in config.data.get("recent_bundle_paths", [])]
            output_bundle_tf.value = str(_default_output_bundle_path(bundle_path))
            config.data["last_output_bundle"] = output_bundle_tf.value
            log(f"Loaded bundle: {bundle_path} ({len(records)} textures)")
            config.append_history("load_bundle", {"bundle": bundle_path, "texture_count": len(records)})
            refresh_texture_list()
        except Exception as exc:
            log(f"Failed to load bundle: {exc}")
        finally:
            set_busy(False)

    def do_preview(_e: ft.ControlEvent | None = None) -> None:
        rec = selected_record()
        if rec is None:
            log("No texture selected")
            return
        st = settings_from_controls()
        try:
            set_busy(True)
            image, meta = backend.build_preview(rec.path_id, st)
            png_buf = io.BytesIO()
            image.save(png_buf, format="PNG")
            preview_image.src = png_buf.getvalue()
            preview_image.key = str(datetime.now().timestamp())
            preview_meta_tf.value = json.dumps(meta, ensure_ascii=False, indent=2)
            config.append_history(
                "preview",
                {"bundle": rec.bundle_path, "path_id": rec.path_id, "name": rec.name, "settings": st.to_dict()},
            )
            log(f"Preview updated: {rec.path_id} ({rec.name})")
        except Exception as exc:
            log(f"Preview failed: {exc}")
        finally:
            set_busy(False)

    def do_extract_selected(_e: ft.ControlEvent | None = None) -> None:
        rec = selected_record()
        if rec is None:
            log("No texture selected")
            return
        out_dir = (extract_dir_tf.value or "").strip()
        if not out_dir:
            log("Extract output folder is empty")
            return
        st = settings_from_controls()
        set_busy(True)
        result = backend.extract_single(rec.path_id, out_dir, st)
        set_busy(False)
        if result.ok:
            config.data["last_extract_dir"] = out_dir
            config.append_history(
                "extract_single",
                {"bundle": rec.bundle_path, "path_id": rec.path_id, "output_path": result.output_path},
            )
            config.save()
            log(f"Extracted: {result.output_path}")
        else:
            log(f"Extract failed: {result.message}")

    def do_extract_all(_e: ft.ControlEvent | None = None) -> None:
        out_dir = (extract_dir_tf.value or "").strip()
        if not out_dir:
            log("Extract output folder is empty")
            return
        set_busy(True)
        settings_lookup: dict[int, TexturePreviewSettings] = {}
        for rec in records:
            ov = config.get_override(rec.bundle_path, rec.path_id)
            settings_lookup[rec.path_id] = TexturePreviewSettings.from_dict(ov) if ov else combined_settings_for_record(rec)
        results, manifest_path = backend.extract_batch(out_dir, settings_lookup=settings_lookup)
        set_busy(False)
        ok_count = sum(1 for r in results if r.ok)
        fail_count = len(results) - ok_count
        config.data["last_extract_dir"] = out_dir
        config.append_history(
            "extract_batch",
            {"bundle": str(backend.bundle_path), "total": len(results), "ok": ok_count, "fail": fail_count},
        )
        config.save()
        log(f"Batch extract done: ok={ok_count}, fail={fail_count}, manifest={manifest_path}")

    def do_import_selected(_e: ft.ControlEvent | None = None) -> None:
        rec = selected_record()
        if rec is None:
            log("No texture selected")
            return
        image_path = (import_image_tf.value or "").strip()
        output_bundle = ensure_output_bundle_path()
        st = settings_from_controls()
        if not image_path:
            log("Import image path is empty")
            return
        set_busy(True)
        result = backend.import_single(
            rec.path_id,
            image_path,
            output_bundle,
            settings=st,
            allow_resize=True,
        )
        set_busy(False)
        if result.ok:
            config.data["last_import_image"] = image_path
            config.data["last_output_bundle"] = output_bundle
            config.append_history(
                "import_single",
                {
                    "bundle": rec.bundle_path,
                    "path_id": rec.path_id,
                    "image_path": image_path,
                    "output_bundle_path": result.output_path,
                },
            )
            config.save()
            log(f"Import(single) done: {result.output_path}")
            log_result_warnings(result.meta)
        else:
            log(f"Import(single) failed: {result.message}")

    def do_import_batch(_e: ft.ControlEvent | None = None) -> None:
        import_dir = (import_dir_tf.value or "").strip()
        output_bundle = ensure_output_bundle_path()
        if not import_dir:
            log("Import folder path is empty")
            return
        settings_lookup: dict[int, TexturePreviewSettings] = {}
        for rec in records:
            ov = config.get_override(rec.bundle_path, rec.path_id)
            settings_lookup[rec.path_id] = (
                TexturePreviewSettings.from_dict(ov) if ov else combined_settings_for_record(rec)
            )
        set_busy(True)
        result = backend.import_batch(
            import_dir,
            output_bundle,
            settings_lookup=settings_lookup,
            allow_resize=True,
        )
        set_busy(False)
        if result.ok:
            config.data["last_import_dir"] = import_dir
            config.data["last_output_bundle"] = output_bundle
            config.append_history(
                "import_batch",
                {
                    "bundle": str(backend.bundle_path),
                    "import_dir": import_dir,
                    "output_bundle_path": result.output_path,
                    "applied_count": int(result.meta.get("applied_count", 0)),
                    "skipped_count": int(result.meta.get("skipped_count", 0)),
                },
            )
            config.save()
            log(
                f"Import(batch) done: out={result.output_path}, "
                f"applied={result.meta.get('applied_count', 0)}, "
                f"skipped={result.meta.get('skipped_count', 0)}, "
                f"warnings={result.meta.get('warning_count', 0)}"
            )
            log_result_warnings(result.meta)
        else:
            log(f"Import(batch) failed: {result.message}")

    async def pick_bundle(_e: ft.ControlEvent) -> None:
        files = await bundle_picker.pick_files(
            allow_multiple=False,
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=["bundle", "assets"],
        )
        if files:
            chosen = str(files[0].path or "")
            bundle_path_tf.value = chosen
            page.update()
            do_load_bundle(chosen)

    async def pick_extract_dir(_e: ft.ControlEvent) -> None:
        path = await extract_dir_picker.get_directory_path()
        if path:
            extract_dir_tf.value = str(path)
            page.update()

    async def pick_import_image(_e: ft.ControlEvent) -> None:
        files = await import_image_picker.pick_files(
            allow_multiple=False,
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=["png", "jpg", "jpeg", "bmp", "webp", "tga", "dds"],
        )
        if files:
            import_image_tf.value = str(files[0].path or "")
            page.update()
            do_import_selected()

    async def pick_import_dir_and_batch(_e: ft.ControlEvent) -> None:
        path = await import_dir_picker.get_directory_path()
        if path:
            import_dir_tf.value = str(path)
            page.update()
            do_import_batch()

    def do_save_file(_e: ft.ControlEvent | None = None) -> None:
        if backend.bundle_path is None:
            log("Bundle is not loaded")
            return
        output_bundle = ensure_output_bundle_path()
        rec = selected_record()
        image_path = (import_image_tf.value or "").strip()
        import_dir = (import_dir_tf.value or "").strip()

        # Prefer single import save when an image and selected texture are present.
        if image_path and rec is not None:
            do_import_selected()
            return
        # Fallback to batch import save when import folder is provided.
        if import_dir:
            do_import_batch()
            return

        # No import source provided: save a clean copy of the currently loaded bundle.
        try:
            src = Path(backend.bundle_path)
            dst = Path(output_bundle)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            config.data["last_output_bundle"] = str(dst)
            config.append_history(
                "save_file_copy",
                {"source_bundle": str(src), "output_bundle_path": str(dst)},
            )
            log(f"Saved bundle copy: {dst}")
        except Exception as exc:
            log(f"Save File failed: {exc}")

    def on_recent_change(_e: ft.ControlEvent) -> None:
        if recent_bundle_dd.value:
            chosen = str(recent_bundle_dd.value)
            bundle_path_tf.value = chosen
            page.update()
            do_load_bundle(chosen)

    recent_bundle_dd.on_select = on_recent_change
    search_tf.on_change = lambda _e: refresh_texture_list()
    bundle_path_tf.on_submit = lambda _e: do_load_bundle(bundle_path_tf.value)
    swizzle_enabled_sw.on_change = live_preview_on_setting_change
    swap_rb_sw.on_change = live_preview_on_setting_change
    flip_tb_sw.on_change = live_preview_on_setting_change
    use_mip_sw.on_change = live_preview_on_setting_change
    roughness_guard_sw.on_change = live_preview_on_setting_change
    bc_mode_dd.on_select = live_preview_on_setting_change
    pipe_dd.on_select = live_preview_on_setting_change
    xor_tf.on_submit = live_preview_on_setting_change
    mip_offset_tf.on_submit = live_preview_on_setting_change

    # restore config-based defaults
    bundle_recent = config.data.get("recent_bundle_paths", [])
    if bundle_recent:
        bundle_path_tf.value = str(bundle_recent[0])
    extract_dir_tf.value = str(config.data.get("last_extract_dir", ""))
    import_image_tf.value = str(config.data.get("last_import_image", ""))
    import_dir_tf.value = str(config.data.get("last_import_dir", ""))
    output_bundle_tf.value = str(MODIFIED_DIR / "output.bundle")
    apply_settings_to_controls(TexturePreviewSettings.from_dict(config.get_global_settings()))

    settings_panel = ft.Column(
        spacing=6,
        controls=[
            ft.Text("Swizzle / Preview", weight=ft.FontWeight.BOLD),
            ft.Row([swizzle_enabled_sw, swap_rb_sw, flip_tb_sw]),
            ft.Row([use_mip_sw, roughness_guard_sw]),
            ft.Row([bc_mode_dd, pipe_dd, xor_tf]),
            ft.Row([mip_offset_tf]),
            ft.Row(
                [
                    ft.Button("Preview", on_click=do_preview),
                    ft.OutlinedButton("Save Override", on_click=lambda _e: save_current_override()),
                    ft.OutlinedButton("Clear Override", on_click=lambda _e: clear_current_override()),
                    ft.OutlinedButton("Save Global", on_click=lambda _e: save_global_settings()),
                ],
            ),
        ],
    )

    workspace_panel = ft.Container(
        expand=24,
        height=MAIN_PANEL_HEIGHT,
        border=ft.Border.all(1, ft.Colors.OUTLINE),
        border_radius=6,
        padding=8,
        content=ft.Column(
            expand=True,
            scroll=ft.ScrollMode.AUTO,
            spacing=8,
            controls=[
                ft.Row([ft.Text("Workspace", weight=ft.FontWeight.BOLD), progress]),
                bundle_path_tf,
                ft.Row(
                    [
                        ft.Button("Browse", on_click=pick_bundle),
                    ]
                ),
                recent_bundle_dd,
                ft.Divider(height=1),
                ft.Text("Extract", weight=ft.FontWeight.W_600),
                extract_dir_tf,
                ft.Row(
                    [
                        ft.Button("Extract Dir", on_click=pick_extract_dir),
                        ft.Button("Extract Selected", on_click=do_extract_selected),
                        ft.Button("Extract All", on_click=do_extract_all),
                    ],
                ),
                ft.Divider(height=1),
                ft.Text("Import", weight=ft.FontWeight.W_600),
                import_image_tf,
                ft.Row(
                    [
                        ft.Button("Import Image", on_click=pick_import_image),
                    ],
                ),
                import_dir_tf,
                output_bundle_tf,
                ft.Row(
                    [
                        ft.Button("Save File", on_click=do_save_file),
                        ft.Button("Import Folder", on_click=pick_import_dir_and_batch),
                    ],
                ),
            ],
        ),
    )

    texture_list_panel = ft.Container(
        expand=46,
        height=MAIN_PANEL_HEIGHT,
        border=ft.Border.all(1, ft.Colors.OUTLINE),
        border_radius=6,
        padding=8,
        content=ft.Column(
            expand=True,
            controls=[
                ft.Row(
                    [search_tf, ft.Button("Refresh List", on_click=lambda _e: refresh_texture_list())]
                ),
                ft.Container(
                    border=ft.Border.all(1, ft.Colors.OUTLINE),
                    border_radius=6,
                    padding=6,
                    expand=True,
                    content=texture_list_view,
                ),
            ],
        ),
    )

    inspector_panel = ft.Container(
        expand=30,
        height=MAIN_PANEL_HEIGHT,
        border=ft.Border.all(1, ft.Colors.OUTLINE),
        border_radius=6,
        padding=8,
        content=ft.Column(
            expand=True,
            scroll=ft.ScrollMode.HIDDEN,
            spacing=8,
            controls=[
                ft.Text("Inspector", weight=ft.FontWeight.BOLD),
                settings_panel,
                ft.Container(
                    height=PREVIEW_BOX_HEIGHT,
                    border=ft.Border.all(1, ft.Colors.OUTLINE),
                    border_radius=6,
                    padding=8,
                    alignment=ft.Alignment(0, 0),
                    content=preview_image,
                ),
            ],
        ),
    )

    log_panel = ft.Container(
        expand=1,
        height=BOTTOM_PANEL_HEIGHT,
        border=ft.Border.all(1, ft.Colors.OUTLINE),
        border_radius=6,
        padding=8,
        content=log_tf,
    )
    meta_panel = ft.Container(
        expand=1,
        height=BOTTOM_PANEL_HEIGHT,
        border=ft.Border.all(1, ft.Colors.OUTLINE),
        border_radius=6,
        padding=8,
        content=preview_meta_tf,
    )

    page.add(
        ft.Row(
            [workspace_panel, texture_list_panel, inspector_panel],
            expand=True,
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.START,
        ),
        ft.Row(
            [log_panel, meta_panel],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.START,
        ),
    )
    log("Ready")
    if bundle_path_tf.value:
        log("Select or load a bundle to start.")


if __name__ == "__main__":
    ft.run(main)
