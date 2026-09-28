"""Qt 版设计令牌（布局度量与状态色）。

主题色与字体由 QFluentWidgets 主题统一供给（`setTheme` / `themeColor`），不在此重复定义；
本模块承载布局度量（8pt 栅格）和 QFluentWidgets 未提供的状态底色 / 文字色。
"""

from __future__ import annotations

# --- Spacing Scale ---
SPACE_XXS = 2
SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16

# --- 布局尺寸 ---
# 左栏：型号选择 + 分类列表；FluentWindow 导航栏另占约 48px。
SIDEBAR_WIDTH = 232
# 右侧详情：选中程序的属性与操作。
DETAIL_PANE_WIDTH = 300
SEARCH_MIN_WIDTH = 240
LOG_VIEW_SIZE = (560, 320)
# 设置页卡片宽度上限：宽窗口下保持左对齐，避免路径值被拉伸过长而难读。
SETTINGS_CARD_MAX_WIDTH = 760
SHARED_SOURCE_PICKER_MIN_WIDTH = 720
SHARED_SOURCE_PICKER_DEFAULT_SIZE = (760, 520)
SHARED_SOURCE_PICKER_ITEM_HEIGHT = 56

# --- DataGrid ---
GRID_BORDER_RADIUS = 8
# 列宽：类型 / 程序名称 / 版本（归属列自动伸展）
GRID_COL_WIDTHS = (130, 300, 110)
GRID_ROW_HEIGHT = 36
TAG_RADIUS = 4

# --- 侧栏 ---
NAV_ITEM_HEIGHT = 34
NAV_SECTION_HEIGHT = 30

# --- 状态色（light, dark）：(底色, 文字色)，对齐 Fluent 2 InfoBar 语义色 ---
STATUS_COLORS: dict[str, tuple[tuple[str, str], tuple[str, str]]] = {
    "info": (("#EBF3FB", "#005A9E"), ("#1F2A36", "#8AC7FF")),
    "success": (("#DFF6DD", "#0F7B0F"), ("#1F3320", "#6CCB5F")),
    "warning": (("#FFF4CE", "#9D5D00"), ("#3B3016", "#FCE100")),
    "error": (("#FDE7E9", "#C42B1C"), ("#3D2224", "#FF99A4")),
}
# 次要文字（计数、说明）：light / dark
MUTED_TEXT = ("#707070", "#9A9A9A")
# 左栏与详情面板的衬底：light / dark
PANE_BACKGROUND = ("rgba(0, 0, 0, 0.025)", "rgba(255, 255, 255, 0.03)")
PANE_BORDER = ("rgba(0, 0, 0, 0.08)", "rgba(255, 255, 255, 0.08)")
