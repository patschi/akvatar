"""Tests for src/image_validation.py - the upload security boundary.

Everything a user (or a remote Gravatar/URL import) sends passes through
``validate_image_bytes`` before a single pixel is processed, so this is the
module where a regression is most expensive: a missed check means arbitrary
bytes reach Pillow, and a wrong rejection means legitimate photos bounce.
"""

import io

import pytest
from PIL import Image

from src.image_validation import (
    MAX_DIMENSION,
    MIN_DIMENSION,
    ValidationError,
    check_magic_bytes,
    validate_image_bytes,
    validate_upload,
)
from tests.helpers import avif_supported, image_bytes, make_image

# ---------------------------------------------------------------------------
# Magic-byte gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_magic_bytes_accept_real_encoder_output(fmt):
    assert check_magic_bytes(image_bytes((64, 64), fmt)) is None


@pytest.mark.skipif(not avif_supported(), reason="Pillow built without AVIF support")
def test_magic_bytes_accept_avif():
    assert check_magic_bytes(image_bytes((64, 64), "AVIF")) is None


def test_magic_bytes_reject_short_input():
    error = check_magic_bytes(b"\xff\xd8\xff")
    assert error is not None
    assert "12 bytes" in error


@pytest.mark.parametrize(
    "payload",
    [
        b"<html><body>hello</body></html>",
        b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>',
        b"PK\x03\x04" + b"\x00" * 32,  # ZIP container
        b"GIF89a" + b"\x00" * 32,  # GIF is importable but not uploadable
        b"\x00" * 64,
    ],
)
def test_magic_bytes_reject_non_image_payloads(payload):
    assert check_magic_bytes(payload) is not None


def test_magic_bytes_reject_riff_container_that_is_not_webp():
    # A WAVE file is a RIFF container too - the fourcc at offset 8 must be checked.
    riff_wave = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 16
    assert check_magic_bytes(riff_wave) is not None


def test_magic_bytes_reject_ftyp_box_with_a_non_avif_brand():
    # An MP4 shares the ISO base media "ftyp" box; only avif/avis brands pass.
    mp4 = b"\x00\x00\x00\x18" + b"ftyp" + b"isom" + b"\x00" * 16
    assert check_magic_bytes(mp4) is not None


# ---------------------------------------------------------------------------
# Full validation pipeline
# ---------------------------------------------------------------------------


def test_valid_jpeg_is_accepted_and_returns_a_decoded_image():
    data = image_bytes((200, 150), "JPEG")
    image = validate_image_bytes(data, "photo.jpg")
    assert isinstance(image, Image.Image)
    assert image.size == (200, 150)
    assert image.format == "JPEG"


def test_extension_is_matched_case_insensitively():
    data = image_bytes((100, 100), "PNG")
    assert validate_image_bytes(data, "PHOTO.PNG").format == "PNG"


def test_missing_filename_is_rejected():
    with pytest.raises(ValidationError):
        validate_image_bytes(image_bytes(), "")


def test_filename_without_extension_is_rejected():
    with pytest.raises(ValidationError):
        validate_image_bytes(image_bytes(), "avatar")


def test_disallowed_extension_is_rejected():
    with pytest.raises(ValidationError) as excinfo:
        validate_image_bytes(image_bytes(), "avatar.gif")
    assert "gif" in str(excinfo.value)


def test_empty_body_is_rejected():
    with pytest.raises(ValidationError):
        validate_image_bytes(b"", "avatar.jpg")


def test_fake_extension_is_caught_by_the_magic_byte_check():
    # An executable renamed to .jpg must never reach the Pillow decoder.
    with pytest.raises(ValidationError):
        validate_image_bytes(b"MZ\x90\x00" + b"\x00" * 64, "payload.jpg")


def test_a_crafted_payload_never_reaches_the_decoder(monkeypatch):
    """The magic-byte gate must reject *before* Pillow parses anything.

    Pillow would refuse this payload too, so asserting only on the exception
    cannot tell the two layers apart - and the whole point of the gate is that
    untrusted bytes never reach the parser in the first place.
    """
    monkeypatch.setattr(
        Image,
        "open",
        lambda *a, **kw: pytest.fail(
            "Pillow was handed a payload the gate should have rejected"
        ),
    )
    with pytest.raises(ValidationError):
        validate_image_bytes(b"MZ\x90\x00" + b"\x00" * 64, "payload.jpg")


def test_truncated_image_is_rejected_by_the_forced_decode():
    # Image.open() only reads the header; .load() is what catches truncation.
    data = image_bytes((300, 300), "PNG")
    with pytest.raises(ValidationError):
        validate_image_bytes(data[: len(data) // 2], "avatar.png")


def test_format_outside_the_allow_list_is_rejected_after_decode(monkeypatch):
    """A decodable image in a format we never intend to handle must be refused.

    The magic-byte gate already stops a TIFF, so it is waved through here to
    leave the post-decode allow-list as the only thing that can reject: that is
    the layer under test, and it is what protects against a payload whose
    signature we accept but whose real format Pillow reports as something else.
    """
    monkeypatch.setattr("src.image_validation.check_magic_bytes", lambda _b: None)
    buf = io.BytesIO()
    make_image((100, 100)).save(buf, format="TIFF")

    with pytest.raises(ValidationError):
        validate_image_bytes(buf.getvalue(), "avatar.png")


def test_image_smaller_than_the_smallest_configured_size_is_rejected():
    too_small = MIN_DIMENSION - 1
    with pytest.raises(ValidationError) as excinfo:
        validate_image_bytes(image_bytes((too_small, too_small), "PNG"), "a.png")
    assert str(MIN_DIMENSION) in str(excinfo.value)


def test_image_at_exactly_the_minimum_dimension_is_accepted():
    data = image_bytes((MIN_DIMENSION, MIN_DIMENSION), "PNG")
    assert validate_image_bytes(data, "a.png").size == (MIN_DIMENSION, MIN_DIMENSION)


def test_min_dimension_tracks_the_smallest_configured_output_size():
    from src.config import img_sizes

    assert MIN_DIMENSION == min(img_sizes)


def test_oversized_image_is_rejected(monkeypatch):
    # Encoding a real 8193 px image is wasteful; lower the cap instead so the
    # comparison itself is what is under test.
    monkeypatch.setattr("src.image_validation.MAX_DIMENSION", 128)
    with pytest.raises(ValidationError) as excinfo:
        validate_image_bytes(image_bytes((200, 100), "PNG"), "a.png")
    assert "128" in str(excinfo.value)


def test_decompression_bomb_limit_is_applied_to_pillow():
    from src.config import MAX_IMAGE_PIXELS

    assert Image.MAX_IMAGE_PIXELS == MAX_IMAGE_PIXELS
    assert MAX_DIMENSION == 8192


def test_rgba_png_is_accepted_without_being_flattened():
    # Normalization happens later in the pipeline; validation must not alter pixels.
    data = image_bytes((100, 100), "PNG", mode="RGBA")
    image = validate_image_bytes(data, "a.png")
    assert image.mode == "RGBA"


# ---------------------------------------------------------------------------
# Werkzeug FileStorage wrapper
# ---------------------------------------------------------------------------


class _FakeUpload:
    """Minimal stand-in for the Werkzeug FileStorage that Flask hands the route."""

    def __init__(self, data: bytes, filename: str | None) -> None:
        self._data = data
        self.filename = filename

    def read(self) -> bytes:
        return self._data


def test_validate_upload_delegates_to_validate_image_bytes():
    upload = _FakeUpload(image_bytes((120, 120), "JPEG"), "avatar.jpg")
    assert validate_upload(upload).size == (120, 120)


def test_validate_upload_rejects_a_file_with_no_filename():
    # A missing filename must not even read the body.
    upload = _FakeUpload(image_bytes(), None)
    with pytest.raises(ValidationError):
        validate_upload(upload)
