"""Audio sharing between paired Windows machines: each machine can stream what it plays to all the
others, and play, mixed together, what the others stream.

Kept apart from the input link on purpose: its own UDP port, its own file (audio.json beside
settings.json), and the pairing tokens read from settings.json, never written. Each datagram is
sealed with a key derived from the pair's token, so only a paired machine is heard.

Beamer plays what it receives itself, one stream per source machine, so Windows mixes them, and
each source has its own volume. What a machine streams leaves Beamer's own sound out (see
audio_loopback), otherwise the sound it plays for others would be sent straight back to them.

Needs PyAudioWPatch (to play). Without it the feature reports why and stays off.
"""

import json
import logging
import os
import socket
import struct
import threading
import time
import warnings
from array import array
from pathlib import Path

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    try:
        import audioop  # removed in Python 3.13, where the audioop-lts package provides it
    except ImportError:
        audioop = None

from nacl.exceptions import CryptoError
from nacl.secret import SecretBox
from nacl.utils import random as random_bytes

import audio_loopback
from core import protocol

LOGGER = logging.getLogger("beamer.audio")

PORT = 24822
FILE_NAME = "audio.json"

# 5 ms of 16-bit stereo at 48 kHz is 960 bytes: with the 56 bytes of sealing it fits one Ethernet
# frame with room to spare, so nothing is fragmented.
CHUNK_DIVISOR = 200
SLICE_BYTES = 960
KEY_ID_SIZE = 16
NONCE_SIZE = SecretBox.NONCE_SIZE
HEADER = struct.Struct(">IIII")  # session, sequence, sample rate, chunk length in bytes
SEALED_MIN = KEY_ID_SIZE + NONCE_SIZE + SecretBox.MACBYTES + HEADER.size
MAX_DATAGRAM = 4096
PRIME_MS = 40  # buffered before playing starts, to ride out network jitter
MAX_MS = 200  # buffered at most; older audio is dropped so a stall never becomes lag
REFRESH_SECONDS = 5.0
IDLE_SECONDS = 1.5  # a source silent this long is shown as quiet and its speakers are freed
DEFAULT_VOLUME = 100
MAX_VOLUME = 150  # percent


def audio_key(token: str) -> bytes:
    return protocol.hkdf_sha256(token.encode("ascii"), b"beamer-audio-v1", b"key")


def audio_key_id(token: str) -> bytes:
    return protocol.hkdf_sha256(token.encode("ascii"), b"beamer-audio-v1", b"id")[:KEY_ID_SIZE]


def seal(token: str, session: int, sequence: int, rate: int, pcm: bytes) -> bytes:
    nonce = random_bytes(NONCE_SIZE)
    sealed = SecretBox(audio_key(token)).encrypt(HEADER.pack(session, sequence, rate, len(pcm)) + pcm, nonce).ciphertext
    return audio_key_id(token) + nonce + sealed


def unseal(token: str, datagram: bytes):
    """(session, sequence, rate, pcm) from a datagram sealed with this token, else None."""
    if len(datagram) < SEALED_MIN or len(datagram) > MAX_DATAGRAM:
        return None
    try:
        plain = SecretBox(audio_key(token)).decrypt(
            datagram[KEY_ID_SIZE + NONCE_SIZE:], datagram[KEY_ID_SIZE:KEY_ID_SIZE + NONCE_SIZE])
    except CryptoError:
        return None
    session, sequence, rate, length = HEADER.unpack_from(plain)
    pcm = plain[HEADER.size:]
    if length != len(pcm) or length % 4 or not 8000 <= rate <= 192000:
        return None
    return session, sequence, rate, pcm


def to_stereo(data: bytes, channels: int) -> bytes:
    """16-bit PCM of any channel count as stereo: the first two channels, or the one doubled."""
    if channels == 2:
        return data
    samples = array("h")
    samples.frombytes(data)
    frames = len(samples) // channels
    out = array("h", bytes(frames * 4))
    out[0::2] = samples[0:frames * channels:channels]
    out[1::2] = samples[min(1, channels - 1):frames * channels:channels]
    return out.tobytes()


def scaled(pcm: bytes, percent: int) -> bytes:
    """16-bit PCM at `percent` of its volume, clipped rather than wrapped."""
    if percent == 100:
        return pcm
    if audioop is not None:
        return audioop.mul(pcm, 2, percent / 100)
    samples = array("h")
    samples.frombytes(pcm)
    factor = percent / 100
    return array("h", (max(-32768, min(32767, int(v * factor))) for v in samples)).tobytes()


def peak(pcm: bytes) -> float:
    """The loudest sample of 16-bit PCM, 0 to 1."""
    if audioop is not None:
        return audioop.max(pcm, 2) / 32768
    samples = array("h")
    samples.frombytes(pcm)
    return max((abs(v) for v in samples), default=0) / 32768


def settings_dir(settings_path) -> Path:
    return Path(settings_path).parent


def load_audio(settings_path) -> dict:
    """The saved choice, forgiving of a missing or damaged file: a machine starts with audio off.
    `volumes` is each source machine's volume in percent, by machine id."""
    try:
        raw = json.loads((settings_dir(settings_path) / FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    volumes = raw.get("volumes") if isinstance(raw.get("volumes"), dict) else {}
    return {
        "send": raw.get("send") is True,
        "receive": raw.get("receive") is True,
        "volumes": {
            str(key): max(0, min(MAX_VOLUME, int(value))) for key, value in volumes.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        },
    }


def save_choice(settings_path, choice: dict) -> None:
    target = settings_dir(settings_path) / FILE_NAME
    temporary = target.with_name(FILE_NAME + ".tmp")
    temporary.write_text(json.dumps(choice), encoding="utf-8")
    os.replace(temporary, target)


def paired_peers(settings_path) -> list:
    """Peers that can carry audio: paired by code (a typed token has no key), with a known address."""
    try:
        raw = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        entries = raw.get("peers", [])
    except (OSError, ValueError, AttributeError):
        return []
    peers = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("from_1_4") or entry.get("platform") != "windows":
            continue
        token = entry.get("token")
        if isinstance(entry.get("id"), str) and entry["id"] and protocol.is_paired_token(token):
            peers.append({
                "id": entry["id"], "name": str(entry.get("label") or entry.get("name") or entry["id"][:8]),
                "host": str(entry.get("host") or ""), "token": token,
            })
    return peers


def _nothing() -> None:
    pass


def _pyaudio():
    import pyaudiowpatch  # imported late: the rest of Beamer works without it

    return pyaudiowpatch


class Player:
    """Plays 16-bit stereo PCM fed to it, with a small buffer that smooths out the network."""

    def __init__(self, pa, rate: int):
        self.rate = rate
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._primed = False
        self._module = pa
        self._pa = pa.PyAudio()
        self._stream = None
        try:
            self._stream = self._open(pa)
        except Exception:
            self._pa.terminate()
            raise

    def _open(self, pa):
        # The default output at the sender's rate. WASAPI shared mode only takes the device's own rate;
        # if that differs, the default host API (MME) lets Windows convert.
        index = self._pa.get_host_api_info_by_type(pa.paWASAPI)["defaultOutputDevice"]
        for device in (index, None):
            try:
                return self._pa.open(
                    format=pa.paInt16, channels=2, rate=self.rate, output=True, output_device_index=device,
                    frames_per_buffer=self.rate // CHUNK_DIVISOR * 2, stream_callback=self._pull)
            except OSError:
                if device is None:
                    raise
                LOGGER.info("WASAPI would not play %d Hz stereo; trying the default device", self.rate)

    def _pull(self, _in_data, frames, _time_info, _status):
        need = frames * 4
        with self._lock:
            if not self._primed and len(self._buffer) >= self.rate * 4 * PRIME_MS // 1000:
                self._primed = True
            if self._primed and len(self._buffer) >= need:
                data = bytes(self._buffer[:need])
                del self._buffer[:need]
            else:
                self._primed = False
                data = bytes(need)
        return data, self._module.paContinue

    def feed(self, pcm: bytes) -> None:
        with self._lock:
            self._buffer += pcm
            limit = self.rate * 4 * MAX_MS // 1000
            if len(self._buffer) > limit:
                del self._buffer[:len(self._buffer) - limit // 3]

    def close(self) -> None:
        try:
            self._stream.stop_stream()
            self._stream.close()
        finally:
            self._pa.terminate()


class _Source:
    """One machine whose sound is playing here."""

    def __init__(self, peer_id: str, name: str):
        self.peer_id = peer_id
        self.name = name
        self.player = None
        self.session = None
        self.sequence = 0
        self.heard = 0.0
        self.level = 0.0


class AudioShare:
    """Runs the sending and receiving sides the saved choice asks for, and follows changes to it."""

    SIDES = ("send", "receive")

    def __init__(self, settings_path):
        self.settings_path = Path(settings_path)
        self.send_status = ""
        self.receive_status = ""
        # peer id -> {"name", "playing", "level"} for the window; filled by the receiving thread.
        self.sources = {}
        self.volumes = {}
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._thread = None
        # Streaming the plain mix of the speakers (no way to leave Beamer's own sound out) and whether this PC hears.
        self._fallback = False
        self._receiving = False
        self._applied = {side: None for side in self.SIDES}
        self._teardowns = {side: _nothing for side in self.SIDES}
        self._workers = {}  # side -> the thread that does its work, to notice one that died

    @property
    def status(self) -> str:
        return " · ".join(text for text in (self.send_status, self.receive_status) if text)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="beamer-audio", daemon=True)
            self._thread.start()

    def apply(self) -> None:
        """Pick up a changed choice now rather than at the next refresh."""
        self._wake.set()

    def set_volume(self, peer_id: str, percent: int) -> None:
        """A source's volume takes effect at once; the window saves it with save_choice."""
        self.volumes[peer_id] = max(0, min(MAX_VOLUME, int(percent)))

    def stop(self) -> None:
        self._stopped.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(3)

    def _run(self) -> None:
        while not self._stopped.is_set():
            self._wake.clear()
            self._reconcile()
            self._wake.wait(REFRESH_SECONDS)
        for side in self.SIDES:
            self._teardowns[side]()

    def _reconcile(self) -> None:
        choice = load_audio(self.settings_path)
        peers = paired_peers(self.settings_path)
        self.volumes.update({k: v for k, v in choice["volumes"].items() if k not in self.volumes})
        targets = tuple((p["host"], p["token"]) for p in peers if p["host"])
        self._receiving = choice["receive"]
        if not choice["send"]:
            wanted_send = ("off",)
        elif not targets:
            wanted_send = ("send-missing",)
        else:
            # Whether Beamer is also hearing only matters when its own sound cannot be left out of the stream.
            wanted_send = ("send", targets, choice["receive"] and self._fallback)
        wanted_receive = ("receive", tuple((p["id"], p["token"]) for p in peers)) if choice["receive"] else ("off",)
        for side, wanted in (("send", wanted_send), ("receive", wanted_receive)):
            try:
                self._apply_side(side, wanted, peers)
            except Exception as error:
                LOGGER.exception("Audio %s failed", side)
                setattr(self, side + "_status", str(error) or error.__class__.__name__)
                self._teardowns[side]()
                self._teardowns[side] = _nothing
                self._applied[side] = None

    def _apply_side(self, side: str, wanted: tuple, peers: list) -> None:
        if wanted == self._applied[side]:
            worker = self._workers.get(side)
            if worker is None or worker.is_alive():
                return
            LOGGER.warning("Audio %s stopped by itself; starting it again", side)
        self._teardowns[side]()
        self._teardowns[side] = _nothing
        self._workers.pop(side, None)
        self._applied[side] = wanted
        status = ""
        kind = wanted[0]
        if kind == "send-missing":
            status = "Pair another Windows PC to stream to"
        elif kind == "send":
            self._teardowns[side] = self._start_sending(wanted[1])
            status = "Streaming this PC's sound to %d PC%s" % (len(wanted[1]), "" if len(wanted[1]) == 1 else "s")
        elif kind == "receive":
            self._teardowns[side] = self._start_receiving(peers)
            status = "Listening for other PCs"
        setattr(self, side + "_status", status)

    def _start_sending(self, targets: tuple):
        self._fallback = False
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        session = struct.unpack(">I", random_bytes(4))[0]
        sequence = [0]

        def send_chunk(pcm: bytes, rate: int = audio_loopback.RATE) -> None:
            for start in range(0, len(pcm), SLICE_BYTES):
                piece = pcm[start:start + SLICE_BYTES]
                sequence[0] = (sequence[0] + 1) & 0xFFFFFFFF
                for host, token in targets:
                    try:
                        sock.sendto(seal(token, session, sequence[0], rate, piece), (host, PORT))
                    except OSError:
                        pass  # that PC is away or the network is down; the next slice tries again

        capture = audio_loopback.LoopbackCapture(send_chunk) if audio_loopback.available() else None
        try:
            if capture is not None:
                capture.start()
                if getattr(capture, "thread", None) is not None:
                    self._workers["send"] = capture.thread
                return lambda: (capture.stop(), sock.close())
        except Exception as error:
            LOGGER.warning("Could not leave Beamer's own sound out of the stream: %s", error)
        if self._receiving:
            sock.close()
            raise RuntimeError(
                "This Windows cannot leave Beamer's own sound out of what it streams. Turn off Hear other PCs to stream.")
        self._fallback = True
        return self._start_plain_loopback(sock, send_chunk)

    def _start_plain_loopback(self, sock, send_chunk):
        """The whole mix of the default speakers, for a Windows that cannot leave Beamer out."""
        pa = _pyaudio()
        player = pa.PyAudio()
        try:
            loopback = player.get_default_wasapi_loopback()
            rate, channels = int(loopback["defaultSampleRate"]), int(loopback["maxInputChannels"])

            def push(data, _frames, _time_info, _status):
                send_chunk(to_stereo(data, channels), rate)
                return None, pa.paContinue

            stream = player.open(
                format=pa.paInt16, channels=channels, rate=rate, input=True, input_device_index=loopback["index"],
                frames_per_buffer=rate // CHUNK_DIVISOR, stream_callback=push)
        except Exception:
            sock.close()
            player.terminate()
            raise

        def teardown():
            try:
                stream.stop_stream()
                stream.close()
                player.terminate()
            finally:
                sock.close()

        return teardown

    def _start_receiving(self, peers: list):
        accepted = {audio_key_id(p["token"]): p for p in peers}

        def bind():
            fresh = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                fresh.bind(("", PORT))
            except OSError:
                fresh.close()
                raise
            fresh.settimeout(0.5)
            return fresh

        holder = [bind()]
        stop = threading.Event()
        pa = _pyaudio()
        self.sources = {p["id"]: {"name": p["name"], "playing": False, "level": 0.0} for p in peers}

        def take(heard, datagram, now):
            peer = accepted.get(datagram[:KEY_ID_SIZE])
            opened = unseal(peer["token"], datagram) if peer else None
            if opened is None:
                return
            session, sequence, rate, pcm = opened
            source = heard.setdefault(peer["id"], _Source(peer["id"], peer["name"]))
            try:
                if source.player is not None and source.session == session and sequence <= source.sequence:
                    return  # a repeat or a late datagram
                if source.player is None or source.player.rate != rate:
                    if source.player is not None:
                        source.player.close()
                        source.player = None
                    source.player = Player(pa, rate)
                    LOGGER.info("Playing sound from %s at %d Hz", peer["name"], rate)
                source.session, source.sequence, source.heard = session, sequence, now
                source.level = peak(pcm)
                percent = self.volumes.get(peer["id"], DEFAULT_VOLUME)
                if percent:
                    source.player.feed(scaled(pcm, percent))
            except Exception as error:
                # This source is dropped and tried again on its next datagram; the others play on.
                LOGGER.exception("Could not play the sound from %s", peer["name"])
                self.receive_status = "Could not play sound: " + (str(error) or error.__class__.__name__)
                player, source.player = source.player, None
                try:
                    if player is not None:
                        player.close()
                except Exception:
                    pass
                return
            self._publish(heard)

        def listen():
            heard = {}  # peer id -> _Source
            try:
                while not stop.is_set():
                    try:
                        datagram = holder[0].recv(MAX_DATAGRAM)
                    except socket.timeout:
                        datagram = None
                    except OSError as error:
                        if stop.is_set():
                            break
                        if getattr(error, "winerror", 0) not in (10040, 10054):
                            # The network changed under the socket: listen again on a fresh one.
                            LOGGER.warning("Audio receive error %s; listening again", error)
                            try:
                                holder[0].close()
                                holder[0] = bind()
                            except OSError:
                                stop.wait(1.0)
                        continue  # an oversize datagram or an ICMP reset: drop it, keep listening
                    now = time.monotonic()
                    for source in list(heard.values()):
                        if now - source.heard > IDLE_SECONDS and source.player is not None:
                            try:
                                source.player.close()
                            except Exception:
                                LOGGER.exception("Could not close the sound of %s", source.name)
                            source.player = None
                            source.level = 0.0
                            self._publish(heard)
                    if datagram is not None:
                        take(heard, datagram, now)
            except Exception as error:
                LOGGER.exception("Audio listening stopped")
                self.receive_status = "Could not play sound: " + (str(error) or error.__class__.__name__)
            finally:
                for source in heard.values():
                    if source.player is not None:
                        try:
                            source.player.close()
                        except Exception:
                            pass

        worker = threading.Thread(target=listen, name="beamer-audio-in", daemon=True)
        worker.start()
        self._workers["receive"] = worker

        def teardown():
            stop.set()
            holder[0].close()
            worker.join(3)
            self.sources = {}

        return teardown

    def _publish(self, heard: dict) -> None:
        """What the window shows: who is playing, and how loud each is."""
        sources = {peer_id: dict(info, playing=False, level=0.0) for peer_id, info in self.sources.items()}
        for peer_id, source in heard.items():
            sources[peer_id] = {"name": source.name, "playing": source.player is not None, "level": source.level}
        self.sources = sources
        names = [info["name"] for info in sources.values() if info["playing"]]
        self.receive_status = "Playing from " + ", ".join(names) if names else "Listening for other PCs"
