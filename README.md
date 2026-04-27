<p align="center">
  <img src="packaging/app_icon.png" alt="Image Overlay Tool Logo" width="128">
</p>

<h1 align="center">Image Overlay Tool</h1>

<p align="center">
  一个跨平台桌面图片叠加工具，用于把多张叠加图放到底图上，调整位置、尺寸、透明度和旋转后导出。
</p>

## 效果展示

### 原图

![原图占位](docs/images/base.png)

### 叠加图

![叠加图占位](docs/images/overlay.svg)

### 合并图

![合并图占位](docs/images/merged.png)

## 功能

- 支持 PNG、JPG、WebP、BMP、SVG 等常见图片输入；SVG 会在导入时渲染为位图参与合成。
- 支持导入图片、图层选择、撤销、重置和导出提示。
- 支持在画布中拖动叠加图、拖动右下角改变大小、拖动上方手柄旋转。
- 支持透明度、宽度、位置、旋转角度的滑块和数字输入。
- 支持导出 PNG、JPG、WebP、BMP。
- 导出检查会提示当前格式是否有损、是否保留透明。
- 默认中文界面，可切换英文。

## 下载安装

在 Release 页面下载对应系统的安装包：

- Windows：下载 `Image-Overlay-Tool-Windows.exe` 后双击运行。
- macOS：下载 `Image-Overlay-Tool-macOS.dmg` 后打开运行。
- Ubuntu/Debian：下载 `Image-Overlay-Tool-Linux.deb` 安装。
- Fedora/RHEL/openSUSE：下载 `Image-Overlay-Tool-Linux.rpm` 安装。

## 本地运行

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 merge_logo_gui.py
```

命令行直接导出：

```bash
python3 merge_logo_gui.py --base base.png --overlay logo.png --out merged.png --save-immediately
```

## 检查和打包

```bash
python3 -m compileall merge_logo_gui.py image_overlay_tool tests
python3 -m unittest
python3 packaging/prepare_icons.py
pyinstaller packaging/image_overlay_tool.spec
```

GitHub Actions 会在 Windows、macOS、Linux 上自动构建。推送 `v1.0.4` 这类 tag 时，会自动创建 Release 并上传 `.exe`、`.dmg`、`.deb`、`.rpm`。

## 说明

PNG 和无损 WebP 会保留透明；JPG 会有损压缩并铺白底。SVG 支持导入作为素材，但不支持导出为 SVG。
