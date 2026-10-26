"""Audio sharing's wire format, mode file and peer list: no sound device needed."""

import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from array import array
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_share
from core import protocol

TOKEN = "A" * 43
OTHER = "B" * 43


class WireTests(unittest.TestCase):
    def test_a_datagram_opens_with_its_own_token_only(self):
        pcm = bytes(range(240)) * 4
        datagram = audio_share.seal(TOKEN, 7, 1, 48000, pcm)
        self.assertLessEqual(len(datagram), 1400)
        self.assertEqual(audio_share.unseal(TOKEN, datagram), (7, 1, 48000, pcm))
        self.assertIsNone(audio_share.unseal(OTHER, datagram))

    def test_a_changed_or_short_datagram_is_refused(self):
        datagram = bytearray(audio_share.seal(TOKEN, 7, 1, 48000, bytes(960)))
        datagram[-1] ^= 1
        self.assertIsNone(audio_share.unseal(TOKEN, bytes(datagram)))
        self.assertIsNone(audio_share.unseal(TOKEN, b"x" * 10))

    def test_the_key_id_tells_a_pair_apart_without_being_the_links_key_id(self):
        self.assertNotEqual(audio_share.audio_key_id(TOKEN), audio_share.audio_key_id(OTHER))
        self.assertNotEqual(audio_share.audio_key_id(TOKEN), protocol.key_id(TOKEN))

    def test_stereo_is_kept_mono_is_doubled_and_surround_keeps_the_front_pair(self):
        stereo = array("h", [1, 2, 3, 4]).tobytes()
        self.assertEqual(audio_share.to_stereo(stereo, 2), stereo)
        self.assertEqual(audio_share.to_stereo(array("h", [5, 6]).tobytes(), 1), array("h", [5, 5, 6, 6]).tobytes())
        surround = array("h", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]).tobytes()
        self.assertEqual(audio_share.to_stereo(surround, 6), array("h", [1, 2, 7, 8]).tobytes())


class MixTests(unittest.TestCase):
    def test_volume_scales_and_clips_instead_of_wrapping(self):
        pcm = array("h", [1000, -1000, 30000, -30000]).tobytes()
        self.assertEqual(audio_share.scaled(pcm, 100), pcm)
        self.assertEqual(array("h", audio_share.scaled(pcm, 50)).tolist(), [500, -500, 15000, -15000])
        self.assertEqual(array("h", audio_share.scaled(pcm, 150)).tolist(), [1500, -1500, 32767, -32768])
        self.assertEqual(array("h", audio_share.scaled(pcm, 0)).tolist(), [0, 0, 0, 0])

    def test_the_peak_is_the_loudest_sample(self):
        self.assertEqual(audio_share.peak(array("h", [0, -16384, 100, 5]).tobytes()), 0.5)
        self.assertEqual(audio_share.peak(b""), 0)

    def test_scaling_without_audioop_gives_the_same_answer(self):
        pcm = array("h", [1000, -1000, 30000, -30000]).tobytes()
        with unittest.mock.patch.object(audio_share, "audioop", None):
            self.assertEqual(array("h", audio_share.scaled(pcm, 150)).tolist(), [1500, -1500, 32767, -32768])
            self.assertEqual(audio_share.peak(pcm), 30000 / 32768)


class FileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.settings = Path(self.dir.name) / "settings.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_a_missing_or_damaged_file_means_off(self):
        self.assertFalse(audio_share.load_audio(self.settings)["send"] or audio_share.load_audio(self.settings)["receive"])
        (Path(self.dir.name) / audio_share.FILE_NAME).write_text("[1", encoding="utf-8")
        self.assertFalse(audio_share.load_audio(self.settings)["send"] or audio_share.load_audio(self.settings)["receive"])

    def test_a_saved_choice_comes_back(self):
        choice = {"send": True, "receive": True, "volumes": {"aaa": 80, "bbb": 0}}
        audio_share.save_choice(self.settings, choice)
        self.assertEqual(audio_share.load_audio(self.settings), choice)

    def test_the_two_switches_are_independent(self):
        audio_share.save_choice(self.settings, {"send": True, "receive": False, "volumes": {}})
        saved = audio_share.load_audio(self.settings)
        self.assertEqual((saved["send"], saved["receive"]), (True, False))

    def test_volumes_are_clamped_and_junk_is_dropped(self):
        (Path(self.dir.name) / audio_share.FILE_NAME).write_text(json.dumps({
            "send": "yes", "receive": 1,
            "volumes": {"loud": 900, "neg": -4, "text": "x", "flag": True, "ok": 55.7}}), encoding="utf-8")
        saved = audio_share.load_audio(self.settings)
        self.assertEqual((saved["send"], saved["receive"]), (False, False))
        self.assertEqual(saved["volumes"], {"loud": audio_share.MAX_VOLUME, "neg": 0, "ok": 55})

    def test_only_windows_peers_paired_by_code_can_carry_audio(self):
        good = {"id": "aaa", "name": "Laptop", "platform": "windows", "token": TOKEN, "host": "192.168.1.5"}
        self.settings.write_text(json.dumps({"peers": [
            good,
            dict(good, id="bbb", platform="macos"),
            dict(good, id="ccc", token="typed by hand"),
            dict(good, id="ddd", from_1_4=True),
            dict(good, id="eee", label="Desk", name="PC"),
        ]}), encoding="utf-8")
        peers = audio_share.paired_peers(self.settings)
        self.assertEqual([p["id"] for p in peers], ["aaa", "eee"])
        self.assertEqual(peers[1]["name"], "Desk")


if __name__ == "__main__":
    unittest.main()
