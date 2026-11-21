from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from nyc_gemini4_hack.config import get_settings
from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.models import ProjectState
from nyc_gemini4_hack.pipeline import generate_media_for_plan, run_project_pipeline


async def _run(
    goal: str | None,
    owned: list[str],
    urls: list[str] | None,
    max_videos: int,
    skip_media: bool,
    max_media_steps: int | None,
    from_json: str | None,
) -> None:
    async def print_event(_pid: str, event: str, data: dict[str, object]) -> None:
        if event == "status":
            print(f"[{data.get('status')}] {data.get('message')}")
        elif event == "research_complete":
            print("\n--- Decisions API Video Gating ---")
            for d in data.get("decisions", []):  # type: ignore[union-attr]
                print(
                    f"  {d['gate_name']}: {d['choice']} "
                    f"(conf={d['confidence']:.2f}, native={d['via_decisions_api']}) — {d['rationale']}"
                )
            print("\n--- Analyzed YouTube Videos (Gemini 4 Carbon) ---")
            for v in data.get("videos_analyzed", []):  # type: ignore[union-attr]
                print(f"  * {v['title']} ({v['url']})\n    Audio/Lang: {v['audio']}")
        elif event == "step_media_ready":
            print(f"  ✓ Step {data.get('step_number')} POV MP4 ready -> {data.get('video_url')}")
        elif event == "step_media_error":
            print(f"  ✗ Step {data.get('step_number')} media error -> {data.get('error')}")
        elif event == "full_video_ready":
            print(f"\n🎬 Full stitched POV tutorial ready -> {data.get('video_url')}")
        elif event == "error":
            print(f"[ERROR] {data.get('message')}")

    if from_json:
        state_path = Path(from_json)
        final_state = ProjectState.model_validate_json(state_path.read_text(encoding="utf-8"))
        print(f"Loaded existing BuildPlan from {state_path} ({final_state.project_id})")
        await generate_media_for_plan(
            project=final_state,
            gemini=Gemini4Client(),
            publish_event=print_event,
            max_steps=max_media_steps,
        )
    else:
        resolved_goal = goal or "Fold a Himmel und Hölle origami paper fortune teller"
        project = ProjectState(
            project_id=uuid.uuid4().hex[:8],
            goal=resolved_goal,
            owned_items=owned,
        )
        final_state = await run_project_pipeline(
            project=project,
            publish_event=print_event,
            direct_urls=urls or None,
            max_videos=max_videos,
            generate_media=not skip_media,
            max_media_steps=max_media_steps,
        )

    out_dir = get_settings().ensure_output_dir() / final_state.project_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "project_state.json"
    out_file.write_text(final_state.model_dump_json(indent=2), encoding="utf-8")

    if final_state.plan and not from_json:
        plan = final_state.plan
        print(f"\n=== BUILD PLAN: {plan.project_title} ({plan.difficulty}, ~{plan.estimated_time_minutes}m) ===")
        print("\n--- Visual Continuity Bible (Hydrogen) ---")
        print(json.dumps(plan.continuity_bible.model_dump(), indent=2))
        print(f"\n--- Bill of Materials ({len(plan.materials)} items) ---")
        for m in plan.materials:
            print(f"  [{m.status.value}] {m.name} ({m.quantity}) — shopify query: '{m.search_query}'")
    print(f"\nSaved full JSON to: {out_file}")


def main() -> None:
    parser = argparse.ArgumentParser(description="CraftPOV CLI — End-to-End Pipeline & POV Video Generator")
    parser.add_argument("goal", nargs="?", help="Physical build goal (default: 'Fold a Himmel und Hölle origami paper fortune teller')")
    parser.add_argument("--owned", default="", help="Comma-separated items you already own")
    parser.add_argument("--url", action="append", default=[], help="Optional direct YouTube URL(s)")
    parser.add_argument("--max-videos", type=int, default=2, help="Max YouTube videos to watch")
    parser.add_argument("--skip-media", action="store_true", help="Skip POV video & voice generation")
    parser.add_argument("--max-media-steps", type=int, default=None, help="Limit number of steps to render POV video for")
    parser.add_argument("--from-json", default=None, help="Render POV media directly from an existing project_state.json")
    args = parser.parse_args()

    owned_list = [x.strip() for x in args.owned.split(",") if x.strip()]
    asyncio.run(
        _run(
            goal=args.goal,
            owned=owned_list,
            urls=args.url,
            max_videos=args.max_videos,
            skip_media=args.skip_media,
            max_media_steps=args.max_media_steps,
            from_json=args.from_json,
        )
    )


if __name__ == "__main__":
    main()
