/**
 * dashboard-session-check.js – Periodically probe /api/heartbeat and redirect
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
 * Each successful probe refreshes the session cookie (Flask's default
 * SESSION_REFRESH_EACH_REQUEST behaviour), keeping the session alive while the
 * tab is visible. When the tab moves to the background the interval is paused
 * so the session is no longer actively kept alive. When the tab returns to
 * the foreground an immediate probe is fired (to catch any expiry that happened
 * while checks were paused) and then normal polling resumes.
 *
 * Depends on server-provided constants injected inline by the template
 * before this script is loaded:
 *   SESSION_CHECK_ENDPOINT, LOGIN_URL
 */

// How often to probe the server (milliseconds). 60 s balances freshness with overhead.
var SESSION_CHECK_INTERVAL_MS = 60000;

// Active interval handle, or null when paused.
var _sessionCheckTimer = null;

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

/**
 * Start the polling interval.
 * If checkNow is true, an immediate probe is fired before the first interval tick.
 * Safe to call when already running (no-op).
 */
function startSessionCheck(checkNow) {
    if (_sessionCheckTimer !== null) {
        return;
    }
    if (checkNow) {
        // Tab just became visible – probe immediately to detect any expiry that
        // occurred while checks were paused, then continue with the normal cadence.
        checkSession();
    }
    _sessionCheckTimer = setInterval(checkSession, SESSION_CHECK_INTERVAL_MS);
}

/**
 * Pause the polling interval.
 * Safe to call when already paused (no-op).
 */
function stopSessionCheck() {
    if (_sessionCheckTimer === null) {
        return;
    }
    clearInterval(_sessionCheckTimer);
    _sessionCheckTimer = null;
}

/** React to the tab becoming visible or hidden. */
function onVisibilityChange() {
    if (document.visibilityState === "hidden") {
        // Tab went to background – stop keeping the session alive.
        stopSessionCheck();
    } else {
        // Tab returned to foreground – check immediately, then resume normal cadence.
        startSessionCheck(true);
    }
}

// Only activate session checks when the user is logged in.
// SESSION_CHECK_ENDPOINT and LOGIN_URL are injected by the dashboard template's
// inline script block, so their presence confirms an authenticated page context.
// If those constants are absent (e.g. script accidentally loaded on login/logged-out
// page), do nothing.
if (typeof SESSION_CHECK_ENDPOINT !== "undefined" && typeof LOGIN_URL !== "undefined") {
    document.addEventListener("visibilitychange", onVisibilityChange);

    // Start polling on page load if the tab is already visible.
    // No immediate probe on load – the server already validated the session to render the page.
    if (document.visibilityState !== "hidden") {
        startSessionCheck(false);
    }
}
