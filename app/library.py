"""화면에서 쓰는 DB 읽기/쓰기"""
import json
import os
from datetime import datetime

from app.drives import connected_drives, drive_letters, volume_info

SORTS = {
    "추가된 순 (최신)": "v.id DESC",
    "이름순": "v.filename COLLATE NOCASE ASC",
    "길이 긴 순": "v.duration DESC",
    "길이 짧은 순": "v.duration ASC",
    "용량 큰 순": "v.size DESC",
    "별점 높은 순": "v.rating DESC, v.id DESC",
    "많이 본 순": "v.play_count DESC, v.id DESC",
    "최근 본 순": "v.last_played DESC",
}

BASE_SQL = (
    "SELECT v.*, d.nickname AS drive_name, "
    "(SELECT GROUP_CONCAT(t.name, ', ') FROM video_tags vt JOIN tags t ON t.id = vt.tag_id "
    " WHERE vt.video_id = v.id) AS tag_names, "
    "(SELECT GROUP_CONCAT(a.name, ', ') FROM video_actors va JOIN actors a ON a.id = va.actor_id "
    " WHERE va.video_id = v.id) AS actor_names "
    "FROM videos v LEFT JOIN drives d ON d.id = v.drive_id"
)

# 검색어 한 단어: 파일명·제목·메모·태그·배우·별명 중 어디에든 있으면
TEXT_SQL = (
    "(v.filename LIKE ? OR IFNULL(v.title,'') LIKE ? OR IFNULL(v.memo,'') LIKE ? "
    "OR EXISTS (SELECT 1 FROM video_tags vt JOIN tags t ON t.id = vt.tag_id "
    "           WHERE vt.video_id = v.id AND t.name LIKE ?) "
    "OR EXISTS (SELECT 1 FROM video_actors va JOIN actors a ON a.id = va.actor_id "
    "           WHERE va.video_id = v.id AND (a.name LIKE ? OR IFNULL(a.aliases,'') LIKE ?)))"
)

# ---------- 제외 폴더 (#2) ----------
def ensure_excluded(conn):
    """videos 표에 '제외' 칸이 없으면 만듦 (기존 기록은 그대로)"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(videos)")]
    if "excluded" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN excluded INTEGER DEFAULT 0")
        conn.commit()


def folder_video_ids(conn, serial, prefix):
    """특정 드라이브·폴더 안에 있는 영상 id 목록"""
    row = conn.execute("SELECT id FROM drives WHERE serial = ?", (serial,)).fetchone()
    if not row:
        return []
    p = os.path.normcase(prefix.rstrip("\\/") + os.sep) if prefix else ""
    return [r["id"] for r in conn.execute("SELECT id, rel_path FROM videos WHERE drive_id = ?", (row["id"],))
            if os.path.normcase(r["rel_path"]).startswith(p)]


def set_excluded_flag(conn, serial, prefix, flag):
    ensure_excluded(conn)
    ids = folder_video_ids(conn, serial, prefix)
    conn.executemany("UPDATE videos SET excluded = ? WHERE id = ?", [(flag, i) for i in ids])
    conn.commit()
    return len(ids)


def excluded_rels(conn, drive_id):
    """스캐너용: 이 드라이브에서 제외할 폴더 경로(드라이브 문자 뺀 것) 목록"""
    row = conn.execute("SELECT serial FROM drives WHERE id = ?", (drive_id,)).fetchone()
    if not row:
        return []
    out = []
    for x in get_setting(conn, "excluded", []):
        if x.get("serial") == row["serial"]:
            p = x.get("prefix") or ""
            out.append(os.path.normcase(p.rstrip("\\/") + os.sep) if p else "")
    return out




def _attach_paths(conn, rows):
    """지금 연결된 드라이브 문자로 실제 경로를 붙임 + 자막 있는지 표시"""
    letters = drive_letters(conn)
    try:
        subs = {r[0] for r in conn.execute("SELECT DISTINCT video_id FROM sub_files")}
    except Exception:
        subs = set()
    for r in rows:
        letter = letters.get(r["drive_id"])
        r["full_path"] = os.path.join(letter + "\\", r["rel_path"]) if letter else None
        r["online"] = bool(letter) and not r["is_missing"]
        r["has_sub"] = r["id"] in subs
    return rows



def load_videos(conn, text="", sort="추가된 순 (최신)", only_new=False,
                tag_ids=(), actor_ids=(), untagged=False):
    ensure_excluded(conn)
    where, params = ["IFNULL(v.excluded, 0) = 0"], []
    for word in text.split():
        where.append(TEXT_SQL)
        params += [f"%{word}%"] * 6
    if only_new:
        where.append("v.is_new = 1")
    for tid in tag_ids:
        where.append("v.id IN (SELECT video_id FROM video_tags WHERE tag_id = ?)")
        params.append(tid)
    for aid in actor_ids:
        where.append("v.id IN (SELECT video_id FROM video_actors WHERE actor_id = ?)")
        params.append(aid)
    if untagged:
        where.append("NOT EXISTS (SELECT 1 FROM video_tags WHERE video_id = v.id) "
                     "AND NOT EXISTS (SELECT 1 FROM video_actors WHERE video_id = v.id)")
    sql = BASE_SQL
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY " + SORTS.get(sort, "v.id DESC")
    rows = [dict(r) for r in conn.execute(sql, params)]
    return _attach_paths(conn, rows)


def get_video(conn, vid):
    rows = [dict(r) for r in conn.execute(BASE_SQL + " WHERE v.id = ?", (vid,))]
    return _attach_paths(conn, rows)[0] if rows else None


def counts(conn):
    ensure_excluded(conn)
    r = conn.execute("SELECT COUNT(*), IFNULL(SUM(is_new), 0) FROM videos "
                     "WHERE IFNULL(excluded, 0) = 0").fetchone()
    return r[0], r[1]


def clear_new(conn, ids):
    conn.executemany("UPDATE videos SET is_new = 0 WHERE id = ?", [(i,) for i in ids])
    conn.commit()


def clear_all_new(conn):
    conn.execute("UPDATE videos SET is_new = 0")
    conn.commit()


def set_rating(conn, ids, n):
    conn.executemany("UPDATE videos SET rating = ? WHERE id = ?", [(n, i) for i in ids])
    conn.commit()


def update_info(conn, vid, title, memo):
    conn.execute("UPDATE videos SET title = ?, memo = ? WHERE id = ?", (title, memo, vid))
    conn.commit()


def mark_played(conn, vid):
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("UPDATE videos SET play_count = play_count + 1, last_played = ?, is_new = 0 "
                 "WHERE id = ?", (now, vid))
    conn.commit()


def save_resume(conn, vid, pos, dur):
    """이어보기 위치 저장. 처음 10초 이내나 거의 끝까지 봤으면 0으로"""
    if pos is None:
        return
    if pos < 10 or (dur and pos > dur - 20):
        pos = 0
    conn.execute("UPDATE videos SET resume_pos = ? WHERE id = ?", (round(pos, 1), vid))
    conn.commit()


# ---------- 설정 ----------
def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()


# ---------- 등록 폴더 (드라이브 문자가 바뀌어도 찾을 수 있게 시리얼로 저장) ----------
def add_folder(conn, path):
    path = os.path.abspath(path)
    letter = os.path.splitdrive(path)[0].upper()
    serial, _ = volume_info(letter + "\\")
    prefix = os.path.relpath(path, letter + "\\")
    item = {"serial": serial, "prefix": "" if prefix == "." else prefix}
    folders = get_setting(conn, "folders", [])
    if item not in folders:
        folders.append(item)
        set_setting(conn, "folders", folders)


def folder_paths(conn):
    """등록 폴더 중 지금 연결된 것의 실제 경로, 연결 안 된 개수"""
    now = connected_drives()
    paths, offline = [], 0
    for f in get_setting(conn, "folders", []):
        letter = now.get(f["serial"])
        if letter:
            paths.append(os.path.join(letter + "\\", f["prefix"]))
        else:
            offline += 1
    return paths, offline
