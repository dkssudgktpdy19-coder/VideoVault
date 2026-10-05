"""드라이브(외장하드) 시리얼 번호로 알아보기"""
import ctypes
from datetime import datetime

_k32 = ctypes.windll.kernel32
_k32.SetErrorMode(0x0001 | 0x8000)   # 빈 카드리더 등에서 오류창 안 뜨게


def volume_info(root):
    """'E:\\' → ('1A2B3C4D', '볼륨이름')"""
    name = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_uint32()
    max_len = ctypes.c_uint32()
    flags = ctypes.c_uint32()
    ok = _k32.GetVolumeInformationW(
        ctypes.c_wchar_p(root), name, 261, ctypes.byref(serial),
        ctypes.byref(max_len), ctypes.byref(flags), fs, 261)
    if not ok:
        raise OSError(f"드라이브 정보를 읽을 수 없습니다: {root}")
    return f"{serial.value:08X}", name.value


def connected_drives():
    """지금 연결된 드라이브 {시리얼: 'E:'}"""
    result = {}
    mask = _k32.GetLogicalDrives()
    for i in range(26):
        if mask & (1 << i):
            letter = f"{chr(65 + i)}:"
            try:
                serial, _ = volume_info(letter + "\\")
                result[serial] = letter
            except OSError:
                pass
    return result


def get_or_create_drive(conn, letter):
    """드라이브를 DB에 등록(또는 갱신)하고 id를 돌려줌"""
    letter = letter[:2].upper()
    serial, label = volume_info(letter + "\\")
    now = datetime.now().isoformat(timespec="seconds")
    row = conn.execute("SELECT id FROM drives WHERE serial=?", (serial,)).fetchone()
    if row:
        conn.execute("UPDATE drives SET label=?, last_letter=?, last_seen=? WHERE id=?",
                     (label, letter, now, row["id"]))
        return row["id"]
    cur = conn.execute(
        "INSERT INTO drives (serial, label, nickname, last_letter, last_seen) VALUES (?,?,?,?,?)",
        (serial, label, label or letter, letter, now))
    return cur.lastrowid


def drive_letters(conn):
    """DB에 등록된 드라이브 중 지금 연결된 것 {drive_id: 'E:'}"""
    now = connected_drives()
    return {r["id"]: now[r["serial"]]
            for r in conn.execute("SELECT id, serial FROM drives")
            if r["serial"] in now}
