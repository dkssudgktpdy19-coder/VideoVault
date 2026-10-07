"""자막 화면 연결: 백그라운드로 만들기, 재생할 때 자동으로 불러오기, 플레이어 단축키"""
import json
import os
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QInputDialog, QLabel, QMenu, QMessageBox,
                               QToolButton, QWidget)

from app import faces as fc
from app import marks
from app import subtitle as st
from app import thumb_tool as tt

STATE_FILE = fc.DATA_DIR / "sub_state.json"
YES = QMessageBox.StandardButton.Yes
CHOICES = [
    ("자동 감지 → 한국어로 번역", None, True),
    ("영어 → 한국어", "en", True),
    ("일본어 → 한국어", "ja", True),
    ("중국어 → 한국어", "zh", True),
    ("원문 자막만 (번역 안 함, 언어 자동 감지)", None, False),
]


def _state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _set_state(**kw):
    s = _state()
    s.update(kw)
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


def _norm(p):
    return os.path.normcase(os.path.normpath(p)) if p else ""


def _osd(window, text):
    marks._osd(window, text)


def _selected(window):
    view = tt._grid(window)
    if not view:
        return []
    sm = view.selectionModel()
    rows = sorted({i.row() for i in sm.selectedIndexes()}) if sm else []
    if not rows and view.currentIndex().isValid():
        rows = [view.currentIndex().row()]
    out = []
    for r in rows:
        d = tt._row_at(view.model(), r)
        if d and "id" in d:
            out.append(d)
    return out


def ask_options(parent, n):
    labels = [c[0] for c in CHOICES]
    last = int(_state().get("choice", 0))
    last = last if 0 <= last < len(labels) else 0
    text, ok = QInputDialog.getItem(parent, "자막 만들기",
                                    f"영상 {n}개의 자막을 만듭니다.\n영상에서 말하는 언어를 고르세요:",
                                    labels, last, False)
    if not ok:
        return None
    i = labels.index(text)
    _set_state(choice=i)
    return CHOICES[i][1], CHOICES[i][2]


# ---------------- 백그라운드 작업 ----------------
class SubThread(QThread):
    progress = Signal(str)
    one_done = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.jobs = []
        self.stop = False
        self.ok = self.fail = self.n = 0

    def run(self):
        conn = eng = None
        try:
            conn = fc.open_db()
            st.ensure_table(conn)
            self.progress.emit("💬 자막 엔진 준비 중…")
            eng = st.Engine()
            while self.jobs and not self.stop:
                vid, path, title, lang, tr = self.jobs.pop(0)
                self.n += 1
                total = self.n + len(self.jobs)
                last = {"t": ""}

                def prog(stage, p, n=self.n, total=total, short=title[:24]):
                    t = f"💬 자막 {n}/{total} · {stage} {p * 100:.0f}% · {short}"
                    if t != last["t"]:
                        last["t"] = t
                        self.progress.emit(t)
                try:
                    src, cnt, did = st.make_subs(eng, conn, vid, path, lang, tr, prog,
                                                 lambda: self.stop)
                    self.ok += 1
                    msg = f"{st.lang_name(src)} {cnt}줄" + (" → 한국어 번역" if did else "")
                    if tr and not did and src != "ko":
                        msg += " (이 언어는 번역 미지원)"
                    self.one_done.emit(vid, msg)
                except st.Cancelled:
                    break
                except Exception as e:
                    self.fail += 1
                    print(f"[자막 실패] {title}: {e}")
                    self.one_done.emit(vid, f"❌ 실패: {e}")
        except Exception as e:
            print("[자막 엔진 오류]", e)
            self.progress.emit("⚠ 자막 엔진 오류 (터미널 확인)")
        finally:
            if eng is not None:
                eng.close()
            if conn is not None:
                conn.close()


# ---------------- 재생할 때 자막 자동으로 불러오기 ----------------
class SubLoader:
    def __init__(self, window):
        self.w = window
        self.last = None
        self.done = False
        self.timer = QTimer(window)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def tick(self):
        mp = tt._mpv(self.w)
        try:
            path, dur = mp.path, mp.duration
        except Exception:
            return
        if path != self.last:
            self.last, self.done = path, False
        if not path or self.done or not dur:
            return
        self.done = True
        self.load(mp, path)

    def load(self, mp, path, notify=False):
        vid = marks.vid_for_path(self.w, path)
        if vid is None:
            return
        subs = st.subs_for(self.w.conn, vid)
        if not subs:
            return
        for k, v in (("sub-font", "Malgun Gothic"), ("sub-font-size", 46), ("sub-border-size", 3)):
            try:
                mp[k] = v
            except Exception:
                pass
        try:
            have = {_norm(t.get("external-filename")) for t in (mp.track_list or [])
                    if t.get("type") == "sub"}
            has_ko = any(k == "ko" for k, _l, _p in subs)
            for kind, lang, p in sorted(subs, key=lambda s: s[0] != "ko"):
                if _norm(p) in have:
                    continue
                title = "한국어" if kind == "ko" else f"원문({st.lang_name(lang)})"
                flag = "select" if (kind == "ko" or not has_ko) else "auto"
                mp.sub_add(p, flag, title, lang)
            if notify:
                _osd(self.w, "💬 자막을 불러왔습니다 (V 켜기/끄기, J 바꾸기)")
        except Exception as e:
            print("[자막 불러오기 실패]", e)

    def reload_if_playing(self, vid):
        mp = tt._mpv(self.w)
        try:
            path = mp.path
        except Exception:
            return
        if path and marks.vid_for_path(self.w, path) == vid:
            self.load(mp, path, notify=True)


# ---------------- 관리 ----------------
class SubManager:
    def __init__(self, window):
        self.w = window
        self.th = None
        self.label = QLabel("")
        try:
            window.statusBar().addPermanentWidget(self.label)
        except Exception:
            pass
        self.loader = SubLoader(window)

    def running(self):
        return bool(self.th and self.th.isRunning())

    def _status(self, text, ms=8000):
        try:
            self.w.statusBar().showMessage(text, ms)
        except Exception:
            pass

    def make(self, infos, parent=None):
        parent = parent or self.w
        jobs, missing = [], 0
        for d in infos:
            p = tt._find_path(d)
            if not p:
                missing += 1
                continue
            jobs.append((d["id"], p, d.get("title") or Path(p).stem))
        if not jobs:
            QMessageBox.information(parent, "자막",
                                    "영상 파일을 찾을 수 없습니다.\n(외장하드 연결 확인)" if missing
                                    else "자막을 만들 영상을 먼저 클릭하세요.")
            return
        have = {r[0] for r in self.w.conn.execute("SELECT DISTINCT video_id FROM sub_files")}
        dup = [j for j in jobs if j[0] in have]
        if dup and len(dup) == len(jobs):
            if QMessageBox.question(parent, "자막", "이미 자막이 있습니다. 다시 만들까요?") != YES:
                return
        elif dup:
            if QMessageBox.question(parent, "자막",
                                    f"{len(dup)}개는 자막이 이미 있습니다. 이 영상들도 다시 만들까요?\n"
                                    f"(아니요 = 자막 없는 {len(jobs) - len(dup)}개만)") != YES:
                jobs = [j for j in jobs if j[0] not in have]
        opt = ask_options(parent, len(jobs))
        if opt is None:
            return
        lang, tr = opt
        queued = {j[0] for j in self.th.jobs} if self.running() else set()
        jobs = [j + (lang, tr) for j in jobs if j[0] not in queued]
        if self.running():
            self.th.jobs.extend(jobs)
            self._status(f"💬 자막 작업 {len(jobs)}개를 대기열에 추가했습니다")
            return
        self.th = SubThread()
        self.th.jobs = jobs
        self.th.progress.connect(self.label.setText)
        self.th.one_done.connect(self._one_done)
        self.th.finished.connect(self._finished)
        self.th.start(QThread.Priority.LowPriority)

    def _one_done(self, vid, msg):
        self._status(f"💬 자막: {msg}")
        if not msg.startswith("❌"):
            self.loader.reload_if_playing(vid)

    def _finished(self):
        self.label.setText("")
        if self.th:
            self._status(f"💬 자막 작업 끝: 성공 {self.th.ok}개, 실패 {self.th.fail}개", 12000)

    def cancel(self):
        if self.running():
            self.th.jobs.clear()
            self.th.stop = True
            self.label.setText("💬 자막 작업 취소 중…")
        else:
            self._status("진행 중인 자막 작업이 없습니다")

    def delete_selected(self):
        infos = _selected(self.w)
        if not infos:
            return
        if QMessageBox.question(self.w, "자막 지우기",
                                f"선택한 영상 {len(infos)}개의 자막 파일을 지울까요?") != YES:
            return
        for d in infos:
            st.clear_subs(self.w.conn, d["id"])
        self.w.conn.commit()
        self._status("🗑 자막을 지웠습니다")

    def stop(self):
        if self.running():
            self.th.jobs.clear()
            self.th.stop = True
            if not self.th.wait(5000):
                os._exit(0)


# ---------------- 플레이어 단축키 ----------------
def _subs(mp):
    return [t for t in (mp.track_list or []) if t.get("type") == "sub"]


def toggle_vis(window):
    mp = tt._mpv(window)
    try:
        v = not bool(mp.sub_visibility)
        mp.sub_visibility = v
        _osd(window, "💬 자막 켜짐" if v else "💬 자막 꺼짐")
    except Exception:
        pass


def cycle(window):
    mp = tt._mpv(window)
    try:
        subs = _subs(mp)
        if not subs:
            _osd(window, "자막 없음 · Ctrl+G로 만들기")
            return
        mp.command("cycle", "sub")
        cur = next((t for t in subs if t.get("id") == mp.sid), None)
        _osd(window, f"💬 자막: {cur.get('title') or cur.get('lang') or cur.get('id')}"
             if cur else "💬 자막: 끔")
    except Exception as e:
        print("[자막 바꾸기 실패]", e)


def toggle_secondary(window):
    mp = tt._mpv(window)
    try:
        subs = _subs(mp)
        if mp["secondary-sid"] not in (False, None, "no"):
            mp["secondary-sid"] = "no"
            _osd(window, "원문 동시 표시 끔")
            return
        other = next((t for t in subs if t.get("id") != mp.sid), None)
        if not other:
            _osd(window, "같이 띄울 다른 자막이 없습니다")
            return
        mp["secondary-sid"] = other["id"]
        _osd(window, f"동시 표시: {other.get('title') or other.get('lang')}")
    except Exception as e:
        print("[자막 동시 표시 실패]", e)


def delay(window, d):
    mp = tt._mpv(window)
    try:
        mp.sub_delay = round(float(mp.sub_delay or 0) + d, 2)
        _osd(window, f"💬 자막 싱크 {mp.sub_delay:+.1f}초")
    except Exception:
        pass


def make_current(window, mgr):
    pw = getattr(window, "player", None)
    path, _t = marks._playing(window)
    if not path:
        return
    d = tt.find_by_path(window, path)
    if not d:
        QMessageBox.information(pw, "자막", "메인 화면 목록에서 이 영상을 찾지 못했습니다.\n"
                                "검색·필터를 해제하고 다시 시도해 주세요.")
        return
    mgr.make([d], parent=pw)


# ---------------- 설치 ----------------
def install(window):
    st.ensure_table(window.conn)
    mgr = SubManager(window)
    window._vv_subs = mgr

    tb = tt._toolbar(window)
    btn = QToolButton(window)
    btn.setText("💬 자막")
    btn.setToolTip("음성 인식 + 한국어 번역 자막 (Ctrl+G)")
    btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    menu = QMenu(btn)
    menu.addAction("선택한 영상 자막 만들기… (Ctrl+G)").triggered.connect(
        lambda: mgr.make(_selected(window)))
    menu.addAction("자막 작업 취소").triggered.connect(mgr.cancel)
    menu.addSeparator()
    menu.addAction("선택한 영상 자막 지우기").triggered.connect(mgr.delete_selected)
    menu.addAction("자막 폴더 열기").triggered.connect(lambda: os.startfile(st.SUB_DIR))
    btn.setMenu(menu)
    tb.addWidget(btn)
    QShortcut(QKeySequence("Ctrl+G"), window).activated.connect(lambda: mgr.make(_selected(window)))

    pw = getattr(window, "player", None)
    if isinstance(pw, QWidget):
        for key, fn in (("V", lambda: toggle_vis(window)),
                        ("J", lambda: cycle(window)),
                        ("Shift+J", lambda: toggle_secondary(window)),
                        ("Z", lambda: delay(window, -0.1)),
                        ("X", lambda: delay(window, 0.1)),
                        ("Ctrl+G", lambda: make_current(window, mgr))):
            QShortcut(QKeySequence(key), pw).activated.connect(fn)
    QApplication.instance().aboutToQuit.connect(mgr.stop)
