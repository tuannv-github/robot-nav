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

function ctrlMeta(camera, name) {
  return camera && camera.controls && camera.controls[name] ? camera.controls[name] : null;
}

function VideoTab() {
  const videoRef = useRef(null);
  const pcRef = useRef(null);
  const putTimer = useRef(null);
  const [topics, setTopics] = useState([]);
  const [topic, setTopic] = useState("");
  const [status, setStatus] = useState("idle");
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
        if (!payload.ok) setError(payload.error || "Could not list topics");
        setTopic((current) => {
          if (current && next.some((item) => item.name === current)) return current;
          if (next.some((item) => item.name === PREFERRED_TOPIC)) return PREFERRED_TOPIC;
          const ready = next.find((item) => item.ready);
          return (ready || next[0] || {}).name || "";
        });
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

  return (
    <div className="video-page">
      <div className="video-toolbar">
        <label className="video-topic">
          <span className="field-label">Topic</span>
          <select
            className="joy-select"
            aria-label="Video topics"
            value={topic}
            onChange={(event) => setTopic(event.target.value)}
          >
            <option value="">{topics.length ? "Select a topic" : "No topics"}</option>
            {topics.map((item) => (
              <option key={item.name} value={item.name}>
                {(item.ready ? "" : "offline · ")
                  + item.name
                  + (item.controllable ? " · cam" : "")}
              </option>
            ))}
          </select>
        </label>
        <label className="video-check video-stamp">
          <input
            type="checkbox"
            checked={timestampOn}
            disabled={!topic || timestampBusy}
            onChange={(event) => setTimestamp(event.target.checked)}
          />
          <span>Timestamp</span>
        </label>
        <span className={"video-status video-status-" + status}>{status}</span>
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

      <div className="video-stage">
        <video
          ref={videoRef}
          className="video-player"
          autoPlay
          muted
          playsInline
        />
        {!topic ? <p className="video-empty">No stream selected</p> : null}
        {error ? <p className="video-error">{error}</p> : null}
      </div>
    </div>
  );
}

Object.assign(window, { VideoTab });
