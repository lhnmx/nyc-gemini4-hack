from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from nyc_gemini4_hack.config import get_settings
from nyc_gemini4_hack.models import ProjectState
from nyc_gemini4_hack.pipeline import run_project_pipeline


async def _run(goal: str, owned: list[str], urls: list[str] | None, max_videos: int) -> None:
    project = ProjectState(
        project_id=uuid.uuid4().hex[:8],
        goal=goal,
        owned_items=owned,
    )

    async def print_event(_pid: str, event: str, data: dict[str, object]) -> None:
        if event == "status":
            print(f"[{data.get('status')}] {data.get('message')}")
        elif event == "research_complete":
            print("\n--- Decisions API Video Gating ---")
            for d in data.get("decisions", []):  # type: ignore[union-attr]
                print(
                    f"  {d['gate_name']}: {d['choice']} "
                    f"(conf={d['confidence']:.2f}, native_decisions_api={d['via_decisions_api']}) — {d['rationale']}"
                )
            print("\n--- Analyzed YouTube Videos (Gemini 4 Carbon) ---")
            for v in data.get("videos_analyzed", []):  # type: ignore[union-attr]
                print(f"  * {v['title']} ({v['url']})\n    Audio/Lang: {v['audio']}")
        elif event == "error":
            print(f"[ERROR] {data.get('message')}")

    final_state = await run_project_pipeline(
        project=project,
        publish_event=print_event,
        direct_urls=urls or None,
        max_videos=max_videos,
    )

    out_dir = get_settings().ensure_output_dir() / final_state.project_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "project_state.json"
    out_file.write_text(final_state.model_dump_json(indent=2), encoding="utf-8")

    if final_state.plan:
        plan = final_state.plan
        print(f"\n=== BUILD PLAN: {plan.project_title} ({plan.difficulty}, ~{plan.estimated_time_minutes}m) ===")
        print("\n--- Visual Continuity Bible (Hydrogen) ---")
        print(json.dumps(plan.continuity_bible.model_dump(), indent=2))
        print(f"\n--- Bill of Materials ({len(plan.materials)} items) ---")
        for m in plan.materials:
            print(f"  [{m.status.value}] {m.name} ({m.quantity}) — shopify query: '{m.search_query}'")
        print(f"\n--- Steps ({len(plan.steps)}) ---")
        for s in plan.steps:
            print(f"\n  Step {s.step_number}: {s.title} ({s.duration_seconds}s)")
            print(f"    Voiceover : {s.narration_script}")
            print(f"    Start     : {s.physical_state.start_state}")
            print(f"    Hands     : {s.physical_state.hand_kinematics}")
            print(f"    End       : {s.physical_state.end_state}")
            print(f"    POV Prompt: {s.pov_video_prompt}")
    print(f"\nSaved full JSON to: {out_file}")


def main() -> None:
    parser = argparse.ArgumentParser(description="CraftPOV CLI — Test Slice 2 end-to-end")
    parser.add_argument("goal", help="Physical build goal (e.g. 'sculpt a polymer clay frog')")
    parser.add_argument("--owned", default="", help="Comma-separated items you already own")
    parser.add_argument("--url", action="append", default=[], help="Optional direct YouTube URL(s)")
    parser.add_argument("--max-videos", type=int, default=2, help="Max YouTube videos to watch")
    args = parser.parse_args()

    owned_list = [x.strip() for x in args.owned.split(",") if x.strip()]
    asyncio.run(_run(args.goal, owned_list, args.url, args.max_videos))


if __name__ == "__main__":
    main()
