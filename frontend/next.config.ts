import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // By default the API route runs the Python extractor in-process, so only
  // `npm run dev` on port 3000 is needed. Set PYTHON_API_URL to proxy to a
  // separately running uvicorn server instead.
};

export default nextConfig;
