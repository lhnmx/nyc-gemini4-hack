# Video generation speed and continuity

Independent workpieces can render at the same time. Each fold of a single paper glider still starts with the real final frame of its preceding fold. Every chapter becomes playable as soon as its narration and captions are ready.

## What changed

The grounded plan records which shot supplies each chapter's opening frame. A value of `-1` in `visual_parent` continues the preceding chapter, `0` starts an independent setup, and a positive value names the earlier chapter whose workpiece is returning to camera. Older plans default to the preceding chapter. Each setup uses the same description of the maker, work surface, framing and lighting.

After each shot downloads and passes validation, the worker extracts its final frame on the 24 fps playback timeline. A child shot can start as soon as that frame exists, while its parent receives narration and captions. A failed shot's children resume from their written opening states. Independent sequences share up to two visual requests per project. Set `MAX_PARALLEL_SHOTS` to a value from 1 through 4 to adjust this limit. Narration and chapter assembly each have a separate limit of two active jobs.

The workbench receives a chapter's start event when a visual slot opens and its ready event once its playable files are published. A later chapter can become ready before an earlier chapter. The full film, combined captions and chapter seek positions always use the plan's order.

Each independent opening establishes the maker, work surface, framing and lighting from the continuity description. Each continuation prompt identifies the attached image as the opening frame and describes grip, support, motion and the visible finish. Google recommends a clear source image, a specific motion description and an explicit single-shot instruction. [Gemini Omni prompt guide](https://aistudio.google.com/docs/omni#prompt-guide) · [Google image-to-video guidance](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/video/best-practice)

The worker requests 16:9, 720p footage from `gemini-omni-1.2-flash` through the Interactions API. It sizes the shot to the planned action and, when already available, the measured speech recording. Otherwise it estimates the narration duration. Requests use whole seconds within Omni's 3–15 s range. Composition preserves every moving frame and holds the final result when the narrator needs more time. The final stitch copies the video streams. [Gemini Omni API guide](https://aistudio.google.com/docs/omni#control-video-duration) · [Model specification](https://aistudio.google.com/docs/models/gemini-omni-flash)

## Measuring the scheduler

The benchmark replays committed example MP4s and WAVs through the Google SDK's async HTTP transport. Each video response takes 1.4 s and each voice response takes 0.3 s. The worker performs real decoding, continuity-frame extraction, narration handling, 720p chapter encoding, caption alignment and final assembly. The first timestamp is the first playable chapter event. The second is the availability of the full MP4 and WebVTT captions locally. Timings exclude research, planning and hosted uploads.

The single-glider replay uses the original sequence of five dependent folds. The two-sheet replay alternates between two sheets folded independently for five chapters, with each continuing fold referring to its own sheet. Both scenarios run at one and two permitted visual requests. The benchmark counts requests actually in flight and compares both MP4 and caption bytes across runs.

| Five-chapter replay | Shot limit | Peak simultaneous video requests | Median first playable chapter | Median full film |
| --- | ---: | ---: | ---: | ---: |
| One glider | 1 | 1 | 1.946 s | 8.712 s |
| One glider | 2 | 1 | 1.909 s | 8.511 s |
| Two sheets | 1 | 1 | 2.076 s | 8.563 s |
| Two sheets | 2 | 2 | 2.092 s | 5.359 s |

Across three runs per setting, the two-sheet film finished **37.4% sooner** at the higher limit. Each scenario produced byte-identical films and captions at both limits. All single-glider runs kept one video request in flight. These worker measurements used temporary definitions for the new project metadata, concurrency setting and voice-source callback during integration. Provider responses were replayed locally.

Run `uv run python tests/benchmark_media.py` for three alternating comparisons per scenario. Use `--scenario two-sheets --runs 1` for one short comparison. Each render writes to a temporary folder.

## Earlier sequential baseline

In an earlier offline replay, overlapping narration and assembly with the next dependent request reduced the median time for the single-glider film by 14.3%.

| Version | Three alternating runs, seconds | Median |
| --- | --- | --- |
| Baseline, commit `67e5088` | 10.168, 10.106, 10.139 | 10.139 s |
| Overlapped rendering | 8.690, 8.628, 8.840 | 8.690 s |

The SHA-256 of the final film was `2a0e94c324a0faacb70f70115048e775938f460e2711fd836f9037c0406995de` in all six historical runs. Pipeline and provider tests check actual reference frames, frame-accurate chapter starts, narration extending past footage, out-of-order playback availability, and cancellation with independent shots in flight.

## Where a live film still spends time

In a previously saved six-step local job, the five intervals between source-footage arrivals were 32.80, 35.23, 34.69, 33.27 and 37.55 s. The corresponding work from each source arriving to its captioned chapter being written took 0.55–0.96 s. These are historical file timestamps. They put the largest wait on the path leading up to the next footage download. Per-shot logs now time generation, retrieval and final-frame extraction individually. The workbench records elapsed media time and ready-chapter counts.

Google documents duration, resolution and service load as factors in generation time. It recommends synchronous, stateless requests for speed and URI delivery for video payloads over 4 MB. The renderer uses stored URI delivery, polls the Files API until the video is active and downloads it once. This handles longer or high-detail shots that can exceed the inline limit. A live comparison of short inline shots would need to measure payload size, failed deliveries, total time to a published film and visual continuity. [Omni performance guidance](https://aistudio.google.com/docs/omni#best-practices) · [File retrieval](https://aistudio.google.com/docs/omni#retrieving-videos-with-an-uri)
