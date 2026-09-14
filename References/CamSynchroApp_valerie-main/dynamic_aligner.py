"""
dynamic_aligner.py – Alignement dynamique des latences de flux.

POURQUOI
========
Figer un ``display_delay_ms`` par caméra (calibration manuelle au flash) est
fragile : la détection de flash échoue dès qu'un capteur sature (cf. Insta 360
plein-champ équirectangulaire dont la luma moyenne plafonne), et l'horloge
interne des caméras RTSP dérive d'une session à l'autre. Résultat : on calibre
contre du bruit et ça ne converge jamais.

PRINCIPE
========
On mesure en continu la latence réelle de chaque flux = âge de la dernière
frame reçue ::

    age_last = now - last_ntp_timestamp

C'est une mesure STABLE et indépendante du contenu visuel (pas de seuil, pas
de saturation). La caméra la plus lente du moment définit l'horizon commun ;
les caméras plus rapides reçoivent un ``display_delay`` dynamique pour
l'attendre, de sorte que toutes affichent un contenu de même âge ::

    target_age            = max(age_last) + marge
    dynamic_delay[cam]    = target_age - age_last[cam]      (borné [0, max])

La caméra la plus lente a ``dynamic_delay ≈ marge`` (elle ne peut pas attendre
elle-même), les autres compensent leur avance.

ROBUSTESSE
==========
- Lissage EMA par caméra : la latence Insta peut spiker de ±300 ms sur un
  burst d'encodage ; sans lissage le délai oscillerait et l'image saccaderait.
- Hystérésis (``min_step_ms``) : on ne ré-applique le délai que si le
  changement est significatif, pour éviter un micro-jitter d'affichage.
- Le délai est borné ``[0, max_delay_ms]`` et ne s'applique qu'aux caméras
  connectées dont le buffer est suffisamment rempli.

Le délai dynamique s'AJOUTE au ``display_delay`` statique de config.py, qui ne
sert donc plus que de biais résiduel manuel éventuel (0 par défaut).
"""

import time


class DynamicAligner:
    """Aligne les latences des flux en pilotant ``set_dynamic_delay`` des cams.

    Appeler :meth:`update` périodiquement (≈ 1 Hz). Activé via :meth:`enable`.
    """

    def __init__(
        self,
        cameras: list,
        margin_ms: float = 80.0,
        ema_alpha: float = 0.15,
        max_delay_ms: float = 2500.0,
        min_step_ms: float = 15.0,
        min_buffer: int = 10,
    ):
        self._cameras = cameras
        self._margin = margin_ms / 1000.0
        self._alpha = ema_alpha
        self._max_delay = max_delay_ms / 1000.0
        self._min_step = min_step_ms / 1000.0
        self._min_buffer = min_buffer
        self._ema_age: dict[str, float] = {}    # cam_id → âge lissé (sec)
        self._applied: dict[str, float] = {}    # cam_id → délai appliqué (sec)
        self._enabled = False

    # ── API publique ────────────────────────────────────────────────────────

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        """Désactive et remet tous les délais dynamiques à zéro."""
        self._enabled = False
        for cam in self._cameras:
            self._apply(cam, 0.0)
        self._ema_age.clear()
        self._applied.clear()

    def toggle(self) -> None:
        if self._enabled:
            self.disable()
        else:
            self.enable()

    def update(self) -> None:
        """Recalcule et applique les délais dynamiques. À appeler ≈ 1 Hz."""
        if not self._enabled:
            return

        now = time.time()

        # 1. Mettre à jour l'EMA d'âge de chaque caméra éligible.
        ages: dict[str, float] = {}
        for cam in self._cameras:
            if not getattr(cam, "is_connected", False):
                continue
            last = getattr(cam, "last_ntp_timestamp", 0.0)
            if last <= 0.0:
                continue
            try:
                buf_n = len(cam.get_buffer_snapshot())
            except Exception:
                continue
            if buf_n < self._min_buffer:
                continue

            age = now - last
            if age < 0.0:
                age = 0.0
            prev = self._ema_age.get(cam.camera_id)
            ema = age if prev is None else (
                self._alpha * age + (1.0 - self._alpha) * prev)
            self._ema_age[cam.camera_id] = ema
            ages[cam.camera_id] = ema

        # Besoin d'au moins 2 caméras pour parler d'alignement relatif.
        if len(ages) < 2:
            return

        # 2. La caméra la plus lente (âge max) définit l'horizon commun.
        target_age = max(ages.values()) + self._margin

        # 3. Appliquer delay = target - age (borné), avec hystérésis.
        for cam in self._cameras:
            cid = cam.camera_id
            if cid not in ages:
                continue
            desired = target_age - ages[cid]
            desired = max(0.0, min(self._max_delay, desired))
            prev = self._applied.get(cid, 0.0)
            if abs(desired - prev) >= self._min_step:
                self._apply(cam, desired)

    # ── Interne ──────────────────────────────────────────────────────────────

    def _apply(self, cam, sec: float) -> None:
        fn = getattr(cam, "set_dynamic_delay", None)
        if fn is None:
            return
        fn(sec)
        self._applied[cam.camera_id] = sec
