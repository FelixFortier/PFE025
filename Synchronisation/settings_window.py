from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
import config

class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.configs = config.load_config_json()

        self.setWindowTitle("Camera Configuration")
        self.resize(1000, 700)

        self.setStyleSheet("""
                    QLineEdit {
                        border: 1px solid gray;
                        border-radius: 4px;
                        padding: 4px;
                        background-color: #161b22;
                        color: white;
                    }
        
                    QLineEdit:focus {
                        border: 2px solid #58a6ff;
                    }
                """)

        layout = QVBoxLayout(self)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        self.general_tab = GeneralSettingsTab(self.configs)
        self.models_tab = ModelsTab(self.configs)
        self.cameras_tab = CamerasTab(self.configs)

        self.tabs.addTab(self.general_tab, "General")
        self.tabs.addTab(self.models_tab, "Models")
        self.tabs.addTab(self.cameras_tab, "Cameras")

        buttons = QHBoxLayout()

        self.btn_ok = QPushButton("OK")
        self.btn_apply = QPushButton("Apply")
        self.btn_cancel = QPushButton("Cancel")

        buttons.addStretch()
        buttons.addWidget(self.btn_apply)
        buttons.addWidget(self.btn_ok)
        buttons.addWidget(self.btn_cancel)

        layout.addLayout(buttons)

        self.btn_apply.clicked.connect(self.apply)
        self.btn_ok.clicked.connect(self.accept_changes)
        self.btn_cancel.clicked.connect(self.reject)

    def apply(self):
        self.general_tab.save()
        self.models_tab.save()
        self.cameras_tab.save()
        config.full_update_config_file(self.configs)

    def accept_changes(self):
        self.apply()
        self.accept()


class GeneralSettingsTab(QWidget):
    def __init__(self, configs):
        super().__init__()

        self.configs = configs

        form = QFormLayout(self)

        self.ntp_server = QLineEdit(configs["NTP_SERVER"])

        self.ntp_interval = QSpinBox()
        self.ntp_interval.setMaximum(999999)
        self.ntp_interval.setValue(configs["NTP_SYNC_INTERVAL_SEC"])

        self.target_fps = QSpinBox()
        self.target_fps.setMaximum(240)
        self.target_fps.setValue(configs["TARGET_FPS"])

        self.sync_buffer = QDoubleSpinBox()
        self.sync_buffer.setDecimals(2)
        self.sync_buffer.setMaximum(9999)
        self.sync_buffer.setValue(configs["SYNC_BUFFER_SEC"])

        self.jitter_window = QSpinBox()
        self.jitter_window.setMaximum(10000)
        self.jitter_window.setValue(configs["JITTER_WINDOW_SIZE"])

        self.playout_delay = QSpinBox()
        self.playout_delay.setMaximum(60000)
        self.playout_delay.setValue(configs["PLAYOUT_DELAY_MS"])

        form.addRow("NTP Server", self.ntp_server)
        form.addRow("NTP Interval (s)", self.ntp_interval)
        form.addRow("Target FPS", self.target_fps)
        form.addRow("Sync Buffer (s)", self.sync_buffer)
        form.addRow("Jitter Window", self.jitter_window)
        form.addRow("Playout Delay (ms)", self.playout_delay)

    def save(self):
        self.configs["NTP_SERVER"] = self.ntp_server.text()
        self.configs["NTP_SYNC_INTERVAL_SEC"] = self.ntp_interval.value()
        self.configs["TARGET_FPS"] = self.target_fps.value()
        self.configs["SYNC_BUFFER_SEC"] = self.sync_buffer.value()
        self.configs["JITTER_WINDOW_SIZE"] = self.jitter_window.value()
        self.configs["PLAYOUT_DELAY_MS"] = self.playout_delay.value()


class ModelsTab(QWidget):
    def __init__(self, configs):
        super().__init__()

        self.configs = configs
        self.current_model = None

        root = QHBoxLayout(self)

        left = QVBoxLayout()
        right = QFormLayout()

        root.addLayout(left, 1)
        root.addLayout(right, 2)

        self.list_models = QListWidget()

        self.btn_add = QPushButton("Add")
        self.btn_remove = QPushButton("Remove")

        left.addWidget(QLabel("Models"))
        left.addWidget(self.list_models)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self.btn_add)
        button_layout.addWidget(self.btn_remove)

        left.addLayout(button_layout)

        self.name = QLineEdit()
        self.bandwidth = QLineEdit()
        self.decoder = QLineEdit()
        self.hw_accel = QLineEdit()

        right.addRow("Name", self.name)
        right.addRow("Bandwidth", self.bandwidth)
        right.addRow("Decoder", self.decoder)
        right.addRow("HW Accel", self.hw_accel)

        self.populate()

        self.list_models.currentRowChanged.connect(self.load_model)
        self.btn_add.clicked.connect(self.add_model)
        self.btn_remove.clicked.connect(self.remove_model)

        if self.list_models.count():
            self.list_models.setCurrentRow(0)

    def populate(self):
        self.list_models.clear()

        for model in self.configs["models"]:
            self.list_models.addItem(model["name"])

    def load_model(self, row):
        if row < 0:
            return

        self.current_model = self.configs["models"][row]

        self.name.setText(self.current_model["name"])
        self.bandwidth.setText(self.current_model["bandwidth"])
        self.decoder.setText(self.current_model["decoder"])
        self.hw_accel.setText(self.current_model["hw_accel"])

    def add_model(self):
        model = {
            "name": "New Model",
            "bandwidth": "",
            "decoder": "",
            "hw_accel": "",
        }

        self.configs["models"].append(model)

        self.populate()
        self.list_models.setCurrentRow(self.list_models.count() - 1)

    def remove_model(self):
        row = self.list_models.currentRow()

        if row < 0:
            return

        del self.configs["models"][row]

        self.populate()

    def save(self):
        row = self.list_models.currentRow()

        if row < 0:
            return

        model = self.configs["models"][row]

        model["name"] = self.name.text()
        model["bandwidth"] = self.bandwidth.text()
        model["decoder"] = self.decoder.text()
        model["hw_accel"] = self.hw_accel.text()

        self.populate()


class CamerasTab(QWidget):
    def __init__(self, configs):
        super().__init__()

        self.configs = configs

        root = QHBoxLayout(self)

        left = QVBoxLayout()
        right = QFormLayout()

        root.addLayout(left, 1)
        root.addLayout(right, 2)

        self.list_cameras = QListWidget()

        self.btn_add = QPushButton("Add")
        self.btn_remove = QPushButton("Remove")

        left.addWidget(QLabel("Cameras"))
        left.addWidget(self.list_cameras)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self.btn_add)
        button_layout.addWidget(self.btn_remove)

        left.addLayout(button_layout)

        self.camera_id = QLineEdit()
        self.name = QLineEdit()

        self.model = QComboBox()

        self.ip = QLineEdit()

        self.protocol = QComboBox()
        self.protocol.addItems(["ndi", "rtsp"])

        self.url = QLineEdit()

        self.delay = QSpinBox()
        self.delay.setMaximum(60000)

        self.enabled = QCheckBox()
        self.flip = QCheckBox()

        right.addRow("ID", self.camera_id)
        right.addRow("Name", self.name)
        right.addRow("Model", self.model)
        right.addRow("IP", self.ip)
        right.addRow("Protocol", self.protocol)
        right.addRow("URL", self.url)
        right.addRow("Display Delay (ms)", self.delay)
        right.addRow("Enabled", self.enabled)
        right.addRow("Flip", self.flip)

        self.populate()

        self.list_cameras.currentRowChanged.connect(self.load_camera)
        self.btn_add.clicked.connect(self.add_camera)
        self.btn_remove.clicked.connect(self.remove_camera)

        if self.list_cameras.count():
            self.list_cameras.setCurrentRow(0)

    def populate(self):
        self.list_cameras.clear()

        for camera in self.configs["cameras"]:
            self.list_cameras.addItem(camera["name"])

        current_model = self.model.currentText()

        self.model.blockSignals(True)
        self.model.clear()

        for model in self.configs["models"]:
            self.model.addItem(model["name"])

        index = self.model.findText(current_model)

        if index >= 0:
            self.model.setCurrentIndex(index)

        self.model.blockSignals(False)

    def load_camera(self, row):
        if row < 0:
            return

        camera = self.configs["cameras"][row]

        self.camera_id.setText(camera["id"])
        self.name.setText(camera["name"])
        self.ip.setText(camera["ip"])

        self.protocol.setCurrentText(camera["protocol"])
        self.url.setText(camera["url"])

        self.model.setCurrentText(camera["model"])

        self.delay.setValue(camera["display_delay_ms"])

        self.enabled.setChecked(
            camera["enabled"] in [True, "True"]
        )

        self.flip.setChecked(
            camera["flip"] in [True, "True"]
        )

    def add_camera(self):
        camera = {
            "id": "new_camera",
            "name": "New Camera",
            "model": "",
            "ip": "",
            "protocol": "rtsp",
            "url": "",
            "flip": False,
            "display_delay_ms": 0,
            "enabled": True,
        }

        self.configs["cameras"].append(camera)

        self.populate()
        self.list_cameras.setCurrentRow(
            self.list_cameras.count() - 1
        )

    def remove_camera(self):
        row = self.list_cameras.currentRow()

        if row < 0:
            return

        del self.configs["cameras"][row]

        self.populate()

    def save(self):
        row = self.list_cameras.currentRow()

        if row < 0:
            return

        camera = self.configs["cameras"][row]

        camera["id"] = self.camera_id.text()
        camera["name"] = self.name.text()
        camera["model"] = self.model.currentText()
        camera["ip"] = self.ip.text()
        camera["protocol"] = self.protocol.currentText()
        camera["url"] = self.url.text()

        camera["display_delay_ms"] = self.delay.value()
        camera["enabled"] = self.enabled.isChecked()
        camera["flip"] = self.flip.isChecked()

        self.populate()


if __name__ == "__main__":
    import json
    import sys

    with open("config.json", "r") as f:
        configs = json.load(f)

    app = QApplication(sys.argv)

    dialog = SettingsDialog()

    dialog.exec_()

    sys.exit(0)