(function () {
  const cfg = window.RAPP_CONFIG;
  if (cfg && cfg.brand && cfg.brand.title) document.title = cfg.brand.title;
  ReactDOM.createRoot(document.getElementById("root")).render(<App />);
})();
