(() => {
  "use strict";

  const transferContainsFiles = (transfer) =>
    transfer &&
    (Array.from(transfer.types || []).includes("Files") || transfer.files.length > 0);
  const preventNativeFileOpen = (event) => {
    if (transferContainsFiles(event.dataTransfer)) event.preventDefault();
  };

  // A file dropped outside the widget must never replace the Agora page or render locally.
  document.addEventListener("dragover", preventNativeFileOpen, true);
  document.addEventListener("drop", preventNativeFileOpen, true);

  const widget = document.querySelector("[data-upload-widget]");
  if (!(widget instanceof HTMLElement)) {
    return;
  }

  const dropzone = widget.querySelector("[data-upload-dropzone]");
  const input = widget.querySelector("[data-upload-input]");
  const summary = widget.querySelector("[data-upload-summary]");
  const announcement = widget.querySelector("[data-upload-announcement]");
  const queueSection = widget.querySelector("[data-upload-queue]");
  const list = widget.querySelector("[data-upload-list]");
  const clearButton = widget.querySelector("[data-upload-clear]");
  const form = widget.closest("form");
  if (
    !(dropzone instanceof HTMLElement) ||
    !(input instanceof HTMLInputElement) ||
    !(summary instanceof HTMLElement) ||
    !(announcement instanceof HTMLElement) ||
    !(queueSection instanceof HTMLElement) ||
    !(list instanceof HTMLUListElement) ||
    !(clearButton instanceof HTMLButtonElement) ||
    !(form instanceof HTMLFormElement)
  ) {
    return;
  }
  widget.dataset.uploadReady = "true";

  const queuedFiles = new Map();
  const numberFormatter = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });

  const logicalKey = (name) => {
    try {
      return name.normalize("NFKC").toLowerCase();
    } catch {
      return name.toLowerCase();
    }
  };

  const extensionFor = (name) => {
    const dot = name.lastIndexOf(".");
    return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
  };

  const kindFor = (name) => {
    const extension = extensionFor(name);
    if (extension === "html") return "HTML entry point";
    if (extension === "csv") return "CSV data";
    if (extension === "css") return "Stylesheet";
    if (["png", "jpg", "jpeg", "gif", "webp"].includes(extension)) return "Image";
    if (["woff", "woff2"].includes(extension)) return "Web font";
    return "Unsupported file";
  };

  const formatBytes = (bytes) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${numberFormatter.format(bytes / 1024)} KB`;
    return `${numberFormatter.format(bytes / (1024 * 1024))} MB`;
  };

  const synchronizeInput = () => {
    if (typeof DataTransfer === "undefined") return;
    const transfer = new DataTransfer();
    for (const file of queuedFiles.values()) {
      transfer.items.add(file);
    }
    input.files = transfer.files;
  };

  const describeNames = (names) => {
    const visibleNames = names.slice(0, 3).join(", ");
    const remainder = names.length - 3;
    return remainder > 0 ? `${visibleNames}, and ${remainder} more` : visibleNames;
  };

  const announceChanges = (additions, replacements) => {
    const messages = [];
    if (additions.length > 0) {
      messages.push(
        `${additions.length} new ${additions.length === 1 ? "file" : "files"} added to the upload queue.`,
      );
    }
    if (replacements.length > 0) {
      messages.push(
        `${describeNames(replacements)} replaced the queued ${replacements.length === 1 ? "file" : "files"} with the same ${replacements.length === 1 ? "name" : "names"}.`,
      );
    }
    announcement.textContent = messages.join(" ");
  };

  const renderQueue = () => {
    list.replaceChildren();
    let totalBytes = 0;
    let htmlCount = 0;

    for (const [key, file] of queuedFiles) {
      totalBytes += file.size;
      if (extensionFor(file.name) === "html") htmlCount += 1;

      const item = document.createElement("li");
      item.className = "portal-upload-queue__item";

      const details = document.createElement("div");
      details.className = "portal-upload-queue__details";
      const filename = document.createElement("strong");
      filename.textContent = file.name;
      const metadata = document.createElement("span");
      metadata.textContent = `${kindFor(file.name)} · ${formatBytes(file.size)}`;
      details.append(filename, metadata);

      const removeButton = document.createElement("button");
      removeButton.className = "portal-button portal-button--quiet portal-upload-queue__remove";
      removeButton.type = "button";
      removeButton.textContent = "Remove";
      removeButton.setAttribute("aria-label", `Remove ${file.name}`);
      removeButton.addEventListener("click", () => {
        queuedFiles.delete(key);
        synchronizeInput();
        renderQueue();
        announcement.textContent = `${file.name} removed from the upload queue.`;
      });

      item.append(details, removeButton);
      list.append(item);
    }

    const count = queuedFiles.size;
    queueSection.hidden = count === 0;
    clearButton.hidden = count === 0;
    if (count === 0) {
      input.setCustomValidity("Choose the files for this dashboard package.");
      summary.textContent = "No files selected.";
      return;
    }

    input.setCustomValidity(
      htmlCount === 1 ? "" : "Choose exactly one HTML entry point for this dashboard package.",
    );
    const htmlMessage =
      htmlCount === 1
        ? "One HTML entry point selected."
        : `Select exactly one HTML entry point; ${htmlCount} selected.`;
    summary.textContent = `${count} ${count === 1 ? "file" : "files"} selected · ${formatBytes(totalBytes)}. ${htmlMessage}`;
  };

  const addFiles = (files) => {
    const additions = [];
    const replacements = [];
    for (const file of files) {
      const key = logicalKey(file.name);
      if (queuedFiles.has(key)) {
        replacements.push(file.name);
      } else {
        additions.push(file.name);
      }
      queuedFiles.set(key, file);
    }
    synchronizeInput();
    renderQueue();
    announceChanges(additions, replacements);
  };

  input.addEventListener("click", () => {
    input.value = "";
  });
  input.addEventListener("change", () => {
    addFiles(Array.from(input.files || []));
  });
  input.addEventListener("cancel", () => {
    synchronizeInput();
  });

  let dragDepth = 0;
  const isFileDrag = (event) => transferContainsFiles(event.dataTransfer);
  const resetDragState = () => {
    dragDepth = 0;
    dropzone.classList.remove("is-dragover");
  };

  dropzone.addEventListener("dragenter", (event) => {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    dragDepth += 1;
    dropzone.classList.add("is-dragover");
  });
  dropzone.addEventListener("dragover", (event) => {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    dropzone.classList.add("is-dragover");
  });
  dropzone.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) resetDragState();
  });
  dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    resetDragState();
    if (event.dataTransfer) addFiles(Array.from(event.dataTransfer.files));
  });

  clearButton.addEventListener("click", () => {
    queuedFiles.clear();
    synchronizeInput();
    renderQueue();
    announcement.textContent = "Upload queue cleared.";
  });
  form.addEventListener("reset", () => {
    window.setTimeout(() => {
      queuedFiles.clear();
      synchronizeInput();
      renderQueue();
    }, 0);
  });

  renderQueue();
})();
