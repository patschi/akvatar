"""
app_middleware.py – WSGI and Jinja2 middleware.

PrefixMiddleware:
    Sets SCRIPT_NAME to a static path prefix so the app can be hosted under
    a subfolder without a reverse proxy setting X-Forwarded-Prefix.

MinifyingTemplateLoader:
    Wraps Flask's Jinja2 template loader to strip HTML comments and collapse
    excess blank lines from template source before compilation.  Runs once per
    template (Jinja2 caches compiled bytecode), so there is no per-request
    overhead.  Template files on disk are left untouched.
"""

import re

from jinja2 import BaseLoader


class PrefixMiddleware:
    """
    WSGI middleware that sets SCRIPT_NAME to a static path prefix so the app
    can be hosted under a subfolder without a reverse proxy setting X-Forwarded-Prefix.

    ProxyFix is applied as the outer middleware (runs before this one), so when a
    reverse proxy *does* set X-Forwarded-Prefix, ProxyFix has already populated
    SCRIPT_NAME and this middleware is a no-op.
    """
    def __init__(self, wsgi_app, prefix):
        self.wsgi_app = wsgi_app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        # Skip if ProxyFix already set SCRIPT_NAME from X-Forwarded-Prefix.
        # Applying the prefix twice would generate double-prefixed URLs (e.g.
        # /avatar-update/avatar-update/callback), causing redirect_uri mismatches.
        if not environ.get('SCRIPT_NAME'):
            environ['SCRIPT_NAME'] = self.prefix
            path_info = environ.get('PATH_INFO', '')
            if path_info.startswith(self.prefix):
                environ['PATH_INFO'] = path_info[len(self.prefix):]
        return self.wsgi_app(environ, start_response)


class MinifyingTemplateLoader(BaseLoader):
    """
    Wraps Flask's Jinja2 template loader to strip HTML comments and collapse
    excess blank lines from template source before compilation.

    Runs once per template (Jinja2 caches compiled bytecode), so there is no
    per-request overhead.  Template files on disk are left untouched.
    """
    _HTML_COMMENT = re.compile(r'<!--.*?-->', re.DOTALL)
    # Matches lines that contain only spaces or tabs (not newlines).
    # re.MULTILINE makes ^ / $ anchor to line boundaries, not string boundaries.
    # \s is intentionally avoided here because it matches \n, which would cause
    # the pattern to span across multiple blank lines.
    _WHITESPACE_LINE = re.compile(r'^[ \t]+$', re.MULTILINE)
    _BLANK_LINES  = re.compile(r'\n{3,}')

    def __init__(self, loader: BaseLoader) -> None:
        self._loader = loader

    def get_source(self, environment, template):
        source, filename, uptodate = self._loader.get_source(environment, template)
        # Strip HTML comments and collapse excess blank lines to reduce response size.
        source = self._HTML_COMMENT.sub('', source)
        # Erase whitespace-only lines so they are treated as blank by step 4
        source = self._WHITESPACE_LINE.sub('', source)
        # Replace 3 or more consecutive newlines with just 2 (preserving intentional blank lines).
        source = self._BLANK_LINES.sub('\n\n', source)
        return source, filename, uptodate
