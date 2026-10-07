from app import cuda_dlls  # GPU 부품 위치 먼저 알려주기 (반드시 맨 위)

"""음성 인식 + 한국어 번역 자막 (3단계-4)
  python -m app.subtitle --setup                 모델 내려받기 + GPU 확인 (처음 한 번)
  python -m app.subtitle "영상 파일 또는 폴더"     자막 만들기
      --lang ja      말하는 언어 직접 지정 (en 영어, ja 일본어, zh 중국어 …)
      --orig-only    번역 없이 원문 자막만
      --redo         자막이 이미 있어도 다시 만들기
      --limit 5      앞에서 5개만
"""
import argparse
import gc
import os
import site
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


from app import config
from app import faces as fc

WHISPER_SIZE = "large-v3-turbo"
BEAM = 5
TR_BATCH = 24
SUB_DIR = Path(getattr(config, "SUB_DIR", fc.DATA_DIR / "subs"))
SUB_DIR.mkdir(parents=True, exist_ok=True)
WHISPER_DIR = fc.MODEL_DIR / "whisper-turbo"
NLLB_DIR = fc.MODEL_DIR / "nllb-1.3b"
NLLB_REPO = "OpenNMT/nllb-200-distilled-1.3B-ct2-int8"
TARGET = "kor_Hang"
NLLB_LANG = {"en": "eng_Latn", "ja": "jpn_Jpan", "zh": "zho_Hans", "es": "spa_Latn",
             "fr": "fra_Latn", "de": "deu_Latn", "ru": "rus_Cyrl", "pt": "por_Latn",
             "it": "ita_Latn", "th": "tha_Thai", "vi": "vie_Latn", "id": "ind_Latn",
             "tr": "tur_Latn", "ar": "arb_Arab", "hi": "hin_Deva"}
LANG_NAME = {"ko": "한국어", "en": "영어", "ja": "일본어", "zh": "중국어", "es": "스페인어",
             "fr": "프랑스어", "de": "독일어", "ru": "러시아어", "pt": "포르투갈어",
             "it": "이탈리아어", "th": "태국어", "vi": "베트남어", "id": "인도네시아어",
             "tr": "튀르키예어", "ar": "아랍어", "hi": "힌디어"}


class Cancelled(Exception):
    pass


def lang_name(code):
    return LANG_NAME.get(code or "", code or "?")


# ---------------- GPU 준비 (가상환경 안의 CUDA 파일 사용) ----------------
_DLL_HANDLES = []
_DLL_READY = False


def setup_gpu_dlls():
    global _DLL_READY
    if _DLL_READY:
        return
    _DLL_READY = True
    roots = {str(Path(sys.prefix) / "Lib" / "site-packages")}
    try:
        roots.update(site.getsitepackages())
    except Exception:
        pass
    for r in roots:
        for b in Path(r, "nvidia").glob("*/bin"):
            try:
                _DLL_HANDLES.append(os.add_dll_directory(str(b)))
            except OSError:
                continue
            os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")
    try:
        import onnxruntime as ort
        if hasattr(ort, "preload_dlls"):
            ort.preload_dlls()
    except Exception:
        pass


def cuda_available():
    setup_gpu_dlls()
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _gpu_error(e):
    s = str(e).lower()
    return any(k in s for k in ("cuda", "cublas", "cudnn", ".dll", "out of memory"))


# ---------------- 엔진 ----------------
class Engine:
    def __init__(self):
        self.device = "cuda" if cuda_available() else "cpu"
        self._whisper = None
        self._tr = None
        self._tok = None

    def _ct(self, gpu):
        return gpu if self.device == "cuda" else "int8"

    def to_cpu(self, err):
        print("\n[자막] GPU를 쓰지 못해 CPU로 바꿉니다 (느림):", err)
        self.close()
        self.device = "cpu"

    def whisper(self):
        if self._whisper is None:
            from faster_whisper import WhisperModel
            src = str(WHISPER_DIR) if (WHISPER_DIR / "model.bin").exists() else WHISPER_SIZE
            self._whisper = WhisperModel(src, device=self.device, compute_type=self._ct("float16"))
        return self._whisper

    def _translator(self):
        if self._tr is None:
            if not (NLLB_DIR / "model.bin").exists():
                raise FileNotFoundError("번역 모델이 없습니다. python -m app.subtitle --setup 먼저 실행")
            import ctranslate2
            import tokenizers
            self._tr = ctranslate2.Translator(str(NLLB_DIR), device=self.device,
                                              compute_type=self._ct("int8_float16"))
            self._tok = tokenizers.Tokenizer.from_file(str(NLLB_DIR / "tokenizer.json"))
        return self._tr, self._tok

    def translate(self, texts, src_lang, progress=None, stop=None):
        tr, tok = self._translator()
        code = NLLB_LANG[src_lang]
        out = []
        for i in range(0, len(texts), TR_BATCH):
            if stop and stop():
                raise Cancelled()
            chunk = texts[i:i + TR_BATCH]
            src = [[code] + tok.encode(t, add_special_tokens=False).tokens + ["</s>"] for t in chunk]
            res = tr.translate_batch(src, target_prefix=[[TARGET]] * len(src), beam_size=4,
                                     max_decoding_length=200, repetition_penalty=1.1)
            for r in res:
                ids = [tok.token_to_id(t) for t in r.hypotheses[0] if t != TARGET]
                out.append(tok.decode([x for x in ids if x is not None],
                                      skip_special_tokens=True).strip())
            if progress:
                progress(min(1.0, (i + len(chunk)) / len(texts)))
        return out

    def close(self):
        self._whisper = self._tr = self._tok = None
        gc.collect()


def _retry(eng, fn):
    try:
        return fn()
    except Cancelled:
        raise
    except Exception as e:
        if eng.device == "cuda" and _gpu_error(e):
            eng.to_cpu(e)
            return fn()
        raise


# ---------------- DB ----------------
def ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS sub_files(
        video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        kind TEXT NOT NULL, lang TEXT, path TEXT NOT NULL, created REAL,
        PRIMARY KEY(video_id, kind))""")
    conn.commit()


def subs_for(conn, vid):
    return [(k, l, p) for k, l, p in conn.execute(
        "SELECT kind, lang, path FROM sub_files WHERE video_id=?", (vid,)) if p and os.path.isfile(p)]


def clear_subs(conn, vid):
    for (p,) in conn.execute("SELECT path FROM sub_files WHERE video_id=?", (vid,)).fetchall():
        fc._unlink(p)
    conn.execute("DELETE FROM sub_files WHERE video_id=?", (vid,))


def _save(conn, vid, kind, lang, path):
    conn.execute("INSERT OR REPLACE INTO sub_files(video_id, kind, lang, path, created) "
                 "VALUES (?,?,?,?,?)", (vid, kind, lang, str(path), time.time()))


# ---------------- 소리 → 글자 ----------------
def extract_audio(path, out):
    subprocess.run([fc.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", path,
                    "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)],
                   capture_output=True, creationflags=fc.NO_WINDOW)
    if not out.exists() or out.stat().st_size < 1000:
        raise RuntimeError("소리를 읽지 못했습니다 (소리 없는 영상일 수 있음)")


def _dedupe(items):
    out, run = [], 0
    for it in items:
        if out and it[2] == out[-1][2]:
            run += 1
            if run >= 2:          # 같은 문장이 계속 반복되면(인식 오류) 2번까지만
                continue
        else:
            run = 0
        out.append(it)
    return out


def transcribe(eng, wav, lang, progress, stop):
    segs, info = eng.whisper().transcribe(
        str(wav), language=lang, task="transcribe", beam_size=BEAM, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500}, condition_on_previous_text=False)
    dur = max(float(info.duration or 0), 1.0)
    items = []
    for s in segs:
        if stop and stop():
            raise Cancelled()
        text = (s.text or "").strip()
        if text and not (s.no_speech_prob > 0.6 and s.avg_logprob < -0.8):
            items.append((float(s.start), float(s.end), text))
        progress(min(1.0, s.end / dur))
    return _dedupe(items), info.language


def _ts(t):
    ms = int(round(max(t, 0) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(path, items):
    lines = []
    for i, (a, b, t) in enumerate(items, 1):
        if b - a < 0.3:
            b = a + 0.8
        lines += [str(i), f"{_ts(a)} --> {_ts(b)}", t, ""]
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def make_subs(eng, conn, vid, path, lang=None, translate=True, progress=None, stop=None):
    """→ (말한 언어, 자막 줄 수, 번역했는지)"""
    prog = progress or (lambda stage, p: None)
    ensure_table(conn)
    with tempfile.TemporaryDirectory(prefix="vv_sub_", ignore_cleanup_errors=True) as tmp:
        wav = Path(tmp) / "audio.wav"
        prog("소리 추출", 0.0)
        extract_audio(path, wav)
        items, det = _retry(eng, lambda: transcribe(eng, wav, lang,
                                                    lambda p: prog("음성 인식", p), stop))
    if not items:
        raise RuntimeError("말소리를 찾지 못했습니다")
    src = lang or det
    clear_subs(conn, vid)
    orig = SUB_DIR / f"v{vid}.{src}.srt"
    write_srt(orig, items)
    translated = False
    if src == "ko":
        _save(conn, vid, "ko", "ko", orig)
    else:
        _save(conn, vid, "orig", src, orig)
        if translate and src in NLLB_LANG:
            texts = [t for _a, _b, t in items]
            ko = _retry(eng, lambda: eng.translate(texts, src, lambda p: prog("번역", p), stop))
            kp = SUB_DIR / f"v{vid}.ko.srt"
            write_srt(kp, [(a, b, k or o) for (a, b, o), k in zip(items, ko)])
            _save(conn, vid, "ko", "ko", kp)
            translated = True
    conn.commit()
    return src, len(items), translated


# ---------------- 실행 ----------------
def setup():
    import numpy as np
    import faster_whisper
    from huggingface_hub import snapshot_download
    if not (WHISPER_DIR / "model.bin").exists():
        print("① 음성 인식 모델(Whisper turbo, 약 1.6GB) 내려받는 중…")
        faster_whisper.download_model(WHISPER_SIZE, output_dir=str(WHISPER_DIR))
    print("① 음성 인식 모델: ✅")
    if not (NLLB_DIR / "model.bin").exists():
        print("② 번역 모델(NLLB 1.3B, 약 1.4GB) 내려받는 중…")
        snapshot_download(NLLB_REPO, local_dir=str(NLLB_DIR))
    print("② 번역 모델: ✅")
    eng = Engine()
    print("GPU(CUDA):", "사용 가능" if eng.device == "cuda" else "사용 불가 → CPU")
    t0 = time.time()

    def test():
        segs, _ = eng.whisper().transcribe(np.zeros(16000 * 3, np.float32), language="en", beam_size=1)
        list(segs)
    _retry(eng, test)
    print(f"음성 인식 엔진: ✅ {eng.device.upper()} ({time.time() - t0:.1f}초)")
    for lang, text in (("en", "Hello, nice to meet you. The weather is really nice today."),
                       ("ja", "今日はとても暑いですね。一緒にご飯を食べに行きませんか？")):
        ko = _retry(eng, lambda: eng.translate([text], lang))[0]
        print(f"번역 테스트 [{lang_name(lang)}] {text}\n   → {ko}")
    print(f"✅ 준비 완료 ({eng.device.upper()})")


def find_targets(conn, target):
    p = Path(target)
    if p.is_file():
        norm = os.path.normcase(os.path.normpath(str(p)))
        return [v for v in fc.find_videos(conn, str(p.parent))
                if os.path.normcase(os.path.normpath(v[1])) == norm]
    if p.is_dir():
        return fc.find_videos(conn, str(p))
    return []


def run_cli(target, lang, orig_only, redo, limit):
    conn = fc.open_db()
    ensure_table(conn)
    vids = find_targets(conn, target)
    if not vids:
        print("자막을 만들 영상을 찾지 못했습니다 (VideoVault에 등록·스캔된 영상만 가능)")
        return
    if not redo:
        have = {r[0] for r in conn.execute("SELECT DISTINCT video_id FROM sub_files")}
        vids = [v for v in vids if v[0] not in have]
    if limit:
        vids = vids[:limit]
    print(f"자막 만들 영상 {len(vids)}개")
    if not vids:
        return
    eng = Engine()
    print("엔진:", eng.device.upper())
    t0, ok, fail = time.time(), 0, 0
    try:
        for i, (vid, path, _dur) in enumerate(vids, 1):
            name = Path(path).name[:40]

            def prog(stage, p, i=i, name=name):
                print(f"\r  [{i}/{len(vids)}] {stage} {p * 100:3.0f}%  {name}      ", end="", flush=True)
            t1 = time.time()
            try:
                src, n, tr = make_subs(eng, conn, vid, path, lang, not orig_only, prog)
                ok += 1
                print(f"\r  [{i}/{len(vids)}] ✅ {lang_name(src)} {n}줄"
                      f"{' → 한국어 번역' if tr else ''} ({time.time() - t1:.0f}초)  {name}      ")
            except Exception as e:
                fail += 1
                print(f"\r  [{i}/{len(vids)}] ❌ {name}: {e}      ")
    except KeyboardInterrupt:
        print("\n⏸ 중단함 (다시 실행하면 남은 영상부터)")
    print(f"===== 완료: 성공 {ok}개, 실패 {fail}개, {time.time() - t0:.0f}초 =====")
    eng.close()
    conn.close()


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?")
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--lang")
    ap.add_argument("--orig-only", action="store_true")
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    if a.setup:
        setup()
    elif a.target:
        run_cli(a.target, a.lang, a.orig_only, a.redo, a.limit)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
