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
  const selectedFileCard = document.getElementById("selectedFile");
  const fileError = document.getElementById("fileError");
  const submitButton = document.getElementById("submitButton");
  const submitControls = Array.from(
    document.querySelectorAll(
      '#uploadForm button[type="submit"], button[type="submit"][form="uploadForm"]',
    ),
  );
  const result = document.getElementById("submissionResult");
  let selectedFile = null;

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

  const updateFileCard = () => {
    selectedFileCard.hidden = !selectedFile;
    if (!selectedFile) {
      fileInput.value = "";
      return;
    }
    const extension = extensionFor(selectedFile);
    document.getElementById("fileExtension").textContent =
      (extension || "FILE").toUpperCase().slice(0, 8);
    document.getElementById("fileName").textContent = selectedFile.name;
    document.getElementById("fileMeta").textContent =
      `${formatBytes(selectedFile.size)} · Ready for submission`;
  };

  const chooseFile = (file) => {
    if (!file) {
      return;
    }

    const extension = extensionFor(file);
    if (!supportedExtensions.has(extension)) {
      selectedFile = null;
      updateFileCard();
      setFileError(
        "Unsupported file type. Choose PDF, DOCX, PPTX, XLSX, TXT, Markdown, or HTML.",
      );
      updateReadiness();
      return;
    }

    if (file.size > config.maxUploadBytes) {
      selectedFile = null;
      updateFileCard();
      setFileError("This file exceeds the 50 MB upload limit.");
      updateReadiness();
      return;
    }

    if (file.size === 0) {
      selectedFile = null;
      updateFileCard();
      setFileError("The selected file is empty.");
      updateReadiness();
      return;
    }

    selectedFile = file;
    setFileError();
    updateFileCard();
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
  document.getElementById("replaceFile").addEventListener("click", openFilePicker);
  document.getElementById("removeFile").addEventListener("click", () => {
    selectedFile = null;
    setFileError();
    updateFileCard();
    updateReadiness();
  });

  dropZone.addEventListener("click", openFilePicker);
  dropZone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openFilePicker();
    }
  });
  fileInput.addEventListener("change", () => chooseFile(fileInput.files[0]));

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
    chooseFile(event.dataTransfer?.files?.[0]);
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
    file: () => Boolean(selectedFile),
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

  const clearForm = () => {
    form.reset();
    selectedFile = null;
    setFileError();
    updateFileCard();
    result.hidden = true;
    updateReadiness();
  };

  document.getElementById("clearForm").addEventListener("click", clearForm);
  document.getElementById("mobileClearForm").addEventListener("click", clearForm);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setFileError();

    if (!selectedFile) {
      setFileError("Choose a document before submitting.");
      dropZone.scrollIntoView({ behavior: "smooth", block: "center" });
      dropZone.focus();
      updateReadiness();
      return;
    }

    if (!form.reportValidity()) {
      return;
    }

    const formData = new FormData();
    formData.append("file", selectedFile);
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
    if (form.supersedes_document_id.value.trim()) {
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
    setResult("pending", "Submitting document", "Validating metadata and securely queuing the file.");

    try {
      const response = await fetch("/documents", {
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

      setResult(
        "success",
        "Document accepted",
        "The document is queued for processing and will move to curator review when ready.",
        body,
      );
      form.reset();
      selectedFile = null;
      updateFileCard();
      updateReadiness();
      pollStatus(body.id);
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
