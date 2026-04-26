from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, UnidentifiedImageError

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
    blend_mode: str = "normal"
    tile: bool = False
    remove_white: bool = False

@dataclass
class OverlayItem:
    image: Image.Image
    path: Path
    placement: Placement
    name: str | None = None

def fast_tile(img: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    tiled = Image.new("RGBA", target_size, (0,0,0,0))
    tiled.paste(img, (0,0))
    current_w, current_h = img.size
    while current_w < target_size[0]:
        tiled.paste(tiled.crop((0, 0, current_w, current_h)), (current_w, 0))
        current_w *= 2
    while current_h < target_size[1]:
        tiled.paste(tiled.crop((0, 0, target_size[0], current_h)), (0, current_h))
        current_h *= 2
    return tiled

def apply_remove_white_bg(img: Image.Image) -> Image.Image:
    if img.mode != 'RGBA': img = img.convert('RGBA')
    r, g, b, a = img.split()
    r_mask = r.point(lambda p: 255 if p > 240 else 0)
    g_mask = g.point(lambda p: 255 if p > 240 else 0)
    b_mask = b.point(lambda p: 255 if p > 240 else 0)
    white_mask = ImageChops.darker(r_mask, ImageChops.darker(g_mask, b_mask))
    keep_mask = ImageOps.invert(white_mask)
    new_a = ImageChops.darker(a, keep_mask)
    img.putalpha(new_a)
    return img



@dataclass(frozen=True)
class ExportQualityReport:
    level: str
    messages: tuple[str, ...]


PREFERRED_INPUT_SUFFIXES = (
    ".png",
    ".webp",
    ".avif",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".bmp",
    ".gif",
)

SUPPORTED_OUTPUT_FORMATS = ("png", "jpg", "jpeg", "webp", "bmp")
JPEG_OUTPUT_FORMATS = {"jpg", "jpeg"}


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
        matches = [
            path
            for path in current_dir.glob(f"{stem}.*")
            if path.is_file() and path.suffix.lower() in PREFERRED_INPUT_SUFFIXES
        ]
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


def load_rgba_image(path: Path, target_size: tuple[int, int] | None = None) -> Image.Image:
    if path.suffix.lower() not in PREFERRED_INPUT_SUFFIXES:
        raise UnidentifiedImageError(f"Unsupported image format: {path.suffix or path.name}")

    with Image.open(path) as source_image:
        image = source_image.convert("RGBA")
        image.load()
    if target_size is not None and image.size != target_size:
        image = image.resize(target_size, RESAMPLE)

    return image


def render_image_at_size(
    path: Path, source_image: Image.Image, target_size: tuple[int, int]
) -> Image.Image:
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
    return Placement(
        x=x,
        y=y,
        width=width,
        opacity=opacity,
        rotation=rotation,
        blend_mode=placement.blend_mode,
        tile=placement.tile,
        remove_white=placement.remove_white,
    )


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
    if placement.remove_white: overlay = apply_remove_white_bg(overlay)
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
    overlays: list[OverlayItem],
) -> Image.Image:
    merged = base_image.copy()
    for item in overlays:
        placement = item.placement
        if not placement.tile:
            placement = clamp_placement(item.placement, merged.size, item.image.size)
        rendered_overlay = render_overlay_image(item.image, item.path, placement)
        
        mode = placement.blend_mode
        tile = placement.tile
        w, h = rendered_overlay.size
        if w == 0 or h == 0: continue
        
        if tile:
            temp_w, temp_h = merged.width + w, merged.height + h
            tiled_temp = fast_tile(rendered_overlay, (temp_w, temp_h))
            start_x = placement.x % w - w if placement.x % w != 0 else 0
            start_y = placement.y % h - h if placement.y % h != 0 else 0
            paste_img = tiled_temp.crop((-start_x, -start_y, -start_x + merged.width, -start_y + merged.height))
            paste_box = (0, 0)
        else:
            paste_img = rendered_overlay
            paste_box = (placement.x, placement.y)
            
        if mode in ("multiply", "screen"):
            x, y = paste_box
            pw, ph = paste_img.size
            box_left, box_top = max(0, x), max(0, y)
            box_right, box_bottom = min(merged.width, x + pw), min(merged.height, y + ph)
            if box_right > box_left and box_bottom > box_top:
                base_crop = merged.crop((box_left, box_top, box_right, box_bottom))
                cx, cy = box_left - x, box_top - y
                overlay_crop = paste_img.crop((cx, cy, cx + (box_right - box_left), cy + (box_bottom - box_top)))
                base_rgb = base_crop.convert("RGB")
                overlay_rgb = overlay_crop.convert("RGB")
                blended = ImageChops.multiply(base_rgb, overlay_rgb) if mode == "multiply" else ImageChops.screen(base_rgb, overlay_rgb)
                base_crop.paste(blended, (0,0), overlay_crop.getchannel("A"))
                merged.paste(base_crop, (box_left, box_top))
        else:
            merged.alpha_composite(paste_img, paste_box)
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


def export_image(
    base_image: Image.Image,
    base_path: Path | None,
    overlays: list[OverlayItem],
    output_path: Path,
    output_format: str | None = None,
) -> Path:
    final_path = output_path_for_format(output_path, output_format)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    merged = compose_image(base_image, overlays)
    try:
        return save_output_image(merged, final_path, output_format)
    finally:
        merged.close()
