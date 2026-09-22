/* ════════════════════════════════════════════════════
   THEME — segmented switcher + localStorage
════════════════════════════════════════════════════ */
(function() {
  var STORAGE_KEY = 'rdc_theme';
  var html      = document.documentElement;
  var lightBtn  = document.getElementById('theme-light-btn');
  var darkBtn   = document.getElementById('theme-dark-btn');

  function applyTheme(t) {
    html.setAttribute('data-theme', t);
    localStorage.setItem(STORAGE_KEY, t);
    if (lightBtn) lightBtn.classList.toggle('active', t === 'light');
    if (darkBtn)  darkBtn.classList.toggle('active',  t === 'dark');
  }

  // Load saved theme or OS preference
  var saved     = localStorage.getItem(STORAGE_KEY);
  var preferred = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
  applyTheme(saved || preferred);

  if (lightBtn) lightBtn.addEventListener('click', function() { applyTheme('light'); });
  if (darkBtn)  darkBtn.addEventListener('click',  function() { applyTheme('dark');  });

  // Watch OS preference changes
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function(e) {
      if (!localStorage.getItem(STORAGE_KEY)) {
        applyTheme(e.matches ? 'dark' : 'light');
      }
    });
  }
})();

/* ════════════════════════════════════════════════════
   FLASH DISMISS
════════════════════════════════════════════════════ */
function dismissFlash(btn) {
  var el = btn.closest ? btn.closest('.flash') : btn.parentElement;
  if (!el) return;
  el.style.transition = 'opacity 0.18s, transform 0.18s, margin 0.15s, padding 0.15s, max-height 0.2s';
  el.style.opacity = '0'; el.style.transform = 'translateX(8px)';
  el.style.maxHeight = el.offsetHeight + 'px';
  setTimeout(function() { el.style.maxHeight = '0'; el.style.margin = '0'; el.style.padding = '0'; }, 140);
  setTimeout(function() { el.remove(); }, 320);
}
document.addEventListener('click', function(e) {
  var b = e.target.closest('.flash-close');
  if (b) dismissFlash(b);
});
document.querySelectorAll('.flash').forEach(function(el, i) {
  setTimeout(function() {
    if (el.parentElement) { var b = el.querySelector('.flash-close'); if (b) b.click(); }
  }, 5200 + i * 350);
});

/* ════════════════════════════════════════════════════
   COUNT-UP  [data-count]
════════════════════════════════════════════════════ */
function animateCount(el) {
  var target = parseInt(el.getAttribute('data-count'), 10);
  if (isNaN(target)) return;
  var start = performance.now();
  var dur = Math.min(1100, 250 + target * 10);
  (function tick(now) {
    var t = Math.min((now - start) / dur, 1);
    var eased = 1 - Math.pow(1 - t, 4);
    el.textContent = Math.round(eased * target);
    if (t < 1) requestAnimationFrame(tick);
  })(start);
}

/* ════════════════════════════════════════════════════
   STAGGERED TABLE ROW ENTRANCE
════════════════════════════════════════════════════ */
function animateTableRows() {
  document.querySelectorAll('.tbl tbody tr').forEach(function(tr, i) {
    tr.style.cssText += 'opacity:0;transform:translateY(7px);transition:none;';
    setTimeout(function() {
      tr.style.transition = 'opacity 0.2s ease, transform 0.2s ease';
      tr.style.opacity = '1'; tr.style.transform = 'translateY(0)';
    }, 25 + i * 20);
  });
}

/* ════════════════════════════════════════════════════
   STAGGERED STAT CARD ENTRANCE
════════════════════════════════════════════════════ */
function animateCards() {
  document.querySelectorAll('.stat-card').forEach(function(c, i) {
    c.style.opacity = '0'; c.style.transform = 'translateY(14px) scale(0.98)';
    setTimeout(function() {
      c.style.transition = 'opacity 0.28s cubic-bezier(0.16,1,0.3,1), transform 0.28s cubic-bezier(0.16,1,0.3,1), box-shadow 0.18s ease';
      c.style.opacity = '1'; c.style.transform = 'translateY(0) scale(1)';
    }, 60 + i * 55);
  });
}

/* ════════════════════════════════════════════════════
   BUTTON RIPPLE
════════════════════════════════════════════════════ */
document.addEventListener('mousedown', function(e) {
  var btn = e.target.closest('.btn');
  if (!btn || btn.classList.contains('btn--loading')) return;
  var rect = btn.getBoundingClientRect();
  var size = Math.max(rect.width, rect.height) * 2;
  var r = document.createElement('span');
  r.className = 'btn-ripple';
  r.style.cssText = 'width:' + size + 'px;height:' + size + 'px;left:' +
    (e.clientX - rect.left - size/2) + 'px;top:' + (e.clientY - rect.top - size/2) + 'px;';
  btn.appendChild(r);
  setTimeout(function() { r.remove(); }, 600);
});

/* ════════════════════════════════════════════════════
   CURSOR GLOW ON CARDS
════════════════════════════════════════════════════ */
document.addEventListener('mousemove', function(e) {
  var card = e.target.closest('.stat-card, .card--glow');
  if (!card) return;
  var rect = card.getBoundingClientRect();
  card.style.setProperty('--glow-x', (e.clientX - rect.left) + 'px');
  card.style.setProperty('--glow-y', (e.clientY - rect.top)  + 'px');
});

/* ════════════════════════════════════════════════════
   INPUT LABEL COLOUR ON FOCUS
════════════════════════════════════════════════════ */
function wireInputLabels() {
  document.querySelectorAll('.inp').forEach(function(inp) {
    inp.addEventListener('focus', function() {
      var lbl = this.closest('div') ? this.closest('div').querySelector('label') : null;
      if (!lbl) lbl = this.previousElementSibling;
      if (lbl && lbl.tagName === 'LABEL') {
        lbl.style.color = 'var(--c-primary)';
        lbl.style.transition = 'color 0.15s';
      }
    });
    inp.addEventListener('blur', function() {
      var lbl = this.closest('div') ? this.closest('div').querySelector('label') : null;
      if (!lbl) lbl = this.previousElementSibling;
      if (lbl && lbl.tagName === 'LABEL') lbl.style.color = '';
    });
  });
}

/* ════════════════════════════════════════════════════
   DOM READY
════════════════════════════════════════════════════ */
document.addEventListener('DOMContentLoaded', function() {
  animateTableRows();
  animateCards();
  document.querySelectorAll('[data-count]').forEach(animateCount);
  wireInputLabels();

  document.querySelectorAll('.stat-card').forEach(function(c) { c.classList.add('card--glow'); });
  document.querySelectorAll('.card:not(form .card)').forEach(function(c) { c.classList.add('card--glow'); });
});

/* ════════════════════════════════════════════════════
   MOBILE SIDEBAR TOGGLE
════════════════════════════════════════════════════ */
(function() {
  var btn     = document.getElementById('hamburger-btn');
  var overlay = document.getElementById('sidebar-overlay');
  var sidebar = document.querySelector('.sidebar');
  if (!btn || !sidebar) return;
  function openSidebar() {
    sidebar.classList.add('sidebar-open');
    overlay.classList.add('open');
    document.body.style.overflow = 'hidden';
  }
  function closeSidebar() {
    sidebar.classList.remove('sidebar-open');
    overlay.classList.remove('open');
    document.body.style.overflow = '';
  }
  btn.addEventListener('click', openSidebar);
  overlay.addEventListener('click', closeSidebar);
})();

/* ════════════════════════════════════════════════════
   CONFIRM MODAL  —  window.confirmAction({…})
════════════════════════════════════════════════════ */
window.confirmAction = function(opts) {
  var overlay = document.getElementById('confirm-overlay');
  var iconEl  = document.getElementById('confirm-icon');
  document.getElementById('confirm-title').textContent = opts.title || 'Confirm';
  document.getElementById('confirm-body').textContent  = opts.body  || 'Are you sure?';
  var okBtn = document.getElementById('confirm-ok');
  okBtn.textContent = opts.confirmLabel || 'Confirm';
  okBtn.className   = 'btn ' + (opts.danger ? 'btn-danger' : 'btn-primary');
  if (iconEl) {
    iconEl.className = opts.danger ? 'danger-icon' : 'info-icon';
    iconEl.style.display = '';
    iconEl.innerHTML = opts.danger
      ? '<svg width="22" height="22" fill="none" stroke="#DC2626" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>'
      : '<svg width="22" height="22" fill="none" stroke="var(--c-primary)" viewBox="0 0 24 24" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>';
  }
  overlay.classList.add('open');

  var newOk = okBtn.cloneNode(true);
  okBtn.parentNode.replaceChild(newOk, okBtn);
  newOk.textContent = opts.confirmLabel || 'Confirm';
  newOk.className   = 'btn ' + (opts.danger ? 'btn-danger' : 'btn-primary');
  newOk.addEventListener('click', function() {
    overlay.classList.remove('open');
    if (opts.onConfirm) opts.onConfirm();
  });
  document.getElementById('confirm-cancel').onclick = function() {
    overlay.classList.remove('open');
  };
};
(function() {
  var _confirmOverlay = document.getElementById('confirm-overlay');
  if (_confirmOverlay) {
    _confirmOverlay.addEventListener('click', function(e) {
      if (e.target === this) this.classList.remove('open');
    });
  }
})();
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    var ov = document.getElementById('confirm-overlay');
    if (ov.classList.contains('open')) ov.classList.remove('open');
  }
});

/* ════════════════════════════════════════════════════
   TOAST SYSTEM  —  window.toast(msg, type, ms)
════════════════════════════════════════════════════ */
window.toast = function(msg, type, duration) {
  type = type || 'info';
  duration = (duration === undefined) ? 4500 : duration;
  var container = document.getElementById('toast-container');
  var t = document.createElement('div');
  t.className = 'toast toast-' + type;
  t.setAttribute('role', 'alert');
  var ICONS = {
    success: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/>',
    danger:  '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2m7-2a9 9 0 11-18 0 9 9 0 0118 0z"/>',
    info:    '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>',
    warning: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/>',
  };
  t.innerHTML =
    '<svg class="toast-icon" width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24">' +
    (ICONS[type] || ICONS.info) + '</svg>' +
    '<span style="flex:1;">' + msg + '</span>' +
    '<button class="toast-close" aria-label="Dismiss">&times;</button>';
  t.querySelector('.toast-close').addEventListener('click', function() { _dismissToast(t); });
  container.appendChild(t);

  if (duration > 0) {
    var prog = document.createElement('div');
    prog.style.cssText = 'position:absolute;bottom:0;left:0;height:3px;border-radius:0 0 12px 12px;background:currentColor;opacity:0.35;width:100%;transform-origin:left;transition:transform linear ' + (duration/1000) + 's;';
    t.style.position = 'relative'; t.appendChild(prog);
    requestAnimationFrame(function() {
      requestAnimationFrame(function() { prog.style.transform = 'scaleX(0)'; });
    });
    setTimeout(function() { _dismissToast(t); }, duration);
  }
  return t;
};
function _dismissToast(t) {
  if (!t.parentElement) return;
  t.classList.add('toast-out');
  setTimeout(function() { if (t.parentElement) t.remove(); }, 220);
}

/* ════════════════════════════════════════════════════
   BUTTON LOADING  (prevent double-submit)
════════════════════════════════════════════════════ */
document.addEventListener('submit', function(e) {
  var btn = e.target.querySelector('[type="submit"]');
  if (!btn || btn.dataset.noLoading) return;
  btn.classList.add('btn--loading');
  btn.disabled = true;
  setTimeout(function() { btn.classList.remove('btn--loading'); btn.disabled = false; }, 12000);
}, true);


/* ════════════════════════════════════════════════════
   PAGINATION — jump-to-page
════════════════════════════════════════════════════ */
window._pgJump = function(inp) {
  var page  = parseInt(inp.value, 10);
  var total = parseInt(inp.dataset.pgTotal, 10);
  var base  = inp.dataset.pgBase;
  if (isNaN(page) || page < 1 || page > total) {
    inp.classList.add('pg-shake');
    inp.focus();
    setTimeout(function() { inp.classList.remove('pg-shake'); }, 420);
    return;
  }
  // Fade out then navigate
  document.body.style.transition = 'opacity 0.14s ease';
  document.body.style.opacity = '0';
  setTimeout(function() { window.location.href = base + 'page=' + page; }, 140);
};

/* ════════════════════════════════════════════════════
   PAGINATION — delegated handlers for .pg-jump-inp / .pg-go-btn
════════════════════════════════════════════════════ */
document.addEventListener('keydown', function (e) {
  if (e.key === 'Enter' && e.target.classList.contains('pg-jump-inp')) {
    e.preventDefault();
    window._pgJump(e.target);
  }
});
document.addEventListener('click', function (e) {
  var btn = e.target.closest('.pg-go-btn');
  if (btn) {
    var inp = btn.previousElementSibling;
    if (inp && inp.classList.contains('pg-jump-inp')) window._pgJump(inp);
  }
});

/* ════════════════════════════════════════════════════
   JS-CONFIRM-FORM  —  forms decorated with class="js-confirm-form"
   use the confirmAction modal instead of browser confirm()
════════════════════════════════════════════════════ */
document.addEventListener('submit', function(e) {
  var form = e.target;
  if (!form.classList.contains('js-confirm-form')) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  window.confirmAction({
    title:        form.dataset.confirmTitle || 'Are you sure?',
    body:         form.dataset.confirmBody  || 'This action cannot be undone.',
    confirmLabel: form.dataset.confirmLabel || 'Confirm',
    danger: true,
    onConfirm: function() {
      // Re-submit without triggering this handler again
      form.classList.remove('js-confirm-form');
      form.submit();
    }
  });
}, true);

/* ════════════════════════════════════════════════════
   INSTANT LIST SEARCH — inputs decorated with
   data-search-rows="<css selector>" (from render_list_search()
   in _macros.html) filter matching rows client-side as you type,
   no page reload. One shared listener powers every admin list's
   search box identically — Designations, Plant Locations, Form
   Fields, Users, Plant/Cluster Mappings, ... — rather than each
   page wiring its own filter logic.
════════════════════════════════════════════════════ */
document.addEventListener('input', function(e) {
  var el = e.target;
  if (!el.matches || !el.matches('[data-search-rows]')) return;
  var q = el.value.trim().toLowerCase();
  var rows = document.querySelectorAll(el.getAttribute('data-search-rows'));
  rows.forEach(function(row) {
    var match = !q || row.textContent.toLowerCase().indexOf(q) !== -1;
    row.style.display = match ? '' : 'none';
  });
  var countId = el.getAttribute('data-search-count');
  if (!countId) return;
  var countEl = document.getElementById(countId);
  if (!countEl) return;
  if (!q) { countEl.textContent = ''; return; }
  // offsetParent === null means hidden by an ancestor (e.g. an inactive
  // tab panel) — only count rows actually visible in the current tab.
  var matched = 0;
  rows.forEach(function(row) {
    if (row.style.display !== 'none' && row.offsetParent !== null) matched++;
  });
  countEl.textContent = matched === 0 ? 'No matches' : matched + ' match' + (matched === 1 ? '' : 'es');
});

/* ════════════════════════════════════════════════════
   PAGE FADE TRANSITION
════════════════════════════════════════════════════ */
document.addEventListener('click', function(e) {
  var link = e.target.closest('a[href]');
  if (!link) return;
  var href = link.getAttribute('href');
  if (!href || href.startsWith('#') || href.startsWith('javascript') || e.ctrlKey || e.metaKey || link.target === '_blank') return;
  e.preventDefault();
  document.body.style.transition = 'opacity 0.16s ease';
  document.body.style.opacity = '0';
  setTimeout(function() { window.location = href; }, 160);
});
window.addEventListener('pageshow', function() {
  document.body.style.transition = '';
  document.body.style.opacity = '1';
});
