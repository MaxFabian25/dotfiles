#!/usr/bin/env bun
/**
 * Generate packages/visual/plugin/kit/tokens.css from effect-ui-extra's
 * WCAG-audited OKLCH visual presets (nova, vega, maia) and analytical spring
 * easing math.
 *
 * Usage:
 *   bun packages/visual/scripts/sync_tokens.ts --effect-ui-extra /path/to/effect-ui-extra
 */
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";

const args = process.argv.slice(2);
const idx = args.indexOf("--effect-ui-extra");
const extraDir =
  idx >= 0 && args[idx + 1]
    ? resolve(args[idx + 1])
    : resolve(process.env.EFFECT_UI_EXTRA_DIR ?? "../effect-ui-extra");

const outIdx = args.indexOf("--out");
const outPath =
  outIdx >= 0 && args[outIdx + 1]
    ? resolve(args[outIdx + 1])
    : resolve(import.meta.dir, "../plugin/kit/tokens.css");

const { VISUAL_PRESETS } = await import(resolve(extraDir, "src/registry/presets.ts"));
const { springLinearEasing } = await import(resolve(extraDir, "src/motion/spring.ts"));
const { auditThemeContrast } = await import(resolve(extraDir, "src/theme/contrast.ts"));
const { themeContrastPairs } = await import(resolve(extraDir, "src/theme/tokens.ts"));

const STYLES = ["nova", "vega", "maia"] as const;

for (const name of STYLES) {
  const preset = VISUAL_PRESETS[name];
  for (const mode of ["light", "dark"] as const) {
    const failures = auditThemeContrast(themeContrastPairs(preset[mode]));
    if (failures.length > 0) {
      console.error(`Contrast audit failed for ${name}/${mode}:`, failures);
      process.exit(1);
    }
  }
}

const snappy = springLinearEasing({ stiffness: 420, damping: 41 }, 20);
const smooth = springLinearEasing({ stiffness: 300, damping: 35 }, 24);

interface StyleExtra {
  headingFont: string;
  headingWeight: number;
  headingTrack: string;
  rOuter: string;
  rInner: string;
  rPill: string;
  darkSurface: string;
  darkRaised: string;
  darkCodeBg: string;
  lightSurface: string;
  lightRaised: string;
  lightCodeBg: string;
  accentTintLight: string;
  accentTintDark: string;
  accentTextLight: string;
  accentTextDark: string;
  successLight: string;
  successDark: string;
  successTintLight: string;
  successTintDark: string;
  warnLight: string;
  warnDark: string;
  warnTintLight: string;
  warnTintDark: string;
  dangerTintLight: string;
  dangerTintDark: string;
  movedLight: string;
  movedDark: string;
  movedTintLight: string;
  movedTintDark: string;
  shadow: string;
}

const STYLE_EXTRAS: Record<(typeof STYLES)[number], StyleExtra> = {
  nova: {
    headingFont: "var(--vk-font-sans)",
    headingWeight: 600,
    headingTrack: "-0.02em",
    rOuter: "12px",
    rInner: "8px",
    rPill: "6px",
    lightSurface: "oklch(0.975 0.004 260)",
    lightRaised: "oklch(0.945 0.006 260)",
    lightCodeBg: "oklch(0.96 0.005 260)",
    darkSurface: "oklch(0.175 0.011 260)",
    darkRaised: "oklch(0.225 0.013 260)",
    darkCodeBg: "oklch(0.145 0.009 260)",
    accentTintLight: "oklch(0.93 0.032 260)",
    accentTintDark: "oklch(0.26 0.045 258)",
    accentTextLight: "oklch(0.40 0.15 260)",
    accentTextDark: "oklch(0.82 0.11 258)",
    successLight: "oklch(0.45 0.14 155)",
    successDark: "oklch(0.74 0.14 158)",
    successTintLight: "oklch(0.94 0.035 155)",
    successTintDark: "oklch(0.23 0.045 158)",
    warnLight: "oklch(0.50 0.14 68)",
    warnDark: "oklch(0.80 0.14 75)",
    warnTintLight: "oklch(0.95 0.04 75)",
    warnTintDark: "oklch(0.25 0.045 70)",
    dangerTintLight: "oklch(0.94 0.035 25)",
    dangerTintDark: "oklch(0.24 0.05 24)",
    movedLight: "oklch(0.48 0.15 300)",
    movedDark: "oklch(0.76 0.14 300)",
    movedTintLight: "oklch(0.94 0.03 300)",
    movedTintDark: "oklch(0.24 0.045 300)",
    shadow: "light-dark(0 1px 2px oklch(0 0 0 / 0.06), inset 0 1px 0 oklch(1 0 0 / 0.06))",
  },
  vega: {
    headingFont: "var(--vk-font-sans)",
    headingWeight: 500,
    headingTrack: "-0.025em",
    rOuter: "10px",
    rInner: "6px",
    rPill: "4px",
    lightSurface: "oklch(0.975 0.002 250)",
    lightRaised: "oklch(0.94 0.004 250)",
    lightCodeBg: "oklch(0.96 0.002 250)",
    darkSurface: "oklch(0.165 0.004 250)",
    darkRaised: "oklch(0.215 0.006 250)",
    darkCodeBg: "oklch(0.14 0.004 250)",
    accentTintLight: "oklch(0.92 0.006 250)",
    accentTintDark: "oklch(0.26 0.008 250)",
    accentTextLight: "oklch(0.20 0.008 250)",
    accentTextDark: "oklch(0.95 0.004 250)",
    successLight: "oklch(0.45 0.13 155)",
    successDark: "oklch(0.74 0.12 158)",
    successTintLight: "oklch(0.94 0.025 155)",
    successTintDark: "oklch(0.22 0.035 158)",
    warnLight: "oklch(0.50 0.13 70)",
    warnDark: "oklch(0.80 0.12 76)",
    warnTintLight: "oklch(0.95 0.03 75)",
    warnTintDark: "oklch(0.24 0.035 72)",
    dangerTintLight: "oklch(0.94 0.03 27)",
    dangerTintDark: "oklch(0.23 0.04 25)",
    movedLight: "oklch(0.48 0.12 260)",
    movedDark: "oklch(0.76 0.11 260)",
    movedTintLight: "oklch(0.94 0.02 260)",
    movedTintDark: "oklch(0.23 0.03 260)",
    shadow: "none",
  },
  maia: {
    headingFont: "var(--vk-font-serif)",
    headingWeight: 500,
    headingTrack: "-0.015em",
    rOuter: "14px",
    rInner: "9px",
    rPill: "999px",
    lightSurface: "oklch(0.968 0.010 75)",
    lightRaised: "oklch(0.935 0.013 75)",
    lightCodeBg: "oklch(0.955 0.010 75)",
    darkSurface: "oklch(0.185 0.011 55)",
    darkRaised: "oklch(0.235 0.013 55)",
    darkCodeBg: "oklch(0.155 0.010 55)",
    accentTintLight: "oklch(0.92 0.032 65)",
    accentTintDark: "oklch(0.27 0.038 50)",
    accentTextLight: "oklch(0.42 0.13 44)",
    accentTextDark: "oklch(0.82 0.11 48)",
    successLight: "oklch(0.45 0.12 152)",
    successDark: "oklch(0.74 0.12 154)",
    successTintLight: "oklch(0.94 0.03 152)",
    successTintDark: "oklch(0.24 0.035 154)",
    warnLight: "oklch(0.50 0.13 65)",
    warnDark: "oklch(0.80 0.13 72)",
    warnTintLight: "oklch(0.94 0.035 70)",
    warnTintDark: "oklch(0.26 0.04 68)",
    dangerTintLight: "oklch(0.94 0.035 22)",
    dangerTintDark: "oklch(0.24 0.045 22)",
    movedLight: "oklch(0.48 0.12 245)",
    movedDark: "oklch(0.76 0.12 245)",
    movedTintLight: "oklch(0.94 0.025 245)",
    movedTintDark: "oklch(0.24 0.035 245)",
    shadow: "light-dark(0 1px 3px oklch(0.25 0.02 60 / 0.08), inset 0 1px 0 oklch(0.95 0.02 70 / 0.06))",
  },
};

function renderStyleBlock(name: (typeof STYLES)[number]): string {
  const p = VISUAL_PRESETS[name];
  const e = STYLE_EXTRAS[name];
  const sel = name === "nova" ? ':root, [data-style="nova"]' : `[data-style="${name}"]`;
  const chartDecls = p.chartLight
    .map((c: string, i: number) => `  --vk-chart-${i + 1}: light-dark(${c}, ${p.chartDark[i]});`)
    .join("\n");

  return `${sel} {
  --vk-heading-font: ${e.headingFont};
  --vk-heading-weight: ${e.headingWeight};
  --vk-heading-track: ${e.headingTrack};
  --vk-r-outer: ${e.rOuter};
  --vk-r-inner: ${e.rInner};
  --vk-r-pill: ${e.rPill};

  --vk-bg-standalone: light-dark(${p.light.background}, ${p.dark.background});
  --vk-fg: light-dark(${p.light.foreground}, ${p.dark.foreground});
  --vk-fg-muted: light-dark(${p.light.mutedForeground}, ${p.dark.mutedForeground});
  --vk-surface: light-dark(${e.lightSurface}, ${e.darkSurface});
  --vk-raised: light-dark(${e.lightRaised}, ${e.darkRaised});
  --vk-code-bg: light-dark(${e.lightCodeBg}, ${e.darkCodeBg});
  --vk-hairline: light-dark(${p.light.border}, ${p.dark.border});
  --vk-border-strong: light-dark(${p.light.inputBorder}, ${p.dark.inputBorder});

  --vk-accent: light-dark(${p.light.primary}, ${p.dark.primary});
  --vk-accent-fg: light-dark(${p.light.primaryForeground}, ${p.dark.primaryForeground});
  --vk-accent-tint: light-dark(${e.accentTintLight}, ${e.accentTintDark});
  --vk-accent-text: light-dark(${e.accentTextLight}, ${e.accentTextDark});
  --vk-ring: light-dark(${p.light.ring}, ${p.dark.ring});

  --vk-success: light-dark(${e.successLight}, ${e.successDark});
  --vk-success-tint: light-dark(${e.successTintLight}, ${e.successTintDark});
  --vk-warn: light-dark(${e.warnLight}, ${e.warnDark});
  --vk-warn-tint: light-dark(${e.warnTintLight}, ${e.warnTintDark});
  --vk-danger: light-dark(${p.light.destructive}, ${p.dark.destructive});
  --vk-danger-tint: light-dark(${e.dangerTintLight}, ${e.dangerTintDark});
  --vk-moved: light-dark(${e.movedLight}, ${e.movedDark});
  --vk-moved-tint: light-dark(${e.movedTintLight}, ${e.movedTintDark});

${chartDecls}
  --vk-shadow: ${e.shadow};
}`;
}

const css = `/* Generated by packages/visual/scripts/sync_tokens.ts from effect-ui-extra.
 * Do not edit values by hand without re-auditing WCAG 2.1 AA contrast pairs. */
:root {
  color-scheme: light dark;
  interpolate-size: allow-keywords;

  --vk-font-sans: 'Geist', var(--font-sans, system-ui, -apple-system, BlinkMacSystemFont, sans-serif);
  --vk-font-mono: 'Geist Mono', var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  --vk-font-serif: 'Geist Serif', Georgia, serif;

  /* Analytical springs from effect-ui-extra/src/motion/spring.ts */
  --vk-spring: ${snappy.easing};
  --vk-spring-smooth: ${smooth.easing};
  --vk-ease-out: cubic-bezier(0.22, 1, 0.36, 1);
  --vk-dur-fast: 160ms;
  --vk-dur-spring: 340ms;
  --vk-dur-smooth: 420ms;
}

${STYLES.map(renderStyleBlock).join("\n\n")}
`;

writeFileSync(outPath, css, "utf8");
console.log(`Wrote ${outPath} (${css.length} bytes, styles: ${STYLES.join(", ")})`);
