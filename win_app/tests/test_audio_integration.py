"""Integration tests for audio sharing: Player buffering, receiving and mixing several PCs, streaming to
all of them, the manager and the Audio page. Uses fakes for PyAudioWPatch and for the capture."""

import json
import os
import socket
import struct
import sys
import tempfile
import threading
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

import audio_page
import audio_loopback
import audio_share

# A port of its own, so the tests never meet a Beamer that is running on this machine.
audio_share.PORT = 24899
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
        player = audio_share.Player(self.fake_module, rate)
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
        player = audio_share.Player(self.fake_module, rate)
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
        player = audio_share.Player(self.fake_module, rate)
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
        player = audio_share.Player(self.fake_module, 48000)
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
            audio_share.Player(self.fake_module, 48000)

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
        player = audio_share.Player(self.fake_module, 48000)
        self.addCleanup(player.close)

        # Should have attempted the default output (index 0), then None
        self.assertEqual(attempted_indices, [0, None])
        self.assertIsNone(player._stream.kwargs.get("output_device_index"))



# ---------------------------------------------------------------------------
# Shared set-up
# ---------------------------------------------------------------------------

PEER_A = {"id": "pa", "name": "Laptop", "platform": "windows", "token": TOKEN_A, "host": "127.0.0.1"}
PEER_B = {"id": "pb", "name": "Desk", "platform": "windows", "token": TOKEN_B, "host": "127.0.0.1"}


class ShareCase(unittest.TestCase):
    """A settings folder, fake PyAudio and a share that is stopped on the way out."""

    peers = (PEER_A, PEER_B)

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_path = Path(self.temp_dir.name) / "settings.json"
        self.settings_path.write_text(json.dumps({"peers": list(self.peers)}), encoding="utf-8")
        self.fake_module = FakePyAudioModule()
        self._orig_pyaudio = audio_share._pyaudio
        audio_share._pyaudio = lambda: self.fake_module
        self.share = audio_share.AudioShare(self.settings_path)

    def tearDown(self):
        self.share.stop()
        audio_share._pyaudio = self._orig_pyaudio
        self.temp_dir.cleanup()

    def choose(self, send=False, receive=False, volumes=None):
        audio_share.save_choice(self.settings_path, {"send": send, "receive": receive, "volumes": volumes or {}})

    def players(self):
        return [s.callback.__self__ for s in self.fake_module.all_opened_streams]

    def buffered(self, index):
        """Bytes waiting in the index-th player, or -1 before it exists."""
        players = self.players()
        return len(players[index]._buffer) if len(players) > index else -1


class FakeCapture:
    """Stands in for audio_loopback.LoopbackCapture: the test calls `emit` where Windows would."""

    instances = []
    fail = None

    def __init__(self, on_chunk):
        self.on_chunk = on_chunk
        self.started = 0
        self.stopped = 0
        FakeCapture.instances.append(self)

    def start(self):
        if FakeCapture.fail:
            raise FakeCapture.fail
        self.started += 1

    def stop(self):
        self.stopped += 1


def fake_capture(case, available=True, fail=None):
    FakeCapture.instances = []
    FakeCapture.fail = fail
    for name, value in (("LoopbackCapture", FakeCapture), ("available", lambda: available)):
        original = getattr(audio_loopback, name)
        setattr(audio_loopback, name, value)
        case.addCleanup(setattr, audio_loopback, name, original)


def pcm_of(value, frames=240):
    return array("h", [value] * (frames * 2)).tobytes()


# ---------------------------------------------------------------------------
# Receiving and mixing
# ---------------------------------------------------------------------------

class ReceiveTests(ShareCase):
    def setUp(self):
        super().setUp()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.sock.close)
        self.sequence = {}

    def send(self, token, pcm, session=1, sequence=None, rate=48000):
        self.sequence[token] = sequence or self.sequence.get(token, 0) + 1
        self.sock.sendto(audio_share.seal(token, session, self.sequence[token], rate, pcm), ("127.0.0.1", audio_share.PORT))

    def listen(self, volumes=None):
        self.choose(receive=True, volumes=volumes)
        self.share.start()
        wait_until(lambda: self.share.receive_status == "Listening for other PCs")

    def test_a_pc_that_streams_is_played(self):
        self.listen()
        self.send(TOKEN_A, pcm_of(1000))
        wait_until(lambda: self.share.receive_status == "Playing from Laptop")
        self.assertTrue(self.share.sources["pa"]["playing"])
        self.assertFalse(self.share.sources["pb"]["playing"])
        wait_until(lambda: self.buffered(0) == 960)

    def test_two_pcs_are_mixed_each_on_its_own_stream(self):
        self.listen()
        self.send(TOKEN_A, pcm_of(1000))
        self.send(TOKEN_B, pcm_of(2000))
        wait_until(lambda: len(self.players()) == 2)
        self.assertEqual(self.share.receive_status, "Playing from Laptop, Desk")
        players = self.players()
        wait_until(lambda: all(len(p._buffer) == 960 for p in players))
        self.assertEqual({bytes(p._buffer) for p in players}, {pcm_of(1000), pcm_of(2000)})

    def test_each_source_has_its_own_volume(self):
        self.listen(volumes={"pa": 50, "pb": 150})
        self.send(TOKEN_A, pcm_of(1000))
        self.send(TOKEN_B, pcm_of(1000))
        wait_until(lambda: len(self.players()) == 2)
        players = self.players()
        wait_until(lambda: all(len(p._buffer) == 960 for p in players))
        self.assertEqual({bytes(p._buffer) for p in players}, {pcm_of(500), pcm_of(1500)})

    def test_a_muted_source_is_not_played_but_is_still_shown(self):
        self.listen(volumes={"pa": 0})
        self.send(TOKEN_A, pcm_of(1000))
        wait_until(lambda: self.share.sources["pa"]["playing"])
        self.assertEqual(self.buffered(0), 0)

    def test_a_volume_change_takes_effect_on_the_next_datagram(self):
        self.listen()
        self.send(TOKEN_A, pcm_of(1000))
        wait_until(lambda: self.buffered(0) == 960)
        self.share.set_volume("pa", 25)
        self.send(TOKEN_A, pcm_of(1000))
        wait_until(lambda: self.buffered(0) == 1920)
        self.assertEqual(bytes(self.players()[0]._buffer[960:]), pcm_of(250))

    def test_volume_is_clamped(self):
        self.share.set_volume("pa", 900)
        self.share.set_volume("pb", -5)
        self.assertEqual(self.share.volumes, {"pa": audio_share.MAX_VOLUME, "pb": 0})

    def test_the_level_follows_the_loudness(self):
        self.listen()
        self.send(TOKEN_A, pcm_of(16384))
        wait_until(lambda: self.share.sources["pa"]["level"] == 0.5)

    def test_a_source_that_goes_quiet_is_freed_and_the_others_keep_playing(self):
        audio_share.IDLE_SECONDS = 0.3
        self.addCleanup(setattr, audio_share, "IDLE_SECONDS", 1.5)
        self.listen()
        self.send(TOKEN_A, pcm_of(1000))
        self.send(TOKEN_B, pcm_of(1000))
        wait_until(lambda: self.share.receive_status == "Playing from Laptop, Desk")
        deadline = time.monotonic() + 3
        while self.share.sources["pa"]["playing"] and time.monotonic() < deadline:
            self.send(TOKEN_B, pcm_of(1000))
            time.sleep(0.05)
        self.assertFalse(self.share.sources["pa"]["playing"])
        self.assertTrue(self.share.sources["pb"]["playing"])
        self.assertEqual(self.share.receive_status, "Playing from Desk")
        self.assertTrue(self.players()[0]._pa.terminated)

    def test_a_new_rate_reopens_only_that_source(self):
        self.listen()
        self.send(TOKEN_A, pcm_of(1000), rate=48000)
        wait_until(lambda: len(self.players()) == 1)
        self.send(TOKEN_A, pcm_of(1000, frames=220), rate=44100)
        wait_until(lambda: len(self.players()) == 2)
        self.assertEqual(self.players()[1].rate, 44100)
        self.assertTrue(self.players()[0]._pa.terminated)

    def test_wrong_tampered_short_and_unknown_datagrams_are_ignored(self):
        self.listen()
        good = audio_share.seal(TOKEN_A, 1, 1, 48000, pcm_of(1000))
        tampered = bytearray(good)
        tampered[-1] ^= 1
        for datagram in (audio_share.seal(TOKEN_C, 1, 1, 48000, pcm_of(1000)), bytes(tampered), b"x" * 10, b"y" * 200):
            self.sock.sendto(datagram, ("127.0.0.1", audio_share.PORT))
        self.send(TOKEN_A, pcm_of(1000))
        wait_until(lambda: self.share.receive_status == "Playing from Laptop")
        self.assertEqual(len(self.players()), 1)

    def test_an_oversize_datagram_does_not_stop_the_listener(self):
        self.listen()
        self.sock.sendto(b"X" * 5000, ("127.0.0.1", audio_share.PORT))
        time.sleep(0.05)
        self.send(TOKEN_A, pcm_of(1000))
        wait_until(lambda: self.share.receive_status == "Playing from Laptop")

    def test_an_unexpected_socket_error_makes_it_listen_again_instead_of_stopping(self):
        self.listen()
        real_recv = socket.socket.recv
        failed = []

        def recv(sock, size, *args):
            if not failed:
                failed.append(True)
                raise OSError(10052, "network reset")
            return real_recv(sock, size, *args)

        with patch("socket.socket.recv", recv):
            wait_until(lambda: failed)
            time.sleep(0.2)
        self.send(TOKEN_A, pcm_of(1000))
        wait_until(lambda: self.share.receive_status == "Playing from Laptop")
        self.assertTrue(self.share._workers["receive"].is_alive())

    def test_one_bad_datagram_or_source_does_not_end_the_listener(self):
        self.listen()
        real_feed = audio_share.Player.feed
        calls = []

        def feed(player, pcm):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("device vanished")
            return real_feed(player, pcm)

        with patch.object(audio_share.Player, "feed", feed):
            self.send(TOKEN_A, pcm_of(1000))
            wait_until(lambda: self.share.receive_status.startswith("Could not play sound"))
            self.send(TOKEN_A, pcm_of(1000))
            wait_until(lambda: self.share.receive_status == "Playing from Laptop")
        self.assertTrue(self.share._workers["receive"].is_alive())

    def test_a_listener_that_died_is_started_again_by_the_manager(self):
        self.listen()
        first = self.share._workers["receive"]
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        self.share._workers["receive"] = dead
        self.share.apply()
        wait_until(lambda: self.share._workers["receive"] is not dead)
        self.assertIsNot(self.share._workers["receive"], first)
        self.send(TOKEN_A, pcm_of(1000))
        wait_until(lambda: self.share.receive_status == "Playing from Laptop")

    def test_a_repeated_or_older_sequence_is_dropped_but_a_new_session_is_not(self):
        self.listen()
        self.send(TOKEN_A, pcm_of(1000), sequence=5)
        wait_until(lambda: self.buffered(0) == 960)
        self.send(TOKEN_A, pcm_of(1000), sequence=5)
        self.send(TOKEN_A, pcm_of(1000), sequence=3)
        self.send(TOKEN_A, pcm_of(2000), session=2, sequence=1)
        wait_until(lambda: self.buffered(0) == 1920)
        time.sleep(0.1)
        self.assertEqual(self.buffered(0), 1920)


# ---------------------------------------------------------------------------
# Streaming to every paired PC
# ---------------------------------------------------------------------------

class SendTests(ShareCase):
    def setUp(self):
        super().setUp()
        fake_capture(self)
        # Both peers live on the loopback address, so one socket hears every datagram.
        self.recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.recv_sock.bind(("127.0.0.1", audio_share.PORT))
        self.recv_sock.settimeout(1.0)
        self.addCleanup(self.recv_sock.close)

    def stream(self):
        self.choose(send=True)
        self.share.start()
        wait_until(lambda: FakeCapture.instances and FakeCapture.instances[0].started)
        return FakeCapture.instances[0]

    def test_it_starts_one_capture_and_says_who_gets_it(self):
        self.stream()
        self.assertEqual(len(FakeCapture.instances), 1)
        wait_until(lambda: self.share.send_status == "Streaming this PC's sound to 2 PCs")

    def test_a_chunk_becomes_sealed_slices_for_every_pc(self):
        capture = self.stream()
        capture.on_chunk(pcm_of(7, frames=480))  # 1920 bytes: two slices
        opened = []
        for _ in range(4):
            datagram = self.recv_sock.recv(4096)
            for name, token in (("pa", TOKEN_A), ("pb", TOKEN_B)):
                if datagram[:audio_share.KEY_ID_SIZE] == audio_share.audio_key_id(token):
                    opened.append((name,) + audio_share.unseal(token, datagram))
        self.assertEqual(sorted(name for name, *_ in opened), ["pa", "pa", "pb", "pb"])
        self.assertTrue(all(len(item[4]) == audio_share.SLICE_BYTES and item[3] == audio_loopback.RATE for item in opened))
        self.assertEqual(sorted({item[2] for item in opened}), [1, 2])

    def test_a_send_error_is_swallowed(self):
        capture = self.stream()
        with patch("socket.socket.sendto", side_effect=OSError("network down")):
            capture.on_chunk(pcm_of(7))

    def test_stopping_stops_the_capture(self):
        capture = self.stream()
        self.share.stop()
        self.assertEqual(capture.stopped, 1)
        self.share.stop()  # a second stop is harmless
        self.assertEqual(capture.stopped, 1)

    def test_without_a_paired_pc_there_is_nothing_to_stream_to(self):
        self.settings_path.write_text(json.dumps({"peers": []}), encoding="utf-8")
        self.choose(send=True)
        self.share.start()
        wait_until(lambda: self.share.send_status == "Pair another Windows PC to stream to")
        self.assertEqual(FakeCapture.instances, [])

    def test_a_new_address_restarts_the_stream(self):
        self.stream()
        moved = dict(PEER_A, host="127.0.0.2")
        self.settings_path.write_text(json.dumps({"peers": [moved, PEER_B]}), encoding="utf-8")
        self.share.apply()
        wait_until(lambda: len(FakeCapture.instances) == 2)
        self.assertEqual(FakeCapture.instances[0].stopped, 1)


class SendFallbackTests(ShareCase):
    """Where Windows cannot leave Beamer's own sound out, the whole mix is streamed, but never while hearing."""

    def setUp(self):
        super().setUp()
        self.recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.recv_sock.bind(("127.0.0.1", audio_share.PORT))
        self.recv_sock.settimeout(1.0)
        self.addCleanup(self.recv_sock.close)

    def test_without_exclusion_and_not_hearing_the_plain_loopback_is_streamed(self):
        fake_capture(self, available=False)
        self.fake_module.loopback = {"index": 3, "name": "Loopback", "defaultSampleRate": 44100, "maxInputChannels": 6}
        self.choose(send=True)
        self.share.start()
        wait_until(lambda: self.fake_module.all_opened_streams)
        stream = self.fake_module.all_opened_streams[0]
        self.assertEqual((stream.kwargs["input_device_index"], stream.kwargs["rate"]), (3, 44100))
        stream.callback(array("h", range(12)).tobytes(), 1, None, None)
        datagram = self.recv_sock.recv(4096)
        token = TOKEN_A if datagram[:audio_share.KEY_ID_SIZE] == audio_share.audio_key_id(TOKEN_A) else TOKEN_B
        _session, _sequence, rate, pcm = audio_share.unseal(token, datagram)
        self.assertEqual((rate, pcm), (44100, array("h", [0, 1, 6, 7]).tobytes()))

    def test_without_exclusion_and_hearing_it_refuses_but_hearing_goes_on(self):
        fake_capture(self, available=False)
        self.choose(send=True, receive=True)
        self.share.start()
        wait_until(lambda: "Turn off Hear other PCs" in self.share.send_status)
        wait_until(lambda: self.share.receive_status == "Listening for other PCs")
        self.assertEqual([s for s in self.fake_module.all_opened_streams if s.kwargs.get("input")], [])

    def test_turning_hearing_on_while_the_plain_mix_is_streamed_stops_the_stream(self):
        fake_capture(self, available=False)
        self.choose(send=True)
        self.share.start()
        wait_until(lambda: self.share.send_status.startswith("Streaming"))
        self.choose(send=True, receive=True)
        self.share.apply()
        wait_until(lambda: "Turn off Hear other PCs" in self.share.send_status)
        self.assertTrue(self.fake_module.all_opened_streams[0].is_closed)

    def test_a_capture_that_will_not_start_is_treated_the_same(self):
        fake_capture(self, fail=OSError("no process loopback"))
        self.choose(send=True, receive=True)
        self.share.start()
        wait_until(lambda: "Turn off Hear other PCs" in self.share.send_status)


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------

class ManagerTests(ShareCase):
    def setUp(self):
        super().setUp()
        fake_capture(self)

    def test_the_two_sides_start_and_stop_independently(self):
        self.choose(send=True)
        self.share.start()
        wait_until(lambda: FakeCapture.instances)
        self.assertEqual(self.share.receive_status, "")
        self.choose(send=True, receive=True)
        self.share.apply()
        wait_until(lambda: self.share.receive_status == "Listening for other PCs")
        self.assertEqual(FakeCapture.instances[0].stopped, 0)
        self.choose(send=False, receive=True)
        self.share.apply()
        wait_until(lambda: self.share.send_status == "")
        self.assertEqual(FakeCapture.instances[0].stopped, 1)
        self.choose()
        self.share.apply()
        wait_until(lambda: self.share.receive_status == "")
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(probe.close)
        probe.bind(("", audio_share.PORT))  # the port is free again

    def test_a_failure_on_one_side_leaves_the_other_running(self):
        FakeCapture.fail = RuntimeError("Audio hardware lost")
        self.choose(send=True, receive=True)
        self.share.start()
        wait_until(lambda: "Turn off Hear other PCs" in self.share.send_status)
        wait_until(lambda: self.share.receive_status == "Listening for other PCs")
        self.assertTrue(self.share._thread.is_alive())

    def test_a_port_that_is_taken_is_reported(self):
        taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        taken.bind(("", audio_share.PORT))
        self.addCleanup(taken.close)
        self.choose(receive=True)
        self.share.start()
        wait_until(lambda: self.share.receive_status)
        self.assertNotIn("Listening", self.share.receive_status)

    def test_stop_is_clean_and_repeatable(self):
        self.choose(send=True, receive=True)
        self.share.start()
        wait_until(lambda: self.share.receive_status == "Listening for other PCs")
        self.share.stop()
        self.share.stop()
        self.assertFalse(self.share._thread.is_alive())


# ---------------------------------------------------------------------------
# The Audio page (Qt, offscreen)
# ---------------------------------------------------------------------------

class AudioPageTests(ShareCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test"])

    def setUp(self):
        super().setUp()
        from PySide6.QtWidgets import QVBoxLayout, QWidget
        self.host = QWidget()
        self.panel = audio_page.AudioPanel(self.share, QVBoxLayout(self.host), self.host)

    def tearDown(self):
        self.panel._timer.stop()
        self.host.close()
        super().tearDown()

    def test_it_starts_off_with_a_card_for_each_paired_windows_pc(self):
        self.assertFalse(self.panel.send_on.isChecked() or self.panel.receive_on.isChecked())
        self.assertEqual(set(self.panel._cards), {"pa", "pb"})

    def test_pcs_that_cannot_carry_audio_get_no_card(self):
        mac = dict(PEER_A, id="pm", platform="macos")
        typed = dict(PEER_A, id="pt", token="typed by hand")
        self.settings_path.write_text(json.dumps({"peers": [PEER_A, mac, typed]}), encoding="utf-8")
        self.panel.refresh()
        self.assertEqual(set(self.panel._cards), {"pa"})
        self.assertFalse(self.panel.empty.isHidden() and False)

    def test_the_switches_are_saved(self):
        self.panel.send_on.setChecked(True)
        self.panel.receive_on.setChecked(True)
        saved = audio_share.load_audio(self.settings_path)
        self.assertTrue(saved["send"] and saved["receive"])

    def test_the_saved_choice_is_shown_again(self):
        self.choose(send=True, volumes={"pb": 40})
        self.share.volumes.clear()
        self.panel.refresh()
        self.assertTrue(self.panel.send_on.isChecked())
        self.assertFalse(self.panel.receive_on.isChecked())
        self.assertEqual(self.panel._cards["pb"].slider.value(), 40)

    def test_a_slider_changes_the_volume_at_once_and_is_saved(self):
        self.panel._cards["pa"].slider.setValue(70)
        self.assertEqual(self.share.volumes["pa"], 70)
        self.panel._save()
        self.assertEqual(audio_share.load_audio(self.settings_path)["volumes"]["pa"], 70)

    def test_mute_goes_to_zero_and_back_to_where_it_was(self):
        card = self.panel._cards["pa"]
        card.slider.setValue(60)
        card.mute.setChecked(True)
        self.assertEqual((card.slider.value(), self.share.volumes["pa"]), (0, 0))
        card.mute.setChecked(False)
        self.assertEqual(card.slider.value(), 60)

    def test_moving_the_slider_off_zero_unmutes(self):
        card = self.panel._cards["pa"]
        card.mute.setChecked(True)
        card.slider.setValue(20)
        self.assertFalse(card.mute.isChecked())

    def test_the_cards_follow_what_is_heard(self):
        card = self.panel._cards["pa"]
        self.panel._tick()
        self.assertEqual(card.state.text(), "Hearing is off")
        self.panel.receive_on.setChecked(True)
        self.share.sources = {"pa": {"name": "Laptop", "playing": True, "level": 0.5}}
        self.panel._tick()
        self.assertEqual(card.state.text(), "Streaming")
        self.assertGreater(card.meter.level, 0.4)
        self.share.sources = {"pa": {"name": "Laptop", "playing": False, "level": 0.0}}
        self.panel._tick()
        self.assertEqual(card.state.text(), "Quiet")

    def test_the_status_lines_follow_the_share(self):
        self.share.send_status = "Streaming this PC's sound to 2 PCs"
        self.share.receive_status = "Playing from Laptop"
        self.panel._tick()
        self.assertEqual(self.panel.send_status.text(), "Streaming this PC's sound to 2 PCs")
        self.assertEqual(self.panel.receive_status.text(), "Playing from Laptop")

    def test_without_a_share_the_page_only_says_it_is_for_windows(self):
        from PySide6.QtWidgets import QVBoxLayout, QWidget
        host = QWidget()
        audio_page.AudioPanel(None, QVBoxLayout(host), host)


if __name__ == "__main__":
    unittest.main()
