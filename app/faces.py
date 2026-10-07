"""얼굴 인식 엔진 v2: 흐린·옆얼굴 제외, 여러 샘플 비교, 이름 붙인 사람 유지
  python -m app.faces --check                 GPU·모델 확인
  python -m app.faces "C:\\yt-dlp"             폴더 안 영상 분석 (멈춰도 이어서 함)
  python -m app.faces --report                묶인 결과 미리보기 이미지
  python -m app.faces --recluster             묶기만 다시 하기 (이름 붙인 사람은 유지)
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

from app import config

FACE_VER = 2           # 분석 방식 버전 (바뀌면 영상을 다시 분석)

# ---------------- 조정 가능한 값 ----------------
DET_THRESH = 0.50      # 얼굴로 인정할 최소 확신도 (못 찾으면 ↓)
MIN_FACE = 32          # 이보다 작은 얼굴(픽셀)은 무시
BLUR_MIN = 20.0        # 이보다 흐린 얼굴은 비교에 안 씀
YAW_MAX = 0.60         # 이보다 옆으로 돌린 얼굴은 비교에 안 씀
SAME_IN_VIDEO = 0.45   # 한 영상 안에서 같은 사람으로 볼 유사도
SAME_PERSON = 0.45     # 영상끼리 자동으로 묶을 유사도 (섞이면 ↑, 쪼개지면 ↓)
NAMED_AUTO = 0.50      # 이름 붙인 사람에게 자동으로 붙일 유사도 (더 엄격)
TOPK = 3               # 가장 닮은 샘플 몇 개의 평균으로 비교할지
SAMPLE_CAP = 30        # 사람마다 비교에 쓸 최대 샘플 수
ANCHOR_MIN = 0.40      # 예전 결과에서 이름 붙인 사람의 샘플로 남길 기준
MIN_FRAMES, MAX_FRAMES, SEC_PER_FRAME = 8, 30, 20
GRAB_WORKERS = 4
DET_SIZE = 800
DIM = 512

DATA_DIR = Path(config.DATA_DIR)
BASE_DIR = Path(getattr(config, "BASE_DIR", DATA_DIR.parent))
MODEL_DIR = BASE_DIR / "models"
FACE_DIR = DATA_DIR / "faces"
FACE_DIR.mkdir(parents=True, exist_ok=True)
VIDEO_EXTS = {(e if str(e).startswith(".") else "." + str(e)).lower()
              for e in getattr(config, "VIDEO_EXTS", [])}
SKIP_DIRS = {"System Volume Information", "$RECYCLE.BIN", "$Recycle.Bin"}
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ARC_DST = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                    [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)


# ---------------- 이미지 도우미 (한글 경로 안전) ----------------
def _save_jpg(path, img):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if ok:
        buf.tofile(str(path))
        return str(path)
    return ""


def _read_img(path):
    try:
        return cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None


def _unlink(path):
    try:
        if path:
            Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _crop(img, box, size=160):
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    half = max(x2 - x1, y2 - y1) * 0.7
    h, w = img.shape[:2]
    a, b = int(max(0, cx - half)), int(max(0, cy - half))
    c, d = int(min(w, cx + half)), int(min(h, cy + half))
    if c - a < 4 or d - b < 4:
        return None
    return cv2.resize(img[b:d, a:c], (size, size))


# ---------------- 얼굴 엔진 ----------------
def _model(name):
    found = list(MODEL_DIR.rglob(name))
    if not found:
        raise FileNotFoundError(f"모델 파일이 없습니다: {MODEL_DIR}\\{name}")
    return str(found[0])


class FaceEngine:
    def __init__(self):
        import onnxruntime as ort
        if hasattr(ort, "preload_dlls"):
            try:
                ort.preload_dlls()
            except Exception as e:
                print("[GPU] DLL 미리 불러오기 실패:", e)
        avail = ort.get_available_providers()
        prov = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in avail]
        so = ort.SessionOptions()
        so.log_severity_level = 3
        self.det = ort.InferenceSession(_model("det_10g.onnx"), so, providers=prov)
        self.rec = ort.InferenceSession(_model("w600k_r50.onnx"), so, providers=prov)
        self.device = "GPU" if self.det.get_providers()[0] == "CUDAExecutionProvider" else "CPU"
        self.det_in = self.det.get_inputs()[0].name
        self.det_out = [o.name for o in self.det.get_outputs()]
        self.rec_in = self.rec.get_inputs()[0].name
        shp = self.det.get_inputs()[0].shape
        self.size = shp[2] if isinstance(shp[2], int) and shp[2] > 0 else DET_SIZE
        self.version = ort.__version__
        self._centers = {}

    def _anchors(self, h, w, stride, na):
        key = (h, w, stride, na)
        if key not in self._centers:
            c = np.stack(np.mgrid[:h, :w][::-1], axis=-1).astype(np.float32)
            c = (c * stride).reshape(-1, 2)
            self._centers[key] = np.repeat(c, na, axis=0) if na > 1 else c
        return self._centers[key]

    def detect(self, img):
        S = self.size
        ih, iw = img.shape[:2]
        if ih > iw:
            nh, nw = S, max(1, int(S * iw / ih))
        else:
            nw, nh = S, max(1, int(S * ih / iw))
        scale = nh / ih
        canvas = np.zeros((S, S, 3), np.uint8)
        canvas[:nh, :nw] = cv2.resize(img, (nw, nh))
        blob = cv2.dnn.blobFromImage(canvas, 1 / 128, (S, S), (127.5, 127.5, 127.5), swapRB=True)
        outs = self.det.run(self.det_out, {self.det_in: blob})
        outs = [o[0] if o.ndim == 3 else o for o in outs]
        fmc = len(outs) // 3
        boxes, scores, kpss = [], [], []
        for i, s in enumerate([8, 16, 32, 64, 128][:fmc]):
            sc = outs[i].reshape(-1)
            bb = outs[i + fmc].reshape(-1, 4) * s
            kp = outs[i + 2 * fmc].reshape(-1, 5, 2) * s
            h = w = S // s
            c = self._anchors(h, w, s, max(1, sc.shape[0] // (h * w)))
            keep = np.where(sc >= DET_THRESH)[0]
            if not len(keep):
                continue
            c, bb, kp = c[keep], bb[keep], kp[keep]
            boxes.append(np.stack([c[:, 0] - bb[:, 0], c[:, 1] - bb[:, 1],
                                   c[:, 0] + bb[:, 2], c[:, 1] + bb[:, 3]], 1))
            kpss.append(kp + c[:, None, :])
            scores.append(sc[keep])
        if not boxes:
            return []
        boxes = np.vstack(boxes) / scale
        kpss = np.vstack(kpss) / scale
        scores = np.concatenate(scores)
        xywh = [[float(b[0]), float(b[1]), float(b[2] - b[0]), float(b[3] - b[1])] for b in boxes]
        idx = np.array(cv2.dnn.NMSBoxes(xywh, scores.tolist(), DET_THRESH, 0.4)).reshape(-1)
        out = []
        for k in idx:
            b = boxes[k]
            if min(b[2] - b[0], b[3] - b[1]) >= MIN_FACE:
                out.append((b, float(scores[k]), kpss[k]))
        return out

    def analyze(self, img):
        aligned, info = [], []
        for box, score, kps in self.detect(img):
            le, rei, nose = kps[0], kps[1], kps[2]
            v = rei - le
            ed = float(np.linalg.norm(v))
            if ed < 2:
                continue
            yaw = abs(float(np.dot(nose - (le + rei) / 2, v / ed))) / ed   # 옆얼굴 정도
            if yaw > YAW_MAX:
                continue
            M, _ = cv2.estimateAffinePartial2D(kps.astype(np.float32), ARC_DST, method=cv2.LMEDS)
            if M is None:
                continue
            al = cv2.warpAffine(img, M, (112, 112), borderValue=0)
            blur = float(cv2.Laplacian(cv2.cvtColor(al, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
            if blur < BLUR_MIN:
                continue
            crop = _crop(img, box)
            if crop is None:
                continue
            size = float(min(box[2] - box[0], box[3] - box[1]))
            w = score * min(1.0, blur / 80) * (1 - 0.5 * yaw / YAW_MAX)
            aligned.append(al)
            info.append({"score": score, "size": size, "crop": crop, "w": max(w, 1e-3),
                         "q": w * min(size, 200)})
        if not aligned:
            return []
        blob = cv2.dnn.blobFromImages(aligned, 1 / 127.5, (112, 112),
                                      (127.5, 127.5, 127.5), swapRB=True)
        try:
            feats = self.rec.run(None, {self.rec_in: blob})[0]
        except Exception:
            feats = np.vstack([self.rec.run(None, {self.rec_in: blob[i:i + 1]})[0]
                               for i in range(len(blob))])
        feats = feats / np.clip(np.linalg.norm(feats, axis=1, keepdims=True), 1e-6, None)
        for d, f in zip(info, feats):
            d["emb"] = f.astype(np.float32)
        return info


# ---------------- 영상에서 장면 뽑기 ----------------
def probe_duration(path):
    try:
        r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30, creationflags=NO_WINDOW)
        return float(r.stdout.strip().splitlines()[0])
    except Exception:
        return 0.0


def _run_grab(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60, creationflags=NO_WINDOW)
    except Exception:
        return None
    if not r.stdout:
        return None
    return cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)


def grab(path, t, maxw=1280):
    """키프레임만 읽어서 빠르게 (실패하면 정확한 방식으로 다시)"""
    head = [FFMPEG, "-hide_banner", "-loglevel", "error"]
    tail = ["-i", path, "-an", "-sn", "-frames:v", "1", "-vf", f"scale='min({maxw},iw)':-2",
            "-f", "image2pipe", "-c:v", "bmp", "-"]
    img = _run_grab(head + ["-skip_frame", "nokey", "-noaccurate_seek", "-ss", f"{t:.2f}"] + tail)
    if img is None:
        img = _run_grab(head + ["-ss", f"{t:.2f}"] + tail)
    return img


def sample_times(dur):
    if not dur or dur <= 0:
        return [0.0]
    n = int(min(MAX_FRAMES, max(MIN_FRAMES, dur // SEC_PER_FRAME)))
    return [dur * (0.04 + 0.92 * (k + 0.5) / n) for k in range(n)]


# ---------------- DB ----------------
def open_db():
    from app import db
    try:
        return db.connect()
    except TypeError:
        return db.connect(config.DB_PATH)


def ensure_tables(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS face_people(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT DEFAULT '', actor_id INTEGER,
        centroid BLOB, n INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0, created REAL);
    CREATE TABLE IF NOT EXISTS video_faces(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        person_id INTEGER REFERENCES face_people(id) ON DELETE SET NULL,
        emb BLOB NOT NULL, hits INTEGER, score REAL, t REAL, thumb TEXT);
    CREATE INDEX IF NOT EXISTS idx_vf_video ON video_faces(video_id);
    CREATE INDEX IF NOT EXISTS idx_vf_person ON video_faces(person_id);
    CREATE TABLE IF NOT EXISTS face_scan(
        video_id INTEGER PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
        done REAL, frames INTEGER, faces INTEGER, status TEXT, ver INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS person_samples(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        person_id INTEGER NOT NULL REFERENCES face_people(id) ON DELETE CASCADE,
        face_id INTEGER, emb BLOB NOT NULL, thumb TEXT DEFAULT '');
    CREATE INDEX IF NOT EXISTS idx_ps_person ON person_samples(person_id);
    CREATE TABLE IF NOT EXISTS face_name_rejects(
        person_id INTEGER, name TEXT, PRIMARY KEY(person_id, name));
    CREATE TABLE IF NOT EXISTS face_meta(key TEXT PRIMARY KEY, value TEXT);
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(face_scan)")}
    if "ver" not in cols:
        conn.execute("ALTER TABLE face_scan ADD COLUMN ver INTEGER DEFAULT 1")
    conn.commit()
    _migrate(conn)


def done_ids(conn):
    return {r[0] for r in conn.execute(
        "SELECT video_id FROM face_scan WHERE COALESCE(ver, 1) >= ?", (FACE_VER,))}


def named_ids(conn):
    return {r[0] for r in conn.execute(
        "SELECT id FROM face_people WHERE actor_id IS NOT NULL OR COALESCE(name, '') <> ''")}


def find_videos(conn, folder):
    """폴더 안 실제 파일 ↔ DB 영상 연결 → [(id, 경로, 길이)]"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(videos)")}
    dcol = "duration" if "duration" in cols else "0"
    by_name = {}
    ex = " WHERE IFNULL(excluded, 0) = 0" if "excluded" in cols else ""
    for vid, rel, dur in conn.execute(f"SELECT id, rel_path, {dcol} FROM videos{ex}"):

        if rel:
            key = os.path.normcase(os.path.normpath(rel)).lstrip("\\/")
            by_name.setdefault(os.path.basename(key), []).append((key, vid, dur or 0))
    out, unknown = [], 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if Path(f).suffix.lower() not in VIDEO_EXTS:
                continue
            full = os.path.join(root, f)
            norm = os.path.normcase(os.path.normpath(full))
            hit = next(((vid, dur) for key, vid, dur in by_name.get(os.path.basename(norm), [])
                        if norm.endswith(key)), None)
            if hit:
                out.append((hit[0], full, float(hit[1] or 0)))
            else:
                unknown += 1
    if unknown:
        print(f"⚠ DB에 없는 영상 {unknown}개는 건너뜀 (프로그램에서 폴더 등록·스캔 먼저)")
    return out


# ---------------- 사람별 샘플 ----------------
def _consistent(E):
    """서로 잘 맞는 얼굴만 True (섞인 묶음에서 다른 사람 걸러내기)"""
    if len(E) <= 2:
        return np.ones(len(E), bool)
    M = E @ E.T
    score = (M.sum(1) - 1) / (len(E) - 1)
    top = np.argsort(-score)[:max(2, len(E) // 2)]
    c = E[top].sum(0)
    c = c / max(np.linalg.norm(c), 1e-6)
    return (E @ c) >= ANCHOR_MIN


def snapshot_anchors(conn, pid, strict=False):
    """이름 붙인 사람의 얼굴을 고정 샘플로 보관 (다시 분석해도 이름 유지)"""
    rows = conn.execute("SELECT id, emb, thumb FROM video_faces WHERE person_id=? "
                        "ORDER BY hits * score DESC LIMIT ?", (pid, SAMPLE_CAP * 2)).fetchall()
    if not rows:
        return 0
    E = np.vstack([np.frombuffer(r[1], np.float32) for r in rows])
    keep = _consistent(E) if strict else np.ones(len(rows), bool)
    have = {r[0] for r in conn.execute("SELECT face_id FROM person_samples WHERE person_id=?", (pid,))}
    n = 0
    for (fid, emb, th), ok in zip(rows, keep):
        if not ok or fid in have:
            continue
        dst = ""
        if th and os.path.isfile(th):
            dst = str(FACE_DIR / f"anchor_{fid}.jpg")
            try:
                shutil.copy2(th, dst)
            except OSError:
                dst = ""
        conn.execute("INSERT INTO person_samples(person_id, face_id, emb, thumb) VALUES (?,?,?,?)",
                     (pid, fid, emb, dst))
        n += 1
    old = conn.execute("SELECT id, thumb FROM person_samples WHERE person_id=? "
                       "ORDER BY id DESC LIMIT -1 OFFSET ?", (pid, SAMPLE_CAP * 2)).fetchall()
    for sid, th in old:
        _unlink(th)
        conn.execute("DELETE FROM person_samples WHERE id=?", (sid,))
    return n


def load_samples(conn):
    per = {}
    for pid, emb, q in conn.execute("SELECT person_id, emb, COALESCE(hits,1) * COALESCE(score,0.5) "
                                    "FROM video_faces WHERE person_id IS NOT NULL"):
        per.setdefault(pid, []).append((q, emb))
    for pid, emb in conn.execute("SELECT person_id, emb FROM person_samples"):
        per.setdefault(pid, []).append((1e9, emb))
    out = {}
    for pid, lst in per.items():
        lst.sort(key=lambda x: -x[0])
        out[pid] = np.vstack([np.frombuffer(e, np.float32) for _, e in lst[:SAMPLE_CAP]])
    return out


def recompute(conn, pid):
    embs = [np.frombuffer(e, np.float32) for (e,) in conn.execute(
        "SELECT emb FROM video_faces WHERE person_id=? UNION ALL "
        "SELECT emb FROM person_samples WHERE person_id=?", (pid, pid))]
    if not embs:
        conn.execute("DELETE FROM face_people WHERE id=? AND actor_id IS NULL "
                     "AND COALESCE(name, '') = ''", (pid,))
        return
    s = np.sum(embs, axis=0)
    s = s / max(np.linalg.norm(s), 1e-6)
    conn.execute("UPDATE face_people SET centroid=?, n=? WHERE id=?",
                 (s.astype(np.float32).tobytes(), len(embs), pid))


def cleanup_empty(conn):
    conn.execute("""DELETE FROM face_people WHERE actor_id IS NULL AND COALESCE(name, '') = ''
        AND id NOT IN (SELECT person_id FROM video_faces WHERE person_id IS NOT NULL)
        AND id NOT IN (SELECT person_id FROM person_samples)""")


def _migrate(conn):
    row = conn.execute("SELECT value FROM face_meta WHERE key='ver'").fetchone()
    if row and int(row[0]) >= FACE_VER:
        return
    named = named_ids(conn)
    for pid in named:
        snapshot_anchors(conn, pid, strict=True)
    had = conn.execute("SELECT COUNT(*) FROM video_faces").fetchone()[0]
    for (th,) in conn.execute("SELECT thumb FROM video_faces").fetchall():
        _unlink(th)
    conn.execute("DELETE FROM video_faces")
    conn.execute("DELETE FROM face_scan")
    conn.execute("DELETE FROM face_name_rejects")
    cleanup_empty(conn)
    for pid in named:
        recompute(conn, pid)
    conn.execute("INSERT OR REPLACE INTO face_meta(key, value) VALUES ('ver', ?)", (str(FACE_VER),))
    conn.commit()
    if had:
        print(f"[얼굴] 인식 방식 v{FACE_VER}로 업그레이드: 이름 붙인 {len(named)}명은 유지, "
              "영상은 다시 분석합니다")


# ---------------- 영상 하나 처리 ----------------
def group_faces(found, many):
    groups = []
    for f in sorted(found, key=lambda x: -x["q"]):
        best, bs = None, SAME_IN_VIDEO
        for g in groups:
            s = float(g["emb"] @ f["emb"])
            if s > bs:
                best, bs = g, s
        if best:
            best["sum"] += f["emb"] * f["w"]
            best["hits"] += 1
            best["emb"] = best["sum"] / np.linalg.norm(best["sum"])
        else:
            groups.append({"sum": f["emb"] * f["w"], "emb": f["emb"], "hits": 1, "best": f})
    if many:   # 장면이 많은데 한 번만 스친 얼굴은 또렷할 때만 남김
        groups = [g for g in groups if g["hits"] >= 2
                  or (g["best"]["score"] >= 0.7 and g["best"]["size"] >= 60)]
    return groups


def process_video(eng, conn, vid, path, dur):
    if not dur:
        dur = probe_duration(path)
    times = sample_times(dur)
    with ThreadPoolExecutor(GRAB_WORKERS) as ex:
        frames = list(ex.map(lambda t: (t, grab(path, t)), times))
    found, ok = [], 0
    for t, img in frames:
        if img is None:
            continue
        ok += 1
        for f in eng.analyze(img):
            f["t"] = t
            found.append(f)
    groups = group_faces(found, many=ok >= 8)
    for (th,) in conn.execute("SELECT thumb FROM video_faces WHERE video_id=?", (vid,)).fetchall():
        _unlink(th)
    conn.execute("DELETE FROM video_faces WHERE video_id=?", (vid,))
    for g in groups:
        b = g["best"]
        fid = conn.execute("INSERT INTO video_faces(video_id, emb, hits, score, t) VALUES (?,?,?,?,?)",
                           (vid, g["emb"].astype(np.float32).tobytes(), g["hits"],
                            b["score"], b["t"])).lastrowid
        conn.execute("UPDATE video_faces SET thumb=? WHERE id=?",
                     (_save_jpg(FACE_DIR / f"vf{fid}.jpg", b["crop"]), fid))
    conn.execute("INSERT OR REPLACE INTO face_scan(video_id, done, frames, faces, status, ver) "
                 "VALUES (?,?,?,?,?,?)", (vid, time.time(), ok, len(groups),
                                          "ok" if ok else "noframe", FACE_VER))
    conn.commit()
    return ok, len(groups)


# ---------------- 영상끼리 같은 사람 묶기 ----------------
def cluster(conn):
    named = named_ids(conn)
    sets = load_samples(conn)
    total = sum(len(a) for a in sets.values())
    cap = max(1024, total * 2)
    S = np.zeros((cap, DIM), np.float32)
    O = np.full(cap, -1, np.int64)
    m, count = 0, {}
    for pid, arr in sets.items():
        S[m:m + len(arr)] = arr
        O[m:m + len(arr)] = pid
        m += len(arr)
        count[pid] = len(arr)
    todo = conn.execute("SELECT id, emb FROM video_faces WHERE person_id IS NULL "
                        "ORDER BY hits * score DESC").fetchall()
    touched, new = set(), 0
    low = min(SAME_PERSON, NAMED_AUTO) - 0.05
    for fid, emb in todo:
        e = np.frombuffer(emb, np.float32)
        pid, best = None, 0.0
        if m:
            sims = S[:m] @ e
            owners = O[:m]
            for o in np.unique(owners[sims >= low]):
                o = int(o)
                sc = float(np.sort(sims[owners == o])[-TOPK:].mean())
                need = NAMED_AUTO if o in named else SAME_PERSON
                if sc >= need and sc > best:
                    pid, best = o, sc
        if pid is None:
            pid = conn.execute("INSERT INTO face_people(n, created) VALUES (0, ?)",
                               (time.time(),)).lastrowid
            new += 1
        conn.execute("UPDATE video_faces SET person_id=? WHERE id=?", (pid, fid))
        touched.add(pid)
        if count.get(pid, 0) < SAMPLE_CAP:
            if m == cap:
                S = np.vstack([S, np.zeros_like(S)])
                O = np.concatenate([O, np.full(cap, -1, np.int64)])
                cap *= 2
            S[m], O[m] = e, pid
            m += 1
            count[pid] = count.get(pid, 0) + 1
    for pid in touched:
        recompute(conn, pid)
    cleanup_empty(conn)
    conn.commit()
    return len(todo), new


def recluster(conn):
    named = named_ids(conn)
    for pid in named:
        snapshot_anchors(conn, pid, strict=False)
    conn.execute("UPDATE video_faces SET person_id=NULL")
    conn.execute("DELETE FROM face_name_rejects")
    cleanup_empty(conn)
    conn.commit()
    n, new = cluster(conn)
    print(f"다시 묶기 완료: 얼굴 {n}개 → 새 사람 {new}명 + 이름 붙인 {len(named)}명 "
          f"(기준 {SAME_PERSON} / 이름 붙인 사람 {NAMED_AUTO})")


# ---------------- 실행 ----------------
def check():
    eng = FaceEngine()
    import onnxruntime as ort
    print("onnxruntime", eng.version, "| 사용 가능:", ort.get_available_providers())
    dummy = np.zeros((720, 1280, 3), np.uint8)
    eng.analyze(dummy)
    t0 = time.time()
    for _ in range(10):
        eng.detect(dummy)
    ms = (time.time() - t0) * 100
    print(f"{'✅ 준비 완료: GPU' if eng.device == 'GPU' else '⚠ CPU'} 사용 "
          f"(얼굴 찾기 크기 {eng.size}, 장면당 {ms:.0f}ms)")


def scan(folder, limit=None, redo=False):
    if not os.path.isdir(folder):
        print("폴더를 찾을 수 없습니다:", folder)
        return
    conn = open_db()
    ensure_tables(conn)
    eng = FaceEngine()
    print(f"얼굴 엔진 준비: {eng.device}")
    vids = find_videos(conn, folder)
    done = done_ids(conn)
    todo = vids if redo else [v for v in vids if v[0] not in done]
    if limit:
        todo = todo[:limit]
    print(f"영상 {len(vids)}개 중 분석할 영상 {len(todo)}개 (이미 끝난 {len(vids) - len(todo)}개 건너뜀)")
    t0, faces, errors, i = time.time(), 0, 0, 0
    try:
        for i, (vid, path, dur) in enumerate(todo, 1):
            try:
                _, n = process_video(eng, conn, vid, path, dur)
                faces += n
            except Exception as e:
                errors += 1
                print(f"\n[오류] {Path(path).name}: {e}")
            el = time.time() - t0
            left = el / i * (len(todo) - i)
            print(f"\r  {i}/{len(todo)}  얼굴 {faces}명분  평균 {el / i:.1f}초/개  "
                  f"남은 시간 약 {int(left // 60)}분 {int(left % 60)}초   ", end="", flush=True)
    except KeyboardInterrupt:
        print("\n⏸ 중단함. 다음에 같은 명령을 실행하면 이어서 합니다.")
    print()
    n, new = cluster(conn)
    print(f"===== 완료: 영상 {i}개, 얼굴 {faces}개, 오류 {errors}개, "
          f"{time.time() - t0:.0f}초 | 새 사람 {new}명 =====")
    conn.close()


def report(top=40, per=8):
    conn = open_db()
    ensure_tables(conn)
    nv = conn.execute("SELECT COUNT(*) FROM face_scan").fetchone()[0]
    nf = conn.execute("SELECT COUNT(*) FROM video_faces").fetchone()[0]
    rows = conn.execute("""SELECT p.id, COUNT(DISTINCT f.video_id) AS cnt FROM face_people p
                           JOIN video_faces f ON f.person_id = p.id
                           GROUP BY p.id HAVING cnt >= 2 ORDER BY cnt DESC LIMIT ?""",
                        (top,)).fetchall()
    print(f"분석한 영상 {nv}개 | 찾은 얼굴 {nf}개 | 영상 2개 이상에 나온 사람 {len(rows)}명")
    if not rows:
        return
    tile, label = 96, 150
    lines = []
    for pid, cnt in rows:
        line = np.full((tile, label + tile * per, 3), 30, np.uint8)
        cv2.putText(line, f"P{pid}", (8, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
        cv2.putText(line, f"{cnt} videos", (8, 75), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (180, 180, 180), 1)
        thumbs = conn.execute("SELECT thumb, MAX(score) FROM video_faces WHERE person_id=? "
                              "GROUP BY video_id ORDER BY MAX(score) DESC LIMIT ?",
                              (pid, per)).fetchall()
        for k, (th, _s) in enumerate(thumbs):
            img = _read_img(th) if th else None
            if img is not None:
                line[:, label + k * tile: label + (k + 1) * tile] = cv2.resize(img, (tile, tile))
        lines.append(line)
        lines.append(np.full((4, line.shape[1], 3), 60, np.uint8))
    out = FACE_DIR / "people_preview.jpg"
    _save_jpg(out, np.vstack(lines))
    print("미리보기 이미지:", out)
    os.startfile(str(out))
    conn.close()


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", nargs="?")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--recluster", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--redo", action="store_true")
    a = ap.parse_args()
    if a.check:
        check()
    elif a.report:
        report()
    elif a.recluster:
        c = open_db()
        ensure_tables(c)
        recluster(c)
    elif a.folder:
        scan(a.folder, a.limit, a.redo)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
