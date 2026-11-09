from django.conf import settings

HELIX_BASE_PAIRS = range(26)  # rungs in the decorative header helix (about 1.3 turns)


def geyser(request):
    return {
        "storage_backend": settings.GEYSER_STORAGE_BACKEND,
        "helix_base_pairs": HELIX_BASE_PAIRS,
        # A page rendered by manage.py publish for the hosted site (publish._render sets it), not the local UI.
        "published": getattr(request, "geyser_static", None) is not None,
    }
