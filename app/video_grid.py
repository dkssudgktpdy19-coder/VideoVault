"""썸네일 바둑판 목록: 데이터(모델)와 카드 그리기(델리게이트)"""
import os
from collections import OrderedDict

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from app.config import THUMB_DIR
from app.utils import fmt_duration, fmt_size, res_label

CARD_W, CARD_H = 252, 234
THUMB_H = 137            # (252 - 8) * 9 / 16
CACHE_MAX = 1500         # 메모리에 보관할 썸네일 개수

_cache = OrderedDict()


def forget_thumb(name):
    """썸네일을 다시 만들었을 때 캐시에서 지움"""
    for key in [k for k in _cache if k.startswith(f"{name}|")]:
        del _cache[key]


class VideoModel(QAbstractListModel):
    def __init__(self):
        super().__init__()
        self.rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def update_video(self, fresh):
        for i, v in enumerate(self.rows):
            if v["id"] == fresh["id"]:
                self.rows[i] = fresh
                idx = self.index(i)
                self.dataChanged.emit(idx, idx)
                return

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        v = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return v["filename"]
        if role == Qt.ItemDataRole.ToolTipRole:
            lines = [v["filename"]]
            if v.get("width"):
                lines.append(f"{v['width']}x{v['height']}  {v.get('video_codec') or ''}  "
                             f"{fmt_size(v.get('size'))}")
            if v.get("actor_names"):
                lines.append("👤 " + v["actor_names"])
            if v.get("tag_names"):
                lines.append("🏷 " + v["tag_names"])
            if v.get("memo"):
                lines.append("📝 " + v["memo"][:120])
            if v.get("has_sub"):
                lines.append("💬 자막 있음")

            lines.append(v["full_path"] or f"연결 안 됨: {v.get('drive_name') or ''}")
            return "\n".join(lines)
        return None


class VideoDelegate(QStyledItemDelegate):
    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.preview_id = None      # 커서를 올린 영상 id (미리보기 중)
        self.preview_pix = None

    def _preview(self, vid, size, widget):
        if vid != self.preview_id or self.preview_pix is None or self.preview_pix.isNull():
            return None
        dpr = widget.devicePixelRatioF() if widget else 1.0
        pix = self.preview_pix.scaled(int(size.width() * dpr), int(size.height() * dpr),
                                      Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation)
        pix.setDevicePixelRatio(dpr)
        return pix

    def sizeHint(self, option, index):
        return QSize(CARD_W, CARD_H)

    def _thumb(self, name, size, widget):
        if not name:
            return None
        dpr = widget.devicePixelRatioF() if widget else 1.0
        key = f"{name}|{size.width()}x{size.height()}|{dpr}"
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
        src = QPixmap(str(THUMB_DIR / name))
        if src.isNull():
            return None
        pix = src.scaled(int(size.width() * dpr), int(size.height() * dpr),
                         Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
        pix.setDevicePixelRatio(dpr)
        _cache[key] = pix
        if len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)
        return pix

    def _badge(self, p, base_font, text, area, anchor, bg):
        f = QFont(base_font)
        f.setPointSize(8)
        f.setBold(True)
        p.setFont(f)
        fm = p.fontMetrics()
        w, h = fm.horizontalAdvance(text) + 10, fm.height() + 2
        x = area.left() + 5 if anchor.endswith("l") else area.right() - w - 4
        y = area.top() + 5 if anchor.startswith("t") else area.bottom() - h - 6
        rect = QRect(x, y, w, h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(bg))
        p.drawRoundedRect(rect, 3, 3)
        p.setPen(QColor("#ffffff"))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def paint(self, p, option, index):
        v = self.model.rows[index.row()]
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = option.font
        r = option.rect.adjusted(4, 4, -4, -4)

        # 카드 배경
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        bg = "#2d5a8c" if selected else "#353535" if hover else "#262626"
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(bg))
        p.drawRoundedRect(r, 6, 6)

        # 썸네일
        tr = QRect(r.x(), r.y(), r.width(), THUMB_H)
        p.fillRect(tr, QColor("#000000"))
        pix = (self._preview(v["id"], tr.size(), option.widget)
            or self._thumb(v.get("thumb_path"), tr.size(), option.widget))

        if pix:
            sz = pix.deviceIndependentSize().toSize()
            p.drawPixmap(tr.x() + (tr.width() - sz.width()) // 2,
                         tr.y() + (tr.height() - sz.height()) // 2, pix)
        else:
            p.setPen(QColor("#666666"))
            p.drawText(tr, Qt.AlignmentFlag.AlignCenter, "썸네일 없음")

        # 연결 안 됨 / 파일 없음
        if not v["online"]:
            p.fillRect(tr, QColor(0, 0, 0, 170))
            p.setPen(QColor("#ff9b9b"))
            msg = "파일 없음" if v["is_missing"] else f"연결 안 됨\n{v.get('drive_name') or ''}"
            p.drawText(tr, Qt.AlignmentFlag.AlignCenter, msg)

        # 배지
        if v["is_new"]:
            self._badge(p, font, "NEW", tr, "tl", "#e53935")
        res = res_label(v.get("width"), v.get("height"))
        if res:
            self._badge(p, font, res, tr, "tr", "#cc1565c0" if res == "4K" else "#aa000000")
        self._badge(p, font, fmt_duration(v.get("duration")), tr, "br", "#bb000000")
        if v.get("has_sub"):
            self._badge(p, font, "자막", tr, "bl", "#dd00897b")


        # 이어보기 진행 막대
        if v.get("resume_pos") and v.get("duration"):
            frac = min(v["resume_pos"] / v["duration"], 1.0)
            p.fillRect(QRect(tr.x(), tr.bottom() - 3, tr.width(), 4), QColor("#555555"))
            p.fillRect(QRect(tr.x(), tr.bottom() - 3, int(tr.width() * frac), 4), QColor("#e53935"))

        # 제목 (2줄)
        f = QFont(font)
        f.setPointSize(9)
        p.setFont(f)
        line_h = p.fontMetrics().lineSpacing()
        title = v.get("title") or os.path.splitext(v["filename"])[0]
        text_rect = QRect(r.x() + 6, tr.bottom() + 5, r.width() - 12, line_h * 2)
        p.setPen(QColor("#eeeeee"))
        p.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                   | Qt.TextFlag.TextWordWrap, title)

        # 별점 / 본 횟수 / 용량
        info = QRect(r.x() + 6, text_rect.bottom() + 3, r.width() - 12, 18)
        rating = int(v.get("rating") or 0)
        p.setPen(QColor("#f5c518"))
        p.drawText(info, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "★" * rating + "☆" * (5 - rating))
        p.setPen(QColor("#9a9a9a"))
        p.drawText(info, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   f"▶ {v.get('play_count') or 0}회   {fmt_size(v.get('size'))}")

        # 배우 / 태그 줄
        parts = []
        if v.get("actor_names"):
            parts.append("👤 " + v["actor_names"])
        if v.get("tag_names"):
            parts.append("🏷 " + v["tag_names"])
        f2 = QFont(font)
        f2.setPointSize(8)
        p.setFont(f2)
        fm = p.fontMetrics()
        line_rect = QRect(r.x() + 6, info.bottom() + 2, r.width() - 12, fm.height() + 2)
        if parts:
            p.setPen(QColor("#7fb2e5"))
            text = fm.elidedText("   ".join(parts), Qt.TextElideMode.ElideRight, line_rect.width())
        else:
            p.setPen(QColor("#5a5a5a"))
            text = "태그 없음"
        p.drawText(line_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        p.restore()
