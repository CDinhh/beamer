"""Tests for Windows audio mute control: mocked COM layer and fallback behavior."""

import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_mute
from audio_mute import SpeakerMute


class MockEndpointVolume:
    """Mock COM IAudioEndpointVolume interface."""

    def __init__(self, initial_mute=0):
        self.mute = initial_mute
        self.calls = []

    def GetMute(self):
        return self.mute

    def SetMute(self, val, ctx):
        self.mute = int(val)
        self.calls.append((val, ctx))


class MockAudioDevice:
    """Mock pycaw AudioDevice."""

    def __init__(self, endpoint_volume):
        self.EndpointVolume = endpoint_volume


class AudioMuteComTests(unittest.TestCase):
    def setUp(self):
        self.epv = MockEndpointVolume(initial_mute=0)
        self.device = MockAudioDevice(self.epv)

    def test_mutes_only_if_not_muted_when_unmuted(self):
        self.epv.mute = 0
        with patch.object(audio_mute, "is_available", return_value=True), \
             patch.object(audio_mute, "_get_endpoint_volume", return_value=self.epv):
            sm = SpeakerMute()
            self.assertFalse(sm.is_active)
            self.assertFalse(sm.changed)

            result = sm.mute()
            self.assertTrue(result)
            self.assertTrue(sm.is_active)
            self.assertTrue(sm.changed)
            self.assertEqual(self.epv.mute, 1)
            self.assertEqual(len(self.epv.calls), 1)
            self.assertEqual(self.epv.calls[0], (1, None))

    def test_mutes_only_if_not_muted_when_already_muted(self):
        self.epv.mute = 1
        with patch.object(audio_mute, "is_available", return_value=True), \
             patch.object(audio_mute, "_get_endpoint_volume", return_value=self.epv):
            sm = SpeakerMute()
            result = sm.mute()
            self.assertTrue(result)
            self.assertTrue(sm.is_active)
            # Was already muted: must not be recorded as changed by us
            self.assertFalse(sm.changed)
            self.assertEqual(self.epv.mute, 1)
            # SetMute was never called
            self.assertEqual(len(self.epv.calls), 0)

    def test_restores_only_what_it_changed_unmuted_original(self):
        self.epv.mute = 0
        with patch.object(audio_mute, "is_available", return_value=True), \
             patch.object(audio_mute, "_get_endpoint_volume", return_value=self.epv):
            sm = SpeakerMute()
            sm.mute()
            self.assertEqual(self.epv.mute, 1)

            result = sm.restore()
            self.assertTrue(result)
            self.assertFalse(sm.is_active)
            self.assertFalse(sm.changed)
            self.assertEqual(self.epv.mute, 0)
            self.assertEqual(len(self.epv.calls), 2)
            self.assertEqual(self.epv.calls[1], (0, None))

    def test_restores_only_what_it_changed_muted_original(self):
        self.epv.mute = 1
        with patch.object(audio_mute, "is_available", return_value=True), \
             patch.object(audio_mute, "_get_endpoint_volume", return_value=self.epv):
            sm = SpeakerMute()
            sm.mute()
            self.assertEqual(self.epv.mute, 1)

            result = sm.restore()
            self.assertTrue(result)
            self.assertFalse(sm.is_active)
            self.assertFalse(sm.changed)
            # Speakers remain muted because they were already muted
            self.assertEqual(self.epv.mute, 1)
            self.assertEqual(len(self.epv.calls), 0)

    def test_restore_without_mute_is_noop(self):
        self.epv.mute = 0
        with patch.object(audio_mute, "is_available", return_value=True), \
             patch.object(audio_mute, "_get_endpoint_volume", return_value=self.epv):
            sm = SpeakerMute()
            result = sm.restore()
            self.assertFalse(result)
            self.assertEqual(len(self.epv.calls), 0)

    def test_idempotent_mute_and_restore(self):
        self.epv.mute = 0
        with patch.object(audio_mute, "is_available", return_value=True), \
             patch.object(audio_mute, "_get_endpoint_volume", return_value=self.epv):
            sm = SpeakerMute()
            # Multiple mutes call SetMute once
            self.assertTrue(sm.mute())
            self.assertTrue(sm.mute())
            self.assertEqual(len(self.epv.calls), 1)

            # Multiple restores call SetMute once
            self.assertTrue(sm.restore())
            self.assertFalse(sm.restore())
            self.assertEqual(len(self.epv.calls), 2)
            self.assertEqual(self.epv.mute, 0)

    def test_restores_on_exception_in_context_manager(self):
        self.epv.mute = 0
        with patch.object(audio_mute, "is_available", return_value=True), \
             patch.object(audio_mute, "_get_endpoint_volume", return_value=self.epv):
            with self.assertRaises(RuntimeError):
                with SpeakerMute() as sm:
                    self.assertTrue(sm.is_active)
                    self.assertEqual(self.epv.mute, 1)
                    raise RuntimeError("Something failed while sending")

            self.assertEqual(self.epv.mute, 0)
            self.assertFalse(sm.is_active)
            self.assertEqual(len(self.epv.calls), 2)
            self.assertEqual(self.epv.calls[1], (0, None))

    def test_stop_alias(self):
        self.epv.mute = 0
        with patch.object(audio_mute, "is_available", return_value=True), \
             patch.object(audio_mute, "_get_endpoint_volume", return_value=self.epv):
            sm = SpeakerMute()
            sm.mute()
            self.assertEqual(self.epv.mute, 1)
            sm.stop()
            self.assertEqual(self.epv.mute, 0)
            self.assertFalse(sm.is_active)


class AudioMuteMissingLibTests(unittest.TestCase):
    def test_noop_when_lib_missing(self):
        with patch.object(audio_mute, "_COM_AVAILABLE", False):
            self.assertFalse(audio_mute.is_available())
            self.assertIsNone(audio_mute.get_mute())
            self.assertFalse(audio_mute.set_mute(True))
            self.assertFalse(audio_mute.set_mute(False))

            sm = SpeakerMute()
            self.assertFalse(sm.mute())
            self.assertFalse(sm.is_active)
            self.assertFalse(sm.restore())
            self.assertFalse(sm.stop())

            # Context manager operates cleanly without error
            with SpeakerMute() as active_sm:
                self.assertFalse(active_sm.is_active)

    def test_device_none_fails_gracefully(self):
        with patch.object(audio_mute, "is_available", return_value=True), \
             patch.object(audio_mute, "_get_endpoint_volume", return_value=None):
            self.assertIsNone(audio_mute.get_mute())
            self.assertFalse(audio_mute.set_mute(True))

            sm = SpeakerMute()
            self.assertFalse(sm.mute())
            self.assertFalse(sm.is_active)
            self.assertFalse(sm.restore())


class AudioMuteConcurrencyTests(unittest.TestCase):
    def test_thread_safety(self):
        epv = MockEndpointVolume(initial_mute=0)
        with patch.object(audio_mute, "is_available", return_value=True), \
             patch.object(audio_mute, "_get_endpoint_volume", return_value=epv):
            sm = SpeakerMute()
            barrier = threading.Barrier(5)

            def worker():
                barrier.wait()
                sm.mute()
                sm.restore()

            threads = [threading.Thread(target=worker) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(2.0)

            self.assertFalse(sm.is_active)
            self.assertFalse(sm.changed)


if __name__ == "__main__":
    unittest.main()
