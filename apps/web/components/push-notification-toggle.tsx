"use client";

import { useEffect, useState } from "react";
import { browserClient } from "@/lib/browser-client";
import { buttonStyle } from "@/components/ui";

// Web Push's applicationServerKey wants a raw BufferSource, not the base64url string the backend
// hands back -- standard conversion, no library needed for one function. Returns the underlying
// ArrayBuffer directly (not the Uint8Array view) since TS's DOM lib types applicationServerKey as
// BufferSource | string, and a plain ArrayBuffer satisfies that without a generic mismatch.
function urlBase64ToBuffer(base64Url: string): ArrayBuffer {
  const padding = "=".repeat((4 - (base64Url.length % 4)) % 4);
  const base64 = (base64Url + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  return Uint8Array.from([...raw].map((char) => char.charCodeAt(0))).buffer;
}

/** A fresh PushSubscription's JSON always has these fields populated -- the DOM lib types them
 * optional only because a subscription CAN be serialized before it's fully established, which
 * never happens on the path this app uses it on. Validates explicitly rather than chaining `!`,
 * so a genuinely malformed subscription throws a real, catchable error instead of silently
 * sending `undefined` to the backend. */
function subscriptionPayload(subscription: PushSubscription): {
  endpoint: string;
  keys: { p256dh: string; auth: string };
} {
  const json = subscription.toJSON();
  if (!json.endpoint || !json.keys?.p256dh || !json.keys?.auth) {
    throw new Error("Browser returned an incomplete push subscription.");
  }
  return { endpoint: json.endpoint, keys: { p256dh: json.keys.p256dh, auth: json.keys.auth } };
}

type Status = "unsupported" | "ios-needs-install" | "checking" | "subscribed" | "unsubscribed" | "denied";

/** iOS Safari genuinely has no `PushManager` in a regular browser tab -- Web Push there only
 * exists once the page has been "Added to Home Screen" and is running standalone (iOS 16.4+).
 * Feature detection alone can't tell "not installed yet" apart from "truly unsupported" (both
 * just lack `PushManager`), so this checks the one thing that can: is this iOS Safari, and is it
 * NOT already running standalone. Used only to decide whether to show install guidance instead of
 * silently hiding the control -- never used to gate any other behavior. */
function isIOSSafariNotInstalled(): boolean {
  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
  const isStandalone =
    window.matchMedia("(display-mode: standalone)").matches ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true;
  return isIOS && !isStandalone;
}

/** Small nav control: subscribes/unsubscribes this browser to real Web Push alerts (job
 * failures, deadline reminders, price/injury changes -- see app.notifications.push_sender). No
 * per-user identity in this system (see packages/database/models/push_subscription.py's own
 * docstring) -- this just registers THIS device as one of the manager's own, notified as a group.
 */
export function PushNotificationToggle() {
  const [status, setStatus] = useState<Status>("checking");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function detectStatus(): Promise<Status> {
      if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
        return isIOSSafariNotInstalled() ? "ios-needs-install" : "unsupported";
      }
      if (Notification.permission === "denied") {
        return "denied";
      }
      try {
        const registration = await navigator.serviceWorker.register("/sw.js");
        const existing = await registration.pushManager.getSubscription();
        return existing ? "subscribed" : "unsubscribed";
      } catch {
        return "unsubscribed";
      }
    }

    detectStatus().then((detected) => {
      if (!cancelled) setStatus(detected);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleSubscribe() {
    setBusy(true);
    setError(null);
    try {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        setStatus("denied");
        return;
      }
      const { data: keyData, response: keyResponse } = await browserClient().GET(
        "/push/vapid-public-key",
      );
      if (!keyData || !keyResponse.ok) {
        throw new Error("Could not load the server's push key.");
      }
      const registration = await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToBuffer(keyData.vapid_public_key),
      });
      const { response } = await browserClient().POST("/push/subscribe", {
        body: subscriptionPayload(subscription),
      });
      if (!response.ok) {
        throw new Error("Could not save the subscription.");
      }
      setStatus("subscribed");
    } catch {
      setError("Could not enable notifications. Try again.");
    } finally {
      setBusy(false);
    }
  }

  async function handleUnsubscribe() {
    setBusy(true);
    setError(null);
    try {
      const registration = await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.getSubscription();
      if (subscription) {
        const payload = subscriptionPayload(subscription);
        await subscription.unsubscribe();
        await browserClient().DELETE("/push/subscribe", { body: payload });
      }
      setStatus("unsubscribed");
    } catch {
      setError("Could not disable notifications. Try again.");
    } finally {
      setBusy(false);
    }
  }

  if (status === "unsupported" || status === "checking") {
    return null;
  }

  if (status === "ios-needs-install") {
    return (
      <span style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>
        Add to Home Screen (Safari share icon) to enable notifications
      </span>
    );
  }

  return (
    <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
      {status === "denied" ? (
        <span style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>
          Notifications blocked in browser settings
        </span>
      ) : (
        <button
          onClick={status === "subscribed" ? handleUnsubscribe : handleSubscribe}
          disabled={busy}
          style={{ ...buttonStyle, opacity: busy ? 0.6 : 1 }}
        >
          {status === "subscribed" ? "Disable notifications" : "Enable notifications"}
        </button>
      )}
      {error ? <span style={{ fontSize: "0.8rem", color: "var(--danger, #d33)" }}>{error}</span> : null}
    </div>
  );
}
