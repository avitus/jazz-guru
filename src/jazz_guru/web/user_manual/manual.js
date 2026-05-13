(() => {
  const toc = document.getElementById('toc');
  const tocToggle = document.getElementById('toc-toggle');
  if (tocToggle && toc) {
    tocToggle.addEventListener('click', () => toc.classList.toggle('open'));
    toc.addEventListener('click', (e) => {
      if (e.target.tagName === 'A') toc.classList.remove('open');
    });
  }

  const links = Array.from(document.querySelectorAll('.toc a[href^="#"]'));
  const sections = links
    .map((a) => document.getElementById(a.getAttribute('href').slice(1)))
    .filter(Boolean);

  if (!('IntersectionObserver' in window)) return;

  const byId = new Map(links.map((a) => [a.getAttribute('href').slice(1), a]));
  const visible = new Set();

  const io = new IntersectionObserver(
    (entries) => {
      for (const ent of entries) {
        if (ent.isIntersecting) visible.add(ent.target.id);
        else visible.delete(ent.target.id);
      }
      let active = null;
      for (const sec of sections) {
        if (visible.has(sec.id)) { active = sec.id; break; }
      }
      if (!active) return;
      links.forEach((a) => a.classList.remove('active'));
      const link = byId.get(active);
      if (link) link.classList.add('active');
    },
    { rootMargin: '-72px 0px -60% 0px', threshold: 0 }
  );
  sections.forEach((s) => io.observe(s));

  // Anchor-link hover affordance
  document.querySelectorAll('section.s h2').forEach((h) => {
    const id = h.parentElement.id;
    if (!id) return;
    const a = document.createElement('a');
    a.className = 'anchor';
    a.href = '#' + id;
    a.textContent = '#';
    a.setAttribute('aria-label', 'permalink');
    h.appendChild(a);
  });
})();
