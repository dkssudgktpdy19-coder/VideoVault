"""메인 화면 추가 기능: 영구 삭제(영상 파일까지), F1 도움말"""
import os
import stat
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from app import player as player_mod
from app.config import THUMB_DIR
from app.utils import fmt_size

THUMBS = Path(THUMB_DIR)
DATA = THUMBS.parent
TYPE_CONFIRM_FROM = 10     # 이 개수 이상이면 '삭제'를 직접 입력해야 지워짐


def _inside_data(p):
    try:
        p.resolve().relative_to(DATA.resolve())
        return True
    except Exception:
        return False


def _rm(p):
    """프로그램 data 폴더 안의 파일만 지움 (안전장치)"""
    try:
        if p and _inside_data(p) and p.is_file():
            p.unlink()
    except OSError:
        pass


def _as_path(value, base):
    if not value:
        return None
    p = Path(value)
    return p if p.is_absolute() else base / p


def _tables_with_video_id(conn):
    out = []
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        if name == "videos":
            continue
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
        if "video_id" in cols:
            out.append(name)
    return out


def _remove_file(path):
    """실제 영상 파일 삭제. 실패하면 이유를 돌려줌"""
    for _ in range(8):
        try:
            os.remove(path)
            return None
        except FileNotFoundError:
            return None
        except PermissionError:
            try:
                os.chmod(path, stat.S_IWRITE)      # 읽기 전용이면 풀기
            except OSError:
                pass
            QApplication.processEvents()
            time.sleep(0.4)
        except OSError as e:
            return str(e)
    return "다른 프로그램이 파일을 쓰고 있습니다 (재생·얼굴 분석·자막 작업 중인지 확인)"


def _cleanup(conn, v):
    """썸네일·미리보기·북마크 그림·자막 파일 정리"""
    vid = v["id"]
    tp = v.get("thumb_path")
    if tp and not conn.execute("SELECT 1 FROM videos WHERE thumb_path = ? AND id != ?",
                               (tp, vid)).fetchone():
        _rm(_as_path(tp, THUMBS))
    for f in (THUMBS / "preview").glob(f"{vid}_*.jpg"):
        _rm(f)
    try:
        from app import marks
        for (t,) in conn.execute("SELECT thumb FROM scene_marks WHERE video_id = ?", (vid,)).fetchall():
            _rm(_as_path(t, Path(marks.MARK_DIR)))
    except Exception:
        pass
    try:
        from app import subtitle as st
        st.clear_subs(conn, vid)
    except Exception:
        pass


def delete_videos(window, vids):
    vids = [v for v in vids if v]
    if not vids:
        return
    offline = [v for v in vids if not v.get("online") and not v.get("is_missing")]
    targets = [v for v in vids if v not in offline]
    if not targets:
        QMessageBox.information(window, "영구 삭제", "외장하드가 연결되어 있지 않아 지울 수 없습니다.")
        return

    total = sum(v.get("size") or 0 for v in targets)
    names = "\n".join("· " + v["filename"] for v in targets[:10])
    if len(targets) > 10:
        names += f"\n… 외 {len(targets) - 10}개"
    if offline:
        names += f"\n\n(외장하드가 연결 안 된 {len(offline)}개는 건너뜁니다)"

    box = QMessageBox(window)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("⛔ 영구 삭제")
    box.setText(f"영상 {len(targets)}개 ({fmt_size(total)})를 파일까지 영구 삭제합니다.\n"
                "휴지통으로 가지 않아 되돌릴 수 없습니다.")
    box.setInformativeText(names)
    btn_del = box.addButton("영구 삭제", QMessageBox.ButtonRole.DestructiveRole)
    btn_no = box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(btn_no)
    box.exec()
    if box.clickedButton() is not btn_del:
        return
    if len(targets) >= TYPE_CONFIRM_FROM:
        text, ok = QInputDialog.getText(window, "한 번 더 확인",
                                        f"{len(targets)}개를 정말 지우려면 '삭제'라고 입력하세요:")
        if not ok or text.strip() != "삭제":
            return

    # 재생 중인 영상이면 먼저 닫기 (파일이 잠겨 있으면 못 지움)
    pw = window.player
    ids = {v["id"] for v in targets}
    if pw.video and pw.video["id"] in ids:
        pw.close()
        QApplication.processEvents()
        time.sleep(0.5)

    conn = window.conn
    tables = _tables_with_video_id(conn)
    done, fails = 0, []
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        for v in targets:
            path = v.get("full_path")
            if path and os.path.exists(path):
                err = _remove_file(path)
                if err:
                    fails.append(f"{v['filename']}\n   → {err}")
                    continue
            _cleanup(conn, v)
            for t in tables:
                conn.execute(f'DELETE FROM "{t}" WHERE video_id = ?', (v["id"],))
            conn.execute("DELETE FROM videos WHERE id = ?", (v["id"],))
            conn.commit()
            done += 1
    finally:
        QApplication.restoreOverrideCursor()

    window.after_edit()
    window.statusBar().showMessage(f"⛔ 영구 삭제 {done}개 완료" + (f", 실패 {len(fails)}개" if fails else ""), 10000)
    if fails:
        QMessageBox.warning(window, "일부 못 지움", "아래 파일은 지우지 못했습니다:\n\n" + "\n".join(fails[:15]))


def install(window):
    window._vv_delete = lambda vids: delete_videos(window, vids)
    sc = QShortcut(QKeySequence("Shift+Del"), window.view)
    sc.setContext(Qt.ShortcutContext.WidgetShortcut)
    sc.activated.connect(lambda: delete_videos(window, window.selected_videos()))
    QShortcut(QKeySequence("F1"), window).activated.connect(
        lambda: player_mod.show_help_dialog(window, player_mod.MAIN_HELP, "단축키 도움말 (메인 화면)"))
