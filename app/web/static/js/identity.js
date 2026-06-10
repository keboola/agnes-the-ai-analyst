/* window.AgnesIdentity — shared user-identity rendering helpers.
 *
 * Single source of truth for the avatar circle shown across admin pages
 * (/admin/users, /admin/adoption, …). Centralised here so the two
 * derivations — 2-letter initials and the stable hash → color — can never
 * drift between pages again (the bug that motivated this file: the adoption
 * dashboard computed initials from the email local-part and used a fixed
 * background, so the same person looked different than on /admin/users).
 *
 * Both functions are pure; pass the same seed on every page to get the same
 * circle. Convention used by callers:
 *   initials:    AgnesIdentity.initials(name || email)
 *   color seed:  AgnesIdentity.avatarColor(email || id)
 */
window.AgnesIdentity = {
  // 2-letter initials from a name (or any string). Splits on whitespace,
  // '@', '.', '_' and '-' so it works for both "Ada Lovelace" → "AL" and
  // "ada.lovelace@x.com" → "AL". Returns "?" when nothing usable is left.
  initials(s) {
    const parts = String(s || "").trim().split(/[\s@._-]+/).filter(Boolean);
    return ((parts[0]?.[0] || "?") + (parts[1]?.[0] || "")).toUpperCase();
  },

  // Stable hash → hsl() color. Deterministic per seed, so the same user
  // gets the same circle color everywhere without storing a color in the DB.
  avatarColor(seed) {
    let h = 0;
    for (const c of seed || "") h = (h * 31 + c.charCodeAt(0)) >>> 0;
    return `hsl(${h % 360}, 55%, 50%)`;
  },
};
