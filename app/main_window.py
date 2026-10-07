"""메인 화면: 왼쪽 태그·배우 사이드바 + 썸네일 바둑판 목록"""
import os
import subprocess
import time

from PySide6.QtCore import QEvent, QItemSelection, QItemSelectionModel, Qt, QThread, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDockWidget, QFileDialog,
                               QLabel, QLineEdit, QListView, QMainWindow, QMenu, QMessageBox,
                               QToolBar)

from app import library, scanner, tags
from app.db import init_db
from app.edit_dialog import EditDialog
from app.player import PlayerWindow
from app.sidebar import Sidebar
from app.video_grid import VideoDelegate, VideoModel

AUTO_CHECK_GAP = 60   # 창으로 돌아왔을 때 자동 확인 최소 간격(초)


def _menu_item(menu, text, fn):
    a = menu.addAction(text)
    a.triggered.connect(lambda checked=False: fn())
    return a


class ScanThread(QThread):
    """화면이 멈추지 않도록 뒤에서 스캔"""
    def __init__(self, folders, parent=None):
        super().__init__(parent)
        self.folders = folders

    def run(self):
        for f in self.folders:
            try:
                scanner.scan_folder(f)
            except Exception as e:
                print(f"[스캔 오류] {f}: {e}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VideoVault")
        self.resize(1500, 920)
        self.conn = init_db()
        tags.ensure_indexes(self.conn)
        self.scan_thread = None
        self._total_before = 0
        self._sig_before = None
        self._last_scan = 0.0

        # 썸네일 바둑판
        self.model = VideoModel()
        self.view = QListView()
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setUniformItemSizes(True)
        self.view.setSpacing(4)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.verticalScrollBar().setSingleStep(40)
        self.view.setMouseTracking(True)
        self.view.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.view.setModel(self.model)
        self.view.setItemDelegate(VideoDelegate(self.model, self.view))
        self.view.activated.connect(lambda idx: self.play_video(self.model.rows[idx.row()]))
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self.show_menu)
        self.setCentralWidget(self.view)

        # 목록 단축키: 숫자키 0~5 = 별점, E = 편집
        for n in range(6):
            sc = QShortcut(QKeySequence(str(n)), self.view)
            sc.setContext(Qt.ShortcutContext.WidgetShortcut)
            sc.activated.connect(lambda n=n: self.set_rating(n))
        sc_edit = QShortcut(QKeySequence("E"), self.view)
        sc_edit.setContext(Qt.ShortcutContext.WidgetShortcut)
        sc_edit.activated.connect(self.edit_selected)

        # 재생 창 (T = 보고 있는 영상 편집)
        self.player = PlayerWindow(self.conn)
        self.player.video_changed.connect(self.refresh_video)
        self.player.next_provider = self.neighbor_video
        sc_t = QShortcut(QKeySequence("T"), self.player)
        sc_t.setContext(Qt.ShortcutContext.WindowShortcut)
        sc_t.activated.connect(self.edit_playing)

        # 검색 지연 타이머
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(lambda: self.reload(keep=False))

        self._build_toolbar()

        # 왼쪽 사이드바
        self.sidebar = Sidebar(self.conn)
        self.sidebar.filters_changed.connect(lambda: self.reload(keep=False))
        self.sidebar.data_edited.connect(self.after_edit)
        dock = QDockWidget("정리", self)
        dock.setObjectName("sidebar")
        dock.setWidget(self.sidebar)
        dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self.resizeDocks([dock], [250], Qt.Orientation.Horizontal)

        # 상태 표시줄
        self.status = QLabel()
        self.statusBar().addWidget(self.status, 1)
        self.scan_label = QLabel()
        self.statusBar().addPermanentWidget(self.scan_label)
        self.audio_label = QLabel()
        self.audio_label.setStyleSheet("color:#9ad; padding-right:6px;")
        self.statusBar().addPermanentWidget(self.audio_label)

        # 요구사항 14번: 현재 사운드 출력 장치 표시 (5초마다 갱신)
        self.audio_timer = QTimer(self)
        self.audio_timer.setInterval(5000)
        self.audio_timer.timeout.connect(self.update_audio)
        self.audio_timer.start()
        self.update_audio()

        self.reload(keep=False)
        # 요구사항 5번: 시작하면 새 영상 자동 확인
        QTimer.singleShot(800, lambda: self.rescan_all(quiet=True))

    # ---------- 도구 막대 ----------
    def _action(self, tb, text, fn, tip=""):
        a = QAction(text, self)
        if tip:
            a.setToolTip(tip)
        a.triggered.connect(lambda checked=False: fn())
        tb.addAction(a)

    def _build_toolbar(self):
        tb = QToolBar("도구")
        tb.setMovable(False)
        self.addToolBar(tb)
        self._action(tb, "📁 폴더 추가", self.add_folder)
        self._action(tb, "📂 등록 폴더", self.show_folders)
        self._action(tb, "🔄 새 영상 확인", self.rescan_all)
        self._action(tb, "🏷 자동 태그", lambda: self.auto_tag(False),
                     "만들어 둔 태그·배우 이름이 파일명·폴더명에 들어 있는 영상에 한 번에 붙이기")
        tb.addSeparator()

        self.search = QLineEdit()
        self.search.setPlaceholderText("🔍 검색: 제목·태그·배우·메모 (띄어쓰기로 여러 단어)")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(340)
        self.search.textChanged.connect(lambda _: self.search_timer.start())
        tb.addWidget(self.search)

        tb.addWidget(QLabel("   정렬 "))
        self.sort = QComboBox()
        self.sort.addItems(list(library.SORTS))
        self.sort.currentIndexChanged.connect(lambda _: self.reload(keep=False))
        tb.addWidget(self.sort)

        tb.addWidget(QLabel("  "))
        self.chk_new = QCheckBox("NEW만 보기")
        self.chk_new.toggled.connect(lambda _: self.reload(keep=False))
        tb.addWidget(self.chk_new)
        tb.addSeparator()
        self._action(tb, "✔ NEW 모두 지우기", self.clear_all_new)

    # ---------- 목록 ----------
    def reload(self, keep=True):
        """keep=True면 스크롤 위치와 선택을 유지"""
        sb = self.view.verticalScrollBar()
        pos = sb.value() if keep else 0
        keep_ids = {v["id"] for v in self.selected_videos()} if keep else set()
        rows = library.load_videos(self.conn, self.search.text(), self.sort.currentText(),
                                   self.chk_new.isChecked(), **self.sidebar.filters())
        self.model.set_rows(rows)
        if keep_ids:
            sel = QItemSelection()
            for i, v in enumerate(rows):
                if v["id"] in keep_ids:
                    idx = self.model.index(i)
                    sel.select(idx, idx)
            self.view.selectionModel().select(sel, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        QTimer.singleShot(0, lambda: sb.setValue(pos))
        self.update_status()

    def update_status(self):
        total, new = library.counts(self.conn)
        n_folders = len(library.get_setting(self.conn, "folders", []))
        folder_txt = (f"등록 폴더 {n_folders}개" if n_folders
                      else "⚠ 등록 폴더 없음 → '📁 폴더 추가'를 해야 새 영상이 자동 확인됩니다")
        summary = self.sidebar.summary()
        filt = f"   |   필터: {summary}" if summary else ""
        self.status.setText(f"표시 {len(self.model.rows)}개 / 전체 {total}개   |   NEW {new}개   |   "
                            f"{folder_txt}{filt}   |   더블클릭: 재생 · E: 태그 편집 · 0~5: 별점")

    def update_audio(self):
        self.audio_label.setText(self.player.refresh_audio_label())

    def refresh_video(self, vid):
        fresh = library.get_video(self.conn, vid)
        if fresh:
            self.model.update_video(fresh)
        self.update_status()
        # 재생 중인 영상을 목록에서 선택해 둠
        if self.player.video and self.player.video["id"] == vid:
            for i, v in enumerate(self.model.rows):
                if v["id"] == vid:
                    idx = self.model.index(i)
                    self.view.selectionModel().setCurrentIndex(
                        idx, QItemSelectionModel.SelectionFlag.ClearAndSelect)
                    self.view.scrollTo(idx)
                    break

    def neighbor_video(self, vid, step):
        """지금 보이는 목록 순서에서 앞(-1)/뒤(+1)의 재생 가능한 영상"""
        rows = self.model.rows
        idx = next((i for i, v in enumerate(rows) if v["id"] == vid), None)
        if idx is None:
            return None
        i = idx + step
        while 0 <= i < len(rows):
            if rows[i]["online"]:
                return rows[i]
            i += step
        return None

    def selected_videos(self):
        return [self.model.rows[i.row()] for i in self.view.selectionModel().selectedIndexes()]

    # ---------- 재생 ----------
    def play_video(self, v):
        if not v["online"]:
            if v["is_missing"]:
                msg = "파일을 찾을 수 없습니다 (삭제 또는 이동).\n'새 영상 확인'을 눌러 보세요."
            else:
                msg = f"'{v.get('drive_name') or '외장하드'}'를 연결해 주세요."
            QMessageBox.information(self, "재생할 수 없음", msg)
            return
        if not self.player.open_video(v):
            QMessageBox.information(self, "재생할 수 없음", f"파일을 열 수 없습니다:\n{v['full_path']}")

    # ---------- 편집 / 태그 ----------
    def edit_selected(self):
        vids = self.selected_videos()
        if vids:
            self.edit_videos(vids, self)

    def edit_playing(self):
        if self.player.video:
            fresh = library.get_video(self.conn, self.player.video["id"])
            if fresh:
                self.edit_videos([fresh], self.player)

    def edit_videos(self, vids, parent):
        dlg = EditDialog(self.conn, vids, parent)
        if dlg.exec():
            self.after_edit()

    def after_edit(self):
        self.sidebar.refresh()
        self.reload(keep=True)

    def auto_tag(self, selected_only=False):
        if not tags.has_dictionary(self.conn):
            QMessageBox.information(
                self, "자동 태그",
                "먼저 왼쪽 사이드바에서 태그나 배우를 만들어 주세요.\n\n"
                "파일 이름이나 폴더 이름에 그 이름이 들어 있는 영상에 자동으로 붙여 줍니다.\n"
                "예) 배우 '원이'를 만들면 → 파일명에 '원이'가 있는 영상에 붙음")
            return
        ids = [v["id"] for v in self.selected_videos()] if selected_only else None
        target = f"선택한 영상 {len(ids)}개" if selected_only else "전체 영상"
        plan = tags.plan_auto_tag(self.conn, ids)
        if not plan:
            QMessageBox.information(self, "자동 태그", f"{target}에서 새로 붙일 태그·배우가 없습니다.")
            return
        n_tag = sum(len(p["tag"]) for p in plan)
        n_actor = sum(len(p["actor"]) for p in plan)
        lines = []
        for p in plan:
            parts = [f"👤{n}" for n in p["actor"]] + [f"🏷{n}" for n in p["tag"]]
            lines.append(f"{scanner.safe_text(p['filename'])}\n    → {'  '.join(parts)}")
        box = QMessageBox(self)
        box.setWindowTitle("파일명으로 자동 태그")
        box.setText(f"{target} 중 {len(plan)}개 영상에\n태그 {n_tag}개, 배우 {n_actor}개를 붙입니다.\n\n"
                    "아래 'Show Details...' 버튼을 누르면 전체 목록을 볼 수 있습니다.\n붙일까요?")
        box.setDetailedText("\n".join(lines))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.exec()
        if box.standardButton(box.clickedButton()) == QMessageBox.StandardButton.Yes:
            tags.apply_plan(self.conn, plan)
            self.after_edit()

    # ---------- 별점 / NEW ----------
    def set_rating(self, n):
        vids = self.selected_videos()
        if not vids:
            return
        library.set_rating(self.conn, [v["id"] for v in vids], n)
        for v in vids:
            v["rating"] = n
        self.view.viewport().update()

    def clear_new_selected(self):
        vids = self.selected_videos()
        library.clear_new(self.conn, [v["id"] for v in vids])
        for v in vids:
            v["is_new"] = 0
        self.view.viewport().update()
        self.update_status()

    def clear_all_new(self):
        if QMessageBox.question(self, "확인", "모든 영상의 NEW 표시를 지울까요?") \
                == QMessageBox.StandardButton.Yes:
            library.clear_all_new(self.conn)
            self.reload()

    # ---------- 우클릭 메뉴 ----------
    def show_menu(self, pos):
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            return
        sel = self.view.selectionModel()
        if not sel.isSelected(idx):
            sel.setCurrentIndex(idx, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        v = self.model.rows[idx.row()]
        count = len(self.selected_videos())

        menu = QMenu(self)
        _menu_item(menu, "▶ 재생", lambda: self.play_video(v))
        menu.addSeparator()
        _menu_item(menu, f"✏ 정보·태그 편집 ({count}개)    E", self.edit_selected)
        _menu_item(menu, f"🏷 파일명으로 자동 태그 ({count}개)", lambda: self.auto_tag(True))
        rate = menu.addMenu(f"★ 별점 매기기 ({count}개)")
        for n in range(5, -1, -1):
            _menu_item(rate, "★" * n if n else "별점 지우기", lambda n=n: self.set_rating(n))
        _menu_item(menu, f"NEW 표시 지우기 ({count}개)", self.clear_new_selected)
        subs = getattr(self, "_vv_subs", None)
        if subs is not None:
            menu.addSeparator()
            _menu_item(menu, f"💬 자막 만들기 ({count}개)    Ctrl+G",
                       lambda: subs.make(self.selected_videos()))
            if any(x.get("has_sub") for x in self.selected_videos()):
                _menu_item(menu, "🗑 자막 지우기", subs.delete_selected)

        menu.addSeparator()
        if v["full_path"]:
            path = v["full_path"]
            _menu_item(menu, "📂 탐색기에서 위치 열기",
                       lambda: subprocess.Popen(["explorer", "/select,", path]))
            _menu_item(menu, "📋 파일 경로 복사",
                       lambda: QGuiApplication.clipboard().setText(path))
        menu.exec(self.view.viewport().mapToGlobal(pos))

    # ---------- 폴더 / 스캔 ----------
    def add_folder(self):
        path = QFileDialog.getExistingDirectory(self, "영상 폴더 선택")
        if not path:
            return
        path = os.path.normpath(path)
        if len(os.path.splitdrive(path)[0]) != 2:
            QMessageBox.warning(self, "지원 안 함", "드라이브 문자(C:, E: 등)가 있는 폴더만 지원합니다.")
            return
        try:
            library.add_folder(self.conn, path)
        except OSError as e:
            QMessageBox.warning(self, "오류", str(e))
            return
        self.update_status()
        self.start_scan([path])

    def show_folders(self):
        folders = library.get_setting(self.conn, "folders", [])
        if not folders:
            QMessageBox.information(self, "등록 폴더", "등록된 폴더가 없습니다.\n'📁 폴더 추가'로 등록하세요.")
            return
        paths, offline = library.folder_paths(self.conn)
        text = "\n".join(paths) if paths else "(지금 연결된 폴더 없음)"
        if offline:
            text += f"\n\n연결 안 된 외장하드의 폴더 {offline}개"
        QMessageBox.information(self, f"등록 폴더 {len(folders)}개", text)

    def rescan_all(self, quiet=False):
        paths, offline = library.folder_paths(self.conn)
        if not paths:
            if not quiet:
                QMessageBox.information(self, "알림", "등록된 폴더가 없거나, 등록된 외장하드가 연결되어 "
                                                    "있지 않습니다.\n'📁 폴더 추가'로 폴더를 등록하세요.")
            return
        self.start_scan(paths, offline, quiet)

    def _signature(self):
        """목록이 바뀌었는지 비교하기 위한 요약값"""
        return tuple(self.conn.execute(
            "SELECT COUNT(*), IFNULL(MAX(id),0), IFNULL(SUM(is_missing),0), "
            "IFNULL(SUM(thumb_path IS NOT NULL AND thumb_path != ''),0), "
            "IFNULL(SUM(LENGTH(rel_path)),0) FROM videos"
        ).fetchone())

    def start_scan(self, paths, offline=0, quiet=False):
        if self.scan_thread and self.scan_thread.isRunning():
            if not quiet:
                QMessageBox.information(self, "스캔 중", "이미 스캔 중입니다. 끝난 뒤 다시 시도하세요.")
            return
        self._last_scan = time.time()
        msg = f"🔄 새 영상 확인 중... (폴더 {len(paths)}개)"
        if offline:
            msg += f"  · 연결 안 된 폴더 {offline}개 건너뜀"
        self.scan_label.setText(msg)
        self._total_before = library.counts(self.conn)[0]
        self._sig_before = self._signature()
        self.scan_thread = ScanThread(paths, self)
        self.scan_thread.finished.connect(self.scan_done)
        self.scan_thread.start()

    def scan_done(self):
        added = library.counts(self.conn)[0] - self._total_before
        self.scan_label.setText(f"✅ 확인 완료 · 새 영상 {added}개")
        if self._signature() != self._sig_before:
            self.reload(keep=True)     # 스크롤·선택 유지한 채 새로고침
        QTimer.singleShot(8000, lambda: self.scan_label.setText(""))

    def changeEvent(self, e):
        """다른 창에 있다가 이 창으로 돌아오면 새 영상 자동 확인"""
        if getattr(self, "_closing", False):
            return
        super().changeEvent(e)
        if e.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            if time.time() - self._last_scan > AUTO_CHECK_GAP:
                self.rescan_all(quiet=True)

    # ---------- 종료 ----------
    def closeEvent(self, event):
        self._closing = True
        self.audio_timer.stop()
        self.player.shutdown()
        scanning = self.scan_thread is not None and self.scan_thread.isRunning()
        self.conn.close()
        event.accept()
        if scanning:
            os._exit(0)   # 스캔 도중이면 바로 종료 (다음 실행 때 이어서 처리됨)
