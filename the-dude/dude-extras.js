// The Dude — extras: embedded terminal (shellinabox via iframe)
(function() {
  // Create terminal panel
  var panel = document.createElement('div');
  panel.id = 'dude-terminal-panel';
  Object.assign(panel.style, {
    position: 'fixed', bottom: '0', left: '0', right: '0',
    height: '0', zIndex: '15', overflow: 'hidden',
    background: 'rgba(0,8,0,0.9)',
    borderTop: '1px solid rgba(0,255,65,0.15)',
    borderRadius: '12px 12px 0 0',
    transition: 'height 0.3s ease-out'
  });

  var iframe = document.createElement('iframe');
  iframe.src = 'http://' + window.location.hostname + ':8012';
  Object.assign(iframe.style, {
    width: '100%', height: '100%', border: 'none'
  });
  panel.appendChild(iframe);
  document.body.appendChild(panel);

  var open = false;
  function toggle() {
    open = !open;
    panel.style.height = open ? '50vh' : '0';
    if (open) iframe.focus();
  }

  document.addEventListener('keydown', function(e) {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if (e.key === 't') toggle();
    if (e.key === 'Escape' && open) toggle();
  });
})();
