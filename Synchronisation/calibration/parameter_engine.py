"""
parameter_engine.py – ParameterFindingEngine.

Finds intrinsic and extrinsic parameters of the cameras already streamed
by the app, from a checkerboard shown to them. No Qt dependency.

Workflow
--------
* A low-priority background loop checks every camera for the pattern
  (~1 Hz per camera, faster for the camera/view selected in the UI).
* Intrinsic captures (perspective cameras only): many pictures of one
  camera, board tilted in different ways -> focal length, principal point
  and distortion (cv2.calibrateCamera, 3 radial coeffs, no tangential:
  same model as the old MATLAB code).
* Extrinsic captures: one click saves a frame from every camera that sees
  the board. The board must stay STILL (ideally on a wall) during all
  extrinsic captures. Every camera's pose is expressed in the board frame.
* Panoramas need no intrinsics. Stereo panoramas (Suometry) use both eyes
  together, which mostly cancels the omnistereo offset of each eye.

Board frame: origin = first inner corner (red dot in the preview), X along
a row of `cols` corners, Y along a column, Z into the board. Metres.
Odd x odd boards (11 x 7) are symmetric: the corner order is chosen so the
origin is the corner that appears *higher* in the image, which assumes
roughly upright cameras and a board on a wall.

Everything is saved in calibration_data/calibration.json, with pictures in
calibration_data/images/<camera_id>/{intrinsic,extrinsic}/.
"""

from __future__ import annotations

import ctypes
import datetime
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from . import pattern as pat
from .pattern import PatternSpec
from .views import (PERSPECTIVE, ViewRenderer, eyes_for, is_panorama,
                    normalize_projection, solve_pose, source_to_rays,
                    view_by_name, views_for)

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "calibration_data"

MIN_INTRINSIC_PICTURES = 10
DETECT_MAX_SIDE = 960         # resolution of the preview / focus detection
STATUS_MAX_SIDE = 720         # resolution of the background status check
CAMERA_PERIOD_S = 1.0         # background check period per camera
FOCUS_PERIOD_S = 0.3          # check period of the selected camera/view
STATUS_TTL_S = 3.0            # a detection older than this is "unknown"
SPREAD_WARN_M = 0.05          # warn if captures disagree by more than this


def _lower_thread_priority():
    """Best effort: make the detection threads yield to the decoders."""
    try:
        if os.name == "nt":
            k32 = ctypes.windll.kernel32
            k32.SetThreadPriority(k32.GetCurrentThread(), -1)  # BELOW_NORMAL
        else:
            os.nice(5)
    except Exception:
        pass


def _now_id() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


# ══════════════════════════════════════════════════════════════════════════════
#  Data
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Detection:
    eye: str               # "mono" | "left" | "right"
    view: str              # view in which it was found
    points: np.ndarray     # (N,2) pixels of the saved frame

    def to_dict(self):
        return {"eye": self.eye, "view": self.view,
                "points": np.round(self.points, 3).tolist()}

    @classmethod
    def from_dict(cls, d):
        return cls(d["eye"], d["view"], np.asarray(d["points"], np.float64))


@dataclass
class Capture:
    id: str
    mode: str              # "intrinsic" | "extrinsic"
    camera_id: str
    camera_name: str
    image: str             # path relative to the data dir
    image_size: tuple[int, int]
    timestamp: float
    detections: list[Detection] = field(default_factory=list)
    group: str = ""        # extrinsic: id of the click that made it

    def to_dict(self):
        return {"id": self.id, "mode": self.mode, "camera_id": self.camera_id,
                "camera_name": self.camera_name, "image": self.image,
                "image_size": list(self.image_size),
                "timestamp": self.timestamp, "group": self.group,
                "detections": [d.to_dict() for d in self.detections]}

    @classmethod
    def from_dict(cls, d):
        return cls(d["id"], d["mode"], d["camera_id"],
                   d.get("camera_name", d["camera_id"]), d["image"],
                   tuple(d["image_size"]), d.get("timestamp", 0.0),
                   [Detection.from_dict(x) for x in d.get("detections", [])],
                   d.get("group", ""))


@dataclass
class ViewStatus:
    found: bool
    corners: np.ndarray | None     # in the coordinates of the render below
    t: float
    size: int = 0                  # long side of the render it was found in


@dataclass
class CameraEntry:
    stream: object
    camera_id: str
    name: str
    projection: str
    last_check: float = 0.0
    has_frame: bool = False
    views: dict[str, ViewStatus] = field(default_factory=dict)

    @property
    def is_panorama(self) -> bool:
        return is_panorama(self.projection)


@dataclass
class CalibPlan:
    used: list[tuple[str, str]]      # (camera name, detail)
    skipped: list[tuple[str, str]]   # (camera name, reason)


# ══════════════════════════════════════════════════════════════════════════════
#  Engine
# ══════════════════════════════════════════════════════════════════════════════

class ParameterFindingEngine:

    def __init__(self, streams, projections: dict[str, str],
                 data_dir: Path | str = DEFAULT_DATA_DIR):
        self.data_dir = Path(data_dir)
        self.json_path = self.data_dir / "calibration.json"
        self.cameras: list[CameraEntry] = [
            CameraEntry(s, s.camera_id, s.name,
                        normalize_projection(projections.get(s.camera_id)))
            for s in streams
        ]
        self._by_id = {c.camera_id: c for c in self.cameras}

        self.pattern = PatternSpec()
        self.captures: list[Capture] = []
        self.results: dict[str, dict] = {}     # camera_id -> parameters
        self._lock = threading.RLock()
        self._renderer = ViewRenderer()
        self._focus: tuple[str, str] | None = None
        self._last_focus_check = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self.load()

    # ── persistence ─────────────────────────────────────────────────────────

    def load(self):
        if not self.json_path.exists():
            return
        try:
            data = json.loads(self.json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[Calib] cannot read {self.json_path}: {exc}", flush=True)
            return
        with self._lock:
            self.pattern = PatternSpec.from_dict(data.get("pattern"))
            self.captures = [Capture.from_dict(c)
                             for c in data.get("captures", [])
                             if (self.data_dir / c["image"]).exists()]
            self.results = data.get("cameras", {})

    def save(self):
        with self._lock:
            data = {
                "version": 1,
                "updated": datetime.datetime.now().isoformat(timespec="seconds"),
                "units": "metres",
                "board_frame": "origin = first inner corner (red dot), X along "
                               "a row of 'cols' corners, Y along a column, "
                               "Z into the board",
                "camera_frame": "X right, Y down, Z forward (panorama: "
                                "forward = image centre, up = top row)",
                "pattern": self.pattern.to_dict(),
                "cameras": self.results,
                "captures": [c.to_dict() for c in self.captures],
            }
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.json_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.json_path)

    # ── pattern ─────────────────────────────────────────────────────────────

    def can_change_pattern(self) -> bool:
        return not self.captures

    def set_pattern(self, spec: PatternSpec) -> bool:
        with self._lock:
            if self.captures:
                return False
            self.pattern = spec
            for cam in self.cameras:
                cam.views.clear()
        return True

    # ── background detection ────────────────────────────────────────────────

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._pool = ThreadPoolExecutor(2, thread_name_prefix="CalibDetect",
                                        initializer=_lower_thread_priority)
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="CalibScheduler")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def set_focus(self, camera_id: str | None, view_name: str | None = None):
        cam = self._by_id.get(camera_id) if camera_id else None
        if cam is None:
            self._focus = None
            return
        view = view_by_name(cam.projection, view_name or "").name
        self._focus = (camera_id, view)
        self._last_focus_check = 0.0

    def _loop(self):
        _lower_thread_priority()
        pending: dict = {}
        while not self._stop.is_set():
            now = time.monotonic()
            # selected camera/view
            if self._focus and now - self._last_focus_check >= FOCUS_PERIOD_S \
                    and "focus" not in pending:
                cid, vname = self._focus
                cam = self._by_id[cid]
                pending["focus"] = self._pool.submit(
                    self._check, cam, [view_by_name(cam.projection, vname)],
                    DETECT_MAX_SIDE)
                self._last_focus_check = now
            # every camera, round robin, one at a time so that the focus
            # check never waits behind a queue of background checks
            if "bg" not in pending:
                due = [c for c in self.cameras
                       if now - c.last_check >= CAMERA_PERIOD_S]
                if due:
                    cam = min(due, key=lambda c: c.last_check)
                    cam.last_check = now
                    pending["bg"] = self._pool.submit(
                        self._check, cam, list(views_for(cam.projection)),
                        STATUS_MAX_SIDE)
            for k in [k for k, f in pending.items() if f.done()]:
                exc = pending.pop(k).exception()
                if exc is not None:
                    print(f"[Calib] detection error: {exc!r}", flush=True)
            self._stop.wait(0.03)

    def _latest(self, cam: CameraEntry) -> np.ndarray | None:
        try:
            tf = cam.stream.latest_frame()
        except Exception:
            return None
        return None if tf is None or tf.frame is None else tf.frame

    def _check(self, cam: CameraEntry, views, max_side: int):
        frame = self._latest(cam)
        cam.has_frame = frame is not None
        if frame is None:
            return
        eye = eyes_for(cam.projection)[0]   # live status uses the first eye
        spec = self.pattern
        for view in views:
            if self._stop.is_set():
                return
            img, _ = self._renderer.render(frame, cam.projection, view, eye,
                                           max_side)
            corners = pat.detect_fast(img, spec)
            cam.views[view.name] = ViewStatus(corners is not None, corners,
                                              time.monotonic(),
                                              max(img.shape[:2]))

    # ── status for the UI ───────────────────────────────────────────────────

    def camera(self, camera_id: str) -> CameraEntry | None:
        return self._by_id.get(camera_id)

    def camera_state(self, camera_id: str) -> str:
        """'no_frame' | 'unknown' | 'found' | 'not_found'."""
        cam = self._by_id[camera_id]
        if not cam.has_frame and cam.last_check:
            return "no_frame"
        fresh = self._fresh_views(cam)
        if fresh is None:
            return "unknown"
        return "found" if any(fresh.values()) else "not_found"

    def view_states(self, camera_id: str) -> dict[str, bool]:
        return self._fresh_views(self._by_id[camera_id]) or {}

    def _fresh_views(self, cam) -> dict[str, bool] | None:
        now = time.monotonic()
        st = {n: s.found for n, s in list(cam.views.items())
              if now - s.t <= STATUS_TTL_S}
        return st or None

    def preview(self, camera_id: str, view_name: str
                ) -> tuple[np.ndarray | None, bool]:
        """Rendered view of the latest frame with the detection overlay."""
        cam = self._by_id[camera_id]
        frame = self._latest(cam)
        if frame is None:
            return None, False
        view = view_by_name(cam.projection, view_name)
        img, _ = self._renderer.render(frame, cam.projection, view,
                                       eyes_for(cam.projection)[0],
                                       DETECT_MAX_SIDE)
        img = img.copy()
        st = cam.views.get(view.name)
        found = bool(st and st.found and time.monotonic() - st.t <= 1.0)
        if found:
            corners = st.corners * (max(img.shape[:2]) / st.size)
            pat.draw(img, self.pattern, corners.astype(np.float32))
        return img, found

    # ── captures ────────────────────────────────────────────────────────────

    def intrinsic_count(self, camera_id: str) -> int:
        return sum(1 for c in self.captures
                   if c.mode == "intrinsic" and c.camera_id == camera_id)

    def capture_intrinsic(self, camera_id: str) -> Capture | None:
        """Picture of one perspective camera. Returns None if no pattern."""
        cam = self._by_id[camera_id]
        if cam.is_panorama:
            raise ValueError("panoramas have no intrinsic calibration")
        frame = self._latest(cam)
        if frame is None:
            return None
        frame = frame.copy()
        corners = pat.detect_precise(frame, self.pattern)
        if corners is None:
            return None
        return self._store(cam, "intrinsic", frame,
                           [Detection("mono", "main", corners.reshape(-1, 2))])

    def capture_extrinsic(self) -> list[Capture]:
        """One frame from every camera that sees the pattern."""
        group = _now_id()
        # snapshot frames and live states first: detection below takes a
        # few seconds and the states would expire meanwhile
        frames = {c.camera_id: self._latest(c) for c in self.cameras}
        states = {c.camera_id: self._fresh_views(c) for c in self.cameras}
        out = []
        for cam in self.cameras:
            frame = frames[cam.camera_id]
            if frame is None:
                continue
            frame = frame.copy()
            dets = self._detect_full(cam, frame, states[cam.camera_id])
            if dets:
                out.append(self._store(cam, "extrinsic", frame, dets, group))
        return out

    def _detect_full(self, cam: CameraEntry, frame,
                     states: dict[str, bool] | None) -> list[Detection]:
        spec = self.pattern
        if not cam.is_panorama:
            c = pat.detect_precise(frame, spec)
            return [] if c is None else [Detection("mono", "main", c.reshape(-1, 2))]
        # views where the live check saw the board, plus views not checked
        # recently (unknown); none at all -> the camera does not see it
        states = states or {}
        names = [v.name for v in views_for(cam.projection)
                 if states.get(v.name, True)]
        if self._focus and self._focus[0] == cam.camera_id \
                and self._focus[1] not in names:
            names.append(self._focus[1])
        if not names:
            return []
        dets = []
        for eye in eyes_for(cam.projection):
            best = None
            for name in names:
                view = view_by_name(cam.projection, name)
                img, geom = self._renderer.render(frame, cam.projection, view,
                                                  eye, None)
                c = pat.detect_precise(img, spec)
                if c is None:
                    continue
                # keep the view where the board is the most central
                cpx = c.reshape(-1, 2)
                d = np.linalg.norm(cpx.mean(axis=0) - geom.c)
                if best is None or d < best[0]:
                    best = (d, Detection(eye, name, geom.to_source(cpx)))
            if best:
                dets.append(best[1])
        return dets

    def _store(self, cam, mode, frame, dets, group="") -> Capture:
        cid = _now_id()
        rel = Path("images") / cam.camera_id / mode / f"{cid}.jpg"
        path = self.data_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        cap = Capture(cid, mode, cam.camera_id, cam.name, rel.as_posix(),
                      (frame.shape[1], frame.shape[0]), time.time(), dets,
                      group)
        with self._lock:
            self.captures.append(cap)
        self.save()
        return cap

    def delete_capture(self, capture_id: str):
        with self._lock:
            cap = next((c for c in self.captures if c.id == capture_id), None)
            if cap is None:
                return
            self.captures.remove(cap)
        try:
            (self.data_dir / cap.image).unlink()
        except OSError:
            pass
        self.save()

    # ── intrinsic calibration ───────────────────────────────────────────────

    def intrinsic_plan(self) -> CalibPlan:
        used, skipped = [], []
        for cam in self.cameras:
            if cam.is_panorama:
                continue
            n = self.intrinsic_count(cam.camera_id)
            if n >= MIN_INTRINSIC_PICTURES:
                used.append((cam.name, f"{n} pictures"))
            else:
                skipped.append((cam.name, f"{n}/{MIN_INTRINSIC_PICTURES} pictures"))
        return CalibPlan(used, skipped)

    def calibrate_intrinsics(self) -> dict[str, dict]:
        """Calibrates every perspective camera with enough pictures."""
        out = {}
        for cam in self.cameras:
            if cam.is_panorama or \
                    self.intrinsic_count(cam.camera_id) < MIN_INTRINSIC_PICTURES:
                continue
            try:
                out[cam.camera_id] = self._calibrate_one_intrinsic(cam)
            except Exception as exc:
                out[cam.camera_id] = {"error": str(exc)}
                continue
            with self._lock:
                self.results.setdefault(cam.camera_id, {}).update(
                    {"name": cam.name, "projection": cam.projection,
                     "intrinsics": out[cam.camera_id]})
        self.save()
        return out

    def _calibrate_one_intrinsic(self, cam) -> dict:
        caps = [c for c in self.captures
                if c.mode == "intrinsic" and c.camera_id == cam.camera_id]
        # use the most common resolution only
        sizes = [c.image_size for c in caps]
        size = max(set(sizes), key=sizes.count)
        caps = [c for c in caps if c.image_size == size]
        obj = [self.pattern.object_points()] * len(caps)
        img = [c.detections[0].points.reshape(-1, 1, 2).astype(np.float32)
               for c in caps]
        flags = cv2.CALIB_ZERO_TANGENT_DIST
        rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(
            obj, img, tuple(size), None, None, flags=flags)
        per_view = []
        for o, p, rv, tv in zip(obj, img, rvecs, tvecs):
            proj, _ = cv2.projectPoints(o, rv, tv, K, dist)
            per_view.append(float(np.sqrt(np.mean(
                np.sum((proj - p) ** 2, axis=2)))))
        newK, _ = cv2.getOptimalNewCameraMatrix(K, dist, tuple(size), 0,
                                                tuple(size))
        return {
            "image_size": list(size),
            "K": K.tolist(),
            "dist": dist.ravel().tolist(),          # k1 k2 p1 p2 k3
            "K_undistorted": newK.tolist(),
            "focal_px": [float(K[0, 0]), float(K[1, 1])],
            "principal_point_px": [float(K[0, 2]), float(K[1, 2])],
            "rms_px": float(rms),
            "per_picture_rms_px": dict(zip([c.id for c in caps], per_view)),
            "n_pictures": len(caps),
            "calibrated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }

    # ── extrinsic calibration ───────────────────────────────────────────────

    def _intrinsics_for(self, cam, size) -> tuple[np.ndarray, np.ndarray] | None:
        intr = self.results.get(cam.camera_id, {}).get("intrinsics")
        if not intr or "K" not in intr:
            return None
        K = np.array(intr["K"], np.float64)
        dist = np.array(intr["dist"], np.float64)
        w0, h0 = intr["image_size"]
        w, h = size
        if (w, h) != (w0, h0):
            if abs(w / h - w0 / h0) > 1e-3:
                return None
            s = w / w0
            K = K.copy()
            K[:2] *= s
        return K, dist

    def _extrinsic_caps(self, cam) -> list[Capture]:
        return [c for c in self.captures
                if c.mode == "extrinsic" and c.camera_id == cam.camera_id
                and c.detections]

    def extrinsic_plan(self) -> CalibPlan:
        used, skipped = [], []
        for cam in self.cameras:
            caps = self._extrinsic_caps(cam)
            if not caps:
                skipped.append((cam.name, "pattern not seen in any capture"))
                continue
            if not cam.is_panorama and \
                    self._intrinsics_for(cam, caps[0].image_size) is None:
                skipped.append((cam.name, "no intrinsic calibration for this "
                                          "resolution"))
                continue
            used.append((cam.name, f"{len(caps)} capture(s)"))
        return CalibPlan(used, skipped)

    def calibrate_extrinsics(self) -> dict[str, dict]:
        out = {}
        for cam in self.cameras:
            caps = self._extrinsic_caps(cam)
            if not caps:
                continue
            try:
                res = self._calibrate_one_extrinsic(cam, caps)
            except Exception as exc:
                out[cam.camera_id] = {"error": str(exc)}
                continue
            if res is None:
                continue
            out[cam.camera_id] = res
            with self._lock:
                self.results.setdefault(cam.camera_id, {}).update(
                    {"name": cam.name, "projection": cam.projection,
                     "extrinsics": res})
        self.save()
        return out

    def _rays_for(self, cam, cap: Capture, det: Detection):
        K = dist = None
        if not cam.is_panorama:
            kd = self._intrinsics_for(cam, cap.image_size)
            if kd is None:
                return None
            K, dist = kd
        return source_to_rays(det.points, cam.projection, det.eye,
                              cap.image_size, K, dist)

    def _calibrate_one_extrinsic(self, cam, caps) -> dict | None:
        obj1 = self.pattern.object_points()
        all_obj, all_rays, per_cap = [], [], []
        for cap in caps:
            c_obj, c_rays = [], []
            for det in cap.detections:
                rays = self._rays_for(cam, cap, det)
                if rays is None:
                    return None
                c_obj.append(obj1)
                c_rays.append(rays)
            all_obj += c_obj
            all_rays += c_rays
            per_cap.append((cap.id, np.vstack(c_obj), np.vstack(c_rays)))

        R, t, rms = solve_pose(np.vstack(all_obj), np.vstack(all_rays))
        centre = -R.T @ t

        # consistency between captures (did the board move?)
        positions = {}
        for cid, o, r in per_cap:
            try:
                Rc, tc, _ = solve_pose(o, r)
                positions[cid] = (-Rc.T @ tc).tolist()
            except ValueError:
                pass
        spread = 0.0
        if len(positions) > 1:
            P = np.array(list(positions.values()))
            spread = float(np.max(np.linalg.norm(P - P.mean(axis=0), axis=1)))

        if cam.is_panorama:
            w = caps[0].image_size[0]
            px_per_rad = w / (2 * np.pi)
        else:
            px_per_rad = float(self._intrinsics_for(cam, caps[0].image_size)[0][0, 0])

        res = {
            "R_board_to_cam": R.tolist(),
            "t_board_to_cam_m": t.tolist(),
            "R_cam_to_board": R.T.tolist(),
            "position_m": centre.tolist(),     # camera centre, board frame
            "distance_to_board_m": float(np.linalg.norm(centre)),
            "rms_deg": float(np.degrees(rms)),
            "rms_px": float(rms * px_per_rad),
            "n_points": int(sum(len(r) for r in all_rays)),
            "captures": [c.id for c in caps],
            "eyes": sorted({d.eye for c in caps for d in c.detections}),
            "per_capture_position_m": positions,
            "capture_spread_m": spread,
            "calibrated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        if spread > SPREAD_WARN_M:
            res["warning"] = (f"captures disagree by {spread * 100:.1f} cm: "
                              "the board probably moved between captures")
        return res
