"""Tests for src/image_formats.py - the single source of truth for format data.

Every other constant in the module is *derived* from FORMAT_MAP / MIME_TO_EXT.
These tests pin those derivations so that adding or removing a format cannot
silently leave one of the downstream sets out of sync.
"""

from src.image_formats import (
    ALLOWED_EXTENSIONS,
    ALLOWED_FORMATS,
    ALLOWED_PROXY_MIMETYPES,
    BACKFILL_SOURCE_PREFERENCE,
    FORMAT_MAP,
    MIME_TO_EXT,
    NEGOTIATION_PREFERENCE,
)


def test_format_map_values_are_pillow_format_and_extension():
    # Each entry maps to (Pillow save format, canonical on-disk extension).
    for key, value in FORMAT_MAP.items():
        assert len(value) == 2, key
        pillow_fmt, ext = value
        assert pillow_fmt.isupper(), key
        assert ext.islower(), key
        assert "." not in ext, key


def test_jpeg_and_jpg_resolve_to_the_same_canonical_output():
    # config.py relies on this to de-duplicate "jpeg" and "jpg" into one file.
    assert FORMAT_MAP["jpeg"] == FORMAT_MAP["jpg"] == ("JPEG", "jpg")


def test_allowed_extensions_is_derived_from_format_map():
    assert ALLOWED_EXTENSIONS == frozenset(FORMAT_MAP.keys())


def test_allowed_formats_covers_every_pillow_format_in_the_map():
    assert ALLOWED_FORMATS == frozenset(v[0] for v in FORMAT_MAP.values())


def test_proxy_mimetypes_are_derived_from_mime_to_ext():
    assert ALLOWED_PROXY_MIMETYPES == frozenset(MIME_TO_EXT.keys())


def test_svg_is_not_proxyable():
    # SVG can carry embedded JavaScript and must never be proxied to the browser.
    assert "image/svg+xml" not in ALLOWED_PROXY_MIMETYPES


def test_gif_is_importable_but_not_an_output_format():
    # Gravatar may serve GIF, so the proxy must recognize it, but the pipeline
    # never writes GIF files.
    assert "image/gif" in ALLOWED_PROXY_MIMETYPES
    assert "gif" not in ALLOWED_EXTENSIONS


def test_negotiation_preference_only_lists_known_mime_and_extension_pairs():
    for mime, ext in NEGOTIATION_PREFERENCE:
        assert mime in MIME_TO_EXT, mime
        assert MIME_TO_EXT[mime] == ext, mime


def test_negotiation_preference_is_ordered_modern_first():
    # Redirect targets are picked by walking this list, so the order is behavior.
    assert [ext for _mime, ext in NEGOTIATION_PREFERENCE] == [
        "avif",
        "webp",
        "png",
        "jpg",
    ]


def test_backfill_source_preference_covers_every_output_extension():
    output_exts = {ext for _fmt, ext in FORMAT_MAP.values()}
    assert set(BACKFILL_SOURCE_PREFERENCE) == output_exts


def test_backfill_prefers_lossless_png_first():
    # Regenerating from a lossless source avoids stacking generation loss.
    assert BACKFILL_SOURCE_PREFERENCE[0] == "png"
