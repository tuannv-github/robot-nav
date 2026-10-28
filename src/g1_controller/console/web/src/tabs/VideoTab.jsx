// tabs/VideoTab.jsx — subscribe to a MediaMTX topic over WebRTC (lowest latency).

const { useEffect, useRef, useState } = React;

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

function VideoTab() {
  const videoRef = useRef(null);
  const pcRef = useRef(null);
  const [topics, setTopics] = useState([]);
  const [topic, setTopic] = useState("");
  const [status, setStatus] = useState("idle");
  const [error, setError] = useState("");

  useEffect(() => {
    let stopped = false;

    async function loadTopics() {
      try {
        const response = await fetch("/video/paths");
        const payload = await response.json();
        if (stopped) return;
        const next = Array.isArray(payload.topics) ? payload.topics : [];
        setTopics(next);
        setError(payload.ok ? "" : (payload.error || "Could not list topics"));
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
                {(item.ready ? "" : "offline · ") + item.name}
              </option>
            ))}
          </select>
        </label>
        <span className={"video-status video-status-" + status}>{status}</span>
      </div>
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
