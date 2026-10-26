"""The Audio page: two switches (stream this PC's sound, hear the other PCs) and, for every paired PC,
a volume and a live level, because what the other PCs stream is mixed together here."""

from PySide6.QtCore import QObject, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget

import audio_share
import theme
import widgets
from core.i18n import tr

TICK_MS = 100
PEERS_EVERY_TICKS = 40
SAVE_AFTER_MS = 300
DECAY = 0.82


class LevelBar(QWidget):
    """A thin meter: how loud a PC is right now."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.level = 0.0
        self.setFixedHeight(6)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_level(self, level: float) -> None:
        level = max(0.0, min(1.0, level))
        if abs(level - self.level) > 0.004:
            self.level = level
            self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.colour("rule")))
        painter.drawRoundedRect(QRectF(self.rect()), 3, 3)
        if self.level > 0:
            fill = QRectF(0, 0, max(6.0, self.width() * self.level), self.height())
            painter.setBrush(QColor(theme.colour("signal")))
            painter.drawRoundedRect(fill, 3, 3)


class SourceCard(widgets.Module):
    """One PC whose sound can play here: its state, its level, its own volume."""

    def __init__(self, peer: dict, volume: int, on_volume, parent=None):
        super().__init__(peer["name"], parent)
        self.peer_id = peer["id"]
        self._on_volume = lambda *_: None  # nothing to save while the card is being built
        self._before_mute = audio_share.DEFAULT_VOLUME
        self.shown_level = 0.0

        top = QHBoxLayout()
        top.setSpacing(10)
        self.led = widgets.Led()
        top.addWidget(self.led, 0, Qt.AlignmentFlag.AlignVCenter)
        self.state = widgets.label("", "note")
        top.addWidget(self.state)
        top.addWidget(LevelBar(), 1)
        self.meter = top.itemAt(top.count() - 1).widget()
        self.body.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        self.mute = QPushButton(tr("Mute"))
        self.mute.setCheckable(True)
        self.mute.setCursor(Qt.CursorShape.PointingHandCursor)
        bottom.addWidget(self.mute)
        self.slider = widgets.Ruler(tr("Volume of {name}").format(name=peer["name"]), audio_share.MAX_VOLUME, step=5)
        self.slider.setValue(volume)
        bottom.addWidget(self.slider, 1)
        self.percent = widgets.label("", "readout")
        self.percent.setMinimumWidth(48)
        self.percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bottom.addWidget(self.percent)
        self.body.addLayout(bottom)

        self.slider.valueChanged.connect(self._moved)
        self.mute.toggled.connect(self._muted)
        self._moved(volume)
        self._on_volume = on_volume

    def _moved(self, value: int) -> None:
        self.percent.setText("%d%%" % value)
        self.mute.blockSignals(True)
        self.mute.setChecked(value == 0)
        self.mute.blockSignals(False)
        if value:
            self._before_mute = value
        self._on_volume(self.peer_id, value)

    def _muted(self, on: bool) -> None:
        self.slider.setValue(0 if on else self._before_mute)

    def show_state(self, receiving: bool, info: dict) -> None:
        playing = receiving and info.get("playing", False)
        self.led.set_tone("signal" if playing else "off")
        if not receiving:
            self.state.setText(tr("Hearing is off"))
        else:
            self.state.setText(tr("Streaming") if playing else tr("Quiet"))
        # A fast attack and a slow fall, so a short sound still shows.
        self.shown_level = max(info.get("level", 0.0) if playing else 0.0, self.shown_level * DECAY)
        self.meter.set_level(self.shown_level)


class AudioPanel(QObject):
    def __init__(self, share, layout, parent=None):
        super().__init__(parent)
        self.share = share
        self._peers = None
        self._cards = {}
        self._ticks = 0
        if share is None:
            layout.addWidget(widgets.label(tr("Audio sharing is only available on Windows."), "note", wrap=True))
            return

        main = widgets.Module(tr("This PC"))
        self.send_on = widgets.Switch(tr("Stream this PC's sound"))
        self.send_note = widgets.label(
            tr("Sends what this PC plays to every paired Windows PC that is hearing. Beamer's own sound is left out."),
            "note", wrap=True)
        self.send_status = widgets.label("", "note-quiet", wrap=True)
        self.receive_on = widgets.Switch(tr("Hear the other PCs"))
        self.receive_note = widgets.label(
            tr("Beamer plays what the other PCs stream, mixed together, on this PC's speakers."), "note", wrap=True)
        self.receive_status = widgets.label("", "note-quiet", wrap=True)
        for item in (self.send_on, self.send_note, self.send_status, widgets.rule(vertical=False),
                     self.receive_on, self.receive_note, self.receive_status):
            main.body.addWidget(item)
        layout.addWidget(main)

        self.sources_title = widgets.label(tr("Sources").upper(), "note-quiet")
        layout.addWidget(self.sources_title)
        self.sources_box = QVBoxLayout()
        self.sources_box.setSpacing(12)
        layout.addLayout(self.sources_box)
        self.empty = widgets.label(
            tr("Pair another Windows PC with this version of Beamer and it shows up here."), "note", wrap=True)
        layout.addWidget(self.empty)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_AFTER_MS)
        self._save_timer.timeout.connect(self._save)

        self.refresh()
        self.send_on.toggled.connect(self._save)
        self.receive_on.toggled.connect(self._save)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(TICK_MS)

    def refresh(self) -> None:
        """Show the saved choice and one card for each paired PC."""
        choice = audio_share.load_audio(self.share.settings_path)
        for switch, value in ((self.send_on, choice["send"]), (self.receive_on, choice["receive"])):
            switch.blockSignals(True)
            switch.setChecked(value)
            switch.blockSignals(False)
        self._peers = audio_share.paired_peers(self.share.settings_path)
        for card in self._cards.values():
            card.setParent(None)
            card.deleteLater()
        self._cards = {}
        for peer in self._peers:
            volume = self.share.volumes.get(peer["id"], choice["volumes"].get(peer["id"], audio_share.DEFAULT_VOLUME))
            self.share.set_volume(peer["id"], volume)
            card = SourceCard(peer, volume, self._volume_changed)
            self._cards[peer["id"]] = card
            self.sources_box.addWidget(card)
        self.empty.setVisible(not self._peers)
        self.sources_title.setVisible(bool(self._peers))

    def _volume_changed(self, peer_id: str, value: int) -> None:
        self.share.set_volume(peer_id, value)
        self._save_timer.start()

    def _save(self, *_args) -> None:
        audio_share.save_choice(self.share.settings_path, {
            "send": self.send_on.isChecked(), "receive": self.receive_on.isChecked(),
            "volumes": {peer_id: card.slider.value() for peer_id, card in self._cards.items()},
        })
        self.share.apply()

    def _tick(self) -> None:
        self.send_status.setText(tr(self.share.send_status) if self.share.send_status else "")
        self.receive_status.setText(tr(self.share.receive_status) if self.share.receive_status else "")
        receiving = self.receive_on.isChecked()
        sources = self.share.sources
        for peer_id, card in self._cards.items():
            card.show_state(receiving, sources.get(peer_id, {}))
        self._ticks += 1
        if self._ticks >= PEERS_EVERY_TICKS:
            self._ticks = 0
            if audio_share.paired_peers(self.share.settings_path) != self._peers:
                self.refresh()
