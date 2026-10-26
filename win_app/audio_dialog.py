"""The Audio sharing window: send this PC's sound to another PC, or play another PC's sound here."""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QDialogButtonBox, QLabel, QRadioButton, QVBoxLayout

import audio_share
from core.i18n import tr


class AudioDialog(QDialog):
    def __init__(self, share: "audio_share.AudioShare", parent=None):
        super().__init__(parent)
        self.share = share
        self.setWindowTitle(tr("Audio sharing"))
        saved = audio_share.load_audio(share.settings_path)
        self.peers = audio_share.paired_peers(share.settings_path)
        layout = QVBoxLayout(self)

        self.off = QRadioButton(tr("Off"))
        self.send = QRadioButton(tr("Send this PC's sound to another PC"))
        self.receive = QRadioButton(tr("Play another PC's sound on this PC"))
        self.target = QComboBox()
        for peer in self.peers:
            self.target.addItem(peer["name"], peer["id"])
        self.target.setEnabled(bool(self.peers))
        self.source = QComboBox()
        self.source.addItem(tr("Any paired PC"), "")
        for peer in self.peers:
            self.source.addItem(peer["name"], peer["id"])
        self.mute = QCheckBox(tr("Mute this PC's speakers while sending"))
        self.mute.setChecked(saved["mute_local"])
        self.output = QComboBox()
        self.output.addItem(tr("Default speakers"), "")
        try:
            for name in audio_share.output_devices():
                self.output.addItem(name, name)
        except Exception:  # no PyAudioWPatch or no sound device: the list stays on the default
            pass
        self._select(self.target, saved["peer"])
        self._select(self.source, saved["peer"])
        self._select(self.output, saved["output"])

        layout.addWidget(self.off)
        layout.addWidget(self.send)
        layout.addWidget(self.target)
        layout.addWidget(self.mute)
        layout.addWidget(self.receive)
        layout.addWidget(QLabel(tr("Play sound from")))
        layout.addWidget(self.source)
        layout.addWidget(QLabel(tr("Play on")))
        layout.addWidget(self.output)
        note = QLabel(tr(
            "Both PCs need Beamer with audio sharing and must be paired. Without the tick above, this PC keeps "
            "playing its own sound too. A PC either sends or plays, never both."))
        note.setWordWrap(True)
        layout.addWidget(note)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        {"send": self.send, "receive": self.receive}.get(saved["mode"], self.off).setChecked(True)
        for radio in (self.off, self.send, self.receive):
            radio.toggled.connect(self._enable)
        self._enable()
        self._tick = QTimer(self)
        self._tick.timeout.connect(lambda: self.status.setText(tr(self.share.status) if self.share.status else ""))
        self._tick.start(500)

    @staticmethod
    def _select(box, value) -> None:
        index = box.findData(value)
        if index >= 0:
            box.setCurrentIndex(index)

    def _enable(self) -> None:
        self.target.setEnabled(self.send.isChecked() and bool(self.peers))
        self.mute.setEnabled(self.send.isChecked())
        self.source.setEnabled(self.receive.isChecked())
        self.output.setEnabled(self.receive.isChecked())

    def _save(self) -> None:
        if self.send.isChecked():
            audio_share.save_audio(
                self.share.settings_path, "send", self.target.currentData() or "", mute_local=self.mute.isChecked())
        elif self.receive.isChecked():
            audio_share.save_audio(
                self.share.settings_path, "receive", self.source.currentData() or "", self.output.currentData() or "")
        else:
            audio_share.save_audio(self.share.settings_path, "off")
        self.share.apply()
        self.accept()
