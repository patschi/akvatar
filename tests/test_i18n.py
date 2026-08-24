"""Tests for src/i18n.py - translation loading and locale resolution.

Two kinds of coverage live here:

  * Behavior - locale negotiation from the OIDC claim, cookie, session and
    Accept-Language header, plus the ``t()`` lookup and formatting.
  * Data integrity - every shipped locale file must parse, expose its UI
    metadata, and (after the startup backfill) contain every key English has,
    including the subset the browser JavaScript reads.  Without this, a missing
    translation key only surfaces as a raw ``login.heading`` string in the UI.
"""

import pytest
import yaml

from src.config import DEFAULT_LOCALE
from src.i18n import (
    _JS_KEYS,
    _LANGUAGES_DIR,
    AVAILABLE_LANGUAGES,
    SUPPORTED_LOCALES,
    TRANSLATIONS,
    _flatten,
    _normalize,
    get_js_translations,
    get_locale,
    resolve_oidc_locale,
    t,
)

NON_DEFAULT_LOCALES = sorted(SUPPORTED_LOCALES - {DEFAULT_LOCALE})


# ---------------------------------------------------------------------------
# Flattening
# ---------------------------------------------------------------------------


def test_nested_sections_flatten_to_dotted_keys():
    assert _flatten({"login": {"heading": "Hi", "sub": {"deep": "x"}}}) == {
        "login.heading": "Hi",
        "login.sub.deep": "x",
    }


def test_top_level_scalars_are_kept_as_is():
    assert _flatten({"_code": "EN", "_name": "English"}) == {
        "_code": "EN",
        "_name": "English",
    }


def test_non_string_values_are_coerced_to_strings():
    # YAML happily parses "1.0" as a float; templates need a string.
    assert _flatten({"a": 5, "b": True}) == {"a": "5", "b": "True"}


# ---------------------------------------------------------------------------
# Locale normalization and OIDC claim parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("en_US", "en_US"),
        ("en-US", "en_US"),
        ("en", "en_US"),
        ("EN", "en_US"),
        ("de", "de_DE"),
        ("de-AT", "de_DE"),  # unknown region falls back to the language
        ("  fr  ", "fr_FR"),
    ],
)
def test_locale_tags_normalize_to_a_supported_locale(raw, expected):
    assert _normalize(raw) == expected


def test_an_unsupported_language_does_not_normalize():
    assert _normalize("zz_ZZ") is None


def test_the_oidc_claim_accepts_a_single_value():
    assert resolve_oidc_locale("de_DE") == "de_DE"


def test_the_oidc_claim_accepts_a_space_separated_preference_list():
    assert resolve_oidc_locale("zz_ZZ fr_FR de_DE") == "fr_FR"


@pytest.mark.parametrize("claim", ["", "zz_ZZ", "klingon"])
def test_an_unusable_oidc_claim_falls_back_to_the_default_locale(claim):
    assert resolve_oidc_locale(claim) == DEFAULT_LOCALE


# ---------------------------------------------------------------------------
# Per-request locale resolution
# ---------------------------------------------------------------------------


def test_locale_outside_a_request_context_is_the_default():
    assert get_locale() == DEFAULT_LOCALE


def test_the_locale_cookie_wins_over_everything(flask_app):
    with flask_app.test_request_context(
        "/", headers={"Accept-Language": "fr-FR"}
    ) as ctx:
        ctx.request.cookies = {"locale": "de_DE"}
        from flask import session

        session["locale"] = "es_ES"
        assert get_locale() == "de_DE"


def test_the_session_locale_is_used_when_no_cookie_is_set(flask_app):
    with flask_app.test_request_context("/", headers={"Accept-Language": "fr-FR"}):
        from flask import session

        session["locale"] = "es_ES"
        assert get_locale() == "es_ES"


def test_accept_language_is_used_for_anonymous_visitors(flask_app):
    with flask_app.test_request_context(
        "/", headers={"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}
    ):
        assert get_locale() == "de_DE"


def test_an_unsupported_accept_language_falls_back_to_the_default(flask_app):
    with flask_app.test_request_context("/", headers={"Accept-Language": "zz-ZZ"}):
        assert get_locale() == DEFAULT_LOCALE


def test_an_unsupported_cookie_value_is_ignored(flask_app):
    with flask_app.test_request_context("/") as ctx:
        ctx.request.cookies = {"locale": "zz_ZZ"}
        assert get_locale() == DEFAULT_LOCALE


def test_the_resolved_locale_is_cached_for_the_request(flask_app):
    with flask_app.test_request_context("/") as ctx:
        first = get_locale()
        # A later cookie change must not flip the locale mid-request.
        ctx.request.cookies = {"locale": "de_DE"}
        assert get_locale() == first


# ---------------------------------------------------------------------------
# t()
# ---------------------------------------------------------------------------


def test_translation_lookup_returns_the_localized_string(flask_app):
    with flask_app.test_request_context("/") as ctx:
        ctx.request.cookies = {"locale": "de_DE"}
        assert t("upload.heading") == TRANSLATIONS["de_DE"]["upload.heading"]


def test_translation_lookup_interpolates_format_arguments():
    rendered = t("error.too_small", w=10, h=10, min_dim=64)
    assert "10" in rendered and "64" in rendered


def test_an_unknown_key_returns_the_key_itself():
    # A missing key is a programming error, and surfacing it verbatim makes it
    # obvious in the UI instead of rendering an empty string.
    assert t("definitely.not.a.real.key") == "definitely.not.a.real.key"


# ---------------------------------------------------------------------------
# Shipped translation data
# ---------------------------------------------------------------------------


def test_english_is_loaded_as_the_reference_language():
    assert DEFAULT_LOCALE in TRANSLATIONS
    assert TRANSLATIONS[DEFAULT_LOCALE]


def test_every_language_file_on_disk_is_loaded():
    on_disk = {path.stem for path in _LANGUAGES_DIR.glob("*.yml")}
    assert on_disk == set(SUPPORTED_LOCALES)
    assert len(on_disk) >= 4  # en_US, de_DE, fr_FR, es_ES


@pytest.mark.parametrize("locale", sorted(SUPPORTED_LOCALES))
def test_every_locale_has_the_full_english_key_set(locale):
    # Missing keys are backfilled at load time; a difference here means the
    # backfill itself regressed.
    assert set(TRANSLATIONS[locale]) == set(TRANSLATIONS[DEFAULT_LOCALE])


@pytest.mark.parametrize("locale", NON_DEFAULT_LOCALES)
def test_translation_files_are_complete_without_relying_on_the_backfill(locale):
    # Reads the raw YAML (pre-backfill) so an untranslated key is reported here
    # rather than silently shipping English text in a localized UI.
    raw = yaml.safe_load((_LANGUAGES_DIR / f"{locale}.yml").read_text(encoding="utf-8"))
    english = yaml.safe_load(
        (_LANGUAGES_DIR / f"{DEFAULT_LOCALE}.yml").read_text(encoding="utf-8")
    )
    missing = set(_flatten(english)) - set(_flatten(raw))
    assert not missing, f"{locale} is missing: {sorted(missing)}"


@pytest.mark.parametrize("locale", sorted(SUPPORTED_LOCALES))
def test_every_locale_declares_its_ui_metadata(locale):
    entry = next(lang for lang in AVAILABLE_LANGUAGES if lang["locale"] == locale)
    assert entry["code"] and entry["name"]


def test_the_language_selector_lists_english_first():
    assert AVAILABLE_LANGUAGES[0]["locale"] == DEFAULT_LOCALE


@pytest.mark.parametrize("key", _JS_KEYS)
def test_every_javascript_key_exists_in_english(key):
    # A typo in _JS_KEYS would ship the raw key name into the browser UI.
    assert key in TRANSLATIONS[DEFAULT_LOCALE]


@pytest.mark.parametrize("locale", sorted(SUPPORTED_LOCALES))
def test_the_javascript_subset_is_precomputed_for_every_locale(locale):
    js = get_js_translations(locale)
    # Dots become underscores so the browser can access them as properties.
    assert set(js) == {key.replace(".", "_") for key in _JS_KEYS}
    assert all(js.values())


def test_the_javascript_subset_falls_back_to_english_for_an_unknown_locale():
    assert get_js_translations("zz_ZZ") == get_js_translations(DEFAULT_LOCALE)


@pytest.mark.parametrize("locale", NON_DEFAULT_LOCALES)
def test_placeholders_match_english_in_every_translation(locale):
    """A translated string must use the same {placeholders} as the English one.

    ``t()`` calls ``str.format(**kwargs)``, so a translator who renames or
    invents a placeholder turns that string into a KeyError at runtime.
    """
    import string

    def placeholders(text: str) -> set[str]:
        return {
            field
            for _lit, field, _spec, _conv in string.Formatter().parse(text)
            if field
        }

    english = TRANSLATIONS[DEFAULT_LOCALE]
    for key, text in TRANSLATIONS[locale].items():
        assert placeholders(text) == placeholders(english[key]), (
            f"{locale}:{key} placeholder mismatch"
        )


# ---------------------------------------------------------------------------
# Translation loading
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_loader(monkeypatch, tmp_path):
    """Run _load_translations() against a temporary languages directory.

    The loader writes to module-level globals, so they are snapshotted and
    restored around each case.
    """
    import src.i18n as i18n

    saved = (i18n.TRANSLATIONS, i18n.SUPPORTED_LOCALES, i18n.AVAILABLE_LANGUAGES)
    monkeypatch.setattr(i18n, "_LANGUAGES_DIR", tmp_path)
    try:
        yield tmp_path
    finally:
        (
            i18n.TRANSLATIONS,
            i18n.SUPPORTED_LOCALES,
            i18n.AVAILABLE_LANGUAGES,
        ) = saved


def write_locale(directory, locale: str, data: dict) -> None:
    (directory / f"{locale}.yml").write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
    )


def test_a_missing_languages_directory_is_reported_not_raised(
    monkeypatch, tmp_path, caplog
):
    import src.i18n as i18n

    saved = (i18n.TRANSLATIONS, i18n.SUPPORTED_LOCALES, i18n.AVAILABLE_LANGUAGES)
    monkeypatch.setattr(i18n, "_LANGUAGES_DIR", tmp_path / "gone")
    try:
        with caplog.at_level("ERROR", logger="i18n"):
            i18n._load_translations()
        assert "Languages directory not found" in caplog.text
    finally:
        (
            i18n.TRANSLATIONS,
            i18n.SUPPORTED_LOCALES,
            i18n.AVAILABLE_LANGUAGES,
        ) = saved


def test_a_missing_reference_language_is_reported(isolated_loader, caplog):
    import src.i18n as i18n

    write_locale(isolated_loader, "de_DE", {"a": "b"})
    with caplog.at_level("ERROR", logger="i18n"):
        i18n._load_translations()
    assert "Reference language file not found" in caplog.text


def test_missing_keys_are_backfilled_from_english(isolated_loader, caplog):
    import src.i18n as i18n

    write_locale(
        isolated_loader,
        DEFAULT_LOCALE,
        {"_code": "EN", "_name": "English", "a": {"b": "English B", "c": "English C"}},
    )
    write_locale(
        isolated_loader,
        "de_DE",
        {"_code": "DE", "_name": "Deutsch", "a": {"b": "Deutsch B"}},
    )

    with caplog.at_level("WARNING", logger="i18n"):
        i18n._load_translations()

    assert i18n.TRANSLATIONS["de_DE"]["a.b"] == "Deutsch B"
    # The untranslated key falls back to English rather than disappearing.
    assert i18n.TRANSLATIONS["de_DE"]["a.c"] == "English C"
    assert "missing 1 key(s), backfilling" in caplog.text


def test_a_locale_file_that_is_not_a_mapping_is_skipped(isolated_loader, caplog):
    import src.i18n as i18n

    write_locale(isolated_loader, DEFAULT_LOCALE, {"a": "b"})
    (isolated_loader / "de_DE.yml").write_text(
        "- just\n- a\n- list\n", encoding="utf-8"
    )

    with caplog.at_level("WARNING", logger="i18n"):
        i18n._load_translations()

    assert "de_DE" not in i18n.SUPPORTED_LOCALES
    assert "expected a YAML mapping" in caplog.text


def test_an_unparseable_locale_file_is_skipped(isolated_loader, caplog):
    import src.i18n as i18n

    write_locale(isolated_loader, DEFAULT_LOCALE, {"a": "b"})
    (isolated_loader / "fr_FR.yml").write_text("a: [unclosed\n", encoding="utf-8")

    with caplog.at_level("ERROR", logger="i18n"):
        i18n._load_translations()

    assert "fr_FR" not in i18n.SUPPORTED_LOCALES
    assert "Failed to load translation file" in caplog.text


def test_ui_metadata_falls_back_to_the_locale_when_not_declared(isolated_loader):
    import src.i18n as i18n

    write_locale(isolated_loader, DEFAULT_LOCALE, {"a": "b"})
    write_locale(isolated_loader, "de_DE", {"a": "c"})

    i18n._load_translations()

    german = next(
        lang for lang in i18n.AVAILABLE_LANGUAGES if lang["locale"] == "de_DE"
    )
    assert german["code"] == "DE"
    assert german["name"] == "de_DE"
