const copyButton = document.querySelector("[data-copy]");
const copyToast = document.querySelector(".copy-toast");

if (copyButton && copyToast) {
  copyButton.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(copyButton.dataset.copy);
      copyButton.textContent = "Copied";
      copyToast.classList.add("visible");
      window.setTimeout(() => {
        copyButton.textContent = "Copy";
        copyToast.classList.remove("visible");
      }, 1800);
    } catch {
      copyButton.textContent = "Select and copy";
    }
  });
}
