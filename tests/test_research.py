import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from google import genai

from nyc_gemini4_hack.agents import researcher
from nyc_gemini4_hack.config import Settings
from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.models import DecisionResult, TutorialExtract, TutorialSegment


def extract():
    return TutorialExtract(
        video_id="suggested", title="Suggested title", channel="Suggested channel", url="suggested-url",
        segments=[TutorialSegment(timestamp_start="00:02", timestamp_end="00:05",
                                  technique="Fold", hand_action="Flatten the crease")],
    )


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_search_duration_lookup_uses_same_credentials_without_polluting_links(self):
        goal = ("Fold a classic dart paper plane from a single rectangular sheet. "
                "Demonstrate the simple pointed-nose design and open its two swept-back wings.")
        def respond(request):
            self.assertEqual(request.headers["X-Goog-Api-Key"], "offline")
            self.assertNotIn("offline", str(request.url))
            if request.url.path.endswith("/search"):
                self.assertEqual(request.url.params["q"], "Fold a classic dart paper plane step by step tutorial")
                return httpx.Response(200, json={"items": [
                    {"id": {"videoId": "abcdefghijk"}, "snippet": {"title": "Fold &amp; Fly"}}
                ]})
            self.assertTrue(request.url.path.endswith("/videos"))
            self.assertEqual(request.url.params["id"], "abcdefghijk")
            return httpx.Response(200, json={"items": [
                {"id": "abcdefghijk", "contentDetails": {"duration": "PT2M7S"}}
            ]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        settings = Settings(_env_file=None, youtube_api_key="offline")
        with patch.object(researcher.httpx, "AsyncClient", return_value=client):
            videos = await researcher.search_youtube_tutorials(goal, settings=settings)
        self.assertEqual((videos[0].title, videos[0].duration_seconds), ("Fold & Fly", 127))

    def test_query_retains_the_material_and_measurement_of_a_specific_build(self):
        goal = "Make a bracelet from 0.5 mm cord; then show it in close-up."
        self.assertEqual(researcher._tutorial_query(goal, broader=True), "Make a bracelet from 0.5 mm cord")

    async def test_one_qualified_source_by_default_with_reserve_only_after_failure(self):
        candidates = [researcher.YouTubeCandidate(key, f"https://youtu.be/{key}", title, "Maker", "", duration)
                      for key, title, duration in [("aaaaaaaaaaa", "Full lesson", 760),
                                                   ("bbbbbbbbbbb", "Quick tutorial", 130),
                                                   ("ccccccccccc", "Product reel", 60)]]
        service = Mock()
        service.decide_batch = AsyncMock(return_value={
            f"video_{i}_relevance": DecisionResult(gate_name=f"video_{i}_relevance", choice=choice, confidence=score)
            for i, choice, score in [(1, "KEEP_TUTORIAL", .98), (2, "KEEP_TUTORIAL", .96), (3, "SKIP_FILLER", .99)]
        })
        with patch.object(researcher, "search_youtube_tutorials", AsyncMock(return_value=candidates)) as search:
            for outputs, expected in [([extract()], ["Quick tutorial"]),
                                      ([RuntimeError("unavailable"), extract()], ["Full lesson"])]:
                with self.subTest(expected=expected):
                    search.reset_mock()
                    service.generate_structured = AsyncMock(side_effect=outputs)
                    sources, decisions = await researcher.research_goal_on_youtube("Fold", service)
                    self.assertEqual([source.title for source in sources], expected)
                    self.assertEqual([decision.source_title for decision in decisions], [c.title for c in candidates])
                    self.assertEqual([decision.source_url for decision in decisions], [c.url for c in candidates])
                    self.assertEqual(service.generate_structured.await_count, len(outputs))
                    urls = [c.kwargs["contents"][0].file_data.file_uri for c in service.generate_structured.await_args_list]
                    self.assertEqual(urls, [candidates[1].url, candidates[0].url][:len(outputs)])
                    self.assertEqual(search.await_args.kwargs["max_results"], 3)
                    search.assert_awaited_once()

    async def test_broader_search_after_rejection_or_unusable_footage_gates_only_new_sources(self):
        goal = "Weave a round coaster. Show each pass of the thread."
        first, second = [researcher.YouTubeCandidate(key, f"https://youtu.be/{key}", title, "Maker", "")
                         for key, title in [("aaaaaaaaaaa", "First result"), ("bbbbbbbbbbb", "Second result")]]
        for first_rejected in (True, False):
            with self.subTest(first_rejected=first_rejected):
                service = Mock()
                service.model_id.return_value = "analyst"
                service.generate_structured = AsyncMock(side_effect=(
                    [extract()] if first_rejected else [ValueError("No usable footage"), extract()]
                ))
                choices = ["SKIP_FILLER" if first_rejected else "KEEP_TUTORIAL", "KEEP_TUTORIAL"]
                service.decide_batch = AsyncMock(side_effect=[
                    {f"video_{i}_relevance": DecisionResult(gate_name=f"video_{i}_relevance", choice=choice)}
                    for i, choice in enumerate(choices, start=1)
                ])
                search = AsyncMock(side_effect=[[first], [first, second, second]])
                report = AsyncMock()
                decisions_report = AsyncMock()
                with patch.object(researcher, "search_youtube_tutorials", search):
                    sources, decisions = await researcher.research_goal_on_youtube(
                        goal, service, on_activity=report, on_decisions=decisions_report,
                    )
                self.assertEqual([(c.kwargs["broader"], c.kwargs["max_results"]) for c in search.await_args_list],
                                 [(False, 3), (True, 6)])
                self.assertEqual([s.url for s in sources], [second.url])
                self.assertEqual([(d.gate_name, d.choice, d.source_url) for d in decisions],
                                 [("video_1_relevance", choices[0], first.url),
                                  ("video_2_relevance", choices[1], second.url)])
                self.assertEqual([c.args[0] for c in decisions_report.await_args_list],
                                 [[decisions[0]], [decisions[1]]])
                for i, call in enumerate(service.decide_batch.await_args_list, start=1):
                    self.assertIn(goal, call.kwargs["context"])
                    self.assertEqual(list(call.kwargs["questions"]), [f"video_{i}_relevance"])
                events = [c.args[0] for c in report.await_args_list]
                analysis = [e for e in events if e.id == "analyze-bbbbbbbbbbb"]
                self.assertEqual(analysis[0].source_segments, [])
                self.assertEqual(len(analysis[-1].source_segments), 1)
                clip = analysis[-1].source_segments[0]
                self.assertEqual((clip.source_url, clip.source_title, clip.start_seconds, clip.end_seconds,
                                  clip.hand_action), (second.url, second.title, 2, 5, "Flatten the crease"))
                for failed in (e for e in events if e.state == "failed"):
                    self.assertEqual(failed.source_segments, [])
                for key in ["source-search", "source-selection", "source-search-2", "source-selection-2"]:
                    self.assertEqual([e.state for e in events if e.id == key], ["started", "completed"])
                for suffix, decision in zip(["", "-2"], decisions, strict=True):
                    start, done = [e for e in events if e.id == "source-search" + suffix]
                    screen = next(e for e in events if e.id == "source-selection" + suffix and e.state == "started")
                    self.assertEqual(start.sources, [])
                    self.assertEqual(done.sources, screen.sources)
                    self.assertEqual([s.model_dump() for s in screen.sources],
                                     [{"gate_name": decision.gate_name, "title": decision.source_title,
                                       "url": decision.source_url, "channel": "Maker", "duration_seconds": None}])
                self.assertEqual([e.detail for e in events if e.id.startswith("source-search") and e.state == "started"],
                                 ["Weave a round coaster step by step tutorial", "Weave a round coaster"])
                analyzed = [call.kwargs["contents"][0].file_data.file_uri for call in service.generate_structured.await_args_list]
                self.assertEqual(analyzed, [second.url] if first_rejected else [first.url, second.url])

    async def test_discovery_key_stays_out_of_urls_and_http_errors(self):
        cfg = Settings(_env_file=None, youtube_api_key="test-only-credential")
        seen = []

        def forbidden(request):
            seen.append(request)
            return httpx.Response(403, json={"error": "quota"})

        session = httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
        with patch.object(researcher.httpx, "AsyncClient", return_value=session):
            with self.assertRaises(httpx.HTTPStatusError) as error:
                await researcher.search_youtube_tutorials("folder", settings=cfg)

        self.assertEqual(seen[0].headers["X-Goog-Api-Key"], cfg.youtube_api_key)
        self.assertNotIn(cfg.youtube_api_key, str(seen[0].url))
        self.assertNotIn(cfg.youtube_api_key, str(error.exception))

    async def test_all_skipped_never_analyzes_a_rejected_candidate(self):
        candidate = researcher.YouTubeCandidate("1234567890A", "source", "Paper haul", "Maker", "")
        gemini = Mock()
        gemini.decide_batch = AsyncMock(return_value={
            "video_1_relevance": DecisionResult(gate_name="video_1_relevance", choice="SKIP_FILLER"),
        })
        gemini.generate_structured = AsyncMock()
        with patch.object(researcher, "search_youtube_tutorials", AsyncMock(return_value=[candidate])) as search:
            with self.assertRaisesRegex(RuntimeError, "No hands-on tutorials"):
                await researcher.research_goal_on_youtube("Fold paper", gemini)
        gemini.generate_structured.assert_not_awaited()
        self.assertEqual(search.await_count, 2)
        gemini.decide_batch.assert_awaited_once()

    async def test_direct_links_are_deduplicated_before_limit_and_one_failure_isolated(self):
        a, b = "aaBBccDD11_", "eeFFggHH22-"
        gemini = Mock()
        gemini.generate_structured = AsyncMock(side_effect=[ValueError("Unavailable video"), extract()])
        extracts, decisions = await researcher.research_goal_on_youtube(
            "Fold paper", gemini, max_videos=2,
            direct_urls=[f"https://youtu.be/{a}?feature=shared", f"https://youtube.com/watch?v={a}",
                         f"https://m.youtube.com/shorts/{b}?t=120"],
        )
        self.assertEqual(decisions, [])
        self.assertEqual([e.video_id for e in extracts], [b])
        self.assertEqual(extracts[0].url, f"https://www.youtube.com/watch?v={b}")
        calls = gemini.generate_structured.await_args_list
        self.assertEqual([call.kwargs["contents"][0].file_data.file_uri for call in calls],
                         [f"https://www.youtube.com/watch?v={key}" for key in [a, b]])

    async def test_invalid_direct_link_fails_before_provider_call(self):
        gemini = Mock()
        gemini.generate_structured = AsyncMock()
        for url in ["https://example.org/watch?v=12345678901", "https://youtube.com/watch?list=123"]:
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "YouTube video link"):
                await researcher.research_goal_on_youtube("Fold", gemini, direct_urls=[url])
        gemini.generate_structured.assert_not_awaited()

    async def test_analysis_uses_source_metadata_and_requires_observations(self):
        original = researcher.YouTubeCandidate("1234567890A", "watch-url", "Paper craft", "Maker", "")
        gemini = Mock()
        gemini.generate_structured = AsyncMock(return_value=extract())
        result = await researcher.analyze_youtube_video("Fold", original, gemini)
        self.assertEqual((result.video_id, result.url, result.title, result.channel),
                         ("1234567890A", "watch-url", "Paper craft", "Maker"))
        gemini.generate_structured.return_value = result.model_copy(update={"segments": []})
        with self.assertRaisesRegex(ValueError, "no hands-on segments"):
            await researcher.analyze_youtube_video("Fold", original, gemini)

    async def test_analysis_discards_unusable_and_out_of_video_intervals(self):
        candidate = researcher.YouTubeCandidate("abcdefghijk", "https://youtu.be/abcdefghijk",
                                                "Clamp lesson", "Maker", "", duration_seconds=35)
        result = extract()
        observed = TutorialSegment(timestamp_start="00:12", timestamp_end="00:23",
                                   technique="Tighten", hand_action="Turn the screw until the pads touch.")
        result.segments = [
            observed.model_copy(update={"timestamp_start": "near the start"}),
            observed.model_copy(update={"timestamp_end": "00:12"}),
            observed.model_copy(update={"timestamp_end": "00:11"}),
            observed.model_copy(update={"timestamp_end": "00:70"}),
            observed.model_copy(update={"timestamp_end": "00:36"}),
            observed.model_copy(update={"hand_action": " "}), observed,
        ]
        gemini = Mock()
        gemini.generate_structured = AsyncMock(return_value=result)
        filtered = await researcher.analyze_youtube_video("Tighten a clamp", candidate, gemini)
        self.assertEqual(filtered.segments, [observed])
        gemini.generate_structured.assert_awaited_once()

    async def test_cancelled_extraction_never_becomes_a_plan_source(self):
        gemini = Mock()
        gemini.generate_structured = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await researcher.research_goal_on_youtube(
                "Fold", gemini, direct_urls=["https://www.youtube.com/watch?v=1234567890A"],
            )


class DecisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.gemini = Gemini4Client(Settings(_env_file=None, gemini_decisions_enabled=True))
        self.sdk = Mock()
        self.gemini._client = self.sdk
        self.sdk.aio.decisions.create = AsyncMock()
        self.sdk.aio.models.generate_content = AsyncMock()
        self.criteria = {"KEEP_TUTORIAL": "Hands-on demo", "SKIP_FILLER": "No demonstrated action"}
        self.questions = {"a": ("Evaluate first", self.criteria), "b": ("Evaluate second", self.criteria)}

    async def test_partial_decision_reuses_valid_answer_and_falls_back_only_for_missing_one(self):
        requests = []

        def respond(request):
            requests.append(request)
            if request.url.path.endswith("/decisions"):
                return httpx.Response(200, json={"answers": {"a": {
                    "type": "choice", "choice": "KEEP_TUTORIAL", "confidence": .94,
                    "probabilities": {"KEEP_TUTORIAL": .94, "SKIP_FILLER": .06},
                }}})
            text = json.dumps({"choice": "SKIP_FILLER", "confidence": .86,
                               "rationale": "Only packaging shown."})
            return httpx.Response(200, json={"candidates": [
                {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}
            ]})

        http = await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        self.gemini._client = genai.Client(api_key="test-only", http_options={"httpx_async_client": http})
        self.addAsyncCleanup(self.gemini.aclose)
        results = await self.gemini.decide_batch("Two videos", self.questions)
        self.assertEqual((results["a"].choice, results["a"].confidence, results["a"].via_decisions_api),
                         ("KEEP_TUTORIAL", .94, True))
        self.assertEqual((results["b"].choice, results["b"].via_decisions_api), ("SKIP_FILLER", False))
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].url.path, "/v1beta/decisions")
        self.assertEqual(set(json.loads(requests[0].content)["questions"]), {"a", "b"})
        self.assertIn("Evaluate second", requests[1].content.decode())

    async def test_invalid_answers_cannot_turn_into_affirmative_decisions(self):
        self.sdk.aio.decisions.create.return_value = SimpleNamespace(
            answers={"a": SimpleNamespace(type="choice", choice="typo", confidence=1.0)}
        )
        self.sdk.aio.models.generate_content.return_value = SimpleNamespace(
            parsed={"choice": "typo", "confidence": .8, "rationale": "Uncertain."}
        )
        with self.assertRaisesRegex(ValueError, "unknown choice"):
            await self.gemini.decide("a", self.criteria, "Classify video", "Data")

    async def test_blocked_native_input_stops_without_retry(self):
        self.sdk.aio.decisions.create.return_value = SimpleNamespace(
            answers={}, prompt_feedback=SimpleNamespace(block_reason="blocklist")
        )
        with self.assertRaisesRegex(ValueError, "declined"):
            await self.gemini.decide_batch("Data", self.questions)
        self.sdk.aio.models.generate_content.assert_not_awaited()

    async def test_cleanup_closes_allocated_transports_once(self):
        self.sdk.aio.aclose = AsyncMock()
        await self.gemini.aclose()
        await self.gemini.aclose()
        self.sdk.aio.aclose.assert_awaited_once_with()
        self.sdk.close.assert_called_once_with()
        untouched = Gemini4Client(Settings(_env_file=None, gemini_api_key=""))
        await untouched.aclose()
        self.assertIsNone(untouched._client)
