// lib/bridge.jsx — G1 controller REST client.

function bridgeOrigin() {
  const params = new URLSearchParams(window.location.search);
  const api = params.get("api");
  if (api) return String(api).replace(/\/$/, "");
  if (window.location.pathname.indexOf("/console") === 0) return window.location.origin;
  const host = window.location.hostname || "127.0.0.1";
  return "http://" + host + ":8081";
}

async function bridgeSend(path, options) {
  const opts = options || {};
  let res;
  try {
    res = await fetch(bridgeOrigin() + path, {
      method: opts.method || "GET",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: opts.body,
    });
  } catch (err) {
    throw new Error("Bridge unreachable");
  }
  let body = null;
  try { body = await res.json(); } catch (e) { body = null; }
  if (!res.ok) {
    const detail = body && (body.detail || body.message) ? (body.detail || body.message) : (res.status + " " + res.statusText);
    const err = new Error(typeof detail === "string" ? detail : "Request failed");
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

function bridgeHalt() {
  const zero = JSON.stringify({ lx: 0, ly: 0, rx: 0, ry: 0, keys: 0 });
  return Promise.all([
    bridgeSend("/wireless", { method: "POST", body: zero }).catch(() => null),
    bridgeSend("/cmd_vel/stop", { method: "POST" }).catch(() => null),
  ]);
}

function startDriveKeepAlive() {
  let ctx = null;
  let osc = null;
  let wake = null;

  async function arm() {
    try {
      if (navigator.wakeLock && navigator.wakeLock.request) {
        wake = await navigator.wakeLock.request("screen");
      }
    } catch (err) {
      wake = null;
    }
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    try {
      if (!ctx) ctx = new AudioCtx();
      if (ctx.state === "suspended") await ctx.resume();
      if (!osc && ctx.state === "running") {
        osc = ctx.createOscillator();
        const gain = ctx.createGain();
        gain.gain.value = 0.00008;
        osc.frequency.value = 20;
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start();
      }
    } catch (err) {
      osc = null;
    }
  }

  function onVisible() {
    if (!document.hidden) arm();
  }

  window.addEventListener("pointerdown", arm);
  window.addEventListener("keydown", arm);
  document.addEventListener("visibilitychange", onVisible);
  arm();
  return function stop() {
    window.removeEventListener("pointerdown", arm);
    window.removeEventListener("keydown", arm);
    document.removeEventListener("visibilitychange", onVisible);
    try { if (osc) osc.stop(); } catch (err) { /* already stopped */ }
    try { if (ctx) ctx.close(); } catch (err) { /* already closed */ }
    try { if (wake) wake.release(); } catch (err) { /* already released */ }
  };
}

function startDriveClock(onTick, periodMs) {
  let stop = false;
  let worker = null;
  const period = periodMs || 50;

  function pulse() {
    if (!stop) onTick();
  }

  try {
    const blob = new Blob(
      ["setInterval(function(){postMessage(1)}," + period + ")"],
      { type: "text/javascript" },
    );
    worker = new Worker(URL.createObjectURL(blob));
    worker.onmessage = pulse;
  } catch (err) {
    worker = null;
  }
  const id = window.setInterval(pulse, period);
  return function clear() {
    stop = true;
    window.clearInterval(id);
    if (worker) {
      try { worker.terminate(); } catch (err) { /* already gone */ }
    }
  };
}

function useBridgeLive(actions) {
  React.useEffect(() => {
    let stop = false;

    async function tick() {
      const next = {};
      try {
        const health = await bridgeSend("/health");
        next.link = health && health.sdk_ready ? "live" : "offline";
        next.linkDetail = health && health.sdk_ready ? "Live" : "SDK down";
        next.health = health;
      } catch (err) {
        next.link = "offline";
        next.linkDetail = "Offline";
        next.health = null;
      }
      if (stop) return;
      actions.setState((prev) => Object.assign({}, prev, next));
    }

    tick();
    const id = setInterval(tick, 1000);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, [actions]);
}

Object.assign(window, {
  bridgeOrigin,
  bridgeSend,
  bridgeHalt,
  useBridgeLive,
  startDriveKeepAlive,
  startDriveClock,
});
