from __future__ import annotations
from pathlib import Path
from PyQt6.QtGui import QFontDatabase
from utilities import get_resource_path
from ui.settings import AppSettings

import logging
logger = logging.getLogger(f'radiata.{__name__}')

class DarkTheme:
    BG_WINDOW         = '#202020'
    BG_SURFACE        = '#161616'
    BG_SELECTED       = '#242424'
    BG_HOVER          = '#1a1a1a'
    BORDER            = '#333333'
    TEXT              = '#dcddde'
    TEXT_MUTED        = '#999999'
    TEXT_SELECTED     = '#ffffff'
    ACCENT            = '#7f6df2'
    ACCENT_HOVER      = '#8875ff'
    INTERACTIVE       = '#2a2a2a'
    INTERACTIVE_HOVER = '#303030'
    ERROR             = '#3d0000'
    ERROR_HOVER       = '#470000'
    SCROLL_HOVER      = 'rgba(255, 255, 255, 0.2)'
    FONT_SANS         = '"Segoe UI", "Open Sans", sans-serif'
    FONT_MONO         = '"Courier New", monospace'
    BASE_FONT_WEIGHT  = 'normal'

class LightTheme:
    BG_WINDOW         = '#ffffff'
    BG_SURFACE        = '#f2f3f5'
    BG_SELECTED       = '#dcdcdc'
    BG_HOVER          = '#e6e6e6'
    BORDER            = '#dddddd'
    TEXT              = '#2e3338'
    TEXT_MUTED        = '#888888'
    TEXT_SELECTED     = '#1f2328'
    ACCENT            = '#705dcf'
    ACCENT_HOVER      = '#7a6ae6'
    INTERACTIVE       = '#cccccc'
    INTERACTIVE_HOVER = '#d9d9d9'
    ERROR             = '#990000'
    ERROR_HOVER       = '#bb0000'
    SCROLL_HOVER      = 'rgba(0, 0, 0, 0.2)'
    FONT_SANS         = '"Segoe UI", "Open Sans", sans-serif'
    FONT_MONO         = '"Courier New", monospace'
    BASE_FONT_WEIGHT  = 'normal'


class ThemeManager:
    BASE_FONT_SIZE     = 14
    current_font_size  = 14
    active_theme: type[DarkTheme] | type[LightTheme] = DarkTheme
    _raw_template      = None
    _app               = None

    THEMES: dict[str, type[DarkTheme] | type[LightTheme]] = {
        # 'Radiata': RadiataTheme,
        'Dark':    DarkTheme,
        'Light':   LightTheme
    }

    @classmethod
    def initialize(cls, app):
        '''Called once at startup'''
        cls._app = app
        cls.apply_theme('Dark')

    @classmethod
    def apply_theme(cls, theme_name: str = 'Dark', delta: int = 0) -> None:
        if not cls._app:
            logger.warning('No application initialized. Make sure that a window exists to apply a theme to.')
            return
        if theme_name in cls.THEMES:
            cls.active_theme = cls.THEMES[theme_name]

        cls.current_font_size = max(8, min(32, cls.BASE_FONT_SIZE + delta))
        fs = cls.current_font_size
        theme = cls.active_theme

        # Cache the raw string templates so we don't hit the disk constantly
        if cls._raw_template is None:
            qss_path = get_resource_path('ui/assets/static_sheet.qss')
            font_path = get_resource_path('ui/assets/dynamic_sheet.qss')

            base_qss = qss_path.read_text(encoding='utf-8') if qss_path.exists() else ''
            font_qss = font_path.read_text(encoding='utf-8') if font_path.exists() else ''
            cls._raw_template = base_qss + '\n' + font_qss

        # The mapping dictionary
        replacements = {
            '{BG_WINDOW}':         theme.BG_WINDOW,
            '{BG_SURFACE}':        theme.BG_SURFACE,
            '{BG_SELECTED}':       theme.BG_SELECTED,
            '{BG_HOVER}':          theme.BG_HOVER,
            '{BORDER}':            theme.BORDER,
            '{TEXT}':              theme.TEXT,
            '{TEXT_MUTED}':        theme.TEXT_MUTED,
            '{TEXT_SELECTED}':     theme.TEXT_SELECTED,
            '{ACCENT}':            theme.ACCENT,
            '{ACCENT_HOVER}':      theme.ACCENT_HOVER,
            '{INTERACTIVE}':       theme.INTERACTIVE,
            '{INTERACTIVE_HOVER}': theme.INTERACTIVE_HOVER,
            '{ERROR}':             theme.ERROR,
            '{ERROR_HOVER}':       theme.ERROR_HOVER,
            '{SCROLL_HOVER}':      theme.SCROLL_HOVER,
            '{FONT_SANS}':         theme.FONT_SANS,
            '{FONT_MONO}':         theme.FONT_MONO,
            '{BASE_FONT_WEIGHT}':  theme.BASE_FONT_WEIGHT,

            '{FONT_SIZE}':         f'{fs}px',
            '{FONT_SIZE_SMALL}':   f'{fs - 2}px',
            '{FONT_SIZE_LARGE}':   f'{fs + 5}px',
            '{BTN_PADDING}':       f'{max(1, fs // 4)}px {max(1, fs // 2)}px',
            '{BTN_PADDING_LARGE}': f'{fs}px {fs * 2}px',
            '{BTN_MIN_WIDTH}':     f'{fs * 2.5}px',
        }
        full_sheet = cls._raw_template
        for key, value in replacements.items():
            full_sheet = full_sheet.replace(key, value)

        cls._app.setStyleSheet(full_sheet)

        logger.debug(f'Stylesheet {theme_name} initialised. (Font Size:{fs})')

    @staticmethod
    def load_custom_font(font_path: Path):
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id == -1:
            logger.error(f'Failed to load font: {font_path}')
            return None
        return QFontDatabase.applicationFontFamilies(font_id)[0]
