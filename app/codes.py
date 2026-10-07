"""품번 (#4): 파일명에서 품번 찾기, 같은 품번 영상에 배우 자동으로 붙이기, 품번→배우 목록 가져오기/내보내기"""
import csv
import os
import re
from pathlib import Path

from app import library, tags

# 품번으로 착각하기 쉬운 글자 (화질·코덱·편 번호 등)
BAD_LABELS = {
    "MP", "MKV", "AVI", "HEVC", "AVC", "AAC", "FLAC", "DTS", "AC", "FHD", "UHD", "HD", "SD", "HQ", "LQ",
    "FPS", "KBPS", "MBPS", "BIT", "CH", "EP", "VOL", "PART", "PT", "CD", "DISC", "DVD", "BD", "BDRIP",
    "WEB", "WEBRIP", "RIP", "NO", "SEASON", "DAY", "TS", "PPV", "FC", "XVID", "DIVX", "SUB", "SUBS",
    "YEAR", "ATMOS", "HDR", "SDR", "HZ", "MB", "GB", "KB", "TB", "MIN", "SEC", "CLIP", "VIDEO", "MOVIE",
    "FILE", "IMG", "DSC", "MOV", "VID", "REC", "CAM", "SCENE", "TAKE", "SHOT", "TEST", "COPY", "NEW",
    "FINAL", "EDIT", "CUT",
}
_YTID = re.compile(r"\[[A-Za-z0-9_-]{11}\]")                    # 유튜브 영상 ID [xxxxxxxxxxx]
_FC2 = re.compile(r"FC2[\s_-]*(?:PPV[\s_-]*)?(\d{5,8})", re.IGNORECASE)
_DASH = re.compile(r"(?<![A-Za-z0-9])(\d{3})?([A-Za-z]{2,6})[-_](\d{2,5})(?![0-9])")
_TIGHT = re.compile(r"(?<![A-Za-z0-9])(\d{3})?([A-Z]{2,6})(\d{3,5})(?![0-9A-Za-z])")
_NAME_SPLIT = re.compile(r"[,/／、]")


def extract(name):
    """파일명 → (품번, 시리즈) 또는 None.   예) 200GANA-2394 → ('200GANA-2394', '200GANA')"""
    stem = os.path.splitext(os.path.basename(str(name or "")))[0]
    stem = _YTID.sub(" ", stem)
    m = _FC2.search(stem)
    if m:
        return f"FC2-PPV-{m.group(1)}", "FC2-PPV"
    for rx in (_DASH, _TIGHT):
        for m in rx.finditer(stem):
            label = m.group(2).upper()
            if label in BAD_LABELS:
                continue
            pre = m.group(1) or ""
            num = (m.group(3).lstrip("0") or "0").zfill(3)      # SSIS-00123 → SSIS-123
            return f"{pre}{label}-{num}", pre + label
    return None


def ensure(conn):
    library.ensure_excluded(conn)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS video_codes(
        video_id INTEGER PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
        code TEXT, series TEXT, fname TEXT);
    CREATE INDEX IF NOT EXISTS idx_vc_code ON video_codes(code);
    CREATE TABLE IF NOT EXISTS code_actors(
        code TEXT NOT NULL,
        actor_id INTEGER NOT NULL REFERENCES actors(id) ON DELETE CASCADE,
        PRIMARY KEY(code, actor_id));
    CREATE TABLE IF NOT EXISTS code_actor_sync(
        video_id INTEGER, actor_id INTEGER, PRIMARY KEY(video_id, actor_id));
    """)
    conn.commit()


def update_codes(conn):
    """새 영상·이름 바뀐 영상만 품번을 다시 찾음 (빠름)"""
    have = {r[0]: r[1] for r in conn.execute("SELECT video_id, fname FROM video_codes")}
    n = 0
    for vid, fn in conn.execute("SELECT id, filename FROM videos").fetchall():
        if have.get(vid) == fn:
            continue
        c = extract(fn)
        conn.execute("INSERT OR REPLACE INTO video_codes(video_id, code, series, fname) VALUES (?,?,?,?)",
                     (vid, c[0] if c else None, c[1] if c else None, fn))
        n += 1
    if n:
        conn.commit()
    return n


def learn(conn):
    """품번 있는 영상에 이미 붙은 배우 → 품번→배우 목록에 기억"""
    before = conn.total_changes
    conn.execute("""INSERT OR IGNORE INTO code_actors(code, actor_id)
                    SELECT DISTINCT vc.code, va.actor_id FROM video_codes vc
                    JOIN video_actors va ON va.video_id = vc.video_id
                    WHERE vc.code IS NOT NULL""")
    conn.commit()
    return conn.total_changes - before


def apply_mappings(conn):
    """품번→배우 목록대로 영상에 배우 붙이기 (한 번 붙인 건 다시 안 함 → 직접 지운 건 유지)"""
    rows = conn.execute("""SELECT DISTINCT vc.video_id, ca.actor_id FROM video_codes vc
                           JOIN code_actors ca ON ca.code = vc.code
                           WHERE NOT EXISTS (SELECT 1 FROM code_actor_sync s
                                             WHERE s.video_id = vc.video_id AND s.actor_id = ca.actor_id)
                        """).fetchall()
    added = 0
    for vid, aid in rows:
        before = conn.total_changes
        conn.execute("INSERT OR IGNORE INTO video_actors(video_id, actor_id) VALUES (?,?)", (vid, aid))
        added += conn.total_changes - before
        conn.execute("INSERT OR IGNORE INTO code_actor_sync(video_id, actor_id) VALUES (?,?)", (vid, aid))
    conn.commit()
    return added


def sync(conn):
    update_codes(conn)
    learn(conn)
    return apply_mappings(conn)


def code_of(conn, vid):
    row = conn.execute("SELECT code FROM video_codes WHERE video_id = ?", (vid,)).fetchone()
    return row[0] if row else None


def videos_of_code(conn, code):
    return [r[0] for r in conn.execute("SELECT video_id FROM video_codes WHERE code = ?", (code,))]


def current_names(conn, code):
    """이 품번 영상에 붙은 배우 + 품번 목록에 등록된 배우 → [(id, 이름)]"""
    return conn.execute("""SELECT a.id, a.name FROM actors a WHERE a.id IN (
                               SELECT va.actor_id FROM video_actors va
                               JOIN video_codes vc ON vc.video_id = va.video_id WHERE vc.code = ?
                               UNION SELECT actor_id FROM code_actors WHERE code = ?)
                           ORDER BY a.name COLLATE NOCASE""", (code, code)).fetchall()


def set_code_actors(conn, code, names):
    """이 품번의 배우를 names로 맞춤 (빠진 이름은 이 품번 영상에서 뗌) → (붙인 수, 뗀 배우 수)"""
    vids = videos_of_code(conn, code)
    old = {r[0] for r in current_names(conn, code)}
    new = set()
    for n in names:
        aid = tags.get_or_create(conn, "actor", n)
        if aid:
            new.add(aid)
    removed = old - new
    for aid in removed:
        conn.executemany("DELETE FROM video_actors WHERE video_id=? AND actor_id=?", [(v, aid) for v in vids])
        conn.executemany("DELETE FROM code_actor_sync WHERE video_id=? AND actor_id=?", [(v, aid) for v in vids])
        conn.execute("DELETE FROM code_actors WHERE code=? AND actor_id=?", (code, aid))
    added = 0
    for aid in new:
        conn.execute("INSERT OR IGNORE INTO code_actors(code, actor_id) VALUES (?,?)", (code, aid))
        for v in vids:
            before = conn.total_changes
            conn.execute("INSERT OR IGNORE INTO video_actors(video_id, actor_id) VALUES (?,?)", (v, aid))
            added += conn.total_changes - before
            conn.execute("INSERT OR IGNORE INTO code_actor_sync(video_id, actor_id) VALUES (?,?)", (v, aid))
    conn.commit()
    return added, len(removed)


def tag_series(conn):
    """품번 시리즈(예: 200GANA)를 태그로 붙이기 → (붙인 수, 시리즈 수)"""
    update_codes(conn)
    groups = {}
    for vid, s in conn.execute("SELECT video_id, series FROM video_codes WHERE series IS NOT NULL"):
        groups.setdefault(s, []).append(vid)
    n = 0
    for s, vids in groups.items():
        n += tags.attach(conn, "tag", vids, [s], source="code")
    conn.commit()
    return n, len(groups)


def stats(conn):
    update_codes(conn)
    live = "JOIN videos v ON v.id = vc.video_id AND IFNULL(v.excluded, 0) = 0"

    def one(sql):
        return conn.execute(sql).fetchone()[0]

    s = {
        "total": one("SELECT COUNT(*) FROM videos WHERE IFNULL(excluded, 0) = 0"),
        "coded": one(f"SELECT COUNT(*) FROM video_codes vc {live} WHERE vc.code IS NOT NULL"),
        "codes": one(f"SELECT COUNT(DISTINCT vc.code) FROM video_codes vc {live}"),
        "with_actor": one(f"SELECT COUNT(DISTINCT vc.code) FROM video_codes vc {live} WHERE EXISTS "
                          "(SELECT 1 FROM video_actors va WHERE va.video_id = vc.video_id)"),
        "mapped": one("SELECT COUNT(DISTINCT code) FROM code_actors"),
    }
    s["top"] = conn.execute(f"SELECT vc.series, COUNT(*) FROM video_codes vc {live} "
                            "WHERE vc.series IS NOT NULL GROUP BY vc.series "
                            "ORDER BY COUNT(*) DESC LIMIT 10").fetchall()
    s["missing"] = [r[0] for r in conn.execute(f"SELECT v.filename FROM video_codes vc {live} "
                                               "WHERE vc.code IS NULL ORDER BY RANDOM() LIMIT 6")]
    return s


def export_file(conn, path):
    """품번 목록 CSV (엑셀에서 배우 칸을 채운 뒤 다시 가져오기)"""
    update_codes(conn)
    rows = conn.execute("""SELECT vc.code, MIN(v.filename) FROM video_codes vc
                           JOIN videos v ON v.id = vc.video_id AND IFNULL(v.excluded, 0) = 0
                           WHERE vc.code IS NOT NULL GROUP BY vc.code
                           ORDER BY vc.series, vc.code""").fetchall()
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["품번", "배우 (여러 명은 / 로 구분)", "# 참고: 파일명 (가져올 때 무시됨)"])
        for code, fn in rows:
            names = [r[1] for r in current_names(conn, code)]
            w.writerow([code, " / ".join(names), "# " + (fn or "")])
    return len(rows)


def import_file(conn, path):
    """CSV/TXT: 한 줄에 '품번, 배우1 / 배우2' → (읽은 줄, 새로 등록한 연결 수, 못 읽은 품번들)"""
    update_codes(conn)
    raw = Path(path).read_bytes()
    text = None
    for enc in ("utf-8-sig", "cp949", "cp932"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", "replace")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    delim = "\t" if any("\t" in ln for ln in lines[:20]) else ","
    ok, mapped, bad = 0, 0, []
    for i, row in enumerate(csv.reader(lines, delimiter=delim)):
        cells = [c.strip() for c in row]
        if not cells or not cells[0]:
            continue
        c = extract(cells[0].upper())
        if not c:
            if i > 0:
                bad.append(cells[0])
            continue
        names = []
        for cell in cells[1:]:
            if cell.startswith("#"):
                break
            names += [tags.clean_name(n) for n in _NAME_SPLIT.split(cell) if tags.clean_name(n)]
        if not names:
            continue
        ok += 1
        for n in names:
            aid = tags.get_or_create(conn, "actor", n)
            before = conn.total_changes
            conn.execute("INSERT OR IGNORE INTO code_actors(code, actor_id) VALUES (?,?)", (c[0], aid))
            mapped += conn.total_changes - before
    conn.commit()
    return ok, mapped, bad
