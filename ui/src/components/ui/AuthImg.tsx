"use client";

import { useAuthenticatedImage } from "@/lib/useAuthenticatedImage";

interface AuthImgProps {
  src: string;
  alt: string;
  className?: string;
  fallback?: React.ReactNode;
  loadingClassName?: string;
  errorClassName?: string;
}

// Renders a JWT-protected image (artifact/preview/fused-render) via the shared
// authenticated-image cache — replaces the 3 independent blob-fetch
// implementations previously duplicated across ReportView/ComparePanel/FusedViewer.
export function AuthImg({
  src,
  alt,
  className,
  fallback = "Image not available",
  loadingClassName = "h-48 bg-gray-900 rounded-lg animate-pulse motion-reduce:animate-none",
  errorClassName = "flex items-center justify-center h-48 text-gray-500 text-xs bg-black rounded-lg",
}: AuthImgProps) {
  const { objectUrl, loading, error } = useAuthenticatedImage(src);

  if (error) return <div className={errorClassName}>{fallback}</div>;
  if (loading || !objectUrl) return <div className={loadingClassName} />;
  /* eslint-disable-next-line @next/next/no-img-element */
  return <img src={objectUrl} alt={alt} className={className} />;
}
