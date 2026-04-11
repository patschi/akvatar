"""
i18n.py – Internationalisation support.

Loads translations from YAML files in the ``src/languages/`` directory at startup.
Each ``.yml`` file represents one locale (e.g. ``en_US.yml``, ``de_DE.yml``).
The locale is resolved from the OIDC ``locale`` claim at login time, falling back to the
browser's ``Accept-Language`` header for unauthenticated pages.
"""

import logging
from pathlib import Path

import yaml
from flask import g, request, session

log = logging.getLogger("i18n")

# Directory containing per-locale YAML translation files
_LANGUAGES_DIR = Path(__file__).resolve().parent / "languages"

DEFAULT_LOCALE = "en_US"

# Loaded at startup by _load_translations()
TRANSLATIONS: dict[str, dict[str, str]] = {}
SUPPORTED_LOCALES: frozenset[str] = frozenset()

# List of available languages for the UI language selector (populated at startup)
# Each entry is a dict with 'locale', 'code', and 'name'.
AVAILABLE_LANGUAGES: list[dict[str, str]] = []


def _flatten(data: dict, prefix: str = "") -> dict[str, str]:
    """
    Recursively flatten a nested YAML dict into dot-separated keys.

    Example: ``{'login': {'heading': 'Hi'}}`` becomes ``{'login.heading': 'Hi'}``.
    Top-level string values (like ``_code``) are kept as-is.
    """
    flat: dict[str, str] = {}
    for key, value in data.items():
        full_key = f"{prefix}{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{full_key}."))
        else:
            flat[full_key] = str(value)
    return flat


def _load_translations() -> None:
    """
    Scan the languages directory for .yml files and load each into TRANSLATIONS.

    English (DEFAULT_LOCALE) is loaded first and serves as the reference language.
    All other locales are checked against English; any missing keys are backfilled
    from English at load time so that every locale is guaranteed to be complete.
    This eliminates the need for runtime fallback logic in ``t()``.

    Each file must be named ``<locale>.yml`` (e.g. ``en_US.yml``).
    YAML files use a grouped structure where each top-level key is a section
    (e.g. ``login``, ``settings``) containing nested translation keys.  The
    nested structure is flattened to dot-separated keys at load time
    (e.g. ``login: {heading: ...}`` becomes ``login.heading``).

    Special keys ``_code`` and ``_name`` provide UI metadata for the language selector.
    """
    global TRANSLATIONS, SUPPORTED_LOCALES, AVAILABLE_LANGUAGES
    log.info("Loading translations...")

    translations: dict[str, dict[str, str]] = {}
    languages: list[dict[str, str]] = []

    if not _LANGUAGES_DIR.is_dir():
        log.error("Languages directory not found: %s", _LANGUAGES_DIR)
        return

    # Load the reference language (English) first
    default_path = _LANGUAGES_DIR / f"{DEFAULT_LOCALE}.yml"
    if not default_path.is_file():
        log.error(
            "Reference language file not found: %s – i18n will be broken.", default_path
        )
        return

    # Helper: load and flatten a single YAML file, returning (flat_dict, raw_data) or None
    def _load_file(yml_path: Path) -> tuple[dict[str, str], dict] | None:
        try:
            with open(yml_path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            if not isinstance(data, dict):
                log.warning(
                    "Skipping %s – expected a YAML mapping, got %s.",
                    yml_path.name,
                    type(data).__name__,
                )
                return None
            return _flatten(data), data
        except Exception:
            log.exception("Failed to load translation file %s.", yml_path.name)
            return None

    # Load English as the reference
    result = _load_file(default_path)
    if result is None:
        log.error("Failed to parse reference language file %s.", default_path.name)
        return
    default_strings, default_raw = result
    translations[DEFAULT_LOCALE] = default_strings
    languages.append(
        {
            "locale": DEFAULT_LOCALE,
            "code": default_raw.get("_code", DEFAULT_LOCALE.split("_")[0].upper()),
            "name": default_raw.get("_name", DEFAULT_LOCALE),
        }
    )
    log.debug(
        "Loaded %d translation key(s) from %s (reference language).",
        len(default_strings),
        default_path.name,
    )

    # Load all other languages, backfilling missing keys from English
    for yml_path in sorted(_LANGUAGES_DIR.glob("*.yml")):
        locale = yml_path.stem
        if locale == DEFAULT_LOCALE:
            continue

        result = _load_file(yml_path)
        if result is None:
            continue
        flat, raw_data = result

        # Check for missing keys and backfill from English
        missing = set(default_strings.keys()) - set(flat.keys())
        if missing:
            log.warning(
                "Locale %r is missing %d key(s), backfilling from %s: %s",
                locale,
                len(missing),
                DEFAULT_LOCALE,
                ", ".join(sorted(missing)),
            )
            for key in missing:
                flat[key] = default_strings[key]

        translations[locale] = flat
        languages.append(
            {
                "locale": locale,
                "code": raw_data.get("_code", locale.split("_")[0].upper()),
                "name": raw_data.get("_name", locale),
            }
        )
        log.debug("Loaded %d translation key(s) from %s.", len(flat), yml_path.name)

    TRANSLATIONS = translations
    SUPPORTED_LOCALES = frozenset(translations.keys())
    AVAILABLE_LANGUAGES = languages


# Load translations immediately at import time (application startup)
_load_translations()

# Cached reference to the default locale's strings (used by JS translation
# precomputation and as the canonical key set for the application).
_DEFAULT_STRINGS: dict[str, str] = TRANSLATIONS.get(DEFAULT_LOCALE, {})

# Pre-compute a lookup from language prefix to full locale (e.g. 'en' -> 'en_US')
_LANG_PREFIX_MAP: dict[str, str] = {}
for _loc in SUPPORTED_LOCALES:
    _LANG_PREFIX_MAP[_loc.split("_")[0].lower()] = _loc


def _normalize(raw: str) -> str | None:
    """Try to match a raw locale string (e.g. 'en-US', 'de', 'en_US') to a supported locale."""
    tag = raw.strip().replace("-", "_")
    # Exact match
    if tag in SUPPORTED_LOCALES:
        return tag
    # Match by language prefix (e.g. 'en' -> 'en_US')
    prefix = tag.split("_")[0].lower()
    return _LANG_PREFIX_MAP.get(prefix, None)


def resolve_oidc_locale(oidc_locale: str) -> str:
    """
    Parse the OIDC ``locale`` claim and return the first supported locale.

    The claim may be a single value (``"de_DE"``) or a space-separated
    preference list (``"en_US de_DE"``).  Returns DEFAULT_LOCALE if no
    match is found.
    """
    if not oidc_locale:
        return DEFAULT_LOCALE
    for part in oidc_locale.split():
        matched = _normalize(part)
        if matched:
            return matched
    return DEFAULT_LOCALE


def _resolve_locale() -> str:
    """Determine the best locale for the current request without caching."""
    # 1. Cookie override (user preference set via settings UI)
    cookie_locale = request.cookies.get("locale", "") if request else ""
    if cookie_locale in SUPPORTED_LOCALES:
        return cookie_locale

    # 2. Session locale (set during OIDC callback)
    loc = session.get("locale")
    if loc and loc in SUPPORTED_LOCALES:
        return loc

    # 3. Accept-Language header (for unauthenticated pages)
    accept = request.headers.get("Accept-Language", "") if request else ""
    for part in accept.split(","):
        matched = _normalize(part.split(";")[0].strip())
        if matched:
            return matched

    return DEFAULT_LOCALE


def get_locale() -> str:
    """Return the active locale for the current request, cached in flask.g for the duration of the request."""
    # Serve from request-scoped cache to avoid repeated cookie/session/header reads.
    # g is only available inside a request context; RuntimeError is caught for startup calls.
    try:
        cached = getattr(g, "_locale", None)
        if cached is not None:
            return cached
        result = _resolve_locale()
        g._locale = result
        return result
    except RuntimeError:
        return _resolve_locale()


def t(key: str, **kwargs) -> str:
    """Translate *key* into the current request's locale, with optional format arguments.

    All locales are guaranteed complete at startup (missing keys are backfilled from
    English during loading), so no per-call fallback logic is needed.  The bare key
    name is returned only if the key does not exist in English either (programming error).
    """
    locale = get_locale()
    text = TRANSLATIONS.get(locale, _DEFAULT_STRINGS).get(key, key)
    if kwargs:
        text = text.format(**kwargs)
    return text


_JS_KEYS = (
    # Client-side validation
    "upload.invalid_ext",
    # Client-side step labels
    "step.crop",
    "step.compress",
    "step.upload",
    # Server-side step labels (needed to pre-render waiting steps before SSE arrives)
    "step.validated",
    "step.filename",
    "step.processed",
    "step.profile_synced",
    "step.ldap_updated",
    "step.rollback",
    # UI strings
    "upload.processing",
    "upload.button",
    "step.save_failed",
    "result.success",
    "result.retry",
    "result.error",
    "result.csrf_failed",
    "result.contact_admin",
    "result.network_error",
    # Avatar removal confirmation dialog
    "reset_avatar.confirm_title",
    "reset_avatar.confirm_message",
    "reset_avatar.confirm_yes",
    "reset_avatar.confirm_no",
    "reset_avatar.failed",
    # Import dialog (loading states and error messages used in JS)
    "import.load",
    "import.gravatar_loading",
    "import.gravatar_not_found",
    "import.gravatar_error",
    "import.url_loading",
    "import.url_error",
    "import.url_invalid",
    "import.fetch_failed",
    "import.image_too_large",
    "import.url_not_allowed",
)

# Pre-compute JS translation dicts per locale at startup (avoids rebuilding on every request).
# Dots are converted to underscores so JS can access keys as properties (e.g. I18N.step_crop).
# All locales are guaranteed complete (backfilled at load time), so no fallback needed here.
_JS_TRANSLATIONS: dict[str, dict[str, str]] = {
    locale: {k.replace(".", "_"): strings[k] for k in _JS_KEYS}
    for locale, strings in TRANSLATIONS.items()
}


def get_js_translations(locale: str | None = None) -> dict[str, str]:
    """Return the pre-computed subset of translations needed by client-side JavaScript."""
    if locale is None:
        locale = get_locale()
    return _JS_TRANSLATIONS.get(locale, _JS_TRANSLATIONS.get(DEFAULT_LOCALE, {}))
