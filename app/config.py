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

for _d in (DATA_DIR, THUMB_DIR, BACKUP_DIR, SUB_DIR, SHOT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 영상으로 인식할 확장자
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv", ".webm",
    ".m4v", ".ts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".vob", ".rmvb",
}

# 자동 백업 보관 개수
BACKUP_KEEP = 7
