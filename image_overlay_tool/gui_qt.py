from __future__ import annotations
from dataclasses import replace
from copy import deepcopy

from io import BytesIO
from pathlib import Path
import os
import sys

from PIL import Image
from PySide6.QtCore import QObject, QSettings, Qt, QRectF, Signal, QThread, QTimer, QPointF
from PySide6.QtGui import QAction, QColor, QDragEnterEvent, QDropEvent, QKeySequence, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QCheckBox,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSlider,
    QSizePolicy,
    QSpinBox,
    QDoubleSpinBox,
    QSplitter,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .core import (
    OverlayItem,
    Placement,
    PREFERRED_INPUT_SUFFIXES,
    SUPPORTED_OUTPUT_FORMATS,
    analyze_export_quality,
    build_initial_placement,
    clamp_placement,
    export_image,
    get_max_overlay_width,
    get_overlay_height,
    get_transformed_overlay_size,
    load_rgba_image,
    output_path_for_format,
    resolve_input_path,
)
from .i18n import LANGUAGE_NAMES, get_text


RECENT_LIMIT = 12
PREVIEW_MAX_PIXMAP_SIDE = 4096


def prepare_qt_environment() -> None:
    gtk_modules = os.environ.get("GTK_MODULES", "")
    if gtk_modules:
        modules = [module for module in gtk_modules.split(":") if module and module != "xapp-gtk3-module"]
        if len(modules) != len([module for module in gtk_modules.split(":") if module]):
            os.environ["GTK_MODULES"] = ":".join(modules)
    os.environ.setdefault("QT_STYLE_OVERRIDE", "Fusion")


def get_project_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def build_image_filter() -> str:
    patterns = " ".join(f"*{suffix}" for suffix in PREFERRED_INPUT_SUFFIXES)
    return f"Images ({patterns});;All files (*)"


def _preview_image_copy(image: Image.Image, max_side: int = PREVIEW_MAX_PIXMAP_SIDE) -> Image.Image:
    preview = image
    longest_side = max(image.size)
    if longest_side > max_side:
        preview = image.copy()
        resample_filter = getattr(Image, "Resampling", Image).LANCZOS
        preview.thumbnail((max_side, max_side), resample_filter)
    return preview


def pil_to_pixmap(image: Image.Image, max_side: int = PREVIEW_MAX_PIXMAP_SIDE) -> QPixmap:
    preview = _preview_image_copy(image, max_side=max_side)
    buffer = BytesIO()
    preview.save(buffer, format="PNG")
    pixmap = QPixmap()
    pixmap.loadFromData(buffer.getvalue(), "PNG")
    return pixmap


class PreviewCanvas(QWidget):
    placement_changed = Signal(object)
    placement_committed = Signal(object)
    layer_selected = Signal(int)

    def __init__(self, language: str) -> None:
        super().__init__()
        self.language = language
        self.base_image: Image.Image | None = None
        self.overlays: list[OverlayItem] = []
        self.active_layer_idx: int = -1

        self._base_pixmap: QPixmap | None = None
        self._base_pixmap_key: object | None = None
        self._overlay_pixmaps: dict[object, QPixmap] = {}
        self._drag_mode: str | None = None
        self._drag_start_pos: tuple[float, float] | None = None
        self._drag_start_placement: Placement | None = None

        self.setMinimumSize(240, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._zoom_factor = 1.0
        self._pan_offset = QPointF(0.0, 0.0)
        self._current_snaps = []
        self._drag_start_pan = None

    def set_language(self, language: str) -> None:
        self.language = language
        self.update()

    def set_content(
        self,
        base_image: Image.Image | None,
        overlays: list[OverlayItem],
        active_layer_idx: int,
    ) -> None:
        if base_image is not self.base_image:
            self._base_pixmap = None
            self._base_pixmap_key = None
        
        current_keys = {id(item.image) for item in overlays}
        self._overlay_pixmaps = {k: v for k, v in self._overlay_pixmaps.items() if k in current_keys}
        
        self.base_image = base_image
        self.overlays = overlays
        self.active_layer_idx = active_layer_idx
        self.update()

    def set_active_placement(self, placement: Placement) -> None:
        if 0 <= self.active_layer_idx < len(self.overlays):
            self.overlays[self.active_layer_idx].placement = placement
            self.update()

    def set_placement(self, placement: Placement | None) -> None:
        if placement is not None:
            self.set_active_placement(placement)
        else:
            self.update()

    def _invalidate_pixmap(self) -> None:
        self._base_pixmap = None
        self._base_pixmap_key = None
        self._overlay_pixmaps = {}

    def _image_rect(self) -> QRectF:
        if self.base_image is None: return QRectF()
        margin = 0
        available_width, available_height = max(1, self.width() - margin * 2), max(1, self.height() - margin * 2)
        scale = min(available_width / self.base_image.width, available_height / self.base_image.height)
        scale *= self._zoom_factor
        display_width, display_height = max(1, self.base_image.width * scale), max(1, self.base_image.height * scale)
        return QRectF((self.width() - display_width) / 2 + self._pan_offset.x(), (self.height() - display_height) / 2 + self._pan_offset.y(), display_width, display_height)

    def _display_scale(self) -> float:
        if self.base_image is None: return 1.0
        return self._image_rect().width() / self.base_image.width

    def _overlay_rect(self, idx: int) -> QRectF | None:
        if self.base_image is None or not (0 <= idx < len(self.overlays)):
            return None
        rect = self._image_rect()
        scale = self._display_scale()
        item = self.overlays[idx]
        overlay_width, overlay_height = get_transformed_overlay_size(
            item.placement.width,
            item.image.size,
            item.placement.rotation,
        )
        return QRectF(
            rect.left() + item.placement.x * scale,
            rect.top() + item.placement.y * scale,
            overlay_width * scale,
            overlay_height * scale,
        )

    def _hit_overlay_index(self, point: QPointF) -> int | None:
        for index in range(len(self.overlays) - 1, -1, -1):
            rect = self._overlay_rect(index)
            if rect is not None and rect.contains(point):
                return index
        return None

    def _resize_handle_rect(self) -> QRectF | None:
        overlay_rect = self._overlay_rect(self.active_layer_idx)
        if overlay_rect is None:
            return None
        handle_size = max(10.0, min(18.0, min(overlay_rect.width(), overlay_rect.height()) / 3))
        return QRectF(
            overlay_rect.right() - handle_size,
            overlay_rect.bottom() - handle_size,
            handle_size,
            handle_size,
        )

    def _base_delta_from_mouse(self, event: QMouseEvent) -> tuple[int, int]:
        if self._drag_start_pos is None:
            return (0, 0)
        scale = max(0.0001, self._display_scale())
        point = event.position()
        return (
            round((point.x() - self._drag_start_pos[0]) / scale),
            round((point.y() - self._drag_start_pos[1]) / scale),
        )

    def _current_base_pixmap(self) -> QPixmap | None:
        if self.base_image is None:
            return None

        key = id(self.base_image)
        if self._base_pixmap is not None and self._base_pixmap_key == key:
            return self._base_pixmap

        self._base_pixmap = pil_to_pixmap(self.base_image)
        self._base_pixmap_key = key
        return self._base_pixmap

    def _get_overlay_pixmap(self, item: OverlayItem) -> QPixmap:
        key = id(item.image)
        if key in self._overlay_pixmaps:
            return self._overlay_pixmaps[key]
        pixmap = pil_to_pixmap(item.image)
        self._overlay_pixmaps[key] = pixmap
        return pixmap

    def _draw_overlay_pixmap(
        self, painter: QPainter, overlay_rect: QRectF, overlay_pixmap: QPixmap, item: OverlayItem
    ) -> None:
        scale = self._display_scale()
        overlay_width = item.placement.width * scale
        overlay_height = get_overlay_height(item.placement.width, item.image.size) * scale

        painter.save()
        painter.setOpacity(item.placement.opacity)
        painter.translate(overlay_rect.center())
        painter.rotate(item.placement.rotation)
        painter.drawPixmap(
            QRectF(-overlay_width / 2, -overlay_height / 2, overlay_width, overlay_height),
            overlay_pixmap,
            QRectF(overlay_pixmap.rect()),
        )
        painter.restore()

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#eef2f7"))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        base_pixmap = self._current_base_pixmap()
        if base_pixmap is None:
            painter.setPen(QColor("#475569"))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                get_text(self.language, "drop_hint"),
            )
            return

        image_rect = self._image_rect()
        painter.drawPixmap(image_rect, base_pixmap, QRectF(base_pixmap.rect()))

        for i, item in enumerate(self.overlays):
            rect = self._overlay_rect(i)
            if rect is None: continue
            pixmap = self._get_overlay_pixmap(item)
            self._draw_overlay_pixmap(painter, rect, pixmap, item)
            if i == self.active_layer_idx:
                pen = QPen(Qt.GlobalColor.cyan)
                pen.setWidth(2)
                painter.setPen(pen)
                painter.drawRect(rect)
                handle_rect = self._resize_handle_rect()
                if handle_rect is not None:
                    painter.fillRect(handle_rect, Qt.GlobalColor.cyan)

        if getattr(self, '_current_snaps', None) and 0 <= self.active_layer_idx < len(self.overlays) and not self.overlays[self.active_layer_idx].placement.tile:
            pen = QPen(Qt.GlobalColor.magenta, 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            rect = self._image_rect()
            scale = self._display_scale()
            for orient, val in self._current_snaps:
                if orient == "v":
                    x = rect.left() + val * scale
                    painter.drawLine(x, int(rect.top()), x, int(rect.bottom()))
                else:
                    y = rect.top() + val * scale
                    painter.drawLine(int(rect.left()), y, int(rect.right()), y)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0: self._zoom_factor *= 1.1
            else: self._zoom_factor /= 1.1
            self.update()
            event.accept()
        else:
            super().wheelEvent(event)

    def keyPressEvent(self, event) -> None:
        if not (0 <= self.active_layer_idx < len(self.overlays)):
            return
        step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
        d_x, d_y = 0, 0
        if event.key() == Qt.Key.Key_Up:
            d_y = -step
        elif event.key() == Qt.Key.Key_Down:
            d_y = step
        elif event.key() == Qt.Key.Key_Left:
            d_x = -step
        elif event.key() == Qt.Key.Key_Right:
            d_x = step
        if d_x != 0 or d_y != 0:
            current = self.overlays[self.active_layer_idx].placement
            next_placement = replace(current, x=current.x + d_x, y=current.y + d_y)
            self.placement_changed.emit(next_placement)
            self.placement_committed.emit((self.active_layer_idx, next_placement))
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        point = event.position()
        if event.button() == Qt.MouseButton.MiddleButton or (
            event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self._drag_mode = "pan"
            self._drag_start_pos = (point.x(), point.y())
            self._drag_start_pan = QPointF(self._pan_offset)
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        hit_index = self._hit_overlay_index(point)
        if hit_index is None:
            return
        if hit_index != self.active_layer_idx:
            self.active_layer_idx = hit_index
            self.layer_selected.emit(hit_index)

        handle_rect = self._resize_handle_rect()
        overlay_rect = self._overlay_rect(hit_index)
        if handle_rect is not None and handle_rect.contains(point):
            self._drag_mode = "resize"
        elif overlay_rect is not None and overlay_rect.contains(point):
            self._drag_mode = "move"
        else:
            return

        self._drag_start_pos = (point.x(), point.y())
        self._drag_start_placement = self.overlays[hit_index].placement
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_mode == "pan":
            if self._drag_start_pos is None or self._drag_start_pan is None:
                self._update_cursor(event)
                return
            diff_x = event.position().x() - self._drag_start_pos[0]
            diff_y = event.position().y() - self._drag_start_pos[1]
            self._pan_offset = QPointF(self._drag_start_pan.x() + diff_x, self._drag_start_pan.y() + diff_y)
            self.update()
            event.accept()
            return

        if self._drag_mode is None or self._drag_start_placement is None:
            self._update_cursor(event)
            return

        delta_x, delta_y = self._base_delta_from_mouse(event)
        start = self._drag_start_placement
        if self._drag_mode == "move":
            candidate_x, candidate_y = start.x + delta_x, start.y + delta_y
            snap_d = 10
            base_w, base_h = self.base_image.size
            ov_w_t, ov_h_t = get_transformed_overlay_size(start.width, self.overlays[self.active_layer_idx].image.size, start.rotation)
            cx, cy = base_w / 2 - ov_w_t / 2, base_h / 2 - ov_h_t / 2
            self._current_snaps = []
            
            if not start.tile:
                for tx in (0, cx, base_w - ov_w_t):
                    if abs(candidate_x - tx) < snap_d:
                        candidate_x = tx
                        self._current_snaps.append(("v", tx + ov_w_t/2 if tx == cx else (tx if tx==0 else base_w)))
                        break
                for ty in (0, cy, base_h - ov_h_t):
                    if abs(candidate_y - ty) < snap_d:
                        candidate_y = ty
                        self._current_snaps.append(("h", ty + ov_h_t/2 if ty == cy else (ty if ty==0 else base_h)))
                        break
            next_placement = replace(start, x=int(candidate_x), y=int(candidate_y))
        else:
            growth = delta_x if abs(delta_x) >= abs(delta_y) else delta_y
            next_placement = replace(start, width=max(1, start.width + growth))

        self.placement_changed.emit(next_placement)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_mode is None:
            return
        self._drag_mode = None
        self._current_snaps = []
        self._drag_start_pos = None
        self._drag_start_placement = None
        self._drag_start_pan = None
        if 0 <= self.active_layer_idx < len(self.overlays):
            self.placement_committed.emit((self.active_layer_idx, self.overlays[self.active_layer_idx].placement))
        self._update_cursor(event)
        event.accept()

    def _update_cursor(self, event: QMouseEvent) -> None:
        point = event.position()
        handle_rect = self._resize_handle_rect()
        overlay_rect = self._overlay_rect(self.active_layer_idx)
        if handle_rect is not None and handle_rect.contains(point):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif self._hit_overlay_index(point) is not None or (overlay_rect is not None and overlay_rect.contains(point)):
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.unsetCursor()


class ImageLoadWorker(QObject):
    loaded = Signal(object, object, object, object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, path: Path, role: str, reset_placement: bool) -> None:
        super().__init__()
        self.path = path
        self.role = role
        self.reset_placement = reset_placement

    def run(self) -> None:
        try:
            image = load_rgba_image(self.path)
        except (OSError, RuntimeError, FileNotFoundError) as exc:
            self.failed.emit(str(exc))
        else:
            self.loaded.emit(self.path, self.role, self.reset_placement, image)
        finally:
            self.finished.emit()


class ExportWorker(QObject):
    saved = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        base_image: Image.Image,
        base_path: Path | None,
        overlays: list[OverlayItem],
        output_path: Path,
        output_format: str,
    ) -> None:
        super().__init__()
        self.base_image = base_image
        self.base_path = base_path
        self.overlays = overlays
        self.output_path = output_path
        self.output_format = output_format

    def run(self) -> None:
        try:
            saved_path = export_image(
                self.base_image,
                self.base_path,
                self.overlays,
                self.output_path,
                output_format=self.output_format,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            self.failed.emit(str(exc))
        else:
            self.saved.emit(saved_path)
        finally:
            self.finished.emit()


class OverlayMainWindow(QMainWindow):
    def __init__(self, args: object, language: str) -> None:
        super().__init__()
        self.args = args
        self.language = language
        self.project_dir = get_project_dir()
        self.settings = QSettings("ImageOverlayTool", "ImageOverlayTool")

        self.base_path: Path | None = None
        self.output_path = Path(getattr(args, 'out', 'merged.png')).expanduser()
        self.base_image: Image.Image | None = None
        self.overlays: list[OverlayItem] = []
        self.default_overlays: list[OverlayItem] = []
        self.active_layer_idx: int = -1
        self.history: list[list[OverlayItem]] = []
        self.history_index = -1
        self.pending_files: list[Path] = []
        self._syncing_controls = False
        self._last_imported_path: Path | None = None
        self._image_load_thread: QThread | None = None
        self._image_load_worker: ImageLoadWorker | None = None
        self._export_thread: QThread | None = None
        self._export_worker: ExportWorker | None = None
        self._export_dialog: QProgressDialog | None = None
        self._export_saved_path: Path | None = None
        self._export_error_message: str | None = None
        self._export_format: str = "png"

        self.setAcceptDrops(True)
        self._build_ui()
        self._refresh_texts()
        self._load_initial_inputs()
        self._update_all()

    def _t(self, key: str, **kwargs: object) -> str:
        return get_text(self.language, key, **kwargs)

    def _build_ui(self) -> None:
        self.resize(1320, 860)
        self.setMinimumSize(720, 420)

        toolbar = QToolBar(self)
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        toolbar.setStyleSheet(
            """
            QToolBar {
                spacing: 8px;
                padding: 8px;
                border-bottom: 1px solid #d0d5dd;
                background: #f8fafc;
            }
            QToolButton {
                color: #1f2937;
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 7px 12px;
                font-weight: 600;
            }
            QToolButton:hover {
                background: #eef6ff;
                border-color: #5b9bd5;
            }
            QToolButton:pressed {
                background: #dbeafe;
            }
            QToolButton:disabled {
                color: #6b7280;
                background: #f3f4f6;
                border-color: #d1d5db;
                font-weight: 500;
            }
            """
        )
        self.addToolBar(toolbar)
        self.toolbar = toolbar

        self.import_action = QAction("", self)
        self.undo_action = QAction("", self)
        self.redo_action = QAction("", self)
        self.reset_action = QAction("", self)
        self.save_action = QAction("", self)
        self.quit_action = QAction("", self)

        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.redo_action.setShortcuts(
            [
                QKeySequence.StandardKey.Redo,
                QKeySequence("Ctrl+Shift+Z"),
            ]
        )
        self.redo_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)

        for action in (
            self.import_action,
        ):
            toolbar.addAction(action)
        toolbar.addSeparator()
        for action in (
            self.undo_action,
            self.redo_action,
            self.reset_action,
            self.save_action,
            self.quit_action,
        ):
            toolbar.addAction(action)

        self.import_action.triggered.connect(self._import_images)
        self.undo_action.triggered.connect(self._undo)
        self.redo_action.triggered.connect(self._redo)
        self.reset_action.triggered.connect(self._reset)
        self.save_action.triggered.connect(self._save)
        self.quit_action.triggered.connect(self.close)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.preview = PreviewCanvas(self.language)
        self.preview.placement_changed.connect(lambda placement: self._set_placement(self.active_layer_idx, placement, record=False))
        self.preview.placement_committed.connect(lambda event: self._commit_preview_placement(event[0], event[1]))
        self.preview.layer_selected.connect(self._select_layer_from_preview)
        splitter.addWidget(self.preview)
        self.side_panel = self._build_side_panel()
        splitter.addWidget(self.side_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        splitter.splitterMoved.connect(lambda *_args: self._ensure_side_panel_visible())
        self.setCentralWidget(splitter)

    def _build_side_panel(self) -> QWidget:
        panel = QWidget(self)
        panel.setMinimumWidth(350)
        panel.setMaximumWidth(390)
        layout = QVBoxLayout(panel)

        self.file_frame = QFrame(panel)
        file_layout = QFormLayout(self.file_frame)
        self.base_value_label = QLabel("")
        file_layout.addRow(QLabel(), self.base_value_label)
        self.base_caption_label = file_layout.labelForField(self.base_value_label)
        self.layers_caption_label = QLabel(panel)
        self.layer_list = QListWidget(panel)
        self.layer_list.currentRowChanged.connect(self._on_layer_selected)
        self.add_layer_button = QPushButton("Add Layer", panel)
        self.add_layer_button.clicked.connect(lambda: self._open_image("overlay"))
        self.remove_layer_button = QPushButton("Remove Layer", panel)
        self.remove_layer_button.clicked.connect(self._remove_layer)
        layer_btn_layout = QHBoxLayout()
        layer_btn_layout.addWidget(self.add_layer_button)
        layer_btn_layout.addWidget(self.remove_layer_button)
        
        file_layout.addRow(self.layers_caption_label, self.layer_list)
        file_layout.addRow(QLabel(), layer_btn_layout)
        layout.addWidget(self.file_frame)

        self.language_combo = QComboBox(panel)
        for code, display_name in LANGUAGE_NAMES.items():
            self.language_combo.addItem(display_name, code)
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)

        self.recent_combo = QComboBox(panel)
        self.recent_combo.currentIndexChanged.connect(self._on_recent_changed)
        self.recent_base_button = QPushButton(panel)
        self.recent_overlay_button = QPushButton(panel)
        self.recent_base_button.clicked.connect(lambda: self._load_recent_as("base"))
        self.recent_overlay_button.clicked.connect(lambda: self._load_recent_as("overlay"))
        recent_buttons = QVBoxLayout()
        recent_buttons.setContentsMargins(0, 0, 0, 0)
        recent_buttons.setSpacing(6)
        recent_buttons.addWidget(self.recent_base_button)
        recent_buttons.addWidget(self.recent_overlay_button)
        recent_box = QWidget(panel)
        recent_box.setLayout(recent_buttons)
        self.pending_hint_label = QLabel(panel)
        self.pending_hint_label.setWordWrap(True)
        self.pending_hint_label.setStyleSheet("color: #475569;")

        self.output_edit = QLineEdit(str(self.output_path), panel)
        self.output_edit.editingFinished.connect(self._on_output_edited)
        self.choose_output_button = QPushButton(panel)
        self.choose_output_button.clicked.connect(self._choose_output_path)
        output_box = QWidget(panel)
        output_layout = QHBoxLayout(output_box)
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.addWidget(self.output_edit)
        output_layout.addWidget(self.choose_output_button)

        self.format_combo = QComboBox(panel)
        self._populate_format_combo("png")
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)

        settings_layout = QFormLayout()
        self.language_label = QLabel(panel)
        self.recent_label = QLabel(panel)
        self.output_label = QLabel(panel)
        self.format_label = QLabel(panel)
        settings_layout.addRow(self.language_label, self.language_combo)
        settings_layout.addRow(self.recent_label, self.recent_combo)
        settings_layout.addRow(QLabel(), recent_box)
        settings_layout.addRow(QLabel(), self.pending_hint_label)
        settings_layout.addRow(self.output_label, output_box)
        settings_layout.addRow(self.format_label, self.format_combo)
        layout.addLayout(settings_layout)

        self.export_check_frame = QFrame(panel)
        self.export_check_frame.setStyleSheet(
            """
            QFrame {
                background: #f8fafc;
                border: 1px solid #d0d5dd;
                border-radius: 6px;
            }
            QLabel {
                border: none;
                background: transparent;
            }
            """
        )
        export_check_layout = QVBoxLayout(self.export_check_frame)
        export_check_layout.setContentsMargins(10, 8, 10, 8)
        export_check_layout.setSpacing(6)
        export_check_header = QHBoxLayout()
        export_check_header.setContentsMargins(0, 0, 0, 0)
        self.export_check_title_label = QLabel(panel)
        self.export_check_title_label.setStyleSheet("font-weight: 600; color: #111827;")
        self.export_check_badge_label = QLabel(panel)
        self.export_check_badge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.export_check_badge_label.setMinimumWidth(54)
        export_check_header.addWidget(self.export_check_title_label)
        export_check_header.addStretch(1)
        export_check_header.addWidget(self.export_check_badge_label)
        self.export_check_details_label = QLabel(panel)
        self.export_check_details_label.setWordWrap(True)
        self.export_check_details_label.setStyleSheet("color: #374151; line-height: 1.25;")
        export_check_layout.addLayout(export_check_header)
        export_check_layout.addWidget(self.export_check_details_label)
        layout.addWidget(self.export_check_frame)

        self.placement_frame = QFrame(panel)
        placement_layout = QFormLayout(self.placement_frame)
        self.x_spin = QSpinBox(panel)
        self.y_spin = QSpinBox(panel)
        self.width_spin = QSpinBox(panel)
        for spin in (self.x_spin, self.y_spin, self.width_spin):
            spin.setRange(0, 999999)
            spin.valueChanged.connect(self._on_numeric_control_changed)
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal, panel)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(100)
        self.opacity_value_label = QLabel("100%", panel)
        self.opacity_slider.valueChanged.connect(self._on_numeric_control_changed)
        opacity_box = QWidget(panel)
        opacity_layout = QHBoxLayout(opacity_box)
        opacity_layout.setContentsMargins(0, 0, 0, 0)
        opacity_layout.addWidget(self.opacity_slider)
        opacity_layout.addWidget(self.opacity_value_label)
        self.blend_combo = QComboBox(panel)
        self.tile_check = QCheckBox(panel)
        self.remove_white_check = QCheckBox(panel)
        for cb in (self.blend_combo, self.tile_check, self.remove_white_check): cb.currentIndexChanged.connect(self._on_numeric_control_changed) if isinstance(cb, QComboBox) else cb.stateChanged.connect(self._on_numeric_control_changed)

        self.rotation_spin = QDoubleSpinBox(panel)
        self.rotation_spin.setRange(-180.0, 180.0)
        self.rotation_spin.setDecimals(1)
        self.rotation_spin.setSingleStep(1.0)
        self.rotation_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.rotation_spin.valueChanged.connect(self._on_numeric_control_changed)
        self.rotation_minus_button = QPushButton("-", panel)
        self.rotation_plus_button = QPushButton("+", panel)
        for button in (self.rotation_minus_button, self.rotation_plus_button):
            button.setFixedWidth(42)
            button.setAutoRepeat(True)
        self.rotation_minus_button.clicked.connect(lambda: self._adjust_rotation(-1.0))
        self.rotation_plus_button.clicked.connect(lambda: self._adjust_rotation(1.0))
        rotation_box = QWidget(panel)
        rotation_layout = QHBoxLayout(rotation_box)
        rotation_layout.setContentsMargins(0, 0, 0, 0)
        rotation_layout.setSpacing(6)
        rotation_layout.addWidget(self.rotation_minus_button)
        rotation_layout.addWidget(self.rotation_spin)
        rotation_layout.addWidget(self.rotation_plus_button)

        self.x_label = QLabel(panel)
        self.y_label = QLabel(panel)
        self.width_label = QLabel(panel)
        self.opacity_label = QLabel(panel)
        self.rotation_label = QLabel(panel)
        self.blend_label = QLabel(panel)
        placement_layout.addRow(self.x_label, self.x_spin)
        placement_layout.addRow(self.y_label, self.y_spin)
        placement_layout.addRow(self.width_label, self.width_spin)
        placement_layout.addRow(self.opacity_label, opacity_box)
        placement_layout.addRow(self.blend_label, self.blend_combo)
        placement_layout.addRow('', self.tile_check)
        placement_layout.addRow('', self.remove_white_check)
        placement_layout.addRow(self.rotation_label, rotation_box)
        layout.addWidget(self.placement_frame)

        self.status_label = QLabel(panel)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(panel)
        scroll.setFixedWidth(430)
        return scroll

    def _format_display_text(self, output_format: str) -> str:
        return output_format

    def _populate_format_combo(self, selected_format: str | None = None) -> None:
        if selected_format is None:
            selected_format = self._selected_output_format()
        self._syncing_controls = True
        self.format_combo.clear()
        for output_format in SUPPORTED_OUTPUT_FORMATS:
            self.format_combo.addItem(self._format_display_text(output_format), output_format)
        index = self.format_combo.findData(selected_format)
        self.format_combo.setCurrentIndex(index if index >= 0 else 0)
        self._syncing_controls = False

    def _populate_blend_combo(self) -> None:
        current_value = self.blend_combo.currentData() or "normal"
        self._syncing_controls = True
        self.blend_combo.clear()
        self.blend_combo.addItem(self._t("blend_normal"), "normal")
        self.blend_combo.addItem(self._t("blend_multiply"), "multiply")
        self.blend_combo.addItem(self._t("blend_screen"), "screen")
        index = self.blend_combo.findData(current_value)
        self.blend_combo.setCurrentIndex(index if index >= 0 else 0)
        self._syncing_controls = False

    def _selected_output_format(self) -> str:
        if not hasattr(self, "format_combo"):
            return "png"
        selected = self.format_combo.currentData()
        if isinstance(selected, str) and selected:
            return selected
        return self.format_combo.currentText() or "png"

    def _refresh_texts(self) -> None:
        self.setWindowTitle(self._t("window_title"))
        self.import_action.setText(self._t("import_images_button"))
        self.import_action.setToolTip(self._t("import_images_tooltip"))
        self.undo_action.setText(self._t("undo_button"))
        self.undo_action.setToolTip(self._t("undo_tooltip"))
        self.redo_action.setText(self._t("redo_button"))
        self.redo_action.setToolTip(self._t("redo_tooltip"))
        self.reset_action.setText(self._t("reset_button"))
        self.reset_action.setToolTip(self._t("reset_tooltip"))
        self.save_action.setText(self._t("save_button"))
        self.save_action.setToolTip(self._t("save_tooltip"))
        self.quit_action.setText(self._t("quit_button"))
        self.quit_action.setToolTip(self._t("quit_tooltip"))

        self.base_caption_label.setText(self._t("base_file_label"))
        self.layers_caption_label.setText(self._t("layers_group_label"))
        self.layer_list.setToolTip(self._t("layers_group_tooltip"))
        self.add_layer_button.setText(self._t("add_layer_button"))
        self.remove_layer_button.setText(self._t("remove_layer_button"))
        self.language_label.setText(self._t("language_label"))
        self.recent_label.setText(self._t("recent_label"))
        self.output_label.setText(self._t("output_label"))
        self.format_label.setText(self._t("format_label"))
        self.choose_output_button.setText("...")
        self.recent_base_button.setText(self._t("recent_base_button"))
        self.recent_overlay_button.setText(self._t("recent_overlay_button"))
        if self._last_imported_path is not None:
            self.pending_hint_label.setText(self._t("import_success", name=self._last_imported_path.name))
        else:
            self.pending_hint_label.setText(self._t("recent_hint"))
        self.x_label.setText(self._t("x_label"))
        self.y_label.setText(self._t("y_label"))
        self.width_label.setText(self._t("width_label"))
        self.opacity_label.setText(self._t("opacity_label"))
        self.rotation_label.setText(self._t("rotation_label"))
        self.blend_label.setText(self._t("blend_mode_label"))
        self.tile_check.setText(self._t("tile_label"))
        self.tile_check.setToolTip(self._t("tile_tooltip"))
        self.remove_white_check.setText(self._t("remove_white_label"))
        self.remove_white_check.setToolTip(self._t("remove_white_tooltip"))
        self.export_check_title_label.setText(self._t("export_check_label"))
        self._populate_format_combo()
        self._populate_blend_combo()

        language_index = self.language_combo.findData(self.language)
        if language_index >= 0:
            self._syncing_controls = True
            self.language_combo.setCurrentIndex(language_index)
            self._syncing_controls = False

        self.preview.set_language(self.language)
        self._refresh_recent_combo()
        self._update_export_quality()
        self._update_status()

    def _load_initial_inputs(self) -> None:
        errors: list[str] = []
        base_arg = getattr(self.args, "base", None)
        overlay_arg = getattr(self.args, "overlay", None)
        if base_arg:
            try:
                self._load_image_path(resolve_input_path(base_arg, self.project_dir), "base", reset_placement=False)
            except (OSError, RuntimeError, FileNotFoundError) as exc:
                errors.append(str(exc))
        if overlay_arg:
            try:
                self._load_image_path(resolve_input_path(overlay_arg, self.project_dir), "overlay", reset_placement=False)
            except (OSError, RuntimeError, FileNotFoundError) as exc:
                errors.append(str(exc))

        if self.base_image is not None and 0 <= self.active_layer_idx < len(self.overlays):
            active_overlay = self.overlays[self.active_layer_idx]
            initial = build_initial_placement(
                getattr(self.args, "x", 0),
                getattr(self.args, "y", 0),
                getattr(self.args, "width", None),
                active_overlay.image.size,
                opacity=getattr(self.args, "opacity", 1.0),
                rotation=getattr(self.args, "rotation", 0.0),
            )
            self._reset_placement(initial)

        output_format = getattr(self.args, "format", None)
        if output_format:
            self._set_format(output_format)
        else:
            suffix = self.output_path.suffix.lower().lstrip(".")
            self._set_format(suffix if suffix in SUPPORTED_OUTPUT_FORMATS else "png")

        if errors:
            QMessageBox.critical(self, self._t("open_failed_title"), "\n".join(errors))
        if self.base_image is not None:
            self._fit_window_to_base_image()


    def _import_images(self, *_args: object) -> None:
        selected_path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            self._t("import_file_title"),
            str(self.base_path.parent if self.base_path else self.project_dir),
            build_image_filter(),
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not selected_path:
            return

        imported_path = Path(selected_path).expanduser().resolve()
        self._last_imported_path = imported_path
        self._add_pending_file(imported_path)
        self._refresh_recent_combo()
        index = self.recent_combo.findData(str(imported_path))
        if index >= 0:
            self.recent_combo.setCurrentIndex(index)
        self.pending_hint_label.setText(self._t("import_success", name=imported_path.name))
        self._update_status()


    def _open_image(self, role: str) -> None:
        title = self._t("open_base_title") if role == "base" else self._t("open_overlay_title")
        selected_path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            title,
            str(self.project_dir),
            build_image_filter(),
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not selected_path:
            return
        try:
            self._load_image_path_async(Path(selected_path), role)
        except (OSError, RuntimeError, FileNotFoundError) as exc:
            QMessageBox.critical(self, self._t("open_failed_title"), str(exc))


    def _fit_window_to_base_image(self) -> None:
        if self.base_image is None:
            return

        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else self.geometry()
        side_width = self.side_panel.width() if hasattr(self, "side_panel") else 430
        if side_width <= 0:
            side_width = 430
        splitter_handle_width = 6

        toolbar_height = self.toolbar.sizeHint().height()
        chrome_width = max(28, self.frameGeometry().width() - self.geometry().width())
        chrome_height = max(48, self.frameGeometry().height() - self.geometry().height())
        max_preview_width = max(240, available.width() - side_width - splitter_handle_width - chrome_width - 48)
        max_preview_height = max(180, available.height() - toolbar_height - chrome_height - 48)

        scale = min(
            1.0,
            max_preview_width / self.base_image.width,
            max_preview_height / self.base_image.height,
        )
        preview_width = max(240, round(self.base_image.width * scale))
        preview_height = max(180, round(self.base_image.height * scale))
        self.preview.setMinimumSize(240, 180)
        self.preview.setMaximumSize(16777215, 16777215)
        window_width = preview_width + side_width + splitter_handle_width
        window_height = preview_height + toolbar_height + chrome_height
        self.resize(window_width, window_height)
        splitter = self.centralWidget()
        if isinstance(splitter, QSplitter):
            splitter.setSizes([preview_width, side_width])
            self._ensure_side_panel_visible()

    def _adjust_rotation(self, delta: float) -> None:
        self.rotation_spin.setValue(self.rotation_spin.value() + delta)

    def _on_language_changed(self, *_args: object) -> None:
        if self._syncing_controls:
            return
        selected_language = self.language_combo.currentData()
        if selected_language in LANGUAGE_NAMES and selected_language != self.language:
            self.language = selected_language
            self.settings.setValue("language", selected_language)
            self._refresh_texts()

    def _on_output_edited(self) -> None:
        value = self.output_edit.text().strip()
        if value:
            self.output_path = Path(value).expanduser()
            self._update_status()

    def _on_format_changed(self, *_args: object) -> None:
        if self._syncing_controls:
            return
        selected_format = self._selected_output_format()
        self.output_path = output_path_for_format(self.output_path, selected_format)
        self.output_edit.setText(str(self.output_path))
        self._update_export_quality()
        self._update_status()

    def _set_format(self, output_format: str) -> None:
        index = self.format_combo.findData(output_format)
        if index < 0:
            index = 0
        self._syncing_controls = True
        self.format_combo.setCurrentIndex(index)
        self._syncing_controls = False
        self._populate_blend_combo()
        self.output_path = output_path_for_format(self.output_path, self._selected_output_format())
        self.output_edit.setText(str(self.output_path))

    def _choose_output_path(self, *_args: object) -> None:
        selected_format = self._selected_output_format()
        selected_path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            self._t("output_dialog_title"),
            str(output_path_for_format(self.output_path, selected_format)),
            f"{selected_format.upper()} (*.{selected_format});;All files (*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not selected_path:
            return
        self.output_path = output_path_for_format(Path(selected_path).expanduser(), selected_format)
        self.output_edit.setText(str(self.output_path))
        self._update_status()











    def _read_pending_files(self) -> list[Path]:
        self.pending_files = [path for path in self.pending_files if path.exists()]
        return list(self.pending_files)

    def _add_pending_file(self, path: Path) -> None:
        resolved = path.resolve()
        paths = [candidate for candidate in self._read_pending_files() if candidate.resolve() != resolved]
        paths.insert(0, resolved)
        self.pending_files = paths[:RECENT_LIMIT]

    def _refresh_recent_combo(self) -> None:
        current_data = self.recent_combo.currentData()
        self._syncing_controls = True
        self.recent_combo.clear()
        pending_files = self._read_pending_files()
        if not pending_files:
            self.recent_combo.addItem(self._t("recent_empty"), None)
        else:
            for path in pending_files:
                self.recent_combo.addItem(path.name, str(path))
        if current_data is not None:
            index = self.recent_combo.findData(current_data)
            if index >= 0:
                self.recent_combo.setCurrentIndex(index)
        self._syncing_controls = False
        self._on_recent_changed()

    def _on_recent_changed(self, *_args: object) -> None:
        has_pending = bool(self.recent_combo.currentData())
        self.recent_base_button.setEnabled(has_pending)
        self.recent_overlay_button.setEnabled(has_pending)
        if not has_pending:
            self.pending_hint_label.setText(self._t("no_pending_hint"))
        elif self._last_imported_path is None:
            self.pending_hint_label.setText(self._t("recent_hint"))

    def _load_recent_as(self, role: str) -> None:
        selected_path = self.recent_combo.currentData()
        if not selected_path:
            return
        try:
            self._load_image_path_async(Path(str(selected_path)), role)
        except (OSError, RuntimeError, FileNotFoundError) as exc:
            QMessageBox.critical(self, self._t("open_failed_title"), str(exc))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [
            Path(url.toLocalFile())
            for url in event.mimeData().urls()
            if url.isLocalFile()
            and Path(url.toLocalFile()).suffix.lower() in PREFERRED_INPUT_SUFFIXES
        ]
        if not paths:
            return

        try:
            if len(paths) >= 2:
                self._load_image_path_async(paths[0], "base")
                self._add_pending_file(paths[1])
                self._refresh_recent_combo()
            elif self.base_image is None:
                self._load_image_path_async(paths[0], "base")
            else:
                self._load_image_path_async(paths[0], "overlay")
        except (OSError, RuntimeError, FileNotFoundError) as exc:
            QMessageBox.critical(self, self._t("open_failed_title"), str(exc))
            return
        event.acceptProposedAction()

    def _on_layer_selected(self, index: int) -> None:
        if self._syncing_controls:
            return
        self.active_layer_idx = index
        self._update_all()

    def _select_layer_from_preview(self, index: int) -> None:
        if 0 <= index < len(self.overlays):
            self.active_layer_idx = index
            self._update_all()

    def _remove_layer(self, *_args: object) -> None:
        if 0 <= self.active_layer_idx < len(self.overlays):
            del self.overlays[self.active_layer_idx]
            if not self.overlays:
                self.active_layer_idx = -1
            else:
                self.active_layer_idx = max(0, min(self.active_layer_idx, len(self.overlays) - 1))
            self._store_default_state()
            self._commit_history()
            self._update_all()

    def _sync_layer_list(self) -> None:
        self._syncing_controls = True
        self.layer_list.clear()
        for i, item in enumerate(self.overlays):
            name = item.name or self._t("layer_item_name", index=i + 1)
            self.layer_list.addItem(name)
        if 0 <= self.active_layer_idx < len(self.overlays):
            self.layer_list.setCurrentRow(self.active_layer_idx)
        self._syncing_controls = False

    def _store_default_state(self) -> None:
        self.default_overlays = deepcopy(self.overlays)

    def _reset_history(self) -> None:
        if self.overlays:
            self.history = [deepcopy(self.overlays)]
            self.history_index = 0
        else:
            self.history = []
            self.history_index = -1

    def _load_image_pair(self, base_path: Path, overlay_path: Path) -> None:
        resolved_base = base_path.expanduser().resolve()
        resolved_overlay = overlay_path.expanduser().resolve()
        if resolved_base == resolved_overlay:
            raise ValueError(self._t("selector_same_file"))

        base_image = load_rgba_image(resolved_base)
        overlay_image = load_rgba_image(resolved_overlay)
        if self.base_image is not None: self.base_image.close()
        for item in self.overlays: item.image.close()

        self.base_path = resolved_base
        self.base_image = base_image
        width = min(overlay_image.width, get_max_overlay_width(self.base_image.size, overlay_image.size))
        item = OverlayItem(image=overlay_image, path=resolved_overlay, placement=Placement(x=0, y=0, width=width), name=resolved_overlay.name)
        self.overlays = [item]
        self.active_layer_idx = 0
        self._add_pending_file(resolved_base)
        self._add_pending_file(resolved_overlay)
        self._store_default_state()
        self._reset_history()
        self._update_all()
        self._fit_window_to_base_image()

    def _set_loading_state(self, is_loading: bool) -> None:
        if is_loading:
            for widget in (
                self.recent_base_button,
                self.recent_overlay_button,
                self.add_layer_button,
                self.remove_layer_button,
            ):
                widget.setEnabled(False)
            self.import_action.setEnabled(False)
            self.statusBar().showMessage(self._t("loading_image"))
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        else:
            self.import_action.setEnabled(True)
            self.statusBar().clearMessage()
            QApplication.restoreOverrideCursor()
            self._update_all()

    def _load_image_path_async(self, path: Path, role: str, reset_placement: bool = True) -> None:
        if self._image_load_thread is not None and self._image_load_thread.isRunning():
            QMessageBox.information(self, self._t("loading_title"), self._t("loading_busy"))
            return

        resolved_path = path.expanduser().resolve()
        thread = QThread(self)
        worker = ImageLoadWorker(resolved_path, role, reset_placement)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.loaded.connect(self._apply_loaded_image)
        worker.failed.connect(lambda message: QMessageBox.critical(self, self._t("open_failed_title"), message))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._finish_image_load)
        self._image_load_thread = thread
        self._image_load_worker = worker
        self._set_loading_state(True)
        thread.start()

    def _finish_image_load(self) -> None:
        self._image_load_thread = None
        self._image_load_worker = None
        self._set_loading_state(False)

    def _apply_loaded_image(
        self, path: Path, role: str, reset_placement: bool, image: Image.Image
    ) -> None:
        if role == "base":
            if self.base_image is not None:
                self.base_image.close()
            self.base_path = path.resolve()
            self.base_image = image
            if self.overlays:
                for item in self.overlays:
                    item.placement = clamp_placement(item.placement, self.base_image.size, item.image.size)
                self._store_default_state()
                self._reset_history()
        else:
            p = path.resolve()
            width = min(image.width, get_max_overlay_width(self.base_image.size, image.size)) if self.base_image else image.width
            item = OverlayItem(image=image, path=p, placement=Placement(x=0, y=0, width=width), name=p.name)
            self.overlays.append(item)
            self.active_layer_idx = len(self.overlays) - 1
            if reset_placement and self.base_image is not None:
                item.placement = clamp_placement(item.placement, self.base_image.size, item.image.size)
            self._store_default_state()
            self._reset_history()
        self._add_pending_file(path)
        self._update_all()
        if self.base_image is not None:
            self._fit_window_to_base_image()

    def _load_image_path(self, path: Path, role: str, reset_placement: bool = True) -> None:
        image = load_rgba_image(path)
        self._apply_loaded_image(path, role, reset_placement, image)

    def _commit_history(self) -> None:
        state = deepcopy(self.overlays)
        self.history = self.history[: self.history_index + 1]
        self.history.append(state)
        self.history_index = len(self.history) - 1

    def _set_placement(self, idx: int, placement: Placement, record: bool = True) -> None:
        if not (0 <= idx < len(self.overlays)) or self.base_image is None:
            return
        item = self.overlays[idx]
        clamped = clamp_placement(placement, self.base_image.size, item.image.size)
        if item.placement == clamped:
            return
        item.placement = clamped
        if record:
            self._commit_history()
            self._update_all()
        else:
            self._update_live_placement()

    def _reset_placement(self, placement: Placement | None = None) -> None:
        if self.base_image is None or not self.overlays:
            self.default_overlays = []
            self.history = []
            self.history_index = -1
            self.active_layer_idx = -1
            return

        idx = self.active_layer_idx if 0 <= self.active_layer_idx < len(self.overlays) else 0
        item = self.overlays[idx]
        if placement is None:
            width = min(item.image.width, get_max_overlay_width(self.base_image.size, item.image.size))
            placement = Placement(x=0, y=0, width=width)
        item.placement = clamp_placement(placement, self.base_image.size, item.image.size)
        self.active_layer_idx = idx
        self._store_default_state()
        self._reset_history()

    def _update_live_placement(self) -> None:
        self._syncing_controls = True
        if 0 <= self.active_layer_idx < len(self.overlays):
            placement = self.overlays[self.active_layer_idx].placement
            self.x_spin.setValue(placement.x)
            self.y_spin.setValue(placement.y)
            self.width_spin.setValue(placement.width)
            self.opacity_slider.setValue(round(placement.opacity * 100))
            self.opacity_value_label.setText(f"{round(placement.opacity * 100)}%")
            self.rotation_spin.setValue(placement.rotation)
            idx = self.blend_combo.findData(getattr(placement, 'blend_mode', 'normal'))
            self.blend_combo.setCurrentIndex(max(0, idx))
            self.tile_check.setChecked(getattr(placement, 'tile', False))
            self.remove_white_check.setChecked(getattr(placement, 'remove_white', False))
        self._syncing_controls = False
        if 0 <= self.active_layer_idx < len(self.overlays):
            self.preview.set_active_placement(self.overlays[self.active_layer_idx].placement)
        self._update_export_quality()

    def _commit_preview_placement(self, idx: int, placement: Placement) -> None:
        if not (0 <= idx < len(self.overlays)) or self.base_image is None:
            return
        clamped = clamp_placement(placement, self.base_image.size, self.overlays[idx].image.size)
        self.overlays[idx].placement = clamped
        self._commit_history()
        self._update_all()

    def _on_numeric_control_changed(self, *_args: object) -> None:
        if self._syncing_controls or not (0 <= self.active_layer_idx < len(self.overlays)): return
        self.opacity_value_label.setText(f"{self.opacity_slider.value()}%")
        opacity = self.opacity_slider.value() / 100
        rotation = self.rotation_spin.value()
        self._set_placement(
            self.active_layer_idx,
            Placement(
                x=self.x_spin.value(),
                y=self.y_spin.value(),
                width=self.width_spin.value(),
                opacity=opacity,
                rotation=rotation,
                blend_mode=self.blend_combo.currentData() or 'normal',
                tile=self.tile_check.isChecked(),
                remove_white=self.remove_white_check.isChecked(),
            )
        )

    def _undo(self, *_args: object) -> None:
        if self.history_index <= 0:
            return
        self.history_index -= 1
        self.overlays = deepcopy(self.history[self.history_index])
        self.active_layer_idx = max(0, min(self.active_layer_idx, len(self.overlays) - 1)) if self.overlays else -1
        self._update_all()

    def _redo(self, *_args: object) -> None:
        if self.history_index < 0 or self.history_index >= len(self.history) - 1:
            return
        self.history_index += 1
        self.overlays = deepcopy(self.history[self.history_index])
        self.active_layer_idx = max(0, min(self.active_layer_idx, len(self.overlays) - 1)) if self.overlays else -1
        self._update_all()

    def _reset(self, *_args: object) -> None:
        if not self.default_overlays:
            return
        self.overlays = deepcopy(self.default_overlays)
        self.active_layer_idx = max(0, min(self.active_layer_idx, len(self.overlays) - 1)) if self.overlays else -1
        self._commit_history()
        self._update_all()

    def _save(self, *_args: object) -> None:
        if self.base_image is None or not self.overlays:
            QMessageBox.warning(self, self._t("save_failed_title"), self._t("images_required"))
            return
        if self._export_thread is not None and self._export_thread.isRunning():
            QMessageBox.information(self, self._t("exporting_title"), self._t("exporting_busy"))
            return

        self.output_path = Path(self.output_edit.text()).expanduser()
        output_format = self._selected_output_format()
        overlays = [
            OverlayItem(
                image=item.image,
                path=item.path,
                placement=item.placement,
                name=item.name,
            )
            for item in self.overlays
        ]
        thread = QThread(self)
        worker = ExportWorker(
            self.base_image,
            self.base_path,
            overlays,
            self.output_path,
            output_format,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.saved.connect(self._on_export_saved)
        worker.failed.connect(self._on_export_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._finish_export)
        self._export_thread = thread
        self._export_worker = worker
        self._export_saved_path = None
        self._export_error_message = None
        self._export_format = output_format
        self._set_exporting_state(True)
        thread.start()

    def _on_export_saved(self, saved_path: Path) -> None:
        self._export_saved_path = saved_path

    def _on_export_failed(self, message: str) -> None:
        self._export_error_message = message

    def _set_exporting_state(self, is_exporting: bool) -> None:
        if is_exporting:
            self.save_action.setEnabled(False)
            self.import_action.setEnabled(False)
            self.reset_action.setEnabled(False)
            self.undo_action.setEnabled(False)
            self.redo_action.setEnabled(False)
            self.statusBar().showMessage(self._t("exporting_image"))
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            dialog = QProgressDialog(self._t("exporting_image"), "", 0, 0, self)
            dialog.setWindowTitle(self._t("exporting_title"))
            dialog.setCancelButton(None)
            dialog.setWindowModality(Qt.WindowModality.WindowModal)
            dialog.setMinimumDuration(0)
            dialog.show()
            self._export_dialog = dialog
        else:
            if self._export_dialog is not None:
                self._export_dialog.close()
                self._export_dialog.deleteLater()
                self._export_dialog = None
            self.statusBar().clearMessage()
            QApplication.restoreOverrideCursor()
            self.import_action.setEnabled(True)
            self._update_action_state()

    def _finish_export(self) -> None:
        self._export_thread = None
        self._export_worker = None
        self._set_exporting_state(False)
        if self._export_error_message is not None:
            QMessageBox.critical(self, self._t("save_failed_title"), self._export_error_message)
            return
        if self._export_saved_path is None:
            return
        saved_path = self._export_saved_path
        self.output_path = saved_path
        self.output_edit.setText(str(saved_path))
        self._update_status()
        message = self._t("saved_message", path=saved_path)
        QMessageBox.information(self, self._t("saved_title"), message)

    def _update_all(self) -> None:
        self._sync_layer_list()
        self._sync_controls_from_state()
        self._update_preview()
        self._update_export_quality()
        self._update_status()
        self._update_action_state()
        self._refresh_recent_combo()

    def _sync_controls_from_state(self) -> None:
        self._syncing_controls = True
        has_active = 0 <= self.active_layer_idx < len(self.overlays)
        for widget in (self.x_spin, self.y_spin, self.width_spin, self.opacity_slider, self.rotation_spin, self.rotation_minus_button, self.rotation_plus_button, self.blend_combo, self.tile_check, self.remove_white_check):
            widget.setEnabled(has_active)
        self.remove_layer_button.setEnabled(has_active)
        if has_active:
            placement = self.overlays[self.active_layer_idx].placement
            self.x_spin.setValue(placement.x)
            self.y_spin.setValue(placement.y)
            self.width_spin.setValue(placement.width)
            self.opacity_slider.setValue(round(placement.opacity * 100))
            self.opacity_value_label.setText(f"{round(placement.opacity * 100)}%")
            self.rotation_spin.setValue(placement.rotation)
            idx = self.blend_combo.findData(getattr(placement, 'blend_mode', 'normal'))
            self.blend_combo.setCurrentIndex(max(0, idx))
            self.tile_check.setChecked(getattr(placement, 'tile', False))
            self.remove_white_check.setChecked(getattr(placement, 'remove_white', False))
        else:
            self.x_spin.setValue(0)
            self.y_spin.setValue(0)
            self.width_spin.setValue(1)
            self.opacity_slider.setValue(100)
            self.opacity_value_label.setText("100%")
            self.rotation_spin.setValue(0.0)
            self.tile_check.setChecked(False)
            self.remove_white_check.setChecked(False)
        self._syncing_controls = False

    def _update_preview(self) -> None:
        self.preview.set_content(self.base_image, self.overlays, self.active_layer_idx)

    def _update_export_quality(self) -> None:
        if not hasattr(self, "export_check_badge_label"): return
        if self.base_image is None or not self.overlays:
            self.export_check_badge_label.setText(self._t("export_quality_warning"))
            self.export_check_badge_label.setStyleSheet("color: #92400e; background: #fef3c7; border-radius: 9px; padding: 2px 8px;")
            self.export_check_details_label.setText(self._t("quality_no_images"))
            return
        active_index = self.active_layer_idx if 0 <= self.active_layer_idx < len(self.overlays) else 0
        active_item = self.overlays[active_index]
        report = analyze_export_quality(self._selected_output_format(), base_path=self.base_path, overlay_path=active_item.path, overlay_size=active_item.image.size, placement=active_item.placement)
        badge_styles = {
            "ok": (self._t("export_quality_ok"), "color: #065f46; background: #d1fae5; border-radius: 9px; padding: 2px 8px;"),
            "warning": (self._t("export_quality_warning"), "color: #92400e; background: #fef3c7; border-radius: 9px; padding: 2px 8px;"),
            "danger": (self._t("export_quality_danger"), "color: #991b1b; background: #fee2e2; border-radius: 9px; padding: 2px 8px;"),
        }
        badge_text, badge_style = badge_styles.get(report.level, badge_styles["warning"])
        self.export_check_badge_label.setText(badge_text)
        self.export_check_badge_label.setStyleSheet(badge_style)
        self.export_check_details_label.setText("\n".join([self._t(f"quality_{message}") for message in report.messages]))

    def _update_status(self) -> None:
        self.base_value_label.setText(self.base_path.name if self.base_path else "-")
        lines = [self._t("status_ready")]
        if self.base_path is not None:
            lines.append(self._t("status_base_file", value=self.base_path.name))
        if self.base_image is not None:
            lines.append(self._t("status_base_size", width=self.base_image.width, height=self.base_image.height))
        lines.append(self._t("status_overlay_count", count=len(self.overlays)))
        if 0 <= self.active_layer_idx < len(self.overlays):
            active_item = self.overlays[self.active_layer_idx]
            lines.append(self._t("status_overlay_file", value=active_item.path.name))
            lines.append(self._t("status_overlay_original", width=active_item.image.width, height=active_item.image.height))
            overlay_width, overlay_height = get_transformed_overlay_size(
                active_item.placement.width,
                active_item.image.size,
                active_item.placement.rotation,
            )
            lines.append(self._t("status_current_overlay", width=overlay_width, height=overlay_height))
        lines.append(self._t("status_output", value=self.output_path))
        self.status_label.setText("\n".join(lines))

    def _update_action_state(self) -> None:
        has_images = self.base_image is not None and len(self.overlays) > 0
        self.save_action.setEnabled(has_images)
        self.reset_action.setEnabled(bool(self.default_overlays))
        self.undo_action.setEnabled(self.history_index > 0)
        self.redo_action.setEnabled(self.history_index >= 0 and self.history_index < len(self.history) - 1)

    def _ensure_side_panel_visible(self) -> None:
        splitter = self.centralWidget()
        if not isinstance(splitter, QSplitter):
            return
        sizes = splitter.sizes()
        if len(sizes) < 2:
            return
        side_width = max(360, self.side_panel.minimumWidth())
        if sizes[1] >= side_width:
            return
        available_width = splitter.width() - splitter.handleWidth()
        if available_width < side_width + 240:
            side_width = max(120, available_width // 2)
        preview_width = max(1, available_width - side_width)
        splitter.setSizes([preview_width, side_width])

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._ensure_side_panel_visible)

    def closeEvent(self, event: object) -> None:
        if self._export_thread is not None and self._export_thread.isRunning():
            QMessageBox.information(self, self._t("exporting_title"), self._t("exporting_busy"))
            event.ignore()
            return
        if self._image_load_thread is not None and self._image_load_thread.isRunning():
            self._image_load_thread.quit()
            self._image_load_thread.wait(3000)
        if self.base_image is not None: self.base_image.close()
        for item in self.overlays: item.image.close()
        super().closeEvent(event)

def run_app(args: object, language: str) -> int:
    prepare_qt_environment()
    application = QApplication.instance()
    owns_application = application is None
    if application is None:
        application = QApplication(sys.argv)
    application.setStyle("Fusion")

    window = OverlayMainWindow(args, language)
    window.show()

    if owns_application:
        return application.exec()
    return 0
