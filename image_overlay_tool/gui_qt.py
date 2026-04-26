from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys

from PIL import Image
from PySide6.QtCore import QSettings, Qt, QRectF, Signal
from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
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
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QDoubleSpinBox,
    QSplitter,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .core import (
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


def get_project_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def build_image_filter() -> str:
    patterns = " ".join(f"*{suffix}" for suffix in PREFERRED_INPUT_SUFFIXES)
    return f"Images ({patterns});;All files (*)"


def pil_to_pixmap(image: Image.Image) -> QPixmap:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    pixmap = QPixmap()
    pixmap.loadFromData(buffer.getvalue(), "PNG")
    return pixmap


class PreviewCanvas(QWidget):
    placement_changed = Signal(object)
    placement_committed = Signal(object)

    def __init__(self, language: str) -> None:
        super().__init__()
        self.language = language
        self.base_image: Image.Image | None = None
        self.overlay_image: Image.Image | None = None
        self.overlay_path: Path | None = None
        self.placement: Placement | None = None

        self._base_pixmap: QPixmap | None = None
        self._base_pixmap_key: object | None = None
        self._overlay_source_pixmap: QPixmap | None = None
        self._overlay_source_pixmap_key: object | None = None
        self._drag_mode: str | None = None
        self._drag_start_pos: tuple[float, float] | None = None
        self._drag_start_placement: Placement | None = None

        self.setMinimumSize(240, 180)
        self.setMouseTracking(True)
        self.setAutoFillBackground(False)

    def set_language(self, language: str) -> None:
        self.language = language
        self.update()

    def set_content(
        self,
        base_image: Image.Image | None,
        overlay_image: Image.Image | None,
        overlay_path: Path | None,
        placement: Placement | None,
    ) -> None:
        if base_image is not self.base_image:
            self._base_pixmap = None
            self._base_pixmap_key = None
        if overlay_image is not self.overlay_image or overlay_path != self.overlay_path:
            self._overlay_source_pixmap = None
            self._overlay_source_pixmap_key = None
        self.base_image = base_image
        self.overlay_image = overlay_image
        self.overlay_path = overlay_path
        self.placement = placement
        self.update()

    def set_placement(self, placement: Placement | None) -> None:
        self.placement = placement
        self.update()

    def _invalidate_pixmap(self) -> None:
        self._base_pixmap = None
        self._base_pixmap_key = None
        self._overlay_source_pixmap = None
        self._overlay_source_pixmap_key = None

    def _image_rect(self) -> QRectF:
        if self.base_image is None:
            return QRectF()

        margin = 0
        available_width = max(1, self.width() - margin * 2)
        available_height = max(1, self.height() - margin * 2)
        scale = min(
            available_width / self.base_image.width,
            available_height / self.base_image.height,
        )
        display_width = max(1, self.base_image.width * scale)
        display_height = max(1, self.base_image.height * scale)
        return QRectF(
            (self.width() - display_width) / 2,
            (self.height() - display_height) / 2,
            display_width,
            display_height,
        )

    def _display_scale(self) -> float:
        if self.base_image is None:
            return 1.0
        rect = self._image_rect()
        return rect.width() / self.base_image.width

    def _overlay_rect(self) -> QRectF | None:
        if self.base_image is None or self.overlay_image is None or self.placement is None:
            return None
        rect = self._image_rect()
        scale = self._display_scale()
        overlay_width, overlay_height = get_transformed_overlay_size(
            self.placement.width,
            self.overlay_image.size,
            self.placement.rotation,
        )
        return QRectF(
            rect.left() + self.placement.x * scale,
            rect.top() + self.placement.y * scale,
            overlay_width * scale,
            overlay_height * scale,
        )

    def _resize_handle_rect(self) -> QRectF | None:
        overlay_rect = self._overlay_rect()
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

    def _current_overlay_source_pixmap(self) -> QPixmap | None:
        if self.overlay_image is None or self.overlay_path is None or self.placement is None:
            return None

        key = id(self.overlay_image)
        if self._overlay_source_pixmap is not None and self._overlay_source_pixmap_key == key:
            return self._overlay_source_pixmap

        self._overlay_source_pixmap = pil_to_pixmap(self.overlay_image)
        self._overlay_source_pixmap_key = key
        return self._overlay_source_pixmap

    def _draw_overlay_pixmap(
        self, painter: QPainter, overlay_rect: QRectF, overlay_pixmap: QPixmap
    ) -> None:
        if self.overlay_image is None or self.placement is None:
            return

        scale = self._display_scale()
        overlay_width = self.placement.width * scale
        overlay_height = get_overlay_height(self.placement.width, self.overlay_image.size) * scale

        painter.save()
        painter.setOpacity(self.placement.opacity)
        painter.translate(overlay_rect.center())
        painter.rotate(self.placement.rotation)
        painter.drawPixmap(
            QRectF(-overlay_width / 2, -overlay_height / 2, overlay_width, overlay_height),
            overlay_pixmap,
            QRectF(overlay_pixmap.rect()),
        )
        painter.restore()

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.white)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        base_pixmap = self._current_base_pixmap()
        if base_pixmap is None:
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                get_text(self.language, "drop_hint"),
            )
            return

        image_rect = self._image_rect()
        painter.drawPixmap(image_rect, base_pixmap, QRectF(base_pixmap.rect()))

        overlay_pixmap = self._current_overlay_source_pixmap()
        if overlay_pixmap is None:
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
                get_text(self.language, "drop_more_hint"),
            )
            return

        overlay_rect = self._overlay_rect()
        if overlay_rect is None:
            return
        self._draw_overlay_pixmap(painter, overlay_rect, overlay_pixmap)

        pen = QPen(Qt.GlobalColor.cyan)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawRect(overlay_rect)

        handle_rect = self._resize_handle_rect()
        if handle_rect is not None:
            painter.fillRect(handle_rect, Qt.GlobalColor.cyan)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self.placement is None:
            return

        point = event.position()
        handle_rect = self._resize_handle_rect()
        overlay_rect = self._overlay_rect()
        if handle_rect is not None and handle_rect.contains(point):
            self._drag_mode = "resize"
        elif overlay_rect is not None and overlay_rect.contains(point):
            self._drag_mode = "move"
        else:
            return

        self._drag_start_pos = (point.x(), point.y())
        self._drag_start_placement = self.placement
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_mode is None or self._drag_start_placement is None:
            self._update_cursor(event)
            return

        delta_x, delta_y = self._base_delta_from_mouse(event)
        start = self._drag_start_placement
        if self._drag_mode == "move":
            next_placement = Placement(
                x=start.x + delta_x,
                y=start.y + delta_y,
                width=start.width,
                opacity=start.opacity,
                rotation=start.rotation,
            )
        else:
            growth = delta_x if abs(delta_x) >= abs(delta_y) else delta_y
            next_placement = Placement(
                x=start.x,
                y=start.y,
                width=start.width + growth,
                opacity=start.opacity,
                rotation=start.rotation,
            )

        self.placement_changed.emit(next_placement)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_mode is None:
            return
        self._drag_mode = None
        self._drag_start_pos = None
        self._drag_start_placement = None
        if self.placement is not None:
            self.placement_committed.emit(self.placement)
        self._update_cursor(event)
        event.accept()

    def _update_cursor(self, event: QMouseEvent) -> None:
        point = event.position()
        handle_rect = self._resize_handle_rect()
        overlay_rect = self._overlay_rect()
        if handle_rect is not None and handle_rect.contains(point):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif overlay_rect is not None and overlay_rect.contains(point):
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.unsetCursor()


class OverlayMainWindow(QMainWindow):
    def __init__(self, args: object, language: str) -> None:
        super().__init__()
        self.args = args
        self.language = language
        self.project_dir = get_project_dir()
        self.settings = QSettings("ImageOverlayTool", "ImageOverlayTool")

        self.base_path: Path | None = None
        self.overlay_path: Path | None = None
        self.output_path = Path(getattr(args, "out", "merged.png")).expanduser()
        self.base_image: Image.Image | None = None
        self.overlay_image: Image.Image | None = None
        self.placement: Placement | None = None
        self.default_placement: Placement | None = None
        self.history: list[Placement] = []
        self.history_index = -1
        self._syncing_controls = False

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
        self.preview.placement_changed.connect(lambda placement: self._set_placement(placement, record=False))
        self.preview.placement_committed.connect(self._commit_preview_placement)
        splitter.addWidget(self.preview)
        self.side_panel = self._build_side_panel()
        splitter.addWidget(self.side_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        self.setCentralWidget(splitter)

    def _build_side_panel(self) -> QWidget:
        panel = QWidget(self)
        panel.setMinimumWidth(350)
        panel.setMaximumWidth(390)
        layout = QVBoxLayout(panel)

        self.file_frame = QFrame(panel)
        file_layout = QFormLayout(self.file_frame)
        self.base_value_label = QLabel("")
        self.overlay_value_label = QLabel("")
        file_layout.addRow(QLabel(), self.base_value_label)
        file_layout.addRow(QLabel(), self.overlay_value_label)
        self.base_caption_label = file_layout.labelForField(self.base_value_label)
        self.overlay_caption_label = file_layout.labelForField(self.overlay_value_label)
        layout.addWidget(self.file_frame)

        self.language_combo = QComboBox(panel)
        for code, display_name in LANGUAGE_NAMES.items():
            self.language_combo.addItem(display_name, code)
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)

        self.recent_combo = QComboBox(panel)
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
        placement_layout.addRow(self.x_label, self.x_spin)
        placement_layout.addRow(self.y_label, self.y_spin)
        placement_layout.addRow(self.width_label, self.width_spin)
        placement_layout.addRow(self.opacity_label, opacity_box)
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
        if output_format == "svg":
            return self._t("format_svg_embedded")
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
        self.overlay_caption_label.setText(self._t("overlay_file_label"))
        self.language_label.setText(self._t("language_label"))
        self.recent_label.setText(self._t("recent_label"))
        self.output_label.setText(self._t("output_label"))
        self.format_label.setText(self._t("format_label"))
        self.choose_output_button.setText("...")
        self.recent_base_button.setText(self._t("recent_base_button"))
        self.recent_overlay_button.setText(self._t("recent_overlay_button"))
        self.x_label.setText(self._t("x_label"))
        self.y_label.setText(self._t("y_label"))
        self.width_label.setText(self._t("width_label"))
        self.opacity_label.setText(self._t("opacity_label"))
        self.rotation_label.setText(self._t("rotation_label"))
        self.export_check_title_label.setText(self._t("export_check_label"))
        self._populate_format_combo()

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

        if self.base_image is not None and self.overlay_image is not None:
            initial = build_initial_placement(
                getattr(self.args, "x", 0),
                getattr(self.args, "y", 0),
                getattr(self.args, "width", None),
                self.overlay_image.size,
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
        base_path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            self._t("open_base_title"),
            str(self.base_path.parent if self.base_path else self.project_dir),
            build_image_filter(),
        )
        if not base_path:
            return

        overlay_path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            self._t("open_overlay_title"),
            str(Path(base_path).parent),
            build_image_filter(),
        )
        if not overlay_path:
            return

        try:
            self._load_image_pair(Path(base_path), Path(overlay_path))
        except (OSError, RuntimeError, FileNotFoundError, ValueError) as exc:
            QMessageBox.critical(self, self._t("open_failed_title"), str(exc))

    def _load_image_pair(self, base_path: Path, overlay_path: Path) -> None:
        resolved_base = base_path.expanduser().resolve()
        resolved_overlay = overlay_path.expanduser().resolve()
        if resolved_base == resolved_overlay:
            raise ValueError(self._t("selector_same_file"))

        base_image = load_rgba_image(resolved_base)
        overlay_image: Image.Image | None = None
        try:
            overlay_image = load_rgba_image(resolved_overlay)
        except Exception:
            base_image.close()
            raise

        if self.base_image is not None:
            self.base_image.close()
        if self.overlay_image is not None:
            self.overlay_image.close()

        self.base_path = resolved_base
        self.overlay_path = resolved_overlay
        self.base_image = base_image
        self.overlay_image = overlay_image
        self._add_recent_file(resolved_base)
        self._add_recent_file(resolved_overlay)

        width = min(
            self.overlay_image.width,
            get_max_overlay_width(self.base_image.size, self.overlay_image.size),
        )
        self._reset_placement(Placement(x=0, y=0, width=width))
        self._update_all()
        self._fit_window_to_base_image()

    def _open_image(self, role: str) -> None:
        title = self._t("open_base_title") if role == "base" else self._t("open_overlay_title")
        selected_path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            title,
            str(self.project_dir),
            build_image_filter(),
        )
        if not selected_path:
            return
        try:
            self._load_image_path(Path(selected_path), role)
        except (OSError, RuntimeError, FileNotFoundError) as exc:
            QMessageBox.critical(self, self._t("open_failed_title"), str(exc))

    def _load_image_path(self, path: Path, role: str, reset_placement: bool = True) -> None:
        image = load_rgba_image(path)
        if role == "base":
            if self.base_image is not None:
                self.base_image.close()
            self.base_path = path.resolve()
            self.base_image = image
        else:
            if self.overlay_image is not None:
                self.overlay_image.close()
            self.overlay_path = path.resolve()
            self.overlay_image = image
        self._add_recent_file(path)

        if self.base_image is not None and self.overlay_image is not None and reset_placement:
            width = min(
                self.overlay_image.width,
                get_max_overlay_width(self.base_image.size, self.overlay_image.size),
            )
            self._reset_placement(Placement(x=0, y=0, width=width))
        self._update_all()
        if self.base_image is not None:
            self._fit_window_to_base_image()

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
        self.preview.setFixedSize(preview_width, preview_height)
        window_width = preview_width + side_width + splitter_handle_width
        window_height = preview_height + toolbar_height
        self.resize(window_width, window_height)
        splitter = self.centralWidget()
        if isinstance(splitter, QSplitter):
            splitter.setSizes([preview_width, side_width])

    def _reset_placement(self, placement: Placement | None = None) -> None:
        if self.base_image is None or self.overlay_image is None:
            self.placement = None
            self.default_placement = None
            self.history = []
            self.history_index = -1
            return

        if placement is None:
            placement = Placement(x=0, y=0, width=self.overlay_image.width)
        clamped = clamp_placement(placement, self.base_image.size, self.overlay_image.size)
        self.placement = clamped
        self.default_placement = clamped
        self.history = [clamped]
        self.history_index = 0

    def _set_placement(self, placement: Placement, record: bool = True) -> None:
        if self.base_image is None or self.overlay_image is None:
            return

        clamped = clamp_placement(placement, self.base_image.size, self.overlay_image.size)
        if self.placement == clamped:
            return

        self.placement = clamped
        if record:
            self.history = self.history[: self.history_index + 1]
            self.history.append(clamped)
            self.history_index = len(self.history) - 1
            self._update_all()
        else:
            self._update_live_placement()

    def _update_live_placement(self) -> None:
        self._syncing_controls = True
        if self.placement is not None:
            self.x_spin.setValue(self.placement.x)
            self.y_spin.setValue(self.placement.y)
            self.width_spin.setValue(self.placement.width)
            self.opacity_slider.setValue(round(self.placement.opacity * 100))
            self.opacity_value_label.setText(f"{round(self.placement.opacity * 100)}%")
            self.rotation_spin.setValue(self.placement.rotation)
        self._syncing_controls = False
        self.preview.set_placement(self.placement)
        self._update_export_quality()

    def _commit_preview_placement(self, placement: Placement) -> None:
        if self.base_image is None or self.overlay_image is None:
            return
        clamped = clamp_placement(placement, self.base_image.size, self.overlay_image.size)
        if self.history_index < 0 or self.history[self.history_index] != clamped:
            self.history = self.history[: self.history_index + 1]
            self.history.append(clamped)
            self.history_index = len(self.history) - 1
        self.placement = clamped
        self._update_all()

    def _on_numeric_control_changed(self, *_args: object) -> None:
        if self._syncing_controls or self.placement is None:
            return
        self.opacity_value_label.setText(f"{self.opacity_slider.value()}%")
        opacity = self.opacity_slider.value() / 100
        rotation = self.rotation_spin.value()
        self._set_placement(
            Placement(
                x=self.x_spin.value(),
                y=self.y_spin.value(),
                width=self.width_spin.value(),
                opacity=opacity,
                rotation=rotation,
            )
        )

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
        self.output_path = output_path_for_format(self.output_path, self._selected_output_format())
        self.output_edit.setText(str(self.output_path))

    def _choose_output_path(self, *_args: object) -> None:
        selected_format = self._selected_output_format()
        selected_path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            self._t("output_dialog_title"),
            str(output_path_for_format(self.output_path, selected_format)),
            f"{selected_format.upper()} (*.{selected_format});;All files (*)",
        )
        if not selected_path:
            return
        self.output_path = output_path_for_format(Path(selected_path).expanduser(), selected_format)
        self.output_edit.setText(str(self.output_path))
        self._update_status()

    def _undo(self, *_args: object) -> None:
        if self.history_index <= 0:
            return
        self.history_index -= 1
        self.placement = self.history[self.history_index]
        self._update_all()

    def _redo(self, *_args: object) -> None:
        if self.history_index < 0 or self.history_index >= len(self.history) - 1:
            return
        self.history_index += 1
        self.placement = self.history[self.history_index]
        self._update_all()

    def _reset(self, *_args: object) -> None:
        if self.default_placement is not None:
            self._set_placement(self.default_placement)

    def _save(self, *_args: object) -> None:
        if (
            self.base_image is None
            or self.overlay_image is None
            or self.overlay_path is None
            or self.placement is None
        ):
            QMessageBox.warning(self, self._t("save_failed_title"), self._t("images_required"))
            return

        try:
            self.output_path = Path(self.output_edit.text()).expanduser()
            saved_path = export_image(
                self.base_image,
                self.overlay_image,
                self.overlay_path,
                self.output_path,
                self.placement,
                output_format=self._selected_output_format(),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            QMessageBox.critical(self, self._t("save_failed_title"), str(exc))
            return

        self.output_path = saved_path
        self.output_edit.setText(str(saved_path))
        self._update_status()
        message = self._t("saved_message", path=saved_path)
        if self._selected_output_format() == "svg":
            message = f"{message}\n\n{self._t('saved_svg_note')}"
        QMessageBox.information(self, self._t("saved_title"), message)

    def _update_all(self) -> None:
        self._sync_controls_from_state()
        self._update_preview()
        self._update_export_quality()
        self._update_status()
        self._update_action_state()
        self._refresh_recent_combo()

    def _sync_controls_from_state(self) -> None:
        self._syncing_controls = True
        has_images = self.base_image is not None and self.overlay_image is not None and self.placement is not None
        for widget in (
            self.x_spin,
            self.y_spin,
            self.width_spin,
            self.opacity_slider,
            self.rotation_spin,
            self.rotation_minus_button,
            self.rotation_plus_button,
        ):
            widget.setEnabled(has_images)

        if has_images and self.placement is not None:
            self.x_spin.setValue(self.placement.x)
            self.y_spin.setValue(self.placement.y)
            self.width_spin.setValue(self.placement.width)
            self.opacity_slider.setValue(round(self.placement.opacity * 100))
            self.opacity_value_label.setText(f"{round(self.placement.opacity * 100)}%")
            self.rotation_spin.setValue(self.placement.rotation)
        else:
            self.x_spin.setValue(0)
            self.y_spin.setValue(0)
            self.width_spin.setValue(1)
            self.opacity_slider.setValue(100)
            self.opacity_value_label.setText("100%")
            self.rotation_spin.setValue(0.0)
        self._syncing_controls = False

    def _update_preview(self) -> None:
        self.preview.set_content(
            self.base_image,
            self.overlay_image,
            self.overlay_path,
            self.placement,
        )

    def _update_export_quality(self) -> None:
        if not hasattr(self, "export_check_badge_label"):
            return

        if self.base_image is None or self.overlay_image is None or self.placement is None:
            self.export_check_badge_label.setText(self._t("export_quality_warning"))
            self.export_check_badge_label.setStyleSheet(
                "color: #92400e; background: #fef3c7; border-radius: 9px; padding: 2px 8px;"
            )
            self.export_check_details_label.setText(self._t("quality_no_images"))
            return

        report = analyze_export_quality(
            self._selected_output_format(),
            base_path=self.base_path,
            overlay_path=self.overlay_path,
            overlay_size=self.overlay_image.size,
            placement=self.placement,
        )
        badge_styles = {
            "ok": (
                self._t("export_quality_ok"),
                "color: #065f46; background: #d1fae5; border-radius: 9px; padding: 2px 8px;",
            ),
            "warning": (
                self._t("export_quality_warning"),
                "color: #92400e; background: #fef3c7; border-radius: 9px; padding: 2px 8px;",
            ),
            "danger": (
                self._t("export_quality_danger"),
                "color: #991b1b; background: #fee2e2; border-radius: 9px; padding: 2px 8px;",
            ),
        }
        badge_text, badge_style = badge_styles.get(report.level, badge_styles["warning"])
        self.export_check_badge_label.setText(badge_text)
        self.export_check_badge_label.setStyleSheet(badge_style)
        detail_lines = [self._t(f"quality_{message}") for message in report.messages]
        self.export_check_details_label.setText("\n".join(detail_lines))

    def _update_status(self) -> None:
        self.base_value_label.setText(self.base_path.name if self.base_path else "-")
        self.overlay_value_label.setText(self.overlay_path.name if self.overlay_path else "-")
        lines = [self._t("status_ready")]
        if self.base_path is not None:
            lines.append(self._t("status_base_file", value=self.base_path.name))
        if self.overlay_path is not None:
            lines.append(self._t("status_overlay_file", value=self.overlay_path.name))
        if self.base_image is not None:
            lines.append(
                self._t(
                    "status_base_size",
                    width=self.base_image.width,
                    height=self.base_image.height,
                )
            )
        if self.overlay_image is not None:
            lines.append(
                self._t(
                    "status_overlay_original",
                    width=self.overlay_image.width,
                    height=self.overlay_image.height,
                )
            )
        if self.overlay_image is not None and self.placement is not None:
            overlay_width, overlay_height = get_transformed_overlay_size(
                self.placement.width,
                self.overlay_image.size,
                self.placement.rotation,
            )
            lines.append(
                self._t(
                    "status_current_overlay",
                    width=overlay_width,
                    height=overlay_height,
                )
            )
        lines.append(self._t("status_output", value=self.output_path))
        self.status_label.setText("\n".join(lines))

    def _update_action_state(self) -> None:
        has_images = self.base_image is not None and self.overlay_image is not None
        self.save_action.setEnabled(has_images)
        self.reset_action.setEnabled(has_images and self.default_placement is not None)
        self.undo_action.setEnabled(self.history_index > 0)
        self.redo_action.setEnabled(self.history_index >= 0 and self.history_index < len(self.history) - 1)

    def _read_recent_files(self) -> list[Path]:
        raw_value = self.settings.value("recentFiles", [])
        if raw_value is None:
            return []
        if isinstance(raw_value, str):
            values = [raw_value]
        else:
            values = list(raw_value)
        paths = [Path(str(value)) for value in values if str(value)]
        return [path for path in paths if path.exists()]

    def _write_recent_files(self, paths: list[Path]) -> None:
        self.settings.setValue("recentFiles", [str(path) for path in paths[:RECENT_LIMIT]])

    def _add_recent_file(self, path: Path) -> None:
        resolved = path.resolve()
        paths = [candidate for candidate in self._read_recent_files() if candidate.resolve() != resolved]
        paths.insert(0, resolved)
        self._write_recent_files(paths)

    def _refresh_recent_combo(self) -> None:
        current_data = self.recent_combo.currentData()
        self._syncing_controls = True
        self.recent_combo.clear()
        recent_files = self._read_recent_files()
        if not recent_files:
            self.recent_combo.addItem(self._t("recent_empty"), None)
        else:
            for path in recent_files:
                self.recent_combo.addItem(path.name, str(path))
        if current_data is not None:
            index = self.recent_combo.findData(current_data)
            if index >= 0:
                self.recent_combo.setCurrentIndex(index)
        self._syncing_controls = False

    def _load_recent_as(self, role: str) -> None:
        selected_path = self.recent_combo.currentData()
        if not selected_path:
            return
        try:
            self._load_image_path(Path(str(selected_path)), role)
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
                self._load_image_path(paths[0], "base")
                self._load_image_path(paths[1], "overlay")
            elif self.base_image is None:
                self._load_image_path(paths[0], "base")
            else:
                self._load_image_path(paths[0], "overlay")
        except (OSError, RuntimeError, FileNotFoundError) as exc:
            QMessageBox.critical(self, self._t("open_failed_title"), str(exc))
            return
        event.acceptProposedAction()

    def closeEvent(self, event: object) -> None:
        if self.base_image is not None:
            self.base_image.close()
        if self.overlay_image is not None:
            self.overlay_image.close()
        super().closeEvent(event)


def run_app(args: object, language: str) -> int:
    application = QApplication.instance()
    owns_application = application is None
    if application is None:
        application = QApplication(sys.argv)

    window = OverlayMainWindow(args, language)
    window.show()

    if owns_application:
        return application.exec()
    return 0
