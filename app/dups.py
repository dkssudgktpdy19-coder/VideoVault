"""같은 영상 찾기 엔진: 이름·확장자·인코딩이 달라도 '길이'와 '장면 지문'으로 판별
  python -m app.dups          후보의 장면 지문 만들기 + 결과 출력 (멈춰도 이어서 함)
  python -m app.dups --list   지문은 만들지 않고 결과만 보기
원리: 길이가 거의 같은 영상끼리만 후보 → 12%·25%…88% 지점 장면 7개를 9×8로 줄여
      밝기 변화 모양(dHash)을 비교 → 대부분 같으면 같은 영상
"""
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from app import library
from app.db import init_db
from app.utils import fmt_size

FP_VER = 1
POINTS = (0.12, 0.25, 0.38, 0.50, 0.62, 0.75, 0.88)   # 장면을 볼 위치 (영상 길이 비율)
DUR_TOL = 1.5          # 길이 차이 허용 (초)
DUR_TOL_PCT = 0.002    # 긴 영상은 길이의 0.2%까지 (1시간 → 7초)
MIN_DUR = 10           # 이보다 짧은 영상은 비교 안 함
HAM_MAX = 10           # 지문 64칸 중 다른 칸이 이 이하면 같은 장면 (못 찾으면 ↑, 엉뚱하면 ↓)
MATCH_MIN = 0.70       # 비교한 장면 중 이 비율 이상 같으면 같은 영상
MIN_COMPARE = 3        # 최소 몇 장면은 비교돼야 판정
STD_MIN = 6.0          # 이보다 밋밋한 화면(검은 화면 등)은 비교에서 뺌
WORKERS = 4
NO_WINDOW = 0x08000000
FFMPEG = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"


def ensure(conn):
    library.ensure_excluded(conn)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS video_fp(
        video_id INTEGER PRIMARY KEY, ver INTEGER, size INTEGER, mtime REAL, hashes TEXT);
    CREATE TABLE IF NOT EXISTS dup_ignore(a INTEGER, b INTEGER, PRIMARY KEY(a, b));
    """)
    conn.commit()


def _tol(d):
    return max(DUR_TOL, (d or 0) * DUR_TOL_PCT)


def _base_rows(conn):
    return [dict(r) for r in conn.execute(
        "SELECT id, drive_id, rel_path, filename, size, mtime, duration, is_missing FROM videos "
        "WHERE IFNULL(excluded, 0) = 0 AND IFNULL(is_missing, 0) = 0 AND IFNULL(duration, 0) >= ?",
        (MIN_DUR,))]


def candidate_pairs(rows):
    """길이가 거의 같은 영상 쌍만"""
    rows = sorted(rows, key=lambda r: r["duration"])
    pairs = []
    for i, a in enumerate(rows):
        j = i + 1
        while j < len(rows) and rows[j]["duration"] - a["duration"] <= _tol(rows[j]["duration"]):
            pairs.append((a["id"], rows[j]["id"]))
            j += 1
    return pairs


def _frame_hash(path, t):
    """한 장면의 지문: 16자리 글자 / '-' (밋밋한 화면) / None (못 읽음)"""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", path,
           "-an", "-sn", "-dn", "-frames:v", "1", "-vf", "scale=9:8:flags=area,format=gray",
           "-f", "rawvideo", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120, creationflags=NO_WINDOW)
    except Exception:
        return None
    if len(r.stdout) < 72:
        return None
    a = np.frombuffer(r.stdout[:72], np.uint8).reshape(8, 9).astype(np.int16)
    if a.std() < STD_MIN:
        return "-"
    v = 0
    for b in (a[:, 1:] > a[:, :-1]).flatten():
        v = (v << 1) | int(b)
    return f"{v:016x}"


def _fresh(fp, r):
    return (fp is not None and fp[0] == FP_VER and fp[1] == r["size"]
            and abs((fp[2] or 0) - (r["mtime"] or 0)) < 1)


def build(conn, progress=None, stop=lambda: False, paused=lambda: False):
    """후보 영상 중 지문이 없는 것만 만듦 → 결과 요약"""
    ensure(conn)
    rows = _base_rows(conn)
    byid = {r["id"]: r for r in rows}
    ids = {x for p in candidate_pairs(rows) for x in p}
    fps = {r[0]: (r[1], r[2], r[3]) for r in conn.execute("SELECT video_id, ver, size, mtime FROM video_fp")}
    todo = [byid[i] for i in ids if not _fresh(fps.get(i), byid[i])]
    library._attach_paths(conn, todo)
    offline = [r for r in todo if not r["online"]]
    todo = sorted((r for r in todo if r["online"]), key=lambda r: r["full_path"])
    done = err = 0
    with ThreadPoolExecutor(WORKERS) as pool:
        for r in todo:
            while paused() and not stop():
                time.sleep(0.5)
            if stop():
                break
            times = [r["duration"] * p for p in POINTS]
            hs = list(pool.map(lambda t, p=r["full_path"]: _frame_hash(p, t), times))
            done += 1
            if all(h is None for h in hs):
                err += 1
            else:
                conn.execute("INSERT OR REPLACE INTO video_fp VALUES (?,?,?,?,?)",
                             (r["id"], FP_VER, r["size"], r["mtime"], "|".join(h or "-" for h in hs)))
                conn.commit()
            if progress:
                progress(done, len(todo), r["filename"])
    return {"todo": len(todo), "done": done, "err": err, "offline": len(offline)}


def _parse(h):
    return [None if x in ("-", "") else int(x, 16) for x in (h or "").split("|")]


def score(ha, hb):
    n = m = 0
    for a, b in zip(ha, hb):
        if a is None or b is None:
            continue
        n += 1
        if bin(a ^ b).count("1") <= HAM_MAX:
            m += 1
    return n, m


def find_groups(conn):
    """같은 영상 묶음 → {"groups": [{"ids": [...], "pct": 일치%}], "pending": 지문 없는 후보 수}"""
    ensure(conn)
    pairs = candidate_pairs(_base_rows(conn))
    fps = {vid: _parse(h) for vid, h in
           conn.execute("SELECT video_id, hashes FROM video_fp WHERE ver = ?", (FP_VER,))}
    ign = {(a, b) for a, b in conn.execute("SELECT a, b FROM dup_ignore")}
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    edges = []
    for a, b in pairs:
        if (min(a, b), max(a, b)) in ign or a not in fps or b not in fps:
            continue
        n, m = score(fps[a], fps[b])
        if n >= MIN_COMPARE and m / n >= MATCH_MIN:
            parent[find(a)] = find(b)
            edges.append((a, b, m / n))
    groups = {}
    for a, b, s in edges:
        g = groups.setdefault(find(a), {"ids": set(), "pct": 1.0})
        g["ids"].update((a, b))
        g["pct"] = min(g["pct"], s)
    out = [{"ids": sorted(g["ids"]), "pct": int(round(g["pct"] * 100))} for g in groups.values()]
    out.sort(key=lambda g: (-g["pct"], g["ids"][0]))
    pending = len({x for p in pairs for x in p} - set(fps))
    return {"groups": out, "pending": pending}


def ignore_pairs(conn, pairs):
    conn.executemany("INSERT OR IGNORE INTO dup_ignore(a, b) VALUES (?, ?)",
                     [(min(a, b), max(a, b)) for a, b in pairs])
    conn.commit()


# ---------------- 지울 영상의 정보를 남길 영상으로 합치기 ----------------
def copy_links(conn, keep, other):
    """태그·배우 연결 복사 (여러 번 해도 같음)"""
    for table in ("video_tags", "video_actors"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] != "id"]
        if "video_id" not in cols:
            continue
        sel = ", ".join("?" if c == "video_id" else c for c in cols)
        conn.execute(f"INSERT OR IGNORE INTO {table}({', '.join(cols)}) "
                     f"SELECT {sel} FROM {table} WHERE video_id = ?", (keep, other))


def move_extras(conn, keep, other):
    """북마크는 옮기고, 남길 영상에 자막이 없으면 자막도 옮김"""
    try:
        conn.execute("UPDATE scene_marks SET video_id = ? WHERE video_id = ?", (keep, other))
    except Exception:
        pass
    try:
        if not conn.execute("SELECT 1 FROM sub_files WHERE video_id = ?", (keep,)).fetchone():
            conn.execute("UPDATE OR IGNORE sub_files SET video_id = ? WHERE video_id = ?", (keep, other))
    except Exception:
        pass


def add_stats(conn, keep, other):
    """별점은 높은 쪽, 본 횟수는 더하기, 메모는 이어 붙이기"""
    sql = "SELECT rating, play_count, memo, last_played FROM videos WHERE id = ?"
    k, o = conn.execute(sql, (keep,)).fetchone(), conn.execute(sql, (other,)).fetchone()
    if not k or not o:
        return
    memo = k["memo"] or ""
    if o["memo"] and o["memo"] not in memo:
        memo = (memo + "\n" + o["memo"]).strip()
    last = max([x for x in (k["last_played"], o["last_played"]) if x], default=None)
    conn.execute("UPDATE videos SET rating = ?, play_count = ?, memo = ?, last_played = ? WHERE id = ?",
                 (max(k["rating"] or 0, o["rating"] or 0), (k["play_count"] or 0) + (o["play_count"] or 0),
                  memo or None, last, keep))


def _main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass
    conn = init_db()
    ensure(conn)
    if "--list" not in sys.argv:
        print("길이가 거의 같은 영상만 골라 장면 지문을 만듭니다 (Ctrl+C로 멈춰도 다음에 이어서 함)")
        t0 = time.time()

        def prog(d, t, n):
            print(f"\r  {d}/{t}  {n[:40]:<40}", end="", flush=True)
        try:
            r = build(conn, progress=prog)
            print(f"\n지문 만듦 {r['done']}개 · 못 읽음 {r['err']}개 · "
                  f"외장하드 연결 안 됨 {r['offline']}개 · {time.time() - t0:.0f}초")
        except KeyboardInterrupt:
            print("\n⏸ 중단함. 같은 명령을 다시 실행하면 이어서 합니다.")
    data = find_groups(conn)
    print(f"\n===== 같은 영상 묶음 {len(data['groups'])}개 | 아직 비교 못 한 후보 {data['pending']}개 =====")
    for i, g in enumerate(data["groups"], 1):
        print(f"[{i}] 장면 일치 {g['pct']}%")
        for vid in g["ids"]:
            r = conn.execute("SELECT filename, size FROM videos WHERE id = ?", (vid,)).fetchone()
            if r:
                print(f"     {r['filename']}  ({fmt_size(r['size'] or 0)})")
    conn.close()


if __name__ == "__main__":
    _main()
