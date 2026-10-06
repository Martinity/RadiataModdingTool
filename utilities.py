'''
Common functions and classes; Resource fetching, string formatting, UI utilities.
'''
from __future__ import annotations

import sys
from pathlib import Path
from PyQt6.QtWidgets import (
    QFrame, QWidget, QVBoxLayout, QLabel, QDialog, QRadioButton, QDialogButtonBox,
    QProgressBar, QSizePolicy
)
from PyQt6.QtGui import QIcon, QPixmap, QPainter
from PyQt6.QtCore import Qt, QTimer, QSize
from PyQt6.QtSvg import QSvgRenderer

from core.dispatcher import ConflictChoice


def get_resource_path(relative_path: str | Path) -> Path:
    '''
    Used to get the path to an asset, either from the frozen build or from source.
    '''
    relative_path = str(relative_path).lstrip('/\\')
    if hasattr(sys, '_MEIPASS'): # For frozen builds
        base_path = Path(sys._MEIPASS)
    else: # For running from source
        base_path = Path(__file__).resolve().parent
    return base_path / relative_path


def human_size(n: int) -> str:
    '''Converts bytes into a human-readable string'''
    if n < 0:
        return 'Invalid Size'
    if n == 0:
        return '0 B'
    value = float(n)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024:
            return f'{value:.1f} {unit}' if unit != 'B' else f'{value} B'
        value /= 1024
    return f'{value:.1f} TB'


def human_time(dur: float) -> str:
    if (mins := dur // 60):
        secs = dur % 60
        dur_str = f'{int(mins)}m {secs:.2f}s'
    else:
        dur_str = f'{dur:.2f}s'
    return dur_str


def hline() -> QFrame:
    f = QFrame()
    f.setObjectName('HLine')
    f.setFrameShape(QFrame.Shape.HLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f

def vline() -> QFrame:
    f = QFrame()
    f.setObjectName('VLine')
    f.setFrameShape(QFrame.Shape.HLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f


class ToastProgressBar(QWidget):
    '''
    Toast-style progress bar.
    '''
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 30, 0)

        self.frame = QFrame()
        self.frame.setObjectName('Popup')

        frame_layout = QVBoxLayout(self.frame)
        self.label = QLabel("")
        self.label.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(True)

        frame_layout.addWidget(self.label)
        frame_layout.addWidget(self.progress)
        layout.addWidget(self.frame)

        self.hide_timer = QTimer()
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide)
        self.hide()

    def show_progress(self, value: int, title: str) -> None:
        self.progress.setValue(value)
        self.label.setText(title)
        if not self.isVisible():
            self.raise_()
            self._reposition()
            self.show()
        if value >= 100:
            self.hide_timer.start(2500)
        else:
            self.hide_timer.stop()

    def _reposition(self) -> None:
        parent = self.parent()
        if not isinstance(parent, QWidget):
            return
        self.adjustSize()
        parent_rect = parent.rect()
        padding_x = 30
        padding_y = 40
        x = parent_rect.width() - self.width() - padding_x
        y = parent_rect.height() - self.height() - padding_y
        self.setGeometry(x, y, self.width(), self.height())


PLAY_SVG = b"""
<svg xmlns="http://www.w3.org/2000/svg"
     width="24" height="24" viewBox="0 0 24 24">
    <path fill="#000000" d="M7 4v16l13-8L7 4z"/>
</svg>
"""

PAUSE_SVG = b"""
<svg xmlns="http://www.w3.org/2000/svg"
     width="24" height="24" viewBox="0 0 24 24">
    <path fill="#000000" d="M6 4h5v16H6V4zm8 0h5v16h-5V4z"/>
</svg>
"""

def play_svg(color: str, size: int) -> bytes:
    return PLAY_SVG.replace(b'#000000', color.encode()).replace(b'24', str(size).encode())

def pause_svg(color: str, size: int) -> bytes:
    return PAUSE_SVG.replace(b'#000000', color.encode()).replace(b'24', str(size).encode())

def svg_to_icon(mode: str, size=24) -> QIcon:
    '''Return a QIcon for Play/Pause QPushButton's, ensures proper cross-platform rendering.
    Button color is not the active theme but the theme at the time of rendering.'''
    from ui.theme_manager import ThemeManager
    c = ThemeManager.active_theme.TEXT
    svg = play_svg(c, size) if mode == 'play' else pause_svg(c, size) if mode == 'pause' else None
    if not svg: raise (ValueError(f'Invalid mode: {mode}, must be "play" or "pause"'))
    renderer = QSvgRenderer(svg)
    icon = QIcon()
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    with QPainter(pixmap) as painter:
        renderer.render(painter)
    return QIcon(pixmap)

###--------------------------------------- Conflict Resolution -----------------------------------###

class ConflictResolverDialog(QDialog):
    '''
    I built the system for a three tiered resolution but the more I think about it
    the more it seems like deferring the choice is not possible as simply having
    conflicting data in the vfs corrupts data.
    '''
    from core.node import VfsNode
    def __init__(self, new_node: VfsNode, old_nodes: str, reason: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Conflict Detected')
        self.setModal(True)

        layout = QVBoxLayout(self)
        info_text = QLabel(f'{reason}')
        info_text.setWordWrap(True)
        info_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(info_text)

        self.radio_keep_old   = QRadioButton(f'1. Keep pending modifications for {old_nodes}.')
        self.radio_keep_new   = QRadioButton(f'2. Keep pending modifications for {new_node}.')
        # self.radio_keep_both  = QRadioButton('3. Keep both, defer to staging time.')
        self.radio_keep_new.setChecked(True)

        layout.addWidget(self.radio_keep_old)
        layout.addWidget(self.radio_keep_new)
        # layout.addWidget(self.radio_keep_both)

        ok_btn = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        ok_btn.accepted.connect(self.accept)
        layout.addWidget(ok_btn)

    def selected_choice(self) -> ConflictChoice:
        if self.radio_keep_old.isChecked():
            return ConflictChoice.KEEP_OLD
        elif self.radio_keep_new.isChecked():
            return ConflictChoice.KEEP_NEW
        return ConflictChoice.KEEP_BOTH
