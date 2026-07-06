import type { Config } from "tailwindcss";

// Slate / emerald / amber color scales bound to CSS variables so themes
// can repaint by setting `data-theme` on <html>. Default values defined
// in src/index.css under `:root` and per-theme blocks.

const cssVar = (name: string) => `var(--${name})`;

const slateScale: Record<string, string> = {
  50:  cssVar("slate-50"),
  100: cssVar("slate-100"),
  200: cssVar("slate-200"),
  300: cssVar("slate-300"),
  400: cssVar("slate-400"),
  500: cssVar("slate-500"),
  600: cssVar("slate-600"),
  700: cssVar("slate-700"),
  800: cssVar("slate-800"),
  900: cssVar("slate-900"),
  950: cssVar("slate-950"),
};

const emeraldScale: Record<string, string> = {
  50:  cssVar("emerald-50"),
  100: cssVar("emerald-100"),
  200: cssVar("emerald-200"),
  300: cssVar("emerald-300"),
  400: cssVar("emerald-400"),
  500: cssVar("emerald-500"),
  600: cssVar("emerald-600"),
  700: cssVar("emerald-700"),
  800: cssVar("emerald-800"),
  900: cssVar("emerald-900"),
};

const amberScale: Record<string, string> = {
  50:  cssVar("amber-50"),
  100: cssVar("amber-100"),
  200: cssVar("amber-200"),
  300: cssVar("amber-300"),
  400: cssVar("amber-400"),
  500: cssVar("amber-500"),
  600: cssVar("amber-600"),
  700: cssVar("amber-700"),
  800: cssVar("amber-800"),
  900: cssVar("amber-900"),
};

const grayScale: Record<string, string> = {
  50:  cssVar("gray-50"),
  100: cssVar("gray-100"),
  200: cssVar("gray-200"),
  300: cssVar("gray-300"),
  400: cssVar("gray-400"),
  500: cssVar("gray-500"),
  600: cssVar("gray-600"),
  700: cssVar("gray-700"),
  800: cssVar("gray-800"),
  900: cssVar("gray-900"),
};

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        slate: slateScale,
        emerald: emeraldScale,
        amber: amberScale,
        gray: grayScale,
        surface: cssVar("color-surface"),
      },
      fontFamily: {
        sans: [cssVar("font-sans")],
        mono: [cssVar("font-mono")],
      },
    },
  },
  plugins: [],
} satisfies Config;
