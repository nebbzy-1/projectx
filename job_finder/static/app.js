(() => {
  const statusBox = document.getElementById("searchStatus");
  if (!statusBox) return;

  const initialEvent = Number(document.body.dataset.eventId || 0);
  let lastSeenEvent = initialEvent;
  const handledPreviews = new Set();
  const message = document.getElementById("statusMessage");
  const details = document.getElementById("statusDetails");
  const progressWrap = document.getElementById("progressWrap");
  const progressFill = document.getElementById("progressFill");
  const progressLabel = document.getElementById("progressLabel");
  const retryForm = document.getElementById("retryForm");
  const resetButtons = document.querySelectorAll(".reset-option");
  const checkListingsButton = document.getElementById("checkListingsButton");

  function updateStatus(state) {
    statusBox.dataset.task = state.task;
    statusBox.classList.toggle("error", Boolean(state.error));
    statusBox.classList.toggle("visible", !["idle", "done"].includes(state.task) || state.message !== "Your saved jobs are ready.");
    message.textContent = state.message || "";
    details.replaceChildren();
    (state.details || []).forEach((item) => {
      const line = document.createElement("li");
      line.textContent = item;
      details.appendChild(line);
    });

    const progress = state.progress;
    progressWrap.hidden = !progress;
    if (progress) {
      const percent = progress.total ? Math.round((progress.done / progress.total) * 100) : 0;
      progressFill.style.width = `${percent}%`;
      progressLabel.textContent = progress.label || `${progress.done} of ${progress.total}`;
    }
    if (retryForm) retryForm.hidden = !state.can_retry_seed;
    resetButtons.forEach((button) => { button.disabled = Boolean(state.busy); });
    if (checkListingsButton) checkListingsButton.disabled = Boolean(state.busy);

    if (state.task === "confirmation" && state.preview_token && !handledPreviews.has(state.preview_token)) {
      handledPreviews.add(state.preview_token);
      const prompt = `This refresh found ${state.preview_count} jobs to show. Latest will contain ${state.total_after} jobs.\n\n` +
        `Latest keeps up to 30. If it goes over 30, the oldest jobs are removed from Latest only; they stay in Backlog. Continue?`;
      const endpoint = window.confirm(prompt) ? "/api/latest/confirm" : "/api/latest/cancel";
      fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: state.preview_token }),
      }).then(async (response) => {
        const result = await response.json();
        if (!response.ok) throw new Error(result.message || "Could not finish the refresh.");
        message.textContent = result.message;
      }).catch((error) => {
        statusBox.classList.add("error");
        message.textContent = error.message;
      });
    }

    const eventId = Number(state.event_id || 0);
    if ((state.task === "done" || state.task === "error") && eventId > lastSeenEvent) {
      lastSeenEvent = eventId;
      window.location.reload();
    } else if (eventId > lastSeenEvent) {
      lastSeenEvent = eventId;
    }
  }

  async function poll() {
    try {
      const response = await fetch("/api/search-state", { headers: { Accept: "application/json" } });
      if (response.ok) updateStatus(await response.json());
    } catch (_error) {
      message.textContent = "The local app server could not be reached. Check the terminal where Job Finder is running.";
      statusBox.classList.add("error");
    }
  }

  const query = document.getElementById("jobQuery");
  const location = document.getElementById("locationFilter");
  const filters = document.getElementById("filterForm");
  if (query && filters) {
    let timer;
    query.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => filters.requestSubmit(), 350);
    });
  }
  if (location && filters) location.addEventListener("change", () => filters.requestSubmit());
  document.querySelectorAll(".job-description").forEach((description) => {
    const card = description.closest(".job-card");
    const syncOpenState = () => card?.classList.toggle("description-open", description.open);
    description.addEventListener("toggle", syncOpenState);
    syncOpenState();
  });

  poll();
  window.setInterval(poll, 1400);
})();
