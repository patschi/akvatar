/**
 * dashboard-remove-avatar.js – Avatar removal with custom confirmation dialog.
 *
 * Handles the "Remove current avatar" button in the profile header.
 * Opens a themed confirmation dialog, sends a POST to the server on confirm,
 * and updates the profile header to show the placeholder initial on success.
 *
 * Depends on server-provided constants injected by the template:
 *   REMOVE_AVATAR_ENDPOINT, CSRF_TOKEN, I18N
 * Depends on escapeHTML() and setProfileAvatar() from dashboard-main.js (loaded before this script).
 */
(function () {
    "use strict";

    var removeAvatarBtn     = document.getElementById("removeAvatarBtn");
    var removeAvatarOverlay = document.getElementById("removeAvatarOverlay");
    if (!removeAvatarBtn || !removeAvatarOverlay) return;

    var removeAvatarTitle      = document.getElementById("removeAvatarTitle");
    var removeAvatarMessage    = document.getElementById("removeAvatarMessage");
    var removeAvatarCloseBtn   = document.getElementById("removeAvatarCloseBtn");
    var removeAvatarCancelBtn  = document.getElementById("removeAvatarCancelBtn");
    var removeAvatarConfirmBtn = document.getElementById("removeAvatarConfirmBtn");

    // Populate text from translations
    removeAvatarTitle.textContent      = I18N.reset_avatar_confirm_title;
    removeAvatarMessage.textContent    = I18N.reset_avatar_confirm_message;
    removeAvatarCancelBtn.textContent  = I18N.reset_avatar_confirm_no;
    removeAvatarConfirmBtn.textContent = I18N.reset_avatar_confirm_yes;

    // Escape key handler (attached only while dialog is open)
    function onEscapeKey(event) {
        if (event.key === "Escape") closeRemoveDialog();
    }

    function openRemoveDialog() {
        logger.info("remove-avatar", "remove avatar dialog opened");
        removeAvatarOverlay.classList.remove("hidden");
        document.addEventListener("keydown", onEscapeKey);
    }

    function closeRemoveDialog() {
        logger.debug("remove-avatar", "remove avatar dialog closed");
        removeAvatarOverlay.classList.add("hidden");
        document.removeEventListener("keydown", onEscapeKey);
    }

    // Open dialog when remove button is clicked
    removeAvatarBtn.addEventListener("click", openRemoveDialog);

    // Close handlers: X button, Cancel button, backdrop click
    removeAvatarCloseBtn.addEventListener("click", closeRemoveDialog);
    removeAvatarCancelBtn.addEventListener("click", closeRemoveDialog);
    removeAvatarOverlay.addEventListener("click", function (event) {
        if (!event.target.closest(".dialog-panel")) closeRemoveDialog();
    });

    // Show an error message inside the dialog (keeps it open so user can dismiss)
    function showDialogError(message) {
        removeAvatarMessage.textContent = message;
        removeAvatarMessage.classList.add("result-error");
        removeAvatarConfirmBtn.classList.add("hidden");
    }

    // Confirm: send the removal request
    removeAvatarConfirmBtn.addEventListener("click", async function () {
        logger.info("remove-avatar", "avatar removal confirmed by user");
        removeAvatarConfirmBtn.disabled = true;
        removeAvatarCancelBtn.disabled = true;

        try {
            var resp = await fetch(REMOVE_AVATAR_ENDPOINT, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRF-Token": CSRF_TOKEN,
                },
            });

            logger.info("remove-avatar", "avatar removal response received", { status: resp.status, ok: resp.ok });

            if (!resp.ok) {
                logger.error("remove-avatar", "avatar removal failed", { status: resp.status });
                showDialogError(I18N.reset_avatar_failed);
                return;
            }

            logger.info("remove-avatar", "avatar removed successfully");
            closeRemoveDialog();

            // Revert the profile avatar in the header to the placeholder circle
            setProfileAvatar(null);

            // Hide the remove button (avatar is gone)
            removeAvatarBtn.classList.add("hidden");
        } catch (e) {
            logger.error("remove-avatar", "network error during avatar removal", { message: e.message });
            showDialogError(I18N.reset_avatar_failed);
        } finally {
            removeAvatarConfirmBtn.disabled = false;
            removeAvatarCancelBtn.disabled = false;
        }
    });
})();
