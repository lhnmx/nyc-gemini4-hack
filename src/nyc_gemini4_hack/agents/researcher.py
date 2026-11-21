from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx
from google.genai import types

from nyc_gemini4_hack.config import Settings, get_settings
from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.models import DecisionResult, TutorialExtract

logger = logging.getLogger(__name__)


@dataclass
class YouTubeCandidate:
    video_id: str
    url: str
    title: str
    channel: str
    description: str


async def search_youtube_tutorials(
    goal: str,
    max_results: int = 5,
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
        "key": cfg.youtube_api_key,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            "https://www.googleapis.com/youtube/v3/search",
            params=params,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])

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
                title=snippet.get("title", ""),
                channel=snippet.get("channelTitle", ""),
                description=snippet.get("description", ""),
            )
        )
    return candidates


async def gate_youtube_candidates(
    goal: str,
    candidates: list[YouTubeCandidate],
    gemini: Gemini4Client,
    max_keep: int = 2,
) -> tuple[list[YouTubeCandidate], list[DecisionResult]]:
    """
    Use the native Gemini Decisions API to filter out vlogs, hauls, unboxings, or
    clickbait and retain only actionable hands-on tutorials.
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

    kept: list[YouTubeCandidate] = []
    decisions: list[DecisionResult] = []
    for idx, cand in enumerate(candidates, start=1):
        res = decision_map[f"video_{idx}_relevance"]
        decisions.append(res)
        if res.choice == "KEEP_TUTORIAL":
            kept.append(cand)

    # Guarantee at least 1 candidate if all were borderline
    if not kept and candidates:
        kept = candidates[:1]

    return kept[:max_keep], decisions


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
        "observe exact hand grips, finger pressure, material proportions, tool swaps, and items visible "
        "on the workbench even if the creator never mentions them verbally (or if the video is silent/non-English). "
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
        "2. extracted_materials: EVERY physical material and tool visible or used (including clay colors, wire, water, sculpting tools, mats, paints).\n"
        "3. segments: chronological timestamped segments (MM:SS) of actual hands-on steps (skip intros/outros/sponsors), detailing technique, exact hand_action, and tools_visible.\n"
        "4. common_mistakes: physical failure modes or tricky spots visible in the build."
    )

    extract = await gemini.generate_structured(
        role="carbon",
        contents=[video_part, prompt_text],
        response_schema=TutorialExtract,
        system_instruction=system_instruction,
        temperature=0.2,
    )
    # Ensure metadata fields are always populated accurately
    extract.video_id = candidate.video_id
    extract.url = candidate.url
    if not extract.title:
        extract.title = candidate.title
    if not extract.channel:
        extract.channel = candidate.channel
    return extract


async def research_goal_on_youtube(
    goal: str,
    gemini: Gemini4Client,
    max_videos: int = 2,
    direct_urls: list[str] | None = None,
) -> tuple[list[TutorialExtract], list[DecisionResult]]:
    """
    Full Research Agent pass:
    1. Search YouTube (or use direct_urls if provided for stress-testing).
    2. Gate candidates with native Decisions API.
    3. Watch selected videos in parallel with Gemini 4 Carbon.
    """
    decisions: list[DecisionResult] = []
    if direct_urls:
        selected = [
            YouTubeCandidate(
                video_id=u.split("v=")[-1].split("&")[0][:15],
                url=u,
                title=f"Direct tutorial ({u})",
                channel="Direct URL",
                description="",
            )
            for u in direct_urls[:max_videos]
        ]
    else:
        candidates = await search_youtube_tutorials(goal, max_results=5, settings=gemini.settings)
        selected, decisions = await gate_youtube_candidates(
            goal=goal,
            candidates=candidates,
            gemini=gemini,
            max_keep=max_videos,
        )

    tasks = [analyze_youtube_video(goal, cand, gemini) for cand in selected]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    extracts: list[TutorialExtract] = []
    for cand, res in zip(selected, results):
        if isinstance(res, Exception):
            logger.warning("Failed to analyze %s: %s", cand.url, res)
            continue
        extracts.append(res)

    if not extracts:
        raise RuntimeError(
            "No YouTube videos could be analyzed. Check GEMINI_API_KEY / YOUTUBE_API_KEY and video availability."
        )
    return extracts, decisions
