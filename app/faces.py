"""얼굴 인식 엔진 (3단계-1): 영상에서 얼굴을 찾아 같은 사람끼리 묶기
  python -m app.faces --check                 GPU·모델 확인
  python -m app.faces "C:\\yt-dlp" --limit 10   폴더 안 영상 분석 (멈춰도 이어서 함)
  python -m app.faces --report                묶인 결과 미리보기 이미지
  python -m app.faces --recluster             묶기만 다시 하기 (기준값 바꾼 뒤)
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

# ---------------- 조정 가능한 값 ----------------
DET_THRESH = 0.60      # 얼굴로 인정할 최소 확신도
MIN_FACE = 40          # 이보다 작은 얼굴(픽셀)은 무시
SAME_IN_VIDEO = 0.50   # 한 영상 안에서 같은 사람으로 볼 유사도
SAME_PERSON = 0.40     # 영상끼리 같은 사람으로 묶을 유사도 (섞이면 ↑, 쪼개지면 ↓)
MIN_FRAMES, MAX_FRAMES, SEC_PER_FRAME = 6, 20, 30
GRAB_WORKERS = 4
DET_SIZE = 640
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
        raise FileNotFoundError(f"모델 파일이 없습니다: {MODEL_DIR}\\{name}  (STEP 2 확인)")
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
        ih, iw = img.shape[:2]
        if ih > iw:
            nh, nw = DET_SIZE, max(1, int(DET_SIZE * iw / ih))
        else:
            nw, nh = DET_SIZE, max(1, int(DET_SIZE * ih / iw))
        scale = nh / ih
        canvas = np.zeros((DET_SIZE, DET_SIZE, 3), np.uint8)
        canvas[:nh, :nw] = cv2.resize(img, (nw, nh))
        blob = cv2.dnn.blobFromImage(canvas, 1 / 128, (DET_SIZE, DET_SIZE),
                                     (127.5, 127.5, 127.5), swapRB=True)
        outs = self.det.run(self.det_out, {self.det_in: blob})
        outs = [o[0] if o.ndim == 3 else o for o in outs]
        fmc = len(outs) // 3
        boxes, scores, kpss = [], [], []
        for i, s in enumerate([8, 16, 32, 64, 128][:fmc]):
            sc = outs[i].reshape(-1)
            bb = outs[i + fmc].reshape(-1, 4) * s
            kp = outs[i + 2 * fmc].reshape(-1, 5, 2) * s
            h = w = DET_SIZE // s
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
        faces = self.detect(img)
        aligned, info = [], []
        for box, score, kps in faces:
            M, _ = cv2.estimateAffinePartial2D(kps.astype(np.float32), ARC_DST, method=cv2.LMEDS)
            if M is None:
                continue
            crop = _crop(img, box)
            if crop is None:
                continue
            aligned.append(cv2.warpAffine(img, M, (112, 112), borderValue=0))
            size = float(min(box[2] - box[0], box[3] - box[1]))
            info.append({"score": score, "size": size, "crop": crop, "q": score * size})
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


def grab(path, t, maxw=1280):
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", path,
           "-an", "-sn", "-frames:v", "1", "-vf", f"scale='min({maxw},iw)':-2",
           "-f", "image2pipe", "-c:v", "bmp", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60, creationflags=NO_WINDOW)
    except Exception:
        return None
    if not r.stdout:
        return None
    return cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)


def sample_times(dur):
    if not dur or dur <= 0:
        return [0.0]
    n = int(min(MAX_FRAMES, max(MIN_FRAMES, dur // SEC_PER_FRAME)))
    return [dur * (0.05 + 0.9 * (k + 0.5) / n) for k in range(n)]


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
        done REAL, frames INTEGER, faces INTEGER, status TEXT);
    """)
    conn.commit()


def find_videos(conn, folder):
    """폴더 안 실제 파일 ↔ DB 영상 연결 → [(id, 경로, 길이)]"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(videos)")}
    dcol = "duration" if "duration" in cols else "0"
    by_name = {}
    for vid, rel, dur in conn.execute(f"SELECT id, rel_path, {dcol} FROM videos"):
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
            best["sum"] += f["emb"]
            best["hits"] += 1
            best["emb"] = best["sum"] / np.linalg.norm(best["sum"])
        else:
            groups.append({"sum": f["emb"].copy(), "emb": f["emb"], "hits": 1, "best": f})
    if many:   # 장면이 많은데 한 번만 스친 흐릿한 얼굴은 버림
        groups = [g for g in groups if g["hits"] >= 2
                  or (g["best"]["score"] >= 0.8 and g["best"]["size"] >= 80)]
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
        if th:
            Path(th).unlink(missing_ok=True)
    conn.execute("DELETE FROM video_faces WHERE video_id=?", (vid,))
    for g in groups:
        b = g["best"]
        fid = conn.execute("INSERT INTO video_faces(video_id, emb, hits, score, t) VALUES (?,?,?,?,?)",
                           (vid, g["emb"].astype(np.float32).tobytes(), g["hits"],
                            b["score"], b["t"])).lastrowid
        conn.execute("UPDATE video_faces SET thumb=? WHERE id=?",
                     (_save_jpg(FACE_DIR / f"vf{fid}.jpg", b["crop"]), fid))
    conn.execute("INSERT OR REPLACE INTO face_scan(video_id, done, frames, faces, status) "
                 "VALUES (?,?,?,?,?)", (vid, time.time(), ok, len(groups), "ok" if ok else "noframe"))
    conn.commit()
    return ok, len(groups)


# ---------------- 영상끼리 같은 사람 묶기 ----------------
def cluster(conn):
    rows = conn.execute("SELECT id, centroid, n FROM face_people WHERE centroid IS NOT NULL").fetchall()
    cap = max(256, len(rows) * 2)
    C = np.zeros((cap, DIM), np.float32)
    S = np.zeros((cap, DIM), np.float32)
    N = np.zeros(cap, np.int64)
    pids = []
    for i, (pid, cen, n) in enumerate(rows):
        v = np.frombuffer(cen, np.float32)
        C[i], S[i], N[i] = v, v * max(n, 1), max(n, 1)
        pids.append(pid)
    m = len(rows)
    todo = conn.execute("SELECT id, emb FROM video_faces WHERE person_id IS NULL "
                        "ORDER BY hits DESC, score DESC").fetchall()
    touched, new = set(), 0
    for fid, emb in todo:
        e = np.frombuffer(emb, np.float32)
        j = -1
        if m:
            sims = C[:m] @ e
            j = int(sims.argmax())
            if sims[j] < SAME_PERSON:
                j = -1
        if j < 0:
            if m == cap:
                C = np.vstack([C, np.zeros_like(C)])
                S = np.vstack([S, np.zeros_like(S)])
                N = np.concatenate([N, np.zeros_like(N)])
                cap *= 2
            pids.append(conn.execute("INSERT INTO face_people(n, created) VALUES (0, ?)",
                                     (time.time(),)).lastrowid)
            j, m, new = m, m + 1, new + 1
        S[j] += e
        N[j] += 1
        C[j] = S[j] / np.linalg.norm(S[j])
        conn.execute("UPDATE video_faces SET person_id=? WHERE id=?", (pids[j], fid))
        touched.add(j)
    for j in touched:
        conn.execute("UPDATE face_people SET centroid=?, n=? WHERE id=?",
                     (C[j].tobytes(), int(N[j]), pids[j]))
    conn.commit()
    return len(todo), new


def recluster(conn):
    conn.execute("UPDATE video_faces SET person_id=NULL")
    conn.execute("DELETE FROM face_people")
    conn.commit()
    n, new = cluster(conn)
    print(f"다시 묶기 완료: 얼굴 {n}개 → 사람 {new}명 (기준 {SAME_PERSON})")


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
    if eng.device == "GPU":
        print(f"✅ 준비 완료: GPU 사용 (얼굴 찾기 장면당 {ms:.0f}ms)")
    else:
        print(f"⚠ GPU를 못 쓰고 CPU로 동작합니다 (장면당 {ms:.0f}ms, 느림)")


def scan(folder, limit=None, redo=False):
    if not os.path.isdir(folder):
        print("폴더를 찾을 수 없습니다:", folder)
        return
    eng = FaceEngine()
    print(f"얼굴 엔진 준비: {eng.device}")
    conn = open_db()
    ensure_tables(conn)
    vids = find_videos(conn, folder)
    done = {r[0] for r in conn.execute("SELECT video_id FROM face_scan")}
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
    print("결과 보기: python -m app.faces --report")
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
        print("아직 여러 영상에 나온 사람이 없습니다. 영상을 더 분석해 보세요.")
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
