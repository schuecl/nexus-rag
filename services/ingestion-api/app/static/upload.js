(() => {
  const config = window.portalUploadConfig;
  const form = document.getElementById("uploadForm");

  if (!config || !form) {
    return;
  }

  const supportedExtensions = new Set([
    "pdf",
    "docx",
    "pptx",
    "xlsx",
    "txt",
    "md",
    "markdown",
    "html",
    "htm",
  ]);
  const terminalStatuses = new Set([
    "pending_review",
    "failed",
    "approved",
    "rejected",
    "superseded",
  ]);

  const fileInput = document.getElementById("fileInput");
  const dropZone = document.getElementById("dropZone");
  const selectedFilesContainer = document.getElementById("selectedFiles");
  const selectedFileTemplate = document.getElementById("selectedFileTemplate");
  const clearFilesButton = document.getElementById("clearFiles");
  const fileError = document.getElementById("fileError");
  const submitButton = document.getElementById("submitButton");
  const submitControls = Array.from(
    document.querySelectorAll(
      '#uploadForm button[type="submit"], button[type="submit"][form="uploadForm"]',
    ),
  );
  const result = document.getElementById("submissionResult");
  const batchResultList = document.getElementById("batchResultList");
  const versionSection = document.getElementById("versionSection");
  const versionSectionHint = document.getElementById("versionSectionHint");
  const supersedesInput = document.getElementById("supersedesDocument");

  const VERSION_HINT_DEFAULT = "Complete this only when the upload replaces an approved document.";
  const VERSION_HINT_BATCH =
    "Not available for a multi-file batch -- supersession replaces one document at a time.";

  // Issue #356: FR-1's "one or more documents" -- one file keeps the
  // existing single-document flow (POST /documents); more than one is
  // submitted as a batch sharing the metadata below (POST /documents/batch).
  let selectedFiles = [];

  const formatBytes = (bytes) => {
    if (bytes < 1024) {
      return `${bytes} bytes`;
    }
    const units = ["KB", "MB", "GB"];
    let value = bytes / 1024;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[index]}`;
  };

  const extensionFor = (file) => {
    const parts = file.name.toLowerCase().split(".");
    return parts.length > 1 ? parts.pop() : "";
  };

  const setFileError = (message = "") => {
    fileError.textContent = message;
    dropZone.classList.toggle("invalid", Boolean(message));
  };

  const updateVersionAvailability = () => {
    const isBatch = selectedFiles.length > 1;
    versionSection.classList.toggle("disabled", isBatch);
    supersedesInput.disabled = isBatch;
    versionSectionHint.textContent = isBatch ? VERSION_HINT_BATCH : VERSION_HINT_DEFAULT;
    if (isBatch) {
      supersedesInput.value = "";
    }
  };

  const renderSelectedFiles = () => {
    // Safe to clear this way -- no markup assigned, just detaching the
    // previous set of cloned rows (see issue #207: the sink to avoid is
    // innerHTML assignment carrying interpolated content, not this).
    selectedFilesContainer.innerHTML = "";
    selectedFiles.forEach((file, index) => {
      const row = selectedFileTemplate.content.firstElementChild.cloneNode(true);
      const extension = extensionFor(file);
      row.querySelector(".file-extension").textContent = (extension || "FILE")
        .toUpperCase()
        .slice(0, 8);
      row.querySelector(".file-name").textContent = file.name;
      row.querySelector(".file-meta").textContent = `${formatBytes(file.size)} · Ready for submission`;
      row.querySelector('[data-action="remove-file"]').addEventListener("click", () => {
        selectedFiles.splice(index, 1);
        renderSelectedFiles();
        updateVersionAvailability();
        updateReadiness();
      });
      selectedFilesContainer.appendChild(row);
    });
    clearFilesButton.hidden = selectedFiles.length === 0;
    if (selectedFiles.length === 0) {
      fileInput.value = "";
    }
  };

  const chooseFiles = (incoming) => {
    const files = Array.from(incoming || []);
    if (!files.length) {
      return;
    }

    const skipped = [];
    files.forEach((file) => {
      const extension = extensionFor(file);
      if (!supportedExtensions.has(extension)) {
        skipped.push(`${file.name} (unsupported type)`);
        return;
      }
      if (file.size > config.maxUploadBytes) {
        skipped.push(`${file.name} (exceeds the 50 MB per-file limit)`);
        return;
      }
      if (file.size === 0) {
        skipped.push(`${file.name} (empty file)`);
        return;
      }
      selectedFiles.push(file);
    });

    if (selectedFiles.length > config.maxBatchFiles) {
      const overflow = selectedFiles.length - config.maxBatchFiles;
      selectedFiles = selectedFiles.slice(0, config.maxBatchFiles);
      skipped.push(`${overflow} file(s) beyond the ${config.maxBatchFiles}-file batch limit`);
    }

    setFileError(
      skipped.length
        ? `Skipped ${skipped.length} file(s): ${skipped.join(", ")}`
        : "",
    );
    renderSelectedFiles();
    updateVersionAvailability();
    updateReadiness();
  };

  const openFilePicker = () => {
    // Clearing first lets selecting the same file again emit a change event.
    fileInput.value = "";
    fileInput.click();
  };

  document.getElementById("browseFile").addEventListener("click", (event) => {
    event.stopPropagation();
    openFilePicker();
  });
  clearFilesButton.addEventListener("click", () => {
    selectedFiles = [];
    setFileError();
    renderSelectedFiles();
    updateVersionAvailability();
    updateReadiness();
  });

  dropZone.addEventListener("click", openFilePicker);
  dropZone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openFilePicker();
    }
  });
  fileInput.addEventListener("change", () => chooseFiles(fileInput.files));

  ["dragenter", "dragover"].forEach((eventName) => {
    dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      if (event.dataTransfer) {
        event.dataTransfer.dropEffect = "copy";
      }
      dropZone.classList.add("dragging");
    });
  });
  ["dragleave", "dragend"].forEach((eventName) => {
    dropZone.addEventListener(eventName, () => dropZone.classList.remove("dragging"));
  });
  dropZone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropZone.classList.remove("dragging");
    chooseFiles(event.dataTransfer?.files);
  });

  const releaseInputs = Array.from(
    document.querySelectorAll('input[name="releasability"]'),
  );
  releaseInputs.forEach((input) => {
    input.addEventListener("change", () => {
      const none = releaseInputs.find(
        (item) => item.value === config.noReleasabilityRestriction,
      );

      if (input === none && input.checked) {
        releaseInputs.forEach((item) => {
          if (item !== none) {
            item.checked = false;
          }
        });
      } else if (input.checked && none) {
        none.checked = false;
      }

      if (!releaseInputs.some((item) => item.checked) && none) {
        none.checked = true;
      }
      updateReadiness();
    });
  });

  const readinessItems = {
    file: () => selectedFiles.length > 0,
    classification: () => Boolean(form.classification.value),
    releasability: () => releaseInputs.some((input) => input.checked),
    scope: () => Boolean(form.access_scope.value.trim()),
    metadata: () =>
      Boolean(form.source_originator.value.trim() && form.doc_type.value.trim()),
  };

  function updateReadiness() {
    let completed = 0;
    Object.entries(readinessItems).forEach(([name, isComplete]) => {
      const done = isComplete();
      document.querySelector(`[data-check="${name}"]`)?.classList.toggle("done", done);
      completed += Number(done);
    });

    document.getElementById("readinessCount").textContent = `${completed} of 5`;
    const progress = document.getElementById("readinessProgress");
    progress.style.width = `${(completed / 5) * 100}%`;
    progress.parentElement.setAttribute("aria-valuenow", String(completed));
  }

  // Batch refactor (issue #356) dropped this without a replacement: file
  // selection and releasability toggles call updateReadiness() explicitly,
  // but classification/access_scope/source_originator/doc_type had nothing
  // wiring them in, so 3 of the 5 checklist items went stale until the next
  // unrelated trigger (e.g. submit).
  ["input", "change"].forEach((eventName) => {
    form.addEventListener(eventName, updateReadiness);
  });

  const setResult = (kind, title, message, body = null) => {
    result.hidden = false;
    result.className = `submission-result ${kind}`;
    document.getElementById("resultTitle").textContent = title;
    document.getElementById("resultMessage").textContent = message;
    const details = document.getElementById("resultDetails");
    details.hidden = !body;
    document.getElementById("resultBody").textContent = body
      ? JSON.stringify(body, null, 2)
      : "";
    result.scrollIntoView({ behavior: "smooth", block: "nearest" });
  };

  const pollStatus = async (documentId, attempt = 0) => {
    const response = await fetch(`/documents/${documentId}`, {
      headers: authHeaders(),
    });
    if (!response.ok) {
      return;
    }

    const documentRecord = await response.json();
    const readableStatus = documentRecord.status.replaceAll("_", " ");
    const failed = documentRecord.status === "failed";
    setResult(
      failed ? "error" : "success",
      failed ? "Document processing failed" : "Document accepted",
      failed
        ? "The worker could not process this document. Open the details for more information."
        : `Current workflow status: ${readableStatus}.`,
      documentRecord,
    );

    if (!terminalStatuses.has(documentRecord.status) && attempt < 30) {
      window.setTimeout(() => pollStatus(documentId, attempt + 1), 1000);
    }
  };

  const pollBatchRow = async (documentId, rowEl, attempt = 0) => {
    const response = await fetch(`/documents/${documentId}`, {
      headers: authHeaders(),
    });
    if (!response.ok) {
      return;
    }

    const documentRecord = await response.json();
    const failed = documentRecord.status === "failed";
    rowEl.classList.toggle("error", failed);
    rowEl.querySelector(".batch-result-status").textContent = documentRecord.status.replaceAll(
      "_",
      " ",
    );

    if (!terminalStatuses.has(documentRecord.status) && attempt < 30) {
      window.setTimeout(() => pollBatchRow(documentId, rowEl, attempt + 1), 1000);
    }
  };

  const renderBatchResults = (items) => {
    batchResultList.innerHTML = "";
    batchResultList.hidden = false;
    items.forEach((item) => {
      const row = document.createElement("li");
      row.className = `batch-result-row ${item.accepted ? "accepted" : "error"}`;

      const name = document.createElement("span");
      name.className = "batch-result-name";
      name.textContent = item.filename;

      const statusText = document.createElement("span");
      statusText.className = "batch-result-status";
      statusText.textContent = item.accepted
        ? (item.document?.status || "queued").replaceAll("_", " ")
        : `rejected: ${item.detail || "submission failed"}`;

      row.append(name, statusText);
      batchResultList.appendChild(row);

      if (item.accepted && item.document?.id) {
        pollBatchRow(item.document.id, row);
      }
    });
  };

  const clearForm = () => {
    form.reset();
    selectedFiles = [];
    setFileError();
    renderSelectedFiles();
    updateVersionAvailability();
    result.hidden = true;
    batchResultList.hidden = true;
    batchResultList.innerHTML = "";
    updateReadiness();
  };

  document.getElementById("clearForm").addEventListener("click", clearForm);
  document.getElementById("mobileClearForm").addEventListener("click", clearForm);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setFileError();

    if (!selectedFiles.length) {
      setFileError("Choose one or more documents before submitting.");
      dropZone.scrollIntoView({ behavior: "smooth", block: "center" });
      dropZone.focus();
      updateReadiness();
      return;
    }

    if (!form.reportValidity()) {
      return;
    }

    const isBatch = selectedFiles.length > 1;
    const formData = new FormData();
    if (isBatch) {
      selectedFiles.forEach((file) => formData.append("files", file));
    } else {
      formData.append("file", selectedFiles[0]);
    }
    formData.append("classification", form.classification.value);
    formData.append(
      "releasability",
      JSON.stringify(
        releaseInputs.filter((input) => input.checked).map((input) => input.value),
      ),
    );
    formData.append(
      "access_scope",
      JSON.stringify(
        form.access_scope.value
          .split(",")
          .map((value) => value.trim())
          .filter(Boolean),
      ),
    );
    formData.append("source_originator", form.source_originator.value.trim());
    formData.append("doc_type", form.doc_type.value.trim());

    if (form.program_community.value.trim()) {
      formData.append("program_community", form.program_community.value.trim());
    }
    if (form.effective_date.value) {
      formData.append("effective_date", form.effective_date.value);
    }
    if (!isBatch && form.supersedes_document_id.value.trim()) {
      formData.append(
        "supersedes_document_id",
        form.supersedes_document_id.value.trim(),
      );
    }

    submitControls.forEach((control) => {
      control.disabled = true;
    });
    submitButton.classList.add("loading");
    submitButton.querySelector("span").textContent = "Submitting…";
    batchResultList.hidden = true;
    batchResultList.innerHTML = "";
    setResult(
      "pending",
      isBatch ? "Submitting batch" : "Submitting document",
      "Validating metadata and securely queuing the file(s).",
    );

    try {
      const response = await fetch(isBatch ? "/documents/batch" : "/documents", {
        method: "POST",
        headers: authHeaders(),
        body: formData,
      });
      const body = await response.json().catch(() => ({
        detail: `Request failed with status ${response.status}`,
      }));

      if (!response.ok) {
        setResult(
          "error",
          "Submission could not be completed",
          body.detail || "Review the fields and try again.",
          body,
        );
        return;
      }

      if (isBatch) {
        const acceptedCount = body.filter((item) => item.accepted).length;
        const kind =
          acceptedCount === body.length ? "success" : acceptedCount === 0 ? "error" : "pending";
        setResult(
          kind,
          `${acceptedCount} of ${body.length} documents accepted`,
          "Each document is queued independently and will move to curator review when ready.",
          body,
        );
        renderBatchResults(body);
      } else {
        setResult(
          "success",
          "Document accepted",
          "The document is queued for processing and will move to curator review when ready.",
          body,
        );
        pollStatus(body.id);
      }

      form.reset();
      selectedFiles = [];
      renderSelectedFiles();
      updateVersionAvailability();
      updateReadiness();
    } catch (error) {
      setResult(
        "error",
        "Connection problem",
        "The portal could not reach the submission service. Try again.",
        { error: String(error) },
      );
    } finally {
      submitControls.forEach((control) => {
        control.disabled = false;
      });
      submitButton.classList.remove("loading");
      submitButton.querySelector("span").textContent = "Submit";
    }
  });

  updateReadiness();
})();
