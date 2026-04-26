# Image Overlay Tool

一个跨平台桌面图片叠加工具，用于把单张叠加图放到底图上，调整位置、尺寸、透明度和旋转后导出。

## 功能

- 支持 PNG、JPG、WebP、BMP 等常见位图输入。
- 支持拖放导入、最近文件、撤销/重做、重置和导出提示。
- 支持导出 PNG、JPG、WebP、BMP。
- 导出检查会提示当前格式是否有损、是否保留透明。
- 默认中文界面，可切换英文。

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

Windows、macOS、Linux 需要分别在对应系统上构建。仓库内的 GitHub Actions 会在三种系统上自动构建；推送 `v1.0.0` 这类 tag 时，会自动创建 Release 并上传三平台压缩包。

## 说明

PNG 和无损 WebP 会保留透明；JPG 会有损压缩并铺白底。SVG 已不再作为导入或导出格式支持。
