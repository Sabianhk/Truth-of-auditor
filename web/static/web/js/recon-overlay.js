/* Overlay "menyusun dossier" saat run rekonsiliasi disubmit. */
(function () {
  var form = document.getElementById('run-form');
  if (!form) return;
  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  form.addEventListener('submit', function () {
    var ov = document.createElement('div');
    ov.setAttribute('style',
      'position:fixed;inset:0;z-index:99;display:grid;place-items:center;' +
      'background:rgba(28,26,21,.86);color:#E9E4D6;font-family:"IBM Plex Mono",monospace');
    ov.innerHTML = '<div style="text-align:center"><canvas id="ov-c" width="260" height="80"></canvas>' +
      '<div style="font-size:12px;letter-spacing:.14em;text-transform:uppercase;margin-top:14px">Menyusun rekonsiliasi…</div></div>';
    document.body.appendChild(ov);
    if (reduce) return;
    var c = ov.querySelector('#ov-c'), x = c.getContext('2d'), N = 140, P = [];
    for (var i = 0; i < N; i++) P.push({ x: Math.random() * 260, y: Math.random() * 80, tx: 20 + (i % 28) * 8, ty: 24 + Math.floor(i / 28) * 8, v: .02 + Math.random() * .04 });
    (function tick() {
      x.clearRect(0, 0, 260, 80); x.fillStyle = '#E9E4D6';
      P.forEach(function (p) { p.x += (p.tx - p.x) * p.v; p.y += (p.ty - p.y) * p.v; x.fillRect(p.x, p.y, 2, 2); });
      if (document.body.contains(ov)) requestAnimationFrame(tick);
    })();
  });
})();
