print("START OF MAIN", flush=True)
"""
main.py – Point d'entrée de CamSync.

Lance l'horloge NTP, les flux caméra (RTSP via PyAV pour Insta,
NDI natif pour Ottica), le moteur de métriques, l'aligneur et l'interface.

IMPORTANT : Utiliser Python 3.12 portable (layout CamSyncPortable).
  .\\python\\python.exe main.py
"""

import os
import sys
import time
import traceback

print("reconfigure")
# Force UTF-8 pour stdout/stderr (évite les crashes en console cp1252 / PowerShell)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

print("excepthook")
def _excepthook(exc_type, exc_value, exc_tb):
    """Affiche les exceptions non capturées (slots Qt) au lieu de tuer
    silencieusement l'application."""
    print("=" * 70, flush=True)
    print("[UNCAUGHT EXCEPTION]", flush=True)
    traceback.print_exception(exc_type, exc_value, exc_tb)
    print("=" * 70, flush=True)


sys.excepthook = _excepthook

# S'assurer que le répertoire du projet est en tête de sys.path
# (évite d'importer les vieux modules de sync_cameras/src via PYTHONPATH)
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_DIR not in sys.path or sys.path[0] != _PROJECT_DIR:
    if _PROJECT_DIR in sys.path:
        sys.path.remove(_PROJECT_DIR)
    sys.path.insert(0, _PROJECT_DIR)

# Enregistrer le répertoire NDI pour les DLLs Windows
# Ordre de recherche :
#   1) <dossier du script>/../ndi/  (layout package portable)
#   2) <dossier du script>/         (NDI files copiés a côté du script)
#   3) chemin développeur original
_script_dir = os.path.dirname(os.path.abspath(__file__))
_ndi_candidates = [
    os.path.join(_script_dir, "..", "ndi"),  # portable package
    _script_dir,                              # fichiers copiés à côté
    r"C:\Users\Client\sync_cameras\src",     # machine développeur
]
_NDI_SRC = next((p for p in _ndi_candidates if os.path.isfile(os.path.join(p, "Processing.NDI.Lib.x64.dll"))), None)
if _NDI_SRC:
    _NDI_SRC = os.path.normpath(_NDI_SRC)
    os.add_dll_directory(_NDI_SRC)
    if _NDI_SRC not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _NDI_SRC + os.pathsep + os.environ.get("PATH", "")

from ndi_camera_stream import NdiCameraStream
from PyQt5.QtWidgets import QApplication
from rtp_camera_stream import RtpCameraStream
import config
from gui import MainWindow
from ntp_clock import NTPClock
from stream_aligner import StreamAligner
from dynamic_aligner import DynamicAligner
from sync_metrics import SyncMetricsEngine

print("START OF MAIN", flush=True)

class MainRuntime:
    def __init__(self,app):
        self.app = app
        self.streams = []
        self.metrics_engine = None
        self.aligner = None
        self.dyn_aligner = None   
        self.window = None
        self._diag_timer = None
        self._dyn_timer = None
        self.window = None
        self._restarting = False
        self.start_thread = None
        self.configs = None
        self.ntp_clock = None
    
    def build_runtime(self):

        # Lecture et parsing de config.json
        self.configs = config.load_config_json()
        CAMERAS = self.configs["cameras"]
        JITTER_WINDOW_SIZE = self.configs["JITTER_WINDOW_SIZE"]
        NTP_SERVER = self.configs["NTP_SERVER"]
        NTP_SYNC_INTERVAL_SEC = self.configs["NTP_SYNC_INTERVAL_SEC"]
        PLAYOUT_DELAY_MS = self.configs["PLAYOUT_DELAY_MS"]
        SYNC_BUFFER_SEC = self.configs["SYNC_BUFFER_SEC"]
        TARGET_FPS = self.configs["TARGET_FPS"]
    
        # 1. Horloge NTP (fallback si RTCP SR indisponible)
        print("[CamSync] Synchronizing host NTP clock...", flush=True)
        self.ntp_clock = NTPClock(server=NTP_SERVER, sync_interval=NTP_SYNC_INTERVAL_SEC)
        self.ntp_clock.start()
        print(f"[CamSync] Host NTP offset: {self.ntp_clock.offset * 1000:.1f} ms", flush=True)
    
        # 1b. Les lives Insta360 doivent être démarrés manuellement avant
        #     le lancement (interface web caméra ou Insta360 Pro2 Controller).
        #     L'application se contente de lire l'URL RTSP configurée.
        insta_cams = [c for c in CAMERAS
                      if c.get("model") == "Insta 360 Pro2" and c.get("enabled", True)]
        if insta_cams:
            print("[CamSync] Reminder: start live streams manually on the Insta360 cameras:",
                  flush=True)
            for c in insta_cams:
                print(f"           - {c['name']}  ({c['url']})", flush=True)
    
        # 2. Préparer les objets caméra (sans les démarrer)
        self.streams: list = []
    
        for cam_cfg in CAMERAS:
            if not cam_cfg.get("enabled", True):
                print(f"[CamSync] {cam_cfg['name']} disabled, skipping.", flush=True)
                continue
            protocol = cam_cfg.get("protocol", "")
    
            model = cam_cfg["model"]
    
            if protocol == "ndi":
                ndi_bw = config.get_model_attribute(self.configs, model, "bandwidth")
                # Respecter la config : timecode NDI hardware par défaut,
                # synthetic_clock seulement si explicitement demandé.
                cs = NdiCameraStream(
                    camera_id=cam_cfg["id"],
                    name=cam_cfg["name"],
                    model=model,
                    ndi_url=cam_cfg["url"],
                    ntp_clock=self.ntp_clock,
                    buffer_duration=SYNC_BUFFER_SEC,
                    target_fps=TARGET_FPS,
                    ndi_bandwidth=ndi_bw,
                    display_delay_ms=cam_cfg.get("display_delay_ms", 0.0),
                    use_synthetic_clock=bool(cam_cfg.get("use_synthetic_clock", False)),
                )
            elif protocol == "rtsp":
                decoder_backend = config.get_model_attribute(self.configs,model, "decoder")
                hw_accel = config.get_model_attribute(self.configs,model, "hw_accel")
                cs = RtpCameraStream(
                    camera_id=cam_cfg["id"],
                    name=cam_cfg["name"],
                    model=cam_cfg["model"],
                    url=cam_cfg["url"],
                    ntp_clock=self.ntp_clock,
                    buffer_duration=SYNC_BUFFER_SEC,
                    target_fps=TARGET_FPS,
                    display_delay_ms=cam_cfg.get("display_delay_ms", 0.0),
                    flip=bool(cam_cfg.get("flip", False)),
                    decoder_backend=decoder_backend,
                    hw_hwaccel=hw_accel,
                )
            else :
                print(f"[CamSync] {cam_cfg['name']} uses a protocol other than NDI or RTSP, skipping.", flush=True)
                continue

            self.streams.append(cs)
    
        # 3. Moteur de métriques / aligneur / validateur
        self.metrics_engine = SyncMetricsEngine(self.streams, window_size=JITTER_WINDOW_SIZE)
        # Playout delay (ms) pour l'alignement UTC cible (lu depuis config.py)
        self.aligner = StreamAligner(self.streams, playout_delay_ms=PLAYOUT_DELAY_MS)
    
        # Aligneur dynamique : compense en continu les différences de latence
        # (age_last) entre flux. DÉSACTIVÉ par défaut car il égalise age_last
        # (~identique sur les 4 caméras ici) et entre en conflit avec la
        # calibration Flash (jitter résiduel). L'alignement principal se fait
        # via Flash Calib (mesure visuelle réelle, appliquée à chaud par
        # session). Le bouton "Sync auto" de la GUI permet de l'activer pour
        # expérimentation.
        self.dyn_aligner = DynamicAligner(self.streams)

        # 4. Créer et afficher la fenêtre AVANT de démarrer les flux
        print("[CamSync] Building window...", flush=True)
        if self.window is None:
            self.window = MainWindow(self)
        else:
            self.window.rebuild_grid()
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()
        self.app.processEvents()
        print("[CamSync] Window displayed.", flush=True)

        from PyQt5.QtCore import QTimer as _QT
        import threading as _threading
        self._restarting = False
        self.start_thread = _threading.Thread(
            target=self._start_streams,
            daemon=True,
            name="StreamStarter"
        )
        _QT.singleShot(100, self.start_thread.start)
    
        # Diagnostic périodique : fps réel, âge de la frame affichée, offset NTP.
        # Permet de vérifier que les Insta tournent bien à 30 fps et que le
        # display_delay est cohérent avec la latence réelle du pipeline.
        def _diag_dump():
            now = time.time()
            lines = []
            for cs in self.streams:
                fps = getattr(cs, "actual_fps", 0.0)
                delay = getattr(cs, "display_delay_sec", 0.0) * 1000.0
                dyn = getattr(cs, "dynamic_delay_sec", 0.0) * 1000.0
                calib = getattr(cs, "calib_delay_sec", 0.0) * 1000.0
                last_ntp = getattr(cs, "last_ntp_timestamp", 0.0)
                buf = cs.get_buffer_snapshot()
                buf_n = len(buf)
                # Âge (ms) de la frame la plus récente par rapport à l'horloge hôte
                age_last_ms = (now - last_ntp) * 1000.0 if last_ntp > 0 else -1
                # Frame qui sera effectivement affichée (tient compte display_delay)
                disp = cs.latest_frame()
                age_disp_ms = (now - disp.ntp_timestamp) * 1000.0 if disp else -1
                lines.append(
                    f"  {cs.name:<22} fps={fps:5.1f} buf={buf_n:3d} "
                    f"delay_cfg={delay:4.0f}ms dyn={dyn:4.0f}ms calib={calib:5.0f}ms  "
                    f"age_last={age_last_ms:6.0f}ms  "
                    f"age_displayed={age_disp_ms:6.0f}ms  src={getattr(cs,'timestamp_source','?')}"
                )
            print("[Diag] Stream status:\n" + "\n".join(lines), flush=True)
    
        self._diag_timer = _QT()
        self._diag_timer.timeout.connect(_diag_dump)
        self._diag_timer.start(5000)  # toutes les 5 s
    
        # Mise à jour de l'aligneur dynamique (≈ 1 Hz, indépendant du diag).
        self._dyn_timer = _QT()
        self._dyn_timer.timeout.connect(self.dyn_aligner.update)
        self._dyn_timer.start(1000)
        
    
    # 5. Démarrer les flux caméra dans un thread dédié (après la fenêtre)
    # Le time.sleep entre cameras NDI ne doit pas bloquer le thread UI.
    def _start_streams(self):
        for cs in self.streams:
            if self._restarting:
                print("[CamSync] Stream startup cancelled.",flush=True)
                return
            src = getattr(cs, "ndi_url", None) or getattr(cs, "url", "?")
            cs.start()
            print(f"[CamSync] {cs.name} → {src}", flush=True)
            # Laisser 3 s entre chaque démarrage NDI pour éviter
            # la concurrence de bande passante lors de la connexion.
            if hasattr(cs, "ndi_url"):
                for _ in range(30):
                    if self._restarting:
                        print("[CamSync] Stream startup cancelled.",flush=True)
                        return
                    time.sleep(0.1)

    def cleanup(self):
        # Nettoyage
        print("[CamSync] Cleaning up", flush=True)
        self._restarting = True
        if self.start_thread:
            print("Wait starter thread",flush=True)
            while self.start_thread.is_alive():
                print("[CamSync] StreamStarter still alive, Waiting...",flush=True)
                self.start_thread.join(timeout=5)      
        print("CLEANUP 1 : Streams", flush=True)
        for cs in self.streams:
            print(f"Stopping {cs.name}", flush=True)
            cs.stop()
        print("CLEANUP 2 : NTP Clock", flush=True)
        if self.ntp_clock:
            self.ntp_clock.stop()
        print("CLEANUP 3 : Metrics Engine", flush=True)
        if self.metrics_engine:
            self.metrics_engine.close()
        print("CLEANUP 4 : Timers")
        if self._diag_timer:
            self._diag_timer.stop()
        if self._dyn_timer:
            self._dyn_timer.stop()
        print("CLEANUP 5 : Clearing references")
        self.streams.clear()
        self.metrics_engine = None
        self.aligner = None
        self.dyn_aligner = None
        self.start_thread = None
        self.configs = None
        self._diag_timer = None
        self._dyn_timer = None
        print("[CamSync] Cleanup Done.", flush=True)
    
    def restart(self):
        print("========= RESTART =========")
        self.cleanup()
        self.build_runtime()

if __name__ == "__main__":
    # ── 0. QApplication DOIT être créée en premier (avant tout thread OpenCV)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    print("[CamSync] QApplication ready.", flush=True)

    mruntime = MainRuntime(app)
    mruntime.build_runtime()

    exit_code = app.exec_()

    mruntime.cleanup()

    print("BEFORE SYS.EXIT", flush=True)
    sys.exit(exit_code)