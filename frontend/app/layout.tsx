import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "CAD PDF Extractor",
  description:
    "Local tool for extracting text, page info, previews and vector drawing statistics from PDF engineering drawings and CAD exports.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
