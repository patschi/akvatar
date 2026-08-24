"""
helpers.py - Shared building blocks for the test suite.

Two groups of helpers live here:

  * Image factories - build real, decodable image bytes (JPEG/PNG/WebP/AVIF,
    with and without alpha, with and without an EXIF orientation tag) so
    validation and processing are exercised against genuine encoder output
    rather than hand-written byte strings.
  * HTTP fakes - drop-in replacements for the module-level ``requests.Session``
    objects in ``src/authentik.py``, ``src/image_import.py`` and
    ``src/webhooks.py``.  They record every call and return scripted responses,
    so no test ever touches the network.
"""

import io
import json as _json
import random

import requests
from PIL import Image

# ---------------------------------------------------------------------------
# Image factories
# ---------------------------------------------------------------------------

# Distinct solid colors so a test can prove which source pixels ended up in an
# output file (e.g. that RGBA transparency was composited onto the configured
# background instead of being mapped to black).
COLOR_OPAQUE = (10, 120, 240)
COLOR_TRANSPARENT = (0, 0, 0, 0)


def make_image(
    size: tuple[int, int] = (300, 300),
    mode: str = "RGB",
    color=COLOR_OPAQUE,
) -> Image.Image:
    """Build a solid-color PIL image of *size* in *mode*."""
    if mode == "RGBA" and len(color) == 3:
        color = (*color, 255)
    return Image.new(mode, size, color)


def encode_image(image: Image.Image, fmt: str = "JPEG", **save_kwargs) -> bytes:
    """Encode *image* to raw bytes in the given Pillow format."""
    buf = io.BytesIO()
    image.save(buf, format=fmt, **save_kwargs)
    return buf.getvalue()


def image_bytes(
    size: tuple[int, int] = (300, 300),
    fmt: str = "JPEG",
    mode: str = "RGB",
    color=COLOR_OPAQUE,
    **save_kwargs,
) -> bytes:
    """Build encoded image bytes in one call - the common case in tests."""
    return encode_image(make_image(size, mode, color), fmt, **save_kwargs)


def noisy_image(size: tuple[int, int] = (300, 300), mode: str = "RGB") -> Image.Image:
    """Build a deterministic high-entropy image.

    Solid-color images compress to a few hundred bytes in every format, which
    makes size-limit behavior untestable.  A seeded pseudo-random pattern gives
    an image that stays comfortably above any small byte ceiling while remaining
    reproducible across runs.
    """
    width, height = size
    bands = len(mode)
    rng = random.Random(1234)
    raw = bytes(rng.getrandbits(8) for _ in range(width * height * bands))
    return Image.frombytes(mode, size, raw)


def half_transparent_rgba(size: tuple[int, int] = (200, 200)) -> Image.Image:
    """Build an RGBA image whose left half is opaque and right half fully transparent.

    Used to prove that ``_flatten_rgba_to_rgb`` composites the transparent
    region onto ``images.rgba_background_color`` rather than onto black.
    """
    image = Image.new("RGBA", size, (*COLOR_OPAQUE, 255))
    width, height = size
    for x in range(width // 2, width):
        for y in range(height):
            image.putpixel((x, y), COLOR_TRANSPARENT)
    return image


def jpeg_with_exif_orientation(
    orientation: int = 6, size: tuple[int, int] = (200, 100)
) -> bytes:
    """Encode a non-square JPEG carrying an EXIF Orientation tag.

    Orientation 6 means "rotate 90 degrees clockwise for display", so a correct
    ``exif_transpose`` implementation swaps the reported width and height.
    """
    image = make_image(size, "RGB")
    exif = image.getexif()
    exif[0x0112] = orientation  # 0x0112 = Orientation
    return encode_image(image, "JPEG", exif=exif)


def avif_supported() -> bool:
    """Return True when the installed Pillow can encode AVIF."""
    try:
        encode_image(make_image((16, 16)), "AVIF")
    except Exception:
        return False
    return True


def upload_file(data: bytes, filename: str = "avatar.jpg"):
    """Build the ``(stream, filename)`` tuple a Flask test client POST expects."""
    return (io.BytesIO(data), filename)


# ---------------------------------------------------------------------------
# HTTP fakes
# ---------------------------------------------------------------------------


class FakeResponse:
    """A minimal stand-in for ``requests.Response``.

    Supports everything the application actually uses: ``status_code``,
    ``headers``, ``json()``, ``content``, ``raise_for_status()``,
    ``iter_content()``, ``close()`` and the context-manager protocol.
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data=None,
        content: bytes = b"",
        headers: dict | None = None,
        url: str = "https://example.invalid/",
        method: str = "GET",
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.content = (
            content if json_data is None else _json.dumps(json_data).encode("utf-8")
        )
        self.headers = dict(headers or {})
        if json_data is not None:
            self.headers.setdefault("Content-Type", "application/json")
        self.url = url
        self.closed = False
        # authentik._parse_json reads resp.request.method / resp.url in its
        # error message, so a request stub has to be present.
        self.request = requests.Request(method=method, url=url)

    def json(self):
        if self._json_data is None:
            raise ValueError("No JSON object could be decoded")
        return self._json_data

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"HTTP {self.status_code}", response=self
            )

    def iter_content(self, chunk_size: int = 8192):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


class RecordedCall:
    """One HTTP call captured by :class:`FakeSession`."""

    def __init__(self, method: str, url: str, kwargs: dict) -> None:
        self.method = method
        self.url = url
        self.kwargs = kwargs

    @property
    def json_body(self):
        return self.kwargs.get("json")

    @property
    def params(self) -> dict:
        return self.kwargs.get("params") or {}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"<RecordedCall {self.method} {self.url}>"


class FakeSession:
    """Records requests and answers them from a scripted handler.

    ``handler(method, url, **kwargs)`` returns a :class:`FakeResponse` (or
    raises, to simulate a network error).  With no handler every call raises,
    which makes an unstubbed outbound request fail loudly instead of silently
    reaching the network.
    """

    def __init__(self, handler=None) -> None:
        self.handler = handler
        self.calls: list[RecordedCall] = []
        self.headers: dict[str, str] = {}
        self.verify = True
        self.cookies = requests.cookies.RequestsCookieJar()

    # -- requests.Session surface used by the application -------------------

    def request(self, method, url, **kwargs):
        return self._dispatch(method.upper(), url, kwargs)

    def get(self, url, **kwargs):
        return self._dispatch("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._dispatch("POST", url, kwargs)

    def patch(self, url, **kwargs):
        return self._dispatch("PATCH", url, kwargs)

    def put(self, url, **kwargs):
        return self._dispatch("PUT", url, kwargs)

    # -- internals ----------------------------------------------------------

    def _dispatch(self, method: str, url: str, kwargs: dict):
        self.calls.append(RecordedCall(method, url, kwargs))
        if self.handler is None:
            raise AssertionError(
                f"Unexpected outbound HTTP call: {method} {url} "
                "(no handler configured for this FakeSession)."
            )
        result = self.handler(method, url, **kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    # -- assertions helpers -------------------------------------------------

    def calls_to(self, substring: str) -> list[RecordedCall]:
        """Return every recorded call whose URL contains *substring*."""
        return [call for call in self.calls if substring in call.url]

    @property
    def methods(self) -> list[str]:
        return [call.method for call in self.calls]


def sequence_handler(*responses):
    """Build a handler that returns the given responses in order.

    Raises AssertionError when more calls arrive than responses were scripted,
    so an unexpected extra request is reported at its call site.
    """
    queue = list(responses)

    def handler(method, url, **kwargs):
        if not queue:
            raise AssertionError(
                f"Unscripted extra HTTP call: {method} {url} "
                f"(sequence of {len(responses)} response(s) exhausted)."
            )
        return queue.pop(0)

    return handler


# ---------------------------------------------------------------------------
# Server-Sent Events
# ---------------------------------------------------------------------------


def sse_events(payload) -> list[dict]:
    """Parse a Server-Sent Events response body into a list of JSON payloads.

    The upload endpoint emits one ``data: {...}`` frame per pipeline step, so
    tests assert on the decoded sequence rather than on raw text.
    """
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    return [
        _json.loads(line[len("data: ") :])
        for line in payload.splitlines()
        if line.startswith("data: ")
    ]
