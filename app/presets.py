"""자주 쓰는 검색 저장: 검색어·정렬·NEW만 보기·사이드바 체크를 이름 붙여 저장하고 한 번에 불러오기"""
import json
import re
import sqlite3
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractButton, QCheckBox, QComboBox, QInputDialog,
                               QLineEdit, QListWidget, QMenu, QMessageBox, QToolBar,
                               QToolButton, QWidget)

from app import thumb_tool as tt

YES = QMessageBox.StandardButton.Yes
_COUNT = re.compile(r"\s*[\(\[]\s*[\d,]+\s*[\)\]]\s*$")


def ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS search_presets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        data TEXT NOT NULL,
        created REAL)""")
    conn.commit()


def _status(window, text, ms=6000):
    try:
        window.statusBar().showMessage(text, ms)
    except Exception:
        pass


# ---------------- 화면에서 조건 위젯 찾기 ----------------
def _key(text):
    return _COUNT.sub("", (text or "").split("\n")[0]).strip()


def _inside(w, parent):
    while w is not None:
        if w is parent:
            return True
        w = w.parentWidget()
    return False


def _in_toolbar(w):
    while w is not None:
        if isinstance(w, QToolBar):
            return True
        w = w.parentWidget()
    return False


def _sidebar(window):
    try:
        from app.sidebar import Sidebar
    except Exception:
        return None
    found = window.findChildren(Sidebar)
    return found[0] if found else None


def _search_box(window):
    sb = _sidebar(window)
    cands = [e for e in window.findChildren(QLineEdit)
             if e.window() is window and not isinstance(e.parentWidget(), QComboBox)
             and not (sb and _inside(e, sb))]
    for e in cands:
        if _in_toolbar(e):
            return e
    for e in cands:
        if "검색" in (e.placeholderText() or ""):
            return e
    return cands[0] if cands else None


def _sort_box(window):
    s = getattr(window, "sort", None)
    return s if isinstance(s, QComboBox) else None


def _new_toggle(window):
    for a in window.findChildren(QAction):
        if a.isCheckable() and "NEW" in a.text().upper():
            return a
    sb = _sidebar(window)
    for b in window.findChildren(QAbstractButton):
        if (b.isCheckable() and "NEW" in b.text().upper() and b.window() is window
                and not (sb and _inside(b, sb))):
            return b
    return None


def _flip(w):
    if isinstance(w, QAction):
        w.trigger()
    else:
        w.click()


def _find_item(lw, s):
    if s.get("id") is not None:
        for i in range(lw.count()):
            if lw.item(i).data(Qt.UserRole) == s["id"]:
                return lw.item(i)
    for i in range(lw.count()):
        if _key(lw.item(i).text()) == s.get("name"):
            return lw.item(i)
    return None


# ---------------- 저장 / 적용 ----------------
def capture(window):
    st = {"text": "", "sort": None, "new": None, "checks": {}, "lists": []}
    box = _search_box(window)
    if box:
        st["text"] = box.text().strip()
    s = _sort_box(window)
    if s:
        st["sort"] = s.currentText()
    n = _new_toggle(window)
    if n is not None:
        st["new"] = n.isChecked()
    sb = _sidebar(window)
    if sb:
        for cb in sb.findChildren(QCheckBox):
            if cb.text():
                st["checks"][cb.text()] = cb.isChecked()
        lists = sb.findChildren(QListWidget)
        if not lists:
            print("[presets] 사이드바 목록을 찾지 못함:",
                  sorted({type(w).__name__ for w in sb.findChildren(QWidget)}))
        for lw in lists:
            picked = []
            for i in range(lw.count()):
                it = lw.item(i)
                if it.checkState() == Qt.CheckState.Checked:
                    v = it.data(Qt.UserRole)
                    picked.append({"id": v if isinstance(v, int) else None,
                                   "name": _key(it.text())})
            st["lists"].append(picked)
    return st


def apply(window, st):
    missing = []
    sb = _sidebar(window)
    if sb:
        if hasattr(sb, "clear"):
            try:
                sb.clear()
            except Exception:
                pass
        for cb in sb.findChildren(QCheckBox):
            want = st.get("checks", {}).get(cb.text())
            if want is not None and cb.isChecked() != want:
                cb.click()
        for i, saved in enumerate(st.get("lists", [])):
            for s in saved:
                lists = sb.findChildren(QListWidget)
                if i >= len(lists):
                    break
                it = _find_item(lists[i], s)
                if it is None:
                    missing.append(s.get("name") or "?")
                elif it.checkState() != Qt.CheckState.Checked:
                    it.setCheckState(Qt.CheckState.Checked)
    n = _new_toggle(window)
    if n is not None and st.get("new") is not None and n.isChecked() != st["new"]:
        _flip(n)
    s = _sort_box(window)
    if s and st.get("sort"):
        i = s.findText(st["sort"])
        if i >= 0:
            s.setCurrentIndex(i)
    box = _search_box(window)
    if box is not None:
        box.setText(st.get("text", ""))
    QTimer.singleShot(400, lambda: tt._refresh(window))
    return missing


def describe(st):
    parts = []
    if st.get("text"):
        parts.append(f"검색 '{st['text']}'")
    names = [s.get("name") for lst in st.get("lists", []) for s in lst if s.get("name")]
    if names:
        parts.append(" + ".join(names))
    for k, v in st.get("checks", {}).items():
        if v:
            parts.append(k)
    if st.get("new"):
        parts.append("NEW만")
    if st.get("sort"):
        parts.append(f"정렬: {st['sort']}")
    return ", ".join(parts) or "조건 없음"


def _rows(conn):
    return conn.execute("SELECT id, name, data FROM search_presets ORDER BY id").fetchall()


def save_current(window):
    st = capture(window)
    desc = describe(st)
    name, ok = QInputDialog.getText(window, "검색 저장", f"저장할 이름:\n\n({desc})",
                                    QLineEdit.EchoMode.Normal, desc[:30])
    if not ok or not name.strip():
        return
    name = name.strip()
    conn = window.conn
    data = json.dumps(st, ensure_ascii=False)
    row = conn.execute("SELECT id FROM search_presets WHERE name=?", (name,)).fetchone()
    if row:
        if QMessageBox.question(window, "검색 저장",
                                f"'{name}'이(가) 이미 있습니다. 덮어쓸까요?") != YES:
            return
        conn.execute("UPDATE search_presets SET data=? WHERE id=?", (data, row[0]))
    else:
        conn.execute("INSERT INTO search_presets(name, data, created) VALUES (?,?,?)",
                     (name, data, time.time()))
    conn.commit()
    _status(window, f"⭐ 검색 '{name}' 저장함")


def load_preset(window, pid):
    row = window.conn.execute("SELECT name, data FROM search_presets WHERE id=?",
                              (pid,)).fetchone()
    if not row:
        return
    missing = apply(window, json.loads(row[1]))
    text = f"⭐ '{row[0]}' 불러옴"
    if missing:
        text += f"  (⚠ 없어진 항목: {', '.join(missing)})"
    _status(window, text, 8000)


def load_by_number(window, n):
    rows = _rows(window.conn)
    if n <= len(rows):
        load_preset(window, rows[n - 1][0])
    else:
        _status(window, f"{n}번에 저장된 검색이 없습니다")


def clear_all(window):
    st = capture(window)
    apply(window, {"text": "", "sort": None, "new": False, "lists": [],
                   "checks": {k: False for k in st.get("checks", {})}})
    _status(window, "모든 조건을 해제했습니다")


def rename(window, pid, old):
    name, ok = QInputDialog.getText(window, "이름 바꾸기", "새 이름:",
                                    QLineEdit.EchoMode.Normal, old)
    if not ok or not name.strip():
        return
    try:
        window.conn.execute("UPDATE search_presets SET name=? WHERE id=?", (name.strip(), pid))
        window.conn.commit()
    except sqlite3.IntegrityError:
        QMessageBox.warning(window, "이름 바꾸기", "같은 이름이 이미 있습니다.")


def delete(window, pid, name):
    if QMessageBox.question(window, "삭제", f"저장한 검색 '{name}'을(를) 삭제할까요?") == YES:
        window.conn.execute("DELETE FROM search_presets WHERE id=?", (pid,))
        window.conn.commit()


def _build(window, menu):
    menu.clear()
    menu.setToolTipsVisible(True)
    menu.addAction("➕ 지금 조건 저장하기… (Ctrl+S)").triggered.connect(
        lambda: save_current(window))
    menu.addAction("↩ 모든 조건 해제").triggered.connect(lambda: clear_all(window))
    menu.addSeparator()
    rows = _rows(window.conn)
    if not rows:
        menu.addAction("(저장된 검색 없음)").setEnabled(False)
    for i, (pid, name, data) in enumerate(rows, 1):
        act = menu.addAction(f"{i}. {name}" + (f"\tCtrl+{i}" if i <= 9 else ""))
        try:
            act.setToolTip(describe(json.loads(data)))
        except Exception:
            pass
        act.triggered.connect(lambda _=False, p=pid: load_preset(window, p))
    if rows:
        menu.addSeparator()
        ren = menu.addMenu("✏ 이름 바꾸기")
        dele = menu.addMenu("🗑 삭제")
        for pid, name, _d in rows:
            ren.addAction(name).triggered.connect(
                lambda _=False, p=pid, n=name: rename(window, p, n))
            dele.addAction(name).triggered.connect(
                lambda _=False, p=pid, n=name: delete(window, p, n))


def install(window):
    ensure_table(window.conn)
    tb = tt._toolbar(window)
    btn = QToolButton(window)
    btn.setText("⭐ 검색")
    btn.setToolTip("자주 쓰는 검색 저장·불러오기\nCtrl+S 저장, Ctrl+1~9 불러오기")
    btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    menu = QMenu(btn)
    menu.aboutToShow.connect(lambda: _build(window, menu))
    btn.setMenu(menu)
    tb.addWidget(btn)

    QShortcut(QKeySequence("Ctrl+S"), window).activated.connect(lambda: save_current(window))
    for i in range(1, 10):
        QShortcut(QKeySequence(f"Ctrl+{i}"), window).activated.connect(
            lambda n=i: load_by_number(window, n))
