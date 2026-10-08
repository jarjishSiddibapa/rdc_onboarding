/* ════════════════════════════════════════════════════
   BUSY PILL — the one app-wide "something is happening" indicator.
   Replaces ad-hoc "Loading…" / "Sending…" / "Generating…" label swaps.

   Shows automatically for:
     • every same-origin fetch() that takes longer than ~250ms
       (opt out with fetch(url, {busy: false}) — background pollers do)
     • page navigations (link clicks / form submits) that take longer than ~300ms

   Manual API (window.Busy):
     Busy.show() / Busy.hide()   ref-counted, shows immediately
     Busy.showFor(ms)            shows now, hides itself after ms (file downloads —
                                 there is no completion event for a plain <a> download)
════════════════════════════════════════════════════ */
(function () {
  var MSG = 'One sec... pretending this is very complicated 😎';
  var FETCH_DELAY = 250;   // don't flash the pill for instant requests
  var NAV_DELAY = 300;
  var NAV_CAP = 30000;     // a "navigation" that returns a file never unloads the page

  var el = null;
  var count = 0;

  function ensure() {
    if (el) return el;
    el = document.createElement('div');
    el.id = 'busy-pill';
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    var spin = document.createElement('span');
    spin.className = 'busy-spinner';
    spin.setAttribute('aria-hidden', 'true');
    var txt = document.createElement('span');
    txt.className = 'busy-text';
    txt.textContent = MSG;
    el.appendChild(spin);
    el.appendChild(txt);
    document.body.appendChild(el);
    return el;
  }

  function render() {
    if (!document.body) return;
    if (count > 0) { ensure().classList.add('busy-on'); }
    else if (el) { el.classList.remove('busy-on'); }
  }

  function show() { count++; render(); }
  function hide() { count = Math.max(0, count - 1); render(); }

  function showFor(ms) {
    show();
    var done = false;
    setTimeout(function () { if (!done) { done = true; hide(); } }, ms);
  }

  window.Busy = { show: show, hide: hide, showFor: showFor };

  /* ── fetch(): track every same-origin request that lingers ── */
  if (window.fetch) {
    var origFetch = window.fetch;
    window.fetch = function (input, init) {
      var p = origFetch.apply(this, arguments);
      try {
        if (init && init.busy === false) return p;
        var url = typeof input === 'string' ? input : (input && input.url) || '';
        var u = new URL(url, location.href);
        if (u.origin !== location.origin) return p;
        var shown = false;
        var t = setTimeout(function () { shown = true; show(); }, FETCH_DELAY);
        var settle = function () { clearTimeout(t); if (shown) { shown = false; hide(); } };
        p.then(settle, settle);
      } catch (e) { /* never let the indicator break a request */ }
      return p;
    };
  }

  /* ── navigations: link clicks and form submits that take a while ── */
  var navTimer = null, navShown = false, navCap = null;

  function navStop() {
    clearTimeout(navTimer); clearTimeout(navCap);
    if (navShown) { navShown = false; hide(); }
  }
  function navStart() {
    navStop();
    navTimer = setTimeout(function () {
      navShown = true; show();
      navCap = setTimeout(navStop, NAV_CAP);
    }, NAV_DELAY);
  }

  // Back/forward cache restores the page as it was left — clear any stuck pill.
  window.addEventListener('pageshow', function () { navStop(); });

  document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest && e.target.closest('a[href]');
    if (!a || a.hasAttribute('download') || a.hasAttribute('data-no-busy')) return;
    var tgt = a.getAttribute('target');
    if (tgt && tgt !== '_self') return;
    var u;
    try { u = new URL(a.href, location.href); } catch (err) { return; }
    if (u.origin !== location.origin || !/^https?:$/.test(u.protocol)) return;
    if (u.pathname === location.pathname && u.search === location.search) return;  // hash / same-page
    // File downloads and document views don't unload the page.
    if (/\/download|\/documents\/|^\/static\//i.test(u.pathname)) return;
    navStart();
  });

  document.addEventListener('submit', function (e) {
    if (e.defaultPrevented) return;
    var f = e.target;
    if (!f || f.hasAttribute('data-no-busy')) return;
    if (f.target && f.target !== '_self') return;
    navStart();
  });
})();
