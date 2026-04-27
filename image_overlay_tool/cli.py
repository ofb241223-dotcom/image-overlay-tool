from __future__ import annotations

import argparse
from pathlib import Path

from .core import (
    SUPPORTED_OUTPUT_FORMATS,
    build_initial_placement,
    clamp_placement,
    export_image,
    OverlayItem,
    load_rgba_image,
    resolve_input_path,
)
from .i18n import get_text, resolve_language


def get_script_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactively place one image onto another and export a merged result. / 交互式调整叠加图片并导出结果。"
    )
    parser.add_argument("--base", default=None, help="Base image path. / 底图路径。")
    parser.add_argument(
        "--overlay",
        "--logo",
        dest="overlay",
        default=None,
        help="Overlay image path. / 叠加图路径。",
    )
    parser.add_argument("--out", default="merged.png", help="Output image path. / 输出图片路径。")
    parser.add_argument("--x", type=int, default=0, help="Initial overlay X position. / 叠加图初始 X 坐标。")
    parser.add_argument("--y", type=int, default=0, help="Initial overlay Y position. / 叠加图初始 Y 坐标。")
    parser.add_argument(
        "--width",
        type=int,
        default=None,
        help="Initial overlay width in base-image pixels. / 叠加图初始宽度，单位为底图像素。",
    )
    parser.add_argument(
        "--opacity",
        type=float,
        default=1.0,
        help="Overlay opacity from 0 to 1. / 叠加图透明度，范围 0 到 1。",
    )
    parser.add_argument(
        "--rotation",
        type=float,
        default=0.0,
        help="Overlay clockwise rotation in degrees. / 叠加图顺时针旋转角度。",
    )
    parser.add_argument(
        "--format",
        choices=SUPPORTED_OUTPUT_FORMATS,
        default=None,
        help="Output format. Defaults to the output file suffix. / 输出格式，默认使用输出文件后缀。",
    )
    parser.add_argument(
        "--save-immediately",
        action="store_true",
        help="Skip the GUI and export immediately. / 跳过图形界面并直接导出。",
    )
    parser.add_argument(
        "--lang",
        choices=("auto", "zh", "en"),
        default="zh",
        help="UI language: auto, zh, or en. / 界面语言：auto、zh 或 en。",
    )
    return parser.parse_args()


def export_from_args(args: argparse.Namespace, language: str) -> int:
    if not args.base or not args.overlay:
        raise SystemExit(get_text(language, "cli_missing_inputs"))

    script_dir = get_script_dir()
    base_path = resolve_input_path(args.base, script_dir)
    overlay_path = resolve_input_path(args.overlay, script_dir)
    output_path = Path(args.out).expanduser()

    base_image = load_rgba_image(base_path)
    overlay_image = load_rgba_image(overlay_path)
    try:
        placement = clamp_placement(
            build_initial_placement(
                args.x,
                args.y,
                args.width,
                overlay_image.size,
                opacity=args.opacity,
                rotation=args.rotation,
            ),
            base_image.size,
            overlay_image.size,
        )
        saved_path = export_image(
            base_image,
            base_path,
            [OverlayItem(image=overlay_image, path=overlay_path, placement=placement)],
            output_path,
            output_format=args.format,
        )
        print(get_text(language, "saved_stdout", path=saved_path))
        return 0
    finally:
        base_image.close()
        overlay_image.close()


def run_gui(args: argparse.Namespace, language: str) -> int:
    try:
        from .gui_tk import run_app
    except ModuleNotFoundError as exc:
        if exc.name in ("tkinter", "customtkinter"):
            raise SystemExit(get_text(language, "gui_missing_tkinter"))
        raise

    return run_app(args, language)


def main() -> int:
    args = parse_args()
    language = resolve_language(args.lang)
    if args.save_immediately:
        return export_from_args(args, language)
    return run_gui(args, language)
