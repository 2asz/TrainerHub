"""主题系统：深色/浅色两套配色 + 样式表生成。

设计：
- 所有颜色集中在这里，UI 代码不再写死十六进制；
- 运行时切换主题：set_theme(name) 后，主窗口重设 QSS + 重绘即可；
- 对话框打开时按当前主题生成 QSS，因此已打开的旧对话框下次打开自动生效。
本模块不依赖 Qt（调色板只是字符串/元组），方便测试。
"""
from .config import config

# 主题中文名（设置页下拉用）
LABELS = {"dark": "深色", "light": "浅色"}

# 配色说明：
# - 十六进制字符串：QColor("#xxx") 直接用；
# - 元组：QColor(*tuple) 用（按钮色/阴影/占位渐变）。
PALETTES = {
    "dark": {
        # 基础
        "bg": "#0f1115", "sidebar": "#12151b",
        "card": "#191d25", "card_hover": "#20262f",
        "border": "#262d3a",
        "accent": "#3d8bfd", "accent_dark": "#2c6fd6",
        "text": "#e8ecf2", "text_dim": "#9aa3b2",
        "running": "#3ddc84",
        # 主窗口局部
        "toolbar": "#14171d", "status": "#14171d",
        "search_bg": "#1a1f28",
        "menu": "#1b1f27", "msgbox": "#171a21",
        "toolbtn_hover": "#232a35",
        "scrollbar": "#343c48", "scrollbar_hover": "#454f5e",
        "empty_btn": "#1f2630", "empty_btn_hover": "#2a3341",
        "empty_btn_pressed": "#1d242f",
        # 对话框
        "dialog_bg": "#14171d", "label": "#cfd5de",
        "input_bg": "#1a1f28", "input_border": "#262d3a",
        "list_bg": "#171a21", "list_item_hover": "#1f2630",
        "btn": "#222a35", "btn_hover": "#2b3543",
        "btn_pressed": "#1c232d", "btn_border": "#2e3745",
        "btn_disabled": "#1c2027", "btn_disabled_fg": "#6a7280",
        "primary_hover": "#5aa0ff",
        "groupbox": "#161a21", "progress_bg": "#1a1f28",
        "radio_fg": "#cfd5de",
        # 卡片绘制
        "shadow": (0, 0, 0, 70),
        "card_selected_bg": "#1f3048", "card_running_bg": "#14281e",
        "name_fg": "#ebeff6", "tag_fg": "#a0a8b2",
        "running_chip_bg": (16, 90, 44, 225), "running_chip_fg": (120, 240, 170),
        "btn_blue_n": (61, 139, 253), "btn_blue_h": (90, 160, 255),
        "btn_blue_p": (44, 111, 214),
        "btn_green_n": (31, 143, 77), "btn_green_h": (40, 172, 95),
        "btn_green_p": (23, 105, 59),
        "btn_gray_n": (52, 59, 70), "btn_gray_h": (61, 70, 84),
        "btn_gray_p": (38, 44, 54), "btn_gray_fg": (180, 188, 198),
        # 侧边栏
        "side_selected": "#1d2531", "side_hover": "#1b2029",
        "side_count": "#6b7686", "side_count_selected": "#7ea8f0",
        # 封面占位图（无封面时的渐变）
        "ph_top": (30, 34, 44), "ph_bottom": (36, 42, 58),
        "ph_text": (140, 150, 166),
    },
    "light": {
        # 基础
        "bg": "#f2f4f8", "sidebar": "#e8ebf1",
        "card": "#ffffff", "card_hover": "#eef1f6",
        "border": "#d4dae3",
        "accent": "#2f6fdb", "accent_dark": "#2456b3",
        "text": "#1c232e", "text_dim": "#667085",
        "running": "#1f9d55",
        # 主窗口局部
        "toolbar": "#e4e8ef", "status": "#e4e8ef",
        "search_bg": "#ffffff",
        "menu": "#ffffff", "msgbox": "#ffffff",
        "toolbtn_hover": "#dde3ec",
        "scrollbar": "#c3cad4", "scrollbar_hover": "#aab3c0",
        "empty_btn": "#ffffff", "empty_btn_hover": "#eef1f6",
        "empty_btn_pressed": "#e2e7ef",
        # 对话框
        "dialog_bg": "#f7f8fb", "label": "#3a4453",
        "input_bg": "#ffffff", "input_border": "#ccd3de",
        "list_bg": "#ffffff", "list_item_hover": "#edf1f7",
        "btn": "#e8ecf3", "btn_hover": "#dde4ee",
        "btn_pressed": "#d2dae6", "btn_border": "#c6cedb",
        "btn_disabled": "#eceff4", "btn_disabled_fg": "#9aa3b2",
        "primary_hover": "#4d86e6",
        "groupbox": "#f0f3f8", "progress_bg": "#e4e9f1",
        "radio_fg": "#3a4453",
        # 卡片绘制
        "shadow": (0, 0, 0, 26),
        "card_selected_bg": "#dce7fb", "card_running_bg": "#ddf2e6",
        "name_fg": "#1c232e", "tag_fg": "#5b6675",
        "running_chip_bg": (22, 120, 64, 235), "running_chip_fg": (226, 247, 235),
        "btn_blue_n": (47, 111, 219), "btn_blue_h": (77, 134, 230),
        "btn_blue_p": (36, 86, 179),
        "btn_green_n": (31, 157, 85), "btn_green_h": (42, 178, 100),
        "btn_green_p": (23, 122, 65),
        "btn_gray_n": (217, 223, 232), "btn_gray_h": (203, 212, 224),
        "btn_gray_p": (189, 200, 214), "btn_gray_fg": (69, 80, 95),
        # 侧边栏
        "side_selected": "#dce7f9", "side_hover": "#e7ecf4",
        "side_count": "#7d8795", "side_count_selected": "#2456b3",
        # 封面占位图
        "ph_top": (235, 238, 243), "ph_bottom": (216, 223, 232),
        "ph_text": (120, 130, 145),
    },
}

# 当前主题名（默认深色；设置里可切换并持久化到 config.json）
_current = "dark"


def theme_name() -> str:
    """当前主题名（"dark"/"light"）。"""
    return _current


def current() -> dict:
    """当前主题的调色板 dict。"""
    return PALETTES[_current]


def set_theme(name: str) -> None:
    """切换当前主题；非法名字忽略。"""
    global _current
    if name in PALETTES:
        _current = name


def load_from_config() -> str:
    """启动时按 config.json 恢复主题，返回主题名。"""
    set_theme(config.get("theme", "dark"))
    return _current


def dialog_qss() -> str:
    """对话框统一样式（_StyledDialog 用），按当前主题生成。"""
    t = current()
    return f"""
        QDialog {{ background: {t['dialog_bg']}; color: {t['text']}; font-size: 13px;
                  font-family: "Microsoft YaHei UI"; }}
        QLabel {{ color: {t['label']}; }}
        QLineEdit, QComboBox, QListWidget, QPlainTextEdit {{
            background: {t['input_bg']}; border: 1px solid {t['input_border']};
            border-radius: 6px; padding: 6px 9px;
            selection-background-color: {t['accent']}; }}
        QLineEdit:focus, QComboBox:focus {{ border: 1px solid {t['accent']}; }}
        QComboBox::drop-down {{ border: none; width: 22px; }}
        QComboBox QAbstractItemView {{ background: {t['menu']};
                                      border: 1px solid {t['input_border']};
                                      selection-background-color: {t['accent_dark']}; }}
        QListWidget {{ background: {t['list_bg']}; border: 1px solid {t['input_border']}; }}
        QListWidget::item {{ padding: 5px 8px; border-radius: 5px; color: {t['label']}; }}
        QListWidget::item:selected {{ background: {t['accent_dark']}; color: white; }}
        QListWidget::item:hover {{ background: {t['list_item_hover']}; }}
        QPushButton {{ background: {t['btn']}; border: 1px solid {t['btn_border']};
                      border-radius: 6px; padding: 6px 15px; color: {t['text']}; }}
        QPushButton:hover {{ background: {t['btn_hover']}; border-color: {t['accent']}; }}
        QPushButton:pressed {{ background: {t['btn_pressed']};
                              padding: 7px 15px 5px 15px; }}
        QPushButton:disabled {{ color: {t['btn_disabled_fg']};
                               background: {t['btn_disabled']}; }}
        QPushButton#primary {{ background: {t['accent']}; border: none; color: white;
                              font-weight: bold; }}
        QPushButton#primary:hover {{ background: {t['primary_hover']}; }}
        QPushButton#primary:pressed {{ background: {t['accent_dark']};
                                      padding: 7px 15px 5px 15px; }}
        QGroupBox {{ border: 1px solid {t['input_border']}; border-radius: 8px;
                    margin-top: 8px; padding-top: 10px; background: {t['groupbox']}; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px;
                           color: {t['text_dim']}; }}
        QProgressBar {{ background: {t['progress_bg']}; border: none; border-radius: 5px;
                       height: 14px; text-align: center; color: {t['text']}; }}
        QProgressBar::chunk {{ background: {t['accent']}; border-radius: 5px; }}
        QRadioButton, QCheckBox {{ color: {t['radio_fg']}; spacing: 6px; }}
        QScrollBar:vertical {{ background: transparent; width: 10px; }}
        QScrollBar::handle:vertical {{ background: {t['scrollbar']}; border-radius: 5px; }}
        QScrollBar::handle:vertical:hover {{ background: {t['scrollbar_hover']}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
        QMessageBox {{ background: {t['dialog_bg']}; }}
        QMessageBox QLabel {{ color: {t['text']}; }}
    """
