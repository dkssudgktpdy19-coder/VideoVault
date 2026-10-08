"""자막 일괄 만들기 (5-0): 등록 폴더 전체를 밤새 처리, 중단해도 이어서 함
python -m app.sub_batch                  등록 폴더 전체 (제외 폴더 빼고)
python -m app.sub_batch "F:\\폴더"       이 폴더만
  --status          진행 현황만 보기
  --limit 5         앞에서 5개만 (속도 시험)
  --short-first     짧은 영상부터
  --lang ja         말하는 언어 직접 지정
  --orig-only       번역 없이 원문만
  --slow            예전 방식(한 구간씩)으로 인식
  --retry           건너뛰기 목록(말소리 없음·반복 실패) 다시 시도
  --redo-recent 5   가장 최근에 만든 자막 5개를 다시 만들기
  --shutdown        다 끝나면 2분 뒤 PC 끄기 (취소: shutdown /a)
"""
from app import subtitle as st  # GPU 준비 포함 (반드시 맨 위)

import argparse
import ctypes
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from app import library
from app.config import DB_PATH

BATCH_SIZE = 8          # 한 번에 인식할 구간 수 (VRAM 8GB에 맞춤)
MAX_FAILS = 2           # 이만큼 실패하면 건너뛰기
NO_SPEECH = ("말소리를 찾지 못했습니다", "소리를 읽지 못했습니다")
MAX_CHARS = 42          # 자막 한 줄 최대 글자
MAX_DUR = 7.0           # 자막 한 줄 최대 시간(초)
DETECT_SEGS = 6         # 언어 판단에 들을 구간 수
JA_RESCUE = 0.15        # 중국어로 나와도 일본어 가능성이 이 이상이면 일본어


def open_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure(conn):
    st.ensure_table(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS sub_skip(
        video_id INTEGER PRIMARY KEY, reason TEXT, fails INTEGER DEFAULT 0, at REAL)""")
    conn.commit()


def mark_skip(conn, vid, reason):
    conn.execute("""INSERT INTO sub_skip(video_id, reason, fails, at) VALUES (?,?,1,?)
        ON CONFLICT(video_id) DO UPDATE SET reason=excluded.reason,
        fails=sub_skip.fails+1, at=excluded.at""", (vid, reason, time.time()))
    conn.commit()


def fmt_h(sec):
    sec = int(max(sec, 0))
    h, m = sec // 3600, sec % 3600 // 60
    return f"{h}시간 {m}분" if h else f"{m}분 {sec % 60}초"


def targets(conn, folder=None):
    """→ (만들 영상 목록, 연결 안 됨 수, 자막 있음 수, 건너뛰기 수)"""
    rows = library.load_videos(conn)   # 제외 폴더는 여기서 이미 빠짐
    have = {r[0] for r in conn.execute("SELECT DISTINCT video_id FROM sub_files")}
    skip = {r[0] for r in conn.execute(
        "SELECT video_id FROM sub_skip WHERE reason='nospeech' OR fails>=?", (MAX_FAILS,))}
    one_file = base = None
    if folder:
        f = os.path.normcase(os.path.abspath(folder))
        if os.path.isfile(folder):
            one_file = f
        else:
            base = f.rstrip("\\/") + os.sep
    out, offline, n_have, n_skip = [], 0, 0, 0
    for r in rows:
        p = r.get("full_path")
        if p:
            np_ = os.path.normcase(os.path.abspath(p))
            if one_file and np_ != one_file:
                continue
            if base and not np_.startswith(base):
                continue
        elif folder:
            continue
        if r["id"] in have:
            n_have += 1
            continue
        if r["id"] in skip:
            n_skip += 1
            continue
        if not r.get("online") or not p:
            offline += 1
            continue
        out.append((r["id"], p, float(r.get("duration") or 0)))
    return out, offline, n_have, n_skip


def recent_targets(conn, n):
    """가장 최근에 자막을 만든 영상 n개"""
    ids = [r[0] for r in conn.execute(
        "SELECT video_id, MAX(rowid) m FROM sub_files GROUP BY video_id "
        "ORDER BY m DESC LIMIT ?", (n,))]
    rows = {r["id"]: r for r in library.load_videos(conn)}
    out = []
    for vid in ids:
        r = rows.get(vid)
        if r and r.get("online") and r.get("full_path"):
            out.append((vid, r["full_path"], float(r.get("duration") or 0)))
    return out


# ---------------- 긴 줄 나누기 ----------------
_SPLIT = re.compile(r"(?<=[。！？!?、，,…])\s*|\s+")


def split_long(items):
    out = []
    for s, e, t in items:
        if len(t) <= MAX_CHARS and e - s <= MAX_DUR:
            out.append((s, e, t))
            continue
        parts = [p for p in _SPLIT.split(t) if p and p.strip()]
        chunks, cur = [], ""
        for p in parts:
            while len(p) > MAX_CHARS:            # 문장부호 없이 긴 덩어리
                if cur:
                    chunks.append(cur)
                    cur = ""
                chunks.append(p[:MAX_CHARS])
                p = p[MAX_CHARS:]
            sep = " " if cur and re.match(r"[A-Za-z0-9]", p[:1]) else ""
            if cur and len(cur) + len(sep) + len(p) > MAX_CHARS:
                chunks.append(cur)
                cur = p
            else:
                cur = cur + sep + p
        if cur:
            chunks.append(cur)
        if len(chunks) <= 1:
            out.append((s, e, t))
            continue
        total = sum(len(c) for c in chunks) or 1
        pos = s
        for c in chunks:
            d = (e - s) * len(c) / total
            out.append((pos, pos + d, c.strip()))
            pos += d
    return out


# ---------------- 언어 판단 (여러 구간) ----------------
def detect_lang(model, audio):
    try:
        code, prob, allp = model.detect_language(
            audio, vad_filter=True, language_detection_segments=DETECT_SEGS)
        probs = dict(allp) if allp else {}
        if code == "zh" and probs.get("ja", 0) >= JA_RESCUE:
            return "ja"
        return code
    except Exception as e:
        print(f"\n[자막] 언어 판단 실패, 기본 방식 사용: {e}")
        return None


# ---------------- 빠른 인식 (여러 구간 동시 처리) ----------------
_orig_transcribe = st.transcribe
_pipe = [None, None]          # [모델, 파이프라인]
_fast_off = [False]
_ts_ok = [True]               # 문장별 시간 설정이 되는지


def transcribe_fast(eng, wav, lang, progress, stop):
    if _fast_off[0]:
        return _orig_transcribe(eng, wav, lang, progress, stop)
    try:
        from faster_whisper import BatchedInferencePipeline, decode_audio
        model = eng.whisper()
        if _pipe[0] is not model:
            _pipe[0], _pipe[1] = model, BatchedInferencePipeline(model=model)
        audio = decode_audio(str(wav), sampling_rate=16000)
        if not lang:
            lang = detect_lang(model, audio)
        kw = dict(language=lang, task="transcribe", beam_size=st.BEAM,
                  batch_size=BATCH_SIZE, vad_filter=True,
                  vad_parameters={"min_silence_duration_ms": 500})
        if _ts_ok[0]:
            try:
                segs, info = _pipe[1].transcribe(audio, without_timestamps=False, **kw)
            except TypeError:
                _ts_ok[0] = False
                segs, info = _pipe[1].transcribe(audio, **kw)
        else:
            segs, info = _pipe[1].transcribe(audio, **kw)
        dur = max(float(info.duration or 0), 1.0)
        items = []
        for s in segs:
            text = (s.text or "").strip()
            nsp = getattr(s, "no_speech_prob", 0) or 0
            alp = getattr(s, "avg_logprob", 0) or 0
            if text and not (nsp > 0.6 and alp < -0.8):
                items.append((float(s.start), float(s.end), text))
            progress(min(1.0, s.end / dur))
        del audio
        return split_long(st._dedupe(items)), info.language
    except st.Cancelled:
        raise
    except Exception as e:
        if st._gpu_error(e):
            raise                      # GPU 문제는 기존 방식대로 CPU 전환
        print(f"\n[자막] 빠른 방식이 안 돼서 예전 방식으로 바꿉니다: {e}")
        _fast_off[0] = True
        return _orig_transcribe(eng, wav, lang, progress, stop)


# ---------------- 절전 막기 ----------------
def keep_awake(on):
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            0x80000000 | (0x00000001 if on else 0))
    except Exception:
        pass


# ---------------- 실행 ----------------
def status(folder):
    conn = open_db()
    ensure(conn)
    vids, offline, have, skip = targets(conn, folder)
    nosp = conn.execute("SELECT COUNT(*) FROM sub_skip WHERE reason='nospeech'").fetchone()[0]
    print(f"자막 있음 {have}개 | 남음 {len(vids)}개 (총 길이 {fmt_h(sum(v[2] for v in vids))})")
    print(f"건너뛰기 {skip}개 (말소리 없음 {nosp}개 포함) | 연결 안 된 드라이브의 영상 {offline}개")
    conn.close()


def run(folder, limit, short_first, lang, orig_only, slow, shutdown, redo_recent):
    conn = open_db()
    ensure(conn)
    redo = bool(redo_recent)
    if redo:
        vids = recent_targets(conn, redo_recent)
        print(f"최근 자막 {len(vids)}개를 다시 만듭니다")
    else:
        vids, offline, have, skip = targets(conn, folder)
        print(f"자막 있음 {have}개 | 건너뛰기 {skip}개 | 연결 안 된 드라이브 {offline}개")
    if short_first:
        vids.sort(key=lambda v: v[2])
    if limit:
        vids = vids[:limit]
    total = sum(v[2] for v in vids)
    print(f"이번에 만들 영상 {len(vids)}개 (총 길이 {fmt_h(total)})")
    if not vids:
        conn.close()
        return
    if not slow:
        st.transcribe = transcribe_fast
    eng = st.Engine()
    print("엔진:", eng.device.upper(), "| 방식:", "예전" if slow else f"빠름(묶음 {BATCH_SIZE})")
    print("멈추기: Ctrl+C (다시 실행하면 남은 영상부터)\n")
    keep_awake(True)
    t0, ok, fail, nosp, done_sec = time.time(), 0, 0, 0, 0.0
    finished = False
    n = len(vids)
    try:
        for i, (vid, path, dur) in enumerate(vids, 1):
            name = Path(path).name[:40]
            el = time.time() - t0
            eta = f"남은 시간 약 {fmt_h((total - done_sec) * el / done_sec)}" if done_sec > 0 else ""
            if not os.path.isfile(path):
                fail += 1
                print(f"  [{i}/{n}] ❌ 파일 없음  {name}")
                done_sec += dur
                continue

            def prog(stage, p, i=i, name=name, eta=eta):
                print(f"\r  [{i}/{n}] {stage} {p * 100:3.0f}%  {eta}  {name}   ", end="", flush=True)

            t1 = time.time()
            try:
                if redo:
                    conn.execute("DELETE FROM sub_files WHERE video_id=?", (vid,))
                    conn.commit()
                src, cnt, tr = st.make_subs(eng, conn, vid, path, lang, not orig_only, prog)
                conn.execute("DELETE FROM sub_skip WHERE video_id=?", (vid,))
                conn.commit()
                ok += 1
                print(f"\r  [{i}/{n}] ✅ {st.lang_name(src)} {cnt}줄"
                      f"{' → 한국어' if tr else ''} ({time.time() - t1:.0f}초)  {name}          ")
            except KeyboardInterrupt:
                raise
            except Exception as e:
                conn.rollback()
                msg = str(e)
                if any(k in msg for k in NO_SPEECH):
                    nosp += 1
                    mark_skip(conn, vid, "nospeech")
                    print(f"\r  [{i}/{n}] 🔇 말소리 없음  {name}                    ")
                else:
                    fail += 1
                    mark_skip(conn, vid, "error")
                    print(f"\r  [{i}/{n}] ❌ {name}: {msg[:80]}          ")
            done_sec += dur
        finished = True
    except KeyboardInterrupt:
        print("\n⏸ 중단함. 다시 실행하면 남은 영상부터 이어서 합니다.")
    finally:
        keep_awake(False)
        eng.close()
        conn.close()
    if not _ts_ok[0]:
        print("(참고: 문장별 시간 설정이 지원되지 않아 긴 줄 나누기로 대신했습니다)")
    print(f"===== 완료: 성공 {ok}개, 말소리 없음 {nosp}개, 실패 {fail}개, "
          f"{fmt_h(time.time() - t0)} =====")
    if shutdown and finished:
        subprocess.run(["shutdown", "/s", "/t", "120"])
        print("⏻ 2분 뒤 PC가 꺼집니다. 취소하려면: shutdown /a")


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--short-first", action="store_true")
    ap.add_argument("--lang")
    ap.add_argument("--orig-only", action="store_true")
    ap.add_argument("--slow", action="store_true")
    ap.add_argument("--retry", action="store_true")
    ap.add_argument("--redo-recent", type=int)
    ap.add_argument("--shutdown", action="store_true")
    a = ap.parse_args()
    if a.retry:
        c = open_db()
        ensure(c)
        c.execute("DELETE FROM sub_skip")
        c.commit()
        c.close()
        print("건너뛰기 목록을 비웠습니다.")
    if a.status:
        status(a.target)
    else:
        run(a.target, a.limit, a.short_first, a.lang, a.orig_only, a.slow,
            a.shutdown, a.redo_recent)


if __name__ == "__main__":
    main()
