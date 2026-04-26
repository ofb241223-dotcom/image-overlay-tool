from pathlib import Path

from PyInstaller.utils.hooks.qt import add_qt6_dependencies


hiddenimports, binaries, datas = add_qt6_dependencies(__file__)

# The app renders images through Pillow and does not need Qt's TIFF image plugin.
# Excluding it avoids a Linux build warning when libqtiff.so depends on libtiff.so.5.
binaries = [
    (source, destination)
    for source, destination in binaries
    if Path(source).name != "libqtiff.so"
]
