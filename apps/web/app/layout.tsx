import type { Metadata, Viewport } from "next";
import { Instrument_Sans, Newsreader, IBM_Plex_Mono } from "next/font/google";
import "./globals.css";

/*
 * Three faces, three jobs. The sans is the instrument, the serif is the
 * document under examination, and the mono is measurement -- confidence,
 * offsets, page numbers. Keeping them to those roles is what stops the
 * interface and the source text from blurring into each other.
 */

const sans = Instrument_Sans({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-instrument",
});

const serif = Newsreader({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-newsreader",
});

const mono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  display: "swap",
  variable: "--font-plex-mono",
});

export const metadata: Metadata = {
  title: "Document → Action Engine",
  description:
    "Turns notices into evidence-backed, dependency-ordered action plans, and says plainly what it could not determine.",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#fbfbfc" },
    { media: "(prefers-color-scheme: dark)", color: "#0d0e12" },
  ],
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className={`${sans.variable} ${serif.variable} ${mono.variable}`}>
        <a className="skip-link" href="#main">
          Skip to content
        </a>
        {children}
      </body>
    </html>
  );
}
