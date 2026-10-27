"""
gst_camera_stream.py – Dataclass commune `TimestampedFrame`.

Le module portait à l'origine une implémentation OpenCV/FFmpeg, remplacée
par PyAV (`rtp_camera_stream.RtpCameraStream`) et NDI
(`ndi_camera_stream.NdiCameraStream`). Seule la dataclass partagée par
les trois pipelines est conservée ici pour rester rétro-compatible avec
les imports existants (`from gst_camera_stream import TimestampedFrame`).
"""

from dataclasses import dataclass

import numpy as np


@dataclass
class TimestampedFrame:
    """Une frame vidéo avec un timestamp NTP provenant de la caméra."""
    frame: np.ndarray
    ntp_timestamp: float          # NTP UTC caméra (secondes Unix epoch)
    capture_monotonic: float      # time.monotonic() à la réception
    sequence_number: int = 0
    timestamp_source: str = ""    # "rtcp_sr" | "ndi_timecode" | "host_ntp"
    confidence: str = "low"       # "high" | "medium" | "low"
    stream_pts_ms: float = -1.0   # PTS décodeur (dérivé du timestamp RTP), -1 si indisponible
