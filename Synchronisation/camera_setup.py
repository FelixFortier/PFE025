from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QVBoxLayout,
    QFormLayout,
    QLineEdit,
    QPushButton,
    QDialogButtonBox
)

class CameraSetup(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle("Camera Configuration")
        self.resize(350, 240)

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

        self.id_edit = QLineEdit()
        self.name_edit = QLineEdit()

        self.model_edit = QComboBox()
        self.model_edit.addItems([
            "Insta360 Pro2",
            "Ottica 4K",
            "Suometry",
            "Other..."
        ])
        self.model_edit.currentTextChanged.connect(self._on_model_changed)

        self.ip_edit = QLineEdit()
        self.url_edit = QLineEdit()
        self.flip_edit = QLineEdit()
        self.display_delay_edit = QLineEdit()

        form = QFormLayout()
        form.addRow("ID:", self.id_edit)
        form.addRow("Name:", self.name_edit)
        form.addRow("Model:", self.model_edit)
        form.addRow("IP:", self.ip_edit)
        form.addRow("URL:", self.url_edit)
        form.addRow("Flip:", self.flip_edit)
        form.addRow("Display Delay (ms):", self.display_delay_edit)

        self.flip_edit.setText("False")
        self.display_delay_edit.setText("0")

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _on_model_changed(self, text):
        if text != "Other...":
            return

        md = ModelDialog(self)
        
        if md.exec():
            values = md.values()

            self.model_edit.blockSignals(True)

            index = self.model_edit.count() - 1  # before "Other..."
            self.model_edit.insertItem(index, values["name"])
            self.model_edit.setCurrentIndex(index)

            self.model_edit.blockSignals(False)

    def values(self):
        return {
            "id": self.id_edit.text(),
            "name": self.name_edit.text(),
            "model": self.model_edit.currentText(),
            "ip": self.ip_edit.text(),
            "url": self.url_edit.text(),
            "flip": self.flip_edit.text(),
            "display_delay_ms": self.display_delay_edit.text()
            }


class ModelDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle("Model Configuration")
        self.resize(350, 240)

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

        self.name_edit = QLineEdit()
        self.bandwidth_edit = QLineEdit()
        self.decoder_edit = QLineEdit()
        self.hw_accel_edit = QLineEdit()

        form = QFormLayout()
        form.addRow("Name:", self.name_edit)
        form.addRow("Bandwith:", self.bandwidth_edit)
        form.addRow("Decoder:", self.decoder_edit)
        form.addRow("Hardware Acceleration:", self.hw_accel_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def values(self):
        return {
            "name": self.name_edit.text(),
            "bandwidth": self.bandwidth_edit.text(),
            "decoder": self.decoder_edit.text(),
            "hw_accel": self.hw_accel_edit.text(),
            }

if __name__ == "__main__":
    import sys
    from PyQt5.QtWidgets import QApplication

    app = QApplication(sys.argv)

    dlg = CameraSetup()
    result = dlg.exec()

    if result:
        print(dlg.camera_data())

    sys.exit(0)