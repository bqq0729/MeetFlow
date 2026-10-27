(() => {
  let active = null;
  let closeTimer;
  const triggers = [...document.querySelectorAll('.tip')];
  function close() {
    if (!active) return;
    active.querySelector('.tip-box').classList.remove('is-open');
    active.setAttribute('aria-expanded', 'false');
    active = null;
  }
  function place() {
    if (!active) return;
    const panel = active.querySelector('.tip-box');
    const r = active.getBoundingClientRect();
    const viewportHeight = window.innerHeight;
    const viewportWidth = window.innerWidth;
    const main = active.closest('main');
    const bounds = main ? main.getBoundingClientRect() : {top:0,bottom:viewportHeight};
    if (r.bottom < Math.max(0,bounds.top) || r.top > Math.min(viewportHeight,bounds.bottom)) return close();
    panel.style.maxHeight = `${Math.max(80, viewportHeight - 32)}px`;
    const width = panel.getBoundingClientRect().width;
    const height = panel.getBoundingClientRect().height;
    let top = r.bottom + 10;
    if (top + height > viewportHeight - 16 && r.top > viewportHeight - r.bottom) top = r.top - height - 10;
    top = Math.max(16, Math.min(top, viewportHeight - height - 16));
    const left = Math.max(16, Math.min(r.left + r.width/2 - width/2, viewportWidth - width - 16));
    panel.style.setProperty('--help-left', `${left}px`);
    panel.style.setProperty('--help-top', `${top}px`);
  }
  function open(trigger) {
    clearTimeout(closeTimer);
    if (active !== trigger) close();
    active = trigger;
    active.setAttribute('aria-expanded', 'true');
    const panel = active.querySelector('.tip-box');
    panel.classList.add('is-open');
    place();
  }
  triggers.forEach((trigger, i) => {
    const panel = trigger.querySelector('.tip-box');
    if (!panel) return;
    panel.id = `option-help-${i}`;
    panel.setAttribute('role', 'tooltip');
    panel.tabIndex = 0;
    trigger.setAttribute('role', 'button');
    trigger.setAttribute('aria-label', `${trigger.parentElement.firstChild.textContent.trim()} 도움말`);
    trigger.setAttribute('aria-controls', panel.id);
    trigger.setAttribute('aria-expanded', 'false');
    trigger.addEventListener('pointerenter', () => open(trigger));
    trigger.addEventListener('focusin', () => open(trigger));
    trigger.addEventListener('click', () => open(trigger));
    trigger.addEventListener('keydown', e => {
      if ((e.key === 'Enter' || e.key === ' ') && e.target === trigger) { e.preventDefault(); open(trigger); }
    });
    const schedule = () => {
      clearTimeout(closeTimer);
      closeTimer = setTimeout(() => {
        if (active === trigger && !trigger.matches(':hover') && !trigger.contains(document.activeElement)) close();
      }, 120);
    };
    trigger.addEventListener('pointerleave', schedule);
    trigger.addEventListener('focusout', schedule);
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  document.addEventListener('pointerdown', e => { if (active && !active.contains(e.target)) close(); });
  document.addEventListener('scroll', e => {
    if (active && e.target !== active.querySelector('.tip-box')) place();
  }, true);
  window.addEventListener('resize', place);
  document.querySelectorAll('[data-page]').forEach(button => button.addEventListener('click', close));
})();
