from __future__ import annotations

from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "app_icon.png"
ICO_OUTPUT = ROOT / "app_icon.ico"
ICNS_OUTPUT = ROOT / "app_icon.icns"


def main() -> int:
    image = Image.open(SOURCE).convert("RGBA")
    image.save(
        ICO_OUTPUT,
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    image.save(ICNS_OUTPUT)
    print(f"created {ICO_OUTPUT}")
    print(f"created {ICNS_OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
