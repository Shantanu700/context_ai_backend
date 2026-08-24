import hashlib
import subprocess
import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.embeddings import embed
from core.gemini import IAB_CATEGORIES
from core.matching import ad_text
from core.models import Ad, Tone
from core.storage import store

# brand, title, description, iab categories, tone, ad_type — alternating video/overlay so
# seeded data exercises both creative kinds, not just one
ADS = [
    ("Northwind Motors", "The 2026 Vega EV", "An electric crossover with 400 km of range and a panoramic roof, built for weekend escapes.", ["Automotive", "Travel"], "aspirational", "video"),
    ("Kettle & Co", "Slow-Roast Coffee Subscription", "Single-origin beans roasted the week they ship, ground to your brewing method.", ["Food & Drink"], "warm", "overlay"),
    ("Summit Outfitters", "Trailhead 40L Pack", "A weatherproof hiking pack with a suspended frame for long approaches.", ["Sports", "Travel", "Hobbies & Interests"], "adventurous", "video"),
    ("Meridian Bank", "No-Fee Everyday Account", "Current account with no monthly fee, instant transfers and salary advance.", ["Personal Finance", "Business"], "reassuring", "overlay"),
    ("Lumen Health", "Sleep Tracking Ring", "A titanium ring that measures sleep stages and recovery without a screen.", ["Health & Fitness", "Technology & Computing"], "calm", "video"),
    ("Verdant Home", "Indoor Herb Garden", "A self-watering countertop planter with grow lights for year-round basil.", ["Home & Garden", "Food & Drink"], "wholesome", "overlay"),
    ("Corso Athletics", "Tempo Running Shoe", "A carbon-plated racing shoe tuned for negative splits on road marathons.", ["Sports", "Health & Fitness"], "energetic", "video"),
    ("Atlas Cloud", "Managed Postgres", "Production Postgres with point-in-time restore and zero-downtime upgrades.", ["Technology & Computing", "Business"], "confident", "overlay"),
    ("Juniper Pet Co", "Grain-Free Dog Food", "Vet-formulated meals portioned for your dog's weight and delivered monthly.", ["Pets", "Family & Parenting"], "affectionate", "video"),
    ("Rowan & Ash", "Merino Travel Blazer", "An unstructured blazer in machine-washable merino that survives a carry-on.", ["Style & Fashion", "Travel"], "understated", "overlay"),
    ("Beacon Learning", "Data Science Bootcamp", "A twelve-week part-time course with a portfolio project and hiring support.", ["Education", "Careers", "Technology & Computing"], "motivating", "video"),
    ("Harbour Insurance", "Family Life Cover", "Term life cover priced in minutes, with a payout promise in writing.", ["Personal Finance", "Family & Parenting"], "serious", "overlay"),
]

# common install paths for a font drawtext can use; ffmpeg needs an explicit fontfile when
# fontconfig isn't set up, so fall back to a plain background if none of these exist
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def _brand_color(brand: str) -> str:
    """A stable hex color per brand, so placeholders are at least visually distinct."""
    return "0x" + hashlib.sha1(brand.encode()).hexdigest()[:6]


def _font_path() -> str | None:
    return next((p for p in _FONT_CANDIDATES if Path(p).exists()), None)


def _generate_asset(brand: str, title: str, ad_type: str, dest: Path) -> None:
    """No real creative exists to seed with, so stand in with a short solid-color clip
    (video ads) or a single labeled frame (overlay ads), colored per brand."""
    label = f"{brand} — {title}".replace("\\", "").replace(":", "\\:").replace("'", "’")
    vf = f"drawtext=text='{label}':fontcolor=white:fontsize=28:x=(w-text_w)/2:y=(h-text_h)/2"
    if font := _font_path():
        vf += f":fontfile={font}"

    cmd = [
        "ffmpeg", "-nostdin", "-v", "error",
        "-f", "lavfi", "-i", f"color=c={_brand_color(brand)}:s=640x360",
        "-vf", vf, "-y",
    ]
    cmd += ["-t", "3", "-pix_fmt", "yuv420p", str(dest)] if ad_type == Ad.AdType.VIDEO else [
        "-frames:v", "1", str(dest),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        raise CommandError("ffmpeg is required to generate seed ad assets but was not found")
    except subprocess.CalledProcessError as e:
        raise CommandError(f"ffmpeg failed to generate a placeholder asset for {brand!r}: {e.stderr}")


class Command(BaseCommand):
    help = "Seed the ad catalog, embed it, and attach a placeholder creative asset to each ad."

    def add_arguments(self, parser):
        parser.add_argument("--replace", action="store_true", help="delete existing ads first")

    def handle(self, *args, **opts):
        unknown = {c for *_, cats, _tone, _type in ADS for c in cats} - set(IAB_CATEGORIES)
        assert not unknown, f"seed ads use categories Gemini is never told about: {unknown}"

        if opts["replace"]:
            self.stdout.write(f"deleted {Ad.objects.all().delete()[0]} existing ads")

        ads = [
            Ad.objects.update_or_create(
                brand=brand,
                title=title,
                defaults={
                    "description": desc,
                    "iab_categories": cats,
                    "target_tone": Tone.objects.get_or_create(name=tone.strip().lower())[0],
                    "ad_type": ad_type,
                },
            )[0]
            for brand, title, desc, cats, tone, ad_type in ADS
        ]

        self.stdout.write("generating placeholder creative assets...")
        with tempfile.TemporaryDirectory() as tmp:
            for ad in ads:
                if ad.asset_key:
                    continue  # already has a creative attached — don't clobber it
                ext = "mp4" if ad.ad_type == Ad.AdType.VIDEO else "jpg"
                local = Path(tmp) / f"{ad.pk}.{ext}"
                _generate_asset(ad.brand, ad.title, ad.ad_type, local)
                ad.asset_key = store(local, f"ads/{ad.pk}/asset.{ext}")
        Ad.objects.bulk_update(ads, ["asset_key"])

        self.stdout.write(f"embedding {len(ads)} ads with the local model...")
        for ad, vector in zip(ads, embed([ad_text(ad) for ad in ads])):
            ad.embedding = vector
        Ad.objects.bulk_update(ads, ["embedding"])
        self.stdout.write(self.style.SUCCESS(f"seeded, embedded and attached assets to {len(ads)} ads"))
