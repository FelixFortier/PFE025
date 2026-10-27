"""
flash_calibrator.py — Mesure de l'écart visuel entre caméras.

Principe
========
Pour mesurer le décalage temporel **réel** (visuel) entre N caméras, on
demande à l'utilisateur de provoquer un changement franc de luminosité
dans le champ commun (allumage ou extinction de la lumière). Sur chaque
caméra :

  1. On échantillonne la luminance moyenne d'une ROI centrale au fil
     des frames qui arrivent (lecture passive du buffer du stream).
  2. On calcule un baseline glissant (médiane des K derniers échantillons
     d'avant la fenêtre courante).
  3. Lorsque |luma_now − baseline| dépasse ``threshold``, on considère
     qu'on a détecté la transition (front montant OU descendant).
  4. On affine la position temporelle par interpolation linéaire sub-frame
     entre l'échantillon juste avant le franchissement et celui qui le
     franchit. Précision finale ~ ½ inter-frame, donc ~20 ms à 25 fps,
     ~50 ms à 10 fps.

Le résultat est ``{camera_id: ntp_timestamp}`` au moment du franchissement.
``max(t) − min(t)`` est l'écart visuel mesuré entre flux.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


# ─── Paramètres par défaut ─────────────────────────────────────────────────
DEFAULT_DURATION_S       = 30.0    # timeout dur (sécurité). On n'attend
                                   # cette durée que si une caméra ne
                                   # détecte jamais ; sinon on finit dès
                                   # que TOUTES ont détecté.
DEFAULT_BASELINE_SAMPLES = 10      # K échantillons pour le baseline
DEFAULT_THRESHOLD_DELTA  = 12.0    # écart luma min (échelle 0-255)
ROI_FRACTION             = 1.0     # ROI = image entière (robuste 360° fisheye)


@dataclass
class _CamState:
    cam_id: str
    name: str
    samples: deque = field(default_factory=lambda: deque(maxlen=400))
    last_ntp_seen: float = -1.0
    flash_ntp: Optional[float] = None
    baseline: float = 0.0
    first_frame: Optional[np.ndarray] = None
    last_frame: Optional[np.ndarray] = None
    frame_shape: Optional[tuple] = None


@dataclass
class FlashResult:
    """Final result of a calibration session."""
    detections: dict[str, float]           # cam_id -> ntp_ts du flash
    spread_ms: float
    relative_offsets_ms: dict[str, float]  # par rapport au plus précoce
    not_detected: list[str]                # noms des caméras sans détection


class FlashCalibrator:
    """Détecte une transition franche de luminance sur N caméras et
    calcule l'écart visuel entre flux."""

    def __init__(self, cameras: list,
                 duration_s: float = DEFAULT_DURATION_S,
                 threshold_delta: float = DEFAULT_THRESHOLD_DELTA,
                 baseline_samples: int = DEFAULT_BASELINE_SAMPLES):
        self._cameras = cameras
        self._duration = duration_s
        self._threshold = threshold_delta
        self._baseline_n = baseline_samples
        self._states: dict[str, _CamState] = {}
        self._running = False
        self._t_start_mono = 0.0
        self._t_first_detect_mono: Optional[float] = None
        self._result: Optional[FlashResult] = None

    # ─── Interface publique ────────────────────────────────────────────

    def start(self) -> bool:
        """Arme la détection. Retourne False si aucune caméra connectée."""
        connected = [c for c in self._cameras
                     if getattr(c, "is_connected", False)]
        if not connected:
            return False
        # Initialise last_ntp_seen au timestamp courant pour ignorer le
        # contenu déjà bufferisé (sinon premier tick = O(buffer × N cams)
        # qui freeze l'UI ~200ms).
        self._states = {}
        for c in connected:
            st = _CamState(cam_id=c.camera_id, name=c.name)
            try:
                buf = c.get_buffer_snapshot()
                if buf:
                    st.last_ntp_seen = buf[-1].capture_monotonic
            except Exception:
                pass
            self._states[c.camera_id] = st
        self._t_start_mono = time.monotonic()
        self._t_first_detect_mono = None
        self._running = True
        self._result = None
        return True

    def cancel(self):
        self._running = False
        self._result = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def progress(self) -> tuple[int, int, float]:
        """Retourne (n_detected, n_total, elapsed_s)."""
        if not self._states:
            return (0, 0, 0.0)
        det = sum(1 for s in self._states.values() if s.flash_ntp is not None)
        elapsed = time.monotonic() - self._t_start_mono
        return (det, len(self._states), elapsed)

    @property
    def result(self) -> Optional[FlashResult]:
        return self._result

    def tick(self) -> bool:
        """À appeler régulièrement (~30 Hz). Retourne True si la session
        vient de se terminer (résultat dispo dans ``self.result``)."""
        if not self._running:
            return False

        elapsed = time.monotonic() - self._t_start_mono

        for cam in self._cameras:
            st = self._states.get(cam.camera_id)
            if st is None:
                continue
            self._process_camera(cam, st)

        all_detected = all(s.flash_ntp is not None
                           for s in self._states.values())

        # On finit UNIQUEMENT quand :
        #  - toutes les caméras ont détecté (avec une marge d'1 s pour
        #    laisser arriver les dernières frames retardées), OU
        #  - timeout dur (sécurité : une caméra qui ne détectera jamais).
        # Plus de raccourci "X secondes après la 1ʳᵉ détection" : on
        # attend réellement les Insta.
        finish = elapsed >= self._duration
        if not finish and all_detected and elapsed > 1.0:
            finish = True

        if finish:
            self._finalize()
            return True
        return False

    # ─── Traitement par caméra ─────────────────────────────────────────

    def _process_camera(self, cam, st: _CamState):
        """Échantillonne les nouvelles frames du buffer et détecte la
        transition. Lecture non destructive du buffer."""
        try:
            snap_fn = getattr(cam, "get_buffer_snapshot", None)
            if snap_fn is None:
                tf = cam.latest_frame()
                buf = [tf] if tf is not None else []
            else:
                buf = snap_fn()
        except Exception:
            return

        if not buf:
            return

        # Limite de sécurité : ne traite jamais plus de 8 frames par tick et
        # par caméra (évite de bloquer la GUI si le buffer s'est rempli).
        new_frames = []
        for tf in buf:
            if tf is None or tf.frame is None:
                continue
            if tf.capture_monotonic <= st.last_ntp_seen:
                continue
            new_frames.append(tf)
        if len(new_frames) > 8:
            new_frames = new_frames[-8:]

        for tf in new_frames:
            st.last_ntp_seen = tf.capture_monotonic
            try:
                luma = _mean_luma_center(tf.frame)
            except Exception:
                continue
            st.samples.append((tf.capture_monotonic, luma))
            if st.first_frame is None:
                st.first_frame = tf.frame.copy()
                st.frame_shape = tf.frame.shape
            st.last_frame = tf.frame  # ref, copied later if dumped

            if st.flash_ntp is None:
                self._try_detect(st, tf.capture_monotonic, luma)

    def _try_detect(self, st: _CamState, ntp_now: float, luma_now: float):
        if len(st.samples) < self._baseline_n + 1:
            return

        # Baseline = médiane des K échantillons précédant l'actuel
        recent = list(st.samples)
        prev_window = recent[-(self._baseline_n + 1):-1]
        prev_lumas = np.fromiter((s[1] for s in prev_window), dtype=np.float32)
        baseline = float(np.median(prev_lumas))
        st.baseline = baseline

        # Seuil adaptatif : max(seuil fixe, 5× écart-type du baseline).
        # Sur une caméra stable (Ottica), le bruit étant faible, le seuil
        # fixe (12) domine. Sur une caméra agitée par l'AGC (Insta), on
        # accepte toute variation > 5σ du bruit observé, ce qui détecte
        # même une transition partiellement compensée par l'AGC.
        baseline_std = float(prev_lumas.std())
        eff_threshold = max(self._threshold, 5.0 * baseline_std)

        delta = luma_now - baseline
        if abs(delta) < eff_threshold:
            return

        # Détection bidirectionnelle : front montant (allumage) ou
        # descendant (extinction).
        thr = baseline + eff_threshold if delta > 0 else baseline - eff_threshold
        prev_ntp, prev_luma = recent[-2]

        crossed = ((delta > 0 and prev_luma < thr <= luma_now)
                   or (delta < 0 and prev_luma > thr >= luma_now))
        if not crossed:
            # Échantillon précédent déjà au-delà du seuil ⇒ transition
            # antérieure ratée (bruit ou frame manquante). On garde l'inst.
            st.flash_ntp = ntp_now
            return

        # Interpolation linéaire sub-frame entre prev et now
        denom = luma_now - prev_luma
        if abs(denom) < 1e-6:
            st.flash_ntp = ntp_now
            return
        frac = (thr - prev_luma) / denom
        frac = max(0.0, min(1.0, frac))
        st.flash_ntp = prev_ntp + frac * (ntp_now - prev_ntp)

    # ─── Finalisation ──────────────────────────────────────────────────

    def _finalize(self):
        self._running = False

        # Diagnostic console : pour chaque cam, statistiques sur la luma
        # observée pendant la session. Permet de voir si la caméra a réellement
        # capté une variation (et de quelle amplitude) même quand la détection
        # n'a pas déclenché.
        print("[FlashCalib] === diagnostic luma ===")
        import os
        from datetime import datetime
        dump_dir = os.path.join("logs", "flash_dumps",
                                datetime.now().strftime("%Y%m%d_%H%M%S"))
        # Création du répertoire une fois pour toutes : si elle échoue
        # (chemin invalide, droits insuffisants), on désactive les dumps
        # plutôt que de réessayer pour chaque caméra.
        try:
            os.makedirs(dump_dir, exist_ok=True)
        except OSError as exc:
            print(f"[FlashCalib] Cannot create {dump_dir}: "
                  f"{exc} (dumps disabled)")
            dump_dir = None
        for st in self._states.values():
            if not st.samples:
                print(f"  - {st.name}: NO samples")
                continue
            lumas = np.fromiter((s[1] for s in st.samples), dtype=np.float32)
            n = len(lumas)
            lo, hi = float(lumas.min()), float(lumas.max())
            mean = float(lumas.mean()); std = float(lumas.std())
            rng = hi - lo
            status = ("OK" if st.flash_ntp is not None
                      else f"NOT DETECTED (threshold={self._threshold:.1f})")
            shape = st.frame_shape if st.frame_shape else "?"
            print(f"  - {st.name}: n={n}  shape={shape}  min={lo:.2f}  max={hi:.2f}  "
                  f"range={rng:.2f}  mean={mean:.2f}  std={std:.3f}  "
                  f"→ {status}")
            # Pour les cams non détectées : dump première/dernière frame
            # + trajectoire luma complète (CSV) pour analyser hors ligne.
            if st.flash_ntp is None and dump_dir is not None:
                try:
                    safe_name = "".join(c if c.isalnum() else "_"
                                         for c in st.name)
                    if st.first_frame is not None:
                        _save_image(os.path.join(
                            dump_dir, f"{safe_name}_first.png"),
                            st.first_frame)
                    if st.last_frame is not None:
                        _save_image(os.path.join(
                            dump_dir, f"{safe_name}_last.png"),
                            st.last_frame)
                    csv_path = os.path.join(dump_dir,
                                            f"{safe_name}_luma.csv")
                    with open(csv_path, "w", encoding="utf-8") as f:
                        f.write("t_rel_s,luma\n")
                        t0 = st.samples[0][0]
                        for ntp, lu in st.samples:
                            f.write(f"{ntp - t0:.3f},{lu:.4f}\n")
                    print(f"      → dump: {dump_dir}\\{safe_name}_*.png/csv")
                except Exception as exc:
                    print(f"      → dump failed: {exc}")
        print("[FlashCalib] =======================")

        detections = {sid: s.flash_ntp for sid, s in self._states.items()
                      if s.flash_ntp is not None}
        not_detected = [s.name for s in self._states.values()
                        if s.flash_ntp is None]

        # L'offset VISUEL = capture_monotonic + délai effectif appliqué à
        # latest_frame() (calib_delay + display_delay). Ce délai représente
        # le "recul" imposé à la caméra pour attendre les plus lentes :
        # la frame affichée a capture_mono = newest - delay.
        # Sans cette correction, le spread mesuré = offset de PIPELINE pur
        # (inchangé après calibration). Avec : il reflète ce que l'œil voit.
        cam_by_id = {c.camera_id: c for c in self._cameras}
        visual_adjusted = {}
        for cid, mono in detections.items():
            cam = cam_by_id.get(cid)
            eff = 0.0
            if cam is not None:
                eff = (getattr(cam, "display_delay_sec", 0.0)
                       + getattr(cam, "calib_delay_sec", 0.0))
            visual_adjusted[cid] = mono + eff

        if len(visual_adjusted) >= 2:
            t_min = min(visual_adjusted.values())
            spread = (max(visual_adjusted.values()) - t_min) * 1000.0
            offsets = {cid: (t - t_min) * 1000.0
                       for cid, t in visual_adjusted.items()}
        else:
            spread = 0.0
            offsets = {}

        self._result = FlashResult(
            detections=detections,
            spread_ms=spread,
            relative_offsets_ms=offsets,
            not_detected=not_detected,
        )


# ─── Helpers ────────────────────────────────────────────────────────────────

def _save_image(path: str, frame: np.ndarray) -> None:
    """Sauve une image (BGR/grayscale) en PNG sans dépendance OpenCV obligatoire."""
    try:
        import cv2
        cv2.imwrite(path, frame)
    except Exception:
        try:
            from PIL import Image
            arr = frame
            if arr.ndim == 3 and arr.shape[2] >= 3:
                arr = arr[..., [2, 1, 0]]  # BGR→RGB
            Image.fromarray(arr).save(path)
        except Exception as exc:
            raise RuntimeError(f"impossible de sauver {path}: {exc}")


def _mean_luma_center(frame: np.ndarray) -> float:
    """Luminance moyenne d'une ROI centrale (downsample x4).
    Tolérant grayscale/BGR/BGRA. La moyenne globale reflète bien un
    changement d'éclairage de la pièce ; le 95e percentile a été essayé
    mais sur les fisheye 360° il était dominé par les zones brillantes
    saturées (lampes, fenêtres) qui ne varient pas → range=0."""
    h, w = frame.shape[:2]
    rh = int(h * ROI_FRACTION)
    rw = int(w * ROI_FRACTION)
    y0 = (h - rh) // 2
    x0 = (w - rw) // 2
    step = 4
    if frame.ndim == 2:
        roi = frame[y0:y0 + rh:step, x0:x0 + rw:step].astype(np.float32)
    elif frame.shape[2] >= 3:
        roi3 = frame[y0:y0 + rh:step, x0:x0 + rw:step, :3]
        b = roi3[..., 0].astype(np.float32)
        g = roi3[..., 1].astype(np.float32)
        r = roi3[..., 2].astype(np.float32)
        roi = 0.114 * b + 0.587 * g + 0.299 * r
    else:
        roi = frame[y0:y0 + rh:step, x0:x0 + rw:step, 0].astype(np.float32)
    return float(roi.mean())
