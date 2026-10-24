// The theme the viewer chose (Auto / Light / Dark, user menu), set before the page paints so it never flashes.
// Auto leaves the attribute off and follows the system (app.css).
try { const t = localStorage.getItem("kn_theme"); if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; } catch { /* storage blocked: Auto */ }
