import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Cloud-Native GitOps Platform — Operational Feed",
  description: "3-tier microservices operational dashboard for Kubernetes GitOps, Redis caching, and Prometheus observability.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="bg-slate-950 text-slate-100 antialiased selection:bg-cyan-500 selection:text-white">
        {children}
      </body>
    </html>
  );
}
