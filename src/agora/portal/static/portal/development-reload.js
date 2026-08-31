(() => {
  "use strict";

  const script = document.currentScript;
  if (!(script instanceof HTMLScriptElement)) {
    return;
  }

  const requestMethod = script.dataset.reloadRequestMethod;
  const reloadUrl = script.dataset.reloadUrl;
  let currentVersion = script.dataset.reloadVersion;
  let serverWasUnavailable = false;
  let stopped = false;

  // Reloading a POST response can replay a mutation. Only idempotent page navigations may
  // participate in source-driven browser refresh.
  if (requestMethod !== "GET" || !reloadUrl || !currentVersion) {
    return;
  }

  window.addEventListener(
    "pagehide",
    () => {
      stopped = true;
    },
    { once: true },
  );

  const poll = async () => {
    try {
      const response = await fetch(reloadUrl, {
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "text/plain" },
      });
      if (response.ok) {
        const nextVersion = (await response.text()).trim();
        if (serverWasUnavailable || nextVersion !== currentVersion) {
          window.location.reload();
          return;
        }
        currentVersion = nextVersion;
        serverWasUnavailable = false;
      }
    } catch {
      serverWasUnavailable = true;
    }

    if (!stopped) {
      window.setTimeout(poll, 600);
    }
  };

  window.setTimeout(poll, 600);
})();
