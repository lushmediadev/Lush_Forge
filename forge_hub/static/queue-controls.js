(() => {
  if (window.__lushForgeQueueControlsLoaded) return;
  window.__lushForgeQueueControlsLoaded = true;

  const pending = { txt2img: 0, img2img: 0, extras: 0 };
  const activeByTab = { txt2img: 0, img2img: 0, extras: 0 };
  const nativeProgressByTab = new Map();
  const restoredProgressAt = new Map();
  const submittedTaskIds = { txt2img: [], img2img: [], extras: [] };
  let accountActiveCount = 0;
  let nativeRequestProgress = null;
  const submitFunctions = [
    ["submit", "txt2img"],
    ["submit_img2img", "img2img"],
    ["submit_extras", "extras"],
  ];

  function hasPending(tab) {
    return Object.prototype.hasOwnProperty.call(pending, tab);
  }

  function hasActiveJobs(tab) {
    return hasPending(tab) && activeByTab[tab] > 0;
  }

  function taskMarker(tab) {
    return `lush_forge_current_task_${tab}`;
  }

  function rememberSubmittedTask(tab, result) {
    const taskId = Array.isArray(result) && typeof result[0] === "string"
      ? result[0]
      : localStorage.getItem(`${tab}_task_id`);
    if (!taskId || !taskId.startsWith("task(")) return;
    submittedTaskIds[tab].push(taskId);
    localStorage.setItem(taskMarker(tab), taskId);
  }

  function completeSubmittedTask(tab, taskId = null) {
    const queue = submittedTaskIds[tab];
    if (queue.length && (!taskId || queue[0] === taskId)) queue.shift();
    const next = queue[0] || null;
    if (next) localStorage.setItem(taskMarker(tab), next);
    else localStorage.removeItem(taskMarker(tab));
  }

  function showNativeControls(tab, active) {
    const generate = document.getElementById(`${tab}_generate`);
    const interrupt = document.getElementById(`${tab}_interrupt`);
    const skip = document.getElementById(`${tab}_skip`);
    const interrupting = document.getElementById(`${tab}_interrupting`);
    if (generate) generate.style.setProperty("display", "block", "important");
    if (skip) skip.style.setProperty("display", active ? "block" : "none", "important");

    if (!active) {
      if (interrupt) interrupt.style.setProperty("display", "none", "important");
      if (interrupting) interrupting.style.setProperty("display", "none", "important");
      return;
    }

    const interruptingVisible = interrupting && getComputedStyle(interrupting).display !== "none";
    if (interrupt && !interruptingVisible) interrupt.style.setProperty("display", "block", "important");
  }

  function updateLayout(tab) {
    const box = document.getElementById(`${tab}_generate_box`);
    const active = hasActiveJobs(tab) || (hasPending(tab) && pending[tab] > 0);
    if (box) box.classList.toggle("lush-queue-active", active);
  }

  function updateGenerateCount(tab) {
    const button = document.getElementById(`${tab}_generate`);
    if (!button) return;
    let badge = button.querySelector(".lush-generate-queue-count");
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "lush-generate-queue-count";
      badge.setAttribute("aria-hidden", "true");
      button.append(badge);
    }
    badge.textContent = String(accountActiveCount);
    badge.hidden = accountActiveCount < 1;
    badge.title = accountActiveCount
      ? `${accountActiveCount} job đang chạy hoặc chờ trong hàng đợi`
      : "";
    button.setAttribute(
      "aria-label",
      accountActiveCount
        ? `Generate, ${accountActiveCount} job đang chạy hoặc chờ`
        : "Generate",
    );
  }

  function syncControls(tab) {
    const active = hasActiveJobs(tab) || (hasPending(tab) && pending[tab] > 0);
    updateLayout(tab);
    showNativeControls(tab, active);
    if (tab !== "extras") updateGenerateCount(tab);
  }

  function setActiveJobs(state) {
    const total = Number(state?.activeCount ?? 0);
    accountActiveCount = Number.isFinite(total) ? Math.max(0, Math.trunc(total)) : 0;
    const byTab = state?.byTab;
    for (const tab of Object.keys(activeByTab)) {
      const count = Number(byTab?.[tab] ?? 0);
      activeByTab[tab] = Number.isFinite(count) ? Math.max(0, Math.trunc(count)) : 0;
    }
    for (const tab of Object.keys(pending)) syncControls(tab);
  }

  function installShowSubmitButtons() {
    const original = window.showSubmitButtons;
    if (typeof original !== "function") return false;
    if (original.__lushQueueWrapped) {
      return true;
    }

    const wrapped = function (tab, show) {
      if (show && hasPending(tab) && pending[tab] > 0) {
        pending[tab] -= 1;
        completeSubmittedTask(tab);
      }
      const result = original.call(this, tab, show);
      if (hasPending(tab)) syncControls(tab);
      return result;
    };
    wrapped.__lushQueueWrapped = true;
    wrapped.__lushQueueOriginal = original;
    window.showSubmitButtons = wrapped;
    return true;
  }

  function progressTab(progressbarContainer) {
    const id = progressbarContainer?.id || "";
    if (id.startsWith("img2img")) return "img2img";
    if (id.startsWith("extras")) return "extras";
    return "txt2img";
  }

  function startNativeProgress(tab, entry) {
    const state = nativeProgressByTab.get(tab);
    if (!state || state.running !== entry) return;
    state.launchGeneration = (state.launchGeneration || 0) + 1;
    const launchGeneration = state.launchGeneration;
    let ended = false;
    const originalAtEnd = entry.args[3];
    const atEnd = (...args) => {
      if (ended) return;
      ended = true;
      try {
        if (typeof originalAtEnd === "function") originalAtEnd(...args);
      } finally {
        if (state.launchGeneration !== launchGeneration) return;
        state.running = null;
        startNextNativeProgress(tab);
      }
    };
    const args = [...entry.args];
    args[3] = atEnd;
    try {
      nativeRequestProgress.apply(window, args);
    } catch (error) {
      atEnd();
      throw error;
    }
  }

  function startNextNativeProgress(tab) {
    const state = nativeProgressByTab.get(tab);
    if (!state || state.running || state.waiting.length === 0) return;
    state.running = state.waiting.shift();
    startNativeProgress(tab, state.running);
  }

  function installRequestProgress() {
    const original = window.requestProgress;
    if (typeof original !== "function") return false;
    if (original.__lushQueueWrapped) {
      nativeRequestProgress = original.__lushQueueOriginal || nativeRequestProgress;
      return true;
    }
    nativeRequestProgress = original;
    const wrapped = function (idTask, progressbarContainer, gallery, atEnd, ...rest) {
      const tab = progressTab(progressbarContainer);
      let state = nativeProgressByTab.get(tab);
      if (!state) {
        state = { running: null, waiting: [] };
        nativeProgressByTab.set(tab, state);
      }
      const entry = { args: [idTask, progressbarContainer, gallery, atEnd, ...rest] };
      if (state.running) state.waiting.push(entry);
      else {
        state.running = entry;
        startNativeProgress(tab, entry);
      }
    };
    wrapped.__lushQueueWrapped = true;
    wrapped.__lushQueueOriginal = original;
    window.requestProgress = wrapped;
    return true;
  }

  function hasNativeProgressElement(tab) {
    const container = document.getElementById(`${tab}_gallery_container`);
    return Boolean(container?.parentElement?.querySelector(".progressDiv"));
  }

  async function restoreNativeProgress(tab) {
    if (!nativeRequestProgress || hasNativeProgressElement(tab)) return;
    const state = nativeProgressByTab.get(tab);
    // The native requestProgress loop cannot be cancelled safely. If it is
    // still alive while Chrome has temporarily removed its DOM, starting a
    // second loop would make both loops mutate the same Gallery.
    if (state?.running || state?.waiting?.length) return;
    const trackedTaskId = state?.running?.args?.[0];
    let taskId = trackedTaskId || localStorage.getItem(taskMarker(tab)) || localStorage.getItem(`${tab}_task_id`);
    const container = document.getElementById(`${tab}_gallery_container`);
    const gallery = document.getElementById(`${tab}_gallery`);
    if (!container || !gallery) return;
    const now = Date.now();
    if (now - (restoredProgressAt.get(tab) || 0) < 1500) return;
    restoredProgressAt.set(tab, now);

    try {
      const response = await fetch("/hub/api/jobs?limit=100", { cache: "no-store" });
      if (response.ok) {
        const jobs = await response.json();
        const matching = jobs
          .filter((job) => (job.kind === tab) && (job.status === "running" || job.status === "queued"))
          .sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
        taskId = matching.find((job) => job.status === "running")?.task_id
          || matching[0]?.task_id
          || taskId;
      }
    } catch (_) {
      // Fall back to Forge's own task marker if the Hub API is temporarily slow.
    }
    if (!taskId) return;

    nativeRequestProgress.call(
      window,
      taskId,
      container,
      gallery,
      () => {
        if (localStorage.getItem(taskMarker(tab)) === taskId) localStorage.removeItem(taskMarker(tab));
        if (localStorage.getItem(`${tab}_task_id`) === taskId) localStorage.removeItem(`${tab}_task_id`);
        syncControls(tab);
      },
      null,
      0,
    );
  }

  async function authoritativeTask(tab) {
    if (!nativeRequestProgress || tab === "extras") return null;
    try {
      const response = await fetch("/hub/api/jobs?limit=100", { cache: "no-store" });
      if (!response.ok) return null;
      const jobs = await response.json();
      const candidates = jobs
        .filter((job) => job.kind === tab && (job.status === "running" || job.status === "queued"))
        .sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
      for (const job of candidates) {
        try {
          const progressResponse = await fetch("/internal/progress", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id_task: job.task_id, live_preview: false }),
            cache: "no-store",
          });
          if (progressResponse.ok && (await progressResponse.json()).active) return job;
        } catch (_) {
          // Keep checking the next candidate; the native loop remains usable.
        }
      }
      return candidates[0] || null;
    } catch (_) {
      return null;
    }
  }

  async function reconcileNativeProgress(tab) {
    if (document.visibilityState !== "visible") return;
    const target = await authoritativeTask(tab);
    if (!target) return;
    let state = nativeProgressByTab.get(tab);
    if (!state) {
      state = { running: null, waiting: [], launchGeneration: 0 };
      nativeProgressByTab.set(tab, state);
    }
    const currentTaskId = state.running?.args?.[0];
    // Never supersede a live native loop just because its progress element
    // was briefly rerendered or hidden. Forge's requestProgress has no public
    // cancellation handle, so a replacement would race the old loop over the
    // shared Gallery and can restore a stale image.
    if (state.running || state.waiting.length) return;
    if (currentTaskId === target.task_id) return;

    const container = document.getElementById(`${tab}_gallery_container`);
    const gallery = document.getElementById(`${tab}_gallery`);
    if (!container || !gallery) return;
    const entry = {
      args: [target.task_id, container, gallery, () => {
        if (localStorage.getItem(taskMarker(tab)) === target.task_id) localStorage.removeItem(taskMarker(tab));
        syncControls(tab);
      }, null, 0],
    };
    state.running = entry;
    startNativeProgress(tab, entry);
  }

  function restoreVisibleProgress() {
    if (document.visibilityState !== "visible") return;
    window.setTimeout(() => {
      for (const tab of ["txt2img", "img2img"]) {
        restoreNativeProgress(tab);
        reconcileNativeProgress(tab);
      }
    }, 120);
  }

  function installSubmit(name, tab) {
    const original = window[name];
    if (typeof original !== "function") return false;
    if (original.__lushQueueWrapped) return true;

    const wrapped = function (...args) {
      pending[tab] += 1;
      updateLayout(tab);
      try {
        const result = original.apply(this, args);
        rememberSubmittedTask(tab, result);
        return result;
      } catch (error) {
        pending[tab] = Math.max(0, pending[tab] - 1);
        updateLayout(tab);
        throw error;
      }
    };
    wrapped.__lushQueueWrapped = true;
    wrapped.__lushQueueOriginal = original;
    window[name] = wrapped;
    return true;
  }

  window.addEventListener("lush-forge-queue-state", (event) => {
    setActiveJobs(event.detail);
  });
  if (window.__lushForgeQueueState) {
    setActiveJobs(window.__lushForgeQueueState);
  }
  document.addEventListener("visibilitychange", restoreVisibleProgress);
  window.addEventListener("pageshow", restoreVisibleProgress);

  let attempts = 0;
  const timer = window.setInterval(() => {
    attempts += 1;
    const showReady = installShowSubmitButtons();
    const progressReady = installRequestProgress();
    const ready = submitFunctions.map(([name, tab]) => installSubmit(name, tab));
    const primaryReady = showReady && progressReady && ready[0] && ready[1];
    if (primaryReady || attempts >= 600) {
      window.clearInterval(timer);
      if (!primaryReady) {
        console.warn("Lush Forge Queue controls could not attach to the active Forge UI");
      }
      for (const tab of Object.keys(pending)) syncControls(tab);
      restoreVisibleProgress();
      window.setInterval(() => {
        if (document.visibilityState !== "visible" || accountActiveCount < 1) return;
        for (const tab of ["txt2img", "img2img"]) reconcileNativeProgress(tab);
      }, 2500);
    }
  }, 50);
})();
