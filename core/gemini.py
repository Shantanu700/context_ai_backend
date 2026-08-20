"""Gemini calls: one structured analysis per scene, one rationale for its top ad."""

import threading

from django.conf import settings
from pydantic import BaseModel

# Constraining the model to a fixed vocabulary is what makes the IAB boost work at all —
# free-form category strings would never overlap with the ad catalog's.
IAB_CATEGORIES = [
    "Automotive",
    "Business",
    "Careers",
    "Education",
    "Family & Parenting",
    "Food & Drink",
    "Health & Fitness",
    "Hobbies & Interests",
    "Home & Garden",
    "Personal Finance",
    "Pets",
    "Real Estate",
    "Science",
    "Shopping",
    "Society",
    "Sports",
    "Style & Fashion",
    "Technology & Computing",
    "Travel",
]

_client = None
_client_lock = threading.Lock()


class SceneAnalysis(BaseModel):
    description: str
    objects: list[str]
    tone: str
    iab_categories: list[str]


class AdFit(BaseModel):
    rationale: str
    brand_safety_flag: bool


def _client_once():
    """One client per process, built under a lock.

    The worker runs a thread pool, and racing constructions leave orphaned clients whose
    cleanup closes the httpx transport the survivor is still using ("Cannot send a
    request, as the client has been closed").
    """
    global _client
    from google import genai
    from google.genai import types

    with _client_lock:
        if _client is None:
            if not settings.GEMINI_API_KEY:
                raise RuntimeError("GEMINI_API_KEY is not set")
            _client = genai.Client(
                api_key=settings.GEMINI_API_KEY,
                # flash gets 503-overloaded regularly; the SDK's own backoff beats
                # hand-rolling retries around every call
                http_options=types.HttpOptions(
                    retry_options=types.HttpRetryOptions(
                        attempts=settings.GEMINI_RETRIES,
                        http_status_codes=[429, 500, 502, 503, 504],
                    )
                ),
            )
    return _client


def _generate(prompt: str, schema: type[BaseModel], images: list[bytes] = ()) -> BaseModel:
    from google.genai import types

    client = _client_once()
    parts = [types.Part.from_bytes(data=b, mime_type="image/jpeg") for b in images]
    response = client.models.generate_content(
        model=settings.GEMINI_MODEL,
        contents=[*parts, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.2,
            # we never pass tools; without this the SDK logs an AFC advisory per call
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
    return response.parsed


def analyze_scene(frames: list[bytes], transcript: str) -> SceneAnalysis:
    prompt = (
        "You are tagging a video scene so advertisers can decide what to place next to it.\n"
        f"{len(frames)} keyframe(s) from the scene are attached.\n"
        f"Spoken words in the scene: {transcript or '(silence)'}\n\n"
        "Return JSON with:\n"
        "- description: one or two sentences on what is happening\n"
        "- objects: the notable objects, places and people visible\n"
        "- tone: a single word for the emotional tone (e.g. tense, joyful, somber, neutral)\n"
        "- iab_categories: one to three, chosen only from this list: "
        f"{', '.join(IAB_CATEGORIES)}"
    )
    return _generate(prompt, SceneAnalysis, frames)


def write_rationale(scene, ad) -> AdFit:
    prompt = (
        "An ad has been matched to a video scene. Judge the pairing.\n\n"
        f"SCENE: {scene.description}\n"
        f"Scene tone: {scene.tone}. Categories: {', '.join(scene.iab_categories)}.\n"
        f"Spoken words: {scene.transcript_text or '(silence)'}\n\n"
        f"AD: {ad.brand} — {ad.title}\n"
        f"{ad.description}\n"
        f"Intended tone: {ad.target_tone}. Categories: {', '.join(ad.iab_categories)}.\n\n"
        "Return JSON with:\n"
        "- rationale: ONE short sentence on why this ad suits this moment\n"
        "- brand_safety_flag: true if the scene's tone or content would embarrass this "
        "brand (e.g. an upbeat ad against a tragic or violent scene), otherwise false"
    )
    return _generate(prompt, AdFit)
