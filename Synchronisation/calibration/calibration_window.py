"""
calibration_window.py – Operator window of the geometric calibration.

Layout
  top    : camera list (green = pattern seen, red = not seen, grey = no
           frame / unknown; panoramas have a view dropdown) | live preview
           of the selected camera/view with the detected board | capture
           panel (mode, Capture button, detection status, pattern size)
  bottom : intrinsic pictures | extrinsic captures | results, with the
           Calibrate Intrinsic / Calibrate Extrinsic buttons
"""

from __future__ import annotations

import threading

import cv2
from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QRadioButton, QSizePolicy, QSpinBox,
    QSplitter, QVBoxLayout, QWidget,
)

from .parameter_engine import MIN_INTRINSIC_PICTURES, ParameterFindingEngine
from .pattern import PatternSpec
from .views import views_for

_BG, _BG2, _BORDER = "#0d1117", "#161b22", "#30363d"
_TEXT, _TEXT2 = "#c9d1d9", "#8b949e"
_GREEN, _RED, _GREY, _BLUE = "#3fb950", "#f85149", "#484f58", "#58a6ff"

_BTN = """
QPushButton {{ background-color: {bg}; color: white; border: none;
              border-radius: 4px; font-size: 11px; font-weight: bold;
              padding: 6px 12px; }}
QPushButton:hover {{ background-color: {hover}; }}
QPushButton:disabled {{ background-color: #444; color: #888; }}
"""


class _Bridge(QObject):
    """Brings results of worker threads back to the Qt thread."""
    done = pyqtSignal(object, object)


# ══════════════════════════════════════════════════════════════════════════════
#  Camera list entry
# ══════════════════════════════════════════════════════════════════════════════

class CameraItem(QFrame):
    clicked = pyqtSignal(str)
    view_changed = pyqtSignal(str, str)

    def __init__(self, camera_id: str, name: str, projection: str, parent=None):
        super().__init__(parent)
        self.camera_id = camera_id
        self.projection = projection
        self._state = "unknown"
        self._selected = False
        self.setCursor(Qt.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(4)
        self.lbl = QLabel(name)
        self.lbl.setStyleSheet(f"color:{_TEXT}; font-weight:bold; border:none;")
        lay.addWidget(self.lbl)
        self.lbl_info = QLabel(projection)
        self.lbl_info.setStyleSheet(f"color:{_TEXT2}; font-size:9px; border:none;")
        lay.addWidget(self.lbl_info)

        self.combo: QComboBox | None = None
        views = views_for(projection)
        if len(views) > 1:
            self.combo = QComboBox()
            self.combo.setStyleSheet(
                f"QComboBox {{ background:{_BG}; color:{_TEXT}; "
                f"border:1px solid {_BORDER}; padding:2px; }}")
            for v in views:
                self.combo.addItem(v.name, v.name)
            self.combo.currentIndexChanged.connect(self._on_combo)
            lay.addWidget(self.combo)
        self._restyle()

    @property
    def view_name(self) -> str:
        return self.combo.currentData() if self.combo else "main"

    def _on_combo(self, _):
        self.view_changed.emit(self.camera_id, self.view_name)

    def mousePressEvent(self, ev):
        self.clicked.emit(self.camera_id)
        super().mousePressEvent(ev)

    def set_selected(self, sel: bool):
        self._selected = sel
        self._restyle()

    def set_state(self, state: str, views: dict[str, bool]):
        self._state = state
        if self.combo:
            for i in range(self.combo.count()):
                name = self.combo.itemData(i)
                mark = "  ●" if views.get(name) else ""
                self.combo.setItemText(i, name + mark)
        self._restyle()

    def _restyle(self):
        color = {"found": _GREEN, "not_found": _RED}.get(self._state, _GREY)
        bg = "#1f2a37" if self._selected else _BG2
        self.setStyleSheet(
            f"CameraItem {{ background:{bg}; border:2px solid {color}; "
            f"border-radius:6px; }}")


# ══════════════════════════════════════════════════════════════════════════════
#  Window
# ══════════════════════════════════════════════════════════════════════════════

class CalibrationWindow(QDialog):

    def __init__(self, streams, projections: dict[str, str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Calibration")
        self.setWindowFlags(self.windowFlags() | Qt.WindowMinMaxButtonsHint)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.resize(1400, 900)
        self.setStyleSheet(f"""
            QDialog {{ background:{_BG}; }}
            QLabel {{ color:{_TEXT}; }}
            QListWidget, QPlainTextEdit {{ background:{_BG2}; color:{_TEXT};
                border:1px solid {_BORDER}; }}
            QRadioButton {{ color:{_TEXT}; }}
            QSpinBox, QDoubleSpinBox {{ background:{_BG2}; color:{_TEXT};
                border:1px solid {_BORDER}; padding:2px; }}
        """)

        self.engine = ParameterFindingEngine(streams, projections)
        self._items: dict[str, CameraItem] = {}
        self._selected: str | None = None
        self._busy = False
        self._preview_found = False
        self._bridge = _Bridge()
        self._bridge.done.connect(lambda cb, res: cb(res))

        self._build_ui()
        if self.engine.cameras:
            self._select(self.engine.cameras[0].camera_id)
        self._refresh_lists()
        self._refresh_results()

        self.engine.start()
        self._timer_preview = QTimer(self)
        self._timer_preview.timeout.connect(self._refresh_preview)
        self._timer_preview.start(100)
        self._timer_status = QTimer(self)
        self._timer_status.timeout.connect(self._refresh_status)
        self._timer_status.start(500)

    # ── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        split = QSplitter(Qt.Vertical)
        root.addWidget(split)

        # ── top ──
        top = QWidget()
        tl = QHBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)

        cams = QVBoxLayout()
        cams.setSpacing(6)
        hdr = QLabel("Cameras")
        hdr.setStyleSheet(f"color:{_TEXT2}; font-weight:bold;")
        cams.addWidget(hdr)
        for cam in self.engine.cameras:
            it = CameraItem(cam.camera_id, cam.name, cam.projection)
            it.clicked.connect(self._select)
            it.view_changed.connect(self._on_view_changed)
            self._items[cam.camera_id] = it
            cams.addWidget(it)
        if not self.engine.cameras:
            cams.addWidget(QLabel("No camera running"))
        cams.addStretch()
        cams_w = QWidget()
        cams_w.setLayout(cams)
        cams_w.setFixedWidth(200)
        tl.addWidget(cams_w)

        self.preview = QLabel("No frame")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(480, 360)
        self.preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview.setStyleSheet(f"background:#000; color:{_TEXT2}; "
                                   f"border:1px solid {_BORDER};")
        tl.addWidget(self.preview, 1)

        side = QVBoxLayout()
        side.setSpacing(10)
        lbl_mode = QLabel("Capture mode")
        lbl_mode.setStyleSheet(f"color:{_TEXT2}; font-weight:bold;")
        side.addWidget(lbl_mode)
        self.rb_intr = QRadioButton("Intrinsic (selected camera)")
        self.rb_extr = QRadioButton("Extrinsic (all cameras)")
        self.rb_extr.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.rb_intr)
        grp.addButton(self.rb_extr)
        grp.buttonClicked.connect(lambda _: self._update_buttons())
        side.addWidget(self.rb_intr)
        side.addWidget(self.rb_extr)

        self.btn_capture = QPushButton("◉  Capture")
        self.btn_capture.setMinimumHeight(48)
        self.btn_capture.setStyleSheet(_BTN.format(bg="#238636", hover=_GREEN))
        self.btn_capture.clicked.connect(self._on_capture)
        side.addWidget(self.btn_capture)

        self.lbl_detect = QLabel("")
        self.lbl_detect.setWordWrap(True)
        self.lbl_detect.setStyleSheet("font-weight:bold;")
        side.addWidget(self.lbl_detect)

        hint = QLabel(
            "Intrinsic: 10+ pictures per perspective camera, board tilted "
            "and moved over the whole image.\n\n"
            "Extrinsic: keep the board still (on a wall) for all captures; "
            "its first corner (red dot) is the origin.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{_TEXT2}; font-size:10px;")
        side.addWidget(hint)

        side.addStretch()
        form = QFormLayout()
        self.sp_cols = QSpinBox(); self.sp_cols.setRange(3, 40)
        self.sp_rows = QSpinBox(); self.sp_rows.setRange(3, 40)
        self.sp_square = QDoubleSpinBox(); self.sp_square.setRange(1, 1000)
        self.sp_square.setDecimals(1); self.sp_square.setSuffix(" mm")
        p = self.engine.pattern
        self.sp_cols.setValue(p.cols)
        self.sp_rows.setValue(p.rows)
        self.sp_square.setValue(p.square_mm)
        for sp in (self.sp_cols, self.sp_rows, self.sp_square):
            sp.valueChanged.connect(self._on_pattern_changed)
        form.addRow("Inner corners X", self.sp_cols)
        form.addRow("Inner corners Y", self.sp_rows)
        form.addRow("Square size", self.sp_square)
        side.addLayout(form)

        side_w = QWidget()
        side_w.setLayout(side)
        side_w.setFixedWidth(230)
        tl.addWidget(side_w)
        split.addWidget(top)

        # ── bottom ──
        bottom = QWidget()
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)

        def _column(title, lst, buttons):
            col = QVBoxLayout()
            t = QLabel(title)
            t.setStyleSheet(f"color:{_TEXT2}; font-weight:bold;")
            col.addWidget(t)
            col.addWidget(lst, 1)
            row = QHBoxLayout()
            for b in buttons:
                row.addWidget(b)
            col.addLayout(row)
            w = QWidget()
            w.setLayout(col)
            return w

        self.list_intr = QListWidget()
        self.list_intr.setSelectionMode(QListWidget.ExtendedSelection)
        self.btn_del_intr = QPushButton("Delete selected")
        self.btn_del_intr.setStyleSheet(_BTN.format(bg="#555", hover="#666"))
        self.btn_del_intr.clicked.connect(lambda: self._delete(self.list_intr))
        self.btn_cal_intr = QPushButton("Calibrate Intrinsic")
        self.btn_cal_intr.setStyleSheet(_BTN.format(bg="#1f6feb", hover=_BLUE))
        self.btn_cal_intr.clicked.connect(self._on_calibrate_intrinsic)
        bl.addWidget(_column("Intrinsic pictures", self.list_intr,
                             [self.btn_del_intr, self.btn_cal_intr]), 1)

        self.list_extr = QListWidget()
        self.list_extr.setSelectionMode(QListWidget.ExtendedSelection)
        self.btn_del_extr = QPushButton("Delete selected")
        self.btn_del_extr.setStyleSheet(_BTN.format(bg="#555", hover="#666"))
        self.btn_del_extr.clicked.connect(lambda: self._delete(self.list_extr))
        self.btn_cal_extr = QPushButton("Calibrate Extrinsic")
        self.btn_cal_extr.setStyleSheet(_BTN.format(bg="#1f6feb", hover=_BLUE))
        self.btn_cal_extr.clicked.connect(self._on_calibrate_extrinsic)
        bl.addWidget(_column("Extrinsic captures", self.list_extr,
                             [self.btn_del_extr, self.btn_cal_extr]), 1)

        self.txt_results = QPlainTextEdit()
        self.txt_results.setReadOnly(True)
        self.txt_results.setStyleSheet(
            f"background:{_BG2}; color:{_TEXT}; font-family:'Courier New'; "
            "font-size:11px;")
        self.txt_results.setLineWrapMode(QPlainTextEdit.NoWrap)
        lbl_path = QLabel(str(self.engine.json_path))
        lbl_path.setStyleSheet(f"color:{_TEXT2}; font-size:9px;")
        lbl_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bl.addWidget(_column("Parameters", self.txt_results, [lbl_path]), 1)

        split.addWidget(bottom)
        split.setSizes([600, 300])

    # ── selection ───────────────────────────────────────────────────────────

    def _select(self, camera_id: str):
        self._selected = camera_id
        for cid, it in self._items.items():
            it.set_selected(cid == camera_id)
        self.engine.set_focus(camera_id, self._items[camera_id].view_name)
        self._preview_found = False
        self._update_buttons()

    def _on_view_changed(self, camera_id: str, _view: str):
        self._select(camera_id)

    # ── timers ──────────────────────────────────────────────────────────────

    def _refresh_preview(self):
        if not self._selected:
            return
        it = self._items[self._selected]
        img, found = self.engine.preview(self._selected, it.view_name)
        self._preview_found = found
        if img is None:
            self.preview.setPixmap(QPixmap())
            self.preview.setText("No frame")
        else:
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            h, w = rgb.shape[:2]
            qimg = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
            pm = QPixmap.fromImage(qimg).scaled(
                self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.preview.setPixmap(pm)
        self._update_buttons()

    def _refresh_status(self):
        for cid, it in self._items.items():
            it.set_state(self.engine.camera_state(cid),
                         self.engine.view_states(cid))

    def _update_buttons(self):
        cam = self.engine.camera(self._selected) if self._selected else None
        intr_mode = self.rb_intr.isChecked()
        can = bool(cam) and self._preview_found and not self._busy
        if cam and intr_mode and cam.is_panorama:
            can = False
            msg, color = "Panoramas need no intrinsic calibration", _TEXT2
        elif self._busy:
            msg, color = "Working…", _TEXT2
        elif self._preview_found:
            msg, color = "Pattern detected", _GREEN
        else:
            msg, color = "No pattern detected", _RED
        if cam and intr_mode and not cam.is_panorama:
            msg += (f"\n{self.engine.intrinsic_count(cam.camera_id)}/"
                    f"{MIN_INTRINSIC_PICTURES} intrinsic pictures")
        self.btn_capture.setEnabled(can)
        self.lbl_detect.setText(msg)
        self.lbl_detect.setStyleSheet(f"font-weight:bold; color:{color};")

        ip, ep = self.engine.intrinsic_plan(), self.engine.extrinsic_plan()
        self.btn_cal_intr.setEnabled(bool(ip.used) and not self._busy)
        self.btn_cal_extr.setEnabled(bool(ep.used) and not self._busy)
        self.btn_cal_intr.setToolTip(self._plan_text(ip))
        self.btn_cal_extr.setToolTip(self._plan_text(ep))
        lock = self.engine.can_change_pattern()
        for sp in (self.sp_cols, self.sp_rows, self.sp_square):
            sp.setEnabled(lock and not self._busy)

    # ── background work ─────────────────────────────────────────────────────

    def _run(self, fn, on_done):
        self._busy = True
        self._update_buttons()

        def work():
            try:
                res = fn()
            except Exception as exc:  # reported in the Qt thread
                res = exc
            self._bridge.done.emit(self._finish(on_done), res)

        threading.Thread(target=work, daemon=True, name="CalibWork").start()

    def _finish(self, on_done):
        def cb(res):
            # an exception escaping a Qt slot aborts the whole app (PyQt5)
            try:
                self._busy = False
                self._refresh_lists()
                self._update_buttons()
                if isinstance(res, Exception):
                    raise res
                on_done(res)
            except Exception as exc:
                QMessageBox.critical(self, "Calibration", f"Error: {exc!r}")
        return cb

    # ── capture ─────────────────────────────────────────────────────────────

    def _on_capture(self):
        cid = self._selected
        if self.rb_intr.isChecked():
            def done(cap):
                if cap is None:
                    QMessageBox.warning(self, "Capture",
                                        "The pattern could not be detected "
                                        "precisely. Try again.")
            self._run(lambda: self.engine.capture_intrinsic(cid), done)
        else:
            def done(caps):
                if not caps:
                    QMessageBox.warning(self, "Capture",
                                        "No camera saw the pattern precisely.")
            self._run(self.engine.capture_extrinsic, done)

    def _refresh_lists(self):
        self.list_intr.clear()
        self.list_extr.clear()
        groups: dict[str, list] = {}
        for cap in self.engine.captures:
            if cap.mode == "intrinsic":
                it = QListWidgetItem(f"{cap.camera_name}   {cap.id}")
                it.setData(Qt.UserRole, [cap.id])
                self.list_intr.addItem(it)
            else:
                groups.setdefault(cap.group or cap.id, []).append(cap)
        for i, (gid, caps) in enumerate(groups.items(), 1):
            names = ", ".join(
                c.camera_name + (f" ({c.detections[0].view})"
                                 if c.detections[0].view != "main" else "")
                for c in caps)
            it = QListWidgetItem(f"#{i}  {gid}   {len(caps)} cam: {names}")
            it.setData(Qt.UserRole, [c.id for c in caps])
            self.list_extr.addItem(it)

    def _delete(self, lst: QListWidget):
        items = lst.selectedItems()
        if not items:
            return
        if QMessageBox.question(
                self, "Delete", f"Delete {len(items)} item(s) and their pictures?"
        ) != QMessageBox.Yes:
            return
        for it in items:
            for cap_id in it.data(Qt.UserRole):
                self.engine.delete_capture(cap_id)
        self._refresh_lists()
        self._update_buttons()

    # ── pattern ─────────────────────────────────────────────────────────────

    def _on_pattern_changed(self, *_):
        self.engine.set_pattern(PatternSpec(
            self.sp_cols.value(), self.sp_rows.value(), self.sp_square.value()))
        self.engine.save()

    # ── calibration ─────────────────────────────────────────────────────────

    @staticmethod
    def _plan_text(plan) -> str:
        lines = ["Will be calibrated:"]
        lines += [f"  ✓ {n}  ({d})" for n, d in plan.used] or ["  (none)"]
        if plan.skipped:
            lines.append("\nSkipped:")
            lines += [f"  ✗ {n}  ({r})" for n, r in plan.skipped]
        return "\n".join(lines)

    def _confirm(self, title, plan) -> bool:
        return QMessageBox.question(
            self, title, self._plan_text(plan) + "\n\nContinue?"
        ) == QMessageBox.Yes

    def _on_calibrate_intrinsic(self):
        if not self._confirm("Calibrate Intrinsic", self.engine.intrinsic_plan()):
            return
        self._run(self.engine.calibrate_intrinsics,
                  lambda res: self._show_results("Intrinsic results", res))

    def _on_calibrate_extrinsic(self):
        if not self._confirm("Calibrate Extrinsic", self.engine.extrinsic_plan()):
            return
        self._run(self.engine.calibrate_extrinsics,
                  lambda res: self._show_results("Extrinsic results", res))

    def _name(self, cid):
        cam = self.engine.camera(cid)
        return cam.name if cam else cid

    def _show_results(self, title, res: dict):
        lines = []
        for cid, r in res.items():
            if "error" in r:
                lines.append(f"{self._name(cid)}: ERROR {r['error']}")
            elif "position_m" in r:
                x, y, z = r["position_m"]
                lines.append(f"{self._name(cid)}: position ({x:.3f}, {y:.3f}, "
                             f"{z:.3f}) m, error {r['rms_px']:.2f} px")
                if "warning" in r:
                    lines.append(f"    ⚠ {r['warning']}")
            else:
                lines.append(f"{self._name(cid)}: f = {r['focal_px'][0]:.1f} px, "
                             f"error {r['rms_px']:.2f} px "
                             f"({r['n_pictures']} pictures)")
        QMessageBox.information(self, title, "\n".join(lines) or "Nothing done.")
        self._refresh_results()

    def _refresh_results(self):
        out = []
        for cid, r in self.engine.results.items():
            out.append(f"■ {r.get('name', cid)}  [{r.get('projection', '')}]")
            intr = r.get("intrinsics")
            if intr and "K" in intr:
                K = intr["K"]
                out.append(f"  f = {K[0][0]:.1f}, {K[1][1]:.1f} px"
                           f"   c = {K[0][2]:.1f}, {K[1][2]:.1f}")
                out.append("  dist = " + ", ".join(f"{d:.4f}" for d in intr["dist"]))
                if "rms_px" in intr:
                    out.append(f"  rms {intr['rms_px']:.2f} px, "
                               f"{intr.get('n_pictures', '?')} pictures, "
                               f"{intr.get('calibrated_at', '')}")
            ext = r.get("extrinsics")
            if ext:
                x, y, z = ext["position_m"]
                out.append(f"  position = ({x:.3f}, {y:.3f}, {z:.3f}) m"
                           f"   dist to board {ext['distance_to_board_m']:.2f} m")
                out.append(f"  rms {ext['rms_px']:.2f} px, {len(ext['captures'])} capture(s),"
                           f" {ext['calibrated_at']}")
                if "warning" in ext:
                    out.append(f"  ⚠ {ext['warning']}")
            out.append("")
        self.txt_results.setPlainText("\n".join(out) or "No parameters yet.")

    # ── close ───────────────────────────────────────────────────────────────

    def reject(self):          # Esc key: close for real (stops the engine)
        self.close()

    def closeEvent(self, ev):
        self._timer_preview.stop()
        self._timer_status.stop()
        self.engine.stop()
        super().closeEvent(ev)
