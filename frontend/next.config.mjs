/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    // Rewrites run on the Next.js SERVER side.
    //  - When Next.js runs inside Docker: use Docker service name (http://backend:8000)
    //  - When running locally on host: use localhost:8000
    //
    // NEXT_PUBLIC_API_URL is browser-facing (exposed to client bundle).
    // BACKEND_INTERNAL_URL is server-side only (Docker network).
    const backendUrl =
      process.env.BACKEND_INTERNAL_URL ||
      process.env.NEXT_PUBLIC_API_URL ||
      "http://localhost:8000";

    return [
      {
        source: "/api/:path*",
        destination: `${backendUrl}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
