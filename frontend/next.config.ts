import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /**
   * `standalone` traces the exact files this app needs and copies them, plus a
   * minimal server, into `.next/standalone`. It is what makes the production
   * image ship without `node_modules` — roughly 1.2GB of dependencies, most of
   * which exist only to build.
   *
   * Without it the Dockerfile has two honest options and both are bad: ship the
   * whole dependency tree, or run `next start` from a full install and hope
   * nothing dev-only is reachable at runtime.
   */
  output: "standalone",
};

export default nextConfig;
