import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The dashboard renders whatever the backend returns, request-by-request --
  // nothing here benefits from static generation, so this stays a plain
  // server-rendered app rather than opting into ISR/static export.
  output: "standalone",
};

export default nextConfig;
