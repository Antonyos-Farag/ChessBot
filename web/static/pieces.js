/* Vector chess pieces.
 *
 * Every piece is drawn inside a 100x100 view box as a small stack of shapes so
 * it stays crisp at any size: the app scales the SVG, nothing here is raster.
 * The fill/stroke colours come from CSS (`--piece-fill` / `--piece-stroke`) so
 * the same artwork works for both colours.
 */

const BASE = '<rect class="pc" x="21" y="80" width="58" height="13" rx="6.5"/>';

const SHAPES = {
  // Pawn: ball head, tapered body, collar.
  p: `
    <circle class="pc" cx="50" cy="30" r="12.5"/>
    <path class="pc" d="M50 41c8 0 12.5 9 10.5 19.5 9.5 6.5 14.5 13.5 15.5 20.5H24c1-7 6-14 15.5-20.5C37.5 50 42 41 50 41z"/>
    ${BASE}
  `,
  // Rook: crenellated tower.
  r: `
    <path class="pc" d="M27 16h11v7h5v-7h14v7h5v-7h11v23H27z"/>
    <path class="pc" d="M34 39h32l4 25H30z"/>
    <path class="pc" d="M31 64h38l3 17H28z"/>
    ${BASE}
  `,
  // Knight: horse head with ear, muzzle and eye.
  n: `
    <path class="pc" d="M22 48C25 40 30 33 40 27L38 15L49 23L55 22L51 12L60 15C68 20 72 30 73 44C74 58 75 70 77 80H46C43 70 39 62 32 56C26 52 22 51 22 48z"/>
    <path class="pc-dark" d="M22 48c5 2 10 2 14-1" fill="none" stroke-width="2.6" stroke-linecap="round"/>
    <circle class="pc-dark" cx="41" cy="35" r="2.8"/>
    ${BASE}
  `,
  // Bishop: mitre with a slit and a ball on top.
  b: `
    <circle class="pc" cx="50" cy="14" r="6.5"/>
    <path class="pc" d="M50 20c14 5 20 20 11 32-3 5-6 7-11 7s-8-2-11-7c-9-12-3-27 11-32z"/>
    <path class="pc-dark" d="M57 30l-11 15" stroke-width="4.5" stroke-linecap="round" fill="none"/>
    <rect class="pc" x="35" y="58" width="30" height="9" rx="4.5"/>
    <path class="pc" d="M38 67h24l5 15H33z"/>
    ${BASE}
  `,
  // Queen: five-pointed crown with pearls.
  q: `
    <circle class="pc" cx="24" cy="28" r="5"/>
    <circle class="pc" cx="38" cy="18" r="5"/>
    <circle class="pc" cx="50" cy="14" r="5.5"/>
    <circle class="pc" cx="62" cy="18" r="5"/>
    <circle class="pc" cx="76" cy="28" r="5"/>
    <path class="pc" d="M24 30l14 12h24l14-12-6 26H30z"/>
    <rect class="pc" x="30" y="55" width="40" height="9" rx="4.5"/>
    <path class="pc" d="M31 64h38l4 18H27z"/>
    ${BASE}
  `,
  // King: cross, dome crown, flared robe.
  k: `
    <path class="pc" d="M47 6h6v14h-6z"/>
    <path class="pc" d="M40 11h20v6H40z"/>
    <path class="pc" d="M50 20c13 0 21 6 21 17H29c0-11 8-17 21-17z"/>
    <path class="pc" d="M33 37h34l6 25H27z"/>
    <path class="pc" d="M30 62h40l4 20H26z"/>
    ${BASE}
  `,
};

/** Return the SVG markup for a piece such as "N" (white knight) or "q". */
export function pieceSvg(symbol, size = null) {
  const type = symbol.toLowerCase();
  const colour = symbol === symbol.toUpperCase() ? 'white' : 'black';
  const shapes = SHAPES[type] || '';
  const dims = size ? ` width="${size}" height="${size}"` : '';
  return `<svg class="piece piece-${colour} piece-${type}" viewBox="0 0 100 100"${dims}
     xmlns="http://www.w3.org/2000/svg" aria-hidden="true">${shapes}</svg>`;
}

export const PIECE_TYPES = Object.keys(SHAPES);
