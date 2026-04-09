"""
i18n.py – Internationalisation support.

Provides translations for all user-facing strings in English (en_US) and German (de_DE).
The locale is resolved from the OIDC ``locale`` claim at login time, falling back to the
browser's ``Accept-Language`` header for unauthenticated pages.
"""

import logging

from flask import g, session, request

log = logging.getLogger('i18n')

SUPPORTED_LOCALES = frozenset(('en_US', 'de_DE'))
DEFAULT_LOCALE = 'en_US'

# Translation strings
TRANSLATIONS: dict[str, dict[str, str]] = {
    'en_US': {
        # -- Page titles (appended after brand name) --
        'title_signin':             'Sign in',
        'title_dashboard':          'Dashboard',
        'title_logged_out':         'Logged out',

        # -- Navbar --
        'nav_logout':               'Log out',

        # -- Settings UI --
        'settings_title':           'Settings',
        'settings_theme':           'Theme',
        'settings_light':           'Light',
        'settings_dark':            'Dark',
        'settings_auto':            'Auto',
        'settings_language':        'Language',
        'settings_reset':           'Reset settings',

        # -- Login page --
        'login_heading':            'Update your avatar',
        'login_subtitle':           'Sign in with your organisation account to upload and manage your profile picture.',
        'login_button':             'Sign in',
        'login_error_oidc_failed':  'Authentication failed. Please try again or contact your administrator.',
        'login_error_pk_failed':    'Login could not be completed — unable to retrieve your account. Please try again or contact your administrator.',

        # -- Logged-out page --
        'logout_heading':           'Logout successful',
        'logout_subtitle':          'You have been signed out. See you next time!',
        'logout_button':            'Back to login',

        # -- Dashboard --
        'upload_heading':           'Update your profile picture',
        'upload_subtitle':          'Select an image, crop it to a square, and hit Upload. Your avatar will be updated across our systems automatically.',
        'upload_drop_hint':         'Drag & drop an image here, or',
        'upload_choose':            'Choose image\u2026',
        'upload_disclaimer':        'By uploading, you confirm this is an appropriate image and that you have the right to use it.',
        'upload_button':            'Upload & Update Avatar',
        'upload_processing':        'Processing\u2026',
        'progress_heading':         'Progress',

        # -- Import dialog --
        'import_or':                    'or import from',
        'import_title':                 'Import image',
        'import_gravatar':              'Gravatar',
        'import_gravatar_placeholder':  'Email address',
        'import_gravatar_loading':      'Loading\u2026',
        'import_gravatar_not_found':    'No Gravatar found for this email address.',
        'import_gravatar_error':        'Could not fetch Gravatar. Please try again.',
        'import_url_placeholder':       'https://example.com/photo.jpg',
        'import_url_loading':           'Fetching\u2026',
        'import_url_error':             'Could not fetch the image. Please check the URL and try again.',
        'import_url_invalid':           'Please enter a valid URL starting with http:// or https://.',
        'import_fetch_failed':          'Could not fetch the image. Please try again.',
        'import_url_not_allowed':       'This URL is not allowed.',
        'import_load':                  'Load',
        'import_ok':                    'Use image',
        'import_cancel':                'Cancel',

        # -- Client-side validation --
        'upload_invalid_ext':       'File type .{ext} is not allowed. Accepted: {allowed}',

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
        'step_ldap_updated':        'User Directory Photo updated',
        'step_rollback':            'Changes rolled back',
        'step_processing_failed':   'Processing the image failed',
        'step_save_failed':         'Could not save your avatar.',

        # -- Result messages --
        'result_success':           'Avatar updated successfully!',
        'result_retry':             'Change avatar again',
        'result_error':             'Could not update your avatar. Please try again later.',
        'result_csrf_failed':       'Your session has expired. Please reload the page and try again.',
        'result_contact_admin':     'Please contact your administrator.',
        'result_network_error':     'Could not reach the server. Please check your connection and try again.',
    },

    'de_DE': {
        # -- Page titles --
        'title_signin':             'Anmelden',
        'title_dashboard':          'Dashboard',
        'title_logged_out':         'Abgemeldet',

        # -- Navbar --
        'nav_logout':               'Abmelden',

        # -- Settings UI --
        'settings_title':           'Einstellungen',
        'settings_theme':           'Design',
        'settings_light':           'Hell',
        'settings_dark':            'Dunkel',
        'settings_auto':            'Auto',
        'settings_language':        'Sprache',
        'settings_reset':           'Einstellungen zurücksetzen',

        # -- Login page --
        'login_heading':            'Avatar aktualisieren',
        'login_subtitle':           'Melden Sie sich mit Ihrem Organisationskonto an, um Ihr Profilbild hochzuladen und zu verwalten.',
        'login_button':             'Anmelden',
        'login_error_oidc_failed':  'Authentifizierung fehlgeschlagen. Bitte versuchen Sie es erneut oder kontaktieren Sie Ihren Administrator.',
        'login_error_pk_failed':    'Anmeldung konnte nicht abgeschlossen werden \u2013 Ihr Konto konnte nicht abgerufen werden. Bitte versuchen Sie es erneut oder kontaktieren Sie Ihren Administrator.',

        # -- Logged-out page --
        'logout_heading':           'Abmeldung erfolgreich',
        'logout_subtitle':          'Sie wurden abgemeldet. Bis zum n\u00e4chsten Mal!',
        'logout_button':            'Zur\u00fcck zur Anmeldung',

        # -- Dashboard --
        'upload_heading':           'Profilbild aktualisieren',
        'upload_subtitle':          'W\u00e4hlen Sie ein Bild aus, schneiden Sie es quadratisch zu und klicken Sie auf Hochladen. Ihr Avatar wird automatisch in unseren Systemen aktualisiert.',
        'upload_drop_hint':         'Bild hierher ziehen, oder',
        'upload_choose':            'Bild ausw\u00e4hlen\u2026',
        'upload_disclaimer':        'Mit dem Hochladen best\u00e4tigen Sie, dass es sich um ein angemessenes Bild handelt und Sie berechtigt sind, es zu verwenden.',
        'upload_button':            'Hochladen & Avatar aktualisieren',
        'upload_processing':        'Verarbeitung\u2026',
        'progress_heading':         'Fortschritt',

        # -- Import dialog --
        'import_or':                    'oder importieren von',
        'import_title':                 'Bild importieren',
        'import_gravatar':              'Gravatar',
        'import_gravatar_placeholder':  'E-Mail-Adresse',
        'import_gravatar_loading':      'Wird geladen\u2026',
        'import_gravatar_not_found':    'Kein Gravatar f\u00fcr diese E-Mail-Adresse gefunden.',
        'import_gravatar_error':        'Gravatar konnte nicht abgerufen werden. Bitte versuchen Sie es erneut.',
        'import_url_placeholder':       'https://beispiel.de/foto.jpg',
        'import_url_loading':           'Wird abgerufen\u2026',
        'import_url_error':             'Das Bild konnte nicht abgerufen werden. Bitte \u00fcberpr\u00fcfen Sie die URL und versuchen Sie es erneut.',
        'import_url_invalid':           'Bitte geben Sie eine g\u00fcltige URL ein, die mit http:// oder https:// beginnt.',
        'import_fetch_failed':          'Das Bild konnte nicht abgerufen werden. Bitte versuchen Sie es erneut.',
        'import_url_not_allowed':       'Diese URL ist nicht erlaubt.',
        'import_load':                  'Laden',
        'import_ok':                    'Bild verwenden',
        'import_cancel':                'Abbrechen',

        # -- Client-side validation --
        'upload_invalid_ext':       'Dateityp .{ext} ist nicht erlaubt. Erlaubt: {allowed}',

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
        'step_ldap_updated':        'Benutzerverzeichnis Foto aktualisiert',
        'step_rollback':            '\u00c4nderungen r\u00fcckg\u00e4ngig gemacht',
        'step_processing_failed':   'Verarbeitung des Bildes fehlgeschlagen',
        'step_save_failed':         'Avatar konnte nicht gespeichert werden.',

        # -- Result messages --
        'result_success':           'Avatar erfolgreich aktualisiert!',
        'result_retry':             'Avatar erneut \u00e4ndern',
        'result_error':             'Avatar konnte nicht aktualisiert werden. Bitte versuchen Sie es sp\u00e4ter erneut.',
        'result_csrf_failed':       'Ihre Sitzung ist abgelaufen. Bitte laden Sie die Seite neu und versuchen Sie es erneut.',
        'result_contact_admin':     'Bitte kontaktieren Sie Ihren Administrator.',
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


def _resolve_locale() -> str:
    """Determine the best locale for the current request without caching."""
    # 1. Cookie override (user preference set via settings UI)
    cookie_locale = request.cookies.get('locale', '') if request else ''
    if cookie_locale in SUPPORTED_LOCALES:
        return cookie_locale

    # 2. Session locale (set during OIDC callback)
    loc = session.get('locale')
    if loc and loc in SUPPORTED_LOCALES:
        return loc

    # 3. Accept-Language header (for unauthenticated pages)
    accept = request.headers.get('Accept-Language', '') if request else ''
    for part in accept.split(','):
        matched = _normalize(part.split(';')[0].strip())
        if matched:
            return matched

    return DEFAULT_LOCALE


def get_locale() -> str:
    """Return the active locale for the current request, cached in flask.g for the duration of the request."""
    # Serve from request-scoped cache to avoid repeated cookie/session/header reads.
    # g is only available inside a request context; RuntimeError is caught for startup calls.
    try:
        cached = getattr(g, '_locale', None)
        if cached is not None:
            return cached
        result = _resolve_locale()
        g._locale = result
        return result
    except RuntimeError:
        return _resolve_locale()


def t(key: str, **kwargs) -> str:
    """Translate *key* into the current request's locale, with optional format arguments."""
    locale = get_locale()
    text = TRANSLATIONS.get(locale, TRANSLATIONS[DEFAULT_LOCALE]).get(key, key)
    if kwargs:
        text = text.format(**kwargs)
    return text


_JS_KEYS = (
    # Client-side validation
    'upload_invalid_ext',
    # Client-side step labels
    'step_crop', 'step_compress', 'step_upload',
    # Server-side step labels (needed to pre-render waiting steps before SSE arrives)
    'step_validated', 'step_filename', 'step_processed',
    'step_profile_synced', 'step_ldap_updated', 'step_rollback',
    # UI strings
    'upload_processing', 'upload_button',
    'step_save_failed',
    'result_success', 'result_retry', 'result_error', 'result_csrf_failed',
    'result_contact_admin', 'result_network_error',
    # Import dialog (loading states and error messages used in JS)
    'import_load', 'import_gravatar_loading',
    'import_gravatar_not_found', 'import_gravatar_error',
    'import_url_loading',
    'import_url_error', 'import_url_invalid',
    'import_fetch_failed', 'import_url_not_allowed',
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
