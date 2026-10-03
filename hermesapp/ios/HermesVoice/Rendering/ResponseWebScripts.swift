/// Trusted main-frame instrumentation and page-world restrictions injected into every response frame.
enum ResponseWebScripts {
    /// The only script message handler, registered in `WKContentWorld.defaultClient`. The reply's own scripts run in
    /// the page world, where no handler exists, so they cannot message the app at all.
    static let handlerName = "hermesResponse"

    /// Trusted instrumentation, run in `WKContentWorld.defaultClient`. It shares the DOM with the reply but none of
    /// its JavaScript, so reply scripts can neither call nor alter these functions or the handler. It reports the
    /// content height, turns real taps on links into requests the app validates, keeps `#fragment` links in the
    /// document, removes frames and resource hints scripts insert, lets wide tables scroll sideways, and applies
    /// streamed updates in place.
    ///
    /// Every message carries the document generation stamped on `<html>` before any reply content parsed, so the app
    /// can ignore messages from a document it has replaced.
    static let instrumentation = #"""
    (() => {
      "use strict";
      const handler = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.hermesResponse;
      const html = document.documentElement;
      const generation = Number(html && html.getAttribute("data-hermes-generation"));
      if (!handler || !Number.isSafeInteger(generation)) return;
      const streaming = html.getAttribute("data-hermes-mode") === "stream";
      const XLINK = "http://www.w3.org/1999/xlink";
      const removedElements = "iframe, frame, frameset, object, embed, portal, fencedframe, link";

      const post = (message) => {
        message.generation = generation;
        try { handler.postMessage(message); } catch (_) {}
      };

      // A streaming reply shows its newest part when it is taller than its frame.
      const followTail = () => {
        if (!streaming) return;
        const end = document.documentElement.scrollHeight - window.innerHeight;
        if (end > 0 && Math.abs(window.scrollY - end) > 1) window.scrollTo(0, end);
      };

      // The height of the content itself, not of the viewport: viewport-relative page styles cannot feed back into it.
      let lastHeight = -1;
      let lastMeasurement = 0;
      let measurementTimer = 0;
      const measure = () => {
        const body = document.body;
        if (!body) return;
        if (typing && performance.now() - lastMeasurement < 100) {
          if (!measurementTimer) measurementTimer = setTimeout(() => { measurementTimer = 0; measure(); }, 100);
          return;
        }
        lastMeasurement = performance.now();
        reportVisible();
        let bottom = 0;
        for (const child of body.children) {
          const rect = child.getBoundingClientRect();
          if (rect.width > 0 || rect.height > 0) bottom = Math.max(bottom, rect.bottom);
        }
        const height = Math.ceil(bottom + window.scrollY);
        if (Number.isFinite(height) && height >= 0 && height !== lastHeight) {
          lastHeight = height;
          post({ type: "size", height });
        }
        followTail();
      };

      const wrapTables = (scope) => {
        if (!scope || scope.nodeType !== 1) return;
        const tables = scope.localName === "table" ? [scope] : Array.from(scope.getElementsByTagName("table"));
        for (const table of tables) {
          const parent = table.parentElement;
          if (!parent || parent.hasAttribute("data-hermes-scroll") || parent.closest("table")) continue;
          const wrapper = document.createElement("div");
          wrapper.setAttribute("data-hermes-scroll", "");
          parent.insertBefore(wrapper, table);
          wrapper.appendChild(table);
        }
      };

      // Frames would give scripts a second, unmodified window; links would load resources or hint connections.
      let parsed = false;
      const inspect = (node) => {
        if (node.nodeType !== 1) return;
        if (node.matches(removedElements)) {
          node.remove();
          return;
        }
        for (const element of node.querySelectorAll(removedElements)) element.remove();
        if (parsed) wrapTables(node);
      };
      new MutationObserver((records) => {
        for (const record of records) {
          for (const node of record.addedNodes) inspect(node);
        }
      }).observe(html, { childList: true, subtree: true });

      const anchorFor = (event) => {
        for (const node of event.composedPath()) {
          if (!node || node.nodeType !== 1) continue;
          const name = node.localName;
          if ((name === "a" || name === "area") && (node.hasAttribute("href") || node.hasAttributeNS(XLINK, "href"))) return node;
        }
        return null;
      };

      const revealFragment = (hash) => {
        let id = hash.slice(1);
        try { id = decodeURIComponent(id); } catch (_) {}
        if (!id) return;
        const target = document.getElementById(id) || document.getElementsByName(id)[0];
        if (!target) return;
        const top = target.getBoundingClientRect().top + window.scrollY;
        if (Number.isFinite(top)) post({ type: "anchor", top: Math.max(0, top) });
        if (target.tabIndex < 0 && !target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
        try { target.focus({ preventScroll: true }); } catch (_) {}
      };

      // File pickers could ask for camera or photo access; replies have no use for them.
      window.addEventListener("click", (event) => {
        const target = event.target;
        if (target && target.nodeType === 1 && target.localName === "input" && String(target.type).toLowerCase() === "file") {
          event.preventDefault();
        }
      }, true);

      // Registered before any reply script, on the window's bubble phase: the reply's own handlers run first and may
      // cancel a click to handle it locally. A link never navigates this document; only a real tap (`isTrusted`, which
      // scripts cannot forge) asks the app to open it, and the app decides whether it may.
      window.addEventListener("click", (event) => {
        if (event.defaultPrevented) return;
        const anchor = anchorFor(event);
        if (!anchor) return;
        event.preventDefault();
        if (!event.isTrusted || event.button !== 0) return;
        const raw = (anchor.getAttribute("href") ?? anchor.getAttributeNS(XLINK, "href") ?? "").trim();
        let url = null;
        try { url = new URL(raw, document.baseURI); } catch (_) {}
        if (url && url.href.split("#")[0] === document.URL.split("#")[0]) {
          revealFragment(url.hash);
          return;
        }
        if ((url && url.protocol === "javascript:") || /^javascript:/i.test(raw)) return;
        post({ type: "link", href: url ? url.href : raw });
      });

      const start = () => {
        parsed = true;
        const content = document.getElementById("hermes-root");
        wrapTables(content);
        const resizes = new ResizeObserver(measure);
        if (content) resizes.observe(content);
        if (document.body) resizes.observe(document.body);
        measure();
        post({ type: "ready" });
      };

      // Each sanitized fragment is parsed once, off-DOM. Only text nodes are masked; tags, CSS, scripts and control
      // values are never typed. Inserting a fragment never executes its scripts. The native final reload enables
      // those scripts only after the latest final snapshot has been revealed.
      const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });
      let typing = null;
      let animationFrame = 0;
      let reportedVisible = false;
      const reportVisible = () => {
        if (reportedVisible) return;
        const root = document.getElementById("hermes-root");
        if (!root) return;
        const displayed = (element) => {
          for (let ancestor = element; ancestor; ancestor = ancestor.parentElement) {
            const style = getComputedStyle(ancestor);
            if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse" || Number(style.opacity) === 0) return false;
          }
          return true;
        };
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        let visible = false;
        while (walker.nextNode()) {
          const node = walker.currentNode;
          const parent = node.parentElement;
          if (!/\S/u.test(node.data) || !parent || parent.closest("script, style, noscript, template, textarea, select, option") || !displayed(parent)) continue;
          const range = document.createRange();
          range.selectNodeContents(node);
          const bounds = range.getBoundingClientRect();
          if (bounds.width > 0 && bounds.height > 0) { visible = true; break; }
        }
        if (!visible) {
          visible = Array.from(root.querySelectorAll("img, svg, canvas")).some((element) => {
            const bounds = element.getBoundingClientRect();
            return bounds.width > 0 && bounds.height > 0 && displayed(element);
          });
        }
        if (visible) {
          reportedVisible = true;
          post({ type: "contentVisible" });
        }
      };
      const reveal = (state, count) => {
        for (const entry of state.nodes) {
          const visible = Math.min(entry.characters.length, Math.max(0, count - entry.offset));
          if (visible !== entry.visible) {
            entry.node.data = entry.characters.slice(0, visible).join("");
            entry.visible = visible;
          }
        }
        state.revealed = count;
      };
      const finishTyping = (state) => {
        if (state.isFinal && !state.finished) {
          state.finished = true;
          post({ type: "typingFinished", revision: state.revision });
        }
      };
      const tick = (time) => {
        animationFrame = 0;
        const state = typing;
        if (!state || document.hidden) return;
        // A suspended/backgrounded view does not jump straight to the end when it returns.
        const elapsed = state.lastTime ? Math.min(0.1, (time - state.lastTime) / 1000) : 0;
        state.lastTime = time;
        state.credit += elapsed * Math.max(48, state.total / 9);
        const advance = Math.floor(state.credit);
        state.credit -= advance;
        if (advance) {
          reveal(state, Math.min(state.total, state.revealed + advance));
          reportVisible();
          measure();
        }
        if (state.revealed >= state.total) {
          finishTyping(state);
        } else {
          animationFrame = requestAnimationFrame(tick);
        }
      };
      const scheduleTyping = () => {
        if (!animationFrame && typing && !document.hidden) animationFrame = requestAnimationFrame(tick);
      };
      document.addEventListener("visibilitychange", () => {
        if (animationFrame) cancelAnimationFrame(animationFrame);
        animationFrame = 0;
        if (typing) typing.lastTime = 0;
        scheduleTyping();
      });
      window.addEventListener("pagehide", () => {
        if (animationFrame) cancelAnimationFrame(animationFrame);
        if (measurementTimer) clearTimeout(measurementTimer);
        animationFrame = 0;
        typing = null;
      });
      const render = (markup, animateTyping = false, isFinal = false, revision = 0) => {
        const content = document.getElementById("hermes-root");
        if (!content || typeof markup !== "string") return false;
        if (animationFrame) cancelAnimationFrame(animationFrame);
        animationFrame = 0;
        if (!animateTyping) {
          typing = null;
          content.innerHTML = markup;
        } else {
          const fragment = document.createElement("template");
          fragment.innerHTML = markup;
          const walker = document.createTreeWalker(fragment.content, NodeFilter.SHOW_TEXT);
          const nodes = [];
          let total = 0;
          let fullText = "";
          while (walker.nextNode()) {
            const node = walker.currentNode;
            const parent = node.parentElement;
            if (!parent || parent.closest("script, style, noscript, template, textarea, select, option, input, [hidden], [aria-hidden='true']")) continue;
            const characters = Array.from(segmenter.segment(node.data), (part) => part.segment);
            fullText += node.data;
            nodes.push({ node, characters, offset: total, visible: -1 });
            total += characters.length;
          }
          // Preserve an append-only stream's exact prefix, not fuzzy matching or duplicated text. A changed
          // authoritative snapshot starts again in this same row, still fully masked before it reaches the DOM.
          const previous = typing;
          const revealed = previous && fullText.startsWith(previous.fullText) ? Math.min(previous.revealed, total) : 0;
          typing = { nodes, total, fullText, revealed: 0, credit: 0, lastTime: 0, isFinal, revision, finished: false };
          reveal(typing, revealed);
          content.replaceChildren(fragment.content);
          scheduleTyping();
        }
        wrapTables(content);
        measure();
        return true;
      };
      Object.defineProperty(globalThis, "__hermesResponse", { value: Object.freeze({ render }) });
      if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start, { once: true });
      } else {
        start();
      }
      window.addEventListener("load", measure);
      window.addEventListener("resize", measure);
    })();
    """#

    /// Runs in the page world before the reply's scripts. It holds no bridge to the app. Replies get per-document,
    /// in-memory `localStorage` and `sessionStorage` — state lasts as long as the document and is never shared with
    /// another reply — and lose the page APIs that could reach the network or make sound on their own outside the
    /// content security policy: WebRTC peer connections and speech synthesis.
    static let pageEnvironment = #"""
    (() => {
      "use strict";
      const define = (target, name, value, enumerable) => {
        try { Object.defineProperty(target, name, { value, enumerable, configurable: false, writable: false }); } catch (_) {}
      };
      const memoryStorage = () => {
        const values = new Map();
        return {
          get length() { return values.size; },
          key(index) { const keys = Array.from(values.keys()); return keys[Number(index)] ?? null; },
          getItem(key) { key = String(key); return values.has(key) ? values.get(key) : null; },
          setItem(key, value) { values.set(String(key), String(value)); },
          removeItem(key) { values.delete(String(key)); },
          clear() { values.clear(); },
        };
      };
      define(window, "localStorage", memoryStorage(), true);
      define(window, "sessionStorage", memoryStorage(), true);
      define(window, "RTCPeerConnection", undefined, false);
      define(window, "webkitRTCPeerConnection", undefined, false);
      if (typeof SpeechSynthesis === "function") define(SpeechSynthesis.prototype, "speak", function speak() {}, false);
    })();
    """#
}
