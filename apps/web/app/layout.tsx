import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "FPL Decision Engine",
  description: "Decision-support dashboard for a single FPL manager's H2H league.",
  manifest: "/manifest.json",
};

export const viewport: Viewport = {
  themeColor: "#00c878",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
