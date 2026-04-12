/**
 * dialog.js - Shared dialog controller factory.
 *
 * createDialog() wires up the standard behaviors for a .dialog-overlay element:
 *   - Backdrop click (outside .dialog-panel) closes the dialog
 *   - .dialog-close button click closes the dialog
 *   - Escape key closes the dialog (only while the overlay is visible)
 *
 * Returns { open, close } so callers can trigger open/close and attach extra
 * logic via the onOpen / onClose callbacks.
 *
 * Must be loaded before any script that calls window.createDialog().
 */
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
