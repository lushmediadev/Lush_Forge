(() => {
  if (window.__lushForgeHistoryLoaded) return;
  window.__lushForgeHistoryLoaded = true;

  // Update the existing asset links while the Hub process is running; the
  // uploaded script and styles can then be replaced without a Forge restart.
  let style = document.querySelector('link[data-lush-lora-upload]');
  if (!style) {
    style = document.createElement("link");
    style.rel = "stylesheet";
    style.dataset.lushLoraUpload = "true";
    document.head.appendChild(style);
  }
  style.href = "/hub/assets/lora-upload.css?v=3";
  let script = document.querySelector('script[data-lush-lora-upload]');
  if (!script) {
    script = document.createElement("script");
    script.defer = true;
    script.dataset.lushLoraUpload = "true";
    document.head.appendChild(script);
  }
  script.src = "/hub/assets/lora-upload.js?v=3";

  const stateNames = { queued: "Đang chờ", running: "Đang tạo", cancelling: "Đang hủy", cancelled: "Đã hủy", done: "Hoàn tất", failed: "Lỗi" };
  const jobs = { all: [], filter: "all", query: "", createdFrom: "", createdTo: "", account: null };
  const progressByTask = new Map();
  const progressRequests = new Set();
  let publishedQueueState = "";
  const node = (tag, className, text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };

  function publishQueueState(activeJobs) {
    const queuedCount = activeJobs.filter((job) => job.status === "queued" || job.status === "cancelling").length;
    const runningCount = activeJobs.filter((job) => job.status === "running").length;
    const byTab = { txt2img: 0, img2img: 0, extras: 0 };
    for (const job of activeJobs) {
      const tab = Object.prototype.hasOwnProperty.call(byTab, job.kind) ? job.kind : "extras";
      byTab[tab] += 1;
    }
    const state = { activeCount: activeJobs.length, queuedCount, runningCount, byTab };
    const taskSignature = activeJobs.map((job) => `${job.task_id}:${job.status}`).sort().join(",");
    const signature = `${state.activeCount}:${queuedCount}:${runningCount}:${JSON.stringify(byTab)}:${taskSignature}`;
    window.__lushForgeQueueState = state;
    if (signature === publishedQueueState) return;
    publishedQueueState = signature;
    window.dispatchEvent(new CustomEvent("lush-forge-queue-state", { detail: state }));
  }

  function urlFor(taskId, action) {
    return `/hub/api/jobs/${encodeURIComponent(taskId)}/${action}`;
  }

  const launcher = node("button", "lush-history-launcher", "Hàng đợi");
  launcher.type = "button";
  launcher.setAttribute("aria-label", "Mở hàng đợi");
  launcher.title = "Hàng đợi và lịch sử";
  launcher.setAttribute("aria-expanded", "false");
  launcher.setAttribute("aria-controls", "lush-history-drawer");
  const launcherBadge = node("span", "lush-history-badge", "");
  launcherBadge.hidden = true;
  launcherBadge.setAttribute("aria-hidden", "true");
  launcher.append(launcherBadge);
  const drawer = node("aside", "lush-history-drawer");
  drawer.id = "lush-history-drawer";
  drawer.hidden = true;
  drawer.setAttribute("aria-label", "Hàng đợi và lịch sử tạo ảnh");
  const header = node("div", "lush-history-header");
  const title = node("h2", "lush-history-title", "Hàng đợi & lịch sử");
  const close = node("button", "", "×");
  close.type = "button";
  close.setAttribute("aria-label", "Đóng lịch sử");
  header.append(title, close);
  const toolbar = node("div", "lush-history-toolbar");
  const search = node("input", "");
  search.type = "search";
  search.placeholder = "Tìm prompt";
  search.setAttribute("aria-label", "Tìm trong lịch sử");
  const filter = node("select", "");
  filter.setAttribute("aria-label", "Lọc trạng thái");
  for (const [value, label] of [["all", "Tất cả"], ["active", "Đang xử lý"], ["done", "Hoàn tất"], ["failed", "Lỗi"], ["cancelled", "Đã hủy"]]) {
    const option = node("option", "", label);
    option.value = value;
    filter.append(option);
  }
  toolbar.append(search, filter);
  const dateFilter = node("div", "lush-history-date-filter");
  const dateFromLabel = node("label", "lush-history-date-field", "Từ ngày");
  const dateFrom = node("input", "");
  dateFrom.type = "date";
  dateFrom.setAttribute("aria-label", "Từ ngày");
  dateFromLabel.append(dateFrom);
  const dateToLabel = node("label", "lush-history-date-field", "Đến ngày");
  const dateTo = node("input", "");
  dateTo.type = "date";
  dateTo.setAttribute("aria-label", "Đến ngày");
  dateToLabel.append(dateTo);
  dateFilter.append(dateFromLabel, dateToLabel);
  const list = node("div", "lush-history-list");
  list.setAttribute("aria-live", "polite");
  const footer = node("div", "lush-history-footer");
  const footerAccount = node("span", "lush-history-footer-account", "Đang tải tài khoản…");
  const logout = node("button", "lush-history-logout");
  logout.type = "button";
  logout.title = "Đăng xuất tài khoản";
  logout.setAttribute("aria-label", "Đăng xuất tài khoản");
  const logoutIcon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  logoutIcon.classList.add("lush-history-logout-icon");
  logoutIcon.setAttribute("viewBox", "0 0 24 24");
  logoutIcon.setAttribute("width", "15");
  logoutIcon.setAttribute("height", "15");
  logoutIcon.setAttribute("fill", "none");
  logoutIcon.setAttribute("stroke", "currentColor");
  logoutIcon.setAttribute("stroke-width", "2");
  logoutIcon.setAttribute("stroke-linecap", "round");
  logoutIcon.setAttribute("stroke-linejoin", "round");
  logoutIcon.setAttribute("aria-hidden", "true");
  const logoutDoor = document.createElementNS("http://www.w3.org/2000/svg", "path");
  logoutDoor.setAttribute("d", "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4");
  const logoutArrow = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  logoutArrow.setAttribute("points", "16 17 21 12 16 7");
  const logoutLine = document.createElementNS("http://www.w3.org/2000/svg", "line");
  logoutLine.setAttribute("x1", "21");
  logoutLine.setAttribute("x2", "9");
  logoutLine.setAttribute("y1", "12");
  logoutLine.setAttribute("y2", "12");
  logoutIcon.append(logoutDoor, logoutArrow, logoutLine);
  logout.append(logoutIcon, document.createTextNode("Đăng xuất"));
  footer.append(footerAccount, logout);
  drawer.append(header, toolbar, dateFilter, list, footer);
  document.body.append(launcher, drawer);

  function setOpen(open) {
    drawer.hidden = !open;
    launcher.setAttribute("aria-expanded", String(open));
    if (open) search.focus();
    else launcher.focus();
  }
  launcher.addEventListener("click", () => setOpen(drawer.hidden));
  close.addEventListener("click", () => setOpen(false));
  logout.addEventListener("click", async () => {
    logout.disabled = true;
    logout.setAttribute("aria-busy", "true");
    footerAccount.textContent = "Đang đăng xuất…";
    try {
      const response = await fetch("/hub/api/logout", { method: "POST", credentials: "same-origin" });
      if (!response.ok) throw new Error("Không thể đăng xuất");
      window.location.assign("/hub/login");
    } catch (cause) {
      logout.disabled = false;
      logout.removeAttribute("aria-busy");
      footerAccount.textContent = cause.message;
    }
  });
  document.addEventListener("click", (event) => {
    if (drawer.hidden || drawer.contains(event.target) || launcher.contains(event.target)) return;
    setOpen(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !drawer.hidden) setOpen(false);
  });
  search.addEventListener("input", () => { jobs.query = search.value.trim().toLocaleLowerCase("vi"); render(); });
  filter.addEventListener("change", () => { jobs.filter = filter.value; render(); });
  dateFrom.addEventListener("change", () => { jobs.createdFrom = dateFrom.value; refresh(); });
  dateTo.addEventListener("change", () => { jobs.createdTo = dateTo.value; refresh(); });

  function timeText(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("vi-VN", { dateStyle: "short", timeStyle: "short" }).format(date);
  }

  function durationText(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) return "";
    const remaining = Math.ceil(seconds);
    if (remaining >= 3600) return `${Math.floor(remaining / 3600)}g ${Math.floor((remaining % 3600) / 60)}p`;
    if (remaining >= 60) return `${Math.floor(remaining / 60)}p ${remaining % 60}s`;
    return `${remaining}s`;
  }

  function progressText(progress) {
    if (!progress || !Number.isFinite(progress.value)) return "Đang khởi tạo…";
    const percent = Math.round(Math.max(0, Math.min(1, progress.value)) * 100);
    const eta = durationText(progress.eta);
    return eta ? `${percent}% · còn ${eta}` : `${percent}%`;
  }

  function makeItem(job) {
    const row = node("article", "lush-history-item");
    row.dataset.taskId = job.task_id;
    const progress = job.status === "running" ? progressByTask.get(job.task_id) : null;
    if (progress?.livePreview || job.has_thumbnail) {
      const image = node("img", "lush-history-thumb");
      image.src = progress?.livePreview || urlFor(job.task_id, "thumbnail");
      image.alt = progress?.livePreview ? "Ảnh xem trước đang tạo" : "Ảnh xem trước kết quả";
      image.loading = "lazy";
      row.append(image);
    } else {
      row.append(node("div", "lush-history-thumb"));
    }
    const main = node("div", "lush-history-item-main");
    const heading = node("div", "lush-history-item-heading");
    const prompt = node("p", "lush-history-prompt", job.prompt || (job.kind === "img2img" ? "Tạo từ ảnh" : "Không có prompt"));
    heading.append(prompt);
    if (job.status === "queued") {
      const cancel = node("button", "lush-history-cancel", "×");
      cancel.type = "button";
      cancel.title = "Hủy job đang chờ";
      cancel.setAttribute("aria-label", `Hủy job đang chờ: ${job.prompt || job.task_id}`);
      cancel.addEventListener("click", async () => {
        cancel.disabled = true;
        try {
          const response = await fetch(urlFor(job.task_id, "cancel"), { method: "POST" });
          const result = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error(result.detail || "Không hủy được job");
          await refresh();
        } catch (cause) {
          await refresh();
          cancel.disabled = false;
          footerAccount.textContent = cause.message;
        }
      });
      heading.append(cancel);
    }
    main.append(heading);
    const meta = node("div", "lush-history-meta");
    const status = node("span", "lush-history-state", stateNames[job.status] || job.status);
    status.dataset.state = job.status;
    meta.append(status, node("span", "", timeText(job.created_at)));
    main.append(meta);
    if (job.status === "running") {
      const progressRow = node("div", "lush-history-progress");
      const track = node("div", "lush-history-progress-track");
      track.setAttribute("role", "progressbar");
      track.setAttribute("aria-label", "Tiến trình tạo ảnh");
      track.setAttribute("aria-valuemin", "0");
      track.setAttribute("aria-valuemax", "100");
      const fill = node("span", "lush-history-progress-fill");
      const value = progress?.value;
      if (Number.isFinite(value)) {
        const percent = Math.round(Math.max(0, Math.min(1, value)) * 100);
        fill.style.width = `${percent}%`;
        track.setAttribute("aria-valuenow", String(percent));
      } else {
        track.removeAttribute("aria-valuenow");
      }
      track.append(fill);
      progressRow.append(track, node("span", "lush-history-progress-label", progressText(progress)));
      main.append(progressRow);
    }
    const actions = node("div", "lush-history-item-actions");
    if (job.has_image) {
      const open = node("a", "", "Xem ảnh");
      open.href = urlFor(job.task_id, "image");
      open.target = "_blank";
      open.rel = "noopener";
      const download = node("a", "", "Tải ảnh");
      download.href = open.href;
      download.download = `lush-forge-${job.task_id.replace(/[^a-z0-9_-]+/gi, "") || "image"}.png`;
      download.setAttribute("aria-label", `Tải ảnh: ${job.prompt || job.task_id}`);
      actions.append(open, download);
    }
    if ((job.status === "failed" || job.status === "cancelled") && job.error_message) {
      main.append(node("p", "lush-history-error", job.error_message));
    }
    if (job.status === "done" || job.status === "failed" || job.status === "cancelled") {
      const remove = node("button", "", "Xóa khỏi lịch sử");
      remove.type = "button";
      remove.addEventListener("click", async () => {
        try {
          const response = await fetch(`/hub/api/jobs/${encodeURIComponent(job.task_id)}`, { method: "DELETE" });
          if (!response.ok) throw new Error("Không xóa được job");
          jobs.all = jobs.all.filter((item) => item.task_id !== job.task_id);
          render();
        } catch (cause) {
          footerAccount.textContent = cause.message;
        }
      });
      actions.append(remove);
    }
    main.append(actions);
    row.append(main);
    return row;
  }

  function render() {
    const activeJobs = jobs.all.filter((job) => job.status === "queued" || job.status === "running" || job.status === "cancelling");
    const active = activeJobs.length;
    publishQueueState(activeJobs);
    launcherBadge.textContent = active > 99 ? "99+" : String(active);
    launcherBadge.hidden = active === 0;
    const activeLabel = `${active} job đang chạy hoặc chờ`;
    launcher.title = active ? `Hàng đợi và lịch sử · ${activeLabel}` : "Hàng đợi và lịch sử";
    launcher.setAttribute("aria-label", active ? `Mở hàng đợi. ${activeLabel}` : "Mở hàng đợi");
    const visible = jobs.all.filter((job) => {
      if (jobs.filter === "active" && !["queued", "running", "cancelling"].includes(job.status)) return false;
      if (jobs.filter === "done" && job.status !== "done") return false;
      if (jobs.filter === "failed" && job.status !== "failed") return false;
      if (jobs.filter === "cancelled" && job.status !== "cancelled") return false;
      return !jobs.query || job.prompt.toLocaleLowerCase("vi").includes(jobs.query);
    });
    list.replaceChildren(...(visible.length ? visible.map(makeItem) : [node("p", "lush-history-empty", "Chưa có job phù hợp.")]));
    if (jobs.account) {
      footerAccount.textContent = `${jobs.account.username} · ${jobs.account.worker_id === "forge1" ? "Máy 1" : "Máy 2"} · Lịch sử dùng chung cho tài khoản này`;
    }
  }

  function updateProgressItem(taskId) {
    const state = progressByTask.get(taskId);
    if (!state) return;
    const row = [...list.querySelectorAll(".lush-history-item")].find((item) => item.dataset.taskId === taskId);
    if (!row) return;
    if (state.livePreview) {
      let image = row.querySelector("img.lush-history-thumb");
      if (!image) {
        image = node("img", "lush-history-thumb");
        image.loading = "lazy";
        row.prepend(image);
      }
      image.src = state.livePreview;
      image.alt = "Ảnh xem trước đang tạo";
    }
    const fill = row.querySelector(".lush-history-progress-fill");
    const track = row.querySelector(".lush-history-progress-track");
    const label = row.querySelector(".lush-history-progress-label");
    if (fill && track && label && Number.isFinite(state.value)) {
      const percent = Math.round(Math.max(0, Math.min(1, state.value)) * 100);
      fill.style.width = `${percent}%`;
      track.setAttribute("aria-valuenow", String(percent));
    }
    if (label) label.textContent = progressText(state);
  }

  async function pollProgress(job) {
    const state = progressByTask.get(job.task_id) || { value: null, eta: null, idLivePreview: -1, livePreview: "" };
    try {
      const response = await fetch("/internal/progress", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id_task: job.task_id,
          id_live_preview: state.idLivePreview,
          live_preview: true,
        }),
        cache: "no-store",
      });
      if (!response.ok) return;
      const current = await response.json();
      state.value = typeof current.progress === "number" ? current.progress : null;
      state.eta = typeof current.eta === "number" ? current.eta : null;
      if (typeof current.id_live_preview === "number" && Number.isFinite(current.id_live_preview)) {
        state.idLivePreview = current.id_live_preview;
      }
      if (typeof current.live_preview === "string" && current.live_preview) {
        state.livePreview = current.live_preview;
      }
      progressByTask.set(job.task_id, state);
      updateProgressItem(job.task_id);
    } catch (_) {
      // The native Forge UI remains usable if the optional queue preview is unavailable.
    }
  }

  function pollActiveProgress() {
    if (drawer.hidden) return;
    const runningJobs = jobs.all
      .filter((job) => job.status === "running")
      .sort((a, b) => Date.parse(b.started_at || b.created_at) - Date.parse(a.started_at || a.created_at))
      .slice(0, 1);
    const runningIds = new Set(runningJobs.map((job) => job.task_id));
    for (const taskId of progressByTask.keys()) {
      if (!runningIds.has(taskId)) progressByTask.delete(taskId);
    }
    for (const job of runningJobs) {
      if (progressRequests.has(job.task_id)) continue;
      progressRequests.add(job.task_id);
      pollProgress(job).finally(() => progressRequests.delete(job.task_id));
    }
  }

  async function refresh() {
    try {
      if (jobs.createdFrom && jobs.createdTo && jobs.createdFrom > jobs.createdTo) {
        throw new Error("Ngày bắt đầu phải trước hoặc trùng ngày kết thúc");
      }
      const params = new URLSearchParams({ limit: jobs.createdFrom || jobs.createdTo ? "1000" : "100" });
      if (jobs.createdFrom) params.set("created_from", new Date(`${jobs.createdFrom}T00:00:00`).toISOString());
      if (jobs.createdTo) {
        const end = new Date(`${jobs.createdTo}T00:00:00`);
        end.setDate(end.getDate() + 1);
        params.set("created_before", end.toISOString());
      }
      const response = await fetch(`/hub/api/jobs?${params}`, { cache: "no-store" });
      if (response.status === 401) {
        window.location.assign("/hub/login");
        return;
      }
      if (!response.ok) throw new Error("Không tải được lịch sử");
      jobs.all = await response.json();
      render();
      pollActiveProgress();
    } catch (cause) {
      footerAccount.textContent = cause.message;
    }
  }

  fetch("/hub/api/me").then((response) => response.json()).then((account) => {
    jobs.account = account;
    render();
  }).catch(() => {});
  refresh();
  window.setInterval(refresh, 3000);
  window.setInterval(pollActiveProgress, 1000);
})();
