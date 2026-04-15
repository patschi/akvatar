/**
 * common-utils.js - Shared utilities loaded on every page.
 *
 * Bundled modules:
 *   1. Logger   - Client-side debug logger with configurable log levels
 *   2. Dialog   - Shared dialog controller factory
 *   3. Tooltip  - Instant CSS-only tooltips (title → data-tooltip conversion)
 */


/* ====================================================================
 *  LOGGER
 *  Client-side debug logger with configurable log levels.
 *
 *  Log levels (ascending severity):
 *    DEBUG (0) - Verbose trace; user actions, DOM events, SSE frames
 *    INFO  (1) - Key lifecycle events; requests sent, responses received
 *    WARN  (2) - Non-fatal anomalies; fallbacks triggered, unexpected state
 *    ERROR (3) - Fatal or user-visible failures
 *
 *  Default level: DEBUG (all messages are printed).
 *  Override before loading this script by setting window.LOG_LEVEL, e.g.:
 *    <script>window.LOG_LEVEL = "INFO";</script>
 *
 *  Runtime level adjustment from the browser console:
 *    logger.setLevel("WARN")   // silence DEBUG and INFO
 *    logger.setLevel("DEBUG")  // restore full verbosity
 * ==================================================================== */

// logger singleton - exposed as a global so all page scripts can access it
var logger = (function () {
    "use strict";

    // Numeric severity values - lower means more verbose
    var LEVELS = { DEBUG: 0, INFO: 1, WARN: 2, ERROR: 3 };

    // Read the initial level from window.LOG_LEVEL, defaulting to DEBUG
    var _levelName = ((window.LOG_LEVEL || "DEBUG") + "").toUpperCase();
    var _currentLevel = LEVELS.hasOwnProperty(_levelName) ? LEVELS[_levelName] : LEVELS.DEBUG;

    // CSS color/weight styles applied via %c for each level
    var _styles = {
        DEBUG: "color: #888888; font-weight: normal;",
        INFO:  "color: #4a9eff; font-weight: bold;",
        WARN:  "color: #f5a623; font-weight: bold;",
        ERROR: "color: #e74c3c; font-weight: bold;",
    };

    /**
     * Emit a log entry to the browser console if the level meets the threshold.
     * @param {string} level     - One of: DEBUG, INFO, WARN, ERROR
     * @param {string} namespace - Short module label shown in the prefix (e.g. "upload")
     * @param {string} message   - Human-readable description of the event
     * @param {*}      [data]    - Optional structured payload (object, array, primitive)
     */
    function _emit(level, namespace, message, data) {
        // Skip messages below the active threshold
        if (LEVELS[level] < _currentLevel) return;

        // UTC timestamp with Z notation (ISO-8601), e.g. 2026-04-15T14:30:00.000Z
        var ts = new Date().toISOString();
        var prefix = "[" + ts + "] [" + level + "] [" + namespace + "]";
        var style  = _styles[level] || _styles.DEBUG;

        if (data !== undefined) {
            console.log("%c" + prefix, style, message, data);
        } else {
            console.log("%c" + prefix, style, message);
        }
    }

    return {
        /** Log at DEBUG level - verbose trace for actions and events. */
        debug: function (namespace, message, data) { _emit("DEBUG", namespace, message, data); },

        /** Log at INFO level - key lifecycle events like requests and responses. */
        info:  function (namespace, message, data) { _emit("INFO",  namespace, message, data); },

        /** Log at WARN level - non-fatal anomalies and fallback paths taken. */
        warn:  function (namespace, message, data) { _emit("WARN",  namespace, message, data); },

        /** Log at ERROR level - fatal failures and user-visible errors. */
        error: function (namespace, message, data) { _emit("ERROR", namespace, message, data); },

        /**
         * Change the active log level at runtime.
         * Call from the browser console to tune verbosity without a page reload.
         * @param {string} levelName - DEBUG | INFO | WARN | ERROR (case-insensitive)
         */
        setLevel: function (levelName) {
            var resolved = LEVELS[((levelName || "") + "").toUpperCase()];
            if (resolved !== undefined) {
                _currentLevel = resolved;
            }
        },

        /** Return the name of the currently active log level (e.g. "DEBUG"). */
        getLevel: function () {
            for (var name in LEVELS) {
                if (LEVELS.hasOwnProperty(name) && LEVELS[name] === _currentLevel) {
                    return name;
                }
            }
            return "DEBUG";
        },

        /** Exported level constants for external comparisons. */
        LEVELS: LEVELS,
    };
})();


/* ====================================================================
 *  DIALOG
 *  Shared dialog controller factory.
 *
 *  createDialog() wires up the standard behaviors for a .dialog-overlay:
 *    - Backdrop click (outside .dialog-panel) closes the dialog
 *    - .dialog-close button click closes the dialog
 *    - Escape key closes the dialog (only while the overlay is visible)
 *
 *  Returns { open, close } so callers can trigger open/close and attach
 *  extra logic via the onOpen / onClose callbacks.
 * ==================================================================== */

(function () {
    "use strict";

    /**
     * Create a dialog controller for a .dialog-overlay element.
     *
     * @param {string}   overlayId         - ID of the .dialog-overlay element
     * @param {Object}   [options]
     * @param {Function} [options.onOpen]  - Called after the overlay becomes visible
     * @param {Function} [options.onClose] - Called after the overlay is hidden
     * @returns {{ open: Function, close: Function } | null} Controller, or null if the overlay is not found
     */
    window.createDialog = function createDialog(overlayId, options) {
        var overlay = document.getElementById(overlayId);
        if (!overlay) return null;

        var opts  = options || {};
        var panel = overlay.querySelector(".dialog-panel");

        // Open: show the overlay, then fire the optional callback
        function open() {
            overlay.classList.remove("hidden");
            if (opts.onOpen) opts.onOpen();
        }

        // Close: hide the overlay, then fire the optional callback
        function close() {
            overlay.classList.add("hidden");
            if (opts.onClose) opts.onClose();
        }

        // Backdrop click (outside the panel) closes the dialog
        overlay.addEventListener("click", function (event) {
            if (!panel.contains(event.target)) {
                close();
            }
        });

        // .dialog-close button click closes the dialog
        var closeBtn = overlay.querySelector(".dialog-close");
        if (closeBtn) {
            closeBtn.addEventListener("click", close);
        }

        // Escape key closes the dialog (only while visible)
        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && !overlay.classList.contains("hidden")) {
                close();
            }
        });

        return { open: open, close: close };
    };
})();


/* ====================================================================
 *  TOOLTIP
 *  Converts native title attributes to data-tooltip so CSS-only
 *  tooltips can show instantly (bypassing the browser's built-in delay).
 * ==================================================================== */

(function () {
    function convertTitles(root) {
        root.querySelectorAll("[title]").forEach(function (el) {
            var text = el.getAttribute("title");
            if (text) {
                el.setAttribute("data-tooltip", text);
                el.removeAttribute("title");
            }
        });
    }

    // Convert existing elements once the DOM is ready
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () { convertTitles(document); });
    } else {
        convertTitles(document);
    }

    // Watch for dynamically added elements with title attributes
    new MutationObserver(function (mutations) {
        mutations.forEach(function (m) {
            m.addedNodes.forEach(function (node) {
                if (node.nodeType === 1) convertTitles(node);
            });
        });
    }).observe(document.documentElement, { childList: true, subtree: true });
})();

