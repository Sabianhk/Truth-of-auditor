/* Truth of Auditor — motion layer (Forensic Ledger). GSAP UMD global; htmx-aware. */
(function () {
  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  var seen = false;
  try { seen = sessionStorage.getItem('toa-motion-seen') === '1'; } catch (e) {}

  function countUp(el) {
    var to = parseFloat(el.getAttribute('data-count')) || 0, dur = 900, t0 = null;
    function step(ts) {
      if (!t0) t0 = ts;
      var p = Math.min((ts - t0) / dur, 1), e = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(to * e).toLocaleString('id-ID');
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  function init(root) {
    root = root || document;
    if (window.gsap && !reduce) {
      var els = root.querySelectorAll('.reveal:not([data-motion-done])');
      if (els.length) {
        els.forEach(function (el) { el.setAttribute('data-motion-done', '1'); });
        if (seen) {
          gsap.set(els, { opacity: 1, y: 0 });
        } else {
          gsap.set(els, { opacity: 1 });
          gsap.from(els, { y: 14, opacity: 0, duration: .55, ease: 'power3.out', stagger: .05 });
        }
      }
    }
    root.querySelectorAll('[data-count]:not([data-motion-done])').forEach(function (el) {
      el.setAttribute('data-motion-done', '1');
      var finalVal = (parseFloat(el.getAttribute('data-count')) || 0).toLocaleString('id-ID');
      (reduce || seen) ? el.textContent = finalVal : countUp(el);
    });
    if (!seen) {
      try { sessionStorage.setItem('toa-motion-seen', '1'); } catch (e) {}
    }
  }

  window.ToaMotion = { init: init };
  if (document.readyState !== 'loading') init(); else document.addEventListener('DOMContentLoaded', function () { init(); });
  document.body && document.body.addEventListener('htmx:afterSwap', function (e) { init(e.target); });
  // htmx belum tentu loaded saat defer — pasang juga saat DOM siap:
  document.addEventListener('DOMContentLoaded', function () {
    document.body.addEventListener('htmx:afterSwap', function (e) { init(e.target); });
  });
})();
