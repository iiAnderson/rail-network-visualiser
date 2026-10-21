"""Wrap an SVG in a self-contained HTML page with wheel zoom (to the cursor), drag to pan and double-click zoom,
for maps too large for a browser's own zoom.

    python tools/svg_viewer.py map.svg map.html [title]
"""
import re
import sys

PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(title)s</title>
<style>
html,body{margin:0;height:100%%;background:#fff;font:14px system-ui,sans-serif;overflow:hidden}
#bar{position:fixed;top:8px;left:8px;z-index:2;background:#fffd;border:1px solid #ccc;border-radius:6px;padding:4px 8px}
#bar button{font-size:16px;margin-right:4px;cursor:pointer}
#stage{width:100vw;height:100vh;cursor:grab}
#stage.drag{cursor:grabbing}
svg{width:100%%;height:100%%;display:block}
</style></head><body>
<div id="bar"><b>%(title)s</b> &nbsp; <button id="in">+</button><button id="out">&minus;</button><button id="reset">reset</button>
<span id="z"></span> &nbsp; wheel = zoom, drag = pan</div>
<div id="stage">%(svg)s</div>
<script>
const stage = document.getElementById('stage'), svg = stage.querySelector('svg');
const home = svg.getAttribute('viewBox').split(/[ ,]+/).map(Number);
let vb = home.slice();
function apply(){ svg.setAttribute('viewBox', vb.join(' ')); document.getElementById('z').textContent = ' ' + Math.round(home[2] / vb[2] * 100) + '%%'; }
function zoomAt(px, py, k){           // px, py: cursor as a fraction of the stage
  const nw = Math.min(Math.max(vb[2] * k, home[2] / 400), home[2] * 2), nh = nw * home[3] / home[2];
  vb = [vb[0] + (vb[2] - nw) * px, vb[1] + (vb[3] - nh) * py, nw, nh]; apply();
}
function frac(e){
  const r = svg.getBoundingClientRect(), s = Math.min(r.width / vb[2], r.height / vb[3]);
  const ox = (r.width - vb[2] * s) / 2, oy = (r.height - vb[3] * s) / 2;
  return [(e.clientX - r.left - ox) / (vb[2] * s), (e.clientY - r.top - oy) / (vb[3] * s)];
}
stage.addEventListener('wheel', e => { e.preventDefault(); const f = frac(e); zoomAt(f[0], f[1], Math.exp(e.deltaY * 0.0015)); }, {passive: false});
stage.addEventListener('dblclick', e => { const f = frac(e); zoomAt(f[0], f[1], 0.4); });
let drag = null;
stage.addEventListener('pointerdown', e => { drag = {x: e.clientX, y: e.clientY, vb: vb.slice()}; stage.classList.add('drag'); stage.setPointerCapture(e.pointerId); });
stage.addEventListener('pointermove', e => {
  if (!drag) return;
  const r = svg.getBoundingClientRect(), s = Math.min(r.width / drag.vb[2], r.height / drag.vb[3]);
  vb = [drag.vb[0] - (e.clientX - drag.x) / s, drag.vb[1] - (e.clientY - drag.y) / s, drag.vb[2], drag.vb[3]]; apply();
});
stage.addEventListener('pointerup', () => { drag = null; stage.classList.remove('drag'); });
document.getElementById('in').onclick = () => zoomAt(.5, .5, .6);
document.getElementById('out').onclick = () => zoomAt(.5, .5, 1 / .6);
document.getElementById('reset').onclick = () => { vb = home.slice(); apply(); };
apply();
</script></body></html>
"""


def main():
    src, dst = sys.argv[1], sys.argv[2]
    title = sys.argv[3] if len(sys.argv) > 3 else src
    s = open(src).read()
    s = re.sub(r"<\?xml[^>]*\?>|<!DOCTYPE[^>]*>", "", s)
    head = re.search(r"<svg[^>]*>", s).group(0)
    new = re.sub(r'\s(width|height)="[^"]*"', "", head).replace("<svg", '<svg preserveAspectRatio="xMidYMid meet"', 1)
    open(dst, "w").write(PAGE % {"title": title, "svg": s.replace(head, new, 1)})
    print("wrote", dst)


if __name__ == "__main__":
    main()
