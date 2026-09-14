"""
gui.py – Interface graphique PyQt5 pour la surveillance multi-caméras.

Design inspiré d'un thème GitHub Dark professionnel.

Architecture :
  - Thread Qt : rendu preview (QTimer 66 ms, ~15 fps) + métriques (500 ms).
  - Thread Python dédié (RecPushThread) : interroge l'aligner à ~30 fps
    et pousse les snapshots au RecordingWorker sans bloquer le thread Qt.
  - Thread RecordingWorker : encode JPEG via cv2.imencode + mux MKV PyAV.
  - Les timestamps affichés proviennent exclusivement des streams
    (NDI timecode ou RTCP SR), jamais recalculés ici.
  - Métriques de synchronisation calculées par SyncMetricsEngine.
  - Alignement temporel via StreamAligner.
"""

import collections
import datetime
import math
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from fractions import Fraction

import av
import cv2
import numpy as np
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QImage, QPixmap, QColor
from PyQt5.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
import pyqtgraph as pg

from gst_camera_stream import TimestampedFrame
from flash_calibrator import FlashCalibrator
from ntp_clock import NTPClock
from stream_aligner import AlignedSnapshot, StreamAligner
from sync_metrics import SyncMetricsEngine

# ══════════════════════════════════════════════════════════════════════════════
#  PALETTE
# ══════════════════════════════════════════════════════════════════════════════
_BG      = "#0d1117"
_BG2     = "#161b22"
_BG3     = "#1c2128"
_BORDER  = "#30363d"
_TEXT    = "#c9d1d9"
_TEXT2   = "#8b949e"
_GOLD    = "#f0c040"
_GREEN   = "#3fb950"
_ORANGE  = "#d29922"
_RED     = "#f85149"
_BLUE    = "#58a6ff"

_FONT_MONO = "Courier New"
_FONT_UI   = "Segoe UI"

# Seuils offset pour la coloration (ms)
_OFF_GOOD = 10.0   # < 10 ms  → vert
_OFF_WARN = 33.0   # < 33 ms  → orange  (1 frame @30fps)
                   # ≥ 33 ms  → rouge


# ══════════════════════════════════════════════════════════════════════════════
#  UTILITAIRES
# ══════════════════════════════════════════════════════════════════════════════

def _style(color: str, size: int = 10, bold: bool = False,
           mono: bool = False) -> str:
    family = _FONT_MONO if mono else _FONT_UI
    weight = "bold" if bold else "normal"
    return (f"color: {color}; font-size: {size}px; font-weight: {weight}; "
            f"font-family: '{family}'; border: none;")


def _lbl(text: str = "", size: int = 10, color: str = _TEXT,
         bold: bool = False, mono: bool = False) -> QLabel:
    lw = QLabel(text)
    lw.setStyleSheet(_style(color, size, bold, mono))
    return lw


def _offset_color(abs_ms: float) -> str:
    if abs_ms < _OFF_GOOD:
        return _GREEN
    if abs_ms < _OFF_WARN:
        return _ORANGE
    return _RED


def _cv_to_pixmap(frame: np.ndarray, target_w: int, target_h: int,
                  keep_ratio: bool = False) -> QPixmap:
    """Convertit une frame BGR en QPixmap.
    keep_ratio=True → letterbox (bords noirs), False → étirer."""
    if keep_ratio:
        h, w = frame.shape[:2]
        scale = min(target_w / w, target_h / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
        canvas = np.zeros((target_h, target_w, 3), dtype=np.uint8)
        y0, x0 = (target_h - nh) // 2, (target_w - nw) // 2
        canvas[y0:y0 + nh, x0:x0 + nw] = resized
        rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
        qimg = QImage(rgb.data, target_w, target_h, target_w * 3, QImage.Format_RGB888)
    else:
        resized = cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        qimg = QImage(rgb.data, target_w, target_h, target_w * 3, QImage.Format_RGB888)
    return QPixmap.fromImage(qimg.copy())


# ══════════════════════════════════════════════════════════════════════════════
#  METRIC CARD
# ══════════════════════════════════════════════════════════════════════════════

class MetricCard(QFrame):
    """Petite carte affichant une valeur numérique avec titre et unité."""

    def __init__(self, title: str, unit: str, color: str = _GOLD, parent=None):
        super().__init__(parent)
        self._default_color = color
        self.setStyleSheet(f"QFrame {{ background-color: {_BG2}; "
                            f"border: 1px solid {_BORDER}; border-radius: 4px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(1)

        t = _lbl(title, 9, _TEXT2)
        t.setAlignment(Qt.AlignCenter)
        lay.addWidget(t)

        self._val = _lbl("--", 20, color, bold=True, mono=True)
        self._val.setAlignment(Qt.AlignCenter)
        lay.addWidget(self._val)

        u = _lbl(unit, 8, _TEXT2)
        u.setAlignment(Qt.AlignCenter)
        lay.addWidget(u)

    def set_value(self, v: float, fmt: str = "{:.2f}", color: str | None = None):
        self._val.setText(fmt.format(v))
        c = color or self._default_color
        self._val.setStyleSheet(
            f"color: {c}; font-size: 20px; font-weight: bold; "
            f"font-family: '{_FONT_MONO}'; border: none;")


# ══════════════════════════════════════════════════════════════════════════════
#  CAMERA WIDGET
# ══════════════════════════════════════════════════════════════════════════════

class CameraWidget(QFrame):
    """
    Vignette d'une caméra :

      ┌────────────────────────────────────────────────────────┐
      │ ⬤  CAM 01  Ottica 4K #1              [NDI TC ✓]  12 TC│  ← top bar
      ├────────────────────────────────────────────────────────┤
      │                                                        │
      │                 vidéo (expanding)                      │
      │                                                        │
      ├────────────────────────────────────────────────────────┤
      │ 14:23:01.456  UTC              25.0 fps  |  seq 42     │  ← bottom bar
      └────────────────────────────────────────────────────────┘
    """

    _SRC_BADGE: dict[str, tuple[str, str]] = {
        "ndi_timecode": (f"background:{_GREEN};  color:#000;", "NDI TC \u2713"),
        "rtcp_sr":      (f"background:{_GREEN};  color:#000;", "RTCP SR \u2713"),
        "host_ntp":     (f"background:{_ORANGE}; color:#000;", "NTP host"),
    }
    _BADGE_COMMON = ("font-size:9px; font-weight:bold; border-radius:3px; "
                     "padding:1px 5px; border:none;")

    def __init__(self, cam_index: int, camera, parent=None):
        super().__init__(parent)
        self._camera = camera
        self._last_seq: int = -1
        self._last_frame_mono: float = 0.0
        self._keep_ratio = True

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {_BG2};
                border: 1px solid {_BORDER};
                border-radius: 4px;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Top bar ───────────────────────────────────────────────────────
        top = QWidget()
        top.setFixedHeight(28)
        top.setStyleSheet(f"background-color:{_BG3}; border:none; "
                           f"border-bottom:1px solid {_BORDER};")
        tb = QHBoxLayout(top)
        tb.setContentsMargins(8, 0, 8, 0)
        tb.setSpacing(6)

        self._dot = _lbl("\u25cf", 9, _ORANGE)
        tb.addWidget(self._dot)
        tb.addWidget(_lbl(f"CAM {cam_index + 1:02d}", 11, _TEXT, bold=True))
        tb.addWidget(_lbl(camera.name, 10, _TEXT2))
        tb.addStretch()

        self._badge = QLabel("--")
        self._badge.setStyleSheet(
            f"color:{_TEXT2}; font-size:9px; border:none; padding:1px 5px;")
        tb.addWidget(self._badge)

        self._lbl_tc = _lbl("0 SR", 9, _TEXT2)
        tb.addWidget(self._lbl_tc)

        root.addWidget(top)

        # ── Video area ────────────────────────────────────────────────────
        self._video = QLabel("Waiting for stream\u2026")
        self._video.setAlignment(Qt.AlignCenter)
        self._video.setMinimumSize(260, 160)
        self._video.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._video.setStyleSheet(
            f"background:#000; color:{_TEXT2}; font-size:12px; border:none;")
        root.addWidget(self._video)

        # ── Bottom bar ────────────────────────────────────────────────────
        bot = QWidget()
        bot.setFixedHeight(32)
        bot.setStyleSheet(f"background-color:{_BG3}; border:none; "
                           f"border-top:1px solid {_BORDER};")
        bb = QHBoxLayout(bot)
        bb.setContentsMargins(8, 0, 8, 0)
        bb.setSpacing(0)

        self._lbl_utc = _lbl("UTC --:--:--.---", 13, _GOLD, bold=True, mono=True)
        bb.addWidget(self._lbl_utc)
        bb.addSpacing(8)
        self._lbl_age = _lbl("\u0394 --", 9, _TEXT2, mono=True)
        bb.addWidget(self._lbl_age)
        bb.addStretch()
        self._lbl_residual = _lbl("", 9, _TEXT2, mono=True)
        bb.addWidget(self._lbl_residual)
        bb.addSpacing(4)
        self._lbl_info = _lbl("-- fps  |  seq --", 9, _TEXT2, mono=True)
        bb.addWidget(self._lbl_info)

        root.addWidget(bot)

    # ── Refresh ───────────────────────────────────────────────────────────

    def refresh(self, tf: TimestampedFrame | None = None,
                residual_ms: float | None = None):
        if tf is None:
            tf = self._camera.latest_frame()

        now_m = time.monotonic()

        if tf is not None:
            vw, vh = self._video.width(), self._video.height()
            if vw > 0 and vh > 0:
                self._video.setPixmap(_cv_to_pixmap(tf.frame, vw, vh, self._keep_ratio))

            utc = datetime.datetime.fromtimestamp(
                tf.ntp_timestamp, tz=datetime.timezone.utc)
            self._lbl_utc.setText("UTC " + utc.strftime("%H:%M:%S.%f")[:-3])

            src = getattr(tf, "timestamp_source", "host_ntp")
            badge_css, badge_txt = self._SRC_BADGE.get(
                src, (f"color:{_TEXT2};", src))
            self._badge.setText(badge_txt)
            self._badge.setStyleSheet(badge_css + self._BADGE_COMMON)

            if tf.sequence_number != self._last_seq:
                self._last_seq = tf.sequence_number
                self._last_frame_mono = now_m
            if self._last_frame_mono > 0:
                age_ms = (now_m - self._last_frame_mono) * 1000
                age_color = (_GREEN if age_ms < 200
                             else _ORANGE if age_ms < 1000 else _RED)
                self._lbl_age.setText(f"\u0394 {age_ms:.0f}ms")
                self._lbl_age.setStyleSheet(_style(age_color, 9, mono=True))
            else:
                self._lbl_age.setText("\u0394 --")
                self._lbl_age.setStyleSheet(_style(_TEXT2, 9, mono=True))
        else:
            self._video.clear()
            self._video.setText("No signal")
            self._lbl_utc.setText("UTC --:--:--.---")
            self._badge.setText("--")
            self._badge.setStyleSheet(
                f"color:{_TEXT2}; font-size:9px; border:none;")
            self._lbl_age.setText("\u0394 --")
            self._lbl_age.setStyleSheet(_style(_TEXT2, 9, mono=True))

        # Résidu d'alignement.
        # Borné par l'intervalle inter-frames : ±8 ms @ 60 fps (Ottica),
        # ±18 ms @ 28 fps (Insta). Seuils ajustés en conséquence.
        if residual_ms is not None:
            res_color = (_GREEN if abs(residual_ms) < 25
                         else _ORANGE if abs(residual_ms) < 50 else _RED)
            self._lbl_residual.setText(f"\u00b1{abs(residual_ms):.1f}ms")
            self._lbl_residual.setStyleSheet(_style(res_color, 9, mono=True))
        else:
            self._lbl_residual.setText("")

        # FPS + séquence
        fps = self._camera.actual_fps
        seq = tf.sequence_number if tf is not None else 0
        self._lbl_info.setText(f"{fps:.1f} fps  |  seq {seq}")

        # Compteur timestamps caméra
        tc      = getattr(self._camera, "sr_count", 0)
        src_cam = getattr(self._camera, "timestamp_source", "host_ntp")
        tc_lbl  = "NDI" if src_cam == "ndi_timecode" else "SR"
        self._lbl_tc.setText(f"{tc} {tc_lbl}")
        self._lbl_tc.setStyleSheet(_style(_GREEN if tc > 0 else _TEXT2, 9))

        # Point de statut
        if self._camera.is_connected:
            self._dot.setStyleSheet(_style(_GREEN, 9))
        else:
            self._dot.setStyleSheet(_style(_RED, 9))
            if tf is None:
                self._video.setText("Reconnecting\u2026")


# ══════════════════════════════════════════════════════════════════════════════
#  RECORDING WORKER
# ══════════════════════════════════════════════════════════════════════════════

class RecordingWorker(threading.Thread):
    """Thread d'enregistrement découplé du GUI.

    Consomme des AlignedSnapshot depuis une queue bornée (maxsize=8) et écrit
    les frames dans des fichiers MKV (MJPEG) via PyAV avec timestamps PTS réels.

    Architecture :
        StreamAligner → Queue(maxsize=8) → RecordingWorker → PyAV MP4
        GUI           → preview only (aucun enregistrement dans _refresh_video)

    NOTE: Les frames sont intentionnellement copiées avant le push dans la
    queue pour garantir l'immutabilité des snapshots inter-thread.
    Coût mémoire : ~200 Mo au pic (8 snapshots × 4 cams × ~6 Mo/frame),
    préféré à tout risque de corruption silencieuse par buffer partagé.

    NOTE: Les caméras Ottica tournent nativement à ~60fps, mais la timeline
    d'enregistrement est gouvernée par le StreamAligner (~28fps, flux le plus
    lent). ~50% des frames Ottica sont intentionnellement abandonnées.
    C'est le compromis retenu pour garantir un playback synchronisé (Option A).
    """

    def __init__(self, snapshot_queue: queue.Queue,
                 cameras: list,
                 rec_paths: dict,   # camera_id → chemin .mp4
                 csv_paths: dict,   # camera_id → chemin .csv
                 ):
        super().__init__(name="RecordingWorker", daemon=True)
        self._queue    = snapshot_queue
        self._cameras  = cameras
        self._rec_paths = rec_paths
        self._csv_paths = csv_paths
        self.written = 0   # snapshots écrits (toutes cams simultanément)
        self.dropped = 0   # snapshots droppés (queue pleine)

    def _init_cam_writer(self, cam, tf,
                         containers: dict, streams: dict,
                         csv_files: dict, frame_idx: dict) -> None:
        """Initialise le container MKV + stream MJPEG + CSV pour une caméra.

        Extrait du run() pour permettre l'initialisation tardive d'une caméra
        qui n'avait pas encore de frame au premier snapshot.
        """
        path = self._rec_paths.get(cam.camera_id)
        if not path:
            return
        h, w = tf.frame.shape[:2]

        # Matroska (.mkv) : streaming-friendly, VFR natif.
        container = av.open(str(path), mode='w', format='matroska')
        stream = container.add_stream('mjpeg', rate=30)
        stream.width   = w
        stream.height  = h
        stream.pix_fmt = 'yuvj420p'
        stream.time_base = Fraction(1, 1000)
        stream.codec_context.time_base = Fraction(1, 1000)
        containers[cam.camera_id] = container
        streams[cam.camera_id]    = stream
        frame_idx[cam.camera_id]  = 0

        csv_path = self._csv_paths.get(cam.camera_id)
        if csv_path:
            f = open(csv_path, 'w', encoding='utf-8')
            f.write("frame_idx,ntp_timestamp,mean_luma\n")
            csv_files[cam.camera_id] = f

    def run(self):
        containers: dict = {}   # camera_id → av.OutputContainer
        streams:    dict = {}   # camera_id → av.VideoStream
        csv_files:  dict = {}   # camera_id → file handle
        frame_idx:  dict = {}   # camera_id → int
        start_ntp: float | None = None

        # Pool d'encodage persistant : évite l'overhead de spawn/teardown
        # de threads à chaque snapshot (~10-30 ms économisés par tour).
        pool = ThreadPoolExecutor(
            max_workers=max(1, len(self._cameras)),
            thread_name_prefix="RecEnc",
        )

        try:
            while True:
                snapshot = self._queue.get()

                # Sentinel → arrêt propre
                if snapshot is None:
                    break

                # ── Initialisation au premier snapshot ──────────────────
                if start_ntp is None:
                    start_ntp = snapshot.reference_ntp
                    for cam in self._cameras:
                        tf = snapshot.frames.get(cam.camera_id)
                        if tf is None or tf.frame is None:
                            print(
                                f"[RecordingWorker] WARNING : caméra "
                                f"{cam.name} absente du premier snapshot "
                                f"— sera initialisée dès qu'une frame arrive.",
                                flush=True,
                            )
                            continue
                        self._init_cam_writer(
                            cam, tf, containers, streams, csv_files, frame_idx
                        )

                # ── PTS en ms relatifs au début de l'enregistrement ────
                pts_ms = int((snapshot.reference_ntp - start_ntp) * 1000.0)
                pts_ms = max(pts_ms, 0)

                # ── Initialisation tardive des caméras manquantes ───────
                # (au cas où la caméra n'avait pas encore de frame au premier
                # snapshot, elle est ajoutée dès qu'une frame est disponible).
                for cam in self._cameras:
                    if cam.camera_id in containers:
                        continue
                    tf = snapshot.frames.get(cam.camera_id)
                    if tf is None or tf.frame is None:
                        continue
                    self._init_cam_writer(
                        cam, tf, containers, streams, csv_files, frame_idx
                    )
                    print(
                        f"[RecordingWorker] Caméra {cam.name} initialisée "
                        f"tardivement (première frame reçue).",
                        flush=True,
                    )

                t_write_start = time.monotonic()

                # ── Encodage des 4 caméras en parallèle ───────────────
                # cv2.imencode libère le GIL → exécution C réellement parallèle
                # sur plusieurs cœurs. Les 4 threads s'exécutent simultanément,
                # latence totale ≈ max(cam1..4) ≈ 5-20 ms au lieu de 100+ ms.
                def _encode_cam(cam):
                    tf = snapshot.frames.get(cam.camera_id)
                    if tf is None or cam.camera_id not in containers:
                        return
                    stream    = streams[cam.camera_id]
                    container = containers[cam.camera_id]

                    # Encodage JPEG via OpenCV (bypass pipeline PyAV MJPEG)
                    ok, jpeg_buf = cv2.imencode(
                        '.jpg', tf.frame,
                        [cv2.IMWRITE_JPEG_QUALITY, 85]
                    )
                    if not ok:
                        return

                    # Injection directe des bytes JPEG comme packet MKV
                    packet           = av.Packet(jpeg_buf)
                    packet.pts       = pts_ms
                    packet.dts       = pts_ms
                    packet.time_base = Fraction(1, 1000)
                    packet.stream    = stream
                    try:
                        container.mux(packet)
                    except Exception as _mux_err:
                        print(f"[RecordingWorker] Erreur mux {cam.camera_id}: "
                              f"{_mux_err}", flush=True)

                    idx       = frame_idx[cam.camera_id]
                    mean_luma = float(tf.frame[..., 1].mean())
                    csv_f     = csv_files.get(cam.camera_id)
                    if csv_f:
                        csv_f.write(f"{idx},{tf.ntp_timestamp:.6f},{mean_luma:.2f}\n")
                    frame_idx[cam.camera_id] = idx + 1

                futures = [pool.submit(_encode_cam, cam) for cam in self._cameras]
                for f in as_completed(futures):
                    f.result()  # propage les exceptions éventuelles

                self.written += 1

                write_ms = (time.monotonic() - t_write_start) * 1000.0
                if write_ms > 100.0:
                    print(f"[RecordingWorker] Latence écriture élevée : "
                          f"{write_ms:.0f} ms", flush=True)

        finally:
            # ── Flush encodeurs + fermeture propre des containers ───────
            for cam_id, container in containers.items():
                stream = streams.get(cam_id)
                if stream:
                    try:
                        # MJPEG raw-mux : pas d'état encodeur à flusher.
                        # L'appel stream.encode() est un no-op ici.
                        for packet in stream.encode():
                            container.mux(packet)
                    except Exception:
                        pass  # normal pour un stream MJPEG raw-mux
                try:
                    container.close()
                except Exception as e:
                    print(f"[RecordingWorker] Close {cam_id}: {e}", flush=True)

            for f in csv_files.values():
                try:
                    f.flush()
                    f.close()
                except Exception:
                    pass

            # Arrêt propre du pool d'encodage persistant.
            try:
                pool.shutdown(wait=True, cancel_futures=True)
            except TypeError:
                # cancel_futures requiert Python 3.9+ ; fallback
                try:
                    pool.shutdown(wait=True)
                except Exception:
                    pass
            except Exception:
                pass

            print(f"[RecordingWorker] Done — {self.written} snapshots written, "
                  f"{self.dropped} dropped.", flush=True)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN WINDOW
# ══════════════════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    """Fenêtre principale de l'application."""

    def __init__(self, cameras: list, ntp_clock: NTPClock,
                 metrics_engine: SyncMetricsEngine, aligner: StreamAligner,
                 dyn_aligner=None,
                 ):
        super().__init__()
        self._cameras        = cameras
        self._clock          = ntp_clock
        self._metrics_engine = metrics_engine
        self._aligner        = aligner
        self._dyn_aligner    = dyn_aligner

        self.setWindowTitle("Cam_Synchro")
        self.setMinimumSize(1280, 760)
        self.setStyleSheet(
            f"QMainWindow, QWidget {{ background-color:{_BG}; color:{_TEXT}; }}")

        # ── Recording state ───────────────────────────────────────────────
        self._recording: bool = False
        self._record_start_mono: float = 0.0
        self._record_dir: str = ""
        self._rec_queue:  queue.Queue | None = None
        self._rec_worker: RecordingWorker | None = None
        self._last_snap_ntp: float = 0.0  # déduplication côté producteur

        self._flash_calib = FlashCalibrator(cameras)
        # Historique des calibrations idéales absolues (fenêtre glissante de 5).
        # Chaque entrée = {cam_id: ideal_calib_ms} — valeur absolue indépendante
        # de la calibration courante. Jamais remis à zéro entre les applications :
        # la médiane sur N mesures absorbe la variabilité pipeline ±75ms des Insta.
        self._flash_history: list[dict] = []
        self._flash_reject_streak: int = 0
        self._cam_name_by_id = {c.camera_id: c.name for c in cameras}

        self._build_ui()
        self._start_timers()

    # ── Construction UI ───────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Barre de titre ────────────────────────────────────────────────
        title_bar = QWidget()
        title_bar.setFixedHeight(40)
        title_bar.setStyleSheet(
            f"background-color:{_BG2}; border-bottom:1px solid {_BORDER};")
        tb = QHBoxLayout(title_bar)
        tb.setContentsMargins(14, 0, 14, 0)
        tb.setSpacing(12)

        logo = _lbl("\u25b6  CAM_SYNCHRO", 13, _TEXT, bold=True)
        logo.setStyleSheet(logo.styleSheet() + " letter-spacing:2px;")
        tb.addWidget(logo)
        tb.addStretch()

        self._btn_align = QPushButton("\u27f3  Align streams")
        self._btn_align.setFixedSize(140, 28)
        self._btn_align.setStyleSheet(f"""
            QPushButton {{
                background-color: #238636; color: white;
                border: none; border-radius: 4px;
                font-size: 11px; font-weight: bold;
            }}
            QPushButton:hover   {{ background-color: {_GREEN}; }}
            QPushButton:pressed {{ background-color: #196127; }}
        """)
        self._btn_align.clicked.connect(self._on_align_toggle)
        tb.addWidget(self._btn_align)

        self._btn_flash = QPushButton("\u26a1  Flash Calib")
        self._btn_flash.setFixedSize(135, 28)
        self._btn_flash.setToolTip(
            "Measures the actual visual offset between the streams.\n"
            "Click, then trigger a sharp brightness change in the shared\n"
            "field of view (turn the light OFF or ON in a single action)\n"
            "during the capture window (8 s).")
        self._btn_flash.setStyleSheet(f"""
            QPushButton {{
                background-color: #a371f7; color: white;
                border: none; border-radius: 4px;
                font-size: 11px; font-weight: bold;
            }}
            QPushButton:hover   {{ background-color: #b389f9; }}
            QPushButton:pressed {{ background-color: #8957e5; }}
            QPushButton:disabled {{ background-color: #444; color: #888; }}
        """)
        self._btn_flash.clicked.connect(self._on_flash_calib)
        tb.addWidget(self._btn_flash)

        self._btn_flash_reset = QPushButton("↺  Reset Calib")
        self._btn_flash_reset.setFixedSize(110, 28)
        self._btn_flash_reset.setToolTip(
            "Resets all flash calibration delays to 0 ms and clears the\n"
            "measurement history. Use this if the image alignment looks wrong\n"
            "after a bad calibration run.")
        self._btn_flash_reset.setStyleSheet(f"""
            QPushButton {{
                background-color: #555; color: #ccc;
                border: none; border-radius: 4px;
                font-size: 11px; font-weight: bold;
            }}
            QPushButton:hover   {{ background-color: #666; color: white; }}
            QPushButton:pressed {{ background-color: #444; color: white; }}
        """)
        self._btn_flash_reset.clicked.connect(self._on_flash_calib_reset)
        tb.addWidget(self._btn_flash_reset)

        self._btn_record = QPushButton("\u23fa  Record")
        self._btn_record.setFixedSize(120, 28)
        self._btn_record.setStyleSheet(f"""
            QPushButton {{
                background-color: {_RED}; color: white;
                border: none; border-radius: 4px;
                font-size: 11px; font-weight: bold;
            }}
            QPushButton:hover   {{ background-color: #da3633; }}
            QPushButton:pressed {{ background-color: #b62324; }}
        """)
        self._btn_record.clicked.connect(self._on_record_toggle)
        tb.addWidget(self._btn_record)

        self._lbl_rec_elapsed = _lbl("", 11, _RED, mono=True, bold=True)
        self._lbl_rec_elapsed.setVisible(False)
        tb.addWidget(self._lbl_rec_elapsed)

        root.addWidget(title_bar)

        # ── Métriques globales (au-dessus des flux) ──────────────────────
        cards_bar = QWidget()
        cards_bar.setFixedHeight(64)
        cards_bar.setStyleSheet(f"background-color:{_BG}; border:none;")
        cb = QHBoxLayout(cards_bar)
        cb.setContentsMargins(10, 4, 10, 4)
        cb.setSpacing(8)

        self._card_offset = MetricCard("MAX OFFSET", "ms",     _GOLD)
        self._card_jitter = MetricCard("AVG JITTER", "ms",     _GOLD)
        self._card_drift  = MetricCard("DRIFT",      "ms/min", _GREEN)
        for card in (self._card_offset, self._card_jitter, self._card_drift):
            cb.addWidget(card)

        root.addWidget(cards_bar)

        # ── Bandeau résultat validation (caché par défaut) ───────────────
        self._validation_bar = QFrame()
        self._validation_bar.setFixedHeight(48)
        self._validation_bar.setStyleSheet(
            f"QFrame {{ background-color: {_BG2}; "
            f"border: 1px solid {_BORDER}; border-radius: 4px; }}")
        self._validation_bar.setVisible(False)
        vb_lay = QHBoxLayout(self._validation_bar)
        vb_lay.setContentsMargins(14, 4, 14, 4)
        vb_lay.setSpacing(12)

        self._lbl_val_icon = _lbl("", 16, _GREEN, bold=True)
        vb_lay.addWidget(self._lbl_val_icon)
        self._lbl_val_title = _lbl("", 12, _TEXT, bold=True)
        vb_lay.addWidget(self._lbl_val_title)
        vb_lay.addSpacing(8)
        self._lbl_val_detail = _lbl("", 10, _TEXT2, mono=True)
        vb_lay.addWidget(self._lbl_val_detail)
        vb_lay.addStretch()
        self._lbl_val_spread = _lbl("", 14, _GOLD, bold=True, mono=True)
        vb_lay.addWidget(self._lbl_val_spread)

        root.addWidget(self._validation_bar)

        # ── Splitter vertical : vidéo en haut, graphe en bas ──────────
        splitter = QSplitter(Qt.Vertical)
        splitter.setStyleSheet(
            f"QSplitter::handle {{ background-color: {_BORDER}; height: 3px; }}")

        # ── Grille vidéo ─────────────────────────────────────────────────
        video_container = QWidget()
        n      = len(self._cameras)
        nb_col = math.ceil(math.sqrt(n))

        grid = QGridLayout(video_container)
        grid.setContentsMargins(10, 4, 10, 4)
        grid.setSpacing(8)

        self._cam_widgets: dict[str, CameraWidget] = {}
        for idx, cam in enumerate(self._cameras):
            cw = CameraWidget(idx, cam)
            grid.addWidget(cw, idx // nb_col, idx % nb_col)
            self._cam_widgets[cam.camera_id] = cw

        splitter.addWidget(video_container)

        # ── Graphiques : deux vues empilées ─────────────────────────────
        # 1) « Désync visuelle » : ce que l'œil voit à l'écran, calibration
        #    comprise (temps_visuel = ntp + display_delay + calib_delay).
        # 2) « Résidu temporel » : écart du timestamp affiché de chaque flux
        #    vs le temps de référence calculé par l'aligneur (ancien graphe).
        pg.setConfigOptions(antialias=True)

        _PLOT_COLORS = ["#58a6ff", "#3fb950", "#f85149",
                        "#bc8cff", "#ff7b72", "#79c0ff", "#56d364"]

        def _build_plot(title: str, ylabel: str):
            """Construit un PlotWidget configuré + ses courbes par caméra."""
            w = pg.PlotWidget(title=title)
            w.setBackground(_BG2)
            w.showGrid(x=True, y=True, alpha=0.3)
            # Pas d'unité SI sur l'axe Y → pyqtgraph n'ajoute pas de préfixe
            # k/M (sinon les gros offsets bruts ≈ 20000 ms → "20 kms").
            w.setLabel("left", ylabel)
            w.getAxis("left").enableAutoSIPrefix(False)
            w.setLabel("bottom", "Elapsed time (s)")
            w.getAxis("bottom").enableAutoSIPrefix(False)
            w.addLegend(
                offset=(10, 10),
                labelTextColor='#ffffff',
                labelTextSize='10pt',
                pen=pg.mkPen('#555555'),
                brush=pg.mkBrush('#1c2128cc'),
                colCount=1)
            w.setMinimumHeight(140)

            # Bandes de tolérance (vert ±33 ms, jaune ±100 ms)
            vb = w.getPlotItem().getViewBox()
            green_band = pg.LinearRegionItem(
                values=(-33, 33), orientation='horizontal', movable=False,
                brush=pg.mkBrush(QColor(63, 185, 80, 40)),
                pen=pg.mkPen(None))
            green_band.setZValue(-10)
            vb.addItem(green_band)
            for vals in ((33, 100), (-100, -33)):
                band = pg.LinearRegionItem(
                    values=vals, orientation='horizontal', movable=False,
                    brush=pg.mkBrush(QColor(240, 192, 64, 30)),
                    pen=pg.mkPen(None))
                band.setZValue(-10)
                vb.addItem(band)

            # Ligne y=0 (référence)
            w.addItem(pg.InfiniteLine(
                angle=0, pos=0,
                pen=pg.mkPen('#888888', width=1, style=Qt.DashLine)))

            curves: dict[str, pg.PlotDataItem] = {}
            history: dict[str, collections.deque] = {}
            for i, cam in enumerate(self._cameras[1:]):
                color = _PLOT_COLORS[i % len(_PLOT_COLORS)]
                curves[cam.camera_id] = w.plot(
                    pen=pg.mkPen(color, width=2),
                    name=f"{cam.name} − {self._cameras[0].name}")
                history[cam.camera_id] = collections.deque(maxlen=300)
            return w, curves, history

        ref_name = self._cameras[0].name
        # Graphe 1 : désync visuelle (effet réel perçu, calibration comprise)
        self._plot_widget, self._plot_curves, self._plot_history = _build_plot(
            f"Visual desync vs {ref_name}",
            "Visual desync (ms)")
        # Graphe 2 : résidu temporel vs temps de référence calculé
        (self._plot_widget_raw, self._plot_curves_raw,
         self._plot_history_raw) = _build_plot(
            f"Temporal residual vs {ref_name} (displayed timestamp vs target)",
            "Residual (ms)")

        self._plot_time_history: collections.deque = collections.deque(maxlen=300)
        self._plot_t0: float = 0.0

        # Fenêtre glissante du MAX OFFSET pour afficher une médiane lissée
        # au lieu du pic instantané. Le jitter RTCP des Insta produit des
        # pics ponctuels de ±150-340ms non représentatifs de la synchro
        # perçue ; la médiane sur ~15 s (30 ticks @ 2 Hz) reflète mieux
        # l'état réel sans masquer une vraie dérive.
        self._max_off_window: collections.deque = collections.deque(maxlen=30)

        # Marker vertical au moment où l'alignement est (dés)activé
        self._align_markers: list = []
        self._last_align_state: bool = self._aligner.is_enabled

        # Les deux graphes empilés verticalement dans un sous-splitter
        plots_splitter = QSplitter(Qt.Vertical)
        plots_splitter.addWidget(self._plot_widget)
        plots_splitter.addWidget(self._plot_widget_raw)
        plots_splitter.setStretchFactor(0, 1)
        plots_splitter.setStretchFactor(1, 1)

        splitter.addWidget(plots_splitter)
        splitter.setStretchFactor(0, 3)  # vidéo = 3/4
        splitter.setStretchFactor(1, 1)  # graphes = 1/4

        root.addWidget(splitter, stretch=1)

    # ── Timers ────────────────────────────────────────────────────────────────

    def _start_timers(self):
        # Timer rendu vidéo preview GUI : 15 fps suffisent à l'œil et libèrent
        # le thread GUI pour le timer d'enregistrement (qui doit, lui, tourner
        # à 30 fps pour produire des snapshots à pleine cadence).
        self._timer_video = QTimer(self)
        self._timer_video.timeout.connect(self._refresh_video)
        self._timer_video.start(66)       # ~15 fps affichage preview

        # Thread Python dédié à l'enregistrement (~30 fps), totalement
        # indépendant du thread Qt. Évite que le rendu GUI (resize+QImage,
        # ~40-60 ms) ne plafonne le rythme de capture d'enregistrement.
        self._rec_push_stop  = threading.Event()
        self._rec_push_thread = threading.Thread(
            target=self._rec_push_loop,
            name="RecPushThread",
            daemon=True,
        )
        self._rec_push_thread.start()

        self._timer_metrics = QTimer(self)
        self._timer_metrics.timeout.connect(self._refresh_metrics)
        self._timer_metrics.start(500)    # métriques 2 Hz

        # Timer dédié à la calibration flash : ~33 Hz, ne tourne que pendant
        # une session active. On le garde toujours "on" mais le tick() est
        # un no-op si la calibration n'est pas armée.
        self._timer_flash = QTimer(self)
        self._timer_flash.timeout.connect(self._refresh_flash_calib)
        self._timer_flash.start(100)  # 10 Hz suffit; évite de starve la GUI

        # Mise à jour de la durée d'enregistrement (1 Hz)
        self._timer_rec = QTimer(self)
        self._timer_rec.timeout.connect(self._refresh_rec_elapsed)
        self._timer_rec.start(1000)

    def _refresh_rec_elapsed(self):
        if not self._recording:
            return
        # Détection crash worker (depuis le thread Qt — accès widgets OK ici)
        if self._rec_worker is not None and not self._rec_worker.is_alive():
            self._recording = False
            self._rec_worker = None
            self._rec_queue  = None
            self._lbl_rec_elapsed.setVisible(False)
            self._btn_record.setText("\u23fa  Record")
            self._btn_record.setStyleSheet(f"""
                QPushButton {{
                    background-color: {_RED}; color: white;
                    border: none; border-radius: 4px;
                    font-size: 11px; font-weight: bold;
                }}
                QPushButton:hover {{ background-color: #da3633; }}
                QPushButton:pressed {{ background-color: #b62324; }}
            """)
            self.setWindowTitle("Cam_Synchro")
            return
        elapsed = time.monotonic() - self._record_start_mono
        h = int(elapsed // 3600)
        m = int((elapsed % 3600) // 60)
        s = int(elapsed % 60)
        nframes = self._rec_worker.written if self._rec_worker else 0
        if h > 0:
            text = f"\u23fa  {h:d}:{m:02d}:{s:02d}  ({nframes} snapshots)"
        else:
            text = f"\u23fa  {m:02d}:{s:02d}  ({nframes} snapshots)"
        self._lbl_rec_elapsed.setText(text)

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _push_snapshot_to_rec(self, snapshot: AlignedSnapshot):
        """Push une copie du snapshot dans la queue d'enregistrement.

        Déduplication côté producteur : on ignore les snapshots dont le
        reference_ntp n'a pas changé (même instant logique).
        Drop oldest si la queue est pleine (backpressure explicite + log).
        """
        if self._rec_queue is None or not self._recording:
            return
        # Détection crash worker : si le thread est mort, on coupe le flux.
        # IMPORTANT : cette méthode est appelée depuis RecPushThread (non-Qt).
        # On ne touche PAS aux widgets Qt depuis ici — on se contente de
        # mettre _recording=False ; le prochain tick de _timer_rec (Qt thread)
        # détecte que le worker est mort et remet l'UI à jour.
        if self._rec_worker is not None and not self._rec_worker.is_alive():
            self._recording = False
            print("[CamSync] RecordingWorker died unexpectedly — recording stopped.", flush=True)
            self._rec_worker = None
            self._rec_queue  = None
            return
        if snapshot.reference_ntp <= self._last_snap_ntp:
            return
        self._last_snap_ntp = snapshot.reference_ntp

        # Copie des frames numpy pour isolation mémoire inter-thread.
        # NOTE: Frames are intentionally copied before queue push
        # to guarantee snapshot immutability across threads.
        # Memory cost (~200 MB peak) preferred over shared-buffer corruption.
        copied_frames: dict = {}
        for cid, tf in snapshot.frames.items():
            if tf is not None:
                copied_frames[cid] = TimestampedFrame(
                    frame=tf.frame.copy(),
                    ntp_timestamp=tf.ntp_timestamp,
                    capture_monotonic=tf.capture_monotonic,
                    sequence_number=tf.sequence_number,
                    timestamp_source=tf.timestamp_source,
                    confidence=tf.confidence,
                    stream_pts_ms=tf.stream_pts_ms,
                )
            else:
                copied_frames[cid] = None

        copied = AlignedSnapshot(
            reference_ntp=snapshot.reference_ntp,
            frames=copied_frames,
            residuals_ms=dict(snapshot.residuals_ms),
        )

        if self._rec_queue.full():
            try:
                self._rec_queue.get_nowait()
                if self._rec_worker is not None:
                    self._rec_worker.dropped += 1
                    print(
                        f"[CamSync] Snapshot dropped (queue full) — "
                        f"total: {self._rec_worker.dropped}", flush=True)
            except queue.Empty:
                pass
        try:
            self._rec_queue.put_nowait(copied)
        except queue.Full:
            pass

    def _refresh_video(self):
        if self._aligner.is_enabled:
            snapshot = self._aligner.get_aligned_frames()
            for cam in self._cameras:
                tf = snapshot.frames.get(cam.camera_id)
                self._cam_widgets[cam.camera_id].refresh(
                    tf,
                    residual_ms=snapshot.residuals_ms.get(cam.camera_id))
        else:
            for cam in self._cameras:
                tf = cam.latest_frame()
                self._cam_widgets[cam.camera_id].refresh(tf)

    def _rec_push_loop(self):
        """Boucle d'enregistrement dans un thread Python dédié (~30 fps).

        Tourne en continu, indépendamment du thread Qt. Lorsque
        `self._recording` est True, interroge l'aligner et pousse les
        snapshots à la queue du worker.

        Toutes les opérations effectuées ici sont thread-safe :
          - `self._aligner.get_aligned_frames()` : lecture des buffers
            internes des caméras (locks internes).
          - `self._push_snapshot_to_rec()` : push dans une `queue.Queue`
            (thread-safe par construction) + lecture/écriture d'attributs
            simples (`_last_snap_ntp`, compteurs).
        AUCUN appel à des objets Qt depuis ce thread.
        """
        period = 0.033   # ~30 Hz
        while not self._rec_push_stop.is_set():
            t0 = time.monotonic()
            if self._recording:
                try:
                    snapshot = self._aligner.get_aligned_frames()
                    self._push_snapshot_to_rec(snapshot)
                except Exception as e:
                    print(f"[RecPushThread] Erreur : {e}", flush=True)
            # Sleep le reste de la période, en restant interruptible.
            elapsed = time.monotonic() - t0
            remaining = period - elapsed
            if remaining > 0:
                self._rec_push_stop.wait(remaining)

    def _refresh_metrics(self):
        # Un seul appel get_aligned_frames() par tick, réutilisé pour les
        # métriques, les cartes et le graphique.
        snap: AlignedSnapshot | None = None
        aligned_ts = None
        if self._aligner.is_enabled:
            snap = self._aligner.get_aligned_frames()
            offsets = self._aligner.get_offsets()
            aligned_ts = {}
            for cid, tf in snap.frames.items():
                if tf is not None:
                    # Timestamp corrigé = timestamp brut + offset correctif
                    aligned_ts[cid] = tf.ntp_timestamp + offsets.get(cid, 0.0)

        metrics = self._metrics_engine.compute_all_pairs(aligned_ts)

        if metrics:
            max_off    = max(abs(m.offset_ms)   for m in metrics)
            avg_jitter = (sum(m.jitter_ms        for m in metrics)
                          / len(metrics))
            avg_drift  = (sum(m.drift_ms_per_sec for m in metrics)
                          / len(metrics))
        else:
            max_off = avg_jitter = avg_drift = 0.0

        # MAX OFFSET affiché = médiane glissante du pic instantané. Absorbe
        # les pics ponctuels du jitter RTCP Insta pour donner une valeur
        # cohérente avec la synchro visuelle perçue (Flash Calib).
        self._max_off_window.append(max_off)
        max_off_display = float(np.median(self._max_off_window))

        drift_per_min = avg_drift * 60.0
        self._card_offset.set_value(max_off_display,
                                    color=_offset_color(max_off_display))
        self._card_jitter.set_value(avg_jitter,   color=_offset_color(avg_jitter))
        self._card_drift.set_value(drift_per_min, fmt="{:+.2f}")

        self._aligner.check_drift_recalib(metrics)

        # ── Mise à jour graphique : désynchronisation visuelle réelle ────────
        # On trace le « temps visuel » de chaque frame affichée vs la référence :
        #   temps_visuel = ntp_frame + (display_delay + calib_delay)
        #   delta_ms     = (temps_visuel_cam - temps_visuel_ref) × 1000
        # Pourquoi ajouter la calibration ? En mode aligné, frame_at() choisit
        # volontairement des frames à des ntp décalés de -calib_delay (c'est la
        # latence pipeline qu'on compense EXPRÈS). Sans le +calib_delay le graphe
        # afficherait ce biais (≈ 340 ms pour les Insta) au lieu de la désync
        # résiduelle. Le +calib_delay annule ce biais → il ne reste que l'erreur
        # réelle (≈ 0 si bien synchro), cohérente avec le Visual spread du Flash
        # Calib. Mode OFF : latest_frame() applique déjà calib_delay en interne,
        # donc la même formule reste valable.
        ref_cam = self._cameras[0]
        if self._aligner.is_enabled and snap is not None:
            ref_tf = snap.frames.get(ref_cam.camera_id)
        else:
            snap = None
            ref_tf = ref_cam.latest_frame()

        if ref_tf is not None:
            if self._plot_t0 == 0.0:
                self._plot_t0 = time.monotonic()
            t_sec = time.monotonic() - self._plot_t0

            ref_eff = (getattr(ref_cam, "display_delay_sec", 0.0)
                       + getattr(ref_cam, "calib_delay_sec", 0.0))
            ref_vis = ref_tf.ntp_timestamp + ref_eff

            # Graphe 2 (résidu temporel) : suit l'évolution du timestamp
            # affiché de chaque flux par rapport au temps de référence calculé.
            #  - Mode ON  : residuals_ms[cam] = ntp_frame - target_ts, soit
            #    l'écart entre la frame retenue et la cible voulue par
            #    l'aligneur (≈ 0 si le buffer contient la bonne frame).
            #  - Mode OFF : pas de cible → écart des ntp_timestamp bruts des
            #    frames les plus récentes (réalité avant alignement).
            ref_residual = 0.0
            if self._aligner.is_enabled and snap is not None:
                ref_residual = snap.residuals_ms.get(ref_cam.camera_id, 0.0)

            for cam in self._cameras[1:]:
                cid = cam.camera_id
                # ── Graphe 1 : désync visuelle (frame réellement affichée) ──
                if self._aligner.is_enabled and snap is not None:
                    cam_tf = snap.frames.get(cid)
                else:
                    cam_tf = cam.latest_frame()

                if cam_tf is not None:
                    cam_eff = (getattr(cam, "display_delay_sec", 0.0)
                               + getattr(cam, "calib_delay_sec", 0.0))
                    cam_vis = cam_tf.ntp_timestamp + cam_eff
                    delta_ms = (cam_vis - ref_vis) * 1000.0
                    self._plot_history[cid].append(delta_ms)
                else:
                    self._plot_history[cid].append(float("nan"))

                # ── Graphe 2 : résidu vs temps de référence calculé ─────────
                if self._aligner.is_enabled and snap is not None:
                    cam_residual = snap.residuals_ms.get(cid)
                    if cam_residual is not None:
                        self._plot_history_raw[cid].append(
                            cam_residual - ref_residual)
                    else:
                        self._plot_history_raw[cid].append(float("nan"))
                else:
                    cam_raw = cam.last_ntp_timestamp
                    ref_raw = ref_cam.last_ntp_timestamp
                    if cam_raw and ref_raw:
                        self._plot_history_raw[cid].append(
                            (cam_raw - ref_raw) * 1000.0)
                    else:
                        self._plot_history_raw[cid].append(float("nan"))

            self._plot_time_history.append(t_sec)

            # Marker vertical si l'état d'alignement a changé
            if self._aligner.is_enabled != self._last_align_state:
                color = '#3fb950' if self._aligner.is_enabled else '#f0c040'
                for w in (self._plot_widget, self._plot_widget_raw):
                    marker = pg.InfiniteLine(
                        angle=90, pos=t_sec,
                        pen=pg.mkPen(color, width=1, style=Qt.DotLine))
                    w.addItem(marker)
                    self._align_markers.append((t_sec, w, marker))
                self._last_align_state = self._aligner.is_enabled

            x = list(self._plot_time_history)
            for cid, curve in self._plot_curves.items():
                y = list(self._plot_history[cid])
                if len(x) == len(y):
                    curve.setData(x, y)
            for cid, curve in self._plot_curves_raw.items():
                y = list(self._plot_history_raw[cid])
                if len(x) == len(y):
                    curve.setData(x, y)

            # Purge markers hors de la fenêtre de 300 points
            if x:
                x_min = x[0]
                to_remove = [
                    (t, w, m) for (t, w, m) in self._align_markers if t < x_min]
                for t, w, m in to_remove:
                    w.removeItem(m)
                    self._align_markers.remove((t, w, m))

    # ── Flash calibration ────────────────────────────────────────────────────

    def _apply_flash_calibration(self, result) -> dict:
        """Applique la médiane des calibrations idéales absolues.

        Pour chaque flash on calcule la calibration IDÉALE ABSOLUE :
            ideal_i = existing_calib_i + (max_visual_off - visual_off_i)
                    = max_visual - capture_monotonic_i
        Cette valeur est indépendante de la calibration courante (c'est une
        propriété absolue du pipeline au moment du flash).

        On accumule ces idéales dans une fenêtre glissante (max 5) et on
        applique la MÉDIANE. Contrairement aux corrections incrémentales
        (qui s'accumulent et divergent quand le pipeline varie), la médiane
        converge vers l'offset pipeline moyen après 3-5 mesures.
        L'historique n'est PAS remis à zéro après application : les prochains
        flashs affineront la médiane sans perdre l'historique.
        """
        offs = result.relative_offsets_ms
        if not offs:
            return {}

        cam_by_id = {c.camera_id: c for c in self._cameras}
        max_off = max(offs.values())

        # Calibration idéale absolue pour ce flash :
        #   ideal_i = existing_i + (max_off - off_i)  [équiv. à max_visual − capture_mono_i]
        ideal_this: dict[str, float] = {}
        for cid, off in offs.items():
            cam = cam_by_id.get(cid)
            if cam is None or not hasattr(cam, "set_calibration_delay"):
                continue
            existing_ms = getattr(cam, "calib_delay_sec", 0.0) * 1000.0
            ideal_this[cid] = max(0.0, existing_ms + (max_off - off))

        # ── Gardes de rejet : on ne pollue pas l'historique avec une mesure
        # manifestement fausse (flash mal détecté, RTCP pas encore stable,
        # ou skip-to-IDR qui a décalé les timestamps Insta d'un GOP entier).
        #
        # Règle 1 : spread > 150 ms à partir du 2e flash.
        #   Après la première calibration provisoire, le spread devrait être
        #   <150 ms si le pipeline est stable. Un spread > 150 ms signifie
        #   qu'un ou plusieurs timestamps sont erronés → on refuse la mesure.
        #
        # Règle 2 : outlier > 200 ms vs médiane courante (à partir du 3e flash).
        #   Si une caméra saute soudainement de ±200 ms par rapport à la
        #   médiane déjà calculée, c'est probablement un mauvais detect.
        _SPREAD_REJECT_MS  = 150.0   # règle 1 : spread trop grand
        _OUTLIER_REJECT_MS = 200.0   # règle 2 : écart vs médiane

        n_hist_before = len(self._flash_history)

        if n_hist_before >= 1 and result.spread_ms > _SPREAD_REJECT_MS:
            print(
                f"[FlashCalib] Measurement REJECTED — spread={result.spread_ms:.0f}ms "
                f"> {_SPREAD_REJECT_MS:.0f}ms — history and calibration unchanged.",
                flush=True,
            )
            return {"_rejected": True, "_reason": f"spread {result.spread_ms:.0f}ms > {_SPREAD_REJECT_MS:.0f}ms"}

        if n_hist_before >= 2:
            import statistics as _stat_tmp
            _running: dict[str, float] = {}
            _all_prev = set()
            for h in self._flash_history:
                _all_prev.update(h.keys())
            for _cid in _all_prev:
                _vals = sorted(h[_cid] for h in self._flash_history if _cid in h)
                if _vals:
                    _running[_cid] = _vals[len(_vals) // 2]
            _outliers = []
            for _cid, _new_val in ideal_this.items():
                _med = _running.get(_cid)
                if _med is not None and abs(_new_val - _med) > _OUTLIER_REJECT_MS:
                    _name = self._cam_name_by_id.get(_cid, _cid)
                    _outliers.append(
                        f"{_name}: new={_new_val:.0f}ms med={_med:.0f}ms "
                        f"Δ={abs(_new_val - _med):.0f}ms"
                    )
            if _outliers:
                print(
                    f"[FlashCalib] Measurement REJECTED — outlier(s): "
                    f"{', '.join(_outliers)} — history and calibration unchanged.",
                    flush=True,
                )
                return {"_rejected": True, "_reason": "outlier: " + "; ".join(_outliers)}

        # Fenêtre glissante de 5 idéales absolues (pas de clear après apply)
        self._flash_history.append(ideal_this)
        if len(self._flash_history) > 5:
            self._flash_history.pop(0)
        n_hist = len(self._flash_history)

        # Médiane par caméra + écart-type (indicateur de stabilité)
        all_cids = set()
        for h in self._flash_history:
            all_cids.update(h.keys())

        import statistics as _statistics
        median_ideal: dict[str, float] = {}
        per_cam_vals: dict[str, list] = {}
        stds: dict[str, float] = {}
        for cid in all_cids:
            vals = sorted(h[cid] for h in self._flash_history if cid in h)
            per_cam_vals[cid] = vals
            median_ideal[cid] = vals[len(vals) // 2]
            stds[cid] = _statistics.stdev(vals) if len(vals) >= 2 else 0.0

        if not median_ideal:
            return {}

        # σmax n'est significatif qu'avec ≥ 2 mesures (stdev(1 valeur) = 0 par convention
        # mais ne reflète pas la stabilité réelle du pipeline).
        max_std = max(stds.values()) if stds else 0.0
        if n_hist < 2:
            stability = f"1 measurement — run {5 - n_hist} more flash(s) to stabilize median"
        elif max_std < 40:
            stability = "✓ stable"
        elif max_std < 80:
            stability = "⚠ unstable"
        else:
            stability = "✗ very unstable"

        # Application de la médiane
        applied: dict[str, float] = {}
        for cid, ideal_ms in median_ideal.items():
            cam = cam_by_id.get(cid)
            if cam is None or not hasattr(cam, "set_calibration_delay"):
                continue
            cam.set_calibration_delay(max(0.0, ideal_ms) / 1000.0)
            applied[cid] = ideal_ms

        if n_hist >= 2:
            label = f"median {n_hist} measurement(s) · σmax={max_std:.0f}ms · {stability}"
        else:
            label = f"provisional (1 measurement) — {stability}"
        print(f"[FlashCalib] Calibration appliquée ({label}) :", flush=True)
        for cid, d in sorted(applied.items(), key=lambda x: -x[1]):
            name = self._cam_name_by_id.get(cid, cid)
            hist_str = ", ".join(f"{v:.0f}" for v in per_cam_vals.get(cid, []))
            print(f"  {name:25s} calib_delay = {d:6.0f} ms  "
                  f"(ideals: [{hist_str}])", flush=True)
        self._flash_reject_streak = 0
        return applied

    def _on_flash_calib_reset(self):
        """Remet tous les délais de calibration flash à 0 et vide l'historique."""
        self._flash_history.clear()
        self._flash_reject_streak = 0
        for cam in self._cameras:
            if hasattr(cam, "set_calibration_delay"):
                cam.set_calibration_delay(0.0)
        print("[FlashCalib] Calibration RESET — all calib_delay set to 0 ms, "
              "history cleared.", flush=True)
        self._lbl_val_icon.setText("↺")
        self._lbl_val_icon.setStyleSheet(_style("#888888", 16, bold=True))
        self._lbl_val_title.setText("Calibration reset — all delays set to 0 ms")
        self._lbl_val_detail.setText("Run Flash Calib again to recalibrate.")
        self._lbl_val_spread.setText("")
        self._validation_bar.setVisible(True)
        QTimer.singleShot(8000, lambda: self._validation_bar.setVisible(False))

    def _on_flash_calib(self):
        """Démarre une session de mesure d'écart visuel par flash lumineux."""
        print("[FlashCalib] Button clicked.", flush=True)
        if self._flash_calib.is_running:
            print("[FlashCalib] Session in progress → cancel.", flush=True)
            self._flash_calib.cancel()
            self._btn_flash.setText("\u26a1  Flash Calib")
            self._validation_bar.setVisible(False)
            return

        # Avertir si le modèle RTCP n'est pas encore stabilisé sur une Insta.
        # La fenêtre glissante de 20 SRs (~100 s) doit être pleine pour que
        # la médiane RTCP converge ; avant cela, les timestamps Insta dérivent.
        _RTCP_MIN_SR = 20
        rtcp_warn = []
        for _cam in self._cameras:
            if hasattr(_cam, 'sr_count') and hasattr(_cam, 'name'):
                _sc = _cam.sr_count
                if 0 < _sc < _RTCP_MIN_SR:
                    rtcp_warn.append(f"{_cam.name}: {_sc}/{_RTCP_MIN_SR} SRs")
        if rtcp_warn:
            print(f"[FlashCalib] WARNING: RTCP not yet stable — "
                  f"{', '.join(rtcp_warn)} — wait ~{_RTCP_MIN_SR * 5}s",
                  flush=True)
            self._lbl_val_icon.setText("⚠")
            self._lbl_val_icon.setStyleSheet(_style(_GOLD, 16, bold=True))
            self._lbl_val_title.setText(
                "RTCP not yet stable — timestamps may be unreliable")
            self._lbl_val_detail.setText(", ".join(rtcp_warn))
            self._lbl_val_spread.setText("")
            self._validation_bar.setVisible(True)
            self._validation_bar.raise_()
            # On laisse quand même démarrer : l'utilisateur est prévenu.

        ok = self._flash_calib.start()
        print(f"[FlashCalib] start() → {ok}", flush=True)
        if not ok:
            self._lbl_val_icon.setText("\u2716")
            self._lbl_val_icon.setStyleSheet(_style(_RED, 16, bold=True))
            self._lbl_val_title.setText("Flash Calib: no camera connected")
            self._lbl_val_detail.setText("")
            self._lbl_val_spread.setText("")
            self._validation_bar.setVisible(True)
            self._validation_bar.raise_()
            QTimer.singleShot(5000, lambda: self._validation_bar.setVisible(False))
            return

        self._btn_flash.setText("\u26a1  Cancel")
        self._lbl_val_icon.setText("\u26a1")
        self._lbl_val_icon.setStyleSheet(_style(_GOLD, 16, bold=True))
        self._lbl_val_title.setText("Flash Calibration in progress… Turn the light off or on in a single action!")
        self._lbl_val_detail.setText("0/0 cameras triggered · 0.0s")
        self._lbl_val_spread.setText("")
        self._validation_bar.setVisible(True)
        self._validation_bar.raise_()

    def _refresh_flash_calib(self):
        """Tick périodique de la calibration flash (no-op si inactive)."""
        if not self._flash_calib.is_running:
            return

        try:
            finished = self._flash_calib.tick()
        except Exception:
            import traceback
            print("[FlashCalib] EXCEPTION dans tick():", flush=True)
            traceback.print_exc()
            self._flash_calib.cancel()
            self._btn_flash.setText("\u26a1  Flash Calib")
            return
        det, total, elapsed = self._flash_calib.progress
        if not finished:
            self._lbl_val_detail.setText(
                f"{det}/{total} cameras triggered · {elapsed:.1f}s")
            return

        # ── Affichage du résultat final ────────────────────────────────
        self._btn_flash.setText("\u26a1  Flash Calib")
        result = self._flash_calib.result
        if result is None:
            return

        n_det = len(result.detections)
        if n_det < 2:
            self._lbl_val_icon.setText("\u2716")
            self._lbl_val_icon.setStyleSheet(_style(_RED, 16, bold=True))
            self._lbl_val_title.setText(
                f"Flash Calib: {n_det} detection(s) — flash too weak or out of frame")
            missing = ", ".join(result.not_detected) or "—"
            self._lbl_val_detail.setText(f"Not detected: {missing}")
            self._lbl_val_spread.setText("")
            self._validation_bar.setVisible(True)
            QTimer.singleShot(15000,
                              lambda: self._validation_bar.setVisible(False))
            QMessageBox.warning(
                self, "Flash Calib — Failed",
                f"Only {n_det} camera(s) detected the brightness "
                f"change.\n\nNot detected: {missing}\n\n"
                "→ The contrast may have been too low, or the transition "
                "too gradual. Try switching the light off/on more sharply.")
            return

        spread = result.spread_ms

        # Seuils fixes basés sur la perception visuelle et la limite physique
        # de cette installation (jitter d'encodage Insta360 ≈ ±33ms/frame).
        # On n'utilise PAS actual_fps (fps de décodage) qui peut tomber à
        # 3.9 fps (après skips) et gonflerait le seuil OK à 256ms.
        # Seuils de référence :
        #   OK        < 80 ms   ≈ 2 frames Insta @ 30fps — pas de désync perçu
        #   ACCEPTABLE < 150 ms  ≈ 1 image de retard perçu sur clap rapide
        #   HIGH       ≥ 150 ms  → recalibration nécessaire
        ok_thr  = 80.0
        acc_thr = 150.0

        if spread < ok_thr:
            color, verdict = _GREEN, "VISUAL ALIGNMENT OK"
        elif spread < acc_thr:
            color, verdict = _ORANGE, "VISUAL OFFSET ACCEPTABLE"
        else:
            color, verdict = _RED, "VISUAL OFFSET HIGH"

        self._lbl_val_icon.setText("\u26a1")
        self._lbl_val_icon.setStyleSheet(_style(color, 16, bold=True))
        self._lbl_val_title.setText(
            f"{verdict}  ({n_det}/{n_det + len(result.not_detected)} cameras)")

        # ── APPLICATION AUTOMATIQUE de la calibration ─────────────────────
        # L'offset flash mesure l'écart visuel réel (intrinsèque) de chaque
        # caméra. Comme cet offset n'est pas reproductible d'une session à
        # l'autre (ancrage NDI/RTCP), on l'applique à chaud : chaque caméra
        # reçoit un délai = (offset_max - offset_cam) pour que la plus en
        # avance attende les autres. Résultat : spread visuel → ~0.
        applied = self._apply_flash_calibration(result)

        # ── Mesure rejetée (flash mal détecté, outlier timestamps) ────────
        if applied.get("_rejected"):
            self._flash_reject_streak += 1
            reason = applied.get("_reason", "unknown")
            reset_hint = self._flash_reject_streak >= 3
            self._lbl_val_icon.setText("⊘")
            self._lbl_val_icon.setStyleSheet(_style(_RED, 16, bold=True))
            if reset_hint:
                self._lbl_val_title.setText(
                    "Repeated rejections — Reset Calib recommended")
                self._lbl_val_detail.setText(
                    f"{reason} · {self._flash_reject_streak} rejected in a row")
            else:
                self._lbl_val_title.setText(
                    "Measurement REJECTED — calibration unchanged")
                self._lbl_val_detail.setText(reason)
            self._lbl_val_spread.setText(f"Visual spread: {spread:.0f} ms")
            self._lbl_val_spread.setStyleSheet(_style(_RED, 14, bold=True, mono=True))
            self._validation_bar.setVisible(True)
            QTimer.singleShot(20000,
                              lambda: self._validation_bar.setVisible(False))
            msg = QMessageBox(self)
            msg.setWindowTitle("Flash Calib — Rejected")
            msg.setIcon(QMessageBox.Warning)
            body = [
                "<b>MEASUREMENT REJECTED</b>",
                "Calibration history and delays are unchanged.",
                "",
                f"Visual spread: <b>{spread:.0f} ms</b>",
                f"Reason: {reason}",
                "",
                "Likely causes: RTCP not yet stable, flash missed,",
                "or Insta skip-to-IDR. Wait a few seconds and retry.",
            ]
            if reset_hint:
                body.extend([
                    "",
                    "<b>Recommended:</b> click Reset Calib, wait until the",
                    "Insta streams are stable, then run Flash Calib again.",
                ])
            msg.setText("<br>".join(body))
            msg.setStandardButtons(QMessageBox.Ok)
            msg.show()
            # Trace console
            print("[FlashCalib] Results:", flush=True)
            for cid, ts in result.detections.items():
                name = self._cam_name_by_id.get(cid, cid)
                off = result.relative_offsets_ms.get(cid, 0.0)
                print(f"  {name:25s} flash @ {ts:.6f}  (+{off:.1f} ms)", flush=True)
            print(f"  Visual spread = {spread:.1f} ms  [REJECTED]", flush=True)
            return


        # Détail : par caméra, offset d'affichage relatif à la plus précoce.
        # L'offset affiché = retard visuel réel de cette caméra vs la référence.
        # Pour réduire l'offset d'une caméra en retard : diminuer son
        # display_delay_ms de la valeur indiquée (ou augmenter celui de la
        # caméra de référence du même montant).
        parts = []
        for cid, off in sorted(result.relative_offsets_ms.items(),
                                key=lambda x: x[1]):
            name = self._cam_name_by_id.get(cid, cid)
            parts.append(f"{name}: +{off:.0f}ms")
        if result.not_detected:
            parts.append("not detected: " + ", ".join(result.not_detected))
        self._lbl_val_detail.setText("   ".join(parts))

        self._lbl_val_spread.setText(f"Visual spread: {spread:.0f} ms")
        self._lbl_val_spread.setStyleSheet(_style(color, 14, bold=True, mono=True))

        self._validation_bar.setVisible(True)
        QTimer.singleShot(30000,
                          lambda: self._validation_bar.setVisible(False))

        # Popup modal pour s'assurer que le verdict est vu
        msg = QMessageBox(self)
        msg.setWindowTitle("Flash Calib — Result")
        icon_kind = (QMessageBox.Information if spread < ok_thr
                     else QMessageBox.Warning if spread < acc_thr
                     else QMessageBox.Critical)
        msg.setIcon(icon_kind)
        body = [f"<b>{verdict}</b>",
                f"Visual spread: <b>{spread:.0f} ms</b>",
                f"Thresholds: OK &lt; {ok_thr:.0f} ms  ·  "
                f"ACCEPTABLE &lt; {acc_thr:.0f} ms",
                f"Detected: {n_det} / {n_det + len(result.not_detected)}",
                "",
                "Per-camera offsets (relative to the earliest):"]
        for cid, off in sorted(result.relative_offsets_ms.items(),
                                key=lambda x: x[1]):
            name = self._cam_name_by_id.get(cid, cid)
            body.append(f"• {name}: +{off:.0f} ms")
        if result.not_detected:
            body.append("")
            body.append("Not detected: " + ", ".join(result.not_detected))
        msg.setText("<br>".join(body))
        msg.setStandardButtons(QMessageBox.Ok)
        msg.show()

        # Trace console pour log/analyse a posteriori
        print("[FlashCalib] Results:", flush=True)
        for cid, ts in result.detections.items():
            name = self._cam_name_by_id.get(cid, cid)
            off = result.relative_offsets_ms.get(cid, 0.0)
            print(f"  {name:25s} flash @ {ts:.6f}  (+{off:.1f} ms)", flush=True)
        print(f"  Visual spread = {spread:.1f} ms", flush=True)
        if result.not_detected:
            print(f"  Not detected: {result.not_detected}", flush=True)

    def _on_record_toggle(self):
        if not self._recording:
            # ── Démarrer l'enregistrement ─────────────────────────────────
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            self._record_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "recordings", f"record_{stamp}")
            os.makedirs(self._record_dir, exist_ok=True)
            print(f"[CamSync] Enregistrement d\u00e9marr\u00e9 \u2192 {self._record_dir}",
                  flush=True)
            self._record_start_mono = time.monotonic()
            self._lbl_rec_elapsed.setText("\u23fa  00:00")
            self._lbl_rec_elapsed.setVisible(True)
            self.setWindowTitle(
                f"Cam_Synchro \u2014 \u23fa REC")
            print("[CamSync] Recording format: MKV (MJPEG) via PyAV — VFR",
                  flush=True)
            rec_paths: dict[str, str] = {}
            csv_paths: dict[str, str] = {}
            for cam in self._cameras:
                safe_name = cam.name.replace(" ", "_").replace("/", "_").replace("#", "N")
                rec_paths[cam.camera_id] = os.path.join(
                    self._record_dir, f"{safe_name}.mkv")
                csv_paths[cam.camera_id] = os.path.join(
                    self._record_dir, f"{safe_name}.csv")

            self._last_snap_ntp = 0.0
            self._rec_queue  = queue.Queue(maxsize=8)
            self._rec_worker = RecordingWorker(
                snapshot_queue=self._rec_queue,
                cameras=self._cameras,
                rec_paths=rec_paths,
                csv_paths=csv_paths,
            )
            self._rec_worker.start()

            # Activer le flag après avoir démarré le worker
            self._recording = True
            self._btn_record.setText("\u23f9  Stop")
            self._btn_record.setStyleSheet(f"""
                QPushButton {{
                    background-color: {_BG3}; color: {_RED};
                    border: 1px solid {_RED}; border-radius: 4px;
                    font-size: 11px; font-weight: bold;
                }}
                QPushButton:hover {{ background-color: {_BG2}; }}
            """)
        else:
            # ── Arrêter l'enregistrement ────────────────────────────────
            # 1. Couper le flux de snapshots vers la queue
            self._recording = False
            elapsed = time.monotonic() - self._record_start_mono

            # 2. Sentinel → flush encodeurs → fermer containers → join
            if self._rec_queue is not None and self._rec_worker is not None:
                self._rec_queue.put(None)
                self._rec_worker.join(timeout=30.0)
                if self._rec_worker.is_alive():
                    print("[CamSync] WARNING: RecordingWorker still active after 30s",
                          flush=True)

            written = self._rec_worker.written if self._rec_worker else 0
            dropped = self._rec_worker.dropped if self._rec_worker else 0
            self._rec_worker = None
            self._rec_queue  = None

            print(f"[CamSync] Recording stopped — files saved to: {self._record_dir}",
                  flush=True)
            print(f"[CamSync] Duration: {elapsed:.1f}s — "
                  f"{written} snapshots written, {dropped} dropped", flush=True)
            summary_parts = []
            fps_real = (written / elapsed) if elapsed > 0 else 0.0
            for cam in self._cameras:
                print(f"[CamSync]   {cam.name:<22}  {written:5d} frames  "
                      f"({fps_real:5.1f} fps avg)", flush=True)
                summary_parts.append(
                    f"{cam.name}: {written} frames ({fps_real:.1f} fps)")

            # Affiche le résumé dans la barre de validation pendant 15 s
            try:
                self._lbl_val_icon.setText("\u23fa")
                self._lbl_val_icon.setStyleSheet(_style(_GREEN, 16, bold=True))
                m = int(elapsed // 60); s = int(elapsed % 60)
                self._lbl_val_title.setText(
                    f"Recording finished  {m:02d}:{s:02d}  ({len(self._cameras)} cameras)")
                self._lbl_val_detail.setText("   ".join(summary_parts))
                self._lbl_val_spread.setText(f"Saved → {self._record_dir}")
                self._validation_bar.setVisible(True)
                QTimer.singleShot(15000,
                                  lambda: self._validation_bar.setVisible(False))
            except Exception:
                pass

            self.setWindowTitle("Cam_Synchro")
            self._lbl_rec_elapsed.setVisible(False)

            self._btn_record.setText("\u23fa  Record")
            self._btn_record.setStyleSheet(f"""
                QPushButton {{
                    background-color: {_RED}; color: white;
                    border: none; border-radius: 4px;
                    font-size: 11px; font-weight: bold;
                }}
                QPushButton:hover {{ background-color: #da3633; }}
                QPushButton:pressed {{ background-color: #b62324; }}
            """)

    def _on_align_toggle(self):
        self._aligner.toggle()
        if self._aligner.is_enabled:
            self._btn_align.setText("\u2713  Aligned")
            self._btn_align.setStyleSheet(f"""
                QPushButton {{
                    background-color: {_BG3}; color: {_GREEN};
                    border: 1px solid {_GREEN}; border-radius: 4px;
                    font-size: 11px; font-weight: bold;
                }}
                QPushButton:hover {{ background-color: {_BG2}; }}
            """)
        else:
            self._btn_align.setText("\u27f3  Align streams")
            self._btn_align.setStyleSheet(f"""
                QPushButton {{
                    background-color: #238636; color: white;
                    border: none; border-radius: 4px;
                    font-size: 11px; font-weight: bold;
                }}
                QPushButton:hover {{ background-color: {_GREEN}; }}
            """)

    # ── Fermeture propre ──────────────────────────────────────────────────────

    def closeEvent(self, event):
        for t in (self._timer_video, self._timer_metrics,
                  self._timer_flash, self._timer_rec):
            t.stop()
        # Arrêt propre du thread de push d'enregistrement.
        try:
            self._rec_push_stop.set()
            if self._rec_push_thread is not None:
                self._rec_push_thread.join(timeout=2.0)
        except Exception:
            pass
        # Fermer le worker d'enregistrement si actif
        if self._recording:
            self._recording = False
            if self._rec_queue is not None and self._rec_worker is not None:
                self._rec_queue.put(None)
                self._rec_worker.join(timeout=10.0)
            self._rec_worker = None
            self._rec_queue  = None
        event.accept()
