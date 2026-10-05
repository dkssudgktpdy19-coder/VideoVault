"""영상 정보·태그·배우 편집 창 (1개 또는 여러 개 한 번에)"""
import html
import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QComboBox, QCompleter, QDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget,
                               QListWidgetItem, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from app import library, tags
from app.config import THUMB_DIR
from app.utils import fmt_duration, fmt_size, res_label

NAME_ROLE = Qt.ItemDataRole.UserRole
KIND_ROLE = Qt.ItemDataRole.UserRole + 1


def _button(text, fn, tip=""):
    b = QPushButton(text)
    b.setAutoDefault(False)     # 입력칸에서 Enter를 눌러도 창이 닫히지 않게
    b.setDefault(False)
    if tip:
        b.setToolTip(tip)
    b.clicked.connect(lambda checked=False: fn())
    return b


class ChipList(QListWidget):
    """이름표처럼 가로로 늘어놓는 목록"""
    delete_pressed = Signal()

    def __init__(self, height=64, color="#2f4f6f"):
        super().__init__()
        self.setViewMode(QListView.ViewMode.ListMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setSpacing(3)
        self.setFixedHeight(height)
        self.setStyleSheet(f"QListWidget::item{{background:{color};border-radius:4px;padding:2px 6px;}}"
                           "QListWidget::item:selected{background:#4a7ab0;}")

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_pressed.emit()
            return
        super().keyPressEvent(e)


class NameEditor(QWidget):
    """태그 또는 배우 입력칸 + 붙어 있는 목록"""

    def __init__(self, label, all_names, initial, total, parent=None):
        super().__init__(parent)
        self.total = total
        self.initial = dict(initial)     # {이름: 붙어 있는 영상 수}
        self.state = dict(initial)

        self.edit = QLineEdit()
        self.edit.setPlaceholderText(f"{label} 입력 후 Enter  (쉼표로 여러 개 · 목록에서 고르기 가능)")
        comp = QCompleter(sorted(all_names, key=str.lower), self)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
        comp.setMaxVisibleItems(12)
        self.edit.setCompleter(comp)
        comp.activated[str].connect(lambda text: QTimer.singleShot(0, lambda: self.submit(text)))
        self.edit.returnPressed.connect(lambda: self.submit(self.edit.text()))

        self.chips = ChipList()
        self.chips.itemDoubleClicked.connect(lambda it: self.remove(it.data(NAME_ROLE)))
        self.chips.delete_pressed.connect(self._remove_selected)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.edit, 1)
        row.addWidget(_button("추가", lambda: self.submit(self.edit.text())))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(row)
        lay.addWidget(self.chips)
        self._render()

    def submit(self, text):
        for part in str(text).split(","):
            name = tags.clean_name(part)
            if name:
                self.add(name)
        self.edit.clear()

    def add(self, name):
        key = next((k for k in self.state if k.lower() == name.lower()), name)
        self.state[key] = self.total
        self._render()

    def remove(self, name):
        self.state.pop(name, None)
        self._render()

    def _remove_selected(self):
        for it in self.chips.selectedItems():
            self.state.pop(it.data(NAME_ROLE), None)
        self._render()

    def _render(self):
        self.chips.clear()
        for name in sorted(self.state, key=str.lower):
            n = self.state[name]
            partial = n < self.total
            it = QListWidgetItem(f"{name} ({n}/{self.total})" if partial else name)
            it.setData(NAME_ROLE, name)
            if partial:
                it.setForeground(QColor("#aaaaaa"))
                it.setToolTip("일부 영상에만 있음 · 다시 입력하면 전체에 붙음 · 더블클릭하면 전체에서 빠짐")
            else:
                it.setToolTip("더블클릭 또는 Delete: 빼기")
            self.chips.addItem(it)

    def changes(self):
        """(새로 붙일 이름들, 뺄 이름들)"""
        self.submit(self.edit.text())    # 입력만 하고 Enter를 안 누른 것도 반영
        added = [n for n, c in self.state.items()
                 if c >= self.total and self.initial.get(n, 0) < self.total]
        removed = [n for n in self.initial if n not in self.state]
        return added, removed


class EditDialog(QDialog):
    def __init__(self, conn, videos, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.ids = [v["id"] for v in videos]
        n = len(videos)
        single = n == 1
        self.setWindowTitle("정보·태그 편집" if single else f"영상 {n}개 한 번에 편집")
        self.resize(680, 660 if single else 520)
        lay = QVBoxLayout(self)
        self.title = self.memo = None
        self.sug = None

        if single:
            v = videos[0]
            head = QHBoxLayout()
            thumb = QLabel()
            thumb.setFixedSize(224, 126)
            thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            thumb.setStyleSheet("background:#000;")
            if v.get("thumb_path"):
                pix = QPixmap(str(THUMB_DIR / v["thumb_path"]))
                if not pix.isNull():
                    thumb.setPixmap(pix.scaled(224, 126, Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))
            res = res_label(v.get("width"), v.get("height"))
            size_txt = f"{v['width']}x{v['height']}" if v.get("width") else "-"
            lines = [
                f"<b>{html.escape(v['filename'])}</b>",
                f"{fmt_duration(v.get('duration'))} · {res} {size_txt} · "
                f"{v.get('video_codec') or '-'} · {fmt_size(v.get('size'))}",
                f"▶ 본 횟수 {v.get('play_count') or 0}회 · 추가 {v.get('added_at') or '-'}",
                f"<span style='color:#888'>{html.escape(v.get('full_path') or '연결 안 됨')}</span>",
            ]
            info = QLabel("<br>".join(lines))
            info.setWordWrap(True)
            info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            head.addWidget(thumb)
            head.addWidget(info, 1)
            lay.addLayout(head)
        else:
            note = QLabel(f"선택한 영상 {n}개에 똑같이 적용됩니다.\n"
                          f"회색 '이름 (2/{n})'은 일부 영상에만 붙어 있는 것입니다. 그대로 두면 바뀌지 않습니다.")
            note.setStyleSheet("color:#9ad;")
            note.setWordWrap(True)
            lay.addWidget(note)

        form = QFormLayout()
        if single:
            self.title = QLineEdit(v.get("title") or "")
            self.title.setPlaceholderText(os.path.splitext(v["filename"])[0])
            self.memo = QPlainTextEdit(v.get("memo") or "")
            self.memo.setPlaceholderText("영상 내용 메모 (검색에도 쓰입니다)")
            self.memo.setFixedHeight(64)
            form.addRow("제목", self.title)
            form.addRow("메모", self.memo)
        self.rating = QComboBox()
        if not single:
            self.rating.addItem("변경 안 함", -1)
        for r in range(6):
            self.rating.addItem("★" * r + "☆" * (5 - r) + ("" if r else "  (없음)"), r)
        if single:
            self.rating.setCurrentIndex(int(v.get("rating") or 0))
        form.addRow("별점", self.rating)
        lay.addLayout(form)

        self.tag_ed = NameEditor("태그", tags.all_names(conn, "tag"),
                                 tags.names_for_videos(conn, "tag", self.ids), n)
        self.actor_ed = NameEditor("배우", tags.all_names(conn, "actor"),
                                   tags.names_for_videos(conn, "actor", self.ids), n)
        lay.addWidget(self._section("🏷 태그"))
        lay.addWidget(self.tag_ed)
        lay.addWidget(self._section("👤 배우"))
        lay.addWidget(self.actor_ed)

        if single:
            sug = tags.suggest(conn, v)
            if sug["tag"] or sug["actor"]:
                lay.addWidget(self._section(
                    "💡 파일명·폴더명에서 찾은 추천  (클릭: 추가 · Shift+클릭: 태그↔배우 바꿔서 추가)"))
                self.sug = ChipList(height=58, color="#3d3d3d")
                for kind in ("actor", "tag"):
                    for name in sug[kind]:
                        it = QListWidgetItem(f"{'👤' if kind == 'actor' else '🏷'} {name}")
                        it.setData(NAME_ROLE, name)
                        it.setData(KIND_ROLE, kind)
                        self.sug.addItem(it)
                self.sug.itemClicked.connect(self._take_suggestion)
                lay.addWidget(self.sug)

        lay.addStretch(1)
        btns = QHBoxLayout()
        hint = QLabel("Ctrl+Enter: 저장 · Esc: 취소")
        hint.setStyleSheet("color:#888;")
        btns.addWidget(hint)
        btns.addStretch(1)
        btns.addWidget(_button("💾 저장", self.save))
        btns.addWidget(_button("취소", self.reject))
        lay.addLayout(btns)

        for key in ("Ctrl+Return", "Ctrl+Enter"):
            sc = QShortcut(QKeySequence(key), self)
            sc.activated.connect(self.save)
        self.tag_ed.edit.setFocus()

    def _section(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet("font-weight:bold; margin-top:6px;")
        return lbl

    def _take_suggestion(self, item):
        kind = item.data(KIND_ROLE)
        if QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier:
            kind = "tag" if kind == "actor" else "actor"
        (self.actor_ed if kind == "actor" else self.tag_ed).add(item.data(NAME_ROLE))
        item.setHidden(True)

    def save(self):
        c = self.conn
        if self.title is not None:
            library.update_info(c, self.ids[0], self.title.text().strip() or None,
                                self.memo.toPlainText().strip() or None)
        r = self.rating.currentData()
        if r is not None and r >= 0:
            library.set_rating(c, self.ids, r)
        for kind, ed in (("tag", self.tag_ed), ("actor", self.actor_ed)):
            added, removed = ed.changes()
            tags.attach(c, kind, self.ids, added)
            tags.detach(c, kind, self.ids, removed)
        c.commit()
        self.accept()
