"""
sync_metrics.py – Calcul des métriques de synchronisation entre flux vidéo.

Métriques calculées :
  • Offset (décalage) : différence de timestamp NTP entre deux flux.
  • Jitter : variation de l'offset au cours du temps (écart-type glissant).
  • Drift : dérive linéaire de l'offset (pente, ms/s).
  • Max |offset| : pire décalage observé dans la fenêtre.
"""

import collections
import csv
import datetime
import os
import time
from dataclasses import dataclass

import numpy as np


@dataclass
class PairMetrics:
    """Métriques de synchronisation entre deux caméras."""
    cam_a_id: str
    cam_b_id: str
    offset_sec: float = 0.0        # offset courant (A - B) en secondes
    offset_ms: float = 0.0         # offset courant en millisecondes
    jitter_ms: float = 0.0         # écart-type de l'offset (ms)
    drift_ms_per_sec: float = 0.0  # dérive de l'offset (ms/s)
    max_abs_offset_ms: float = 0.0 # pire |offset| sur la fenêtre (ms)
    samples: int = 0               # nombre d'échantillons collectés


class SyncMetricsEngine:
    """Calcule en continu les métriques de synchro entre flux caméra.

    Si csv_dir est spécifié, les métriques sont logées dans un fichier CSV
    pour analyse post-hoc (un fichier par session, nommé par date/heure).
    """

    def __init__(self, cameras: list, window_size: int = 60,
                 csv_dir: str = "logs"):
        self._cameras = cameras
        self._window_size = window_size

        # Historique d'offsets par paire (cam_a_id, cam_b_id)
        self._history: dict[tuple[str, str], collections.deque[tuple[float, float]]] = {}
        # Initialiser les paires (toutes les combinaisons référencées à la caméra 0)
        if cameras:
            ref = cameras[0]
            for other in cameras[1:]:
                key = (ref.camera_id, other.camera_id)
                self._history[key] = collections.deque(maxlen=window_size)

        # ── CSV logging ──────────────────────────────────────────────────────────
        self._csv_writer = None
        self._csv_file = None
        if csv_dir:
            try:
                os.makedirs(csv_dir, exist_ok=True)
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                path = os.path.join(csv_dir, f"sync_metrics_{ts}.csv")
                self._csv_file = open(path, "w", newline="", encoding="utf-8")
                self._csv_writer = csv.writer(self._csv_file)
                self._csv_writer.writerow([
                    "timestamp_utc", "cam_a", "cam_b",
                    "offset_ms", "jitter_ms", "drift_ms_per_sec",
                    "max_abs_offset_ms", "samples",
                ])
                print(f"[SyncMetrics] Logging CSV → {path}", flush=True)
            except Exception as e:
                print(f"[SyncMetrics] CSV logging désactivé : {e}", flush=True)
                self._csv_writer = None

    # ── API publique ────────────────────────────────────────────────────────

    def compute_all_pairs(self, aligned_timestamps: dict[str, float] | None = None) -> list[PairMetrics]:
        """Calcule les métriques pour toutes les paires.

        Si *aligned_timestamps* est fourni (dict camera_id → NTP timestamp),
        utilise ces timestamps (post-alignement) au lieu des timestamps bruts.
        """
        results: list[PairMetrics] = []
        n = len(self._cameras)
        if n < 2:
            return results

        now_mono = time.monotonic()

        for i in range(n):
            for j in range(i + 1, n):
                cam_a = self._cameras[i]
                cam_b = self._cameras[j]

                if aligned_timestamps is not None:
                    ts_a = aligned_timestamps.get(cam_a.camera_id, 0.0)
                    ts_b = aligned_timestamps.get(cam_b.camera_id, 0.0)
                else:
                    ts_a = cam_a.last_ntp_timestamp
                    ts_b = cam_b.last_ntp_timestamp

                if ts_a == 0.0 or ts_b == 0.0:
                    continue

                key = (cam_a.camera_id, cam_b.camera_id)
                offset = ts_a - ts_b

                history = self._history.setdefault(
                    key, collections.deque(maxlen=self._window_size)
                )
                history.append((now_mono, offset))
                results.append(self._compute_pair(key, history))

        self._log_csv(results)
        return results

    # ── CSV logging ────────────────────────────────────────────────────────

    def _log_csv(self, metrics: list[PairMetrics]):
        """Ajoute les métriques au fichier CSV si actif."""
        if not self._csv_writer or not metrics:
            return
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="milliseconds")
        for m in metrics:
            self._csv_writer.writerow([
                now_utc, m.cam_a_id, m.cam_b_id,
                f"{m.offset_ms:.3f}", f"{m.jitter_ms:.3f}",
                f"{m.drift_ms_per_sec:.4f}", f"{m.max_abs_offset_ms:.3f}",
                m.samples,
            ])
        self._csv_file.flush()

    def close(self):
        """Ferme le fichier CSV de logging."""
        if self._csv_file:
            try:
                self._csv_file.close()
            except Exception:
                pass
            self._csv_file = None
            self._csv_writer = None

    # ── Calcul interne ──────────────────────────────────────────────────────

    @staticmethod
    def _compute_pair(
        key: tuple[str, str],
        history: collections.deque[tuple[float, float]],
    ) -> PairMetrics:
        """Calcule les métriques sur l'historique d'une paire."""
        offsets = [o for _, o in history]
        times = [t for t, _ in history]
        n = len(offsets)

        current_offset = offsets[-1]
        offset_ms_arr = np.array(offsets) * 1000.0

        jitter = float(np.std(offset_ms_arr)) if n >= 2 else 0.0
        max_abs = float(np.max(np.abs(offset_ms_arr)))

        # Drift via régression linéaire (ms/s)
        drift = 0.0
        if n >= 3:
            t_arr = np.array(times) - times[0]
            if t_arr[-1] > 0:
                coeffs = np.polyfit(t_arr, offset_ms_arr, 1)
                drift = float(coeffs[0])  # pente en ms/s

        return PairMetrics(
            cam_a_id=key[0],
            cam_b_id=key[1],
            offset_sec=current_offset,
            offset_ms=current_offset * 1000.0,
            jitter_ms=jitter,
            drift_ms_per_sec=drift,
            max_abs_offset_ms=max_abs,
            samples=n,
        )
