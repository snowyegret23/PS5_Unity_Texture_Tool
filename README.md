# PS5 Unity Texture Tool (Flet)

Python/Flet GUI tool for PS5 Unity textures:

- Texture preview
- Single extract
- Batch extract (folder)
- Single import
- Batch import (folder)
- `config.json` persistence (recent paths, settings, operation history)
- Auto-detect swizzle/mip metadata per texture
- Manual override for swizzle/BC mode/pipe/xor/offset

## Requirements

- Python 3.12+
- Windows (tested)

Install:

```powershell
python -m pip install -r requirements.txt
```

## Run

```powershell
python app.py
```

## Notes

- The app contains its own local PS5 texture core (`ps5_tool/ps5_core.py`); it does not import from `Unity_Font_Replacer`.
- Crunched formats (`DXT1Crunched`, `DXT5Crunched`) are handled via UnityPy decode path.
- Import saves to `Output Bundle Path`; original bundle is not overwritten unless you choose the same path.

## Config

`config.json` is created in project root and stores:

- recent bundle paths
- last extract/import paths
- global preview settings
- per-texture override settings
- operation history
