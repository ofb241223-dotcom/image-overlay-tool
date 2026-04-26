from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from PIL import Image

from image_overlay_tool.core import (
    Placement,
    analyze_export_quality,
    clamp_placement,
    compose_image,
    export_image,
    get_transformed_overlay_size,
    load_rgba_image,
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

    def test_compose_keeps_transparent_png_result(self) -> None:
        base = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        overlay = Image.new("RGBA", (4, 4), (10, 20, 30, 255))
        with tempfile.TemporaryDirectory() as tmpdir:
            overlay_path = Path(tmpdir) / "overlay.png"
            overlay.save(overlay_path)
            merged = compose_image(base, overlay, overlay_path, Placement(x=2, y=3, width=4))

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

    def test_svg_export_embeds_png_preview(self) -> None:
        image = Image.new("RGBA", (5, 7), (20, 40, 60, 128))
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "out.svg"
            saved_path = save_output_image(image, output_path, "svg")
            svg = saved_path.read_text(encoding="utf-8")

        self.assertEqual(saved_path.name, "out.svg")
        self.assertIn("<svg", svg)
        self.assertIn('width="5"', svg)
        self.assertIn('height="7"', svg)
        self.assertIn("data:image/png;base64,", svg)

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

    def test_export_quality_reports_svg_and_resampling(self) -> None:
        report = analyze_export_quality(
            "svg",
            base_path=Path("base.png"),
            overlay_path=Path("overlay.svg"),
            overlay_size=(20, 10),
            placement=Placement(x=0, y=0, width=12, rotation=5),
        )

        self.assertEqual(report.level, "warning")
        self.assertIn("svg_embedded_raster", report.messages)
        self.assertIn("svg_raster_inputs", report.messages)
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
                overlay,
                overlay_path,
                Placement(x=1, y=1, width=4, opacity=0.5),
            )

        self.assertTrue(120 <= merged.getpixel((1, 1))[3] <= 135)

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
                    overlay_loaded,
                    overlay_path,
                    output_path,
                    Placement(x=2, y=2, width=4),
                    output_format="png",
                )
            finally:
                base_loaded.close()
                overlay_loaded.close()

            self.assertEqual(saved_path.name, "merged.png")
            self.assertTrue(saved_path.exists())


if __name__ == "__main__":
    unittest.main()
