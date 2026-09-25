/* The Line Breaker — Service Worker (Web Push).
 * Scope: / (served from /sw.js). Shows a branded notification when the
 * +EV Board finds a new edge, and opens the board on tap.
 */
self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    /* plain-text payload fallback */
    data = { body: event.data ? event.data.text() : "" };
  }
  const title = data.title || "The Line Breaker";
  const options = {
    body: data.body || "New +EV edge on the board.",
    icon: "/static/img/apple-touch-icon.png",
    badge: "/static/img/apple-touch-icon.png",
    data: { url: data.url || "/ev-board" },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url =
    (event.notification.data && event.notification.data.url) || "/ev-board";
  event.waitUntil(
    clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((list) => {
        for (const c of list) {
          if (c.url.indexOf(url) !== -1 && "focus" in c) return c.focus();
        }
        if (clients.openWindow) return clients.openWindow(url);
        return null;
      })
  );
});
