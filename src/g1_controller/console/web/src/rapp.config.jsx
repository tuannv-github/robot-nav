// rapp.config.jsx — G1 controller console manifest.

const ICONS = {
  control: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="8" />
      <circle cx="12" cy="12" r="2" />
      <path d="M12 4v2M12 18v2M4 12h2M18 12h2" />
    </svg>
  ),
  settings: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="3" />
      <path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4" />
    </svg>
  ),
  modes: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="5" r="2" />
      <path d="M12 7v6M8 21l4-8 4 8M7 11h10" />
    </svg>
  ),
  video: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="6" width="13" height="12" rx="2" />
      <path d="M16 10l5-3v10l-5-3z" />
    </svg>
  ),
  zmq: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="6" cy="12" r="2.5" />
      <circle cx="18" cy="7" r="2.5" />
      <circle cx="18" cy="17" r="2.5" />
      <path d="M8.5 12h7M8.2 10.8 15.6 7.8M8.2 13.2l7.4 3" />
    </svg>
  ),
};

const FSM_LABELS = {
  0: "Zero Torque",
  1: "Damping",
  2: "Position Control Squat",
  3: "Position Control Sit Down",
  4: "Lock Standing",
  702: "Lie Down, Stand Up",
  706: "Squat Stand",
  500: "Walk Motion",
  501: "Walk Motion-3Dof-waist",
  801: "Run",
  802: "Run",
};

function fsmLabel(health, empty) {
  if (!health || health.fsm_id == null) return empty;
  const id = Number(health.fsm_id);
  return FSM_LABELS[id] || health.fsm_name || String(id);
}

const RAPP_CONFIG = {
  brand: {
    product: "NeuroRAN",
    shortName: "G1",
    title: "NeuroRAN G1 Console",
    logo: "logo/NeuroRAN.png",
    logoPng: "logo/NeuroRAN.png",
  },
  site: {
    label: "G1",
    id: "g1",
    clockTz: "Asia/Singapore",
  },
  theme: {
    accent: "Energy green",
    dark: false,
    density: "comfy",
  },
  exampleState: {
    link: "offline",
    linkDetail: "Offline",
    notice: "",
    driveMode: "stick",
    speed: 0.2,
    moveGain: 0.35,
    turnGain: 1,
    stick: { lx: 0, ly: 0, rx: 0 },
    sport: { vx: 0, vy: 0, w: 0 },
    health: null,
    modeResult: null,
  },
  tabs: [
    { id: "control", label: "Control", icon: ICONS.control, component: "ControlTab" },
    { id: "stream", label: "Stream", icon: ICONS.video, component: "VideoTab" },
    { id: "zmq", label: "ZMQ", icon: ICONS.zmq, component: "ZmqTab" },
    { id: "modes", label: "Modes", icon: ICONS.modes, component: "ModesTab" },
    { id: "settings", label: "Settings", icon: ICONS.settings, component: "SettingsTab" },
  ],
  kpis: (state) => {
    const health = state && state.health;
    const fsm = fsmLabel(health, "-");
    const iface = health && health.network_interface ? health.network_interface : "-";
    return [
      {
        label: "Bridge",
        kicker: "REST",
        value: state && state.link === "live" ? "Live" : "Offline",
        icon: "signal",
        bad: !state || state.link !== "live",
      },
      {
        label: "FSM",
        kicker: "mode",
        value: fsm,
        icon: "gauge",
      },
      {
        label: "DDS",
        kicker: "iface",
        value: iface,
        icon: "grid",
      },
      {
        label: "Drive",
        kicker: "mode",
        value: state && state.driveMode === "sport" ? "Sport" : "Stick",
        icon: "check",
      },
    ];
  },
};

Object.assign(window, { RAPP_CONFIG, ICONS, FSM_LABELS, fsmLabel });
