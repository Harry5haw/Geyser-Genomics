from django.conf import settings
from django.urls import reverse

from .alphagenome import TERMS_URL

HELIX_BASE_PAIRS = range(26)  # rungs in the decorative header helix (about 1.3 turns)
SITE_VERSION = "v3.1"  # the release these pages belong to (footer); bump with each tagged release
GITHUB_URL = "https://github.com/Harry5haw/Geyser-Genomics"


def nav_here(path: str) -> str:
    """Which header link is the current page, from the path alone: the publisher renders through RequestFactory with
    the page's real path but no URL resolution. "sample" = a sample's page (inside Samples, not the list itself)."""
    if path == reverse("samples:list"):
        return "samples"
    for name, here in (("figures", "figures"), ("upload_page", "upload"), ("about", "about")):
        if path.startswith(reverse(f"samples:{name}")):
            return here
    return "sample" if path.startswith("/samples/") else ""


def upload_href(static) -> str:
    """The Upload link: the hosted upload page (only with a web API to send to), or the live UI's form on the list."""
    if static is not None:
        return reverse("samples:upload_page") if static.api_base else ""
    return reverse("samples:list") + "#upload"


def geyser(request):
    static = getattr(request, "geyser_static", None)
    return {
        "storage_backend": settings.GEYSER_STORAGE_BACKEND,
        "helix_base_pairs": HELIX_BASE_PAIRS,
        # A page rendered by manage.py publish for the hosted site (publish._render sets it), not the local UI.
        "published": static is not None,
        "nav_here": nav_here(request.path),
        "upload_href": upload_href(static),
        "site_version": SITE_VERSION,
        "github_url": GITHUB_URL,
        "alphagenome_terms_url": TERMS_URL,
    }
