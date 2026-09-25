/* The Line Breaker — Web Push client.
 * Usage: await window.TLB_push.enable() -> 'granted' | 'denied' | 'unsupported' | 'error'
 *        await window.TLB_push.status() -> Notification.permission or 'unsupported'
 */
window.TLB_push = (function () {
  "use strict";

  function supported() {
    return (
      "serviceWorker" in navigator &&
      "PushManager" in window &&
      "Notification" in window
    );
  }

  function urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding)
      .replace(/-/g, "+")
      .replace(/_/g, "/");
    const raw = atob(base64);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  async function enable() {
    if (!supported()) return "unsupported";
    try {
      const reg = await navigator.serviceWorker.register("/sw.js");
      const perm = await Notification.requestPermission();
      if (perm !== "granted") return "denied";
      const keyRes = await fetch("/push/vapid-public-key");
      if (!keyRes.ok) return "error";
      const keyJson = await keyRes.json();
      if (!keyJson.publicKey) return "error";
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(keyJson.publicKey),
      });
      const saveRes = await fetch("/push/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sub.toJSON()),
      });
      return saveRes.ok ? "granted" : "error";
    } catch (e) {
      return "error";
    }
  }

  async function status() {
    if (!supported()) return "unsupported";
    return Notification.permission;
  }

  return { enable: enable, status: status };
})();
