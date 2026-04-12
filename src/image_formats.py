"""
image_formats.py - Static image format definitions.

Single source of truth for all image format constants. Adding or removing
a format only requires touching this file: ALLOWED_EXTENSIONS,
ALLOWED_FORMATS, and ALLOWED_PROXY_MIMETYPES are all derived from the two
primary maps below.

This module has zero imports so that both config.py and imaging.py can
import from it without creating a circular dependency.
"""

# Maps each accepted format name (lower-case) to (Pillow save format, canonical file extension).
# Both "jpeg" and "jpg" are accepted inputs; both resolve to the "jpg" extension.
FORMAT_MAP: dict[str, tuple[str, str]] = {
    "jpeg": ("JPEG", "jpg"),
    "jpg": ("JPEG", "jpg"),
    "png": ("PNG", "png"),
    "webp": ("WEBP", "webp"),
}

# File extensions the upload endpoint accepts - derived from FORMAT_MAP so
# the two stay in sync automatically.
ALLOWED_EXTENSIONS: frozenset[str] = frozenset(FORMAT_MAP.keys())

# Pillow format strings considered legitimate (what image.format returns
# after decode) - derived from FORMAT_MAP values.
ALLOWED_FORMATS: frozenset[str] = frozenset(v[0] for v in FORMAT_MAP.values())

# Maps each accepted MIME type to the canonical file extension used on disk.
# "image/gif" is included for Gravatar responses that may return GIF content;
# GIF is not an output format, but it must be recognized so the import
# pipeline can download and convert it.
MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}

# Allowlist of MIME types accepted from remote servers - derived from
# MIME_TO_EXT so both stay in sync automatically: adding a new MIME type to
# MIME_TO_EXT automatically permits it here too.  Types absent from
# MIME_TO_EXT (e.g. image/svg+xml, which can carry embedded JavaScript) are
# excluded by design.  The upload pipeline's magic-byte check and Pillow
# decode are the definitive gate; this is a first layer that prevents
# obviously wrong content from being proxied to the browser at all.
ALLOWED_PROXY_MIMETYPES: frozenset[str] = frozenset(MIME_TO_EXT.keys())
