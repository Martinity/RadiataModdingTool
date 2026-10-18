'''Global log signal for the application.
For more advanced debugging implementation should use {__name__}
Only signals messages to the workspace log console (or any build console where __name__ helps)'''
from __future__ import annotations

from PyQt6.QtWidgets import QPushButton, QPlainTextEdit
from PyQt6.QtGui import QTextCursor, QColor
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot, QTimer

import logging
import sys

class QtLogHandler(logging.Handler, QObject):
    '''Python logs routed to Qt signals'''
    log_signal = pyqtSignal(str, int) # format: message, level

    def __init__(self, parent: QObject | None = None):
        logging.Handler.__init__(self)
        QObject.__init__(self, parent)

    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        self.log_signal.emit(msg, record.levelno)


def setup_logging(level: int = logging.INFO) -> QtLogHandler:
    '''Called on application launch. Returns logger, log_signal'''
    logger = logging.getLogger('radiata')
    logger.setLevel(level)
    logger.propagate = False

    if sys.stderr is not None and hasattr(sys.stderr, 'fileno'):
        try:
            console = logging.StreamHandler(
                open(sys.stderr.fileno(), 'w', encoding='utf-8', errors='replace', closefd=False)
            )
            console.setFormatter(logging.Formatter('%(levelname)s: %(name)s - %(message)s'))
            logger.addHandler(console)
        except Exception:
            pass

    qt_handler = QtLogHandler()
    qt_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(qt_handler)

    return qt_handler

###---------------------------------------- UI ---------------------------------------------###

class LoggingWindow(QPlainTextEdit):
    '''Log viewer widget'''
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('TextSubtitle')
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        self.clear_button = QPushButton('Clear', self)
        self.clear_button.clicked.connect(self.clear)
        self.clear_button.show()
        self.update_button_position()

    def __repr__(self) -> str:
        doc = self.document()
        lines = doc.lineCount() if doc else 0
        return f"<LoggingWindow lines={lines}>"

    def __str__(self) -> str:
        return "Log Console"

    @pyqtSlot(str, int)
    def append_log(self, message: str, level: int):
        '''Slot gets logs from QtLogHandler'''
        scrollbar = self.verticalScrollBar()
        assert scrollbar is not None
        at_bottom = scrollbar.value() >= (scrollbar.maximum() - 10)

        from ui.theme_manager import ThemeManager
        theme = ThemeManager.active_theme
        colors = {
            logging.DEBUG:     theme.ACCENT,
            logging.INFO:      theme.TEXT,
            logging.WARNING:   "#ffaa33",
            logging.ERROR:     "#ff3333",
            logging.CRITICAL:  "#990000"
        }
        color = colors.get(level, theme.TEXT)

        cursor = self.textCursor()
        cursor.beginEditBlock()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        _format = cursor.charFormat()
        _format.setForeground(QColor(color))
        cursor.setCharFormat(_format)

        cursor.insertText(message + '\n')
        cursor.endEditBlock()

        if at_bottom:
            self.moveCursor(QTextCursor.MoveOperation.End)
            QTimer.singleShot(0, lambda: scrollbar.setValue(scrollbar.maximum()))

    def resizeEvent(self, event): # type: ignore
        super().resizeEvent(event)
        self.update_button_position()

    def update_button_position(self) -> None:
        padding = 4
        v_bar = self.verticalScrollBar() if self.verticalScrollBar() else 0
        scroll_width = v_bar.width() if v_bar else 0
        button_width = self.clear_button.width()
        x = self.width() - button_width - padding * 2 - scroll_width
        y = padding
        self.clear_button.move(x, y)
        self.clear_button.raise_()
