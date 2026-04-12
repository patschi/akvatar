/**
 * logger.js - Client-side debug logger with configurable log levels.
 *
 * Log levels (ascending severity):
 *   DEBUG (0) - Verbose trace; user actions, DOM events, SSE frames
 *   INFO  (1) - Key lifecycle events; requests sent, responses received
 *   WARN  (2) - Non-fatal anomalies; fallbacks triggered, unexpected state
 *   ERROR (3) - Fatal or user-visible failures
 *
 * Default level: DEBUG (all messages are printed).
 * Override before loading this script by setting window.LOG_LEVEL, e.g.:
 *   <script>window.LOG_LEVEL = "INFO";</script>
 *
 * Runtime level adjustment from the browser console:
 *   logger.setLevel("WARN")   // silence DEBUG and INFO
 *   logger.setLevel("DEBUG")  // restore full verbosity
 */

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

        var prefix = "[" + level + "] [" + namespace + "]";
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
