"""The Audio page: send this PC's sound to another PC, and play another PC's sound here, both at once."""

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QComboBox, QPushButton

import audio_share
import widgets
from core.i18n import tr

PEERS_EVERY_MS = 4000


class AudioPanel(QObject):
    def __init__(self, share, layout, parent=None):
        super().__init__(parent)
        self.share = share
        self._peers = None
        if share is None:
            layout.addWidget(widgets.label(tr("Audio sharing is only available on Windows."), "note", wrap=True))
            return

        send = widgets.Module(tr("Send this PC's sound"))
        self.send_on = widgets.Switch(tr("Send this PC's sound to another PC"))
        self.send_peer = QComboBox()
        self.send_source = QComboBox()
        self.mute = widgets.Switch(tr("Mute this PC's speakers while sending"))
        self.send_status = widgets.label("", "note", wrap=True)
        send.body.addWidget(self.send_on)
        send.body.addWidget(widgets.FormRow(widgets.label(tr("Send to"), "key"), self.send_peer))
        send.body.addWidget(widgets.FormRow(widgets.label(tr("Sound to send"), "key"), self.send_source))
        send.body.addWidget(self.mute)
        send.body.addWidget(widgets.label(tr(
            "To keep this PC quiet, use a virtual sound device (such as VB-CABLE): choose it as the Sound to send, "
            "then pick it as the output in Windows. Whatever plays on that output goes to the other PC only."),
            "note", wrap=True))
        send.body.addWidget(self.send_status)
        layout.addWidget(send)

        receive = widgets.Module(tr("Play another PC's sound here"))
        self.receive_on = widgets.Switch(tr("Play another PC's sound on this PC"))
        self.receive_peer = QComboBox()
        self.receive_output = QComboBox()
        self.receive_status = widgets.label("", "note", wrap=True)
        receive.body.addWidget(self.receive_on)
        receive.body.addWidget(widgets.FormRow(widgets.label(tr("Play sound from"), "key"), self.receive_peer))
        receive.body.addWidget(widgets.FormRow(widgets.label(tr("Play on"), "key"), self.receive_output))
        receive.body.addWidget(widgets.label(tr(
            "Send and play on different devices, or the sound loops back. Both PCs need this version of Beamer and "
            "must be paired."), "note", wrap=True))
        receive.body.addWidget(self.receive_status)
        layout.addWidget(receive)

        refresh = QPushButton(tr("Refresh devices"))
        refresh.clicked.connect(self.refresh)
        layout.addWidget(refresh, 0, Qt.AlignmentFlag.AlignLeft)

        self.refresh()
        for box in (self.send_peer, self.send_source, self.receive_peer, self.receive_output):
            box.activated.connect(self._save)
        for switch in (self.send_on, self.mute, self.receive_on):
            switch.toggled.connect(self._save)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)
        self._ticks = 0

    @staticmethod
    def _select(box: QComboBox, value) -> None:
        index = box.findData(value)
        if index >= 0:
            box.setCurrentIndex(index)

    def refresh(self) -> None:
        """Fill the lists from the paired machines and the sound devices, then show the saved choice."""
        choice = audio_share.load_audio(self.share.settings_path)
        self._peers = audio_share.paired_peers(self.share.settings_path)
        try:
            devices = audio_share.output_devices()
        except Exception:  # no PyAudioWPatch or no sound device: the lists keep the defaults
            devices = []
        boxes = (self.send_peer, self.send_source, self.receive_peer, self.receive_output)
        for box in boxes:
            box.blockSignals(True)
            box.clear()
        for peer in self._peers:
            self.send_peer.addItem(peer["name"], peer["id"])
        self.receive_peer.addItem(tr("Any paired PC"), "")
        for peer in self._peers:
            self.receive_peer.addItem(peer["name"], peer["id"])
        self.send_source.addItem(tr("Default speakers"), "")
        self.receive_output.addItem(tr("Default speakers"), "")
        for name in devices:
            self.send_source.addItem(name, name)
            self.receive_output.addItem(name, name)
        send, receive = choice["send"], choice["receive"]
        self._select(self.send_peer, send["peer"])
        self._select(self.send_source, send["source"])
        self._select(self.receive_peer, receive["peer"])
        self._select(self.receive_output, receive["output"])
        for switch, value in ((self.send_on, send["on"]), (self.mute, send["mute_local"]), (self.receive_on, receive["on"])):
            switch.blockSignals(True)
            switch.setChecked(value)
            switch.blockSignals(False)
        for box in boxes:
            box.blockSignals(False)
        self.send_peer.setEnabled(bool(self._peers))
        self._enable()

    def _enable(self) -> None:
        # Muting acts on the default speakers, so it only means something when those are what is sent.
        self.mute.setEnabled(not self.send_source.currentData())

    def _save(self, *_args) -> None:
        self._enable()
        audio_share.save_choice(self.share.settings_path, {
            "send": {
                "on": self.send_on.isChecked(), "peer": self.send_peer.currentData() or "",
                "source": self.send_source.currentData() or "",
                "mute_local": self.mute.isChecked() and self.mute.isEnabled(),
            },
            "receive": {
                "on": self.receive_on.isChecked(), "peer": self.receive_peer.currentData() or "",
                "output": self.receive_output.currentData() or "",
            },
        })
        self.share.apply()

    def _tick(self) -> None:
        self.send_status.setText(tr(self.share.send_status) if self.share.send_status else "")
        self.receive_status.setText(tr(self.share.receive_status) if self.share.receive_status else "")
        self._ticks += 1
        if self._ticks * 500 >= PEERS_EVERY_MS:
            self._ticks = 0
            if audio_share.paired_peers(self.share.settings_path) != self._peers:
                self.refresh()
