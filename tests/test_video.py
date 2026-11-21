import asyncio
import base64
import math
import subprocess
import tempfile
import unittest
import wave
from array import array
from pathlib import Path
from unittest.mock import Mock

import imageio_ffmpeg
from google.genai import types

from nyc_gemini4_hack.media.composer import compose_step_pov_video, stitch_full_tutorial
from nyc_gemini4_hack.media.video_gen import generate_step_pov_frames
from nyc_gemini4_hack.models import BuildStep, PhysicalStateTransition, VisualContinuityBible


# One red, green, and blue pixel. Changes in shot order are detectable after encoding.
PNG_FRAMES = tuple(base64.b64decode(value) for value in (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC",
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNg+M8AAAICAQB7CYF4AAAAAElFTkSuQmCC",
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYPgPAAEDAQAIicLsAAAAAElFTkSuQmCC",
))


def write_tone(path, seconds, sample_rate):
    samples = array('h', (round(12000 * math.sin(2 * math.pi * 400 * i / sample_rate))
                          for i in range(round(seconds * sample_rate))))
    with wave.open(str(path), 'wb') as out:
        out.setparams((1, 2, sample_rate, len(samples), 'NONE', 'not compressed'))
        out.writeframes(samples.tobytes())


def decode(path, output_args):
    return subprocess.check_output([
        imageio_ffmpeg.get_ffmpeg_exe(), '-loglevel', 'error', '-i', str(path), *output_args, '-',
    ])


class VideoTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(suffix="'s bench")
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.step = BuildStep(
            step_number=2, title='Fold the flap', instruction_text='Fold the paper in half.',
            narration_script='Line up the corners, then press the fold flat.',
            pov_video_prompt='Thumb pins the lower left corner.',
            physical_state=PhysicalStateTransition(
                start_state='One unfolded square', hand_kinematics='Index finger traces the crease',
                end_state='One doubled triangle', verification_cues=['Corners meet edge to edge'],
            ),
        )
        self.bible = VisualContinuityBible(
            workspace_surface='maple table', lighting='left window', camera_style='fixed overhead',
            subject_palette='navy paper', hands_description='short nails',
        )

    def test_three_phases_chain_actual_image_bytes_and_preserve_craft(self):
        client = Mock()
        client.settings.pov_image_model = 'test-image'
        calls = client.client.models.generate_content
        calls.side_effect = [types.GenerateContentResponse(candidates=[
            types.Candidate(),
            types.Candidate(content=types.Content(parts=[
                types.Part(inline_data=types.Blob(data=PNG_FRAMES[2], mime_type='image/png'), thought=True),
                types.Part.from_bytes(data=b'unusable image', mime_type='image/jpeg'),
                types.Part(inline_data=types.Blob(data=data)),
            ])),
        ]) for data in PNG_FRAMES]
        previous = self.folder / 'old_project_frame.jpg'
        previous.write_bytes(PNG_FRAMES[2])
        paths = asyncio.run(generate_step_pov_frames(self.step, self.bible, self.folder, client, previous))

        self.assertEqual([p.read_bytes() for p in paths], list(PNG_FRAMES))
        self.assertEqual([p.suffix for p in paths], ['.png'] * 3)
        prompts = []
        for call, prior in zip(calls.call_args_list, [previous, *paths[:2]], strict=True):
            ref, prompt = call.kwargs['contents']
            self.assertEqual(ref.inline_data.data, prior.read_bytes())
            self.assertEqual(ref.inline_data.mime_type, 'image/png')
            self.assertEqual(call.kwargs['config'].image_config.aspect_ratio, '16:9')
            self.assertIn('maple table', prompt)
            self.assertIn('fixed overhead', prompt)
            self.assertNotIn('clay', prompt.lower())
            prompts.append(prompt)
        state = self.step.physical_state
        self.assertIn(state.start_state, prompts[0])
        self.assertNotIn(state.end_state, prompts[0])
        self.assertIn(state.hand_kinematics, prompts[1])
        self.assertIn(self.step.pov_video_prompt, prompts[1])
        self.assertIn(state.end_state, prompts[2])
        self.assertIn(state.verification_cues[0], prompts[2])

    def test_no_image_stops_at_setup(self):
        client = Mock()
        client.client.models.generate_content.return_value = types.GenerateContentResponse(
            candidates=[types.Candidate()]
        )
        with self.assertRaisesRegex(RuntimeError, 'no final POV image'):
            asyncio.run(generate_step_pov_frames(self.step, self.bible, self.folder, client))
        client.client.models.generate_content.assert_called_once()
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_render_preserves_shot_order_audio_length_and_stitched_narration(self):
        frames = []
        for idx, data in enumerate(PNG_FRAMES):
            frame = self.folder / f'{idx}.png'
            frame.write_bytes(data)
            frames.append(frame)
        voice = self.folder / 'voice.wav'
        write_tone(voice, 0.2, 24000)
        first = asyncio.run(compose_step_pov_video(frames, voice, self.folder / 'first.mp4', 3))
        pixels = decode(first, ['-map', '0:v', '-vf', 'scale=1:1', '-pix_fmt', 'rgb24', '-f', 'rawvideo'])
        self.assertEqual(len(pixels), 72 * 3)
        for frame_index, channel in ((0, 0), (36, 1), (71, 2)):
            pixel = pixels[frame_index * 3:frame_index * 3 + 3]
            self.assertGreater(pixel[channel], 100)
            self.assertEqual(pixel[channel], max(pixel))

        write_tone(voice, 0.7, 44100)
        second = asyncio.run(compose_step_pov_video(frames[:1], voice, self.folder / 'second.mp4', 1))
        tutorial = asyncio.run(stitch_full_tutorial([first, second], self.folder / 'full.mp4'))
        stitched_pixels = decode(tutorial, ['-map', '0:v', '-vf', 'scale=1:1',
                                            '-fps_mode', 'passthrough', '-pix_fmt', 'rgb24', '-f', 'rawvideo'])
        self.assertEqual(len(stitched_pixels), (72 + 36) * 3)
        self.assertEqual(max(stitched_pixels[-3:]), stitched_pixels[-3])
        samples = array('h')
        samples.frombytes(decode(tutorial, ['-map', '0:a', '-ar', '8000', '-f', 's16le']))
        self.assertAlmostEqual(len(samples) / 8000, 4.5, delta=0.08)
        for start, audible in ((0.07, True), (0.4, False), (2.9, False), (3.1, True), (4.15, False)):
            window = samples[round(start * 8000):round((start + 0.03) * 8000)]
            self.assertEqual(max(abs(s) for s in window) > 1000, audible, f'audio at {start}s')


if __name__ == '__main__':
    unittest.main()
