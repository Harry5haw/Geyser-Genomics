"""Static files storage: hashed, compressed and immutable when collected, never a 500 when not.

WhiteNoise's CompressedManifestStaticFilesStorage gives content-hashed names (geyser.<hash>.js), precompressed
copies and immutable caching after collectstatic. On its own it fails every page ("Missing staticfiles manifest
entry") when STATIC_ROOT is set but collectstatic hasn't run, or the manifest predates a new file. Here a name
the manifest doesn't know is served under its plain name, which WhiteNoise finds in the app folders
(WHITENOISE_USE_FINDERS): slower caching for that one file, but the page works and the asset loads.

Not hashing it on the fly (manifest_strict = False) on purpose: that would name a hashed file collectstatic never
wrote, so the URL would 404.
"""

from whitenoise.storage import CompressedManifestStaticFilesStorage


class ForgivingManifestStaticFilesStorage(CompressedManifestStaticFilesStorage):
    def stored_name(self, name):
        try:
            return super().stored_name(name)
        except ValueError:  # "Missing staticfiles manifest entry": not collected, or collected before this file
            return name
