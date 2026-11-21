from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from nyc_gemini4_hack.config import get_settings
from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.models import ProjectState, ProjectStatus
from nyc_gemini4_hack.pipeline import generate_media_for_plan, run_project_pipeline

logger = logging.getLogger(__name__)
DEFAULT_GOAL = "Fold a Himmel und Hölle origami paper fortune teller"


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


async def _run(
    project: ProjectState,
    urls: list[str] | None,
    max_videos: int,
    skip_media: bool,
    max_media_steps: int | None,
    render_saved_plan: bool,
) -> ProjectStatus:
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

    if render_saved_plan:
        gemini = Gemini4Client()
        try:
            await generate_media_for_plan(
                project=project,
                gemini=gemini,
                publish_event=print_event,
                max_steps=max_media_steps,
            )
        except Exception:
            logger.exception("Could not render saved project %s", project.project_id)
            project.status = ProjectStatus.FAILED
            project.error = "Could not render the saved guide. Check the output directory and media configuration."
            project.status_message = project.error
            print(f"[{project.status.value}] {project.error}")
        finally:
            await gemini.aclose()
    else:
        project = await run_project_pipeline(
            project=project,
            publish_event=print_event,
            direct_urls=urls or None,
            max_videos=max_videos,
            generate_media=not skip_media,
            max_media_steps=max_media_steps,
        )

    out_dir = get_settings().ensure_output_dir() / project.project_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "project_state.json"
    with TemporaryDirectory(dir=out_dir) as temp_dir:
        staged = Path(temp_dir) / out_file.name
        staged.write_text(project.model_dump_json(indent=2), encoding="utf-8")
        staged.replace(out_file)

    if project.plan and not render_saved_plan:
        plan = project.plan
        print(f"\n=== BUILD PLAN: {plan.project_title} ({plan.difficulty}, ~{plan.estimated_time_minutes}m) ===")
        print("\n--- Visual Continuity Bible (Hydrogen) ---")
        print(json.dumps(plan.continuity_bible.model_dump(), indent=2))
        print(f"\n--- Bill of Materials ({len(plan.materials)} items) ---")
        for m in plan.materials:
            print(f"  [{m.status.value}] {m.name} ({m.quantity}) — shopify query: '{m.search_query}'")
    print(f"\nSaved full JSON to: {out_file}")
    return project.status


def main() -> None:
    parser = argparse.ArgumentParser(description="CraftPOV CLI — End-to-End Pipeline & POV Video Generator")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("goal", nargs="?", help=f"Physical build goal (default: {DEFAULT_GOAL})")
    source.add_argument("--from-json", type=Path, help="Render POV media directly from an existing project_state.json")
    parser.add_argument("--owned", default="", help="Comma-separated items you already own")
    parser.add_argument("--url", action="append", default=[], help="Optional direct YouTube URL(s)")
    parser.add_argument("--max-videos", type=positive_int, default=1, help="Number of usable YouTube tutorials to analyze")
    parser.add_argument("--skip-media", action="store_true", help="Skip POV video & voice generation")
    parser.add_argument("--max-media-steps", type=positive_int, default=None, help="Limit number of steps to render POV video for")
    args = parser.parse_args()

    if args.from_json is not None:
        if args.skip_media:
            parser.error("--from-json renders media and cannot be combined with --skip-media")
        try:
            project = ProjectState.model_validate_json(args.from_json.read_bytes())
        except OSError as exc:
            parser.error(f"Could not read {args.from_json}: {exc.strerror}")
        except ValidationError as exc:
            error = exc.errors(include_input=False)[0]
            location = ".".join(str(part) for part in error["loc"]) or "project"
            parser.error(f"Invalid saved project in {args.from_json} ({location}): {error['msg']}")
        if not project.plan or not project.plan.steps:
            parser.error("The saved project needs a build plan with steps to render.")
        print(f"Loaded existing BuildPlan from {args.from_json} ({project.project_id})")
    else:
        goal = DEFAULT_GOAL if args.goal is None else args.goal.strip()
        if not goal:
            parser.error("Provide a build goal.")
        project = ProjectState(
            project_id=uuid.uuid4().hex[:8],
            goal=goal,
            owned_items=[item.strip() for item in args.owned.split(",") if item.strip()],
        )

    try:
        status = asyncio.run(_run(
            project=project,
            urls=args.url,
            max_videos=args.max_videos,
            skip_media=args.skip_media,
            max_media_steps=args.max_media_steps,
            render_saved_plan=args.from_json is not None,
        ))
    except OSError as exc:
        parser.exit(1, f"Could not save the project output: {exc}\n")
    if status != ProjectStatus.COMPLETED:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
