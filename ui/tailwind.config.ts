import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: "class",
  content: ["./src/**/*.{js,ts,jsx,tsx,mdx}"],
  theme: {
    extend: {
      colors: {
        primary: {
          50: "#eff6ff",
          100: "#dbeafe",
          200: "#bfdbfe",
          300: "#93c5fd",
          400: "#60a5fa",
          500: "#3b82f6",
          600: "#2563eb",
          700: "#1d4ed8",
          800: "#1e40af",
          900: "#1e3a5f",
          950: "#172554",
        },
        // Five-tier clinical severity scale. Every tier is paired with an icon in
        // lib/design/severity.ts — never rely on this color alone to convey severity.
        severity: {
          critical: { 50: "#fef2f2", 500: "#dc2626", 700: "#b91c1c" },
          high: { 50: "#fff7ed", 500: "#ea580c", 700: "#c2410c" },
          moderate: { 50: "#fffbeb", 500: "#d97706", 700: "#b45309" },
          informational: { 50: "#eff6ff", 500: "#2563eb", 700: "#1d4ed8" },
          good: { 50: "#f0fdf4", 500: "#16a34a", 700: "#15803d" },
        },
        // Semantic surface tokens for dark-mode swapping (values in globals.css).
        surface: "rgb(var(--surface) / <alpha-value>)",
        "surface-raised": "rgb(var(--surface-raised) / <alpha-value>)",
        border: "rgb(var(--border) / <alpha-value>)",
      },
      fontSize: {
        display: ["1.875rem", { lineHeight: "2.25rem", fontWeight: "700" }],
        heading: ["1.125rem", { lineHeight: "1.5rem", fontWeight: "600" }],
        caption: ["0.75rem", { lineHeight: "1rem", fontWeight: "500" }],
      },
    },
  },
  plugins: [],
};

export default config;
