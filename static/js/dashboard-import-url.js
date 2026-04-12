/**
 * dashboard-import-url.js - URL tab handler for the import dialog.
 *
 * Fetches an image from a remote URL via a server-side proxy, then shows it
 * in the shared import preview.
 *
 * Only loaded when import_url_enabled is true (template-conditional).
 *
 * Depends on ImportDialog from dashboard-import-common.js (loaded before this script).
 * Depends on server-provided constants injected by the template:
 *   URL_FETCH_ENDPOINT, CSRF_TOKEN, I18N
 */
(function () {
    "use strict";

    var urlInput   = document.getElementById("urlInput");
    var urlLoadBtn = document.getElementById("urlLoadBtn");
    if (!urlInput || !urlLoadBtn) return;

    urlLoadBtn.addEventListener("click", async function () {
        var url = urlInput.value.trim();
        if (!url) return;

        // Client-side scheme validation
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            logger.warn("import", "URL rejected - invalid scheme", { url: url });
            ImportDialog.showError(I18N.import_url_invalid);
            return;
        }

        logger.info("import", "URL fetch started", { url: url });
        urlLoadBtn.disabled = true;
        urlLoadBtn.textContent = I18N.import_url_loading;
        // Clear any previous error immediately so the UI looks responsive while fetching
        ImportDialog.clearError();

        try {
            var resp = await fetch(URL_FETCH_ENDPOINT, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRF-Token": CSRF_TOKEN,
                },
                body: JSON.stringify({ url: url }),
            });

            logger.info("import", "URL fetch response received", { status: resp.status, ok: resp.ok });

            if (!resp.ok) {
                var errData = await resp.json().catch(function () { return {}; });
                logger.error("import", "URL fetch failed", { error: errData.error || null, status: resp.status });
                ImportDialog.showError(ImportDialog.translateError(errData, I18N.import_url_error));
                return;
            }

            var blob = await resp.blob();
            // Use the last path segment as a display name, with a fallback
            var displayName = url.split("/").pop().split("?")[0] || "Remote image";
            ImportDialog.showPreview(URL.createObjectURL(blob), displayName);
        } catch (e) {
            logger.error("import", "URL fetch threw a network error", { message: e.message });
            ImportDialog.showError(I18N.import_url_error);
        } finally {
            ImportDialog.startLoadCooldown(urlLoadBtn);
        }
    });

    // Enter key in the URL input triggers load
    urlInput.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
            event.preventDefault();
            urlLoadBtn.click();
        }
    });
})();
