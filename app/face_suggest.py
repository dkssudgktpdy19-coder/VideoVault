"""얼굴 이름 추천(파일명·폴더명·얼굴 닮음) + 확인 대기(Y/N)"""
import html
import re
from collections import Counter
from pathlib import Path

import numpy as np
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QInputDialog, QLabel, QListView,
                               QListWidget, QListWidgetItem, QMessageBox, QPushButton,
                               QVBoxLayout)

from app import faces as fc

QUEUE_MIN = 0.35                 # 이 가능성 이상만 확인 대기에 올림
FACE_LO, FACE_HI = 0.28, 0.55    # 얼굴 유사도 → 가능성(%) 환산 구간
FILE_MIN_PCT = 0.30              # 그 사람 영상 중 이 비율 이상에 나온 단어만
FILE_MIN_LIFT = 2.0              # 전체 영상보다 이 배수 이상 자주 나온 단어만
YES = QMessageBox.StandardButton.Yes

STOP = {
    "mp4", "mkv", "mov", "avi", "ts", "wmv", "webm", "m4v", "flv", "full", "hd", "fhd", "uhd",
    "the", "and", "for", "with", "from", "official", "video", "videos", "mv", "live", "ver",
    "version", "feat", "ft", "shorts", "short", "clip", "clips", "vlog", "part", "eng", "sub",
    "kor", "jp", "jpn", "sns", "youtube", "tiktok", "instagram", "insta", "twitter", "fancam",
    "teaser", "trailer", "behind", "making", "reaction", "highlight", "special", "new", "best",
    "영상", "풀영상", "동영상", "직캠", "하이라이트", "모음", "정리", "편집", "무편집", "풀버전",
    "유튜브", "틱톡", "인스타", "쇼츠", "근황", "최신", "공식", "본편", "예고", "리액션", "레전드",
    "動画", "完全", "公式", "最新", "限定", "企画", "チャンネル", "フル", "ショート",
}
_WORD = re.compile(r"[^\W_]+")
_BRACKET = re.compile(r"[\[\(【「『（〔](.{2,30}?)[\]\)】」』）〕]")
_HASH = re.compile(r"#([^\s#\[\]\(\)]{2,30})")
_YTID = re.compile(r"[A-Za-z0-9_-]{11}")
_JUNK = re.compile(r"\d+|\d+(p|k|fps|kbps|mb|gb|bit|hz|x\d+)|s\d+(e\d+)?|ep?\d+|part\d+|v\d+"
                   r"|시즌\d+|\d+(화|회|편|부|탄|기|년|월|일|분|초)")
_PARTICLES = ("이랑", "에서", "한테", "님", "씨", "의", "와", "과", "랑", "은", "는", "을", "를", "도")
_HANGUL = re.compile(r"[가-힣]")


# ---------------- 파일명에서 단어 뽑기 ----------------
def _ok(t):
    t = t.strip()
    low = t.lower()
    if len(t) < 2 or len(t) > 30 or low in STOP or _JUNK.fullmatch(low):
        return False
    if _YTID.fullmatch(t) and (any(c.isdigit() for c in t) or
                               (sum(c.isupper() for c in t) >= 2 and sum(c.islower() for c in t) >= 2)):
        return False
    return any(c.isalpha() for c in t)


def _variants(w):
    yield w
    if _HANGUL.match(w[-1:]):
        for p in _PARTICLES:
            if w.endswith(p) and len(w) - len(p) >= 2:
                yield w[:-len(p)]
                break


def tokens(text):
    out = set()
    for m in _BRACKET.finditer(text):
        if _ok(m.group(1)):
            out.add(m.group(1).strip())
    for m in _HASH.finditer(text):
        if _ok(m.group(1)):
            out.add(m.group(1))
    for w in _WORD.findall(text):
        for v in _variants(w):
            if _ok(v):
                out.add(v)
    return out


def va_cols(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(video_actors)")]
    v = "video_id" if "video_id" in cols else next(c for c in cols if "video" in c)
    a = "actor_id" if "actor_id" in cols else next(c for c in cols if "actor" in c)
    return v, a, "source" in cols


_IX = {"key": None}


def _index(conn):
    key = (tuple(conn.execute("SELECT COUNT(*), COALESCE(MAX(id),0), "
                              "COALESCE(SUM(LENGTH(rel_path)),0) FROM videos").fetchone())
           + tuple(conn.execute("SELECT COUNT(*) FROM video_actors").fetchone())
           + tuple(conn.execute("SELECT COUNT(*) FROM actors").fetchone()))
    if _IX["key"] == key:
        return _IX
    cols = {r[1] for r in conn.execute("PRAGMA table_info(videos)")}
    tcol = "title" if "title" in cols else "''"
    vt, forms = {}, {}

    def add(vid, word, weight=1):
        low = word.lower()
        vt.setdefault(vid, set()).add(low)
        forms.setdefault(low, Counter())[word] += weight

    for vid, rel, title in conn.execute(f"SELECT id, rel_path, {tcol} FROM videos"):
        p = Path(rel or "")
        text = " ".join([p.stem, title or ""] + list(p.parent.parts[-2:]))
        for t in tokens(text):
            add(vid, t)
    v, a, has_src = va_cols(conn)
    sql = f"SELECT va.{v}, ac.name FROM video_actors va JOIN actors ac ON ac.id = va.{a}"
    if has_src:
        sql += " WHERE COALESCE(va.source, '') <> 'face'"     # 얼굴로 자동 추가한 건 제외
    for vid, name in conn.execute(sql):
        if name:
            add(vid, name, 5)
    df = Counter()
    for ks in vt.values():
        df.update(ks)
    actors = {r[0].lower() for r in conn.execute("SELECT name FROM actors") if r[0]}
    _IX.update(key=key, vt=vt, df=df, N=max(1, len(vt)), forms=forms, actors=actors)
    return _IX


def file_suggestions(conn, pid, ix=None):
    ix = ix or _index(conn)
    vids = [r[0] for r in conn.execute(
        "SELECT DISTINCT video_id FROM video_faces WHERE person_id=?", (pid,))]
    n = len(vids)
    if n < 2:
        return []
    cnt = Counter()
    for vid in vids:
        cnt.update(ix["vt"].get(vid, ()))
    res = []
    for key, k in cnt.items():
        if k < 2:
            continue
        pct = k / n
        lift = pct / (ix["df"][key] / ix["N"])
        if pct < FILE_MIN_PCT or lift < FILE_MIN_LIFT:
            continue
        res.append({"key": key, "name": ix["forms"][key].most_common(1)[0][0], "pct": pct,
                    "k": k, "n": n, "lift": lift, "actor": key in ix["actors"]})
    res.sort(key=lambda d: (d["actor"], d["pct"], d["lift"]), reverse=True)
    return res[:5]


# ---------------- 얼굴 닮음 ----------------
_SAMP = {"key": None, "sets": {}}


def named_people(conn):
    return {pid: name for pid, name in conn.execute(
        "SELECT p.id, COALESCE(a.name, p.name) FROM face_people p "
        "LEFT JOIN actors a ON a.id = p.actor_id "
        "WHERE p.hidden = 0 AND COALESCE(a.name, p.name, '') <> ''")}


def _ctx(conn):
    key = (tuple(conn.execute("SELECT COUNT(*), COALESCE(SUM(person_id),0), COALESCE(MAX(id),0) "
                              "FROM video_faces").fetchone())
           + tuple(conn.execute("SELECT COUNT(*), COALESCE(SUM(person_id),0) "
                                "FROM person_samples").fetchone()))
    if _SAMP["key"] != key:
        _SAMP.update(key=key, sets=fc.load_samples(conn))
    named = named_people(conn)
    return {"sets": _SAMP["sets"], "named": named,
            "by_name": {n.lower(): p for p, n in named.items()}, "ix": _index(conn)}


def cross(A, B):
    best = (A @ B.T).max(axis=1)
    k = min(fc.TOPK, len(best))
    return float(np.sort(best)[-k:].mean())


def rejected(conn, pid):
    return {r[0] for r in conn.execute("SELECT name FROM face_name_rejects WHERE person_id=?", (pid,))}


def reject(conn, pid, name):
    conn.execute("INSERT OR IGNORE INTO face_name_rejects(person_id, name) VALUES (?,?)",
                 (pid, name.lower()))
    conn.commit()


def suggest(conn, pid, ctx=None):
    """이 사람의 이름 후보 → [{name, p, face, file, k, n, target}] (가능성 높은 순)"""
    ctx = ctx or _ctx(conn)
    res = {}
    mine = ctx["sets"].get(pid)
    if mine is not None:
        for npid, name in ctx["named"].items():
            other = ctx["sets"].get(npid)
            if npid == pid or other is None:
                continue
            sc = cross(mine, other)
            pn = float(np.clip((sc - FACE_LO) / (FACE_HI - FACE_LO), 0, 1))
            key = name.lower()
            if pn > 0 and pn > res.get(key, {}).get("face", 0):
                res[key] = {"name": name, "face": pn, "file": 0.0, "k": 0, "n": 0, "target": npid}
    for d in file_suggestions(conn, pid, ctx["ix"]):
        r = res.setdefault(d["key"], {"name": d["name"], "face": 0.0, "file": 0.0, "k": 0, "n": 0,
                                      "target": ctx["by_name"].get(d["key"])})
        if d["pct"] > r["file"]:
            r.update(file=d["pct"], k=d["k"], n=d["n"])
    rej = rejected(conn, pid)
    out = []
    for key, r in res.items():
        if key in rej:
            continue
        r["p"] = 1 - (1 - r["face"]) * (1 - r["file"])
        out.append(r)
    out.sort(key=lambda r: -r["p"])
    return out


def reason(r):
    parts = []
    if r["face"] > 0:
        parts.append(f"얼굴 닮음 {r['face'] * 100:.0f}%")
    if r["file"] > 0:
        parts.append(f"파일명 일치 {r['file'] * 100:.0f}% ({r['n']}개 중 {r['k']}개)")
    return " · ".join(parts)


def build_queue(conn, limit=500):
    ctx = _ctx(conn)
    rows = conn.execute("""SELECT p.id FROM face_people p JOIN video_faces f ON f.person_id = p.id
        WHERE p.hidden = 0 AND p.actor_id IS NULL AND COALESCE(p.name, '') = ''
        GROUP BY p.id ORDER BY COUNT(DISTINCT f.video_id) DESC""").fetchall()
    q = []
    for (pid,) in rows:
        s = suggest(conn, pid, ctx)
        if s and s[0]["p"] >= QUEUE_MIN:
            q.append((s[0]["p"], pid, s[0]))
    q.sort(key=lambda x: -x[0])
    return [(pid, r) for _p, pid, r in q[:limit]]


def thumbs(conn, pid, limit=8):
    paths = [r[0] for r in conn.execute(
        "SELECT thumb FROM video_faces WHERE person_id=? AND COALESCE(thumb,'') <> '' "
        "ORDER BY hits * score DESC LIMIT ?", (pid, limit))]
    if len(paths) < limit:
        paths += [r[0] for r in conn.execute(
            "SELECT thumb FROM person_samples WHERE person_id=? AND COALESCE(thumb,'') <> '' "
            "ORDER BY id DESC LIMIT ?", (pid, limit - len(paths)))]
    return paths


# ---------------- 확인 대기 창 ----------------
class ReviewDialog(QDialog):
    def __init__(self, window, parent, queue):
        super().__init__(parent)
        self.main = window
        self.conn = window.conn
        self.queue = queue
        self.i = -1
        self.yes_n = self.no_n = 0
        self.dirty = False
        self.cur = None
        self.setWindowTitle("✅ 확인 대기 - 같은 사람인가요?")
        self.resize(980, 600)

        self.progress = QLabel()
        self.progress.setStyleSheet("font-size:14px; color:#aaa;")
        self.left_title, self.right_title = QLabel(), QLabel()
        for lb in (self.left_title, self.right_title):
            lb.setStyleSheet("font-size:16px;")
        self.left, self.right = self._grid(), self._grid()
        arrow = QLabel("→")
        arrow.setStyleSheet("font-size:40px; color:#7cc4ff;")
        arrow.setAlignment(Qt.AlignCenter)
        lv, rv = QVBoxLayout(), QVBoxLayout()
        lv.addWidget(self.left_title)
        lv.addWidget(self.left, 1)
        rv.addWidget(self.right_title)
        rv.addWidget(self.right, 1)
        panes = QHBoxLayout()
        panes.addLayout(lv, 1)
        panes.addWidget(arrow)
        panes.addLayout(rv, 1)

        self.reason = QLabel()
        self.reason.setStyleSheet("font-size:15px; color:#ffd54f;")
        self.videos = QLabel()
        self.videos.setWordWrap(True)
        self.videos.setStyleSheet("color:#999;")

        bts = QHBoxLayout()
        spec = (("✅ 맞음 (Y / Enter)", self.yes), ("❌ 아님 (N)", self.no),
                ("⏭ 건너뛰기 (S)", self.skip), ("✏ 다른 이름 (E)", self.other),
                ("🙈 숨기기 (H)", self.hide_person))
        for i, (text, fn) in enumerate(spec):
            b = QPushButton(text)
            b.setFocusPolicy(Qt.NoFocus)
            b.setAutoDefault(False)
            b.clicked.connect(fn)
            if i == 0:
                b.setDefault(True)
                b.setStyleSheet("font-weight:bold; padding:6px 14px;")
            bts.addWidget(b)
        bts.addStretch(1)
        close = QPushButton("닫기 (Esc)")
        close.setFocusPolicy(Qt.NoFocus)
        close.setAutoDefault(False)
        close.clicked.connect(self.reject)
        bts.addWidget(close)

        lay = QVBoxLayout(self)
        lay.addWidget(self.progress)
        lay.addLayout(panes, 1)
        lay.addWidget(self.reason)
        lay.addWidget(self.videos)
        lay.addLayout(bts)

        for key, fn in (("Y", self.yes), ("N", self.no), ("S", self.skip), ("Space", self.skip),
                        ("E", self.other), ("H", self.hide_person)):
            QShortcut(QKeySequence(key), self).activated.connect(fn)
        self.next()

    def _grid(self):
        lw = QListWidget()
        lw.setViewMode(QListView.ViewMode.IconMode)
        lw.setIconSize(QSize(96, 96))
        lw.setGridSize(QSize(104, 104))
        lw.setMovement(QListView.Movement.Static)
        lw.setResizeMode(QListView.ResizeMode.Adjust)
        lw.setFocusPolicy(Qt.NoFocus)
        lw.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        lw.setMinimumHeight(230)
        return lw

    def _fill(self, lw, paths, empty=""):
        lw.clear()
        for p in paths:
            lw.addItem(QListWidgetItem(QIcon(QPixmap(p)), ""))
        if not paths and empty:
            lw.addItem(QListWidgetItem(empty))

    def _valid(self, pid):
        row = self.conn.execute("SELECT actor_id, name, hidden FROM face_people WHERE id=?",
                                (pid,)).fetchone()
        return row is not None and row[0] is None and not (row[1] or "") and not row[2]

    def next(self):
        self.i += 1
        while self.i < len(self.queue) and not self._valid(self.queue[self.i][0]):
            self.i += 1
        if self.i >= len(self.queue):
            self._finish()
            return
        pid, r = self.queue[self.i]
        self.cur = (pid, r)
        named = named_people(self.conn)
        target = r.get("target")
        if target not in named:
            target = {n.lower(): p for p, n in named.items()}.get(r["name"].lower())
        vids = [x[0] for x in self.conn.execute(
            "SELECT DISTINCT v.rel_path FROM video_faces f JOIN videos v ON v.id = f.video_id "
            "WHERE f.person_id=?", (pid,))]
        self.progress.setText(f"{self.i + 1} / {len(self.queue)}      "
                              f"맞음 {self.yes_n} · 아님 {self.no_n}")
        self.left_title.setText(f"<b>P{pid}</b>  (영상 {len(vids)}개)")
        self._fill(self.left, thumbs(self.conn, pid))
        name = html.escape(r["name"])
        if target:
            self.right_title.setText(f"<b>'{name}'</b> 맞나요?")
            self._fill(self.right, thumbs(self.conn, target), "얼굴 샘플 없음")
        else:
            self.right_title.setText(f"<b>'{name}'</b> (새 이름) 맞나요?")
            self._fill(self.right, [], "아직 이 이름의 얼굴이 없습니다.\n맞으면 이 이름으로 등록됩니다.")
        self.reason.setText("💡 " + reason(r))
        names = [Path(v or "").stem for v in vids[:4]]
        self.videos.setText("나온 영상: " + "  /  ".join(names) + (" …" if len(vids) > 4 else ""))

    def _finish(self):
        if self.yes_n and QMessageBox.question(
                self, "확인 대기",
                f"이번 확인: 맞음 {self.yes_n} · 아님 {self.no_n}\n\n"
                "이름 붙은 얼굴이 늘어서 새 후보가 생겼을 수 있습니다.\n다시 찾아볼까요?") == YES:
            self.queue = build_queue(self.conn)
            self.i, self.yes_n, self.no_n = -1, 0, 0
            if self.queue:
                self.next()
                return
        QMessageBox.information(self, "확인 대기", "확인할 후보를 모두 봤습니다.")
        self.accept()

    def _name(self, name):
        from app import face_ui
        pid, _r = self.cur
        face_ui.name_person(self.conn, pid, name)
        self.yes_n += 1
        self.dirty = True
        self.next()

    def yes(self):
        if self.cur:
            self._name(self.cur[1]["name"])

    def no(self):
        if not self.cur:
            return
        pid, r = self.cur
        reject(self.conn, pid, r["name"])
        self.no_n += 1
        s = suggest(self.conn, pid)          # 다음 후보가 있으면 같은 사람을 한 번 더
        if s and s[0]["p"] >= QUEUE_MIN:
            self.queue[self.i] = (pid, s[0])
            self.i -= 1
        self.next()

    def skip(self):
        self.next()

    def other(self):
        if not self.cur:
            return
        names = [r[0] for r in self.conn.execute("SELECT name FROM actors ORDER BY name")]
        cur = self.cur[1]["name"]
        text, ok = QInputDialog.getItem(self, "다른 이름", "이 사람의 이름:",
                                        [cur] + [n for n in names if n != cur], 0, True)
        if ok and text.strip():
            self._name(text.strip())

    def hide_person(self):
        if not self.cur:
            return
        self.conn.execute("UPDATE face_people SET hidden=1 WHERE id=?", (self.cur[0],))
        self.conn.commit()
        self.next()

    def done(self, r):
        if self.dirty:
            from app import face_ui
            face_ui._refresh_main(self.main)
        super().done(r)
