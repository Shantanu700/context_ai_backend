import uuid

from django.conf import settings
from django.db import models
from pgvector.django import VectorField


class Video(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        PROCESSING = "processing"
        DONE = "done"
        FAILED = "failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_url = models.URLField(blank=True)
    file_key = models.CharField(max_length=512, blank=True)
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    error = models.TextField(blank=True)

    duration = models.FloatField(null=True, blank=True)
    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)
    has_audio = models.BooleanField(null=True, blank=True)

    scenes_total = models.IntegerField(default=0)
    scenes_done = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.file_key or self.source_url or self.id} ({self.status})"


class Ad(models.Model):
    brand = models.CharField(max_length=128)
    title = models.CharField(max_length=256)
    description = models.TextField()
    iab_categories = models.JSONField(default=list)
    target_tone = models.CharField(max_length=64, blank=True)
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
    tone = models.CharField(max_length=64, blank=True)
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
