import { Card } from "@/components/ui";

/**
 * Shown while any page under (app) is doing its own server-side data fetch (every page here is
 * `dynamic = "force-dynamic"`, hitting the real backend on every request -- see e.g. app/(app)
 * /page.tsx's own DeadlineCard). Real gap this closes: Render's free tier cold-starts after 15
 * minutes idle and can take 30-60s to wake on the next request -- with nothing here, that wait
 * rendered as a blank page, which is especially confusing in the installed PWA's standalone view
 * (no browser chrome/spinner to hint anything is happening at all, unlike a regular browser tab).
 * AppLayout's own <Nav /> has no async work of its own, so it still renders immediately above
 * this -- only the page content area waits behind this Suspense fallback.
 */
export default function Loading() {
  return (
    <Card>
      <p style={{ color: "var(--text-muted)", margin: 0 }}>Loading...</p>
    </Card>
  );
}
