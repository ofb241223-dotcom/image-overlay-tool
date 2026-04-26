from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from PIL import Image
from PIL import UnidentifiedImageError

from image_overlay_tool.core import (
    Placement,
    PREFERRED_INPUT_SUFFIXES,
    SUPPORTED_OUTPUT_FORMATS,
    analyze_export_quality,
    clamp_placement,
    compose_image,
    export_image,
    OverlayItem,
    get_transformed_overlay_size,
    load_rgba_image,
    normalize_output_format,
    resolve_input_path,
    save_output_image,
)


class CoreImageTests(unittest.TestCase):
    def test_resolve_input_path_prefers_supported_suffix_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            (directory / "logo.jpg").write_bytes(b"jpg")
            (directory / "logo.png").write_bytes(b"png")

            resolved = resolve_input_path("logo", directory)

        self.assertEqual(resolved.name, "logo.png")

    def test_clamp_placement_limits_edges_and_oversized_overlay(self) -> None:
        placement = Placement(x=999, y=999, width=500)
        clamped = clamp_placement(placement, (100, 80), (40, 20))

        self.assertEqual(clamped.width, 100)
        self.assertEqual(clamped.x, 0)
        self.assertEqual(clamped.y, 30)

    def test_clamp_placement_preserves_extra_flags(self) -> None:
        placement = Placement(
            x=999,
            y=999,
            width=500,
            opacity=0.4,
            rotation=30,
            blend_mode="multiply",
            tile=True,
            remove_white=True,
        )
        clamped = clamp_placement(placement, (100, 80), (40, 20))

        self.assertEqual(clamped.blend_mode, "multiply")
        self.assertTrue(clamped.tile)
        self.assertTrue(clamped.remove_white)

    def test_compose_keeps_transparent_png_result(self) -> None:
        base = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        overlay = Image.new("RGBA", (4, 4), (10, 20, 30, 255))
        with tempfile.TemporaryDirectory() as tmpdir:
            overlay_path = Path(tmpdir) / "overlay.png"
            overlay.save(overlay_path)
            merged = compose_image(base, [OverlayItem(image=overlay, path=overlay_path, placement=Placement(x=2, y=3, width=4))])

        self.assertEqual(merged.getpixel((0, 0)), (0, 0, 0, 0))
        self.assertEqual(merged.getpixel((2, 3)), (10, 20, 30, 255))

    def test_jpg_export_uses_white_background(self) -> None:
        image = Image.new("RGBA", (3, 3), (0, 0, 0, 0))
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "out.jpg"
            saved_path = save_output_image(image, output_path)
            with Image.open(saved_path) as saved:
                saved_rgb = saved.convert("RGB")
                pixel = saved_rgb.getpixel((1, 1))

        self.assertEqual(pixel, (255, 255, 255))

    def test_svg_is_not_a_supported_input_or_output_format(self) -> None:
        self.assertNotIn(".svg", PREFERRED_INPUT_SUFFIXES)
        self.assertNotIn("svg", SUPPORTED_OUTPUT_FORMATS)
        with self.assertRaises(ValueError):
            normalize_output_format(Path("out.svg"), "svg")
        with tempfile.TemporaryDirectory() as tmpdir:
            svg_path = Path(tmpdir) / "logo.svg"
            svg_path.write_text("<svg></svg>", encoding="utf-8")
            with self.assertRaises(UnidentifiedImageError):
                load_rgba_image(svg_path)

    def test_webp_export_is_lossless(self) -> None:
        image = Image.new("RGBA", (2, 1), (0, 0, 0, 0))
        image.putpixel((0, 0), (10, 20, 30, 255))
        image.putpixel((1, 0), (40, 50, 60, 128))
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "out.webp"
            saved_path = save_output_image(image, output_path, "webp")
            with Image.open(saved_path) as saved:
                saved_rgba = saved.convert("RGBA")
                try:
                    pixels = [saved_rgba.getpixel((0, 0)), saved_rgba.getpixel((1, 0))]
                finally:
                    saved_rgba.close()

        self.assertEqual(pixels, [(10, 20, 30, 255), (40, 50, 60, 128)])

    def test_export_quality_reports_lossless_transparent_formats(self) -> None:
        png_report = analyze_export_quality(
            "png",
            overlay_size=(10, 5),
            placement=Placement(x=0, y=0, width=10),
        )
        webp_report = analyze_export_quality(
            "webp",
            overlay_size=(10, 5),
            placement=Placement(x=0, y=0, width=10),
        )

        self.assertEqual(png_report.level, "ok")
        self.assertIn("png_lossless_transparency", png_report.messages)
        self.assertEqual(webp_report.level, "ok")
        self.assertIn("webp_lossless_transparency", webp_report.messages)

    def test_export_quality_reports_lossy_or_flattened_formats(self) -> None:
        jpg_report = analyze_export_quality("jpg")
        bmp_report = analyze_export_quality("bmp")

        self.assertEqual(jpg_report.level, "danger")
        self.assertIn("jpg_lossy_no_transparency", jpg_report.messages)
        self.assertIn("white_background", jpg_report.messages)
        self.assertEqual(bmp_report.level, "warning")
        self.assertIn("bmp_lossless_no_transparency", bmp_report.messages)
        self.assertIn("white_background", bmp_report.messages)

    def test_export_quality_reports_resampling(self) -> None:
        report = analyze_export_quality(
            "png",
            overlay_size=(20, 10),
            placement=Placement(x=0, y=0, width=12, rotation=5),
        )

        self.assertEqual(report.level, "warning")
        self.assertIn("resampled", report.messages)

    def test_rotation_changes_transformed_bounds(self) -> None:
        self.assertEqual(get_transformed_overlay_size(20, (20, 10), 0), (20, 10))
        self.assertEqual(get_transformed_overlay_size(20, (20, 10), 90), (10, 20))

    def test_opacity_parameter_affects_alpha(self) -> None:
        base = Image.new("RGBA", (6, 6), (0, 0, 0, 0))
        overlay = Image.new("RGBA", (4, 4), (255, 0, 0, 255))
        with tempfile.TemporaryDirectory() as tmpdir:
            overlay_path = Path(tmpdir) / "overlay.png"
            overlay.save(overlay_path)
            merged = compose_image(
                base,
                [OverlayItem(image=overlay, path=overlay_path, placement=Placement(x=1, y=1, width=4, opacity=0.5))],
            )

        self.assertTrue(120 <= merged.getpixel((1, 1))[3] <= 135)

    def test_tile_mode_repeats_overlay_across_base(self) -> None:
        base = Image.new("RGBA", (6, 6), (0, 0, 0, 0))
        overlay = Image.new("RGBA", (2, 2), (0, 255, 0, 255))
        with tempfile.TemporaryDirectory() as tmpdir:
            overlay_path = Path(tmpdir) / "tile.png"
            overlay.save(overlay_path)
            merged = compose_image(
                base,
                [OverlayItem(image=overlay, path=overlay_path, placement=Placement(x=0, y=0, width=2, tile=True))],
            )

        self.assertEqual(merged.getpixel((0, 0)), (0, 255, 0, 255))
        self.assertEqual(merged.getpixel((5, 5)), (0, 255, 0, 255))

    def test_remove_white_background_keeps_base_visible(self) -> None:
        base = Image.new("RGBA", (3, 3), (0, 0, 255, 255))
        overlay = Image.new("RGBA", (3, 3), (255, 255, 255, 255))
        overlay.putpixel((1, 1), (0, 0, 0, 255))
        with tempfile.TemporaryDirectory() as tmpdir:
            overlay_path = Path(tmpdir) / "logo.png"
            overlay.save(overlay_path)
            merged = compose_image(
                base,
                [OverlayItem(image=overlay, path=overlay_path, placement=Placement(x=0, y=0, width=3, remove_white=True))],
            )

        self.assertEqual(merged.getpixel((0, 0)), (0, 0, 255, 255))
        self.assertEqual(merged.getpixel((1, 1)), (0, 0, 0, 255))

    def test_blend_mode_survives_clamping_and_changes_result(self) -> None:
        base = Image.new("RGBA", (2, 2), (100, 100, 100, 255))
        overlay = Image.new("RGBA", (2, 2), (200, 50, 50, 255))
        with tempfile.TemporaryDirectory() as tmpdir:
            overlay_path = Path(tmpdir) / "blend.png"
            overlay.save(overlay_path)
            merged = compose_image(
                base,
                [OverlayItem(image=overlay, path=overlay_path, placement=Placement(x=0, y=0, width=2, blend_mode="multiply"))],
            )

        self.assertEqual(merged.getpixel((0, 0)), (78, 19, 19, 255))

    def test_export_image_writes_requested_format(self) -> None:
        base = Image.new("RGBA", (8, 8), (255, 255, 255, 255))
        overlay = Image.new("RGBA", (4, 4), (0, 0, 255, 255))
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            base_path = directory / "base.png"
            overlay_path = directory / "overlay.png"
            output_path = directory / "merged"
            base.save(base_path)
            overlay.save(overlay_path)

            base_loaded = load_rgba_image(base_path)
            overlay_loaded = load_rgba_image(overlay_path)
            try:
                saved_path = export_image(
                    base_loaded,
                    None,
                    [OverlayItem(image=overlay_loaded, path=overlay_path, placement=Placement(x=2, y=2, width=4))],
                    output_path,
                    output_format="png",
                )
            finally:
                base_loaded.close()
                overlay_loaded.close()

            self.assertEqual(saved_path.name, "merged.png")
            self.assertTrue(saved_path.exists())


if __name__ == "__main__":
    unittest.main()
