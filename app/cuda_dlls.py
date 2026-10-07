"""GPU 부품(DLL) 위치 알려주기: pip로 받은 NVIDIA DLL을 자막 엔진이 찾을 수 있게 함"""
import os
import sys
from pathlib import Path


def setup():
    found = []
    for sp in sys.path:
        base = Path(sp) / "nvidia"
        if not base.is_dir():
            continue
        for d in base.glob("*/bin"):
            s = str(d)
            if d.is_dir() and s not in found:
                found.append(s)
    for d in found:
        try:
            os.add_dll_directory(d)
        except (OSError, AttributeError):
            pass
    if found:
        os.environ["PATH"] = os.pathsep.join(found) + os.pathsep + os.environ.get("PATH", "")
    return found


DLL_DIRS = setup()


if __name__ == "__main__":
    import ctypes
    print("찾은 GPU 부품 폴더:", len(DLL_DIRS), "개")
    for d in DLL_DIRS:
        print("  ", d)
    for name in ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll"):
        try:
            ctypes.WinDLL(name)
            print("✅", name)
        except OSError as e:
            print("❌", name, "-", e)
