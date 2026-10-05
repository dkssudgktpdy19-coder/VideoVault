"""FFmpeg로 영상 정보 읽기, 썸네일 만들기, 파일 고유 ID 만들기"""
import hashlib
import json
import os
import subprocess

NO_WINDOW = subprocess.CREATE_NO_WINDOW   # 검은 창 안 뜨게
CHUNK = 1024 * 1024                       # 1MB


def file_key(path, size):
    """파일 고유 ID: 크기 + 앞·중간·끝 1MB 해시 (빠름)"""
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(CHUNK))
        if size > CHUNK * 3:
            f.seek(size // 2)
            h.update(f.read(CHUNK))
            f.seek(size - CHUNK)
            h.update(f.read(CHUNK))
    return f"{size:x}-{h.hexdigest()}"


def _run(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fps(text):
    try:
        n, d = text.split("/")
        return round(float(n) / float(d), 3) if float(d) else None
    except (AttributeError, ValueError):
        return None


def probe(path):
    """영상 길이·해상도·코덱 등을 읽어서 dict로 돌려줌"""
    try:
        r = _run(["ffprobe", "-v", "error", "-print_format", "json",
                  "-show_format", "-show_streams", str(path)], 60)
        data = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return {}
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = _num(fmt.get("duration")) or (_num(v.get("duration")) if v else None)
    bitrate = _num(fmt.get("bit_rate"))
    return {
        "duration": duration,
        "width": v.get("width") if v else None,
        "height": v.get("height") if v else None,
        "fps": _fps(v.get("avg_frame_rate") or v.get("r_frame_rate")) if v else None,
        "video_codec": v.get("codec_name") if v else None,
        "audio_codec": a.get("codec_name") if a else None,
        "bitrate": int(bitrate) if bitrate else None,
    }


def make_thumb(path, out_path, time_sec, width=320):
    """time_sec 지점 장면을 jpg로 저장. 실패하면 맨 앞 장면으로 다시 시도"""
    out_path = str(out_path)
    for t in dict.fromkeys((time_sec, 0)):
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path),
               "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", out_path]
        try:
            _run(cmd, 90)
        except (subprocess.TimeoutExpired, OSError):
            continue
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return True
    return False
