"use client";

import { createContext, useContext, useEffect, useState } from "react";
import { en, type Strings } from "./locales/en";
import { ur } from "./locales/ur";
import { setFormatLocale } from "./format";

export type Locale = "en" | "ur";
export type Direction = "ltr" | "rtl";

const CATALOGS: Record<Locale, Strings> = { en, ur };
const INTL_LOCALE: Record<Locale, string> = { en: "en-US", ur: "ur-PK" };
const STORAGE_KEY = "locale";

interface LocaleContextValue {
  locale: Locale;
  dir: Direction;
  strings: Strings;
  setLocale: (locale: Locale) => void;
}

const LocaleContext = createContext<LocaleContextValue>({
  locale: "en",
  dir: "ltr",
  strings: en,
  setLocale: () => {},
});

function dirFor(locale: Locale): Direction {
  return locale === "ur" ? "rtl" : "ltr";
}

export function LocaleProvider({ children }: { children: React.ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>("en");

  useEffect(() => {
    const stored = localStorage.getItem(STORAGE_KEY) as Locale | null;
    if (stored === "en" || stored === "ur") setLocaleState(stored);
  }, []);

  useEffect(() => {
    document.documentElement.dir = dirFor(locale);
    document.documentElement.lang = locale;
    setFormatLocale(INTL_LOCALE[locale]);
  }, [locale]);

  const setLocale = (next: Locale) => {
    localStorage.setItem(STORAGE_KEY, next);
    setLocaleState(next);
  };

  const value: LocaleContextValue = {
    locale,
    dir: dirFor(locale),
    strings: CATALOGS[locale],
    setLocale,
  };

  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>;
}

export function useLocale(): LocaleContextValue {
  return useContext(LocaleContext);
}

// Inline, pre-hydration script (mounted in app/layout.tsx <head>) that sets
// dir/lang on <html> before React hydrates — mirrors THEME_ANTI_FLASH_SCRIPT's
// rationale (avoid a flash of the wrong direction/locale).
export const LOCALE_ANTI_FLASH_SCRIPT = `(function(){try{var l=localStorage.getItem('${STORAGE_KEY}');if(l==='ur'){document.documentElement.dir='rtl';document.documentElement.lang='ur';}}catch(e){}})();`;
