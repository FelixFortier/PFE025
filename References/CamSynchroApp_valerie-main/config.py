"""
Configuration des caméras et paramètres de l'application.
"""

# ── Définition des caméras ──────────────────────────────────────────────────
CAMERAS = [
    {
        "id": "ottica_4k_1",
        "name": "Ottica 4K #1",
        "model": "Ottica 4K",
        "ip": "10.180.145.13",
        "ndi_url": "10.180.145.13:5962",
        "ndi_bandwidth": "highest",  # Full-res pour calib flash fiable
        # display_delay_ms : biais STATIQUE optionnel (ms). L'alignement
        # principal est désormais fait par Flash Calib qui mesure l'écart
        # visuel réel et l'applique à chaud (calib_delay) à chaque session.
        # L'offset intrinsèque n'est pas reproductible d'une session à
        # l'autre (ancrage NDI/RTCP), donc on recalibre au flash au début de
        # chaque session. Laisser à 0 sauf biais constant avéré.
        "display_delay_ms": 0,
    },
    {
        "id": "ottica_4k_2",
        "name": "Ottica 4K #2",
        "model": "Ottica 4K",
        "ip": "10.180.145.58",
        "ndi_url": "10.180.145.58:5962",
        "ndi_bandwidth": "highest",  # Full-res pour calib flash fiable
        "display_delay_ms": 0,
    },
    {
        "id": "insta360_pro2_2",
        "name": "Insta 1",
        "model": "Insta 360 Pro2",
        "ip": "10.180.145.19",
        "url": "rtsp://10.180.145.19/live/live",
        "flip": False,
        "display_delay_ms": 0,
    },
    {
        "id": "insta360_pro2_3",
        "name": "Insta 2",
        "model": "Insta 360 Pro2",
        "ip": "10.180.145.20",
        "url": "rtsp://10.180.145.20/live/live",
        "flip": False,
        "display_delay_ms": 0,
    },
]

# ── Serveur NTP pour horodatage absolu ──────────────────────────────────────
NTP_SERVER = "pool.ntp.org"
NTP_SYNC_INTERVAL_SEC = 30  # Ré-interrogation NTP toutes les 30 s

# ── Paramètres d'affichage ──────────────────────────────────────────────────

TARGET_FPS = 30       # FPS cible pour la lecture des flux

# NDI bandwidth pour les caméras Ottica.
# "lowest"  : faible bande passante (souvent résolution réduite)
# "highest" : qualité/résolution maximale envoyée par la source
# Note: NDI n'a pas de vrai niveau "medium" standard.
# Le compromis recommandé est de définir "ndi_bandwidth" par caméra
# (ex. une caméra en highest, l'autre en lowest).
OTTICA_NDI_BANDWIDTH = "highest"

# ── Paramètres de synchronisation ───────────────────────────────────────────
SYNC_BUFFER_SEC = 5.0       # Tampon circulaire (secondes) pour l'alignement
JITTER_WINDOW_SIZE = 60     # Nombre d'échantillons pour le calcul du jitter
PLAYOUT_DELAY_MS = 5000     # Délai de lecture pour l'alignement temporel (ms)

# ── Paramètres décodeur Insta 360 ───────────────────────────────────────────
INSTA_DECODER  = "pyav"     # Backend de décodage (pyav = libavcodec direct)
# Accélération matérielle pour le décodage H.264 des Insta :
#   None / ""  : décodage SOFTWARE (libavcodec / CPU) — RECOMMANDÉ pour la
#                synchro car le décodeur software + LOW_DELAY sort chaque
#                frame immédiatement sans attendre le GOP complet. NVDEC
#                (h264_cuvid) bufferise ~1 GOP (=30 frames ≈ 1s) avant de
#                libérer, ce qui introduit un jitter de pipeline de 400-800ms
#                qui rend la calibration flash impossible à stabiliser.
#   "cuda"     : NVIDIA NVDEC via h264_cuvid — décoder rapide mais pipeline
#                variable → NE PAS UTILISER pour la calibration flash.
#   "qsv"      : Intel QuickSync via h264_qsv
INSTA_HW_ACCEL = ""   # Software : LOW_DELAY + thread_count=4 → jitter < 50ms
