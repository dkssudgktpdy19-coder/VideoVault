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


def _grab(path, out_path, t, width, accurate, video_only):
    """장면 하나를 jpg로 저장. accurate=True면 느리지만 특이한 파일에도 잘 됨"""
    seek = ["-ss", f"{t:.2f}"]
    src = ["-i", str(path)]
    cmd = ["ffmpeg", "-y", "-v", "error"]
    cmd += (src + seek) if accurate else (seek + src)
    if video_only:
        cmd += ["-map", "0:V:0"]          # 앨범 표지 같은 그림은 빼고 진짜 영상만
    cmd += ["-an", "-sn", "-dn", "-frames:v", "1",
            "-vf", f"scale={width}:-2", "-q:v", "4", out_path]
    try:
        _run(cmd, 120 if accurate else 60)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return os.path.exists(out_path) and os.path.getsize(out_path) > 0


def make_thumb(path, out_path, time_sec, width=320, duration=None):
    """여러 방법을 차례로 시도해서 하나라도 성공하면 True"""
    out_path = str(out_path)
    times = [time_sec]
    if duration and duration > 4:
        times.append(duration * 0.5)
    times.append(0)
    tries = [(t, False, True) for t in dict.fromkeys(round(x, 2) for x in times)]
    tries.append((min(time_sec, 3.0), True, True))   # 정확한 방식
    tries.append((0, False, False))                  # 앨범 표지라도 사용
    for t, accurate, video_only in tries:
        try:
            os.remove(out_path)
        except OSError:
            pass
        if _grab(path, out_path, t, width, accurate, video_only):
            return True
    return False
