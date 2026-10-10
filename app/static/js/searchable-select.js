/* Searchable dropdowns — every <select> in the app becomes "type to filter".
 *
 * The real <select> stays in the page (invisible, still the source of truth), so forms submit exactly as before,
 * `required` still validates, and all existing code that reads/sets select.value, rebuilds its <option>s, toggles
 * disabled / display, or listens for "change" keeps working untouched. This script only puts a button + a search
 * panel in front of it, and keeps the button in sync with whatever happens to the real select.
 *
 * Opt out per dropdown with data-no-search. <select multiple> and list boxes (size > 1) are left alone.
 * Self-hosted on purpose (see CLAUDE.md "no external CDN" policy).
 */
(function () {
  'use strict';

  var selDesc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
  var idxDesc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'selectedIndex');
  var openInstance = null;     // only one panel open at a time
  var uid = 0;

  function esc(t) { var d = document.createElement('div'); d.textContent = t == null ? '' : String(t); return d.innerHTML; }
  function norm(t) { return String(t || '').toLowerCase().replace(/\s+/g, ' ').trim(); }

  function Enhanced(select) {
    var self = this;
    this.select = select;
    this.id = 'ss' + (++uid);
    this.items = [];           // currently rendered rows: {value, label, el}
    this.active = -1;

    var wrap = document.createElement('div');
    wrap.className = 'ss-wrap' + (select.classList.contains('th-filter-select') ? ' ss-inline' : '');
    this.wrap = wrap;

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = (select.className || '') + ' ss-trigger';
    btn.setAttribute('aria-haspopup', 'listbox');
    btn.setAttribute('aria-expanded', 'false');
    if (select.getAttribute('aria-label')) btn.setAttribute('aria-label', select.getAttribute('aria-label'));
    // Keep the select's own inline look (font size etc.) but never its display/margins — those belong to the wrapper.
    if (select.getAttribute('style')) btn.style.cssText = select.getAttribute('style');
    btn.style.display = ''; btn.style.margin = '';
    wrap.style.marginBottom = select.style.marginBottom || '';
    this.btn = btn;

    var label = document.createElement('span');
    label.className = 'ss-label';
    btn.appendChild(label);
    this.labelEl = label;

    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(btn);
    wrap.appendChild(select);
    select.classList.add('ss-native');
    select.setAttribute('tabindex', '-1');
    select.setAttribute('data-ss', '1');

    // Programmatic changes (select.value = x / selectedIndex = n) fire no event — mirror them onto the button.
    Object.defineProperty(select, 'value', {
      configurable: true,
      get: function () { return selDesc.get.call(select); },
      set: function (v) { selDesc.set.call(select, v); self.refresh(); }
    });
    Object.defineProperty(select, 'selectedIndex', {
      configurable: true,
      get: function () { return idxDesc.get.call(select); },
      set: function (v) { idxDesc.set.call(select, v); self.refresh(); }
    });

    // Options rebuilt (innerHTML / appendChild), disabled toggled, hidden/display toggled, validation classes added.
    this.mo = new MutationObserver(function () { self.refresh(); });
    this.mo.observe(select, { childList: true, subtree: true, attributes: true,
                              attributeFilter: ['disabled', 'style', 'hidden', 'class', 'selected', 'label'] });
    select.addEventListener('change', function () { self.refresh(); });
    select.addEventListener('focus', function () { btn.focus(); });          // <label for>, validation focus
    select.addEventListener('invalid', function () { btn.classList.add('ss-invalid'); });

    btn.addEventListener('click', function () { self.isOpen() ? self.close(true) : self.open(''); });
    btn.addEventListener('keydown', function (e) {
      if (self.isOpen()) return;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Enter' || e.key === ' ') {
        e.preventDefault(); self.open('');
      } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault(); self.open(e.key);                                // start typing straight away
      }
    });

    // A form reset puts the select back to its defaults without firing change.
    if (select.form) select.form.addEventListener('reset', function () { setTimeout(function () { self.refresh(); }, 0); });

    this.refresh();
  }

  Enhanced.prototype.isOpen = function () { return !!this.panel; };

  Enhanced.prototype.refresh = function () {
    var s = this.select, btn = this.btn;
    var opt = s.options[selIndex(s)];
    var text = opt ? opt.textContent.replace(/\s+/g, ' ').trim() : '';
    this.labelEl.textContent = text || ' ';
    btn.classList.toggle('ss-placeholder', !opt || opt.value === '');
    btn.disabled = s.disabled;
    var hidden = s.hidden || s.style.display === 'none';
    this.wrap.style.display = hidden ? 'none' : '';
    ['is-invalid', 'is-valid', 'error', 'has-error'].forEach(function (c) { btn.classList.toggle(c, s.classList.contains(c)); });
    if (this.isOpen()) this.render(this.search.value);
  };

  function selIndex(s) { return idxDesc.get.call(s); }

  Enhanced.prototype.open = function (initialText) {
    if (this.select.disabled) return;
    if (openInstance && openInstance !== this) openInstance.close(false);
    openInstance = this;
    var self = this;

    var panel = document.createElement('div');
    panel.className = 'ss-panel';
    panel.setAttribute('role', 'presentation');
    var search = document.createElement('input');
    search.type = 'text'; search.className = 'ss-search'; search.autocomplete = 'off';
    search.setAttribute('placeholder', 'Type to search…');
    search.setAttribute('aria-label', 'Search options');
    var list = document.createElement('ul');
    list.className = 'ss-list'; list.setAttribute('role', 'listbox'); list.id = this.id + '-list';
    panel.appendChild(search); panel.appendChild(list);
    document.body.appendChild(panel);
    this.panel = panel; this.search = search; this.list = list;
    this.btn.setAttribute('aria-expanded', 'true');
    this.btn.setAttribute('aria-controls', list.id);

    search.value = initialText || '';
    this.render(search.value, true);
    this.position();

    search.addEventListener('input', function () { self.render(search.value); });
    search.addEventListener('keydown', function (e) { self.onKey(e); });
    list.addEventListener('mousedown', function (e) { e.preventDefault(); });            // keep focus in the search box
    list.addEventListener('click', function (e) {
      var li = e.target.closest('li[data-i]');
      if (li) self.choose(self.items[+li.getAttribute('data-i')]);
    });
    list.addEventListener('mousemove', function (e) {
      var li = e.target.closest('li[data-i]');
      if (li) self.setActive(+li.getAttribute('data-i'), false);
    });

    this._onDoc = function (e) { if (!panel.contains(e.target) && !self.wrap.contains(e.target)) self.close(false); };
    this._onMove = function (e) { if (e && e.target && panel.contains(e.target)) return; self.position(); };
    document.addEventListener('mousedown', this._onDoc, true);
    document.addEventListener('touchstart', this._onDoc, true);
    window.addEventListener('scroll', this._onMove, true);
    window.addEventListener('resize', this._onMove);
    search.focus();
    if (initialText) search.setSelectionRange(search.value.length, search.value.length);
  };

  Enhanced.prototype.close = function (refocus) {
    if (!this.panel) return;
    document.removeEventListener('mousedown', this._onDoc, true);
    document.removeEventListener('touchstart', this._onDoc, true);
    window.removeEventListener('scroll', this._onMove, true);
    window.removeEventListener('resize', this._onMove);
    this.panel.parentNode && this.panel.parentNode.removeChild(this.panel);
    this.panel = this.search = this.list = null;
    this.btn.setAttribute('aria-expanded', 'false');
    if (openInstance === this) openInstance = null;
    if (refocus) this.btn.focus();
  };

  Enhanced.prototype.position = function () {
    if (!this.panel) return;
    var r = this.btn.getBoundingClientRect();
    var p = this.panel, vh = window.innerHeight, vw = window.innerWidth;
    var width = Math.max(r.width, 220);
    var left = Math.min(Math.max(8, r.left), Math.max(8, vw - width - 8));
    p.style.minWidth = width + 'px'; p.style.maxWidth = Math.min(vw - 16, 460) + 'px'; p.style.left = left + 'px';
    var below = vh - r.bottom - 12, above = r.top - 12;
    var flip = below < 220 && above > below;
    var room = Math.max(140, Math.min(flip ? above : below, 340));
    this.list.style.maxHeight = (room - 56) + 'px';
    if (flip) { p.style.top = ''; p.style.bottom = (vh - r.top + 4) + 'px'; }
    else { p.style.bottom = ''; p.style.top = (r.bottom + 4) + 'px'; }
  };

  Enhanced.prototype.render = function (query, scrollToSelected) {
    var q = norm(query), s = this.select, list = this.list;
    var curOpt = s.options[selIndex(s)];
    list.innerHTML = '';
    this.items = [];
    var shown = 0, selectedAt = -1, self = this;

    function addOption(o, groupLabel) {
      var label = o.textContent.replace(/\s+/g, ' ').trim();
      if (q && norm(label).indexOf(q) === -1 && !(groupLabel && norm(groupLabel).indexOf(q) !== -1)) return false;
      var i = self.items.length;
      var li = document.createElement('li');
      li.setAttribute('role', 'option'); li.setAttribute('data-i', i);
      li.className = 'ss-opt' + (o.disabled ? ' ss-disabled' : '') + (o.value === '' ? ' ss-empty' : '');
      if (o === curOpt) { li.classList.add('ss-selected'); li.setAttribute('aria-selected', 'true'); selectedAt = i; }
      var t = label || ' ';
      if (q && label) {
        var at = norm(label).indexOf(q);
        li.innerHTML = at >= 0 && norm(label).length === label.length
          ? esc(label.slice(0, at)) + '<mark>' + esc(label.slice(at, at + q.length)) + '</mark>' + esc(label.slice(at + q.length))
          : esc(t);
      } else { li.textContent = t; }
      list.appendChild(li);
      self.items.push({ value: o.value, label: label, el: li, disabled: o.disabled });
      shown++;
      return true;
    }

    Array.prototype.forEach.call(s.children, function (child) {
      if (child.tagName === 'OPTGROUP') {
        var before = list.children.length, head = null;
        var gl = child.label || '';
        head = document.createElement('li'); head.className = 'ss-group'; head.textContent = gl; head.setAttribute('role', 'presentation');
        list.appendChild(head);
        var any = false;
        Array.prototype.forEach.call(child.children, function (o) {
          if (o.tagName === 'OPTION' && addOption(o, gl)) any = true;
        });
        if (!any) list.removeChild(head);
      } else if (child.tagName === 'OPTION') {
        addOption(child, null);
      }
    });

    if (!shown) {
      var none = document.createElement('li');
      none.className = 'ss-none'; none.textContent = 'No matches'; none.setAttribute('role', 'presentation');
      list.appendChild(none);
    }
    // Active row: the current choice when opening, otherwise the first real match.
    var first = -1;
    for (var k = 0; k < this.items.length; k++) { if (!this.items[k].disabled && (q || this.items[k].value !== '' || this.items.length === 1)) { first = k; break; } }
    if (first === -1) first = this.items.findIndex(function (it) { return !it.disabled; });
    var useSel = scrollToSelected && selectedAt >= 0 && this.items[selectedAt].value !== '';
    this.setActive(useSel ? selectedAt : first, true);
    this.position();
  };

  Enhanced.prototype.setActive = function (i, scroll) {
    if (this.active >= 0 && this.items[this.active]) this.items[this.active].el.classList.remove('ss-active');
    this.active = i;
    var it = this.items[i];
    if (!it) { this.search && this.search.removeAttribute('aria-activedescendant'); return; }
    it.el.classList.add('ss-active');
    if (scroll) {
      var el = it.el, l = this.list;
      if (el.offsetTop < l.scrollTop) l.scrollTop = el.offsetTop;
      else if (el.offsetTop + el.offsetHeight > l.scrollTop + l.clientHeight) l.scrollTop = el.offsetTop + el.offsetHeight - l.clientHeight;
    }
  };

  Enhanced.prototype.move = function (dir) {
    var n = this.items.length, i = this.active;
    if (!n) return;
    for (var step = 0; step < n; step++) {
      i = (i + dir + n) % n;
      if (!this.items[i].disabled) { this.setActive(i, true); return; }
    }
  };

  Enhanced.prototype.onKey = function (e) {
    if (e.key === 'ArrowDown') { e.preventDefault(); this.move(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); this.move(-1); }
    else if (e.key === 'Enter') { e.preventDefault(); if (this.items[this.active]) this.choose(this.items[this.active]); }
    else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); this.close(true); }
    else if (e.key === 'Tab') { this.close(false); }
    else if (e.key === 'Home' && !this.search.value) { e.preventDefault(); this.setActive(0, true); }
    else if (e.key === 'End' && !this.search.value) { e.preventDefault(); this.setActive(this.items.length - 1, true); }
  };

  Enhanced.prototype.choose = function (item) {
    if (!item || item.disabled) return;
    var s = this.select, changed = selDesc.get.call(s) !== item.value;
    selDesc.set.call(s, item.value);
    this.btn.classList.remove('ss-invalid');
    this.close(true);
    this.refresh();
    if (changed) {
      s.dispatchEvent(new Event('input', { bubbles: true }));
      s.dispatchEvent(new Event('change', { bubbles: true }));
    }
  };

  function enhance(select) {
    if (select.getAttribute('data-ss') || select.hasAttribute('data-no-search')) return;
    if (select.multiple || (select.size && select.size > 1)) return;
    if (!select.parentNode) return;
    new Enhanced(select);
  }

  function scan(root) {
    if (!root) return;
    if (root.tagName === 'SELECT') { enhance(root); return; }
    if (root.querySelectorAll) Array.prototype.forEach.call(root.querySelectorAll('select'), enhance);
  }

  function start() {
    scan(document);
    // Dropdowns created later (modals, JS-built forms) get the same treatment.
    new MutationObserver(function (muts) {
      muts.forEach(function (m) { Array.prototype.forEach.call(m.addedNodes, function (n) { if (n.nodeType === 1) scan(n); }); });
    }).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
