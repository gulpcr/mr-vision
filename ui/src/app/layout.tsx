import type { Metadata } from "next";
import "./globals.css";
import { AppShell } from "@/components/AppShell";
import { ThemeProvider, THEME_ANTI_FLASH_SCRIPT } from "@/lib/theme";
import { LocaleProvider, LOCALE_ANTI_FLASH_SCRIPT } from "@/lib/i18n";

export const metadata: Metadata = {
  title: "MRI AI Platform",
  description: "AI-powered MRI analysis platform",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_ANTI_FLASH_SCRIPT }} />
        <script dangerouslySetInnerHTML={{ __html: LOCALE_ANTI_FLASH_SCRIPT }} />
      </head>
      <body>
        <ThemeProvider>
          <LocaleProvider>
            <AppShell>{children}</AppShell>
          </LocaleProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
