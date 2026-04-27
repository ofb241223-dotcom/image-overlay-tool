from __future__ import annotations

import math
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk
from pathlib import Path
import threading
from dataclasses import replace

from PIL import Image, ImageTk

from .core import (
    OverlayItem,
    Placement,
    SUPPORTED_OUTPUT_FORMATS,
    analyze_export_quality,
    build_initial_placement,
    clamp_placement,
    export_image,
    get_supported_filetypes,
    get_overlay_height,
    get_transformed_overlay_size,
    is_svg,
    load_rgba_image,
    render_overlay_image,
    resolve_input_path,
)
from .i18n import LANGUAGE_NAMES, get_text

PREVIEW_MAX_SIDE = 2048
FONT_NORMAL = ("", 16)
FONT_BOLD = ("", 18, "bold")
FONT_CONTROL = ("", 16)
FONT_SMALL = ("", 14)


def _preview_image_copy(image: Image.Image, max_side: int = PREVIEW_MAX_SIDE) -> Image.Image:
    longest_side = max(image.size)
    if longest_side > max_side:
        preview = image.copy()
        resample_filter = getattr(Image, "Resampling", Image).LANCZOS
        preview.thumbnail((max_side, max_side), resample_filter)
        return preview
    return image


def _render_preview_overlay_image(item: OverlayItem) -> Image.Image:
    placement = item.placement
    if not is_svg(item.path):
        return render_overlay_image(item.image, item.path, placement)

    overlay_height = get_overlay_height(placement.width, item.image.size)
    overlay = item.image.resize((placement.width, overlay_height), Image.Resampling.LANCZOS)
    if placement.remove_white:
        from .core import apply_remove_white_bg

        overlay = apply_remove_white_bg(overlay)
    opacity = max(0.0, min(1.0, placement.opacity))
    if opacity < 1.0:
        alpha = overlay.getchannel("A").point(lambda value: round(value * opacity))
        overlay.putalpha(alpha)
    if placement.rotation:
        overlay = overlay.rotate(-placement.rotation, resample=Image.Resampling.BICUBIC, expand=True)
    return overlay


class PreviewCanvas(tk.Canvas):
    def __init__(self, master, language: str, on_placement_changed, on_placement_committed, on_layer_selected):
        super().__init__(master, bg="#eef2f7", highlightthickness=0)
        self.language = language
        self.on_placement_changed = on_placement_changed
        self.on_placement_committed = on_placement_committed
        self.on_layer_selected = on_layer_selected
        
        self.base_image: Image.Image | None = None
        self.overlays: list[OverlayItem] = []
        self.active_layer_idx: int = -1
        self._zoom_factor = 1.0
        self._pan_offset_x = 0.0
        self._pan_offset_y = 0.0

        self._base_tk_image = None
        self._base_tk_image_scale = -1
        self._overlay_tk_images = {}
        
        self._drag_mode = None
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._drag_start_pan_x = 0
        self._drag_start_pan_y = 0
        self._drag_start_placement = None
        self._drag_layer_idx = -1
        self._drag_start_angle = 0.0
        self._drag_start_center_x = 0.0
        self._drag_start_center_y = 0.0
        
        self.bind("<Configure>", lambda e: self.update_view())
        self.bind("<ButtonPress-1>", self.on_press)
        self.bind("<B1-Motion>", self.on_drag)
        self.bind("<ButtonRelease-1>", self.on_release)
        self.bind("<ButtonPress-2>", self.on_pan_press)
        self.bind("<B2-Motion>", self.on_pan_drag)
        self.bind("<ButtonPress-3>", self.on_pan_press)
        self.bind("<B3-Motion>", self.on_pan_drag)
        self.bind("<MouseWheel>", self.on_mouse_wheel)
        self.bind("<Button-4>", self.on_mouse_wheel)
        self.bind("<Button-5>", self.on_mouse_wheel)

    def set_content(self, base_image, overlays, active_layer_idx):
        if self.base_image is not base_image:
            self._base_tk_image = None
        self.base_image = base_image
        self.overlays = overlays
        self.active_layer_idx = active_layer_idx
        
        current_ids = {id(item.image) for item in self.overlays}
        self._overlay_tk_images = {k: v for k, v in self._overlay_tk_images.items() if k in current_ids}
        self.update_view()

    def set_language(self, language):
        self.language = language
        self.update_view()

    def _get_display_scale(self):
        if not self.base_image:
            return 1.0
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return 1.0
        scale = min(w / self.base_image.width, h / self.base_image.height) * self._zoom_factor
        return scale

    def _get_image_rect(self):
        if not self.base_image:
            return 0, 0, 0, 0
        scale = self._get_display_scale()
        dw, dh = self.base_image.width * scale, self.base_image.height * scale
        x = (self.winfo_width() - dw) / 2 + self._pan_offset_x
        y = (self.winfo_height() - dh) / 2 + self._pan_offset_y
        return x, y, dw, dh

    def _get_overlay_rect(self, idx):
        if not self.base_image or not (0 <= idx < len(self.overlays)):
            return None
        ix, iy, iw, ih = self._get_image_rect()
        scale = self._get_display_scale()
        item = self.overlays[idx]
        tw, th = get_transformed_overlay_size(item.placement.width, item.image.size, item.placement.rotation)
        ox = ix + item.placement.x * scale
        oy = iy + item.placement.y * scale
        return ox, oy, tw * scale, th * scale

    def update_view(self):
        self.delete("all")
        if not self.base_image:
            w, h = self.winfo_width(), self.winfo_height()
            self.create_text(w/2, h/2, text=get_text(self.language, "drop_hint"), fill="#475569", font=("Arial", 14))
            return

        ix, iy, iw, dh = self._get_image_rect()
        scale = self._get_display_scale()

        if not self._base_tk_image or self._base_tk_image_scale != scale:
            preview = _preview_image_copy(self.base_image)
            target_w, target_h = max(1, int(iw)), max(1, int(dh))
            res = preview.resize((target_w, target_h), Image.Resampling.LANCZOS if target_w < preview.width else Image.Resampling.BICUBIC)
            self._base_tk_image = ImageTk.PhotoImage(res)
            self._base_tk_image_scale = scale
        
        self.create_image(ix, iy, anchor="nw", image=self._base_tk_image)

        for i, item in enumerate(self.overlays):
            rect = self._get_overlay_rect(i)
            if not rect: continue
            ox, oy, ow, oh = rect
            
            p = item.placement
            cache_key = (
                id(item.image),
                p.width,
                p.rotation,
                p.opacity,
                p.remove_white,
                p.blend_mode,
                p.tile,
                scale,
            )
            if cache_key in self._overlay_tk_images:
                tk_img = self._overlay_tk_images[cache_key]
            else:
                rendered = _render_preview_overlay_image(item)
                tw, th = rendered.size
                disp_w, disp_h = max(1, int(tw * scale)), max(1, int(th * scale))
                disp_img = rendered.resize((disp_w, disp_h), Image.Resampling.LANCZOS)
                tk_img = ImageTk.PhotoImage(disp_img)
                self._overlay_tk_images[cache_key] = tk_img
            
            self.create_image(ox, oy, anchor="nw", image=tk_img)

            if i == self.active_layer_idx:
                self.create_rectangle(ox, oy, ox+ow, oy+oh, outline="cyan", width=2)
                hx, hy, hw, hh = self._get_handle_rect()
                self.create_rectangle(hx, hy, hx+hw, hy+hh, fill="cyan", outline="cyan")
                rx, ry, rr = self._get_rotate_handle()
                self.create_line(ox + ow / 2, oy, rx, ry, fill="#f97316", width=2)
                self.create_oval(rx-rr, ry-rr, rx+rr, ry+rr, fill="#f97316", outline="#ea580c", width=2)

    def _get_handle_rect(self):
        rect = self._get_overlay_rect(self.active_layer_idx)
        if not rect: return 0,0,0,0
        ox, oy, ow, oh = rect
        h_size = max(10, min(18, min(ow, oh) / 3))
        return ox+ow-h_size, oy+oh-h_size, h_size, h_size

    def _get_rotate_handle(self):
        rect = self._get_overlay_rect(self.active_layer_idx)
        if not rect:
            return 0, 0, 0
        ox, oy, ow, oh = rect
        radius = max(7, min(11, min(ow, oh) / 4))
        return ox + ow / 2, oy - max(24, radius * 3), radius

    def _event_angle_from_center(self, x, y):
        center_x, center_y = self._drag_start_center_x, self._drag_start_center_y
        return math.degrees(math.atan2(y - center_y, x - center_x))

    def _hit_test(self, x, y):
        for i in range(len(self.overlays)-1, -1, -1):
            rect = self._get_overlay_rect(i)
            if not rect: continue
            ox, oy, ow, oh = rect
            if ox <= x <= ox+ow and oy <= y <= oy+oh:
                return i
        return None

    def on_press(self, event):
        x, y = event.x, event.y
        hx, hy, hw, hh = self._get_handle_rect()
        rx, ry, rr = self._get_rotate_handle()
        hit = self._hit_test(x, y)

        if rr > 0 and (x - rx) ** 2 + (y - ry) ** 2 <= rr ** 2:
            self._drag_mode = "rotate"
            hit = self.active_layer_idx
        elif hx <= x <= hx+hw and hy <= y <= hy+hh:
            self._drag_mode = "resize"
            hit = self.active_layer_idx
        elif hit is not None:
            self._drag_mode = "move"
        else:
            self._drag_mode = None
            return

        if hit != self.active_layer_idx:
            self.on_layer_selected(hit)
        self.active_layer_idx = hit
        
        self._drag_start_x = x
        self._drag_start_y = y
        self._drag_layer_idx = hit
        self._drag_start_placement = self.overlays[hit].placement
        rect = self._get_overlay_rect(hit)
        if rect:
            ox, oy, ow, oh = rect
            self._drag_start_center_x = ox + ow / 2
            self._drag_start_center_y = oy + oh / 2
            self._drag_start_angle = self._event_angle_from_center(x, y)

    def on_drag(self, event):
        if not self._drag_mode or not self._drag_start_placement: return
        scale = max(0.0001, self._get_display_scale())
        dx = (event.x - self._drag_start_x) / scale
        dy = (event.y - self._drag_start_y) / scale
        
        start = self._drag_start_placement
        if self._drag_mode == "move":
            new_p = replace(start, x=int(start.x + dx), y=int(start.y + dy))
        elif self._drag_mode == "resize":
            growth = dx if abs(dx) >= abs(dy) else dy
            new_p = replace(start, width=max(1, int(start.width + growth)))
        else:
            current_angle = self._event_angle_from_center(event.x, event.y)
            rotation = start.rotation + current_angle - self._drag_start_angle
            center_x = self._drag_start_center_x
            center_y = self._drag_start_center_y
            ix, iy, _, _ = self._get_image_rect()
            center_image_x = (center_x - ix) / scale
            center_image_y = (center_y - iy) / scale
            overlay_w, overlay_h = get_transformed_overlay_size(
                start.width,
                self.overlays[self._drag_layer_idx].image.size,
                rotation,
            )
            new_p = replace(
                start,
                x=int(round(center_image_x - overlay_w / 2)),
                y=int(round(center_image_y - overlay_h / 2)),
                rotation=rotation,
            )
            
        self.on_placement_changed(self._drag_layer_idx, new_p)

    def on_release(self, event):
        if self._drag_mode:
            if 0 <= self._drag_layer_idx < len(self.overlays):
                self.on_placement_committed(
                    self._drag_layer_idx,
                    self.overlays[self._drag_layer_idx].placement,
                )
            self._drag_mode = None
            self._drag_layer_idx = -1

    def on_pan_press(self, event):
        self._drag_mode = "pan"
        self._drag_start_x = event.x
        self._drag_start_y = event.y
        self._drag_start_pan_x = self._pan_offset_x
        self._drag_start_pan_y = self._pan_offset_y

    def on_pan_drag(self, event):
        if self._drag_mode == "pan":
            self._pan_offset_x = self._drag_start_pan_x + (event.x - self._drag_start_x)
            self._pan_offset_y = self._drag_start_pan_y + (event.y - self._drag_start_y)
            self.update_view()

    def on_mouse_wheel(self, event):
        if not (0 <= self.active_layer_idx < len(self.overlays)):
            return
        current = self.overlays[self.active_layer_idx].placement
        if event.num == 4 or getattr(event, 'delta', 0) > 0:
            factor = 1.05
        elif event.num == 5 or getattr(event, 'delta', 0) < 0:
            factor = 0.95
        else:
            return
        next_width = max(1, round(current.width * factor))
        if next_width == current.width:
            next_width += 1 if factor > 1 else -1
        next_placement = replace(current, width=max(1, next_width))
        self.on_placement_changed(self.active_layer_idx, next_placement)
        self.on_placement_committed(self.active_layer_idx, next_placement)


import subprocess
import shutil
import sys

def _zenity_file_filter(label: str, patterns: str) -> str:
    return f"{label} | {patterns}"


def modern_askopenfilename(title="Open"):
    filetypes = get_supported_filetypes()
    if sys.platform.startswith("linux") and shutil.which("zenity"):
        try:
            cmd = ["zenity", "--file-selection", f"--title={title}"]
            for label, patterns in filetypes:
                cmd.append(f"--file-filter={_zenity_file_filter(label, patterns)}")
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
            return ""
        except Exception:
            pass
    from tkinter import filedialog
    return filedialog.askopenfilename(title=title, filetypes=filetypes)

def modern_asksaveasfilename(initialfile=""):
    output_patterns = " ".join(f"*.{fmt}" for fmt in SUPPORTED_OUTPUT_FORMATS)
    if sys.platform.startswith("linux") and shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", "--save", "--confirm-overwrite"]
        cmd.append(f"--file-filter={_zenity_file_filter('Output images', output_patterns)}")
        if initialfile:
            cmd.append(f"--filename={initialfile}")
        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
            return ""
        except Exception:
            pass
    from tkinter import filedialog
    return filedialog.asksaveasfilename(
        initialfile=initialfile,
        filetypes=[("Output images", output_patterns), ("All files", "*.*")],
    )

class WorkerThread(threading.Thread):
    def __init__(self, ui_root, target, args=(), on_success=None, on_error=None, on_finish=None):
        super().__init__()
        self.ui_root = ui_root
        self.target = target
        self.args = args
        self.on_success = on_success
        self.on_error = on_error
        self.on_finish = on_finish
        self.daemon = True

    def _dispatch(self, callback, *args):
        if callback is None:
            return
        self.ui_root.after(0, lambda: callback(*args))

    def run(self):
        try:
            res = self.target(*self.args)
            self._dispatch(self.on_success, res)
        except Exception as e:
            self._dispatch(self.on_error, e)
        finally:
            self._dispatch(self.on_finish)


class OverlayMainWindow(ctk.CTk):
    def __init__(self, args, language):
        super().__init__()
        self.args = args
        self.language = language
        self.title(get_text(language, "window_title"))
        self.geometry("1680x1040")
        self.minsize(1200, 780)
        
        self.base_path = None
        self.output_path = Path(getattr(args, 'out', 'merged.png')).expanduser()
        self.base_image = None
        self.overlays = []
        self.active_layer_idx = -1
        self.pending_files = []
        self.pending_file_labels = {}
        self.history = []
        self.history_index = -1
        
        self._syncing_controls = False
        self._worker_active = False

        self._build_ui()
        self._refresh_texts()
        self._push_history()
        
        self._load_initial()

    def _t(self, key, **kwargs):
        return get_text(self.language, key, **kwargs)

    def _bind_panel_mousewheel(self, widget):
        widget.bind("<MouseWheel>", self._on_panel_mousewheel, add="+")
        widget.bind("<Button-4>", self._on_panel_mousewheel, add="+")
        widget.bind("<Button-5>", self._on_panel_mousewheel, add="+")
        for child in widget.winfo_children():
            self._bind_panel_mousewheel(child)

    def _on_panel_mousewheel(self, event):
        canvas = getattr(self.right_panel, "_parent_canvas", None)
        if canvas is None:
            return
        if event.num == 4 or getattr(event, "delta", 0) > 0:
            canvas.yview_scroll(-3, "units")
        elif event.num == 5 or getattr(event, "delta", 0) < 0:
            canvas.yview_scroll(3, "units")

    def _build_ui(self):
        # Top toolbar
        self.toolbar = ctk.CTkFrame(self, height=50, fg_color="transparent", corner_radius=0)
        self.toolbar.pack(side="top", fill="x", padx=10, pady=5)
        
        self.btn_import = ctk.CTkButton(self.toolbar, command=self._import_image, width=80)
        self.btn_undo = ctk.CTkButton(self.toolbar, command=self._undo, width=80)
        self.btn_reset = ctk.CTkButton(self.toolbar, command=self._reset, width=80)
        self.btn_save = ctk.CTkButton(self.toolbar, command=self._save, width=80)
        for button in (self.btn_import, self.btn_undo, self.btn_reset, self.btn_save):
            button.configure(font=FONT_CONTROL, height=36)
        
        self.btn_import.pack(side="left", padx=5)
        self.btn_undo.pack(side="left", padx=5)
        self.btn_reset.pack(side="left", padx=5)
        self.btn_save.pack(side="left", padx=5)

        # Main split
        self.main_pane = tk.PanedWindow(self, orient=tk.HORIZONTAL, bd=0, sashwidth=4, bg="#d1d5db")
        self.main_pane.pack(fill="both", expand=True)

        self.preview = PreviewCanvas(self.main_pane, self.language, self._on_preview_change, self._on_preview_commit, self._on_preview_select)
        self.main_pane.add(self.preview, stretch="always")

        # Right panel
        self.right_panel_wrapper = ctk.CTkFrame(self.main_pane, width=430, fg_color="transparent")
        self.main_pane.add(self.right_panel_wrapper, stretch="never")
        self.right_panel = ctk.CTkScrollableFrame(self.right_panel_wrapper, width=430, fg_color="transparent")
        self.right_panel.pack(fill="both", expand=True)
        
        # Helper to create sections
        def create_section(title_key):
            frame = ctk.CTkFrame(self.right_panel)
            frame.pack(fill="x", padx=5, pady=5)
            lbl = ctk.CTkLabel(frame, text="", font=FONT_BOLD)
            lbl.pack(anchor="w", padx=10, pady=5)
            setattr(self, f"lbl_{title_key}", lbl)
            return frame

        # File Section
        f_file = create_section("file_group_label")
        self.lbl_lang = ctk.CTkLabel(f_file, text="Language:", font=FONT_NORMAL)
        self.lbl_lang.pack(anchor="w", padx=10)
        self.combo_lang = ctk.CTkComboBox(f_file, values=["中文", "English"], command=self._on_lang_change, font=FONT_CONTROL)
        self.combo_lang.set(LANGUAGE_NAMES.get(self.language, "中文"))
        self.combo_lang.pack(fill="x", padx=10, pady=(0, 10))

        self.lbl_recent = ctk.CTkLabel(f_file, text="Pending:", font=FONT_NORMAL)
        self.lbl_recent.pack(anchor="w", padx=10)
        self.combo_recent = ctk.CTkComboBox(f_file, values=[], font=FONT_CONTROL)
        self.combo_recent.set("")
        self.combo_recent.pack(fill="x", padx=10, pady=(0, 5))
        
        row_btn = ctk.CTkFrame(f_file, fg_color="transparent")
        row_btn.pack(fill="x", padx=10, pady=(0, 10))
        self.btn_set_base = ctk.CTkButton(row_btn, command=lambda: self._set_from_recent('base'), width=120)
        self.btn_set_overlay = ctk.CTkButton(row_btn, command=lambda: self._set_from_recent('overlay'), width=120)
        self.btn_set_base.configure(font=FONT_CONTROL, height=36)
        self.btn_set_overlay.configure(font=FONT_CONTROL, height=36)
        self.btn_set_base.pack(side="left", expand=True, padx=(0, 5))
        self.btn_set_overlay.pack(side="right", expand=True, padx=(5, 0))

        # Layer Section
        f_layer = create_section("layers_group_label")
        self.listbox_layers = tk.Listbox(f_layer, height=4, bg="#f3f4f6", selectbackground="#3b82f6", selectforeground="white", highlightthickness=0, borderwidth=1, font=("Arial", 15))
        self.listbox_layers.pack(fill="x", padx=10, pady=(0, 5))
        self.listbox_layers.bind("<<ListboxSelect>>", self._on_listbox_select)
        
        row_layer_btn = ctk.CTkFrame(f_layer, fg_color="transparent")
        row_layer_btn.pack(fill="x", padx=10, pady=(0, 10))
        self.btn_add_layer = ctk.CTkButton(row_layer_btn, command=self._add_layer, width=120)
        self.btn_remove_layer = ctk.CTkButton(row_layer_btn, command=self._remove_layer, width=120)
        self.btn_add_layer.configure(font=FONT_CONTROL, height=36)
        self.btn_remove_layer.configure(font=FONT_CONTROL, height=36)
        self.btn_add_layer.pack(side="left", expand=True, padx=(0, 5))
        self.btn_remove_layer.pack(side="right", expand=True, padx=(5, 0))

        # Placement Section
        f_place = create_section("placement_group")
        def add_adjust_row(parent, label_key, from_, to_):
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=2)
            lbl = ctk.CTkLabel(row, width=80, anchor="w", font=FONT_NORMAL)
            lbl.pack(side="left")
            setattr(self, f"lbl_{label_key}", lbl)
            var = tk.StringVar(value="0")
            entry = ctk.CTkEntry(row, textvariable=var, font=FONT_CONTROL, height=36, width=76)
            entry.pack(side="right", padx=(8, 0))
            slider = ctk.CTkSlider(row, from_=from_, to=to_, command=lambda value, v=var: v.set(str(round(value, 1) if isinstance(value, float) and not value.is_integer() else int(value))))
            slider.pack(side="right", fill="x", expand=True)
            var.trace_add("write", lambda *a: self._on_prop_change())
            return var, slider

        self.var_x, self.slider_x = add_adjust_row(f_place, "x_label", 0, 1)
        self.var_y, self.slider_y = add_adjust_row(f_place, "y_label", 0, 1)
        self.var_w, self.slider_w = add_adjust_row(f_place, "width_label", 1, 1)
        self.var_rot, self.slider_rot = add_adjust_row(f_place, "rotation_label", -180, 180)
        
        row_op = ctk.CTkFrame(f_place, fg_color="transparent")
        row_op.pack(fill="x", padx=10, pady=2)
        self.lbl_opacity_label = ctk.CTkLabel(row_op, width=80, anchor="w", font=FONT_NORMAL)
        self.lbl_opacity_label.pack(side="left")
        self.var_op = tk.StringVar(value="100")
        self.slider_op = ctk.CTkSlider(row_op, from_=0, to=100, command=self._on_prop_change)
        self.entry_op = ctk.CTkEntry(row_op, textvariable=self.var_op, font=FONT_CONTROL, height=36, width=76)
        self.entry_op.pack(side="right", padx=(8, 0))
        self.slider_op.pack(side="right", fill="x", expand=True)
        self.slider_op.set(100)
        self.var_op.trace_add("write", lambda *a: self._on_prop_change())

        # Adjust Section
        f_adj = create_section("adjust_group_label")
        self.var_rw = tk.BooleanVar(value=False)
        self.chk_rw = ctk.CTkCheckBox(f_adj, text="", variable=self.var_rw, command=lambda: self._on_prop_change())
        self.chk_rw.configure(font=FONT_CONTROL)
        self.chk_rw.pack(anchor="w", padx=10, pady=5)
        self.after(0, lambda: self._bind_panel_mousewheel(self.right_panel))

        # Export Section
        f_exp = create_section("export_group_label")
        self.lbl_output_label = ctk.CTkLabel(f_exp, anchor="w", font=FONT_NORMAL)
        self.lbl_output_label.pack(fill="x", padx=10)
        
        row_out = ctk.CTkFrame(f_exp, fg_color="transparent")
        row_out.pack(fill="x", padx=10, pady=2)
        self.var_output = tk.StringVar(value=str(self.output_path))
        self.entry_out = ctk.CTkEntry(row_out, textvariable=self.var_output, font=FONT_CONTROL, height=36)
        self.entry_out.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.btn_out_browse = ctk.CTkButton(row_out, text="...", width=38, height=36, command=self._choose_output, font=FONT_CONTROL)
        self.btn_out_browse.pack(side="right")
        
        row_fmt = ctk.CTkFrame(f_exp, fg_color="transparent")
        row_fmt.pack(fill="x", padx=10, pady=2)
        self.lbl_format_label = ctk.CTkLabel(row_fmt, width=80, anchor="w", font=FONT_NORMAL)
        self.lbl_format_label.pack(side="left")
        self.combo_fmt = ctk.CTkComboBox(row_fmt, values=list(SUPPORTED_OUTPUT_FORMATS), command=self._update_export_check, font=FONT_CONTROL)
        self.combo_fmt.pack(side="right", fill="x", expand=True)
        self.combo_fmt.set("png")
        
        self.lbl_export_check = ctk.CTkLabel(f_exp, text="", font=FONT_SMALL, text_color="#ef4444", wraplength=350, justify="left")
        self.lbl_export_check.pack(fill="x", padx=10, pady=5)
        
    def _refresh_texts(self):
        t = self._t
        self.btn_import.configure(text=t("import_images_button"))
        self.btn_undo.configure(text=t("undo_button"))
        self.btn_reset.configure(text=t("reset_button"))
        self.btn_save.configure(text=t("save_button"))
        
        self.lbl_file_group_label.configure(text=t("file_group_label"))
        self.lbl_lang.configure(text=t("language_label"))
        self.lbl_recent.configure(text=t("recent_label"))
        self.btn_set_base.configure(text=t("recent_base_button"))
        self.btn_set_overlay.configure(text=t("recent_overlay_button"))
        
        self.lbl_layers_group_label.configure(text=t("layers_group_label"))
        self.btn_add_layer.configure(text=t("add_layer_button"))
        self.btn_remove_layer.configure(text=t("remove_layer_button"))
        
        self.lbl_placement_group.configure(text=t("placement_group"))
        self.lbl_x_label.configure(text=t("x_label"))
        self.lbl_y_label.configure(text=t("y_label"))
        self.lbl_width_label.configure(text=t("width_label"))
        self.lbl_rotation_label.configure(text=t("rotation_label"))
        self.lbl_opacity_label.configure(text=t("opacity_label"))
        
        self.lbl_adjust_group_label.configure(text=t("adjust_group_label"))
        self.chk_rw.configure(text=t("remove_white_label"))
        
        self.lbl_export_group_label.configure(text=t("export_group_label"))
        self.lbl_output_label.configure(text=t("output_label"))
        self.lbl_format_label.configure(text=t("format_label"))
        
        self.preview.set_language(self.language)
        
        self._update_layer_listbox()
        self._update_export_check()
        self._update_buttons_state()

    def _on_lang_change(self, choice):
        codes = {"中文": "zh", "English": "en"}
        self.language = codes.get(choice, "zh")
        self._refresh_texts()

    def _update_buttons_state(self):
        can_edit = bool(self.base_image and self.overlays and 0 <= self.active_layer_idx < len(self.overlays))
        state = "normal" if can_edit else "disabled"
        self.btn_reset.configure(state=state)
        self.btn_remove_layer.configure(state=state)
        pending_state = "normal" if self.pending_files else "disabled"
        self.btn_set_base.configure(state=pending_state)
        self.btn_set_overlay.configure(state=pending_state)
        
        can_undo = self.history_index > 0
        self.btn_undo.configure(state="normal" if can_undo else "disabled")
        
        can_save = bool(self.base_image and self.overlays)
        self.btn_save.configure(state="normal" if can_save else "disabled")

    def _load_initial(self):
        def _load_task():
            b_path = None
            b_img = None
            if getattr(self.args, 'base', None):
                b_path = resolve_input_path(self.args.base, Path.cwd())
                b_img = load_rgba_image(b_path)
            o_path = None
            o_img = None
            if getattr(self.args, 'overlay', None):
                o_path = resolve_input_path(self.args.overlay, Path.cwd())
                o_img = load_rgba_image(o_path)
            return b_img, o_img, b_path, o_path

        def _on_loaded(res):
            b_img, o_img, b_path, o_path = res
            if b_img:
                self.base_image = b_img
                self.base_path = Path(b_path).resolve()
            if o_img and b_img:
                p = build_initial_placement(getattr(self.args, 'x', 0), getattr(self.args, 'y', 0), getattr(self.args, 'width', None), o_img.size, getattr(self.args, 'opacity', 1.0), getattr(self.args, 'rotation', 0.0))
                p = clamp_placement(p, b_img.size, o_img.size)
                self.overlays.append(OverlayItem(image=o_img, path=Path(o_path).resolve(), placement=p, name=Path(o_path).name))
                self.active_layer_idx = 0
            if b_img or o_img:
                self._push_history()
            self._update_all()

        WorkerThread(self, _load_task, on_success=_on_loaded).start()

    def _import_image(self):
        f = modern_askopenfilename(self._t("import_file_title"))
        if f:
            p = Path(f)
            if p not in self.pending_files:
                self.pending_files.append(p)
                self._refresh_pending_files(p)

    def _refresh_pending_files(self, selected_path=None):
        labels = []
        self.pending_file_labels = {}
        for index, path in enumerate(self.pending_files, start=1):
            label = f"{index}. {path.name}"
            labels.append(label)
            self.pending_file_labels[label] = path
        self.combo_recent.configure(values=labels)
        if not labels:
            self.combo_recent.set("")
            return
        if selected_path is not None:
            for label, path in self.pending_file_labels.items():
                if path == selected_path:
                    self.combo_recent.set(label)
                    break
        elif self.combo_recent.get() not in self.pending_file_labels:
            self.combo_recent.set(labels[0])
        self._update_buttons_state()

    def _set_from_recent(self, role):
        if not self.pending_files: return
        path = self.pending_file_labels.get(self.combo_recent.get())
        if path is None:
            return
        self._load_and_set(path, role)

    def _add_layer(self):
        f = modern_askopenfilename(self._t("import_file_title"))
        if f: self._load_and_set(Path(f), "overlay")

    def _load_and_set(self, path, role):
        if self._worker_active: return
        self._worker_active = True
        
        def _task():
            return load_rgba_image(path), path
            
        def _on_success(res):
            img, p = res
            if role == "base":
                self.base_image = img
                self.base_path = p
                for o in self.overlays:
                    o.placement = clamp_placement(o.placement, img.size, o.image.size)
                self._push_history()
            else:
                if not self.base_image:
                    messagebox.showwarning("Warning", self._t("selector_missing_base"))
                    self._worker_active = False
                    return
                placement = build_initial_placement(0, 0, None, img.size)
                placement = clamp_placement(placement, self.base_image.size, img.size)
                self.overlays.append(OverlayItem(image=img, path=p, placement=placement, name=p.name))
                self.active_layer_idx = len(self.overlays) - 1
                self._push_history()
            
            self._update_all()
            self._worker_active = False

        def _on_error(e):
            messagebox.showerror("Error", str(e))
            self._worker_active = False

        WorkerThread(self, _task, on_success=_on_success, on_error=_on_error).start()

    def _remove_layer(self):
        if 0 <= self.active_layer_idx < len(self.overlays):
            del self.overlays[self.active_layer_idx]
            if self.active_layer_idx >= len(self.overlays):
                self.active_layer_idx = len(self.overlays) - 1
            self._push_history()
            self._update_all()

    def _update_all(self):
        self.preview.set_content(self.base_image, self.overlays, self.active_layer_idx)
        self._update_layer_listbox()
        self._sync_controls()
        self._update_buttons_state()
        self._update_export_check()

    def _update_layer_listbox(self):
        self.listbox_layers.delete(0, tk.END)
        for i, item in enumerate(self.overlays):
            name = item.name or self._t("layer_item_name", index=i+1)
            self.listbox_layers.insert(tk.END, name)
            if i == self.active_layer_idx:
                self.listbox_layers.selection_clear(0, tk.END)
                self.listbox_layers.selection_set(i)

    def _on_listbox_select(self, event):
        sel = self.listbox_layers.curselection()
        if sel:
            self.active_layer_idx = sel[0]
            self.preview.active_layer_idx = self.active_layer_idx
            self.preview.update_view()
            self._sync_controls()
            self._update_buttons_state()

    def _on_preview_select(self, idx):
        self.active_layer_idx = idx
        self.preview.active_layer_idx = idx
        self._update_layer_listbox()
        self._sync_controls()
        self._update_buttons_state()

    def _on_preview_change(self, idx, placement):
        if 0 <= idx < len(self.overlays):
            self.active_layer_idx = idx
            self.preview.active_layer_idx = idx
            self.overlays[idx].placement = placement
            self._sync_controls()
            self.preview.update_view()

    def _on_preview_commit(self, idx, placement):
        if 0 <= idx < len(self.overlays):
            self.active_layer_idx = idx
            self.preview.active_layer_idx = idx
            self.overlays[idx].placement = clamp_placement(
                placement,
                self.base_image.size,
                self.overlays[idx].image.size,
            )
            self._sync_controls()
            self._push_history()
            self.preview.update_view()

    def _sync_controls(self):
        if not (0 <= self.active_layer_idx < len(self.overlays)):
            self._syncing_controls = True
            self.var_x.set("0")
            self.var_y.set("0")
            self.var_w.set("0")
            self.var_rot.set("0")
            self.slider_x.configure(from_=0, to=1)
            self.slider_y.configure(from_=0, to=1)
            self.slider_w.configure(from_=1, to=2)
            self.slider_x.set(0)
            self.slider_y.set(0)
            self.slider_w.set(1)
            self.slider_rot.set(0)
            self.slider_op.set(100)
            self.var_op.set("100")
            self.var_rw.set(False)
            self._syncing_controls = False
            return
            
        self._syncing_controls = True
        p = self.overlays[self.active_layer_idx].placement
        overlay_w, overlay_h = get_transformed_overlay_size(p.width, self.overlays[self.active_layer_idx].image.size, p.rotation)
        max_x = max(1, self.base_image.width - overlay_w)
        max_y = max(1, self.base_image.height - overlay_h)
        max_w = max(2, self.base_image.width)
        self.slider_x.configure(from_=0, to=max_x)
        self.slider_y.configure(from_=0, to=max_y)
        self.slider_w.configure(from_=1, to=max_w)
        self.var_x.set(str(int(p.x)))
        self.var_y.set(str(int(p.y)))
        self.var_w.set(str(int(p.width)))
        self.var_rot.set(str(round(p.rotation, 1)))
        self.slider_x.set(int(p.x))
        self.slider_y.set(int(p.y))
        self.slider_w.set(int(p.width))
        self.slider_rot.set(round(p.rotation, 1))
        self.slider_op.set(int(p.opacity * 100))
        self.var_op.set(str(int(p.opacity * 100)))
        self.var_rw.set(p.remove_white)
        self._syncing_controls = False

    def _on_prop_change(self, *_args):
        if self._syncing_controls or not (0 <= self.active_layer_idx < len(self.overlays)):
            return
        try:
            x = int(float(self.var_x.get() or 0))
            y = int(float(self.var_y.get() or 0))
            w = int(float(self.var_w.get() or 1))
            rot = float(self.var_rot.get() or 0)
            opacity_percent = float(self.var_op.get() or 100)
        except ValueError:
            return
            
        opacity_percent = max(0.0, min(100.0, opacity_percent))
        if abs(self.slider_op.get() - opacity_percent) > 0.5:
            self.slider_op.set(opacity_percent)
        op = opacity_percent / 100.0
        
        p = Placement(x, y, w, op, rot, "normal", False, self.var_rw.get())
        p = clamp_placement(p, self.base_image.size, self.overlays[self.active_layer_idx].image.size)
        
        self.overlays[self.active_layer_idx].placement = p
        self._sync_controls()
        self.preview.update_view()
        if hasattr(self, '_history_timer'):
            self.after_cancel(self._history_timer)
        self._history_timer = self.after(500, self._push_history)

    def _push_history(self):
        state = {
            "base_image": self.base_image,
            "base_path": self.base_path,
            "overlays": [OverlayItem(o.image, o.path, o.placement, o.name) for o in self.overlays],
            "active_layer_idx": self.active_layer_idx,
        }
        self.history = self.history[:self.history_index+1]
        self.history.append(state)
        self.history_index = len(self.history) - 1
        self._update_buttons_state()

    def _undo(self):
        if self.history_index > 0:
            self.history_index -= 1
            self._restore_history()

    def _reset(self):
        if not (0 <= self.active_layer_idx < len(self.overlays)) or not self.base_image: return
        img = self.overlays[self.active_layer_idx].image
        p = build_initial_placement(0, 0, None, img.size)
        self.overlays[self.active_layer_idx].placement = clamp_placement(p, self.base_image.size, img.size)
        self._push_history()
        self._update_all()

    def _restore_history(self):
        state = self.history[self.history_index]
        self.base_image = state["base_image"]
        self.base_path = state["base_path"]
        self.overlays = [OverlayItem(o.image, o.path, o.placement, o.name) for o in state["overlays"]]
        self.active_layer_idx = state["active_layer_idx"]
        if self.active_layer_idx >= len(self.overlays):
            self.active_layer_idx = len(self.overlays) - 1
        self._update_all()

    def _choose_output(self):
        f = modern_asksaveasfilename(initialfile=self.var_output.get())
        if f:
            self.var_output.set(f)

    def _update_export_check(self, *_):
        if not self.base_image or not self.overlays:
            self.lbl_export_check.configure(text=self._t("quality_no_images"))
            return
            
        fmt = self.combo_fmt.get()
        idx = self.active_layer_idx if self.active_layer_idx >= 0 else 0
        item = self.overlays[idx]
        
        rep = analyze_export_quality(fmt, self.base_path, item.path, item.image.size, item.placement)
        
        texts = [self._t(f"quality_{m}") for m in rep.messages]
        self.lbl_export_check.configure(text="\n".join(texts))
        
        color = "#10b981" if rep.level == "ok" else "#f59e0b" if rep.level == "warning" else "#ef4444"
        self.lbl_export_check.configure(text_color=color)

    def _save(self):
        if not self.base_image or not self.overlays: return
        if self._worker_active: return
        
        out_path = Path(self.var_output.get()).resolve()
        fmt = self.combo_fmt.get()
        self._worker_active = True
        
        prog = ctk.CTkToplevel(self)
        prog.title(self._t("exporting_title"))
        prog.geometry("300x100")
        prog.transient(self)
        prog.grab_set()
        ctk.CTkLabel(prog, text=self._t("exporting_image")).pack(expand=True)
        
        def _task():
            return export_image(self.base_image, self.base_path, self.overlays, out_path, fmt)
            
        def _on_success(path):
            prog.destroy()
            messagebox.showinfo(self._t("saved_title"), self._t("saved_message", path=path))
            self._worker_active = False
            
        def _on_error(e):
            prog.destroy()
            messagebox.showerror(self._t("save_failed_title"), str(e))
            self._worker_active = False
            
        WorkerThread(self, _task, on_success=_on_success, on_error=_on_error).start()

def run_app(args, language):
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")
    ctk.set_widget_scaling(1.5)  # 增加字体和组件大小
    ctk.set_window_scaling(1.5)  # 增加整体窗口渲染大小
    app = OverlayMainWindow(args, language)
    app.mainloop()
    return 0
