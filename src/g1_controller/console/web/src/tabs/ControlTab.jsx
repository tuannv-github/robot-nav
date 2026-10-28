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

const PAD_MISS_LIMIT = 5;

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

function startDown(gp) {
  if (!gp.buttons) return false;
  const indexes = gp.mapping === "standard" ? [9] : [9, 7];
  return indexes.some((index) => gp.buttons[index] && gp.buttons[index].pressed);
}

function ControlTab({ state, actions }) {
  useBridgeLive(actions);
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
  const stickHold = React.useRef(null);

  function pushJoy(kind, name, detail) {
    const id = joySeq.current;
    joySeq.current += 1;
    setJoyEvents((prev) => prev.concat([{ id, ts: Date.now(), kind, name, detail }]).slice(-400));
  }

  function stablePads(pads) {
    const live = {};
    pads.forEach((gp) => {
      const index = String(gp.index);
      live[index] = true;
      padCache.current[index] = {
        index,
        label: padLabel(gp),
        id: gp.id || "",
        misses: 0,
      };
    });
    if (!document.hidden) {
      Object.keys(padCache.current).forEach((index) => {
        if (live[index]) return;
        padCache.current[index].misses += 1;
        if (padCache.current[index].misses >= PAD_MISS_LIMIT) delete padCache.current[index];
      });
    }
    return Object.keys(padCache.current)
      .sort((a, b) => Number(a) - Number(b))
      .map((index) => padCache.current[index]);
  }

  function sampleJoysticks(pads, stable) {
    const book = joyBook.current;
    const sig = stable.map((item) => item.index + "\t" + item.label).join("\n");
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
        if (buttons[i] && buttons[i].pressed) pressed.push(String(i));
      }
      const buttonSig = pressed.join(",");
      if (book.button[gp.index] !== buttonSig) {
        const previous = book.button[gp.index] == null ? [] : book.button[gp.index].split(",").filter(Boolean);
        const first = book.button[gp.index] == null;
        book.button[gp.index] = buttonSig;
        if (!first) {
          const down = pressed.filter((index) => previous.indexOf(index) < 0);
          const up = previous.filter((index) => pressed.indexOf(index) < 0);
          if (down.length) pushJoy("button", down.map(joyButtonName).join(" "), "down");
          if (up.length) pushJoy("button", up.map(joyButtonName).join(" "), "up");
        }
      }
    });
    Object.keys(book.axis).forEach((id) => {
      if (!live[String(id)]) {
        delete book.axis[id];
        delete book.button[id];
      }
    });
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
        stickHold.current = held;
        lx += held.lx;
        ly += held.ly;
        rx += held.rx;
        ry += held.ry;
      } else if (padGrace.current && stickHold.current) {
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

    async function tick() {
      if (dead) return;
      const pads = connectedPads();
      const stable = stablePads(pads);
      const selected = joyIndexRef.current;
      padGrace.current = !document.hidden && !!selected
        && stable.some((item) => item.index === selected)
        && !pads.some((gp) => String(gp.index) === selected);
      sampleJoysticks(pads, stable);
      const sig = stable.map((item) => item.index + "\t" + item.id).join("\n");
      if (sig !== padsSig.current) {
        padsSig.current = sig;
        setJoyPads(stable.map((item) => ({ index: item.index, label: item.label })));
      }
      if (selected && !stable.some((item) => item.index === selected)) {
        joyIndexRef.current = "";
        setJoyIndex("");
        stickHold.current = null;
        padGrace.current = false;
        if (joyOnRef.current) {
          joyOnRef.current = false;
          setJoyOn(false);
          lastSig.current = "";
          bridgeHalt();
        }
      } else if (!joyIndexRef.current && stable.length === 1) {
        joyIndexRef.current = stable[0].index;
        setJoyIndex(joyIndexRef.current);
        joyOnRef.current = true;
        setJoyOn(true);
      }
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
        if (!next) {
          lastSig.current = "";
          bridgeHalt();
        }
      }
      startWas.current = !!starter;
      applyAxes();
      const a = axes.current;
      const s = stateRef.current || {};
      const driveMode = s.driveMode === "sport" ? "sport" : "stick";
      setKnobs({
        left: { x: a.lx, y: a.ly },
        right: { x: a.rx, y: a.ry },
      });

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
        try {
          if (!active) {
            await bridgeSend("/cmd_vel/stop", { method: "POST" });
          } else {
            await bridgeSend("/cmd_vel", {
              method: "POST",
              body: JSON.stringify({ vx, vy, w }),
            });
          }
          if (!dead) actions.setState((prev) => ({ ...prev, sport: { vx, vy, w }, notice: "" }));
        } catch (err) {
          if (!dead) actions.setState((prev) => ({ ...prev, notice: err.message || "Sport command failed" }));
        }
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
      try {
        if (!active) {
          await bridgeHalt();
        } else {
          await bridgeSend("/wireless", {
            method: "POST",
            body: JSON.stringify({ lx, ly, rx, ry: a.ry * moveGain, keys: 0 }),
          });
        }
        if (!dead) actions.setState((prev) => ({ ...prev, stick: { lx, ly, rx }, notice: "" }));
      } catch (err) {
        if (!dead) actions.setState((prev) => ({ ...prev, notice: err.message || "Stick command failed" }));
      }
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

    const id = setInterval(tick, 100);
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    return () => {
      dead = true;
      clearInterval(id);
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
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
      lastSig.current = "";
      bridgeHalt();
    }
  }

  function toggleJoystick() {
    const next = !joyOnRef.current;
    joyOnRef.current = next;
    setJoyOn(next);
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
    if (mode.confirm && !window.confirm(mode.confirm)) return;
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
        <Card className="board-card">
          <div className="panel-head">
            <SectionLabel kicker={driveMode === "sport" ? "m/s" : "gain"}>Drive</SectionLabel>
            <button
              type="button"
              className="status-btn"
              aria-pressed={joyOn}
              disabled={offline && !joyOn}
              onClick={toggleJoystick}
            >
              <StatusDot state={joyOn ? "ok" : "warn"} label={joyOn ? "On" : "Off"} />
            </button>
          </div>
          <div className="drive-tools">
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
          <div className="pad-row board-pads">
            <div className="pad-col">
              <span className="pad-label">Move</span>
              <div
                className="pad"
                role="application"
                aria-label="Left stick. Drag to walk and strafe. W A S D."
                onPointerDown={onLeftPad}
                onPointerMove={(event) => { if (leftPad.current.active) onLeftPad(event); }}
                onPointerUp={() => { leftPad.current = { x: 0, y: 0, active: false }; }}
                onPointerCancel={() => { leftPad.current = { x: 0, y: 0, active: false }; }}
              >
                <span className="pad-cross" />
                <span className="pad-knob" style={knobStyle(knobs.left)} />
              </div>
            </div>
            <div className="pad-col">
              <span className="pad-label">Turn</span>
              <div
                className="pad"
                role="application"
                aria-label="Right stick. Drag left or right to turn. Q E."
                onPointerDown={onRightPad}
                onPointerMove={(event) => { if (rightPad.current.active) onRightPad(event); }}
                onPointerUp={() => { rightPad.current = { x: 0, y: 0, active: false }; }}
                onPointerCancel={() => { rightPad.current = { x: 0, y: 0, active: false }; }}
              >
                <span className="pad-cross" />
                <span className="pad-knob" style={knobStyle(knobs.right)} />
              </div>
            </div>
          </div>
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
                  onChange={(event) => {
                    actions.setState((prev) => ({ ...prev, speed: Number(event.target.value) }));
                  }}
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
                    onChange={(event) => {
                      actions.setState((prev) => ({ ...prev, moveGain: Number(event.target.value) }));
                    }}
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
                    onChange={(event) => {
                      actions.setState((prev) => ({ ...prev, turnGain: Number(event.target.value) }));
                    }}
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
        </Card>
      </div>
      <div className="board-logs">
        <ActionLogPanel state={state} />
        <JoystickLogPanel entries={joyEvents} pads={joyPads.length} />
      </div>
    </div>
  );
}

Object.assign(window, { ControlTab, ModeChoice, WALK_MODES, OTHER_MODES, MODE_SLUGS, MODE_FSM });
