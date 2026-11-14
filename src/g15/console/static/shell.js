// Shared industrial rail. Pin state is a local display preference only.
const navigationOrder = ['health', '/simulation', '/laboratory', '/diagnosis', '/workbench', 'alarms', 'maintenance', 'reports', '/operations', '/screen', 'notifications'];
function navigationRank(item) {
  const target = new URL(item.getAttribute('href') || '/', location.href);
  const key = item.dataset.view || (item.id === 'nav-notifications' ? 'notifications' : target.pathname === '/' ? target.searchParams.get('view') || 'health' : target.pathname);
  const rank = navigationOrder.indexOf(key);
  return rank < 0 ? navigationOrder.length : rank;
}
function arrangeNavigation(container, selector, before = null) {
  const items = [...container.querySelectorAll(selector)].sort((a, b) => navigationRank(a) - navigationRank(b));
  for (const item of items) container.insertBefore(item, before);
}
const fleetLinks = document.querySelector('.fleet-links');
if (fleetLinks && !fleetLinks.querySelector('[href="/workbench"]')) {
  const workbenchLink = document.createElement('a');
  workbenchLink.href = '/workbench';
  workbenchLink.textContent = '分析工作台';
  fleetLinks.insertBefore(workbenchLink, fleetLinks.querySelector('button'));
}
if (fleetLinks && !fleetLinks.querySelector('[href="/laboratory"]')) {
  const laboratoryLink = document.createElement('a');
  laboratoryLink.href = '/laboratory';
  laboratoryLink.textContent = '波形与实验台';
  fleetLinks.insertBefore(laboratoryLink, fleetLinks.querySelector('button'));
}
if (fleetLinks) arrangeNavigation(fleetLinks, 'a', fleetLinks.querySelector('button'));
const rail = document.querySelector('.sidebar');
if (rail) {
  const oldBrand = rail.querySelector('.brand');
  const brand = document.createElement('button');
  brand.type = 'button';
  brand.className = 'brand rail-brand';
  brand.innerHTML = '<span class="rail-logo"><img src="/assets/g15-logo.png" alt="G15 预测性维护平台" width="2048" height="683"></span><span class="rail-pin" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m8 3 8 0-1 7 3 4H6l3-4zM12 14v7"/></svg></span>';
  oldBrand?.replaceWith(brand);
  const syncMotionVisibility = () => { rail.dataset.motionPaused = String(document.hidden); };
  document.addEventListener('visibilitychange', syncMotionVisibility);
  syncMotionVisibility();
  const nav = rail.querySelector('.main-nav');
  nav.id ||= 'rail-navigation';
  brand.setAttribute('aria-controls', nav.id);
  if (!nav.querySelector('[href="/simulation"]')) {
    const item = document.createElement('a');
    item.className = 'nav-item';
    item.href = '/simulation';
    item.innerHTML = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 5h5v5H3zm13 9h5v5h-5zM8 7h5v9h3M3 19l3-6 3 6m5-14h7m-3-3v6"/></svg><span>仿真数据流</span>';
    nav.append(item);
  }
  if (!nav.querySelector('[href="/laboratory"]')) {
    const item = document.createElement('a');
    item.className = 'nav-item';
    item.href = '/laboratory';
    item.innerHTML = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 4h18v16H3zM5 12h3l2-5 4 10 2-5h3"/></svg><span>波形与实验台</span>';
    nav.querySelector('[href="/simulation"]')?.after(item);
  }
  if (!nav.querySelector('[href="/workbench"]')) {
    const item = document.createElement('a');
    item.className = 'nav-item';
    item.href = '/workbench';
    item.innerHTML = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 4h18v16H3zM7 16v-4m5 4V8m5 8v-6"/></svg><span>分析工作台</span>';
    nav.querySelector('[href="/laboratory"]')?.after(item);
  }
  arrangeNavigation(nav, '.nav-item');
  for (const item of nav.querySelectorAll('.nav-item')) {
    const label = item.textContent.trim();
    item.setAttribute('aria-label', label);
    item.title = label;
    if (item.getAttribute('href') === location.pathname && location.pathname !== '/') item.setAttribute('aria-current', 'page');
  }
  rail.querySelector('.sidebar-footer')?.remove();
  let pinned = false;
  let hover = false;
  let keyboard = false;
  let dismissed = false;
  try { pinned = localStorage.getItem('g15.rail.pinned') === 'true'; } catch { /* Optional persistence. */ }
  function render() {
    const open = pinned || (!dismissed && (hover || keyboard));
    rail.dataset.expanded = String(open);
    rail.dataset.pinned = String(pinned);
    brand.setAttribute('aria-pressed', String(pinned));
    brand.setAttribute('aria-expanded', String(open));
    brand.setAttribute('aria-label', pinned ? '取消固定侧边栏' : '固定侧边栏');
  }
  brand.addEventListener('click', () => {
    pinned = !pinned;
    dismissed = !pinned;
    try { localStorage.setItem('g15.rail.pinned', String(pinned)); } catch { /* Keep session state. */ }
    render();
  });
  rail.addEventListener('pointerenter', (event) => { if (event.pointerType !== 'touch') { hover = true; dismissed = false; render(); } });
  rail.addEventListener('pointerleave', () => { hover = false; dismissed = false; render(); });
  rail.addEventListener('focusin', (event) => { keyboard = event.target.matches(':focus-visible'); if (keyboard) dismissed = false; render(); });
  rail.addEventListener('focusout', (event) => { if (!rail.contains(event.relatedTarget)) { keyboard = false; render(); } });
  rail.addEventListener('keydown', (event) => { if (event.key === 'Escape') { brand.focus(); hover = false; keyboard = false; dismissed = true; render(); } });
  render();
}
