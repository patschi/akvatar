"""
i18n.py – Internationalisation support.

Provides translations for all user-facing strings in English (en_US) and German (de_DE).
The locale is resolved from the OIDC ``locale`` claim at login time, falling back to the
browser's ``Accept-Language`` header for unauthenticated pages.
"""

import logging

from flask import session, request

log = logging.getLogger('i18n')

SUPPORTED_LOCALES = frozenset(('en_US', 'de_DE'))
DEFAULT_LOCALE = 'en_US'

# ---------------------------------------------------------------------------
# Translation strings
# ---------------------------------------------------------------------------
TRANSLATIONS: dict[str, dict[str, str]] = {
    'en_US': {
        # -- Page titles (appended after brand name) --
        'title_signin':             'Sign in',
        'title_dashboard':          'Dashboard',
        'title_logged_out':         'Logged out',

        # -- Navbar --
        'nav_logout':               'Log out',

        # -- Login page --
        'login_heading':            'Update your avatar',
        'login_subtitle':           'Sign in with your organisation account to upload and manage your profile picture.',
        'login_button':             'Sign in',

        # -- Logged-out page --
        'logout_heading':           'Logout successful',
        'logout_subtitle':          'You have been signed out. See you next time!',
        'logout_button':            'Back to login',

        # -- Dashboard --
        'upload_heading':           'Update your profile picture',
        'upload_subtitle':          'Select an image, crop it to a square, and hit <strong>Upload</strong>. Your avatar will be updated across our systems automatically.',
        'upload_choose':            'Choose image\u2026',
        'upload_button':            'Upload & Update Avatar',
        'upload_processing':        'Processing\u2026',
        'progress_heading':         'Progress',

        # -- Progress steps (client-side) --
        'step_crop':                'Cropping image in browser',
        'step_compress':            'Compressing image in browser',
        'step_upload':              'Uploading to server',

        # -- Progress steps (server-side) --
        'step_validated':           'Image validated & loaded',
        'step_validated_detail':    'Image metadata removed',
        'step_filename':            'Filename generated',
        'step_processed':           'Image processed & saved in all sizes/formats',
        'step_processed_detail':    '{sizes} sizes, {formats} formats, {total} total',
        'step_profile_synced':      'Login Portal Photo updated',
        'step_ad_updated':          'User Directory Photo updated',
        'step_processing_failed':   'Processing',

        # -- Result messages --
        'result_success':           'Avatar updated successfully!',
        'result_retry':             'Change avatar again',
        'result_error':             'Could not update your avatar. Please try again later.',
        'result_network_error':     'Could not reach the server. Please check your connection and try again.',
    },

    'de_DE': {
        # -- Page titles --
        'title_signin':             'Anmelden',
        'title_dashboard':          'Dashboard',
        'title_logged_out':         'Abgemeldet',

        # -- Navbar --
        'nav_logout':               'Abmelden',

        # -- Login page --
        'login_heading':            'Avatar aktualisieren',
        'login_subtitle':           'Melden Sie sich mit Ihrem Organisationskonto an, um Ihr Profilbild hochzuladen und zu verwalten.',
        'login_button':             'Anmelden',

        # -- Logged-out page --
        'logout_heading':           'Abmeldung erfolgreich',
        'logout_subtitle':          'Sie wurden abgemeldet. Bis zum n\u00e4chsten Mal!',
        'logout_button':            'Zur\u00fcck zur Anmeldung',

        # -- Dashboard --
        'upload_heading':           'Profilbild aktualisieren',
        'upload_subtitle':          'W\u00e4hlen Sie ein Bild aus, schneiden Sie es quadratisch zu und klicken Sie auf <strong>Hochladen</strong>. Ihr Avatar wird automatisch in unseren Systemen aktualisiert.',
        'upload_choose':            'Bild ausw\u00e4hlen\u2026',
        'upload_button':            'Hochladen & Avatar aktualisieren',
        'upload_processing':        'Verarbeitung\u2026',
        'progress_heading':         'Fortschritt',

        # -- Progress steps (client-side) --
        'step_crop':                'Bild wird im Browser zugeschnitten',
        'step_compress':            'Bild wird im Browser komprimiert',
        'step_upload':              'Wird auf den Server hochgeladen',

        # -- Progress steps (server-side) --
        'step_validated':           'Bild validiert & geladen',
        'step_validated_detail':    'Bildmetadaten entfernt',
        'step_filename':            'Dateiname generiert',
        'step_processed':           'Bild in allen Gr\u00f6\u00dfen/Formaten verarbeitet & gespeichert',
        'step_processed_detail':    '{sizes} Gr\u00f6\u00dfen, {formats} Formate, {total} gesamt',
        'step_profile_synced':      'Anmelde-Portal Foto aktualisiert',
        'step_ad_updated':          'Benutzerverzeichnis Foto aktualisiert',
        'step_processing_failed':   'Verarbeitung',

        # -- Result messages --
        'result_success':           'Avatar erfolgreich aktualisiert!',
        'result_retry':             'Avatar erneut \u00e4ndern',
        'result_error':             'Avatar konnte nicht aktualisiert werden. Bitte versuchen Sie es sp\u00e4ter erneut.',
        'result_network_error':     'Server nicht erreichbar. Bitte \u00fcberpr\u00fcfen Sie Ihre Verbindung und versuchen Sie es erneut.',
    },
}

# Pre-compute a lookup from language prefix to full locale (e.g. 'en' -> 'en_US')
_LANG_PREFIX_MAP: dict[str, str] = {}
for _loc in SUPPORTED_LOCALES:
    _LANG_PREFIX_MAP[_loc.split('_')[0].lower()] = _loc


def _normalize(raw: str) -> str | None:
    """Try to match a raw locale string (e.g. 'en-US', 'de', 'en_US') to a supported locale."""
    tag = raw.strip().replace('-', '_')
    # Exact match
    if tag in SUPPORTED_LOCALES:
        return tag
    # Match by language prefix (e.g. 'en' -> 'en_US')
    prefix = tag.split('_')[0].lower()
    return _LANG_PREFIX_MAP.get(prefix)


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


def get_locale() -> str:
    """Return the active locale for the current request."""
    # 1. Session (set during OIDC callback)
    loc = session.get('locale')
    if loc and loc in SUPPORTED_LOCALES:
        return loc

    # 2. Accept-Language header (for unauthenticated pages)
    accept = request.headers.get('Accept-Language', '') if request else ''
    for part in accept.split(','):
        tag = part.split(';')[0].strip()
        matched = _normalize(tag)
        if matched:
            return matched

    return DEFAULT_LOCALE


def t(key: str, **kwargs) -> str:
    """Translate *key* into the current request's locale, with optional format arguments."""
    locale = get_locale()
    text = TRANSLATIONS.get(locale, TRANSLATIONS[DEFAULT_LOCALE]).get(key, key)
    if kwargs:
        text = text.format(**kwargs)
    return text


_JS_KEYS = (
    'step_crop', 'step_compress', 'step_upload',
    'upload_processing', 'upload_button',
    'result_success', 'result_retry', 'result_error', 'result_network_error',
)

# Pre-compute JS translation dicts per locale at startup (avoids rebuilding on every request)
_JS_TRANSLATIONS: dict[str, dict[str, str]] = {
    locale: {k: strings[k] for k in _JS_KEYS}
    for locale, strings in TRANSLATIONS.items()
}


def get_js_translations(locale: str | None = None) -> dict[str, str]:
    """Return the pre-computed subset of translations needed by client-side JavaScript."""
    if locale is None:
        locale = get_locale()
    return _JS_TRANSLATIONS.get(locale, _JS_TRANSLATIONS[DEFAULT_LOCALE])
