"""백업·복원: 태그, 배우, 별점, 본 횟수, 메모, 직접 지정한 썸네일을 zip 하나로 보관"""
import json
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QFileDialog, QMenu, QMessageBox, QToolBar, QToolButton

from app import config

DATA_DIR = Path(config.DATA_DIR)
BACKUP_DIR = Path(getattr(config, "BACKUP_DIR", DATA_DIR / "backups"))
THUMB_DIR = Path(getattr(config, "THUMB_DIR", DATA_DIR / "thumbs"))
SUB_DIR = Path(getattr(config, "SUB_DIR", DATA_DIR / "subs"))
KEEP = int(getattr(config, "BACKUP_KEEP", 7))
STATE_FILE = DATA_DIR / "backup_state.json"
AUTO_GAP = 20 * 3600            # 마지막 자동 백업 후 20시간 지나면 다시
CUSTOM_GLOB = "v*_custom_*.jpg"
YES = QMessageBox.StandardButton.Yes

BACKUP_DIR.mkdir(parents=True, exist_ok=True)
THUMB_DIR.mkdir(parents=True, exist_ok=True)


# ---------------- 설정 파일 ----------------
def _state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _set_state(**kw):
    s = _state()
    s.update(kw)
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


def extra_dir():
    p = _state().get("extra_dir") or ""
    return Path(p) if p else None


# ---------------- 백업 만들기 ----------------
def _count(c, sql):
    try:
        return c.execute(sql).fetchone()[0]
    except Exception:
        return "?"


def _summary(c):
    return {
        "영상": _count(c, "SELECT COUNT(*) FROM videos"),
        "태그": _count(c, "SELECT COUNT(*) FROM tags"),
        "배우": _count(c, "SELECT COUNT(*) FROM actors"),
        "태그 붙은 영상": _count(c, "SELECT COUNT(DISTINCT video_id) FROM video_tags"),
        "별점 준 영상": _count(c, "SELECT COUNT(*) FROM videos WHERE rating > 0"),
    }


def _prune(folder, prefix):
    files = sorted(folder.glob(prefix + "*.zip"), key=lambda p: p.name, reverse=True)
    for old in files[KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass


def make_backup(conn, kind="manual"):
    """kind: auto / manual / before_restore → (zip 경로, 추가 위치 복사 결과)"""
    conn.commit()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"VV_{kind}_{stamp}.zip"
    target = BACKUP_DIR / name
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "videovault.db"
        dst = sqlite3.connect(snap)
        try:
            conn.backup(dst)
            summary = _summary(dst)
        finally:
            dst.close()
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "videovault.db")
            for f in THUMB_DIR.glob(CUSTOM_GLOB):
                z.write(f, "custom_thumbs/" + f.name)
            for f in SUB_DIR.glob("*.srt"):
                z.write(f, "subs/" + f.name)

            z.writestr("info.json", json.dumps(
                {"created": stamp, "kind": kind, "summary": summary},
                ensure_ascii=False, indent=2))
    if kind == "auto":
        _prune(BACKUP_DIR, "VV_auto_")
    copied = None
    ex = extra_dir()
    if ex:
        try:
            ex.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, ex / name)
            if kind == "auto":
                _prune(ex, "VV_auto_")
            copied = True
        except OSError:
            copied = False
    return target, copied


# ---------------- 복원 ----------------
def read_backup(zip_path):
    tmp = Path(tempfile.mkdtemp(prefix="vv_restore_"))
    with zipfile.ZipFile(zip_path) as z:
        if "videovault.db" not in z.namelist():
            raise ValueError("VideoVault 백업 파일이 아닙니다.")
        z.extract("videovault.db", tmp)
    db = tmp / "videovault.db"
    c = sqlite3.connect(db)
    try:
        if c.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("백업 파일이 손상되었습니다.")
        summary = _summary(c)
    finally:
        c.close()
    return db, summary


def restore(conn, zip_path, db):
    conn.commit()
    src = sqlite3.connect(db)
    try:
        src.backup(conn)            # 현재 DB 내용을 백업 내용으로 통째로 교체
    finally:
        src.close()
    with zipfile.ZipFile(zip_path) as z:
        for n in z.namelist():
            if n.startswith("custom_thumbs/") and not n.endswith("/"):
                (THUMB_DIR / Path(n).name).write_bytes(z.read(n))
            elif n.startswith("subs/") and not n.endswith("/"):
                SUB_DIR.mkdir(parents=True, exist_ok=True)
                (SUB_DIR / Path(n).name).write_bytes(z.read(n))

    shutil.rmtree(db.parent, ignore_errors=True)


# ---------------- 화면 연결 ----------------
def _scanning(window):
    threads = set(window.findChildren(QThread))
    for v in list(vars(window).values()):
        if isinstance(v, QThread):
            threads.add(v)
    return any(t.isRunning() for t in threads)


def _status(window, text, ms=6000):
    try:
        window.statusBar().showMessage(text, ms)
    except Exception:
        pass


def _update_tip(window):
    btn = getattr(window, "_vv_backup_btn", None)
    if not btn:
        return
    files = sorted(BACKUP_DIR.glob("VV_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    last = (datetime.fromtimestamp(files[0].stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            if files else "없음")
    btn.setToolTip(f"마지막 백업: {last}\n추가 백업 위치: {extra_dir() or '지정 안 함'}")


def backup_now(window):
    try:
        path, copied = make_backup(window.conn, "manual")
    except Exception as e:
        QMessageBox.critical(window, "백업 실패", str(e))
        return
    msg = f"백업했습니다.\n\n{path}"
    if copied is True:
        msg += f"\n\n추가 위치에도 복사함:\n{extra_dir()}"
    elif copied is False:
        msg += (f"\n\n⚠ 추가 위치({extra_dir()})에는 복사하지 못했습니다.\n"
                "외장하드가 연결되어 있는지 확인해 주세요.")
    QMessageBox.information(window, "백업 완료", msg)
    _update_tip(window)


def auto_backup(window):
    if getattr(window, "_closing", False):
        return
    if time.time() - float(_state().get("last_auto", 0)) < AUTO_GAP:
        return
    try:
        path, copied = make_backup(window.conn, "auto")
    except Exception as e:
        print("[자동 백업 실패]", e)
        _status(window, "⚠ 자동 백업 실패")
        return
    _set_state(last_auto=time.time(), last_file=str(path))
    text = "💾 자동 백업 완료"
    if copied is False:
        text += " (추가 백업 위치는 연결 안 됨)"
    _status(window, text)
    _update_tip(window)


def choose_extra(window):
    d = QFileDialog.getExistingDirectory(window, "추가 백업 위치 선택 (외장하드 권장)",
                                         str(extra_dir() or ""))
    if not d:
        return
    _set_state(extra_dir=d)
    _update_tip(window)
    if QMessageBox.question(window, "추가 백업 위치",
                            f"앞으로 백업할 때마다 여기에도 복사합니다.\n{d}\n\n"
                            "지금 바로 백업할까요?") == YES:
        backup_now(window)


def restore_dialog(window):
    if _scanning(window):
        QMessageBox.information(window, "복원", "영상 확인(스캔)이 끝난 뒤에 다시 시도해 주세요.")
        return
    path, _ = QFileDialog.getOpenFileName(window, "복원할 백업 파일 선택", str(BACKUP_DIR),
                                          "VideoVault 백업 (*.zip)")
    if not path:
        return
    try:
        db, summary = read_backup(path)
    except Exception as e:
        QMessageBox.warning(window, "복원", f"이 파일로는 복원할 수 없습니다.\n\n{e}")
        return
    lines = "\n".join(f"   {k}: {v}" for k, v in summary.items())
    if QMessageBox.question(
            window, "복원 확인",
            f"{Path(path).name}\n\n{lines}\n\n"
            "지금 정리된 내용이 이 백업 내용으로 바뀝니다.\n"
            "(지금 상태는 복원 전에 따로 백업해 둡니다)\n\n복원할까요?") != YES:
        shutil.rmtree(db.parent, ignore_errors=True)
        return
    try:
        safety, _ = make_backup(window.conn, "before_restore")
        restore(window.conn, path, db)
    except Exception as e:
        QMessageBox.critical(window, "복원 실패", str(e))
        return
    QMessageBox.information(window, "복원 완료",
                            f"복원했습니다.\n(복원 전 상태는 {safety.name} 으로 보관됨)\n\n"
                            "프로그램을 닫습니다. 다시 실행해 주세요.")
    window.close()


def install(window):
    bars = window.findChildren(QToolBar)
    tb = bars[0] if bars else window.addToolBar("도구")

    btn = QToolButton(window)
    btn.setText("💾 백업")
    btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    menu = QMenu(btn)
    for text, fn in (("지금 백업하기", lambda: backup_now(window)),
                     ("백업에서 복원하기…", lambda: restore_dialog(window)),
                     (None, None),
                     ("백업 폴더 열기", lambda: os.startfile(BACKUP_DIR)),
                     ("추가 백업 위치 지정… (외장하드 권장)", lambda: choose_extra(window))):
        if text is None:
            menu.addSeparator()
            continue
        act = menu.addAction(text)
        act.triggered.connect(fn)
    btn.setMenu(menu)

    tb.addSeparator()
    tb.addWidget(btn)
    window._vv_backup_btn = btn
    _update_tip(window)
    QTimer.singleShot(15000, lambda: auto_backup(window))
