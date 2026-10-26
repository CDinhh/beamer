"""Integration tests for audio sharing: Player buffering, AudioShare networking,
Manager lifecycle and AudioDialog UI. Uses fakes for PyAudioWPatch.
"""

import json
import os
import socket
import struct
import sys
import tempfile
import time
import unittest
from array import array
from pathlib import Path
from unittest.mock import patch

# Ensure project root and win_app are on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import audio_dialog
import audio_share
from core import protocol

# Valid 43-char base64 paired tokens (from 32 bytes)
TOKEN_A = protocol.id_text(b"A" * 32)
TOKEN_B = protocol.id_text(b"B" * 32)
TOKEN_C = protocol.id_text(b"C" * 32)


def wait_until(predicate, timeout=3.0, interval=0.01, message="Condition timed out"):
    """Poll predicate with a short interval until true or timeout."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        val = predicate()
        if val:
            return val
        time.sleep(interval)
    val = predicate()
    if val:
        return val
    raise TimeoutError(message)


class FakeStream:
    """Fake PyAudio stream tracking state and stream_callback."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.is_stopped = False
        self.is_closed = False
        self.callback = kwargs.get("stream_callback")

    def stop_stream(self):
        self.is_stopped = True

    def close(self):
        self.is_closed = True


class FakePyAudioInstance:
    """Fake PyAudio instance providing host api and device queries."""

    def __init__(self, module):
        self.module = module
        self.terminated = False
        self.opened_streams = []

    def get_host_api_info_by_type(self, host_api_type):
        return {"index": 0, "defaultOutputDevice": 0}

    def get_device_count(self):
        return len(self.module.devices)

    def get_device_info_by_index(self, index):
        if 0 <= index < len(self.module.devices):
            return self.module.devices[index]
        raise ValueError(f"Invalid device index {index}")

    def get_default_wasapi_loopback(self):
        if self.module.loopback_hook:
            return self.module.loopback_hook()
        return self.module.loopback

    def open(self, **kwargs):
        if self.module.open_hook:
            self.module.open_hook(kwargs)
        stream = FakeStream(**kwargs)
        self.opened_streams.append(stream)
        self.module.all_opened_streams.append(stream)
        return stream

    def terminate(self):
        self.terminated = True


class FakePyAudioModule:
    """Fake PyAudioWPatch module with constants and PyAudio factory."""

    paInt16 = 1
    paContinue = 0
    paWASAPI = 1

    def __init__(self, devices=None, loopback=None, open_hook=None):
        self.devices = devices or [
            {"index": 0, "name": "Default Speakers", "hostApi": 0, "maxOutputChannels": 2, "defaultSampleRate": 48000},
            {"index": 1, "name": "Headphones", "hostApi": 0, "maxOutputChannels": 2, "defaultSampleRate": 48000},
        ]
        self.loopback = loopback or {
            "index": 0,
            "name": "WASAPI Loopback",
            "defaultSampleRate": 48000,
            "maxInputChannels": 2,
        }
        self.open_hook = open_hook
        self.loopback_hook = None
        self.instances = []
        self.all_opened_streams = []

    def PyAudio(self):
        instance = FakePyAudioInstance(self)
        self.instances.append(instance)
        return instance


# ---------------------------------------------------------------------------
# Player Tests
# ---------------------------------------------------------------------------

class PlayerTests(unittest.TestCase):
    def setUp(self):
        self.fake_module = FakePyAudioModule()

    def test_priming_silence_until_prime_ms_buffered(self):
        """Player returns silence on pull until PRIME_MS is buffered, then plays audio."""
        rate = 48000
        player = audio_share.Player(self.fake_module, rate, "")
        self.addCleanup(player.close)

        # 40 ms at 48000 Hz, stereo 16-bit = 7680 bytes
        prime_bytes = rate * 4 * audio_share.PRIME_MS // 1000
        self.assertEqual(prime_bytes, 7680)

        # Pull before any audio fed: returns silence
        frames = 240  # 960 bytes needed
        data, flag = player._pull(None, frames, None, None)
        self.assertEqual(data, bytes(frames * 4))
        self.assertEqual(flag, self.fake_module.paContinue)
        self.assertFalse(player._primed)

        # Feed partial audio (less than PRIME_MS): still returns silence
        player.feed(b"\x11" * 2000)
        data, flag = player._pull(None, frames, None, None)
        self.assertEqual(data, bytes(frames * 4))
        self.assertFalse(player._primed)

        # Feed enough to exceed PRIME_MS
        player.feed(b"\x22" * 6000)  # total 8000 >= 7680
        data, flag = player._pull(None, frames, None, None)
        self.assertTrue(player._primed)
        # First 960 bytes of buffered data should be returned
        self.assertEqual(data, b"\x11" * 960)
        self.assertEqual(len(player._buffer), 8000 - 960)

    def test_underrun_returns_silence_and_reprimes(self):
        """When the buffer starves, Player emits silence, clears primed flag, and waits for PRIME_MS."""
        rate = 48000
        player = audio_share.Player(self.fake_module, rate, "")
        self.addCleanup(player.close)

        # Prime the player with 8000 bytes
        player.feed(b"\x33" * 8000)
        frames = 240  # 960 bytes
        data, _ = player._pull(None, frames, None, None)
        self.assertTrue(player._primed)

        # Force buffer into starvation (fewer bytes than needed for next frame)
        with player._lock:
            player._buffer = bytearray(b"\x44" * 400)

        # Pull with insufficient data: underrun returns silence and resets _primed
        data, _ = player._pull(None, frames, None, None)
        self.assertEqual(data, bytes(frames * 4))
        self.assertFalse(player._primed)

        # Feed 1000 bytes (more than 960 needed, but less than 7680 PRIME_MS threshold)
        player.feed(b"\x55" * 1000)  # total 1400 bytes
        data, _ = player._pull(None, frames, None, None)
        # Must still return silence because it must re-prime first
        self.assertEqual(data, bytes(frames * 4))
        self.assertFalse(player._primed)
        self.assertEqual(len(player._buffer), 1400)

        # Feed enough to reach PRIME_MS threshold
        player.feed(b"\x66" * 7000)  # total 8400 >= 7680
        data, _ = player._pull(None, frames, None, None)
        self.assertTrue(player._primed)
        self.assertEqual(data, (b"\x44" * 400 + b"\x55" * 560))

    def test_overflow_trimming_past_max_ms(self):
        """Feeding more than MAX_MS causes the buffer to be trimmed to limit // 3, keeping newest audio."""
        rate = 48000
        player = audio_share.Player(self.fake_module, rate, "")
        self.addCleanup(player.close)

        limit = rate * 4 * audio_share.MAX_MS // 1000  # 38400 bytes
        retained = limit // 3  # 12800 bytes

        # Feed exactly up to the limit
        player.feed(b"A" * limit)
        self.assertEqual(len(player._buffer), limit)

        # Overflow by feeding another 10000 bytes of "B"
        player.feed(b"B" * 10000)
        self.assertEqual(len(player._buffer), retained)
        # Oldest bytes were trimmed; retained content ends with the newest "B"s
        expected = b"A" * (retained - 10000) + b"B" * 10000
        self.assertEqual(bytes(player._buffer), expected)

    def test_close_terminates_pyaudio_instance(self):
        """player.close() stops and closes the stream and terminates the PyAudio instance."""
        player = audio_share.Player(self.fake_module, 48000, "")
        self.assertEqual(len(self.fake_module.instances), 1)
        pa_instance = self.fake_module.instances[0]
        stream = player._stream

        self.assertFalse(stream.is_stopped)
        self.assertFalse(stream.is_closed)
        self.assertFalse(pa_instance.terminated)

        player.close()

        self.assertTrue(stream.is_stopped)
        self.assertTrue(stream.is_closed)
        self.assertTrue(pa_instance.terminated)

    def test_open_failure_terminates_pyaudio_instance(self):
        """If stream opening raises an exception, PyAudio instance is terminated before re-raising."""
        def failing_open(_kwargs):
            raise OSError("Hardware unavailable")

        self.fake_module.open_hook = failing_open
        with self.assertRaises(OSError):
            audio_share.Player(self.fake_module, 48000, "")

        self.assertEqual(len(self.fake_module.instances), 1)
        self.assertTrue(self.fake_module.instances[0].terminated)

    def test_wasapi_oserror_falls_back_to_default_device(self):
        """If opening with the WASAPI device index raises OSError, it retries with default device (None)."""
        attempted_indices = []

        def wasapi_fails(kwargs):
            dev = kwargs.get("output_device_index")
            attempted_indices.append(dev)
            if dev is not None:
                raise OSError("WASAPI shared mode failed for device")

        self.fake_module.open_hook = wasapi_fails
        player = audio_share.Player(self.fake_module, 48000, "Headphones")
        self.addCleanup(player.close)

        # Should have attempted Headphones (index 1), then None
        self.assertEqual(attempted_indices, [1, None])
        self.assertIsNone(player._stream.kwargs.get("output_device_index"))

    def test_device_selection_by_name_and_fallback(self):
        """Selecting by device name finds matching WASAPI output device; missing name uses default."""
        p1 = audio_share.Player(self.fake_module, 48000, "Headphones")
        self.assertEqual(p1._stream.kwargs.get("output_device_index"), 1)
        p1.close()

        p2 = audio_share.Player(self.fake_module, 48000, "")
        self.assertEqual(p2._stream.kwargs.get("output_device_index"), 0)
        p2.close()

        p3 = audio_share.Player(self.fake_module, 48000, "NonExistentDevice")
        self.assertEqual(p3._stream.kwargs.get("output_device_index"), 0)
        p3.close()


# ---------------------------------------------------------------------------
# Receive Path Tests
# ---------------------------------------------------------------------------

class ReceivePathTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_path = Path(self.temp_dir.name) / "settings.json"
        self.fake_module = FakePyAudioModule()
        self._orig_pyaudio = audio_share._pyaudio
        self._orig_idle = audio_share.IDLE_SECONDS
        audio_share._pyaudio = lambda: self.fake_module

        self.peer1 = {"id": "p1", "name": "Laptop", "platform": "windows", "token": TOKEN_A, "host": "127.0.0.1"}
        self.peer2 = {"id": "p2", "name": "Desk", "platform": "windows", "token": TOKEN_B, "host": "127.0.0.1"}
        self.settings_path.write_text(json.dumps({"peers": [self.peer1, self.peer2]}), encoding="utf-8")
        audio_share.save_audio(self.settings_path, "receive")

        self.share = audio_share.AudioShare(self.settings_path)
        self.share.start()
        wait_until(lambda: self.share.status == "Waiting for sound")

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def tearDown(self):
        self.sock.close()
        self.share.stop()
        audio_share._pyaudio = self._orig_pyaudio
        audio_share.IDLE_SECONDS = self._orig_idle
        self.temp_dir.cleanup()

    def test_valid_datagram_updates_status_and_pcm_reaches_stream(self):
        """A valid datagram sets status to 'Playing sound from <name>' and feeds PCM to the stream."""
        pcm = b"\x12\x34" * 480  # 960 bytes
        dg = audio_share.seal(TOKEN_A, session=1, sequence=1, rate=48000, pcm=pcm)
        self.sock.sendto(dg, ("127.0.0.1", audio_share.PORT))

        wait_until(lambda: self.share.status == "Playing sound from Laptop")
        self.assertEqual(len(self.fake_module.all_opened_streams), 1)

        stream = self.fake_module.all_opened_streams[0]
        player = stream.kwargs["stream_callback"].__self__
        wait_until(lambda: len(player._buffer) >= len(pcm))
        self.assertEqual(bytes(player._buffer[:len(pcm)]), pcm)

    def test_wrong_token_datagram_ignored(self):
        """Datagram sealed with an unknown token is ignored and status remains 'Waiting for sound'."""
        pcm = bytes(960)
        dg = audio_share.seal(TOKEN_C, session=1, sequence=1, rate=48000, pcm=pcm)
        self.sock.sendto(dg, ("127.0.0.1", audio_share.PORT))

        time.sleep(0.05)
        self.assertEqual(self.share.status, "Waiting for sound")
        self.assertEqual(len(self.fake_module.all_opened_streams), 0)

        # Subsequent valid datagram is still handled
        valid_dg = audio_share.seal(TOKEN_A, session=1, sequence=1, rate=48000, pcm=pcm)
        self.sock.sendto(valid_dg, ("127.0.0.1", audio_share.PORT))
        wait_until(lambda: self.share.status == "Playing sound from Laptop")

    def test_tampered_datagram_ignored(self):
        """A datagram with tampered ciphertext fails decryption and is ignored."""
        pcm = bytes(960)
        dg = bytearray(audio_share.seal(TOKEN_A, session=1, sequence=1, rate=48000, pcm=pcm))
        dg[-1] ^= 0xFF
        self.sock.sendto(bytes(dg), ("127.0.0.1", audio_share.PORT))

        time.sleep(0.05)
        self.assertEqual(self.share.status, "Waiting for sound")
        self.assertEqual(len(self.fake_module.all_opened_streams), 0)

        # Subsequent valid datagram succeeds
        valid_dg = audio_share.seal(TOKEN_A, session=1, sequence=1, rate=48000, pcm=pcm)
        self.sock.sendto(valid_dg, ("127.0.0.1", audio_share.PORT))
        wait_until(lambda: self.share.status == "Playing sound from Laptop")

    def test_short_datagram_ignored(self):
        """Datagrams shorter than SEALED_MIN are ignored."""
        self.sock.sendto(b"too_short", ("127.0.0.1", audio_share.PORT))

        time.sleep(0.05)
        self.assertEqual(self.share.status, "Waiting for sound")
        self.assertEqual(len(self.fake_module.all_opened_streams), 0)

        valid_dg = audio_share.seal(TOKEN_A, session=1, sequence=1, rate=48000, pcm=bytes(960))
        self.sock.sendto(valid_dg, ("127.0.0.1", audio_share.PORT))
        wait_until(lambda: self.share.status == "Playing sound from Laptop")

    def test_oversize_datagram_ignored(self):
        # Regression (fixed): on Windows, sending a UDP datagram larger than MAX_DATAGRAM (4096 bytes) causes
        # sock.recv(MAX_DATAGRAM) in audio_share.py to raise OSError: [WinError 10040] (WSAEMSGSIZE).
        # audio_share.py lines 357-358 catch OSError and unconditionally 'break', terminating the
        # receive worker thread permanently instead of ignoring the oversize packet.
        # As a result, all subsequent valid datagrams are dropped and playback never starts.
        oversize_payload = b"X" * 5000
        self.sock.sendto(oversize_payload, ("127.0.0.1", audio_share.PORT))
        time.sleep(0.05)

        valid_dg = audio_share.seal(TOKEN_A, session=1, sequence=1, rate=48000, pcm=bytes(960))
        self.sock.sendto(valid_dg, ("127.0.0.1", audio_share.PORT))
        wait_until(lambda: self.share.status == "Playing sound from Laptop", timeout=1.0)

    def test_repeated_sequence_ignored(self):
        """A datagram with a sequence number <= the last heard sequence in the same session is ignored."""
        pcm1 = b"\x01" * 960
        dg1 = audio_share.seal(TOKEN_A, session=1, sequence=5, rate=48000, pcm=pcm1)
        self.sock.sendto(dg1, ("127.0.0.1", audio_share.PORT))
        wait_until(lambda: self.share.status == "Playing sound from Laptop")

        stream = self.fake_module.all_opened_streams[0]
        player = stream.kwargs["stream_callback"].__self__
        wait_until(lambda: len(player._buffer) == 960)

        # Send duplicate sequence 5
        pcm_dup = b"\x02" * 960
        dg_dup = audio_share.seal(TOKEN_A, session=1, sequence=5, rate=48000, pcm=pcm_dup)
        self.sock.sendto(dg_dup, ("127.0.0.1", audio_share.PORT))
        time.sleep(0.05)
        self.assertEqual(len(player._buffer), 960)

    def test_older_sequence_ignored(self):
        """A datagram with an older sequence number than previously received is ignored."""
        pcm1 = b"\x01" * 960
        dg1 = audio_share.seal(TOKEN_A, session=1, sequence=5, rate=48000, pcm=pcm1)
        self.sock.sendto(dg1, ("127.0.0.1", audio_share.PORT))
        wait_until(lambda: self.share.status == "Playing sound from Laptop")

        stream = self.fake_module.all_opened_streams[0]
        player = stream.kwargs["stream_callback"].__self__
        wait_until(lambda: len(player._buffer) == 960)

        # Send older sequence 4
        pcm_old = b"\x02" * 960
        dg_old = audio_share.seal(TOKEN_A, session=1, sequence=4, rate=48000, pcm=pcm_old)
        self.sock.sendto(dg_old, ("127.0.0.1", audio_share.PORT))
        time.sleep(0.05)
        self.assertEqual(len(player._buffer), 960)

        # Send newer sequence 6
        pcm_new = b"\x03" * 960
        dg_new = audio_share.seal(TOKEN_A, session=1, sequence=6, rate=48000, pcm=pcm_new)
        self.sock.sendto(dg_new, ("127.0.0.1", audio_share.PORT))
        wait_until(lambda: len(player._buffer) == 1920)

    def test_new_session_accepted_even_with_lower_sequence(self):
        """When session id changes (sender restarted), sequence check resets."""
        dg1 = audio_share.seal(TOKEN_A, session=10, sequence=5, rate=48000, pcm=b"\x01" * 960)
        self.sock.sendto(dg1, ("127.0.0.1", audio_share.PORT))
        wait_until(lambda: self.share.status == "Playing sound from Laptop")

        stream = self.fake_module.all_opened_streams[0]
        player = stream.kwargs["stream_callback"].__self__
        wait_until(lambda: len(player._buffer) == 960)

        # New session with sequence 1
        dg2 = audio_share.seal(TOKEN_A, session=11, sequence=1, rate=48000, pcm=b"\x02" * 960)
        self.sock.sendto(dg2, ("127.0.0.1", audio_share.PORT))
        wait_until(lambda: len(player._buffer) == 1920)

    def test_second_peer_ignored_while_first_active(self):
        """A second peer's datagram is ignored while the first peer is active."""
        dg1 = audio_share.seal(TOKEN_A, session=1, sequence=1, rate=48000, pcm=b"\x01" * 960)
        self.sock.sendto(dg1, ("127.0.0.1", audio_share.PORT))
        wait_until(lambda: self.share.status == "Playing sound from Laptop")

        stream = self.fake_module.all_opened_streams[0]
        player = stream.kwargs["stream_callback"].__self__
        wait_until(lambda: len(player._buffer) == 960)

        # Immediately send from peer 2
        dg2 = audio_share.seal(TOKEN_B, session=1, sequence=1, rate=48000, pcm=b"\x02" * 960)
        self.sock.sendto(dg2, ("127.0.0.1", audio_share.PORT))
        time.sleep(0.05)

        self.assertEqual(self.share.status, "Playing sound from Laptop")
        self.assertEqual(len(player._buffer), 960)

    def test_second_peer_accepted_after_idle_seconds(self):
        """After IDLE_SECONDS pass with no audio from the first peer, a second peer is accepted."""
        audio_share.IDLE_SECONDS = 0.05

        dg1 = audio_share.seal(TOKEN_A, session=1, sequence=1, rate=48000, pcm=b"\x01" * 960)
        self.sock.sendto(dg1, ("127.0.0.1", audio_share.PORT))
        wait_until(lambda: self.share.status == "Playing sound from Laptop")

        # Wait for idle timeout
        wait_until(lambda: self.share.status == "Waiting for sound", timeout=1.5)

        # Peer 2 sends audio
        dg2 = audio_share.seal(TOKEN_B, session=1, sequence=1, rate=48000, pcm=b"\x02" * 960)
        self.sock.sendto(dg2, ("127.0.0.1", audio_share.PORT))
        wait_until(lambda: self.share.status == "Playing sound from Desk")

        self.assertEqual(len(self.fake_module.all_opened_streams), 2)
        stream2 = self.fake_module.all_opened_streams[1]
        player2 = stream2.kwargs["stream_callback"].__self__
        wait_until(lambda: len(player2._buffer) == 960)
        self.assertEqual(bytes(player2._buffer), b"\x02" * 960)

    def test_second_peer_status_when_packets_continuous_during_idle(self):
        # Regression (fixed): when a second peer takes over after IDLE_SECONDS without a socket.timeout occurring
        # (e.g. peer 2 was actively sending packets while peer 1 went silent), line 369 in audio_share.py
        # checks `if player is None or player.rate != rate:` before updating self.status.
        # Because player is already open and sample rate is unchanged, line 374 is skipped, and
        # self.status remains "Playing sound from <peer1>" even though sound from peer2 is playing.
        audio_share.IDLE_SECONDS = 0.1

        dg1 = audio_share.seal(TOKEN_A, session=1, sequence=1, rate=48000, pcm=bytes(960))
        self.sock.sendto(dg1, ("127.0.0.1", audio_share.PORT))
        wait_until(lambda: self.share.status == "Playing sound from Laptop")

        # Peer 2 sends packets at intervals shorter than socket timeout (0.5s) for > IDLE_SECONDS
        start = time.monotonic()
        seq = 1
        while time.monotonic() - start < 0.3:
            time.sleep(0.04)
            seq += 1
            dg2 = audio_share.seal(TOKEN_B, session=2, sequence=seq, rate=48000, pcm=bytes(960))
            self.sock.sendto(dg2, ("127.0.0.1", audio_share.PORT))

        # Status should update to "Playing sound from Desk", but fails due to line 369 bug
        self.assertEqual(self.share.status, "Playing sound from Desk")

    def test_peer_filter_rejects_other_paired_peers(self):
        """When audio.json sets 'peer' to a specific id, datagrams from other paired peers are rejected."""
        self.share.stop()
        audio_share.save_audio(self.settings_path, "receive", peer="p1")
        self.share = audio_share.AudioShare(self.settings_path)
        self.share.start()
        wait_until(lambda: self.share.status == "Waiting for sound")

        # Peer 2 is paired, but peer filter is set to p1
        dg2 = audio_share.seal(TOKEN_B, session=1, sequence=1, rate=48000, pcm=bytes(960))
        self.sock.sendto(dg2, ("127.0.0.1", audio_share.PORT))
        time.sleep(0.05)
        self.assertEqual(self.share.status, "Waiting for sound")
        self.assertEqual(len(self.fake_module.all_opened_streams), 0)

        # Peer 1 matches the filter
        dg1 = audio_share.seal(TOKEN_A, session=1, sequence=1, rate=48000, pcm=bytes(960))
        self.sock.sendto(dg1, ("127.0.0.1", audio_share.PORT))
        wait_until(lambda: self.share.status == "Playing sound from Laptop")

    def test_rate_change_reopens_player(self):
        """When incoming sample rate changes, the old Player is closed and a new Player is opened."""
        dg1 = audio_share.seal(TOKEN_A, session=1, sequence=1, rate=48000, pcm=bytes(960))
        self.sock.sendto(dg1, ("127.0.0.1", audio_share.PORT))
        wait_until(lambda: self.share.status == "Playing sound from Laptop")
        self.assertEqual(len(self.fake_module.all_opened_streams), 1)
        stream1 = self.fake_module.all_opened_streams[0]
        self.assertEqual(stream1.kwargs["rate"], 48000)

        # Rate change to 44100
        dg2 = audio_share.seal(TOKEN_A, session=1, sequence=2, rate=44100, pcm=bytes(880))
        self.sock.sendto(dg2, ("127.0.0.1", audio_share.PORT))
        wait_until(lambda: len(self.fake_module.all_opened_streams) == 2)
        stream2 = self.fake_module.all_opened_streams[1]

        self.assertTrue(stream1.is_stopped)
        self.assertTrue(stream1.is_closed)
        self.assertEqual(stream2.kwargs["rate"], 44100)
        self.assertEqual(self.share.status, "Playing sound from Laptop")

    def test_idle_timeout_resets_status(self):
        """When incoming datagrams stop, status resets to 'Waiting for sound' and Player is closed."""
        audio_share.IDLE_SECONDS = 0.05

        dg = audio_share.seal(TOKEN_A, session=1, sequence=1, rate=48000, pcm=bytes(960))
        self.sock.sendto(dg, ("127.0.0.1", audio_share.PORT))
        wait_until(lambda: self.share.status == "Playing sound from Laptop")
        stream = self.fake_module.all_opened_streams[0]

        # Stop sending and wait for idle timeout
        wait_until(lambda: self.share.status == "Waiting for sound", timeout=1.5)
        self.assertTrue(stream.is_stopped)
        self.assertTrue(stream.is_closed)


# ---------------------------------------------------------------------------
# Send Path Tests
# ---------------------------------------------------------------------------

class SendPathTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_path = Path(self.temp_dir.name) / "settings.json"
        self.fake_module = FakePyAudioModule()
        self._orig_pyaudio = audio_share._pyaudio
        audio_share._pyaudio = lambda: self.fake_module

        self.token = TOKEN_A
        self.peer = {"id": "p1", "name": "Laptop", "platform": "windows", "token": self.token, "host": "127.0.0.1"}
        self.settings_path.write_text(json.dumps({"peers": [self.peer]}), encoding="utf-8")
        audio_share.save_audio(self.settings_path, "send", peer="p1")

        self.recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.recv_sock.bind(("127.0.0.1", audio_share.PORT))
        self.recv_sock.settimeout(1.0)

        self.share = audio_share.AudioShare(self.settings_path)
        self.share.start()
        wait_until(lambda: self.share.status == "Sending this PC's sound")
        self.stream = self.fake_module.all_opened_streams[0]
        self.push = self.stream.callback

    def tearDown(self):
        self.share.stop()
        self.recv_sock.close()
        audio_share._pyaudio = self._orig_pyaudio
        self.temp_dir.cleanup()

    def test_send_callback_produces_valid_datagram_with_correct_token(self):
        """AudioShare send callback sends a UDP datagram with key id and valid ciphertext."""
        pcm = array("h", [i % 32767 for i in range(480)]).tobytes()  # 240 stereo frames = 960 bytes
        ret, flag = self.push(pcm, 240, None, None)
        self.assertIsNone(ret)
        self.assertEqual(flag, self.fake_module.paContinue)

        datagram, _ = self.recv_sock.recvfrom(4096)
        self.assertTrue(datagram.startswith(audio_share.audio_key_id(self.token)))

        opened = audio_share.unseal(self.token, datagram)
        self.assertIsNotNone(opened)
        session, sequence, rate, received_pcm = opened
        self.assertEqual(sequence, 1)
        self.assertEqual(rate, 48000)
        self.assertEqual(received_pcm, pcm)

        # Cannot unseal with wrong token
        self.assertIsNone(audio_share.unseal(TOKEN_B, datagram))

    def test_send_sequence_increments(self):
        """Each chunk pushed through the callback increments the sequence number."""
        for expected_seq in range(1, 4):
            self.push(bytes(960), 240, None, None)
            datagram, _ = self.recv_sock.recvfrom(4096)
            _, seq, _, _ = audio_share.unseal(self.token, datagram)
            self.assertEqual(seq, expected_seq)

    def test_send_channel_downmix_surround_to_stereo(self):
        """When capturing 6-channel surround sound, callback downmixes to stereo."""
        self.share.stop()
        self.fake_module.loopback["maxInputChannels"] = 6
        self.fake_module.all_opened_streams.clear()

        self.share = audio_share.AudioShare(self.settings_path)
        self.share.start()
        wait_until(lambda: self.share.status == "Sending this PC's sound")

        stream = self.fake_module.all_opened_streams[0]
        push = stream.callback

        # 240 frames of 6 int16 samples
        raw_samples = [((frame * 6 + ch) * 100) % 32767 for frame in range(240) for ch in range(6)]
        surround_data = array("h", raw_samples).tobytes()

        push(surround_data, 240, None, None)
        datagram, _ = self.recv_sock.recvfrom(4096)
        _, _, _, pcm = audio_share.unseal(self.token, datagram)

        expected_stereo = audio_share.to_stereo(surround_data, 6)
        self.assertEqual(pcm, expected_stereo)

    def test_send_channel_downmix_mono_to_stereo(self):
        """When capturing 1-channel mono sound, callback doubles mono to stereo."""
        self.share.stop()
        self.fake_module.loopback["maxInputChannels"] = 1
        self.fake_module.all_opened_streams.clear()

        self.share = audio_share.AudioShare(self.settings_path)
        self.share.start()
        wait_until(lambda: self.share.status == "Sending this PC's sound")

        stream = self.fake_module.all_opened_streams[0]
        push = stream.callback

        mono_samples = [(frame * 100) % 32767 for frame in range(240)]
        mono_data = array("h", mono_samples).tobytes()

        push(mono_data, 240, None, None)
        datagram, _ = self.recv_sock.recvfrom(4096)
        _, _, _, pcm = audio_share.unseal(self.token, datagram)

        expected_stereo = audio_share.to_stereo(mono_data, 1)
        self.assertEqual(pcm, expected_stereo)

    def test_sendto_oserror_swallowed(self):
        """If sock.sendto raises OSError (network unreachable), callback swallows it and returns paContinue."""
        with patch.object(socket.socket, "sendto", side_effect=OSError("Network unreachable")):
            ret, flag = self.push(bytes(960), 240, None, None)
            self.assertIsNone(ret)
            self.assertEqual(flag, self.fake_module.paContinue)


# ---------------------------------------------------------------------------
# Manager Tests
# ---------------------------------------------------------------------------

class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_path = Path(self.temp_dir.name) / "settings.json"
        self.fake_module = FakePyAudioModule()
        self._orig_pyaudio = audio_share._pyaudio
        audio_share._pyaudio = lambda: self.fake_module

        self.peer = {"id": "p1", "name": "Laptop", "platform": "windows", "token": TOKEN_A, "host": "127.0.0.1"}
        self.settings_path.write_text(json.dumps({"peers": [self.peer]}), encoding="utf-8")

        self.share = audio_share.AudioShare(self.settings_path)

    def tearDown(self):
        self.share.stop()
        audio_share._pyaudio = self._orig_pyaudio
        self.temp_dir.cleanup()

    def test_mode_transitions_tear_down_previous_side_and_free_port(self):
        """Transitioning modes tears down previous sockets and streams, freeing UDP PORT."""
        # Start in receive mode
        audio_share.save_audio(self.settings_path, "receive")
        self.share.start()
        wait_until(lambda: self.share.status == "Waiting for sound")

        # Port is bound by receive mode
        probe_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with self.assertRaises(OSError):
            probe_sock.bind(("", audio_share.PORT))
        probe_sock.close()

        # Switch to mode "off"
        audio_share.save_audio(self.settings_path, "off")
        self.share.apply()
        wait_until(lambda: self.share.status == "")

        # Verify PORT is freed and can be bound
        probe_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe_sock.bind(("", audio_share.PORT))
        probe_sock.close()

        # Switch to mode "send"
        audio_share.save_audio(self.settings_path, "send", peer="p1")
        self.share.apply()
        wait_until(lambda: self.share.status == "Sending this PC's sound")

        # Send mode does not bind to PORT: port remains free
        probe_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe_sock.bind(("", audio_share.PORT))
        probe_sock.close()
        time.sleep(0.05)

        # Switch back to "receive"
        audio_share.save_audio(self.settings_path, "receive")
        self.share.apply()
        wait_until(lambda: self.share.status == "Waiting for sound")

        # Switch to "off" cleans up
        audio_share.save_audio(self.settings_path, "off")
        self.share.apply()
        wait_until(lambda: self.share.status == "")

    def test_send_mode_unknown_peer_gives_status(self):
        """Send mode with an unknown peer id sets status to 'Choose a paired machine to send to'."""
        audio_share.save_audio(self.settings_path, "send", peer="unknown_id")
        self.share.start()
        wait_until(lambda: self.share.status == "Choose a paired machine to send to")
        self.assertEqual(len(self.fake_module.all_opened_streams), 0)

    def test_send_mode_hostless_peer_gives_status(self):
        """Send mode with a peer that lacks a host sets status to 'Choose a paired machine to send to'."""
        hostless_peer = {"id": "p_nohost", "name": "NoHost", "platform": "windows", "token": TOKEN_A, "host": ""}
        self.settings_path.write_text(json.dumps({"peers": [hostless_peer]}), encoding="utf-8")
        audio_share.save_audio(self.settings_path, "send", peer="p_nohost")
        self.share.start()
        wait_until(lambda: self.share.status == "Choose a paired machine to send to")
        self.assertEqual(len(self.fake_module.all_opened_streams), 0)

    def test_send_mode_empty_peer_gives_status(self):
        """Send mode with peer='' sets status to 'Choose a paired machine to send to'."""
        audio_share.save_audio(self.settings_path, "send", peer="")
        self.share.start()
        wait_until(lambda: self.share.status == "Choose a paired machine to send to")
        self.assertEqual(len(self.fake_module.all_opened_streams), 0)

    def test_settings_json_peer_host_change_restarts_sending(self):
        """When a peer's host in settings.json changes, sending tears down and restarts with the new host."""
        audio_share.save_audio(self.settings_path, "send", peer="p1")
        self.share.start()
        wait_until(lambda: self.share.status == "Sending this PC's sound")

        self.assertEqual(len(self.fake_module.all_opened_streams), 1)
        stream1 = self.fake_module.all_opened_streams[0]

        # Update peer's host in settings.json
        updated_peer = dict(self.peer, host="127.0.0.2")
        self.settings_path.write_text(json.dumps({"peers": [updated_peer]}), encoding="utf-8")
        self.share.apply()

        wait_until(lambda: len(self.fake_module.all_opened_streams) == 2)
        stream2 = self.fake_module.all_opened_streams[1]

        self.assertTrue(stream1.is_stopped)
        self.assertTrue(stream1.is_closed)
        self.assertFalse(stream2.is_stopped)
        self.assertEqual(self.share._applied, ("send", "127.0.0.2", TOKEN_A, False))

    def test_exceptions_set_status_and_do_not_kill_thread(self):
        """An unhandled error in reconcile sets self.status and leaves the worker thread alive."""
        audio_share.save_audio(self.settings_path, "send", peer="p1")

        def failing_open(_kwargs):
            raise RuntimeError("Audio hardware lost")

        self.fake_module.open_hook = failing_open
        self.share.start()

        wait_until(lambda: self.share.status == "Audio hardware lost")
        self.assertTrue(self.share._thread.is_alive())

        # Recover by fixing the error and switching to off
        self.fake_module.open_hook = None
        audio_share.save_audio(self.settings_path, "off")
        self.share.apply()
        wait_until(lambda: self.share.status == "")
        self.assertTrue(self.share._thread.is_alive())

    def test_stop_is_clean_and_idempotent(self):
        """stop() terminates worker threads and can be called repeatedly without error."""
        audio_share.save_audio(self.settings_path, "receive")
        self.share.start()
        wait_until(lambda: self.share._thread is not None and self.share._thread.is_alive())

        self.share.stop()
        self.assertTrue(self.share._stopped.is_set())
        self.assertFalse(self.share._thread.is_alive())

        # Idempotent calls
        self.share.stop()
        self.share.stop()


# ---------------------------------------------------------------------------
# AudioDialog Tests (Qt Offscreen)
# ---------------------------------------------------------------------------

class AudioDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test"])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_path = Path(self.temp_dir.name) / "settings.json"
        self.fake_module = FakePyAudioModule()
        self._orig_pyaudio = audio_share._pyaudio
        audio_share._pyaudio = lambda: self.fake_module

        self.peer1 = {"id": "p1", "name": "Laptop", "platform": "windows", "token": TOKEN_A, "host": "192.168.1.10"}
        self.peer2 = {"id": "p2", "name": "Desk", "platform": "windows", "token": TOKEN_B, "host": "192.168.1.20"}
        self.settings_path.write_text(json.dumps({"peers": [self.peer1, self.peer2]}), encoding="utf-8")

        self.share = audio_share.AudioShare(self.settings_path)

    def tearDown(self):
        self.share.stop()
        audio_share._pyaudio = self._orig_pyaudio
        self.temp_dir.cleanup()

    def test_dialog_loads_saved_choice_off(self):
        """AudioDialog selects 'off' radio button when saved mode is off."""
        audio_share.save_audio(self.settings_path, "off")
        dlg = audio_dialog.AudioDialog(self.share)
        self.addCleanup(dlg.close)

        self.assertTrue(dlg.off.isChecked())
        self.assertFalse(dlg.send.isChecked())
        self.assertFalse(dlg.receive.isChecked())

    def test_dialog_loads_saved_choice_send(self):
        """AudioDialog selects 'send' and preselects the saved target peer."""
        audio_share.save_audio(self.settings_path, "send", peer="p2")
        dlg = audio_dialog.AudioDialog(self.share)
        self.addCleanup(dlg.close)

        self.assertTrue(dlg.send.isChecked())
        self.assertEqual(dlg.target.currentData(), "p2")

    def test_dialog_loads_saved_choice_receive(self):
        """AudioDialog selects 'receive' and preselects source peer and output device."""
        audio_share.save_audio(self.settings_path, "receive", peer="p1", output="Headphones")
        dlg = audio_dialog.AudioDialog(self.share)
        self.addCleanup(dlg.close)

        self.assertTrue(dlg.receive.isChecked())
        self.assertEqual(dlg.source.currentData(), "p1")
        self.assertEqual(dlg.output.currentData(), "Headphones")

    def test_dialog_enable_toggles(self):
        """Toggling radio buttons enables/disables target, source, and output controls."""
        audio_share.save_audio(self.settings_path, "off")
        dlg = audio_dialog.AudioDialog(self.share)
        self.addCleanup(dlg.close)

        # Off mode
        self.assertFalse(dlg.target.isEnabled())
        self.assertFalse(dlg.source.isEnabled())
        self.assertFalse(dlg.output.isEnabled())

        # Send mode
        dlg.send.setChecked(True)
        self.assertTrue(dlg.target.isEnabled())
        self.assertFalse(dlg.source.isEnabled())
        self.assertFalse(dlg.output.isEnabled())

        # Receive mode
        dlg.receive.setChecked(True)
        self.assertFalse(dlg.target.isEnabled())
        self.assertTrue(dlg.source.isEnabled())
        self.assertTrue(dlg.output.isEnabled())

        # Off mode again
        dlg.off.setChecked(True)
        self.assertFalse(dlg.target.isEnabled())
        self.assertFalse(dlg.source.isEnabled())
        self.assertFalse(dlg.output.isEnabled())

    def test_dialog_save_writes_off(self):
        """_save() writes mode 'off' when off is checked."""
        audio_share.save_audio(self.settings_path, "send", peer="p1")
        dlg = audio_dialog.AudioDialog(self.share)
        self.addCleanup(dlg.close)

        dlg.off.setChecked(True)
        dlg._save()

        saved = audio_share.load_audio(self.settings_path)
        self.assertEqual(saved, {"mode": "off", "peer": "", "output": "", "mute_local": False})

    def test_dialog_save_writes_send(self):
        """_save() writes mode 'send' and the selected peer id."""
        audio_share.save_audio(self.settings_path, "off")
        dlg = audio_dialog.AudioDialog(self.share)
        self.addCleanup(dlg.close)

        dlg.send.setChecked(True)
        index = dlg.target.findData("p2")
        dlg.target.setCurrentIndex(index)
        dlg._save()

        saved = audio_share.load_audio(self.settings_path)
        self.assertEqual(saved, {"mode": "send", "peer": "p2", "output": "", "mute_local": False})

    def test_dialog_save_writes_receive(self):
        """_save() writes mode 'receive', source peer, and output device."""
        audio_share.save_audio(self.settings_path, "off")
        dlg = audio_dialog.AudioDialog(self.share)
        self.addCleanup(dlg.close)

        dlg.receive.setChecked(True)
        dlg.source.setCurrentIndex(dlg.source.findData("p1"))
        dlg.output.setCurrentIndex(dlg.output.findData("Headphones"))
        dlg._save()

        saved = audio_share.load_audio(self.settings_path)
        self.assertEqual(saved, {"mode": "receive", "peer": "p1", "output": "Headphones", "mute_local": False})

    def test_dialog_empty_peer_list(self):
        """AudioDialog behaves cleanly when no peers are paired."""
        self.settings_path.write_text(json.dumps({"peers": []}), encoding="utf-8")
        dlg = audio_dialog.AudioDialog(self.share)
        self.addCleanup(dlg.close)

        self.assertEqual(len(dlg.peers), 0)
        dlg.send.setChecked(True)
        # Target disabled because peers list is empty
        self.assertFalse(dlg.target.isEnabled())

        # Source has default "Any paired PC"
        self.assertEqual(dlg.source.count(), 1)
        self.assertEqual(dlg.source.currentData(), "")

        # Save send with no peers selected saves peer as empty string
        dlg._save()
        saved = audio_share.load_audio(self.settings_path)
        self.assertEqual(saved, {"mode": "send", "peer": "", "output": "", "mute_local": False})

    def test_dialog_status_timer_updates(self):
        """Timer callback updates dialog status label when share status changes."""
        dlg = audio_dialog.AudioDialog(self.share)
        self.addCleanup(dlg.close)

        self.share.status = "Playing sound from Laptop"
        dlg._tick.timeout.emit()
        self.assertEqual(dlg.status.text(), "Playing sound from Laptop")

        self.share.status = ""
        dlg._tick.timeout.emit()
        self.assertEqual(dlg.status.text(), "")




class MuteLocalTests(unittest.TestCase):
    """Sending with the mute option tick mutes this PC's speakers and gives them back."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_path = Path(self.temp_dir.name) / "settings.json"
        self.fake_module = FakePyAudioModule()
        self._orig_pyaudio = audio_share._pyaudio
        audio_share._pyaudio = lambda: self.fake_module
        self.events = []
        events = self.events

        class FakeMute:
            def mute(self):
                events.append("mute")
                return True

            def restore(self):
                events.append("restore")
                return True

        self._orig_mute = audio_share.audio_mute.SpeakerMute
        audio_share.audio_mute.SpeakerMute = FakeMute
        peer = {"id": "p1", "name": "Laptop", "platform": "windows", "token": TOKEN_A, "host": "127.0.0.1"}
        self.settings_path.write_text(json.dumps({"peers": [peer]}), encoding="utf-8")
        self.share = audio_share.AudioShare(self.settings_path)

    def tearDown(self):
        self.share.stop()
        audio_share.audio_mute.SpeakerMute = self._orig_mute
        audio_share._pyaudio = self._orig_pyaudio
        self.temp_dir.cleanup()

    def test_mute_then_restore_around_sending(self):
        audio_share.save_audio(self.settings_path, "send", "p1", mute_local=True)
        self.share.start()
        wait_until(lambda: self.events == ["mute"])
        audio_share.save_audio(self.settings_path, "off")
        self.share.apply()
        wait_until(lambda: self.events == ["mute", "restore"])

    def test_no_mute_without_the_tick(self):
        audio_share.save_audio(self.settings_path, "send", "p1")
        self.share.start()
        wait_until(lambda: self.share.status == "Sending this PC's sound")
        self.share.stop()
        self.assertEqual(self.events, [])

    def test_stop_restores_the_speakers(self):
        audio_share.save_audio(self.settings_path, "send", "p1", mute_local=True)
        self.share.start()
        wait_until(lambda: self.events == ["mute"])
        self.share.stop()
        self.assertEqual(self.events, ["mute", "restore"])

    def test_dialog_saves_the_tick(self):
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        dlg = audio_dialog.AudioDialog(self.share)
        self.addCleanup(dlg.close)
        dlg.send.setChecked(True)
        dlg.mute.setChecked(True)
        dlg._save()
        self.assertTrue(audio_share.load_audio(self.settings_path)["mute_local"])


if __name__ == "__main__":
    unittest.main()
