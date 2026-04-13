/**
 * dashboard-import-gravatar.js - Gravatar tab handler for the import dialog.
 *
 * Fetches the Gravatar image for an email address via a server-side proxy,
 * then shows it in the shared import preview.
 *
 * Only loaded when import_gravatar_enabled is true (template-conditional).
 *
 * Depends on ImportDialog from dashboard-import-common.js (loaded before this script).
 * Depends on server-provided constants injected by the template:
 *   GRAVATAR_ENDPOINT, CSRF_TOKEN, I18N
 */
(function () {
    "use strict";

    var gravatarEmail   = document.getElementById("gravatarEmail");
    var gravatarLoadBtn = document.getElementById("gravatarLoadBtn");
    if (!gravatarEmail || !gravatarLoadBtn) return;

    gravatarLoadBtn.addEventListener("click", async function () {
        var email = gravatarEmail.value.trim();
        if (!email) return;

        logger.info("import", "Gravatar fetch started", { email: email });
        gravatarLoadBtn.disabled = true;
        gravatarLoadBtn.textContent = I18N.import_gravatar_loading;
        // Clear any previous error immediately so the UI looks responsive while fetching
        ImportDialog.clearError();

        try {
            var resp = await fetch(GRAVATAR_ENDPOINT, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRF-Token": CSRF_TOKEN,
                },
                body: JSON.stringify({ email: email }),
            });

            logger.info("import", "Gravatar response received", { status: resp.status, ok: resp.ok });

            if (resp.status === 404) {
                logger.info("import", "Gravatar not found for email");
                ImportDialog.showError(I18N.import_gravatar_not_found);
                return;
            }
            if (!resp.ok) {
                var errData = await resp.json().catch(function () { return {}; });
                logger.error("import", "Gravatar fetch failed", { error: errData.error || null, status: resp.status });
                ImportDialog.showError(ImportDialog.translateError(errData, I18N.import_gravatar_error));
                return;
            }

            var blob = await resp.blob();
            // Extract the filename from the Content-Disposition header
            // (the server includes the hash + file extension)
            var disposition = resp.headers.get("Content-Disposition") || "";
            var nameMatch   = disposition.match(/filename="?([^"]+)"?/);
            var displayName = nameMatch ? nameMatch[1] : "gravatar";
            ImportDialog.showPreview(URL.createObjectURL(blob), displayName);
        } catch (e) {
            logger.error("import", "Gravatar fetch threw a network error", { message: e.message });
            ImportDialog.showError(I18N.import_gravatar_error);
        } finally {
            ImportDialog.startLoadCooldown(gravatarLoadBtn, IMPORT_GRAVATAR_COOLDOWN);
        }
    });

    // Enter key in the Gravatar email input triggers load
    gravatarEmail.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
            event.preventDefault();
            gravatarLoadBtn.click();
        }
    });
})();
