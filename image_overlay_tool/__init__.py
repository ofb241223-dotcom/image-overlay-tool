"""Image Overlay Tool package."""

from .core import (
    Placement,
    PREFERRED_INPUT_SUFFIXES,
    SUPPORTED_OUTPUT_FORMATS,
    clamp_placement,
    compose_image,
    export_image,
    get_max_overlay_width,
    get_overlay_height,
    get_transformed_overlay_size,
    load_rgba_image,
    resolve_input_path,
    save_output_image,
)

__all__ = [
    "Placement",
    "PREFERRED_INPUT_SUFFIXES",
    "SUPPORTED_OUTPUT_FORMATS",
    "clamp_placement",
    "compose_image",
    "export_image",
    "get_max_overlay_width",
    "get_overlay_height",
    "get_transformed_overlay_size",
    "load_rgba_image",
    "resolve_input_path",
    "save_output_image",
]
