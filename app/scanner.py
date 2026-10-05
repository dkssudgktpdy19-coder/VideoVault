"""폴더를 스캔해서 영상 정보를 DB에 저장하고 썸네일을 만듦"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import media
from app.config import THUMB_DIR, VIDEO_EXTS
from app.db import init_db
from app.drives import drive_letters, get_or_create_drive
from app.utils import fmt_duration, fmt_size

WORKERS = 4    # 동시에 처리할 개수
SKIP_DIRS = {"System Volume Information", "$RECYCLE.BIN", "$Recycle.Bin"}
META_COLS = ("duration", "width", "height", "fps", "video_codec", "audio_codec", "bitrate")


def _progress(done, total):
    print(f"\r      {done}/{total} ({done * 100 // total}%)", end="", flush=True)
    if done == total:
        print()


def _walk(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                yield os.path.join(dirpath, name)


def _analyze(full, size):
    return media.file_key(full, size), media.probe(full)


def _unique_key(conn, key):
    n = 2
    while conn.execute("SELECT 1 FROM videos WHERE file_key=?", (f"{key}_{n}",)).fetchone():
        n += 1
    return f"{key}_{n}"


def _save(conn, letters, drive_id, rel, size, mtime, key, info):
    """DB에 저장하고 'new' / 'moved' / 'updated' 중 하나를 돌려줌"""
    meta = [info.get(c) for c in META_COLS]
    sets = ", ".join(f"{c}=?" for c in META_COLS)
    filename = os.path.basename(rel)

    same = conn.execute("SELECT id, drive_id, rel_path FROM videos WHERE file_key=?", (key,)).fetchone()
    if same:
        if same["drive_id"] == drive_id and same["rel_path"] == rel:
            conn.execute(f"UPDATE videos SET size=?, mtime=?, {sets}, is_missing=0 WHERE id=?",
                         (size, mtime, *meta, same["id"]))
            return "updated"
        old_letter = letters.get(same["drive_id"])
        old_exists = old_letter and os.path.exists(os.path.join(old_letter + "\\", same["rel_path"]))
        if not old_exists:
            # 이름 변경 또는 이동 → 기존 태그·별점 그대로 유지
            conn.execute("UPDATE videos SET drive_id=?, rel_path=?, filename=?, size=?, mtime=?, "
                         "is_missing=0 WHERE id=?", (drive_id, rel, filename, size, mtime, same["id"]))
            return "moved"
        key = _unique_key(conn, key)   # 같은 파일이 두 군데 있음 → 복사본으로 따로 등록

    by_path = conn.execute("SELECT id FROM videos WHERE drive_id=? AND rel_path=?",
                           (drive_id, rel)).fetchone()
    if by_path:
        # 이름은 같은데 내용이 바뀐 파일 → 정보 갱신, 썸네일 다시 만들기
        conn.execute(f"UPDATE videos SET file_key=?, size=?, mtime=?, {sets}, is_missing=0, "
                     "thumb_path=CASE WHEN thumb_custom=1 THEN thumb_path ELSE NULL END WHERE id=?",
                     (key, size, mtime, *meta, by_path["id"]))
        return "updated"

    cols = ", ".join(META_COLS)
    marks = ", ".join("?" * len(META_COLS))
    conn.execute(f"INSERT INTO videos (file_key, drive_id, rel_path, filename, size, mtime, {cols}) "
                 f"VALUES (?,?,?,?,?,?, {marks})", (key, drive_id, rel, filename, size, mtime, *meta))
    return "new"


def _thumb_job(vid, full, duration):
    if duration and duration > 2:
        t = min(max(duration * 0.1, 1.0), duration - 1)
    else:
        t = 0
    name = f"{vid}.jpg"
    ok = media.make_thumb(full, THUMB_DIR / name, t)
    return vid, (name if ok else None), t


def make_thumbnails(conn, drive_id, root, prefix):
    rows = conn.execute("SELECT id, rel_path, duration, thumb_path FROM videos "
                        "WHERE drive_id=? AND is_missing=0 AND thumb_custom=0", (drive_id,)).fetchall()
    jobs = [r for r in rows if r["rel_path"].startswith(prefix)
            and not (r["thumb_path"] and (THUMB_DIR / r["thumb_path"]).exists())]
    if not jobs:
        print("      새로 만들 썸네일 없음")
        return 0, 0
    ok = fail = 0
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = [pool.submit(_thumb_job, r["id"], os.path.join(root, r["rel_path"]), r["duration"])
                   for r in jobs]
        for i, fut in enumerate(as_completed(futures), 1):
            vid, name, t = fut.result()
            if name:
                conn.execute("UPDATE videos SET thumb_path=?, thumb_time=? WHERE id=?", (name, t, vid))
                ok += 1
            else:
                fail += 1
            if i % 20 == 0:
                conn.commit()
            _progress(i, len(jobs))
    conn.commit()
    return ok, fail


def scan_folder(folder):
    folder = os.path.abspath(folder.strip().strip('"'))
    if not os.path.isdir(folder):
        print(f"폴더를 찾을 수 없습니다: {folder}")
        return
    letter = os.path.splitdrive(folder)[0].upper()
    if len(letter) != 2:
        print("드라이브 문자(E: 등)가 있는 폴더만 지원합니다.")
        return

    t0 = time.time()
    conn = init_db()
    drive_id = get_or_create_drive(conn, letter)
    conn.commit()
    letters = drive_letters(conn)
    root = letter + "\\"
    prefix = os.path.relpath(folder, root)
    prefix = "" if prefix == "." else prefix + os.sep

    # 1) 파일 목록 확인 (바뀐 것만 골라냄)
    print(f"[1/3] 파일 목록 확인 중: {folder}")
    known = {r["rel_path"]: r for r in conn.execute(
        "SELECT id, rel_path, size, mtime, is_missing FROM videos WHERE drive_id=?", (drive_id,))}
    seen, todo, unchanged = set(), [], 0
    for full in _walk(folder):
        rel = os.path.relpath(full, root)
        try:
            st = os.stat(full)
        except OSError:
            continue
        seen.add(rel)
        row = known.get(rel)
        if row and row["size"] == st.st_size and abs((row["mtime"] or 0) - st.st_mtime) < 2:
            unchanged += 1
            if row["is_missing"]:
                conn.execute("UPDATE videos SET is_missing=0 WHERE id=?", (row["id"],))
            continue
        todo.append((full, rel, st.st_size, st.st_mtime))
    print(f"      영상 {len(seen)}개 발견 (변경 없음 {unchanged}개 / 분석 필요 {len(todo)}개)")

    # 2) 새 영상·바뀐 영상 분석
    print("[2/3] 영상 정보 분석 중")
    stats = {"new": 0, "moved": 0, "updated": 0, "error": 0}
    if todo:
        with ThreadPoolExecutor(WORKERS) as pool:
            futures = {pool.submit(_analyze, f, s): (f, r, s, m) for f, r, s, m in todo}
            for i, fut in enumerate(as_completed(futures), 1):
                full, rel, size, mtime = futures[fut]
                try:
                    key, info = fut.result()
                    stats[_save(conn, letters, drive_id, rel, size, mtime, key, info)] += 1
                except Exception as e:
                    stats["error"] += 1
                    print(f"\n      [오류] {rel}: {e}")
                if i % 20 == 0:
                    conn.commit()
                _progress(i, len(todo))
    else:
        print("      분석할 영상 없음")
    conn.commit()

    # 사라진 파일 표시
    missing = 0
    for rel, row in known.items():
        if rel.startswith(prefix) and rel not in seen:
            cur = conn.execute("UPDATE videos SET is_missing=1 "
                               "WHERE id=? AND drive_id=? AND rel_path=? AND is_missing=0",
                               (row["id"], drive_id, rel))
            missing += cur.rowcount
    conn.commit()

    # 3) 썸네일
    print("[3/3] 썸네일 만드는 중")
    t_ok, t_fail = make_thumbnails(conn, drive_id, root, prefix)
    conn.close()

    print("\n===== 스캔 완료 =====")
    print(f"새 영상(NEW): {stats['new']}개 | 이동·이름변경: {stats['moved']}개 | "
          f"내용 변경: {stats['updated']}개 | 사라짐: {missing}개 | 오류: {stats['error']}개")
    print(f"썸네일 생성: {t_ok}개 | 썸네일 실패: {t_fail}개")
    print(f"걸린 시간: {time.time() - t0:.1f}초")


def list_videos(limit=20):
    conn = init_db()
    total = conn.execute("SELECT COUNT(*), SUM(is_new), SUM(is_missing) FROM videos").fetchone()
    print(f"전체 {total[0]}개 | NEW {total[1] or 0}개 | 못 찾음 {total[2] or 0}개 (최근 {limit}개 표시)\n")
    for r in conn.execute("SELECT * FROM videos ORDER BY id DESC LIMIT ?", (limit,)):
        badge = "[NEW]" if r["is_new"] else "     "
        res = f"{r['height']}p" if r["height"] else "?"
        thumb = "O" if r["thumb_path"] else "X"
        lost = " (못 찾음)" if r["is_missing"] else ""
        print(f"{badge} {fmt_duration(r['duration']):>8}  {res:>6}  {fmt_size(r['size']):>9}  "
              f"썸네일:{thumb}  {r['filename']}{lost}")
    conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('사용법:  python -m app.scanner "E:\\영상폴더"   또는   python -m app.scanner --list')
    elif sys.argv[1] == "--list":
        list_videos()
    else:
        scan_folder(" ".join(sys.argv[1:]))
