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
const profileDivider     = document.getElementById("profileDivider");
const progressPanel      = document.getElementById("progressPanel");
const progressList       = document.getElementById("progressList");
const resultMessage      = document.getElementById("resultMessage");

// Import section element (hidden during upload processing)
const importSection      = document.getElementById("importSection");

// Cropper.js v2 instance - created when user selects an image
let cropperInstance = null;

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

/** Build and return the retry/change-avatar button element shown after a result. */
function buildRetryButton() {
    var btn = document.createElement("button");
    btn.className = "btn btn-block btn-retry";
    btn.textContent = I18N.result_retry;
    // Use addEventListener instead of an inline onclick attribute - inline handlers
    // are blocked by the CSP script-src nonce directive.
    btn.addEventListener("click", function () { location.reload(); });
    return btn;
}

/** Reset upload button to its default enabled state. */
function resetUploadButton() {
    uploadButton.disabled = false;
    uploadButton.textContent = I18N.upload_button;
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
 * Update the profile avatar overlay image in the header.
 * The container div (with the initials span) is always present in the DOM as
 * the base layer.  Passing a URL adds or updates the overlay <img>; passing
 * null removes it so the initials show through again.
 */
function setProfileAvatar(url) {
    logger.debug("main", "profile avatar updated in header", { url: url });
    var avatarContainer = document.querySelector(".profile-avatar");
    if (!avatarContainer) return;
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
            img.onerror = function () { this.remove(); };
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
    if (existing) existing.onerror = function () { this.remove(); };
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
    uploadDisclaimer.classList.remove("hidden");
    uploadButton.classList.remove("hidden");
    uploadButton.disabled = false;

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

    // Clear the file input so the same file can be re-selected
    fileInput.value = "";

    // Hide image bar and cropper controls
    imageSelectedBar.classList.add("hidden");
    cropperWrapper.classList.add("hidden");
    uploadDisclaimer.classList.add("hidden");
    uploadButton.classList.add("hidden");

    // Restore file picker and import section
    filePicker.classList.remove("hidden");
    if (importSection) importSection.classList.remove("hidden");
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

// Upload flow with SSE streaming progress
uploadButton.addEventListener("click", async function () {
    if (!cropperInstance) return;

    logger.info("main", "upload started by user");

    // Lock the UI and switch to progress view
    uploadSection.classList.add("hidden");
    progressPanel.classList.remove("hidden");
    progressList.innerHTML = "";
    resultMessage.innerHTML = "";

    // Step 1: Crop the image to a square at the server's max avatar dimension
    var cropStepElement = appendStep(I18N.step_crop, "active");
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
    updateStep(cropStepElement, I18N.step_crop, "success");

    // Step 2: Compress to WebP (preferred) with JPEG as fallback
    var compressStepElement = appendStep(I18N.step_compress, "active");
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

    var fileSizeKB = (imageBlob.size / 1024).toFixed(0);
    logger.debug("main", "compress complete", { format: imageFormat, sizeKB: fileSizeKB });
    updateStep(compressStepElement, I18N.step_compress, "success",
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
                showResult("result-error", I18N.result_csrf_failed);
                return;
            }
            logger.error("main", "upload rejected - server validation error", { error: errorData.error });
            updateStep(uploadStepElement, I18N.step_upload, "failed", errorData.error || "");
            // resultMessage.innerHTML only contains static I18N strings
            // that are escaped via escapeHTML() - no untrusted data is interpolated.
            resultMessage.innerHTML = '<p class="result-error">' + escapeHTML(I18N.result_error) + '</p>';
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

            // Success: show the updated avatar
            showResult("result-success", I18N.result_success);

            // Update the profile avatar in the header with the new URL
            setProfileAvatar(finalResult.avatar_url);
        } else {
            // Failure: show the error with a retry button
            var errorCode = finalResult && finalResult.error;
            logger.error("main", "avatar upload failed", { error: errorCode || null });
            var errorMessage = (errorCode === 'contact_admin')
                ? I18N.step_save_failed + ' ' + I18N.result_contact_admin
                : (errorCode ? errorCode : I18N.result_error);
            showResult("result-error", errorMessage);
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
        showResult("result-error", I18N.result_network_error);
    }
});
