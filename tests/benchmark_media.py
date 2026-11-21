"""Offline replay measuring when the first chapter and the full captioned film become playable."""
import argparse
import asyncio
import base64
import hashlib
import json
import statistics
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from unittest.mock import patch

import httpx
from google import genai

from nyc_gemini4_hack import pipeline
from nyc_gemini4_hack.config import Settings
from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.models import ProjectState

EXAMPLE = Path(__file__).resolve().parents[1] / "src/nyc_gemini4_hack/web/example"


@dataclass
class Timing:
    first_chapter: float
    full_film: float
    peak_shots: int
    film_hash: str
    captions_hash: str


def replay_inputs(scenario):
    project = ProjectState.model_validate_json((EXAMPLE / "project.json").read_bytes())
    source_steps = project.plan.steps
    # Each copy of the paper has its own unbroken series of folds.
    if scenario == "two-sheets":
        sequence = [(1, "First sheet", 0), (1, "Second sheet", 0), (2, "First sheet", 1),
                    (2, "Second sheet", 2), (3, "First sheet", 3)]
    else:
        sequence = [(step.step_number, "", -1) for step in source_steps]
    steps, videos, voices = [], {}, {}
    for number, (source, sheet, parent) in enumerate(sequence, start=1):
        step = source_steps[source - 1].model_copy(deep=True)
        step.step_number = number
        step.visual_parent = parent
        if sheet:
            step.title = f"{sheet} · {step.title}"
        steps.append(step)
        videos[step.title] = (EXAMPLE / f"step-{source}.mp4").read_bytes()
        voices[step.narration_script] = (EXAMPLE / f"step-{source}.wav").read_bytes()
    project.plan.steps = steps
    project.media.clear()
    return project, videos, voices


async def render(scenario: str, limit: int) -> Timing:
    project, videos, voices = replay_inputs(scenario)
    active_shots = peak_shots = 0
    ready_times = []

    async def provider(request):
        nonlocal active_shots, peak_shots
        body = json.loads(request.content)
        if request.url.path.endswith(":generateContent"):
            # Replay a successful review so dependent shots receive their reference image.
            images = [part for content in body["contents"] for part in content["parts"] if "inlineData" in part]
            findings = {name: {"verdict": "matches", "sample": sample, "observation": "Offline replay."}
                        for name, sample in [("opening", 1), ("contact", (len(images) + 1) // 2), ("result", len(images))]}
            return httpx.Response(200, json={"candidates": [{"content": {
                "role": "model", "parts": [{"text": json.dumps(findings)}]}, "finishReason": "STOP"}]})
        kind = body["response_format"]["type"]
        if kind == "video":
            prompt = body["input"][0]["content"][-1]["text"]
            data = next(data for title, data in videos.items() if f"Action — {title}:" in prompt)
            mime = "video/mp4"
            active_shots += 1
            peak_shots = max(peak_shots, active_shots)
            try:
                await asyncio.sleep(1.4)
            finally:
                active_shots -= 1
        else:
            script = body["input"][0]["content"][0]["text"]
            data, mime = voices[script], "audio/wav"
            await asyncio.sleep(0.3)
        return httpx.Response(200, json={"id": kind, "status": "completed", "steps": [{
            "type": "model_output", "content": [{
                "type": kind, "mime_type": mime, "data": base64.b64encode(data).decode("ascii"),
            }],
        }]})

    async def publish(_id, event, _data):
        if event == "step_media_ready":
            ready_times.append(monotonic() - started)

    with tempfile.TemporaryDirectory() as directory:
        settings = Settings(_env_file=None, gemini_api_key="offline", media_output_dir=Path(directory),
                            max_parallel_shots=limit)
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
            gemini = Gemini4Client(settings)
            gemini._client = genai.Client(api_key="offline", http_options={"httpx_async_client": transport})
            try:
                with patch.object(pipeline, "get_settings", return_value=settings):
                    started = monotonic()
                    full = await pipeline.generate_media_for_plan(project, gemini, publish)
                    elapsed = monotonic() - started
            finally:
                await gemini.aclose()
        assert full and full.stat().st_size and full.with_suffix(".vtt").is_file(), project.status_message
        assert all(asset.status == "ready" and asset.shot_reviews[-1].status == "passed"
                   for asset in project.media.values())
        assert peak_shots == (limit if scenario == "two-sheets" else 1)
        return Timing(ready_times[0], elapsed, peak_shots, hashlib.sha256(full.read_bytes()).hexdigest(),
                      hashlib.sha256(full.with_suffix(".vtt").read_bytes()).hexdigest())


async def compare(scenarios, runs):
    for scenario in scenarios:
        results = {1: [], 2: []}
        for index in range(runs):
            for limit in ([1, 2] if index % 2 == 0 else [2, 1]):
                timing = await render(scenario, limit)
                results[limit].append(timing)
                print(f"{scenario} limit={limit} · first {timing.first_chapter:.3f}s · full {timing.full_film:.3f}s "
                      f"· peak {timing.peak_shots}", flush=True)
        for limit, group in results.items():
            print(f"{scenario} limit={limit} median · first {statistics.median(t.first_chapter for t in group):.3f}s "
                  f"· full {statistics.median(t.full_film for t in group):.3f}s")
        hashes = {(t.film_hash, t.captions_hash) for group in results.values() for t in group}
        assert len(hashes) == 1, "The replay produced different MP4 or caption bytes."
        film, captions = hashes.pop()
        print(f"{scenario} · MP4 SHA-256 {film} · VTT SHA-256 {captions}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["one-glider", "two-sheets", "both"], default="both")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    asyncio.run(compare(["one-glider", "two-sheets"] if args.scenario == "both" else [args.scenario], args.runs))
