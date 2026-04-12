/**
 * dashboard-import-webcam.js - Webcam tab handler for the import dialog.
 *
 * Manages camera access via getUserMedia(), live preview, and frame capture.
 * The captured frame is converted to a Blob and passed to the shared
 * ImportDialog preview pipeline (same path as Gravatar / URL imports).
 *
 * Also registers ImportDialog.onDialogClose and ImportDialog.onTabSwitch hooks
 * so the common module can stop the camera stream on close or tab switch.
 *
 * Only loaded when import_webcam_enabled is true (template-conditional).
 *
 * Depends on ImportDialog from dashboard-import-common.js (loaded before this script).
 * Depends on server-provided constants injected by the template: I18N
 */
(function () {
    "use strict";

    var tabWebcam         = document.getElementById("importTabWebcam");
    var webcamStage       = document.getElementById("webcamStage");
    var webcamVideo       = document.getElementById("webcamVideo");
    var webcamPlaceholder = document.getElementById("webcamPlaceholder");
    var webcamStartBtn    = document.getElementById("webcamStartBtn");
    var webcamCaptureBtn  = document.getElementById("webcamCaptureBtn");
    var webcamRetakeBtn   = document.getElementById("webcamRetakeBtn");
    var webcamStopBtn     = document.getElementById("webcamStopBtn");
    if (!tabWebcam || !webcamStartBtn || !webcamVideo) return;

    // Active MediaStream (retained so its tracks can be stopped on close)
    var webcamStream = null;

    /**
     * Release the active MediaStream (if any) and reset the webcam UI back
     * to its initial "Start camera" state. Called on tab switch, dialog
     * close, and after an unsuccessful start.
     */
    function stopWebcam() {
        if (webcamStream) {
            webcamStream.getTracks().forEach(function (track) { track.stop(); });
            webcamStream = null;
        }
        webcamVideo.srcObject = null;
        webcamVideo.classList.add("hidden");
        // Ensure the stage is visible so it shows the placeholder on next open
        if (webcamStage)       webcamStage.classList.remove("hidden");
        if (webcamPlaceholder) webcamPlaceholder.classList.remove("hidden");
        webcamStartBtn.classList.remove("hidden");
        webcamStartBtn.disabled = false;
        webcamStartBtn.textContent = I18N.import_webcam_start;
        if (webcamCaptureBtn) webcamCaptureBtn.classList.add("hidden");
        if (webcamRetakeBtn)  webcamRetakeBtn.classList.add("hidden");
        if (webcamStopBtn)    webcamStopBtn.classList.add("hidden");
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

    // ── Register hooks with the common module ────────────────────────

    // Stop the stream when the dialog is closed (camera light must go off)
    ImportDialog.onDialogClose = stopWebcam;

    // Stop the stream when switching away from the webcam tab
    ImportDialog.onTabSwitch = function (newTab) {
        if (newTab !== "webcam") stopWebcam();
    };

    // ── Start camera ─────────────────────────────────────────────────

    webcamStartBtn.addEventListener("click", async function () {
        // Feature-detect: getUserMedia is only exposed on secure contexts.
        // On plain HTTP (except localhost) navigator.mediaDevices is undefined.
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            logger.warn("import", "webcam unavailable - getUserMedia not supported in this context");
            ImportDialog.showError(I18N.import_webcam_unsupported);
            return;
        }

        logger.info("import", "webcam start requested");
        webcamStartBtn.disabled = true;
        webcamStartBtn.textContent = I18N.import_webcam_starting;
        ImportDialog.clearError();

        try {
            // Request the user-facing camera at a reasonable resolution.
            // The browser may deliver a different resolution or aspect ratio
            // depending on the hardware - that's fine, we crop client-side.
            webcamStream = await navigator.mediaDevices.getUserMedia({
                video: {
                    facingMode: "user",
                    width:  { ideal: 1280 },
                    height: { ideal: 720 },
                },
                audio: false,
            });

            var track    = webcamStream.getVideoTracks()[0];
            var settings = track ? track.getSettings() : {};
            logger.info("import", "webcam stream acquired", { width: settings.width, height: settings.height, deviceId: settings.deviceId });

            webcamVideo.srcObject = webcamStream;
            webcamVideo.classList.remove("hidden");
            if (webcamPlaceholder) webcamPlaceholder.classList.add("hidden");
            webcamStartBtn.classList.add("hidden");
            if (webcamCaptureBtn) webcamCaptureBtn.classList.remove("hidden");
            if (webcamStopBtn)    webcamStopBtn.classList.remove("hidden");
        } catch (err) {
            // Permission denied, no camera, hardware busy, etc.
            logger.error("import", "webcam start failed", { errorName: err.name, message: err.message });
            ImportDialog.showError(webcamErrorMessage(err));
            stopWebcam();
        } finally {
            webcamStartBtn.disabled = false;
            webcamStartBtn.textContent = I18N.import_webcam_start;
        }
    });

    // ── Capture frame ────────────────────────────────────────────────

    if (webcamCaptureBtn) {
        webcamCaptureBtn.addEventListener("click", function () {
            // Guard against a click before the first frame has been decoded
            var vw = webcamVideo.videoWidth;
            var vh = webcamVideo.videoHeight;
            if (!vw || !vh) {
                ImportDialog.showError(I18N.import_webcam_error);
                return;
            }

            // Draw the current frame to an offscreen canvas and convert to a Blob.
            // This is entirely client-side: no upload happens until the user
            // confirms with the OK button, which funnels the blob into the
            // existing cropper flow (same path as Gravatar / URL imports).
            var canvas = document.createElement("canvas");
            canvas.width  = vw;
            canvas.height = vh;
            var ctx = canvas.getContext("2d");
            // Mirror the capture horizontally to match the mirrored live preview
            // (the video element is flipped via CSS transform: scaleX(-1)). Without
            // this, the saved image would look horizontally reversed compared to
            // what the user sees while framing the shot.
            ctx.translate(vw, 0);
            ctx.scale(-1, 1);
            ctx.drawImage(webcamVideo, 0, 0, vw, vh);

            canvas.toBlob(function (blob) {
                if (!blob) {
                    logger.error("import", "webcam frame capture failed - canvas toBlob returned null");
                    ImportDialog.showError(I18N.import_webcam_error);
                    return;
                }
                logger.debug("import", "webcam frame captured", { width: vw, height: vh });
                // Display the captured frame in the shared preview area and
                // swap the control bar to "Retake". The live stream keeps
                // running so the user can retake without re-requesting
                // camera permission. Hide the stage so only the captured
                // image (in importPreview) is visible.
                ImportDialog.showPreview(URL.createObjectURL(blob), "webcam.jpg");
                if (webcamStage)  webcamStage.classList.add("hidden");
                webcamVideo.classList.add("hidden");
                webcamCaptureBtn.classList.add("hidden");
                if (webcamRetakeBtn) webcamRetakeBtn.classList.remove("hidden");
            }, "image/jpeg", 0.92);
        });
    }

    // ── Retake ───────────────────────────────────────────────────────

    if (webcamRetakeBtn) {
        webcamRetakeBtn.addEventListener("click", function () {
            logger.debug("import", "webcam retake requested");
            // Clear the captured preview and return to the live stream.
            // resetPreview() revokes the blob URL created by showPreview().
            ImportDialog.resetPreview();
            if (webcamStream) {
                if (webcamStage)  webcamStage.classList.remove("hidden");
                webcamVideo.classList.remove("hidden");
                if (webcamCaptureBtn) webcamCaptureBtn.classList.remove("hidden");
                webcamRetakeBtn.classList.add("hidden");
            } else {
                // Stream was somehow torn down - force a full reset.
                stopWebcam();
            }
        });
    }

    // ── Stop camera ──────────────────────────────────────────────────

    if (webcamStopBtn) {
        webcamStopBtn.addEventListener("click", function () {
            logger.debug("import", "webcam stopped by user");
            ImportDialog.resetPreview();
            stopWebcam();
        });
    }
})();
