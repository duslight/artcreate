// acDlg：页内模态对话框（替代浏览器原生 alert/confirm/prompt）
// 约定：本项目所有前端弹窗一律走本组件，禁止直接调用 window.alert/confirm/prompt。
// API：
//   acConfirm({title, message, okText, danger})      -> Promise<boolean>
//   acPrompt({title, message, placeholder, value})   -> Promise<string|null>（null=取消）
//   acAlert({title, message})                        -> Promise<void>
// 样式与 workbench 深色主题一致；Esc=取消，回车=确认（prompt/alert）。
(function () {
  let overlay = null;

  function ensureDom() {
    if (overlay) return;
    overlay = document.createElement('div');
    overlay.id = 'ac-dlg-overlay';
    overlay.style.cssText =
      'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.55);' +
      'display:none;align-items:center;justify-content:center;font-size:13px;';
    overlay.innerHTML =
      '<div id="ac-dlg" style="background:#1e2023;border:1px solid #3a3d42;' +
      'border-radius:10px;box-shadow:0 8px 32px rgba(0,0,0,.6);' +
      'min-width:320px;max-width:440px;padding:16px 18px;color:#d8dad9;' +
      'font-family:inherit;line-height:1.6;">' +
      '<div id="ac-dlg-title" style="font-weight:600;font-size:14px;margin-bottom:8px"></div>' +
      '<div id="ac-dlg-msg" style="color:#a8acb0;margin-bottom:12px"></div>' +
      '<input id="ac-dlg-input" style="display:none;width:100%;box-sizing:border-box;' +
      'background:#141518;border:1px solid #3a3d42;border-radius:6px;color:#d8dad9;' +
      'padding:7px 9px;font-size:13px;margin-bottom:12px;outline:none">' +
      '<div style="display:flex;gap:8px;justify-content:flex-end">' +
      '<button id="ac-dlg-cancel" style="background:#2a2d31;border:1px solid #3a3d42;' +
      'color:#d8dad9;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:13px">取消</button>' +
      '<button id="ac-dlg-ok" style="background:#3d5a80;border:1px solid #5a7ca6;' +
      'color:#fff;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:13px">确定</button>' +
      '</div></div>';
    document.body.appendChild(overlay);
  }

  function open(opts) {
    // opts: {title, message, input:false, placeholder, value, okText, danger, onDone}
    ensureDom();
    return new Promise(resolve => {
      const dlg = overlay.querySelector('#ac-dlg');
      const titleEl = overlay.querySelector('#ac-dlg-title');
      const msgEl = overlay.querySelector('#ac-dlg-msg');
      const input = overlay.querySelector('#ac-dlg-input');
      const okBtn = overlay.querySelector('#ac-dlg-ok');
      const cancelBtn = overlay.querySelector('#ac-dlg-cancel');

      titleEl.textContent = opts.title || '确认';
      msgEl.textContent = opts.message || '';
      msgEl.style.display = opts.message ? '' : 'none';
      okBtn.textContent = opts.okText || '确定';
      okBtn.style.background = opts.danger ? '#8a3232' : '#3d5a80';
      okBtn.style.borderColor = opts.danger ? '#b05555' : '#5a7ca6';
      if (opts.input) {
        input.style.display = '';
        input.placeholder = opts.placeholder || '';
        input.value = opts.value || '';
      } else {
        input.style.display = 'none';
      }

      function close(result) {
        overlay.style.display = 'none';
        document.removeEventListener('keydown', onKey, true);
        resolve(result);
      }
      function onKey(ev) {
        if (ev.key === 'Escape') { ev.stopPropagation(); close(opts.input ? null : false); }
        else if (ev.key === 'Enter' && (opts.input || opts.alert)) {
          ev.stopPropagation();
          close(opts.input ? (input.value || '') : true);
        }
      }

      okBtn.onclick = () => close(opts.input ? (input.value || '') : true);
      cancelBtn.onclick = () => close(opts.input ? null : false);
      overlay.onclick = ev => { if (ev.target === overlay) close(opts.input ? null : false); };
      document.addEventListener('keydown', onKey, true);

      overlay.style.display = 'flex';
      if (opts.input) { input.focus(); input.select(); }
      else okBtn.focus();
      dlg.style.outline = 'none';
    });
  }

  window.acConfirm = opts => open(Object.assign({}, opts, {input: false}));
  window.acPrompt = opts => open(Object.assign({}, opts, {input: true}));
  window.acAlert = opts => open(Object.assign({}, opts, {input: false, alert: true, okText: '知道了'}));
})();
