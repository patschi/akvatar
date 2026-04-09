/**
 * Settings overlay – manages theme and language preferences.
 *
 * Theme preference is applied instantly via data-theme attribute.
 * Language preference requires a page reload so the server re-renders.
 * Theme "Auto" removes the cookie so the OS preference takes over.
 * Selecting a language always sets the cookie; the reset button clears both.
 */
(function () {
    "use strict";

    // Cookie helpers

    /** Read a cookie value by name, or null if not set. */
    function getCookie(cookieName) {
        var pattern = new RegExp("(?:^|; )" + cookieName + "=([^;]*)");
        var match   = document.cookie.match(pattern);
        return match ? match[1] : null;
    }

    /** Set a persistent cookie (400 days — maximum allowed by browsers). */
    function setCookie(cookieName, cookieValue) {
        var expiryDate = new Date(Date.now() + 400 * 86400000).toUTCString();
        document.cookie = cookieName + "=" + cookieValue
            + ";expires=" + expiryDate
            + ";path=/;SameSite=Lax";
    }

    /** Delete a cookie by setting its expiry date to the past. */
    function deleteCookie(cookieName) {
        document.cookie = cookieName + "=;expires=Thu, 01 Jan 1970 00:00:00 GMT;path=/;SameSite=Lax";
    }

    // Theme management

    /**
     * Resolve a theme preference to a concrete value ("light" or "dark").
     * If the preference is "auto" (or anything else), follow the OS setting.
     */
    function resolveTheme(preference) {
        if (preference === "light" || preference === "dark") {
            return preference;
        }
        return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }

    /**
     * Apply a theme preference to the page.
     * Sets the data-theme attribute on <html> and updates the color-scheme meta tag
     * so native browser elements (scrollbars, form controls) match.
     */
    function applyTheme(preference) {
        var resolvedTheme = resolveTheme(preference);
        document.documentElement.setAttribute("data-theme", resolvedTheme);

        var colorSchemeMeta = document.querySelector('meta[name="color-scheme"]');
        if (colorSchemeMeta) {
            colorSchemeMeta.setAttribute("content", resolvedTheme);
        }
    }

    // When the OS theme changes, re-apply if user hasn't set an explicit preference
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
        var savedTheme = getCookie("theme");
        var hasExplicitTheme = (savedTheme === "light" || savedTheme === "dark");
        if (!hasExplicitTheme) {
            applyTheme("auto");
        }
    });

    // Overlay open/close

    var settingsOverlay = document.getElementById("settingsOverlay");
    var settingsOpenButtons = document.querySelectorAll(".settings-btn");

    // Bail out if the overlay or buttons aren't present on this page
    if (!settingsOverlay || !settingsOpenButtons.length) return;

    var settingsPanel = settingsOverlay.querySelector(".dialog-panel");

    function openSettingsOverlay() {
        settingsOverlay.classList.remove("hidden");
    }

    function closeSettingsOverlay() {
        settingsOverlay.classList.add("hidden");
    }

    // Open when any settings cog button is clicked
    settingsOpenButtons.forEach(function (button) {
        button.addEventListener("click", openSettingsOverlay);
    });

    // Close when clicking the backdrop area (outside the panel)
    settingsOverlay.addEventListener("click", function (event) {
        if (!settingsPanel.contains(event.target)) {
            closeSettingsOverlay();
        }
    });

    // Close when clicking the explicit X button
    var settingsCloseButton = settingsOverlay.querySelector(".dialog-close");
    if (settingsCloseButton) {
        settingsCloseButton.addEventListener("click", closeSettingsOverlay);
    }

    // Close on Escape key
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && !settingsOverlay.classList.contains("hidden")) {
            closeSettingsOverlay();
        }
    });

    // Button state helpers

    var themeButtons  = settingsOverlay.querySelectorAll("[data-theme-value]");
    var localeButtons = settingsOverlay.querySelectorAll("[data-locale]");

    /** Highlight the active theme button matching the given value. */
    function highlightActiveTheme(activeValue) {
        themeButtons.forEach(function (button) {
            var isActive = (button.dataset.themeValue === activeValue);
            button.classList.toggle("active", isActive);
        });
    }

    /** Highlight the active locale button matching the current page language. */
    function highlightActiveLocale() {
        var currentLanguage = document.documentElement.lang;
        localeButtons.forEach(function (button) {
            var isActive = button.dataset.locale.startsWith(currentLanguage);
            button.classList.toggle("active", isActive);
        });
    }

    // Set initial active states

    // Theme: if no cookie is set, "auto" is the default
    var savedThemeCookie = getCookie("theme");
    highlightActiveTheme(savedThemeCookie || "auto");

    // Locale: highlight whichever language the server rendered
    highlightActiveLocale();

    // Theme button click handlers (instant, no page reload needed)

    themeButtons.forEach(function (button) {
        button.addEventListener("click", function () {
            var selectedTheme = button.dataset.themeValue;

            highlightActiveTheme(selectedTheme);

            // "Auto" removes the cookie so the OS preference takes over
            if (selectedTheme === "auto") {
                deleteCookie("theme");
            } else {
                setCookie("theme", selectedTheme);
            }

            applyTheme(selectedTheme);
        });
    });

    // Locale button click handlers (requires reload for server-rendered locale)

    localeButtons.forEach(function (button) {
        button.addEventListener("click", function () {
            setCookie("locale", button.dataset.locale);
            location.reload();
        });
    });

    // Reset settings button – removes all preference cookies and reloads with defaults

    var resetButton = document.getElementById("settingsReset");
    if (resetButton) {
        resetButton.addEventListener("click", function () {
            deleteCookie("theme");
            deleteCookie("locale");
            location.reload();
        });
    }
})();
