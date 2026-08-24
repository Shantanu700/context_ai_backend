import uuid6

from django.conf import settings
from django.db import models
from pgvector.django import VectorField


class Video(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        PROCESSING = "processing"
        DONE = "done"
        FAILED = "failed"

    uuid = models.UUIDField(default=uuid6.uuid7, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="videos", null=True, blank=True, on_delete=models.CASCADE
    )
    source_url = models.URLField(blank=True)
    file_key = models.CharField(max_length=512, blank=True)
    thumbnail_key = models.CharField(max_length=512, blank=True)
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    error = models.TextField(blank=True)

    duration = models.FloatField(null=True, blank=True)
    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)
    has_audio = models.BooleanField(null=True, blank=True)

    detect_scenes_enabled = models.BooleanField(default=True)
    transcribe_enabled = models.BooleanField(default=True)
    analyze_enabled = models.BooleanField(default=True)
    transcript_segments = models.JSONField(default=list, blank=True)

    scenes_total = models.IntegerField(default=0)
    scenes_done = models.IntegerField(default=0)

    slots_seeded = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.file_key or self.source_url or self.uuid} ({self.status})"


class Tone(models.Model):
    name = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Ad(models.Model):
    class AdType(models.TextChoices):
        VIDEO = "video"
        OVERLAY = "overlay"

    brand = models.CharField(max_length=128)
    title = models.CharField(max_length=256)
    description = models.TextField()
    ad_type = models.CharField(max_length=16, choices=AdType.choices)
    asset_key = models.CharField(max_length=512, blank=True)
    iab_categories = models.JSONField(default=list)
    target_tone = models.ForeignKey(Tone, null=True, blank=True, on_delete=models.SET_NULL, related_name="ads")
    embedding = VectorField(dimensions=settings.EMBEDDING_DIM, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["brand", "title"]

    def __str__(self):
        return f"{self.brand} — {self.title}"


class Scene(models.Model):
    video = models.ForeignKey(Video, related_name="scenes", on_delete=models.CASCADE)
    index = models.IntegerField()
    start = models.FloatField()
    end = models.FloatField()
    keyframe_keys = models.JSONField(default=list)
    transcript_text = models.TextField(blank=True)

    # filled by Gemini analysis (Phase 4)
    description = models.TextField(blank=True)
    objects_seen = models.JSONField(default=list)
    tone = models.ForeignKey(Tone, null=True, blank=True, on_delete=models.SET_NULL, related_name="scenes")
    iab_categories = models.JSONField(default=list)
    embedding = VectorField(dimensions=settings.EMBEDDING_DIM, null=True, blank=True)

    # recommendation lives here — top-k is a read-only artifact of one run
    recommended_ad = models.ForeignKey(Ad, null=True, blank=True, on_delete=models.SET_NULL)
    match_score = models.FloatField(null=True, blank=True)
    rationale = models.TextField(blank=True)
    brand_safety_flag = models.BooleanField(default=False)
    top_matches = models.JSONField(default=list)

    class Meta:
        ordering = ["video", "index"]
        constraints = [models.UniqueConstraint(fields=["video", "index"], name="unique_scene_index")]

    def __str__(self):
        return f"scene {self.index} [{self.start:.1f}-{self.end:.1f}]"


class AdSlot(models.Model):
    class State(models.TextChoices):
        SUGGESTED = "suggested"
        ACCEPTED = "accepted"
        HELD = "held"
        REJECTED = "rejected"

    class Placement(models.TextChoices):
        PRE = "pre_roll"
        MID = "mid_roll"
        POST = "post_roll"

    video = models.ForeignKey(Video, related_name="slots", on_delete=models.CASCADE)
    scene = models.ForeignKey(Scene, null=True, blank=True, related_name="slots", on_delete=models.SET_NULL)
    ad = models.ForeignKey(Ad, null=True, blank=True, on_delete=models.SET_NULL)

    at_seconds = models.FloatField()
    duration = models.FloatField(default=15.0)
    placement = models.CharField(max_length=16, choices=Placement, default=Placement.MID)
    is_overlay = models.BooleanField(default=False)  # video break vs. overlay: which timeline lane
    state = models.CharField(max_length=16, choices=State, default=State.SUGGESTED)
    score = models.FloatField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["video", "at_seconds"]

    def __str__(self):
        return f"{self.get_placement_display()} @ {self.at_seconds:.1f}s ({self.state})"
