// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * ┌─────────────────────────────────────────────────────────────────────────┐
 * │  THE FIRMAMENT — palette                                                │
 * └─────────────────────────────────────────────────────────────────────────┘
 *
 * Every domain holds its own colour, so a reader can tell the topics of a field
 * apart at a glance and follow one across the sky.
 *
 * Domains are not authored here — a field can have a hundred, and they arrive
 * live — so colours are DERIVED, once, from the substrate:
 *
 *   · Hue steps round the wheel by the golden ratio, in the order the domains
 *     first appeared in the field. Each new hue lands as far as it can from the
 *     ones already taken, so neighbours in time never look alike, and a domain
 *     keeps its colour when later ones arrive.
 *   · The volume, its dust and the strands between domains use the full colour.
 *     Words use a lighter ink of the same hue, so they stay legible on the dark.
 *
 * (An earlier palette kept every domain a near-white, on the view that colour
 * would turn regions into categories. Fields asked to tell their topics apart.)
 */
export const VOID_COLOR = 0x050508;
export const STARLIGHT = [0.86, 0.9, 1.0];

const GOLDEN = 0.6180339887498949;
const tints = new Map();
const inks = new Map();

/** Compute and cache every domain's colour. Call once, after the Substrate exists. */
export function paintDomains(substrate) {
  tints.clear();
  inks.clear();
  substrate.domains.forEach((d, i) => {
    const hue = (0.07 + i * GOLDEN) % 1;
    tints.set(d.id, hsl(hue, 0.7, 0.6));
    inks.set(d.id, hsl(hue, 0.6, 0.83));
  });
}

/** A domain's colour: its region, its dust, and the strands that leave it. */
export function tintFor(domainId) {
  return tints.get(domainId) ?? STARLIGHT;
}

/** The same hue, lighter: for the words of a domain, so they read on the dark. */
export function inkFor(domainId) {
  return inks.get(domainId) ?? STARLIGHT;
}

/** Colours as a flat array indexed by domain index — convenient for buffers. */
export function tintTable(domains) {
  return domains.map((d) => tintFor(d.id));
}

function hsl(h, s, l) {
  const k = (n) => (n + h * 12) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return [f(0), f(8), f(4)];
}
