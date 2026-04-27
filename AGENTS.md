# Repository Guidelines

## Project Structure & Module Organization

This repository is a compact Python image-overlay utility. The main application starts from `merge_logo_gui.py`; it delegates argument parsing and export logic to `image_overlay_tool/cli.py`, keeps Pillow image processing in `image_overlay_tool/core.py`, and uses `image_overlay_tool/gui_tk.py` for the CustomTkinter editor UI. User-supplied images are selected at runtime or passed with CLI flags. Treat `__pycache__/` as generated output and leave it out of reviews and commits.

## Build, Test, and Development Commands

- `python3 merge_logo_gui.py` launches the CustomTkinter GUI for selecting a base image and overlay image.
- `python3 merge_logo_gui.py --base base.png --overlay logo.png --out merged.png --save-immediately` runs a non-interactive export.
- `python3 -m py_compile merge_logo_gui.py image_overlay_tool/*.py tests/test_core.py` performs a quick syntax check.
- `python3 -m unittest tests.test_core` runs the core test suite.
- `python3 -m pip install -r requirements.txt` installs Pillow, CustomTkinter, and packaging tools in a fresh environment.

SVG import is supported for source images, but SVG export is intentionally unsupported; keep export format support focused on common bitmap files.

## Coding Style & Naming Conventions

Use Python 3 style with 4-space indentation, type hints, and small helper functions. Keep dataclasses such as `Placement` immutable unless a real stateful need appears. Use `snake_case` for functions, variables, and CLI flags; use `PascalCase` for CustomTkinter/Tkinter classes. Keep bilingual UI strings in `image_overlay_tool/i18n.py`, and add matching `zh` and `en` entries for every new interface label or message.

## Testing Guidelines

Before submitting changes, run `python3 -m py_compile merge_logo_gui.py image_overlay_tool/*.py tests/test_core.py` and `python3 -m unittest tests.test_core`. For placement changes, test edge positions, oversized overlays, JPG output, transparent PNG output, and SVG input if a local SVG renderer is available.

## Commit & Pull Request Guidelines

Use short, imperative commit messages such as `Fix overlay clamping` or `Add English selector text`. Pull requests should describe the user-facing behavior changed, list manual test commands, mention image formats tested, and include screenshots when GUI layout or preview behavior changes.

## Security & Configuration Tips

Do not hardcode local image paths or environment-specific browser paths. Preserve the current behavior of resolving relative input files from the script directory, and keep subprocess calls argument-list based rather than shell-string based.
