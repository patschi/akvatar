/**
 * dashboard-image-import.js – Import dialog for fetching images from Gravatar or a remote URL.
 *
 * Opens a modal overlay with tab switching between Gravatar (by email) and URL
 * modes.  Each tab has an input field and a "Load" button that fetches the image
 * via a server-side proxy and shows a preview.  Clicking "Use image" sends the
 * fetched image to initCropper() on the dashboard.
 *
 * Depends on server-provided constants injected by the template:
 *   GRAVATAR_ENDPOINT, URL_FETCH_ENDPOINT, CSRF_TOKEN, I18N,
 *   IMPORT_GRAVATAR_ENABLED, IMPORT_URL_ENABLED
 * Depends on initCropper() from dashboard-main.js (loaded before this script).
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

    // Webcam tab elements (null when webcam import is disabled in config)
    var tabWebcam = document.getElementById("importTabWebcam");
    var webcamVideo = document.getElementById("webcamVideo");
    var webcamPlaceholder = document.getElementById("webcamPlaceholder");
    var webcamStartBtn = document.getElementById("webcamStartBtn");
    var webcamCaptureBtn = document.getElementById("webcamCaptureBtn");
    var webcamRetakeBtn = document.getElementById("webcamRetakeBtn");
    var webcamStopBtn = document.getElementById("webcamStopBtn");

    // Active MediaStream (retained so its tracks can be stopped on close)
    var webcamStream = null;

    // Trigger buttons on the dashboard page (open dialog with a specific tab)
    var triggerBtns = document.querySelectorAll(".import-triggers [data-import-tab]");

    // State: currently loaded preview blob URL and display name
    var currentBlobUrl = null;
    var currentDisplayName = null;

    // Cooldown delay (in seconds) before a Load button becomes pressable again
    var LOAD_COOLDOWN_SECONDS = 3;

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

    // Map known server error codes to translated messages
    var errorMessages = {
        "csrf_failed":            I18N.result_csrf_failed,
        "fetch_failed":           I18N.import_fetch_failed,
        "image_too_large":        I18N.import_image_too_large,
        "url_not_allowed":        I18N.import_url_not_allowed,
    };

    // ── Dialog open / close ──────────────────────────────────────────

    function openDialog(tab) {
        // Default to whichever tab is actually available (gravatar -> url -> webcam)
        if (tab === "gravatar" && !tabGravatar) tab = tabUrl ? "url" : "webcam";
        if (tab === "url" && !tabUrl) tab = tabGravatar ? "gravatar" : "webcam";
        if (tab === "webcam" && !tabWebcam) tab = tabGravatar ? "gravatar" : "url";
        switchTab(tab || "gravatar");
        resetPreview();
        overlay.classList.remove("hidden");
    }

    function closeDialog() {
        overlay.classList.add("hidden");
        stopWebcam();
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
        if (tabWebcam) tabWebcam.classList.toggle("hidden", tab !== "webcam");
        // Leaving the webcam tab must stop the stream so the camera light goes off
        if (tab !== "webcam") {
            stopWebcam();
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
    function translateError(errData, fallback) {
        var msg = errorMessages[errData.error] || fallback;
        // Interpolate dynamic placeholders (e.g. {max_size_mb}) from the error response
        if (errData.max_size_mb !== undefined) {
            msg = msg.replace("{max_size_mb}", errData.max_size_mb);
        }
        return msg;
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
                    showError(translateError(errData, I18N.import_gravatar_error));
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
                startLoadCooldown(gravatarLoadBtn);
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
                    showError(translateError(errData, I18N.import_url_error));
                    return;
                }

                var blob = await resp.blob();
                // Use the last path segment as a display name, with a fallback
                var displayName = url.split("/").pop().split("?")[0] || "Remote image";
                showPreview(URL.createObjectURL(blob), displayName);
            } catch (e) {
                showError(I18N.import_url_error);
            } finally {
                startLoadCooldown(urlLoadBtn);
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

    // ── Webcam capture ───────────────────────────────────────────────

    /**
     * Release the active MediaStream (if any) and reset the webcam UI back
     * to its initial "Start camera" state.  Called on tab switch, dialog
     * close, and after a frame is captured.
     */
    function stopWebcam() {
        if (webcamStream) {
            webcamStream.getTracks().forEach(function (track) { track.stop(); });
            webcamStream = null;
        }
        if (webcamVideo) {
            webcamVideo.srcObject = null;
            webcamVideo.classList.add("hidden");
        }
        if (webcamPlaceholder) webcamPlaceholder.classList.remove("hidden");
        if (webcamStartBtn) {
            webcamStartBtn.classList.remove("hidden");
            webcamStartBtn.disabled = false;
            webcamStartBtn.textContent = I18N.import_webcam_start;
        }
        if (webcamCaptureBtn) webcamCaptureBtn.classList.add("hidden");
        if (webcamRetakeBtn) webcamRetakeBtn.classList.add("hidden");
        if (webcamStopBtn) webcamStopBtn.classList.add("hidden");
    }

    /**
     * Translate a getUserMedia() DOMException name to a user-facing message.
     * The most common cases are permission denial and the absence of any
     * video input device; everything else gets a generic fallback.
     */
    function webcamErrorMessage(err) {
        if (!err || !err.name) return I18N.import_webcam_error;
        switch (err.name) {
            case "NotAllowedError":
            case "SecurityError":
                return I18N.import_webcam_denied;
            case "NotFoundError":
            case "OverconstrainedError":
                return I18N.import_webcam_not_found;
            case "NotReadableError":
            case "AbortError":
                return I18N.import_webcam_in_use;
            default:
                return I18N.import_webcam_error;
        }
    }

    if (webcamStartBtn && webcamVideo) {
        webcamStartBtn.addEventListener("click", async function () {
            // Feature-detect: getUserMedia is only exposed on secure contexts.
            // On plain HTTP (except localhost) navigator.mediaDevices is undefined.
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                showError(I18N.import_webcam_unsupported);
                return;
            }

            webcamStartBtn.disabled = true;
            webcamStartBtn.textContent = I18N.import_webcam_starting;
            errorArea.classList.add("hidden");

            try {
                // Request the user-facing camera with a square-ish aspect ratio
                // so the live preview approximates the final crop.  The browser
                // is free to ignore ideal constraints when the camera cannot
                // satisfy them, which is fine - we crop client-side anyway.
                webcamStream = await navigator.mediaDevices.getUserMedia({
                    video: {
                        facingMode: "user",
                        width: { ideal: 1280 },
                        height: { ideal: 1280 },
                    },
                    audio: false,
                });

                webcamVideo.srcObject = webcamStream;
                webcamVideo.classList.remove("hidden");
                if (webcamPlaceholder) webcamPlaceholder.classList.add("hidden");
                webcamStartBtn.classList.add("hidden");
                webcamCaptureBtn.classList.remove("hidden");
                webcamStopBtn.classList.remove("hidden");
            } catch (err) {
                // Permission denied, no camera, hardware busy, etc.
                showError(webcamErrorMessage(err));
                stopWebcam();
            } finally {
                webcamStartBtn.disabled = false;
                webcamStartBtn.textContent = I18N.import_webcam_start;
            }
        });
    }

    if (webcamCaptureBtn && webcamVideo) {
        webcamCaptureBtn.addEventListener("click", function () {
            // Guard against a click before the first frame has been decoded
            var vw = webcamVideo.videoWidth;
            var vh = webcamVideo.videoHeight;
            if (!vw || !vh) {
                showError(I18N.import_webcam_error);
                return;
            }

            // Draw the current frame to an offscreen canvas and convert to a Blob.
            // This is entirely client-side: no upload happens until the user
            // confirms with the OK button, which funnels the blob into the
            // existing cropper flow (same path as Gravatar / URL imports).
            var canvas = document.createElement("canvas");
            canvas.width = vw;
            canvas.height = vh;
            var ctx = canvas.getContext("2d");
            // Mirror the capture horizontally to match the mirrored live preview
            // (the video element is flipped via CSS transform: scaleX(-1)).  Without
            // this, the saved image would look horizontally reversed compared to
            // what the user sees while framing the shot.
            ctx.translate(vw, 0);
            ctx.scale(-1, 1);
            ctx.drawImage(webcamVideo, 0, 0, vw, vh);

            canvas.toBlob(function (blob) {
                if (!blob) {
                    showError(I18N.import_webcam_error);
                    return;
                }
                // Display the captured frame in the shared preview area and
                // swap the control bar to "Retake".  The live stream keeps
                // running so the user can retake without re-requesting
                // camera permission.
                showPreview(URL.createObjectURL(blob), "webcam.jpg");
                webcamVideo.classList.add("hidden");
                webcamCaptureBtn.classList.add("hidden");
                webcamRetakeBtn.classList.remove("hidden");
            }, "image/jpeg", 0.92);
        });
    }

    if (webcamRetakeBtn && webcamVideo) {
        webcamRetakeBtn.addEventListener("click", function () {
            // Clear the captured preview and return to the live stream.
            // resetPreview() revokes the blob URL created by showPreview().
            resetPreview();
            if (webcamStream) {
                webcamVideo.classList.remove("hidden");
                webcamCaptureBtn.classList.remove("hidden");
                webcamRetakeBtn.classList.add("hidden");
            } else {
                // Stream was somehow torn down - force a full reset.
                stopWebcam();
            }
        });
    }

    if (webcamStopBtn) {
        webcamStopBtn.addEventListener("click", function () {
            resetPreview();
            stopWebcam();
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
