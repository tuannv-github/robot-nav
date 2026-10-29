// tabs/VideoTab.jsx — subscribe to a MediaMTX topic over WebRTC; tune camera by topic.

const { useEffect, useRef, useState, useCallback } = React;

const PREFERRED_TOPIC = "stream/g1/front";

function waitForIce(pc) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      pc.removeEventListener("icegatheringstatechange", onChange);
      resolve();
    };
    const onChange = () => {
      if (pc.iceGatheringState === "complete") done();
    };
    pc.addEventListener("icegatheringstatechange", onChange);
    setTimeout(done, 800);
  });
}

function tuneReceiver(receiver) {
  if (!receiver) return;
  try {
    if ("playoutDelayHint" in receiver) receiver.playoutDelayHint = 0;
  } catch (_err) { /* older browsers */ }
  try {
    if ("jitterBufferTarget" in receiver) receiver.jitterBufferTarget = 0;
  } catch (_err) { /* property may reject 0 */ }
}

async function subscribeWhep(topic, video) {
  const pc = new RTCPeerConnection({
    bundlePolicy: "max-bundle",
    iceServers: [],
  });
  pc.addTransceiver("video", { direction: "recvonly" });
  pc.ontrack = (event) => {
    const stream = event.streams && event.streams[0]
      ? event.streams[0]
      : new MediaStream([event.track]);
    video.srcObject = stream;
    tuneReceiver(event.receiver);
    video.play().catch(() => {});
  };
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  await waitForIce(pc);
  const response = await fetch("/video/whep?path=" + encodeURIComponent(topic), {
    method: "POST",
    headers: { "Content-Type": "application/sdp" },
    body: pc.localDescription.sdp,
  });
  const answer = await response.text();
  if (!response.ok) {
    pc.close();
    throw new Error(answer || ("subscribe failed (" + response.status + ")"));
  }
  await pc.setRemoteDescription({ type: "answer", sdp: answer });
  pc.getReceivers().forEach(tuneReceiver);
  return pc;
}

const LAYOUT_AXIS = [1, 2, 3, 4];

function ctrlMeta(camera, name) {
  return camera && camera.controls && camera.controls[name] ? camera.controls[name] : null;
}

function emptySlots() {
  return Array(16).fill("");
}

function StreamTile({ topic, topics, selected, onSelect, onTopic }) {
  const videoRef = useRef(null);
  const pcRef = useRef(null);
  const [status, setStatus] = useState(topic ? "connecting" : "idle");
  const [error, setError] = useState("");
  const known = topics.some((item) => item.name === topic);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return undefined;
    if (pcRef.current) {
      pcRef.current.close();
      pcRef.current = null;
    }
    video.srcObject = null;
    if (!topic) {
      setStatus("idle");
      setError("");
      return undefined;
    }
    let cancelled = false;
    setStatus("connecting");
    setError("");
    subscribeWhep(topic, video).then((pc) => {
      if (cancelled) {
        pc.close();
        return;
      }
      pcRef.current = pc;
      setStatus("live");
      pc.addEventListener("connectionstatechange", () => {
        if (cancelled) return;
        if (pc.connectionState === "failed" || pc.connectionState === "disconnected") {
          setStatus(pc.connectionState);
        } else if (pc.connectionState === "connected") {
          setStatus("live");
        }
      });
    }).catch((exc) => {
      if (!cancelled) {
        setStatus("error");
        setError(String(exc.message || exc));
      }
    });
    return () => {
      cancelled = true;
      if (pcRef.current) {
        pcRef.current.close();
        pcRef.current = null;
      }
      if (video.srcObject) video.srcObject = null;
    };
  }, [topic]);

  return (
    <div
      className={"video-cell" + (selected ? " is-selected" : "")}
      onClick={onSelect}
    >
      <div className="video-cell-bar">
        <select
          className="joy-select"
          aria-label="Stream topic"
          value={topic}
          onClick={(event) => event.stopPropagation()}
          onChange={(event) => onTopic(event.target.value)}
        >
          <option value="">{topics.length ? "Select a topic" : "No topics"}</option>
          {topic && !known ? <option value={topic}>{topic}</option> : null}
          {topics.map((item) => (
            <option key={item.name} value={item.name}>
              {(item.ready ? "" : "offline · ")
                + item.name
                + (item.controllable ? " · cam" : "")}
            </option>
          ))}
        </select>
        <span className={"video-status video-status-" + status}>{status}</span>
      </div>
      <div className="video-stage">
        <video ref={videoRef} className="video-player" autoPlay muted playsInline />
        {!topic ? <p className="video-empty">No stream</p> : null}
        {error ? <p className="video-error">{error}</p> : null}
      </div>
    </div>
  );
}

function VideoTab() {
  const putTimer = useRef(null);
  const filledDefault = useRef(false);
  const layoutSig = useRef("");
  const [topics, setTopics] = useState([]);
  const [server, setServer] = useState("");
  const [rows, setRows] = useState(1);
  const [cols, setCols] = useState(1);
  const [slots, setSlots] = useState(emptySlots);
  const [selected, setSelected] = useState(0);
  const [layoutReady, setLayoutReady] = useState(false);
  const [error, setError] = useState("");
  const [camera, setCamera] = useState(null);
  const [draft, setDraft] = useState({ zoom: null, focus: null, focus_auto: 0 });
  const [busy, setBusy] = useState(false);
  const [timestampOn, setTimestampOn] = useState(true);
  const [timestampBusy, setTimestampBusy] = useState(false);

  const loadCamera = useCallback(async (path, { silent } = {}) => {
    if (!path) {
      setCamera(null);
      return;
    }
    try {
      const response = await fetch("/video/camera?path=" + encodeURIComponent(path));
      const payload = await response.json();
      if (!response.ok || payload.ok === false) {
        if (!silent) setError(payload.error || "camera status failed");
        setCamera(payload.controllable ? payload : { ...payload, controllable: false });
        return;
      }
      setCamera(payload);
      if (payload.controllable) {
        setDraft({
          zoom: payload.controls?.zoom?.value ?? null,
          focus: payload.controls?.focus?.value ?? null,
          focus_auto: payload.controls?.focus_auto?.value ?? 0,
        });
      }
    } catch (exc) {
      if (!silent) setError(String(exc.message || exc));
      setCamera(null);
    }
  }, []);

  useEffect(() => {
    let stopped = false;

    async function loadTopics() {
      try {
        const response = await fetch("/video/paths");
        const payload = await response.json();
        if (stopped) return;
        const next = Array.isArray(payload.topics) ? payload.topics : [];
        setTopics(next);
        if (payload.host) setServer(String(payload.host));
        if (!payload.ok) setError(payload.error || "Could not list topics");
      } catch (exc) {
        if (!stopped) setError(String(exc.message || exc));
      }
    }

    loadTopics();
    const timer = setInterval(loadTopics, 2000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, []);

  const adoptLayout = useCallback((payload) => {
    const nextRows = LAYOUT_AXIS.indexOf(Number(payload.rows)) >= 0 ? Number(payload.rows) : 1;
    const nextCols = LAYOUT_AXIS.indexOf(Number(payload.cols)) >= 0 ? Number(payload.cols) : 1;
    const nextSlots = emptySlots();
    (Array.isArray(payload.slots) ? payload.slots : []).slice(0, 16).forEach((item, index) => {
      nextSlots[index] = String(item || "");
    });
    const sig = nextRows + "x" + nextCols + "\n" + nextSlots.join("\n");
    if (sig === layoutSig.current) return;
    layoutSig.current = sig;
    setRows(nextRows);
    setCols(nextCols);
    setSlots(nextSlots);
    setSelected((index) => Math.min(index, nextRows * nextCols - 1));
  }, []);

  useEffect(() => {
    let stopped = false;
    fetch("/video/layout")
      .then((response) => response.json())
      .then((payload) => {
        if (stopped) return;
        if (payload.ok) adoptLayout(payload);
        setLayoutReady(true);
      })
      .catch((exc) => {
        if (!stopped) setError(String(exc.message || exc));
        setLayoutReady(true);
      });
    return () => {
      stopped = true;
    };
  }, [adoptLayout]);

  useEffect(() => {
    if (!layoutReady) return undefined;
    let stopped = false;
    async function refresh() {
      try {
        const response = await fetch("/video/layout");
        const payload = await response.json();
        if (stopped || !payload.ok) return;
        adoptLayout(payload);
      } catch (_exc) { /* keep the last shared layout */ }
    }
    const timer = setInterval(refresh, 2000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [layoutReady, adoptLayout]);

  const saveLayout = useCallback(async (nextRows, nextCols, nextSlots) => {
    const stored = emptySlots();
    nextSlots.slice(0, 16).forEach((item, index) => {
      stored[index] = String(item || "");
    });
    layoutSig.current = nextRows + "x" + nextCols + "\n" + stored.join("\n");
    try {
      const response = await fetch("/video/layout", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rows: nextRows, cols: nextCols, slots: stored }),
      });
      const payload = await response.json();
      if (!response.ok || payload.ok === false) {
        layoutSig.current = "";
        setError(payload.error || "could not save stream layout");
        return;
      }
      adoptLayout(payload);
      setError("");
    } catch (exc) {
      layoutSig.current = "";
      setError(String(exc.message || exc));
    }
  }, [adoptLayout]);

  useEffect(() => {
    if (!layoutReady || filledDefault.current || !topics.length) return;
    filledDefault.current = true;
    if (slots.some((item) => item)) return;
    const preferred = topics.some((item) => item.name === PREFERRED_TOPIC)
      ? PREFERRED_TOPIC
      : ((topics.find((item) => item.ready) || topics[0] || {}).name || "");
    if (!preferred) return;
    const next = slots.slice();
    next[0] = preferred;
    setSlots(next);
    saveLayout(rows, cols, next);
  }, [layoutReady, topics, slots, rows, cols, saveLayout]);

  const topic = slots[selected] || "";

  useEffect(() => {
    if (!topic) {
      setCamera(null);
      return undefined;
    }
    let stopped = false;
    loadCamera(topic);
    const timer = setInterval(() => {
      if (!stopped) loadCamera(topic, { silent: true });
    }, 3000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [topic, loadCamera]);

  useEffect(() => () => {
    if (putTimer.current) clearTimeout(putTimer.current);
  }, []);

  useEffect(() => {
    if (!topic) {
      setTimestampOn(true);
      return undefined;
    }
    let stopped = false;
    fetch("/video/overlay?path=" + encodeURIComponent(topic))
      .then((response) => response.json())
      .then((payload) => {
        if (stopped || !payload.ok) return;
        setTimestampOn(!!payload.timestamp);
      })
      .catch(() => {});
    return () => {
      stopped = true;
    };
  }, [topic]);

  const setTimestamp = useCallback(async (enabled) => {
    if (!topic) return;
    const previous = timestampOn;
    setTimestampOn(enabled);
    setTimestampBusy(true);
    try {
      const response = await fetch("/video/overlay?path=" + encodeURIComponent(topic), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ timestamp: enabled }),
      });
      const payload = await response.json();
      if (!response.ok || payload.ok === false) {
        setTimestampOn(previous);
        setError(payload.error || "timestamp update failed");
        return;
      }
      setError("");
      setTimestampOn(!!payload.timestamp);
    } catch (exc) {
      setTimestampOn(previous);
      setError(String(exc.message || exc));
    } finally {
      setTimestampBusy(false);
    }
  }, [topic, timestampOn]);

  const applyControls = useCallback(async (path, body) => {
    setBusy(true);
    try {
      const response = await fetch("/video/camera?path=" + encodeURIComponent(path), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const payload = await response.json();
      if (!response.ok || payload.ok === false) {
        setError(payload.error || "camera update failed");
        return;
      }
      setError("");
      setCamera(payload);
      setDraft({
        zoom: payload.controls?.zoom?.value ?? null,
        focus: payload.controls?.focus?.value ?? null,
        focus_auto: payload.controls?.focus_auto?.value ?? 0,
      });
    } catch (exc) {
      setError(String(exc.message || exc));
    } finally {
      setBusy(false);
    }
  }, []);

  const schedulePut = useCallback((body) => {
    if (!topic) return;
    if (putTimer.current) clearTimeout(putTimer.current);
    putTimer.current = setTimeout(() => applyControls(topic, body), 120);
  }, [topic, applyControls]);

  const zoom = ctrlMeta(camera, "zoom");
  const focus = ctrlMeta(camera, "focus");
  const focusAuto = ctrlMeta(camera, "focus_auto");
  const controllable = !!(camera && camera.controllable);
  const tileCount = rows * cols;
  const visible = slots.slice(0, tileCount);

  function chooseAxis(kind, value) {
    const size = Number(value);
    const nextRows = kind === "rows" ? size : rows;
    const nextCols = kind === "cols" ? size : cols;
    if (nextRows * nextCols > 16) return;
    setRows(nextRows);
    setCols(nextCols);
    setSelected((index) => Math.min(index, nextRows * nextCols - 1));
    saveLayout(nextRows, nextCols, slots);
  }

  function chooseSlot(index, nextTopic) {
    const next = slots.slice();
    next[index] = nextTopic;
    setSlots(next);
    setSelected(index);
    saveLayout(rows, cols, next);
  }

  return (
    <div className="video-page">
      <div className="video-toolbar">
        <label className="video-server">
          <span className="field-label">Server</span>
          <span className="video-server-ip">{server || "—"}</span>
        </label>
        <label className="video-count">
          <span className="field-label">Rows</span>
          <select
            className="joy-select"
            aria-label="Rows"
            value={rows}
            onChange={(event) => chooseAxis("rows", event.target.value)}
          >
            {LAYOUT_AXIS.map((size) => (
              <option key={size} value={size} disabled={size * cols > 16}>{size}</option>
            ))}
          </select>
        </label>
        <label className="video-count">
          <span className="field-label">Columns</span>
          <select
            className="joy-select"
            aria-label="Columns"
            value={cols}
            onChange={(event) => chooseAxis("cols", event.target.value)}
          >
            {LAYOUT_AXIS.map((size) => (
              <option key={size} value={size} disabled={rows * size > 16}>{size}</option>
            ))}
          </select>
        </label>
        <span className="video-slot-label">Tile {selected + 1}</span>
        <label className="video-check video-stamp">
          <input
            type="checkbox"
            checked={timestampOn}
            disabled={!topic || timestampBusy}
            onChange={(event) => setTimestamp(event.target.checked)}
          />
          <span>Timestamp</span>
        </label>
      </div>

      {controllable ? (
        <div className="video-controls">
          <div className="video-cam-meta">
            <span>{camera.camera || "Camera"}</span>
            <span className="video-cam-device">{camera.device}</span>
          </div>
          {zoom ? (
            <label className="video-ctrl">
              <span className="field-label">
                Zoom <strong>{draft.zoom ?? zoom.value}</strong>
                <span className="video-ctrl-hint"> (lower = wider)</span>
              </span>
              <input
                className="stick-range"
                type="range"
                min={zoom.min}
                max={zoom.max}
                step={zoom.step || 1}
                value={draft.zoom ?? zoom.value}
                disabled={busy}
                onChange={(event) => {
                  const value = Number(event.target.value);
                  setDraft((prev) => ({ ...prev, zoom: value }));
                  schedulePut({ zoom: value });
                }}
              />
              <div className="video-ctrl-actions">
                <button
                  type="button"
                  className="icon-btn"
                  disabled={busy || (draft.zoom ?? zoom.value) === zoom.min}
                  onClick={() => {
                    setDraft((prev) => ({ ...prev, zoom: zoom.min }));
                    applyControls(topic, { zoom: zoom.min });
                  }}
                >
                  Widest
                </button>
              </div>
            </label>
          ) : null}
          {focus ? (
            <label className="video-ctrl">
              <span className="field-label">
                Focus <strong>{draft.focus ?? focus.value}</strong>
              </span>
              <input
                className="stick-range"
                type="range"
                min={focus.min}
                max={focus.max}
                step={focus.step || 1}
                value={draft.focus ?? focus.value}
                disabled={busy || !!(draft.focus_auto ?? focusAuto?.value)}
                onChange={(event) => {
                  const value = Number(event.target.value);
                  setDraft((prev) => ({ ...prev, focus: value, focus_auto: 0 }));
                  schedulePut({ focus: value, focus_auto: 0 });
                }}
              />
            </label>
          ) : null}
          {focusAuto ? (
            <label className="video-check">
              <input
                type="checkbox"
                checked={!!(draft.focus_auto ?? focusAuto.value)}
                disabled={busy}
                onChange={(event) => {
                  const value = event.target.checked ? 1 : 0;
                  setDraft((prev) => ({ ...prev, focus_auto: value }));
                  applyControls(topic, { focus_auto: value });
                }}
              />
              <span>Autofocus</span>
            </label>
          ) : null}
        </div>
      ) : topic ? (
        <p className="video-cam-note">No camera controls for this topic.</p>
      ) : null}

      {error ? <p className="video-cam-note video-error-inline">{error}</p> : null}

      <div
        className="video-grid"
        style={{
          gridTemplateColumns: "repeat(" + cols + ", minmax(0, 1fr))",
          gridTemplateRows: "repeat(" + rows + ", minmax(0, 1fr))",
        }}
      >
        {visible.map((slotTopic, index) => (
          <StreamTile
            key={index}
            topic={slotTopic}
            topics={topics}
            selected={index === selected}
            onSelect={() => setSelected(index)}
            onTopic={(nextTopic) => chooseSlot(index, nextTopic)}
          />
        ))}
      </div>
    </div>
  );
}

Object.assign(window, { VideoTab });
