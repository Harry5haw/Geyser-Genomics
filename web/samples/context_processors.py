from django.conf import settings


def geyser(request):
    return {"storage_backend": settings.GEYSER_STORAGE_BACKEND}
