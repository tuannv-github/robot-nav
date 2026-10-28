// tabs/ModesTab.jsx — walk motions, other robot modes, gestures, and sport tasks.

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

function ModesTab({ state, actions }) {
  useBridgeLive(actions);
  const [modeBusy, setModeBusy] = React.useState("");
  const [gestures, setGestures] = React.useState([]);
  const [gestureBusy, setGestureBusy] = React.useState("");
  const [sport, setSport] = React.useState([]);
  const [sportBusy, setSportBusy] = React.useState("");

  React.useEffect(() => {
    let stop = false;
    bridgeSend("/arm").then((body) => {
      if (!stop) setGestures((body && body.actions) || []);
    }).catch(() => {
      if (!stop) setGestures([]);
    });
    bridgeSend("/sport").then((body) => {
      if (stop) return;
      const actionsList = (body && body.actions) || [];
      setSport(actionsList.filter((item) => (
        !MODE_SLUGS[item.slug] && (item.fsm_id == null || !MODE_FSM[item.fsm_id])
      )));
    }).catch(() => {
      if (!stop) setSport([]);
    });
    return () => { stop = true; };
  }, [state.link]);

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

  async function runGesture(slug) {
    setGestureBusy(slug);
    try {
      await bridgeSend("/arm/" + slug, { method: "POST" });
      actions.setState((prev) => ({ ...prev, notice: "" }));
    } catch (err) {
      actions.setState((prev) => ({ ...prev, notice: err.message || "Gesture failed" }));
    } finally {
      setGestureBusy("");
    }
  }

  async function runSport(slug) {
    setSportBusy(slug);
    try {
      await bridgeSend("/sport/" + slug, { method: "POST", body: "{}" });
      actions.setState((prev) => ({ ...prev, notice: "" }));
    } catch (err) {
      actions.setState((prev) => ({ ...prev, notice: err.message || "Sport action failed" }));
    } finally {
      setSportBusy("");
    }
  }

  const fsmId = state.health && state.health.fsm_id != null ? Number(state.health.fsm_id) : null;
  const fsmName = fsmLabel(state.health, "Unknown");
  const offline = state.link !== "live";
  const linkState = offline ? "bad" : "ok";

  return (
    <>
      <div className="tier">
        <Card>
          <div className="panel-head">
            <SectionLabel kicker={fsmId == null ? "fsm" : String(fsmId)}>{fsmName}</SectionLabel>
            <StatusDot state={linkState} label={state.linkDetail || "Offline"} />
          </div>
          {state.modeResult ? (
            <div className={"mode-result" + (state.modeResult.applied ? " is-ok" : " is-bad")}>
              <span className={"feed-badge " + (state.modeResult.applied ? "feed-badge-ok" : "feed-badge-warn")}>
                {state.modeResult.applied ? "Applied" : (state.modeResult.ok ? "Not applied" : "Rejected")}
              </span>
              <span>{state.modeResult.name}</span>
            </div>
          ) : null}
          <div className="mode-group">
            <SectionLabel>Walk motions</SectionLabel>
            <div className="mode-grid">
              {WALK_MODES.map((mode) => (
                <ModeChoice key={mode.slug} mode={mode} fsmId={fsmId} busy={modeBusy} offline={offline} onSelect={selectMode} />
              ))}
            </div>
          </div>
          <div className="mode-group">
            <SectionLabel>Other modes</SectionLabel>
            <div className="mode-grid">
              {OTHER_MODES.map((mode) => (
                <ModeChoice key={mode.slug} mode={mode} fsmId={fsmId} busy={modeBusy} offline={offline} onSelect={selectMode} />
              ))}
            </div>
          </div>
          {state.notice ? (
            <p className="drive-notice"><span className="feed-badge feed-badge-warn">Fail</span>{state.notice}</p>
          ) : null}
        </Card>
      </div>
      <div className="tier row-2">
        <Card>
          <SectionLabel kicker="walk motion">Gestures</SectionLabel>
          <div className="action-grid">
            {gestures.map((item) => (
              <button
                key={item.slug}
                type="button"
                className="act-btn"
                disabled={!!gestureBusy || offline}
                onClick={() => runGesture(item.slug)}
              >
                {gestureBusy === item.slug ? "…" : item.name}
              </button>
            ))}
            {!gestures.length ? <p className="pad-hint">No gestures yet. The catalog loads when the bridge is up.</p> : null}
          </div>
        </Card>
        <Card>
          <SectionLabel kicker="tasks">Sport</SectionLabel>
          <div className="action-grid">
            {sport.map((item) => (
              <button
                key={item.slug}
                type="button"
                className={"act-btn" + (item.slug === "stop-move" ? " act-btn-stop" : "")}
                disabled={!!sportBusy || offline}
                onClick={() => runSport(item.slug)}
              >
                {sportBusy === item.slug ? "…" : item.name}
              </button>
            ))}
            {!sport.length ? <p className="pad-hint">No extra sport tasks yet.</p> : null}
          </div>
        </Card>
      </div>
    </>
  );
}

Object.assign(window, { ModesTab });
