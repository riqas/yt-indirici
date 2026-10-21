"""YT İndirici — YouTube'dan doğrudan MP3 / MP4.

Web dönüştürücülerin aksine dosya önce bir sunucuya inip orada dönüştürülmez:
yt-dlp YouTube'un CDN'inden doğrudan indirir, ffmpeg bu PC'de dönüştürür.
"""
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve, QObject, QPropertyAnimation, QRectF, QRunnable, QSettings, Qt, QThreadPool, pyqtSignal,
)
from PyQt6.QtGui import (
    QColor, QFont, QFontDatabase, QGuiApplication, QIcon, QKeySequence, QLinearGradient, QPainter,
    QPainterPath, QPixmap, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

import yt_dlp
from yt_dlp.utils import DownloadCancelled

APP_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = Path.home() / "Downloads" / "YT Indirici"
LOCAL_FFMPEG = APP_DIR / "ffmpeg" / "bin" / "ffmpeg.exe"

MP3_QUALITIES = ["320", "256", "192", "128"]
MP4_QUALITIES = ["En iyi", "2160p", "1440p", "1080p", "720p", "480p", "360p"]



def human_speed(bps):
    if not bps:
        return ""
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024:
            return f"{bps:.0f} {unit}" if unit == "B/s" else f"{bps:.1f} {unit}"
        bps /= 1024
    return f"{bps:.1f} TB/s"


def human_eta(sec):
    if sec is None:
        return ""
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


FRIENDLY_ERRORS = (
    ("Private video", "Bu video gizli."),
    ("video is unavailable", "Video bulunamadı: silinmiş, gizli ya da bölgende kapalı."),
    ("Video unavailable", "Video bulunamadı: silinmiş, gizli ya da bölgende kapalı."),
    ("confirm your age", "Yaş doğrulaması isteyen video, giriş yapmadan indirilemiyor."),
    ("not a bot", "YouTube bot doğrulaması istedi. Biraz bekleyip tekrar dene."),
    ("members-only", "Yalnız kanal üyelerine açık video."),
    ("Unsupported URL", "Bu link tanınmadı. YouTube video, playlist ya da kanal linki yapıştır."),
    ("HTTP Error 404", "Sayfa bulunamadı (404). Linki kontrol et."),
)


def clean_error(msg):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(msg))
    for needle, friendly in FRIENDLY_ERRORS:
        if needle.lower() in msg.lower():
            return friendly
    msg = re.sub(r"^(ERROR:\s*)?(\[[^\]]+\]\s*)?([\w-]{6,}:\s*)?", "", msg)
    return msg.strip()[:200] or "Bilinmeyen hata"


MAX_ATTEMPTS = 5
TRANSIENT = ("SSL", "timed out", "Connection reset", "Connection aborted", "Remote end closed",
             "HTTP Error 5", "getaddrinfo")

# GoodbyeDPI gibi DPI aşma araçları (Türkiye'de Discord için yaygın) Google'a giden TLS el sıkışmalarının
# yarıdan fazlasını bozuyor (SSL: INVALID_SESSION_ID / handshake timeout); TLS sürümü değiştirmek işe yaramadı.
# Bozuk el sıkışma anında düşüyor, yeniden denemek ucuz; tutan bağlantı açık kalıp sonraki isteklerde kullanılıyor.
NET_OPTS = {
    "extractor_retries": 25,
    "retries": 25,
    "fragment_retries": 25,
    "socket_timeout": 8,
    "retry_sleep_functions": {k: (lambda n: 0.3) for k in ("http", "fragment", "extractor")},
    "js_runtimes": {"deno": {}, "node": {}},
}


def is_transient(err):
    """Ağ kaynaklı geçici hata mı? (ör. bazı PC'lerde ara sıra SSL INVALID_SESSION_ID)"""
    return any(t in str(err) for t in TRANSIENT)


class QuietLogger:
    """yt-dlp çıktısını yut (pythonw'da konsol yok), son hatayı tut."""

    def __init__(self):
        self.last_error = ""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        self.last_error = msg


def build_opts(fmt, quality, out_dir):
    opts = {
        "outtmpl": str(Path(out_dir) / "%(title)s.%(ext)s"),
        "windowsfilenames": True,
        "noplaylist": True,          # watch?v=..&list=.. → sadece o video
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "concurrent_fragment_downloads": 8,
        "http_chunk_size": 10 * 1024 * 1024,  # YouTube hız kısıtına takılmamak için parçalı istek
        **NET_OPTS,
    }
    # başlatıcının indirdiği ffmpeg: PATH'e güvenme (app.py doğrudan açılırsa PATH'te olmaz)
    if LOCAL_FFMPEG.exists():
        opts["ffmpeg_location"] = str(LOCAL_FFMPEG.parent)
    if fmt == "mp3":
        opts.update({
            "format": "bestaudio/best",
            "writethumbnail": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": quality},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ],
        })
    else:
        sort = ["vcodec:h264", "acodec:aac"]  # aynı çözünürlükte en uyumlu codec'i seç
        if quality != "En iyi":
            sort.insert(0, f"res:{quality.rstrip('p')}")
        opts.update({
            "format": "bv*+ba/b",
            "format_sort": sort,
            "merge_output_format": "mp4",
            "postprocessors": [{"key": "FFmpegMetadata", "add_metadata": True}],
        })
    return opts


class Signals(QObject):
    title = pyqtSignal(str, str)
    status = pyqtSignal(str, str)
    progress = pyqtSignal(str, int, str, str)
    done = pyqtSignal(str, bool, str, str)          # id, ok, mesaj, dosya yolu
    expanded = pyqtSignal(str, list)                # playlist id, [(url, başlık)]


class DownloadJob(QRunnable):
    def __init__(self, job_id, url, fmt, quality, out_dir, cancel, signals):
        super().__init__()
        self.job_id, self.url = job_id, url
        self.fmt, self.quality, self.out_dir = fmt, quality, out_dir
        self.cancel, self.s = cancel, signals
        self.final_path = ""
        self.stem = ""
        self.phase = 0

    def _remove_leftovers(self, since):
        """İptalde yarım kalanları sil (.part, ayrı inen ses/video, MP3'e çevrilmeyi bekleyen dosya, kapak).
        Hepsi 'başlık.' ile başlıyor; bu işten önce var olan dosyalara (ör. aynı videonun öbür biçimi) dokunma."""
        if not self.stem:
            return
        for p in Path(self.out_dir).iterdir():
            try:
                if p.name.startswith(self.stem + ".") and p.stat().st_mtime >= since - 1:
                    p.unlink()
            except OSError:
                pass

    def _hook(self, d):
        if self.cancel.is_set():
            raise DownloadCancelled()
        info = d.get("info_dict") or {}
        if self.phase == 0 and info.get("title"):
            self.s.title.emit(self.job_id, info["title"])
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            pct = int(d.get("downloaded_bytes", 0) * 100 / total) if total else 0
            label = "İndiriliyor"
            if self.fmt == "mp4" and info.get("vcodec") not in (None, "none") and info.get("acodec") == "none":
                label = "Video iniyor"
            elif self.fmt == "mp4" and info.get("vcodec") == "none":
                label = "Ses iniyor"
            self.s.status.emit(self.job_id, label)
            self.s.progress.emit(self.job_id, pct, human_speed(d.get("speed")), human_eta(d.get("eta")))
        elif d["status"] == "finished":
            self.phase += 1

    def _pp_hook(self, d):
        if self.cancel.is_set():
            raise DownloadCancelled()
        if d["status"] == "started":
            names = {"FFmpegExtractAudio": "MP3'e çevriliyor", "FFmpegMerger": "Birleştiriliyor",
                     "EmbedThumbnail": "Kapak ekleniyor", "FFmpegMetadata": "Etiketleniyor"}
            self.s.status.emit(self.job_id, names.get(d.get("postprocessor"), "İşleniyor"))
            self.s.progress.emit(self.job_id, 100, "", "")

    def run(self):
        if self.cancel.is_set():
            self.s.done.emit(self.job_id, False, "İptal edildi", "")
            return
        logger = QuietLogger()
        opts = build_opts(self.fmt, self.quality, self.out_dir)
        opts.update({
            "logger": logger,
            "progress_hooks": [self._hook],
            "postprocessor_hooks": [self._pp_hook],
            "post_hooks": [lambda path: setattr(self, "final_path", path)],
        })
        started = time.time()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.s.status.emit(self.job_id, "Bağlanıyor" if attempt == 1 else f"Tekrar deneniyor ({attempt})")
            logger.last_error = ""
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(self.url, download=False)
                    if info.get("title"):
                        self.s.title.emit(self.job_id, info["title"])
                    # yt-dlp MP3'te dosya varken videoyu yeniden indirip üstüne yazıyor: önce hedefe bak
                    target = Path(ydl.prepare_filename(info)).with_suffix(f".{self.fmt}")
                    self.stem = target.stem
                    if target.exists():
                        self.s.done.emit(self.job_id, True, "Zaten var", str(target))
                        return
                    ydl.process_ie_result(info, download=True)
                self.s.done.emit(self.job_id, True, "Tamam", self.final_path)
                return
            except DownloadCancelled:
                self._remove_leftovers(started)
                self.s.done.emit(self.job_id, False, "İptal edildi", "")
                return
            except Exception as e:  # noqa: BLE001 — her hatayı satırda göster, uygulamayı düşürme
                err = logger.last_error or str(e)
            if not is_transient(err) or self.cancel.is_set():
                break
        self.s.done.emit(self.job_id, False, clean_error(err), "")


class PlaylistResolver(QRunnable):
    """Playlist/kanal linkini tek tek video linklerine açar (indirmeden, hızlı)."""

    def __init__(self, job_id, url, signals):
        super().__init__()
        self.job_id, self.url, self.s = job_id, url, signals

    def run(self):
        logger = QuietLogger()
        opts = {"extract_flat": "in_playlist", "quiet": True, "logger": logger, **NET_OPTS}
        try:
            info = None
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(self.url, download=False)
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == MAX_ATTEMPTS or not is_transient(logger.last_error or str(e)):
                        raise
            entries = []
            for e in info.get("entries") or []:
                if not e:
                    continue
                url = e.get("url") or e.get("webpage_url")
                if url and not url.startswith("http"):
                    url = f"https://www.youtube.com/watch?v={e.get('id')}"
                if url:
                    entries.append((url, e.get("title") or url))
            if not entries:
                self.s.done.emit(self.job_id, False, "Playlist boş", "")
            else:
                self.s.expanded.emit(self.job_id, entries)
        except Exception as e:  # noqa: BLE001
            self.s.done.emit(self.job_id, False, clean_error(logger.last_error or e), "")


def video_id(url):
    """Tek videonun 11 karakterlik id'si (watch?v=, youtu.be/, shorts/, embed/, live/); playlist/kanal için ''."""
    if is_playlist(url):
        return ""
    m = re.search(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/|/live/)([\w-]{11})", url)
    return m.group(1) if m else ""


def is_playlist(url):
    return ("list=" in url and "v=" not in url) or bool(
        re.search(r"youtube\.com/(@|c/|channel/|user/)", url))


# ---------- görünüm ----------
C = {
    "bg": "#0e0e12", "surface": "#16161c", "surface2": "#1d1d25", "line": "#272731", "line2": "#363643",
    "text": "#ececf1", "muted": "#8b8b99", "faint": "#62626f",
    "accent": "#ff3b5c", "accent2": "#ff5a76", "ok": "#3ddc97", "err": "#ff6b6b",
}

STYLE = f"""
* {{ font-family: 'Segoe UI'; font-size: 10pt; color: {C['text']}; }}
QWidget#root, QWidget#list {{ background: {C['bg']}; }}
QLabel#h1 {{ font-size: 18pt; font-weight: 700; }}
QLabel#muted {{ color: {C['muted']}; }}
QLabel#faint {{ color: {C['faint']}; font-size: 9pt; }}
QLabel#section {{ color: {C['muted']}; font-size: 8.5pt; font-weight: 700; letter-spacing: 1px; }}
QLabel#empty {{ color: {C['muted']}; font-size: 10.5pt; }}
QFrame#card {{ background: {C['surface']}; border: 1px solid {C['line']}; border-radius: 14px; }}
QFrame#sep {{ background: {C['line']}; border: none; }}
QPlainTextEdit#urls {{ background: transparent; border: none; padding: 2px; font-size: 10.5pt;
    selection-background-color: {C['accent']}; }}
QFrame#segbox {{ background: {C['bg']}; border: 1px solid {C['line']}; border-radius: 10px; }}
QPushButton#seg {{ background: transparent; border: none; border-radius: 7px; padding: 6px 14px;
    font-weight: 600; color: {C['muted']}; }}
QPushButton#seg:hover {{ color: {C['text']}; }}
QPushButton#seg:checked {{ background: {C['accent']}; color: white; }}
QComboBox {{ background: {C['surface2']}; border: 1px solid {C['line']}; border-radius: 9px; padding: 6px 10px; }}
QComboBox:hover {{ border-color: {C['line2']}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{ background: {C['surface2']}; border: 1px solid {C['line2']}; outline: none;
    selection-background-color: {C['accent']}; padding: 4px; }}
QPushButton {{ background: {C['surface2']}; border: 1px solid {C['line']}; border-radius: 9px; padding: 7px 14px; }}
QPushButton:hover {{ background: #25252f; border-color: {C['line2']}; }}
QPushButton:pressed {{ background: #1a1a21; }}
QPushButton:disabled {{ color: {C['faint']}; }}
QPushButton#ghost {{ background: transparent; border: 1px solid transparent; color: {C['muted']}; }}
QPushButton#ghost:hover {{ background: {C['surface2']}; border-color: {C['line']}; color: {C['text']}; }}
QPushButton#primary {{ background: {C['accent']}; border: none; color: white; font-weight: 700;
    padding: 8px 22px; border-radius: 10px; }}
QPushButton#primary:hover {{ background: {C['accent2']}; }}
QPushButton#primary:pressed {{ background: #e8304f; }}
QFrame#job {{ background: {C['surface']}; border: 1px solid {C['line']}; border-radius: 12px; }}
QFrame#job:hover {{ border-color: {C['line2']}; }}
QLabel#jobtitle {{ font-weight: 600; font-size: 10.5pt; }}
QProgressBar {{ background: {C['surface2']}; border: none; border-radius: 2px; min-height: 4px; max-height: 4px; }}
QProgressBar::chunk {{ background: {C['accent']}; border-radius: 2px; }}
QProgressBar[state="done"]::chunk {{ background: {C['ok']}; }}
QProgressBar[state="error"]::chunk {{ background: {C['err']}; }}
QProgressBar[state="cancelled"]::chunk {{ background: {C['line2']}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {C['line2']}; border-radius: 3px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QToolTip {{ background: {C['surface2']}; color: {C['text']}; border: 1px solid {C['line2']}; padding: 5px; }}
"""

# Windows'un kendi ikon fontu: 11'de Segoe Fluent Icons, 10'da Segoe MDL2 Assets
G_DOWNLOAD, G_FOLDER, G_PASTE, G_SYNC, G_CANCEL = "\uE896", "\uED25", "\uE77F", "\uE895", "\uE711"
G_MUSIC, G_VIDEO, G_CHECK, G_ERROR, G_CLEAR, G_LIST = "\uEC4F", "\uE714", "\uE73E", "\uE783", "\uE74D", "\uE8FD"
_icon_family = None


def icon_family():
    global _icon_family
    if _icon_family is None:
        fams = QFontDatabase.families()
        _icon_family = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams), "")
    return _icon_family


def glyph_pixmap(code, color, px, bg=None, radius=0.0):
    """İkon fontundan pixmap; bg verilirse yuvarlak köşeli zemin üstüne çizer."""
    dpr = 2.0
    pm = QPixmap(int(px * dpr), int(px * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if bg:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(bg))
        p.drawRoundedRect(QRectF(0, 0, px, px), radius, radius)
    if icon_family():
        f = QFont(icon_family())
        f.setPixelSize(int(px * (0.45 if bg else 0.8)))
        p.setFont(f)
        p.setPen(QColor(color))
        p.drawText(QRectF(0, 0, px, px), Qt.AlignmentFlag.AlignCenter, code)
    p.end()
    return pm


def glyph_icon(code, color=C["text"], px=16):
    return QIcon(glyph_pixmap(code, color, px))


def tint(hex_color, alpha):
    c = QColor(hex_color)
    c.setAlphaF(alpha)
    return c.name(QColor.NameFormat.HexArgb)


def logo_pixmap(px):
    """Kırmızı yuvarlak kare + beyaz oynat üçgeni (pencere ve görev çubuğu ikonu)."""
    dpr = 2.0
    pm = QPixmap(int(px * dpr), int(px * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    grad = QLinearGradient(0, 0, px, px)
    grad.setColorAt(0, QColor("#ff5a76"))
    grad.setColorAt(1, QColor("#e3284a"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(QRectF(0, 0, px, px), px * 0.26, px * 0.26)
    tri = QPainterPath()
    tri.moveTo(px * 0.40, px * 0.30)
    tri.lineTo(px * 0.72, px * 0.50)
    tri.lineTo(px * 0.40, px * 0.70)
    tri.closeSubpath()
    p.setBrush(QColor("white"))
    p.drawPath(tri)
    p.end()
    return pm


def apply_style(app):
    """QSS açılır menü okunu dosyadan ister: oku ikon fontundan bir kez PNG'ye çizip ona bağla."""
    arrow = Path(tempfile.gettempdir()) / "yt_indirici_chevron.png"
    glyph_pixmap("\uE70D", C["muted"], 12).save(str(arrow))
    app.setStyleSheet(STYLE + f'QComboBox::down-arrow {{ image: url("{arrow.as_posix()}"); width: 10px; height: 10px; }}')


class ElidedLabel(QLabel):
    """Sığmayan metni '…' ile kısaltır; tamamı ipucunda."""

    def __init__(self, text="", mode=Qt.TextElideMode.ElideRight, **kw):
        super().__init__(**kw)
        self._full, self._mode = text, mode
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setFullText(text)

    def setFullText(self, text):
        self._full = text
        self.setToolTip(text)
        self._refresh()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._refresh()

    def _refresh(self):
        self.setText(self.fontMetrics().elidedText(self._full, self._mode, max(20, self.width())))


class JobRow(QFrame):
    """Listede tek indirme: format ikonu, başlık, ince ilerleme çubuğu, durum, hız/kalan."""

    def __init__(self, title, kind, on_reveal):
        super().__init__(objectName="job")
        self.kind, self.state, self.path, self._on_reveal = kind, "queued", "", on_reveal
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(12)
        self.tile = QLabel()
        self.tile.setFixedSize(40, 40)
        lay.addWidget(self.tile)

        mid = QVBoxLayout()
        mid.setSpacing(5)
        top = QHBoxLayout()
        self.title = ElidedLabel(title, objectName="jobtitle")
        self.pct = QLabel(objectName="muted")
        top.addWidget(self.title, 1)
        top.addWidget(self.pct)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        bottom = QHBoxLayout()
        self.status = QLabel(objectName="muted")
        self.meta = QLabel(objectName="faint")
        bottom.addWidget(self.status)
        bottom.addStretch()
        bottom.addWidget(self.meta)
        mid.addLayout(top)
        mid.addWidget(self.bar)
        mid.addLayout(bottom)
        lay.addLayout(mid, 1)

        self.btn = QPushButton(objectName="ghost")
        self.btn.setIcon(glyph_icon(G_FOLDER, C["muted"], 16))
        self.btn.setToolTip("Klasörde göster")
        self.btn.setFixedSize(36, 36)
        self.btn.clicked.connect(lambda: self._on_reveal(self))
        self.btn.hide()
        lay.addWidget(self.btn)

        # ilerleme atlamasın, akarak dolsun
        self.anim = QPropertyAnimation(self.bar, b"value", self)
        self.anim.setDuration(280)
        self.anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.set_status("Sırada")
        self._paint_tile()

    def _paint_tile(self):
        glyph = {"mp3": G_MUSIC, "mp4": G_VIDEO, "list": G_LIST}[self.kind]
        color = {"queued": C["muted"], "active": C["accent"], "done": C["ok"], "error": C["err"],
                 "cancelled": C["muted"]}[self.state]
        if self.state == "done":
            glyph = G_CHECK
        elif self.state == "error":
            glyph = G_ERROR
        elif self.state == "cancelled":
            glyph = G_CANCEL
        bg = C["surface2"] if self.state == "queued" else tint(color, 0.14)
        self.tile.setPixmap(glyph_pixmap(glyph, color, 40, bg=bg, radius=10))

    def _animate_to(self, value):
        self.anim.stop()
        self.anim.setStartValue(self.bar.value())
        self.anim.setEndValue(value)
        self.anim.start()

    def set_title(self, title):
        self.title.setFullText(title)

    def set_status(self, text):
        self.status.setText(text)
        if text != "Sırada" and self.state == "queued":
            self.state = "active"
            self._paint_tile()

    def set_progress(self, pct, speed, eta):
        if self.state == "queued":
            self.state = "active"
            self._paint_tile()
        self._animate_to(pct * 10)
        self.pct.setText(f"%{pct}")
        self.meta.setText(" · ".join(x for x in (speed, f"{eta} kaldı" if eta else "") if x))

    def finish(self, ok, msg, path=""):
        self.state = "done" if ok else "cancelled" if msg == "İptal edildi" else "error"
        self.path = path
        self.anim.stop()
        self.bar.setValue(1000 if ok else max(self.bar.value(), 1000))
        self.bar.setProperty("state", self.state)
        self.bar.style().unpolish(self.bar)
        self.bar.style().polish(self.bar)
        self.pct.setText("")
        self.meta.setText("")
        self.status.setText(msg)
        self.status.setToolTip(msg)
        self.status.setStyleSheet(f"color: {C[{'done': 'ok', 'error': 'err'}.get(self.state, 'muted')]};")
        self.btn.setVisible(ok and bool(path))
        self._paint_tile()

    def mouseDoubleClickEvent(self, e):
        if self.state == "done":
            self._on_reveal(self)
        super().mouseDoubleClickEvent(e)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YT İndirici")
        self.setWindowIcon(QIcon(logo_pixmap(64)))
        self.resize(980, 700)
        self.setMinimumSize(760, 560)
        self.settings = QSettings("YTIndirici", "YTIndirici")
        self.pool = QThreadPool()
        self.signals = Signals()
        self.signals.title.connect(self.on_title)
        self.signals.status.connect(self.on_status)
        self.signals.progress.connect(self.on_progress)
        self.signals.done.connect(self.on_done)
        self.signals.expanded.connect(self.on_expanded)
        self.rows = {}        # sürmekte olan job id → JobRow
        self.all_rows = []
        self.n_ok = self.n_err = self.n_cancel = 0
        self.cancel = threading.Event()
        self.active = 0
        self._build()

    # ---------- arayüz ----------
    def _build(self):
        root = QWidget(objectName="root")
        lay = QVBoxLayout(root)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(14)

        head = QHBoxLayout()
        head.setSpacing(14)
        logo = QLabel()
        logo.setPixmap(logo_pixmap(44))
        head.addWidget(logo)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(QLabel("YT İndirici", objectName="h1"))
        col.addWidget(QLabel("YouTube'dan doğrudan MP3 ve MP4. Reklam yok, sıra yok.", objectName="muted"))
        head.addLayout(col)
        head.addStretch()
        self.btn_update = QPushButton(" yt-dlp güncelle", objectName="ghost")
        self.btn_update.setIcon(glyph_icon(G_SYNC, C["muted"]))
        self.btn_update.setToolTip("YouTube bir şeyi değiştirip indirme bozulursa buna bas.")
        self.btn_update.clicked.connect(self.update_ytdlp)
        head.addWidget(self.btn_update)
        lay.addLayout(head)

        card = QFrame(objectName="card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(10)
        self.urls = QPlainTextEdit(objectName="urls")
        self.urls.setPlaceholderText("YouTube linklerini buraya yapıştır, her satıra bir tane.\n"
                                     "Video, playlist ve kanal linkleri olur.  ·  Ctrl+Enter ile başlat")
        self.urls.setFixedHeight(84)
        cl.addWidget(self.urls)
        sep = QFrame(objectName="sep")
        sep.setFixedHeight(1)
        cl.addWidget(sep)

        row = QHBoxLayout()
        row.setSpacing(10)
        segbox = QFrame(objectName="segbox")
        sl = QHBoxLayout(segbox)
        sl.setContentsMargins(3, 3, 3, 3)
        sl.setSpacing(2)
        self.seg = QButtonGroup(self)
        self.btn_mp3 = QPushButton(" MP3", objectName="seg", checkable=True)
        self.btn_mp4 = QPushButton(" MP4", objectName="seg", checkable=True)
        for b in (self.btn_mp3, self.btn_mp4):
            self.seg.addButton(b)
            sl.addWidget(b)
        row.addWidget(segbox)
        self.quality = QComboBox()
        self.quality.setMinimumWidth(120)
        row.addWidget(self.quality)
        row.addSpacing(6)
        row.addWidget(QLabel("Aynı anda", objectName="muted"))
        self.parallel = QComboBox()
        self.parallel.addItems([str(n) for n in range(1, 7)])
        self.parallel.setCurrentText(str(self.settings.value("parallel", 3)))
        row.addWidget(self.parallel)
        row.addStretch()
        paste = QPushButton(" Yapıştır", objectName="ghost")
        paste.setIcon(glyph_icon(G_PASTE, C["muted"]))
        paste.clicked.connect(self.paste)
        row.addWidget(paste)
        self.btn_go = QPushButton(" İndir", objectName="primary")
        self.btn_go.setIcon(glyph_icon(G_DOWNLOAD, "white"))
        self.btn_go.setToolTip("Ctrl+Enter")
        self.btn_go.clicked.connect(self.start)
        row.addWidget(self.btn_go)
        cl.addLayout(row)
        lay.addWidget(card)

        out = QHBoxLayout()
        out.setSpacing(8)
        ficon = QLabel()
        ficon.setPixmap(glyph_pixmap(G_FOLDER, C["muted"], 16))
        out.addWidget(ficon)
        self.out_dir = self.settings.value("out_dir", str(DEFAULT_OUT))
        self.out_label = ElidedLabel(self.out_dir, Qt.TextElideMode.ElideMiddle, objectName="muted")
        out.addWidget(self.out_label, 1)
        change = QPushButton("Değiştir", objectName="ghost")
        change.clicked.connect(self.pick_dir)
        opendir = QPushButton("Klasörü aç", objectName="ghost")
        opendir.clicked.connect(lambda: self.open_path(self.out_dir))
        out.addWidget(change)
        out.addWidget(opendir)
        lay.addLayout(out)

        lh = QHBoxLayout()
        lh.addWidget(QLabel("İNDİRMELER", objectName="section"))
        lh.addStretch()
        self.summary = QLabel(objectName="muted")
        lh.addWidget(self.summary)
        lay.addLayout(lh)

        self.list = QWidget(objectName="list")
        self.list_lay = QVBoxLayout(self.list)
        self.list_lay.setContentsMargins(0, 0, 4, 0)
        self.list_lay.setSpacing(8)
        self.empty = QWidget()
        el = QVBoxLayout(self.empty)
        el.setContentsMargins(0, 40, 0, 0)
        el.setSpacing(10)
        ei = QLabel()
        ei.setPixmap(glyph_pixmap(G_DOWNLOAD, C["faint"], 64, bg=C["surface"], radius=18))
        ei.setAlignment(Qt.AlignmentFlag.AlignCenter)
        et = QLabel("Henüz indirme yok.\nLinki yukarı yapıştır, İndir'e bas.", objectName="empty")
        et.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addWidget(ei)
        el.addWidget(et)
        self.list_lay.addWidget(self.empty)
        self.list_lay.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self.list)
        lay.addWidget(scroll, 1)

        foot = QHBoxLayout()
        self.hint = QLabel("Biten satıra çift tıkla, dosyayı klasörde gösteririm.", objectName="faint")
        foot.addWidget(self.hint, 1)
        clear = QPushButton(" Bitenleri temizle", objectName="ghost")
        clear.setIcon(glyph_icon(G_CLEAR, C["muted"]))
        clear.clicked.connect(self.clear_done)
        self.btn_cancel = QPushButton(" Hepsini iptal et", objectName="ghost")
        self.btn_cancel.setIcon(glyph_icon(G_CANCEL, C["muted"]))
        self.btn_cancel.clicked.connect(self.cancel_all)
        foot.addWidget(clear)
        foot.addWidget(self.btn_cancel)
        lay.addLayout(foot)

        self.setCentralWidget(root)
        self.seg.buttonClicked.connect(lambda _: self._format_changed())
        (self.btn_mp4 if self.settings.value("fmt", "mp3") == "mp4" else self.btn_mp3).setChecked(True)
        self._format_changed(self.settings.value("quality"))
        QShortcut(QKeySequence("Ctrl+Return"), self).activated.connect(self.start)
        QShortcut(QKeySequence("Ctrl+Enter"), self).activated.connect(self.start)
        self.refresh_summary()

    def fmt(self):
        return "mp4" if self.btn_mp4.isChecked() else "mp3"

    def _format_changed(self, keep=None):
        on, off = (self.btn_mp4, self.btn_mp3) if self.fmt() == "mp4" else (self.btn_mp3, self.btn_mp4)
        on.setIcon(glyph_icon(G_VIDEO if on is self.btn_mp4 else G_MUSIC, "white"))
        off.setIcon(glyph_icon(G_VIDEO if off is self.btn_mp4 else G_MUSIC, C["muted"]))
        self.fill_quality(keep)

    def fill_quality(self, keep=None):
        self.quality.clear()
        if self.fmt() == "mp3":
            self.quality.addItems([f"{q} kbps" for q in MP3_QUALITIES])
        else:
            self.quality.addItems(MP4_QUALITIES)
        if keep:
            i = self.quality.findText(keep)
            if i >= 0:
                self.quality.setCurrentIndex(i)

    # ---------- işlemler ----------
    def paste(self):
        text = QGuiApplication.clipboard().text().strip()
        if text:
            cur = self.urls.toPlainText().strip()
            self.urls.setPlainText(f"{cur}\n{text}".strip())

    def pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "İndirme klasörü", self.out_dir)
        if d:
            self.out_dir = d
            self.out_label.setFullText(d)
            self.settings.setValue("out_dir", d)

    def start(self):
        urls = [u.strip() for u in re.split(r"[\s,]+", self.urls.toPlainText()) if u.strip().startswith("http")]
        if not urls:
            self.hint.setText("Geçerli link yok. YouTube linkini yukarıdaki kutuya yapıştır.")
            return
        Path(self.out_dir).mkdir(parents=True, exist_ok=True)
        if self.active == 0:
            self.cancel = threading.Event()
        self.pool.setMaxThreadCount(int(self.parallel.currentText()))
        self.settings.setValue("fmt", self.fmt())
        self.settings.setValue("quality", self.quality.currentText())
        self.settings.setValue("parallel", int(self.parallel.currentText()))
        # aynı video farklı link biçimleriyle gelebilir (youtu.be, shorts, &t=): iki iş aynı dosyaya yazmasın
        unique = {}
        for u in urls:
            unique.setdefault(video_id(u) or u, u)
        for u in unique.values():
            if is_playlist(u):
                job_id = self.add_row(u, "list")
                self.on_status(job_id, "Playlist açılıyor")
                self.active += 1
                self.pool.start(PlaylistResolver(job_id, u, self.signals))
            else:
                self.enqueue(self.add_row(u), u)
        self.urls.clear()
        self.hint.setText("Biten satıra çift tıkla, dosyayı klasörde gösteririm.")
        self.refresh_summary()

    def enqueue(self, job_id, url):
        q = self.quality.currentText().split()[0]
        self.active += 1
        self.pool.start(DownloadJob(job_id, url, self.fmt(), q, self.out_dir, self.cancel, self.signals))

    def add_row(self, title, kind=None):
        job_id = uuid.uuid4().hex
        row = JobRow(title, kind or self.fmt(), self.reveal)
        self.list_lay.insertWidget(self.list_lay.count() - 1, row)   # stretch'in önüne
        self.empty.hide()
        self.rows[job_id] = row
        self.all_rows.append(row)
        return job_id

    def cancel_all(self):
        self.cancel.set()
        self.pool.clear()   # henüz başlamamışları kuyruktan at
        for job_id, row in list(self.rows.items()):
            if row.state == "queued":
                self.on_done(job_id, False, "İptal edildi", "")
        self.hint.setText("İptal ediliyor…")

    def clear_done(self):
        if self.active:
            self.hint.setText("İndirme sürerken temizlenmez, bitince bas.")
            return
        for row in self.all_rows:
            row.deleteLater()
        self.all_rows.clear()
        self.rows.clear()
        self.n_ok = self.n_err = self.n_cancel = 0
        self.empty.show()
        self.refresh_summary()

    # ---------- sinyaller ----------
    def on_title(self, job_id, title):
        if row := self.rows.get(job_id):
            row.set_title(title)

    def on_status(self, job_id, text):
        if row := self.rows.get(job_id):
            was = row.state
            row.set_status(text)
            if row.state != was:
                self.refresh_summary()

    def on_progress(self, job_id, pct, speed, eta):
        if row := self.rows.get(job_id):
            row.set_progress(pct, speed, eta)

    def on_done(self, job_id, ok, msg, path):
        row = self.rows.pop(job_id, None)
        if row is None:
            return
        self.active = max(0, self.active - 1)
        row.finish(ok, msg, path)
        if ok:
            self.n_ok += 1
        elif row.state == "cancelled":
            self.n_cancel += 1
        else:
            self.n_err += 1
        if self.active == 0 and self.cancel.is_set():
            self.hint.setText("İptal edildi.")
        self.refresh_summary()

    def on_expanded(self, job_id, entries):
        row = self.rows.pop(job_id, None)
        self.active = max(0, self.active - 1)
        if row is not None:
            row.finish(True, f"{len(entries)} video sıraya eklendi")
        if self.cancel.is_set():
            return
        for url, title in entries:
            self.enqueue(self.add_row(title), url)
        self.refresh_summary()

    def refresh_summary(self):
        parts = []
        if self.active:
            # paralel sınırı yüzünden bir kısmı sırada bekliyor: ikisini ayrı göster
            queued = sum(1 for r in self.rows.values() if r.state == "queued")
            if self.active - queued:
                parts.append(f"<span style='color:{C['text']}'><b>{self.active - queued}</b> iniyor</span>")
            if queued:
                parts.append(f"<span style='color:{C['muted']}'>{queued} sırada</span>")
        if self.n_ok:
            parts.append(f"<span style='color:{C['ok']}'>{self.n_ok} bitti</span>")
        if self.n_err:
            parts.append(f"<span style='color:{C['err']}'>{self.n_err} hata</span>")
        if self.n_cancel:
            parts.append(f"<span style='color:{C['muted']}'>{self.n_cancel} iptal</span>")
        self.summary.setText("  ·  ".join(parts) or "Hazır")

    # ---------- yardımcılar ----------
    def reveal(self, row):
        if row.path and os.path.exists(row.path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(row.path)])
        else:
            self.open_path(self.out_dir)

    def open_path(self, p):
        Path(p).mkdir(parents=True, exist_ok=True)
        os.startfile(p)

    def update_ytdlp(self):
        if self.active:
            QMessageBox.information(self, "YT İndirici", "Önce indirmeler bitsin.")
            return
        self.btn_update.setEnabled(False)
        self.btn_update.setText("Güncelleniyor…")
        QApplication.processEvents()
        py = sys.executable.replace("pythonw.exe", "python.exe")
        cmds = [["uv", "pip", "install", "-U", "yt-dlp[default]", "--python", py],
                [py, "-m", "pip", "install", "-U", "yt-dlp[default]"]]
        ok, out = False, ""
        for cmd in cmds:
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                out = (p.stdout + p.stderr).strip()
                if p.returncode == 0:
                    ok = True
                    break
            except (OSError, subprocess.TimeoutExpired) as e:
                out = str(e)
        self.btn_update.setEnabled(True)
        self.btn_update.setText(" yt-dlp güncelle")
        if ok:
            QMessageBox.information(self, "YT İndirici",
                                    "yt-dlp güncel. Yeni sürüm uygulamayı yeniden açınca devreye girer.")
        else:
            QMessageBox.warning(self, "YT İndirici", f"Güncellenemedi:\n{out[-600:]}")


def excepthook(etype, value, tb):
    """pythonw'da konsol yok: beklenmeyen hatayı hata.log'a yaz ve göster, sessizce ölme."""
    import traceback
    text = "".join(traceback.format_exception(etype, value, tb))
    try:
        (APP_DIR / "hata.log").write_text(text, encoding="utf-8")
    except OSError:
        pass
    if QApplication.instance():
        QMessageBox.critical(None, "YT İndirici", f"Beklenmeyen hata (hata.log'a yazıldı):\n\n{text[-800:]}")


def main():
    sys.excepthook = excepthook
    if sys.platform == "win32":
        # görev çubuğunda python ikonu yerine uygulamanın kendi ikonu görünsün
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("YTIndirici.App")
    app = QApplication(sys.argv)
    apply_style(app)
    app.setWindowIcon(QIcon(logo_pixmap(64)))
    w = Window()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
