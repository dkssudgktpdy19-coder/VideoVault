"""현재 Windows 기본 사운드 출력 장치 이름 (요구사항 14번)"""


def default_output_name():
    """예: '스피커 (Realtek High Definition Audio)'. 실패하면 None"""
    try:
        from pycaw.pycaw import AudioUtilities
        dev = AudioUtilities.GetSpeakers()
        name = getattr(dev, "FriendlyName", None)          # 새 버전 pycaw
        if not name:
            name = AudioUtilities.CreateDevice(dev).FriendlyName   # 예전 버전 pycaw
        return name
    except Exception:
        return None
