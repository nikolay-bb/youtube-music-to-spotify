import type { Metadata } from "next";
import { Inter } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});

export const metadata: Metadata = {
  title: "YouTube Music to Spotify",
  description: "Move your playlists and liked songs across to Spotify.",
};

/** The app's mark, from app/icon.svg: a note crossing from red to green. */
function Logo() {
  return (
    <svg viewBox="0 0 32 32" className="h-7 w-7 shrink-0" aria-hidden>
      <rect width="32" height="32" rx="8" fill="#1b1f28" />
      <circle cx="11" cy="21" r="3.6" fill="#ff3347" />
      <rect x="13.6" y="8" width="1.9" height="13" fill="#ff3347" />
      <path d="M15.5 8 L23 10.2 L23 13.4 L15.5 11.2 Z" fill="#1ed760" />
      <circle cx="23" cy="17.5" r="3.2" fill="#1ed760" />
      <rect x="21.4" y="10.2" width="1.7" height="7.6" fill="#1ed760" />
    </svg>
  );
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en-GB" className={inter.variable}>
      <body className="flex min-h-screen flex-col">
        <header className="sticky top-0 z-10 border-b border-line/70 bg-canvas/80 backdrop-blur-md">
          <div className="mx-auto flex max-w-5xl items-center gap-3 px-6 py-3.5">
            <Link href="/" className="flex items-center gap-2.5">
              <Logo />
              <span className="text-base font-semibold tracking-tight">
                YouTube Music <span className="font-normal text-muted">to</span>{" "}
                <span className="bg-gradient-to-r from-spotify to-emerald-300 bg-clip-text text-transparent">
                  Spotify
                </span>
              </span>
            </Link>
            <span className="ml-auto hidden text-xs text-muted sm:block">
              Runs on your machine. Nothing is uploaded anywhere.
            </span>
          </div>
        </header>
        <main className="mx-auto w-full max-w-5xl flex-1 px-6 py-10">
          {children}
        </main>
        <footer className="border-t border-line/60">
          <div className="mx-auto max-w-5xl px-6 py-4 text-xs text-muted">
            A local tool. Your keys and tokens stay on this computer.
          </div>
        </footer>
      </body>
    </html>
  );
}
