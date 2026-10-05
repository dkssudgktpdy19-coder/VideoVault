"""태그·배우 저장, 이름 바꾸기·합치기, 파일명으로 자동 태그"""
import os
import re

KINDS = {
    "tag": {"table": "tags", "link": "video_tags", "col": "tag_id"},
    "actor": {"table": "actors", "link": "video_actors", "col": "actor_id"},
}
LABEL = {"tag": "태그", "actor": "배우"}


def ensure_indexes(conn):
    """태그·배우로 빨리 찾기 위한 색인 (한 번만 만들어짐)"""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vt_tag ON video_tags(tag_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_va_actor ON video_actors(actor_id)")
    conn.commit()


def clean_name(name):
    """앞뒤 공백 정리, 쉼표 제거"""
    return " ".join(str(name or "").replace(",", " ").split())


def list_items(conn, kind):
    k = KINDS[kind]
    sql = (f"SELECT x.id, x.name, COUNT(l.video_id) AS n FROM {k['table']} x "
           f"LEFT JOIN {k['link']} l ON l.{k['col']} = x.id "
           "GROUP BY x.id ORDER BY x.name COLLATE NOCASE")
    return [dict(r) for r in conn.execute(sql)]


def all_names(conn, kind):
    t = KINDS[kind]["table"]
    return [r[0] for r in conn.execute(f"SELECT name FROM {t} ORDER BY name COLLATE NOCASE")]


def find_id(conn, kind, name):
    t = KINDS[kind]["table"]
    row = conn.execute(f"SELECT id FROM {t} WHERE name = ? COLLATE NOCASE",
                       (clean_name(name),)).fetchone()
    return row[0] if row else None


def get_or_create(conn, kind, name):
    name = clean_name(name)
    if not name:
        return None
    xid = find_id(conn, kind, name)
    if xid is None:
        xid = conn.execute(f"INSERT INTO {KINDS[kind]['table']} (name) VALUES (?)", (name,)).lastrowid
    return xid


def names_for_videos(conn, kind, video_ids):
    """선택한 영상들에 붙어 있는 {이름: 영상 수}"""
    if not video_ids:
        return {}
    k = KINDS[kind]
    marks = ",".join("?" * len(video_ids))
    sql = (f"SELECT x.name, COUNT(*) FROM {k['link']} l JOIN {k['table']} x ON x.id = l.{k['col']} "
           f"WHERE l.video_id IN ({marks}) GROUP BY x.id")
    return {r[0]: r[1] for r in conn.execute(sql, list(video_ids))}


def attach(conn, kind, video_ids, names, source="manual"):
    """영상들에 태그(배우) 붙이기. 새로 붙은 개수를 돌려줌 (저장은 부르는 쪽에서)"""
    added = 0
    for name in names:
        xid = get_or_create(conn, kind, name)
        if xid is None:
            continue
        if kind == "tag":
            sql = "INSERT OR IGNORE INTO video_tags (video_id, tag_id, source) VALUES (?,?,?)"
            rows = [(vid, xid, source) for vid in video_ids]
        else:
            sql = "INSERT OR IGNORE INTO video_actors (video_id, actor_id) VALUES (?,?)"
            rows = [(vid, xid) for vid in video_ids]
        before = conn.total_changes
        conn.executemany(sql, rows)
        added += conn.total_changes - before
    return added


def detach(conn, kind, video_ids, names):
    k = KINDS[kind]
    for name in names:
        xid = find_id(conn, kind, name)
        if xid is not None:
            conn.executemany(f"DELETE FROM {k['link']} WHERE video_id = ? AND {k['col']} = ?",
                             [(vid, xid) for vid in video_ids])


def merge(conn, kind, src_id, dst_id):
    """src를 dst에 합침 (src가 붙어 있던 영상에 dst를 붙이고 src 삭제)"""
    k = KINDS[kind]
    conn.execute(f"INSERT OR IGNORE INTO {k['link']} (video_id, {k['col']}) "
                 f"SELECT video_id, ? FROM {k['link']} WHERE {k['col']} = ?", (dst_id, src_id))
    if kind == "actor":
        conn.execute("UPDATE faces SET actor_id = ? WHERE actor_id = ?", (dst_id, src_id))
    else:
        conn.execute("UPDATE bookmarks SET tag_id = ? WHERE tag_id = ?", (dst_id, src_id))
    conn.execute(f"DELETE FROM {k['table']} WHERE id = ?", (src_id,))


def rename(conn, kind, xid, new_name):
    """이름 바꾸기. 같은 이름이 이미 있으면 그쪽으로 합침"""
    new_name = clean_name(new_name)
    if not new_name:
        return
    other = find_id(conn, kind, new_name)
    if other is not None and other != xid:
        merge(conn, kind, xid, other)
    else:
        conn.execute(f"UPDATE {KINDS[kind]['table']} SET name = ? WHERE id = ?", (new_name, xid))
    conn.commit()


def delete(conn, kind, xid):
    conn.execute(f"DELETE FROM {KINDS[kind]['table']} WHERE id = ?", (xid,))
    conn.commit()


def get_aliases(conn, actor_id):
    row = conn.execute("SELECT aliases FROM actors WHERE id = ?", (actor_id,)).fetchone()
    return (row[0] or "") if row else ""


def set_aliases(conn, actor_id, text):
    words = [clean_name(w) for w in str(text).split(",")]
    value = ", ".join(w for w in words if w) or None
    conn.execute("UPDATE actors SET aliases = ? WHERE id = ?", (value, actor_id))
    conn.commit()


# ---------- 파일명·폴더명에서 추천 / 자동 태그 ----------
_BRACKET = re.compile(r"[\[【(（「『〔]([^\[\]【】()（）「」『』〔〕]{2,40})[\]】)）」』〕]")
_HASHTAG = re.compile(r"#([^\s#\[\]()【】]{2,30})")
_SPLIT = re.compile(r"[\\/]")


def _meaningless(s):
    """추천할 가치가 없는 글자 (숫자만, 유튜브 영상 ID 등)"""
    if len(s) < 2 or len(s) > 30:
        return True
    if re.fullmatch(r"[\d\s.\-_:]+", s):
        return True
    if re.fullmatch(r"[A-Za-z0-9_\-]{8,}", s):
        upper_inside = sum(ch.isupper() for ch in s[1:])
        has_lower = any(ch.islower() for ch in s)
        if any(ch.isdigit() for ch in s) or "_" in s or "-" in s or (has_lower and upper_inside >= 2):
            return True
    return False


def _dictionary(conn):
    """이미 있는 태그·배우(별명 포함) 목록"""
    dic = {"tag": [], "actor": []}
    for r in conn.execute("SELECT name FROM tags"):
        if len(r[0]) >= 2:
            dic["tag"].append((r[0], [r[0].lower()]))
    for r in conn.execute("SELECT name, aliases FROM actors"):
        words = [r[0]] + (r[1] or "").split(",")
        words = [w.strip().lower() for w in words if len(w.strip()) >= 2]
        if words:
            dic["actor"].append((r[0], words))
    return dic


def has_dictionary(conn):
    dic = _dictionary(conn)
    return bool(dic["tag"] or dic["actor"])


def _match(entries, text):
    low = text.lower()
    return [name for name, words in entries if any(w in low for w in words)]


def _video_text(v):
    return f"{v.get('rel_path') or ''} {v.get('title') or ''}"


def suggest(conn, v):
    """영상 하나의 파일명·폴더명에서 태그·배우 추천"""
    dic = _dictionary(conn)
    text = _video_text(v)
    have = {kind: {n.lower() for n in names_for_videos(conn, kind, [v["id"]])} for kind in KINDS}
    actors = [n for n in _match(dic["actor"], text) if n.lower() not in have["actor"]]
    tag_list = [n for n in _match(dic["tag"], text) if n.lower() not in have["tag"]]

    # 새 태그 후보: [괄호] 안 글자, #해시태그, 폴더 이름
    stem = os.path.splitext(v.get("filename") or "")[0]
    cands = _BRACKET.findall(stem) + _HASHTAG.findall(stem)
    cands += _SPLIT.split(os.path.dirname(v.get("rel_path") or ""))
    actor_words = {w for _, words in dic["actor"] for w in words}
    known = {n.lower() for n in tag_list + actors} | have["tag"] | have["actor"] | actor_words
    for c in cands:
        c = clean_name(c)
        if not c or _meaningless(c) or c.lower() in known:
            continue
        known.add(c.lower())
        tag_list.append(c)
    return {"tag": tag_list[:20], "actor": actors[:10]}


def _links(conn, kind):
    """{영상 id: {붙어 있는 이름(소문자)}}"""
    k = KINDS[kind]
    out = {}
    sql = f"SELECT l.video_id, x.name FROM {k['link']} l JOIN {k['table']} x ON x.id = l.{k['col']}"
    for vid, name in conn.execute(sql):
        out.setdefault(vid, set()).add(name.lower())
    return out


def plan_auto_tag(conn, video_ids=None):
    """이미 있는 태그·배우 이름이 파일명·폴더명에 들어 있는 영상 찾기 (아직 저장 안 함)"""
    dic = _dictionary(conn)
    links = {kind: _links(conn, kind) for kind in KINDS}
    wanted = set(video_ids) if video_ids is not None else None
    plan = []
    for r in conn.execute("SELECT id, filename, rel_path, title FROM videos").fetchall():
        if wanted is not None and r["id"] not in wanted:
            continue
        v = dict(r)
        text = _video_text(v)
        item = {"id": v["id"], "filename": v["filename"]}
        for kind in KINDS:
            have = links[kind].get(v["id"], set())
            item[kind] = [n for n in _match(dic[kind], text) if n.lower() not in have]
        if item["tag"] or item["actor"]:
            plan.append(item)
    return plan


def apply_plan(conn, plan):
    n = 0
    for item in plan:
        n += attach(conn, "tag", [item["id"]], item["tag"], source="filename")
        n += attach(conn, "actor", [item["id"]], item["actor"])
    conn.commit()
    return n
