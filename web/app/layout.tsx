import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "ClipForge",
  description: "Podcast to hand-directed vertical shorts",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <header className="border-b border-ink-700 bg-ink-900">
          <nav className="mx-auto flex max-w-6xl items-center gap-6 px-4 py-3">
            <Link href="/" className="text-lg font-bold tracking-tight">
              Clip<span className="text-accent">Forge</span>
            </Link>
            <Link href="/" className="text-sm text-gray-300 hover:text-white">
              Projects
            </Link>
            <Link href="/settings" className="text-sm text-gray-300 hover:text-white">
              Settings
            </Link>
            <Link href="/system" className="text-sm text-gray-300 hover:text-white">
              System
            </Link>
          </nav>
        </header>
        <main className="mx-auto max-w-6xl px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
