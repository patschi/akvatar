/**
 * dashboard-main.js - Avatar upload with client-side cropping and server-side
 * processing streamed back as Server-Sent Events (SSE).
 *
 * Depends on server-provided constants injected inline by the template
 * before this script is loaded:
 *   UPLOAD_ENDPOINT, MAX_AVATAR_SIZE, I18N, DIR_SYNC, ALLOWED_EXTENSIONS
 */

// DOM element references
const uploadSection      = document.getElementById("uploadSection");
const filePicker         = document.getElementById("filePicker");
const fileInput          = document.getElementById("fileInput");
const imageSelectedBar   = document.getElementById("imageSelectedBar");
const imageSelectedName  = document.getElementById("imageSelectedName");
const discardImageBtn    = document.getElementById("discardImageBtn");
const cropperWrapper     = document.getElementById("cropperWrapper");
const cropperImage       = document.getElementById("cropperImage");
const uploadButton       = document.getElementById("uploadBtn");
const uploadDisclaimer   = document.getElementById("uploadDisclaimer");
const previewSection     = document.getElementById("previewSection");
const previewImage       = document.getElementById("previewImage");
const previewUploadBtn   = document.getElementById("previewUploadBtn");
const previewReturnBtn   = document.getElementById("previewReturnBtn");
const profileDivider     = document.getElementById("profileDivider");
const progressPanel      = document.getElementById("progressPanel");
const progressList       = document.getElementById("progressList");
const resultMessage      = document.getElementById("resultMessage");

// Import section element (hidden during upload processing)
const importSection      = document.getElementById("importSection");

// Step indicator elements
const stepIndicator      = document.getElementById("stepIndicator");
const stepItems          = stepIndicator ? stepIndicator.querySelectorAll(".step-indicator__step") : [];
const stepLines          = stepIndicator ? stepIndicator.querySelectorAll(".step-indicator__line") : [];

/** Update the step indicator to reflect the current step (1-based). */
function setStep(n) {
    if (stepIndicator) {
        stepIndicator.classList.toggle("hidden", n === 1);
    }
    // Apply active/done state to both step dots and connector lines
    function applyState(nodes) {
        nodes.forEach(function (el, i) {
            el.classList.remove("active", "done", "error");
            if (i + 1 < n) el.classList.add("done");
            else if (i + 1 === n) el.classList.add("active");
        });
    }
    applyState(stepItems);
    applyState(stepLines);
}

/** Mark a step in the indicator as errored (1-based). */
function markStepError(n) {
    if (!stepIndicator || n < 1 || n > stepItems.length) return;
    var step = stepItems[n - 1];
    step.classList.remove("active", "done");
    step.classList.add("error");
}

// Cropper.js v2 instance - created when user selects an image
let cropperInstance = null;

// Preview state: stores the cropped blob between preview and upload
let previewBlob = null;
let previewFormat = null;

// Upload cooldown state: prevents rapid re-uploads after failure
let uploadCooldownEnd = 0;
let uploadCooldownTimer = null;

/** Revoke the preview blob URL (if any), clear the image src, and null out stored blob state. */
function clearPreviewState() {
    if (previewImage.src.startsWith("blob:")) {
        URL.revokeObjectURL(previewImage.src);
    }
    previewImage.src = "";
    previewBlob = null;
    previewFormat = null;
}

// Cropper.js v2 template
var CROPPER_TEMPLATE = ''
    + '<cropper-canvas background>'
    +   '<cropper-image scalable translatable></cropper-image>'
    +   '<cropper-shade hidden></cropper-shade>'
    +   '<cropper-handle action="move" plain></cropper-handle>'
    +   '<cropper-selection aspect-ratio="1" initial-coverage="1" movable resizable outlined>'
    +     '<cropper-grid role="grid" covered></cropper-grid>'
    +     '<cropper-crosshair centered></cropper-crosshair>'
    +     '<cropper-handle action="move" plain></cropper-handle>'
    +     '<cropper-handle action="n-resize"></cropper-handle>'
    +     '<cropper-handle action="e-resize"></cropper-handle>'
    +     '<cropper-handle action="s-resize"></cropper-handle>'
    +     '<cropper-handle action="w-resize"></cropper-handle>'
    +     '<cropper-handle action="ne-resize"></cropper-handle>'
    +     '<cropper-handle action="nw-resize"></cropper-handle>'
    +     '<cropper-handle action="se-resize"></cropper-handle>'
    +     '<cropper-handle action="sw-resize"></cropper-handle>'
    +   '</cropper-selection>'
    + '</cropper-canvas>';

// Progress step icons (SVGs using currentColor for theme adaptation)
const ICON_CHECK_CIRCLE = '<svg width="20" height="20" viewBox="0 0 20 20" fill="none">'
    + '<circle cx="10" cy="10" r="9" stroke="currentColor" stroke-width="1.5"></circle>'
    + '<path d="M6 10.5l2.5 3L14 7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path>'
    + '</svg>';

const ICON_CROSS_CIRCLE = '<svg width="20" height="20" viewBox="0 0 20 20" fill="none">'
    + '<circle cx="10" cy="10" r="9" stroke="currentColor" stroke-width="1.5"></circle>'
    + '<path d="M7 7l6 6M13 7l-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round"></path>'
    + '</svg>';

const ICON_DASH_CIRCLE = '<svg width="20" height="20" viewBox="0 0 20 20" fill="none">'
    + '<circle cx="10" cy="10" r="9" stroke="currentColor" stroke-width="1.5"></circle>'
    + '<path d="M7 10h6" stroke="currentColor" stroke-width="2" stroke-linecap="round"></path>'
    + '</svg>';

const ICON_DASHED_CIRCLE = '<svg width="20" height="20" viewBox="0 0 20 20" fill="none">'
    + '<circle cx="10" cy="10" r="9" stroke="currentColor" stroke-width="1.5" stroke-dasharray="4 3"></circle>'
    + '</svg>';

const ICON_SPINNER = '<svg width="20" height="20" viewBox="0 0 20 20" fill="none">'
    + '<circle cx="10" cy="10" r="8" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-dasharray="38 14"></circle>'
    + '</svg>';

// Map step status to its SVG icon
const STEP_STATUS_ICONS = {
    success:   ICON_CHECK_CIRCLE,
    "dry-run": ICON_CHECK_CIRCLE,
    failed:    ICON_CROSS_CIRCLE,
    skipped:   ICON_DASH_CIRCLE,
    pending:   ICON_DASHED_CIRCLE,
    active:    ICON_SPINNER,
};

// Progress step list helpers

/**
 * Escape a string for safe insertion into innerHTML.
 * Also used by dashboard-remove-avatar.js and dashboard-import-common.js.
 */
var _escapeDiv = document.createElement("div");
function escapeHTML(s) {
    _escapeDiv.textContent = s;
    return _escapeDiv.innerHTML;
}

/** Build the inner HTML for a single progress step row. */
function buildStepHTML(label, status, detail) {
    var icon = STEP_STATUS_ICONS[status] || STEP_STATUS_ICONS.pending;
    // All dynamic values (label, detail) are escaped via escapeHTML() before
    // insertion - only static SVG icon markup is injected unescaped.
    return '<span class="step-icon">' + icon + '</span>'
        + '<span class="step-label">' + escapeHTML(label) + '</span>'
        + (detail ? '<span class="step-detail">' + escapeHTML(detail) + '</span>' : "");
}

/** Append a new progress step row and return its <li> element. */
function appendStep(label, status, detail) {
    var stepElement = document.createElement("li");
    stepElement.className = "step " + status;
    stepElement.innerHTML = buildStepHTML(label, status, detail);
    progressList.appendChild(stepElement);
    return stepElement;
}

/** Update an existing progress step row with new status and detail. */
function updateStep(stepElement, label, status, detail) {
    stepElement.className = "step " + status;
    stepElement.innerHTML = buildStepHTML(label, status, detail);
}

/** Restore the preview step from the progress/result view (used by retry on failure). */
function returnToPreview() {
    progressPanel.classList.add("hidden");
    progressList.innerHTML = "";
    resultMessage.innerHTML = "";
    uploadSection.classList.remove("hidden");
    previewSection.classList.remove("hidden");
    uploadDisclaimer.classList.remove("hidden");
    applyUploadCooldown();
    previewSection.scrollIntoView({ behavior: "smooth", block: "center" });

    setStep(3);
}

/** Record the upload cooldown start (called on failure paths in performUpload). */
function startUploadCooldown() {
    if (!UPLOAD_COOLDOWN) return;
    uploadCooldownEnd = Date.now() + UPLOAD_COOLDOWN * 1000;
}

/**
 * Apply the upload cooldown to previewUploadBtn if still active.
 * Shows a countdown ("Upload image (Ns)") and disables the button until
 * the cooldown expires.  No-op when the cooldown has already elapsed or
 * UPLOAD_COOLDOWN is 0.
 */
function applyUploadCooldown() {
    if (uploadCooldownTimer) {
        clearInterval(uploadCooldownTimer);
        uploadCooldownTimer = null;
    }
    let remaining = Math.ceil((uploadCooldownEnd - Date.now()) / 1000);
    if (!UPLOAD_COOLDOWN || remaining <= 0) {
        previewUploadBtn.disabled = false;
        previewUploadBtn.textContent = I18N.upload_upload_image;
        return;
    }
    previewUploadBtn.disabled = true;
    previewUploadBtn.textContent = I18N.upload_upload_image + " (" + remaining + "s)";
    uploadCooldownTimer = setInterval(function () {
        remaining = Math.ceil((uploadCooldownEnd - Date.now()) / 1000);
        if (remaining > 0) {
            previewUploadBtn.textContent = I18N.upload_upload_image + " (" + remaining + "s)";
        } else {
            clearInterval(uploadCooldownTimer);
            uploadCooldownTimer = null;
            previewUploadBtn.disabled = false;
            previewUploadBtn.textContent = I18N.upload_upload_image;
        }
    }, 1000);
}

/** Build and return the retry/change-avatar button element shown after a result. */
function buildRetryButton() {
    var btn = document.createElement("button");
    btn.className = "btn btn-block btn-retry";
    btn.textContent = I18N.result_retry;
    // Use addEventListener instead of an inline onclick attribute - inline handlers
    // are blocked by the CSP script-src nonce directive.
    btn.addEventListener("click", function () {
        // If the preview blob is still available (upload failed), return to the
        // preview step so the user can retry without re-selecting/re-cropping.
        if (previewBlob) {
            returnToPreview();
        } else {
            location.reload();
        }
    });
    return btn;
}

/** Reset upload button to its default enabled state. */
function resetUploadButton() {
    uploadButton.disabled = false;
    uploadButton.textContent = I18N.upload_preview_image;
}

/** Switch from the upload form to the result view (hides upload controls, shows progress panel). */
function showResultView() {
    uploadSection.classList.add("hidden");
}

/**
 * Display a result message with a retry button and switch to the result view.
 * Also used by dashboard-remove-avatar.js for error display.
 */
function showResult(cssClass, messageText) {
    showResultView();
    // Use DOM methods with textContent for safe text insertion - no escaping needed.
    var para = document.createElement("p");
    para.className = cssClass;
    para.textContent = messageText;
    resultMessage.textContent = "";
    resultMessage.appendChild(para);
    resultMessage.appendChild(buildRetryButton());
}

/**
 * Display an upload failure and start the upload cooldown in one step.
 * All upload failure paths that transition to the result view should use this
 * instead of calling startUploadCooldown() + showResult() separately.
 */
function showUploadError(messageText) {
    startUploadCooldown();
    markStepError(4);
    showResult("result-error", messageText);
}

/**
 * Append a small error badge to the avatar container to signal a load failure.
 * No-op if a badge is already present.
 */
function showAvatarErrorBadge() {
    var avatarContainer = document.querySelector(".profile-avatar");
    if (!avatarContainer) return;
    if (avatarContainer.querySelector(".profile-avatar__error-badge")) return;
    var badge = document.createElement("span");
    badge.className = "profile-avatar__error-badge";
    badge.setAttribute("aria-label", I18N.upload_avatar_load_error);
    badge.setAttribute("data-tooltip", I18N.upload_avatar_load_error);
    badge.innerHTML = '<svg width="10" height="10" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><line x1="10" y1="5" x2="10" y2="11"></line><circle cx="10" cy="15" r="1.2" fill="currentColor" stroke="none"></circle></svg>';
    avatarContainer.appendChild(badge);
}

/** Remove the error badge from the avatar container if present. */
function clearAvatarErrorBadge() {
    var avatarContainer = document.querySelector(".profile-avatar");
    if (!avatarContainer) return;
    var badge = avatarContainer.querySelector(".profile-avatar__error-badge");
    if (badge) badge.remove();
}

/** Attach onload/onerror handlers to an avatar <img> element. */
function attachAvatarImgHandlers(img) {
    img.onload = function () { clearAvatarErrorBadge(); };
    img.onerror = function () {
        logger.error("main", "avatar image failed to load", { src: this.src });
        showAvatarErrorBadge();
        this.remove();
    };
}

/**
 * Update the profile avatar overlay image in the header.
 * The container div (with the initials span) is always present in the DOM as
 * the base layer.  Passing a URL adds or updates the overlay <img>; passing
 * null removes it so the initials show through again.
 */
function setProfileAvatar(url) {
    logger.debug("main", "profile avatar updated in header", { url: url });
    var avatarContainer = document.querySelector(".profile-avatar");
    if (!avatarContainer) return;
    // Clear any stale error badge when the avatar is being updated
    clearAvatarErrorBadge();
    var img = avatarContainer.querySelector(".profile-avatar__img");
    if (url) {
        if (img) {
            // Already has an overlay - update the src
            img.src = url;
        } else {
            // Add the overlay image on top of the initials
            img = document.createElement("img");
            img.className = "profile-avatar__img";
            img.alt = I18N.upload_current_picture;
            attachAvatarImgHandlers(img);
            img.src = url;
            avatarContainer.appendChild(img);
        }
    } else {
        // Remove the overlay so the initials show through
        if (img) img.remove();
    }
}

// Attach onerror fallback to the server-rendered avatar image (if present) so
// a broken URL reveals the initials underneath instead of a broken-image icon.
// Cannot use an inline onerror attribute because CSP blocks inline handlers.
(function () {
    var existing = document.querySelector(".profile-avatar__img");
    if (!existing) return;
    // Image may have already settled before this script ran - check before attaching handlers
    if (existing.complete) {
        if (existing.naturalHeight === 0) {
            // Already failed - show badge and remove the broken element now
            logger.error("main", "avatar image failed to load", { src: existing.src });
            showAvatarErrorBadge();
            existing.remove();
        }
        // Already loaded successfully - nothing to do
        return;
    }
    attachAvatarImgHandlers(existing);
})();

// Drop zone element reference
var dropZone = document.getElementById("dropZone");

/**
 * Initialize the cropper with an image source and display name.
 * Shared by file selection, Gravatar import, and URL import.
 */
function initCropper(imageSrc, displayName) {
    logger.debug("main", "cropper initialized", { displayName: displayName, srcType: imageSrc.startsWith("blob:") ? "blob" : "url" });

    // Hide file picker and import section; reveal the image-selected bar
    filePicker.classList.add("hidden");
    if (importSection) importSection.classList.add("hidden");
    imageSelectedName.textContent = displayName;
    imageSelectedBar.classList.remove("hidden");

    // Clean up previous cropper instance and revoke its object URL to free memory
    if (cropperImage.src.startsWith("blob:")) {
        URL.revokeObjectURL(cropperImage.src);
    }
    if (cropperInstance) {
        cropperInstance.destroy();
        cropperInstance = null;
    }

    // Show the cropper area and upload controls
    cropperImage.src = imageSrc;
    cropperWrapper.classList.remove("hidden");
    uploadButton.classList.remove("hidden");
    uploadButton.disabled = false;

    setStep(2);

    // Initialize Cropper.js v2 with the custom template
    cropperInstance = new Cropper.default(cropperImage, {
        template: CROPPER_TEMPLATE,
    });

    // Scroll the cropper into view once the image is fully loaded (v2 ready equivalent)
    var cropperImageEl = cropperInstance.getCropperImage();
    if (cropperImageEl) {
        cropperImageEl.$ready().then(function () {
            cropperWrapper.scrollIntoView({ behavior: "smooth", block: "center" });
        });
    }
}

/** Discard the selected image and restore the file picker and import section. */
function discardImage() {
    logger.debug("main", "image discarded by user");

    // Destroy the cropper and free the blob URL
    if (cropperImage.src.startsWith("blob:")) {
        URL.revokeObjectURL(cropperImage.src);
    }
    if (cropperInstance) {
        cropperInstance.destroy();
        cropperInstance = null;
    }
    cropperImage.src = "";

    // Clean up preview state
    clearPreviewState();

    // Clear the file input so the same file can be re-selected
    fileInput.value = "";

    // Hide image bar, cropper controls, and preview section
    imageSelectedBar.classList.add("hidden");
    cropperWrapper.classList.add("hidden");
    uploadDisclaimer.classList.add("hidden");
    uploadButton.classList.add("hidden");
    previewSection.classList.add("hidden");

    // Restore file picker and import section
    filePicker.classList.remove("hidden");
    if (importSection) importSection.classList.remove("hidden");

    setStep(1);
}

discardImageBtn.addEventListener("click", discardImage);

/** Validate a selected file and initialize the cropper. Used by both file input and drag-and-drop. */
function handleFileSelection(selectedFile) {
    if (!selectedFile) return;

    // Extract and validate file extension (case-insensitive)
    var fileExtension = selectedFile.name.includes(".")
        ? selectedFile.name.split(".").pop().toLowerCase()
        : "";

    logger.debug("main", "file selected", { name: selectedFile.name, sizeBytes: selectedFile.size, type: selectedFile.type, ext: fileExtension });

    if (!ALLOWED_EXTENSIONS.has(fileExtension)) {
        var allowedList = Array.from(ALLOWED_EXTENSIONS).join(", ");
        logger.warn("main", "file rejected - invalid extension", { ext: fileExtension, allowed: allowedList });
        alert(I18N.upload_invalid_ext
            .replace("{ext}", fileExtension)
            .replace("{allowed}", allowedList));
        fileInput.value = "";
        return;
    }

    initCropper(URL.createObjectURL(selectedFile), selectedFile.name);
}

// File selection via file picker button
fileInput.addEventListener("change", function (event) {
    handleFileSelection(event.target.files[0]);
});

// Clicking anywhere in the drop zone opens the file dialog
dropZone.addEventListener("click", function (event) {
    // Avoid triggering twice when the label/button itself is clicked (it already opens the dialog)
    if (event.target === fileInput || event.target.closest(".file-label")) return;
    fileInput.click();
});

// Drag-and-drop: prevent default browser behavior on the entire page to avoid
// accidentally navigating to the dropped file
document.addEventListener("dragover", function (event) {
    event.preventDefault();
});
document.addEventListener("drop", function (event) {
    event.preventDefault();
});

// Drag-and-drop: visual feedback when hovering over the drop zone
dropZone.addEventListener("dragenter", function (event) {
    event.preventDefault();
    dropZone.classList.add("drop-zone--active");
});
dropZone.addEventListener("dragover", function (event) {
    event.preventDefault();
});
dropZone.addEventListener("dragleave", function (event) {
    // Only remove the highlight when the cursor truly leaves the drop zone,
    // not when hovering over a child element (relatedTarget still inside)
    if (!dropZone.contains(event.relatedTarget)) {
        dropZone.classList.remove("drop-zone--active");
    }
});

// Drag-and-drop: handle the dropped file
dropZone.addEventListener("drop", function (event) {
    event.preventDefault();
    event.stopPropagation();
    dropZone.classList.remove("drop-zone--active");

    var droppedFile = event.dataTransfer.files[0];
    if (droppedFile) {
        logger.debug("main", "file dropped onto drop zone", { name: droppedFile.name });
        handleFileSelection(droppedFile);
    }
});

// Preview flow: crop and compress, then show preview for confirmation
uploadButton.addEventListener("click", async function () {
    if (!cropperInstance) return;

    logger.info("main", "preview started by user");

    // Disable button while processing
    uploadButton.disabled = true;
    uploadButton.textContent = I18N.upload_processing;

    // Crop the image to a square at the server's max avatar dimension
    var cropperSelection = cropperInstance.getCropperSelection();
    var croppedCanvas = await cropperSelection.$toCanvas({
        width: MAX_AVATAR_SIZE,
        height: MAX_AVATAR_SIZE,
        beforeDraw: function (context) {
            context.imageSmoothingEnabled = true;
            context.imageSmoothingQuality = "high";
        },
    });
    logger.debug("main", "crop complete", { width: MAX_AVATAR_SIZE, height: MAX_AVATAR_SIZE });

    // Compress to WebP (preferred) with JPEG as fallback
    var imageBlob = await new Promise(function (resolve) {
        croppedCanvas.toBlob(resolve, "image/webp", 0.85);
    });
    var imageFormat = "webp";

    // Fall back to JPEG if the browser doesn't support WebP encoding
    if (!imageBlob || imageBlob.type !== "image/webp") {
        logger.warn("main", "WebP encoding not supported, falling back to JPEG");
        imageBlob = await new Promise(function (resolve) {
            croppedCanvas.toBlob(resolve, "image/jpeg", 0.85);
        });
        imageFormat = "jpg";
    }

    logger.debug("main", "compress complete", { format: imageFormat, sizeKB: (imageBlob.size / 1024).toFixed(0) });

    // Store blob for upload (revoke any stale blob URL before overwriting)
    if (previewImage.src.startsWith("blob:")) {
        URL.revokeObjectURL(previewImage.src);
    }
    previewBlob = imageBlob;
    previewFormat = imageFormat;

    // Show preview image
    previewImage.src = URL.createObjectURL(imageBlob);

    // Hide cropper controls, show preview section with disclaimer
    cropperWrapper.classList.add("hidden");
    uploadButton.classList.add("hidden");
    uploadDisclaimer.classList.remove("hidden");
    previewSection.classList.remove("hidden");

    // Reset button for when user returns to cropping
    resetUploadButton();

    setStep(3);

    // Scroll preview into view
    previewSection.scrollIntoView({ behavior: "smooth", block: "center" });
});

// Return to cropping from preview
previewReturnBtn.addEventListener("click", function () {
    logger.debug("main", "returning to cropping from preview");

    // Revoke preview blob URL and clear stored state
    clearPreviewState();

    // Hide preview and disclaimer, show cropper and controls
    previewSection.classList.add("hidden");
    uploadDisclaimer.classList.add("hidden");
    cropperWrapper.classList.remove("hidden");
    uploadButton.classList.remove("hidden");

    setStep(2);

    // Scroll cropper into view
    cropperWrapper.scrollIntoView({ behavior: "smooth", block: "center" });
});

/**
 * Send the cropped image blob to the server and stream processing progress
 * as Server-Sent Events (SSE).
 */
async function performUpload(imageBlob, imageFormat) {
    // Lock the UI and switch to progress view
    uploadSection.classList.add("hidden");
    progressPanel.classList.remove("hidden");
    progressList.innerHTML = "";
    resultMessage.innerHTML = "";

    setStep(4);

    // Show crop and compress as already completed
    appendStep(I18N.step_crop, "success");
    var fileSizeKB = (imageBlob.size / 1024).toFixed(0);
    appendStep(I18N.step_compress, "success",
        imageFormat.toUpperCase() + ", " + fileSizeKB + " KB");

    // Step 3: Upload the compressed image to the server
    var uploadStepElement = appendStep(I18N.step_upload, "active");
    var formData = new FormData();
    formData.append("file", imageBlob, "avatar." + imageFormat);

    logger.info("main", "sending upload request", { endpoint: UPLOAD_ENDPOINT, sizeBytes: imageBlob.size });

    try {
        var response = await fetch(UPLOAD_ENDPOINT, {
            method: "POST",
            headers: { "X-CSRF-Token": CSRF_TOKEN },
            body: formData,
        });

        // Server returns JSON for validation errors (4xx responses)
        var contentType = response.headers.get("content-type") || "";
        logger.info("main", "upload response received", { status: response.status, contentType: contentType });

        if (contentType.includes("application/json")) {
            var errorData = await response.json();
            // CSRF failure indicates an expired or missing session token -
            // show a specific translated message prompting a page reload.
            if (errorData.error === "csrf_failed") {
                logger.error("main", "upload rejected - CSRF token failure");
                updateStep(uploadStepElement, I18N.step_upload, "failed");
                showUploadError(I18N.result_csrf_failed);
                return;
            }
            logger.error("main", "upload rejected - server validation error", { error: errorData.error });
            updateStep(uploadStepElement, I18N.step_upload, "failed", errorData.error || "");
            markStepError(4);
            // resultMessage.innerHTML only contains static I18N strings
            // that are escaped via escapeHTML() - no untrusted data is interpolated.
            resultMessage.innerHTML = '<p class="result-error">' + escapeHTML(I18N.result_error) + '</p>';
            startUploadCooldown();
            resetUploadButton();
            return;
        }

        // Upload accepted - server streams processing progress as SSE
        logger.info("main", "upload accepted, streaming SSE progress");
        updateStep(uploadStepElement, I18N.step_upload, "success");

        // Pre-render all expected server steps as "pending" so the user sees what's coming
        var serverStepLabels = [
            I18N.step_validated,
            I18N.step_prepare,
            I18N.step_processed,
            I18N.step_profile_synced,
        ];
        if (DIR_SYNC) {
            serverStepLabels.push(I18N.step_ldap_updated);
        }

        var serverStepElements = serverStepLabels.map(function (label) {
            return appendStep(label, "pending");
        });

        // Mark the first server step as actively processing
        var currentStepIndex = 0;
        updateStep(serverStepElements[0], serverStepLabels[0], "active");

        // Parse the SSE response stream: read chunks, split into frames,
        // update progress steps as each server event arrives
        var streamReader = response.body.getReader();
        var textDecoder = new TextDecoder();
        var sseBuffer = "";
        var finalResult = null;

        while (true) {
            var readResult = await streamReader.read();
            if (readResult.done) break;

            sseBuffer += textDecoder.decode(readResult.value, { stream: true });

            // Safety: discard if buffer grows beyond 64 KB (malformed stream)
            if (sseBuffer.length > 65536) sseBuffer = "";

            // SSE frames are separated by double newlines
            var sseFrames = sseBuffer.split("\n\n");
            sseBuffer = sseFrames.pop(); // Keep the incomplete last frame

            for (var frameIndex = 0; frameIndex < sseFrames.length; frameIndex++) {
                var frameLines = sseFrames[frameIndex].split("\n");

                for (var lineIndex = 0; lineIndex < frameLines.length; lineIndex++) {
                    var line = frameLines[lineIndex];
                    if (!line.startsWith("data: ")) continue;

                    var sseEvent = JSON.parse(line.slice(6));

                    // The final event signals completion with an avatar URL or error
                    if (sseEvent.done) {
                        logger.info("main", "SSE stream complete", { hasAvatarUrl: !!sseEvent.avatar_url, error: sseEvent.error || null });
                        finalResult = sseEvent;
                        continue;
                    }

                    logger.debug("main", "SSE event received", { step: sseEvent.step, status: sseEvent.status, detail: sseEvent.detail || null });

                    // Update the matching pre-rendered step, or append unexpected extra steps
                    if (currentStepIndex < serverStepElements.length) {
                        updateStep(serverStepElements[currentStepIndex],
                            sseEvent.step, sseEvent.status, sseEvent.detail || "");
                    } else {
                        appendStep(sseEvent.step, sseEvent.status, sseEvent.detail || "");
                    }
                    currentStepIndex++;

                    // Advance the spinner to the next pending step
                    if (currentStepIndex < serverStepElements.length) {
                        updateStep(serverStepElements[currentStepIndex],
                            serverStepLabels[currentStepIndex], "active");
                    }
                }
            }
        }

        // Mark any remaining pre-rendered steps as skipped (pipeline ended early)
        for (var i = currentStepIndex; i < serverStepElements.length; i++) {
            updateStep(serverStepElements[i], serverStepLabels[i], "skipped");
        }

        // Display final result
        if (finalResult && finalResult.avatar_url && !finalResult.error) {
            logger.info("main", "avatar upload succeeded", { avatarUrl: finalResult.avatar_url });
            // Commit the pending avatar URL to the session cookie.
            // During the upload request the server stored the canonical URL in
            // session["_pending_avatar"] (captured in the cookie header before the
            // SSE stream starts).  This follow-up POST promotes it to the active
            // session avatar so reloading the page shows the new photo without
            // requiring a re-login.  No body is sent - the server uses the value
            // it already stored, so there is nothing to forge or validate here.
            try {
                await fetch(UPLOAD_COMMIT_ENDPOINT, {
                    method: "POST",
                    headers: { "X-CSRF-Token": CSRF_TOKEN },
                });
            } catch (_commitErr) {
                // Non-fatal: the avatar was updated successfully; the session
                // commit is a best-effort convenience for post-reload display.
            }

            // Success: release the preview blob (no longer needed) and show result
            clearPreviewState();
            showResult("result-success", I18N.result_success);

            setStep(5);

            // Update the profile avatar in the header with the new URL
            setProfileAvatar(finalResult.avatar_url);
        } else {
            // Failure: show the error with a retry button
            var errorCode = finalResult && finalResult.error;
            logger.error("main", "avatar upload failed", { error: errorCode || null });
            var errorMessage = (errorCode === 'contact_admin')
                ? I18N.step_save_failed + ' ' + I18N.result_contact_admin
                : (errorCode ? errorCode : I18N.result_error);
            showUploadError(errorMessage);
        }
    } catch (networkError) {
        // Network failure or stream read error
        logger.error("main", "network or stream error during upload", { message: networkError.message });
        var lastStep = progressList.lastChild;
        if (lastStep && lastStep.classList.contains("pending")) {
            lastStep.remove();
        }
        appendStep(I18N.step_upload, "failed", networkError.message);

        // Show the error with a retry button
        showUploadError(I18N.result_network_error);
    }
    // Note: preview state is intentionally NOT cleared in a finally block.
    // It is released on the success path only (see clearPreviewState() above),
    // so the retry button can return to the preview step without re-selecting
    // or re-cropping the image.
}

// Upload from preview (triggers the server upload with the stored blob)
previewUploadBtn.addEventListener("click", function () {
    if (!previewBlob) return;
    if (uploadDisclaimer.classList.contains("hidden")) return;
    logger.info("main", "upload started from preview");
    performUpload(previewBlob, previewFormat);
});
