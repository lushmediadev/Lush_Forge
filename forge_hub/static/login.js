const form = document.getElementById("login-form");
const error = document.getElementById("login-error");
const submit = document.getElementById("login-submit");
const submitLabel = submit.querySelector(".submit-label");
const password = document.getElementById("password");
const togglePassword = document.getElementById("toggle-password");

togglePassword.addEventListener("click", () => {
  const visible = password.type === "password";
  password.type = visible ? "text" : "password";
  togglePassword.textContent = visible ? "Ẩn" : "Hiện";
  togglePassword.setAttribute("aria-label", visible ? "Ẩn mật khẩu" : "Hiện mật khẩu");
  togglePassword.setAttribute("aria-pressed", String(visible));
  password.focus();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.hidden = true;
  submit.disabled = true;
  submitLabel.textContent = "Đang đăng nhập…";
  try {
    const response = await fetch("/hub/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: form.elements.namedItem("username").value.trim(),
        password: form.elements.namedItem("password").value,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Không đăng nhập được");
    window.location.assign(data.account.must_change_password ? data.next : data.account.role === "admin" ? "/hub/admin" : data.next);
  } catch (cause) {
    error.textContent = cause.message;
    error.hidden = false;
  } finally {
    submit.disabled = false;
    submitLabel.textContent = "Đăng nhập";
  }
});
