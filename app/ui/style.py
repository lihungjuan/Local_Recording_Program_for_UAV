APP_QSS = r"""
* {
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 13px;
    color: #dce6ef;
}
QMainWindow, QDialog {
    background: #080c11;
}
QWidget#Root {
    background: qradialgradient(cx:0.15, cy:0.0, radius:1.2, fx:0.15, fy:0.0,
        stop:0 #10202a, stop:0.45 #0b1118, stop:1 #080c11);
}
QFrame#Panel {
    background: rgba(15, 22, 30, 0.92);
    border: 1px solid #1c2a36;
    border-radius: 14px;
}
QFrame#PanelStrong {
    background: rgba(18, 27, 37, 0.96);
    border: 1px solid #24384a;
    border-radius: 14px;
}
QFrame#Divider {
    background: #1c2a36;
    max-height: 1px;
    min-height: 1px;
}
QFrame#MiniCard {
    background: #0c141b;
    border: 1px solid #1a2833;
    border-radius: 10px;
}
QLabel#Title {
    font-size: 22px;
    font-weight: 700;
    color: #f2f8ff;
    letter-spacing: 1px;
}
QLabel#SubTitle {
    font-size: 12px;
    color: #6f8496;
}
QLabel#Section {
    font-size: 12px;
    font-weight: 700;
    color: #8fa6b8;
    letter-spacing: 2px;
}
QLabel#Value {
    font-size: 26px;
    font-weight: 700;
    color: #f2f8ff;
}
QLabel#ValueSmall {
    font-size: 18px;
    font-weight: 700;
    color: #f2f8ff;
}
QLabel#Unit {
    font-size: 12px;
    color: #7c93a5;
}
QLabel#Muted {
    color: #7c93a5;
}
QLabel#Badge {
    border-radius: 10px;
    padding: 3px 10px;
    font-size: 11px;
    font-weight: 700;
}
QLabel#StatusText {
    font-size: 15px;
    font-weight: 700;
}
QPushButton {
    background: #16232e;
    border: 1px solid #263a4b;
    border-radius: 9px;
    padding: 9px 14px;
    color: #cfe0ed;
    font-weight: 600;
}
QPushButton:hover {
    background: #1c2d3b;
    border-color: #35536a;
}
QPushButton:pressed {
    background: #12202a;
}
QPushButton:disabled {
    color: #4f606d;
    background: #0f171e;
    border-color: #1a2630;
}
QPushButton[kind="primary"] {
    background: #1f8f7f;
    border-color: #2bc3ab;
    color: #031411;
}
QPushButton[kind="primary"]:hover {
    background: #25a794;
}
QPushButton[kind="primary"]:disabled {
    background: #153b36;
    border-color: #1d4c45;
    color: #587a74;
}
QPushButton[kind="active"] {
    background: #2a6e63;
    border-color: #37d6c0;
    color: #eafffb;
}
QPushButton[kind="danger"] {
    background: #3a1a1e;
    border-color: #7d3038;
    color: #ffb8bd;
}
QPushButton[kind="danger"]:hover {
    background: #4a2025;
}
QPushButton[kind="record"] {
    background: #351a1d;
    border-color: #6f3038;
    color: #ffb0b6;
}
QPushButton[kind="record"]:hover {
    background: #452126;
}
QPushButton[kind="ghost"] {
    background: transparent;
    border-color: #24384a;
    color: #9fb4c5;
}
QLineEdit, QComboBox, QSpinBox {
    background: #0d151c;
    border: 1px solid #223444;
    border-radius: 8px;
    padding: 7px 10px;
    selection-background-color: #1f8f7f;
}
QLineEdit:focus, QComboBox:focus {
    border-color: #2bc3ab;
}
QCheckBox {
    spacing: 9px;
    color: #c7d6e2;
}
QCheckBox::indicator {
    width: 17px;
    height: 17px;
    border-radius: 5px;
    border: 1px solid #35536a;
    background: #0d151c;
}
QCheckBox::indicator:checked {
    background: #1f8f7f;
    border-color: #37d6c0;
}
QCheckBox:disabled {
    color: #5b6f7e;
}
QCheckBox::indicator:disabled {
    background: #0a1116;
    border-color: #23343f;
}
QCheckBox::indicator:checked:disabled {
    background: #14564f;
    border-color: #2a6b61;
}
QPlainTextEdit, QTextEdit {
    background: #070b0f;
    border: 1px solid #1a2833;
    border-radius: 10px;
    color: #9fb4c5;
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 12px;
    padding: 8px;
    selection-background-color: #1f8f7f;
}
QTabWidget::pane {
    border: 1px solid #1a2833;
    border-radius: 10px;
    background: #070b0f;
}
QTabBar::tab {
    background: transparent;
    color: #6f8496;
    padding: 8px 14px;
    border-bottom: 2px solid transparent;
}
QTabBar::tab:selected {
    color: #dce6ef;
    border-bottom: 2px solid #37d6c0;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #24384a;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QToolTip {
    background: #101a22;
    color: #dce6ef;
    border: 1px solid #2bc3ab;
    padding: 6px;
}
"""
