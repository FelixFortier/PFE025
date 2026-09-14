"""
rtp_camera_stream.py – Capture vidéo RTSP/RTP brute pour Insta 360 Pro2.

Pourquoi ne pas utiliser OpenCV/FFmpeg ?
----------------------------------------
Le pipeline cv2.VideoCapture(CAP_FFMPEG) accumule plusieurs centaines de
ms de latence (buffering interne ffmpeg) et plafonne le débit décodé à
~7-9 fps sur 1920×1440 H.264 sur Windows, parce que cv2 sérialise
grab/retrieve depuis un thread Python lent.

Ce module remplace ce pipeline par :
  • négociation RTSP manuelle (OPTIONS / DESCRIBE / SETUP / PLAY),
  • transport TCP interleaved (RTP+RTCP encapsulés dans la même
    connexion TCP que les requêtes RTSP — robuste sur lien VPN, aucune
    perte UDP possible),
  • réassemblage H.264 FU-A / STAP-A / single NALU,
  • décodage software via PyAV (libavcodec direct, pas de buffering
    démuxeur),
  • parsing RTCP Sender Reports inline pour obtenir un ancrage NTP
    de la caméra.
"""

from __future__ import annotations

import base64
import collections
import queue
import re
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Optional

import av
import cv2
import numpy as np

from gst_camera_stream import TimestampedFrame  # ré-utilise la même dataclass


# ── Constantes ────────────────────────────────────────────────────────────────

_RTSP_TIMEOUT_S   = 12.0
_NTP_EPOCH_OFFSET = 2_208_988_800       # 1900 → 1970
_RTP_SENTINEL     = 0xFFFFFFFF          # rtp_ts « invalide » du firmware Insta
_RTP_CLOCK_HZ     = 90_000
_TCP_RCVBUF_BYTES = 4 * 1024 * 1024     # buffer TCP large pour absorber les bursts


# ── RTSP minimal client ───────────────────────────────────────────────────────

class _RTSPSession:
    """OPTIONS / DESCRIBE / SETUP / PLAY / TEARDOWN en TCP interleaved.

    Le SETUP demande `interleaved=0-1` : RTP et RTCP sont encapsulés en
    `$-frames` dans la même connexion TCP que les requêtes RTSP. Plus
    robuste sur lien VPN/wifi (aucune perte UDP).
    """

    def __init__(self, url: str, timeout: float = _RTSP_TIMEOUT_S):
        self.url = url
        self._timeout = timeout
        self._cseq = 1
        self._session_id = ""
        self._control_url = url
        self._tcp: Optional[socket.socket] = None
        self._rxbuf = b""               # buffer pour mode interleaved

        m = re.match(r"rtsp://([^/:]+)(?::(\d+))?", url)
        if not m:
            raise ValueError(f"Invalid RTSP URL: {url}")
        self._host = m.group(1)
        self._port = int(m.group(2) or 554)

    def connect(self) -> str:
        """Effectue OPTIONS/DESCRIBE/SETUP/PLAY. Retourne sprop-parameter-sets."""
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.settimeout(self._timeout)
        self._tcp.connect((self._host, self._port))
        # Buffer de réception large (le décodeur peut être plus lent que le réseau)
        try:
            self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF,
                                 _TCP_RCVBUF_BYTES)
        except OSError:
            pass

        self._request("OPTIONS", self.url)
        desc = self._request("DESCRIBE", self.url,
                             extra="Accept: application/sdp\r\n")

        in_video = False
        video_control: Optional[str] = None
        sprop = ""
        for line in desc.splitlines():
            line = line.strip()
            if line.startswith("m=video"):
                in_video = True
                continue
            if line.startswith("m=") and not line.startswith("m=video"):
                in_video = False
                continue
            if in_video and line.startswith("a=control:"):
                ctrl = line[len("a=control:"):].strip()
                if ctrl != "*":
                    video_control = (
                        ctrl if ctrl.startswith("rtsp://")
                        else self.url.rstrip("/") + "/" + ctrl.lstrip("/")
                    )
            if in_video and "sprop-parameter-sets=" in line:
                m2 = re.search(r"sprop-parameter-sets=([^\s;]+)", line)
                if m2:
                    sprop = m2.group(1)

        self._control_url = video_control or self.url

        transport_hdr = "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n"

        setup = self._request("SETUP", self._control_url, extra=transport_hdr)
        if not setup.startswith("RTSP/1.0 200"):
            status = setup.splitlines()[0] if setup else "(empty)"
            raise RuntimeError(f"RTSP SETUP failed: {status}")

        sm = re.search(r"[Ss]ession:\s*([^;\r\n]+)", setup)
        if sm:
            self._session_id = sm.group(1).strip()
        if not self._session_id:
            raise RuntimeError("RTSP SETUP returned no Session ID")

        self._request("PLAY", self.url, extra="Range: npt=0.000-\r\n")
        # En mode TCP, la socket est désormais utilisée en mode binaire.
        self._tcp.settimeout(2.0)
        return sprop

    # ── Mode TCP interleaved : lecture des $-frames ───────────────────────

    def read_interleaved(self) -> Optional[tuple[int, bytes]]:
        """Lit le prochain paquet RTP/RTCP en mode TCP interleaved.

        Retourne (channel, data) ou None sur timeout. Channel 0 = RTP,
        1 = RTCP. Les réponses RTSP éventuelles (keepalive) sont consommées
        et ignorées silencieusement.

        Lève ConnectionError si la socket TCP est fermée ou en erreur :
        l'appelant doit alors reconnecter.
        """
        while True:
            # Trouver l'octet '$' (0x24) qui débute une trame interleaved
            while b"$" not in self._rxbuf:
                if not self._fill_rxbuf():
                    return None
            idx = self._rxbuf.index(b"$")
            if idx > 0:
                # Texte avant le '$' : ce sont des octets RTSP (réponse keepalive ?)
                # On les jette ; un parser plus robuste stockerait pour matcher
                # la réponse, mais on n'envoie aucune requête RTSP en mode stream.
                self._rxbuf = self._rxbuf[idx:]

            # On a maintenant idx=0 → '$' au début. Header = 4 octets.
            while len(self._rxbuf) < 4:
                if not self._fill_rxbuf():
                    return None
            channel = self._rxbuf[1]
            length = struct.unpack_from("!H", self._rxbuf, 2)[0]
            while len(self._rxbuf) < 4 + length:
                if not self._fill_rxbuf():
                    return None
            data = self._rxbuf[4:4 + length]
            self._rxbuf = self._rxbuf[4 + length:]
            return channel, bytes(data)

    def _fill_rxbuf(self) -> bool:
        """Retourne True si des octets ont été lus, False sur timeout.
        Lève ConnectionError si la socket est fermée/en erreur (EOF)."""
        try:
            chunk = self._tcp.recv(65536)
        except socket.timeout:
            return False
        except OSError as exc:
            raise ConnectionError(f"TCP recv failed: {exc}") from exc
        if not chunk:
            raise ConnectionError("TCP socket closed by peer (EOF)")
        self._rxbuf += chunk
        return True

    # ── Teardown ───────────────────────────────────────────────────
    # En TCP interleaved on ne peut pas envoyer une requête RTSP sans
    # casser le démultiplexage des $-frames côté client : pas de keepalive
    # explicite, la session reste vivante tant qu'on consomme les paquets.

    def teardown(self) -> None:
        try:
            if self._tcp:
                self._tcp.close()
        except Exception:
            pass

    def _request(self, method: str, url: str, extra: str = "") -> str:
        session_hdr = f"Session: {self._session_id}\r\n" if self._session_id else ""
        msg = (
            f"{method} {url} RTSP/1.0\r\n"
            f"CSeq: {self._cseq}\r\n"
            f"User-Agent: CamSync-RTP/1.0\r\n"
            f"{session_hdr}{extra}\r\n"
        )
        self._cseq += 1
        self._tcp.sendall(msg.encode())
        return self._recv_response()

    def _recv_response(self) -> str:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self._tcp.recv(4096)
            if not chunk:
                break
            buf += chunk
        header_part = buf.split(b"\r\n\r\n", 1)[0]
        cl = re.search(rb"Content-Length:\s*(\d+)", header_part, re.IGNORECASE)
        if cl:
            content_length = int(cl.group(1))
            body_start = buf.index(b"\r\n\r\n") + 4
            body = buf[body_start:]
            while len(body) < content_length:
                chunk = self._tcp.recv(min(content_length - len(body), 4096))
                if not chunk:
                    break
                body += chunk
            buf = buf[:body_start] + body
        return buf.decode(errors="replace")


# ── Réassemblage H.264 RTP ────────────────────────────────────────────────────

class _H264RTPReassembler:
    """Réassemble FU-A / STAP-A / single NALU. Yield (rtp_ts, nalu, marker)."""

    def __init__(self) -> None:
        self._frags: list[bytes] = []
        self._frag_ts: int = 0

    def feed(self, pkt: bytes):
        if len(pkt) < 12:
            return None
        if (pkt[0] >> 6) & 0x3 != 2:
            return None

        marker = (pkt[1] >> 7) & 0x1
        rtp_ts = struct.unpack_from("!I", pkt, 4)[0]
        cc = pkt[0] & 0x0F
        hdr_len = 12 + cc * 4
        if (pkt[0] >> 4) & 0x1:                    # extension présente
            if len(pkt) < hdr_len + 4:
                return None
            ext_len = struct.unpack_from("!H", pkt, hdr_len + 2)[0]
            hdr_len += 4 + ext_len * 4

        payload = pkt[hdr_len:]
        if not payload:
            return None

        nal_type = payload[0] & 0x1F

        if 1 <= nal_type <= 23:                    # NALU unique
            self._frags = []
            return rtp_ts, b"\x00\x00\x00\x01" + payload, bool(marker)

        if nal_type == 24:                         # STAP-A
            out = b""
            i = 1
            while i + 2 <= len(payload):
                size = struct.unpack_from("!H", payload, i)[0]
                i += 2
                if i + size > len(payload):
                    break
                out += b"\x00\x00\x00\x01" + payload[i:i + size]
                i += size
            if out:
                self._frags = []
                return rtp_ts, out, bool(marker)
            return None

        if nal_type == 28 and len(payload) >= 2:   # FU-A
            fu_indicator = payload[0]
            fu_header = payload[1]
            start = (fu_header >> 7) & 0x1
            end = (fu_header >> 6) & 0x1
            fu_nal_type = fu_header & 0x1F
            data = payload[2:]
            if start:
                nal_hdr = (fu_indicator & 0xE0) | fu_nal_type
                self._frags = [bytes([nal_hdr]) + data]
                self._frag_ts = rtp_ts
            elif self._frags:
                self._frags.append(data)
            if end and self._frags:
                nalu = b"".join(self._frags)
                ts = self._frag_ts
                self._frags = []
                return ts, b"\x00\x00\x00\x01" + nalu, bool(marker)

        return None


# ── Modèle d'horloge RTCP ─────────────────────────────────────────────────────

@dataclass
class _SR:
    ntp_unix: float        # NTP du SR converti en epoch Unix
    rtp_ts: int            # rtp_ts associé
    arrival_mono: float    # time.monotonic() à la réception du SR


class _RTCPClock:
    """Modèle d'horloge RTCP avec ancre figée + correction host-NTP.

    Approche :
      - Le **tout premier SR** définit l'ancre figée (rtp_ts, arrival_mono).
        Ces deux valeurs ne changent JAMAIS après — garantit la continuité
        de `rtp_ts_to_unix` et `arrival_to_unix`.
      - L'offset host_ntp - cam_ntp est suivi par un **min glissant sur
        les 20 derniers SRs** (~100 s à 5 s/SR). Le minimum de la fenêtre
        représente la mesure la moins bruitée par la gigue réseau UDP.
        Un min global (ratchet pur) dériverait indéfiniment vers des valeurs
        extrêmes sur des sessions longues.

    Pourquoi corriger l'horloge cam :
      Les Insta Pro2 envoient un NTP cam dans le SR qui peut être totalement
      faux (testé : décalage de 147 jours). Sans correction, FlashCalib
      cherche les frames dans le futur et ne détecte rien. La correction
      ramène le wall-time des Insta sur le même référentiel que les Ottica
      (qui utilisent l'horloge host directement via NDI).
    """

    _WINDOW = 20   # nombre de SRs dans la fenêtre glissante (~100 s)

    def __init__(self):
        self._lock = threading.Lock()
        # Premier SR figé : (ntp_cam_brut, rtp_ts, arrival_mono).
        # Jamais modifié après le 1er SR.
        self._first_sr_raw: Optional[_SR] = None
        # Offset (host_ntp - cam_ntp) appliqué à _first_sr_raw.ntp_unix.
        # Min glissant sur les _WINDOW derniers SRs.
        self._anchor_offset: float = 0.0
        self._offset_window: collections.deque = collections.deque(
            maxlen=self._WINDOW)
        self._sr_count = 0

    @property
    def has_lock(self) -> bool:
        return self._first_sr_raw is not None

    @property
    def sr_count(self) -> int:
        return self._sr_count

    def _corrected_ntp(self) -> Optional[float]:
        if self._first_sr_raw is None:
            return None
        return self._first_sr_raw.ntp_unix + self._anchor_offset

    def update(self, ntp_unix_cam: float, rtp_ts: int, arrival_mono: float,
               host_ntp_now: float) -> None:
        new_offset = host_ntp_now - ntp_unix_cam
        with self._lock:
            self._sr_count += 1
            if self._first_sr_raw is None:
                self._first_sr_raw = _SR(
                    ntp_unix=ntp_unix_cam,
                    rtp_ts=rtp_ts,
                    arrival_mono=arrival_mono,
                )
                self._anchor_offset = new_offset
            self._offset_window.append(new_offset)
            # Médiane glissante : robuste à la gigue réseau sans se verrouiller
            # sur un extrême comme le ferait min(). Chaque caméra converge vers
            # le même référentiel host_ntp, réduisant le biais relatif inter-caméras.
            self._anchor_offset = float(np.median(list(self._offset_window)))
            return None

    def rtp_ts_to_unix(self, rtp_ts: int) -> Optional[float]:
        with self._lock:
            sr = self._first_sr_raw
            ntp_corrected = self._corrected_ntp()
        if sr is None or ntp_corrected is None:
            return None
        delta = (rtp_ts - sr.rtp_ts) & 0xFFFFFFFF
        if delta > 0x80000000:
            delta -= 0x1_0000_0000
        return ntp_corrected + delta / _RTP_CLOCK_HZ

    def arrival_to_unix(self, arrival_mono: float) -> Optional[float]:
        with self._lock:
            sr = self._first_sr_raw
            ntp_corrected = self._corrected_ntp()
        if sr is None or ntp_corrected is None:
            return None
        return ntp_corrected + (arrival_mono - sr.arrival_mono)


def _ntp64_to_unix(msw: int, lsw: int) -> float:
    return (msw - _NTP_EPOCH_OFFSET) + lsw / (2 ** 32)


def _parse_rtcp_compound_sr(data: bytes) -> list[tuple[float, int]]:
    """Retourne [(ntp_unix, rtp_ts), ...] pour chaque Sender Report."""
    out = []
    off = 0
    while off + 4 <= len(data):
        b0 = data[off]
        version = (b0 >> 6) & 0x03
        pt = data[off + 1]
        length_words = struct.unpack_from("!H", data, off + 2)[0]
        pkt_len = (length_words + 1) * 4
        if version != 2 or off + pkt_len > len(data):
            break
        if pt == 200 and pkt_len >= 28:
            _, msw, lsw, rtp_ts, _, _ = struct.unpack_from(
                "!IIIIII", data, off + 4
            )
            out.append((_ntp64_to_unix(msw, lsw), rtp_ts))
        off += pkt_len
    return out



# ── Classe publique ───────────────────────────────────────────────────────────

class RtpCameraStream:
    """Capture vidéo RTSP/RTP avec décodage PyAV et timestamps RTCP/NTP.

    API publique compatible `GstCameraStream` :
      start(), stop(), latest_frame(), frame_at(), get_buffer_snapshot(),
      is_connected, actual_fps, last_ntp_timestamp, display_delay_sec,
      sr_count, timestamp_source, name, model, url, camera_id.
    """

    def __init__(
        self,
        camera_id: str,
        name: str,
        model: str,
        url: str,
        ntp_clock,
        buffer_duration: float = 5.0,
        target_fps: int = 30,
        display_delay_ms: float = 0.0,
        flip: bool = False,
        decoder_backend: str = "pyav",
        hw_hwaccel=None,
    ):
        self.camera_id = camera_id
        self.name = name
        self.model = model
        self.url = url
        self._clock = ntp_clock
        self._target_fps = target_fps
        self._flip = flip
        self._hw_hwaccel = hw_hwaccel
        self._display_delay_sec = max(0.0, float(display_delay_ms) / 1000.0)
        # Délai dynamique additionnel piloté par DynamicAligner.
        self._dynamic_delay_sec = 0.0
        # Délai de CALIBRATION mesuré par Flash Calib, appliqué à chaud.
        self._calib_delay_sec = 0.0

        max_frames = int(buffer_duration * target_fps)
        self._buffer: collections.deque[TimestampedFrame] = collections.deque(
            maxlen=max_frames
        )
        self._lock = threading.Lock()

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._connected = False
        self._actual_fps = 0.0
        self._last_timestamp = 0.0
        self._seq = 0
        self._ts_source = "host_ntp"
        self._fps_count = 0
        self._fps_start = time.monotonic()

        self._clock_model = _RTCPClock()

    # ── API publique ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"RTP-{self.name}"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=8)
            self._thread = None
        self._connected = False

    def latest_frame(self) -> Optional[TimestampedFrame]:
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
                d = abs(tf.capture_monotonic - target)
                if d < best_diff:
                    best_diff = d
                    best = tf
            return best

    def frame_at(self, target_ntp: float, tolerance: float = 0.1
                 ) -> Optional[TimestampedFrame]:
        with self._lock:
            best = None
            best_diff = float("inf")
            for tf in self._buffer:
                d = abs(tf.ntp_timestamp - target_ntp)
                if d < best_diff:
                    best_diff = d
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
        return self._clock_model.sr_count

    @property
    def timestamp_source(self) -> str:
        with self._lock:
            return self._ts_source

    # ── Boucle thread principale ──────────────────────────────────────────

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop_event.is_set():
            try:
                self._session_loop()
                backoff = 2.0
            except Exception as exc:
                print(f"[{self.name}] Erreur RTP/RTSP : {exc}", flush=True)
            self._connected = False
            if self._stop_event.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 15.0)

    def _session_loop(self) -> None:
        rtsp = _RTSPSession(self.url)
        codec_ctx: Optional[av.CodecContext] = None
        try:
            sprop = rtsp.connect()
            print(f"[{self.name}] RTSP PLAY OK TCP interleaved", flush=True)
            codec_ctx = self._make_codec(sprop)
            self._connected = True
            self._ingest_tcp(rtsp, codec_ctx)
        finally:
            self._connected = False
            try:
                rtsp.teardown()
            except Exception:
                pass
            if codec_ctx is not None:
                try:
                    codec_ctx.close()
                except Exception:
                    pass

    def _make_codec(self, sprop: str) -> av.CodecContext:
        # Décodeur H.264. Par défaut software (PyAV / libavcodec).
        # Si self._hw_hwaccel est défini, on tente le décodeur matériel
        # correspondant (h264_cuvid pour NVIDIA NVDEC, h264_qsv pour Intel
        # QuickSync). En cas d'échec d'ouverture, on retombe en software.
        codec_name = "h264"
        accel = (self._hw_hwaccel or "").lower().strip()
        hw_map = {
            "cuda": "h264_cuvid",
            "nvdec": "h264_cuvid",
            "cuvid": "h264_cuvid",
            "qsv": "h264_qsv",
            "intel": "h264_qsv",
        }
        if accel in hw_map:
            codec_name = hw_map[accel]

        try:
            codec_ctx = av.CodecContext.create(codec_name, "r")
        except Exception as exc:
            print(f"[{self.name}] codec {codec_name} indisponible "
                  f"({exc}) → fallback software h264", flush=True)
            codec_name = "h264"
            codec_ctx = av.CodecContext.create("h264", "r")

        try:
            codec_ctx.thread_count = 4
            # FRAME threading : parallélise le décodage de plusieurs frames
            # simultanément. Incompatible avec LOW_DELAY (qui forçait
            # thread_count=1 silencieusement dans libavcodec). Les Insta360
            # Pro2 streament en H.264 Baseline/Main sans B-frames, donc
            # retirer LOW_DELAY n'ajoute aucune latence de reordonnancement.
            codec_ctx.thread_type = "FRAME"
        except Exception:
            pass
        try:
            # AV_CODEC_FLAG2_FAST = 0x1 : saute le loop filter (déblocking).
            # Réduit le temps de décodage de ~20-30% avec une légère perte
            # de qualité acceptable pour un flux de monitoring live.
            codec_ctx.flags2 |= 0x1
        except Exception:
            pass
        if codec_name == "h264_cuvid":
            # Réduire le nombre de surfaces NVDEC (défaut ~25) pour diminuer
            # le buffer interne du décodeur matériel. Avec 4 surfaces, le
            # décodeur ne peut pas accumuler plus de 4 frames en attente,
            # ce qui réduit la latence de décodage variable (GOP jitter).
            try:
                codec_ctx.options = {"surfaces": "4"}
            except Exception:
                pass
        if sprop:
            extradata = b""
            for b64 in sprop.split(","):
                b64 = b64.strip()
                if b64:
                    try:
                        nalu = base64.b64decode(b64 + "==")
                        extradata += b"\x00\x00\x00\x01" + nalu
                    except Exception:
                        pass
            if extradata:
                codec_ctx.extradata = extradata
        try:
            codec_ctx.open()
        except Exception as exc:
            if codec_name != "h264":
                print(f"[{self.name}] open {codec_name} échoué ({exc}) "
                      f"→ fallback software h264", flush=True)
                codec_ctx = av.CodecContext.create("h264", "r")
                try:
                    codec_ctx.thread_count = 4
                    codec_ctx.thread_type = "FRAME"
                except Exception:
                    pass
                try:
                    codec_ctx.flags2 |= 0x1
                except Exception:
                    pass
                if sprop and extradata:
                    codec_ctx.extradata = extradata
                codec_ctx.open()
                codec_name = "h264"
            else:
                raise
        try:
            print(f"[{self.name}] codec opened name={codec_name} "
                  f"thread_count={codec_ctx.thread_count} "
                  f"thread_type={codec_ctx.thread_type}", flush=True)
        except Exception:
            pass
        return codec_ctx

    def _ingest_tcp(self, rtsp: _RTSPSession,
                    codec_ctx: av.CodecContext) -> None:
        """Reader TCP + decoder dans des threads séparés."""
        # maxsize=8 : absorbe les petites rafales H.264 sans déclencher
        # le drop-oldest + skip-to-IDR (qui provoque des spikes de timestamp
        # de ~1 s correspondant à l'intervalle IDR).
        au_queue: queue.Queue = queue.Queue(maxsize=8)

        decoder_thread = threading.Thread(
            target=self._decoder_loop, args=(au_queue, codec_ctx),
            daemon=True, name=f"DEC-{self.name}",
        )
        decoder_thread.start()

        try:
            self._reader_loop_tcp(rtsp, au_queue)
        finally:
            au_queue.put(None)                     # sentinelle d'arrêt
            decoder_thread.join(timeout=3)

    def _reader_loop_tcp(self, rtsp: _RTSPSession,
                         au_queue: "queue.Queue") -> None:
        reass = _H264RTPReassembler()
        au_nalus: list[bytes] = []
        au_rtp_ts: Optional[int] = None
        au_arrival = time.monotonic()

        # Quand le décodeur prend du retard, on saute tout le flux jusqu'à
        # la prochaine IDR pour préserver l'intégrité du GOP côté décodeur.
        # wait_for_idr_until : 0.0 = pas en attente ; sinon deadline monotonic
        # au-delà de laquelle on abandonne l'attente (max 200 ms / ~6 frames).
        wait_for_idr_until: float = 0.0

        diag_t = time.monotonic()
        diag_pkts = 0
        diag_aus_pushed = 0
        diag_aus_skipped = 0
        diag_resync = 0

        last_rtp_t = time.monotonic()
        STALL_TIMEOUT_S = 5.0

        while not self._stop_event.is_set():
            if time.monotonic() - last_rtp_t > STALL_TIMEOUT_S:
                raise ConnectionError(
                    f"[{self.name}] Aucun paquet RTP depuis "
                    f"{STALL_TIMEOUT_S:.0f}s — flux figé, reconnexion."
                )

            if time.monotonic() - diag_t > 30.0:
                print(f"[{self.name}] RTP TCP : pkts/s={diag_pkts/30:.0f} "
                      f"aus/s={diag_aus_pushed/30:.1f} "
                      f"skip/s={diag_aus_skipped/30:.1f} "
                      f"resync/s={diag_resync/30:.2f} "
                      f"q={au_queue.qsize()} fps={self._actual_fps:.1f}",
                      flush=True)
                diag_t = time.monotonic()
                diag_pkts = 0
                diag_aus_pushed = 0
                diag_aus_skipped = 0
                diag_resync = 0

            res = rtsp.read_interleaved()
            if res is None:
                continue
            channel, payload = res
            arrival_mono = time.monotonic()

            if channel == 1:                       # RTCP
                for ntp_unix, rtp_ts in _parse_rtcp_compound_sr(payload):
                    self._clock_model.update(
                        ntp_unix_cam=ntp_unix, rtp_ts=rtp_ts,
                        arrival_mono=arrival_mono,
                        host_ntp_now=self._clock.now(),
                    )
                continue
            if channel != 0:
                continue

            diag_pkts += 1
            last_rtp_t = time.monotonic()
            r = reass.feed(payload)
            if r is None:
                continue
            rtp_ts, nalu, is_last = r

            if au_rtp_ts is None:
                au_rtp_ts = rtp_ts
                au_arrival = arrival_mono

            if rtp_ts != au_rtp_ts:
                if au_nalus:
                    pushed, wait_for_idr_until, resynced = self._submit_au(
                        au_queue, au_nalus, au_rtp_ts, au_arrival,
                        wait_for_idr_until,
                    )
                    if pushed:
                        diag_aus_pushed += 1
                    else:
                        diag_aus_skipped += 1
                    if resynced:
                        diag_resync += 1
                au_nalus = []
                au_rtp_ts = rtp_ts
                au_arrival = arrival_mono

            au_nalus.append(nalu)

            if is_last and au_nalus:
                pushed, wait_for_idr_until, resynced = self._submit_au(
                    au_queue, au_nalus, au_rtp_ts, au_arrival,
                    wait_for_idr_until,
                )
                if pushed:
                    diag_aus_pushed += 1
                else:
                    diag_aus_skipped += 1
                if resynced:
                    diag_resync += 1
                au_nalus = []
                au_rtp_ts = None

    @staticmethod
    def _au_contains_idr(au_bytes: bytes) -> bool:
        """True si l'AU contient un slice IDR (NAL type 5)."""
        i = 0
        n = len(au_bytes)
        while i + 5 <= n:
            if au_bytes[i:i+4] == b"\x00\x00\x00\x01":
                if (au_bytes[i+4] & 0x1F) == 5:
                    return True
                i += 4
            else:
                i += 1
        return False

    # Durée max pendant laquelle on attend un IDR après un queue overflow.
    # Au-delà, on reprend le décodage même sans IDR : le décodeur produit
    # quelques frames avec artefacts puis se resynchronise seul, ce qui
    # est moins perturbant pour la calibration flash qu'un freeze de 400 ms.
    _IDR_WAIT_MAX_S: float = 0.200   # ≈ 6 frames @ 30 fps

    def _submit_au(self, au_queue: "queue.Queue", au_nalus: list,
                   rtp_ts: int, arrival_mono: float,
                   wait_for_idr_until: float) -> tuple[bool, float, bool]:
        """Tente de pousser un AU. Retourne (pushed, wait_for_idr_until, resynced).

        Stratégie : drop-oldest + skip-to-IDR borné dans le temps.
        - queue full → drop oldest + wait_for_idr_until = now + 200ms.
        - pendant l'attente : AUs non-IDR rejetés (préserve la cohérence GOP).
        - après 200ms sans IDR : on abandonne l'attente et on reprend quand
          même — le décodeur produit 1-2 frames glitchées puis se récupère.
        """
        au_bytes = b"".join(au_nalus)
        is_idr = self._au_contains_idr(au_bytes)
        resynced = False

        # Attente IDR en cours : vérifier si le délai est dépassé.
        if wait_for_idr_until > 0.0:
            if not is_idr and time.monotonic() < wait_for_idr_until:
                # Toujours dans la fenêtre d'attente, jeter cet AU.
                return False, wait_for_idr_until, False
            # IDR reçu OU délai dépassé → sortir du mode skip.
            wait_for_idr_until = 0.0

        item = (rtp_ts, au_bytes, arrival_mono, is_idr)
        try:
            au_queue.put_nowait(item)
            return True, 0.0, False
        except queue.Full:
            # Drop oldest, push new, armer le skip-to-IDR borné.
            try:
                au_queue.get_nowait()
            except queue.Empty:
                pass
            resynced = True
            new_wait = 0.0 if is_idr else (time.monotonic() + self._IDR_WAIT_MAX_S)
            try:
                au_queue.put_nowait(item)
                return True, new_wait, resynced
            except queue.Full:
                return False, (time.monotonic() + self._IDR_WAIT_MAX_S), resynced

    def _decoder_loop(self, au_queue: "queue.Queue",
                      codec_ctx: av.CodecContext) -> None:
        self._fps_count = 0
        self._fps_start = time.monotonic()
        while not self._stop_event.is_set():
            try:
                item = au_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            rtp_ts, au_bytes, arrival_mono, _is_idr = item
            self._decode_au(codec_ctx, au_bytes, rtp_ts, arrival_mono)

    def _decode_au(self, codec_ctx: av.CodecContext, au_bytes: bytes,
                   rtp_ts: int, arrival_mono: float) -> None:
        try:
            pkt = av.Packet(au_bytes)
            frames = codec_ctx.decode(pkt)
        except Exception:
            return

        for frame in frames:
            try:
                img = frame.to_ndarray(format="bgr24")
            except Exception:
                continue

            if self._flip:
                img = cv2.flip(img, -1)

            ntp_ts, source = self._timestamp(rtp_ts, arrival_mono)
            self._seq += 1
            tf = TimestampedFrame(
                frame=img,
                ntp_timestamp=ntp_ts,
                capture_monotonic=arrival_mono,
                sequence_number=self._seq,
                timestamp_source=source,
                confidence="medium" if source == "rtcp_sr" else "low",
                stream_pts_ms=-1.0,
            )
            with self._lock:
                self._buffer.append(tf)
                self._ts_source = source
            self._last_timestamp = ntp_ts

            self._fps_count += 1
            el = time.monotonic() - self._fps_start
            if el >= 1.0:
                self._actual_fps = self._fps_count / el
                self._fps_count = 0
                self._fps_start = time.monotonic()

    def _timestamp(self, rtp_ts: int, arrival_mono: float) -> tuple[float, str]:
        """Priorité : RTP→NTP via SR, puis interp. monotone, puis NTP hôte."""
        if self._clock_model.has_lock:
            if rtp_ts != _RTP_SENTINEL:
                v = self._clock_model.rtp_ts_to_unix(rtp_ts)
                if v is not None:
                    return v, "rtcp_sr"
            v = self._clock_model.arrival_to_unix(arrival_mono)
            if v is not None:
                return v, "rtcp_sr"
        return self._clock.now(), "host_ntp"
