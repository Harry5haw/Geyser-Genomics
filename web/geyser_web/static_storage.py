"""Static files storage: hashed, compressed and immutable when collected, never a 500 when not.

WhiteNoise's CompressedManifestStaticFilesStorage gives content-hashed names (geyser.<hash>.js), precompressed
copies and immutable caching after collectstatic. On its own it fails every page ("Missing staticfiles manifest
entry") when STATIC_ROOT is set but collectstatic hasn't run, or the manifest predates a new file. Here a name
the manifest doesn't know falls back to the plain name, which WhiteNoise serves from the app folders
(WHITENOISE_USE_FINDERS): slower caching for that one file, but the page works.
"""

from whitenoise.storage import CompressedManifestStaticFilesStorage


class ForgivingManifestStaticFilesStorage(CompressedManifestStaticFilesStorage):
    manifest_strict = False  # a name missing from the manifest is hashed on the fly, or (below) left plain

    def hashed_name(self, name, content=None, filename=None):
        try:
            return super().hashed_name(name, content, filename)
        except ValueError:  # not collected (no file under STATIC_ROOT): serve the plain name
            return name
