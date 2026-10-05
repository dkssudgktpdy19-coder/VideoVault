"""프로그램 전체에서 쓰는 경로와 기본 설정"""
from pathlib import Path

# C:\VideoVault
BASE_DIR = Path(__file__).resolve().parent.parent

# 개인 데이터 폴더 (PC에 저장, GitHub에는 올리지 않음)
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "videovault.db"
THUMB_DIR = DATA_DIR / "thumbs"        # 썸네일
BACKUP_DIR = DATA_DIR / "backups"      # 자동 백업
SUB_DIR = DATA_DIR / "subtitles"       # 자동 생성 자막
SHOT_DIR = DATA_DIR / "screenshots"    # 스크린샷
LOG_PATH = DATA_DIR / "log.txt"        # 바로가기로 실행했을 때 기록

for _d in (DATA_DIR, THUMB_DIR, BACKUP_DIR, SUB_DIR, SHOT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 영상으로 인식할 확장자
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv", ".webm",
    ".m4v", ".ts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".vob", ".rmvb",
}

# 자동 백업 보관 개수
BACKUP_KEEP = 7

# ---------- 시작 런처 (요구사항 15번) ----------
# 여기 적은 키를 '모두' 누른 채로 실행해야 VideoVault가 열립니다.
# 누르지 않으면 크롬 네이버가 열립니다.
# 예)  ["shift"]   /   ["shift", "v"]  (Shift와 V를 같이)   /   ["ctrl"]
# ⚠ "alt"는 쓰지 마세요 (Alt + 더블클릭 = 속성 창이 열림)
# ⚠ "ctrl"과 "shift"를 같이 쓰지 마세요 (관리자 권한 실행으로 바뀔 수 있음)
LAUNCH_KEYS = ["shift"]
LAUNCH_WAIT = 1.0      # 실행 후 몇 초 동안 키가 눌렸는지 확인할지

# ---------- 빠른 종료 (요구사항 16번) ----------
# 어느 창에 있든 이 키를 누르면: 소리 끔 → 창 숨김 → 크롬 네이버 → 종료
# 예)  "ctrl+alt+q"   /   "pause"   /   "ctrl+shift+f12"
QUIT_HOTKEY = "ctrl+alt+q"
START_URL = "https://www.naver.com"
