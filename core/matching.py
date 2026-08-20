"""Scene <-> ad matching: pgvector similarity, boosted by shared IAB categories."""

from django.conf import settings
from pgvector.django import CosineDistance

from .models import Ad


def scene_text(scene) -> str:
    """The blob that gets embedded — visuals, objects, tone and speech together."""
    return " ".join(filter(None, [
        scene.description,
        f"Objects: {', '.join(scene.objects_seen)}." if scene.objects_seen else "",
        f"Tone: {scene.tone}." if scene.tone else "",
        f"Categories: {', '.join(scene.iab_categories)}." if scene.iab_categories else "",
        scene.transcript_text,
    ]))


def ad_text(ad: Ad) -> str:
    return (
        f"{ad.brand} — {ad.title}. {ad.description} "
        f"Tone: {ad.target_tone}. Categories: {', '.join(ad.iab_categories)}."
    )


def rank_ads(scene) -> list[tuple[Ad, float]]:
    """Top-k ads for a scene. Vector similarity plus a bonus per shared IAB category.

    ponytail: candidates come back from pgvector then re-rank in Python — the boost is a
    JSON set intersection, and a dozen-row catalog makes doing it in SQL pure ceremony.
    Push it into the query if the catalog ever reaches thousands.
    """
    candidates = (
        Ad.objects.exclude(embedding=None)
        .annotate(distance=CosineDistance("embedding", scene.embedding))
        .order_by("distance")[: settings.AD_CANDIDATES]
    )

    scene_cats = {c.casefold() for c in scene.iab_categories}
    scored = []
    for ad in candidates:
        shared = scene_cats & {c.casefold() for c in ad.iab_categories}
        scored.append((ad, (1.0 - ad.distance) + settings.IAB_BOOST * len(shared)))

    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[: settings.TOP_K_ADS]
