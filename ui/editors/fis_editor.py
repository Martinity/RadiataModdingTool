'''BaseEditor for modifiying FIS textures. All FIS textures are displayed as Indexed QImage'''
from __future__ import annotations

from typing import Callable, Any
from pathlib import Path
from PyQt6.QtCore import Qt, QSize, pyqtSignal, QPoint, QTimer, QObject
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QWidget, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QFrame, QFileDialog, QListWidget, QListView, QAbstractItemView, QListWidgetItem, QColorDialog,
    QSlider, QButtonGroup, QStackedLayout, QStackedWidget
)
from PyQt6.QtGui import QPixmap, QImage, QColor, QIcon, QTransform, qRgb, qRed, qGreen, qBlue, qAlpha

from core.contracts import BaseEditor
from core.registry import Registry
from core.node import VfsNode
from core.handlers.fis_leaf import FisEditorPayload, FISInfo, FisHandler

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###----------------------------------------- History ------------------------------------------------###

class HistoryManager(QObject):
    can_undo_changed = pyqtSignal(bool)
    can_redo_changed = pyqtSignal(bool)

    def __init__(self, debounce_ms: int = 400, parent=None):
        super().__init__(parent)
        self.undo_stack: list[QImage] = []
        self.redo_stack: list[QImage] = []

        self._current_state:  QImage | None = None
        self._pending_state:  QImage | None = None

        self.debounce_timer = QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(debounce_ms)
        self.debounce_timer.timeout.connect(self._commit_state)

    def initialize(self, initial_state: QImage) -> None:
        self.debounce_timer.stop()
        self._current_state = initial_state.copy()
        self._pending_state = None
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._emit_status()

    def push_change(self, new_state: QImage) -> None:
        # First edit of a debounce burst: snapshot the pre-edit state for undo.
        if not self.debounce_timer.isActive() and self._current_state is not None:
            self.undo_stack.append(self._current_state.copy())
            self.redo_stack.clear()
        self._pending_state = new_state.copy()
        self.debounce_timer.start()
        self._emit_status()

    def _commit_state(self) -> None:
        # Finalize the coalesced burst as the new current state.
        if self._pending_state is not None:
            self._current_state = self._pending_state
            self._pending_state = None
        self._emit_status()

    def undo(self) -> QImage | None:
        if self.debounce_timer.isActive():
            self.debounce_timer.stop()
            self._commit_state()
        if not self.undo_stack or self._current_state is None:
            return None
        self.redo_stack.append(self._current_state.copy())
        self._current_state = self.undo_stack.pop()
        self._emit_status()
        return self._current_state.copy()

    def redo(self) -> QImage | None:
        if self.debounce_timer.isActive():
            self.debounce_timer.stop()
            self._commit_state()
        if not self.redo_stack or self._current_state is None:
            return None
        self.undo_stack.append(self._current_state.copy())
        self._current_state = self.redo_stack.pop()
        self._emit_status()
        return self._current_state.copy()

    def sync_current(self, img: QImage) -> None:
        '''Keep _current_state in sync after an external mutation'''
        self._current_state = img.copy()

    def _emit_status(self):
        self.can_undo_changed.emit(bool(self.undo_stack))
        self.can_redo_changed.emit(bool(self.redo_stack))

###----------------------------------------- Canvas ------------------------------------------------###

class InteractiveCanvas(QLabel):
    '''Zoom-aware canvas for editing indexed QImages'''
    editing_started = pyqtSignal()
    painted         = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.image_ref: QImage | None = None
        self.zoom_factor:       float = 1.0
        self.selected_color_idx:  int = -1
        self.selected_color:      QColor = QColor(255, 0, 0, 255)
        self.current_tool:        str = 'brush'
        self.brush_size:          int = 1

    def set_image(self, img: QImage, zoom: float) -> None:
        self.image_ref   = img
        self.zoom_factor = zoom
        self.update_display()

    def update_display(self) -> None:
        if not self.image_ref:
            return
        new_size = QSize(
            int(self.image_ref.width()  * self.zoom_factor),
            int(self.image_ref.height() * self.zoom_factor)
        )
        pixmap = QPixmap.fromImage(self.image_ref).scaled(
            new_size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation
        )
        self.setPixmap(pixmap)
        self.resize(pixmap.size())

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.editing_started.emit()
            if self.current_tool == 'bucket':
                self._flood_fill(event.position().toPoint())
            else:
                self._paint_pixel(event.position().toPoint())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            if self.current_tool == 'brush':
                self._paint_pixel(event.position().toPoint())
        super().mouseMoveEvent(event)

    def _paint_pixel(self, pos: QPoint) -> None:
        if not self.image_ref:
            return
        is_indexed = (self.image_ref.format() == QImage.Format.Format_Indexed8)
        if is_indexed and self.selected_color_idx < 0:
            return

        x = int(pos.x() / self.zoom_factor)
        y = int(pos.y() / self.zoom_factor)
        w, h = self.image_ref.width(), self.image_ref.height()

        changed      = False
        start_offset = -(self.brush_size // 2) if self.brush_size != 1 else 0
        end_offset   = start_offset + self.brush_size if self.brush_size != 1 else 1

        target_rgba = self.selected_color.rgba()

        for dx in range(start_offset, end_offset):
            for dy in range(start_offset, end_offset):
                nx, ny = x + dx, y + dy # apply scale factor to coord
                if 0 <= nx < w and 0 <= ny < h:
                    if is_indexed:
                        if self.image_ref.pixelIndex(nx, ny) != self.selected_color_idx:
                            self.image_ref.setPixel(nx, ny, self.selected_color_idx)
                            changed = True
                    else:
                        if self.image_ref.pixel(nx, ny) != target_rgba:
                            self.image_ref.setPixel(nx, ny, target_rgba)
                            changed = True
        if changed:
            self.update_display()
            self.painted.emit()

    def _flood_fill(self, pos: QPoint) -> None:
        '''Flood fill enforced by palette index'''
        if not self.image_ref or self.selected_color_idx < 0:
            return
        is_indexed = (self.image_ref.format() == QImage.Format.Format_Indexed8)
        if is_indexed and self.selected_color_idx < 0:
            return

        x = int(pos.x() / self.zoom_factor)
        y = int(pos.y() / self.zoom_factor)
        w, h = self.image_ref.width(), self.image_ref.height()
        if not (0 <= x < w and 0 <= y < h):
            return
        if is_indexed:
            target_val = self.image_ref.pixelIndex(x, y)

            if target_val == self.selected_color_idx:
                return
            self.image_ref.setPixel(x, y, self.selected_color_idx)
        else:
            target_val = self.image_ref.pixel(x, y)
            new_rgba = self.selected_color.rgba()
            if target_val == new_rgba:
                return
            self.image_ref.setPixel(x, y, new_rgba)

        stack = [(x, y)]
        while stack:
            cx, cy = stack.pop() # get temp central pos
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)): # check adjacent
                nx, ny = cx + dx, cy + dy # adjacent pos calculation
                if 0 <= nx < w and 0 <= ny < h:
                    if is_indexed:
                        if self.image_ref.pixelIndex(nx, ny) == target_val:
                            self.image_ref.setPixel(nx, ny, self.selected_color_idx)
                            stack.append((nx, ny))
                    else:
                        if self.image_ref.pixel(nx, ny) == target_val:
                            self.image_ref.setPixel(nx, ny, self.selected_color.rgba())
                            stack.append((nx, ny))

        self.update_display()
        self.painted.emit()

###-------------------------------------------------- Editor -------------------------------------------------###

@Registry.register_editor(name='FIS Texture Editor', handler=FisHandler, extensions=('.fis',))
class FisEditorWidget(BaseEditor):
    '''Displays and edits a decoded FIS texture (Indexed image) with palette editing and undo/redo.'''

    def __init__(self, parent=None, data_resolver=None) -> None:
        super().__init__(parent)
        self._data_resolver = data_resolver
        self.img:   QImage  | None = None
        self.info:  FISInfo | None = None
        self.raw_fis: bytes | None = None

        self.history = HistoryManager(debounce_ms=400, parent=self)
        self.history.can_undo_changed.connect(lambda _: self._emit_undo_state())
        self.history.can_redo_changed.connect(lambda _: self._emit_undo_state())

        self._zoom_factor: float = 1.0
        self._zoom_step:   float = 1.2
        self._min_zoom:    float = 0.1
        self._max_zoom:    float = 30.0
        self._is_panning:  bool  = False

        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_toolbar())
        body = QHBoxLayout()
        body.setContentsMargins(8, 8, 8, 8)
        body.setSpacing(8)
        body.addWidget(self._build_image_area(), stretch=3)
        body.addWidget(self._build_info_panel(), stretch=1)
        root.addLayout(body)

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName('SurfaceToolbar')
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 5, 10, 5)

        self._btn_export  = QPushButton('Export PNG')
        self._btn_export.setEnabled(False)
        self._btn_export.clicked.connect(self._export_png)

        # self._btn_rotate = QPushButton('Rotate 90°')
        # self._btn_rotate.clicked.connect(self._rotate_texture)

        lay.addStretch()
        # lay.addWidget(self._btn_rotate)
        lay.addWidget(self._btn_export)
        return bar

    def _build_image_area(self) -> QScrollArea:
        self._image_label = InteractiveCanvas()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._image_label.setMinimumSize(64, 64)
        self._image_label.setText('No texture loaded')
        self._image_label.editing_started.connect(self._on_editing_started)
        self._image_label.painted.connect(self._on_painted)

        area = QScrollArea()
        area.setWidget(self._image_label)
        area.setWidgetResizable(False)
        area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        area.viewport().installEventFilter(self)
        self._image_label.installEventFilter(self)
        self._scroll_area = area
        return area

    def _build_info_panel(self) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setFixedWidth(240)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)

        ### INFO Section
        self._info_rows: dict[str, QLabel] = {}
        for key in ('Name', 'PSM', 'BPP', 'Width', 'Height',
                    'Dim mode', 'Swizzled', 'Padded CLUT',
                    'Pal offset', 'Image offset', 'Image size'):
            row = QHBoxLayout()
            row.addWidget(QLabel(f'{key}:'))
            val = QLabel('—')
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            val.setWordWrap(True)
            row.addWidget(val, stretch=1)
            lay.addLayout(row)
            self._info_rows[key] = val
        lay.addStretch()
        lay.addSpacing(10)

        ### TOOLS Sections
        lay.addWidget(QLabel('<b>Tools</b>'))
        tools_row = QHBoxLayout()
        self.btn_brush  = QPushButton('Brush')
        self.btn_bucket = QPushButton('Bucket')
        self.btn_brush.setCheckable(True)
        self.btn_bucket.setCheckable(True)
        self.btn_brush.setChecked(True)
        self._tool_group = QButtonGroup(self)
        self._tool_group.addButton(self.btn_brush,  0)
        self._tool_group.addButton(self.btn_bucket, 1)
        self._tool_group.idClicked.connect(self._on_tool_changed)
        tools_row.addWidget(self.btn_brush)
        tools_row.addWidget(self.btn_bucket)
        lay.addLayout(tools_row)

        size_row = QHBoxLayout()
        size_row.addWidget(QLabel('Brush Size:'))
        self._slider_size = QSlider(Qt.Orientation.Horizontal)
        self._slider_size.setRange(1, 10)
        self._slider_size.setValue(1)
        self._slider_size.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._slider_size.setTickInterval(1)
        self._slider_size.valueChanged.connect(self._on_brush_size_changed)
        size_row.addWidget(self._slider_size)
        lay.addLayout(size_row)
        lay.addSpacing(15)

        self._color_stack = QStackedWidget()

        clut_widget = QWidget()
        clut_lay = QVBoxLayout(clut_widget)
        clut_lay.setContentsMargins(0, 0, 0, 0)
        ### CLUT Section
        clut_lay.addWidget(QLabel('<b>Palette </b>*right click to edit'))

        self._palette_list = QListWidget()
        self._palette_list.setViewMode(QListView.ViewMode.IconMode)
        self._palette_list.setIconSize(QSize(20, 20))
        self._palette_list.setSpacing(2)
        self._palette_list.setResizeMode(QListView.ResizeMode.Adjust)
        self._palette_list.setMovement(QListView.Movement.Static)
        self._palette_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._palette_list.currentRowChanged.connect(self._on_color_selected)
        self._palette_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._palette_list.customContextMenuRequested.connect(self._on_palette_context)

        clut_lay.addWidget(self._palette_list)

        direct_widget = QWidget()
        direct_lay = QVBoxLayout(direct_widget)
        direct_lay.setContentsMargins(0, 0, 0, 0)
        direct_lay.addWidget(QLabel('<b>Active Drawing Color</b>'))

        self._btn_direct_color = QPushButton('Choose COlor...')
        self._btn_direct_color.setFixedHeight(36)
        self._btn_direct_color.clicked.connect(self._pick_direct_color)
        direct_lay.addWidget(self._btn_direct_color)
        direct_lay.addStretch()

        self._color_stack.addWidget(clut_widget)
        self._color_stack.addWidget(direct_widget)

        lay.addWidget(self._color_stack, stretch=1)
        return frame

###----------------------------------- Contractuals ---------------------------------------------###

    def begin_loading(self, node: VfsNode) -> None:
        super().begin_loading(node)
        self._image_label.setText(f'Loading {node.name}...')
        self._image_label.adjustSize()
        self._btn_export.setEnabled(False)

    def receive_data(self, result: Any, data_resolver: Callable[[VfsNode], bytes] | None = None) -> None:
        '''Override for recieving the FISEditorPayload'''
        self._data_resolver = data_resolver
        self._original_payload = result
        if not isinstance(result, FisEditorPayload):
            self.show_error(
                f'Expected FisEditorPayload, got {type(result).__name__}. '
                f'Ensure FisHandler.prepare_editor_data returns FisEditorPayload.'
            )
            return
        self.img     = result.image.copy()
        self.info    = result.info
        self.raw_fis = result.raw_bytes
        self.set_dirty(False)
        self._populate_ui()

    def _populate_ui(self, data: Any = None) -> None:
        '''Populate the editor with the current self.img/info'''
        if isinstance(data, FisEditorPayload): # on discard restore original state
            self.img     = data.image.copy()
            self.info    = data.info
            self.raw_fis = data.raw_bytes
        if not self.img or not self.info or not self.current_node:
            return
        self.history.initialize(self.img)
        if self.img.format() == QImage.Format.Format_Indexed8:
            self._color_stack.setCurrentIndex(0)
            self._populate_palette()
        else:
            self._color_stack.setCurrentIndex(1)
            self._update_direct_color_button(self._image_label.selected_color)

        self._display_image(self.img)
        self._populate_info(self.info)
        self._btn_export.setEnabled(True)
        logger.debug(f'FIS: loaded {self.current_node.name} ({self.info.width}×{self.info.height} {self.info.psm_name})')

    def show_error(self, message: str) -> None:
        '''Show error in the canvas area'''
        self._image_label.setPixmap(QPixmap())
        self._image_label.setText(f'Error: {message}')
        self._image_label.adjustSize()
        self._btn_export.setEnabled(False)
        for lbl in self._info_rows.values():
            lbl.setText('—')
        super().show_error(message) # log the error

###------------------------------------ Lifecycle --------------------------------------------------###

    def current_data(self) -> Any:
        '''
        Return (QImage, raw_fis_bytes) for dispatcher -> handler.decode_editor_payload
        Called by snapshot() before saving state
        '''
        return (self.img.copy(), self.raw_fis) if self.img and self.raw_fis else self._original_payload

    def confirm_changes_applied(self) -> None:
        '''
        Called by the session after a successful save
        updates _original_payload so discard_changes reverts to the latest saved state
        '''
        if self._pending_data is not None and isinstance(self._pending_data, tuple):
            img, raw_fis = self._pending_data
            assert self.info
            self._original_payload = FisEditorPayload(
                image=img.copy(),
                info=self.info,
                raw_bytes=raw_fis
            )
            self._pending_data = None
        self.set_dirty(False)

    def cleanup(self) -> None:
        self.history.debounce_timer.stop()
        self.img      = None
        self.info     = None
        self.raw_fis  = None
        super().cleanup()

###------------------------------------- History ---------------------------------------------------###

    def _emit_undo_state(self) -> None:
        self.undo_state_changed.emit(
            bool(self.history.undo_stack),
            bool(self.history.redo_stack)
        )

    def undo(self) -> None:
        '''Undo action and sync states'''
        prev_img = self.history.undo()
        if prev_img and self.img:
            is_indexed = (self.img.format() == QImage.Format.Format_Indexed8)
            clut_changed = is_indexed and (self.img.colorTable() != prev_img.colorTable())
            self.img = prev_img
            self._apply_zoom()
            if clut_changed:
                self._update_palette_icons()
            self._update_dirty_state()

    def redo(self) -> None:
        next_img = self.history.redo()
        if next_img and self.img:
            is_indexed = (self.img.format() == QImage.Format.Format_Indexed8)
            clut_changed = is_indexed and (self.img.colorTable() != next_img.colorTable())
            self.img = next_img
            self._apply_zoom()
            if clut_changed:
                self._update_palette_icons()
            self._update_dirty_state()

###------------------------------------- Editing Actions --------------------------------------------###

    # def _rotate_texture(self, clockwise: bool = True) -> None:
    #     '''Flush pending debounce, add to history, and rotate the image'''
    #     if not self.img:
    #         return
    #     if self.history.debounce_timer.isActive():
    #         self.history.debounce_timer.stop()
    #         self.history._commit_state()
    #     self.history.undo_stack.append(self.img.copy())
    #     self.history.redo_stack.clear()
    #     self.history._emit_status()

    #     self.img = self._rotate_indexed(self.img, clockwise=clockwise)
    #     self.history._current_state = self.img.copy()

    #     self._apply_zoom()
    #     self._info_rows['Width'].setText(str(self.img.width()))
    #     self._info_rows['Height'].setText(str(self.img.height()))
    #     if self.current_node and self.info:
    #         self._title_label.setText(
    #             f'{self.current_node.name} — {self.img.width()}×{self.img.height()} {self.info.psm_name}'
    #         )
    #     self.set_dirty(True)

    # def _rotate_indexed(self, img: QImage, clockwise: bool) -> QImage:
    #     w, h = img.width(), img.height()
    #     new_img = QImage(h, w, QImage.Format.Format_Indexed8)
    #     new_img.setColorTable(img.colorTable())
    #     for y in range(h):
    #         for x in range(w):
    #             idx = img.pixelIndex(x, y)
    #             if clockwise:
    #                 new_img.setPixel(h - 1 - y, x, idx)
    #             else:
    #                 new_img.setPixel(y, w - 1 - x, idx)
    #     return new_img

    def _on_editing_started(self) -> None:
        if self.img:
            self.history.push_change(self.img)

    def _on_painted(self) -> None:
        if self.img:
            self.history.push_change(self.img)
            self._update_dirty_state()

    def _pick_direct_color(self) -> None:
        new_color = QColorDialog.getColor(
            initial=self._image_label.selected_color,
            parent=self,
            title='Select Direct Color',
            options=QColorDialog.ColorDialogOption.ShowAlphaChannel | QColorDialog.ColorDialogOption.DontUseNativeDialog
        )
        if new_color.isValid():
            self._image_label.selected_color = new_color
            self._update_direct_color_button(new_color)

    def _update_direct_color_button(self, color: QColor) -> None:
        pix = QPixmap(20, 20)
        pix.fill(color)
        self._btn_direct_color.setIcon(QIcon(pix))
        self._btn_direct_color.setText(f' {color.name().upper()} (A: {color.alpha()})')

    def _on_palette_context(self, pos: QPoint) -> None:
        item = self._palette_list.itemAt(pos)
        if not item or not self.img or self.img.format() != QImage.Format.Format_Indexed8:
            return
        idx           = self._palette_list.row(item)
        current_color = QColor.fromRgba(self.img.color(idx))
        new_color     = QColorDialog.getColor(
            initial=current_color,
            parent=self,
            title=f'Overwrite CLUT Color [{idx}]',
            options=QColorDialog.ColorDialogOption.ShowAlphaChannel | QColorDialog.ColorDialogOption.DontUseNativeDialog
        )
        if new_color.isValid() and new_color != current_color:
            self.history.push_change(self.img)
            self.img.setColor(idx, new_color.rgba())
            pixmap = QPixmap(20, 20)
            pixmap.fill(new_color)
            item.setIcon(QIcon(pixmap))
            item.setToolTip(f'Index: {idx} (Hex: {new_color.name()})')
            self._image_label.update_display()
            self._update_dirty_state()

    def _update_dirty_state(self) -> None:
        is_dirty = bool(self.history.undo_stack) or self.history.debounce_timer.isActive()
        self.set_dirty(is_dirty)

###------------------------------------ Display Helpers ---------------------------------------------###

    def _display_image(self, img: QImage) -> None:
        '''Show the decoded image, scaled to fit without distortion.'''
        available = self._scroll_area.size() - QSize(4, 4)
        if available.width() > 50 and available.height() > 50:
            if img.width() > available.width() or img.height() > available.height():
                self._zoom_factor = min(
                    available.width()  / img.width(),
                    available.height() / img.height(),
                )
            else:
                self._zoom_factor = 1.0
        else:
            self._zoom_factor = 1.0
        self._apply_zoom()

    def _populate_info(self, info: FISInfo) -> None:
        def fmt_hex(v: int | None) -> str:
            return '—' if v is None else hex(v)

        self._info_rows['Name'].setText(repr(info.name))
        self._info_rows['PSM'].setText(info.psm_name)
        self._info_rows['BPP'].setText(str(info.bpp))
        self._info_rows['Width'].setText(str(info.width))
        self._info_rows['Height'].setText(str(info.height))
        self._info_rows['Dim mode'].setText(info.dimension_mode)
        self._info_rows['Swizzled'].setText(str(info.swizzled))
        self._info_rows['Padded CLUT'].setText(str(info.padded_4bpp_clut))
        self._info_rows['Pal offset'].setText(fmt_hex(info.palette_offset))
        self._info_rows['Image offset'].setText(fmt_hex(info.image_offset))
        self._info_rows['Image size'].setText(fmt_hex(info.image_size))

    def _populate_palette(self) -> None:
        '''Extracts the Qimage color table (CLUT) stored from the handler'''
        self._palette_list.clear()
        if not self.img or self.img.format() != QImage.Format.Format_Indexed8:
            return
        colors = self.img.colorTable()
        for idx, rgb in enumerate(colors):
            color = QColor.fromRgba(rgb)
            pixmap = QPixmap(20, 20)
            pixmap.fill(color)

            item = QListWidgetItem()
            item.setIcon(QIcon(pixmap))
            item.setToolTip(f'Index: {idx} (Hex: {color.name()})')
            self._palette_list.addItem(item)
        if colors:
            self._palette_list.setCurrentRow(0)

    def _update_palette_icons(self) -> None:
        '''Updates color icons in place'''
        if not self.img or self.img.format() != QImage.Format.Format_Indexed8:
            return
        for idx, rgb in enumerate(self.img.colorTable()):
            item = self._palette_list.item(idx)
            if item:
                color  = QColor.fromRgba(rgb)
                pixmap = QPixmap(20, 20)
                pixmap.fill(color)
                item.setIcon(QIcon(pixmap))
                item.setToolTip(f'Index: {idx} (Hex: {color.name()})')

    def _apply_zoom(self) -> None:
        if self.img:
            self._image_label.image_ref = self.img
        self._image_label.zoom_factor = self._zoom_factor
        self._image_label.update_display()

    def _export_png(self) -> None:
        if not self.img or not self.current_node:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, 'Export PNG', f'{self.current_node.name}.png', 'PNG Images (*.png)'
        )
        if not path:
            return
        if not self.img.save(path, 'PNG'):
            logger.error(f'FIS: QImage.save() failed for {path}')
            return
        logger.info(f'FIS: exported to {Path(path).name}')

###--------------------------------------- Event Handlers ------------------------------------------------###

    def _on_color_selected(self, index: int) -> None:
        '''Passes the selected CLUT index to the drawing canvas'''
        self._image_label.selected_color_idx = index

    def _on_tool_changed(self, button_id: int) -> None:
        '''0 = Brush, 1 = Bucket'''
        self._image_label.current_tool = 'brush' if button_id == 0 else 'bucket'

    def _on_brush_size_changed(self, size: int) -> None:
        self._image_label.brush_size = size

    def eventFilter(self, source, event) -> bool:
        '''Intercept viewport and label events to handle middle-mouse panning'''
        if source in (self._scroll_area.viewport(), self._image_label):
            if event.type() == event.Type.MouseButtonPress: # Middle Mouse Event
                if event.button() == Qt.MouseButton.MiddleButton:
                    self._is_panning      = True
                    self._pan_start_pos   = event.globalPosition().toPoint()
                    self._pan_start_h_bar = self._scroll_area.horizontalScrollBar().value()
                    self._pan_start_v_bar = self._scroll_area.verticalScrollBar().value()
                    self._scroll_area.setCursor(Qt.CursorShape.ClosedHandCursor)
                    return True
            elif event.type() == event.Type.MouseMove: # Mouse Drag Event
                if self._is_panning:
                    delta = event.globalPosition().toPoint() - self._pan_start_pos
                    self._scroll_area.horizontalScrollBar().setValue(self._pan_start_h_bar - delta.x())
                    self._scroll_area.verticalScrollBar().setValue(self._pan_start_v_bar - delta.y())
                    return True
            elif event.type() == event.Type.MouseButtonRelease: # Release Middle Mouse Event
                if event.button() == Qt.MouseButton.MiddleButton and self._is_panning:
                    self._is_panning = False
                    self._scroll_area.unsetCursor()
                    return True
        return super().eventFilter(source, event)

    def wheelEvent(self, event) -> None:
        '''Ctrl+scroll to adjust image size'''
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.angleDelta().y() > 0:
                self._zoom_in()
            else:
                self._zoom_out()
            event.accept()
            return
        super().wheelEvent(event)

    def _zoom_in(self) -> None:
        self._zoom_factor = min(self._zoom_factor * self._zoom_step, self._max_zoom)
        self._apply_zoom()

    def _zoom_out(self) -> None:
        self._zoom_factor = max(self._zoom_factor / self._zoom_step, self._min_zoom)
        self._apply_zoom()
