// tabs/ControlTab.jsx — one page for robot mode, drive, gestures, and sport tasks.

const BRINGUP = [
  {
    step: "1",
    slug: "zero-torque",
    name: "Zero Torque",
    fsm: [0],
    danger: true,
    confirm: "Switch to Zero Torque? The motors release and the robot will collapse if it is not supported.",
    desc: "Motors off. No balance. Remote: L2 + Y.",
  },
  {
    step: "2",
    slug: "damping",
    name: "Damping",
    fsm: [1],
    confirmFrom: [500, 501, 801, 802],
    confirm: "Switch from walking to Damping? Balance stops and the robot softens.",
    desc: "Soft hold. Allowed from any mode. Remote: L2 + B.",
  },
  {
    step: "3",
    slug: "lock-standing",
    name: "Lock Standing",
    fsm: [4],
    desc: "Ready pose, about 5 seconds. No balance. Remote: L2 + Up.",
  },
  {
    step: "4",
    slug: "walk-motion",
    name: "Walk Motion",
    fsm: [500],
    desc: "Main locomotion. Drive uses this mode. Remote: R1 + X.",
  },
];

const WALK_MODES = [
  {
    slug: "walk-motion-3dof-waist",
    name: "Walk Motion-3Dof-waist",
    fsm: [501],
    desc: "Walk Motion with a 3-DoF waist. Remote: R1 + Y.",
  },
  {
    slug: "run",
    name: "Run",
    fsm: [801, 802],
    desc: "Walk-run. Id 802 after ai_sport 8.6 on 29-DoF. Remote: R2 + A.",
  },
];

const OTHER_MODES = [
  {
    slug: "position-control-squat",
    name: "Position Control Squat",
    fsm: [2],
    desc: "Squat. No balance. From main control, return through Damping first.",
  },
  {
    slug: "position-control-sit-down",
    name: "Position Control Sit Down",
    fsm: [3],
    desc: "Sit on a chair before shutdown. Remote: L2 + Left.",
  },
  {
    slug: "lie-down-stand-up",
    name: "Lie Down, Stand Up",
    fsm: [702],
    desc: "From lying face-up on a hard, flat floor. Remote: L2 + X.",
  },
  {
    slug: "squat-stand",
    name: "Squat Stand",
    fsm: [706],
    desc: "Balance squat and squat-to-stand. Remote: L2 + A.",
  },
];

const MODE_SLUGS = {};
const MODE_FSM = {};
BRINGUP.concat(WALK_MODES, OTHER_MODES).forEach((mode) => {
  MODE_SLUGS[mode.slug] = true;
  mode.fsm.forEach((id) => { MODE_FSM[id] = true; });
});

function ModeChoice({ mode, fsmId, busy, offline, onSelect, compact }) {
  const selected = fsmId != null && mode.fsm.indexOf(fsmId) >= 0;
  const pending = busy === mode.slug;
  if (compact) {
    return (
      <button
        type="button"
        className={
          "mode-chip"
          + (selected ? " is-selected" : "")
          + (mode.danger ? " mode-chip-danger" : "")
        }
        title={mode.desc}
        aria-pressed={selected}
        disabled={!!busy || offline}
        onClick={() => onSelect(mode)}
      >
        {mode.step ? <span className="mode-step">{mode.step}</span> : null}
        <strong>{pending ? "…" : mode.name}</strong>
      </button>
    );
  }
  return (
    <button
      type="button"
      className={
        "config-profile"
        + (selected ? " is-selected" : "")
        + (mode.danger ? " config-profile-danger" : "")
      }
      aria-pressed={selected}
      disabled={!!busy || offline}
      onClick={() => onSelect(mode)}
    >
      <span className="config-profile-mark" />
      <span className="config-profile-body">
        <span className="config-profile-head">
          {mode.step ? <span className="mode-step">{mode.step}</span> : null}
          <strong>{pending ? "Switching…" : mode.name}</strong>
        </span>
        <span className="config-profile-desc">{mode.desc}</span>
      </span>
    </button>
  );
}

function connectedPads() {
  const raw = navigator.getGamepads ? navigator.getGamepads() : [];
  const found = [];
  for (let i = 0; i < raw.length; i += 1) {
    const gp = raw[i];
    if (gp && gp.connected) found.push(gp);
  }
  return found;
}

function padLabel(gp) {
  const raw = String(gp.id || "Gamepad");
  const name = raw.replace(/\s*\(.*?\)\s*/g, " ").replace(/\s+/g, " ").trim() || "Gamepad";
  return name + " · " + (gp.index + 1);
}

const JOY_BUTTONS = ["A", "B", "X", "Y", "LB", "RB", "LT", "RT", "Back", "Start", "L3", "R3", "Up", "Down", "Left", "Right"];

function joyButtonName(index) {
  const i = Number(index);
  return JOY_BUTTONS[i] || ("B" + i);
}

function buttonDown(button) {
  if (!button) return false;
  if (button.pressed) return true;
  return typeof button.value === "number" && button.value >= 0.5;
}

function startDown(gp) {
  return buttonDown(gp.buttons && gp.buttons[9]);
}

const ODOM_CHOICES = [
  { id: "pelvis", name: "Pelvis" },
  { id: "torso", name: "Torso" },
  { id: "odom", name: "Odom mode" },
  { id: "fusion", name: "Fusion" },
  { id: "fusion-torso", name: "Fusion torso" },
];

function degFromRad(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  return number * 180 / Math.PI;
}

function odomGrow(view, x, y) {
  const extent = Math.max(Math.abs(x - view.x), Math.abs(y - view.y));
  const limit = 1.5 * (2 ** 16);
  while (extent > view.span / 2 && view.span < limit) view.span *= 2;
}

function odomFrameFrom(view) {
  if (!view) {
    return { minX: -0.75, maxX: 0.75, minY: -0.75, maxY: 0.75, span: 1.5 };
  }
  const half = view.span / 2;
  return {
    minX: view.x - half,
    maxX: view.x + half,
    minY: view.y - half,
    maxY: view.y + half,
    span: view.span,
  };
}

function odomToSvg(point, frame) {
  const spanX = frame.maxX - frame.minX || 1;
  const spanY = frame.maxY - frame.minY || 1;
  const pad = 18;
  const size = 200;
  return {
    x: pad + ((point.x - frame.minX) / spanX) * (size - pad * 2),
    y: size - pad - ((point.y - frame.minY) / spanY) * (size - pad * 2),
  };
}

function OdomPlot({ points, head, frame }) {
  const drawn = points.length ? points : [];
  const span = frame.span;
  const grid = [];
  for (let i = 1; i <= 3; i += 1) {
    const at = 18 + ((200 - 36) * i) / 4;
    grid.push(at);
  }
  const path = drawn.map((point) => {
    const xy = odomToSvg(point, frame);
    return xy.x.toFixed(1) + "," + xy.y.toFixed(1);
  }).join(" ");
  const tipLen = Math.max(span * 0.07, 0.12);
  let mark = "";
  if (head) {
    const yaw = Number.isFinite(head.yaw) ? head.yaw : 0;
    const nose = odomToSvg({
      x: head.x + Math.cos(yaw) * tipLen,
      y: head.y + Math.sin(yaw) * tipLen,
    }, frame);
    const left = odomToSvg({
      x: head.x + Math.cos(yaw + 2.6) * tipLen * 0.45,
      y: head.y + Math.sin(yaw + 2.6) * tipLen * 0.45,
    }, frame);
    const right = odomToSvg({
      x: head.x + Math.cos(yaw - 2.6) * tipLen * 0.45,
      y: head.y + Math.sin(yaw - 2.6) * tipLen * 0.45,
    }, frame);
    mark = nose.x.toFixed(1) + "," + nose.y.toFixed(1)
      + " " + left.x.toFixed(1) + "," + left.y.toFixed(1)
      + " " + right.x.toFixed(1) + "," + right.y.toFixed(1);
  }
  const start = drawn.length ? odomToSvg(drawn[0], frame) : null;
  const origin = odomToSvg({ x: 0, y: 0 }, frame);
  const originIn = origin.x >= 16 && origin.x <= 184 && origin.y >= 16 && origin.y <= 184;

  return (
    <svg className="odom-plot" viewBox="0 0 200 200" role="img" aria-label="Odometry path, top view. Origin at 0, 0, 0.">
      <rect className="odom-plot-bg" x="0" y="0" width="200" height="200" rx="10" />
      {grid.map((at) => (
        <g key={at} className="odom-grid-line">
          <line x1={at} y1="18" x2={at} y2="182" />
          <line x1="18" y1={at} x2="182" y2={at} />
        </g>
      ))}
      {originIn ? (
        <g className="odom-origin">
          <line x1="18" y1={origin.y} x2="182" y2={origin.y} />
          <line x1={origin.x} y1="18" x2={origin.x} y2="182" />
          <circle cx={origin.x} cy={origin.y} r="3.2" />
          <text x={origin.x + 6} y={origin.y - 6}>0, 0, 0</text>
        </g>
      ) : null}
      {path ? <polyline className="odom-path" points={path} /> : null}
      {start ? <circle className="odom-start" cx={start.x} cy={start.y} r="2.2" /> : null}
      {mark ? <polygon className="odom-head" points={mark} /> : null}
      <text className="odom-axis" x="18" y="196">{fmtNumber(frame.minX, frame.span <= 1.5 ? 2 : 1)}</text>
      <text className="odom-axis" x="182" y="196" textAnchor="end">{fmtNumber(frame.maxX, frame.span <= 1.5 ? 2 : 1)}</text>
      <text className="odom-axis" x="6" y="22">{fmtNumber(frame.maxY, frame.span <= 1.5 ? 2 : 1)}</text>
      {!head ? <text className="odom-empty" x="100" y="104" textAnchor="middle">Waiting for a sample</text> : null}
    </svg>
  );
}

function OdomBox() {
  const [sourceId, setSourceId] = React.useState(() => {
    try {
      return window.localStorage.getItem("g1.odomSource") || "pelvis";
    } catch (err) {
      return "pelvis";
    }
  });
  const [feed, setFeed] = React.useState(null);
  const trails = React.useRef({});
  const views = React.useRef({});
  const [draw, setDraw] = React.useState({ id: "pelvis", points: [], head: null });

  React.useEffect(() => {
    let stop = false;

    async function tick() {
      try {
        const body = await bridgeSend("/odom");
        if (!stop) setFeed(body);
      } catch (err) {
        if (!stop) setFeed(null);
      }
    }

    tick();
    const id = setInterval(tick, 200);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, []);

  React.useEffect(() => {
    const sources = (feed && feed.sources) || [];
    const sample = sources.find((item) => item.id === sourceId);
    if (!sample || !sample.position) {
      setDraw((prev) => (
        prev.id === sourceId && !prev.head
          ? prev
          : { id: sourceId, points: trails.current[sourceId] || [], head: null }
      ));
      return;
    }
    const head = {
      x: Number(sample.position[0]),
      y: Number(sample.position[1]),
      z: Number(sample.position[2]),
      yaw: sample.rpy ? Number(sample.rpy[2]) : 0,
    };
    const list = trails.current[sourceId] || (trails.current[sourceId] = []);
    if (!views.current[sourceId]) {
      views.current[sourceId] = { x: 0, y: 0, span: 1.5 };
    }
    odomGrow(views.current[sourceId], head.x, head.y);
    const last = list[list.length - 1];
    const moved = !last || Math.hypot(head.x - last.x, head.y - last.y) >= 0.01;
    if (moved) {
      list.push({ x: head.x, y: head.y, yaw: head.yaw });
      if (list.length > 800) list.splice(0, list.length - 800);
    } else {
      last.x = head.x;
      last.y = head.y;
      last.yaw = head.yaw;
    }
    setDraw({ id: sourceId, points: list, head });
  }, [feed, sourceId]);

  function chooseSource(value) {
    setSourceId(value);
    const points = trails.current[value] || [];
    setDraw({ id: value, points, head: points[points.length - 1] || null });
    try {
      window.localStorage.setItem("g1.odomSource", value);
    } catch (err) {
      /* the box still switches for this visit */
    }
  }

  function clearPath() {
    trails.current[sourceId] = [];
    delete views.current[sourceId];
    setDraw((prev) => ({ id: sourceId, points: [], head: prev.head }));
  }

  const sources = (feed && feed.sources) || [];
  const choices = sources.length ? sources : ODOM_CHOICES;
  const selected = sources.find((item) => item.id === sourceId) || null;
  const live = !!(selected && selected.live);
  const shown = draw.id === sourceId ? draw : { points: [], head: null };
  const frames = selected && selected.frame_id && selected.child_frame_id
    ? selected.frame_id + " → " + selected.child_frame_id
    : "";
  let status = "bad";
  let statusLabel = "Offline";
  if (feed) {
    status = live ? "ok" : (selected && selected.position ? "warn" : "bad");
    statusLabel = live ? "Live" : "Silent";
  }
  const pose = shown.head;

  return (
    <Card className="board-card odom-card">
      <div className="panel-head">
        <SectionLabel kicker={live && selected.hz ? selected.hz + " Hz" : "odom"}>Odometry</SectionLabel>
        <StatusDot state={status} label={statusLabel} />
      </div>
      <select
        className="joy-select"
        aria-label="Odometry source"
        value={choices.some((item) => item.id === sourceId) ? sourceId : "pelvis"}
        onChange={(event) => chooseSource(event.target.value)}
      >
        {choices.map((item) => (
          <option key={item.id} value={item.id}>{item.name}</option>
        ))}
      </select>
      <div className="odom-meta">
        <p className="odom-topic">{frames || (selected && selected.topic) || "Waiting for the robot"}</p>
        <button type="button" className="odom-clear" onClick={clearPath}>Clear path</button>
      </div>
      <p className="odom-pose mono">
        <span>x {pose ? fmtNumber(pose.x, 2) : "—"}</span>
        <span>y {pose ? fmtNumber(pose.y, 2) : "—"}</span>
        <span>z {pose && Number.isFinite(pose.z) ? fmtNumber(pose.z, 2) : "—"}</span>
        <span>yaw {pose ? fmtNumber(degFromRad(pose.yaw), 0) + "°" : "—"}</span>
      </p>
      <OdomPlot points={shown.points} head={pose} frame={odomFrameFrom(views.current[sourceId])} />
    </Card>
  );
}

function ControlTab({ state, actions }) {
  const stateRef = React.useRef(state);
  stateRef.current = state;
  const keys = React.useRef(new Set());
  const leftPad = React.useRef({ x: 0, y: 0, active: false });
  const rightPad = React.useRef({ x: 0, y: 0, active: false });
  const axes = React.useRef({ lx: 0, ly: 0, rx: 0, ry: 0 });
  const lastSig = React.useRef("");
  const [knobs, setKnobs] = React.useState({
    left: { x: 0, y: 0 },
    right: { x: 0, y: 0 },
  });
  const [modeBusy, setModeBusy] = React.useState("");
  const [joyPads, setJoyPads] = React.useState([]);
  const [joyIndex, setJoyIndex] = React.useState("");
  const [joyOn, setJoyOn] = React.useState(true);
  const joyOnRef = React.useRef(true);
  const joyIndexRef = React.useRef("");
  const padsSig = React.useRef("");
  const startWas = React.useRef(false);
  const joyBook = React.useRef({ pads: "", axis: {}, button: {} });
  const joySeq = React.useRef(1);
  const [joyEvents, setJoyEvents] = React.useState([]);
  const padCache = React.useRef({});
  const padGrace = React.useRef(false);
  const padSeenAt = React.useRef(0);
  const stickHold = React.useRef(null);
  const poseSig = React.useRef("");

  function pushJoy(kind, name, detail) {
    const id = joySeq.current;
    joySeq.current += 1;
    setJoyEvents((prev) => prev.concat([{ id, ts: Date.now(), kind, name, detail }]).slice(-400));
  }

  function rememberPad(gp) {
    const index = String(gp.index);
    const id = gp.id || "";
    if (id) {
      Object.keys(padCache.current).forEach((key) => {
        if (key !== index && padCache.current[key].id === id) {
          if (joyIndexRef.current === key) {
            joyIndexRef.current = index;
            setJoyIndex(index);
          }
          delete padCache.current[key];
        }
      });
    }
    padCache.current[index] = {
      index,
      label: padLabel(gp),
      id,
    };
  }

  function listPads() {
    return Object.keys(padCache.current)
      .sort((a, b) => Number(a) - Number(b))
      .map((index) => padCache.current[index]);
  }

  function scanPads() {
    const raw = navigator.getGamepads ? navigator.getGamepads() : [];
    const found = [];
    for (let i = 0; i < raw.length; i += 1) {
      const gp = raw[i];
      if (!gp || gp.connected === false) continue;
      rememberPad(gp);
      found.push(gp);
    }
    return found;
  }

  function sampleJoysticks(pads, stable) {
    const book = joyBook.current;
    const sig = stable.map((item) => (item.id || item.index) + "\t" + item.label).join("\n");
    if (sig !== book.pads) {
      const prevRows = book.pads ? book.pads.split("\n") : [];
      const nextRows = sig ? sig.split("\n") : [];
      const prevSet = {};
      const nextSet = {};
      prevRows.forEach((row) => {
        const parts = row.split("\t");
        prevSet[parts[0]] = parts.slice(1).join("\t");
      });
      nextRows.forEach((row) => {
        const parts = row.split("\t");
        nextSet[parts[0]] = parts.slice(1).join("\t");
      });
      Object.keys(prevSet).forEach((id) => {
        if (!nextSet[id]) pushJoy("pad", prevSet[id], "Disconnected");
      });
      Object.keys(nextSet).forEach((id) => {
        if (!prevSet[id]) pushJoy("pad", nextSet[id], "Connected");
      });
      book.pads = sig;
    }
    const live = {};
    stable.forEach((item) => { live[item.index] = true; });
    pads.forEach((gp) => {
      live[String(gp.index)] = true;
      const axis = (index) => (gp.axes && gp.axes.length > index ? gp.axes[index] : 0);
      const rounded = [axis(0), -axis(1), axis(2), -axis(3)].map((value) => Math.round(value * 100) / 100);
      const key = rounded.join(",");
      if (book.axis[gp.index] !== key) {
        const first = book.axis[gp.index] == null;
        book.axis[gp.index] = key;
        const idle = rounded.every((value) => value === 0);
        if (!(first && idle)) {
          pushJoy(
            "axis",
            padLabel(gp),
            idle
              ? "Released"
              : "lx " + rounded[0].toFixed(2) + "  ly " + rounded[1].toFixed(2)
                + "  rx " + rounded[2].toFixed(2) + "  ry " + rounded[3].toFixed(2),
          );
        }
      }
      const pressed = [];
      const buttons = gp.buttons || [];
      for (let i = 0; i < buttons.length; i += 1) {
        if (buttonDown(buttons[i])) pressed.push(String(i));
      }
      const buttonSig = pressed.join(",");
      if (book.button[gp.index] !== buttonSig) {
        const previous = book.button[gp.index] == null ? [] : book.button[gp.index].split(",").filter(Boolean);
        const first = book.button[gp.index] == null;
        book.button[gp.index] = buttonSig;
        const now = pressed;
        const down = first ? now : now.filter((index) => previous.indexOf(index) < 0);
        const up = first ? [] : previous.filter((index) => now.indexOf(index) < 0);
        if (down.length) pushJoy("button", down.map(joyButtonName).join(" "), "down");
        if (up.length) pushJoy("button", up.map(joyButtonName).join(" "), "up");
      }
    });
    Object.keys(book.axis).forEach((id) => {
      if (!live[String(id)]) {
        delete book.axis[id];
        delete book.button[id];
      }
    });
  }

  function showPose(pads) {
    const gp = pads.find((item) => String(item.index) === joyIndexRef.current) || (pads.length === 1 ? pads[0] : null);
    if (!gp) return;
    const axis = (index) => (gp.axes && gp.axes.length > index ? gp.axes[index] : 0);
    const pose = {
      left: { x: axis(0), y: -axis(1) },
      right: { x: axis(2), y: -axis(3) },
    };
    const key = [pose.left.x, pose.left.y, pose.right.x, pose.right.y].map((value) => Math.round(value * 100)).join(",");
    if (key === poseSig.current) return;
    poseSig.current = key;
    setKnobs(pose);
  }

  function noteStart(pads) {
    let starter = null;
    pads.forEach((gp) => {
      if (startDown(gp)) starter = gp;
    });
      if (starter && !startWas.current) {
      joyIndexRef.current = String(starter.index);
      setJoyIndex(joyIndexRef.current);
      const next = !joyOnRef.current;
      joyOnRef.current = next;
      setJoyOn(next);
      setGrab(next);
      if (!next) {
        lastSig.current = "";
        bridgeHalt();
      }
    }
    startWas.current = !!starter;
  }

  function setGrab(on) {
    bridgeSend("/wireless", {
      method: "POST",
      body: JSON.stringify({ grab: !!on }),
    }).catch(() => null);
  }

  function observePads(pads) {
    sampleJoysticks(pads, listPads());
    showPose(pads);
    noteStart(pads);
  }

  const applyAxes = React.useCallback(() => {
    if (!joyOnRef.current) {
      axes.current = { lx: 0, ly: 0, rx: 0, ry: 0 };
      return;
    }
    let lx = 0;
    let ly = 0;
    let rx = 0;
    let ry = 0;
    keys.current.forEach((name) => {
      if (name === "w" || name === "arrowup") ly += 1;
      if (name === "s" || name === "arrowdown") ly -= 1;
      if (name === "a") lx -= 1;
      if (name === "d") lx += 1;
      if (name === "q" || name === "arrowleft") rx -= 1;
      if (name === "e" || name === "arrowright") rx += 1;
    });
    if (leftPad.current.active) {
      lx += leftPad.current.x;
      ly += leftPad.current.y;
    }
    if (rightPad.current.active) {
      rx += rightPad.current.x;
      ry += rightPad.current.y;
    }
    if (joyOnRef.current && joyIndexRef.current) {
      const gp = connectedPads().find((item) => String(item.index) === joyIndexRef.current);
      const axis = (index) => (gp && gp.axes && gp.axes.length > index ? gp.axes[index] : 0);
      if (gp) {
        const held = { lx: axis(0), ly: -axis(1), rx: axis(2), ry: -axis(3) };
        const idle = Math.abs(held.lx) + Math.abs(held.ly) + Math.abs(held.rx) + Math.abs(held.ry) < 0.02;
        const blurred = !document.hasFocus() || document.hidden;
        if (idle && blurred && stickHold.current) {
          lx += stickHold.current.lx;
          ly += stickHold.current.ly;
          rx += stickHold.current.rx;
          ry += stickHold.current.ry;
        } else {
          stickHold.current = held;
          lx += held.lx;
          ly += held.ly;
          rx += held.rx;
          ry += held.ry;
        }
      } else if (stickHold.current && (padGrace.current || !document.hasFocus() || document.hidden)) {
        lx += stickHold.current.lx;
        ly += stickHold.current.ly;
        rx += stickHold.current.rx;
        ry += stickHold.current.ry;
      }
    }
    const clamp = (v) => Math.max(-1, Math.min(1, v));
    axes.current = { lx: clamp(lx), ly: clamp(ly), rx: clamp(rx), ry: clamp(ry) };
  }, []);

  React.useEffect(() => {
    let dead = false;
    let sending = false;

    function tick() {
      if (dead) return;
      const pads = scanPads();
      const stable = listPads();
      const selected = joyIndexRef.current;
      const liveNow = !!selected && pads.some((gp) => String(gp.index) === selected);
      if (pads.length === 1 && String(pads[0].index) !== selected) {
        const hadSelection = !!selected;
        joyIndexRef.current = String(pads[0].index);
        setJoyIndex(joyIndexRef.current);
        if (!hadSelection) {
          joyOnRef.current = true;
          setJoyOn(true);
        }
      } else if (selected && !liveNow) {
        const previousId = (padCache.current[selected] || {}).id;
        const rebound = previousId && pads.find((gp) => gp.id === previousId);
        if (rebound) {
          joyIndexRef.current = String(rebound.index);
          setJoyIndex(joyIndexRef.current);
        }
      }
      if (liveNow || pads.some((gp) => String(gp.index) === joyIndexRef.current)) {
        padSeenAt.current = Date.now();
      }
      padGrace.current = !!joyIndexRef.current
        && !pads.some((gp) => String(gp.index) === joyIndexRef.current)
        && !!padCache.current[joyIndexRef.current];
      sampleJoysticks(pads, stable);
      const sig = stable.map((item) => item.index + "\t" + item.id).join("\n");
      if (sig !== padsSig.current) {
        padsSig.current = sig;
        setJoyPads(stable.map((item) => ({ index: item.index, label: item.label })));
      }
      noteStart(pads);
      applyAxes();
      const a = axes.current;
      const s = stateRef.current || {};
      const driveMode = s.driveMode === "sport" ? "sport" : "stick";
      if (!joyIndexRef.current) {
        setKnobs({
          left: { x: a.lx, y: a.ly },
          right: { x: a.rx, y: a.ry },
        });
      }

      if (driveMode === "sport") {
        const speed = Number(s.speed);
        const scale = Number.isFinite(speed) ? speed : 0.2;
        const vx = a.ly * scale;
        const vy = a.lx * scale;
        const w = a.rx * scale;
        const active = Math.abs(vx) + Math.abs(vy) + Math.abs(w) > 0.0001;
        if (!active) {
          if (lastSig.current === "0") return;
          lastSig.current = "0";
        } else {
          lastSig.current = "sport";
        }
        if (sending) return;
        sending = true;
        const sportSend = active
          ? bridgeSend("/cmd_vel", { method: "POST", body: JSON.stringify({ vx, vy, w }) })
          : bridgeSend("/cmd_vel/stop", { method: "POST" });
        sportSend.then(() => {
          if (!dead) actions.setState((prev) => ({ ...prev, sport: { vx, vy, w }, notice: "" }));
        }).catch((err) => {
          if (!dead) actions.setState((prev) => ({ ...prev, notice: err.message || "Sport command failed" }));
        }).then(() => { sending = false; });
        return;
      }

      const move = Number(s.moveGain);
      const turn = Number(s.turnGain);
      const moveGain = Number.isFinite(move) ? move : 0.35;
      const turnGain = Number.isFinite(turn) ? turn : 1;
      const lx = a.lx * moveGain;
      const ly = a.ly * moveGain;
      const rx = a.rx * turnGain;
      const active = Math.abs(lx) + Math.abs(ly) + Math.abs(rx) > 0.0001;
      if (!active) {
        if (lastSig.current === "0") return;
        lastSig.current = "0";
      } else {
        lastSig.current = "stick";
      }
      if (sending) return;
      sending = true;
      const stickSend = active
        ? bridgeSend("/wireless", {
          method: "POST",
          body: JSON.stringify({ lx, ly, rx, ry: a.ry * moveGain, keys: 0 }),
        })
        : bridgeHalt();
      stickSend.then(() => {
        if (!dead) actions.setState((prev) => ({ ...prev, stick: { lx, ly, rx }, notice: "" }));
      }).catch((err) => {
        if (!dead) actions.setState((prev) => ({ ...prev, notice: err.message || "Stick command failed" }));
      }).then(() => { sending = false; });
    }

    function typing(event) {
      const tag = event.target && event.target.tagName;
      return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    }

    function onKeyDown(event) {
      if (typing(event)) return;
      if (event.code === "Space") {
        event.preventDefault();
        keys.current.clear();
        leftPad.current = { x: 0, y: 0, active: false };
        rightPad.current = { x: 0, y: 0, active: false };
        lastSig.current = "";
        joyOnRef.current = false;
        setJoyOn(false);
        setGrab(false);
        bridgeHalt();
        return;
      }
      const name = event.key.toLowerCase();
      const known = ["w", "a", "s", "d", "q", "e", "arrowup", "arrowdown", "arrowleft", "arrowright"];
      if (known.indexOf(name) < 0) return;
      event.preventDefault();
      keys.current.add(name);
    }

    function onKeyUp(event) {
      keys.current.delete(event.key.toLowerCase());
    }

    function onPadConnect(event) {
      const gp = event && event.gamepad;
      if (!gp || gp.connected === false) return;
      rememberPad(gp);
      const pads = scanPads();
      const seen = pads.some((item) => item.index === gp.index);
      observePads(seen ? pads : pads.concat([gp]));
      if (seen) {
        sampleJoysticks([gp], listPads());
        noteStart([gp]);
      }
    }

    function onFocusLost() {
      keys.current.clear();
    }

    let frame = 0;
    function watchPads() {
      if (dead) return;
      observePads(scanPads());
      frame = window.requestAnimationFrame(watchPads);
    }

    const stopClock = startDriveClock(tick, 50);
    const stopKeepAlive = startDriveKeepAlive();
    if (joyOnRef.current) setGrab(true);
    frame = window.requestAnimationFrame(watchPads);
    window.addEventListener("gamepadconnected", onPadConnect);
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", onFocusLost);
    return () => {
      dead = true;
      stopClock();
      stopKeepAlive();
      window.cancelAnimationFrame(frame);
      window.removeEventListener("gamepadconnected", onPadConnect);
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", onFocusLost);
      bridgeHalt();
    };
  }, [actions, applyAxes]);

  function setDriveMode(next) {
    lastSig.current = "";
    bridgeHalt();
    actions.setState((prev) => ({ ...prev, driveMode: next, notice: "" }));
  }

  function chooseJoystick(value) {
    joyIndexRef.current = value;
    setJoyIndex(value);
    if (!value && joyOnRef.current) {
      joyOnRef.current = false;
      setJoyOn(false);
      setGrab(false);
      lastSig.current = "";
      bridgeHalt();
    }
  }

  function toggleJoystick() {
    const next = !joyOnRef.current;
    joyOnRef.current = next;
    setJoyOn(next);
    setGrab(next);
    if (!next) {
      keys.current.clear();
      leftPad.current = { x: 0, y: 0, active: false };
      rightPad.current = { x: 0, y: 0, active: false };
      lastSig.current = "";
      bridgeHalt();
    }
  }

  function pointOnPad(event) {
    const rect = event.currentTarget.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    const y = -(((event.clientY - rect.top) / rect.height) * 2 - 1);
    return {
      x: Math.max(-1, Math.min(1, x)),
      y: Math.max(-1, Math.min(1, y)),
      active: true,
    };
  }

  function onLeftPad(event) {
    leftPad.current = pointOnPad(event);
    if (event.currentTarget.setPointerCapture) {
      event.currentTarget.setPointerCapture(event.pointerId);
    }
  }

  function onRightPad(event) {
    rightPad.current = pointOnPad(event);
    if (event.currentTarget.setPointerCapture) {
      event.currentTarget.setPointerCapture(event.pointerId);
    }
  }

  function modeResultFrom(mode, body, fallback) {
    const reported = body && body.fsm_id != null ? Number(body.fsm_id) : null;
    const requested = body && body.requested_fsm_id != null ? Number(body.requested_fsm_id) : mode.fsm[0];
    return {
      ok: !!(body && body.ok),
      name: (body && body.name) || mode.name,
      errorCode: body && body.error_code != null ? body.error_code : null,
      requestedId: requested,
      fsmId: reported,
      fsmName: (body && body.fsm_name) || (reported != null ? fsmLabel({ fsm_id: reported }, "") : ""),
      applied: !!(body && body.applied),
      detail: (body && body.detail) || fallback || "",
    };
  }

  function applyModeResult(prev, modeResult) {
    const next = { ...prev, notice: "", modeResult };
    if (modeResult.fsmId == null) return next;
    return {
      ...next,
      health: {
        ...(prev.health || {}),
        fsm_id: modeResult.fsmId,
        fsm_name: modeResult.fsmName || fsmLabel({ fsm_id: modeResult.fsmId }, ""),
      },
    };
  }

  async function selectMode(mode) {
    const fromWalk = mode.confirmFrom && mode.confirmFrom.indexOf(fsmId) >= 0;
    const prompt = mode.confirm && (!mode.confirmFrom || fromWalk) ? mode.confirm : "";
    if (prompt && !window.confirm(prompt)) return;
    setModeBusy(mode.slug);
    try {
      const body = await bridgeSend("/sport/" + mode.slug, { method: "POST", body: "{}" });
      actions.setState((prev) => applyModeResult(prev, modeResultFrom(mode, body)));
    } catch (err) {
      actions.setState((prev) => applyModeResult(
        prev,
        modeResultFrom(mode, err.body, err.message || "Mode change failed"),
      ));
    } finally {
      setModeBusy("");
    }
  }

  const scaleTimer = React.useRef(0);
  const scaleRef = React.useRef({
    moveGain: state.moveGain,
    turnGain: state.turnGain,
    speed: state.speed,
  });

  React.useEffect(() => {
    let stop = false;
    bridgeSend("/config/scale").then((body) => {
      if (stop || !body) return;
      const loaded = {
        moveGain: Number(body.move),
        turnGain: Number(body.turn),
        speed: Number(body.speed),
      };
      scaleRef.current = loaded;
      actions.setState((prev) => ({ ...prev, ...loaded }));
    }).catch(() => {});
    return () => { stop = true; };
  }, [actions]);

  function setScale(patch) {
    scaleRef.current = { ...scaleRef.current, ...patch };
    actions.setState((prev) => ({ ...prev, ...patch }));
    window.clearTimeout(scaleTimer.current);
    const pending = scaleRef.current;
    scaleTimer.current = window.setTimeout(() => {
      bridgeSend("/config/scale", {
        method: "PUT",
        body: JSON.stringify({
          move: pending.moveGain,
          turn: pending.turnGain,
          speed: pending.speed,
        }),
      }).catch(() => {});
    }, 300);
  }

  const fsmId = state.health && state.health.fsm_id != null ? Number(state.health.fsm_id) : null;
  const fsmName = fsmLabel(state.health, "Unknown");
  const offline = state.link !== "live";
  const driveMode = state.driveMode === "sport" ? "sport" : "stick";
  const linkState = offline ? "bad" : "ok";
  const shown = driveMode === "sport" ? state.sport || {} : state.stick || {};

  function knobStyle(stick) {
    return {
      left: ((stick.x + 1) / 2) * 100 + "%",
      top: ((1 - stick.y) / 2) * 100 + "%",
    };
  }

  return (
    <div className="board">
      <div className="board-top">
        <Card className="board-card">
          <div className="panel-head">
            <SectionLabel kicker={fsmId == null ? "fsm" : String(fsmId)}>{fsmName}</SectionLabel>
            <StatusDot state={linkState} label={state.linkDetail || "Offline"} />
          </div>
          {state.modeResult ? (
            <div className={"mode-result" + (state.modeResult.applied ? " is-ok" : " is-bad")}>
              <span className={"feed-badge " + (state.modeResult.applied ? "feed-badge-ok" : "feed-badge-warn")}>
                {state.modeResult.applied ? "Applied" : (state.modeResult.ok ? "Not applied" : "Rejected")}
              </span>
              <span>
                {state.modeResult.name}
                {state.modeResult.fsmId != null ? " · now " + (state.modeResult.fsmName || state.modeResult.fsmId) : ""}
              </span>
            </div>
          ) : null}
          {state.notice ? (
            <p className="drive-notice"><span className="feed-badge feed-badge-warn">Fail</span>{state.notice}</p>
          ) : null}
          <div className="mode-tiles">
            {BRINGUP.map((mode) => (
              <ModeChoice
                key={mode.slug}
                compact
                mode={mode}
                fsmId={fsmId}
                busy={modeBusy}
                offline={offline}
                onSelect={selectMode}
              />
            ))}
          </div>
        </Card>
        <Card className="board-card drive-card">
          <div className="panel-head drive-head">
            <SectionLabel kicker={driveMode === "sport" ? "m/s" : "gain"}>Drive</SectionLabel>
            <select
              className="joy-select"
              aria-label="Detected joysticks"
              value={joyPads.some((item) => item.index === joyIndex) ? joyIndex : ""}
              disabled={offline}
              onChange={(event) => chooseJoystick(event.target.value)}
            >
              <option value="">{joyPads.length ? "Select a joystick" : "No joystick detected"}</option>
              {joyPads.map((item) => (
                <option key={item.index} value={item.index}>{item.label}</option>
              ))}
            </select>
            <div className="drive-chip-row">
              <button
                type="button"
                className={"mode-chip" + (joyOn ? " is-selected" : "")}
                aria-pressed={joyOn}
                disabled={offline && !joyOn}
                onClick={toggleJoystick}
              >
                <strong>{joyOn ? "On" : "Off"}</strong>
              </button>
              <button
                type="button"
                className={"mode-chip" + (driveMode === "stick" ? " is-selected" : "")}
                aria-pressed={driveMode === "stick"}
                onClick={() => setDriveMode("stick")}
              >
                <strong>Stick</strong>
              </button>
              <button
                type="button"
                className={"mode-chip" + (driveMode === "sport" ? " is-selected" : "")}
                aria-pressed={driveMode === "sport"}
                onClick={() => setDriveMode("sport")}
              >
                <strong>Sport</strong>
              </button>
            </div>
          </div>
          <div className="drive-body">
          <div className="pad-row board-pads">
            <div
              className="pad"
              role="application"
              aria-label="Move. Drag to walk and strafe. W A S D."
              onPointerDown={onLeftPad}
              onPointerMove={(event) => { if (leftPad.current.active) onLeftPad(event); }}
              onPointerUp={() => { leftPad.current = { x: 0, y: 0, active: false }; }}
              onPointerCancel={() => { leftPad.current = { x: 0, y: 0, active: false }; }}
            >
              <span className="pad-name">Move</span>
              <span className="pad-cross" />
              <span className="pad-knob" style={knobStyle(knobs.left)} />
            </div>
            <div
              className="pad"
              role="application"
              aria-label="Turn. Drag left or right to turn. Q E."
              onPointerDown={onRightPad}
              onPointerMove={(event) => { if (rightPad.current.active) onRightPad(event); }}
              onPointerUp={() => { rightPad.current = { x: 0, y: 0, active: false }; }}
              onPointerCancel={() => { rightPad.current = { x: 0, y: 0, active: false }; }}
            >
              <span className="pad-name">Turn</span>
              <span className="pad-cross" />
              <span className="pad-knob" style={knobStyle(knobs.right)} />
            </div>
          </div>
          <div className="drive-side">
          <div className="drive-gains">
            {driveMode === "sport" ? (
              <label className="gain-row">
                <span className="field-label">Speed</span>
                <input
                  className="stick-range"
                  type="range"
                  min="0"
                  max="0.6"
                  step="0.05"
                  value={state.speed}
                  onChange={(event) => setScale({ speed: Number(event.target.value) })}
                />
                <span className="mono field-value">{fmtNumber(state.speed, 2)}</span>
              </label>
            ) : (
              <>
                <label className="gain-row">
                  <span className="field-label">Move</span>
                  <input
                    className="stick-range"
                    type="range"
                    min="0"
                    max="2"
                    step="0.05"
                    value={state.moveGain}
                    onChange={(event) => setScale({ moveGain: Number(event.target.value) })}
                  />
                  <span className="mono field-value">{fmtNumber(state.moveGain, 2)}</span>
                </label>
                <label className="gain-row">
                  <span className="field-label">Turn</span>
                  <input
                    className="stick-range"
                    type="range"
                    min="0"
                    max="2"
                    step="0.05"
                    value={state.turnGain}
                    onChange={(event) => setScale({ turnGain: Number(event.target.value) })}
                  />
                  <span className="mono field-value">{fmtNumber(state.turnGain, 2)}</span>
                </label>
              </>
            )}
          </div>
          <div className="drive-readout">
            <div className="cmd-cell">
              <div className="cmd-k">{driveMode === "sport" ? "vx" : "ly"}</div>
              <div className="cmd-v mono">{fmtNumber(driveMode === "sport" ? shown.vx : shown.ly, 2)}</div>
            </div>
            <div className="cmd-cell">
              <div className="cmd-k">{driveMode === "sport" ? "vy" : "lx"}</div>
              <div className="cmd-v mono">{fmtNumber(driveMode === "sport" ? shown.vy : shown.lx, 2)}</div>
            </div>
            <div className="cmd-cell">
              <div className="cmd-k">{driveMode === "sport" ? "w" : "rx"}</div>
              <div className="cmd-v mono">{fmtNumber(driveMode === "sport" ? shown.w : shown.rx, 2)}</div>
            </div>
          </div>
          </div>
          </div>
        </Card>
      </div>
      <div className="board-logs">
        <ActionLogPanel state={state} />
        <JoystickLogPanel entries={joyEvents} pads={joyPads.length} />
        <OdomBox />
      </div>
    </div>
  );
}

Object.assign(window, { ControlTab, ModeChoice, WALK_MODES, OTHER_MODES, MODE_SLUGS, MODE_FSM });
