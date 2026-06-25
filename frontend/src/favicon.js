// Browser-tab favicon, wired the SAME de-identified way as the sidebar avatar.
//
// import.meta.glob tolerates ZERO matches at build time, so:
//   - a fresh clone / repo HEAD has NO favicon.local.* -> faviconUrl is null ->
//     applyFavicon() does nothing -> the browser shows its default tab icon.
//   - the maintainer's machine / the VPS has frontend/src/assets/favicon.local.png
//     -> Vite bundles it (hashed) under /assets/ and we inject <link rel="icon">.
//
// Nothing personal is referenced from tracked files; the image stays gitignored
// (see .gitignore: frontend/src/assets/favicon.local.*).
const _icons = import.meta.glob("./assets/favicon.local.*", {
  eager: true,
  query: "?url",
  import: "default",
});
const faviconUrl = Object.values(_icons)[0] ?? null;

// Set (or create) the <link rel="icon"> to the bundled favicon, when present.
export function applyFavicon() {
  if (!faviconUrl) return; // fresh clone: no personal favicon -> browser default
  let link = document.querySelector("link[rel='icon']");
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    document.head.appendChild(link);
  }
  link.type = "image/png";
  link.href = faviconUrl;
}
