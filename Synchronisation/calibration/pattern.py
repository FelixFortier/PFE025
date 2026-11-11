"""
pattern.py – Checkerboard pattern definition and detection.

Board frame convention (same as OpenCV / the old MATLAB code):
  origin = first inner corner, X along a row of `cols` corners,
  Y along a column of `rows` corners, Z pointing *into* the board.
  Units: metres.
"""

from dataclasses import dataclass, asdict

import cv2
import numpy as np


@dataclass
class PatternSpec:
    cols: int = 11            # inner corners per row
    rows: int = 7             # inner corners per column
    square_mm: float = 148.0  # side of one square

    @property
    def size(self) -> tuple[int, int]:
        return (self.cols, self.rows)

    @property
    def n_corners(self) -> int:
        return self.cols * self.rows

    def object_points(self) -> np.ndarray:
        """(N, 3) float32 board coordinates in metres."""
        s = self.square_mm / 1000.0
        grid = np.mgrid[0:self.cols, 0:self.rows].T.reshape(-1, 2)
        pts = np.zeros((self.n_corners, 3), np.float32)
        pts[:, :2] = grid * s
        return pts

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "PatternSpec":
        if not d:
            return cls()
        return cls(int(d.get("cols", 11)), int(d.get("rows", 7)),
                   float(d.get("square_mm", 148.0)))


def _to_gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img


def _canonical_order(corners: np.ndarray) -> np.ndarray:
    """Remove the 180° ordering ambiguity of a symmetric board.

    With an odd x odd number of inner corners (e.g. 11 x 7), the detector
    can return the corners starting from either end. To get the same board
    origin in every camera we keep the order whose first corner is *above*
    the last one in the image. This assumes the cameras are roughly upright
    and the board is on a wall (see README notes in parameter_engine.py).
    """
    corners = np.asarray(corners, np.float32).reshape(-1, 1, 2)
    if corners[0, 0, 1] > corners[-1, 0, 1]:
        corners = corners[::-1].copy()
    return corners


def detect_fast(img: np.ndarray, spec: PatternSpec) -> np.ndarray | None:
    """Fast detection used for the live status. Returns (N,1,2) or None."""
    gray = _to_gray(img)
    ok, corners = cv2.findChessboardCornersSB(
        gray, spec.size, cv2.CALIB_CB_NORMALIZE_IMAGE)
    if not ok or corners is None or len(corners) != spec.n_corners:
        return None
    return _canonical_order(corners.astype(np.float32))


def detect_precise(img: np.ndarray, spec: PatternSpec) -> np.ndarray | None:
    """Accurate detection used when a picture is captured.

    Falls back to the fast detector + cornerSubPix refinement when the
    accurate detector fails (e.g. motion blur).
    """
    gray = _to_gray(img)
    ok, corners = cv2.findChessboardCornersSB(
        gray, spec.size,
        cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_EXHAUSTIVE
        | cv2.CALIB_CB_ACCURACY)
    if ok and corners is not None and len(corners) == spec.n_corners:
        return _canonical_order(corners.astype(np.float32))
    hint = detect_fast(gray, spec)
    if hint is None:
        return None
    refined = hint.copy()
    cv2.cornerSubPix(
        gray, refined, (7, 7), (-1, -1),
        (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 40, 0.01))
    return _canonical_order(refined)


def draw(img: np.ndarray, spec: PatternSpec, corners: np.ndarray | None
         ) -> np.ndarray:
    """Draws the detected corners and the board outline in place."""
    if corners is None:
        return img
    cv2.drawChessboardCorners(img, spec.size, corners, True)
    c = corners.reshape(spec.rows, spec.cols, 2)
    outline = np.array([c[0, 0], c[0, -1], c[-1, -1], c[-1, 0]], np.int32)
    cv2.polylines(img, [outline], True, (80, 220, 80), 3, cv2.LINE_AA)
    cv2.circle(img, tuple(int(v) for v in c[0, 0]), 9, (0, 0, 255), -1,
               cv2.LINE_AA)  # board origin
    return img
