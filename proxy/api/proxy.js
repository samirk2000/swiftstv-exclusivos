export default async function handler(req, res) {
  const backend = (process.env.RENDER_PROXY_URL || "").replace(/\/$/, "");
  if (!backend) {
    res.status(503).json({
      error: "RENDER_PROXY_URL is not configured on Vercel.",
      hint: "Deploy proxy/Dockerfile on Render, then set RENDER_PROXY_URL.",
    });
    return;
  }

  const incoming = req.url || "/";
  const path = incoming.startsWith("/api/proxy")
    ? req.headers["x-vercel-proxy-path"] || "/"
    : incoming;
  const target = `${backend}${path}`;
  const headers = {};
  for (const [key, value] of Object.entries(req.headers || {})) {
    const lower = key.toLowerCase();
    if (
      lower === "x-forwarded-for" ||
      lower === "x-real-ip" ||
      lower === "cf-connecting-ip" ||
      lower === "referer" ||
      lower === "user-agent"
    ) {
      headers[key] = value;
    }
  }
  if (process.env.PROXY_API_KEY) {
    headers["X-Proxy-Key"] = process.env.PROXY_API_KEY;
  }

  try {
    const upstream = await fetch(target, {
      method: req.method || "GET",
      headers,
      redirect: "follow",
    });
    const status =
      upstream.status >= 300 && upstream.status < 400 ? 200 : upstream.status;
    res.status(status);
    const contentType = upstream.headers.get("content-type");
    if (contentType) {
      res.setHeader("content-type", contentType);
    }
    const body = await upstream.arrayBuffer();
    res.send(Buffer.from(body));
  } catch (error) {
    res.status(502).json({ error: String(error) });
  }
}
