'''浅色主题：调色板与全局样式表。

配色思路：中性冷灰底 + 白色卡片 + 翡翠绿强调色，弱化边框、放大圆角，
用留白和层级替代重描边，避免旧版「深绿侧栏 + 米绿底」的厚重感。
'''

from string import Template

PALETTE = {
    'bg': '#F5F6FA',
    'surface': '#FFFFFF',
    'surface2': '#FAFBFD',
    'row_alt': '#F6F7FB',
    'border': '#E7E9F1',
    'border_soft': '#F0F2F7',
    'text': '#141821',
    'text2': '#5C6675',
    'text3': '#98A1B2',
    'sidebar': '#FFFFFF',
    'sidebar_h': '#F5F6FA',
    'sidebar_brd': '#ECEEF4',
    'sidebar_text': '#141821',
    'sidebar_text2': '#5C6675',
    'sidebar_text3': '#98A1B2',
    'primary': '#0DA46E',
    'primary_h': '#0B9161',
    'primary_press': '#087A51',
    'primary_bg': '#E8F7F1',
    'primary_text': '#05774F',
    'blue': '#3B82F6',
    'green': '#0DA46E',
    'green_h': '#0B9161',
    'red': '#EF4444',
    'red_h': '#DC2626',
    'purple': '#7C3AED',
    'purple_bg': '#F5F1FF',
    'purple_text': '#6D28D9',
    'orange': '#F59E0B',
}

_QSS = Template(r'''
/* ===== 基础 ===== */
QMainWindow, QDialog { background: $bg; }
QWidget {
    color: $text;
    font-family: 'Segoe UI Variable Text', 'Segoe UI', 'Microsoft YaHei UI', 'Microsoft YaHei', 'PingFang SC', sans-serif;
    font-size: 13px;
}
QLabel { background: transparent; }
QToolTip {
    background: #141821; color: #FFFFFF;
    border: none; border-radius: 8px; padding: 6px 10px; font-size: 12px;
}

/* ===== 左侧导航 ===== */
#sidebar { background: $sidebar; border-right: 1px solid $sidebar_brd; }
#brandLogo { background: transparent; border: none; }
#brandName { font-size: 15px; font-weight: 700; color: $text; }
#brandSub { font-size: 11px; color: $text3; }
#navSection { font-size: 10px; font-weight: 700; color: #AEB6C4; letter-spacing: 0.3px; padding: 12px 0 2px 12px; }
#navBtn {
    background: transparent; border: none; border-radius: 10px;
    color: $sidebar_text2; font-size: 13px; font-weight: 600;
    text-align: left; padding: 0 12px;
}
#navBtn:hover { background: $sidebar_h; color: $text; }
#navBtn:checked { background: $primary_bg; color: $primary_text; }
#settingsBtn {
    background: $row_alt; border: 1px solid $border_soft; border-radius: 10px;
    color: $sidebar_text2; font-size: 12.5px; font-weight: 600;
    text-align: left; padding: 0 12px;
}
#settingsBtn:hover { background: $sidebar_h; border-color: #DCE0EA; color: $text; }
#sidebarFooter { color: #AEB6C4; font-size: 10.5px; padding-left: 2px; }

/* ===== 顶栏 ===== */
#topBar { background: $surface; border-bottom: 1px solid $border_soft; }
#pageTitle { font-size: 19px; font-weight: 700; color: $text; }
#pageSub { font-size: 11.5px; color: $text3; }
#urlInput {
    background: $row_alt; border: 1px solid $border; border-radius: 12px;
    padding: 10px 14px; color: $text; font-size: 13px;
    selection-background-color: $primary_bg; selection-color: $primary_text;
}
#urlInput:focus { background: $surface; border: 1px solid $primary; }
#btnScrape {
    background: $primary; color: #FFFFFF; border: none; border-radius: 12px;
    font-size: 13.5px; font-weight: 700; padding: 0 18px;
}
#btnScrape:hover { background: $primary_h; }
#btnScrape:pressed { background: $primary_press; }
#btnScrape:disabled { background: #EDF0F5; color: #B3BBC9; }
#btnStop {
    background: $surface; color: $text2; border: 1px solid $border;
    border-radius: 12px; font-size: 13px; font-weight: 600; padding: 0 14px;
}
#btnStop:hover { background: #FEF3F3; border-color: #F6C7C7; color: $red_h; }
#btnStop:disabled { background: $surface2; color: #C2C9D6; border-color: $border_soft; }
#progressBar { background: #EDF0F5; border: none; border-radius: 2px; }
#progressBar::chunk { background: $primary; border-radius: 2px; }

/* ===== 主区域 ===== */
#bodyArea, #mainCol { background: $bg; }
#contentArea { background: $surface; border: 1px solid $border; border-radius: 18px; }
#viewTitle { font-size: 17px; font-weight: 700; color: $text; }
#viewHint { font-size: 11.5px; color: $text3; padding-top: 2px; }
#commentCount { font-size: 11.5px; color: $text3; padding: 2px 4px 0 4px; }
#commentDate {
    background: $row_alt; border: 1px solid $border; border-radius: 9px;
    padding: 4px 10px; color: $text2; font-size: 12px; font-weight: 600;
}
#commentDate:hover { background: $surface; border-color: $primary; color: $text; }
#commentDate:disabled { color: $text3; }
#commentDate::menu-indicator { image: none; width: 0; }
QWebEngineView { background: $surface; }

/* ===== 右侧面板 ===== */
#ctxScroll { background: transparent; border: none; }
#ctxScroll > QWidget > QWidget { background: transparent; }
#ctxPanel { background: transparent; }
#panelCard { background: $surface; border: 1px solid $border; border-radius: 16px; }
#cardTitle { font-size: 10.5px; font-weight: 700; color: #8A93A5; letter-spacing: 0.4px; }
#cardSub { font-size: 11px; color: $text3; }
#ctxTitle { font-size: 15.5px; font-weight: 700; color: $text; }
#searchInfo { color: $text3; font-size: 11px; }

/* ===== 面板内按钮 ===== */
#btnAction {
    background: $surface; border: 1px solid $border; border-radius: 11px;
    color: #33404F; font-size: 12.5px; font-weight: 600;
    text-align: left; padding: 7px 12px;
}
#btnAction:hover { background: $row_alt; border-color: #DCE0EA; color: $text; }
#btnAction:pressed { background: #EDF0F5; }
#btnAction:disabled { background: $surface2; border-color: $border_soft; color: #C2CAD7; }
#btnAIPrimary {
    background: $purple_bg; border: 1px solid #E6DEFF; border-radius: 11px;
    color: $purple_text; font-size: 12.5px; font-weight: 700;
    text-align: left; padding: 7px 12px;
}
#btnAIPrimary:hover { background: #EFE9FF; border-color: #D9CDFF; }
#btnAIPrimary:pressed { background: #E7DFFF; }
#btnAIPrimary:disabled { background: $surface2; border-color: $border_soft; color: #C4B8E8; }
#btnMini {
    background: $surface; border: 1px solid $border; border-radius: 10px;
    color: #33404F; font-size: 12px; font-weight: 600; padding: 7px 6px;
}
#btnMini:hover { background: $primary_bg; border-color: #BFE9D6; color: $primary_text; }
#btnMini:disabled { background: $surface2; border-color: $border_soft; color: #C2C9D6; }
#btnDanger {
    background: $red; color: #FFFFFF; border: 1px solid $red; border-radius: 9px;
    font-size: 12.5px; font-weight: 600; padding: 7px 16px;
}
#btnDanger:hover { background: $red_h; border-color: $red_h; }
#btnDanger:disabled { background: #F4F5F9; color: #C2C9D6; border-color: $border_soft; }
#btnGhost { background: $surface; border: 1px solid $border; border-radius: 9px; }
#btnGhost:hover { background: $row_alt; border-color: #DCE0EA; }
#btnGhost:disabled { background: $surface2; border-color: $border_soft; }
#searchInput {
    background: $row_alt; border: 1px solid $border; border-radius: 10px;
    padding: 7px 11px; color: $text; font-size: 12.5px;
}
#searchInput:focus { background: $surface; border-color: $primary; }

/* ===== 通用控件 ===== */
QPushButton {
    background: $surface; border: 1px solid $border; border-radius: 9px;
    padding: 7px 14px; color: $text2; font-size: 12.5px; font-weight: 600;
}
QPushButton:hover { background: $row_alt; border-color: #DCE0EA; color: $text; }
QPushButton:pressed { background: #EDF0F5; }
QPushButton:disabled { background: $surface2; color: #C2C9D6; border-color: $border_soft; }
QPushButton:default { background: $primary; border: 1px solid $primary; color: #FFFFFF; }
QPushButton:default:hover { background: $primary_h; border-color: $primary_h; }
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: $surface; border: 1px solid $border; border-radius: 9px;
    padding: 6px 10px; color: $text;
    selection-background-color: $primary_bg; selection-color: $primary_text;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus {
    border-color: $primary;
}
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: $surface; border: 1px solid $border; border-radius: 10px;
    padding: 4px; selection-background-color: $primary_bg; selection-color: $primary_text;
}
QCheckBox, QRadioButton { color: $text2; font-size: 12.5px; spacing: 8px; }
QCheckBox::indicator, QRadioButton::indicator {
    width: 16px; height: 16px; border: 1px solid #CFD5E2; border-radius: 5px; background: $surface;
}
QRadioButton::indicator { border-radius: 8px; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: $primary; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked { background: $primary; border-color: $primary; }

/* ===== 滚动条 ===== */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #D9DEE9; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #C3CAD8; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #D9DEE9; border-radius: 4px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: #C3CAD8; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

/* ===== 状态栏 ===== */
#statusBar { background: $surface; border-top: 1px solid $border_soft; color: $text3; font-size: 11px; }
#statusBar::item { border: none; }
#statusHint { color: #AEB6C4; font-size: 11px; padding-right: 12px; }

/* ===== 菜单 ===== */
QMenuBar { background: transparent; color: $text2; }
QMenuBar::item { padding: 5px 12px; border-radius: 7px; background: transparent; }
QMenuBar::item:selected { background: $row_alt; color: $text; }
QMenu { background: $surface; border: 1px solid $border; border-radius: 12px; padding: 6px; }
QMenu::item { padding: 7px 28px 7px 14px; border-radius: 8px; color: $text2; }
QMenu::item:selected { background: $primary_bg; color: $primary_text; }
QMenu::separator { height: 1px; background: $border_soft; margin: 6px 10px; }

/* ===== 表格 / 其它 ===== */
QTableWidget {
    background: $surface; border: 1px solid $border; border-radius: 12px;
    gridline-color: $border_soft; alternate-background-color: $surface2;
    selection-background-color: $primary_bg; selection-color: $primary_text;
}
QHeaderView::section {
    background: $row_alt; color: #7A8494; border: none;
    border-bottom: 1px solid $border; padding: 8px; font-size: 11px; font-weight: 700;
}
QTableWidget::item { padding: 6px; }
QTableCornerButton::section { background: $row_alt; border: none; }
QMessageBox { background: $surface; }
QMessageBox QLabel { color: $text; font-size: 13px; }
QProgressBar {
    background: #EDF0F5; border: none; border-radius: 4px;
    text-align: center; color: $text2; font-size: 11px;
}
QProgressBar::chunk { background: $primary; border-radius: 4px; }
QSplitter::handle { background: $border_soft; }
''')


def stylesheet():
    '''返回渲染好的全局 QSS。'''

    return _QSS.substitute(PALETTE)



_CONTENT_CSS = Template(r'''
    * { margin: 0; padding: 0; box-sizing: border-box; }
    html, body { min-height: 100%; }
    body {
        font-family: 'Segoe UI Variable Text', 'Segoe UI', 'Microsoft YaHei UI', '微软雅黑', 'PingFang SC', sans-serif;
        font-size: 15px; line-height: 1.95; color: #1B2130;
        background-color: $surface;
        word-wrap: break-word; overflow-wrap: break-word;
        -webkit-font-smoothing: antialiased;
    }
    .reader { max-width: 780px; margin: 0 auto; padding: 36px 40px 64px; }
    .reader > :first-child { margin-top: 0 !important; }
    .reader > :last-child { margin-bottom: 0 !important; }
    .welcome { padding: 96px 20px 40px; text-align: center; }
    .welcome-mark {
        display: inline-block; background: $primary_bg; color: $primary_text;
        border: 1px solid #CDEEDF; border-radius: 999px;
        padding: 5px 13px; font-size: 11px; font-weight: 700;
    }
    .welcome h1 {
        font-size: 26px; font-weight: 700; color: #0F1420;
        margin: 18px 0 12px; letter-spacing: -0.01em;
    }
    .welcome p {
        max-width: 480px; margin: 0 auto; color: $text2;
        font-size: 13.5px; line-height: 1.9;
    }
    .welcome-tips {
        display: flex; justify-content: center; gap: 8px;
        margin-top: 26px; flex-wrap: wrap;
    }
    .welcome-tips span {
        background: $surface; border: 1px solid $border;
        border-radius: 999px; padding: 6px 13px;
        color: $text2; font-size: 11.5px;
        box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
    }
    p { margin: 0 0 1em; }
    a {
        color: $primary_text; text-decoration: none;
        border-bottom: 1px solid rgba(13, 164, 110, 0.28);
    }
    a:hover { color: $primary; border-bottom-color: $primary; }
    a:visited { color: $primary_text; }
    strong, b { font-weight: 700; color: #0F1420; }
    img {
        max-width: 100%; max-height: 620px; height: auto; width: auto;
        border-radius: 12px; margin: 16px auto; display: block;
        cursor: zoom-in; object-fit: contain; background: $row_alt;
        box-shadow: 0 6px 26px rgba(16, 24, 40, 0.08);
    }
    .ocr-block {
        max-width: 100%; margin: -4px auto 18px; padding: 10px 13px;
        background: $row_alt; border: 1px dashed #E1E5EE; border-radius: 12px;
    }
    .ocr-label {
        display: block; font-size: 9px; font-weight: 800;
        color: $primary_text; margin-bottom: 4px;
    }
    .ocr-text { color: #5C6675; font-size: 12.5px; line-height: 1.7; }
    blockquote {
        border-left: 3px solid #C9EBDC; margin: 1.2em 0;
        padding: 14px 18px; color: #4B5563;
        background: $row_alt; border-radius: 0 12px 12px 0;
    }
    blockquote p:last-child { margin-bottom: 0; }
    pre, code {
        font-family: 'Cascadia Code', 'Consolas', 'Courier New', monospace;
    }
    code {
        background: #F1F3F8; color: #0F766E;
        border: 1px solid #E4E7F0; border-radius: 6px;
        padding: 2px 6px; font-size: 0.86em;
    }
    pre {
        background: #101623; color: #E6EAF2;
        border-radius: 12px; padding: 18px 20px;
        overflow-x: auto; margin: 1.2em 0; line-height: 1.7;
    }
    pre code {
        background: none; border: none; color: inherit;
        padding: 0; font-size: 0.9em;
    }
    table {
        border-collapse: separate; border-spacing: 0; width: 100%;
        margin: 1.2em 0; border: 1px solid $border;
        border-radius: 10px; overflow: hidden;
    }
    th, td {
        border-bottom: 1px solid $border_soft;
        padding: 9px 13px; text-align: left;
    }
    th { background: $row_alt; font-weight: 700; color: $text2; }
    tr:last-child td { border-bottom: none; }
    ul, ol { padding-left: 1.55em; margin: 0.85em 0; }
    li { margin: 0.32em 0; }
    h1, h2, h3, h4, h5, h6 {
        color: #0F1420; font-weight: 700; line-height: 1.45;
    }
    h1 { font-size: 1.55em; margin: 1.2em 0 0.55em; }
    h2 {
        font-size: 1.3em; margin: 1.35em 0 0.55em;
        padding-bottom: 0.35em; border-bottom: 1px solid $border_soft;
    }
    h3 { font-size: 1.12em; margin: 1.2em 0 0.45em; }
    h4, h5, h6 { font-size: 1em; margin: 1em 0 0.4em; }
    hr { border: none; border-top: 1px solid $border_soft; margin: 1.9em 0; }
    mark.dbp-hl {
        background: #FFF1B8; color: #4A3B00;
        border-radius: 3px; padding: 0 2px;
    }
    ::selection { background: rgba(13, 164, 110, 0.18); }
    ::-webkit-scrollbar { width: 10px; height: 10px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb {
        background: #D9DEE9; border-radius: 5px;
        border: 2px solid $surface;
    }
    ::-webkit-scrollbar-thumb:hover { background: #C3CAD8; }
''')

_COMMENTS_CSS = Template(r'''
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
        font-family: 'Segoe UI Variable Text', 'Segoe UI', 'Microsoft YaHei UI', '微软雅黑', 'PingFang SC', sans-serif;
        font-size: 13.5px; line-height: 1.75; color: #1B2130;
        background-color: $surface; padding: 12px 16px 36px;
        word-wrap: break-word; overflow-wrap: break-word;
        -webkit-font-smoothing: antialiased;
    }
    a {
        color: $primary_text; text-decoration: none;
        border-bottom: 1px solid rgba(13, 164, 110, 0.28);
    }
    a:hover { border-bottom-color: $primary; }
    a:visited { color: $primary_text; }
    img {
        max-width: 100%; height: auto; border-radius: 10px;
        margin: 10px 0; display: block; cursor: zoom-in;
    }
    .comment-count { font-size: 11.5px; color: $text3; padding: 2px 4px 12px; }
    .empty {
        display: flex; flex-direction: column; align-items: center;
        justify-content: center; min-height: 360px; padding: 80px 20px;
        text-align: center;
    }
    .empty-mark {
        display: inline-block; background: $primary_bg; color: $primary_text;
        border: 1px solid #CDEEDF; border-radius: 999px;
        padding: 4px 12px; font-size: 10px; font-weight: 700;
        margin-bottom: 12px;
    }
    .empty-title { color: #0F1420; font-size: 15px; font-weight: 700; }
    .empty-sub { color: $text3; font-size: 12px; margin-top: 6px; }
    .comments { display: flex; flex-direction: column; gap: 12px; }
    .comment-card {
        display: flex; gap: 13px; background: $surface;
        border: 1px solid $border; border-radius: 14px;
        padding: 15px 17px;
        transition: border-color 0.16s ease, box-shadow 0.16s ease;
    }
    .comment-card:hover {
        border-color: #DCE1EC;
        box-shadow: 0 4px 16px rgba(16, 24, 40, 0.05);
    }
    .comment-card.reply-card {
        border-left: 3px solid #D9CBFF;
        background: #FCFBFF;
    }
    .avatar {
        width: 36px; height: 36px; border-radius: 11px;
        display: flex; align-items: center; justify-content: center;
        flex: 0 0 36px; font-size: 13.5px; font-weight: 800;
        user-select: none;
    }
    .c-main { flex: 1; min-width: 0; }
    .comment-header {
        display: flex; align-items: center; gap: 8px;
        flex-wrap: wrap; margin-bottom: 6px;
    }
    .author { font-size: 13.5px; font-weight: 700; color: #0F1420; }
    .floor {
        font-size: 10.5px; color: $text3; font-weight: 600;
        background: $row_alt; border-radius: 999px; padding: 1px 7px;
    }
    .meta-right {
        margin-left: auto; display: flex; align-items: center;
        gap: 8px; color: $text3; font-size: 10.5px;
    }
    .ip {
        background: $row_alt; border-radius: 6px;
        padding: 1px 7px; white-space: nowrap;
    }
    .time { white-space: nowrap; }
    .reply-badge {
        display: inline-block; max-width: 100%; overflow: hidden;
        text-overflow: ellipsis; white-space: nowrap;
        background: #F3EEFF; color: $purple_text;
        border-radius: 7px; padding: 2px 8px;
        font-size: 10.5px; line-height: 1.5;
    }
    .reply-quote {
        background: #F8F6FF; border-left: 2px solid #C4B5FD;
        border-radius: 10px; padding: 9px 12px; margin-bottom: 8px;
    }
    .reply-quote-who { font-size: 11.5px; font-weight: 700; color: $purple_text; }
    .reply-quote-text {
        color: $text2; font-size: 12.5px; line-height: 1.7; margin-top: 3px;
        display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
        overflow: hidden;
    }
    .comment-body {
        color: #333A48; font-size: 13.5px; line-height: 1.8;
    }
    .comment-body p { margin: 0 0 0.55em; }
    .comment-body p:last-child { margin-bottom: 0; }
    .comment-body img { margin: 10px 0; }
    .muted { color: $text3; }
    .ocr-group {
        display: flex; flex-direction: column; gap: 6px; margin-top: 9px;
    }
    .ocr-block {
        background: $row_alt; border: 1px dashed #E1E5EE;
        border-radius: 10px; padding: 9px 11px;
    }
    .ocr-label {
        display: block; font-size: 9px; font-weight: 800;
        color: $primary_text; margin-bottom: 3px;
    }
    .ocr-text { color: #5C6675; font-size: 12px; line-height: 1.65; }
    .ocr-empty .ocr-text { color: $text3; }
    mark.dbp-hl {
        background: #FFF1B8; color: #4A3B00;
        border-radius: 3px; padding: 0 2px;
    }
    ::selection { background: rgba(13, 164, 110, 0.18); }
    ::-webkit-scrollbar { width: 10px; height: 10px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb {
        background: #D9DEE9; border-radius: 5px;
        border: 2px solid $surface;
    }
    ::-webkit-scrollbar-thumb:hover { background: #C3CAD8; }
''')

_GALLERY_CSS = Template(r'''
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
        font-family: 'Segoe UI Variable Text', 'Segoe UI', 'Microsoft YaHei UI', '微软雅黑', sans-serif;
        background-color: $surface; padding: 16px 16px 32px;
    }
    .grid {
        column-width: 210px; column-gap: 14px;
    }
    .cell {
        position: relative; break-inside: avoid; margin: 0 0 14px;
        border-radius: 14px; overflow: hidden;
        background: $row_alt; border: 1px solid $border;
        transition: box-shadow .18s ease, transform .18s ease, border-color .18s ease;
    }
    .cell:hover {
        box-shadow: 0 10px 28px rgba(16, 24, 40, 0.10);
        border-color: #DCE1EC;
        transform: translateY(-3px);
    }
    .cell img {
        width: 100%; height: auto; display: block;
        cursor: zoom-in; transition: transform .28s ease;
    }
    .cell:hover img { transform: scale(1.04); }
    .cell::after {
        content: '点击查看'; position: absolute; right: 10px; bottom: 10px;
        background: rgba(16, 22, 35, 0.72); color: #FFFFFF;
        font-size: 10px; line-height: 1; padding: 5px 9px;
        border-radius: 7px; opacity: 0; transition: opacity .18s ease;
        pointer-events: none;
    }
    .cell:hover::after { opacity: 1; }
    .empty {
        display: flex; flex-direction: column; align-items: center;
        justify-content: center; padding: 40px 20px;
        text-align: center;
        /* 空状态直接挂在 body 下（不放进 .grid），用视口高度撑满并居中；
           注意 .grid 是多栏瀑布流，把 display:flex 的空状态放进去会被
           Chrome 按栏拆散（标签/标题分散到不同列），不能依赖 grid-column。 */
        min-height: max(360px, calc(100vh - 48px));
    }
    .empty-mark {
        display: inline-block; background: $primary_bg; color: $primary_text;
        border: 1px solid #CDEEDF; border-radius: 999px;
        padding: 4px 12px; font-size: 10px; font-weight: 700;
        margin-bottom: 12px;
    }
    .empty-title { color: #0F1420; font-size: 15px; font-weight: 700; }
    .empty-sub { color: $text3; font-size: 12px; margin-top: 6px; }
    ::-webkit-scrollbar { width: 10px; height: 10px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb {
        background: #D9DEE9; border-radius: 5px;
        border: 2px solid $surface;
    }
    ::-webkit-scrollbar-thumb:hover { background: #C3CAD8; }
''')


def content_css():
    return _CONTENT_CSS.substitute(PALETTE)


def comments_css():
    return _COMMENTS_CSS.substitute(PALETTE)


def gallery_css():
    return _GALLERY_CSS.substitute(PALETTE)
