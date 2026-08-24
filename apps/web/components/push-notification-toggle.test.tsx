import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { PushNotificationToggle } from "@/components/push-notification-toggle";

// Real bug this guards: iOS Safari in a regular (not "Added to Home Screen") tab genuinely has no
// window.PushManager -- the component used to silently render nothing in that case, giving a user
// on their phone zero indication of what to do. These lock in the fix: distinguish "install to
// home screen first" (iOS Safari, not standalone) from "genuinely unsupported" (render nothing).

const IOS_USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15";
const ANDROID_USER_AGENT =
  "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36";

function setUserAgent(userAgent: string) {
  Object.defineProperty(window.navigator, "userAgent", { value: userAgent, configurable: true });
}

function setStandalone(isStandalone: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query === "(display-mode: standalone)" && isStandalone,
    media: query,
    addListener: vi.fn(),
    removeListener: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

describe("PushNotificationToggle", () => {
  beforeEach(() => {
    // jsdom has neither of these by default -- explicit delete keeps each test's own intent
    // clear regardless of jsdom version behavior.
    Reflect.deleteProperty(navigator, "serviceWorker");
    Reflect.deleteProperty(window, "PushManager");
    setStandalone(false);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows Add to Home Screen guidance on iOS Safari that is not installed", async () => {
    setUserAgent(IOS_USER_AGENT);

    render(<PushNotificationToggle />);

    expect(await screen.findByText(/Add to Home Screen/i)).toBeInTheDocument();
  });

  it("renders nothing on a non-iOS browser without push support", async () => {
    setUserAgent(ANDROID_USER_AGENT);

    const { container } = render(<PushNotificationToggle />);

    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it("renders nothing on iOS already running standalone but still lacking PushManager", async () => {
    setUserAgent(IOS_USER_AGENT);
    setStandalone(true);

    const { container } = render(<PushNotificationToggle />);

    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});
