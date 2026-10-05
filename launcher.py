"""시작 런처 (요구사항 15번)
지정한 키를 누른 채로 실행 → VideoVault  /  안 누르면 → 크롬 네이버"""
import ctypes
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app.config import LAUNCH_KEYS, LAUNCH_WAIT, LOG_PATH

_u32 = ctypes.WinDLL("user32")
_u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_u32.GetAsyncKeyState.restype = ctypes.c_short

VK = {"shift": 0x10, "ctrl": 0x11, "alt": 0x12, "space": 0x20, "tab": 0x09}


def _vk(name):
    name = name.strip().lower()
    if name in VK:
        return VK[name]
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    if name.startswith("f") and name[1:].isdigit():
        return 0x6F + int(name[1:])
    raise ValueError(f"알 수 없는 키: {name}")


def keys_held():
    """LAUNCH_WAIT초 동안 지정한 키가 모두 눌려 있는 순간이 있었는지"""
    codes = [_vk(k) for k in LAUNCH_KEYS]
    end = time.time() + LAUNCH_WAIT
    while True:
        if all(_u32.GetAsyncKeyState(c) & 0x8000 for c in codes):
            return True
        if time.time() >= end:
            return False
        time.sleep(0.02)


def setup_log():
    """검은 창 없이 실행될 때 출력과 오류를 data\\log.txt에 기록"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2_000_000:
            LOG_PATH.replace(LOG_PATH.with_name("log_old.txt"))
    except OSError:
        pass
    f = open(LOG_PATH, "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = sys.stderr = f
    print(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 시작 =====")


if __name__ == "__main__":
    if keys_held():
        setup_log()
        import run
        run.main()
    else:
        from app.browser import open_start_page
        open_start_page()
