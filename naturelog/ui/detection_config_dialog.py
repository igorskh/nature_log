"""Dialog choosing the models species detection runs with."""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QWidget

from naturelog.species import AudioModel, DetectionConfig, ImageModel


class DetectionConfigDialog(QDialog):
    """Image model (BioCLIP 2) and audio model (BirdNET v3 or Perch 2)."""

    def __init__(self, config: DetectionConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Species detection models")

        self._image = QComboBox()
        for model in ImageModel:
            self._image.addItem(model.label, model)
        self._image.setCurrentIndex(self._image.findData(config.image_model))
        self._image.setToolTip("Model that identifies species in photos")

        self._audio = QComboBox()
        for model in AudioModel:
            self._audio.addItem(model.label, model)
        self._audio.setCurrentIndex(self._audio.findData(config.audio_model))
        self._audio.setToolTip("Model that identifies species in recordings")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QFormLayout(self)
        layout.addRow("Images:", self._image)
        layout.addRow("Audio:", self._audio)
        layout.addRow(buttons)

    def config(self) -> DetectionConfig:
        return DetectionConfig(
            ImageModel(self._image.currentData()), AudioModel(self._audio.currentData())
        )
