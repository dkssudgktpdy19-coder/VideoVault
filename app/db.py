"""DB(SQLite) 설계와 연결"""
import sqlite3

from app.config import DB_PATH

SCHEMA_VERSION = 1

SCHEMA = """
-- 외장하드 목록 (드라이브 문자가 바뀌어도 시리얼 번호로 알아봄)
CREATE TABLE IF NOT EXISTS drives (
    id          INTEGER PRIMARY KEY,
    serial      TEXT UNIQUE NOT NULL,
    label       TEXT,
    nickname    TEXT,
    last_letter TEXT,
    last_seen   TEXT
);

-- 영상 정보
CREATE TABLE IF NOT EXISTS videos (
    id           INTEGER PRIMARY KEY,
    file_key     TEXT UNIQUE NOT NULL,   -- 파일 고유 ID (크기 + 일부 해시)
    drive_id     INTEGER REFERENCES drives(id),
    rel_path     TEXT NOT NULL,          -- 드라이브 문자를 뺀 경로
    filename     TEXT NOT NULL,
    size         INTEGER,
    mtime        REAL,
    duration     REAL,
    width        INTEGER,
    height       INTEGER,
    fps          REAL,
    video_codec  TEXT,
    audio_codec  TEXT,
    bitrate      INTEGER,
    title        TEXT,
    memo         TEXT,
    rating       INTEGER DEFAULT 0,      -- 별점 0~5
    play_count   INTEGER DEFAULT 0,      -- 본 횟수
    last_played  TEXT,
    resume_pos   REAL DEFAULT 0,         -- 이어보기 위치(초)
    thumb_path   TEXT,
    thumb_time   REAL,
    thumb_custom INTEGER DEFAULT 0,      -- 1이면 직접 지정한 썸네일
    is_new       INTEGER DEFAULT 1,      -- NEW 표시
    is_missing   INTEGER DEFAULT 0,      -- 파일을 못 찾음
    phash        TEXT,                   -- 중복 영상 찾기용
    added_at     TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_videos_drive  ON videos(drive_id);
CREATE INDEX IF NOT EXISTS idx_videos_rating ON videos(rating);
CREATE INDEX IF NOT EXISTS idx_videos_new    ON videos(is_new);

-- 태그
CREATE TABLE IF NOT EXISTS tags (
    id       INTEGER PRIMARY KEY,
    name     TEXT UNIQUE NOT NULL,
    category TEXT DEFAULT '일반',
    color    TEXT
);
CREATE TABLE IF NOT EXISTS video_tags (
    video_id INTEGER REFERENCES videos(id) ON DELETE CASCADE,
    tag_id   INTEGER REFERENCES tags(id)   ON DELETE CASCADE,
    source   TEXT DEFAULT 'manual',      -- manual / filename / clip / face
    PRIMARY KEY (video_id, tag_id)
);

-- 배우
CREATE TABLE IF NOT EXISTS actors (
    id         INTEGER PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    aliases    TEXT,
    memo       TEXT,
    photo_path TEXT,
    rating     INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS video_actors (
    video_id INTEGER REFERENCES videos(id) ON DELETE CASCADE,
    actor_id INTEGER REFERENCES actors(id) ON DELETE CASCADE,
    PRIMARY KEY (video_id, actor_id)
);

-- 장면 북마크
CREATE TABLE IF NOT EXISTS bookmarks (
    id         INTEGER PRIMARY KEY,
    video_id   INTEGER REFERENCES videos(id) ON DELETE CASCADE,
    time_sec   REAL NOT NULL,
    note       TEXT,
    tag_id     INTEGER REFERENCES tags(id) ON DELETE SET NULL,
    thumb_path TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 저장된 검색 (스마트 폴더)
CREATE TABLE IF NOT EXISTS saved_searches (
    id         INTEGER PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    query_json TEXT NOT NULL
);

-- 얼굴 인식 결과
CREATE TABLE IF NOT EXISTS faces (
    id         INTEGER PRIMARY KEY,
    video_id   INTEGER REFERENCES videos(id) ON DELETE CASCADE,
    time_sec   REAL,
    bbox       TEXT,
    embedding  BLOB,
    cluster_id INTEGER,
    actor_id   INTEGER REFERENCES actors(id) ON DELETE SET NULL,
    thumb_path TEXT
);

-- AI 장면 분석 (CLIP)
CREATE TABLE IF NOT EXISTS scene_embeddings (
    id        INTEGER PRIMARY KEY,
    video_id  INTEGER REFERENCES videos(id) ON DELETE CASCADE,
    time_sec  REAL,
    embedding BLOB
);

-- 자동 생성 자막
CREATE TABLE IF NOT EXISTS subtitles (
    id         INTEGER PRIMARY KEY,
    video_id   INTEGER REFERENCES videos(id) ON DELETE CASCADE,
    lang       TEXT,
    path       TEXT,
    source     TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 프로그램 설정
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect():
    """DB에 연결"""
    conn = sqlite3.connect(DB_PATH, timeout=30)      # 잠겨 있으면 최대 30초 기다림
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")        # 읽기와 쓰기를 동시에
    conn.execute("PRAGMA synchronous = NORMAL")      # 자주 저장해도 빠르게
    return conn


def init_db():
    """DB가 없으면 만들고, 연결을 돌려줌"""
    conn = connect()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    return conn


if __name__ == "__main__":
    conn = init_db()
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print("DB 위치:", DB_PATH)
    print(f"테이블 {len(tables)}개:", ", ".join(tables))
    conn.close()
