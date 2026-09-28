const accountsBody = document.getElementById("accounts-body");
const accountsMessage = document.getElementById("accounts-message");
const createMessage = document.getElementById("create-message");
const passwordDialog = document.getElementById("password-dialog");
let selectedAccount = null;
let accounts = [];

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (response.status === 401) {
    window.location.assign("/hub/login");
    throw new Error("Phiên đăng nhập đã hết hạn");
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "Không thực hiện được");
  return data;
}

function message(target, value, error = false) {
  target.textContent = value;
  target.dataset.error = String(error);
  target.dataset.visible = String(Boolean(value));
}

function button(label, action) {
  const node = document.createElement("button");
  node.type = "button";
  node.className = "button button-secondary";
  node.textContent = label;
  node.addEventListener("click", action);
  return node;
}

async function update(id, payload) {
  try {
    await api(`/hub/api/accounts/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
    message(accountsMessage, "Đã lưu thay đổi. Các phiên đăng nhập của tài khoản này cần đăng nhập lại.");
    await loadAccounts();
  } catch (cause) {
    message(accountsMessage, cause.message, true);
    await loadAccounts();
  }
}

function renderAccount(account) {
  const row = document.createElement("tr");
  const name = document.createElement("td");
  name.className = "account-name";
  name.textContent = account.username;
  const role = document.createElement("td");
  role.textContent = account.role === "admin" ? "Admin" : "Người dùng";
  const worker = document.createElement("td");
  const select = document.createElement("select");
  select.className = "cell-select";
  for (const [value, title] of [["forge1", "Máy 1"], ["forge2", "Máy 2"]]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = title;
    select.append(option);
  }
  select.value = account.worker_id;
  select.setAttribute("aria-label", `Máy Forge của ${account.username}`);
  select.addEventListener("change", () => update(account.id, { worker_id: select.value }));
  worker.append(select);
  const state = document.createElement("td");
  state.textContent = account.is_active ? "Đang dùng" : "Đã khóa";
  state.className = account.is_active ? "state-on" : "state-off";
  const actions = document.createElement("td");
  actions.className = "cell-actions";
  actions.append(button("Đổi mật khẩu", () => {
    selectedAccount = account;
    document.getElementById("password-account").textContent = account.username;
    document.getElementById("password-form").reset();
    document.getElementById("password-error").hidden = true;
    passwordDialog.showModal();
  }));
  if (account.role !== "admin") {
    actions.append(button(account.is_active ? "Khóa" : "Mở khóa", () => update(account.id, { is_active: !account.is_active })));
  }
  row.append(name, role, worker, state, actions);
  return row;
}

function renderAccounts() {
  const query = document.getElementById("account-search").value.trim().toLocaleLowerCase("vi");
  const filtered = accounts.filter((account) => account.username.toLocaleLowerCase("vi").includes(query));
  document.getElementById("account-count").textContent = `${accounts.length} tài khoản · ${filtered.length} hiển thị`;
  if (!filtered.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.className = "empty-row";
    cell.textContent = query ? "Không tìm thấy tài khoản phù hợp." : "Chưa có tài khoản nào.";
    row.append(cell);
    accountsBody.replaceChildren(row);
    return;
  }
  accountsBody.replaceChildren(...filtered.map(renderAccount));
}

async function loadAccounts() {
  accounts = await api("/hub/api/accounts");
  renderAccounts();
}

async function loadWorkerStatus() {
  try {
    const workers = await api("/hub/api/workers");
    const status = document.getElementById("worker-status");
    status.replaceChildren();
    for (const worker of workers) {
      const item = document.createElement("span");
      item.className = `worker-state ${worker.online ? "is-online" : "is-offline"}`;
      const name = worker.id === "forge1" ? "Máy 1" : "Máy 2";
      item.textContent = `${name} · ${worker.online ? "Trực tuyến" : "Chưa kết nối"}`;
      status.append(item);
    }
  } catch (cause) {
    message(accountsMessage, cause.message, true);
  }
}

document.getElementById("create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('[type="submit"]');
  submit.disabled = true;
  try {
    await api("/hub/api/accounts", {
      method: "POST",
      body: JSON.stringify({
        username: form.elements.namedItem("username").value.trim(),
        password: form.elements.namedItem("password").value,
        worker_id: form.elements.namedItem("worker_id").value,
      }),
    });
    form.reset();
    message(createMessage, "Đã tạo tài khoản.");
    await loadAccounts();
  } catch (cause) {
    message(createMessage, cause.message, true);
  } finally {
    submit.disabled = false;
  }
});

document.getElementById("password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.getElementById("password-error");
  try {
    await api(`/hub/api/accounts/${selectedAccount.id}/password`, {
      method: "POST", body: JSON.stringify({ password: document.getElementById("reset-password").value }),
    });
    passwordDialog.close();
    message(accountsMessage, `Đã đổi mật khẩu cho ${selectedAccount.username}.`);
  } catch (cause) {
    error.textContent = cause.message;
    error.hidden = false;
  }
});

document.getElementById("cancel-password").addEventListener("click", () => passwordDialog.close());
document.getElementById("account-search").addEventListener("input", renderAccounts);
document.getElementById("refresh").addEventListener("click", () => { loadAccounts(); loadWorkerStatus(); });
document.getElementById("logout").addEventListener("click", async () => {
  await api("/hub/api/logout", { method: "POST" });
  window.location.assign("/hub/login");
});

loadAccounts().catch((cause) => message(accountsMessage, cause.message, true));
loadWorkerStatus();
