(function () {
  "use strict";
  if (window.__lushLoraUploadLoaded) return;
  window.__lushLoraUploadLoaded = true;

  const SEARCH_IDS = [
    "txt2img_lora_extra_search",
    "img2img_lora_extra_search",
  ];
  const activeUploads = new WeakMap();
  const resetTimers = new WeakMap();
  const pendingCancellations = new Set();
  let allowedNames = new Set();
  let displayNames = new Map();
  let manifestReady = false;

  function setStatus(button, message, isError = false) {
    const label = button.querySelector(".lush-lora-upload-label");
    if (label) label.textContent = message;
    button.dataset.error = String(isError);
    button.title = isError ? message : "Tải LoRA lên máy Forge của tài khoản";
  }

  function finish(button, cancel, message, isError = false) {
    const timer = resetTimers.get(button);
    if (timer) window.clearTimeout(timer);
    pendingCancellations.delete(button);
    activeUploads.delete(button);
    button.disabled = false;
    button.dataset.uploading = "false";
    button.style.removeProperty("--upload-progress");
    cancel.hidden = true;
    cancel.disabled = false;
    setStatus(button, message, isError);
    resetTimers.set(button, window.setTimeout(() => setStatus(button, "Upload LoRA"), 4800));
  }

  async function cancelUpload(button, cancel) {
    const active = activeUploads.get(button);
    if (!active || active.cancelInFlight) return;
    active.cancelled = true;
    active.cancelInFlight = true;
    cancel.disabled = true;
    setStatus(button, "Đang hủy…");
    active.xhr.abort();
    try {
      const response = await fetch(`/hub/api/lora/uploads/${active.id}/cancel`, {
        method: "POST", credentials: "same-origin",
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok || !result.cancelled) throw new Error(result.detail || "Không xác nhận được lệnh hủy");
      finish(button, cancel, "Đã hủy upload");
    } catch (error) {
      active.cancelInFlight = false;
      cancel.disabled = false;
      pendingCancellations.add(button);
      setStatus(button, "Chưa hủy · nhấn × thử lại", true);
    }
  }

  window.addEventListener("online", () => {
    for (const button of pendingCancellations) {
      const cancel = button.nextElementSibling;
      if (cancel?.classList.contains("lush-lora-cancel")) cancelUpload(button, cancel);
    }
  });

  function upload(button, cancel, input) {
    const file = input.files && input.files[0];
    input.value = "";
    if (!file) return;

    const suffix = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
    if (![".safetensors", ".ckpt", ".pt"].includes(suffix)) {
      finish(button, cancel, "Sai định dạng", true);
      return;
    }
    if (!file.size || file.size > 2 * 1024 * 1024 * 1024) {
      finish(button, cancel, "File trống hoặc quá 2 GB", true);
      return;
    }

    const timer = resetTimers.get(button);
    if (timer) window.clearTimeout(timer);
    const id = crypto.randomUUID();
    const xhr = new XMLHttpRequest();
    const active = { id, xhr, cancelled: false, cancelInFlight: false };
    activeUploads.set(button, active);
    button.disabled = true;
    button.dataset.uploading = "true";
    button.style.setProperty("--upload-progress", "0%");
    cancel.hidden = false;
    setStatus(button, "Đang tải 0%");
    xhr.open("POST", `/hub/api/lora/uploads/${id}?filename=${encodeURIComponent(file.name)}`);
    xhr.upload.onprogress = (event) => {
      if (active.cancelled || !event.lengthComputable) return;
      const percent = Math.min(100, Math.round(event.loaded / event.total * 100));
      button.style.setProperty("--upload-progress", `${percent}%`);
      setStatus(button, percent === 100 ? "Đang lưu 100%" : `Đang tải ${percent}%`);
    };
    xhr.onload = async () => {
      if (active.cancelled) return;
      let result = {};
      try { result = JSON.parse(xhr.responseText); } catch (_) { /* Show the HTTP error below. */ }
      if (xhr.status < 200 || xhr.status >= 300 || !result.ok) {
        if (xhr.status >= 500 || xhr.status === 0) {
          cancelUpload(button, cancel);
          return;
        }
        finish(button, cancel, result.detail || "Không tải được LoRA", true);
        return;
      }
      try {
        const refreshResponse = await fetch("/sdapi/v1/refresh-loras", { method: "POST", credentials: "same-origin" });
        if (active.cancelled) return;
        if (!refreshResponse.ok) throw new Error("Đã tải lên; hãy làm mới tab Lora");
        document.querySelectorAll('[id$="_lora_extra_refresh"]').forEach((refresh) => refresh.click());
        await refreshManifest();
        finish(button, cancel, "Đã tải lên");
      } catch (error) {
        if (active.cancelled) return;
        finish(button, cancel, error.message || "Hãy làm mới tab Lora", true);
      }
    };
    xhr.onerror = () => {
      if (active.cancelled) return;
      cancelUpload(button, cancel);
    };
    xhr.send(file);
  }

  function scopeLoraCards() {
    document.querySelectorAll("#txt2img_lora_pane .card[data-name], #img2img_lora_pane .card[data-name]").forEach((card) => {
      const name = card.dataset.name || "";
      const privateLora = name.startsWith("__lush_owner_");
      const allowed = manifestReady ? allowedNames.has(name) : !privateLora;
      card.classList.toggle("lush-lora-hidden", !allowed);
      card.classList.toggle("lush-lora-allowed", allowed && privateLora);
      const label = card.querySelector(".name");
      const displayName = displayNames.get(name);
      if (label && displayName && label.textContent !== displayName) label.textContent = displayName;
    });
  }

  async function refreshManifest() {
    try {
      const response = await fetch("/hub/api/lora/manifest", { cache: "no-store" });
      if (!response.ok) throw new Error("Không tải được danh sách LoRA theo tài khoản");
      const payload = await response.json();
      allowedNames = new Set();
      displayNames = new Map();
      for (const item of payload.items || []) {
        if (!item || typeof item.name !== "string") continue;
        allowedNames.add(item.name);
        if (typeof item.display_name === "string") displayNames.set(item.name, item.display_name);
        for (const alias of item.aliases || []) if (typeof alias === "string") allowedNames.add(alias);
      }
      manifestReady = true;
      scopeLoraCards();
    } catch (_) {
      manifestReady = false;
      scopeLoraCards();
    }
  }

  function attach(search) {
    const searchWrapper = search && search.parentElement;
    const toolbar = searchWrapper && searchWrapper.parentElement;
    if (!searchWrapper || !toolbar || toolbar.querySelector(".lush-lora-upload")) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "lush-lora-upload";
    button.title = "Tải LoRA lên máy Forge của tài khoản";
    const icon = document.createElement("span");
    icon.className = "lush-lora-upload-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.innerHTML = '<svg viewBox="0 0 24 24" focusable="false"><path d="M12 16V4m0 0L7 9m5-5 5 5M5 14v5h14v-5"/></svg>';
    const label = document.createElement("span");
    label.className = "lush-lora-upload-label";
    label.textContent = "Upload LoRA";
    button.append(icon, label);

    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "lush-lora-cancel";
    cancel.textContent = "×";
    cancel.title = "Hủy upload LoRA";
    cancel.setAttribute("aria-label", "Hủy upload LoRA");
    cancel.hidden = true;
    cancel.addEventListener("click", () => cancelUpload(button, cancel));

    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".safetensors,.ckpt,.pt";
    input.hidden = true;
    input.addEventListener("change", () => upload(button, cancel, input));
    button.addEventListener("click", () => input.click());

    // Forge owns the search wrapper's absolute magnifying-glass icon. Keep
    // the upload action beside that wrapper, never inside it.
    toolbar.insertBefore(button, searchWrapper);
    toolbar.insertBefore(cancel, searchWrapper);
    toolbar.appendChild(input);
  }

  function attachAll() {
    SEARCH_IDS.forEach((id) => attach(document.getElementById(id)));
    scopeLoraCards();
  }

  function boot() {
    attachAll();
    const observer = new MutationObserver(attachAll);
    observer.observe(document.body, { childList: true, subtree: true });
    refreshManifest();
    document.addEventListener("click", (event) => {
      const refresh = event.target.closest('[id$="_lora_extra_refresh"]');
      if (refresh) window.setTimeout(refreshManifest, 500);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot, { once: true });
  else boot();
})();
