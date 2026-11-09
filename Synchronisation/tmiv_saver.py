"""
tmiv_encoder.py – Génération des fichiers de métadonnées TMIV.

Expose write_tmiv_metadata(), appelée par RecordingWorker dans gui.py
après chaque session d'enregistrement.

Format cible : TMIV v4 / ISO-IEC 23090-12
  - tmiv_metadata.json  : inventaire interne des fichiers YUV.
  - sequences/T01.json  : séquence au format TMIV CTC, consommé par
    TmivEncoder avec :
      inputSequenceConfigPathFmt = "sequences/{1}.json"
      inputTexturePathFmt        = "{3}.yuv"

Les champs Position / Rotation / Focal / Principle_point sont des
placeholders (0) ; les remplacer par les résultats de calibration
(Calibration/).
"""

import json
import math
import os


def write_tmiv_metadata(
    out_dir: str,
    cameras: list,
    rec_paths: dict,
    frame_sizes: dict,
    n_frames: int,
    fps: float = 30.0,
) -> None:
    """Écrit tmiv_metadata.json et sequences/T01.json dans out_dir.

    Args:
        out_dir:     Dossier de l'enregistrement (chemin absolu).
        cameras:     Liste d'objets caméra (attributs .camera_id et .name).
        rec_paths:   Dict camera_id → chemin absolu du fichier .yuv.
        frame_sizes: Dict camera_id → (width, height).
        n_frames:    Nombre de frames enregistrées.
        fps:         Fréquence d'images de la séquence.
    """
    _write_internal_metadata(out_dir, cameras, rec_paths, frame_sizes, n_frames, fps)
    _write_sequence_config(out_dir, cameras, frame_sizes, n_frames, fps)


def _write_internal_metadata(
    out_dir: str,
    cameras: list,
    rec_paths: dict,
    frame_sizes: dict,
    n_frames: int,
    fps: float,
) -> None:
    """Écrit tmiv_metadata.json (inventaire interne des fichiers YUV)."""
    meta_path    = os.path.join(out_dir, "tmiv_metadata.json")
    cameras_meta = []
    for cam in cameras:
        cid  = cam.camera_id
        w, h = frame_sizes.get(cid, (0, 0))
        cameras_meta.append({
            "Name":            cam.name,
            "BitDepthColor":   8,
            "ColorSpace":      "YUV420",
            "Resolution":      [w, h],
            "Projection":      "Perspective",
            "Position":        [0.0, 0.0, 0.0],
            "Rotation":        [0.0, 0.0, 0.0],
            "Depth_range":     [0.1, 1000.0],
            "BitDepthDepth":   16,
            "DepthColorSpace": "YUV420",
            "yuv_file":        os.path.basename(rec_paths.get(cid, "")),
        })
    metadata = {
        "Version":        "4.0",
        "Content_name":   os.path.basename(out_dir),
        "Fps":            fps,
        "NumberOfFrames": n_frames,
        "cameras":        cameras_meta,
    }
    try:
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2)
        print(f"[TMIV] metadata → {meta_path}", flush=True)
    except Exception as e:
        print(f"[TMIV] Erreur écriture metadata : {e}", flush=True)


def _write_sequence_config(
    out_dir: str,
    cameras: list,
    frame_sizes: dict,
    n_frames: int,
    fps: float,
) -> None:
    """Écrit sequences/T01.json (séquence TMIV v4 pour TmivEncoder).

    Le champ Name de chaque caméra correspond au safe_name utilisé pour
    nommer les fichiers YUV (espaces → _, / → _, # → N), de sorte que
    inputTexturePathFmt = "{3}.yuv" résolve directement le bon fichier.
    """
    seq_dir  = os.path.join(out_dir, "sequences")
    seq_path = os.path.join(seq_dir, "T01.json")
    try:
        os.makedirs(seq_dir, exist_ok=True)
        source_names = []
        seq_cameras  = []
        for cam in cameras:
            cid       = cam.camera_id
            w, h      = frame_sizes.get(cid, (0, 0))
            if w == 0 or h == 0:
                continue
            safe_name = (cam.name
                         .replace(" ", "_")
                         .replace("/", "_")
                         .replace("#", "N"))
            source_names.append(safe_name)
            # Focal estimée pour ~60° FoV horizontal (1 / tan(30°) × w/2).
            # Remplacer par les valeurs réelles issues de Calibration/.
            focal_est = round(w / (2.0 * math.tan(math.radians(30.0))), 1)
            seq_cameras.append({
                "Background":      1,
                "BitDepthColor":   8,
                "BitDepthDepth":   16,
                "ColorSpace":      "YUV420",
                "DepthColorSpace": "YUV420",
                "Depth_range":     [0.1, 100.0],
                "Depthmap":        0,
                "Focal":           [focal_est, focal_est],
                "HasInvalidDepth": False,
                "Name":            safe_name,
                "Position":        [0.0, 0.0, 0.0],
                "Principle_point": [round(w / 2.0, 1), round(h / 2.0, 1)],
                "Projection":      "Perspective",
                "Resolution":      [w, h],
                "Rotation":        [0.0, 0.0, 0.0],
            })
        seq_meta = {
            "BoundingBox_center": [0.0, 0.0, 0.0],
            "Content_name":       os.path.basename(out_dir),
            "Fps":                fps,
            "Frames_number":      n_frames,
            "Version":            "4.0",
            "cameras":            seq_cameras,
            "lengthsInMeters":    True,
            "sourceCameraNames":  source_names,
        }
        with open(seq_path, 'w', encoding='utf-8') as f:
            json.dump(seq_meta, f, indent=4)
        print(f"[TMIV] sequence config → {seq_path}", flush=True)
    except Exception as e:
        print(f"[TMIV] Erreur écriture sequence config : {e}", flush=True)
