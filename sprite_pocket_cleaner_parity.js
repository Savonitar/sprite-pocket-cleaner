// Run sprite_pocket_cleaner.html's OWN fill code over a fixture and print the per-frame result.
//
// This exists so sprite_pocket_cleaner.py --parity can prove the browser page previews
// exactly what the python tool writes. The two are separate implementations of one algorithm
// and have already drifted apart once, which would have shown a clean preview while the png
// kept its pocket.
//
// Nothing here reimplements the algorithm: the <script> is read out of the shipped html and
// executed against DOM stubs, so what runs is what ships. Driven by python:
//
//     python3 sprite_pocket_cleaner.py --parity
//
// Standalone: node sprite_pocket_cleaner_parity.js <fixture-without-extension>
// reading <fixture>.json (w/h/boxes/seed/frame/tolerance/search/areaRatio/seedCandidates)
// and <fixture>.rgba (raw RGBA bytes), printing a JSON array of px counts per frame.
const fs = require('fs');
const path = require('path');

const fixture = process.argv[2];
if (!fixture) {
  console.error('usage: node sprite_pocket_cleaner_parity.js <fixture-without-extension>');
  process.exit(2);
}

const html = fs.readFileSync(path.join(__dirname, 'sprite_pocket_cleaner.html'), 'utf8');
const open = html.lastIndexOf('<script>');
const close = html.lastIndexOf('</script>');
if (open < 0 || close < 0) {
  console.error('no <script> block in sprite_pocket_cleaner.html');
  process.exit(2);
}
const source = html.slice(open + '<script>'.length, close);

// Enough of a DOM for the page's top-level wiring to run. Every handler is inert - only the
// pure fill functions are called from here.
const element = () => new Proxy({}, {
  get: (target, key) => {
    if (key === 'getContext') return () => new Proxy({}, { get: () => () => {} });
    if (key === 'getBoundingClientRect') return () => ({ left: 0, top: 0, width: 0, height: 0 });
    if (key === 'addEventListener' || key === 'appendChild') return () => {};
    if (key === 'value') return '0';
    if (key === 'checked') return false;
    if (key === 'style') return {};
    return target[key];
  },
  set: (target, key, value) => { target[key] = value; return true; }
});

const factory = new Function(
  'document', 'window', 'Image', 'FileReader', 'Blob', 'URL', 'DataTransfer', 'alert',
  'ImageData', 'DragEvent', 'MouseEvent',
  source + '\n; return { floodFill, findSeeds, brightest, seedIsBackgroundLike, ' +
  'load: (i, w, h, b) => { img = i; W = w; H = h; boxes = b; } };');

const api = factory(
  { getElementById: element, createElement: element, addEventListener: () => {} },
  { addEventListener: () => {} },
  class {}, class {}, class {}, { createObjectURL: () => '' }, class {}, () => {},
  class {}, class {}, class {});

const cfg = JSON.parse(fs.readFileSync(fixture + '.json', 'utf8'));
const data = new Uint8ClampedArray(fs.readFileSync(fixture + '.rgba'));
api.load({ data, width: cfg.w, height: cfg.h }, cfg.w, cfg.h, cfg.boxes);

const [sx, sy] = cfg.seed;
const home = cfg.boxes[cfg.frame];
const hit = api.floodFill(sx, sy, cfg.tolerance, home);
if (!hit) { console.error('the fixture seed filled nothing'); process.exit(2); }

const per = new Array(cfg.boxes.length).fill(0);
per[cfg.frame] = hit.count;
const ref = api.brightest(hit.mask);
for (let j = 0; j < cfg.boxes.length; j++) {
  if (j === cfg.frame) continue;
  const box = cfg.boxes[j];
  const gx = box[0] + (sx - home[0]);
  const gy = box[1] + (sy - home[1]);
  const cands = api.findSeeds(box, gx, gy, ref, cfg.tolerance, cfg.search)
                   .slice(0, cfg.seedCandidates);
  let best = null, bestCount = 0;
  for (const [cx, cy] of cands) {
    if (best && best[cy * cfg.w + cx]) continue;
    const other = api.floodFill(cx, cy, cfg.tolerance, box);
    if (!other) continue;
    if (other.count > hit.count * cfg.areaRatio) continue;
    if (other.count > bestCount) { best = other.mask; bestCount = other.count; }
  }
  per[j] = bestCount;
}
const guards = (cfg.guardSeeds || []).map(seed => api.seedIsBackgroundLike(seed));
process.stdout.write(JSON.stringify({ per, guards }));
