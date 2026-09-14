"""
stream_aligner.py – Alignement temporel des flux vidéo.

Stratégie :
  1. On choisit un instant de référence commun : le timestamp NTP le plus
     ancien parmi les caméras connectées (la caméra la plus en retard définit
     l’horizon commun, garantissant que toutes ont une frame disponible).
  2. Pour chaque caméra, on sélectionne dans son buffer la frame dont le
     timestamp NTP est le plus proche de cet instant, corrigé de l’offset.
  3. En mode « alignement continu », on décale la lecture de chaque flux
     d'un offset calculé pour compenser le décalage mesuré.
"""

import time
from dataclasses import dataclass

from gst_camera_stream import TimestampedFrame


@dataclass
class AlignedSnapshot:
    """Ensemble de frames alignées à un même instant NTP."""
    reference_ntp: float
    frames: dict[str, TimestampedFrame | None]  # camera_id → frame
    residuals_ms: dict[str, float]               # camera_id → résidu (ms)


class StreamAligner:
    """Aligne les flux vidéo dans le temps en utilisant les buffers horodatés.

    Amélioration inspirée du rapport de stage : recalibration automatique
    des offsets quand le drift mesuré dépasse un seuil configurable.
    """

    def __init__(self, cameras: list, drift_threshold_ms_per_sec: float = 0.5,
                 recalib_interval_sec: float = 10.0,
                 playout_delay_ms: float = 5000.0):
        self._cameras = cameras
        self._enabled = False
        self._offsets: dict[str, float] = {}  # camera_id → offset correctif (sec)
        self._drift_threshold = drift_threshold_ms_per_sec
        self._recalib_interval = recalib_interval_sec
        self._playout_delay_ms = playout_delay_ms
        self._last_recalib_mono: float = 0.0

    # ── API publique ────────────────────────────────────────────────────────

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def enable(self):
        """Active l'alignement : calcule les offsets correctifs."""
        self._compute_offsets()
        self._enabled = True

    def disable(self):
        """Désactive l'alignement (lecture temps réel brute)."""
        self._enabled = False
        self._offsets.clear()

    def toggle(self):
        if self._enabled:
            self.disable()
        else:
            self.enable()

    def get_aligned_frames(self) -> AlignedSnapshot:
        """
        Renvoie un snapshot de frames alignées.
        - Si l'alignement est activé : cherche dans les buffers les frames
          correspondant à un instant commun.
        - Sinon : renvoie simplement les frames les plus récentes.
        """
        if not self._enabled:
            return self._latest_snapshot()
        return self._aligned_snapshot()

    def get_offsets(self) -> dict[str, float]:
        """Renvoie les offsets correctifs appliqués (secondes)."""
        return dict(self._offsets)

    def check_drift_recalib(self, pair_metrics: list) -> bool:
        """
        Vérifie si le drift justifie une recalibration automatique.
        Appelé périodiquement par la GUI.

        Retourne True si une recalibration a été effectuée.
        """
        if not self._enabled or not pair_metrics:
            return False

        now = time.monotonic()
        if (now - self._last_recalib_mono) < self._recalib_interval:
            return False

        # Drift max parmi toutes les paires
        max_drift = max(abs(m.drift_ms_per_sec) for m in pair_metrics)
        if max_drift > self._drift_threshold:
            self._compute_offsets()
            self._last_recalib_mono = now
            return True
        return False

    # ── Calcul des offsets ──────────────────────────────────────────────────

    def _compute_offsets(self):
        """
        Calcule l'offset correctif de chaque caméra par rapport à la
        caméra de référence (première de la liste).

        Amélioration : moyenne sur les N dernières frames du buffer au lieu
        d'un seul point (plus robuste face aux fluctuations de latence).
        """
        self._offsets.clear()
        if not self._cameras:
            return

        ref = self._cameras[0]
        ref_buf = ref.get_buffer_snapshot()
        if not ref_buf:
            return

        # Moyenne des timestamps des 10 dernières frames de la référence
        ref_recent = ref_buf[-10:]
        ref_avg = sum(f.ntp_timestamp for f in ref_recent) / len(ref_recent)

        self._offsets[ref.camera_id] = 0.0
        for cam in self._cameras[1:]:
            cam_buf = cam.get_buffer_snapshot()
            if not cam_buf:
                self._offsets[cam.camera_id] = 0.0
                print(
                    f"[StreamAligner] Caméra {cam.name} : buffer vide, "
                    f"offset forcé à 0.0 (traitée comme non alignée).",
                    flush=True,
                )
            else:
                cam_recent = cam_buf[-10:]
                cam_avg = sum(f.ntp_timestamp for f in cam_recent) / len(cam_recent)
                self._offsets[cam.camera_id] = ref_avg - cam_avg

    # ── Snapshots ───────────────────────────────────────────────────────────

    def _latest_snapshot(self) -> AlignedSnapshot:
        """Snapshot sans alignement : frames les plus récentes."""
        frames: dict[str, TimestampedFrame | None] = {}
        residuals: dict[str, float] = {}
        ts_list: list[float] = []

        for cam in self._cameras:
            tf = cam.latest_frame()
            frames[cam.camera_id] = tf
            if tf:
                ts_list.append(tf.ntp_timestamp)

        ref_ntp = max(ts_list) if ts_list else 0.0
        for cam_id, tf in frames.items():
            if tf:
                residuals[cam_id] = (tf.ntp_timestamp - ref_ntp) * 1000.0
            else:
                residuals[cam_id] = 0.0

        return AlignedSnapshot(
            reference_ntp=ref_ntp,
            frames=frames,
            residuals_ms=residuals,
        )

    def _aligned_snapshot(self) -> AlignedSnapshot:
        """
        Snapshot aligné : pour chaque caméra, on cherche la frame à
        l'instant (ref_ntp - offset[cam]).

        ref_ntp = dernier timestamp de cameras[0] (la caméra de référence
        utilisée pour calculer les offsets dans _compute_offsets).
        Les offsets sont définis comme offset[cam] = ref_avg - cam_avg,
        donc target[cam] = ref_ntp - offset[cam] = ref_ntp - ref_avg + cam_avg,
        ce qui pointe vers une frame passée de chaque caméra dans son buffer.

        Compensation display_delay : chaque caméra peut avoir un
        display_delay_sec (ex : Ottica=0.4 s pour matcher la latence
        pipeline des Insta360). On soustrait ce delay au target, afin que
        la frame retournée corresponde au contenu réel simultané.
        """
        ref_cam = self._cameras[0] if self._cameras else None
        if ref_cam is None or not ref_cam.is_connected:
            return self._latest_snapshot()
        ref_last = ref_cam.last_ntp_timestamp
        if ref_last == 0.0:
            return self._latest_snapshot()

        ref_delay = getattr(ref_cam, "display_delay_sec", 0.0)
        # ref_ntp reflète l'instant « affiché » pour la caméra de référence.
        ref_ntp = ref_last - ref_delay

        # Pour chaque caméra, target = ref_last - offset[cam] - cam_display_delay.
        # Ainsi chaque caméra renvoie sa frame « affichée », i.e. celle qui
        # correspond au même instant de capture réel que la référence.
        frames: dict[str, TimestampedFrame | None] = {}
        residuals: dict[str, float] = {}

        for cam in self._cameras:
            offset = self._offsets.get(cam.camera_id, 0.0)
            cam_delay = (getattr(cam, "display_delay_sec", 0.0)
                         + getattr(cam, "calib_delay_sec", 0.0))
            target_ts = ref_last - offset - cam_delay
            tf = cam.frame_at(target_ts, tolerance=0.2)
            frames[cam.camera_id] = tf
            if tf:
                residuals[cam.camera_id] = (tf.ntp_timestamp - target_ts) * 1000.0
            else:
                residuals[cam.camera_id] = 0.0

        return AlignedSnapshot(
            reference_ntp=ref_ntp,
            frames=frames,
            residuals_ms=residuals,
        )
