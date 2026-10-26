"""Audio sharing's wire format, mode file and peer list: no sound device needed."""

import json
import os
import sys
import tempfile
import unittest
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


class FileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.settings = Path(self.dir.name) / "settings.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_a_missing_or_damaged_file_means_off(self):
        self.assertFalse(audio_share.load_audio(self.settings)["send"]["on"] or audio_share.load_audio(self.settings)["receive"]["on"])
        (Path(self.dir.name) / audio_share.FILE_NAME).write_text("[1", encoding="utf-8")
        self.assertFalse(audio_share.load_audio(self.settings)["send"]["on"] or audio_share.load_audio(self.settings)["receive"]["on"])

    def test_a_saved_choice_comes_back(self):
        audio_share.save_audio(self.settings, "receive", "abc", "Speakers")
        saved = audio_share.load_audio(self.settings)
        self.assertEqual(saved["receive"], {"on": True, "peer": "abc", "output": "Speakers"})
        self.assertFalse(saved["send"]["on"])
        with self.assertRaises(ValueError):
            audio_share.save_audio(self.settings, "both")

    def test_both_sides_can_be_on_at_once(self):
        choice = {"send": {"on": True, "peer": "a", "source": "CABLE Input", "mute_local": False},
                  "receive": {"on": True, "peer": "", "output": "Speakers"}}
        audio_share.save_choice(self.settings, choice)
        self.assertEqual(audio_share.load_audio(self.settings), choice)

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
