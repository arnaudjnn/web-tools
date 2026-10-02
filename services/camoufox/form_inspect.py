"""Read-only form inspection: what an agent needs to build `/form-submit` fields.

`/form-inspect` navigates with the same isolated form browser and admission
as `/form-submit`, never fills or clicks anything, and aborts EVERY mutating
request (any method but GET/HEAD/OPTIONS, any origin — stricter than
`inspect_only`, which blocks same-origin only: an inspection has no business
sending a beacon either). It returns, per visible form:

- action / method, and whether the action is same-origin (a `/form-submit`
  POST budget only counts same-origin `submission_urls`);
- fields as {selector, name, type, action, label, required, placeholder,
  autocomplete, pattern, options} — `selector` is stable (`#id`, else
  `tag[name="..."]`, scoped by the form when ambiguous) and `action` is the
  `/form-submit` field action. Radios are one entry per group whose
  `options[].selector` is what to check. NO VALUES, ever: not typed, default,
  hidden or selected ones — names of hidden inputs only;
- submit candidates (in-form submit controls first);
- honeypot candidates (text-like inputs hidden, offscreen, aria-hidden or
  tabindex=-1, or with a trap-like name) — never put these in `fields`;
- a `suggested` `/form-submit` skeleton (required fields, best submit,
  dismiss, captcha_field) — the caller adds the values.

Page-wide: CAPTCHA providers (reCAPTCHA v2/invisible/v3/enterprise, hCaptcha,
Turnstile — presence of a sitekey, never the key), cookie-banner dismiss
candidates and wizard hints. Child frames are inspected too; a form found in
one is marked `frame_url` — `/form-submit` drives the main frame only.

Nothing here can submit: there is no click, no typing, and the route guard
would abort the POST if the page tried on its own.
"""
import logging
import time
from functools import partial
from typing import Optional
from urllib.parse import urlsplit

log = logging.getLogger("camoufox.forms")

CAPTCHA_FRAME_HOSTS = ("google.com", "recaptcha.net", "gstatic.com", "hcaptcha.com",
                       "challenges.cloudflare.com")

# Runs in the page (isolated world is enough: DOM only, no page globals).
INSPECT_JS = r"""
() => {
  const clean = (s, n = 200) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n);
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/[^\w-]/g, '\\$&');
  const q = (s) => String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const unique = (sel) => { try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
  const TEXTLIKE = /^(text|email|tel|url|number|search|password|date|datetime-local|month|week|time|textarea)$/;
  const HONEY_NAME = /(honey|hpot|^hp_|_hp$|gotcha|trap|leave.?(this|it)?.?(blank|empty)|do.?not.?fill|email_?(last|confirm2)|^website$|^url$|^homepage$|^fax$|^nickname$|^b_[a-z0-9]+_[a-z0-9]+$)/i;
  const SUBMIT_TEXT = /\b(submit|send|sign ?up|register|request|apply|subscribe|continue|next|confirm|invia|inviare|registrati|iscriviti|richiedi|continua|avanti|prosegui|conferma|envoyer|absenden|senden|weiter|enviar)\b/i;
  const NEXT_TEXT = /^\s*(next|continue|proceed|avanti|continua|prosegui|weiter|suivant|siguiente)\b/i;

  const pathSelector = (el) => {
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.documentElement) {
      if (node.id && unique('#' + esc(node.id))) { parts.unshift('#' + esc(node.id)); break; }
      const tag = node.tagName.toLowerCase();
      let k = 1;
      for (let s = node.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === node.tagName) k++;
      parts.unshift(tag + ':nth-of-type(' + k + ')');
      node = node.parentElement;
    }
    const sel = parts.join(' > ');
    return unique(sel) ? sel : null;
  };
  const formSelector = (f) => {
    if (!f) return null;
    if (f.id && unique('#' + esc(f.id))) return '#' + esc(f.id);
    const name = f.getAttribute('name');
    if (name && unique('form[name="' + q(name) + '"]')) return 'form[name="' + q(name) + '"]';
    const action = f.getAttribute('action');
    if (action && unique('form[action="' + q(action) + '"]')) return 'form[action="' + q(action) + '"]';
    return pathSelector(f);
  };
  const selectorFor = (el, value) => {
    if (value == null && el.id && unique('#' + esc(el.id))) return '#' + esc(el.id);
    const tag = el.tagName.toLowerCase();
    const name = el.getAttribute('name');
    if (name) {
      let s = tag + '[name="' + q(name) + '"]';
      if (value != null) s += '[value="' + q(value) + '"]';
      if (unique(s)) return s;
      const fs = formSelector(el.form);
      if (fs && unique(fs + ' ' + s)) return fs + ' ' + s;
    }
    if (value != null && el.id && unique('#' + esc(el.id))) return '#' + esc(el.id);
    return pathSelector(el);
  };

  // Why el is not visible, and which element hides it (null = visible).
  const hiddenInfo = (el) => {
    if (!el.isConnected) return { reason: 'detached', node: el };
    const ah = el.closest('[aria-hidden="true"]');
    if (ah) return { reason: 'aria_hidden', node: ah };
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const st = getComputedStyle(n);
      if (st.display === 'none') return { reason: 'display_none', node: n };
      if (st.visibility === 'hidden' || st.visibility === 'collapse') return { reason: 'visibility_hidden', node: n };
      if (parseFloat(st.opacity) === 0) return { reason: 'opacity_0', node: n };
    }
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return { reason: 'zero_size', node: el };
    const W = Math.max(document.documentElement.scrollWidth, innerWidth);
    if (r.right + scrollX <= 0 || r.bottom + scrollY <= 0 || r.left + scrollX >= W) return { reason: 'offscreen', node: el };
    return null;
  };
  const hiddenReason = (el) => { const h = hiddenInfo(el); return h ? h.reason : null; };
  const visible = (el) => hiddenInfo(el) === null;

  // Label text WITHOUT any control content (a wrapped textarea/select would
  // otherwise leak its default value or option texts).
  const textOf = (node) => {
    if (!node) return '';
    const c = node.cloneNode(true);
    c.querySelectorAll('input,select,textarea,option,script,style').forEach((x) => x.remove());
    return clean(c.textContent);
  };
  const labelOf = (el) => {
    const parts = [];
    if (el.labels) for (const l of el.labels) parts.push(textOf(l));
    const by = el.getAttribute('aria-labelledby');
    if (by) for (const id of by.split(/\s+/)) parts.push(textOf(document.getElementById(id)));
    parts.push(clean(el.getAttribute('aria-label')));
    const label = clean(parts.filter(Boolean).join(' '));
    return label || clean(el.getAttribute('title')) || null;
  };
  const controlType = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'select') return el.multiple ? 'select-multiple' : 'select';
    if (tag === 'textarea') return 'textarea';
    return (el.getAttribute('type') || 'text').toLowerCase();
  };
  const isButton = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'button') return true;
    return tag === 'input' && /^(submit|button|reset|image)$/.test(controlType(el));
  };
  const buttonText = (el) => clean(el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('alt'), 80);
  const isSubmit = (el) => {
    const tag = el.tagName.toLowerCase();
    const t = (el.getAttribute('type') || (tag === 'button' ? 'submit' : '')).toLowerCase();
    return t === 'submit' || t === 'image';
  };

  const honeypotOf = (el) => {
    const type = controlType(el);
    if (type === 'hidden' || !(TEXTLIKE.test(type) || type === 'checkbox')) return null;
    const reasons = [];
    const info = hiddenInfo(el);
    const hidden = info ? info.reason : null;
    if (hidden) reasons.push(hidden);
    if (el.getAttribute('tabindex') === '-1') reasons.push('tabindex_-1');
    const name = el.getAttribute('name') || el.id || '';
    const trapName = HONEY_NAME.test(name);
    if (trapName) reasons.push('trap_name');
    if ((el.getAttribute('autocomplete') || '').toLowerCase() === 'off' && reasons.length) reasons.push('autocomplete_off');
    // A hidden native checkbox with a visible label is a styled control, not a trap.
    if (hidden && type === 'checkbox' && el.labels && [...el.labels].some(visible)) return null;
    if (!hidden) return reasons.includes('tabindex_-1') && trapName ? { selector: selectorFor(el), name: el.getAttribute('name'), type, reasons } : null;
    // A container hiding SEVERAL controls is a later wizard step or a closed
    // panel, not a trap — unless the name itself says trap.
    if (!trapName && info.node !== el && /^(display_none|visibility_hidden|aria_hidden)$/.test(hidden)) {
      const siblings = info.node.querySelectorAll('input:not([type="hidden"]),select,textarea').length;
      if (siblings > 1) return null;
    }
    return { selector: selectorFor(el), name: el.getAttribute('name'), type, reasons };
  };

  const describeForm = (formEl, controls) => {
    const fields = [], hiddenNames = [], honeypots = [], submits = [];
    const radios = new Map();
    for (const el of controls) {
      if (!el.tagName || !/^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(el.tagName)) continue;
      if (isButton(el)) {
        if (visible(el) && !el.disabled) submits.push({ selector: selectorFor(el), text: buttonText(el),
          type: isSubmit(el) ? 'submit' : 'button' });
        continue;
      }
      const type = controlType(el);
      const name = el.getAttribute('name');
      if (type === 'hidden') { if (name) hiddenNames.push(name); continue; }
      const trap = honeypotOf(el);
      if (trap) { honeypots.push(trap); continue; }
      const hidden = hiddenReason(el);
      let labelSelector = null;
      if (hidden) {
        // Styled checkbox/radio: the native input is hidden behind its label.
        const lbl = (type === 'checkbox' || type === 'radio') && el.labels ? [...el.labels].find(visible) : null;
        if (!lbl) continue;  // invisible and not a trap: a later step or a closed menu
        labelSelector = lbl.getAttribute('for') && el.id ? 'label[for="' + q(el.id) + '"]' : pathSelector(lbl);
      }
      const label = labelOf(el);
      const required = !!(el.required || el.getAttribute('aria-required') === 'true' || (label && /\*\s*$/.test(label)));
      if (type === 'radio') {
        const key = name || selectorFor(el);
        if (!radios.has(key)) {
          const group = el.closest('fieldset');
          const legend = group && group.querySelector('legend');
          const rg = el.closest('[role="radiogroup"]');
          const entry = { selector: null, name, type: 'radio', action: 'check',
            label: (legend && textOf(legend)) || (rg && (clean(rg.getAttribute('aria-label')) || textOf(document.getElementById(rg.getAttribute('aria-labelledby') || '')))) || null,
            required: false, options: [] };
          radios.set(key, entry);
          fields.push(entry);
        }
        const entry = radios.get(key);
        entry.required = entry.required || required;
        if (entry.options.length < 50) entry.options.push({ value: el.getAttribute('value') || 'on', label,
          selector: selectorFor(el, el.getAttribute('value') || null), ...(labelSelector ? { label_selector: labelSelector } : {}) });
        continue;
      }
      const field = { selector: selectorFor(el), name, type,
        action: type.startsWith('select') ? 'select' : type === 'checkbox' ? 'check' : type === 'file' ? null : 'type',
        label, required };
      const ph = clean(el.getAttribute('placeholder'));
      if (ph) field.placeholder = ph;
      const ac = clean(el.getAttribute('autocomplete'), 60);
      if (ac) field.autocomplete = ac;
      const pattern = el.getAttribute('pattern');
      if (pattern) field.pattern = pattern.slice(0, 200);
      if (el.disabled) field.disabled = true;
      if (el.readOnly) field.readonly = true;
      if (labelSelector) field.label_selector = labelSelector;
      if (type.startsWith('select')) {
        // value + visible text only: which option is SELECTED is a value too.
        const opts = [...el.options];
        field.options = opts.slice(0, 100).map((o) => ({ value: o.value, label: clean(o.textContent, 120),
          ...(o.disabled ? { disabled: true } : {}) }));
        if (opts.length > 100) field.options_truncated = opts.length;
      }
      fields.push(field);
    }
    submits.sort((a, b) => (a.type === 'submit' ? 0 : 1) - (b.type === 'submit' ? 0 : 1));
    const out = { selector: formEl ? formSelector(formEl) : null, formless: !formEl,
      action: formEl ? formEl.action || location.href : null,
      method: formEl ? (formEl.getAttribute('method') || 'get').toLowerCase() : null,
      fields, submit_candidates: submits.slice(0, 10), honeypot_candidates: honeypots,
      hidden_inputs: [...new Set(hiddenNames)].slice(0, 50) };
    return out;
  };

  // Forms, then controls outside any form (SPAs) as one pseudo-form whose
  // submit candidates are visible buttons with submit-like text.
  const forms = [];
  let hiddenForms = 0;
  for (const f of document.querySelectorAll('form')) {
    const d = describeForm(f, [...f.elements]);
    if (d.fields.length || d.honeypot_candidates.length && d.submit_candidates.length) forms.push(d);
    else hiddenForms++;
  }
  const loose = [...document.querySelectorAll('input,select,textarea')].filter((el) => !el.form);
  if (loose.length) {
    const d = describeForm(null, loose);
    if (d.fields.length) {
      for (const b of document.querySelectorAll('button,[role="button"],input[type="submit"],input[type="button"],a')) {
        if (b.form || !visible(b) || !SUBMIT_TEXT.test(buttonText(b))) continue;
        d.submit_candidates.push({ selector: selectorFor(b), text: buttonText(b), type: 'button' });
        if (d.submit_candidates.length >= 10) break;
      }
      forms.push(d);
    }
  }

  // CAPTCHA: script/iframe/resource URLs + widget markup. Presence only.
  const urls = [...document.scripts].map((s) => s.src).filter(Boolean)
    .concat([...document.querySelectorAll('iframe')].map((f) => f.src).filter(Boolean))
    .concat(performance.getEntriesByType('resource').map((e) => e.name));
  const any = (re) => urls.some((u) => re.test(u));
  const captcha = [];
  {
    const variants = new Set();
    const api = urls.filter((u) => /\/recaptcha\/(api|enterprise)\.js/.test(u));
    if (urls.some((u) => /\/recaptcha\/enterprise/.test(u))) variants.add('enterprise');
    for (const u of api) {
      try { const r = new URL(u).searchParams.get('render'); if (r && r !== 'explicit' && r !== 'onload') variants.add('v3'); } catch (e) {}
    }
    const widgets = [...document.querySelectorAll('.g-recaptcha, [data-sitekey]:not(.h-captcha):not(.cf-turnstile)')];
    for (const w of widgets) variants.add((w.getAttribute('data-size') || '').toLowerCase() === 'invisible' ? 'v2-invisible' : 'v2');
    if (urls.some((u) => /\/recaptcha\/(api2|enterprise)\/anchor.*size=invisible/.test(u)) && !variants.has('v3')) variants.add('invisible');
    if (urls.some((u) => /\/recaptcha\/(api2|enterprise)\/anchor.*size=normal/.test(u))) variants.add('v2');
    if (document.querySelector('.grecaptcha-badge') && !variants.has('v3') && !variants.has('v2-invisible')) variants.add('v3-or-invisible');
    if (api.length || variants.size || any(/\/recaptcha\//)) {
      captcha.push({ provider: 'recaptcha', variants: [...variants],
        sitekey_present: api.some((u) => /[?&]render=(?!explicit|onload)[^&]+/.test(u)) || widgets.some((w) => !!w.getAttribute('data-sitekey')),
        token_field: 'g-recaptcha-response',
        token_field_present: !!document.querySelector('[name="g-recaptcha-response"]') });
    }
  }
  if (any(/hcaptcha\.com/) || document.querySelector('.h-captcha')) {
    captcha.push({ provider: 'hcaptcha', variants: [], sitekey_present: !!document.querySelector('.h-captcha[data-sitekey]'),
      token_field: 'h-captcha-response', token_field_present: !!document.querySelector('[name="h-captcha-response"]') });
  }
  if (any(/challenges\.cloudflare\.com\/turnstile/) || document.querySelector('.cf-turnstile')) {
    captcha.push({ provider: 'turnstile', variants: [], sitekey_present: !!document.querySelector('.cf-turnstile[data-sitekey]'),
      token_field: 'cf-turnstile-response', token_field_present: !!document.querySelector('[name="cf-turnstile-response"]') });
  }

  // Cookie banners: known CMP buttons, then accept-like text inside a
  // cookie/consent-looking container. Visible ones only.
  const KNOWN = ['#onetrust-accept-btn-handler', '#didomi-notice-agree-button', '.iubenda-cs-accept-btn',
    '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll', '#CybotCookiebotDialogBodyButtonAccept',
    '[data-cookiefirst-action="accept"]', '#axeptio_btn_acceptAll', '.qc-cmp2-summary-buttons button[mode="primary"]',
    '#truste-consent-button', '.cc-allow', '.cc-dismiss', '.cky-btn-accept', '#cookie_action_close_header',
    '.cmplz-accept', '#wt-cli-accept-all-btn', '.fc-cta-consent'];
  const banners = [];
  const seen = new Set();
  for (const sel of KNOWN) {
    const el = document.querySelector(sel);
    if (el && visible(el)) { banners.push({ selector: sel, text: buttonText(el), source: 'known' }); seen.add(el); }
  }
  const ACCEPT = /^(accept|accept all|accept cookies|allow all|allow cookies|agree|i agree|ok|got it|accetta|accetta tutti|accetto|acconsento|consenti|tout accepter|accepter|alle akzeptieren|akzeptieren|aceptar|aceptar todo)$/i;
  for (const b of document.querySelectorAll('button,a,[role="button"]')) {
    if (banners.length >= 5) break;
    if (seen.has(b) || !ACCEPT.test(buttonText(b).replace(/[.!]$/, '')) || !visible(b)) continue;
    if (!b.closest('[id*="cookie" i],[class*="cookie" i],[id*="consent" i],[class*="consent" i],[id*="gdpr" i],[class*="gdpr" i],[class*="cmp" i],[id*="cmp" i],[aria-label*="cookie" i],[role="dialog"]')) continue;
    const sel = selectorFor(b);
    if (sel) banners.push({ selector: sel, text: buttonText(b), source: 'heuristic' });
  }

  // Wizard hints: step indicators, next buttons, steps pre-rendered hidden.
  const signals = [];
  const body = clean(document.body ? document.body.innerText : '', 20000);
  const stepText = body.match(/\b(step|passo|fase|[ée]tape|schritt|paso)\s*\d+\s*(of|di|de|sur|von|\/)\s*\d+/i);
  if (stepText) signals.push('step_text:' + clean(stepText[0], 40));
  const stepEls = document.querySelectorAll('[data-step],[aria-current="step"],[class*="wizard" i],[class*="stepper" i],[role="progressbar"]').length;
  if (stepEls) signals.push('step_elements:' + stepEls);
  const nextButtons = [];
  for (const b of document.querySelectorAll('button,[role="button"],a,input[type="button"],input[type="submit"]')) {
    if (nextButtons.length >= 5) break;
    if (visible(b) && NEXT_TEXT.test(buttonText(b))) nextButtons.push({ selector: selectorFor(b), text: buttonText(b) });
  }
  if (nextButtons.length) signals.push('next_buttons:' + nextButtons.length);
  let hiddenSteps = 0;
  for (const g of document.querySelectorAll('form fieldset, form section, form [class*="step" i]')) {
    const ctl = [...g.querySelectorAll('input:not([type="hidden"]),select,textarea')];
    if (ctl.length && !visible(g) && !ctl.some(visible)) hiddenSteps++;
  }
  if (hiddenSteps) signals.push('hidden_steps:' + hiddenSteps);
  if (hiddenForms) signals.push('hidden_forms:' + hiddenForms);

  return { forms, captcha, cookie_banners: banners,
    wizard: { likely: !!(stepText || nextButtons.length || hiddenSteps), signals, next_buttons: nextButtons } };
}
"""

WIZARD_HINT = ("Multi-step form: submit step0's fields/submit, then pass step2 + step2_submit "
               "for the second step, gate_text for an interstitial button clicked once, and "
               "completion_markers for an end state that keeps the URL; timeout_ms ~240000.")


def _suggest(url, form, captcha, banners):
    """A /form-submit skeleton for one form; the caller adds `value`s."""
    fields = []
    for field in form["fields"]:
        if not field.get("required") or field.get("disabled") or not field.get("action"):
            continue
        if field["type"] == "radio":
            if field["options"]:
                fields.append({"selector": field["options"][0]["selector"], "action": "check"})
            continue
        fields.append({"selector": field["selector"], "action": field["action"]})
    if not fields:
        fields = [{"selector": f["selector"], "action": f["action"]} for f in form["fields"]
                  if f.get("selector") and f.get("action") and not f.get("disabled")]
    suggested = {"url": url, "fields": fields,
                 "submit": form["submit_candidates"][0]["selector"] if form["submit_candidates"] else None}
    if banners:
        suggested["dismiss"] = [b["selector"] for b in banners[:3]]
    if captcha:
        suggested["captcha_field"] = captcha[0]["token_field"]
    action = form.get("action")
    if action and form.get("method") == "post":
        page, target = urlsplit(url), urlsplit(action)
        if (target.scheme, target.netloc) == (page.scheme, page.netloc):
            if target._replace(query="", fragment="") != page._replace(query="", fragment=""):
                suggested["submission_urls"] = [target._replace(query="", fragment="").geturl()]
    return suggested


def inspect_form(context, *, url, wait_until="domcontentloaded", wait_ms=4000, timeout_ms=60000):
    """Navigate (no input, no clicks, every mutating request aborted) and describe the forms."""
    diagnostics = {"inspection_only": True, "blocked_mutations": 0, "navigation_status": None,
                   "frames_inspected": 0, "frame_errors": 0}

    def guard(route):
        if route.request.method not in ("GET", "HEAD", "OPTIONS"):
            diagnostics["blocked_mutations"] += 1
            route.abort("blockedbyclient")
            return
        route.continue_()

    deadline = time.monotonic() + timeout_ms / 1000

    def remaining():
        value = int((deadline - time.monotonic()) * 1000)
        if value <= 0:
            raise TimeoutError("Inspection deadline exceeded")
        return value

    context.route("**/*", guard)
    page = context.new_page()
    navigation = page.goto(url, wait_until=wait_until, timeout=remaining())
    if navigation is not None and isinstance(navigation.status, int):
        diagnostics["navigation_status"] = navigation.status
    if wait_ms:
        page.wait_for_timeout(min(wait_ms, remaining()))
    data = page.evaluate(INSPECT_JS)
    diagnostics["frames_inspected"] = 1
    forms, captcha, banners = data["forms"], data["captcha"], data["cookie_banners"]
    for frame in page.frames:
        if frame is page.main_frame:
            continue
        host = urlsplit(frame.url or "").hostname or ""
        if not host or any(host == h or host.endswith("." + h) for h in CAPTCHA_FRAME_HOSTS):
            continue
        try:
            sub = frame.evaluate(INSPECT_JS)
        except Exception:
            diagnostics["frame_errors"] += 1
            continue
        diagnostics["frames_inspected"] += 1
        for form in sub["forms"]:
            form["frame_url"] = urlsplit(frame.url)._replace(query="", fragment="").geturl()
            forms.append(form)
        known = {c["provider"] for c in captcha}
        captcha.extend(c for c in sub["captcha"] if c["provider"] not in known)
        for banner in sub["cookie_banners"]:
            banner["frame_url"] = urlsplit(frame.url)._replace(query="", fragment="").geturl()
            banners.append(banner)
    main_banners = [b for b in banners if "frame_url" not in b]
    for index, form in enumerate(forms):
        form["index"] = index
        if "frame_url" not in form:
            form["suggested"] = _suggest(page.url, form, captcha, main_banners)
    wizard = data["wizard"]
    if wizard["likely"]:
        wizard["hint"] = WIZARD_HINT
    return {"ok": True, "error": None, "form_submissions": 0, "status": diagnostics["navigation_status"] or 0,
            "url": page.url, "forms": forms, "captcha": captcha, "cookie_banners": banners,
            "wizard": wizard, "diagnostics": diagnostics}


def register(app, *, worker, form_browser, shared_session, camoufox=None):
    """Mount POST /form-inspect on the app, sharing the form worker's admission."""
    import secrets

    from fastapi import HTTPException
    from pydantic import BaseModel, Field

    from form_flow import FormLive, validate_form
    from form_worker import run_isolated_form

    class FormInspectRequest(BaseModel):
        url: str
        wait_until: str = Field("domcontentloaded")
        wait_ms: int = Field(4000, ge=0, le=60_000)
        timeout_ms: int = Field(60_000, ge=1000, le=180_000)
        fresh_ip: bool = Field(True, description="new context + new exit IP")
        exit_session: Optional[str] = Field(None, description="pin the exit (same token = same IP)")
        headed: bool = Field(False, description="headed browser under xvfb")
        profile: Optional[str] = Field(None, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
                                       description="named persistent profile (a visit warms it)")

    @app.post("/form-inspect")
    async def form_inspect(req: FormInspectRequest):
        deadline = time.monotonic() + req.timeout_ms / 1000
        try:
            validate_form(req.url, None, None)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        session = req.exit_session or (secrets.token_hex(6) if req.fresh_ip else shared_session)
        # One per job, shared by the worker and the runner: one form-run line.
        live = FormLive(profile=req.profile, headed=req.headed, camoufox=camoufox)
        try:
            data = await worker.run(partial(run_isolated_form,
                partial(form_browser, session, False, req.headed, req.profile),
                deadline=deadline, runner=inspect_form, live=live, url=req.url,
                wait_until=req.wait_until, wait_ms=req.wait_ms), url=req.url, deadline=deadline,
                live=live)
        except Exception as e:
            # Read-only: nothing was submitted, so every failure is replayable.
            log.warning("form-inspect failed (%s)", type(e).__name__)
            raise HTTPException(status_code=503, detail={
                "message": "Inspection failed (%s); read-only, safe to retry" % type(e).__name__,
                "retryable": True})
        if data.get("error"):
            raise HTTPException(status_code=503, detail={
                "message": "Inspection never ran (%s); read-only, safe to retry" % data["error"],
                "retryable": True})
        data["exit_session"] = req.exit_session or ""
        return data

    return form_inspect
