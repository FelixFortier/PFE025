"""
ndi_camera_stream.py – Capture vidéo NDI pour les caméras Ottica 4K.

Les caméras Ottica 4K diffusent en NDI et embarquent un timecode en unités
de 100 nanosecondes depuis minuit UTC (selon leur horloge interne) dans
chaque frame vidéo.

Stratégie d'horodatage (ancrage NTP↔NDI) :
  - Le timecode NDI est très précis en relatif (100 ns/tick, horloge matérielle)
    mais potentiellement décalé en absolu (l'Ottica n'a pas de client NTP).
  - L'horloge hôte NTP est juste en absolu mais moins précise en relatif.
  - À la première frame reçue avec un timecode valide, on calcule un
    anchor_offset = host_ntp - tc_sec. Ce offset convertit ensuite
    chaque timecode NDI en timestamp Unix absolu, comparable avec les
    timestamps RTCP des Insta360.

Corrections par rapport au code de référence (timecode.py) :
  1. Ancrage NTP au lieu du calcul midnight (supprime le problème de base
     temporelle entre protocoles et le rollover de minuit).
  2. Valeurs sentinelles : timecode ≤ 0 ou INT64_MAX → fallback NTP hôte.
  3. Extraction BGR correcte depuis BGRX (stride paddé).
  4. Connexion directe par URL ip:port (pas de découverte NDI de 15 s).
"""

import collections
import os
import sys
import threading
import time

import numpy as np

# ── NDI : chemin des bindings ───────────────────────────────────────────────
# Ordre de recherche :
#   1) <dossier du script>/../ndi/  (layout package portable)
#   2) <dossier du script>/         (fichiers copiés à côté)
#   3) chemin développeur original
_script_dir = os.path.dirname(os.path.abspath(__file__))
_ndi_candidates = [
    os.path.join(_script_dir, "..", "ndi"),
    _script_dir,
    r"C:\Users\Client\sync_cameras\src",
]
_NDI_SRC = next((p for p in _ndi_candidates if os.path.isfile(os.path.join(p, "Processing.NDI.Lib.x64.dll"))), None)
if _NDI_SRC is None:
    raise RuntimeError("[NdiCameraStream] Binaires NDI introuvables. Verifiez que Processing.NDI.Lib.x64.dll est present dans ndi\\ ou a cote du script.")
_NDI_SRC = os.path.normpath(_NDI_SRC)
if _NDI_SRC not in sys.path:
    sys.path.append(_NDI_SRC)
os.add_dll_directory(_NDI_SRC)

import NDIlib as ndi

# Initialisation globale NDI (sûr d'appeler plusieurs fois selon le SDK NDI)
if not ndi.initialize():
    raise RuntimeError("[NdiCameraStream] Impossible d'initialiser NDI")

# Verrou global pour sérialiser les connexions NDI (évite la concurrence
# de bande passante quand plusieurs receivers démarrent simultanément via VPN).
_ndi_connect_lock = threading.Lock()

# Réutiliser TimestampedFrame depuis gst_camera_stream (compatibilité API)
from gst_camera_stream import TimestampedFrame

# ── Constantes ──────────────────────────────────────────────────────────────
_TC_100NS = 10_000_000              # ticks 100 ns → secondes : diviser par 10^7
_NDI_TC_SYNTHESIZED = 0x7FFFFFFFFFFFFFFF  # INT64_MAX : NDI synthétise le TC

# Seuil pour distinguer l'epoch FILETIME (1601) de l'epoch Unix (1970).
# En ticks 100 ns, le 1er janvier 2000 en epoch FILETIME ≈ 1.26×10^17.
# Un timecode « depuis minuit » ne dépasse jamais 8.64×10^11 (24 h).
_FILETIME_THRESHOLD = 10**15  # Si tc > 10^15, c'est un epoch FILETIME
_FILETIME_EPOCH_OFFSET = 116444736000000000  # ticks 100 ns entre 1601 et 1970


def _timecode_to_seconds(timecode: int) -> float | None:
    """
    Convertit un timecode NDI brut (ticks 100 ns) en secondes Unix.

    Détecte automatiquement l'epoch :
      - FILETIME (1601) : certaines caméras NDI (Windows) envoient des
        ticks depuis le 1er janvier 1601. On soustrait l'offset FILETIME→Unix.
      - Epoch Unix ou relatif (minuit) : utilisé tel quel (÷ 10^7).

    L'ancrage sur le NTP hôte est effectué dans la boucle de capture.
    Retourne None si le timecode est invalide (sentinelle ou ≤ 0).
    """
    if timecode <= 0 or timecode >= _NDI_TC_SYNTHESIZED:
        return None
    if timecode > _FILETIME_THRESHOLD:
        # Epoch FILETIME (1601) → convertir en secondes Unix
        return (timecode - _FILETIME_EPOCH_OFFSET) / _TC_100NS
    return timecode / _TC_100NS


def _extract_bgr(v) -> np.ndarray | None:
    """
    Extrait une frame BGR depuis un NDI VideoFrameV2 au format BGRX.

    BGRX = 4 octets/pixel (B, G, R, padding).
    Le stride peut inclure du padding de fin de ligne.
    """
    try:
        h, w = v.yres, v.xres
        if h <= 0 or w <= 0:
            return None
        stride = v.line_stride_in_bytes or (w * 4)
        raw = np.frombuffer(v.data, dtype=np.uint8)
        expected = stride * h
        if len(raw) < expected:
            # Buffer plus court qu'annoncé : on recalcule un stride à partir
            # de la taille effective. S'il est insuffisant pour contenir w
            # pixels par ligne (4 octets), on abandonne plutôt que de
            # produire une image corrompue.
            stride = len(raw) // h
            if stride < w * 4:
                return None
        row_px = stride // 4
        if row_px < w:
            return None
        frame_4ch = raw[: stride * h].reshape(h, row_px, 4)
        return frame_4ch[:, :w, :3].copy()  # BGR (discard X channel)
    except Exception:
        return None


class NdiCameraStream:
    """
    Capture vidéo NDI pour les caméras Ottica 4K.

    Interface publique identique à GstCameraStream.
    """

    def __init__(
        self,
        camera_id: str,
        name: str,
        model: str,
        ndi_url: str,           # Format « ip:port », ex : « 10.180.145.13:5961 »
        ntp_clock,
        buffer_duration: float = 5.0,
        target_fps: int = 30,
        ndi_bandwidth: str = "lowest",
        display_delay_ms: float = 0.0,
        use_synthetic_clock: bool = False,
    ):
        self.camera_id = camera_id
        self.name = name
        self.model = model
        self.ndi_url = ndi_url
        self._clock = ntp_clock
        self._target_fps = target_fps
        self._ndi_bandwidth = (ndi_bandwidth or "lowest").lower()
        self._logged_resolution = False
        # Décalage appliqué à l'affichage (latest_frame) pour compenser la
        # latence de capture des flux RTSP (timestamp Insta360 inclut le
        # pipeline encode/transport/decode ~400 ms).
        self._display_delay_sec = max(0.0, float(display_delay_ms) / 1000.0)
        # Délai dynamique additionnel, piloté en continu par DynamicAligner
        # (référence = caméra la plus lente du moment). Indépendant du
        # display_delay statique ci-dessus, qui ne sert plus que de biais
        # résiduel manuel éventuel.
        self._dynamic_delay_sec = 0.0
        # Délai de CALIBRATION mesuré par le flash (Flash Calib) et appliqué
        # à chaud. Recalculé à chaque session car l'offset intrinsèque
        # (ancrage NDI/RTCP) n'est pas reproductible d'une session à l'autre.
        self._calib_delay_sec = 0.0

        bw_map = {
            "lowest": ndi.RECV_BANDWIDTH_LOWEST,
            "highest": ndi.RECV_BANDWIDTH_HIGHEST,
            "metadata_only": getattr(
                ndi, "RECV_BANDWIDTH_METADATA_ONLY", ndi.RECV_BANDWIDTH_LOWEST
            ),
        }
        self._ndi_bandwidth_value = bw_map.get(
            self._ndi_bandwidth, ndi.RECV_BANDWIDTH_LOWEST
        )

        max_frames = int(buffer_duration * target_fps)
        self._buffer: collections.deque[TimestampedFrame] = collections.deque(
            maxlen=max_frames
        )
        self._lock = threading.Lock()

        self._running = False
        self._connected = False
        self._thread: threading.Thread | None = None
        self._seq = 0
        self._actual_fps: float = 0.0
        self._last_timestamp: float = 0.0
        self._timecode_count: int = 0   # frames avec timecode NDI valide
        self._ts_source: str = "host_ntp"
        self._fps_start: float = 0.0
        self._fps_count: int = 0

        # Calibration NDI → NTP : à la première frame avec timecode valide,
        # on calcule l'écart entre le NTP brut dérivé du timecode NDI et
        # l'horloge hôte NTP. Cet offset est appliqué à tous les timecodes
        # suivants, de sorte que :
        #   - le temps absolu est ancré sur le NTP hôte (même base que les Insta360)
        #   - la précision sub-ms entre frames vient du timecode NDI (100 ns)
        # anchor_offset = host_ntp - tc_sec à la première frame valide.
        # Permet de convertir tout timecode NDI en NTP absolu :
        #   ntp = tc_sec + anchor_offset
        # Recalibré à chaque reconnexion.
        self._anchor_offset: float | None = None

    # ── API publique ────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_capture, daemon=True, name=f"NDI-{self.name}"
        )
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=8)
            self._thread = None
        self._connected = False

    def latest_frame(self) -> TimestampedFrame | None:
        with self._lock:
            if not self._buffer:
                return None
            effective_delay = (
                self._display_delay_sec
                + self._dynamic_delay_sec
                + self._calib_delay_sec
            )
            if effective_delay <= 0.0:
                return self._buffer[-1]
            target = self._buffer[-1].capture_monotonic - effective_delay
            best = None
            best_diff = float("inf")
            for tf in self._buffer:
                diff = abs(tf.capture_monotonic - target)
                if diff < best_diff:
                    best_diff = diff
                    best = tf
            return best

    def frame_at(self, target_ntp: float, tolerance: float = 0.1) -> TimestampedFrame | None:
        with self._lock:
            best = None
            best_diff = float("inf")
            for tf in self._buffer:
                diff = abs(tf.ntp_timestamp - target_ntp)
                if diff < best_diff:
                    best_diff = diff
                    best = tf
            if best and best_diff <= tolerance:
                return best
            return None

    def get_buffer_snapshot(self) -> list[TimestampedFrame]:
        with self._lock:
            return list(self._buffer)

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def actual_fps(self) -> float:
        return self._actual_fps

    @property
    def last_ntp_timestamp(self) -> float:
        return self._last_timestamp

    @property
    def display_delay_sec(self) -> float:
        return self._display_delay_sec

    @property
    def dynamic_delay_sec(self) -> float:
        """Délai dynamique courant (sec) imposé par DynamicAligner."""
        return self._dynamic_delay_sec

    @property
    def effective_delay_sec(self) -> float:
        """Délai total réellement appliqué à latest_frame (sec)."""
        return (self._display_delay_sec + self._dynamic_delay_sec
                + self._calib_delay_sec)

    def set_dynamic_delay(self, sec: float) -> None:
        """Définit le délai dynamique (sec, borné ≥ 0). Thread-safe."""
        with self._lock:
            self._dynamic_delay_sec = max(0.0, float(sec))

    @property
    def calib_delay_sec(self) -> float:
        """Délai de calibration courant (sec) mesuré par Flash Calib."""
        return self._calib_delay_sec

    def set_calibration_delay(self, sec: float) -> None:
        """Définit le délai de calibration (sec, borné ≥ 0). Thread-safe."""
        with self._lock:
            self._calib_delay_sec = max(0.0, float(sec))

    @property
    def sr_count(self) -> int:
        """Nombre de frames avec timecode NDI valide (alias pour compatibilité API)."""
        return self._timecode_count

    @property
    def timestamp_source(self) -> str:
        with self._lock:
            return self._ts_source

    # ── Capture NDI ─────────────────────────────────────────────────────────

    def _run_capture(self):
        """Thread de capture NDI avec reconnexion automatique."""
        while self._running:
            recv = None
            try:
                recv_cfg = ndi.RecvCreateV3()
                recv_cfg.color_format = ndi.RECV_COLOR_FORMAT_BGRX_BGRA
                recv_cfg.bandwidth = self._ndi_bandwidth_value
                recv_cfg.allow_video_fields = False
                recv = ndi.recv_create_v3(recv_cfg)
                if recv is None:
                    raise RuntimeError("recv_create_v3 a retourné None")

                self._capture_loop(recv)

            except Exception as e:
                print(f"[{self.name}] NDI erreur : {e}")
            finally:
                if recv is not None:
                    try:
                        ndi.recv_connect(recv, None)  # déconnexion propre
                        ndi.recv_destroy(recv)
                    except Exception:
                        pass
                self._connected = False

            if self._running:
                time.sleep(3)

    def _capture_loop(self, recv):
        """Boucle de réception NDI pour une connexion ouverte."""
        # Sérialiser les connexions NDI pour éviter que 2 receivers
        # se connectent en même temps et saturent le VPN.
        with _ndi_connect_lock:
            src = ndi.Source()
            src.url_address = self.ndi_url
            print(
                f"[{self.name}] NDI recv_connect → {self.ndi_url} "
                f"(bandwidth={self._ndi_bandwidth})",
                flush=True,
            )
            ndi.recv_connect(recv, src)
            time.sleep(2)       # Laisser la connexion s'établir (VPN = latence)
            # Attendre les premières données avant de libérer le verrou
            for _ in range(10):
                t, v, a, m = ndi.recv_capture_v3(recv, 2000)
                if t == ndi.FRAME_TYPE_VIDEO:
                    ndi.recv_free_video_v2(recv, v)
                    print(f"[{self.name}] First NDI frame received", flush=True)
                    break
                elif t == ndi.FRAME_TYPE_AUDIO:
                    ndi.recv_free_audio_v3(recv, a)
                elif t == ndi.FRAME_TYPE_METADATA:
                    ndi.recv_free_metadata(recv, m)

        self._connected = True
        self._anchor_offset = None   # recalibrer à chaque reconnexion
        no_data_streak = 0

        while self._running:
            t, v, a, m = ndi.recv_capture_v3(recv, 1000)

            if t == ndi.FRAME_TYPE_VIDEO:
                no_data_streak = 0
                capture_mono = time.monotonic()
                now_unix = self._clock.now()

                tc_sec = _timecode_to_seconds(v.timecode)
                if tc_sec is not None:
                    # Ancrage NDI → NTP par MINIMUM ROULANT.
                    # À chaque frame, candidate_offset = now_unix - tc_sec
                    # inclut la latence de pipeline NDI de CETTE frame.
                    # Le minimum observé sur une fenêtre glissante
                    # approche le vrai offset horloge (au pire une frame
                    # a traversé le pipeline en un temps quasi nul). Cela
                    # supprime le biais +250ms introduit par la bufferisation
                    # du SDK NDI à la première frame après recv_connect.
                    candidate = now_unix - tc_sec
                    if self._anchor_offset is None:
                        self._anchor_offset = candidate
                        print(f"[{self.name}] NDI→NTP initial anchor: "
                              f"offset={self._anchor_offset:.3f}s "
                              f"(tc={tc_sec:.3f}s since midnight cam)",
                              flush=True)
                    elif candidate < self._anchor_offset:
                        # Nouvelle latence plus faible → on resserre l'ancre.
                        improvement = self._anchor_offset - candidate
                        self._anchor_offset = candidate
                        if improvement > 0.050:
                            print(f"[{self.name}] NDI anchor tightened "
                                  f"by {improvement*1000:.0f}ms → "
                                  f"offset={self._anchor_offset:.3f}s",
                                  flush=True)
                    ntp_ts = tc_sec + self._anchor_offset
                    self._timecode_count += 1
                    with self._lock:
                        self._ts_source = "ndi_timecode"
                    confidence = "high"  # timecode per-frame NDI
                else:
                    ntp_ts = now_unix
                    with self._lock:
                        self._ts_source = "host_ntp"
                    confidence = "low"

                frame = _extract_bgr(v)
                ndi.recv_free_video_v2(recv, v)

                if frame is not None:
                    if not self._logged_resolution:
                        h, w = frame.shape[:2]
                        print(f"[{self.name}] NDI resolution: {w}x{h}", flush=True)
                        self._logged_resolution = True
                    self._seq += 1
                    tf = TimestampedFrame(
                        frame=frame,
                        ntp_timestamp=ntp_ts,
                        capture_monotonic=capture_mono,
                        sequence_number=self._seq,
                        timestamp_source=self._ts_source,
                        confidence=confidence,
                    )
                    with self._lock:
                        self._buffer.append(tf)
                    self._last_timestamp = ntp_ts
                    self._update_fps(capture_mono)

            elif t == ndi.FRAME_TYPE_AUDIO:
                ndi.recv_free_audio_v3(recv, a)

            elif t == ndi.FRAME_TYPE_METADATA:
                ndi.recv_free_metadata(recv, m)

            elif t == ndi.FRAME_TYPE_NONE:
                no_data_streak += 1
                if no_data_streak >= 20:    # 20 × 1 s = 20 s sans données
                    print(f"[{self.name}] Pas de données NDI depuis 20 s, reconnexion…")
                    break

    def _update_fps(self, now: float):
        self._fps_count += 1
        elapsed = now - self._fps_start
        if elapsed >= 1.0:
            self._actual_fps = self._fps_count / elapsed
            self._fps_count = 0
            self._fps_start = now
