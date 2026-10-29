// appShell.jsx — G1 console shell (rail + topbar + active tab).

const { useState, useEffect, useMemo } = React;

function SiteClock({ tz }) {
  const [now, setNow] = useState(() => fmtSiteNow(tz));
  useEffect(() => {
    const id = setInterval(() => setNow(fmtSiteNow(tz)), 1000);
    return () => clearInterval(id);
  }, [tz]);
  return (
    <span className="clock" title={tz}>
      <span className="status-dot dot-ok" />
      {now}
    </span>
  );
}

function Rail({ tabs, tab, setTab, brand }) {
  return (
    <aside className="rail">
      <div className="rail-brand" title={brand.title}>
        <img className="rail-logo" src={brand.logo} alt={brand.product} />
      </div>
      <nav className="rail-nav" aria-label="Main navigation">
        {tabs.map(({ id, label, icon }) => (
          <button
            key={id}
            type="button"
            className={"rail-tab" + (tab === id ? " rail-tab-active" : "")}
            onClick={() => setTab(id)}
            aria-current={tab === id ? "page" : undefined}
          >
            <span className="rail-tab-icon">{icon}</span>
            <span className="rail-tab-label">{label}</span>
          </button>
        ))}
      </nav>
      <div className="rail-foot">
        <div className="rail-status">
          <span className="rail-status-dot" />
          <span className="rail-status-text">{brand.shortName}</span>
        </div>
      </div>
    </aside>
  );
}

const LINK_ITEMS = [
  { id: "http", label: "HTTP" },
  { id: "ws", label: "WebSocket" },
  { id: "zmq", label: "ZMQ" },
];

function robotCard(state) {
  const robot = state.health && state.health.robot_link;
  if (!robot || robot.age_s == null) {
    return { id: "robot", label: "Robot", state: "warn", count: "…", title: "Waiting for robot status" };
  }
  if (robot.up) {
    const name = robot.fsm_name || (robot.fsm_id == null ? "up" : String(robot.fsm_id));
    return {
      id: "robot",
      label: "Robot",
      state: "ok",
      count: robot.fsm_id == null ? "up" : String(robot.fsm_id),
      title: "Robot " + name,
    };
  }
  return {
    id: "robot",
    label: "Robot",
    state: "bad",
    count: "down",
    title: robot.error || "Robot not responding",
  };
}

function linkCards(state) {
  const offline = state.link !== "live";
  const links = (state.health && state.health.links) || null;
  return [robotCard(state)].concat(LINK_ITEMS.map((item) => {
    const row = links && links[item.id];
    if (offline || !row) {
      return { ...item, state: offline ? "bad" : "warn", count: "—", title: item.label };
    }
    const up = !!row.up;
    const count = row.connections == null ? 0 : row.connections;
    return {
      ...item,
      state: up ? "ok" : "bad",
      count: String(count),
      title: row.error ? item.label + " " + row.error : item.label + (up ? " up" : " down"),
    };
  }));
}

function App() {
  const config = window.RAPP_CONFIG;
  const tabs = config.tabs || [];
  const [dark, setDark] = useState(!!config.theme?.dark);
  const [state, setState] = useState(() => config.exampleState || {});
  const [tab, setTab] = useState(tabs[0]?.id);
  const colors = useMemo(() => buildColors(dark, config.theme?.accent || "Energy green"), [dark, config.theme]);
  const actions = useMemo(() => ({ setState, goToTab: setTab }), []);
  useBridgeLive(actions);
  const links = linkCards(state);
  const activeTab = tabs.find((x) => x.id === tab) || tabs[0];
  const TabComp = activeTab ? window[activeTab.component] : null;
  const fit = activeTab?.id === "control" || activeTab?.id === "stream";
  const appClass = "app " + (dark ? "theme-dark" : "theme-light") + " density-comfy";
  const appVars = {
    "--accent": colors.accent,
    "--accent2": colors.accent2,
    "--accent-glow": colors.accentGlow,
    "--bad": colors.bad,
  };

  return (
    <div className={appClass} style={appVars}>
      <Rail tabs={tabs} tab={tab} setTab={setTab} brand={config.brand} />
      <div className="app-body">
        <div className={"main-col" + (fit ? " main-fit" : "")}>
          <header className="topbar">
            <div className="crumb">{activeTab?.label || config.brand.title}</div>
            {config.site?.label && <span className="site-badge">{config.site.label}</span>}
            <div className="topbar-right">
              <div className="topbar-links" aria-label="Interface connections">
                {links.map((item) => (
                  <div className="link-cell" key={item.id} title={item.title}>
                    <StatusDot state={item.state} label={item.label} />
                    <span className="link-n">{item.count}</span>
                  </div>
                ))}
              </div>
              <a className="icon-btn" href="/docs">Swagger</a>
              <SiteClock tz={config.site?.clockTz} />
              <button
                type="button"
                className="icon-btn icon-btn-sq"
                onClick={() => setDark((value) => !value)}
                aria-label={dark ? "Switch to light mode" : "Switch to dark mode"}
              >
                {dark ? "☀" : "☾"}
              </button>
            </div>
          </header>
          <main className="dash">
            {TabComp
              ? <TabComp state={state} colors={colors} config={config} actions={actions} />
              : <div className="card">Tab is not registered.</div>}
          </main>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { App });
