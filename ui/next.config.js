/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.BACKEND_URL || "http://backend:8000"}/api/:path*`,
      },
      {
        source: "/health",
        destination: `${process.env.BACKEND_URL || "http://backend:8000"}/health`,
      },
      // No /orthanc passthrough: it exposed Orthanc's full REST API (list/download/
      // delete every study) with no authentication. The UI reaches Orthanc only through
      // the backend (/api/orthanc/*), and the viewer through nginx's gated /dicom-web.
    ];
  },
};

module.exports = nextConfig;
