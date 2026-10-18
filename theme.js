// Shared by application and authentication pages before their stylesheets load.
(() => {
  const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
  let preference = "system";
  try {
    const savedPreference = localStorage.getItem("theme");
    if (savedPreference === "light" || savedPreference === "dark") preference = savedPreference;
  } catch (error) {}

  function renderTheme() {
    if (preference === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.dataset.theme = preference;
    const effectiveDark = preference === "dark" || (preference === "system" && systemTheme.matches);
    const themeColor = document.querySelector('meta[name="theme-color"]');
    if (themeColor) themeColor.setAttribute("content", effectiveDark ? "#202124" : "#FFFFFF");
  }

  window.readThemePreference = () => preference;
  window.applyThemePreference = (theme) => {
    preference = theme === "light" || theme === "dark" ? theme : "system";
    try { localStorage.setItem("theme", preference); } catch (error) {}
    renderTheme();
  };
  if (systemTheme.addEventListener) systemTheme.addEventListener("change", renderTheme);
  else systemTheme.addListener(renderTheme);
  renderTheme();
})();
