// tabs/ZmqTab.jsx — lidar and odometry forward rates, plus ZMQ control messages.

function zmqClock(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return "—";
  return fmtSiteNow("Asia/Singapore", new Date(n * 1000));
}

function ZmqTab() {
  const [snap, setSnap] = React.useState(null);
  const [fault, setFault] = React.useState("");

  React.useEffect(() => {
    let stop = false;

    async function tick() {
      try {
        const body = await bridgeSend("/zmq");
        if (stop) return;
        setSnap(body);
        setFault("");
      } catch (err) {
        if (!stop) setFault(err.message || "ZMQ status unavailable");
      }
    }

    tick();
    const id = setInterval(tick, 1000);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, []);

  const forward = (snap && snap.forward) || {};
  const lidar = forward.lidar || {};
  const odom = forward.odom || {};
  const control = (snap && snap.control) || {};
  const messages = Array.isArray(control.messages) ? control.messages : [];
  const sources = Array.isArray(odom.sources) ? odom.sources : [];
  const forwardUp = !!forward.ok;
  const listenUp = !!(snap && snap.running);

  const kpis = [
    { label: "Lidar", kicker: "pkt/s", value: forwardUp ? fmtNumber(lidar.pkt_s) : "—", icon: "signal", bad: !forwardUp || !lidar.up },
    { label: "Points", kicker: "pt/s", value: forwardUp ? fmtNumber(lidar.pt_s) : "—", icon: "grid", bad: !forwardUp || !lidar.up },
    { label: "Odometry", kicker: "msg/s", value: forwardUp ? fmtNumber(odom.msg_s) : "—", icon: "gauge", bad: !forwardUp || !odom.up },
    { label: "Control", kicker: "msg/s", value: snap ? fmtNumber(control.hz) : "—", icon: "bolt", bad: !listenUp },
  ];

  return (
    <div>
      <KpiStrip items={kpis} />
      {fault ? <p className="drive-notice"><span className="feed-badge feed-badge-warn">Fail</span>{fault}</p> : null}
      <div className="row-2">
        <Card>
          <div className="panel-head">
            <SectionLabel kicker={forward.pub || "pub"}>Forward</SectionLabel>
            <StatusDot state={forwardUp ? "ok" : "bad"} label={forwardUp ? "Live" : "Down"} />
          </div>
          <p className="console-path">{forward.push || "Waiting for the forwarder"}</p>
          {forward.error ? <p className="pad-hint" style={{ textAlign: "left" }}>{forward.error}</p> : null}
          <div className="table-scroll">
            <table className="dtable">
              <thead>
                <tr>
                  <th>Stream</th>
                  <th className="num">Rate</th>
                  <th className="num">Drops</th>
                  <th className="num">Total</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>Lidar {lidar.bind || ""}</td>
                  <td className="num">{fmtNumber(lidar.pkt_s)} pkt/s</td>
                  <td className="num">{fmtNumber(lidar.drops)}</td>
                  <td className="num">{fmtNumber(lidar.total)}</td>
                </tr>
                <tr>
                  <td>Odometry {odom.iface || ""}</td>
                  <td className="num">{fmtNumber(odom.msg_s)} msg/s</td>
                  <td className="num">{fmtNumber(odom.drops)}</td>
                  <td className="num">{fmtNumber(odom.total)}</td>
                </tr>
                {sources.map((row) => (
                  <tr key={row.id}>
                    <td className="dim">{row.name}</td>
                    <td className="num">{fmtNumber(row.hz, 1)} Hz</td>
                    <td className="num dim">{row.age_ms == null ? "—" : row.age_ms + " ms"}</td>
                    <td className="num">{fmtNumber(row.sent)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {lidar.error ? <p className="pad-hint" style={{ textAlign: "left" }}>{lidar.error}</p> : null}
          {odom.error ? <p className="pad-hint" style={{ textAlign: "left" }}>{odom.error}</p> : null}
        </Card>
        <Card className="feed-card">
          <div className="panel-head">
            <SectionLabel kicker={snap && snap.listen ? snap.listen : "pull"}>Control</SectionLabel>
            <StatusDot state={listenUp ? "ok" : "bad"} label={listenUp ? "Listening" : "Down"} />
          </div>
          <p className="console-path">
            {fmtNumber(control.total)} received
            {snap && snap.error ? " · " + snap.error : ""}
          </p>
          <div className="feed-list">
            {messages.length === 0 ? (
              <p className="pad-hint">No ZMQ control messages yet.</p>
            ) : messages.map((msg, index) => (
              <div className="feed-row" key={msg.ts + "-" + index}>
                <span className="feed-ts">{zmqClock(msg.ts)}</span>
                <span className={"feed-badge " + (msg.ok ? "feed-badge-ok" : "feed-badge-warn")}>{msg.type || "?"}</span>
                <span className="feed-text">
                  {msg.detail || (msg.ok ? "received" : "rejected")}
                  {msg.error ? <span className="console-detail">{msg.error}</span> : null}
                </span>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}

Object.assign(window, { ZmqTab });
