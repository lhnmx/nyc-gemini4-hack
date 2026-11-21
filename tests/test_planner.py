import json
import unittest
from unittest.mock import AsyncMock

from pydantic import ValidationError

from nyc_gemini4_hack.agents.planner import _HydrogenGroundingOutput, _WrittenPlan, synthesize_build_plan
from nyc_gemini4_hack.models import (
    BuildPlan, BuildStep, MaterialItem, MaterialStatus, PhysicalStateTransition,
    TutorialExtract, TutorialSegment, VisualContinuityBible,
)


def make_plan():
    bible = VisualContinuityBible(
        workspace_surface="oak", lighting="left window", camera_style="steady overhead", subject_palette="white",
    )
    paper = PhysicalStateTransition(start_state="flat", hand_kinematics="pinch the crease", end_state="folded")
    first = BuildStep(
        step_number=1, title="Fold", instruction_text="Fold in half and match corners.",
        narration_script="Fold the paper until the corners meet.", pov_video_prompt="Fold paper.", physical_state=paper,
    )
    second = first.model_copy(update={"step_number": 2, "title": "Flatten"})
    names = ["Water", "Watercolor paint", "Clay knife"]
    return BuildPlan(
        project_title="Paper model", continuity_bible=bible, steps=[first, second],
        materials=[
            MaterialItem(id=str(idx), name=name, search_query=name, status=MaterialStatus.OWNED_EXACT)
            for idx, name in enumerate(names)
        ],
        source_youtube_urls=["https://invented-source.invalid/", "https://youtu.be/source-a", "https://youtu.be/source-a"],
    )


def grounding(plan, step_numbers):
    return _HydrogenGroundingOutput(
        continuity_bible=plan.continuity_bible.model_copy(update={"lighting": "daylight"}),
        steps=[{
            "step_number": number, "pov_video_prompt": f"Show action {number}",
            "physical_state": {
                "start_state": f"State {number - 1}", "hand_kinematics": f"Motion {number}",
                "end_state": f"State {number}", "verification_cues": ["Edges match"],
            },
        } for number in step_numbers],
    )


def tutorial(url):
    return TutorialExtract(video_id=url.split("/")[-1], title="Fold", url=url)


class PlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_chapter_keeps_only_its_resolved_footage_and_time_range(self):
        first_url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        second_url = "https://www.youtube.com/watch?v=bbbbbbbbbbb"
        sources = [
            TutorialExtract(video_id="aaaaaaaaaaa", url=first_url, title="Clamp setup", segments=[
                TutorialSegment(timestamp_start="0:17", timestamp_end="0:23", technique="Seat jaw",
                                hand_action="Slide the fixed jaw flush to the shoulder.", tools_visible=["clamp"],
                                notes="The jaw is square to the bench."),
                TutorialSegment(timestamp_start="0:25", timestamp_end="0:37", technique="Store jig",
                                hand_action="Hang the spare jig on the peg."),
            ]),
            TutorialExtract(video_id="bbbbbbbbbbb", url=second_url, title="Tighten clamp", segments=[
                TutorialSegment(timestamp_start="1:05:02", timestamp_end="1:05:10", technique="Turn handle",
                                hand_action="Turn the handle clockwise until the pad meets the stock."),
            ]),
        ]
        plan = make_plan()
        draft = _WrittenPlan.model_validate(plan.model_dump())
        draft.project_title = "Clamp a joint"
        draft.source_youtube_urls = [first_url, "https://invented.invalid"]
        draft.steps[0].title = "Seat the clamp"
        draft.steps[0].instruction_text = "Slide the fixed jaw flush to the shoulder."
        draft.steps[1].title = "Tighten the handle"
        draft.steps[1].instruction_text = "Turn the handle until the pad contacts the stock."
        draft.steps[0].source_segments = ["S1.1", "S1.1", "S1.72"]
        draft.steps[1].source_segments = ["https://invented.invalid", "S2.1"]
        draft.steps.append(draft.steps[0].model_copy(update={"step_number": 3, "source_segments": ["S9.4"]}))
        service = AsyncMock()
        service.generate_structured.side_effect = [draft, grounding(plan, [3, 2, 1])]
        result = await synthesize_build_plan("Clamp a joint", sources, [], service)

        self.assertEqual(service.generate_structured.await_count, 2)
        draft_prompt, physical_prompt = [call.kwargs["contents"] for call in service.generate_structured.await_args_list]
        self.assertIn('"segment_id":"S1.2"', draft_prompt)
        chapters_line = next(line for line in physical_prompt.splitlines() if line.startswith("Chapters with"))
        chapters = json.loads(chapters_line.split(": ", 1)[1])
        self.assertEqual([[item["hand_action"] for item in chapter["observations"]] for chapter in chapters],
                         [[sources[0].segments[0].hand_action], [sources[1].segments[0].hand_action], []])
        self.assertNotIn("spare jig", physical_prompt)
        self.assertNotIn("invented.invalid", physical_prompt)
        reloaded = BuildPlan.model_validate_json(result.model_dump_json())
        self.assertEqual(reloaded.source_youtube_urls, [first_url, second_url])
        one, two, three = reloaded.steps
        self.assertEqual([s.segment_id for s in one.source_segments], ["S1.1"])
        self.assertEqual([s.segment_id for s in two.source_segments], ["S2.1"])
        self.assertEqual(three.source_segments, [])
        fixed, turning = one.source_segments[0], two.source_segments[0]
        self.assertEqual((fixed.source_title, fixed.source_url, fixed.start_seconds, fixed.end_seconds),
                         ("Clamp setup", first_url, 17, 23))
        self.assertEqual((fixed.notes, fixed.tools_visible), ("The jaw is square to the bench.", ["clamp"]))
        self.assertEqual((turning.source_url, turning.start_seconds, turning.end_seconds),
                         (second_url, 3902, 3910))

    def test_legacy_chapters_load_without_attributing_footage(self):
        old = make_plan().model_dump(exclude={"steps": {"__all__": {"source_segments"}}})
        self.assertTrue(all(step.source_segments == [] for step in BuildPlan.model_validate(old).steps))

    async def test_source_inventory_and_transitions_follow_observations(self):
        plan = make_plan()
        service = AsyncMock()
        service.generate_structured.side_effect = [plan, grounding(plan, [2, 1])]
        result = await synthesize_build_plan("Fold", [tutorial("https://youtu.be/source-a"), tutorial("https://youtu.be/source-b")],
                                             ["  WATER ", "clay", "  "], service)
        self.assertEqual([m.status for m in result.materials],
                         [MaterialStatus.OWNED_EXACT, MaterialStatus.BUY, MaterialStatus.BUY])
        self.assertEqual(result.source_youtube_urls, ["https://youtu.be/source-a"])
        self.assertEqual(result.continuity_bible.lighting, "daylight")
        self.assertEqual([s.pov_video_prompt for s in result.steps], ["Motion 1", "Motion 2"])
        self.assertEqual(result.steps[0].physical_state.end_state, result.steps[1].physical_state.start_state)
        self.assertEqual(result.steps[0].narration_script, "Fold the paper until the corners meet.")

    async def test_hallucinated_sources_fall_back_to_actual_sources(self):
        plan = make_plan()
        plan.source_youtube_urls = ["https://invented-source.invalid/"]
        service = AsyncMock()
        service.generate_structured.side_effect = [plan, grounding(plan, [1, 2])]
        result = await synthesize_build_plan("Fold", [tutorial("https://youtu.be/source-b")], [], service)
        self.assertEqual(result.source_youtube_urls, ["https://youtu.be/source-b"])
        self.assertTrue(all(m.status == MaterialStatus.BUY for m in result.materials))

    async def test_compact_guide_preserves_action_measurements_wait_and_safety(self):
        plan = make_plan()
        written = "Clamp the joint with a 2 mm gap. Wear gloves and let the glue cure for 24 hours."
        plan.steps[0].instruction_text = written
        service = AsyncMock()
        service.generate_structured.side_effect = [plan, grounding(plan, [1, 2])]
        result = await synthesize_build_plan("Make a joint", [tutorial("https://youtu.be/source-a")], [], service)
        draft, revision = [call.kwargs["contents"] for call in service.generate_structured.await_args_list]
        writer_schema, physical_schema = [str(call.kwargs["response_schema"].model_json_schema())
                                         for call in service.generate_structured.await_args_list]
        self.assertNotIn("hand_kinematics", writer_schema)
        self.assertNotIn("continuity_bible", writer_schema)
        self.assertNotIn("narration_script", physical_schema)
        self.assertNotIn("pov_video_prompt", physical_schema)
        self.assertIn("one or two short sentences", draft)
        self.assertIn("change the contact point, tool", draft)
        self.assertNotIn("3–6", draft)
        self.assertIn("measurements, real waiting times and necessary safety precautions", draft)
        self.assertIn("one short, observable verification cue", revision)
        self.assertIn(written, revision)
        self.assertNotIn("search_query", revision)
        self.assertNotIn("narration_script", revision)
        self.assertEqual(result.steps[0].instruction_text, written)
        self.assertEqual(result.steps[1].physical_state.verification_cues, ["Edges match"])

    async def test_grounding_reviews_independence_and_retains_only_valid_backward_edges(self):
        plan = make_plan()
        # The draft marks a continuing fold independent. Grounding reattaches it,
        # frees a second sheet, and later returns to the first sheet.
        plan.steps[1].visual_parent = 0
        plan.steps.extend([plan.steps[0].model_copy(update={"step_number": n}) for n in (3, 4)])
        updates = grounding(plan, [1, 2, 3, 4])
        updates.steps[2].visual_parent = 0
        updates.steps[3].visual_parent = 2
        service = AsyncMock()
        service.generate_structured.side_effect = [plan, updates]
        result = await synthesize_build_plan("Fold a two-piece model", [], [], service)
        self.assertEqual([step.visual_parent for step in result.steps], [-1, -1, 0, 2])
        self.assertIn("Preserve the workpiece and every completed change", service.generate_structured.await_args.kwargs["contents"])

        for unsafe_parent in (3, 4, 999):
            with self.subTest(unsafe_parent=unsafe_parent):
                updates.steps[2].visual_parent = unsafe_parent
                service.generate_structured.side_effect = [plan, updates]
                with self.assertRaises(ValidationError):
                    await synthesize_build_plan("Fold a two-piece model", [], [], service)

    async def test_reject_incomplete_duplicate_and_unknown_grounded_steps(self):
        for numbers in ([1], [2, 2], [1, 3], [1, 2, 3]):
            with self.subTest(numbers=numbers):
                plan = make_plan()
                original_bible = plan.continuity_bible
                service = AsyncMock()
                service.generate_structured.side_effect = [plan, grounding(plan, numbers)]
                with self.assertRaisesRegex(ValueError, "one physical transition"):
                    await synthesize_build_plan("Fold", [tutorial("https://youtu.be/source-a")], [], service)
                self.assertIs(plan.continuity_bible, original_bible)

    def test_ambiguous_steps_and_unusable_narration_are_rejected_on_load(self):
        plan = make_plan().model_dump()
        step = plan["steps"][0]
        for steps in ([], [step, step], [dict(step, duration_seconds=0)], [dict(step, narration_script=" \t ")]):
            with self.subTest(steps=steps):
                with self.assertRaises(ValidationError):
                    BuildPlan.model_validate(dict(plan, steps=steps))
        material = plan["materials"][0]
        for materials in ([material, material], [dict(material, id=" ")]):
            with self.subTest(materials=materials):
                with self.assertRaises(ValidationError):
                    BuildPlan.model_validate(dict(plan, materials=materials))
