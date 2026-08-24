"""Tests for src/imaging.py - filename generation, processing, LDAP encoding,
cleanup and backfill.

This is where uploaded pixels actually become files on disk, so the tests here
work against the real (temp) AVATAR_ROOT rather than a mock filesystem.
"""

import io
import json
import re

import pytest
from PIL import Image

from src.config import (
    img_formats,
    img_rgba_background_color,
    img_sizes,
    public_avatar_url,
)
from src.imaging import (
    AVATAR_ROOT,
    METADATA_ROOT,
    _flatten_rgba_to_rgb,
    _load_largest_source_image,
    backfill_avatar_set,
    cleanup_avatar_files,
    ensure_size_directories_existence,
    generate_filename,
    get_all_avatar_metadata,
    load_metadata_file,
    normalize_image,
    prepare_ldap_image,
    process_image,
)
from tests.helpers import (
    COLOR_OPAQUE,
    encode_image,
    half_transparent_rgba,
    jpeg_with_exif_orientation,
    make_image,
    noisy_image,
)


def _write_meta(filename_base: str, payload: dict) -> None:
    """Write a metadata sidecar directly, bypassing save_avatar_metadata()."""
    (METADATA_ROOT / f"{filename_base}.meta.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Filename generation
# ---------------------------------------------------------------------------


def test_generated_filename_has_the_documented_three_part_shape():
    name = generate_filename()
    # uuid4 hex (32 chars) - token_urlsafe(64) - nanosecond timestamp
    assert re.fullmatch(r"[0-9a-f]{32}-[A-Za-z0-9_-]{86}-\d+", name), name


def test_generated_filenames_are_unique():
    names = {generate_filename() for _ in range(200)}
    assert len(names) == 200


def test_generated_filename_contains_no_path_separators():
    # The value is used directly as a path component and inside public URLs.
    name = generate_filename()
    assert "/" not in name and "\\" not in name and ".." not in name


# ---------------------------------------------------------------------------
# Storage layout
# ---------------------------------------------------------------------------


def test_ensure_size_directories_creates_every_configured_size_and_metadata():
    ensure_size_directories_existence()
    assert METADATA_ROOT.is_dir()
    for size in img_sizes:
        assert (AVATAR_ROOT / f"{size}x{size}").is_dir()


# ---------------------------------------------------------------------------
# normalize_image
# ---------------------------------------------------------------------------


def test_normalize_applies_exif_orientation_to_the_pixels():
    # Orientation 6 = rotate 90 degrees, so a 200x100 source becomes 100x200.
    raw = jpeg_with_exif_orientation(orientation=6, size=(200, 100))
    with Image.open(io.BytesIO(raw)) as source:
        source.load()
        assert source.size == (200, 100)
        normalized = normalize_image(source)
    assert normalized.size == (100, 200)


def test_normalize_strips_exif_and_icc_metadata():
    raw = jpeg_with_exif_orientation(orientation=1, size=(120, 120))
    with Image.open(io.BytesIO(raw)) as source:
        source.load()
        assert source.getexif()  # the source really does carry EXIF
        normalized = normalize_image(source)
    assert dict(normalized.getexif()) == {}
    assert normalized.info.get("icc_profile") is None
    assert normalized.info.get("exif") is None


@pytest.mark.parametrize("mode", ["P", "L", "LA", "CMYK", "1"])
def test_normalize_converts_exotic_modes_to_rgba(mode):
    normalized = normalize_image(make_image((80, 80), "RGB").convert(mode))
    assert normalized.mode == "RGBA"


@pytest.mark.parametrize("mode", ["RGB", "RGBA"])
def test_normalize_leaves_rgb_and_rgba_modes_alone(mode):
    assert normalize_image(make_image((80, 80), mode)).mode == mode


# ---------------------------------------------------------------------------
# RGBA flattening
# ---------------------------------------------------------------------------


def test_transparent_pixels_are_composited_onto_the_configured_background():
    flattened = _flatten_rgba_to_rgb(half_transparent_rgba((40, 40)))
    assert flattened.mode == "RGB"
    # Left half keeps the opaque source color, right half becomes the configured
    # background (red in the test config) - not black, which a bare convert() gives.
    assert flattened.getpixel((5, 20)) == COLOR_OPAQUE
    assert flattened.getpixel((35, 20)) == tuple(img_rgba_background_color)


def test_flattening_an_rgb_image_is_a_no_op():
    source = make_image((20, 20), "RGB")
    assert _flatten_rgba_to_rgb(source) is source


def test_flattening_an_unexpected_mode_raises_instead_of_silently_blackening():
    with pytest.raises(ValueError, match="Unexpected image mode"):
        _flatten_rgba_to_rgb(make_image((20, 20), "RGB").convert("L"))


# ---------------------------------------------------------------------------
# process_image
# ---------------------------------------------------------------------------


def test_process_image_writes_every_size_and_format_combination():
    base = generate_filename()
    urls, total_bytes = process_image(make_image((300, 300)), base)

    for size in img_sizes:
        for ext in img_formats:
            path = AVATAR_ROOT / f"{size}x{size}" / f"{base}.{ext}"
            assert path.is_file(), path
            with Image.open(path) as written:
                assert written.size == (size, size)

    assert total_bytes > 0
    assert set(urls) == {f"{s}x{s}" for s in img_sizes}
    assert set(urls[f"{img_sizes[0]}x{img_sizes[0]}"]) == set(img_formats)


def test_process_image_returns_public_urls_built_from_the_configured_base():
    base = generate_filename()
    urls, _ = process_image(make_image((300, 300)), base)
    size_key = f"{img_sizes[0]}x{img_sizes[0]}"
    assert urls[size_key]["jpg"] == f"{public_avatar_url}/{size_key}/{base}.jpg"


def test_process_image_total_bytes_matches_the_files_on_disk():
    base = generate_filename()
    _urls, total_bytes = process_image(make_image((300, 300)), base)
    on_disk = sum(
        (AVATAR_ROOT / f"{s}x{s}" / f"{base}.{ext}").stat().st_size
        for s in img_sizes
        for ext in img_formats
    )
    assert total_bytes == on_disk


def test_process_image_preserves_config_size_order_in_its_result():
    # Resizing runs largest-first, but the returned mapping must follow config.
    base = generate_filename()
    urls, _ = process_image(make_image((300, 300)), base)
    assert list(urls) == [f"{s}x{s}" for s in img_sizes]


def test_process_image_flattens_alpha_for_jpeg_but_keeps_it_for_png():
    base = generate_filename()
    process_image(normalize_image(half_transparent_rgba((300, 300))), base)

    size_key = f"{img_sizes[0]}x{img_sizes[0]}"
    with Image.open(AVATAR_ROOT / size_key / f"{base}.jpg") as jpeg:
        assert jpeg.mode == "RGB"
    with Image.open(AVATAR_ROOT / size_key / f"{base}.png") as png:
        assert png.mode == "RGBA"


# ---------------------------------------------------------------------------
# prepare_ldap_image
# ---------------------------------------------------------------------------


def test_ldap_image_reuses_a_pre_generated_file_when_it_fits():
    base = generate_filename()
    process_image(make_image((300, 300)), base)
    existing = (AVATAR_ROOT / "128x128" / f"{base}.jpg").read_bytes()

    data = prepare_ldap_image(make_image((300, 300)), base, 128, "jpeg", 0)
    assert data == existing


def test_ldap_image_reencodes_when_the_pre_generated_file_is_too_large():
    # A high-entropy source is required here: a solid-color 128x128 JPEG already
    # weighs a few hundred bytes, so no ceiling would ever force a re-encode.
    base = generate_filename()
    source = noisy_image((300, 300))
    process_image(source, base)
    existing = (AVATAR_ROOT / "128x128" / f"{base}.jpg").read_bytes()
    assert len(existing) > 2048  # the pre-generated file really is too big

    # 2 KB limit forces the quality-reduction loop rather than reuse.
    data = prepare_ldap_image(source, base, 128, "jpeg", 2)
    assert data != existing
    assert len(data) <= 2048


def test_ldap_image_encodes_from_source_when_no_file_exists():
    data = prepare_ldap_image(make_image((300, 300)), generate_filename(), 64, "png", 0)
    with Image.open(io.BytesIO(data)) as decoded:
        assert decoded.size == (64, 64)
        assert decoded.format == "PNG"


def test_ldap_image_raises_when_png_cannot_meet_the_size_limit():
    # PNG is lossless, so there is no quality knob to turn - this must fail loudly.
    with pytest.raises(ValueError, match="lossless"):
        prepare_ldap_image(noisy_image((300, 300)), generate_filename(), 256, "png", 1)


def test_ldap_image_raises_when_even_minimum_quality_is_too_large():
    with pytest.raises(ValueError, match="quality=10"):
        # A sub-kilobyte ceiling is unreachable for a 256px noise image at any
        # JPEG quality down to the floor of 10.
        prepare_ldap_image(
            noisy_image((300, 300)), generate_filename(), 256, "jpeg", 0.5
        )


def test_ldap_image_flattens_alpha_before_jpeg_encoding():
    data = prepare_ldap_image(
        half_transparent_rgba((300, 300)), generate_filename(), 128, "jpeg", 0
    )
    with Image.open(io.BytesIO(data)) as decoded:
        assert decoded.mode == "RGB"


# ---------------------------------------------------------------------------
# cleanup_avatar_files
# ---------------------------------------------------------------------------


def test_cleanup_removes_every_generated_file_and_the_metadata_sidecar():
    base = generate_filename()
    process_image(make_image((300, 300)), base)
    _write_meta(base, {"filename": base, "user_pk": 1})

    deleted, failed = cleanup_avatar_files(base)

    assert deleted == len(img_sizes) * len(img_formats) + 1
    assert failed == 0
    assert not (METADATA_ROOT / f"{base}.meta.json").exists()
    for size in img_sizes:
        assert not list((AVATAR_ROOT / f"{size}x{size}").glob(f"{base}.*"))


def test_cleanup_of_a_nonexistent_set_reports_no_failures():
    assert cleanup_avatar_files(generate_filename()) == (0, 0)


def test_cleanup_counts_unremovable_files_as_failures(monkeypatch):
    base = generate_filename()
    process_image(make_image((300, 300)), base)

    from pathlib import Path as _Path

    real_unlink = _Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self.suffix == ".jpg":
            raise OSError("read-only filesystem")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(_Path, "unlink", failing_unlink)
    deleted, failed = cleanup_avatar_files(base)

    assert failed == len(img_sizes)  # one .jpg per size
    assert deleted == len(img_sizes) * (len(img_formats) - 1)


# ---------------------------------------------------------------------------
# Metadata reading
# ---------------------------------------------------------------------------


def test_get_all_avatar_metadata_returns_every_readable_entry():
    _write_meta("alpha", {"filename": "alpha", "user_pk": 1})
    _write_meta("beta", {"filename": "beta", "user_pk": 2})

    entries = get_all_avatar_metadata()
    assert {entry["filename"] for entry in entries} == {"alpha", "beta"}


def test_get_all_avatar_metadata_skips_corrupt_files():
    _write_meta("good", {"filename": "good", "user_pk": 1})
    (METADATA_ROOT / "broken.meta.json").write_text("{not json", encoding="utf-8")

    entries = get_all_avatar_metadata()
    assert [entry["filename"] for entry in entries] == ["good"]


def test_load_metadata_file_reads_a_single_sidecar():
    _write_meta("alpha", {"filename": "alpha", "user_pk": 7})
    assert load_metadata_file("alpha.meta.json") == {"filename": "alpha", "user_pk": 7}


def test_load_metadata_file_returns_none_for_a_missing_file():
    assert load_metadata_file("nope.meta.json") is None


@pytest.mark.parametrize(
    "traversal",
    ["../config/config.yml", "../../etc/passwd", "..%2fconfig.yml"],
)
def test_load_metadata_file_blocks_path_traversal(traversal):
    assert load_metadata_file(traversal) is None


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


def test_backfill_is_a_no_op_when_the_set_is_complete():
    base = generate_filename()
    process_image(make_image((300, 300)), base)
    assert backfill_avatar_set(base) == (0, 0, 0)


def test_backfill_regenerates_only_the_missing_files():
    base = generate_filename()
    process_image(make_image((300, 300)), base)

    missing = AVATAR_ROOT / "64x64" / f"{base}.webp"
    survivor = AVATAR_ROOT / "64x64" / f"{base}.png"
    survivor_bytes = survivor.read_bytes()
    missing.unlink()

    generated, failed, skipped = backfill_avatar_set(base)

    assert (generated, failed, skipped) == (1, 0, 0)
    assert missing.is_file()
    with Image.open(missing) as regenerated:
        assert regenerated.size == (64, 64)
    # Existing files must never be rewritten.
    assert survivor.read_bytes() == survivor_bytes


def test_backfill_creates_a_size_directory_that_does_not_exist_yet(monkeypatch):
    # Reproduces the "new size added to config" case: the directory for the new
    # size has not been created yet, so the save must not blow up.
    base = generate_filename()
    process_image(make_image((300, 300)), base)

    new_size_dir = AVATAR_ROOT / "32x32"
    assert not new_size_dir.exists()
    monkeypatch.setattr("src.imaging.img_sizes", [*img_sizes, 32])

    generated, failed, skipped = backfill_avatar_set(base)

    assert failed == 0 and skipped == 0
    assert generated == len(img_formats)
    assert new_size_dir.is_dir()


def test_backfill_reports_missing_files_as_skipped_when_no_source_survives():
    base = generate_filename()
    process_image(make_image((300, 300)), base)
    for size in img_sizes:
        for ext in img_formats:
            (AVATAR_ROOT / f"{size}x{size}" / f"{base}.{ext}").unlink()

    generated, failed, skipped = backfill_avatar_set(base)

    # Nothing to regenerate from: reported, but never counted as a failure.
    assert (generated, failed) == (0, 0)
    assert skipped == len(img_sizes) * len(img_formats)


def test_backfill_upscales_from_the_largest_available_source(caplog):
    base = generate_filename()
    process_image(make_image((300, 300)), base)
    # Leave only the smallest size on disk, then delete a large one.
    largest = f"{max(img_sizes)}x{max(img_sizes)}"
    for ext in img_formats:
        (AVATAR_ROOT / largest / f"{base}.{ext}").unlink()

    with caplog.at_level("WARNING", logger="imaging"):
        generated, failed, skipped = backfill_avatar_set(base)

    assert (generated, failed, skipped) == (len(img_formats), 0, 0)
    assert "upscaling" in caplog.text
    with Image.open(AVATAR_ROOT / largest / f"{base}.jpg") as restored:
        assert restored.size == (max(img_sizes), max(img_sizes))


def test_backfill_writes_nothing_in_dry_run_mode(monkeypatch):
    base = generate_filename()
    process_image(make_image((300, 300)), base)
    missing = AVATAR_ROOT / "64x64" / f"{base}.webp"
    missing.unlink()

    monkeypatch.setattr("src.imaging.dry_run", True)
    generated, failed, skipped = backfill_avatar_set(base)

    assert (generated, failed, skipped) == (1, 0, 0)
    assert not missing.exists()  # counted as "would generate", not written


def test_backfill_counts_encode_failures(monkeypatch):
    base = generate_filename()
    process_image(make_image((300, 300)), base)
    (AVATAR_ROOT / "64x64" / f"{base}.webp").unlink()

    def exploding_save(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("src.imaging._save_image", exploding_save)
    generated, failed, skipped = backfill_avatar_set(base)

    assert (generated, failed, skipped) == (0, 1, 0)


def test_backfill_source_selection_prefers_largest_size_then_lossless_format():
    base = generate_filename()
    process_image(make_image((300, 300)), base)

    source = _load_largest_source_image(base)
    assert source is not None
    image, size = source
    assert size == max(img_sizes)
    assert image.size == (max(img_sizes), max(img_sizes))


def test_backfill_source_selection_returns_none_when_nothing_is_readable():
    assert _load_largest_source_image(generate_filename()) is None


def test_backfill_source_selection_skips_an_undecodable_file(caplog):
    base = generate_filename()
    largest = f"{max(img_sizes)}x{max(img_sizes)}"
    (AVATAR_ROOT / largest / f"{base}.png").write_bytes(b"not an image at all")
    # A valid smaller image remains as the fallback source.
    smallest = f"{min(img_sizes)}x{min(img_sizes)}"
    (AVATAR_ROOT / smallest / f"{base}.png").write_bytes(
        encode_image(make_image((min(img_sizes), min(img_sizes))), "PNG")
    )

    with caplog.at_level("WARNING", logger="imaging"):
        source = _load_largest_source_image(base)

    assert source is not None
    assert source[1] == min(img_sizes)
    assert "backfill source" in caplog.text
