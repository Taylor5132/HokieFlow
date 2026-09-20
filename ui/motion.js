// Locally bundled GSAP: ordinary scrolling, no pinning or scroll hijacking.
export function mountScrollMotion(root) {
  const {gsap, ScrollTrigger} = window;
  if (!root || !gsap || !ScrollTrigger) return () => {};
  gsap.registerPlugin(ScrollTrigger);
  const preference = matchMedia('(prefers-reduced-motion: reduce)');
  const seen = new WeakSet();
  const active = new Map();
  const headers = new WeakSet();
  const selector = '.home-card, #upcoming, .dining-place, .about-feature, .team-credits, .settings-account, .answer, .copyright';
  let frame = 0;
  function clear(element, tween) {
    tween.scrollTrigger?.kill();
    tween.kill();
    gsap.set(element, {clearProps: 'opacity,transform,--rule-progress'});
    active.delete(element);
  }
  function sync() {
    frame = 0;
    for (const [element, tween] of active) {
      if (!element.isConnected || preference.matches) clear(element, tween);
    }
    if (preference.matches) return;
    const header=root.querySelector('.home-header');
    if(header&&!headers.has(header)){
      headers.add(header);
      const line=gsap.fromTo(header,{'--rule-progress':0},{'--rule-progress':1,duration:1,ease:'power3.out',onComplete(){active.delete(header);}});
      active.set(header,line);
    }
    for (const element of root.querySelectorAll(selector)) {
      if (seen.has(element)) continue;
      seen.add(element);
      // Live bus/dining refreshes should never blink or replay visible content.
      // Only animate sections entering from below the current viewport.
      if (element.getBoundingClientRect().top < innerHeight * .92) continue;
      const tween = gsap.fromTo(element, {opacity: .12, y: 42, scale:.985}, {
        opacity: 1, y: 0, scale:1, duration: .85, ease: 'power2.out',
        scrollTrigger: {trigger: element, start: 'top 92%', once: true},
        onComplete() {gsap.set(element, {clearProps: 'opacity,transform,--rule-progress'}); active.delete(element);}
      });
      active.set(element, tween);
    }
    ScrollTrigger.refresh();
  }
  function schedule() { if (!frame) frame = requestAnimationFrame(sync); }
  // Follow route changes and asynchronous data without accumulating triggers.
  const observer = new MutationObserver(schedule);
  observer.observe(root, {childList: true, subtree: true});
  preference.addEventListener('change', schedule);
  // Keyboard navigation must reveal a section immediately.
  function focus(event) {
    for (const [element, tween] of active) {
      if (element.contains(event.target)) clear(element, tween);
    }
  }
  root.addEventListener('focusin', focus);
  schedule();
  return () => {
    observer.disconnect(); cancelAnimationFrame(frame);
    preference.removeEventListener('change', schedule);
    root.removeEventListener('focusin', focus);
    for (const [element, tween] of active) clear(element, tween);
  };
}
