/**
 * dashboard-remove-avatar.js – Avatar removal with custom confirmation dialog.
 *
 * Handles the "Remove current avatar" button in the profile header.
 * Opens a themed confirmation dialog, sends a POST to the server on confirm,
 * and updates the profile header to show the placeholder initial on success.
 *
 * Depends on server-provided constants injected by the template:
 *   REMOVE_AVATAR_ENDPOINT, CSRF_TOKEN, I18N, AVATAR_INITIAL
 * Depends on escapeHTML() and showResult() from dashboard-main.js
 *   (loaded before this script).
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

    function openRemoveDialog() {
        removeAvatarOverlay.classList.remove("hidden");
    }

    function closeRemoveDialog() {
        removeAvatarOverlay.classList.add("hidden");
    }

    // Open dialog when remove button is clicked
    removeAvatarBtn.addEventListener("click", openRemoveDialog);

    // Close handlers: X button, Cancel button, backdrop click, Escape key
    removeAvatarCloseBtn.addEventListener("click", closeRemoveDialog);
    removeAvatarCancelBtn.addEventListener("click", closeRemoveDialog);
    removeAvatarOverlay.addEventListener("click", function (event) {
        if (!event.target.closest(".dialog-panel")) closeRemoveDialog();
    });
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && !removeAvatarOverlay.classList.contains("hidden")) {
            closeRemoveDialog();
        }
    });

    // Confirm: send the removal request
    removeAvatarConfirmBtn.addEventListener("click", async function () {
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

            closeRemoveDialog();

            if (!resp.ok) {
                showResult("result-error", escapeHTML(I18N.reset_avatar_failed));
                return;
            }

            // Replace the avatar <img> with a placeholder initial-letter circle
            var profileAvatar = document.querySelector(".profile-avatar");
            if (profileAvatar) {
                var placeholder = document.createElement("div");
                placeholder.className = "profile-avatar profile-avatar--placeholder";
                placeholder.textContent = AVATAR_INITIAL;
                profileAvatar.replaceWith(placeholder);
            }

            // Hide the remove button (avatar is gone)
            removeAvatarBtn.classList.add("hidden");
        } catch (e) {
            closeRemoveDialog();
            showResult("result-error", escapeHTML(I18N.reset_avatar_failed));
        } finally {
            removeAvatarConfirmBtn.disabled = false;
            removeAvatarCancelBtn.disabled = false;
        }
    });
})();
