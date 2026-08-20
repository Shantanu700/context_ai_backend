from django.core.management.base import BaseCommand

from core.embeddings import embed
from core.gemini import IAB_CATEGORIES
from core.matching import ad_text
from core.models import Ad

ADS = [
    ("Northwind Motors", "The 2026 Vega EV", "An electric crossover with 400 km of range and a panoramic roof, built for weekend escapes.", ["Automotive", "Travel"], "aspirational"),
    ("Kettle & Co", "Slow-Roast Coffee Subscription", "Single-origin beans roasted the week they ship, ground to your brewing method.", ["Food & Drink"], "warm"),
    ("Summit Outfitters", "Trailhead 40L Pack", "A weatherproof hiking pack with a suspended frame for long approaches.", ["Sports", "Travel", "Hobbies & Interests"], "adventurous"),
    ("Meridian Bank", "No-Fee Everyday Account", "Current account with no monthly fee, instant transfers and salary advance.", ["Personal Finance", "Business"], "reassuring"),
    ("Lumen Health", "Sleep Tracking Ring", "A titanium ring that measures sleep stages and recovery without a screen.", ["Health & Fitness", "Technology & Computing"], "calm"),
    ("Verdant Home", "Indoor Herb Garden", "A self-watering countertop planter with grow lights for year-round basil.", ["Home & Garden", "Food & Drink"], "wholesome"),
    ("Corso Athletics", "Tempo Running Shoe", "A carbon-plated racing shoe tuned for negative splits on road marathons.", ["Sports", "Health & Fitness"], "energetic"),
    ("Atlas Cloud", "Managed Postgres", "Production Postgres with point-in-time restore and zero-downtime upgrades.", ["Technology & Computing", "Business"], "confident"),
    ("Juniper Pet Co", "Grain-Free Dog Food", "Vet-formulated meals portioned for your dog's weight and delivered monthly.", ["Pets", "Family & Parenting"], "affectionate"),
    ("Rowan & Ash", "Merino Travel Blazer", "An unstructured blazer in machine-washable merino that survives a carry-on.", ["Style & Fashion", "Travel"], "understated"),
    ("Beacon Learning", "Data Science Bootcamp", "A twelve-week part-time course with a portfolio project and hiring support.", ["Education", "Careers", "Technology & Computing"], "motivating"),
    ("Harbour Insurance", "Family Life Cover", "Term life cover priced in minutes, with a payout promise in writing.", ["Personal Finance", "Family & Parenting"], "serious"),
]


class Command(BaseCommand):
    help = "Seed the ad catalog and embed it."

    def add_arguments(self, parser):
        parser.add_argument("--replace", action="store_true", help="delete existing ads first")

    def handle(self, *args, **opts):
        unknown = {c for *_, cats, _ in ADS for c in cats} - set(IAB_CATEGORIES)
        assert not unknown, f"seed ads use categories Gemini is never told about: {unknown}"

        if opts["replace"]:
            self.stdout.write(f"deleted {Ad.objects.all().delete()[0]} existing ads")

        ads = [
            Ad.objects.update_or_create(
                brand=brand,
                title=title,
                defaults={"description": desc, "iab_categories": cats, "target_tone": tone},
            )[0]
            for brand, title, desc, cats, tone in ADS
        ]

        self.stdout.write(f"embedding {len(ads)} ads with the local model...")
        for ad, vector in zip(ads, embed([ad_text(ad) for ad in ads])):
            ad.embedding = vector
        Ad.objects.bulk_update(ads, ["embedding"])
        self.stdout.write(self.style.SUCCESS(f"seeded and embedded {len(ads)} ads"))
