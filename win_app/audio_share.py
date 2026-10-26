"""Audio sharing between paired Windows machines: one machine sends what its speakers play, the
other plays it on its own speakers.

Kept apart from the input link on purpose: its own UDP port, its own file (audio.json beside
settings.json), and the pairing tokens read from settings.json, never written. Each datagram is
sealed with a key derived from the pair's token, so only a paired machine is heard.

A machine is in one mode: "off", "send" (its sound goes to one peer) or "receive" (it plays a
peer's sound). Never both: a machine that plays a peer's sound and sends its own would echo it
back for ever, since the sender captures whatever its speakers play.

Needs PyAudioWPatch (WASAPI loopback). Without it the feature reports why and stays off.
"""

import json
import logging
import os
import socket
import struct
import threading
import time
from array import array
from pathlib import Path

from nacl.exceptions import CryptoError
from nacl.secret import SecretBox
from nacl.utils import random as random_bytes

from core import protocol

LOGGER = logging.getLogger("beamer.audio")

PORT = 24822
MODES = ("off", "send", "receive")
FILE_NAME = "audio.json"

# 5 ms of 16-bit stereo at 48 kHz is 960 bytes: with the 56 bytes of sealing it fits one Ethernet
# frame with room to spare, so nothing is fragmented.
CHUNK_DIVISOR = 200
KEY_ID_SIZE = 16
NONCE_SIZE = SecretBox.NONCE_SIZE
HEADER = struct.Struct(">IIII")  # session, sequence, sample rate, chunk length in bytes
SEALED_MIN = KEY_ID_SIZE + NONCE_SIZE + SecretBox.MACBYTES + HEADER.size
MAX_DATAGRAM = 4096
PRIME_MS = 40  # buffered before playing starts, to ride out network jitter
MAX_MS = 200  # buffered at most; older audio is dropped so a stall never becomes lag
REFRESH_SECONDS = 5.0
IDLE_SECONDS = 1.5  # a source silent this long frees the speakers for another


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


def settings_dir(settings_path) -> Path:
    return Path(settings_path).parent


def load_audio(settings_path) -> dict:
    """The saved choice, forgiving of a missing or damaged file: a machine starts with audio off."""
    try:
        raw = json.loads((settings_dir(settings_path) / FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    mode = raw.get("mode")
    return {
        "mode": mode if mode in MODES else "off",
        "peer": raw["peer"] if isinstance(raw.get("peer"), str) else "",
        "output": raw["output"] if isinstance(raw.get("output"), str) else "",
    }


def save_audio(settings_path, mode: str, peer: str = "", output: str = "") -> None:
    if mode not in MODES:
        raise ValueError(mode)
    target = settings_dir(settings_path) / FILE_NAME
    temporary = target.with_name(FILE_NAME + ".tmp")
    temporary.write_text(json.dumps({"mode": mode, "peer": peer, "output": output}), encoding="utf-8")
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


def _pyaudio():
    import pyaudiowpatch  # imported late: the rest of Beamer works without it

    return pyaudiowpatch


def output_devices() -> list:
    """Names of the speakers this machine can play on, for the choice list."""
    pa = _pyaudio()
    player = pa.PyAudio()
    try:
        wasapi = player.get_host_api_info_by_type(pa.paWASAPI)["index"]
        return sorted({
            info["name"] for info in (player.get_device_info_by_index(i) for i in range(player.get_device_count()))
            if info["hostApi"] == wasapi and info["maxOutputChannels"] > 0
        })
    finally:
        player.terminate()


class Player:
    """Plays 16-bit stereo PCM fed to it, with a small buffer that smooths out the network."""

    def __init__(self, pa, rate: int, device_name: str):
        self.rate = rate
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._primed = False
        self._module = pa
        self._pa = pa.PyAudio()
        self._stream = None
        try:
            self._stream = self._open(pa, device_name)
        except Exception:
            self._pa.terminate()
            raise

    def _device_index(self, pa, device_name: str):
        wasapi = self._pa.get_host_api_info_by_type(pa.paWASAPI)
        if device_name:
            for i in range(self._pa.get_device_count()):
                info = self._pa.get_device_info_by_index(i)
                if info["hostApi"] == wasapi["index"] and info["name"] == device_name and info["maxOutputChannels"]:
                    return i
        return wasapi["defaultOutputDevice"]

    def _open(self, pa, device_name):
        index = self._device_index(pa, device_name)
        # WASAPI shared mode only takes the device's own rate; if the sender's differs, the default
        # host API (MME) lets Windows convert.
        for device in (index, None):
            try:
                return self._pa.open(
                    format=pa.paInt16, channels=2, rate=self.rate, output=True, output_device_index=device,
                    frames_per_buffer=self.rate // CHUNK_DIVISOR * 2, stream_callback=self._pull)
            except OSError:
                if device is None:
                    raise
                LOGGER.info("WASAPI would not play %d Hz stereo; trying the default device", self.rate)
                index = None

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


class AudioShare:
    """Owns the sending or receiving side the saved mode asks for, and follows changes to it."""

    def __init__(self, settings_path):
        self.settings_path = Path(settings_path)
        self.status = ""
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._thread = None
        self._applied = None
        self._teardown = lambda: None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="beamer-audio", daemon=True)
            self._thread.start()

    def apply(self) -> None:
        """Pick up a changed choice now rather than at the next refresh."""
        self._wake.set()

    def stop(self) -> None:
        self._stopped.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(3)

    def _run(self) -> None:
        while not self._stopped.is_set():
            self._wake.clear()
            try:
                self._reconcile()
            except Exception as error:
                LOGGER.exception("Audio sharing failed")
                self.status = str(error) or error.__class__.__name__
                self._teardown()
                self._teardown = lambda: None
                self._applied = None
            self._wake.wait(REFRESH_SECONDS)
        self._teardown()

    def _reconcile(self) -> None:
        choice = load_audio(self.settings_path)
        peers = paired_peers(self.settings_path)
        if choice["mode"] == "send":
            target = next((p for p in peers if p["id"] == choice["peer"] and p["host"]), None)
            wanted = ("send", target["host"], target["token"]) if target else ("send-missing",)
        elif choice["mode"] == "receive":
            wanted = ("receive", choice["peer"], choice["output"], tuple((p["id"], p["token"]) for p in peers))
        else:
            wanted = ("off",)
        if wanted == self._applied:
            return
        self._teardown()
        self._teardown = lambda: None
        self._applied = wanted
        if wanted[0] == "off":
            self.status = ""
        elif wanted[0] == "send-missing":
            self.status = "Choose a paired machine to send to"
        elif wanted[0] == "send":
            self._teardown = self._start_sending(wanted[1], wanted[2])
            self.status = "Sending this PC's sound"
        else:
            self._teardown = self._start_receiving(choice["peer"], choice["output"], peers)
            self.status = "Waiting for sound"

    def _start_sending(self, host: str, token: str):
        pa = _pyaudio()
        player = pa.PyAudio()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        session = struct.unpack(">I", random_bytes(4))[0]
        sequence = [0]
        try:
            loopback = player.get_default_wasapi_loopback()
            rate, channels = int(loopback["defaultSampleRate"]), int(loopback["maxInputChannels"])

            def push(data, _frames, _time_info, _status):
                sequence[0] = (sequence[0] + 1) & 0xFFFFFFFF
                try:
                    sock.sendto(seal(token, session, sequence[0], rate, to_stereo(data, channels)), (host, PORT))
                except OSError:
                    pass  # the peer is away or the network is down; the next chunk tries again
                return None, pa.paContinue

            stream = player.open(
                format=pa.paInt16, channels=channels, rate=rate, input=True, input_device_index=loopback["index"],
                frames_per_buffer=rate // CHUNK_DIVISOR, stream_callback=push)
        except Exception:
            sock.close()
            player.terminate()
            raise

        def teardown():
            stream.stop_stream()
            stream.close()
            player.terminate()
            sock.close()

        return teardown

    def _start_receiving(self, peer_id: str, output: str, peers: list):
        accepted = {audio_key_id(p["token"]): p for p in peers if not peer_id or p["id"] == peer_id}
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("", PORT))
        except OSError:
            sock.close()
            raise
        sock.settimeout(0.5)
        stop = threading.Event()
        pa = _pyaudio()

        def listen():
            player = source = None  # source: (peer id, session, last sequence, last heard)
            try:
                while not stop.is_set():
                    try:
                        datagram = sock.recv(MAX_DATAGRAM)
                    except socket.timeout:
                        if player is not None and time.monotonic() - source[3] > IDLE_SECONDS:
                            player.close()
                            player = source = None
                            self.status = "Waiting for sound"
                        continue
                    except OSError:
                        break
                    peer = accepted.get(datagram[:KEY_ID_SIZE])
                    opened = unseal(peer["token"], datagram) if peer else None
                    if opened is None:
                        continue
                    session, sequence, rate, pcm = opened
                    now = time.monotonic()
                    if source is not None and source[0] != peer["id"] and now - source[3] < IDLE_SECONDS:
                        continue  # the speakers are taken by another machine
                    if source is not None and source[0] == peer["id"] and source[1] == session and sequence <= source[2]:
                        continue  # a repeat or a late datagram
                    if player is None or player.rate != rate:
                        if player is not None:
                            player.close()
                            player = None
                        player = Player(pa, rate, output)
                        self.status = "Playing sound from " + peer["name"]
                    source = (peer["id"], session, sequence, now)
                    player.feed(pcm)
            except Exception as error:
                LOGGER.exception("Audio playback failed")
                self.status = "Could not play sound: " + (str(error) or error.__class__.__name__)
            finally:
                if player is not None:
                    player.close()

        worker = threading.Thread(target=listen, name="beamer-audio-in", daemon=True)
        worker.start()

        def teardown():
            stop.set()
            sock.close()
            worker.join(3)

        return teardown
