# Repository Guidelines

## Project Structure & Module Organization

This repository is a compact Python image-overlay utility. The main application lives in `merge_logo_gui.py`; it contains argument parsing, image loading and SVG rasterization helpers, placement logic, the Tkinter selection dialog, the editor UI, and the CLI export path. There is no package directory, test directory, or bundled asset tree. User-supplied images are selected at runtime or passed with CLI flags. Treat `__pycache__/` as generated output and leave it out of reviews and commits.

## Build, Test, and Development Commands

- `python3 merge_logo_gui.py` launches the Tkinter GUI for selecting a base image and overlay image.
- `python3 merge_logo_gui.py --base base.png --overlay logo.png --out merged.png --save-immediately` runs a non-interactive export.
- `python3 -m py_compile merge_logo_gui.py` performs a quick syntax check.
- `python3 -m pip install pillow` installs the required Pillow dependency when working in a fresh environment.

SVG support may require a Chrome-compatible browser for transparent rasterization and ImageMagick's `convert` command as a fallback for difficult image formats.

## Coding Style & Naming Conventions

Use Python 3 style with 4-space indentation, type hints, and small helper functions. Keep dataclasses such as `Placement` immutable unless a real stateful need appears. Use `snake_case` for functions, variables, and CLI flags; use `PascalCase` for Tkinter classes. Keep bilingual UI strings in the `TEXTS` dictionary, and add matching `zh` and `en` entries for every new interface label or message.

## Testing Guidelines

There is no formal test framework in this snapshot. Before submitting changes, run `python3 -m py_compile merge_logo_gui.py` and at least one smoke export with known PNG or JPG inputs. For placement changes, test edge positions, oversized overlays, JPG output, and transparent PNG output. For SVG changes, test both a normal SVG and the missing-browser/error path where possible.

## Commit & Pull Request Guidelines

This directory is not currently a Git repository, so no local commit history is available. Use short, imperative commit messages such as `Fix overlay clamping` or `Add English selector text`. Pull requests should describe the user-facing behavior changed, list manual test commands, mention image formats tested, and include screenshots when GUI layout or preview behavior changes.

## Security & Configuration Tips

Do not hardcode local image paths or environment-specific browser paths. Preserve the current behavior of resolving relative input files from the script directory, and keep subprocess calls argument-list based rather than shell-string based.
