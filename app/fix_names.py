"""파일·폴더 이름의 깨진 글자(반쯤 잘린 이모지 등)를 _로 바꿔 주는 도구"""
import os
import re
import sys

BROKEN = re.compile("[\ud800-\udfff]")


def safe(s):
    return s.encode("utf-8", "replace").decode("utf-8")


def find_broken(root):
    """아래쪽(안쪽)부터 찾아야 폴더 이름을 바꿔도 안쪽 경로가 안 꼬임"""
    found = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames + dirnames:
            if BROKEN.search(name):
                found.append((dirpath, name))
    return found


def free_name(dirpath, name):
    base = BROKEN.sub("_", name)
    stem, ext = os.path.splitext(base)
    cand, n = base, 2
    while os.path.exists(os.path.join(dirpath, cand)):
        cand = f"{stem} ({n}){ext}"
        n += 1
    return cand


def main(root):
    root = os.path.abspath(root.strip().strip('"'))
    if not os.path.isdir(root):
        print(f"폴더를 찾을 수 없습니다: {safe(root)}")
        return
    print("깨진 이름 찾는 중...")
    found = find_broken(root)
    if not found:
        print("✅ 깨진 이름이 없습니다.")
        return
    print(f"\n깨진 이름 {len(found)}개 (? 부분이 _로 바뀝니다):\n")
    for dirpath, name in found:
        print(f"  {safe(name)}\n    → {safe(BROKEN.sub('_', name))}")
    answer = input("\n이름을 바꿀까요? (y 입력 후 Enter / 취소는 그냥 Enter): ").strip().lower()
    if answer != "y":
        print("취소했습니다. 아무것도 바뀌지 않았습니다.")
        return
    ok = fail = 0
    for dirpath, name in found:
        try:
            os.rename(os.path.join(dirpath, name), os.path.join(dirpath, free_name(dirpath, name)))
            ok += 1
        except OSError as e:
            fail += 1
            print(f"  [실패] {safe(name)}: {e}")
    print(f"\n✅ 완료: {ok}개 바꿈, 실패 {fail}개")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    if len(sys.argv) < 2:
        print('사용법:  python -m app.fix_names "F:\\영상폴더"')
    else:
        main(" ".join(sys.argv[1:]))
