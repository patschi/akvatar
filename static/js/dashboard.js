/**
 * dashboard.js – Avatar upload with client-side cropping and server-side
 * processing streamed back as Server-Sent Events (SSE).
 *
 * Depends on server-provided constants injected inline by the template
 * before this script is loaded:
 *   UPLOAD_ENDPOINT, MAX_AVATAR_SIZE, I18N, LDAP_ENABLED, ALLOWED_EXTENSIONS
 */

// DOM element references
const uploadSection      = document.getElementById("uploadSection");
const filePicker         = document.getElementById("filePicker");
const fileInput          = document.getElementById("fileInput");
const fileNameDisplay    = document.getElementById("fileName");
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

// Cropper.js instance — created when user selects an image
let cropperInstance = null;

// Progress step icons (SVGs using currentColor for theme adaptation)
const ICON_CHECK_CIRCLE = '<svg width="20" height="20" viewBox="0 0 20 20" fill="none">'
    + '<circle cx="10" cy="10" r="9" stroke="currentColor" stroke-width="1.5"/>'
    + '<path d="M6 10.5l2.5 3L14 7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
    + '</svg>';

const ICON_CROSS_CIRCLE = '<svg width="20" height="20" viewBox="0 0 20 20" fill="none">'
    + '<circle cx="10" cy="10" r="9" stroke="currentColor" stroke-width="1.5"/>'
    + '<path d="M7 7l6 6M13 7l-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
    + '</svg>';

const ICON_DASH_CIRCLE = '<svg width="20" height="20" viewBox="0 0 20 20" fill="none">'
    + '<circle cx="10" cy="10" r="9" stroke="currentColor" stroke-width="1.5"/>'
    + '<path d="M7 10h6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
    + '</svg>';

const ICON_DASHED_CIRCLE = '<svg width="20" height="20" viewBox="0 0 20 20" fill="none">'
    + '<circle cx="10" cy="10" r="9" stroke="currentColor" stroke-width="1.5" stroke-dasharray="4 3"/>'
    + '</svg>';

const ICON_SPINNER = '<svg width="20" height="20" viewBox="0 0 20 20" fill="none">'
    + '<circle cx="10" cy="10" r="8" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-dasharray="38 14"/>'
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

/** Escape a string for safe insertion into innerHTML. */
var _escapeDiv = document.createElement("div");
function escapeHTML(s) {
    _escapeDiv.textContent = s;
    return _escapeDiv.innerHTML;
}

/** Build the inner HTML for a single progress step row. */
function buildStepHTML(label, status, detail) {
    var icon = STEP_STATUS_ICONS[status] || STEP_STATUS_ICONS.pending;
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

/** Build the HTML for the retry/change-avatar button shown after a result. */
function buildRetryButtonHTML() {
    return '<button class="btn btn-block btn-retry" onclick="location.reload()">'
        + escapeHTML(I18N.result_retry) + '</button>';
}

/** Reset upload button to its default enabled state. */
function resetUploadButton() {
    uploadButton.disabled = false;
    uploadButton.textContent = I18N.upload_button;
}

/** Switch from the upload form to the result view (hides upload controls, shows progress panel). */
function showResultView() {
    uploadSection.classList.add("hidden");
    profileDivider.classList.add("hidden");
    progressPanel.style.marginTop = "0";
    progressPanel.style.paddingTop = "10px";
}

/** Display a result message with a retry button and switch to the result view. */
function showResult(cssClass, messageHTML) {
    showResultView();
    resultMessage.innerHTML =
        '<p class="' + cssClass + '">' + messageHTML + '</p>' +
        buildRetryButtonHTML();
}

// Drop zone element reference
var dropZone = document.getElementById("dropZone");

/**
 * Initialise the cropper with an image source and display name.
 * Shared by file selection, Gravatar import, and URL import.
 */
function initCropper(imageSrc, displayName) {
    fileNameDisplay.textContent = displayName;

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

    // Initialise Cropper.js with a locked square aspect ratio
    cropperInstance = new Cropper(cropperImage, {
        aspectRatio: 1,
        viewMode: 1,
        dragMode: "move",
        autoCropArea: 0.9,
        responsive: true,
        restore: false,
        guides: true,
        center: true,
        highlight: false,
        cropBoxMovable: true,
        cropBoxResizable: true,
        toggleDragModeOnDblclick: false,
        ready: function () {
            // Scroll the cropper into view once the image is fully rendered
            cropperWrapper.scrollIntoView({ behavior: "smooth", block: "center" });
        },
    });
}

/** Validate a selected file and initialise the cropper. Used by both file input and drag-and-drop. */
function handleFileSelection(selectedFile) {
    if (!selectedFile) return;

    // Extract and validate file extension (case-insensitive)
    var fileExtension = selectedFile.name.includes(".")
        ? selectedFile.name.split(".").pop().toLowerCase()
        : "";

    if (!ALLOWED_EXTENSIONS.has(fileExtension)) {
        var allowedList = Array.from(ALLOWED_EXTENSIONS).join(", ");
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

// Drag-and-drop: prevent default browser behaviour on the entire page to avoid
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
    dropZone.classList.add("drop-zone--active");
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
        handleFileSelection(droppedFile);
    }
});

// Upload flow with SSE streaming progress
uploadButton.addEventListener("click", async function () {
    if (!cropperInstance) return;

    // Lock the UI and switch to progress view
    uploadButton.disabled = true;
    uploadButton.textContent = I18N.upload_processing;
    filePicker.classList.add("hidden");
    if (importSection) importSection.classList.add("hidden");
    cropperWrapper.classList.add("hidden");
    progressPanel.classList.remove("hidden");
    progressList.innerHTML = "";
    resultMessage.innerHTML = "";

    // Step 1: Crop the image to a square at the server's max avatar dimension
    var cropStepElement = appendStep(I18N.step_crop, "active");
    var croppedCanvas = cropperInstance.getCroppedCanvas({
        width: MAX_AVATAR_SIZE,
        height: MAX_AVATAR_SIZE,
        imageSmoothingEnabled: true,
        imageSmoothingQuality: "high",
    });
    updateStep(cropStepElement, I18N.step_crop, "success");

    // Step 2: Compress to WebP (preferred) with JPEG as fallback
    var compressStepElement = appendStep(I18N.step_compress, "active");
    var imageBlob = await new Promise(function (resolve) {
        croppedCanvas.toBlob(resolve, "image/webp", 0.85);
    });
    var imageFormat = "webp";

    // Fall back to JPEG if the browser doesn't support WebP encoding
    if (!imageBlob || imageBlob.type !== "image/webp") {
        imageBlob = await new Promise(function (resolve) {
            croppedCanvas.toBlob(resolve, "image/jpeg", 0.85);
        });
        imageFormat = "jpg";
    }

    var fileSizeKB = (imageBlob.size / 1024).toFixed(0);
    updateStep(compressStepElement, I18N.step_compress, "success",
        imageFormat.toUpperCase() + ", " + fileSizeKB + " KB");

    // Step 3: Upload the compressed image to the server
    var uploadStepElement = appendStep(I18N.step_upload, "active");
    var formData = new FormData();
    formData.append("file", imageBlob, "avatar." + imageFormat);

    try {
        var response = await fetch(UPLOAD_ENDPOINT, {
            method: "POST",
            headers: { "X-CSRF-Token": CSRF_TOKEN },
            body: formData,
        });

        // Server returns JSON for validation errors (4xx responses)
        var contentType = response.headers.get("content-type") || "";
        if (contentType.includes("application/json")) {
            var errorData = await response.json();
            // CSRF failure indicates an expired or missing session token –
            // show a specific translated message prompting a page reload.
            if (errorData.error === "csrf_failed") {
                updateStep(uploadStepElement, I18N.step_upload, "failed");
                showResult("result-error", escapeHTML(I18N.result_csrf_failed));
                return;
            }
            updateStep(uploadStepElement, I18N.step_upload, "failed", errorData.error || "");
            // Note: showResult uses escapeHTML internally for all dynamic content.
            // The resultMessage.innerHTML below only contains static I18N strings
            // that are escaped via escapeHTML() – no untrusted data is interpolated.
            resultMessage.innerHTML = '<p class="result-error">' + escapeHTML(I18N.result_error) + '</p>';
            resetUploadButton();
            return;
        }

        // Upload accepted — server streams processing progress as SSE
        updateStep(uploadStepElement, I18N.step_upload, "success");

        // Pre-render all expected server steps as "pending" so the user sees what's coming
        var serverStepLabels = [
            I18N.step_validated,
            I18N.step_filename,
            I18N.step_processed,
            I18N.step_profile_synced,
        ];
        if (LDAP_ENABLED) {
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
                        finalResult = sseEvent;
                        continue;
                    }

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
            // Success: show the updated avatar
            showResult("result-success", escapeHTML(I18N.result_success));

            // Update the avatar image in the profile header
            var profileAvatar = document.querySelector(".profile-avatar");
            if (profileAvatar && profileAvatar.tagName === "IMG") {
                profileAvatar.src = finalResult.avatar_url;
            }
        } else {
            // Failure: show the error with a retry button
            var errorCode = finalResult && finalResult.error;
            var errorMessage = (errorCode === 'contact_admin')
                ? escapeHTML(I18N.step_save_failed) + ' ' + escapeHTML(I18N.result_contact_admin)
                : (errorCode ? escapeHTML(errorCode) : escapeHTML(I18N.result_error));
            showResult("result-error", errorMessage);
        }
    } catch (networkError) {
        // Network failure or stream read error
        var lastStep = progressList.lastChild;
        if (lastStep && lastStep.classList.contains("pending")) {
            lastStep.remove();
        }
        appendStep(I18N.step_upload, "failed", networkError.message);

        // Show the error with a retry button
        showResult("result-error", escapeHTML(I18N.result_network_error));
    }
});
