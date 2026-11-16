"""
Configuration des caméras et paramètres de l'application.
"""
import json
import os

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')

# ── Définition des caméras ──────────────────────────────────────────────────
CAMERAS = None

    # display_delay_ms : biais STATIQUE optionnel (ms). L'alignement
    # principal est désormais fait par Flash Calib qui mesure l'écart
    # visuel réel et l'applique à chaud (calib_delay) à chaque session.
    # L'offset intrinsèque n'est pas reproductible d'une session à
    # l'autre (ancrage NDI/RTCP), donc on recalibre au flash au début de
    # chaque session. Laisser à 0 sauf biais constant avéré.

# ── Serveur NTP pour horodatage absolu ──────────────────────────────────────
NTP_SERVER = None
NTP_SYNC_INTERVAL_SEC = None  # Ré-interrogation NTP toutes les 30 s

# ── Paramètres d'affichage ──────────────────────────────────────────────────

TARGET_FPS = None       # FPS cible pour la lecture des flux



# ── Paramètres de synchronisation ───────────────────────────────────────────
SYNC_BUFFER_SEC = None       # Tampon circulaire (secondes) pour l'alignement
JITTER_WINDOW_SIZE = None     # Nombre d'échantillons pour le calcul du jitter
PLAYOUT_DELAY_MS = None     # Délai de lecture pour l'alignement temporel (ms)

# ── Paramètres des Modèles ───────────────────────────────────────────

MODELS = None

# DECODER
# Pour Insta360
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
#   "pyav"     : Backend de décodage (pyav = libavcodec direct)


# BANDWIDTH
# Pour les caméras Ottica
# NDI bandwidth
# "lowest"  : faible bande passante (souvent résolution réduite)
# "highest" : qualité/résolution maximale envoyée par la source
# Note: NDI n'a pas de vrai niveau "medium" standard.
# Le compromis recommandé est de définir "ndi_bandwidth" par caméra
# (ex. une caméra en highest, l'autre en lowest).
# Full-res pour calib flash fiable

# HW_ACCEL
# Pour Insta360
# Software : LOW_DELAY + thread_count=4 → jitter < 50ms

def load_config_json():
    with open(_CONFIG_PATH, 'r', encoding='utf-8') as file:
        configs = json.load(file)
        return configs

def get_model_attribute(configs, name, attribute):
    MODELS = configs["models"]
    value = (model[attribute] for model in MODELS if model["name"] == name)
    return next(value, None)

def get_camera_attribute(configs, name, attribute):
    CAMERAS = configs["cameras"]
    value = (camera[attribute] for camera in CAMERAS if camera["name"] == name)
    return next(value, None)

def full_update_config_file(configs):
    with open(_CONFIG_PATH, "w") as file:
        json.dump(configs, file, indent=4)
        
        
if __name__ == "__main__":
    
    configs = load_config_json()

    CAMERAS = configs["cameras"]
    MODELS = configs["models"]
    JITTER_WINDOW_SIZE = configs["JITTER_WINDOW_SIZE"]
    NTP_SERVER = configs["NTP_SERVER"]
    NTP_SYNC_INTERVAL_SEC = configs["NTP_SYNC_INTERVAL_SEC"]
    PLAYOUT_DELAY_MS = configs["PLAYOUT_DELAY_MS"]
    SYNC_BUFFER_SEC = configs["SYNC_BUFFER_SEC"]
    TARGET_FPS = configs["TARGET_FPS"]

    print(CAMERAS)
    print([camera["name"] for camera in CAMERAS]) 
    print(MODELS)
    print(JITTER_WINDOW_SIZE)
    print(NTP_SERVER)
    print(NTP_SYNC_INTERVAL_SEC)
    print(PLAYOUT_DELAY_MS)
    print(SYNC_BUFFER_SEC)
    print(TARGET_FPS)


    