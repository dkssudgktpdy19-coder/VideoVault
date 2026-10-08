"""툴바 정리: 나중에 추가된 버튼들을 맨 앞 ☰ 메뉴 하나로 모아 툴바를 짧게"""
from PySide6.QtWidgets import QMenu, QToolButton

from app import thumb_tool as tt

# 툴바에 계속 두고 싶은 버튼의 글자 일부. 예: ("🎭", "👤")
KEEP_ON_BAR = ()


def snapshot(window):
    """처음부터 있던 툴바 항목 기억 (MainWindow 만든 직후에 호출)"""
    window._vv_tb_base = set(tt._toolbar(window).actions())


def _keep(text):
    return any(k in (text or "") for k in KEEP_ON_BAR)


def install(window):
    """맨 마지막에 호출: 기억해 둔 것 외의 버튼을 ☰ 메뉴로"""
    tb = tt._toolbar(window)
    base = getattr(window, "_vv_tb_base", None)
    if base is None:
        return
    menu = QMenu(window)
    plain, subs = [], []
    for act in list(tb.actions()):
        if act in base:
            continue
        if act.isSeparator():
            tb.removeAction(act)
            continue
        w = tb.widgetForAction(act)
        if not isinstance(w, QToolButton):
            continue
        if w.defaultAction() is act:                 # 보통 버튼
            if _keep(act.text()):
                continue
            if len(act.text().strip()) <= 2 and act.toolTip():
                act.setText(f"{act.text().strip()} {act.toolTip().splitlines()[0]}")
            plain.append(act)
        elif w.menu() is not None:                   # ▼ 메뉴가 달린 버튼 → 하위 메뉴
            if not _keep(w.text()):
                subs.append((act, w))
    for act in plain:
        window.addAction(act)                        # 단축키 유지
        tb.removeAction(act)
        menu.addAction(act)
    if plain and subs:
        menu.addSeparator()
    for act, w in subs:
        m = w.menu()
        m.setTitle(w.text())
        menu.addMenu(m)
        act.setVisible(False)
    if menu.isEmpty():
        return
    btn = QToolButton(window)
    btn.setText("☰ 메뉴")
    btn.setToolTip("배우·얼굴·품번·같은 영상·북마크·자막·백업·폴더 관리·미리보기 켜기/끄기")
    btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    btn.setMenu(menu)
    acts = tb.actions()
    if acts:
        tb.insertWidget(acts[0], btn)
    else:
        tb.addWidget(btn)
    window._vv_menu_btn = btn
