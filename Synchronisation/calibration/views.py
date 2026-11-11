"""
views.py – Projections, virtual views and geometry helpers.

Projections (model attribute "projection" in config.json):
  perspective         classic camera, pinhole + distortion (needs intrinsics)
  equirect            360° equirectangular panorama (stitched Insta360)
  equirect_stereo_tb  stereo panorama, left eye on top / right eye below
                      (Suometry). Both eyes are solved together.

Camera frame convention (all projections): X right, Y down, Z forward.
For a panorama, "forward" is the centre of the image (longitude 0) and
"up" is the top row.

A panorama is never searched directly: the checkerboard is detected in
virtual pinhole crops ("views": front/right/back/left/up/down) and the
detected corners are mapped back to panorama pixels, then to rays.
"""

from dataclasses import dataclass

import cv2
import numpy as np

PERSPECTIVE = "perspective"
EQUIRECT = "equirect"
EQUIRECT_STEREO_TB = "equirect_stereo_tb"
PROJECTIONS = (PERSPECTIVE, EQUIRECT, EQUIRECT_STEREO_TB)

VIEW_FOV_DEG = 120.0     # crops overlap so a board near an edge is whole in one


@dataclass(frozen=True)
class View:
    name: str
    yaw_deg: float = 0.0     # + = to the right
    pitch_deg: float = 0.0   # + = up


MAIN_VIEW = View("main")
PANO_VIEWS = (
    View("front", 0, 0), View("right", 90, 0), View("back", 180, 0),
    View("left", -90, 0), View("up", 0, 90), View("down", 0, -90),
)


def normalize_projection(p: str | None) -> str:
    return p if p in PROJECTIONS else PERSPECTIVE


def is_panorama(projection: str) -> bool:
    return projection in (EQUIRECT, EQUIRECT_STEREO_TB)


def views_for(projection: str) -> tuple[View, ...]:
    return PANO_VIEWS if is_panorama(projection) else (MAIN_VIEW,)


def view_by_name(projection: str, name: str) -> View:
    for v in views_for(projection):
        if v.name == name:
            return v
    return views_for(projection)[0]


def eyes_for(projection: str) -> tuple[str, ...]:
    return ("left", "right") if projection == EQUIRECT_STEREO_TB else ("mono",)


def eye_rows(projection: str, eye: str, frame_h: int) -> tuple[int, int]:
    """Row range [y0, y1) of the given eye in the full frame."""
    if projection == EQUIRECT_STEREO_TB:
        half = frame_h // 2
        return (0, half) if eye == "left" else (half, 2 * half)
    return (0, frame_h)


# ── rotations ────────────────────────────────────────────────────────────────

def _rot_y(deg: float) -> np.ndarray:
    a = np.radians(deg)
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def _rot_x(deg: float) -> np.ndarray:
    a = np.radians(deg)
    c, s = np.cos(a), np.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def view_rotation(view: View) -> np.ndarray:
    """Rotation view frame -> panorama frame."""
    return _rot_y(view.yaw_deg) @ _rot_x(view.pitch_deg)


# ── equirectangular pixel <-> ray ────────────────────────────────────────────

def erp_pixels_to_rays(pts: np.ndarray, w: int, h: int) -> np.ndarray:
    """(N,2) ERP pixels of one eye (row 0 = top of that eye) -> (N,3) rays."""
    u, v = pts[:, 0], pts[:, 1]
    lon = (u + 0.5) / w * 2 * np.pi - np.pi
    lat = np.pi / 2 - (v + 0.5) / h * np.pi
    return np.stack([np.cos(lat) * np.sin(lon), -np.sin(lat),
                     np.cos(lat) * np.cos(lon)], axis=1)


def rays_to_erp_pixels(rays: np.ndarray, w: int, h: int) -> np.ndarray:
    rays = rays / np.linalg.norm(rays, axis=-1, keepdims=True)
    lon = np.arctan2(rays[..., 0], rays[..., 2])
    lat = np.arcsin(np.clip(-rays[..., 1], -1, 1))
    u = (lon + np.pi) / (2 * np.pi) * w - 0.5
    v = (np.pi / 2 - lat) / np.pi * h - 0.5
    return np.stack([u, v], axis=-1)


# ── rendering ────────────────────────────────────────────────────────────────

@dataclass
class RenderGeom:
    """How pixels of a rendered view relate to the source frame."""
    projection: str
    eye: str
    scale: float = 1.0                 # perspective: view px = src px * scale
    R: np.ndarray | None = None        # panorama: view -> pano rotation
    f: float = 0.0
    c: float = 0.0
    erp_w: int = 0
    erp_h: int = 0
    row0: int = 0                      # first row of the eye in the frame
    size: tuple[int, int] = (0, 0)     # (w, h) of the rendered view

    def to_source(self, pts: np.ndarray) -> np.ndarray:
        """(N,2) view pixels -> (N,2) pixels of the full source frame."""
        pts = np.asarray(pts, np.float64).reshape(-1, 2)
        if self.R is None:
            return pts / self.scale
        rays = np.column_stack([(pts[:, 0] - self.c) / self.f,
                                (pts[:, 1] - self.c) / self.f,
                                np.ones(len(pts))]) @ self.R.T
        src = rays_to_erp_pixels(rays, self.erp_w, self.erp_h)
        src[:, 0] %= self.erp_w
        src[:, 1] += self.row0
        return src


class ViewRenderer:
    """Renders views of frames. Remap tables are cached per geometry."""

    def __init__(self, fov_deg: float = VIEW_FOV_DEG):
        self.fov_deg = fov_deg
        self._cache: dict = {}

    def render(self, frame: np.ndarray, projection: str, view: View,
               eye: str = "mono", max_side: int | None = None
               ) -> tuple[np.ndarray, RenderGeom]:
        h, w = frame.shape[:2]
        if not is_panorama(projection):
            scale = 1.0
            if max_side and max(w, h) > max_side:
                scale = max_side / max(w, h)
                img = cv2.resize(frame, (round(w * scale), round(h * scale)),
                                 interpolation=cv2.INTER_AREA)
            else:
                img = frame
            return img, RenderGeom(projection, eye, scale=scale,
                                   size=(img.shape[1], img.shape[0]))

        y0, y1 = eye_rows(projection, eye, h)
        eh = y1 - y0
        key = (w, eh, view, max_side, self.fov_deg)
        entry = self._cache.get(key)
        if entry is None:
            entry = self._build_maps(w, eh, view, max_side)
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[key] = entry
        m1, m2, R, f, c, n = entry
        img = cv2.remap(frame[y0:y1], m1, m2, cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_WRAP)
        return img, RenderGeom(projection, eye, R=R, f=f, c=c, erp_w=w,
                               erp_h=eh, row0=y0, size=(n, n))

    def _build_maps(self, w, h, view, max_side):
        t = np.tan(np.radians(self.fov_deg) / 2)
        f = w / (2 * np.pi)              # same angular density as the ERP
        n = int(round(2 * f * t))
        if max_side and n > max_side:
            n = max_side
            f = n / (2 * t)
        c = (n - 1) / 2.0
        R = view_rotation(view)
        xs, ys = np.meshgrid(np.arange(n), np.arange(n))
        rays = np.stack([(xs - c) / f, (ys - c) / f, np.ones_like(xs, float)],
                        axis=-1) @ R.T
        uv = rays_to_erp_pixels(rays, w, h).astype(np.float32)
        m1, m2 = cv2.convertMaps(uv[..., 0], uv[..., 1], cv2.CV_16SC2)
        return m1, m2, R, f, c, n


# ── source pixels -> rays ────────────────────────────────────────────────────

def source_to_rays(pts: np.ndarray, projection: str, eye: str,
                   frame_size: tuple[int, int],
                   K: np.ndarray | None = None,
                   dist: np.ndarray | None = None) -> np.ndarray:
    """(N,2) pixels of the saved frame -> (N,3) unit rays in camera frame."""
    pts = np.asarray(pts, np.float64).reshape(-1, 2)
    w, h = frame_size
    if is_panorama(projection):
        y0, y1 = eye_rows(projection, eye, h)
        local = pts.copy()
        local[:, 1] -= y0
        return erp_pixels_to_rays(local, w, y1 - y0)
    if K is None:
        raise ValueError("perspective camera needs intrinsics")
    und = cv2.undistortPoints(pts.reshape(-1, 1, 2), K,
                              None if dist is None else dist).reshape(-1, 2)
    rays = np.column_stack([und, np.ones(len(und))])
    return rays / np.linalg.norm(rays, axis=1, keepdims=True)


# ── pose from rays ───────────────────────────────────────────────────────────

def _frame_around(axis: np.ndarray) -> np.ndarray:
    """Rotation whose 3rd row is `axis` (maps axis onto +Z)."""
    z = axis / np.linalg.norm(axis)
    helper = np.array([0.0, 1.0, 0.0]) if abs(z[1]) < 0.9 else np.array([1.0, 0, 0])
    x = np.cross(helper, z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    return np.vstack([x, y, z])


def solve_pose(obj_pts: np.ndarray, rays: np.ndarray
               ) -> tuple[np.ndarray, np.ndarray, float]:
    """Board pose from 3D board points and observed unit rays.

    Works for any central camera (pinhole after undistortion, panorama).
    Returns (R, t, rms_angle_rad) with X_cam = R @ X_board + t.
    """
    obj = np.asarray(obj_pts, np.float64).reshape(-1, 3)
    rays = np.asarray(rays, np.float64).reshape(-1, 3)
    Ra = _frame_around(rays.mean(axis=0))
    r = rays @ Ra.T
    if np.any(r[:, 2] < 0.15):
        raise ValueError("pattern spans too wide an angle")
    img = (r[:, :2] / r[:, 2:3]).reshape(-1, 1, 2)
    K = np.eye(3)
    ok, rvec, tvec = cv2.solvePnP(obj.reshape(-1, 1, 3), img, K, None,
                                  flags=cv2.SOLVEPNP_IPPE)
    if not ok:
        ok, rvec, tvec = cv2.solvePnP(obj.reshape(-1, 1, 3), img, K, None,
                                      flags=cv2.SOLVEPNP_ITERATIVE)
    if not ok:
        raise ValueError("solvePnP failed")
    rvec, tvec = cv2.solvePnPRefineLM(obj.reshape(-1, 1, 3), img, K, None,
                                      rvec, tvec)
    Rr, _ = cv2.Rodrigues(rvec)
    R = Ra.T @ Rr
    t = Ra.T @ tvec.reshape(3)
    pred = obj @ R.T + t
    pred /= np.linalg.norm(pred, axis=1, keepdims=True)
    ang = np.arccos(np.clip(np.sum(pred * rays, axis=1), -1, 1))
    return R, t, float(np.sqrt(np.mean(ang ** 2)))
