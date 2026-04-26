# 打包说明

本项目使用本地虚拟环境安装依赖，不需要改系统 Python。

Linux/X11 下 PySide6 还需要系统库 `libxcb-cursor0`。如果启动时看到 `Could not load the Qt platform plugin "xcb"` 或 `libxcb-cursor.so.0 => not found`，先运行：

```bash
sudo apt install -y libxcb-cursor0
```

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
```

语法和测试检查：

```bash
python3 -m py_compile merge_logo_gui.py image_overlay_tool/*.py
python3 -m unittest
```

当前系统打包：

```bash
python3 packaging/prepare_icons.py
pyinstaller packaging/image_overlay_tool.spec
```

Windows、macOS、Linux 需要分别在对应系统上构建。Windows 会得到可执行程序目录，macOS 会得到 `.app`，Linux 会得到可执行目录。图标源文件是 `packaging/app_icon.png`，打包前会转换出 Windows 用的 `.ico` 和 macOS 用的 `.icns`。

如果你手头没有 Windows 或 macOS 电脑，可以把项目推到 GitHub，然后在仓库的 Actions 页面手动运行 `构建桌面应用`。工作流会分别用 `ubuntu-latest`、`windows-latest`、`macos-latest` 构建，并把 `dist/` 里的结果上传成下载附件。

如果要发布到 Release 页面，创建并推送 `v1.0.0` 这类 tag。GitHub Actions 会构建三平台压缩包并自动创建 Release。

注意：现在支持导出 `svg`，这个 SVG 是把最终合成图以 PNG 数据嵌入 SVG 文件里，适合网页或排版软件引用；它不是把位图重新矢量化。
