/**
 * dashboard-session-check.js – Periodically probe /api/session and redirect
 * to the login page with a session-expired notice if the server-side session
 * has expired.
 *
 * This prevents users from filling in the upload form only to discover their
 * session is gone at submit time.
 *
 * A 401 response means the session is definitively expired – redirect immediately.
 * Network errors are treated as transient and silently ignored so a momentary
 * connectivity blip does not log the user out.
 *
 * As a side-effect, each successful probe refreshes the session cookie
 * (Flask's default SESSION_REFRESH_EACH_REQUEST behaviour), so the session
 * stays alive as long as the dashboard is open in the browser.
 *
 * Depends on server-provided constants injected inline by the template
 * before this script is loaded:
 *   SESSION_CHECK_ENDPOINT, LOGIN_URL
 */

// How often to probe the server (milliseconds). 60 s balances freshness with overhead.
var SESSION_CHECK_INTERVAL_MS = 60000;

/** Redirect to the login page with the session_expired error key. */
function redirectSessionExpired() {
    window.location.href = LOGIN_URL + "?error=session_expired";
}

/**
 * Probe the session endpoint once.
 * Redirects on 401 (expired); ignores 5xx and network failures (transient).
 */
function checkSession() {
    fetch(SESSION_CHECK_ENDPOINT, {
        method: "GET",
        credentials: "same-origin",
        // Bypass the HTTP cache so every tick hits the server and refreshes the cookie.
        cache: "no-store",
    }).then(function (response) {
        if (response.status === 401) {
            redirectSessionExpired();
        }
        // 200 = alive; anything else (5xx, etc.) = server trouble, retry next tick
    }).catch(function () {
        // Network error – transient failure, do not redirect
    });
}

// Start polling after the first interval so a page-load probe isn't wasted
// (the server already validated the session to render the dashboard).
setInterval(checkSession, SESSION_CHECK_INTERVAL_MS);
