import asyncio
import tempfile
import unittest
import wave
from pathlib import Path

from nyc_gemini4_hack.media.composer import compose_step_pov_video, stitch_full_tutorial


class ComposerFailureTests(unittest.TestCase):
    def test_failed_regeneration_preserves_existing_files_and_removes_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            output = directory / "tutorial.mp4"
            output.write_bytes(b"previous render")
            voice = directory / "voice.wav"
            with wave.open(str(voice), "wb") as wav:
                wav.setparams((1, 2, 8000, 800, "NONE", "not compressed"))
                wav.writeframes(bytes(1600))

            with self.assertRaisesRegex(RuntimeError, "FFmpeg could not produce tutorial.mp4"):
                asyncio.run(compose_step_pov_video([directory / "absent.png"], voice, output))
            self.assertEqual(output.read_bytes(), b"previous render")

            with self.assertRaisesRegex(RuntimeError, "FFmpeg could not produce tutorial.mp4"):
                asyncio.run(stitch_full_tutorial([directory / "absent.mp4"], output))
            self.assertEqual(output.read_bytes(), b"previous render")
            self.assertEqual(set(directory.iterdir()), {output, voice})
