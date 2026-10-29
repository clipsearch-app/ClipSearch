const pending = new Map();
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("message", (e) => {
  const d = e.data || {};
  if (d.t !== "dl" || !e.ports[0]) return;
  const port = e.ports[0];
  const stream = new ReadableStream({
    start(c) {
      port.onmessage = (m) => {
        const x = m.data;
        if (x === "end") { try { c.close(); } catch (_) {} }
        else if (x && x.err) { try { c.error(new Error(x.err)); } catch (_) {} }
        else if (x instanceof ArrayBuffer) c.enqueue(new Uint8Array(x));
      };
    },
    pull() { port.postMessage({ pull: true }); },
    cancel() { port.postMessage({ cancel: true }); },
  }, { highWaterMark: 1 });
  pending.set(d.id, { stream, name: String(d.name || "video.mp4") });
  setTimeout(() => pending.delete(d.id), 120e3);
  port.postMessage({ ready: true });
});
self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url);
  if (u.origin !== self.location.origin || !u.pathname.startsWith("/__dl/")) return;
  const id = u.pathname.split("/")[2] || "";
  const it = pending.get(id);
  if (!it) { e.respondWith(new Response("Not found", { status: 404 })); return; }
  pending.delete(id);
  const ascii = it.name.replace(/[^\x20-\x7e]+/g, "_").replace(/"/g, "'");
  e.respondWith(new Response(it.stream, { headers: {
    "Content-Type": "application/octet-stream",
    "Content-Disposition": `attachment; filename="${ascii}"; filename*=UTF-8''${encodeURIComponent(it.name)}`,
    "X-Content-Type-Options": "nosniff",
  } }));
});
