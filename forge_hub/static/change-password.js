const form = document.getElementById("change-form");
const error = document.getElementById("change-error");
const submit = document.getElementById("change-submit");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.hidden = true;
  const password = form.elements.namedItem("new-password").value;
  const confirmation = form.elements.namedItem("confirm-password").value;
  if (password !== confirmation) {
    error.textContent = "Hai mật khẩu chưa khớp";
    error.hidden = false;
    return;
  }
  submit.disabled = true;
  try {
    const response = await fetch("/hub/api/change-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Không đổi được mật khẩu");
    window.location.assign(data.next);
  } catch (cause) {
    error.textContent = cause.message;
    error.hidden = false;
  } finally {
    submit.disabled = false;
  }
});
