"""Shared drawing values for the console charts.

These are plain constants, deliberately not a theme manager and not an
application-wide stylesheet: the table and dialog styling elsewhere is
untouched. The palette is the approved chart-set A palette.
"""

from __future__ import annotations

from PyQt6.QtGui import QColor

READ_COLOR: QColor = QColor("#397fd1")
WRITE_COLOR: QColor = QColor("#e47e38")
TEXT_COLOR: QColor = QColor("#25303d")
MUTED_COLOR: QColor = QColor("#63758a")
GRID_COLOR: QColor = QColor("#d5deea")
TRACK_COLOR: QColor = QColor("#e6ecf3")
BORDER_COLOR: QColor = QColor("#cbd7e5")
AXIS_PEN_WIDTH: float = 1.0
BAR_HEIGHT: int = 6
PLOT_MARGIN: int = 8
GUTTER_WIDTH: int = 144

__all__ = [
    "AXIS_PEN_WIDTH",
    "BAR_HEIGHT",
    "BORDER_COLOR",
    "GRID_COLOR",
    "GUTTER_WIDTH",
    "MUTED_COLOR",
    "PLOT_MARGIN",
    "READ_COLOR",
    "TEXT_COLOR",
    "TRACK_COLOR",
    "WRITE_COLOR",
]
