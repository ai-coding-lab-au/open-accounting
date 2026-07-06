// Theme registry. Each theme defines a set of CSS custom properties that
// map onto Tailwind's slate/emerald/amber/white scales. Applied by setting
// `data-theme` on <html>; the matching block in `index.css` writes the
// vars. Tailwind config rebinds `slate-*`, `emerald-*`, etc. to var()
// references, so existing hardcoded class names switch palette without
// touching component code.

export type ThemeId =
  | "default"
  | "midnight"
  | "graphite"
  | "carbon"
  | "nord"
  | "solarized-light"
  | "solarized-dark"
  | "dracula"
  | "monokai"
  | "github-light"
  | "github-dark"
  | "linear"
  | "stripe"
  | "notion"
  | "sepia"
  | "paper"
  | "newspaper"
  | "blueprint"
  | "terminal"
  | "matrix"
  | "win95"
  | "macclassic"
  | "sunset"
  | "ocean"
  | "forest"
  | "lavender"
  | "rose"
  | "mono-light"
  | "mono-dark"
  | "highcontrast";

export interface ThemeMeta {
  id: ThemeId;
  label: string;
  family: "Light" | "Dark" | "Retro" | "Tinted" | "High-contrast";
  description: string;
}

export const THEMES: ThemeMeta[] = [
  // Light, mainstream
  { id: "default",          label: "Default Slate",      family: "Light",         description: "The original slate + emerald. Familiar SaaS." },
  { id: "github-light",     label: "GitHub Light",       family: "Light",         description: "Borrowed from github.com — neutral grays + blue accent." },
  { id: "linear",           label: "Linear",             family: "Light",         description: "Linear.app aesthetic — soft purples + tight type." },
  { id: "stripe",           label: "Stripe",             family: "Light",         description: "Stripe-style indigo + crisp whites." },
  { id: "notion",           label: "Notion",             family: "Light",         description: "Warm off-white + nearly-black text." },
  { id: "paper",            label: "Paper",              family: "Light",         description: "Soft cream paper background, ink-blue accents." },
  { id: "sepia",            label: "Sepia",              family: "Light",         description: "Reading-app sepia tones." },
  { id: "newspaper",        label: "Newspaper",          family: "Light",         description: "High-contrast black/white with serif headings." },

  // Dark
  { id: "midnight",         label: "Midnight",           family: "Dark",          description: "Deep navy + cyan." },
  { id: "graphite",         label: "Graphite",           family: "Dark",          description: "Neutral dark gray, no blue cast." },
  { id: "carbon",           label: "Carbon (IBM)",       family: "Dark",          description: "IBM Carbon-inspired — very dark + blue accent." },
  { id: "nord",             label: "Nord",               family: "Dark",          description: "Arctic blue palette (popular dev theme)." },
  { id: "solarized-dark",   label: "Solarized Dark",     family: "Dark",          description: "Ethan Schoonover's solarized dark." },
  { id: "dracula",          label: "Dracula",            family: "Dark",          description: "Pink + purple on near-black." },
  { id: "monokai",          label: "Monokai",            family: "Dark",          description: "Classic Sublime / TextMate yellows + greens." },
  { id: "github-dark",      label: "GitHub Dark",        family: "Dark",          description: "GitHub's official dark mode." },
  { id: "solarized-light",  label: "Solarized Light",    family: "Light",         description: "Solarized for daytime." },

  // Retro / playful
  { id: "blueprint",        label: "Blueprint",          family: "Retro",         description: "Engineering blueprint — cyan grid on navy." },
  { id: "terminal",         label: "Amber Terminal",     family: "Retro",         description: "DEC VT220 amber-on-black colour treatment." },
  { id: "matrix",           label: "Matrix",             family: "Retro",         description: "Phosphor green on black." },
  { id: "win95",            label: "Windows 95",         family: "Retro",         description: "Teal desktop and button bevels." },
  { id: "macclassic",       label: "Mac Classic",        family: "Retro",         description: "Black-and-white System 7 styling." },

  // Tinted
  { id: "sunset",           label: "Sunset",             family: "Tinted",        description: "Warm orange-pink gradient cues." },
  { id: "ocean",            label: "Ocean",              family: "Tinted",        description: "Teal + sand tones." },
  { id: "forest",           label: "Forest",             family: "Tinted",        description: "Deep green + olive accents." },
  { id: "lavender",         label: "Lavender",           family: "Tinted",        description: "Soft purples + pinks." },
  { id: "rose",             label: "Rose",               family: "Tinted",        description: "Dusty pink + mauve." },

  // Mono / accessibility
  { id: "mono-light",       label: "Mono (light)",       family: "Light",         description: "Pure grayscale, no accent color." },
  { id: "mono-dark",        label: "Mono (dark)",        family: "Dark",          description: "Pure grayscale, dark." },
  { id: "highcontrast",     label: "High Contrast",      family: "High-contrast", description: "Pure black/white/yellow — accessibility." },
];

export const THEME_IDS: ThemeId[] = THEMES.map((t) => t.id);

export const DEFAULT_THEME: ThemeId = "paper";

export function isThemeId(s: string): s is ThemeId {
  return (THEME_IDS as string[]).includes(s);
}
