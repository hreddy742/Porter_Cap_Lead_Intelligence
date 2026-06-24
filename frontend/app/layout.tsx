import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Sidebar from "@/components/layout/Sidebar";
import { AlertTriangle } from "lucide-react";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Porter Capital — Lead Intelligence",
  description: "Internal lead pipeline — research-ready view only",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="h-full flex bg-slate-50 text-slate-900">
        <Sidebar />

        <div className="flex flex-col flex-1 min-w-0 overflow-hidden">
          {/* Research-only warning strip */}
          <div className="shrink-0 bg-amber-50 border-b border-amber-200/80 px-5 py-[7px] flex items-center gap-2.5">
            <AlertTriangle className="w-3.5 h-3.5 text-amber-600 shrink-0" />
            <p className="text-[11px] text-amber-800 leading-snug">
              <span className="font-semibold">Research-ready only.</span> No
              verified contacts. No Salesforce push. Human review required
              before any outreach.
            </p>
          </div>

          {/* Page content */}
          <main className="flex-1 overflow-y-auto flex flex-col">
            {children}
          </main>
        </div>
      </body>
    </html>
  );
}
