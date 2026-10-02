import SwiftUI
import UIKit

/// The five color tokens replies style themselves with — `--hermes-text`, `--hermes-muted`, `--hermes-surface`,
/// `--hermes-border` and `--hermes-accent` — resolved from the app's own colors for light, dark and increased contrast,
/// so a reply matches the transcript around it.
struct ResponseWebPalette {
    struct Tokens {
        let text: String
        let muted: String
        let surface: String
        let border: String
        let accent: String

        var declarations: String {
            "--hermes-text: \(text); --hermes-muted: \(muted); --hermes-surface: \(surface); --hermes-border: \(border); --hermes-accent: \(accent);"
        }
    }

    let light: Tokens
    let dark: Tokens
    let lightIncreasedContrast: Tokens
    let darkIncreasedContrast: Tokens

    @MainActor
    static func resolve() -> ResponseWebPalette {
        let accent = UIColor(named: "AccentColor") ?? .systemBlue
        func tokens(_ scheme: ColorScheme, contrast: UIAccessibilityContrast) -> Tokens {
            let traits = UITraitCollection { traits in
                traits.userInterfaceStyle = scheme == .dark ? .dark : .light
                traits.accessibilityContrast = contrast
            }
            return Tokens(
                text: css(.label, traits),
                muted: css(.secondaryLabel, traits),
                surface: css(UIColor(HermesPalette.control(scheme)), traits),
                border: css(.separator, traits),
                accent: css(accent, traits)
            )
        }
        return ResponseWebPalette(
            light: tokens(.light, contrast: .normal),
            dark: tokens(.dark, contrast: .normal),
            lightIncreasedContrast: tokens(.light, contrast: .high),
            darkIncreasedContrast: tokens(.dark, contrast: .high)
        )
    }

    private static func css(_ color: UIColor, _ traits: UITraitCollection) -> String {
        var red: CGFloat = 0, green: CGFloat = 0, blue: CGFloat = 0, alpha: CGFloat = 0
        guard color.resolvedColor(with: traits).getRed(&red, green: &green, blue: &blue, alpha: &alpha) else {
            return "CanvasText"
        }
        func channel(_ value: CGFloat) -> Int { Int((min(max(value, 0), 1) * 255).rounded()) }
        let opacity = (min(max(alpha, 0), 1) * 1_000).rounded() / 1_000
        return "rgba(\(channel(red)), \(channel(green)), \(channel(blue)), \(opacity))"
    }
}

/// Default styling for reply documents: system text that follows Dynamic Type, the palette tokens, and readable,
/// touch-sized defaults for the elements replies use. Element defaults sit in `:where()`, which has no specificity, so
/// any style a reply declares wins over them.
enum ResponseWebStyles {
    static func stylesheet(_ palette: ResponseWebPalette) -> String {
        #"""
        :root { color-scheme: light dark; \#(palette.light.declarations) }
        @media (prefers-color-scheme: dark) { :root { \#(palette.dark.declarations) } }
        @media (prefers-contrast: more) { :root { \#(palette.lightIncreasedContrast.declarations) } }
        @media (prefers-contrast: more) and (prefers-color-scheme: dark) { :root { \#(palette.darkIncreasedContrast.declarations) } }

        html {
          font: -apple-system-body;
          -webkit-text-size-adjust: 100%;
          text-size-adjust: 100%;
          color: var(--hermes-text);
          background: transparent;
          overflow-wrap: break-word;
          touch-action: manipulation;
          -webkit-tap-highlight-color: transparent;
        }
        body { margin: 0; padding: 0; background: transparent; line-height: 1.5; }
        #hermes-root { display: flow-root; position: relative; max-width: 100%; overflow-x: auto; overflow-y: hidden; }
        #hermes-root > :nth-child(1 of :not(style, script, template)) { margin-top: 0; }
        #hermes-root > :nth-last-child(1 of :not(style, script, template)) { margin-bottom: 0; }
        [hidden]:not([hidden="until-found"]) { display: none !important; }
        ::selection { background: color-mix(in srgb, var(--hermes-accent) 30%, transparent); }

        :where(p, ul, ol, dl, pre, blockquote, figure, details, table, [data-hermes-scroll]) { margin: 0 0 0.8em; }
        :where(li, td, th, dd, blockquote, details, figure, section, article, aside, div) > :where(:nth-last-child(1 of :not(style, script, template))) { margin-bottom: 0; }
        :where(p, li, dt, dd, td, th, h1, h2, h3, h4, h5, h6, blockquote, figcaption, caption, summary, label) { unicode-bidi: plaintext; text-align: start; }

        :where(h1, h2, h3, h4, h5, h6) { margin: 0 0 0.4em; line-height: 1.25; font-weight: 700; text-wrap: balance; }
        :where(:not(style, script, template) + :is(h1, h2, h3, h4, h5, h6)) { margin-top: 1.2em; }
        :where(h1) { font-size: 1.5em; letter-spacing: -0.01em; }
        :where(h2) { font-size: 1.3em; }
        :where(h3) { font-size: 1.12em; }
        :where(h4) { font-size: 1em; }
        :where(h5) { font-size: 0.94em; }
        :where(h6) { font-size: 0.88em; color: var(--hermes-muted); }
        :where(small) { font-size: 0.86em; }
        :where(sub, sup) { line-height: 0; }
        :where(abbr[title]) { text-decoration: underline dotted; }
        :where(mark) { color: inherit; background: color-mix(in srgb, var(--hermes-accent) 24%, transparent); border-radius: 3px; padding: 0 0.1em; }
        :where(hr) { border: 0; border-top: 1px solid var(--hermes-border); margin: 1.2em 0; }

        :where(a[href]) {
          color: var(--hermes-accent);
          text-decoration-line: underline;
          text-decoration-thickness: 0.08em;
          text-underline-offset: 0.18em;
          text-decoration-color: color-mix(in srgb, var(--hermes-accent) 55%, transparent);
          overflow-wrap: anywhere;
          border-radius: 3px;
          -webkit-tap-highlight-color: color-mix(in srgb, var(--hermes-accent) 20%, transparent);
        }
        :where(a[href]:active) { opacity: 0.6; }
        :where(a[href], button, summary, input, select, textarea, [tabindex]):focus-visible { outline: 2px solid var(--hermes-accent); outline-offset: 2px; }

        :where(ul) { padding-inline-start: 1.35em; }
        :where(ol) { padding-inline-start: 3em; }
        :where(li + li) { margin-top: 0.3em; }
        :where(li > ul, li > ol) { margin: 0.3em 0 0; }
        :where(li > p) { margin: 0 0 0.35em; }
        :where(li)::marker { color: var(--hermes-muted); font-variant-numeric: tabular-nums; }
        :where(.task-list-item) { list-style: none; }
        :where(dt) { font-weight: 600; }
        :where(dd) { margin: 0 0 0.5em 1.1em; }

        :where(blockquote) {
          margin-inline: 0;
          padding: 0.1em 0 0.1em 0.9em;
          border-inline-start: 3px solid color-mix(in srgb, var(--hermes-accent) 45%, var(--hermes-border));
          color: var(--hermes-muted);
        }
        :where(figure) { margin-inline: 0; }
        :where(figcaption) { margin-top: 0.4em; font-size: 0.88em; color: var(--hermes-muted); }
        :where(img) { max-width: 100%; height: auto; }
        :where(svg, canvas) { max-width: 100%; }

        :where(code, kbd, samp, pre) { font-family: ui-monospace, "SF Mono", Menlo, monospace; }
        :where(code, kbd, samp) { font-size: 0.88em; }
        :where(:not(pre) > code, kbd, samp) { background: var(--hermes-surface); border-radius: 6px; padding: 0.1em 0.35em; overflow-wrap: anywhere; }
        :where(kbd) { border: 1px solid var(--hermes-border); border-bottom-width: 2px; }
        :where(pre) {
          overflow-x: auto;
          padding: 0.8em 0.95em;
          background: var(--hermes-surface);
          border-radius: 12px;
          font-size: 0.86em;
          line-height: 1.45;
          white-space: pre;
          tab-size: 4;
        }
        :where(pre code) { font-size: inherit; background: none; padding: 0; border-radius: 0; }

        :where([data-hermes-scroll]) { max-width: 100%; overflow-x: auto; border: 1px solid var(--hermes-border); border-radius: 12px; }
        :where([data-hermes-scroll] > table) { margin: 0; }
        :where(table) { border-collapse: separate; border-spacing: 0; min-width: 100%; font-size: 0.94em; font-variant-numeric: tabular-nums; }
        :where(caption) { padding: 0.55em 0.8em 0.35em; font-size: 0.92em; color: var(--hermes-muted); }
        :where(th, td) { padding: 0.55em 0.8em; vertical-align: top; border-bottom: 1px solid var(--hermes-border); }
        :where(th) { font-weight: 600; background: var(--hermes-surface); }
        :where(tr:last-child > th, tr:last-child > td) { border-bottom: 0; }
        :where(thead tr:last-child > th, thead tr:last-child > td) { border-bottom: 1px solid var(--hermes-border); }
        :where(tbody tr:nth-child(even) > td) { background: color-mix(in srgb, var(--hermes-surface) 45%, transparent); }

        :where(details) { border: 1px solid var(--hermes-border); border-radius: 12px; padding: 0 0.9em; }
        :where(details[open]) { padding-bottom: 0.75em; }
        :where(summary) { min-height: 44px; box-sizing: border-box; padding: 0.6em 0; font-weight: 600; cursor: pointer; -webkit-user-select: none; user-select: none; }
        :where(summary)::marker { color: var(--hermes-muted); }
        :where(summary)::-webkit-details-marker { color: var(--hermes-muted); }

        :where(button, input[type="button"], input[type="submit"], input[type="reset"]) {
          appearance: none;
          -webkit-appearance: none;
          box-sizing: border-box;
          min-width: 44px;
          min-height: 44px;
          padding: 0.45em 1em;
          font: inherit;
          font-size: 0.94em;
          font-weight: 600;
          line-height: 1.2;
          color: var(--hermes-text);
          background: var(--hermes-surface);
          border: 1px solid var(--hermes-border);
          border-radius: 12px;
          cursor: pointer;
          -webkit-user-select: none;
          user-select: none;
        }
        :where(button:active) { background: color-mix(in srgb, var(--hermes-accent) 16%, var(--hermes-surface)); }
        :where(button:disabled) { opacity: 0.45; }
        :where(button[aria-pressed="true"], button[aria-selected="true"], [role="tab"][aria-selected="true"], button.active, button.selected) {
          background: color-mix(in srgb, var(--hermes-accent) 18%, var(--hermes-surface));
          border-color: color-mix(in srgb, var(--hermes-accent) 55%, transparent);
        }
        :where([role="tablist"]) { display: flex; gap: 0.4em; overflow-x: auto; }
        :where(input:not([type]), input[type="text"], input[type="search"], input[type="number"], input[type="email"], input[type="url"], input[type="tel"], input[type="date"], input[type="time"], select, textarea) {
          box-sizing: border-box;
          max-width: 100%;
          min-height: 44px;
          padding: 0.4em 0.7em;
          font: inherit;
          color: var(--hermes-text);
          background: var(--hermes-surface);
          border: 1px solid var(--hermes-border);
          border-radius: 10px;
        }
        :where(textarea) { width: 100%; min-height: 88px; }
        :where(input[type="checkbox"], input[type="radio"]) { width: 1.15em; height: 1.15em; margin: 0 0.45em 0 0; vertical-align: -0.18em; accent-color: var(--hermes-accent); }
        /* GitHub-style task list items hang their checkbox where the bullet would be. */
        :where(.task-list-item > input[type="checkbox"]:first-child) { margin: 0 0.45em 0 -1.35em; }
        :where(input[type="range"], progress, meter) { max-width: 100%; accent-color: var(--hermes-accent); }
        :where(label:has(> input[type="checkbox"], > input[type="radio"])) {
          display: inline-flex;
          align-items: flex-start;
          gap: 0.5em;
          max-width: 100%;
          min-height: 44px;
          box-sizing: border-box;
          padding-block: 0.3em;
        }
        :where(label > input[type="checkbox"], label > input[type="radio"]) { flex: none; margin: 0.18em 0 0; }

        @media (prefers-reduced-motion: reduce) {
          *, *::before, *::after {
            animation-duration: 0.01ms !important;
            animation-iteration-count: 1 !important;
            transition-duration: 0.01ms !important;
            scroll-behavior: auto !important;
          }
        }
        """#
    }
}
