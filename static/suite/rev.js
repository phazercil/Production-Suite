// After an update, screens left open load the new version by themselves (checked once a minute).
// A screen where someone is typing or has a dialog open shows a note instead, and reloads once it is idle.
(function(){
  var me=document.currentScript, rev=String(me&&me.dataset.rev||''), note=null;
  function busy(){var a=document.activeElement;return (a&&/^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)&&a.type!=='checkbox')||!!document.querySelector('.modal:not([hidden]),dialog[open]')}
  function check(){
    fetch('/api/version',{cache:'no-store',credentials:'same-origin'}).then(function(r){return r.ok?r.json():null}).then(function(v){
      if(!v||!rev||String(v.revision)===rev)return;
      if(!busy()){location.reload();return}
      if(!note){note=document.createElement('div');note.className='suite-rev-new';note.setAttribute('role','status');
        note.textContent='A new version (rev '+v.revision+') is installed. This screen reloads when you finish.';
        note.style.cssText='position:fixed;right:8px;bottom:18px;z-index:2147483001;background:#22344A;color:#CFE3F7;padding:6px 10px;border-radius:6px;font:500 13px system-ui,sans-serif;cursor:pointer';
        note.onclick=function(){location.reload()};document.body.appendChild(note)}
    }).catch(function(){});
  }
  setInterval(check,60000);
})();
