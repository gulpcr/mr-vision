import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: "class",
  content: ["./src/**/*.{js,ts,jsx,tsx,mdx}"],
  theme: {
    extend: {
      colors: {
        // Primary ramp = Cortex Radiology brand cyan/teal (accent #0894AB light,
        // #2DE1E6 dark). Remapped from the old blue so every `primary-*` button,
        // link and header across the app reflects the Cortex brand color.
        primary: {
          50: "#ecfeff",
          100: "#cffafe",
          200: "#a5f3fc",
          300: "#67e8f9",
          400: "#22d3ee",
          500: "#06b6d4",
          600: "#0891b2",
          700: "#0e7490",
          800: "#155e75",
          900: "#134e5e",
          950: "#083344",
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
        // Cortex brand accents, driven by CSS vars so they follow the theme.
        accent: "rgb(var(--accent) / <alpha-value>)",     // cyan
        "accent-2": "rgb(var(--accent-2) / <alpha-value>)", // violet
        signal: "rgb(var(--signal) / <alpha-value>)",       // amber
      },
      fontSize: {
        display: ["1.875rem", { lineHeight: "2.25rem", fontWeight: "700" }],
        heading: ["1.125rem", { lineHeight: "1.5rem", fontWeight: "600" }],
        caption: ["0.75rem", { lineHeight: "1rem", fontWeight: "500" }],
      },
      boxShadow: {
        glow: "0 10px 40px -14px rgba(45, 225, 230, 0.4)",
        "glow-lg": "0 24px 70px -20px rgba(45, 225, 230, 0.5)",
      },
      backgroundImage: {
        "accent-gradient": "linear-gradient(120deg, #0894ab, #2de1e6)",
      },
      borderRadius: {
        "2xl": "1rem",
        "3xl": "1.5rem",
      },
    },
  },
  plugins: [],
};

export default config;
