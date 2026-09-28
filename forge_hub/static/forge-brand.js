(() => {
  const title = "Forge - Trình tạo ảnh";
  const faviconHref = "/hub/assets/lush-logo-red.svg?v=4";

  const applyBranding = () => {
    if (document.title !== title) document.title = title;
    const favicon = document.querySelector("link[data-lush-forge-favicon]");
    if (favicon && favicon.getAttribute("href") !== faviconHref) {
      favicon.setAttribute("href", faviconHref);
    }
  };

  const start = () => {
    if (!document.head) return;
    const observer = new MutationObserver(applyBranding);
    observer.observe(document.head, {
      childList: true,
      characterData: true,
      subtree: true,
    });
    applyBranding();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
