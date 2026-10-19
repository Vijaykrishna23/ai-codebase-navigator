import type { HighlighterCore } from "shiki/core";

const LANGS: Record<string, string> = {
  py: "python",
  js: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  jsx: "jsx",
  ts: "typescript",
  mts: "typescript",
  cts: "typescript",
  tsx: "tsx",
};

export function langOf(path: string): string | null {
  return LANGS[path.split(".").pop()?.toLowerCase() ?? ""] ?? null;
}

let highlighter: Promise<HighlighterCore> | null = null;

// Loaded on first use with only the languages the index parses, so the
// grammars stay out of the initial bundle.
function load(): Promise<HighlighterCore> {
  highlighter ??= (async () => {
    const [{ createHighlighterCore }, { createJavaScriptRegexEngine }] = await Promise.all([
      import("shiki/core"),
      import("shiki/engine/javascript"),
    ]);
    return createHighlighterCore({
      themes: [import("shiki/themes/github-light.mjs"), import("shiki/themes/github-dark.mjs")],
      langs: [
        import("shiki/langs/python.mjs"),
        import("shiki/langs/javascript.mjs"),
        import("shiki/langs/jsx.mjs"),
        import("shiki/langs/typescript.mjs"),
        import("shiki/langs/tsx.mjs"),
      ],
      engine: createJavaScriptRegexEngine(),
    });
  })();
  return highlighter;
}

export async function highlight(code: string, lang: string): Promise<string> {
  const h = await load();
  return h.codeToHtml(code, {
    lang,
    themes: { light: "github-light", dark: "github-dark" },
    defaultColor: false,
  });
}
