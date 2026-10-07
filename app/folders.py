"""폴더 관리 (#2): 등록 해제, 하위 폴더 제외 / 제외 풀기. 영상 파일과 기록은 지우지 않음"""
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout)

from app import library
from app import thumb_tool as tt
from app.drives import connected_drives, volume_info

ROLE = Qt.ItemDataRole.UserRole


def _item_of(path):
    path = os.path.abspath(path)
    letter = os.path.splitdrive(path)[0].upper()
    serial, _ = volume_info(letter + "\\")
    prefix = os.path.relpath(path, letter + "\\")
    return {"serial": serial, "prefix": "" if prefix == "." else prefix}


def _same(a, b):
    return (a.get("serial") == b.get("serial")
            and os.path.normcase(a.get("prefix") or "") == os.path.normcase(b.get("prefix") or ""))


def _show(item, now):
    letter = now.get(item["serial"])
    if letter:
        return os.path.join(letter + "\\", item["prefix"])
    return f"(외장하드 연결 안 됨)  …\\{item['prefix']}"


def exclude(conn, item):
    lst = library.get_setting(conn, "excluded", [])
    if not any(_same(x, item) for x in lst):
        lst.append(item)
        library.set_setting(conn, "excluded", lst)
    return library.set_excluded_flag(conn, item["serial"], item["prefix"], 1)


def unexclude(conn, item):
    lst = [x for x in library.get_setting(conn, "excluded", []) if not _same(x, item)]
    library.set_setting(conn, "excluded", lst)
    n = library.set_excluded_flag(conn, item["serial"], item["prefix"], 0)
    for x in lst:                     # 안쪽·바깥쪽에 겹친 다른 제외는 다시 적용
        library.set_excluded_flag(conn, x["serial"], x["prefix"], 1)
    return n


class FolderDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.w, self.conn, self.changed = window, window.conn, False
        self.setWindowTitle("📂 폴더 관리")
        self.resize(780, 560)
        lay = QVBoxLayout(self)

        lay.addWidget(QLabel("<b>등록 폴더</b>: 새 영상을 자동으로 확인하는 폴더 "
                             "(추가는 메인 화면의 📁 폴더 추가 버튼)"))
        self.reg = QListWidget()
        lay.addWidget(self.reg)
        r1 = QHBoxLayout()
        self._btn(r1, "➖ 등록 해제", self.remove_reg)
        self._btn(r1, "🚫 하위 폴더 제외하기…", self.add_excl)
        r1.addStretch()
        lay.addLayout(r1)

        lay.addWidget(QLabel("<b>제외 폴더</b>: 스캔하지 않고 목록에서도 숨김 "
                             "<span style='color:#8fd;'>(파일·태그·별점·얼굴·자막 기록은 그대로 남음)</span>"))
        self.exc = QListWidget()
        lay.addWidget(self.exc)
        r2 = QHBoxLayout()
        self._btn(r2, "↩ 제외 풀기", self.remove_excl)
        r2.addStretch()
        self._btn(r2, "닫기", self.accept)
        lay.addLayout(r2)
        self.fill()

    def _btn(self, row, text, fn):
        b = QPushButton(text)
        b.clicked.connect(lambda checked=False: fn())
        row.addWidget(b)

    def fill(self):
        now = connected_drives()
        for widget, key in ((self.reg, "folders"), (self.exc, "excluded")):
            widget.clear()
            for item in library.get_setting(self.conn, key, []):
                n = len(library.folder_video_ids(self.conn, item["serial"], item["prefix"]))
                it = QListWidgetItem(f"{_show(item, now)}      (영상 {n}개)")
                it.setData(ROLE, item)
                widget.addItem(it)

    def _picked(self, widget):
        it = widget.currentItem()
        if it is None:
            QMessageBox.information(self, "폴더 관리", "목록에서 폴더를 먼저 클릭하세요.")
            return None
        return it.data(ROLE)

    def remove_reg(self):
        item = self._picked(self.reg)
        if not item:
            return
        box = QMessageBox(self)
        box.setWindowTitle("등록 해제")
        box.setText(f"{_show(item, connected_drives())}\n\n이 폴더를 등록에서 뺍니다. "
                    "이 폴더 영상을 목록에서도 숨길까요?\n(영상 파일과 기록은 지워지지 않습니다)")
        b_hide = box.addButton("목록에서도 숨기기", QMessageBox.ButtonRole.AcceptRole)
        b_keep = box.addButton("등록만 해제 (영상은 계속 보임)", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() not in (b_hide, b_keep):
            return
        folders = [x for x in library.get_setting(self.conn, "folders", []) if not _same(x, item)]
        library.set_setting(self.conn, "folders", folders)
        if box.clickedButton() is b_hide:
            exclude(self.conn, item)
        self.changed = True
        self.fill()

    def add_excl(self):
        start = ""
        it = self.reg.currentItem()
        if it is not None:
            p = _show(it.data(ROLE), connected_drives())
            start = p if os.path.isdir(p) else ""
        path = QFileDialog.getExistingDirectory(self, "제외할 폴더 고르기", start)
        if not path:
            return
        item = _item_of(path)
        n = len(library.folder_video_ids(self.conn, item["serial"], item["prefix"]))
        if QMessageBox.question(self, "폴더 제외",
                                f"{os.path.normpath(path)}\n\n안의 영상 {n}개를 목록에서 숨기고 "
                                "앞으로 스캔하지 않습니다.\n(파일과 기록은 그대로, 언제든 제외 풀기 가능)"
                                ) != QMessageBox.StandardButton.Yes:
            return
        exclude(self.conn, item)
        self.changed = True
        self.fill()

    def remove_excl(self):
        item = self._picked(self.exc)
        if not item:
            return
        n = unexclude(self.conn, item)
        self.changed = True
        self.fill()
        QMessageBox.information(self, "제외 풀기", f"영상 {n}개가 다시 목록에 보입니다.")

    def done(self, r):
        if self.changed:
            self.w.after_edit()
        super().done(r)


def exclude_video_folder(window, vids):
    """우클릭: 선택한 영상이 들어 있는 폴더를 제외"""
    dirs = sorted({os.path.dirname(v["full_path"]) for v in vids if v.get("online") and v.get("full_path")})
    if not dirs:
        QMessageBox.information(window, "폴더 제외", "외장하드가 연결된 영상을 골라 주세요.")
        return
    items = [_item_of(d) for d in dirs]
    n = sum(len(library.folder_video_ids(window.conn, i["serial"], i["prefix"])) for i in items)
    text = "\n".join("· " + d for d in dirs[:8]) + (f"\n… 외 {len(dirs) - 8}개" if len(dirs) > 8 else "")
    if QMessageBox.question(window, "폴더 제외",
                            f"{text}\n\n이 폴더의 영상 {n}개를 목록에서 숨기고 앞으로 스캔하지 않습니다.\n"
                            "(파일과 기록은 그대로, 📂 폴더 관리에서 제외 풀기 가능)"
                            ) != QMessageBox.StandardButton.Yes:
        return
    for i in items:
        exclude(window.conn, i)
    window.after_edit()
    window.statusBar().showMessage(f"🚫 폴더 {len(items)}개 제외 (영상 {n}개 숨김)", 8000)


def install(window):
    window._vv_exclude = lambda vids: exclude_video_folder(window, vids)
    act = QAction("📂 폴더 관리", window)
    act.setToolTip("등록 폴더 해제, 하위 폴더 제외 / 제외 풀기")
    act.triggered.connect(lambda checked=False: FolderDialog(window).exec())
    tt._toolbar(window).addAction(act)
