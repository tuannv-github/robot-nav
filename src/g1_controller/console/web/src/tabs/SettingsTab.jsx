// tabs/SettingsTab.jsx — HTTP, WebSocket, and ZMQ interface settings.

function SettingsTab() {
  const [form, setForm] = React.useState(null);
  const [database, setDatabase] = React.useState("");
  const [runtime, setRuntime] = React.useState(null);
  const [fault, setFault] = React.useState("");
  const [notice, setNotice] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  function applyPayload(body) {
    setForm(body.interfaces);
    setDatabase(body.database || "");
    setRuntime(body.runtime || null);
  }

  React.useEffect(() => {
    let stop = false;
    bridgeSend("/config/interfaces")
      .then((body) => {
        if (stop || !body) return;
        applyPayload(body);
        setFault("");
      })
      .catch((err) => {
        if (!stop) setFault(err.message || "Settings unavailable");
      });
    return () => { stop = true; };
  }, []);

  function patch(name, key, value) {
    setForm((prev) => {
      if (!prev) return prev;
      return { ...prev, [name]: { ...prev[name], [key]: value } };
    });
    setNotice("");
  }

  async function save(event) {
    event.preventDefault();
    if (!form) return;
    setBusy(true);
    setFault("");
    setNotice("");
    try {
      const body = await bridgeSend("/config/interfaces", {
        method: "PUT",
        body: JSON.stringify({ interfaces: form }),
      });
      applyPayload(body);
      setNotice(body.restart
        ? "Saved to the database. Restart the controller to apply the HTTP port or WebSocket path."
        : "Saved to the database.");
    } catch (err) {
      setFault(err.message || "Save failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={save}>
      <div className="panel-head">
        <SectionLabel kicker="sqlite">Interfaces</SectionLabel>
        <StatusDot state={fault ? "bad" : "ok"} label={fault ? "Not saved" : "Database"} />
      </div>
      <p className="pad-hint settings-note">
        The web console drives the robot over HTTP. WebSocket and ZMQ accept the same stick, velocity, and stop messages. The controller log records a command from whichever interface sent it.
      </p>
      {database ? <p className="console-path" title={database}>{database}</p> : null}
      {fault ? <p className="drive-notice"><span className="feed-badge feed-badge-warn">Fail</span>{fault}</p> : null}
      {notice ? <p className="drive-notice"><span className="feed-badge feed-badge-ok">Saved</span>{notice}</p> : null}
      {form ? (
        <div className="settings-grid">
          <Card className="settings-card">
            <SectionLabel kicker={runtime && runtime.http ? runtime.http.port : "port"}>HTTP</SectionLabel>
            <label className="field check-row">
              <input type="checkbox" checked disabled />
              <span className="field-label">Enabled. The console uses this interface.</span>
            </label>
            <label className="field">
              <span className="field-label">Host</span>
              <input
                className="field-input"
                value={form.http.host}
                onChange={(event) => patch("http", "host", event.target.value)}
              />
            </label>
            <label className="field">
              <span className="field-label">Port</span>
              <input
                className="field-input"
                inputMode="numeric"
                value={form.http.port}
                onChange={(event) => patch("http", "port", event.target.value)}
              />
            </label>
          </Card>
          <Card className="settings-card">
            <SectionLabel kicker={form.ws.enabled ? "on" : "off"}>WebSocket</SectionLabel>
            <label className="field check-row">
              <input
                type="checkbox"
                checked={!!form.ws.enabled}
                onChange={(event) => patch("ws", "enabled", event.target.checked)}
              />
              <span className="field-label">Enabled</span>
            </label>
            <label className="field">
              <span className="field-label">Path</span>
              <input
                className="field-input"
                value={form.ws.path}
                onChange={(event) => patch("ws", "path", event.target.value)}
              />
            </label>
            <p className="pad-hint">Uses the HTTP host and port. Clients send type stick, cmd_vel, or stop.</p>
          </Card>
          <Card className="settings-card">
            <SectionLabel kicker={form.zmq.enabled ? "on" : "off"}>ZMQ</SectionLabel>
            <label className="field check-row">
              <input
                type="checkbox"
                checked={!!form.zmq.enabled}
                onChange={(event) => patch("zmq", "enabled", event.target.checked)}
              />
              <span className="field-label">Enabled</span>
            </label>
            <label className="field">
              <span className="field-label">Endpoint</span>
              <input
                className="field-input"
                value={form.zmq.endpoint}
                onChange={(event) => patch("zmq", "endpoint", event.target.value)}
              />
            </label>
            {runtime && runtime.zmq && runtime.zmq.error ? (
              <p className="pad-hint">{runtime.zmq.error}</p>
            ) : (
              <p className="pad-hint">PULL socket. Clients PUSH the same JSON control messages.</p>
            )}
          </Card>
        </div>
      ) : (
        <p className="pad-hint">{fault ? "" : "Loading settings…"}</p>
      )}
      <div className="act-row">
        <button type="submit" className="act-btn act-btn-primary" disabled={!form || busy}>
          {busy ? "Saving…" : "Save"}
        </button>
      </div>
    </form>
  );
}

Object.assign(window, { SettingsTab });
