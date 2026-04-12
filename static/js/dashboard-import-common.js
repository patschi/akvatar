/**
 * dashboard-import-common.js - Shared infrastructure for the import dialog.
 *
 * Manages the dialog overlay: open/close, tab switching, preview/error
 * display, load-button cooldown, and the OK button that forwards the
 * selected image to initCropper().
 *
 * Exposes window.ImportDialog so per-method import scripts can call into it.
 * The webcam module registers cleanup hooks via ImportDialog.onDialogClose and
 * ImportDialog.onTabSwitch so the camera stream is stopped on tab switch or close.
 *
 * Depends on server-provided constants injected by the template:
 *   I18N, CSRF_TOKEN
 * Depends on initCropper() from dashboard-main.js (loaded before this script).
 */
(function () {
    "use strict";

    // Dialog overlay - backdrop click, .dialog-close button, and Escape key are
    // all wired by createDialog(); bail out early if the overlay is absent.
    var overlay = document.getElementById("importOverlay");
    if (!overlay) return;
    var importDialog = createDialog("importOverlay", {
        onClose: function () {
            // Notify the webcam module (if loaded) so it can stop the camera stream
            if (ImportDialog.onDialogClose) {
                ImportDialog.onDialogClose();
            }
            resetPreview();
        },
    });

    // Tab buttons inside the dialog (only present when multiple sources are enabled)
    var dialogTabBtns = overlay.querySelectorAll("[data-import-tab]");

    // Tab content panes (null when the source is disabled in config)
    var tabGravatar = document.getElementById("importTabGravatar");
    var tabUrl      = document.getElementById("importTabUrl");
    var tabWebcam   = document.getElementById("importTabWebcam");

    // Preview and error areas
    var previewArea = document.getElementById("importPreview");
    var previewImg  = document.getElementById("importPreviewImg");
    var errorArea   = document.getElementById("importError");

    // Footer buttons
    var cancelBtn = document.getElementById("importCancelBtn");
    var okBtn     = document.getElementById("importOkBtn");

    // Trigger buttons on the dashboard (open dialog with a specific tab)
    var triggerBtns = document.querySelectorAll(".import-triggers [data-import-tab]");

    // State: currently loaded preview blob URL and display name
    var currentBlobUrl     = null;
    var currentDisplayName = null;

    // Cooldown delay (in seconds) before a Load button becomes pressable again
    var LOAD_COOLDOWN_SECONDS = 3;

    // Map known server error codes to translated messages
    var errorMessages = {
        "csrf_failed":     I18N.result_csrf_failed,
        "fetch_failed":    I18N.import_fetch_failed,
        "image_too_large": I18N.import_image_too_large,
        "url_not_allowed": I18N.import_url_not_allowed,
    };

    // ── Dialog open / close ──────────────────────────────────────────

    function openDialog(tab) {
        // Default to whichever tab is actually available (gravatar -> url -> webcam)
        if (tab === "gravatar" && !tabGravatar) tab = tabUrl ? "url" : "webcam";
        if (tab === "url"      && !tabUrl)      tab = tabGravatar ? "gravatar" : "webcam";
        if (tab === "webcam"   && !tabWebcam)   tab = tabGravatar ? "gravatar" : "url";
        logger.info("import", "import dialog opened", { tab: tab || "gravatar" });
        switchTab(tab || "gravatar");
        resetPreview();
        importDialog.open();
    }

    function closeDialog() {
        logger.debug("import", "import dialog closed");
        // importDialog.close() hides the overlay then fires onClose (webcam cleanup + resetPreview)
        importDialog.close();
    }

    // ── Tab switching ────────────────────────────────────────────────

    function switchTab(tab) {
        logger.debug("import", "tab switched", { tab: tab });
        // Highlight the active tab button
        dialogTabBtns.forEach(function (btn) {
            btn.classList.toggle("active", btn.dataset.importTab === tab);
        });
        // Show/hide tab content panes
        if (tabGravatar) tabGravatar.classList.toggle("hidden", tab !== "gravatar");
        if (tabUrl)      tabUrl.classList.toggle("hidden",      tab !== "url");
        if (tabWebcam)   tabWebcam.classList.toggle("hidden",   tab !== "webcam");
        // Notify the webcam module (if loaded) to stop the stream when leaving the webcam tab
        if (ImportDialog.onTabSwitch) {
            ImportDialog.onTabSwitch(tab);
        }
        // Clear preview when switching tabs
        resetPreview();
    }

    dialogTabBtns.forEach(function (btn) {
        btn.addEventListener("click", function () {
            switchTab(btn.dataset.importTab);
        });
    });

    // ── Trigger buttons (on the dashboard) ───────────────────────────

    triggerBtns.forEach(function (btn) {
        btn.addEventListener("click", function () {
            openDialog(btn.dataset.importTab);
        });
    });

    // ── Close handlers ───────────────────────────────────────────────

    // Cancel button (X button, backdrop, and Escape key are wired by createDialog)
    cancelBtn.addEventListener("click", closeDialog);

    // ── Preview helpers ──────────────────────────────────────────────

    /** Clear the preview image and error state, revoke any blob URL. */
    function resetPreview() {
        if (currentBlobUrl) {
            URL.revokeObjectURL(currentBlobUrl);
            currentBlobUrl = null;
        }
        currentDisplayName = null;
        previewArea.classList.add("hidden");
        previewImg.src = "";
        errorArea.classList.add("hidden");
        errorArea.textContent = "";
        okBtn.disabled = true;
    }

    /** Display a successfully fetched image in the preview area. */
    function showPreview(blobUrl, displayName) {
        logger.debug("import", "preview shown", { displayName: displayName });
        if (currentBlobUrl) {
            URL.revokeObjectURL(currentBlobUrl);
        }
        currentBlobUrl    = blobUrl;
        currentDisplayName = displayName;
        previewImg.src = blobUrl;
        previewArea.classList.remove("hidden");
        errorArea.classList.add("hidden");
        okBtn.disabled = false;
    }

    /** Display an error message in the dialog. */
    function showError(message) {
        logger.error("import", "import error displayed", { message: message });
        errorArea.textContent = message;
        errorArea.classList.remove("hidden");
        previewArea.classList.add("hidden");
        previewImg.src = "";
        okBtn.disabled = true;
    }

    /** Hide the current error message (called before a new fetch attempt). */
    function clearError() {
        errorArea.classList.add("hidden");
        errorArea.textContent = "";
    }

    /** Translate a server error code to a user-facing message, with a fallback. */
    function translateError(errData, fallback) {
        var msg = errorMessages[errData.error] || fallback;
        // Interpolate dynamic placeholders (e.g. {max_size_mb}) from the error response
        if (errData.max_size_mb !== undefined) {
            msg = msg.replace("{max_size_mb}", errData.max_size_mb);
        }
        return msg;
    }

    /**
     * Start a cooldown countdown on a Load button after a fetch completes.
     * Disables the button and shows "Load (3s)", "Load (2s)", "Load (1s)" before
     * re-enabling it with the original label.
     */
    function startLoadCooldown(btn) {
        var remaining = LOAD_COOLDOWN_SECONDS;
        btn.disabled = true;
        btn.textContent = I18N.import_load + " (" + remaining + "s)";
        var timer = setInterval(function () {
            remaining--;
            if (remaining > 0) {
                btn.textContent = I18N.import_load + " (" + remaining + "s)";
            } else {
                clearInterval(timer);
                btn.disabled = false;
                btn.textContent = I18N.import_load;
            }
        }, 1000);
    }

    // ── OK button: confirm and send to cropper ───────────────────────

    okBtn.addEventListener("click", function () {
        if (!currentBlobUrl) return;

        logger.info("import", "import confirmed, sending to cropper", { displayName: currentDisplayName });

        // Transfer ownership of the blob URL to initCropper - don't revoke it
        var blobUrl = currentBlobUrl;
        var name    = currentDisplayName;
        currentBlobUrl     = null;
        currentDisplayName = null;
        closeDialog();
        initCropper(blobUrl, name);
    });

    // ── Public API ───────────────────────────────────────────────────

    window.ImportDialog = {
        openDialog:        openDialog,
        closeDialog:       closeDialog,
        resetPreview:      resetPreview,
        showPreview:       showPreview,
        showError:         showError,
        clearError:        clearError,
        translateError:    translateError,
        startLoadCooldown: startLoadCooldown,
        /**
         * Set by dashboard-import-webcam.js to stop the camera stream when
         * the dialog is closed.
         */
        onDialogClose: null,
        /**
         * Set by dashboard-import-webcam.js to stop the camera stream when
         * switching away from the webcam tab. Receives the new tab name.
         */
        onTabSwitch: null,
    };
})();
