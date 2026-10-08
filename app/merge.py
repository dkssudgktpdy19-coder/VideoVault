"""영상 합치기 엔진 (5-1): 나뉜 영상(CD1·CD2, part1·part2, -A·-B …) 찾기, 이어 붙이기, 원본 보관함
  python -m app.merge      나뉜 영상 묶음 목록만 보기 (아무것도 바꾸지 않음)

  · 형식(코덱·해상도·fps·소리)이 모두 같으면 다시 인코딩 없이 이어 붙임 → 빠르고 화질 그대로
  · 다르면 GPU(NVENC)로 다시 인코딩 (안 되면 CPU)
  · 결과 길이가 조각 길이 합과 맞을 때만 원본을 같은 드라이브의 숨김 폴더 .VideoVault_보관 으로 옮김
"""
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from app import library, media
from app.config import THUMB_DIR
from app.drives import drive_letters, get_or_create_drive
from app.utils import fmt_duration, fmt_size

NO_WINDOW = 0x08000000
FFMPEG = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
FFPROBE = shutil.which("ffprobe") or str(Path(FFMPEG).with_name("ffprobe.exe"))
ARCHIVE_NAME = ".VideoVault_보관"
DEFAULT_DAYS = 7
SETTING_DAYS = "merge_auto_days"
MAX_PARTS = 30
META_COLS = ("duration", "width", "height", "fps", "video_codec", "audio_codec", "bitrate")
FORMATS = {".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".mkv": "matroska", ".webm": "webm",
           ".ts": "mpegts", ".m2ts": "mpegts", ".mts": "mpegts", ".avi": "avi",
           ".wmv": "asf", ".flv": "flv"}


class MergeError(Exception):
    pass


class Cancelled(Exception):
    pass


def ensure(conn):
    library.ensure_excluded(conn)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS merge_jobs(
            id INTEGER PRIMARY KEY, merged_id INTEGER, merged_name TEXT,
            created REAL, total_size INTEGER, mode TEXT);
        CREATE TABLE IF NOT EXISTS merge_items(
            job_id INTEGER, video_id INTEGER, drive_id INTEGER,
            orig_rel TEXT, arch_rel TEXT, ord INTEGER, PRIMARY KEY(job_id, video_id));
    """)
    conn.commit()


def get_days(conn):
    """보관 원본 자동 삭제 일수 (0 = 자동 삭제 끔)"""
    try:
        return max(0, int(library.get_setting(conn, SETTING_DAYS, DEFAULT_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_DAYS


def set_days(conn, days):
    library.set_setting(conn, SETTING_DAYS, int(days))


# ---------------- 나뉜 영상 찾기 ----------------
_PATTERNS = (
    # (정규식, 확실한 표시인지, A·B 글자인지)
    (re.compile(r"^(.*?)[\s._-]*(?:cd|part|pt|disc|disk)[\s._-]*0*([1-9]\d?)$", re.I), True, False),
    (re.compile(r"^(.*?[A-Za-z]{2,}[-_]?\d{2,5})[-_]?([A-Da-d])$"), False, True),
    (re.compile(r"^(.*?\D)[\s._-]+([1-9])$"), False, False),
)


def split_part(stem):
    """'ABC-123-CD2' → ('ABC-123', 2, True) / 표시 없으면 None"""
    stem = (stem or "").strip()
    for rx, sure, letter in _PATTERNS:
        m = rx.match(stem)
        if not m:
            continue
        base = m.group(1).strip(" ._-")
        if len(base) < 3:
            continue
        p = m.group(2)
        n = ord(p.upper()) - 64 if letter else int(p)
        return base, n, sure
    return None


def natural_key(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", (s or "").lower())]


def order_rows(rows):
    parts = [split_part(os.path.splitext(r["filename"])[0]) for r in rows]
    if all(parts):
        return [r for _, r in sorted(zip(parts, rows), key=lambda x: (x[0][1], natural_key(x[1]["filename"])))]
    return sorted(rows, key=lambda r: natural_key(r["filename"]))


def find_groups(conn):
    """→ [{"rows": [조각 순서대로], "sure": True/False}]  (연결된 드라이브의 영상만)"""
    ensure(conn)
    buckets = {}
    for r in library.load_videos(conn):
        if not r.get("online") or not r.get("full_path"):
            continue
        sp = split_part(os.path.splitext(r["filename"])[0])
        if not sp:
            continue
        base, n, sure = sp
        key = (os.path.normcase(os.path.dirname(r["full_path"])), base.lower())
        buckets.setdefault(key, []).append((n, sure, r))
    out = []
    for items in buckets.values():
        nums = sorted(n for n, _, _ in items)
        if not 2 <= len(items) <= MAX_PARTS or nums != list(range(1, len(items) + 1)):
            continue
        items.sort(key=lambda x: x[0])
        out.append({"rows": [r for _, _, r in items], "sure": all(s for _, s, _ in items)})
    out.sort(key=lambda g: (not g["sure"], natural_key(g["rows"][0]["filename"])))
    return out


def group_for(conn, vid):
    for g in find_groups(conn):
        if any(r["id"] == vid for r in g["rows"]):
            return g
    return None


def suggest_name(rows):
    stems = [os.path.splitext(r["filename"])[0] for r in rows]
    sp = split_part(stems[0])
    if sp and all((split_part(s) or ("",))[0].lower() == sp[0].lower() for s in stems):
        return sp[0]
    pre = os.path.commonprefix(stems).rstrip(" ._-([")
    return pre if len(pre) >= 3 else stems[0] + "_합침"


# ---------------- 형식 확인 ----------------
def _rate(s):
    for k in ("avg_frame_rate", "r_frame_rate"):
        v = s.get(k) or ""
        try:
            a, b = (float(x) for x in v.split("/"))
            if a > 0 and b > 0 and a / b <= 240:
                return v, round(a / b, 2)
        except ValueError:
            pass
    return "30", 30.0


def probe(path):
    try:
        r = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json",
                            str(path)], capture_output=True, timeout=180, creationflags=NO_WINDOW)
        data = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    except Exception:
        return None
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    auds = [s for s in streams if s.get("codec_type") == "audio"]
    try:
        dur = float((data.get("format") or {}).get("duration") or 0)
    except ValueError:
        dur = 0.0
    if v is None:
        return {"dur": dur, "v": None, "a": [], "w": 0, "h": 0, "fps": 30.0, "rate": "30", "codec": None}
    rate, fps = _rate(v)
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    return {"dur": dur, "w": w, "h": h, "fps": fps, "rate": rate, "codec": v.get("codec_name"),
            "v": (v.get("codec_name"), w, h, v.get("pix_fmt"), fps),
            "a": [(a.get("codec_name"), a.get("sample_rate"), a.get("channels")) for a in auds]}


def plan(rows, probes):
    """→ {"mode": "copy"/"encode"/None, "ext", "why"}"""
    if not rows or len(rows) < 2:
        return {"mode": None, "ext": "", "why": "합칠 영상이 2개 이상 필요합니다"}
    if any(p is None or p["v"] is None for p in probes):
        return {"mode": None, "ext": "", "why": "영상 정보를 읽지 못한 조각이 있습니다"}
    has_a = [bool(p["a"]) for p in probes]
    if any(has_a) and not all(has_a):
        return {"mode": None, "ext": "", "why": "소리가 없는 조각이 섞여 있습니다"}
    exts = {os.path.splitext(r["filename"])[1].lower() for r in rows}
    p0 = probes[0]
    why = []
    if len(exts) > 1:
        why.append("파일 형식")
    if any(p["codec"] != p0["codec"] for p in probes):
        why.append("코덱")
    if any((p["w"], p["h"]) != (p0["w"], p0["h"]) for p in probes):
        why.append("해상도")
    if any(p["fps"] != p0["fps"] for p in probes):
        why.append("fps")
    if any(p["v"][3] != p0["v"][3] for p in probes):
        why.append("색 형식")
    if any(p["a"] != p0["a"] for p in probes):
        why.append("소리 형식")
    ext = next(iter(exts))
    if not why and ext in FORMATS:
        return {"mode": "copy", "ext": ext,
                "why": "형식이 모두 같아 그대로 이어 붙입니다 (화질 그대로, 빠름)"}
    return {"mode": "encode", "ext": ".mp4",
            "why": f"조각마다 {'·'.join(why) or '형식'}이(가) 달라 GPU로 다시 인코딩합니다 (시간이 더 걸림)"}


# ---------------- ffmpeg ----------------
def _run_ffmpeg(cmd, total, progress, stop, label):
    errf = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, stdin=subprocess.DEVNULL,
                            creationflags=NO_WINDOW)
    try:
        for raw in proc.stdout:
            if stop():
                proc.kill()
                proc.wait()
                raise Cancelled()
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    t = int(line.split("=", 1)[1]) / 1e6
                except ValueError:
                    continue
                progress(min(max(t / total, 0.0), 0.99) if total else 0.0, label)
        proc.wait()
        if stop():
            raise Cancelled()
        if proc.returncode != 0:
            errf.seek(0)
            lines = errf.read().decode("utf-8", "replace").strip().splitlines()
            raise MergeError("ffmpeg 오류: " + (lines[-1] if lines else f"코드 {proc.returncode}"))
    finally:
        if proc.poll() is None:
            proc.kill()
        errf.close()


def _concat_copy(rows, tmp, ext, total, progress, stop):
    lst = Path(tempfile.gettempdir()) / f"vv_concat_{os.getpid()}_{int(time.time() * 1000)}.txt"
    lines = ["file '" + r["full_path"].replace("\\", "/").replace("'", "'\\''") + "'" for r in rows]
    lst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        _run_ffmpeg([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                     "-f", "concat", "-safe", "0", "-i", str(lst),
                     "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                     "-avoid_negative_ts", "make_zero",
                     "-progress", "pipe:1", "-nostats", "-f", FORMATS[ext], str(tmp)],
                    total, progress, stop, "이어 붙이는 중")
    finally:
        try:
            lst.unlink()
        except OSError:
            pass


def _concat_encode(rows, probes, tmp, total, progress, stop):
    big = max(probes, key=lambda p: p["w"] * p["h"])
    W, H = max(2, big["w"] // 2 * 2), max(2, big["h"] // 2 * 2)
    rate = probes[0]["rate"]
    audio = bool(probes[0]["a"])
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    for r in rows:
        cmd += ["-i", r["full_path"]]
    parts, labels = [], ""
    for i in range(len(rows)):
        parts.append(f"[{i}:v:0]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                     f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={rate},format=yuv420p[v{i}]")
        labels += f"[v{i}]"
        if audio:
            parts.append(f"[{i}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
            labels += f"[a{i}]"
    graph = ";".join(parts) + f";{labels}concat=n={len(rows)}:v=1:a={1 if audio else 0}[v]" + ("[a]" if audio else "")
    base = cmd + ["-filter_complex", graph, "-map", "[v]"]
    if audio:
        base += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    tail = ["-progress", "pipe:1", "-nostats", "-f", "mp4", str(tmp)]
    try:
        _run_ffmpeg(base + ["-c:v", "h264_nvenc", "-preset", "p5", "-profile:v", "high",
                            "-rc", "vbr", "-cq", "21", "-b:v", "0"] + tail,
                    total, progress, stop, "GPU 인코딩")
    except MergeError as e:
        print("[합치기] GPU 인코더 실패 → CPU로 다시:", e)
        progress(0.0, "CPU 인코딩 (GPU 실패)")
        _run_ffmpeg(base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"] + tail,
                    total, progress, stop, "CPU 인코딩")


# ---------------- 도우미 ----------------
def _clean_name(s):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (s or "").strip()).strip(" .")
    return s[:150] or "합친 영상"


def _unique_path(p):
    p = Path(p)
    if not p.exists():
        return p
    for n in range(2, 1000):
        q = p.with_name(f"{p.stem} ({n}){p.suffix}")
        if not q.exists():
            return q
    raise MergeError("같은 이름의 파일이 너무 많습니다")


def _hide(p):
    try:
        ctypes.windll.kernel32.SetFileAttributesW(str(p), 0x2)
    except Exception:
        pass


def rmdir_up(d, root):
    """보관 폴더 안의 빈 폴더 정리"""
    arch = os.path.normcase(os.path.join(root, ARCHIVE_NAME))
    d = os.path.normpath(d)
    while os.path.normcase(d).startswith(arch):
        try:
            os.rmdir(d)
        except OSError:
            break
        if os.path.normcase(d) == arch:
            break
        d = os.path.dirname(d)


# ---------------- DB 등록·정보 옮기기 ----------------
def _register(conn, final):
    full = str(final)
    letter = os.path.splitdrive(full)[0].upper()
    drive_id = get_or_create_drive(conn, letter)
    conn.commit()
    rel = os.path.relpath(full, letter + "\\")
    st_ = os.stat(full)
    info = media.probe(full) or {}
    row = conn.execute("SELECT id FROM videos WHERE drive_id=? AND rel_path=?", (drive_id, rel)).fetchone()
    if row:                                   # 자동 스캔이 먼저 등록한 경우
        vid = row[0]
        conn.execute("UPDATE videos SET is_new=0, is_missing=0 WHERE id=?", (vid,))
    else:
        key = base = media.file_key(full, st_.st_size)
        n = 2
        while conn.execute("SELECT 1 FROM videos WHERE file_key=?", (key,)).fetchone():
            key = f"{base}_{n}"
            n += 1
        cols = ", ".join(META_COLS)
        marks = ", ".join("?" * len(META_COLS))
        cur = conn.execute(
            f"INSERT INTO videos (file_key, drive_id, rel_path, filename, size, mtime, is_new, {cols}) "
            f"VALUES (?,?,?,?,?,?,0,{marks})",
            (key, drive_id, rel, final.name, st_.st_size, st_.st_mtime, *[info.get(c) for c in META_COLS]))
        vid = cur.lastrowid
    conn.commit()
    dur = float(info.get("duration") or 0)
    t = min(max(dur * 0.1, 1.0), dur - 1) if dur > 2 else 0
    try:
        name = f"{vid}.jpg"
        if media.make_thumb(full, Path(THUMB_DIR) / name, t, duration=dur):
            conn.execute("UPDATE videos SET thumb_path=?, thumb_time=? WHERE id=?", (name, t, vid))
            conn.commit()
    except Exception as e:
        print("[합치기] 썸네일 실패:", e)
    return vid, info


_TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def read_srt(path):
    items = []
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    for block in re.split(r"\r?\n\s*\r?\n", text):
        lines = block.strip().splitlines()
        for i, ln in enumerate(lines):
            m = _TS.search(ln)
            if m:
                g = [int(x) for x in m.groups()]
                a = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
                b = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
                body = "\n".join(lines[i + 1:]).strip()
                if body:
                    items.append((a, b, body))
                break
    return items


def _merge_subs(conn, new, ids, offs):
    from app import subtitle as st
    st.ensure_table(conn)
    found = {}
    for vid, off in zip(ids, offs):
        for kind, lang, path in st.subs_for(conn, vid):
            try:
                its = read_srt(path)
            except OSError:
                continue
            f = found.setdefault(kind, {"langs": [], "items": []})
            f["langs"].append(lang)
            f["items"] += [(a + off, b + off, t) for a, b, t in its]
    lines = 0
    for kind, f in found.items():
        if not f["items"]:
            continue
        lang = max(set(f["langs"]), key=f["langs"].count)
        if kind == "ko":
            name = f"v{new}.ko.srt"
        else:
            name = f"v{new}.orig.srt" if lang == "ko" else f"v{new}.{lang}.srt"
        p = st.SUB_DIR / name
        st.write_srt(p, f["items"])
        st._save(conn, new, kind, lang, p)
        lines = max(lines, len(f["items"]))
    return lines


def _copy_marks(conn, new, ids, offs):
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(scene_marks)")]
    except Exception:
        return 0
    if "video_id" not in cols or "t" not in cols:
        return 0
    from app import marks
    mdir = Path(marks.MARK_DIR)
    keep = [c for c in cols if c != "id"]
    n = 0
    for vid, off in zip(ids, offs):
        for row in conn.execute(f"SELECT {', '.join(keep)} FROM scene_marks WHERE video_id=?", (vid,)).fetchall():
            d = dict(zip(keep, row))
            d["video_id"] = new
            d["t"] = float(d["t"] or 0) + off
            th = d.get("thumb")
            if th:
                src = Path(th) if Path(th).is_absolute() else mdir / th
                if src.is_file():
                    dst = mdir / f"m{new}_{n}_{int(time.time())}.jpg"
                    shutil.copy2(src, dst)
                    d["thumb"] = str(dst) if Path(th).is_absolute() else dst.name
                else:
                    d["thumb"] = None
            conn.execute(f"INSERT OR IGNORE INTO scene_marks ({', '.join(keep)}) "
                         f"VALUES ({', '.join('?' * len(keep))})", [d[c] for c in keep])
            n += 1
    return n


def _merge_info(conn, new, rows, durs):
    from app import dups
    ids = [r["id"] for r in rows]
    offs = [sum(durs[:i]) for i in range(len(rows))]
    for vid in ids:
        dups.copy_links(conn, new, vid)
    q = ",".join("?" * len(ids))
    olds = conn.execute(f"SELECT rating, play_count, memo, last_played FROM videos WHERE id IN ({q})", ids).fetchall()
    memo = "\n".join(dict.fromkeys(o["memo"] for o in olds if o["memo"])) or None
    lp = [o["last_played"] for o in olds if o["last_played"]]
    conn.execute("UPDATE videos SET rating=?, play_count=?, memo=?, last_played=? WHERE id=?",
                 (max((o["rating"] or 0) for o in olds), max((o["play_count"] or 0) for o in olds),
                  memo, max(lp) if lp else None, new))
    n_mark = _copy_marks(conn, new, ids, offs)
    n_sub = _merge_subs(conn, new, ids, offs)
    conn.commit()
    return n_sub, n_mark


def _archive(conn, new, merged_name, rows, mode):
    stamp = time.strftime("%Y%m%d_%H%M%S")
    job = conn.execute("INSERT INTO merge_jobs(merged_id, merged_name, created, total_size, mode) "
                       "VALUES (?,?,?,0,?)", (new, merged_name, time.time(), mode)).lastrowid
    conn.commit()
    moved, failed, size = 0, [], 0
    for i, r in enumerate(rows):
        full = r["full_path"]
        root = os.path.splitdrive(full)[0].upper() + "\\"
        arch_root = Path(root, ARCHIVE_NAME)
        try:
            arch_dir = arch_root / f"{stamp}_{job}"
            arch_dir.mkdir(parents=True, exist_ok=True)
            _hide(arch_root)
            dst = arch_dir / os.path.basename(full)
            fsize = os.path.getsize(full)
            os.replace(full, dst)
        except OSError as e:
            failed.append(f"{r['filename']}: {e}")
            continue
        arch_rel = os.path.relpath(str(dst), root)
        conn.execute("UPDATE videos SET rel_path=?, excluded=1 WHERE id=?", (arch_rel, r["id"]))
        conn.execute("INSERT OR REPLACE INTO merge_items VALUES (?,?,?,?,?,?)",
                     (job, r["id"], r["drive_id"], r["rel_path"], arch_rel, i))
        conn.commit()
        moved += 1
        size += fsize
    if moved:
        conn.execute("UPDATE merge_jobs SET total_size=? WHERE id=?", (size, job))
    else:
        conn.execute("DELETE FROM merge_jobs WHERE id=?", (job,))
    conn.commit()
    return moved, failed


# ---------------- 합치기 ----------------
def run_merge(conn, rows, out_name, progress=None, stop=None):
    prog = progress or (lambda p, s: None)
    stop = stop or (lambda: False)
    ensure(conn)
    t0 = time.time()
    if len(rows) < 2:
        raise MergeError("합칠 영상이 2개 이상 필요합니다")
    for r in rows:
        if not r.get("full_path") or not os.path.isfile(r["full_path"]):
            raise MergeError(f"파일을 찾을 수 없습니다: {r['filename']}")
    prog(0.0, "조각 확인")
    probes = [probe(r["full_path"]) for r in rows]
    pl = plan(rows, probes)
    if not pl["mode"]:
        raise MergeError(pl["why"])
    folder = os.path.dirname(rows[0]["full_path"])
    total_size = sum(os.path.getsize(r["full_path"]) for r in rows)
    need = int(total_size * 1.05) + 500 * 1024 ** 2
    free = shutil.disk_usage(folder).free
    if free < need:
        raise MergeError(f"드라이브 공간이 부족합니다 (필요 약 {fmt_size(need)}, 남은 공간 {fmt_size(free)})")
    durs = [p["dur"] or float(r.get("duration") or 0) for p, r in zip(probes, rows)]
    total = sum(durs)
    final = _unique_path(Path(folder) / (_clean_name(out_name) + pl["ext"]))
    tmp = final.with_name(final.name + ".vvpart")
    try:
        if pl["mode"] == "copy":
            _concat_copy(rows, tmp, pl["ext"], total, prog, stop)
        else:
            _concat_encode(rows, probes, tmp, total, prog, stop)
        prog(0.995, "결과 확인")
        out = probe(tmp)
        if not out or out["v"] is None:
            raise MergeError("합친 파일을 읽을 수 없습니다")
        tol = max(2.0, 1.0 * len(rows))
        if abs(out["dur"] - total) > tol:
            raise MergeError(f"길이가 맞지 않아 취소했습니다 (예상 {fmt_duration(total)}, "
                             f"결과 {fmt_duration(out['dur'])})")
        os.replace(tmp, final)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    # ---- 여기부터는 합친 파일이 생긴 상태 ----
    try:
        vid, info = _register(conn, final)
    except Exception as e:
        raise MergeError(f"합친 파일은 만들었지만 목록 등록에 실패했습니다: {e}\n"
                         "'🔄 새 영상 확인'을 누르면 목록에 나타납니다. 원본 조각은 그대로 있습니다.")
    warn = []
    n_sub = n_mark = 0
    try:
        n_sub, n_mark = _merge_info(conn, vid, rows, durs)
    except Exception as e:
        conn.rollback()
        warn.append(f"정보 옮기기 일부 실패: {e}")
    moved, failed = _archive(conn, vid, final.name, rows, pl["mode"])
    return {"vid": vid, "name": final.name, "mode": pl["mode"], "dur": out["dur"],
            "moved": moved, "failed": warn + failed, "subs": n_sub, "marks": n_mark,
            "secs": time.time() - t0}


# ---------------- 보관함 ----------------
def list_jobs(conn):
    ensure(conn)
    letters = drive_letters(conn)
    days = get_days(conn)
    out = []
    for j in conn.execute("SELECT * FROM merge_jobs ORDER BY created DESC").fetchall():
        items = conn.execute("SELECT * FROM merge_items WHERE job_id=? ORDER BY ord", (j["id"],)).fetchall()
        if not items:
            conn.execute("DELETE FROM merge_jobs WHERE id=?", (j["id"],))
            continue
        online = all(letters.get(it["drive_id"]) for it in items)
        first = None
        if letters.get(items[0]["drive_id"]):
            first = os.path.join(letters[items[0]["drive_id"]] + "\\", items[0]["arch_rel"])
        left = None if not days else days - (time.time() - (j["created"] or 0)) / 86400
        out.append({"id": j["id"], "merged_id": j["merged_id"], "merged_name": j["merged_name"],
                    "created": j["created"], "size": j["total_size"] or 0, "mode": j["mode"],
                    "n": len(items), "online": online, "first": first, "left": left,
                    "names": [os.path.basename(it["orig_rel"]) for it in items]})
    conn.commit()
    return out


def expired_jobs(conn):
    return [j for j in list_jobs(conn) if j["left"] is not None and j["left"] <= 0]


def restore_job(conn, job_id):
    """원본 조각을 원래 자리로 → (되돌린 수, 실패 목록, 합친 영상 id, 모두 되돌렸는지)"""
    ensure(conn)
    job = conn.execute("SELECT * FROM merge_jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        return 0, ["보관 기록이 없습니다"], None, False
    letters = drive_letters(conn)
    ok, fails = 0, []
    for it in conn.execute("SELECT * FROM merge_items WHERE job_id=? ORDER BY ord", (job_id,)).fetchall():
        name = os.path.basename(it["orig_rel"])
        L = letters.get(it["drive_id"])
        if not L:
            fails.append(f"{name}: 외장하드 연결 안 됨")
            continue
        root = L + "\\"
        src = os.path.join(root, it["arch_rel"])
        if not os.path.exists(src):
            fails.append(f"{name}: 보관 파일이 없습니다")
            continue
        dst = _unique_path(Path(root, it["orig_rel"]))
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)
        except OSError as e:
            fails.append(f"{name}: {e}")
            continue
        conn.execute("UPDATE videos SET rel_path=?, filename=?, excluded=0, is_missing=0 WHERE id=?",
                     (os.path.relpath(str(dst), root), dst.name, it["video_id"]))
        conn.execute("DELETE FROM merge_items WHERE job_id=? AND video_id=?", (job_id, it["video_id"]))
        conn.commit()
        ok += 1
        rmdir_up(os.path.dirname(src), root)
    left = conn.execute("SELECT COUNT(*) FROM merge_items WHERE job_id=?", (job_id,)).fetchone()[0]
    if not left:
        conn.execute("DELETE FROM merge_jobs WHERE id=?", (job_id,))
        conn.commit()
    return ok, fails, job["merged_id"], left == 0


def _main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass
    from app.db import init_db
    conn = init_db()
    groups = find_groups(conn)
    print(f"===== 나뉜 영상 묶음 {len(groups)}개 (연결된 드라이브 기준) =====")
    for i, g in enumerate(groups, 1):
        total = sum(r.get("duration") or 0 for r in g["rows"])
        print(f"[{i}] {'확실' if g['sure'] else '추정'} · {len(g['rows'])}조각 · {fmt_duration(total)}")
        for r in g["rows"]:
            print(f"     {r['filename']}")
    print(f"\n보관함: {len(list_jobs(conn))}묶음 · 자동 삭제 "
          f"{str(get_days(conn)) + '일' if get_days(conn) else '꺼짐'}")
    conn.close()


if __name__ == "__main__":
    _main()
