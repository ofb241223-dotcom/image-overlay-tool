# 打包说明

本项目使用本地虚拟环境安装依赖，不需要改系统 Python。桌面界面基于 CustomTkinter，打包时不再需要 Qt 的 XCB 插件依赖。

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

注意：本工具支持 SVG 导入作为素材，但不支持 SVG 导出，导出格式保留 PNG、JPG、WebP、BMP。
