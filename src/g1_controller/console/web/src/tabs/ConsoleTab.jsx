// tabs/ConsoleTab.jsx — action log shared by the control screen.

function ifaceLabel(source) {
  if (source === "http" || source === "wireless" || source === "cmd_vel") return "HTTP";
  if (source === "ws" || source === "control") return "WS";
  if (source === "zmq") return "ZMQ";
  if (source === "hold") return "Hold";
  return source || "";
}

const KIND_LABEL = {
  mode: "Mode",
  sport: "Sport",
  gesture: "Gesture",
  drive: "Drive",
  joystick: "Stick",
  system: "System",
};

function useStickToBottom(entries) {
  const listRef = React.useRef(null);
  const followRef = React.useRef(true);
  const ignoreRef = React.useRef(false);
  const userRef = React.useRef(false);
  const intentTimer = React.useRef(0);
  const [follow, setFollow] = React.useState(true);
  const count = entries ? entries.length : 0;
  const tail = count ? entries[count - 1].id : 0;

  const pin = React.useCallback(() => {
    const el = listRef.current;
    if (!el || !followRef.current) return;
    if (el.clientHeight <= 0) {
      requestAnimationFrame(() => {
        const node = listRef.current;
        if (!node || !followRef.current || node.clientHeight <= 0) return;
        ignoreRef.current = true;
        node.scrollTop = node.scrollHeight;
        requestAnimationFrame(() => { ignoreRef.current = false; });
      });
      return;
    }
    ignoreRef.current = true;
    el.scrollTop = el.scrollHeight;
    requestAnimationFrame(() => {
      const node = listRef.current;
      if (node && followRef.current) node.scrollTop = node.scrollHeight;
      requestAnimationFrame(() => { ignoreRef.current = false; });
    });
  }, []);

  function armIdle() {
    window.clearTimeout(intentTimer.current);
    intentTimer.current = window.setTimeout(() => {
      userRef.current = false;
      if (followRef.current) pin();
    }, 250);
  }

  React.useLayoutEffect(() => {
    pin();
  }, [count, tail, pin]);

  React.useEffect(() => () => window.clearTimeout(intentTimer.current), []);

  function markUser() {
    userRef.current = true;
    armIdle();
  }

  function onScroll() {
    if (ignoreRef.current) return;
    const el = listRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
    if (!userRef.current) {
      if (followRef.current && !nearBottom) pin();
      return;
    }
    armIdle();
    if (nearBottom === followRef.current) return;
    followRef.current = nearBottom;
    setFollow(nearBottom);
  }

  function resume() {
    followRef.current = true;
    setFollow(true);
    pin();
  }

  return { listRef, follow, onScroll, markUser, resume };
}

function ActionLogPanel({ state }) {
  const [entries, setEntries] = React.useState([]);
  const [path, setPath] = React.useState("");
  const [fault, setFault] = React.useState("");
  const afterRef = React.useRef(0);
  const { listRef, follow, onScroll, markUser, resume } = useStickToBottom(entries);

  React.useEffect(() => {
    let stop = false;

    async function tick() {
      try {
        const body = await bridgeSend("/logs?after=" + afterRef.current);
        if (stop) return;
        setFault("");
        if (body && body.path) setPath(body.path);
        const rows = (body && body.entries) || [];
        if (!rows.length) return;
        afterRef.current = rows[rows.length - 1].id;
        setEntries((prev) => prev.concat(rows).slice(-500));
      } catch (err) {
        if (!stop) setFault(err.message || "Log unavailable");
      }
    }

    tick();
    const id = setInterval(tick, 1000);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, []);

  const linkState = state && state.link === "live" ? "ok" : "bad";

  return (
    <Card className="feed-card console-card board-log">
      <div className="panel-head">
        <SectionLabel kicker={entries.length ? String(entries.length) : "idle"}>Controller</SectionLabel>
        <StatusDot state={fault ? "bad" : linkState} label={fault || (state && state.linkDetail) || "Offline"} />
      </div>
      {path ? <p className="console-path" title={path}>{path}</p> : null}
      <div
        className="feed-list console-log"
        ref={listRef}
        onScroll={onScroll}
        onWheel={markUser}
        onPointerDown={markUser}
        onTouchMove={markUser}
      >
        {entries.map((entry) => (
          <div className="feed-row" key={entry.id}>
            <span className="feed-ts">{fmtSiteNow(RAPP_CONFIG.site.clockTz, new Date(entry.ts * 1000))}</span>
            <span className={"feed-badge " + (entry.ok ? "feed-badge-ok" : "feed-badge-warn")}>
              {KIND_LABEL[entry.kind] || entry.kind}
            </span>
            <span className="feed-text">
              <strong>{entry.name}</strong>
              {entry.kind === "drive" && entry.source ? (
                <span className="console-source">{ifaceLabel(entry.source)}</span>
              ) : null}
              {entry.detail ? <span className="console-detail">{entry.detail}</span> : null}
            </span>
          </div>
        ))}
        {!entries.length ? (
          <p className="pad-hint">No controlled actions yet.</p>
        ) : null}
      </div>
      {!follow ? (
        <div className="act-row">
          <button type="button" className="act-btn" onClick={resume}>Follow latest</button>
        </div>
      ) : null}
    </Card>
  );
}

const JOY_KIND = { pad: "Pad", axis: "Axis", button: "Button" };

function JoystickLogPanel({ entries, pads }) {
  const rows = entries || [];
  const { listRef, follow, onScroll, markUser, resume } = useStickToBottom(rows);

  return (
    <Card className="feed-card console-card board-log">
      <div className="panel-head">
        <SectionLabel kicker={rows.length ? String(rows.length) : "idle"}>Joystick</SectionLabel>
        <StatusDot state={pads ? "ok" : "warn"} label={pads ? "Listening" : "No joystick"} />
      </div>
      <p className="console-path">Browser gamepad · logged while control is off</p>
      <div
        className="feed-list console-log"
        ref={listRef}
        onScroll={onScroll}
        onWheel={markUser}
        onPointerDown={markUser}
        onTouchMove={markUser}
      >
        {rows.map((entry) => (
          <div className="feed-row" key={entry.id}>
            <span className="feed-ts">{fmtSiteNow(RAPP_CONFIG.site.clockTz, new Date(entry.ts))}</span>
            <span className={"feed-badge " + (entry.kind === "pad" && entry.detail === "Disconnected" ? "feed-badge-warn" : "feed-badge-act")}>
              {JOY_KIND[entry.kind] || entry.kind}
            </span>
            <span className="feed-text">
              <strong>{entry.name}</strong>
              {entry.detail ? <span className="console-detail">{entry.detail}</span> : null}
            </span>
          </div>
        ))}
        {!rows.length ? (
          <p className="pad-hint">Move a stick or press a button. Events are logged while control is off.</p>
        ) : null}
      </div>
      {!follow ? (
        <div className="act-row">
          <button type="button" className="act-btn" onClick={resume}>Follow latest</button>
        </div>
      ) : null}
    </Card>
  );
}

Object.assign(window, { ActionLogPanel, JoystickLogPanel });
