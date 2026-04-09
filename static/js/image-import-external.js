/**
 * image-import-external.js – Import dialog for fetching images from Gravatar or a remote URL.
 *
 * Opens a modal overlay with tab switching between Gravatar (by email) and URL
 * modes.  Each tab has an input field and a "Load" button that fetches the image
 * via a server-side proxy and shows a preview.  Clicking "Use image" sends the
 * fetched image to initCropper() on the dashboard.
 *
 * Depends on server-provided constants injected by the template:
 *   GRAVATAR_ENDPOINT, URL_FETCH_ENDPOINT, CSRF_TOKEN, I18N,
 *   IMPORT_GRAVATAR_ENABLED, IMPORT_URL_ENABLED
 * Depends on initCropper() from dashboard.js (loaded before this script).
 */
(function () {
    "use strict";

    // Dialog overlay and panel
    var overlay = document.getElementById("importOverlay");
    if (!overlay) return;
    var panel = overlay.querySelector(".dialog-panel");
    var closeBtn = overlay.querySelector(".dialog-close");

    // Tab buttons inside the dialog (only present when both sources are enabled)
    var dialogTabBtns = overlay.querySelectorAll("[data-import-tab]");

    // Tab content panes (may be absent if the source is disabled in config)
    var tabGravatar = document.getElementById("importTabGravatar");
    var tabUrl = document.getElementById("importTabUrl");

    // Preview and error areas
    var previewArea = document.getElementById("importPreview");
    var previewImg = document.getElementById("importPreviewImg");
    var errorArea = document.getElementById("importError");

    // Footer buttons
    var cancelBtn = document.getElementById("importCancelBtn");
    var okBtn = document.getElementById("importOkBtn");

    // Input elements (may be null if the source is disabled in config)
    var gravatarEmail = document.getElementById("gravatarEmail");
    var gravatarLoadBtn = document.getElementById("gravatarLoadBtn");
    var urlInput = document.getElementById("urlInput");
    var urlLoadBtn = document.getElementById("urlLoadBtn");

    // Trigger buttons on the dashboard page (open dialog with a specific tab)
    var triggerBtns = document.querySelectorAll(".import-triggers [data-import-tab]");

    // State: currently loaded preview blob URL and display name
    var currentBlobUrl = null;
    var currentDisplayName = null;

    // Map known server error codes to translated messages
    var errorMessages = {
        "csrf_failed":            I18N.result_csrf_failed,
        "fetch_failed":           I18N.import_fetch_failed,
        "url_not_allowed":        I18N.import_url_not_allowed,
    };

    // ── Dialog open / close ──────────────────────────────────────────

    function openDialog(tab) {
        // Default to whichever tab is actually available
        if (tab === "gravatar" && !tabGravatar) tab = "url";
        if (tab === "url" && !tabUrl) tab = "gravatar";
        switchTab(tab || "gravatar");
        resetPreview();
        overlay.classList.remove("hidden");
    }

    function closeDialog() {
        overlay.classList.add("hidden");
        resetPreview();
    }

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

    // ── Tab switching ────────────────────────────────────────────────

    function switchTab(tab) {
        // Highlight the active tab button
        dialogTabBtns.forEach(function (btn) {
            btn.classList.toggle("active", btn.dataset.importTab === tab);
        });
        // Show/hide tab content panes
        if (tabGravatar) tabGravatar.classList.toggle("hidden", tab !== "gravatar");
        if (tabUrl) tabUrl.classList.toggle("hidden", tab !== "url");
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

    // Close button (X)
    closeBtn.addEventListener("click", closeDialog);

    // Cancel button
    cancelBtn.addEventListener("click", closeDialog);

    // Backdrop click (outside the panel)
    overlay.addEventListener("click", function (event) {
        if (!panel.contains(event.target)) {
            closeDialog();
        }
    });

    // Escape key
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && !overlay.classList.contains("hidden")) {
            closeDialog();
        }
    });

    // ── Preview helpers ──────────────────────────────────────────────

    /** Display a successfully fetched image in the preview area. */
    function showPreview(blobUrl, displayName) {
        if (currentBlobUrl) {
            URL.revokeObjectURL(currentBlobUrl);
        }
        currentBlobUrl = blobUrl;
        currentDisplayName = displayName;
        previewImg.src = blobUrl;
        previewArea.classList.remove("hidden");
        errorArea.classList.add("hidden");
        okBtn.disabled = false;
    }

    /** Display an error message in the dialog. */
    function showError(message) {
        errorArea.textContent = message;
        errorArea.classList.remove("hidden");
        previewArea.classList.add("hidden");
        previewImg.src = "";
        okBtn.disabled = true;
    }

    /** Translate a server error code to a user-facing message, with a fallback. */
    function translateError(errorCode, fallback) {
        return errorMessages[errorCode] || fallback;
    }

    // ── Gravatar load ────────────────────────────────────────────────

    if (gravatarLoadBtn && gravatarEmail) {
        gravatarLoadBtn.addEventListener("click", async function () {
            var email = gravatarEmail.value.trim();
            if (!email) return;

            gravatarLoadBtn.disabled = true;
            gravatarLoadBtn.textContent = I18N.import_gravatar_loading;
            errorArea.classList.add("hidden");

            try {
                var resp = await fetch(GRAVATAR_ENDPOINT, {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "X-CSRF-Token": CSRF_TOKEN,
                    },
                    body: JSON.stringify({ email: email }),
                });

                if (resp.status === 404) {
                    showError(I18N.import_gravatar_not_found);
                    return;
                }
                if (!resp.ok) {
                    var errData = await resp.json().catch(function () { return {}; });
                    showError(translateError(errData.error, I18N.import_gravatar_error));
                    return;
                }

                var blob = await resp.blob();
                // Extract the filename from the Content-Disposition header
                // (the server includes the hash + file extension)
                var disposition = resp.headers.get("Content-Disposition") || "";
                var nameMatch = disposition.match(/filename="?([^"]+)"?/);
                var displayName = nameMatch ? nameMatch[1] : "gravatar";
                showPreview(URL.createObjectURL(blob), displayName);
            } catch (e) {
                showError(I18N.import_gravatar_error);
            } finally {
                gravatarLoadBtn.disabled = false;
                gravatarLoadBtn.textContent = I18N.import_load;
            }
        });

        // Enter key in Gravatar email input triggers load
        gravatarEmail.addEventListener("keydown", function (event) {
            if (event.key === "Enter") {
                event.preventDefault();
                gravatarLoadBtn.click();
            }
        });
    }

    // ── URL load ─────────────────────────────────────────────────────

    if (urlLoadBtn && urlInput) {
        urlLoadBtn.addEventListener("click", async function () {
            var url = urlInput.value.trim();
            if (!url) return;

            // Client-side scheme validation
            if (!url.startsWith("http://") && !url.startsWith("https://")) {
                showError(I18N.import_url_invalid);
                return;
            }

            urlLoadBtn.disabled = true;
            urlLoadBtn.textContent = I18N.import_url_loading;
            errorArea.classList.add("hidden");

            try {
                var resp = await fetch(URL_FETCH_ENDPOINT, {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "X-CSRF-Token": CSRF_TOKEN,
                    },
                    body: JSON.stringify({ url: url }),
                });

                if (!resp.ok) {
                    var errData = await resp.json().catch(function () { return {}; });
                    showError(translateError(errData.error, I18N.import_url_error));
                    return;
                }

                var blob = await resp.blob();
                // Use the last path segment as a display name, with a fallback
                var displayName = url.split("/").pop().split("?")[0] || "Remote image";
                showPreview(URL.createObjectURL(blob), displayName);
            } catch (e) {
                showError(I18N.import_url_error);
            } finally {
                urlLoadBtn.disabled = false;
                urlLoadBtn.textContent = I18N.import_load;
            }
        });

        // Enter key in URL input triggers load
        urlInput.addEventListener("keydown", function (event) {
            if (event.key === "Enter") {
                event.preventDefault();
                urlLoadBtn.click();
            }
        });
    }

    // ── OK button: confirm and send to cropper ───────────────────────

    okBtn.addEventListener("click", function () {
        if (!currentBlobUrl) return;

        // Transfer ownership of the blob URL to initCropper – don't revoke it
        var blobUrl = currentBlobUrl;
        var name = currentDisplayName;
        currentBlobUrl = null;
        currentDisplayName = null;
        closeDialog();
        initCropper(blobUrl, name);
    });
})();
