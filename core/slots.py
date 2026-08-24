"""Turning the analysis into a first draft of the ad plan.

The pipeline recommends an ad per scene; the editor works in slots. This bridges the
two once, the first time a video is opened, and then gets out of the way — every later
change is the operator's, sent back by the editor.
"""

from .models import AdSlot

# A recommendation at the very top or very bottom of a video is really a pre/post-roll,
# not a break in the middle of the story.
EDGE_FRACTION = 0.05
DEFAULT_DURATION = 15.0


def placement_for(at_seconds: float, duration: float | None) -> str:
    """Where in the video this lands. Falls back to mid-roll when duration is unknown."""
    if not duration or duration <= 0:
        return AdSlot.Placement.MID
    if at_seconds <= duration * EDGE_FRACTION:
        return AdSlot.Placement.PRE
    if at_seconds >= duration * (1 - EDGE_FRACTION):
        return AdSlot.Placement.POST
    return AdSlot.Placement.MID


def seed_slots(video) -> int:
    """Draft one suggested slot per scene that has a recommended ad. Returns how many.

    Runs at most once per video. After that the slot list is the operator's document, and
    re-deriving it would silently undo their edits — most visibly the deletions, which
    leave no row behind for an "are there slots?" check to notice.
    """
    if video.slots_seeded:
        return 0

    scenes = video.scenes.select_related("recommended_ad").exclude(recommended_ad=None).order_by("index")
    drafts = [
        AdSlot(
            video=video,
            scene=scene,
            ad=scene.recommended_ad,
            at_seconds=scene.start,
            duration=DEFAULT_DURATION,
            placement=placement_for(scene.start, video.duration),
            is_overlay=scene.recommended_ad.ad_type == "overlay",
            state=AdSlot.State.SUGGESTED,
            score=scene.match_score,
        )
        for scene in scenes
    ]
    AdSlot.objects.bulk_create(drafts)
    video.slots_seeded = True
    video.save(update_fields=["slots_seeded"])
    return len(drafts)
