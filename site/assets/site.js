const root = document.documentElement;
const themeButton = document.querySelector("[data-theme-toggle]");
const themeMeta = document.querySelector('meta[name="theme-color"]');
const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");

function currentTheme() {
  return root.dataset.theme || (systemTheme.matches ? "dark" : "light");
}

function updateThemeControls() {
  const theme = currentTheme();
  if (themeButton) {
    themeButton.setAttribute("aria-label", `Use ${theme === "dark" ? "light" : "dark"} theme`);
    themeButton.title = `Use ${theme === "dark" ? "light" : "dark"} theme`;
  }
  if (themeMeta) {
    themeMeta.content = theme === "dark" ? "#1b1b1f" : "#ffffff";
  }
}

const savedTheme = localStorage.getItem("link-studio-docs-theme");
if (savedTheme === "light" || savedTheme === "dark") {
  root.dataset.theme = savedTheme;
}
updateThemeControls();

themeButton?.addEventListener("click", () => {
  const nextTheme = currentTheme() === "dark" ? "light" : "dark";
  root.dataset.theme = nextTheme;
  localStorage.setItem("link-studio-docs-theme", nextTheme);
  updateThemeControls();
});

systemTheme.addEventListener("change", () => {
  if (!root.dataset.theme) updateThemeControls();
});

const menuButton = document.querySelector("[data-sidebar-toggle]");
const sidebar = document.querySelector(".docs-sidebar");
const backdrop = document.querySelector(".sidebar-backdrop");

function setSidebar(open) {
  document.body.classList.toggle("sidebar-open", open);
  menuButton?.setAttribute("aria-expanded", String(open));
}

menuButton?.addEventListener("click", () => {
  setSidebar(!document.body.classList.contains("sidebar-open"));
});
backdrop?.addEventListener("click", () => setSidebar(false));
sidebar?.addEventListener("click", (event) => {
  if (event.target.closest("a")) setSidebar(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") setSidebar(false);
});

document.querySelectorAll(".code-block").forEach((block) => {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "copy-code";
  button.textContent = "Copy";
  button.setAttribute("aria-label", "Copy code");
  button.addEventListener("click", async () => {
    const content = [...block.childNodes]
      .filter((node) => node !== button)
      .map((node) => node.textContent)
      .join("")
      .trim();
    try {
      await navigator.clipboard.writeText(content);
      button.textContent = "Copied";
      window.setTimeout(() => { button.textContent = "Copy"; }, 1600);
    } catch {
      button.textContent = "Select";
    }
  });
  block.append(button);
});

const tocLinks = [...document.querySelectorAll(".page-toc a")];
if (tocLinks.length) {
  const sections = tocLinks
    .map((link) => document.querySelector(link.getAttribute("href")))
    .filter(Boolean);
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      tocLinks.forEach((link) => {
        link.classList.toggle("active", link.getAttribute("href") === `#${entry.target.id}`);
      });
    }
  }, { rootMargin: "-20% 0px -70% 0px" });
  sections.forEach((section) => observer.observe(section));
}
