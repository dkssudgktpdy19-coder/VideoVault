"""왼쪽 사이드바: 태그·배우 목록 (체크하면 그 영상만 보기)"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox, QPushButton, QSplitter,
                               QVBoxLayout, QWidget)

from app import tags

ID_ROLE = Qt.ItemDataRole.UserRole
NAME_ROLE = Qt.ItemDataRole.UserRole + 1
ICON = {"tag": "🏷", "actor": "👤"}


class ItemList(QWidget):
    changed = Signal()   # 체크가 바뀜 → 영상 목록 다시 거르기
    edited = Signal()    # 이름 변경·삭제 → 영상 목록도 새로고침

    def __init__(self, conn, kind, parent=None):
        super().__init__(parent)
        self.conn, self.kind = conn, kind
        self.label = tags.LABEL[kind]
        self._checked = set()

        title = QLabel(f"{ICON[kind]} {self.label}")
        title.setStyleSheet("font-weight:bold; font-size:13px;")
        self.lbl_count = QLabel()
        self.lbl_count.setStyleSheet("color:#888;")
        btn_new = QPushButton("+ 새로")
        btn_new.setToolTip(f"새 {self.label} 만들기")
        btn_new.clicked.connect(lambda checked=False: self.create())
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(title)
        head.addWidget(self.lbl_count)
        head.addStretch(1)
        head.addWidget(btn_new)

        self.find = QLineEdit()
        self.find.setPlaceholderText(f"{self.label} 찾기")
        self.find.setClearButtonEnabled(True)
        self.find.textChanged.connect(lambda _: self._apply_find())

        self.list = QListWidget()
        self.list.itemChanged.connect(self._on_item_changed)
        self.list.itemDoubleClicked.connect(self.only_this)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.addLayout(head)
        lay.addWidget(self.find)
        lay.addWidget(self.list, 1)
        self.refresh()

    # ---------- 목록 ----------
    def refresh(self):
        items = tags.list_items(self.conn, self.kind)
        self._checked &= {it["id"] for it in items}
        self.list.blockSignals(True)
        self.list.clear()
        for it in items:
            li = QListWidgetItem(f"{it['name']}  ({it['n']})")
            li.setData(ID_ROLE, it["id"])
            li.setData(NAME_ROLE, it["name"])
            li.setFlags(li.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            li.setCheckState(Qt.CheckState.Checked if it["id"] in self._checked
                             else Qt.CheckState.Unchecked)
            self.list.addItem(li)
        self.list.blockSignals(False)
        self.lbl_count.setText(f"{len(items)}개")
        self._apply_find()

    def _apply_find(self):
        text = self.find.text().strip().lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(bool(text) and text not in it.data(NAME_ROLE).lower())

    def _sync_checks(self):
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setCheckState(Qt.CheckState.Checked if it.data(ID_ROLE) in self._checked
                             else Qt.CheckState.Unchecked)
        self.list.blockSignals(False)

    def _on_item_changed(self, item):
        xid = item.data(ID_ROLE)
        if item.checkState() == Qt.CheckState.Checked:
            self._checked.add(xid)
        else:
            self._checked.discard(xid)
        self.changed.emit()

    def only_this(self, item):
        self._checked = {item.data(ID_ROLE)}
        self._sync_checks()
        self.changed.emit()

    def checked_ids(self):
        return list(self._checked)

    def checked_names(self):
        return [self.list.item(i).data(NAME_ROLE) for i in range(self.list.count())
                if self.list.item(i).data(ID_ROLE) in self._checked]

    def clear_checks(self):
        self._checked.clear()
        self._sync_checks()

    # ---------- 만들기 / 우클릭 메뉴 ----------
    def create(self):
        name, ok = QInputDialog.getText(self, f"새 {self.label}", f"{self.label} 이름:")
        name = tags.clean_name(name)
        if ok and name:
            tags.get_or_create(self.conn, self.kind, name)
            self.conn.commit()
            self.refresh()

    def _menu(self, pos):
        item = self.list.itemAt(pos)
        if item is None:
            return
        xid, name = item.data(ID_ROLE), item.data(NAME_ROLE)
        menu = QMenu(self)
        a_only = menu.addAction("👁 이것만 보기")
        menu.addSeparator()
        a_rename = menu.addAction("✏ 이름 바꾸기 (같은 이름이 있으면 합치기)")
        a_alias = menu.addAction("🔤 별명 편집 (파일명 자동 인식용)") if self.kind == "actor" else None
        menu.addSeparator()
        a_del = menu.addAction("🗑 삭제")
        chosen = menu.exec(self.list.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == a_only:
            self.only_this(item)
        elif chosen == a_rename:
            self._rename(xid, name)
        elif a_alias is not None and chosen == a_alias:
            self._aliases(xid, name)
        elif chosen == a_del:
            self._delete(xid, name)

    def _rename(self, xid, name):
        new, ok = QInputDialog.getText(self, "이름 바꾸기", f"'{name}'의 새 이름:",
                                       QLineEdit.EchoMode.Normal, name)
        new = tags.clean_name(new)
        if not ok or not new or new == name:
            return
        other = tags.find_id(self.conn, self.kind, new)
        if other is not None and other != xid:
            ans = QMessageBox.question(self, "합치기",
                                       f"'{new}'이(가) 이미 있습니다.\n'{name}'을(를) '{new}'에 합칠까요?\n"
                                       f"('{name}'이 붙어 있던 영상에는 '{new}'가 붙습니다)")
            if ans != QMessageBox.StandardButton.Yes:
                return
            if xid in self._checked:
                self._checked.discard(xid)
                self._checked.add(other)
        tags.rename(self.conn, self.kind, xid, new)
        self.refresh()
        self.edited.emit()

    def _aliases(self, xid, name):
        current = tags.get_aliases(self.conn, xid)
        text, ok = QInputDialog.getText(
            self, "별명 편집",
            f"'{name}'의 다른 이름들 (쉼표로 구분)\n예) Wonee, WONI, 원희\n\n"
            "자동 태그할 때 파일명에 별명이 있어도 이 배우로 인식합니다.",
            QLineEdit.EchoMode.Normal, current)
        if ok:
            tags.set_aliases(self.conn, xid, text)
            self.edited.emit()

    def _delete(self, xid, name):
        ans = QMessageBox.question(self, "삭제",
                                   f"{self.label} '{name}'을(를) 삭제할까요?\n"
                                   f"영상에서 이 {self.label}만 빠지고, 영상 파일은 그대로입니다.")
        if ans != QMessageBox.StandardButton.Yes:
            return
        tags.delete(self.conn, self.kind, xid)
        self._checked.discard(xid)
        self.refresh()
        self.edited.emit()


class Sidebar(QWidget):
    filters_changed = Signal()
    data_edited = Signal()

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.chk_untagged = QCheckBox("정리 안 된 영상만 (태그·배우 없음)")
        self.chk_untagged.toggled.connect(lambda _: self.filters_changed.emit())

        self.tag_list = ItemList(conn, "tag")
        self.actor_list = ItemList(conn, "actor")
        for lst in (self.tag_list, self.actor_list):
            lst.changed.connect(self.filters_changed.emit)
            lst.edited.connect(self.data_edited.emit)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.tag_list)
        split.addWidget(self.actor_list)

        btn_clear = QPushButton("✖ 필터 모두 해제")
        btn_clear.clicked.connect(lambda checked=False: self.clear())

        hint = QLabel("☑ 체크: 그것이 붙은 영상만\n☑☑ 여러 개 체크: 모두 붙은 영상만\n"
                      "더블클릭: 그것만 보기 · 우클릭: 이름 변경/삭제")
        hint.setStyleSheet("color:#888; font-size:11px;")
        hint.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(self.chk_untagged)
        lay.addWidget(split, 1)
        lay.addWidget(btn_clear)
        lay.addWidget(hint)
        self.setMinimumWidth(210)

    def refresh(self):
        self.tag_list.refresh()
        self.actor_list.refresh()

    def filters(self):
        return {"tag_ids": self.tag_list.checked_ids(),
                "actor_ids": self.actor_list.checked_ids(),
                "untagged": self.chk_untagged.isChecked()}

    def summary(self):
        parts = [f"🏷{n}" for n in self.tag_list.checked_names()]
        parts += [f"👤{n}" for n in self.actor_list.checked_names()]
        if self.chk_untagged.isChecked():
            parts.append("정리 안 된 영상")
        return " + ".join(parts)

    def clear(self):
        self.chk_untagged.blockSignals(True)
        self.chk_untagged.setChecked(False)
        self.chk_untagged.blockSignals(False)
        self.tag_list.clear_checks()
        self.actor_list.clear_checks()
        self.filters_changed.emit()
