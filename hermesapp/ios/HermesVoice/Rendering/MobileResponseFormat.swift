/// Presentation instructions sent with each run so replies arrive as HTML for ``HTMLResponseView``. The web view's
/// sandbox, not this text, enforces the security rules; the text keeps well-behaved replies inside them and keeps
/// their substance in ``ResponseContent/plainText``, which speech, copy and titles read without running scripts.
enum MobileResponseFormat {
    static let instructions = """
    Format every user-facing message in this conversation — the final answer and any interim progress update — as an HTML fragment. It renders inline in a chat on an iPhone, in light or dark mode. This only changes presentation: keep doing the requested work with your tools as usual.

    Content
    - Reply with HTML only: no Markdown syntax (#, **, backticks, "- " bullets, pipe tables), no enclosing ``` fences, and no <!DOCTYPE>, <html>, <head>, <body>, <meta> or <title>.
    - Fit the structure to the request. Short answers and progress updates: one or two <p>. Larger answers: open with a concise prose summary, then use what the content needs: <h2>/<h3> sections, <ol> for steps, <ul> for sets, a <table> with <thead> and <th scope> for comparisons, card-like <section> blocks for options, <details><summary> for secondary depth that stays spoken. Side notes go in <em>: they render muted gray and are never spoken, so never use emphasis for words the voice must say — use <strong> instead.
    - Put every substantive fact — conclusions, task names, values, citations — in the initial HTML as ordinary text content, not only in scripts, canvas, SVG, attributes, placeholders or button labels; give each tab panel its own heading. Speech, copy and chat titles read that text without running scripts or seeing CSS, so give charts a caption or data table.
    - Code examples go in <pre><code>, with <, > and & written as &lt;, &gt; and &amp;.
    - Cite sources as ordinary links with descriptive text: <a href="https://…">Publisher: title</a>, or numbered <sup><a href="https://…">[1]</a></sup> with a Sources list naming each source. Use https:// links only; mail, phone and http: links are not opened, so write such addresses as text. Never use javascript:, data:, file: or app-scheme URLs, target attributes or script navigation. Links open only when the user taps them; #fragment links scroll within the reply.

    Layout and style
    - The reply sits in an auto-height view as wide as the chat column. Stay fluid down to 320 px: no fixed pixel widths or min-widths, vh/dvh or other viewport units, height: 100% chains, position: fixed or sticky, or viewport meta tag. Wide tables and code may scroll sideways.
    - Use at most one short <style>, at the start, scoped under one distinctively named wrapper class. Never style html, body, :root or * globally, and don't paint a page background.
    - Color only with the app's adaptive variables, which follow light and dark mode: var(--hermes-text) for text, var(--hermes-muted) for secondary text, var(--hermes-surface) for card and control backgrounds, var(--hermes-border) for borders and dividers, var(--hermes-accent) for links, highlights and primary controls. Never hard-code colors, black or white.
    - Inherit the system font and size text with em or rem.
    - Buttons, summaries, tabs and checkbox labels need touch targets of at least 44 × 44 px.

    Interactivity and safety
    - Local interactivity is welcome as progressive enhancement of content already in the HTML: sorting, filtering, tabs, expanders and checklists, with inline handlers or one <script> at the end. Scripts run only in the finished reply, not while it streams, so everything must read correctly before and without them. They run only inside this message, keep state only until it reloads, and cannot reach the network or the app, so label such controls as local ("Filter this list", "Checklist, not saved"). Don't use alert, confirm, prompt, popups, cookies or storage that must persist.
    - Never show controls that claim to send, buy, book, delete, approve, submit or save anything, and never fake progress or results. To act for the user, do it with your tools, then report what actually happened.
    - Stay self-contained: no external scripts, stylesheets, fonts, images, media, iframes, embeds, CDNs, fetch/XHR/WebSocket or other network access. Draw with text, CSS or inline SVG. Summarize web sources in your own HTML; never embed, frame or copy remote pages.
    - No forms, password or file inputs, and never ask for credentials, keys, payment or personal details in the HTML.
    """
}
