/* The Sharp Team — Web Push client (v3, 2026-10-16).
 * Arreglo del botón que "no hacía nada" (reporte de Alex 2026-10-15):
 *  - status() verifica la SUSCRIPCIÓN REAL (pushManager.getSubscription),
 *    no solo Notification.permission: si hay permiso pero no suscripción,
 *    devuelve 'unsubscribed' y el botón ofrece reconectar.
 *  - enable() re-sincroniza una suscripción existente con el servidor antes
 *    de crear una nueva, y guarda el motivo en TLB_push.lastError.
 * Usage: await window.TLB_push.enable()
 *          -> 'granted' | 'denied' | 'unsupported' | 'error'
 *        await window.TLB_push.status()
 *          -> 'granted' | 'denied' | 'default' | 'unsubscribed' | 'unsupported'
 */
window.TLB_push = (function () {
  "use strict";

  var api = { lastError: "" };

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

  async function saveSubscription(sub) {
    const saveRes = await fetch("/push/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sub.toJSON()),
    });
    return saveRes.ok;
  }

  async function enable() {
    api.lastError = "";
    if (!supported()) return "unsupported";
    try {
      const reg = await navigator.serviceWorker.register("/sw.js");
      await navigator.serviceWorker.ready;
      const perm = await Notification.requestPermission();
      if (perm === "denied") return "denied";
      if (perm !== "granted") return "default";
      // Si ya existe una suscripción en este aparato, re-sincronizarla con
      // el servidor (cubre el caso "permiso sí, suscripción perdida").
      let sub = null;
      try {
        sub = await reg.pushManager.getSubscription();
      } catch (e) {
        sub = null;
      }
      if (sub) {
        return (await saveSubscription(sub)) ? "granted" : "error";
      }
      const keyRes = await fetch("/push/vapid-public-key");
      if (!keyRes.ok) {
        api.lastError = "vapid-key";
        return "error";
      }
      const keyJson = await keyRes.json();
      if (!keyJson.publicKey) {
        api.lastError = "vapid-key";
        return "error";
      }
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(keyJson.publicKey),
      });
      if (!(await saveSubscription(sub))) {
        api.lastError = "save";
        return "error";
      }
      return "granted";
    } catch (e) {
      api.lastError = (e && e.name) || "error";
      return "error";
    }
  }

  async function disable() {
    api.lastError = "";
    if (!supported()) return "unsupported";
    try {
      const reg = await navigator.serviceWorker.getRegistration();
      if (!reg) return "off";
      const sub = await reg.pushManager.getSubscription();
      if (!sub) return "off";
      const endpoint = sub.endpoint;
      try {
        await sub.unsubscribe();
      } catch (e) {
        /* seguimos: hay que limpiar el servidor igual */
      }
      try {
        await fetch("/push/unsubscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ endpoint: endpoint }),
        });
      } catch (e) {
        /* la fila huerfana se poda sola en el proximo envio fallido */
      }
      return "off";
    } catch (e) {
      api.lastError = (e && e.name) || "error";
      return "error";
    }
  }

  async function status() {
    if (!supported()) return "unsupported";
    const perm = Notification.permission;
    if (perm !== "granted") return perm;
    // Permiso concedido: lo que manda es la suscripción real de este aparato.
    try {
      const reg = await navigator.serviceWorker.getRegistration();
      if (!reg) return "unsubscribed";
      const sub = await reg.pushManager.getSubscription();
      return sub ? "granted" : "unsubscribed";
    } catch (e) {
      return perm;
    }
  }

  api.enable = enable;
  api.disable = disable;
  api.status = status;
  return api;
})();
