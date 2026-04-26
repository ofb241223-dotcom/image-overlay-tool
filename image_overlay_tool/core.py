from __future__ import annotations

import base64
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
import math
from pathlib import Path
import shutil
import subprocess
import tempfile

from PIL import Image, UnidentifiedImageError

try:
    RESAMPLE = Image.Resampling.LANCZOS
    ROTATE_RESAMPLE = Image.Resampling.BICUBIC
except AttributeError:
    RESAMPLE = Image.LANCZOS
    ROTATE_RESAMPLE = Image.BICUBIC


@dataclass(frozen=True)
class Placement:
    x: int
    y: int
    width: int
    opacity: float = 1.0
    rotation: float = 0.0


@dataclass(frozen=True)
class ExportQualityReport:
    level: str
    messages: tuple[str, ...]


PREFERRED_INPUT_SUFFIXES = (
    ".png",
    ".svg",
    ".webp",
    ".avif",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".bmp",
    ".gif",
)

SUPPORTED_OUTPUT_FORMATS = ("png", "jpg", "jpeg", "webp", "bmp", "svg")
JPEG_OUTPUT_FORMATS = {"jpg", "jpeg"}


def is_svg(path: Path) -> bool:
    return path.suffix.lower() == ".svg"


def sort_input_matches(paths: list[Path]) -> list[Path]:
    suffix_rank = {suffix: index for index, suffix in enumerate(PREFERRED_INPUT_SUFFIXES)}
    return sorted(paths, key=lambda path: (suffix_rank.get(path.suffix.lower(), 999), path.name.lower()))


def resolve_input_path(raw_path: str, search_dir: Path | None = None) -> Path:
    raw_candidate = Path(raw_path).expanduser()
    candidates_to_try: list[Path] = []
    if raw_candidate.is_absolute():
        candidates_to_try.append(raw_candidate)
    else:
        if search_dir is not None:
            candidates_to_try.append((search_dir / raw_candidate).expanduser())
        candidates_to_try.append(raw_candidate)

    for candidate in candidates_to_try:
        if candidate.exists():
            return candidate.resolve()

    search_dirs: list[Path] = []
    if search_dir is not None:
        search_dirs.append(search_dir)
    if str(raw_candidate.parent):
        search_dirs.append(raw_candidate.parent)
    else:
        search_dirs.append(Path("."))

    stem = raw_candidate.stem if raw_candidate.suffix else raw_candidate.name
    for current_dir in search_dirs:
        matches = [path for path in current_dir.glob(f"{stem}.*") if path.is_file()]
        if matches:
            return sort_input_matches(matches)[0].resolve()

    raise FileNotFoundError(
        f"Could not find input image '{raw_path}'. You can pass an explicit path, "
        "or place a file with the same stem and a different extension in the same directory."
    )


def get_supported_filetypes() -> list[tuple[str, str]]:
    patterns = " ".join(f"*{suffix}" for suffix in PREFERRED_INPUT_SUFFIXES)
    return [
        ("Supported images", patterns),
        ("All files", "*.*"),
    ]


def list_candidate_images(directory: Path) -> list[str]:
    candidates = [
        path.name
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in PREFERRED_INPUT_SUFFIXES
    ]
    return sorted(candidates, key=str.lower)


def require_convert(path: Path) -> None:
    if shutil.which("convert") is None:
        raise RuntimeError(
            f"{path.name} requires ImageMagick's 'convert' command, but it is not available on this system."
        )


def get_chrome_binary() -> str | None:
    for candidate in ("google-chrome", "chrome", "chromium-browser", "chromium"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _render_svg_flat_with_chrome(
    path: Path, target_size: tuple[int, int], background_hex: str
) -> Image.Image:
    chrome_binary = get_chrome_binary()
    if chrome_binary is None:
        raise RuntimeError(
            f"{path.name} requires a Chrome-compatible browser for SVG rendering, "
            "but no supported binary was found."
        )

    width, height = target_size
    file_url = path.resolve().as_uri()
    viewport_width = max(width + 200, 1200)
    viewport_height = max(height + 200, 400)

    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>html,body{{margin:0;padding:0;background:{background_hex};overflow:hidden;}}"
        f"img{{display:block;width:{width}px;height:{height}px;object-fit:fill;}}</style>"
        f"</head><body><img src=\"{file_url}\" alt=\"svg\"></body></html>"
    )

    with tempfile.TemporaryDirectory(prefix="svg-render-") as tmpdir:
        tmpdir_path = Path(tmpdir)
        html_path = tmpdir_path / "render.html"
        png_path = tmpdir_path / "render.png"
        html_path.write_text(html, encoding="utf-8")

        command = [
            chrome_binary,
            "--headless",
            "--disable-gpu",
            "--allow-file-access-from-files",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            f"--window-size={viewport_width},{viewport_height}",
            f"--screenshot={png_path}",
            html_path.resolve().as_uri(),
        ]
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode != 0:
            error = result.stderr.decode("utf-8", errors="replace").strip() or "unknown chrome error"
            raise RuntimeError(f"Failed to rasterize '{path}' with Chrome: {error}")

        if not png_path.exists():
            raise RuntimeError(f"Chrome did not produce a rendered PNG for '{path}'.")

        with Image.open(png_path) as source_image:
            image = source_image.convert("RGBA").crop((0, 0, width, height))
            image.load()
        return image


def _reconstruct_transparent_image(black_image: Image.Image, white_image: Image.Image) -> Image.Image:
    black_rgb = black_image.convert("RGB")
    white_rgb = white_image.convert("RGB")
    output = Image.new("RGBA", black_rgb.size)

    for y in range(black_rgb.height):
        for x in range(black_rgb.width):
            rb, gb, bb = black_rgb.getpixel((x, y))
            rw, gw, bw = white_rgb.getpixel((x, y))

            alpha_r = 255 - (rw - rb)
            alpha_g = 255 - (gw - gb)
            alpha_b = 255 - (bw - bb)
            alpha = max(0, min(255, round((alpha_r + alpha_g + alpha_b) / 3)))

            if alpha == 0:
                output.putpixel((x, y), (0, 0, 0, 0))
                continue

            red = max(0, min(255, round(rb * 255 / alpha)))
            green = max(0, min(255, round(gb * 255 / alpha)))
            blue = max(0, min(255, round(bb * 255 / alpha)))
            output.putpixel((x, y), (red, green, blue, alpha))

    return output


@lru_cache(maxsize=32)
def _rasterize_svg_with_chrome_cached(path_str: str, width: int, height: int) -> bytes:
    path = Path(path_str)
    black_image = _render_svg_flat_with_chrome(path, (width, height), "#000000")
    white_image = _render_svg_flat_with_chrome(path, (width, height), "#ffffff")
    reconstructed = _reconstruct_transparent_image(black_image, white_image)

    buffer = BytesIO()
    reconstructed.save(buffer, format="PNG")
    black_image.close()
    white_image.close()
    reconstructed.close()
    return buffer.getvalue()


def rasterize_svg_with_chrome(path: Path, target_size: tuple[int, int]) -> Image.Image:
    width, height = target_size
    png_bytes = _rasterize_svg_with_chrome_cached(str(path.resolve()), width, height)
    with Image.open(BytesIO(png_bytes)) as source_image:
        image = source_image.convert("RGBA")
        image.load()
    return image


def rasterize_with_convert(path: Path, target_size: tuple[int, int] | None = None) -> Image.Image:
    require_convert(path)
    command = ["convert", str(path), "-background", "none"]
    if target_size is not None:
        command.extend(["-resize", f"{target_size[0]}x{target_size[1]}!"])
    command.append("png:-")

    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0:
        error = result.stderr.decode("utf-8", errors="replace").strip() or "unknown convert error"
        raise RuntimeError(f"Failed to rasterize '{path}': {error}")

    image = Image.open(BytesIO(result.stdout)).convert("RGBA")
    image.load()
    return image


def load_rgba_image(path: Path, target_size: tuple[int, int] | None = None) -> Image.Image:
    if is_svg(path):
        svg_target = target_size
        if svg_target is None:
            probe = rasterize_with_convert(path)
            svg_target = probe.size
            probe.close()
        return rasterize_svg_with_chrome(path, svg_target)

    try:
        with Image.open(path) as source_image:
            image = source_image.convert("RGBA")
            image.load()
    except (UnidentifiedImageError, OSError):
        image = rasterize_with_convert(path, target_size)
    else:
        if target_size is not None and image.size != target_size:
            image = image.resize(target_size, RESAMPLE)

    return image


def render_image_at_size(
    path: Path, source_image: Image.Image, target_size: tuple[int, int]
) -> Image.Image:
    if is_svg(path):
        return rasterize_svg_with_chrome(path, target_size)
    if source_image.size == target_size:
        return source_image.copy()
    return source_image.resize(target_size, RESAMPLE)


def clamp_opacity(value: float) -> float:
    if not math.isfinite(value):
        return 1.0
    return max(0.0, min(1.0, value))


def normalize_rotation(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    normalized = ((value + 180.0) % 360.0) - 180.0
    return 180.0 if normalized == -180.0 else normalized


def get_overlay_height(width: int, overlay_size: tuple[int, int]) -> int:
    return max(1, round(width * overlay_size[1] / overlay_size[0]))


def get_transformed_overlay_size(
    width: int, overlay_size: tuple[int, int], rotation: float = 0.0
) -> tuple[int, int]:
    overlay_height = get_overlay_height(width, overlay_size)
    angle = math.radians(normalize_rotation(rotation))
    cosine = abs(math.cos(angle))
    sine = abs(math.sin(angle))
    transformed_width = max(1, math.ceil(width * cosine + overlay_height * sine - 1e-9))
    transformed_height = max(1, math.ceil(width * sine + overlay_height * cosine - 1e-9))
    return transformed_width, transformed_height


def get_max_overlay_width(
    base_size: tuple[int, int], overlay_size: tuple[int, int], rotation: float = 0.0
) -> int:
    base_w, base_h = base_size
    overlay_w, overlay_h = overlay_size
    aspect = overlay_h / overlay_w
    angle = math.radians(normalize_rotation(rotation))
    cosine = abs(math.cos(angle))
    sine = abs(math.sin(angle))

    width_coefficient = max(0.0001, cosine + aspect * sine)
    height_coefficient = max(0.0001, sine + aspect * cosine)
    max_by_width = base_w / width_coefficient
    max_by_height = base_h / height_coefficient
    return max(1, math.floor(min(max_by_width, max_by_height)))


def clamp_placement(
    placement: Placement, base_size: tuple[int, int], overlay_size: tuple[int, int]
) -> Placement:
    rotation = normalize_rotation(float(placement.rotation))
    opacity = clamp_opacity(float(placement.opacity))
    max_width = get_max_overlay_width(base_size, overlay_size, rotation)
    width = max(1, min(int(round(placement.width)), max_width))
    overlay_w, overlay_h = get_transformed_overlay_size(width, overlay_size, rotation)

    base_w, base_h = base_size
    max_x = max(0, base_w - overlay_w)
    max_y = max(0, base_h - overlay_h)
    x = max(0, min(int(round(placement.x)), max_x))
    y = max(0, min(int(round(placement.y)), max_y))
    return Placement(x=x, y=y, width=width, opacity=opacity, rotation=rotation)


def build_initial_placement(
    x: int,
    y: int,
    width: int | None,
    overlay_size: tuple[int, int],
    opacity: float = 1.0,
    rotation: float = 0.0,
) -> Placement:
    return Placement(
        x=x,
        y=y,
        width=width if width is not None else overlay_size[0],
        opacity=opacity,
        rotation=rotation,
    )


def render_overlay_image(
    overlay_image: Image.Image, overlay_path: Path, placement: Placement
) -> Image.Image:
    overlay_height = get_overlay_height(placement.width, overlay_image.size)
    overlay = render_image_at_size(overlay_path, overlay_image, (placement.width, overlay_height))
    opacity = clamp_opacity(placement.opacity)
    if opacity < 1.0:
        alpha = overlay.getchannel("A").point(lambda value: round(value * opacity))
        overlay.putalpha(alpha)

    rotation = normalize_rotation(placement.rotation)
    if rotation:
        overlay = overlay.rotate(-rotation, resample=ROTATE_RESAMPLE, expand=True)
    return overlay


def compose_image(
    base_image: Image.Image,
    overlay_image: Image.Image,
    overlay_path: Path,
    placement: Placement,
) -> Image.Image:
    placement = clamp_placement(placement, base_image.size, overlay_image.size)
    rendered_overlay = render_overlay_image(overlay_image, overlay_path, placement)

    merged = base_image.copy()
    merged.alpha_composite(rendered_overlay, (placement.x, placement.y))
    return merged


def normalize_output_format(output_path: Path, output_format: str | None = None) -> str:
    if output_format:
        normalized = output_format.lower().lstrip(".")
    else:
        normalized = output_path.suffix.lower().lstrip(".") or "png"
    if normalized not in SUPPORTED_OUTPUT_FORMATS:
        raise ValueError(f"Unsupported output format '{normalized}'.")
    return normalized


def analyze_export_quality(
    output_format: str,
    base_path: Path | None = None,
    overlay_path: Path | None = None,
    overlay_size: tuple[int, int] | None = None,
    placement: Placement | None = None,
) -> ExportQualityReport:
    normalized = normalize_output_format(Path(f"output.{output_format}"), output_format)
    messages: list[str] = []

    is_resampled = False
    if overlay_size is not None and placement is not None:
        is_resampled = (
            int(round(placement.width)) != overlay_size[0]
            or normalize_rotation(float(placement.rotation)) != 0.0
        )

    if normalized == "png":
        level = "warning" if is_resampled else "ok"
        messages.append("png_lossless_transparency")
    elif normalized == "webp":
        level = "warning" if is_resampled else "ok"
        messages.append("webp_lossless_transparency")
    elif normalized in JPEG_OUTPUT_FORMATS:
        level = "danger"
        messages.extend(("jpg_lossy_no_transparency", "white_background"))
    elif normalized == "bmp":
        level = "warning"
        messages.extend(("bmp_lossless_no_transparency", "white_background"))
    elif normalized == "svg":
        level = "warning"
        messages.append("svg_embedded_raster")
        input_paths = [path for path in (base_path, overlay_path) if path is not None]
        if len(input_paths) < 2 or any(not is_svg(path) for path in input_paths):
            messages.append("svg_raster_inputs")
    else:
        level = "warning"

    if is_resampled:
        messages.append("resampled")

    return ExportQualityReport(level=level, messages=tuple(messages))


def output_path_for_format(output_path: Path, output_format: str | None = None) -> Path:
    normalized = normalize_output_format(output_path, output_format)
    suffix = ".jpg" if normalized == "jpeg" else f".{normalized}"
    if output_path.suffix.lower() != suffix:
        return output_path.with_suffix(suffix)
    return output_path


def save_output_image(
    image: Image.Image, output_path: Path, output_format: str | None = None
) -> Path:
    normalized_format = normalize_output_format(output_path, output_format)
    final_path = output_path_for_format(output_path, normalized_format)

    if normalized_format == "svg":
        save_svg_output(image, final_path)
        return final_path

    if normalized_format in JPEG_OUTPUT_FORMATS:
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        background.save(final_path, quality=95)
        return final_path

    if normalized_format == "bmp":
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        background.save(final_path)
        return final_path

    if normalized_format == "webp":
        image.save(final_path, lossless=True)
        return final_path

    image.save(final_path)
    return final_path


def save_svg_output(image: Image.Image, output_path: Path) -> None:
    buffer = BytesIO()
    rgba_image = image.convert("RGBA")
    try:
        rgba_image.save(buffer, format="PNG")
    finally:
        rgba_image.close()
    encoded_png = base64.b64encode(buffer.getvalue()).decode("ascii")
    width, height = image.size
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">\n'
        f'  <image href="data:image/png;base64,{encoded_png}" x="0" y="0" '
        f'width="{width}" height="{height}" />\n'
        "</svg>\n"
    )
    output_path.write_text(svg, encoding="utf-8")


def export_image(
    base_image: Image.Image,
    overlay_image: Image.Image,
    overlay_path: Path,
    output_path: Path,
    placement: Placement,
    output_format: str | None = None,
) -> Path:
    final_path = output_path_for_format(output_path, output_format)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    merged = compose_image(base_image, overlay_image, overlay_path, placement)
    try:
        return save_output_image(merged, final_path, output_format)
    finally:
        merged.close()
