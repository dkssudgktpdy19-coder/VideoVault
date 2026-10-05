"""화면 표시용 도우미 함수"""


def fmt_duration(sec):
    """초 → 1:23:45 또는 23:45"""
    if not sec:
        return "--:--"
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_size(n):
    """바이트 → 1.5 GB"""
    if not n:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
