import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The dashboard renders whatever the backend returns, request-by-request --
  // nothing here benefits from static generation, so this stays a plain
  // server-rendered app rather than opting into ISR/static export.
  //
  // "standalone" output is needed for the local Docker build (apps/web/Dockerfile copies
  // .next/standalone directly) but conflicts with Vercel's own deployment packaging -- confirmed
  // via a real deploy (2026-08-21): the build itself succeeded (compiled, typechecked, generated
  // pages), but the deploy then failed at Vercel's "Assigning Custom Domains" step, a known
  // standalone-output-on-Vercel gotcha. Vercel sets its own VERCEL=1 env var at build time, so
  // this only applies "standalone" for the Docker path, never on Vercel.
  ...(process.env.VERCEL ? {} : { output: "standalone" }),
};

export default nextConfig;
