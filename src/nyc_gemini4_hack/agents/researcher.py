from __future__ import annotations

import asyncio
import html
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlsplit

import httpx
from google.genai import types

from nyc_gemini4_hack.config import Settings, get_settings
from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.models import AgentActivity, DecisionResult, TutorialExtract

logger = logging.getLogger(__name__)

ActivityReporter = Callable[[AgentActivity], Awaitable[None]]
DecisionReporter = Callable[[list[DecisionResult]], Awaitable[None]]


@asynccontextmanager
async def agent_operation(
    report: ActivityReporter | None, *, id: str, stage: Literal["research", "planning"],
    label: str, agent: str, model: str | None = None, detail: str = "",
) -> AsyncIterator[AgentActivity]:
    """Publish one operation's real boundaries, including validation or provider failures."""
    entry = AgentActivity(id=id, stage=stage, state="started", label=label,
                          agent=agent, model=model, detail=detail)
    if report:
        await report(entry.model_copy())
    state: Literal["failed", "completed"] = "failed"
    try:
        yield entry
        state = "completed"
    finally:
        if report:
            # Create a new timestamp when the provider returns and attribution is known.
            await report(AgentActivity(**entry.model_dump(exclude={"state", "timestamp"}), state=state))


@dataclass
class YouTubeCandidate:
    video_id: str
    url: str
    title: str
    channel: str
    description: str
    duration_seconds: int | None = None


def _direct_candidate(url: str) -> YouTubeCandidate:
    parts = urlsplit(url.strip())
    path = parts.path.strip("/").split("/")
    video_id = ""
    if parts.scheme in {"http", "https"}:
        if parts.hostname in {"youtu.be", "www.youtu.be"} and len(path) == 1:
            video_id = path[0]
        elif parts.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
            if path == ["watch"]:
                video_id = parse_qs(parts.query).get("v", [""])[0]
            elif len(path) == 2 and path[0] in {"shorts", "embed", "live"}:
                video_id = path[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Provide a YouTube video link, such as a watch, Shorts, or youtu.be URL.")
    return YouTubeCandidate(video_id, f"https://www.youtube.com/watch?v={video_id}", "", "", "")


async def search_youtube_tutorials(
    goal: str,
    max_results: int = 3,
    settings: Settings | None = None,
) -> list[YouTubeCandidate]:
    """Search YouTube Data API v3 for hands-on tutorial videos matching the physical goal."""
    cfg = settings or get_settings()
    if not cfg.youtube_api_key.strip():
        raise RuntimeError("YOUTUBE_API_KEY is not set in .env.")

    query = f"{goal} step by step tutorial"
    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "videoEmbeddable": "true",
        "maxResults": max_results,
        "relevanceLanguage": "en",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            "https://www.googleapis.com/youtube/v3/search",
            params=params,
            headers={"X-Goog-Api-Key": cfg.youtube_api_key},
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        ids = [item["id"]["videoId"] for item in items if item.get("id", {}).get("videoId")]
        durations: dict[str, int] = {}
        if ids:
            # YouTube search omits running time. One metadata lookup lets equally relevant
            # demonstrations compete on how much footage the video analyst has to watch.
            try:
                info = await client.get("https://www.googleapis.com/youtube/v3/videos",
                                        params={"part": "contentDetails", "id": ",".join(ids)},
                                        headers={"X-Goog-Api-Key": cfg.youtube_api_key})
                info.raise_for_status()
                for row in info.json().get("items", []):
                    raw = row.get("contentDetails", {}).get("duration", "")
                    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", raw)
                    if match:
                        h, m, s = (int(part or 0) for part in match.groups())
                        durations[row["id"]] = h * 3600 + m * 60 + s
            except httpx.HTTPError:
                logger.info("Tutorial durations unavailable; using relevance order.")

    candidates: list[YouTubeCandidate] = []
    for item in items:
        vid = item.get("id", {}).get("videoId")
        if not vid:
            continue
        snippet = item.get("snippet", {})
        candidates.append(
            YouTubeCandidate(
                video_id=vid,
                url=f"https://www.youtube.com/watch?v={vid}",
                title=html.unescape(snippet.get("title", "")),
                channel=html.unescape(snippet.get("channelTitle", "")),
                description=html.unescape(snippet.get("description", "")),
                duration_seconds=durations.get(vid),
            )
        )
    return candidates


async def gate_youtube_candidates(
    goal: str,
    candidates: list[YouTubeCandidate],
    gemini: Gemini4Client,
    max_keep: int = 1,
) -> tuple[list[YouTubeCandidate], list[DecisionResult]]:
    """
    Select actionable tutorials using Decisions API answers or the structured model fallback.
    """
    if not candidates:
        return [], []

    context_lines = [f"User physical build goal: {goal}", "", "Candidate YouTube videos:"]
    questions: dict[str, tuple[str, dict[str, str]]] = {}

    for idx, cand in enumerate(candidates, start=1):
        context_lines.append(
            f"[{idx}] Title: {cand.title}\n"
            f"    Channel: {cand.channel}\n"
            f"    Description: {cand.description[:250]}"
        )
        questions[f"video_{idx}_relevance"] = (
            f"Should we watch candidate [{idx}] ('{cand.title}') to learn how to physically make '{goal}'?",
            {
                "KEEP_TUTORIAL": "Actionable hands-on craft/build tutorial showing real step-by-step making.",
                "SKIP_FILLER": "Unboxing, haul, timelapse-only showcase without clear steps, unrelated topic, or vlog.",
            },
        )

    decision_map = await gemini.decide_batch(
        context="\n".join(context_lines),
        questions=questions,
        role="neon",
    )

    kept: list[tuple[float, YouTubeCandidate]] = []
    decisions: list[DecisionResult] = []
    for idx, cand in enumerate(candidates, start=1):
        res = decision_map[f"video_{idx}_relevance"]
        res.source_title = cand.title
        decisions.append(res)
        if res.choice == "KEEP_TUTORIAL":
            kept.append((res.confidence, cand))

    best = max((score for score, _ in kept), default=0)
    # Prefer a concise demonstration among similarly strong matches. A very short
    # showcase or unknown duration does not displace a complete, clearly relevant tutorial.
    def rank(item: tuple[float, YouTubeCandidate]) -> tuple[bool, float, float]:
        score, video = item
        duration = video.duration_seconds
        strong = score >= best - 0.05
        return not strong, duration if strong and duration and duration >= 45 else float("inf"), -score

    return [video for _, video in sorted(kept, key=rank)[:max_keep]], decisions


async def analyze_youtube_video(
    goal: str,
    candidate: YouTubeCandidate,
    gemini: Gemini4Client,
) -> TutorialExtract:
    """
    Watch and listen to a raw YouTube video directly with Gemini 4 Carbon.
    Extracts actual hand techniques, timing, and visible tools (even if silent or non-English).
    """
    system_instruction = (
        "You are an expert physical craft & fabrication analyst (Gemini 4 Carbon). "
        "Watch the raw video and listen to the audio carefully. Do NOT rely only on spoken words — "
        "describe observable grips, contact, material proportions, tool swaps, and items visible "
        "on the workbench even if the creator never mentions them verbally (or if the video is silent/non-English). "
        "Use one brief phrase per field. Combine repetitions of the same motion; identify its target and count. "
        "Preserve demonstrated measurements, waiting times and safety precautions. "
        "Distinguish visible cues from estimates. Omit unsupported or repeated details. "
        "Return a structured TutorialExtract JSON."
    )

    video_part = types.Part(
        file_data=types.FileData(file_uri=candidate.url, mime_type="video/mp4")
    )
    prompt_text = (
        f"Goal: {goal}\n"
        f"Video ID: {candidate.video_id}\n"
        f"URL: {candidate.url}\n"
        f"Title: {candidate.title}\n"
        f"Channel: {candidate.channel}\n\n"
        "Analyze this video from start to finish and extract:\n"
        "1. language_or_audio_notes: note if it is silent/ASMR, music-only, non-English, or narrated.\n"
        "2. extracted_materials: materials and tools actually used in the demonstrated build.\n"
        "3. segments: a compact chronological sequence of essential physical changes, each with MM:SS timestamps, "
        "one action sentence identifying contact and the visible result, and tools_visible. Skip filler and optional decoration.\n"
        "4. common_mistakes: physical failure modes or tricky spots visible in the build."
    )

    extract = await gemini.generate_structured(
        role="carbon",
        contents=[video_part, prompt_text],
        response_schema=TutorialExtract,
        system_instruction=system_instruction,
        temperature=0.2,
    )
    if not extract.segments:
        raise ValueError("The video yielded no hands-on segments.")
    # Search metadata and the supplied video identity take precedence over model guesses.
    extract.video_id = candidate.video_id
    extract.url = candidate.url
    if candidate.title or not extract.title.strip():
        extract.title = candidate.title or f"YouTube video {candidate.video_id}"
    if candidate.channel:
        extract.channel = candidate.channel
    return extract


async def research_goal_on_youtube(
    goal: str,
    gemini: Gemini4Client,
    max_videos: int = 1,
    direct_urls: list[str] | None = None,
    *,
    on_activity: ActivityReporter | None = None,
    on_decisions: DecisionReporter | None = None,
) -> tuple[list[TutorialExtract], list[DecisionResult]]:
    """
    Full Research Agent pass:
    1. Search YouTube (or use direct_urls if provided for stress-testing).
    2. When searching, select candidates using the decision gate and retain its provider metadata.
    3. Watch selected videos in parallel with Gemini 4 Carbon.
    """
    decisions: list[DecisionResult] = []
    if max_videos < 1:
        raise ValueError("The number of videos to analyze must be positive.")
    if direct_urls:
        by_id: dict[str, YouTubeCandidate] = {}
        for url in direct_urls:
            candidate = _direct_candidate(url)
            by_id[candidate.video_id] = candidate
            if len(by_id) == max_videos + 1:
                break
        selected = list(by_id.values())
    else:
        async with agent_operation(on_activity, id="source-search", stage="research", label="Find tutorials",
                                   agent="YouTube Data API v3", detail=goal) as search:
            candidates = await search_youtube_tutorials(goal, max_results=min(50, max(3, max_videos + 1)), settings=gemini.settings)
            search.detail = " · ".join(c.title for c in candidates) or "No tutorials found."
        selected = []
        if candidates:
            async with agent_operation(on_activity, id="source-selection", stage="research", label="Select tutorials",
                                       agent="Research agent", detail=" · ".join(c.title for c in candidates)) as selection:
                selected, decisions = await gate_youtube_candidates(goal, candidates, gemini, max_keep=max_videos + 1)
                native = sum(d.via_decisions_api for d in decisions)
                fallback = len(decisions) - native
                routes = []
                if native:
                    routes.append(f"Decisions API ({native})")
                if fallback:
                    routes.append(f"Structured fallback ({fallback})")
                selection.agent = " · ".join(routes) or "Research agent"
                selection.model = ", ".join(dict.fromkeys(d.model_used for d in decisions if d.model_used)) or None
                selection.detail = "Selected " + " · ".join(c.title for c in selected[:max_videos]) if selected else "No hands-on match."
            if on_decisions:
                await on_decisions(decisions)
    if not selected:
        raise RuntimeError("No hands-on tutorials matched this goal.")

    async def analyze(candidate: YouTubeCandidate) -> TutorialExtract:
        async with agent_operation(on_activity, id=f"analyze-{candidate.video_id}", stage="research",
                                   label="Analyze tutorial footage", agent="Research agent",
                                   model=gemini.model_id("carbon") if on_activity else None,
                                   detail=" · ".join(filter(None, [candidate.title or candidate.url, candidate.channel]))) as analysis:
            result = await analyze_youtube_video(goal, candidate, gemini)
            count = len(result.segments)
            analysis.detail = f"{result.title} · {count} hands-on segment{'s' if count != 1 else ''}. " + " · ".join(
                f"{s.timestamp_start} {s.technique}" for s in result.segments[:3])
            return result

    extracts: list[TutorialExtract] = []
    while selected and len(extracts) < max_videos:
        needed = max_videos - len(extracts)
        batch, selected = selected[:needed], selected[needed:]
        results = await asyncio.gather(*(analyze(c) for c in batch), return_exceptions=True)
        for cand, res in zip(batch, results, strict=True):
            if isinstance(res, asyncio.CancelledError):
                raise res
            if isinstance(res, Exception):
                logger.warning("Failed to analyze video %s (%s)", cand.video_id, type(res).__name__)
                continue
            extracts.append(res)

    if not extracts:
        raise RuntimeError(
            "No YouTube videos could be analyzed. Check GEMINI_API_KEY / YOUTUBE_API_KEY and video availability."
        )
    return extracts, decisions
