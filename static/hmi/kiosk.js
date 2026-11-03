/* Operator Kiosk front end.
 * Django owns every rule and record (hmi/engine.py). This script only draws the screens from the
 * snapshot the server sends and posts each tap back as a command. It polls /api/hmi/state every
 * two seconds and redraws when the server's version number changes.
 */
const MIN=60000;
const LS={get:k=>{try{return localStorage.getItem(k)}catch(e){return null}},set:(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}},del:k=>{try{localStorage.removeItem(k)}catch(e){}}};
const QS=new URLSearchParams(location.search);
// A machine's own address (/station/<machine>/) locks this screen to that machine's operator pages.
const STATION_LOCK=(document.querySelector('meta[name="station-lock"]')||{}).content||'';
const LOCKED_OUT=['team','admin','setup'];
if(QS.get('line')&&!STATION_LOCK)LS.set('hmi-station',QS.get('line'));
let S=null, ME=null, CLIENT_IP='', SRV={server:true}, recvAt=Date.now(), online=true, booted=false, API=null;
let station=STATION_LOCK||LS.get('hmi-station')||'';
let ui={conn:{},dbSql:{},dbRes:{},dbTP:{},bld:{},page:'controls',schedView:'timeline',schedFilter:'OPEN',sort:null,pick:null,lastNote:null,modal:null,modalSig:'',lastHTML:''};

function csrf(){const m=document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);return m?decodeURIComponent(m[1]):''}
const Remote={
  v:null, timer:null,
  async get(force){
    try{
      const r=await fetch('/api/hmi/state?line='+encodeURIComponent(station)+(this.v!=null&&!force?'&v='+this.v:'')+(force?'&force=1':''),{cache:'no-store',credentials:'same-origin'});
      const j=await r.json();
      if(r.status===401&&j.keyRequired){location.reload();return false}
      setOnline(true);
      if(!j.same){this.v=j.snap.v;applyState(j)}
      return true;
    }catch(e){setOnline(false);return false}
  },
  poll(){clearTimeout(this.timer);this.timer=setTimeout(async()=>{await this.get();this.poll()},2000)},
  async post(path,body){
    try{
      const r=await fetch(path,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},body:JSON.stringify(body||{})});
      const j=await r.json(); setOnline(true);
      if(j.snap){this.v=j.snap.v;applyState(j)} else if(j.note) toast(j.note);
      return j;
    }catch(e){setOnline(false);return {ok:false,error:'The server is not answering. Check the network and try again.'}}
  },
  login(userId,pin,adminOnly){return this.post('/api/hmi/login',{userId,pin,adminOnly})},
  logout(){return this.post('/api/hmi/logout',{})},
  cmd(name,p){return this.post('/api/hmi/cmd',{name,p:p||{},line:station})},
  ping(){fetch('/api/hmi/ping',{method:'POST',credentials:'same-origin',headers:{'X-CSRFToken':csrf()}}).catch(()=>{})}
};

/* Read-only helpers over the snapshot (no plant rules here; the server applies those). */
const E={
  MIN,
  get QDEF(){return S.qdef},
  partKey:i=>String(i||'').trim().toUpperCase().replace(/^\d(?=[A-Z])/,''),
  part:(S,i)=>{const k=E.partKey(i);return S.specs[k]||{key:k,pleats:24,len:9,wid:8,ph:1,sb:1.1,tol:0.06,rev:'A',media:'Unknown media',estimated:true}},
  activeLines:S=>S.lines.filter(l=>l.active!==false),
  lineOf:(S,id)=>S.lines.find(l=>l.id===id),
  moById:(S,id)=>id==null?null:S.mos.find(m=>String(m.id)===String(id))||null,
  curMO:(S,line)=>{const sh=(S.runs[line]||{}).shift;return sh&&sh.active?E.moById(S,sh.moId):null},
  shiftStats:(S,l)=>(S.runs[l]||{}).stats||{mins:0,run:0,down:0,uptime:100},
  shiftAt:(S)=>S.shiftNow?{name:S.shiftNow}:null,
  toMin:v=>{const [h,m]=String(v||'0:0').split(':').map(Number);return (h||0)*60+(m||0)},
  midnight:ms=>{const d=new Date(ms);d.setHours(0,0,0,0);return d.getTime()},
  coParams:(S,line)=>{const sh=(S.runs[line]||{}).shift,co=sh&&sh.co,m=co&&co.newMo?E.moById(S,co.newMo):E.curMO(S,line),old=co&&co.oldMo?E.moById(S,co.oldMo):null,L=E.lineOf(S,line);
    return {part:m?E.partKey(m.item):'',machine:line,from_part:old?E.partKey(old.item):'',mo:m?String(m.mo):'',line_code:L?L.code:''}},
  normSteps:rows=>(rows||[]).map(r=>{const o={};for(const k in r)o[k.toLowerCase()]=r[k];return o}).map((o,i)=>({o,i}))
    .sort((a,b)=>(a.o.seq!=null&&b.o.seq!=null?(+a.o.seq)-(+b.o.seq):0)||a.i-b.i)
    .map(({o})=>({text:String(o.step??o.instruction??o.text??'').trim(),detail:o.detail??'',setting:o.setting??'',value:o.value??'',unit:o.unit??'',asset:o.asset??'',image:o.image??''})).filter(x=>x.text),
  normSettings:rows=>(rows||[]).map(r=>{const o={};for(const k in r)o[k.toLowerCase()]=r[k];return o}).map(o=>({name:String(o.name??o.setting??'').trim(),value:o.value??'',unit:o.unit??'',asset:o.asset??''})).filter(x=>x.name)
};

function applyState(st){
  ME=st.me; SRV=st.server||{server:false}; S=st.snap; recvAt=Date.now(); if(typeof st.clientIp==='string')CLIENT_IP=st.clientIp;
  if(st.note&&st.note!==ui.lastNote){ui.lastNote=st.note;toast(st.note)} if(!st.note)ui.lastNote=null;
  derive(); onState();
}
function derive(){
  const act=E.activeLines(S);
  if(STATION_LOCK) station=STATION_LOCK;
  else if(!station||!E.lineOf(S,station)) station=(act[0]||S.lines[0]||{}).id||'';
  const R=S.runs[station]||{shift:null,hist:[],qcLast:0,feed:{}};
  S.shift=R.shift; S.hist=R.hist||[]; S.qcLast=R.qcLast||0; S.feed=R.feed||{};
}
function setOnline(v){if(online!==v){online=v;if(booted)renderBanner()}}
function onState(){
  if(!booted) return;
  // Ask for downtime reasons as soon as unexplained stops add up to the limit (not over another open window).
  if(reasonDue()&&$('#modal').hidden&&!pending&&(!ui.downSnooze||Date.now()>ui.downSnooze)){ui.downSig=JSON.stringify(needReason().map(d=>d.id));openDownPrompt()}
  renderNav(); renderBanner();
  const a=document.activeElement, typing=a&&a!==document.body&&/^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName);
  if(!$('#modal').hidden) refreshModal(typing&&$('#sheet').contains(a));
  if(ui.page==='controls'||ui.page==='status') patch();
  else if(!(typing&&$('#view').contains(a))) render();
  liveBits();
}
function refreshModal(typing){
  if(ui.modal==='downPrompt'&&!typing){if(!reasonDue())closeModal();else if(JSON.stringify(needReason().map(d=>d.id))!==ui.downSig){ui.downSig=JSON.stringify(needReason().map(d=>d.id));openDownPrompt()}return}
  if(ui.modal==='changeover'){const sig=JSON.stringify([S.shift&&S.shift.co,shiftOn()]);if(sig!==ui.modalSig&&!typing){if(S.shift&&S.shift.co)openChangeover(true);else closeModal()}}
}

/* ===================== helpers ===================== */
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const now=()=>S?S.now+(Date.now()-recvAt):Date.now();
const fmtT=ms=>{const d=new Date(ms);let h=d.getHours();const ap=h>=12?'pm':'am';h=h%12||12;return h+':'+String(d.getMinutes()).padStart(2,'0')+' '+ap};
const fmtD=ms=>{const d=new Date(ms);return (d.getMonth()+1)+'/'+d.getDate()+'/'+d.getFullYear()};
const fmtISO=s=>{if(!s)return'';const [y,m,d]=String(s).split('-');return +m+'/'+ +d+'/'+y};
const isoToMs=s=>{const [y,m,d]=String(s).split('-').map(Number);return new Date(y,m-1,d).getTime()};
const hm=min=>{min=Math.max(0,Math.round(min));return Math.floor(min/60)+':'+String(min%60).padStart(2,'0')};
const hms=ms=>{const s=Math.max(0,Math.floor(ms/1000));return Math.floor(s/3600)+':'+String(Math.floor(s%3600/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0')};
const user=()=>ME;
const isAdmin=()=>!!(ME&&ME.role==='admin');
const C=()=>S.config;
const part=i=>E.part(S,i), partKey=E.partKey, lineOf=id=>E.lineOf(S,id), moById=id=>E.moById(S,id), activeLines=()=>E.activeLines(S);
const curMO=()=>E.curMO(S,station);
const shiftOn=()=>!!(S.shift&&S.shift.active);
const sid=()=>S.shift?S.shift.id:null;
const shiftStats=line=>E.shiftStats(S,line||station);
const isLate=m=>m.status==='OPEN'&&isoToMs(m.due)<now();
// Copy text. The clipboard API only exists on https pages, and screens reach the suite over plain http,
// so fall back to the older copy command; resolves false if neither works (the caller then shows the text).
function copyText(text){
  if(navigator.clipboard&&window.isSecureContext)return navigator.clipboard.writeText(text).then(()=>true,()=>legacyCopy(text));
  return Promise.resolve(legacyCopy(text));
}
function legacyCopy(text){
  const ta=document.createElement('textarea');ta.value=text;ta.setAttribute('readonly','');
  ta.style.cssText='position:fixed;top:0;left:0;width:1px;height:1px;opacity:0';
  document.body.appendChild(ta);ta.select();ta.setSelectionRange(0,text.length);
  let ok=false;try{ok=document.execCommand('copy')}catch(e){ok=false}
  ta.remove();return ok;
}
function toast(msg){const t=$('#toast');if(!t)return;t.textContent=msg;t.hidden=false;clearTimeout(toast.t);toast.t=setTimeout(()=>t.hidden=true,3600)}
function css(v){return getComputedStyle(document.documentElement).getPropertyValue(v).trim()}
// Ask the server (or the demo engine) to make a change
async function send(name,p,opts={}){
  const r=await API.cmd(name,p||{});
  if(!r.ok){
    if(r.admin&&opts.retry!==false){openLogin(()=>send(name,p,Object.assign({},opts,{retry:false})),true);return null}
    if(r.login&&opts.retry!==false){openLogin(()=>send(name,p,Object.assign({},opts,{retry:false})));return null}
    if(opts.err&&$(opts.err)){$(opts.err).textContent=r.error;return null}
    toast(r.error); if(opts.restore) render(true); return null;
  }
  if(r.msg&&!opts.quiet) toast(r.msg);
  return r;
}

/* ===================== derived ===================== */
const shiftDowns=()=>S.downtime.filter(d=>d.shift===sid()).sort((a,b)=>a.start-b.start);
const needReason=()=>S.downtime.filter(d=>!d.reason&&d.line===station);
// Stops without a reason are only asked about once they add up to Rules > reasonAfter minutes.
const unexplainedMins=()=>needReason().reduce((t,d)=>t+(d.mins||0),0);
const reasonDue=()=>needReason().length>0&&unexplainedMins()>=(C().rules.reasonAfter||0);
const canOverride=()=>!!(ME&&(ME.role==='admin'||ME.role==='maintenance'));
const qcDue=()=>shiftOn()&&S.settings.autoQC&&now()-S.qcLast>=S.settings.qcEvery*MIN;
const unreadSched=()=>S.notices.filter(n=>!n.read&&n.kind!=='team');
const openTickets=()=>S.tickets.filter(t=>t.status!=='Closed');
// errors still showing on a machine, plus alarm-log entries since its current stop began
const curErrors=line=>{const R=S.runs[line]||{},sh=R.shift,from=sh&&sh.stopStart?sh.stopStart-2*MIN:now()-10*MIN;return (R.errors||[]).filter(x=>x.end==null||x.end>=from).reverse()};
function alarms(){
  const a=[];
  if(SRV.server&&!SRV.storeOk) a.push({sev:'bad',text:`The server cannot save records: ${SRV.storeErr}`,go:'admin'});
  if(shiftOn()&&S.feed&&S.feed.type&&!S.feed.ok) a.push({sev:'bad',text:`No machine data from ${station}: ${S.feed.err||'feed offline'}`,go:'status'});
  if(shiftOn()&&S.feed&&S.feed.error) a.push({sev:'warn',text:`${station} reports: ${S.feed.error}`,go:'downtime'});
  const nr=reasonDue()?needReason().length:0; if(nr) a.push({sev:'bad',text:`${nr} downtime ${nr>1?'entries need':'entry needs'} a reason`,go:'downtime'});
  if(qcDue()) a.push({sev:'warn',text:`QC check due (every ${S.settings.qcEvery} min)`,go:'qcCheck'});
  unreadSched().forEach(n=>a.push({sev:'warn',text:n.text,go:'schedule',notice:n.id}));
  openTickets().filter(t=>t.priority==='High').forEach(t=>a.push({sev:'bad',text:`Maintenance: ${t.machine} — ${t.type}`,go:'maintenance'}));
  return a;
}

/* ===================== shell ===================== */
const PAGES=[
  ['controls','Run screen'],['schedule','Schedule'],['qc-log','QC Log'],['downtime','Downtime'],['maintenance','Maintenance'],['status','Status'],['end-of-shift','End of Shift'],
  null,['team','Team'],['admin','Admin',1]
];
function renderNav(){
  const flags={schedule:unreadSched().length?'!':'',downtime:reasonDue()?needReason().length:'', 'qc-log':qcDue()?'!':'',maintenance:openTickets().length||''};
  $('#nav').innerHTML=PAGES.filter(p=>!(STATION_LOCK&&(!p||LOCKED_OUT.includes(p[0])))).map(p=>{
    if(!p) return '<hr>';
    const [id,label,adm]=p, f=flags[id];
    const flag=(id==='schedule'&&f)||(id==='downtime'&&f)||(id==='qc-log'&&f);
    return `<button data-action="go" data-page="${id}" class="${ui.page===id?'on':''} ${flag?'flag':''}">${label}${adm&&!isAdmin()?'<span class="lock" title="Admin">●</span>':''}${f?`<span class="badge">${f}</span>`:''}</button>`;
  }).join('');
  const n=alarms().length; $('#alarmCount').textContent=n||''; $('#alarmBtn').style.opacity=n?1:.35;
  const u=user(); $('#loginLabel').textContent=u?'Log out':'Log in';
  $('#loginBtn').querySelector('.who')?.remove();
  if(u) $('#loginBtn').insertAdjacentHTML('beforeend',`<span class="who">${esc(u.name)}</span>`);
}
function renderBanner(){
  const parts=[];
  if(reasonDue()) parts.push(`<a data-action="downPrompt">${needReason().length} downtime ${needReason().length>1?'entries need':'entry needs'} a reason</a>`);
  if(unreadSched().length) parts.push('The <a data-action="go" data-page="schedule">schedule</a> changed');
  if(qcDue()) parts.push('<a data-action="qcCheck">QC check</a> due');
  const sh=S.shift;
  const state=shiftOn()?(sh.state==='changeover'?'Changeover in progress':sh.state==='stopped'?'Line stopped':'Running'):'No shift running';
  const chips=[];
  if(SRV.server&&!online) chips.push('<span class="demo bad">Server offline · retrying</span>');
  else if(SRV.server&&!SRV.storeOk) chips.push(`<span class="demo bad" title="${esc(SRV.storeErr)}">Not saving</span>`);
  if(!SRV.server) chips.push('<span class="demo" title="Running in this browser. Install the HMI server to share data between screens.">Demo · no server</span>');
  else if(S.demo) chips.push('<span class="demo">Demo data</span>');
  if(isAdmin()) chips.push('<button class="demo bad" data-action="login" title="Log out of admin mode">Admin mode ✕</button>');
  const st=!STATION_LOCK&&activeLines().length>1?`<select id="stationSel" class="stationsel" data-change="station" aria-label="This screen's machine">${activeLines().map(l=>`<option ${l.id===station?'selected':''}>${esc(l.id)}</option>`).join('')}</select>`:`<span class="ln">${esc(station)}</span>`;
  const u=user(), op=shiftOn()?(sh.userName||sh.user):(u?u.name:'');
  const conn=SRV.server?(online?'<span class="dot ok"></span>Server connected':'<span class="dot bad"></span>Server offline'):'<span class="dot warn"></span>This browser only';
  $('#banner').innerHTML=`<div class="lineid"><span class="ln">Line ${st}</span><span class="sh">${esc((E.shiftAt(S,now())||{name:'Off shift'}).name)} · ${state}</span></div>
    <div class="msg ${parts.length?'alert':''}">${parts.join(' · ')}</div>${chips.join('')}
    ${op?`<div class="who"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#A7AFB8" stroke-width="1.8" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"/></svg><div><small>Operator${S.users.some(x=>x.source==='paycom')?' · Paycom':''}</small><b>${esc(op)}</b></div></div>`:''}
    <div class="conn">${conn}</div><div class="clock num" title="${fmtD(now())}">${fmtT(now()).replace(/ (am|pm)/,'<small style="font-size:14px;color:var(--ink-2)"> $1</small>')}</div>`;
}
function render(force){
  if(!S) return; renderNav(); renderBanner();
  const g=$('.gantt'); if(g) ui.ganttX=g.scrollLeft;
  const html=(VIEWS[ui.page]||VIEWS.controls)();
  if(force||html!==ui.lastHTML){$('#view').innerHTML=html;ui.lastHTML=html}
  afterRender();
}
// swap only regions marked data-r, so embedded Grafana panels are never reloaded
function patch(){
  const t=document.createElement('div'); t.innerHTML=VIEWS[ui.page](); let ok=true;
  t.querySelectorAll('[data-r]').forEach(n=>{const cur=document.querySelector('#view [data-r="'+n.dataset.r+'"]');if(!cur){ok=false;return}if(cur.outerHTML!==n.outerHTML)cur.replaceWith(n)});
  if(!ok) render(true); else afterRender();
}
const LIVE={
  qcNext:()=>{if(!S.settings.autoQC)return'Auto QC off';const m=S.settings.qcEvery-(now()-S.qcLast)/MIN;return m<=0?`Overdue ${hm(-m)}`:`in ${hm(m)}`},
  coTimer:()=>S.shift&&S.shift.co&&S.shift.co.start?hms(now()-S.shift.co.start):'0:00:00',
  moTimer:()=>{const sh=S.shift;if(!shiftOn())return '0:00:00';return sh.state==='changeover'&&sh.co&&sh.co.start?hms(now()-sh.co.start):curMO()?hms(now()-sh.moStart):'0:00:00'}
};
function liveBits(){document.querySelectorAll('[data-live]').forEach(el=>{const k=el.dataset.live;if(LIVE[k])el.textContent=LIVE[k]()})}
function afterRender(){
  document.querySelectorAll('canvas[data-chart]').forEach(c=>drawPPM(c));
  const g=$('.gantt');if(g){if(ui.ganttX!=null)g.scrollLeft=ui.ganttX;else{const nl=g.querySelector('.nowline');if(nl)g.scrollLeft=Math.max(0,parseFloat(nl.style.left)-320);ui.ganttX=g.scrollLeft}}
}
function go(p){if(p==='qcCheck'){openQC();return}if(STATION_LOCK&&LOCKED_OUT.includes(p))p='controls';if(p==='setup'){ui.adminTab='connections';p='admin'}ui.page=p;ui.ganttX=null;try{if(location.hash!=='#'+p)history.replaceState(null,'','#'+p)}catch(e){}$('#nav').classList.remove('open');render(true);$('main').scrollTop=0}

/* ===================== chart ===================== */
function drawPPM(c){
  const r=c.getBoundingClientRect(); if(!r.width) return;
  const dpr=window.devicePixelRatio||1; c.width=r.width*dpr; c.height=r.height*dpr;
  const x=c.getContext('2d'); x.scale(dpr,dpr);
  const W=r.width,H=r.height,L=40,R=10,T=26,B=22, h=S.hist;
  x.fillStyle=css('--ground');x.fillRect(0,0,W,H);
  const ink2=css('--ink-2'), grid=css('--rule');
  x.font='500 10px '+css('--f-mono'); x.fillStyle=ink2;
  if(!h.length){x.textAlign='center';x.font='600 14px '+css('--f-body');x.fillText(shiftOn()?'Waiting for the first reading…':'No shift running',W/2,H/2);return}
  const line=lineOf(S.shift.line), rated=line?line.rated:200;
  const t0=h[0].t-MIN, t1=Math.max(h[h.length-1].t,t0+90*MIN);
  const ymax=Math.ceil(Math.max(rated,...h.map(p=>p.ppm))*1.1/100)*100;
  const X=t=>L+(t-t0)/(t1-t0)*(W-L-R), Y=v=>T+(1-v/ymax)*(H-T-B);
  // grid + y ticks
  x.strokeStyle=grid;x.lineWidth=1;x.textAlign='right';x.textBaseline='middle';
  for(let v=0;v<=ymax;v+=ymax/4){x.beginPath();x.moveTo(L,Y(v));x.lineTo(W-R,Y(v));x.stroke();x.fillText(Math.round(v),L-6,Y(v))}
  // x ticks hourly
  x.textAlign='center';x.textBaseline='alphabetic';
  const d=new Date(t0);d.setMinutes(0,0,0);let tk=d.getTime()+3600000;
  const step=(t1-t0)>8*3600000?2*3600000:3600000;
  for(;tk<t1;tk+=step){x.fillText(fmtT(tk).replace(':00 ',''),X(tk),H-6)}
  // downtime bands + letters
  const evs=shiftDowns(); const open=S.shift.stopStart;
  const bands=evs.map((e,i)=>({s:e.start,e:e.end,ok:!!e.reason,l:String.fromCharCode(65+i%26)}));
  if(open) bands.push({s:open,e:now(),ok:false,l:'…'});
  let lastCx=-99;
  bands.forEach(b=>{
    x.fillStyle=b.ok?'rgba(167,175,184,.12)':'rgba(224,108,91,.18)';x.fillRect(X(b.s),T,Math.max(2,X(b.e)-X(b.s)),H-T-B);
    const cx=(X(b.s)+X(b.e))/2; if(cx-lastCx<18) return; lastCx=cx; x.beginPath();x.arc(cx,13,8,0,7);x.fillStyle=css('--well');x.fill();x.strokeStyle=b.ok?ink2:css('--bad');x.lineWidth=1.5;x.stroke();
    x.fillStyle=b.ok?ink2:css('--bad');x.font='600 10px '+css('--f-mono');x.textBaseline='middle';x.fillText(b.l,cx,13.5);x.textBaseline='alphabetic';
  });
  // rated line
  x.setLineDash([4,4]);x.strokeStyle=css('--ink-3');x.beginPath();x.moveTo(L,Y(rated));x.lineTo(W-R,Y(rated));x.stroke();x.setLineDash([]);
  x.fillStyle=ink2;x.textAlign='left';x.font='500 10px '+css('--f-mono');x.fillText(`rated ${rated} P/m`,L+4,Y(rated)-4);
  // series
  x.beginPath();h.forEach((p,i)=>{const px=X(p.t),py=Y(p.ppm);i?x.lineTo(px,py):x.moveTo(px,py)});
  x.lineTo(X(h[h.length-1].t),Y(0));x.lineTo(X(h[0].t),Y(0));x.closePath();x.fillStyle='rgba(91,155,213,.16)';x.fill();
  x.beginPath();h.forEach((p,i)=>{const px=X(p.t),py=Y(p.ppm);i?x.lineTo(px,py):x.moveTo(px,py)});
  x.strokeStyle=css('--accent');x.lineWidth=2;x.lineJoin='round';x.stroke();
  const lp=h[h.length-1];x.beginPath();x.arc(X(lp.t),Y(lp.ppm),3.5,0,7);x.fillStyle=css('--accent');x.fill();
  x.fillStyle=css('--ink');x.font='600 11px '+css('--f-mono');x.textAlign='right';x.fillText(`${lp.ppm} P/m`,W-R-2,T+12);
}
function grafanaSrc(panel,extra=''){
  const g=S.settings.grafana; if(!g.on||!g.url||!g.uid||!panel) return '';
  const from=S.shift?S.shift.start:'now-12h';
  const q=new URLSearchParams({orgId:g.org||1,panelId:panel,from:String(from),to:'now',refresh:g.refresh||'10s',theme:'dark'});
  if(g.lineVar&&S.shift) q.set('var-'+g.lineVar,S.shift.line);
  return `${g.url.replace(/\/$/,'')}/d-solo/${encodeURIComponent(g.uid)}/${encodeURIComponent(g.slug||'_')}?${q}${extra}`;
}
/* ===================== icons & art ===================== */
const ico=d=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const IC={
 start:ico('<path d="M8 5v14l11-7Z"/>'),
 end:ico('<rect x="6" y="6" width="12" height="12" rx="1"/>'),
 co:ico('<path d="M4 7h13l-3-3M20 17H7l3 3"/>'),
 maint:ico('<path d="M14 6l4 4-8 8H6v-4zM12 8l4 4"/>'),
 camera:ico('<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>'),
 draw:ico('<path d="M5 5h14v14H5zM9 9h6M9 13h6"/>'),
 qc:ico('<circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5M8.5 11l2 2 3.5-4"/>'),
 rolls:ico('<ellipse cx="8" cy="12" rx="3" ry="6"/><path d="M8 6h9a3 6 0 0 1 0 12H8"/>'),
 notes:ico('<path d="M6 4h9l3 3v13H6zM9 10h6M9 14h6"/>'),
 down:ico('<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>')
};
const PLAN_FMT=ms=>{const d=new Date(ms),t=new Date(now()),days=(E.midnight(ms)-E.midnight(+t))/864e5;return (days===0?'':days>0&&days<6?['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][d.getDay()]+' ':(d.getMonth()+1)+'/'+d.getDate()+' ')+fmtT(ms)};
function upNext(){
  const sh=S.shift, run=shiftOn()?curMO():null, out=[];
  const list=S.mos.filter(m=>m.line===station&&m.status==='OPEN'&&!(run&&String(run.id)===String(m.id))).sort((a,b)=>a.seq-b.seq);
  let prev=run?run.item:(sh&&sh.co&&sh.co.oldMo?(moById(sh.co.oldMo)||{}).item:null);
  for(const m of list){out.push({m,co:prev==null||partKey(prev)!==partKey(m.item)});prev=m.item}
  return out;
}
const VIEWS={};
VIEWS.controls=()=>{
  const sh=S.shift, on=shiftOn(), mo=curMO(), st=shiftStats(), g=grafanaSrc(S.settings.grafana.ppm);
  const stateP=!on?'<span class="pill idle">IDLE</span>':sh.state==='changeover'?'<span class="pill warn">CHANGEOVER</span>':sh.state==='stopped'?'<span class="pill bad">STOPPED</span>':'<span class="pill ok">RUNNING</span>';
  const lastPpm=S.hist.length?S.hist[S.hist.length-1].ppm:0;
  const nx=upNext();
  let order;
  if(!on){
    const first=nx[0];
    order=`<div class="panel idlecard" data-r="order"><span class="eyebrow">Line ${esc(station)}</span><h2>No shift running</h2>
      <p class="sub">Log in, walk the safety checklist and scan the first MO to start running.${first?` First up from the planner: MO ${esc(first.m.mo)} · ${esc(first.m.item)}.`:''}</p>
      <button class="btn go bigbtn" style="width:auto;padding-inline:28px" data-action="startShift">${IC.start}Start shift</button></div>`;
  }else{
    const p=mo?part(mo.item):null, good=mo?Math.max(0,mo.produced-mo.scrap):0, pct=mo&&mo.qty?Math.min(100,good/mo.qty*100):0;
    const fin=mo?(S.views[station]||{}).eta:null;
    const per=(sh.override&&sh.override.pleats)||(p?p.pleats:0);
    const timer=sh.state==='changeover'&&sh.co&&sh.co.start?hms(now()-sh.co.start):mo?hms(now()-sh.moStart):'0:00:00';
    order=`<div class="panel order" data-r="order">
      <div class="hd"><span class="eyebrow">${sh.state==='changeover'?'Changeover':'Current order'}</span>${mo?'<span class="from">from S2K</span>':''}<span class="grow"></span>${stateP}</div>
      ${mo?`<div class="mo"><b>MO ${esc(mo.mo)}</b><span>Part ${esc(mo.item)}</span></div>
      <div class="desc">${esc(p.media)} · ${p.pleats} pleats/unit · ${p.len.toFixed(2)}″ × ${p.wid.toFixed(2)}″ · drawing rev ${esc(p.rev)}${p.estimated?' · <span style="color:var(--warn)">estimated specs</span>':''}</div>
      <div class="prog"><div class="lbl"><span>Good parts</span><b class="num">${good.toLocaleString()} <small>/ ${mo.qty.toLocaleString()}</small></b></div><div class="track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(pct)}" aria-label="MO progress"><i style="width:${pct}%"></i></div></div>
      <div class="metrics">
        <div class="metric"><span>Rate (pcs/min)</span><b class="num">${per?(lastPpm/per).toFixed(1):'—'} <small>${lastPpm} P/m</small></b></div>
        <div class="metric ${mo.scrap?'bad':''}"><span>Scrap</span><b class="num">${mo.scrap.toLocaleString()}</b></div>
        <div class="metric"><span>Est. finish</span><b class="num">${fin?PLAN_FMT(fin).replace(/^(\S+) (?=\d+:)/,'<small>$1 </small>').replace(/ (am|pm)/,'<small>$1</small>'):'—'}</b></div>
      </div>`
      :`<div class="mo"><b>${sh.state==='changeover'?'Setting up':'No MO'}</b></div><div class="desc">${sh.state==='changeover'?'Finish the changeover steps to start the next MO.':'Scan an MO from Change Over to start counting parts against it.'}</div>`}
      <div class="shiftline"><span>Shift count <b>${sh.count.toLocaleString()}</b></span><span>Uptime <b>${st.uptime}%</b></span><span>Down <b>${st.down} min</b></span>${S.feed&&S.feed.recipe?`<span>Recipe <b>${esc(S.feed.recipe)}</b></span>`:''}<span>${sh.state==='changeover'?'Changeover':'MO'} timer <b data-live="moTimer">${timer}</b></span></div>
    </div>`;
  }
  const alert=reasonDue()||(sh&&sh.stopStart);
  const chart=`<div class="panel chartpanel"><div class="row"><span class="eyebrow">Line rate · this shift</span><span class="small">${g?'Grafana panel':'Pleats per minute'}</span></div>
    <div class="chartcard ${alert?'alert':''}" ${g?'':'data-r="chart"'}>${g?`<iframe src="${esc(g)}" title="Pleater speed (Grafana)" loading="lazy"></iframe><span class="src">Grafana</span>`:`<canvas data-chart="ppm" aria-label="Pleater speed, pleats per minute, this shift"></canvas>`}</div></div>`;
  const nextHTML=nx.slice(0,4).map((x,i)=>`<div class="job"><div class="t"><b>MO ${esc(x.m.mo)}</b><span>#${i+1}</span></div><div class="p">${esc(x.m.item)} · ${(x.m.qty-x.m.produced).toLocaleString()} pcs</div>
      ${x.m.planStart?`<div class="w">Planned ${PLAN_FMT(x.m.planStart)}${x.m.planEnd?` – ${PLAN_FMT(x.m.planEnd)}`:''}</div>`:''}${x.co?`<div class="co">${IC.co}Changeover required</div>`:''}</div>`).join('');
  const aside=`<aside class="panel upnext" data-r="next"><div class="row"><span class="eyebrow">Up next</span><span class="small">from planner${S.planAt?' · '+fmtT(S.planAt):''}</span></div>
    ${nextHTML||'<div class="empty">Nothing else is planned for this line. The scheduler adds runs here.</div>'}${nx.length>4?`<div class="small">+${nx.length-4} more on the <a href="#" data-action="go" data-page="schedule" style="color:var(--accent)">schedule</a></div>`:''}
    <div class="spacer"></div>
    ${on?`<button class="btn warn bigbtn" data-action="changeover">${IC.co}${sh.state==='changeover'?'Continue changeover':'Start changeover'}</button>`:''}</aside>`;
  const a=(k,label,act,cls='')=>`<button class="act ${cls}" data-action="${act}">${IC[k]}<span>${label}</span></button>`;
  return `${on&&S.feed&&S.feed.t&&!S.feed.ok?`<div class="panel" data-r="feedwarn" style="border-color:var(--bad)"><b>No machine data from ${esc(station)}.</b> ${esc(S.feed.err||'')} Counts and downtime are paused until readings come back.</div>`:''}
  <div class="runscreen"><div class="runcol">${order}${chart}</div>${aside}</div>
  <div class="actions" data-r="acts">
    ${a('qc','QC check','qcCheck',qcDue()?'due':'')}
    ${a('down','Downtime','goDowntime',reasonDue()?'due':'')}
    ${a('draw','Instructions','drawing')}
    ${a('rolls','Roll tags','rollTags')}
    ${a('notes','Shift notes','shiftNotes')}
    ${on?a('end','End shift','endShift'):a('start','Start shift','startShift','due')}
    ${a('maint','Call maintenance','maintTicket','bad')}
  </div>`;
};
const toMin=E.toMin, midnight=E.midnight;
// The run order and times come from the Scheduler. A running MO is drawn from its actual start to the server's live finish estimate.
function lineBlocks(lineId){
  const sh=(S.runs[lineId]||{}).shift, on=sh&&sh.active, out=[];
  const list=S.mos.filter(m=>m.line===lineId&&m.status==='OPEN').sort((a,b)=>a.seq-b.seq);
  for(const m of list){
    const running=on&&String(sh.moId)===String(m.id);
    if(running){out.push({kind:'mo',m,start:sh.moStart,end:Math.max(now(),(S.views[lineId]||{}).eta||m.planEnd||now()),running:true});continue}
    if(!m.planStart) continue;
    if(m.planCo){const coEnd=m.planStart+m.planCo*MIN;out.push({kind:'co',start:m.planStart,end:coEnd,mins:m.planCo})}
    out.push({kind:'mo',m,start:m.planCo?m.planStart+m.planCo*MIN:m.planStart,end:m.planEnd,running:false});
  }
  if(on&&sh.state==='changeover'&&sh.co&&sh.co.start) out.unshift({kind:'co',start:sh.co.start,end:Math.max(now(),sh.co.start+C().rules.coMins*MIN),mins:C().rules.coMins});
  return out;
}
function gantt(){
  const wins=S.today.windows||[], ds=wins.length?wins[0].s:midnight(now())+360*MIN, de=wins.length?Math.max(...wins.map(w=>w.e)):ds+16*60*MIN;
  const slots=Math.ceil((de-ds)/(30*MIN)), SW=66, LW=190, total=LW+slots*SW, X=t=>LW+(t-ds)/(30*MIN)*SW, end=ds+slots*30*MIN;
  const brks=wins.flatMap(w=>w.brk);
  let head=`<div class="ghead" style="width:${total}px"><div class="corner" style="width:${LW}px;flex:none"></div>`;
  for(let i=0;i<slots;i++){const tt=ds+i*30*MIN;head+=`<div class="slot" style="width:${SW}px">${fmtT(tt).replace(/ (am|pm)/,'')}</div>`}
  head+=wins.map((w,i)=>`<span class="day" style="left:${X(w.s)+4}px">${esc(w.name)}${i?'':' · '+fmtD(ds)}</span>`).join('')+'</div>';
  const lanes=activeLines().map(l=>{
    const blocks=lineBlocks(l.id); let html='';
    blocks.forEach((b,i)=>{
      if(b.start>=end||b.end<=ds) return;
      const l0=Math.max(X(b.start),LW), w=Math.max(30,Math.min(X(b.end),total)-l0-4), more=b.end>end;
      if(b.kind==='co'){html+=`<div class="blk co" style="left:${l0}px;width:${w}px">Change<br>Over<br>~${Math.round(b.mins)} min</div>`;const nx=blocks[i+1];if(nx)html+=`<span class="eta" style="left:${Math.max(LW,X(nx.start)-110)}px">ETA ${fmtT(nx.start)}</span>`;return}
      const m=b.m;
      html+=`<div class="blk ${b.running?'run':''}" style="left:${l0}px;width:${w}px;background:${l.color}" title="${esc(m.item)} · MO ${m.mo} · ${fmtT(b.start)}–${fmtT(b.end)}"><div>${esc(m.item)} - ${m.qty.toLocaleString()} units</div><div>MO ${m.mo} - ${((b.end-b.start)/36e5).toFixed(1)} h</div>${more?'<span class="more">›</span>':''}</div>`;
    });
    brks.forEach(b=>{html+=`<div class="blk lunch" style="left:${X(b.s)+2}px;width:${Math.max(24,X(b.e)-X(b.s)-4)}px" title="${esc(b.name)} ${fmtT(b.s)}–${fmtT(b.e)}">${esc(b.name)}</div>`});
    return `<div class="lane" style="width:${total}px"><div class="lab" style="width:${LW}px"><div style="background:${l.color}">${esc(l.id)}<span style="font-size:15px">${l.rated} P/m</span></div></div>${html}</div>`;
  }).join('');
  const nl=now()>ds&&now()<end?`<div class="nowline" style="left:${X(now())}px" title="Now"></div>`:'';
  return `<div class="gantt"><div class="inner" style="width:${total}px">${head}${lanes}${nl}</div></div>`;
}
function calendar(){
  const d=new Date(now()); const y=d.getFullYear(), m=d.getMonth(); const first=new Date(y,m,1); const start=new Date(y,m,1-first.getDay());
  let h='<div class="cal">'+['Sun','Mon','Tue','Wed','Thu','Fri','Sat'].map(x=>`<div class="dh">${x}</div>`).join('');
  for(let i=0;i<42;i++){
    const day=new Date(start.getFullYear(),start.getMonth(),start.getDate()+i);
    const iso=`${day.getFullYear()}-${String(day.getMonth()+1).padStart(2,'0')}-${String(day.getDate()).padStart(2,'0')}`;
    const due=S.mos.filter(x=>x.due===iso&&x.status==='OPEN');
    const today=day.toDateString()===d.toDateString();
    h+=`<div class="cell ${day.getMonth()!==m?'out':''} ${today?'today':''}"><span class="d">${day.getDate()}</span>${due.slice(0,3).map(x=>{const L=x.line?lineOf(x.line):null;return `<span class="chip" style="background:${L?L.color:'#6c7076'}" title="MO ${x.mo} · ${esc(x.item)} · ${x.qty}">${esc(x.item)}</span>`}).join('')}${due.length>3?`<span class="d">+${due.length-3} more</span>`:''}</div>`;
  }
  return h+'</div>';
}
function moTable(rows,opts={}){
  const cols=[['added','Added to sheet'],['mo','MO No.'],['due','Due date'],['qty','Make qty'],['item','Item'],['process','Process'],['mfg','MFG/OUT'],['produced','Produced qty'],['scrap','Scrap qty'],['final','Final qty'],['status','Open/Close'],['line','Line']];
  if(ui.sort){const [k,dir]=ui.sort;rows=[...rows].sort((a,b)=>{const va=k==='final'?a.produced-a.scrap:a[k],vb=k==='final'?b.produced-b.scrap:b[k];return (va>vb?1:va<vb?-1:0)*dir})}
  return `<div class="tblwrap"><table class="xl"><thead><tr>${cols.map(([k,l])=>`<th data-action="sort" data-k="${k}">${l}${ui.sort&&ui.sort[0]===k?(ui.sort[1]>0?' ▲':' ▼'):''}</th>`).join('')}</tr></thead><tbody>
  ${rows.map(m=>`<tr><td>${fmtISO(m.added)}</td><td>${m.mo}</td><td>${fmtISO(m.due)}</td><td>${m.qty}</td><td class="${isLate(m)?'late':''} ${m.hot?'hot':''}">${esc(m.item)}</td><td>${m.process}</td><td>${m.mfg}</td><td>${m.produced}</td><td>${m.scrap}</td><td class="final">${m.produced-m.scrap}</td><td>${m.status}</td><td>${m.line?esc(m.line):'—'}</td></tr>`).join('')||'<tr><td colspan="12">No MOs match.</td></tr>'}
  </tbody></table></div>`;
}
VIEWS.schedule=()=>{
  const n=unreadSched();
  const rows=S.mos.filter(m=>ui.schedFilter==='ALL'||m.status===ui.schedFilter);
  return `
  ${n.length?`<div class="panel" style="border-color:var(--warn)"><div class="row"><div class="grow">${n.map(x=>`<div><b class="num">${fmtT(x.t)}</b> · ${esc(x.text)}</div>`).join('')}</div><button class="btn" data-action="ackSched">Acknowledge</button></div></div>`:''}
  <div class="row"><h1 class="h1 grow">Schedule</h1>
    <div class="seg"><button class="${ui.schedView==='timeline'?'on':''}" data-action="schedView" data-v="timeline">Timeline</button><button class="${ui.schedView==='calendar'?'on':''}" data-action="schedView" data-v="calendar">Calendar</button></div>
    <a class="btn ghost" href="/scheduler/">Plan in the Scheduler</a></div>
  <p class="sub">Run order and times come from the Scheduler. The running MO uses its live progress.</p>
  ${ui.schedView==='timeline'?gantt():calendar()}
  <div class="row"><h2 class="h2 grow">MO list</h2><div class="seg">${['OPEN','CLOSE','ALL'].map(f=>`<button class="${ui.schedFilter===f?'on':''}" data-action="schedFilter" data-v="${f}">${f==='CLOSE'?'Closed':f==='OPEN'?'Open':'All'}</button>`).join('')}</div></div>
  ${moTable(rows)}
  <p class="note">Red item = past due. Yellow item = hot order. Final qty = produced − scrap.</p>`;
};

VIEWS['qc-log']=()=>{
  const list=S.qc.filter(q=>q.line===station).reverse(), fails=list.filter(q=>!q.pass).length;
  const spec=(q,k)=>{const p=part(q.item);const target={len:p.len,wid:p.wid,ph:p.ph,sbl:p.sb,sbr:p.sb}[k];if(q[k]==null)return '<td class="note">—</td>';const ok=Math.abs(q[k]-target)<=p.tol;return `<td class="${ok?'':'oos'}">${Number(q[k]).toFixed(2)}</td>`};
  return `
  <div class="row"><h1 class="h1 grow">QC Log · ${esc(station)}</h1><button class="btn primary" data-action="qcCheck">New QC check</button></div>
  ${(S.autoqc||{})[station]?(()=>{const A=S.autoqc[station];const pct=A.n?Math.round(100*(A.n-A.fails)/A.n):100;
    return `<div class="panel" style="border-color:${A.fails?'var(--warn)':'var(--rule)'}"><div class="row"><h2 class="h2 grow">Machine vision QC · last 8 hours</h2><span class="pill ${A.fails?'warn':'ok'}">${pct}% PASS</span></div>
    <p class="sub" style="margin-top:4px">${A.n.toLocaleString()} measured · ${A.fails} failed · from ${A.sources.map(esc).join(', ')}. These are automatic checks; the operator QC checks below are still due on their own timer.</p>
    ${A.last.length?`<div class="tblwrap" style="margin-top:8px"><table class="xl"><thead><tr><th>Time</th><th>Part</th><th>MO</th><th>Length</th><th>Width</th><th>Pleat ht</th><th>SB L</th><th>SB R</th></tr></thead><tbody>${A.last.map(q=>`<tr><td>${fmtT(q.t)}</td><td>${esc(q.item)}</td><td>${esc(q.mo)}</td>${['len','wid','ph','sbl','sbr'].map(k=>`<td>${q[k]==null?'—':Number(q[k]).toFixed(3)}</td>`).join('')}</tr>`).join('')}</tbody></table></div><p class="note" style="margin-top:4px">Latest failed measurements.</p>`:''}</div>`})():''}
  <div class="grid3">
    <div class="panel stat"><span>Next check</span><b data-live="qcNext">${LIVE.qcNext()}</b></div>
    <div class="panel stat"><span>Checks / fails</span><b>${list.length} / ${fails}</b></div>
    <div class="panel stat"><span>Scrap logged</span><b>${list.reduce((s,q)=>s+q.scrap,0)}</b></div>
    <div class="panel"><div class="stat"><span>Auto QC reminder</span></div>
      <div class="row" style="margin-top:6px"><div class="seg"><button class="${S.settings.autoQC?'on':''}" data-action="autoQC" data-v="1">On</button><button class="${S.settings.autoQC?'':'on'}" data-action="autoQC" data-v="0">Off</button></div>
      <select id="qcEvery" data-change="qcEvery" style="width:auto" ${isAdmin()?'':'disabled'}>${[15,30,45,60].map(v=>`<option ${S.settings.qcEvery==v?'selected':''} value="${v}">every ${v} min</option>`).join('')}</select></div>
      ${isAdmin()?'':'<p class="note">Shift lead can change these.</p>'}</div>
  </div>
  <div class="tblwrap"><table class="xl"><thead><tr><th>Time</th><th>Line</th><th>MO</th><th>Item</th><th>Length</th><th>Width</th><th>Pleat ht</th><th>Side band L</th><th>Side band R</th><th>Glue</th><th>Cut</th><th>Scrap 30 min</th><th>By</th><th>Result</th></tr></thead><tbody>
  ${list.map(q=>`<tr><td>${fmtT(q.t)}</td><td>${esc(q.line)}</td><td>${q.mo||''}</td><td>${esc(q.item)}</td>${spec(q,'len')}${spec(q,'wid')}${spec(q,'ph')}${spec(q,'sbl')}${spec(q,'sbr')}<td>${q.glue}</td><td>${q.cut}</td><td>${q.scrap}</td><td>${esc(q.by)}</td><td>${q.pass?'<span class="pill ok">PASS</span>':'<span class="pill bad">FAIL</span>'}</td></tr>`).join('')||'<tr><td colspan="14">No checks yet this shift.</td></tr>'}
  </tbody></table></div>`;
};

VIEWS.maintenance=()=>{
  const list=[...S.tickets].reverse();
  return `<div class="row"><h1 class="h1 grow">Maintenance</h1><button class="btn primary" data-action="maintTicket">New ticket</button></div>
  <div class="tblwrap"><table class="xl"><thead><tr><th>Opened</th><th>Machine</th><th>Issue</th><th>Priority</th><th>Notes</th><th>Pictures</th><th>By</th><th>Status</th><th></th></tr></thead><tbody>
  ${list.map(t=>{const ph=t.photos||[];return `<tr><td>${fmtD(t.t)} ${fmtT(t.t)}</td><td>${esc(t.machine)}</td><td>${esc(t.type)}</td><td>${t.priority==='High'?'<span class="pill bad">High · line down</span>':t.priority}</td><td class="l">${esc(t.notes)}</td>
    <td class="l"><div class="thumbs sm">${ph.map((p,i)=>`<button class="thumb" data-action="photoView" data-id="${p.id}" aria-label="Picture ${i+1} on this ticket"><img src="/api/hmi/ticket-photo/${p.id}" alt="" loading="lazy"></button>`).join('')}</div>
      ${ph.length<PHOTO_LIMIT?`<label class="btn sm ghost" style="margin-top:4px">${IC.camera}Add picture<input type="file" accept="image/*" multiple hidden data-change="ticketPhotoAdd" data-id="${t.id}"></label>`:''}</td>
    <td>${esc(t.by)}</td><td>${t.status}</td><td>${t.status!=='Closed'?`<button class="btn" data-action="ticketStatus" data-id="${t.id}">${t.status==='Open'?'Start work':'Close'}</button>`:''}</td></tr>`}).join('')||'<tr><td colspan="9">No tickets.</td></tr>'}
  </tbody></table></div>`;
};

// Errors and alarms the machine reported during a stop (or up to 2 minutes before it)
function errChips(list,max=4){
  if(!list||!list.length)return '';
  const one=x=>`<span class="errchip" title="${esc((x.text||x.code)+' · '+fmtT(x.start)+(x.end&&x.end!==x.start?' – '+fmtT(x.end):x.end?'':' – still showing'))}">${esc(x.code||x.text)}${x.code&&x.text?` <span>${esc(x.text)}</span>`:''}</span>`;
  return `<div class="errchips">${list.slice(0,max).map(one).join('')}${list.length>max?`<span class="note">+${list.length-max} more</span>`:''}</div>`;
}
function errLog(d){
  modal(`Machine errors · ${esc(d.line)} ${fmtT(d.start)} – ${fmtT(d.end)}`,`<p class="sub">What ${esc(d.line)} reported during this stop (and in the 2 minutes before it).</p>
    <div class="tblwrap" style="margin-top:10px"><table class="xl"><thead><tr><th>Time</th><th>Code</th><th>Message</th></tr></thead><tbody>
    ${(d.errors||[]).map(x=>`<tr><td>${fmtT(x.start)}${x.end&&x.end!==x.start?` – ${fmtT(x.end)}`:x.end?'':' – still showing'}</td><td><b>${esc(x.code)}</b></td><td class="l">${esc(x.text)}</td></tr>`).join('')}</tbody></table></div>`);
}
VIEWS.downtime=()=>{
  const evs=shiftDowns(), sh=S.shift, older=S.downtime.filter(d=>d.shift!==sid()&&!d.reason&&d.line===station);
  const row=(d,i)=>`<tr class="${d.reason?'':'unset'}"><td><b>${i!=null?String.fromCharCode(65+i%26):''}</b></td><td>${fmtT(d.start)} – ${fmtT(d.end)}</td><td>${d.mins}</td><td>${esc(d.line)}</td><td>${d.mo||''}</td>
    <td class="l">${(d.errors||[]).length?`<button class="linkbtn" data-action="errLog" data-id="${d.id}" aria-label="Machine errors during this stop">${errChips(d.errors,3)}</button>`:'<span class="note">—</span>'}</td>
    <td>${reasonCell(d)}</td>
    <td class="l"><input type="text" id="dn-${d.id}" data-change="dNotes" data-id="${d.id}" value="${esc(d.notes)}" placeholder="What happened?"></td></tr>`;
  const reasons={};S.downtime.filter(d=>d.shift===sid()).forEach(d=>{const k=d.reason||'Not entered';reasons[k]=(reasons[k]||0)+d.mins});
  const max=Math.max(1,...Object.values(reasons));
  return `<div class="row"><h1 class="h1 grow">Downtime · ${esc(station)}</h1><span class="sub">Stops of ${C().rules.minDown} minutes or more are logged automatically; breaks, lunch and the first ${C().rules.startGrace||0} minutes of a shift are not downtime. A reason is required once stops without one add up to ${C().rules.reasonAfter||0} minutes.${canOverride()?' As maintenance or a shift lead you can type your own reason or change one already entered.':''}</span></div>
  ${sh&&sh.stopStart?`<div class="panel" style="border-color:var(--bad)"><b>Line stopped since ${fmtT(sh.stopStart)}</b> · ${Math.round((now()-sh.stopStart)/MIN)} min so far. It will appear below when the line restarts.</div>`:''}
  ${sh&&sh.stopStart&&curErrors(station).length?`<div class="panel" style="border-color:var(--warn)"><b>The machine is reporting:</b>${errChips(curErrors(station),6)}</div>`:''}
  <div class="tblwrap"><table class="xl"><thead><tr><th></th><th>Time</th><th>Min</th><th>Line</th><th>MO</th><th>Machine errors</th><th>Reason</th><th>Notes</th></tr></thead><tbody>
  ${evs.map(row).join('')}${older.map(d=>row(d,null)).join('')}${!evs.length&&!older.length?'<tr><td colspan="8">No stops this shift.</td></tr>':''}</tbody></table></div>
  ${Object.keys(reasons).length?`<div class="panel"><h2 class="h2">This shift by reason</h2><div class="bars" style="margin-top:10px">${Object.entries(reasons).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`<div class="bar"><span>${esc(k)}</span><i style="width:${v/max*100}%;${k==='Not entered'?'':'background:var(--ink-3)'}"></i><span class="num">${v} min</span></div>`).join('')}</div></div>`:''}`;
};
VIEWS.status=()=>{
  const g=S.settings.grafana;
  const panels=g.on&&g.url&&g.uid?[['Pleater speed',g.ppm],['Uptime',g.uptime],['Count',g.count]].filter(p=>p[1]).map(([t,p])=>`<div><div class="note">${t} · ${esc(station)}</div><iframe src="${esc(grafanaSrc(p))}" title="${t} (Grafana)" loading="lazy"></iframe></div>`).join(''):'';
  const pill=(l,sh,on)=>!on?(l.active===false?'<span class="pill bad">OUT OF SERVICE</span>':'<span class="pill idle">IDLE</span>'):sh.state==='running'?'<span class="pill ok">RUNNING</span>':sh.state==='changeover'?'<span class="pill warn">CHANGEOVER</span>':'<span class="pill bad">STOPPED</span>';
  return `<div class="row"><h1 class="h1 grow">Status</h1>${g.on&&g.url&&g.uid?`<a class="btn ghost" href="${esc(g.url.replace(/\/$/,'')+'/d/'+g.uid)}" target="_blank" rel="noopener">Open dashboard in Grafana</a>`:`<button class="btn ghost" data-action="go" data-page="setup">Connect Grafana</button>`}</div>
  <div class="grid2" data-r="lines">${S.lines.map(l=>{const R=S.runs[l.id]||{},sh=R.shift,on=!!(sh&&sh.active),st=E.shiftStats(S,l.id),mo=on?E.curMO(S,l.id):null,nxt=S.mos.filter(m=>m.line===l.id&&m.status==='OPEN'&&!(mo&&String(mo.id)===String(m.id))).sort((a,b)=>a.seq-b.seq)[0],f=R.feed||{},h=R.hist||[];
    return `<div class="panel linecard"><div class="swatch" style="background:${l.color}"></div><div style="min-width:0"><div class="row"><h2 class="h2 grow">${esc(l.id)}${l.id===station?' <span class="note">(this screen)</span>':''}</h2>${pill(l,sh,on)}</div>
    <dl class="kv" style="margin-top:8px"><dt>Rated</dt><dd class="num">${l.rated} P/m</dd>
    <dt>Machine data</dt><dd>${{demo:'Demo replay',http:'Read from URL',push:'Pushed to the server',influx:'InfluxDB query',db:'Database table',none:'Not connected'}[(l.feed||{}).type]||'—'}${f.t?(f.ok?' · <span class="pill ok">OK</span>':` · <span class="pill bad" title="${esc(f.err)}">NO DATA</span>`):''}</dd>
    ${f.recipe?`<dt>Recipe</dt><dd>${esc(f.recipe)}</dd>`:''}
    ${(l.feed.errors||{}).type?`<dt>Machine errors</dt><dd>${curErrors(l.id).length?errChips(curErrors(l.id),3):'<span class="note">None now</span>'}${(R.errLog||{}).ok===false?` <span class="pill warn" title="${esc(R.errLog.err)}">ALARM LOG NOT READ</span>`:''}</dd>`:''}
    ${(l.feed.uptime||{}).type?`<dt>Run signal</dt><dd>${f.runErr?`<span class="pill warn" title="${esc(f.runErr)}">NOT READ</span> <span class="note">uptime uses the speed</span>`:f.run===true?'<span class="pill ok">RUNNING</span>':f.run===false?'<span class="pill bad">STOPPED</span>':'—'}</dd>`:''}
    ${on?`<dt>Operator</dt><dd>${esc(sh.userName||sh.user)}</dd><dt>MO</dt><dd>${mo?`${mo.mo} · ${esc(mo.item)}`:'—'}</dd><dt>Speed now</dt><dd class="num">${h.length?h[h.length-1].ppm:0} P/m</dd><dt>Shift count</dt><dd class="num">${sh.count.toLocaleString()}</dd><dt>Uptime</dt><dd class="num">${st.uptime}% · ${st.down} min down</dd>`:''}
    <dt>Next up</dt><dd>${nxt?`${esc(nxt.item)} · MO ${nxt.mo}`:'Nothing scheduled'}</dd></dl></div></div>`}).join('')}</div>
  <div class="panel"><div class="row"><h2 class="h2 grow">Performance over time · ${esc(station)}</h2><span class="note">${panels?'Live panels from Grafana':'Built-in chart · connect Grafana in Admin › Connections for history across shifts and lines'}</span></div>
    <div style="margin-top:10px">${panels?`<div class="gpanels">${panels}</div>`:`<div class="chartcard" data-r="chart2" style="height:260px"><canvas data-chart="ppm"></canvas></div>`}</div></div>
  <div class="panel" data-r="conn"><h2 class="h2">Server</h2><dl class="kv" style="margin-top:8px">
    ${SRV.server?`<dt>Connection</dt><dd>${online?'<span class="pill ok">CONNECTED</span>':'<span class="pill bad">OFFLINE · RETRYING</span>'} · version ${esc(SRV.version)}</dd>
    <dt>Records</dt><dd>Saved to ${SRV.store==='mariadb'?'MariaDB':'files on the server'} ${SRV.storeOk?'<span class="pill ok">OK</span>':`<span class="pill bad">NOT SAVING</span> ${esc(SRV.storeErr)}`}${SRV.pending?` · ${SRV.pending} waiting`:''}</dd>
    <dt>Asset database</dt><dd>${SRV.assets?'Set up':'Not set up'}</dd><dt>Paycom</dt><dd>${SRV.paycom?'Set up':'Not set up'}</dd>`
    :`<dt>Mode</dt><dd>Demo in this browser. Nothing is shared with other screens until the HMI server is installed.</dd>`}
    <dt>Grafana</dt><dd>${g.on&&g.url?esc(g.url):'Not connected'}</dd></dl></div>`;
};

function s2kPanel(s,adm,dis){
  const db=s.s2kSource==='db', K=ui.s2k=ui.s2k||{};
  let h=`<div class="panel"><h2 class="h2">S2K schedule</h2>
    <div class="grid2" style="margin-top:10px">
      <label class="f">Read open MOs from<select id="s2kSource" data-change="set" data-k="s2kSource" ${dis}><option value="url" ${db?'':'selected'}>An export URL (JSON)</option><option value="db" ${db?'selected':''}>The S2K database (read-only)</option></select></label>
      <label class="f">Pull automatically every (min, 0 = off)<input type="number" min="0" id="s2kEvery" data-change="set" data-k="s2kEvery" value="${s.s2kEvery}" ${dis}></label>
      ${db?'':`<label class="f">Open MO export URL (JSON)<input type="url" id="s2kUrl" data-change="set" data-k="s2kUrl" value="${esc(s.s2kUrl)}" placeholder="http://s2k-bridge.plant.local/open-mos" ${dis}></label>`}
    </div>`;
  if(!db){
    h+=`<p class="note" style="margin-top:8px">Expects a list like <code>[{"mo":309300,"item":"MG4062HP","qty":2400,"due":"2026-04-08","process":"BARB","mfg":"MFGOUT"}]</code>. New MOs land in Unscheduled; existing ones are left alone.</p>`;
  }else if(!SRV.s2kdb){
    h+=`<p class="sub" style="margin-top:8px;color:var(--warn-ink)">The S2K database isn't set up on the server yet. Run the installer again and answer yes to "S2K database". It only tests the login; nothing is created in S2K.</p>`;
  }else{
    if(adm&&K.sql===undefined&&!K.loading){K.loading=true;API.cmd('dbQueries',{}).then(q=>{const sv=q.ok&&q.data.s2k_mos;K.saved=sv||null;K.sql=sv?sv.sql:'SELECT mo_number AS mo, item_number AS item, qty_open AS qty, due_date AS due, process_code AS process\nFROM open_orders\nWHERE status = \'OPEN\'';K.loading=false;render(true)})}
    h+=`<p class="sub" style="margin-top:8px">The server runs one SELECT against the S2K database and adds MOs it hasn't seen. The connection is read-only: the suite can't change, lock or add anything in S2K. Name the columns <code>mo</code>, <code>item</code>, <code>qty</code>, and optionally <code>due</code>, <code>process</code>, <code>mfg</code>, <code>hot</code> (use AS).</p>`;
    if(adm){
      h+=`<label class="f" style="margin-top:10px">SQL<textarea id="s2kSql" data-change="s2kSql" spellcheck="false" style="font-family:var(--f-mono);font-size:13px;min-height:110px">${esc(K.sql||'')}</textarea></label>
      <div class="row" style="margin-top:8px"><button class="btn" data-action="s2kTables">${K.schema?'Hide tables':'Show tables'}</button><button class="btn" data-action="s2kTest">Run test</button><button class="btn primary" data-action="s2kSave">Save on server</button>${K.saved?`<span class="pill ok">SAVED</span><span class="note">${esc(K.saved.savedBy||'')} ${K.saved.savedAt?fmtD(K.saved.savedAt):''}</span>`:'<span class="pill idle">NOT SAVED</span>'}</div>
      ${K.msg?`<p class="note" style="margin-top:6px;color:${K.err?'var(--bad)':'inherit'}">${esc(K.msg)}</p>`:''}
      ${K.schema?`<div class="tblwrap" style="margin-top:8px;max-height:260px"><table class="xl"><thead><tr><th>Table</th><th>Columns</th></tr></thead><tbody>${K.schema.tables.map(t=>`<tr><td class="l"><b>${esc(t.name)}</b></td><td class="l note">${t.columns.map(c=>esc(c.name)).join(', ')}</td></tr>`).join('')}</tbody></table></div>`:''}
      ${K.schema?s2kBuilder(K):''}
      ${K.res?rowsTable(K.res.rows.slice(0,20),K.res.columns):''}`;
    }else h+='<p class="note" style="margin-top:8px">Log in as a shift lead to set up the S2K query.</p>';
  }
  return h+(adm?'<div class="row" style="margin-top:8px"><button class="btn" data-action="pullS2K">Pull MOs now</button></div>':'')+'</div>';
}
const S2K_ROLES=[['mo','MO number',1],['item','Item / part number',1],['qty','Quantity open',1],['due','Due date',0],['process','Process code',0],['mfg','MFG location',0],['hot','Hot flag',0]];
function s2kBuilder(K){
  const B=K.bld=K.bld||{}, tables=K.schema.tables, cols=((tables.find(t=>t.name===B.table)||{}).columns||[]).map(c=>c.name);
  const sel=(id,f,val,opts)=>`<select id="${id}" data-change="s2kBld" data-f="${f}"><option value="">—</option>${opts.map(o=>`<option ${o===val?'selected':''}>${esc(o)}</option>`).join('')}</select>`;
  return `<details style="margin-top:10px" ${B.table?'open':''}><summary><b>Build the query for me</b> <span class="note">pick a table and the columns; the SQL above is written for you</span></summary>
    <div class="grid3" style="margin-top:8px"><label class="f">Table *${sel('s2b-table','table',B.table,tables.map(t=>t.name))}</label>
    ${S2K_ROLES.map(([k,l,req])=>`<label class="f">${l}${req?' *':''}${sel('s2b-'+k,k,B[k],cols)}</label>`).join('')}
    <label class="f">Only rows where column${sel('s2b-fcol','fcol',B.fcol,cols)}</label>
    <label class="f">equals (e.g. OPEN)<input type="text" id="s2b-fval" value="${esc(B.fval||'')}" data-change="s2kBld" data-f="fval" autocomplete="off" spellcheck="false"></label></div>
    <div class="row" style="margin-top:8px"><button class="btn" data-action="s2kBuild">Write the SQL</button><span class="note">Then Run test, and Save on server.</span></div></details>`;
}
function s2kBuildSQL(B){
  const q=c=>'`'+String(c).replace(/`/g,'``')+'`';
  if(!B.table) throw new Error('Pick a table.');
  const miss=S2K_ROLES.filter(([k,,req])=>req&&!B[k]).map(r=>r[1]); if(miss.length) throw new Error('Pick a column for: '+miss.join(', '));
  const sel=S2K_ROLES.filter(([k])=>B[k]).map(([k])=>`${q(B[k])} AS ${k}`).join(',\n       ');
  let w=''; if(B.fcol){ if(!String(B.fval||'').trim()) throw new Error('Type the value to keep, or clear the "only rows where" column.');
    w=`\nWHERE ${q(B.fcol)} = '${String(B.fval).trim().replace(/\\/g,'\\\\').replace(/'/g,"''")}'`}
  return `SELECT ${sel}\nFROM ${q(B.table)}${w}`;
}
function sourcesPanel(adm){
  if(!SRV.server||!adm)return '';
  if(!ui.srcs){if(!ui.srcLoading){ui.srcLoading=true;API.cmd('srcList',{}).then(r=>{ui.srcLoading=false;if(r.ok){ui.srcs=r.data;render(true)}})}
    return `<div class="panel"><h2 class="h2">Incoming data</h2><p class="note" style="margin-top:6px">Loading…</p></div>`}
  const E=ui.srcEdit, lines=S.lines.map(l=>l.id);
  const list=ui.srcs.length?`<div class="tblwrap" style="margin-top:10px"><table class="xl"><thead><tr><th>Source</th><th>Data</th><th>Line</th><th>How</th><th>Last data</th><th>Received</th><th></th></tr></thead><tbody>
    ${ui.srcs.map(x=>`<tr><td class="l"><b>${esc(x.name)}</b>${x.enabled?'':' <span class="pill idle">OFF</span>'}</td><td>${x.kind==='qc'?'QC measurements':'Other readings'}</td><td>${esc(x.line||'from the data')}</td>
      <td class="l">${x.mode==='push'?'Sends to the server':`Collected every ${x.every}s<br><span class="note" style="word-break:break-all">${esc(x.url)}</span>`}</td>
      <td class="l">${x.lastAt?`<span class="pill ${x.lastOk?'ok':'bad'}">${x.lastOk?'OK':'PROBLEM'}</span> <span class="note">${fmtD(x.lastAt)} ${fmtT(x.lastAt)} · ${esc(x.lastMsg)}</span>`:'<span class="pill idle">NOTHING YET</span>'}</td>
      <td>${Number(x.received).toLocaleString()}</td>
      <td style="white-space:nowrap"><button class="btn sm" data-action="srcEdit" data-id="${x.id}">Edit</button> <button class="btn sm" data-action="srcTest" data-id="${x.id}">Test</button>${x.mode==='push'?` <button class="btn ghost sm" data-action="srcKey" data-id="${x.id}">New key</button>`:''} <button class="btn ghost sm" data-action="srcDel" data-id="${x.id}">Remove</button></td></tr>`).join('')}
    </tbody></table></div>`:'<p class="note" style="margin-top:8px">No data sources yet.</p>';
  let form='';
  if(E){
    const fields=(E.fields||[]);const dl=`<datalist id="srcFields">${fields.map(f=>`<option value="${esc(f)}">`).join('')}</datalist>`;
    const mp=k=>E.mapping[k]||'';
    const mf=(k,label,ph)=>`<label class="f">${label}<input type="text" list="srcFields" id="sm-${k}" value="${esc(mp(k))}" data-change="srcMap" data-k="${k}" placeholder="${esc(ph||'field name, e.g. result.length')}" autocomplete="off" spellcheck="false"></label>`;
    form=`<div class="panel" style="background:var(--well-3);margin-top:10px">${dl}<h3 class="h2" style="font-size:18px">${E.id?'Edit data source':'New data source'}</h3>
      <div class="grid3" style="margin-top:8px">
        <label class="f">Name (part of its address)<input type="text" id="se-name" value="${esc(E.name)}" data-change="srcF" data-f="name" placeholder="vision-barbara" autocomplete="off" spellcheck="false"></label>
        <label class="f">What it sends<select id="se-kind" data-change="srcF" data-f="kind"><option value="qc" ${E.kind==='qc'?'selected':''}>QC measurements (machine vision)</option><option value="readings" ${E.kind==='readings'?'selected':''}>Other readings</option></select></label>
        <label class="f">Line<select id="se-line" data-change="srcF" data-f="line"><option value="">Named in the data</option>${lines.map(l=>`<option ${E.line===l?'selected':''}>${esc(l)}</option>`).join('')}</select></label>
        <label class="f">How the data arrives<select id="se-mode" data-change="srcF" data-f="mode"><option value="push" ${E.mode==='push'?'selected':''}>It sends to the server</option><option value="poll" ${E.mode==='poll'?'selected':''}>The server collects it from an address</option></select></label>
        ${E.mode==='poll'?`<label class="f">Address to collect from (JSON)<input type="url" id="se-url" value="${esc(E.url)}" data-change="srcF" data-f="url" placeholder="http://192.168.100.60/api/measurements"></label>
        <label class="f">Collect every (seconds)<input type="number" min="10" id="se-every" value="${E.every}" data-change="srcF" data-f="every"></label>`:''}
        <label class="chk"><input type="checkbox" id="se-on" ${E.enabled!==false?'checked':''} data-change="srcF" data-f="enabled"> Turned on</label>
      </div>
      <h3 class="h2" style="font-size:16px;margin-top:12px">Which field holds what</h3>
      <p class="note">Type the field name from the system's JSON (dots for nested fields). ${fields.length?'Fields seen in the last data received are suggested as you type.':'After the first data arrives (or a Test), its fields are suggested here.'} Leave blank what it doesn't send.</p>
      <div class="grid3" style="margin-top:6px">
        ${mf('time','Time','e.g. timestamp (blank = time received)')}${E.line?'':mf('line','Line name','e.g. station')}
        ${E.kind==='qc'?`${mf('part','Part number','blank = the part running on the line')}${mf('mo','MO','blank = the MO running on the line')}
          ${mf('len','Length')}${mf('wid','Width')}${mf('ph','Pleat height')}${mf('sbl','Side band height L')}${mf('sbr','Side band height R')}
          ${mf('passed','Pass / fail','blank = checked against the drawing specs')}
          <label class="f">Measurements are in<select id="sm-units" data-change="srcMap" data-k="units"><option value="in" ${mp('units')!=='mm'?'selected':''}>inches</option><option value="mm" ${mp('units')==='mm'?'selected':''}>millimetres (converted to inches)</option></select></label>`
          :`<label class="f">Number fields to keep (comma separated, blank = all)<input type="text" id="sm-fields" value="${esc((E.mapping.fields||[]).join(', '))}" data-change="srcMap" data-k="fields" autocomplete="off" spellcheck="false"></label>`}
        ${E.mode==='poll'?mf('id','Record id or sequence (so nothing is read twice)','blank = use the time'):''}
      </div>
      <div class="row" style="margin-top:10px"><button class="btn primary" data-action="srcSave">Save</button><button class="btn ghost" data-action="srcCancel">Cancel</button><span class="note" id="srcErr" style="color:var(--bad)"></span></div></div>`;
  }
  const K=ui.srcKey?`<div class="panel" style="border-color:var(--accent);margin-top:10px"><h3 class="h2" style="font-size:18px">Set up ${esc(ui.srcKey.name)} to send its data</h3>
      <p class="sub" style="margin-top:4px">In the other system, send JSON (one object or a list) by HTTP POST to:</p>
      <p class="num" style="margin-top:6px;user-select:all;word-break:break-all"><b>${esc(location.origin)}/api/ingest/${esc(ui.srcKey.name)}</b></p>
      <p class="sub" style="margin-top:6px">with these headers (the key is shown only now; copy it):</p>
      <p class="num" style="margin-top:4px;user-select:all;word-break:break-all">Content-Type: application/json<br>X-Source-Key: <b>${esc(ui.srcKey.key)}</b></p>
      <div class="row" style="margin-top:8px"><button class="btn sm" data-action="srcCopyKey">Copy key</button><button class="btn ghost sm" data-action="srcKeyDone">Done</button></div></div>`:'';
  const T=ui.srcTest?`<div class="panel" style="margin-top:10px"><h3 class="h2" style="font-size:18px">Test: ${esc(ui.srcTest.name)}</h3><p class="note">${ui.srcTest.count} record(s) found; nothing was stored. How the first ones would be read:</p>
      ${ui.srcTest.preview.map(r=>r.ok?`<div class="note" style="margin-top:4px">✓ ${esc(r.line)} · ${esc(new Date(r.t).toLocaleString())} · ${r.kind==='qc'?`${esc(r.part||'(part from the line)')} ${['len','wid','ph','sbl','sbr'].map(k=>r.vals[k]==null?'':`${k} ${r.vals[k]}`).join(' ')}${r.passed==null?' · pass/fail from the specs':r.passed?' · PASS':' · FAIL'}`:esc(JSON.stringify(r.data))}</div>`:`<div class="note" style="margin-top:4px;color:var(--bad)">✕ ${esc(r.error)}</div>`).join('')}
      <div class="row" style="margin-top:8px"><button class="btn ghost sm" data-action="srcTestDone">Close</button></div></div>`:'';
  return `<div class="panel"><div class="row"><h2 class="h2 grow">Incoming data</h2>${E?'':'<button class="btn primary" data-action="srcNew">Add data source</button>'}</div>
    <p class="sub" style="margin-top:4px">Systems that send data to the suite, such as a machine vision QC system. QC measurements show on the line's QC Log and in Reports; other readings are kept for Reports and Grafana (table hmi_inbound). Sources must be on the plant network.</p>
    ${list}${K}${T}${form}</div>`;
}
function reasonCell(d,prefix='dr'){
  const list=C().reasons, custom=d.reason&&!list.includes(d.reason), lock=d.reason&&!canOverride()&&ME&&d.by&&d.by!==ME.name;
  if(lock)return `<span>${esc(d.reason)}</span><br><span class="note">by ${esc(d.by)}</span>`;
  return `<select id="${prefix}-${d.id}" data-change="dReason" data-id="${d.id}" aria-label="Reason"><option value="">Choose a reason…</option>${list.map(r=>`<option ${d.reason===r?'selected':''}>${esc(r)}</option>`).join('')}${custom?`<option selected>${esc(d.reason)}</option>`:''}${canOverride()?'<option value="__other">Other… (type a reason)</option>':''}</select>${d.reason&&d.by?`<br><span class="note">by ${esc(d.by)}</span>`:''}`;
}
function openDownPrompt(){
  const list=needReason();
  if(!list.length){if(ui.modal==='downPrompt')closeModal();return}
  ui.downPromptAt=Date.now();
  const total=unexplainedMins();
  modal('Why did the line stop?',`<p class="sub">${esc(station)} has ${list.length} stop${list.length>1?'s':''} adding up to <b>${total} min</b> without a reason. Pick a reason for each one${canOverride()?', or choose Other to type your own':''}.</p>
    ${user()?'':'<p class="note" style="color:var(--warn)">Log in to enter reasons.</p>'}
    <div class="tblwrap" style="margin-top:10px"><table class="xl"><thead><tr><th>Time</th><th>Min</th><th>MO</th><th>Machine errors</th><th>Reason</th></tr></thead><tbody>
    ${list.map(d=>`<tr><td>${fmtT(d.start)} – ${fmtT(d.end)}</td><td>${d.mins}</td><td>${esc(d.mo||'')}</td><td class="l">${errChips(d.errors,3)||'<span class="note">—</span>'}</td><td>${reasonCell(d,'dp')}</td></tr>`).join('')}</tbody></table></div>
    <p class="note" style="margin-top:8px">The reasons list is kept in Admin › Downtime reasons. ${canOverride()?'':'If the right reason isn\'t listed, ask maintenance or your shift lead.'}</p>`,
    `${user()?'':'<button class="btn primary" data-action="downLogin">Log in</button>'}${canOverride()?'<button class="btn ghost" data-action="close">Later</button>':''}`);
  ui.modal='downPrompt';
}
function alertsBody(){
  if(!SRV.server)return `<div class="panel" style="border-color:var(--warn)">Alerts are sent by the server. This preview has no server.</div>`;
  if(!isAdmin())return `<div class="panel"><h2 class="h2">Downtime alerts</h2><p class="sub" style="margin-top:6px">Text or email people when a line is down. Log in as a shift lead to set it up.</p></div>`;
  const I=ui.alerts;
  if(!I){if(!ui.alertLoading){ui.alertLoading=true;API.cmd('alertInfo',{}).then(r=>{ui.alertLoading=false;if(r.ok){ui.alerts=r.data;render(true)}})}return '<div class="panel"><p class="note">Loading…</p></div>'}
  const lines=S.lines.map(l=>l.id),E=ui.alertEdit,mf=ui.mailForm=ui.mailForm||{host:I.smtp.host,port:I.smtp.port,security:I.smtp.security,user:I.smtp.user,password:'',from:I.smtp.from,internet:I.smtp.internet},
    tf=ui.smsForm=ui.smsForm||{sid:I.sms.sid,token:'',from:I.sms.from},msg=k=>ui.alertMsg&&ui.alertMsg[k]?`<p style="margin-top:6px;color:${ui.alertMsg[k].ok?'var(--ok)':'var(--bad)'}">${esc(ui.alertMsg[k].text)}</p>`:'';
  const people=I.contacts.length?`<div class="tblwrap" style="margin-top:10px"><table class="xl"><thead><tr><th>Name</th><th>Email</th><th>Mobile</th><th>Lines</th><th>Tell them after</th><th>When it runs again</th><th></th></tr></thead><tbody>
    ${I.contacts.map(c=>`<tr><td class="l"><b>${esc(c.name)}</b>${c.active?'':' <span class="pill idle">OFF</span>'}</td><td class="l">${esc(c.email||'—')}</td><td>${esc(c.phone||'—')}</td><td class="l">${c.lines.length?c.lines.map(esc).join(', '):'All lines'}</td>
      <td>${c.downAfter} min down</td><td>${c.notifyBack?'Tell them':'—'}</td>
      <td style="white-space:nowrap"><button class="btn sm" data-action="alertEdit" data-id="${c.id}">Edit</button> <button class="btn sm" data-action="alertTest" data-id="${c.id}">Send a test</button> <button class="btn ghost sm" data-action="alertDel" data-id="${c.id}">Remove</button></td></tr>`).join('')}</tbody></table></div>`:'<p class="note" style="margin-top:8px">Nobody on the list yet.</p>';
  const form=E?`<div class="panel" style="background:var(--well-3);margin-top:10px"><h3 class="h2" style="font-size:18px">${E.id?'Edit person':'Add a person'}</h3>
    <div class="grid3" style="margin-top:8px">
      <label class="f">Name<input type="text" id="ae-name" value="${esc(E.name)}" data-change="alertF" data-f="name"></label>
      <label class="f">Email (or a carrier's email-to-text address)<input type="email" id="ae-email" value="${esc(E.email)}" data-change="alertF" data-f="email" autocomplete="off"></label>
      <label class="f">Mobile number (texts through Twilio)<input type="tel" id="ae-phone" value="${esc(E.phone)}" data-change="alertF" data-f="phone" placeholder="555-123-4567" autocomplete="off"></label>
      <label class="f">Tell them when a line has been down for (min)<input type="number" min="1" id="ae-after" value="${E.downAfter}" data-change="alertF" data-f="downAfter"></label>
      <label class="chk"><input type="checkbox" id="ae-back" ${E.notifyBack?'checked':''} data-change="alertF" data-f="notifyBack"> Also tell them when it runs again</label>
      <label class="chk"><input type="checkbox" id="ae-on" ${E.active?'checked':''} data-change="alertF" data-f="active"> Turned on</label>
    </div>
    <div style="margin-top:8px"><span class="note">Lines (none ticked = every line):</span><div class="row" style="flex-wrap:wrap;gap:12px;margin-top:4px">${lines.map(l=>`<label class="chk"><input type="checkbox" data-change="alertLine" data-line="${esc(l)}" ${E.lines.includes(l)?'checked':''}> ${esc(l)}</label>`).join('')}</div></div>
    <div class="row" style="margin-top:10px"><button class="btn primary" data-action="alertSave">Save</button><button class="btn ghost" data-action="alertCancel">Cancel</button><span class="note" id="alertErr" style="color:var(--bad)"></span></div></div>`:'';
  return `<div class="panel"><div class="row"><h2 class="h2 grow">Downtime alerts</h2>${E?'':'<button class="btn primary" data-action="alertNew">Add a person</button>'}</div>
    <p class="sub" style="margin-top:4px">People get one message when a line has been down for their number of minutes. Breaks, lunch and start-up time don't count. Each stop is reported once, and again when the line runs (if ticked).</p>
    ${people}${msg('people')}${form}</div>
  <div class="panel"><div class="row"><h2 class="h2 grow">Email (your mail server)</h2>${I.smtp.on?'<span class="pill ok">SET UP</span>':'<span class="pill idle">NOT SET UP</span>'}</div>
    <div class="grid3" style="margin-top:8px">
      <label class="f">Mail server (SMTP)<input type="text" id="am-host" value="${esc(mf.host)}" data-change="mailF" data-f="host" placeholder="mail.yourcompany.local" autocomplete="off"></label>
      <label class="f">Port<input type="number" id="am-port" value="${esc(mf.port)}" data-change="mailF" data-f="port"></label>
      <label class="f">Security<select id="am-sec" data-change="mailF" data-f="security">${[['starttls','STARTTLS (port 587)'],['ssl','SSL/TLS (port 465)'],['none','None (port 25, inside the plant only)']].map(([v,l])=>`<option value="${v}" ${mf.security===v?'selected':''}>${l}</option>`).join('')}</select></label>
      <label class="f">From address<input type="email" id="am-from" value="${esc(mf.from)}" data-change="mailF" data-f="from" placeholder="plant-alerts@yourcompany.com"></label>
      <label class="f">User name (if the server needs a login)<input type="text" id="am-user" value="${esc(mf.user)}" data-change="mailF" data-f="user" autocomplete="off"></label>
      <label class="f">Password<input type="password" id="am-pw" value="${esc(mf.password)}" data-change="mailF" data-f="password" autocomplete="new-password" placeholder="${I.smtp.hasPassword?'Leave blank to keep the saved one':''}"></label>
      <label class="chk"><input type="checkbox" id="am-net" ${mf.internet?'checked':''} data-change="mailF" data-f="internet"> The mail server is on the internet (for example Office 365). This allows one outside connection, for email only.</label>
    </div>
    <div class="row" style="margin-top:8px"><button class="btn primary" data-action="mailSave">Save</button>${I.smtp.on?'<button class="btn ghost" data-action="mailRemove">Remove</button>':''}</div>${msg('smtp')}</div>
  <div class="panel"><div class="row"><h2 class="h2 grow">Text messages (Twilio)</h2>${I.sms.on?'<span class="pill ok">SET UP</span>':'<span class="pill idle">NOT SET UP</span>'}</div>
    <p class="sub" style="margin-top:4px">Texts go through a Twilio account, a cloud service, so this is an internet connection, used only when set up here. Without Twilio, you can still text people by using their carrier's email-to-text address as their email.</p>
    <div class="grid3" style="margin-top:8px">
      <label class="f">Account SID<input type="text" id="at-sid" value="${esc(tf.sid)}" data-change="smsF" data-f="sid" autocomplete="off" spellcheck="false"></label>
      <label class="f">Auth token<input type="password" id="at-tok" value="${esc(tf.token)}" data-change="smsF" data-f="token" autocomplete="new-password" placeholder="${I.sms.hasToken?'Leave blank to keep the saved one':''}"></label>
      <label class="f">Twilio number texts come from<input type="tel" id="at-from" value="${esc(tf.from)}" data-change="smsF" data-f="from"></label>
    </div>
    <div class="row" style="margin-top:8px"><button class="btn primary" data-action="smsSave">Save</button>${I.sms.on?'<button class="btn ghost" data-action="smsRemove">Remove</button>':''}</div>${msg('sms')}</div>
  ${I.log.length?`<div class="panel"><h2 class="h2">Recent messages</h2><div class="tblwrap" style="margin-top:8px"><table class="xl"><thead><tr><th>Time</th><th>What</th><th>Line</th><th>To</th><th>Result</th></tr></thead><tbody>
    ${I.log.map(x=>`<tr><td>${fmtD(x.t)} ${fmtT(x.t)}</td><td>${{down:'Line down',back:'Running again',test:'Test'}[x.kind]||x.kind}</td><td>${esc(x.line)}</td><td class="l">${esc(x.contact)} · ${x.channel==='sms'?'text':'email'} ${esc(x.to)}</td><td class="l">${x.ok?'<span class="pill ok">SENT</span>':`<span class="pill bad">FAILED</span> <span class="note">${esc(x.msg)}</span>`}</td></tr>`).join('')}</tbody></table></div></div>`:''}`;
}
function grafanaQueriesPanel(){
  const q=[
    ['Uptime % (leaves out breaks, lunch and start-up, like the suite)',`SELECT $__timeGroupAlias(t, '1h'),\n  100 * SUM(run) / NULLIF(SUM(run OR planned = 0), 0) AS "Uptime %"\nFROM hmi_ppm\nWHERE line = '$line' AND $__timeFilter(t)\nGROUP BY 1 ORDER BY 1`],
    ['Downtime minutes (planned time is never in this table)',`SELECT $__timeGroupAlias(start_time, '1d'), SUM(minutes) AS "Down min"\nFROM hmi_downtime\nWHERE line = '$line' AND $__timeFilter(start_time)\nGROUP BY 1 ORDER BY 1`],
    ['Downtime by reason',`SELECT COALESCE(reason, 'No reason given') AS reason, SUM(minutes) AS "Minutes"\nFROM hmi_downtime\nWHERE line = '$line' AND $__timeFilter(start_time)\nGROUP BY 1 ORDER BY 2 DESC`],
    ['Speed without planned stops',`SELECT t AS time, ppm AS "P/m"\nFROM hmi_ppm\nWHERE line = '$line' AND planned = 0 AND $__timeFilter(t)\nORDER BY t`],
    ['Filters made',`SELECT $__timeGroupAlias(t, '1h'), SUM(units) AS "Filters"\nFROM hmi_ppm\nWHERE line = '$line' AND $__timeFilter(t)\nGROUP BY 1 ORDER BY 1`],
    ['Planned breaks as shaded regions (dashboard annotation)',`SELECT start AS time, end AS timeend, kind AS text\nFROM hmi_planned\nWHERE line = '$line' AND $__timeFilter(start)`],
    ['Machine errors and alarms (table panel)',`SELECT start AS time, code AS "Code", text AS "Message", TIMESTAMPDIFF(MINUTE, start, COALESCE(end, NOW())) AS "Min"\nFROM hmi_machine_errors\nWHERE line = '$line' AND $__timeFilter(start)\nORDER BY start DESC`],
    ['Machine vision QC pass %',`SELECT $__timeGroupAlias(t, '1h'), 100 * AVG(passed) AS "Pass %"\nFROM hmi_qc_auto\nWHERE line = '$line' AND $__timeFilter(t)\nGROUP BY 1 ORDER BY 1`]];
  const IX=SRV.influx&&SRV.influx.out, b=IX?SRV.influx.out:'', v2=IX&&SRV.influx.version!=='1';
  if(IX) q.push(...(v2?[
    ['InfluxDB · Uptime % (same rule as the suite)',`from(bucket: "${b}")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n  |> filter(fn: (r) => r._measurement == "suite_minute" and r.line == "\${line}")\n  |> filter(fn: (r) => r._field == "run" or r._field == "counted")\n  |> group(columns: ["_field"])\n  |> aggregateWindow(every: 1h, fn: sum, createEmpty: false)\n  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n  |> map(fn: (r) => ({_time: r._time, "Uptime %": if r.counted > 0 then 100.0 * float(v: r.run) / float(v: r.counted) else 0.0}))`],
    ['InfluxDB · Downtime minutes (planned time left out)',`from(bucket: "${b}")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n  |> filter(fn: (r) => r._measurement == "suite_minute" and r.line == "\${line}" and r._field == "down")\n  |> group()\n  |> aggregateWindow(every: 1h, fn: sum, createEmpty: false)`],
    ['InfluxDB · Downtime by reason',`from(bucket: "${b}")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n  |> filter(fn: (r) => r._measurement == "suite_downtime" and r.line == "\${line}")\n  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n  |> group(columns: ["reason"])\n  |> sum(column: "minutes")`],
    ['InfluxDB · Speed without planned stops',`from(bucket: "${b}")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n  |> filter(fn: (r) => r._measurement == "suite_minute" and r.line == "\${line}")\n  |> filter(fn: (r) => r._field == "ppm" or r._field == "planned")\n  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n  |> filter(fn: (r) => r.planned == 0)\n  |> keep(columns: ["_time", "ppm"])`]]:[
    ['InfluxDB · Uptime % (same rule as the suite)',`SELECT 100 * sum("run") / sum("counted") AS "Uptime %" FROM "suite_minute"\nWHERE "line" = '$line' AND $timeFilter GROUP BY time(1h) fill(none)`],
    ['InfluxDB · Downtime minutes (planned time left out)',`SELECT sum("down") AS "Down min" FROM "suite_minute"\nWHERE "line" = '$line' AND $timeFilter GROUP BY time(1h) fill(none)`],
    ['InfluxDB · Speed without planned stops',`SELECT "ppm" FROM "suite_minute"\nWHERE "line" = '$line' AND "planned" = 0 AND $timeFilter`]]));
  return `<div class="panel"><h2 class="h2">Grafana queries that match the suite</h2>
    <p class="sub" style="margin-top:4px">The suite leaves planned time out of downtime: Scheduler breaks and lunches, and ${C().rules.startGrace||0} minutes of start-up after each shift's scheduled start. It marks those minutes so Grafana can do the same: <code>hmi_ppm.planned</code> is 1 for a minute inside planned time, <code>hmi_planned</code> lists each planned stretch per line and shift, and <code>hmi_downtime</code> never includes planned time. Paste these into your panels (MySQL data source on the suite's database, with a <code>line</code> dashboard variable).${IX?` The InfluxDB queries read the copy in bucket <code>${esc(b)}</code>: <code>suite_minute</code> has <code>run</code>, <code>down</code>, <code>planned</code> and <code>counted</code> (1 or 0) per line and minute, <code>suite_downtime</code> and <code>suite_planned</code> one point per stop or break.`:''}</p>
    ${q.map(([t,sql],i)=>`<div style="margin-top:10px"><div class="row"><b class="grow">${t}</b><button class="btn ghost sm" data-action="copySql" data-i="${i}">Copy</button></div><pre class="num" style="white-space:pre-wrap;background:var(--well-3);padding:8px;border-radius:6px;margin:4px 0 0;font-size:12px;user-select:all">${esc(sql)}</pre></div>`).join('')}</div>`;
}
function stationURL(id){return location.origin+'/station/'+encodeURIComponent(id)+'/'}
function connAccountsPanel(adm){
  if(!SRV.server||!adm)return '';
  const I=ui.connInfo;
  if(!I){if(!ui.connLoading){ui.connLoading=true;API.cmd('connInfo',{}).then(r=>{ui.connLoading=false;if(r.ok){ui.connInfo=r.data;render(true)}})}
    return `<div class="panel"><h2 class="h2">Databases and accounts</h2><p class="note" style="margin-top:6px">Loading…</p></div>`}
  const F=ui.connForm=ui.connForm||{};
  const dbBox=(g,title,help)=>{const d=I[g];const f=F[g]=F[g]||{host:d.host,port:d.port||'3306',name:d.name,user:d.user,password:''};const msg=(ui.connMsg||{})[g];
    return `<div class="panel" style="background:var(--well-3)"><div class="row"><h3 class="h2 grow" style="font-size:18px">${title}</h3>${d.on?'<span class="pill ok">CONNECTED</span>':'<span class="pill idle">NOT SET UP</span>'}</div>
      <p class="sub" style="margin-top:4px">${help}</p>
      <div class="grid3" style="margin-top:8px">
        <label class="f">Server name or address<input type="text" id="cf-${g}-host" value="${esc(f.host)}" data-change="connF" data-g="${g}" data-f="host" autocomplete="off" spellcheck="false" placeholder="192.168.100.30"></label>
        <label class="f">Port<input type="number" id="cf-${g}-port" value="${esc(f.port)}" data-change="connF" data-g="${g}" data-f="port"></label>
        <label class="f">Database name<input type="text" id="cf-${g}-name" value="${esc(f.name)}" data-change="connF" data-g="${g}" data-f="name" autocomplete="off" spellcheck="false"></label>
        <label class="f">Read-only user<input type="text" id="cf-${g}-user" value="${esc(f.user)}" data-change="connF" data-g="${g}" data-f="user" autocomplete="off" spellcheck="false"></label>
        <label class="f">Password<input type="password" id="cf-${g}-pw" value="${esc(f.password)}" data-change="connF" data-g="${g}" data-f="password" autocomplete="new-password" placeholder="${d.hasPassword?'Leave blank to keep the saved one':''}"></label>
      </div>
      <div class="row" style="margin-top:8px"><button class="btn" data-action="connTest" data-g="${g}">Test connection</button><button class="btn primary" data-action="connSave" data-g="${g}">Save</button>${d.on?`<button class="btn ghost" data-action="connRemove" data-g="${g}">Remove</button>`:''}</div>
      ${msg?`<p style="margin-top:6px;color:${msg.ok?'var(--ok)':'var(--bad)'}">${esc(msg.text)}</p>`:''}</div>`};
  const pf=F.paycom=F.paycom||{sid:I.paycom.sid,token:''},pm=(ui.connMsg||{}).paycom,fm=(ui.connMsg||{}).feed;
  return `<div class="panel"><h2 class="h2">Databases and accounts</h2>
    <p class="sub" style="margin-top:4px">Add or change these any time; no reinstall or restart needed. Passwords are kept in the server's protected settings file, never shown here again, and never stored in the records database. The suite only reads from the asset and S2K databases: it signs in read-only and never creates or changes anything there.</p>
    <p class="note" style="margin-top:6px">Records database: ${esc(I.records.engine)}${I.records.host?` · ${esc(I.records.name)} on ${esc(I.records.host)} as ${esc(I.records.user)}`:''}. To move the records to another server, run Setup.exe on the server.</p>
    ${I.writable?'':'<p class="note" style="margin-top:6px;color:var(--bad)">The server can\'t write its settings file, so changes can\'t be saved here. Re-run Setup.exe to repair the folder permissions.</p>'}
    <div style="display:flex;flex-direction:column;gap:10px;margin-top:10px">
    ${dbBox('s2k','S2K database (read-only)','Open MOs for the Scheduler. After saving, set up the query under S2K schedule below.')}
    ${dbBox('machines','Machine data database (read-only)','The database the machines (PLCs, data loggers) write their readings to. Pick each machine\'s table under Machine data below.')}
    ${dbBox('assets','Asset database (read-only)','Changeover steps, machine settings and drawing specs. Queries are set up on the Changeover data tab.')}
    <div class="panel" style="background:var(--well-3)"><div class="row"><h3 class="h2 grow" style="font-size:18px">Paycom</h3>${I.paycom.on?'<span class="pill ok">SET UP</span>':'<span class="pill idle">NOT SET UP</span>'}</div>
      <p class="sub" style="margin-top:4px">Imports the operator list. This is an internet connection (Paycom is a cloud service), used only when set up here.</p>
      <div class="grid2" style="margin-top:8px"><label class="f">SID<input type="text" id="cf-paycom-sid" value="${esc(pf.sid)}" data-change="connF" data-g="paycom" data-f="sid" autocomplete="off" spellcheck="false"></label>
      <label class="f">API token<input type="password" id="cf-paycom-token" value="${esc(pf.token)}" data-change="connF" data-g="paycom" data-f="token" autocomplete="new-password" placeholder="${I.paycom.hasToken?'Leave blank to keep the saved one':''}"></label></div>
      <div class="row" style="margin-top:8px"><button class="btn primary" data-action="connSave" data-g="paycom">Save</button>${I.paycom.on?'<button class="btn ghost" data-action="connRemove" data-g="paycom">Remove</button>':''}</div>
      ${pm?`<p style="margin-top:6px;color:${pm.ok?'var(--ok)':'var(--bad)'}">${esc(pm.text)}</p>`:''}</div>
    ${influxBox(I,F)}
    <div class="panel" style="background:var(--well-3)"><div class="row"><h3 class="h2 grow" style="font-size:18px">Grafana service account token</h3>${I.grafana.hasToken?'<span class="pill ok">SET</span>':'<span class="pill idle">NOT SET</span>'}</div>
      <p class="sub" style="margin-top:4px">Lets the Reports page show the data behind your Grafana panels. In Grafana: Administration › Service accounts › Add (Viewer role) › Add token, then paste it here.</p>
      <div class="row" style="margin-top:8px"><input type="password" id="cf-grafana-token" style="flex:1;min-width:0" data-change="connF" data-g="grafana" data-f="token" autocomplete="new-password" placeholder="${I.grafana.hasToken?'Paste a new token to replace the saved one':'glsa_…'}"><button class="btn primary" data-action="connSave" data-g="grafana">Save</button>${I.grafana.hasToken?'<button class="btn ghost" data-action="connRemove" data-g="grafana">Remove</button>':''}</div>
      ${(ui.connMsg||{}).grafana?`<p style="margin-top:6px;color:${ui.connMsg.grafana.ok?'var(--ok)':'var(--bad)'}">${esc(ui.connMsg.grafana.text)}</p>`:''}</div>
    <div class="panel" style="background:var(--well-3)"><div class="row"><h3 class="h2 grow" style="font-size:18px">Machine feed key</h3>${I.feed.on?'<span class="pill ok">SET</span>':'<span class="pill idle">NOT SET</span>'}</div>
      <p class="sub" style="margin-top:4px">Machines or gateways that push readings to the server send this key in the X-Feed-Key header. Needed only for machines set to "Pushed to server".</p>
      <div class="row" style="margin-top:8px"><button class="btn primary" data-action="feedKeyNew">${I.feed.on?'Make a new key':'Make a key'}</button>${I.feed.on?'<button class="btn ghost" data-action="connRemove" data-g="feed">Remove</button>':''}</div>
      ${ui.newFeedKey?`<p style="margin-top:8px">New key (shown once, copy it into each gateway): <code class="num" style="user-select:all">${esc(ui.newFeedKey)}</code> <button class="btn sm" data-action="copyFeedKey">Copy</button></p>`:''}
      ${fm?`<p style="margin-top:6px;color:${fm.ok?'var(--ok)':'var(--bad)'}">${esc(fm.text)}</p>`:''}</div>
    </div></div>`;
}
function influxBox(I,F){
  const d=I.influx, f=F.influx=F.influx||{url:d.url,version:d.version,org:d.org,user:d.user,db:d.db,out:d.out,token:''}, v2=f.version!=='1', m=(ui.connMsg||{}).influx;
  const inp=(k,label,extra='')=>`<label class="f">${label}<input type="text" id="cf-influx-${k}" value="${esc(f[k]||'')}" data-change="connF" data-g="influx" data-f="${k}" autocomplete="off" spellcheck="false" ${extra}></label>`;
  return `<div class="panel" style="background:var(--well-3)"><div class="row"><h3 class="h2 grow" style="font-size:18px">InfluxDB</h3>${d.on?'<span class="pill ok">CONNECTED</span>':'<span class="pill idle">NOT SET UP</span>'}</div>
    <p class="sub" style="margin-top:4px">Read machine speed and counts from InfluxDB (choose "InfluxDB query" for a machine under Machine data), and/or copy the suite's minute readings, downtime and planned breaks into a bucket so Grafana dashboards on InfluxDB use the same downtime rules as the suite. InfluxDB 3: choose 1.x and use the token as the password.</p>
    <div class="grid3" style="margin-top:8px">
      <label class="f">Version<select id="cf-influx-version" data-change="connFR" data-g="influx" data-f="version"><option value="2" ${v2?'selected':''}>2.x (Flux, token)</option><option value="1" ${v2?'':'selected'}>1.x or 3.x (InfluxQL)</option></select></label>
      ${inp('url','Address','placeholder="http://192.168.100.40:8086"')}
      ${v2?inp('org','Organization'):inp('user','User name')}
      <label class="f">${v2?'API token':'Password or token'}<input type="password" id="cf-influx-token" value="${esc(f.token||'')}" data-change="connF" data-g="influx" data-f="token" autocomplete="new-password" placeholder="${d.hasToken?'Leave blank to keep the saved one':''}"></label>
      ${v2?'':inp('db','Database to read from')}
      ${inp('out','Copy suite records to bucket (blank = don\'t)','placeholder="'+(v2?'production_suite':'production_suite')+'"')}
    </div>
    <p class="note" style="margin-top:6px">${v2?'A token with read access to your machine bucket is enough for machine data; copying records also needs write access to that one bucket.':'The user needs read access; copying records also needs write access to that database.'} Create the bucket in InfluxDB first. Queries the suite runs can only read.</p>
    <div class="row" style="margin-top:8px"><button class="btn" data-action="connTest" data-g="influx">Test connection</button><button class="btn primary" data-action="connSave" data-g="influx">Save</button>${d.on?'<button class="btn ghost" data-action="connRemove" data-g="influx">Remove</button>':''}</div>
    ${m?`<p style="margin-top:6px;color:${m.ok?'var(--ok)':'var(--bad)'}">${esc(m.text)}</p>`:''}</div>`;
}
// ---------------------------------------------------------------- Admin › Connections › Machine data
const FEED_LABEL={db:'Database table',http:'Read from URL',push:'Pushed to server',influx:'InfluxDB query',demo:'Demo replay',none:'Not connected'};
const TABLE_DBS={machines:'Machine data database',assets:'Asset database'};
function mTables(db,adm){
  const T=ui.mtables=ui.mtables||{};
  if(!T[db]&&adm&&SRV.server){T[db]={loading:true};API.cmd('machineTables',{db}).then(r=>{T[db]=r.ok?{tables:r.data.tables}:{err:r.error||'Couldn\'t list the tables'};render(true)})}
  return T[db]||{};
}
function mSelect(id,lid,f,val,opts,blank,dis){
  const has=opts.includes(val), known=opts.length>0;  // "not found" only once the database has listed its tables or columns
  return `<select id="${id}" data-change="lineF" data-id="${esc(lid)}" data-f="${f}" ${dis}>${blank!==null?`<option value="" ${!val?'selected':''}>${esc(blank)}</option>`:''}${val&&!has?`<option value="${esc(val)}" selected>${esc(val)}${known?' (not found)':''}</option>`:''}${opts.map(o=>`<option value="${esc(o)}" ${o===val?'selected':''}>${esc(o)}</option>`).join('')}</select>`;
}
// Where a table's rows come from: database, table, newest row, which rows are this machine's.
function mTableFields(l,pre,src,adm,dis,noAge){
  const id=l.id, k=pre.replace(/\W/g,''), T=mTables(src.db||'machines',adm), tbls=(T.tables||[]).map(t=>t.name);
  const cols=((T.tables||[]).find(t=>t.name===src.table)||{columns:[]}).columns.map(c=>c.name);
  const ready=src.db==='assets'?SRV.assets:SRV.machinesdb;
  return {cols,html:`<div class="grid3" style="margin-top:8px">
    <label class="f">Database<select id="md-${k}-db-${esc(id)}" data-change="lineF" data-id="${esc(id)}" data-f="${pre}db" ${dis}>${Object.entries({machines:'Machine data',assets:'Asset database'}).map(([v,t])=>`<option value="${v}" ${(src.db||'machines')===v?'selected':''}>${t}</option>`).join('')}</select></label>
    <label class="f">Table${mSelect(`md-${k}-tb-${id}`,id,pre+'table',src.table||'',tbls,'Pick the table',dis)}</label>
    <label class="f">Newest row by${mSelect(`md-${k}-ob-${id}`,id,pre+'orderCol',src.orderCol||'',cols,'Pick a time stamp or ID column',dis)}</label>
    <label class="f">Rows for this machine (if the table has several)${mSelect(`md-${k}-kc-${id}`,id,pre+'keyCol',src.keyCol||'',cols,'Whole table is this machine',dis)}</label>
    ${src.keyCol?`<label class="f">…where ${esc(src.keyCol)} is<input type="text" id="md-${k}-kv-${esc(id)}" data-change="lineF" data-id="${esc(id)}" data-f="${pre}keyVal" value="${esc(src.keyVal||'')}" placeholder="${esc(id)}" ${dis}></label>`:''}
    ${noAge?'':`<label class="f">No data after (min without a new row, 0 = never)<input type="number" min="0" step="1" id="md-${k}-ma-${esc(id)}" data-change="lineF" data-id="${esc(id)}" data-f="${pre}maxAge" value="${src.maxAge!=null?src.maxAge:(pre==='uptime.'?0:3)}" ${dis}></label>`}
  </div>${!ready?`<p class="note" style="color:var(--bad);margin-top:4px">Set up the ${esc(TABLE_DBS[src.db||'machines'].toLowerCase())} under Databases and accounts above first.</p>`:T.err?`<p class="note" style="color:var(--bad);margin-top:4px">${esc(T.err)}</p>`:T.loading?'<p class="note" style="margin-top:4px">Loading tables…</p>':''}`};
}
function machineDataPanel(adm){
  const dis=adm?'':'disabled', stop=C().rules.stopPpm, R=ui.feedTest||{};
  const status=k=>{const c=(S.conn||{})[k];return c?`<span class="pill ${c.ok?'ok':'bad'}" title="${esc(c.msg)}">${c.ok?'OK':'FAILED'}</span>`:''};
  const card=l=>{
    const f=l.feed, id=l.id, src=f.src||{}, up=f.uptime||{}, live=(S.runs[id]||{}).feed||{};
    const db=f.type==='db', tf=db?mTableFields(l,'feed.src.',src,adm,dis):null;
    const valIn=(key,label,val,ph,help)=>`<label class="f">${label}${db?mSelect(`md-v-${key}-${id}`,id,'feed.'+key,val||'',tf.cols,key==='ppmField'?'Pick the column':'Not given',dis):`<input type="text" id="md-v-${key}-${esc(id)}" data-change="lineF" data-id="${esc(id)}" data-f="feed.${key}" value="${esc(val||'')}" placeholder="${esc(ph)}" ${dis}>`}<span class="note">${help}</span></label>`;
    let where='';
    if(db) where=tf.html;
    else if(f.type==='http') where=`<div class="grid2" style="margin-top:8px"><label class="f">URL to read<input type="url" id="fd-u-${esc(id)}" data-change="lineF" data-id="${esc(id)}" data-f="feed.url" value="${esc(f.url)}" placeholder="http://nodered:1880/${esc(id.toLowerCase())}" ${dis}></label><label class="f">Read every (s)<input type="number" min="2" id="fd-s-${esc(id)}" data-change="lineF" data-id="${esc(id)}" data-f="feed.poll" value="${f.poll}" ${dis}></label></div>`;
    else if(f.type==='influx') where=`<label class="f" style="margin-top:8px">InfluxDB query<textarea id="fd-q-${esc(id)}" data-change="lineF" data-id="${esc(id)}" data-f="feed.query" spellcheck="false" ${dis} style="min-height:110px;font-family:var(--f-mono);font-size:12px">${esc(f.query||'')}</textarea></label>${SRV.influx&&SRV.influx.url?'':'<div class="note" style="color:var(--bad)">Set up InfluxDB above first.</div>'}`;
    else if(f.type==='push') where=`<p class="note" style="margin-top:8px"><code>POST http://&lt;server&gt;/api/feed/${esc(encodeURIComponent(id))}</code> with header <code>X-Feed-Key</code>.</p>`;
    const values=['db','http','push','influx'].includes(f.type)?`<h4 class="h3" style="margin-top:12px">Values from the machine</h4><div class="grid3" style="margin-top:6px">
      ${f.type==='push'?`<label class="f">Pleats per minute<input type="text" value="ppm" disabled><span class="note">Always sent as ppm.</span></label><label class="f">Filters cut (counter)<input type="text" value="count" disabled><span class="note">Optional, sent as count.</span></label>`
        :`${valIn('ppmField','Pleats per minute',f.ppmField,'ppm','Required.')}${valIn('countField','Filters cut (counter)',f.countField,'count','Optional. Without it, filters = pleats ÷ pleats per filter.')}`}
      ${valIn('recipeField','Recipe in the machine',f.recipeField,'recipe','Optional. Shown on the screens and kept with each minute.')}</div>`:'';
    // second place for uptime: the run signal
    let upHtml='';
    if(f.type!=='none'&&f.type!=='demo'){
      const ut=up.type||'', uf=ut==='db'?mTableFields(l,'uptime.',up,adm,dis):null;
      const fieldIn=ut==='feed'?(db?mSelect(`md-u-f-${id}`,id,'uptime.field',up.field||'',tf.cols,'Pick the column',dis):`<input type="text" id="md-u-f-${esc(id)}" data-change="lineF" data-id="${esc(id)}" data-f="uptime.field" value="${esc(up.field||'')}" placeholder="run" ${dis}>`)
        :ut==='db'?mSelect(`md-u-f-${id}`,id,'uptime.field',up.field||'',uf.cols,'Pick the column',dis):'';
      upHtml=`<h4 class="h3" style="margin-top:12px">Uptime (run signal)</h4><div class="grid3" style="margin-top:6px">
        <label class="f">Where it comes from<select id="md-u-t-${esc(id)}" data-change="lineF" data-id="${esc(id)}" data-f="uptime.type" ${dis}>
          <option value="" ${!ut?'selected':''}>Speed: stopped at or below ${stop} P/m</option>
          <option value="feed" ${ut==='feed'?'selected':''}>${db?'Another column of the same table':'Another field of the same reading'}</option>
          <option value="db" ${ut==='db'?'selected':''}>A separate database table</option></select></label>
        ${ut?`<label class="f">Signal ${ut==='feed'&&!db?'field':'column'}${fieldIn}</label>
        <label class="f">Running when the signal is<select id="md-u-w-${esc(id)}" data-change="lineF" data-id="${esc(id)}" data-f="uptime.runWhen" ${dis}><option value="high" ${up.runWhen!=='low'?'selected':''}>High (1, true, on, run)</option><option value="low" ${up.runWhen==='low'?'selected':''}>Low (0, false, off)</option></select></label>`:''}
      </div>${ut==='db'?uf.html:''}
      ${ut?`<p class="note" style="margin-top:4px">Uptime, downtime and stop alarms follow this signal. If it can't be read in a minute, that minute uses the speed instead.</p>`:''}`;
    }
    // errors and alarms the machine reports, kept with each downtime
    let errHtml='';
    if(f.type!=='none'&&f.type!=='demo'){
      const es=f.errors||{}, et=es.type||'', ef=et==='db'?mTableFields(l,'err.',es,adm,dis,true):null;
      const fld=(key,label,ph,req)=>`<label class="f">${label}${db?mSelect(`md-e-${key}-${id}`,id,'err.'+key,es[key]||'',tf.cols,req?'Pick the column':'Not given',dis):`<input type="text" id="md-e-${key}-${esc(id)}" data-change="lineF" data-id="${esc(id)}" data-f="err.${key}" value="${esc(es[key]||'')}" placeholder="${ph}" ${dis}>`}</label>`;
      const col=(key,label,blank)=>`<label class="f">${label}${mSelect(`md-e-${key}-${id}`,id,'err.'+key,es[key]||'',ef.cols,blank,dis)}</label>`;
      errHtml=`<h4 class="h3" style="margin-top:12px">Errors and alarms</h4><div class="grid3" style="margin-top:6px">
        <label class="f">Where they come from<select id="md-e-t-${esc(id)}" data-change="lineF" data-id="${esc(id)}" data-f="err.type" ${dis}>
          <option value="" ${!et?'selected':''}>Not kept</option>
          <option value="feed" ${et==='feed'?'selected':''}>${db?'Columns of the same table':'Fields of the same reading'}</option>
          <option value="db" ${et==='db'?'selected':''}>The machine's alarm log table</option></select></label>
        ${et==='feed'?`${fld('field','Error code',db?'':'error',true)}${fld('textField','Error message (optional)',db?'':'errorText',false)}`:''}
        ${et==='db'?`${col('codeCol','Alarm code','Not given')}${col('textCol','Alarm message','Not given')}${col('timeCol','When it happened','Use the time it was read')}`:''}
        ${et?`<label class="f">Values that mean no error<input type="text" id="md-e-ok-${esc(id)}" data-change="lineF" data-id="${esc(id)}" data-f="err.ok" value="${esc(es.ok||'')}" placeholder="0, OK, none (always)" ${dis}></label>`:''}
      </div>${et==='db'?ef.html:''}
      ${et==='feed'?'<p class="note" style="margin-top:4px">An error is kept from the minute it appears until it changes or clears.</p>':et==='db'?'<p class="note" style="margin-top:4px">Each new row becomes one entry. Reading starts from the newest row; older history isn\'t imported.</p>':''}
      ${et?'<p class="note">Each downtime shows the errors reported during it (and in the 2 minutes before), on the Downtime page and when picking a reason.</p>':''}`;
    }
    const tr=R[id], tu=R[id+'|uptime'], te=R[id+'|err'];
    const live1=live.t?`<span class="note">Now: ${live.ok&&live.ppm!=null?`${live.ppm} P/m`:'no data'}${live.recipe?` · recipe ${esc(live.recipe)}`:''}${live.run===true?' · running':live.run===false?' · stopped':''}</span>`:'';
    return `<div class="panel" style="background:var(--well-3)">
      <div class="row"><h3 class="h2 grow" style="font-size:18px">${esc(id)}</h3>${live1}${status('feed:'+id)}</div>
      <div class="row" style="margin-top:8px;align-items:flex-end"><label class="f" style="min-width:220px">Source<select id="fd-t-${esc(id)}" data-change="lineF" data-id="${esc(id)}" data-f="feed.type" ${dis}>${Object.entries(FEED_LABEL).map(([k,v])=>`<option value="${k}" ${f.type===k?'selected':''}>${v}</option>`).join('')}</select></label>
        ${adm&&SRV.server&&f.type!=='none'?`<button class="btn" data-action="${db?'mdTest':'testConn'}" data-id="${esc(id)}" data-k="${esc('feed:'+id)}">Test</button>`:''}
        ${adm&&SRV.server&&up.type==='db'?`<button class="btn" data-action="mdTest" data-id="${esc(id)}" data-w="uptime">Test run signal</button>`:''}
        ${adm&&SRV.server&&(f.errors||{}).type==='db'?`<button class="btn" data-action="mdTest" data-id="${esc(id)}" data-w="err">Test alarm log</button>`:''}</div>
      ${where}${values}
      ${tr?`<p style="margin-top:6px;color:${tr.ok?'var(--ok)':'var(--bad)'}">${esc(tr.text)}</p>`:''}
      ${upHtml}
      ${tu?`<p style="margin-top:6px;color:${tu.ok?'var(--ok)':'var(--bad)'}">${esc(tu.text)}</p>`:''}
      ${errHtml}
      ${te?`<p style="margin-top:6px;color:${te.ok?'var(--ok)':'var(--bad)'};white-space:pre-line">${esc(te.text)}</p>`:''}
    </div>`;
  };
  return `<div class="panel"><h2 class="h2">Machine data</h2>
    <p class="sub" style="margin-top:4px">Once a minute the server reads each machine: pleats per minute, filters cut and the recipe loaded. Pick where each machine's readings are, optionally a second place for its run signal (High = running) for uptime, and where its errors and alarms come from so each downtime shows what the machine reported.</p>
    <div style="display:flex;flex-direction:column;gap:10px;margin-top:10px">${S.lines.map(card).join('')||'<p class="note">Add machines first (Admin › Machines).</p>'}</div>
    <dl class="kv" style="margin-top:10px;font-size:14px">
      <dt>Database table</dt><dd>The newest row (by the column you pick, usually a time stamp or an auto-number ID) is read each minute, through a read-only login. One table can hold every machine: pick the column that names the machine. If no new row arrives within the minutes you set, it counts as no data.</dd>
      <dt>Read from URL</dt><dd>The URL returns JSON such as <code>{"ppm":190,"count":48211,"recipe":"MG-24","run":1}</code>. Field names can be paths like <code>data.speed</code>.</dd>
      <dt>InfluxDB query</dt><dd>The newest values from the query; Flux results by <code>_field</code>, InfluxQL by column. <code>{machine}</code> is replaced with the machine's name. Readings older than 2½ minutes count as no data.</dd>
      <dt>Pushed to server</dt><dd><code>POST http://&lt;server&gt;/api/feed/&lt;machine&gt;</code> with <code>{"ppm":190,"count":48211,"recipe":"MG-24","run":1}</code> and header <code>X-Feed-Key</code>.</dd>
      <dt>Filters cut</dt><dd>The machine's counter. The suite counts how much it went up each minute; a reset to zero is handled.</dd>
      <dt>Errors and alarms</dt><dd>An error code (and message) in the reading, kept from when it appears until it clears; or the machine's alarm log table, one entry per new row. Pushed readings can send <code>"error":"E214","errorText":"Film break"</code>.</dd>
      <dt>Run signal</dt><dd>High/Low, 1/0, true/false, on/off or run/stop. Without one, the line counts as stopped at or below the stop speed (Admin › Rules).</dd>
      <dt>No reading</dt><dd>After 2½ minutes without data, the screen shows an alarm and nothing is recorded, so a dropped feed never looks like downtime.</dd></dl></div>`;
}
function connectionsBody(){
  const s=S.settings,g=s.grafana,adm=isAdmin(),dis=adm?'':'disabled',srv=SRV.server;
  const st=k=>{const c=(S.conn||{})[k];return c?`<span class="pill ${c.ok?'ok':'bad'}">${c.ok?'OK':'FAILED'}</span> <span class="note">${esc(c.msg)} · ${fmtT(c.t)}</span>`:'<span class="pill idle">NOT TESTED</span>'};
  const feedLabel=FEED_LABEL;
  const rows=[['store','Records database',srv?(SRV.store==='mariadb'?'MariaDB (HMI_DB_* in .env)':'Files on the server (data folder)'):''],
    ...S.lines.map(l=>['feed:'+l.id,'Machine data · '+l.id,l.feed.type==='http'?l.feed.url:l.feed.type==='db'?(TABLE_DBS[(l.feed.src||{}).db||'machines']+(l.feed.src&&l.feed.src.table?' · '+l.feed.src.table:'')):feedLabel[l.feed.type]]),
    ...(SRV.machinesdb?[['machines','Machine data database','Set up in .env']]:[]),
    ...(SRV.influx&&SRV.influx.url?[['influx','InfluxDB',SRV.influx.url]]:[]),...(SRV.influx&&SRV.influx.out?[['influxOut','Suite records copied to InfluxDB','bucket '+SRV.influx.out]]:[]),
    ['grafana','Grafana',g.on&&g.url?g.url:''],['assets','Asset database (MariaDB)',SRV.assets?'Set up in .env':''],['paycom','Paycom',SRV.paycom?(s.paycom.auto?`automatic every ${s.paycom.every} min`:'manual'):''],
    ['s2k','S2K schedule',s.s2kSource==='db'?(SRV.s2kdb?'S2K database (read-only)':''):s.s2kUrl],['people','People & training list',s.peopleUrl],['webhook','Copy records to another system',s.webhook]];
  return `<div class="panel"><div class="row"><h2 class="h2 grow">Connection status</h2>${adm&&srv?'<button class="btn" data-action="testAll">Test all</button>':''}</div>
    <div class="tblwrap" style="margin-top:10px"><table class="xl"><thead><tr><th>Connection</th><th>Address</th><th>Status</th><th></th></tr></thead><tbody>
    ${rows.map(([k,l,addr])=>`<tr><td class="l"><b>${esc(l)}</b></td><td class="l" style="word-break:break-all">${addr?esc(addr):'<span class="note">Not set</span>'}</td><td class="l">${st(k)}</td><td>${adm&&srv&&addr?`<button class="btn sm" data-action="testConn" data-k="${esc(k)}">Test</button>`:''}</td></tr>`).join('')}
    </tbody></table></div>
    <p class="note" style="margin-top:8px">The server makes all of these connections, so the screens only need to reach the server.</p></div>
  ${connAccountsPanel(adm&&srv)}
  ${sourcesPanel(adm&&srv)}
  ${grafanaQueriesPanel()}
  ${machineDataPanel(adm&&srv)}
  <div class="panel"><h2 class="h2">Grafana (time-based performance graphs)</h2>
    <p class="sub" style="margin-block:6px 12px">When connected, the Controls chart and the Status page show live Grafana panels for this screen's machine and shift instead of the built-in chart.</p>
    <div class="grid2">
      <label class="chk"><input type="checkbox" id="g-on" data-change="set" data-k="grafana.on" ${g.on?'checked':''} ${dis}> Use Grafana panels</label>
      <label class="f">Grafana URL<input type="url" id="g-url" data-change="set" data-k="grafana.url" value="${esc(g.url)}" placeholder="http://grafana.plant.local:3000" ${dis}></label>
      <label class="f">Dashboard UID<input type="text" id="g-uid" data-change="set" data-k="grafana.uid" value="${esc(g.uid)}" placeholder="pleat-lines" ${dis}></label>
      <label class="f">Dashboard slug<input type="text" id="g-slug" data-change="set" data-k="grafana.slug" value="${esc(g.slug)}" ${dis}></label>
      <label class="f">Org ID<input type="number" id="g-org" data-change="set" data-k="grafana.org" value="${g.org}" ${dis}></label>
      <label class="f">Line variable name<input type="text" id="g-var" data-change="set" data-k="grafana.lineVar" value="${esc(g.lineVar)}" ${dis}></label>
      <label class="f">Speed panel ID<input type="number" id="g-ppm" data-change="set" data-k="grafana.ppm" value="${g.ppm}" ${dis}></label>
      <label class="f">Uptime panel ID<input type="number" id="g-up" data-change="set" data-k="grafana.uptime" value="${g.uptime}" ${dis}></label>
      <label class="f">Count panel ID<input type="number" id="g-cnt" data-change="set" data-k="grafana.count" value="${g.count}" ${dis}></label>
      <label class="f">Refresh<select id="g-ref" data-change="set" data-k="grafana.refresh" ${dis}>${['5s','10s','30s','1m'].map(v=>`<option ${g.refresh===v?'selected':''}>${v}</option>`).join('')}</select></label>
    </div>
    <details style="margin-top:12px"><summary style="cursor:pointer;font:600 16px var(--f-disp)">How to set up Grafana</summary>
      <ol style="display:flex;flex-direction:column;gap:8px;padding-left:20px">
        <li>In Grafana, add a <b>MySQL</b> data source pointing at the HMI database (the <code>HMI_DB_*</code> settings in the server's .env). Use a read-only account.</li>
        <li>Make a dashboard with a <code>line</code> variable: <code>SELECT DISTINCT line FROM hmi_ppm</code>. This screen passes <code>var-line</code> and the shift start time, so one dashboard serves every machine.</li>
        <li>Speed panel query:<pre>SELECT t AS time, ppm AS "P/m"
FROM hmi_ppm
WHERE line = '$line' AND $__timeFilter(t)
ORDER BY t</pre></li>
        <li>Downtime as annotations (Dashboard settings › Annotations, same data source):<pre>SELECT start_time AS time, end_time AS timeend,
       CONCAT(reason, ' · ', minutes, ' min') AS text
FROM hmi_downtime
WHERE line = '$line' AND $__timeFilter(start_time)</pre></li>
        <li>Other ready-made views: <code>hmi_qc</code>, <code>hmi_shifts</code>, <code>hmi_mo_runs</code>, <code>hmi_tickets</code>.</li>
        <li>Allow embedding in <code>grafana.ini</code>, then restart Grafana:<pre>[security]
allow_embedding = true

[auth.anonymous]
enabled = true
org_role = Viewer        ; or use your SSO / auth proxy instead</pre></li>
        <li>Find panel IDs: open a panel › Share › Embed. The link contains <code>panelId=</code>.</li>
      </ol></details></div>
  <div class="panel"><h2 class="h2">Data Kiosk (wall screen)</h2>
    <p class="sub" style="margin-block:6px 12px">The Data Kiosk at <code>/data/</code> shows these Grafana panels for every pleating line, using the Grafana connection above. Put one panel per line as <code>Title = panel ID</code>.</p>
    <div class="grid2">
      <label class="f">Layout<select id="k-layout" data-change="set" data-k="kiosk.layout" ${dis}><option value="panels" ${S.settings.kiosk.layout==='panels'?'selected':''}>Panels for each line</option><option value="dashboard" ${S.settings.kiosk.layout==='dashboard'?'selected':''}>Whole Grafana dashboard</option></select></label>
      <label class="f">Time range<select id="k-range" data-change="set" data-k="kiosk.range" ${dis}>${[['now-8h','Last 8 hours'],['now-12h','Last 12 hours'],['now-24h','Last 24 hours'],['now-7d','Last 7 days']].map(([v,l])=>`<option value="${v}" ${S.settings.kiosk.range===v?'selected':''}>${l}</option>`).join('')}</select></label>
      <label class="f">Columns<select id="k-cols" data-change="set" data-k="kiosk.cols" ${dis}>${[1,2,3,4].map(v=>`<option ${+S.settings.kiosk.cols===v?'selected':''}>${v}</option>`).join('')}</select></label>
      <label class="f">Rotate lines every (s, 0 = show all)<input type="number" min="0" id="k-rot" data-change="set" data-k="kiosk.rotate" value="${S.settings.kiosk.rotate||0}" ${dis}></label>
    </div>
    <label class="f" style="margin-top:12px">Panels<textarea id="k-panels" data-change="kioskPanels" ${dis} style="font-family:var(--f-mono);font-size:14px">${esc((S.settings.kiosk.panels||[]).map(x=>`${x.title} = ${x.panel}`).join('\n'))}</textarea></label></div>
  <div class="panel"><h2 class="h2">Paycom operator import</h2>
    <p class="sub" style="margin-top:4px">${SRV.paycom?'The server pulls the employee directory and keeps the Team list current.':'Add PAYCOM_SID and PAYCOM_TOKEN to the server .env and register the server\'s IP with Paycom to turn this on.'} New operators are added with no training; terminated employees, or anyone outside the filters, are set inactive so their history stays. Roles, training and shift lead access are never changed by the import.</p>
    <div class="grid2" style="margin-top:10px">
      <label class="chk"><input type="checkbox" id="pcAuto" data-change="set" data-k="paycom.auto" ${s.paycom.auto?'checked':''} ${dis}> Import automatically</label>
      <label class="f">Every (minutes)<input type="number" min="15" id="pcEvery" data-change="set" data-k="paycom.every" value="${s.paycom.every}" ${dis}></label>
      <label class="f">Departments to include (comma list, blank = all)<input type="text" id="pcDept" data-change="set" data-k="paycom.dept" value="${esc(s.paycom.dept)}" placeholder="Production, Pleating" ${dis}></label>
      <label class="f">Positions to include (comma list, blank = all)<input type="text" id="pcPos" data-change="set" data-k="paycom.position" value="${esc(s.paycom.position)}" placeholder="Operator, Machine" ${dis}></label>
      <label class="f">Login PIN for new operators<select id="pcPin" data-change="set" data-k="paycom.pinFrom" ${dis}><option value="code" ${s.paycom.pinFrom==='code'?'selected':''}>Paycom employee code</option><option value="badge" ${s.paycom.pinFrom==='badge'?'selected':''}>Badge / clock number</option><option value="none" ${s.paycom.pinFrom==='none'?'selected':''}>None (shift lead sets it)</option></select></label>
    </div>
    <p class="note" style="margin-top:8px">${s.paycom.last?`Last import ${fmtD(s.paycom.last)} ${fmtT(s.paycom.last)}: ${esc(s.paycom.msg)}`:s.paycom.msg?esc(s.paycom.msg):'Not imported yet.'}</p>
    ${adm?`<div class="row" style="margin-top:8px"><button class="btn primary" data-action="paycomNow" ${SRV.paycom?'':'disabled'}>Import now</button><button class="btn ghost" data-action="paycomFields" ${SRV.paycom?'':'disabled'}>Show Paycom fields</button></div>`:''}
    ${ui.pcFields?`<div class="panel" style="margin-top:10px;background:var(--well-3)"><p class="sub">Fields Paycom sends: <span class="num">${ui.pcFields.fields.map(esc).join(', ')||'none'}</span></p><p class="note" style="margin-top:6px">If department, position or badge come through blank below, set PAYCOM_FIELD_DEPT / _POSITION / _BADGE in the server .env, or PAYCOM_FETCH_DETAILS=1.</p>${rowsTable(ui.pcFields.sample,['code','name','status','active','department','position','badge'])}</div>`:''}
  </div>
  ${s2kPanel(s,adm,dis)}
  <div class="panel"><h2 class="h2">People & training list</h2>
    <div class="grid2" style="margin-top:10px">
      <label class="f">URL (JSON)<input type="url" id="peopleUrl" data-change="set" data-k="peopleUrl" value="${esc(s.peopleUrl)}" placeholder="http://hr.plant.local/api/line-operators" ${dis}></label>
      <label class="f">Pull automatically every (min, 0 = off)<input type="number" min="0" id="peopleEvery" data-change="set" data-k="peopleEvery" value="${s.peopleEvery}" ${dis}></label>
    </div>
    <p class="note" style="margin-top:8px">Expects <code>[{"id":"jortiz","name":"J. Ortiz","role":"operator","trained":["Barbara"],"pin":"1111"}]</code>. Matching people are updated, new people added; nobody is removed.</p>
    ${adm?'<div class="row" style="margin-top:8px"><button class="btn" data-action="pullPeople">Pull people now</button></div>':''}</div>
  <div class="panel"><h2 class="h2">Copy records to another system (optional)</h2>
    <div class="grid2" style="margin-top:10px"><label class="f">Webhook URL<input type="url" id="webhook" data-change="set" data-k="webhook" value="${esc(s.webhook)}" placeholder="http://mes.plant.local/hmi-events" ${dis}></label></div>
    <p class="note" style="margin-top:8px">The server posts batches of events (shift start/end, MO changes, QC checks, downtime, rolls, tickets, notes, admin changes) as <code>{"source":"pleat-line-hmi","events":[…]}</code>. The HMI database already keeps everything; this is only for feeding another system.</p></div>`;
}

function report(){
  const sh=S.shift; if(!sh) return null;
  const id=sh.id, st=shiftStats(), u=S.users.find(x=>x.id===sh.user);
  const downs=S.downtime.filter(d=>d.shift===id), qc=S.qc.filter(q=>q.shift===id), rolls=S.rolls.filter(r=>r.shift===id), tickets=S.tickets.filter(t=>t.shift===id), notes=S.notes.filter(n=>n.shift===id), mos=S.moLog.filter(m=>m.shift===id);
  const cur=curMO(); if(sh.active&&cur) mos.push({mo:cur.mo,item:cur.item,qty:sh.moCount,start:sh.moStart,end:null});
  const byR={};downs.forEach(d=>{const k=d.reason||'Not entered';byR[k]=(byR[k]||0)+d.mins});
  return {sh,u,st,downs,qc,rolls,tickets,notes,mos,byR,scrap:qc.reduce((s,q)=>s+q.scrap,0)+mos.reduce((s,m)=>s+(m.scrap||0),0)};
}
VIEWS['end-of-shift']=()=>{
  const r=report(); if(!r) return `<h1 class="h1">End of Shift</h1><div class="empty">No shift yet. Start one from Controls.</div>`;
  const {sh,u,st}=r;
  return `<div class="row"><h1 class="h1 grow">End of Shift report</h1><button class="btn" data-action="copyReport">Copy report</button>${sh.active?'<button class="btn danger" data-action="endShift">End shift</button>':''}</div>
  <p class="sub">${esc(sh.line)}${sh.shiftName?' · '+esc(sh.shiftName):''} · ${esc(u?u.name:sh.user)} · ${fmtD(sh.start)} ${fmtT(sh.start)} – ${sh.active?'now (shift still running)':fmtT(sh.end)}</p>
  <div class="grid3">
    <div class="panel stat"><span>Units made</span><b>${sh.count.toLocaleString()}</b></div>
    <div class="panel stat"><span>Uptime</span><b>${st.uptime}%</b></div>
    <div class="panel stat"><span>Downtime</span><b>${st.down} min</b></div>
    <div class="panel stat"><span>QC checks</span><b>${r.qc.length} <small style="font-size:14px">${r.qc.filter(q=>!q.pass).length} fail</small></b></div>
    <div class="panel stat"><span>Scrap</span><b>${r.scrap}</b></div>
    <div class="panel stat"><span>Rolls run</span><b>${r.rolls.length}</b></div>
  </div>
  <div class="grid2">
    <div class="panel"><h2 class="h2">MOs run</h2><div class="tblwrap" style="margin-top:8px"><table class="xl"><thead><tr><th>MO</th><th>Item</th><th>Qty this shift</th><th>Start</th><th>End</th></tr></thead><tbody>${r.mos.map(m=>`<tr><td>${m.mo}</td><td>${esc(m.item)}</td><td>${m.qty}</td><td>${fmtT(m.start)}</td><td>${m.end?fmtT(m.end):'running'}</td></tr>`).join('')||'<tr><td colspan="5">None</td></tr>'}</tbody></table></div></div>
    <div class="panel"><h2 class="h2">Downtime by reason</h2><div class="bars" style="margin-top:10px">${Object.entries(r.byR).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`<div class="bar"><span>${esc(k)}</span><i style="width:${v/Math.max(...Object.values(r.byR))*100}%;${k==='Not entered'?'':'background:var(--ink-3)'}"></i><span class="num">${v} min</span></div>`).join('')||'<p class="note">No stops.</p>'}</div>
      ${needReason().length?`<p class="note" style="color:var(--warn)">${needReason().length} stops still need a reason. <a href="#downtime" data-action="go" data-page="downtime" style="color:var(--accent)">Enter them</a></p>`:''}</div>
    <div class="panel"><h2 class="h2">Roll tags</h2><div class="tblwrap" style="margin-top:8px"><table class="xl"><thead><tr><th>Roll</th><th>Material</th><th>Lot</th><th>MO</th><th>Loaded</th></tr></thead><tbody>${r.rolls.map(x=>`<tr><td>${esc(x.roll)}</td><td>${esc(x.material)}</td><td>${esc(x.lot)}</td><td>${x.mo||''}</td><td>${fmtT(x.t)}</td></tr>`).join('')||'<tr><td colspan="5">None scanned</td></tr>'}</tbody></table></div></div>
    <div class="panel"><h2 class="h2">Issues, tickets and notes</h2><div style="display:flex;flex-direction:column;gap:6px;margin-top:8px">
      ${sh.issues?`<div><b>Start-of-shift issue:</b> ${esc(sh.issues)}</div>`:''}
      ${sh.endIssues?`<div><b>End-of-shift issue:</b> ${esc(sh.endIssues)}</div>`:''}
      ${r.tickets.map(t=>`<div><b>Ticket</b> ${esc(t.machine)} · ${esc(t.type)} · ${t.status}${(t.photos||[]).length?` · ${t.photos.length} picture${t.photos.length>1?'s':''}`:''}</div>`).join('')}
      ${r.notes.map(n=>`<div><b class="num">${fmtT(n.t)}</b> ${esc(n.text)}</div>`).join('')}
      ${!sh.issues&&!sh.endIssues&&!r.tickets.length&&!r.notes.length?'<p class="note">Nothing reported.</p>':''}</div></div>
  </div>`;
};
function reportText(){
  const r=report(); if(!r) return '';
  const {sh,u,st}=r; const L=[];
  L.push(`END OF SHIFT — ${sh.line} — ${fmtD(sh.start)}`,`Operator: ${u?u.name:sh.user}`,`Time: ${fmtT(sh.start)} – ${sh.active?'running':fmtT(sh.end)}`,`Units: ${sh.count}  Uptime: ${st.uptime}%  Downtime: ${st.down} min  Scrap: ${r.scrap}`,'');
  L.push('MOs:');r.mos.forEach(m=>L.push(`  ${m.mo} ${m.item}: ${m.qty}`));
  L.push('Downtime:');Object.entries(r.byR).forEach(([k,v])=>L.push(`  ${k}: ${v} min`));
  L.push(`QC: ${r.qc.length} checks, ${r.qc.filter(q=>!q.pass).length} fail`);
  L.push('Rolls:');r.rolls.forEach(x=>L.push(`  ${x.roll} ${x.material} lot ${x.lot}`));
  if(sh.issues)L.push('Start-of-shift issue: '+sh.issues);
  if(sh.endIssues)L.push('End-of-shift issue: '+sh.endIssues);
  r.tickets.forEach(t=>L.push(`Ticket: ${t.machine} ${t.type} (${t.status})`));
  r.notes.forEach(n=>L.push(`Note ${fmtT(n.t)}: ${n.text}`));
  return L.join('\n');
}

VIEWS.team=()=>{
  const adm=isAdmin(), pc=S.settings.paycom, all=S.users, list=all.filter(u=>ui.showInactive||u.active!==false);
  const inact=all.filter(u=>u.active===false).length, needPin=all.filter(u=>u.active!==false&&!u.hasPin).length, untrained=all.filter(u=>u.active!==false&&!u.trained.length).length;
  return `<div class="row"><h1 class="h1 grow">Team & training matrix</h1>${adm?`${SRV.paycom?'<button class="btn" data-action="paycomNow">Import from Paycom</button>':''}<button class="btn primary" data-action="addUser">Add person</button>`:''}</div>
  <p class="sub">Operators can only start a shift on machines they are trained on. ${adm?'Tick boxes to change training.':'A shift lead can edit this.'} ${pc.last?`Paycom import ${fmtD(pc.last)} ${fmtT(pc.last)}${pc.auto?`, automatic every ${pc.every} min`:''}.`:SRV.paycom?'':'Paycom can import operators once it is set up on the server.'}</p>
  ${needPin||untrained?`<div class="panel" style="border-color:var(--warn)">${needPin?`${needPin} active ${needPin>1?'people need':'person needs'} a PIN before they can log in. `:''}${untrained?`${untrained} active ${untrained>1?'people have':'person has'} no machine training yet.`:''}</div>`:''}
  <div class="row"><label class="chk" style="padding:6px 10px"><input type="checkbox" id="showInactive" data-change="showInactive" ${ui.showInactive?'checked':''}> Show inactive (${inact})</label></div>
  <div class="tblwrap"><table class="xl"><thead><tr><th>Name</th><th>Source</th><th>Role</th>${activeLines().map(l=>`<th>${esc(l.id)}</th>`).join('')}<th>PIN</th><th>Status</th></tr></thead><tbody>
  ${list.map(u=>`<tr style="${u.active===false?'opacity:.55':''}"><td class="l">${esc(u.name)}${ME&&u.id===ME.id?' (you)':''}</td>
    <td class="l">${u.source==='paycom'?`<span class="srcpill">Paycom ${esc(u.eecode)}</span><br><span class="small">${esc([u.dept,u.position].filter(Boolean).join(' · '))}</span>`:'Added here'}</td>
    <td>${adm?`<select id="role-${u.id}" data-change="role" data-id="${u.id}">${['operator','maintenance','admin'].map(r=>`<option ${u.role===r?'selected':''} value="${r}">${r==='admin'?'shift lead':r}</option>`).join('')}</select>`:(u.role==='admin'?'shift lead':u.role)}</td>
    ${activeLines().map(l=>`<td><input type="checkbox" id="tr-${u.id}-${esc(l.id)}" data-change="train" data-id="${u.id}" data-line="${esc(l.id)}" ${u.trained.includes(l.id)?'checked':''} ${adm?'':'disabled'} style="width:22px;height:22px"></td>`).join('')}
    <td>${adm?`<input type="text" id="pin-${u.id}" value="${esc(u.pin)}" data-change="pin" data-id="${u.id}" placeholder="Set PIN" style="width:96px;${u.pin?'':'border-color:var(--bad)'}">`:(u.hasPin?'••••':'<b class="red">none</b>')}</td>
    <td>${u.source==='paycom'?(u.active===false?`<span class="pill idle">INACTIVE</span>${u.leftList?'<br><span class="small">not in Paycom list</span>':''}`:'<span class="pill ok">ACTIVE</span>'):(adm&&!(ME&&u.id===ME.id)?`<label style="display:inline-flex;gap:6px;align-items:center"><input type="checkbox" id="ua-${u.id}" data-change="uactive" data-id="${u.id}" ${u.active!==false?'checked':''} style="width:20px;height:20px">Active</label>`:(u.active===false?'<span class="pill idle">INACTIVE</span>':'<span class="pill ok">ACTIVE</span>'))}</td></tr>`).join('')}
  </tbody></table></div>
  <p class="note">Paycom sets names, department, position and active status for imported people. Roles, training and PINs are managed here and are never overwritten by the import.</p>`;
};
const ADMIN_TABS=[['machines','Machines'],['connections','Connections'],['codata','Changeover data'],['shifts','Shifts & breaks'],['reasons','Downtime reasons'],['checklists','Checklists'],['maintenance','Maintenance lists'],['parts','Parts & specs'],['rules','Rules'],['alerts','Alerts'],['network','Network'],['updates','Updates'],['backup','Backup']];
function listEd(k,title,help){
  const L=C()[k], adm=isAdmin(), dis=adm?'':'disabled';
  return `<div class="panel"><div class="row"><h2 class="h2 grow">${title}</h2><span class="note">${L.length} items</span></div>${help?`<p class="sub" style="margin-top:4px">${help}</p>`:''}
  <div class="ledit">${L.map((v,i)=>`<div class="lrow"><span class="num note">${i+1}</span><input type="text" id="le-${k}-${i}" value="${esc(v)}" data-change="lst" data-k="${k}" data-i="${i}" ${dis}>${adm?`<button class="btn ghost sm" data-action="lstMove" data-k="${k}" data-i="${i}" data-d="-1" aria-label="Move up" ${i?'':'disabled'}>↑</button><button class="btn ghost sm" data-action="lstMove" data-k="${k}" data-i="${i}" data-d="1" aria-label="Move down" ${i<L.length-1?'':'disabled'}>↓</button><button class="btn ghost sm" data-action="lstDel" data-k="${k}" data-i="${i}" aria-label="Remove ${esc(v)}">✕</button>`:''}</div>`).join('')}</div>
  ${adm?`<div class="row" style="margin-top:10px"><input type="text" id="la-${k}" placeholder="Add a new item" style="flex:1;width:auto;min-width:0" data-enter="lstAdd" data-k="${k}"><button class="btn primary" data-action="lstAdd" data-k="${k}">Add</button></div>`:''}</div>`;
}
function buildSQL(name){
  const B=ui.bld[name]||{}, D=E.QDEF[name], q=c=>'`'+String(c).replace(/`/g,'``')+'`';
  if(!B.table) throw new Error('Pick a table.');
  const miss=D.roles.filter(([k,,req])=>req&&!B[k]).map(r=>r[1]); if(miss.length) throw new Error('Pick a column for: '+miss.join(', '));
  if(!B.f_part) throw new Error('Pick the column that holds the part number.');
  const sel=D.roles.filter(([k])=>B[k]).map(([k])=>`${q(B[k])} AS ${k}`).join(',\n       ');
  const w=[`${q(B.f_part)} = :part`]; if(B.f_machine) w.push(`(${q(B.f_machine)} = :machine OR ${q(B.f_machine)} IS NULL)`); if(B.f_from) w.push(`(${q(B.f_from)} = :from_part OR ${q(B.f_from)} IS NULL)`);
  return `SELECT ${sel}\nFROM ${q(B.table)}\nWHERE ${w.join('\n  AND ')}${D.order&&B.seq?`\nORDER BY ${q(B.seq)}`:''}`;
}
function codataBody(){
  const adm=isAdmin(), dis=adm?'':'disabled';
  if(!SRV.server) return `<div class="panel" style="border-color:var(--warn)">Changeover data comes from your asset database through the HMI server. This preview has no server, so changeovers use the standard checklist from Admin › Checklists.</div>`;
  if(!SRV.assets) return `<div class="panel" style="border-color:var(--warn)"><h2 class="h2">Asset database not set up</h2><p class="sub" style="margin-top:6px">Add <code>ASSET_DB_HOST</code>, <code>ASSET_DB_NAME</code>, <code>ASSET_DB_USER</code> and <code>ASSET_DB_PASSWORD</code> (a read-only account) to the server's .env and restart it. Until then, changeovers use the standard checklist.</p></div>`;
  const sc=ui.dbSchema, tables=sc?sc.tables:[], cols=t=>((tables.find(x=>x.name===t)||{}).columns||[]).map(c=>c.name);
  const sel=(id,ch,val,opts,extra='')=>`<select id="${id}" ${ch} ${extra}><option value="">—</option>${opts.map(o=>`<option ${o===val?'selected':''}>${esc(o)}</option>`).join('')}</select>`;
  const cp=E.coParams(S,station);
  let h=`<div class="panel"><div class="row"><h2 class="h2 grow">Changeover data from MariaDB</h2></div>
    <p class="sub" style="margin-top:4px">During a changeover, the server looks up the steps and machine settings for the new part on this machine. If the database has nothing for that part, or cannot be reached, operators get the last steps saved for it, then the standard checklist from Admin › Checklists.</p>
    <div class="grid2" style="margin-top:10px"><label class="chk"><input type="checkbox" id="useDb" data-change="set" data-k="useDb" ${S.settings.useDb?'checked':''} ${dis}> Use database steps in changeovers</label>
</div>
    ${adm?`<div class="row" style="margin-top:8px"><button class="btn primary" data-action="dbConnect">${sc?'Reload tables and queries':'Connect to database'}</button><span class="note">${esc(ui.dbMsg||'')}</span></div>`:'<p class="note" style="margin-top:8px">Log in as a shift lead to set up queries.</p>'}</div>`;
  if(!sc||!adm) return h;
  h+=`<div class="panel"><h2 class="h2">Browse tables</h2><p class="sub" style="margin-top:4px">${tables.length} tables in ${esc(sc.database||'the database')}. Pick one to see its columns and first rows.</p>
    <div class="row" style="margin-top:8px">${sel('dbTable','data-change="dbTable"',ui.dbTable,tables.map(t=>t.name),'style="max-width:320px"')}</div>
    ${ui.dbTable?`<p class="note num" style="margin-top:8px">${cols(ui.dbTable).map(esc).join(' · ')}</p>${ui.dbPrev?rowsTable(ui.dbPrev.rows,ui.dbPrev.columns):''}`:''}</div>`;
  for(const [name,D] of Object.entries(E.QDEF)){
    const B=ui.bld[name]=ui.bld[name]||{}, saved=(ui.dbSaved||{})[name], res=ui.dbRes[name], tp=ui.dbTP[name]=ui.dbTP[name]||{part:cp.part||'MG4062HP',machine:cp.machine||(activeLines()[0]||{}).id||'',from_part:cp.from_part||''};
    const preview=res&&!res.error?(name==='changeover_steps'?E.normSteps(res.rows):name==='asset_settings'?E.normSettings(res.rows):res.rows):[];
    h+=`<div class="panel" style="display:flex;flex-direction:column;gap:10px"><div class="row"><h2 class="h2 grow">${D.title}</h2>${saved?`<span class="pill ok">SAVED</span><span class="note">${esc(saved.savedBy||'')} ${saved.savedAt?fmtD(saved.savedAt):''}</span>`:'<span class="pill idle">NOT SAVED</span>'}</div>
      <p class="sub">${D.help} Use <code>:part</code>, <code>:machine</code>, <code>:from_part</code>, <code>:mo</code> or <code>:line_code</code> where the value should go. Column names are matched by the names below, so alias them with AS.</p>
      <details ${ui.dbSql[name]?'':'open'}><summary style="cursor:pointer;font:600 16px var(--f-disp)">Build from a table</summary>
        <div class="grid3" style="margin-top:8px"><label class="f">Table${sel(`b-${name}-table`,`data-change="bldF" data-q="${name}" data-f="table"`,B.table,tables.map(t=>t.name))}</label>
        ${D.roles.map(([k,l,req])=>`<label class="f">${l}${req?' *':''}${sel(`b-${name}-${k}`,`data-change="bldF" data-q="${name}" data-f="${k}"`,B[k],cols(B.table))}</label>`).join('')}
        <label class="f">Part number column *${sel(`b-${name}-fp`,`data-change="bldF" data-q="${name}" data-f="f_part"`,B.f_part,cols(B.table))}</label>
        ${name==='part_spec'?'':`<label class="f">Machine column (blank rows apply to all)${sel(`b-${name}-fm`,`data-change="bldF" data-q="${name}" data-f="f_machine"`,B.f_machine,cols(B.table))}</label>`}
        ${name==='changeover_steps'?`<label class="f">From-part column (blank rows apply to all)${sel(`b-${name}-ff`,`data-change="bldF" data-q="${name}" data-f="f_from"`,B.f_from,cols(B.table))}</label>`:''}</div>
        <div class="row" style="margin-top:8px"><button class="btn" data-action="dbBuild" data-q="${name}">Write the SQL</button></div></details>
      <label class="f">SQL<textarea id="sql-${name}" data-change="dbSql" data-q="${name}" spellcheck="false" style="font-family:var(--f-mono);font-size:13px;min-height:130px" placeholder="SELECT instruction AS step, step_no AS seq FROM changeover_steps WHERE part_no = :part ORDER BY step_no">${esc(ui.dbSql[name]||'')}</textarea></label>
      <div class="grid3"><label class="f">Test part<input type="text" id="tp-${name}-part" data-change="dbTP" data-q="${name}" data-f="part" value="${esc(tp.part)}"></label><label class="f">Test machine<input type="text" id="tp-${name}-machine" data-change="dbTP" data-q="${name}" data-f="machine" value="${esc(tp.machine)}"></label><label class="f">Test from-part<input type="text" id="tp-${name}-from" data-change="dbTP" data-q="${name}" data-f="from_part" value="${esc(tp.from_part)}"></label></div>
      <div class="row"><button class="btn" data-action="dbTest" data-q="${name}">Run test</button><button class="btn primary" data-action="dbSave" data-q="${name}">Save on server</button>${saved?`<button class="btn ghost" data-action="dbDrop" data-q="${name}">Stop using this query</button>`:''}</div>
      ${res?(res.error?`<p style="color:var(--bad)">${esc(res.error)}</p>`:`<p class="note">${res.rows.length} row${res.rows.length===1?'':'s'} in ${res.ms} ms${res.truncated?' (first 500)':''}. ${preview.length?'Operators will see:':'<span style="color:var(--warn)">No usable rows — check the column names (need '+D.roles.filter(r=>r[2]).map(r=>r[0]).join(', ')+').</span>'}</p>
        ${preview.length?(name==='changeover_steps'?`<div class="panel" style="background:var(--well-3);display:flex;flex-direction:column;gap:6px">${preview.slice(0,8).map((st,i)=>coStepHTML(st,i,false,true)).join('')}${preview.length>8?`<span class="note">+${preview.length-8} more</span>`:''}</div>`:name==='asset_settings'?coSettingsHTML(preview):rowsTable(preview,res.columns)):''}
        <details><summary class="note" style="cursor:pointer">Raw rows</summary>${rowsTable(res.rows,res.columns)}</details>`):''}
    </div>`;
  }
  return h;
}
function coStepHTML(st,i,checked,preview){
  const tag=(st.setting||st.value!=='')&&(st.setting||st.value)?`<span class="pill warn" style="margin-left:6px">${esc(st.setting)} ${esc(st.value)}${esc(st.unit)}</span>`:'';
  return `<label class="chk" style="align-items:flex-start">${preview?'<span style="width:22px"></span>':`<input type="checkbox" id="cos${i}" data-change="coStep" data-i="${i}" ${checked?'checked':''}>`} <span class="num" style="color:var(--ink-2)">${i+1}</span>
    <span style="flex:1;min-width:0"><span>${esc(st.text)}</span>${tag}${st.asset?`<span class="note"> · ${esc(st.asset)}</span>`:''}${st.detail?`<br><span class="note">${esc(st.detail)}</span>`:''}</span>
    ${st.image&&/^https?:\/\//i.test(st.image)?`<a class="btn ghost sm" href="${esc(st.image)}" target="_blank" rel="noopener">Photo</a>`:''}</label>`;
}
function coSettingsHTML(list){return `<div class="tblwrap"><table class="xl"><thead><tr><th>Setting</th><th>Value</th><th>Unit</th><th>Asset</th></tr></thead><tbody>${list.map(x=>`<tr><td class="l"><b>${esc(x.name)}</b></td><td><b>${esc(x.value)}</b></td><td>${esc(x.unit)}</td><td class="l">${esc(x.asset)}</td></tr>`).join('')}</tbody></table></div>`}
function updatesBody(){
  if(!SRV.server) return `<div class="panel" style="border-color:var(--warn)">Updates are installed on the server. This preview has no server.</div>`;
  if(!isAdmin()) return `<div class="panel"><h2 class="h2">Updates</h2><p class="sub" style="margin-top:6px">This screen runs Production Suite v${esc(SRV.version)} rev ${esc(SRV.revision)}. Log in as a shift lead to install updates.</p></div>`;
  const I=ui.upd;
  if(!I){if(!ui.updLoading){ui.updLoading=true;API.cmd('updInfo',{}).then(r=>{ui.updLoading=false;if(r.ok){ui.upd=r.data;render(true)}})}
    return `<div class="panel"><h2 class="h2">Updates</h2><p class="note" style="margin-top:6px">Loading…</p></div>`}
  const st=I.state||{}, s=st.status||'idle', working=s==='starting'||s==='installing';
  if(working||(s==='ready'&&st.when==='now')){clearTimeout(ui.updTimer);ui.updTimer=setTimeout(()=>{API.cmd('updInfo',{}).then(r=>{if(r.ok){ui.upd=r.data;if(ui.adminTab==='updates')render(true)}}).catch(()=>{})},3000)}
  const when=w=>({now:'right away',idle:'when no line is running a shift',at:st.whenAt?`at ${fmtD(Date.parse(st.whenAt))} ${fmtT(Date.parse(st.whenAt))}`:'at a set time',hold:'when a shift lead says so'}[w]||w);
  const pill={idle:'',ready:'<span class="pill warn">WAITING</span>',starting:'<span class="pill warn">STARTING</span>',installing:'<span class="pill warn">INSTALLING</span>',
    done:'<span class="pill ok">DONE</span>',failed:'<span class="pill bad">NOT INSTALLED</span>',rejected:'<span class="pill bad">REFUSED</span>'}[s]||'';
  const F=ui.updForm=ui.updForm||{when:'idle',at:''};
  const logBox=(st.log||[]).length?`<pre class="num" style="white-space:pre-wrap;background:var(--well-3);padding:8px;border-radius:6px;margin:8px 0 0;font-size:12px;max-height:240px;overflow:auto">${esc(st.log.join('\n'))}</pre>`:'';
  let cur='';
  if(s==='ready') cur=`<p style="margin-top:8px"><b>${st.job==='goback'?'Going back to':'Update'} v${esc(st.version)} rev ${esc(st.revision)}</b> ${st.job==='goback'?'':`checked and waiting${st.packages?' (includes Python packages)':''}`}. Installs <b>${when(st.when)}</b>${st.by?` · from ${esc(st.by)}`:''}.</p>
      ${st.notes?`<p class="note" style="margin-top:4px;white-space:pre-wrap">${esc(st.notes)}</p>`:''}
      ${st.job==='goback'?'':`<div class="row" style="margin-top:8px;flex-wrap:wrap"><button class="btn primary" data-action="updWhen" data-w="now">Install now</button><button class="btn" data-action="updWhen" data-w="idle">When no line is running</button>
        <input type="datetime-local" id="upd-at2" value="${esc(F.at)}" data-change="updF" data-f="at"><button class="btn" data-action="updWhen" data-w="at">At this time</button><button class="btn ghost" data-action="updCancel">Remove</button></div>`}`;
  else if(working) cur=`<p style="margin-top:8px"><b>${st.job==='goback'?'Going back to':'Installing'} v${esc(st.version)} rev ${esc(st.revision)}.</b> The suite stops for a minute or two; screens show "offline" and reconnect by themselves.</p>`;
  else if(st.message) cur=`<p style="margin-top:8px;color:${s==='failed'||s==='rejected'?'var(--bad)':s==='done'?'var(--ok)':'inherit'}">${esc(st.message)}</p>`;
  const prev=(I.backups||[])[0];
  return `<div class="panel"><div class="row"><h2 class="h2 grow">Updates</h2>${pill}</div>
    <p class="sub" style="margin-top:4px">Installed: <b>v${esc(I.version)} rev ${esc(I.revision)}</b>${I.installed?` · since ${fmtD(Date.parse(I.installed))}`:''}. Updates keep every setting, record and login. Before installing, the suite backs itself up; if the new version doesn't start, it goes back by itself.</p>
    ${cur}${logBox}</div>
  <div class="panel"><h2 class="h2">Install an update file</h2>
    <p class="sub" style="margin-top:4px">Choose a <code>ProductionSuite-Update-….zip</code>. It is checked before anything changes: only update files signed for this suite, newer than what's installed, with every file intact, are accepted.</p>
    <div class="grid3" style="margin-top:8px">
      <label class="f">Update file<input type="file" id="upd-file" accept=".zip" data-change="updFile" ${working?'disabled':''}></label>
      <label class="f">Install<select id="upd-when" data-change="updFR" data-f="when"><option value="now" ${F.when==='now'?'selected':''}>Right away</option><option value="idle" ${F.when==='idle'?'selected':''}>When no line is running a shift</option><option value="at" ${F.when==='at'?'selected':''}>At a set time</option></select></label>
      ${F.when==='at'?`<label class="f">Date and time<input type="datetime-local" id="upd-at" value="${esc(F.at)}" data-change="updF" data-f="at"></label>`:''}
    </div>
    <div class="row" style="margin-top:8px"><button class="btn primary" data-action="updUpload" ${working?'disabled':''}>Check and install</button>${ui.updFile?`<span class="note">${esc(ui.updFile.name)}</span>`:''}<span class="note" id="updMsg">${esc(ui.updMsg||'')}</span></div></div>
  <div class="panel"><h2 class="h2">Update folder</h2>
    <p class="sub" style="margin-top:4px">IT can also copy update files into this folder on the server (it can be shared on the network). The suite picks them up within a minute, checks them the same way, and moves refused files to <code>updates\\rejected</code>.</p>
    <p style="margin-top:6px"><code class="num" style="user-select:all;word-break:break-all">${esc(I.folder)}</code></p>
    <label class="f" style="margin-top:8px;max-width:420px">Files from the folder install<select id="upd-folder" data-change="updFolder"><option value="idle" ${I.policy.folderWhen==='idle'?'selected':''}>When no line is running a shift</option><option value="now" ${I.policy.folderWhen==='now'?'selected':''}>Right away</option><option value="hold" ${I.policy.folderWhen==='hold'?'selected':''}>Only when a shift lead says so here</option></select></label></div>
  <div class="panel"><h2 class="h2">Go back</h2>
    ${prev?`<p class="sub" style="margin-top:4px">The previous version, v${esc(prev.version)} rev ${esc(prev.revision)}${prev.at?` (backed up ${fmtD(Date.parse(prev.at))} ${fmtT(Date.parse(prev.at))})`:''}, is kept. Going back puts its program files back and takes the database tables back to match; records made since are kept.</p>
      <div class="row" style="margin-top:8px"><button class="btn" data-action="updGoBack" ${working?'disabled':''}>Go back to v${esc(prev.version)} rev ${esc(prev.revision)}</button></div>`
      :'<p class="sub" style="margin-top:4px">No earlier version is kept yet. After the first update, the version it replaced is kept here (the last 3).</p>'}</div>`;
}
function networkBody(){
  if(!SRV.server) return `<div class="panel" style="border-color:var(--warn)">The plant network is set on the server. This preview has no server.</div>`;
  const N=S.network;
  if(!N||!isAdmin()) return `<div class="panel"><h2 class="h2">Plant network</h2><p class="sub" style="margin-top:6px">Only devices on the plant network can open the suite. Log in as a shift lead to see and change it.</p><p class="note" style="margin-top:8px">This device: <b class="num">${esc(CLIENT_IP||'unknown')}</b></p></div>`;
  const saved=N.allowed.map(a=>a.range), draft=ui.netDraft||saved, changed=JSON.stringify(draft)!==JSON.stringify(saved);
  const desc=r=>(N.allowed.find(a=>a.range===r)||{}).desc||'';
  const local=/^(127\.|::1$|::ffff:127\.)/.test(CLIENT_IP);
  const src={settings:`Set here${N.by?` by ${esc(N.by)}`:''}${N.at?` on ${fmtD(Date.parse(N.at))} ${fmtT(Date.parse(N.at))}`:''}.`,installer:'Set by the installer.',default:'The standard plant network.'}[N.source]||'';
  const fw=N.firewall;
  const fwPill=!fw?'<span class="pill idle">NOT CHECKED</span>':fw.ok===true?'<span class="pill ok">IN STEP</span>':fw.ok===false?'<span class="pill bad">NEEDS ATTENTION</span>':'<span class="pill idle">NOT USED</span>';
  return `<div class="panel"><div class="row"><h2 class="h2 grow">Plant network</h2>${changed?'<span class="pill warn">NOT SAVED</span>':''}</div>
    <p class="sub" style="margin-top:4px">Screens, tablets and PCs can open the suite only from these addresses, and the server only connects to machine feeds, S2K, Grafana and databases inside them. The server itself is always allowed. Only private ranges can be added (10.x, 172.16–31.x, 192.168.x, 100.64–127.x), never the internet.</p>
    <p class="note" style="margin-top:8px">This device: <b class="num">${esc(CLIENT_IP||'unknown')}</b>${local?' (the server itself)':''} · ${src}</p>
    <div class="ledit" style="margin-top:10px">${draft.map((r,i)=>`<div><div class="lrow"><span class="num note">${i+1}</span><input type="text" id="net-${i}" value="${esc(r)}" data-change="netEd" data-i="${i}" inputmode="decimal" autocomplete="off" spellcheck="false" aria-label="Network range ${i+1}"><button class="btn ghost sm" data-action="netDel" data-i="${i}" aria-label="Remove ${esc(r)}" ${draft.length>1?'':'disabled'}>✕</button></div><div class="note" style="margin:2px 0 0 34px">${esc(desc(r)||'Not saved yet.')}</div></div>`).join('')}</div>
    <div class="row" style="margin-top:10px"><input type="text" id="netAdd" placeholder="Add a range, e.g. 192.168.101.0/24, or one address" style="flex:1;width:auto;min-width:0" data-enter="netAdd" inputmode="decimal" autocomplete="off" spellcheck="false"><button class="btn" data-action="netAdd">Add</button></div>
    <p class="note" style="margin-top:6px">192.168.100.0/24 means every address from 192.168.100.0 to 192.168.100.255. A single address such as 192.168.100.25 allows just that device.</p>
    <div class="row" style="margin-top:10px"><button class="btn primary" data-action="netSave" ${changed?'':'disabled'}>Save plant network</button>${changed?'<button class="btn ghost" data-action="netUndo">Discard changes</button>':''}${draft.length!==1||draft[0]!==N.default?`<button class="btn ghost" data-action="netDefault">Use ${esc(N.default)}</button>`:''}<span class="note" id="netErr" style="color:var(--bad)"></span></div></div>
  <div class="panel"><div class="row"><h2 class="h2 grow">Windows Firewall</h2>${fwPill}</div>
    <p class="sub" style="margin-top:4px">Saving also updates the server's firewall rule, so the firewall and the suite always allow the same addresses.</p>
    ${fw?`<p class="note" style="margin-top:8px">${esc(fw.msg)}${fw.t?` · ${fmtD(fw.t)} ${fmtT(fw.t)}`:''}</p>`:''}
    <div class="row" style="margin-top:8px"><button class="btn" data-action="netFw">Check firewall again</button></div></div>
  <div class="panel"><h2 class="h2">Locked out?</h2><p class="sub" style="margin-top:4px">A save that would shut out the device making it is refused. If screens still can't connect, sign in on the server itself and open <code>http://localhost:${esc(location.port||'80')}/operator/</code>, or run <code>python manage.py network --set 192.168.100.0/24</code> in the suite's folder.</p></div>`;
}
const ADM={
  updates:()=>updatesBody(),
  alerts:()=>alertsBody(),
  network:()=>networkBody(),
  connections:()=>connectionsBody(),
  codata:()=>codataBody(),
  machines:()=>{const adm=isAdmin(),dis=adm?'':'disabled';return `<div class="panel"><div class="row"><h2 class="h2 grow">Machines</h2>${adm?'<button class="btn primary" data-action="addLine">Add machine</button>':''}</div>
    <p class="sub" style="margin-top:4px">Rated speed sets the default feed rate and the dashed line on the speed chart. Taking a machine out of service moves its queued MOs back to Unscheduled and hides it from Start Shift.</p>
    <div class="tblwrap" style="margin-top:10px"><table class="xl"><thead><tr><th>Name</th><th>Process code</th><th>Rated P/m</th><th>Color</th><th>In service</th><th>Queued MOs</th><th>Trained people</th><th>Screen address</th><th></th></tr></thead><tbody>
    ${S.lines.map((l,i)=>`<tr><td><input type="text" id="ln-${i}" value="${esc(l.id)}" data-change="lineF" data-id="${esc(l.id)}" data-f="id" ${dis} style="width:150px"></td>
      <td><input type="text" id="lc-${i}" value="${esc(l.code)}" data-change="lineF" data-id="${esc(l.id)}" data-f="code" ${dis} style="width:90px"></td>
      <td><input type="number" min="1" id="lr-${i}" value="${l.rated}" data-change="lineF" data-id="${esc(l.id)}" data-f="rated" ${dis} style="width:90px"></td>
      <td><input type="color" id="lk-${i}" value="${esc(l.color)}" data-change="lineF" data-id="${esc(l.id)}" data-f="color" ${dis} class="swatchin"></td>
      <td><input type="checkbox" id="la-${i}" ${l.active!==false?'checked':''} data-change="lineF" data-id="${esc(l.id)}" data-f="active" ${dis} style="width:22px;height:22px"></td>
      <td>${S.mos.filter(m=>m.line===l.id&&m.status==='OPEN').length}</td><td>${S.users.filter(u=>u.trained.includes(l.id)).length}</td>
      <td class="l"><a href="${esc(stationURL(l.id))}" target="_blank" rel="noopener" class="num" style="color:var(--accent);word-break:break-all">${esc(stationURL(l.id).replace(/^https?:\/\//,''))}</a> <button class="btn ghost sm" data-action="copyStation" data-id="${esc(l.id)}">Copy</button></td>
      <td>${adm?`<button class="btn ghost sm" data-action="delLine" data-id="${esc(l.id)}">Remove</button>`:''}</td></tr>`).join('')}
    </tbody></table></div><p class="note" style="margin-top:8px">Open a machine's screen address on its touchscreen: that screen then shows only this machine's operator pages (no other modes, machines, Team or Admin). Shift leads use the normal Operator Kiosk address for Admin. Renaming a machine changes its address. Renaming a machine carries its schedule and training over. Run speeds, scrap, downtime, changeover times and shifts are set in the <a href="/scheduler/#lines" style="color:var(--accent)">Scheduler</a>. Set training on the <a href="#team" data-action="go" data-page="team" style="color:var(--accent)">Team</a> page.</p></div>`},
  shifts:()=>{const adm=isAdmin(),dis=adm?'':'disabled';return `<div class="row"><p class="sub grow">Shifts set the schedule timeline, the shift name in the banner and on reports. MO run times on the timeline pause over breaks. An end time earlier than the start runs past midnight.</p>${adm?'<button class="btn primary" data-action="addShift">Add shift</button>':''}</div>
    <div class="grid2">${C().shifts.map((sh,i)=>{let len=toMin(sh.end)-toMin(sh.start);if(len<=0)len+=1440;const paid=len-(sh.breaks||[]).reduce((a,b)=>a+(+b.mins||0),0);
    return `<div class="panel" style="display:flex;flex-direction:column;gap:10px"><div class="row"><input type="text" id="sn-${i}" value="${esc(sh.name)}" data-change="shiftF" data-s="${i}" data-f="name" ${dis} style="flex:1;width:auto;font-weight:600">${adm?`<button class="btn ghost sm" data-action="delShift" data-s="${i}">Remove</button>`:''}</div>
      <div class="grid2" style="grid-template-columns:1fr 1fr"><label class="f">Start<input type="time" id="ss-${i}" value="${esc(sh.start)}" data-change="shiftF" data-s="${i}" data-f="start" ${dis}></label><label class="f">End<input type="time" id="se-${i}" value="${esc(sh.end)}" data-change="shiftF" data-s="${i}" data-f="end" ${dis}></label></div>
      <div class="note num">${hm(len)} long · ${hm(paid)} run time after breaks</div>
      <div style="display:flex;flex-direction:column;gap:6px">${(sh.breaks||[]).map((b,j)=>`<div class="brow"><input type="text" id="bn-${i}-${j}" value="${esc(b.name)}" data-change="brkF" data-s="${i}" data-b="${j}" data-f="name" ${dis} aria-label="Break name"><input type="time" id="bs-${i}-${j}" value="${esc(b.start)}" data-change="brkF" data-s="${i}" data-b="${j}" data-f="start" ${dis} aria-label="Break start"><input type="number" min="1" id="bm-${i}-${j}" value="${b.mins}" data-change="brkF" data-s="${i}" data-b="${j}" data-f="mins" ${dis} aria-label="Minutes"><span class="note">min</span>${adm?`<button class="btn ghost sm" data-action="delBreak" data-s="${i}" data-b="${j}" aria-label="Remove break">✕</button>`:''}</div>`).join('')||'<p class="note">No breaks.</p>'}</div>
      ${adm?`<button class="btn ghost" data-action="addBreak" data-s="${i}">Add break</button>`:''}</div>`}).join('')}</div>`},
  reasons:()=>listEd('reasons','Downtime reasons','The list operators pick from when the downtime prompt asks why the line stopped. Add, rename, reorder or remove reasons here; the order here is the order on the screen. Maintenance and shift leads can also type a reason that isn\'t listed, or change a reason already entered. Removing a reason keeps it on stops already logged. When the prompt appears is set in Rules.'),
  checklists:()=>listEd('safety','QC and safety checklist','Every item must be checked before the line can start.')+listEd('pm','Shift PM checklist','Recorded at start of shift; not required to start.')+listEd('coSteps','Changeover steps','Shown in order during a changeover. All must be checked before running.'),
  maintenance:()=>listEd('issueTypes','Issue types','Choices on a maintenance ticket.')+listEd('equipment','Other equipment','Listed after the machines when someone opens a ticket.'),
  parts:()=>{const adm=isAdmin(),dis=adm?'':'disabled',P=C().parts,F=[['pleats','Pleats/unit',1],['len','Length',.01],['wid','Width',.01],['ph','Pleat ht',.01],['sb','Side band',.01],['tol','Tol ±',.01]];
    return `<div class="panel"><div class="row"><h2 class="h2 grow">Parts & QC specs</h2>${adm?`<input type="text" id="newPart" placeholder="Part number, e.g. MG4120" style="width:220px" data-enter="addPart"><button class="btn primary" data-action="addPart">Add part</button>`:''}</div>
    <p class="sub" style="margin-top:4px">QC checks, drawings and unit counts use these values. Prefixes like 1MG/2MG share the MG spec. Inches.</p>
    <div class="tblwrap" style="margin-top:10px"><table class="xl"><thead><tr><th>Part</th>${F.map(f=>`<th>${f[1]}</th>`).join('')}<th>Rev</th><th>Media</th><th></th></tr></thead><tbody>
    ${Object.keys(P).sort().map(k=>`<tr><td><b>${esc(k)}</b></td>${F.map(([f,,st])=>`<td><input type="number" step="${st}" id="p-${esc(k)}-${f}" value="${P[k][f]}" data-change="partF" data-k="${esc(k)}" data-f="${f}" ${dis} style="width:84px"></td>`).join('')}
      <td><input type="text" id="p-${esc(k)}-rev" value="${esc(P[k].rev)}" data-change="partF" data-k="${esc(k)}" data-f="rev" ${dis} style="width:56px"></td>
      <td><input type="text" id="p-${esc(k)}-media" value="${esc(P[k].media)}" data-change="partF" data-k="${esc(k)}" data-f="media" ${dis} style="width:190px"></td>
      <td>${adm?`<button class="btn ghost sm" data-action="delPart" data-k="${esc(k)}">✕</button>`:''}</td></tr>`).join('')}
    </tbody></table></div></div>`},
  rules:()=>{const adm=isAdmin(),dis=adm?'':'disabled',r=C().rules,num=(k,label,help,min=0)=>`<label class="f">${label}<input type="number" min="${min}" id="r-${k}" value="${r[k]}" data-change="rule" data-k="${k}" ${dis}><span class="note">${help}</span></label>`;
    return `<div class="panel"><h2 class="h2">Rules</h2><div class="grid2" style="margin-top:10px">
      ${num('stopPpm','Line counts as stopped at or below (P/m)','Speed readings at or below this count as downtime, unless the machine has a run signal (Admin › Connections › Machine data).')}
      ${num('minDown','Log a stop as downtime after (min)','Shorter stops still count against uptime but need no reason.',1)}
      ${num('reasonAfter','Ask for a reason once unexplained downtime adds up to (min)','Logged stops without a reason are collected; when they reach this many minutes the operator must pick a reason. 0 asks at every stop.')}
      ${num('startGrace','Start-up time at the start of each shift (min)','Stops in this many minutes after the shift\'s scheduled start are not downtime. Breaks and lunches from the Scheduler\'s shifts never count as downtime.')}
      ${num('coMins','Changeover allowance (min)','Time reserved on the schedule between different parts.',0)}
      <label class="f">QC check interval (min)<input type="number" min="5" id="r-qc" value="${S.settings.qcEvery}" data-change="set" data-k="qcEvery" ${dis}><span class="note">How often the QC reminder comes due.</span></label>
      <label class="chk"><input type="checkbox" id="r-aqc" ${S.settings.autoQC?'checked':''} data-change="set" data-k="autoQC" ${dis}> Auto QC reminder on</label>
      ${num('adminTimeout','Log out shift lead after idle (min)','0 keeps admin mode on until someone logs out.')}
      ${num('photoDays','Keep pictures on closed maintenance tickets (days)','Pictures are kept in the records database. 0 keeps them for good.')}
    </div></div>`},
  backup:()=>{const adm=isAdmin(),dis=adm?'':'disabled';const exp=JSON.stringify({app:'pleat-line-hmi',exported:new Date().toISOString(),config:S.config,lines:S.lines,users:S.users.map(u=>({...u,pin:undefined}))},null,1);
    return `<div class="panel"><h2 class="h2">Copy settings to another station</h2><p class="sub" style="margin-top:4px">Machines, shifts, reasons, checklists, parts, rules, feed rates and people (without PINs).</p>
      <textarea id="expJson" readonly style="min-height:160px;font-family:var(--f-mono);font-size:12px;margin-top:10px">${esc(exp)}</textarea><div class="row" style="margin-top:8px"><button class="btn" data-action="copyCfg">Copy settings</button></div></div>
    <div class="panel"><h2 class="h2">Load settings</h2><textarea id="impJson" placeholder="Paste settings copied from another station" style="min-height:120px;margin-top:10px" ${dis}></textarea>
      <div class="row" style="margin-top:8px"><button class="btn primary" data-action="importCfg" ${dis}>Load settings</button><button class="btn danger" data-action="resetCfg" ${dis}>Restore default settings</button><span class="note" id="impErr"></span></div></div>
    <div class="panel" style="border-color:var(--bad)"><h2 class="h2">Plant data</h2><p class="sub" style="margin-top:4px">${S.demo?'The server is running on demo data. When you are ready to go live, clear it: machines, people, training, settings and connections stay.':'Records are kept on the server. Loading demo data replaces everything and is only for training.'}</p>
      <div class="row" style="margin-top:8px"><button class="btn danger" data-action="resetData" data-mode="clear" ${dis}>${S.demo?'Clear demo data and go live':'Clear MOs and records'}</button><button class="btn ghost" data-action="resetData" data-mode="demo" ${dis}>Load demo data</button></div></div>`}
};
VIEWS.admin=()=>{
  const tab=ui.adminTab||'machines', adm=isAdmin();
  return `<div class="row"><h1 class="h1 grow">Admin</h1>${adm?`<span class="pill bad">Admin mode · ${esc(user().name)}</span><button class="btn ghost" data-action="login">Exit admin mode</button>`:''}</div>
  ${adm?'':`<div class="panel row" style="border-color:var(--warn)"><span class="grow">View only. Log in as a shift lead to make changes.</span><button class="btn primary" data-action="adminLogin">Enter admin mode</button></div>`}
  <div class="tabs" role="tablist">${ADMIN_TABS.map(([k,l])=>`<button role="tab" aria-selected="${tab===k}" class="${tab===k?'on':''}" data-action="adminTab" data-v="${k}">${l}</button>`).join('')}</div>
  ${ADM[tab]()}
  ${S.audit.length?`<div class="panel"><h2 class="h2">Recent changes</h2><div class="auditlog">${S.audit.slice(0,12).map(a=>`<div><span class="num note">${fmtD(a.t)} ${fmtT(a.t)}</span> · ${esc(a.by)} · ${esc(a.what)}</div>`).join('')}</div></div>`:''}`;
};
function modal(title,body,foot=''){
  ui.modal=null;
  $('#sheet').innerHTML=`<header><h2 class="h1">${title}</h2><button class="btn ghost" data-action="close" aria-label="Close">Close</button></header><div class="body">${body}</div>${foot?`<footer>${foot}</footer>`:''}`;
  $('#modal').hidden=false; const f=$('#sheet [autofocus]'); if(f) setTimeout(()=>f.focus(),30);
}
function closeModal(){$('#modal').hidden=true;ui.modal=null;render(true)}
function needLogin(next){if(user())return true;openLogin(next);return false}
let pending=null;
function openLogin(next,adminOnly=false){
  pending={next,adminOnly,pick:null};
  const list=S.users.filter(u=>u.active!==false&&(!adminOnly||u.role==='admin'));
  modal(adminOnly?'Shift lead approval':'Log in',`
    ${adminOnly?'<p class="sub">This change needs a shift lead PIN.</p>':''}
    <div class="users">${list.map(u=>`<button class="btn" data-action="pickUser" data-id="${u.id}">${esc(u.name)}<br><span class="note">${u.role==='admin'?'shift lead':u.role}</span></button>`).join('')}</div>
    <label class="f">PIN<input type="password" id="pin" autocomplete="off" placeholder="Scan badge or type PIN"></label>
    ${S.demo?'<p class="note">Example PINs: J. Ortiz 1111 · M. Lee 2222 · K. Patel 3333 · Shift Lead 9999</p>':''}<p class="note" id="loginErr" style="color:var(--bad)"></p>`,
    `<button class="btn go" data-action="doLogin">${adminOnly?'Approve':'Log in'}</button>`);
}

function openStartShift(){
  if(!needLogin(openStartShift)) return;
  const u=user();
  if(shiftOn()){modal('Shift already running',`<p>${esc(S.shift.line)} has been running since ${fmtT(S.shift.start)}. End that shift first.</p>`,`<button class="btn" data-action="close">OK</button>`);return}
  const line=STATION_LOCK||pending?.line||(u.trained.includes(station)&&lineOf(station)&&lineOf(station).active!==false?station:(activeLines().find(l=>u.trained.includes(l.id))||{}).id)||'';
  const q=line?S.mos.filter(m=>m.line===line&&m.status==='OPEN').sort((a,b)=>a.seq-b.seq):[];
  modal('Start of shift',`
    <div class="grid2">
      <label class="f">MFG line<select id="ssLine" data-change="ssLine">${S.lines.filter(l=>!STATION_LOCK||l.id===STATION_LOCK).map(l=>{const ok=u.trained.includes(l.id)&&l.active!==false;return `<option value="${esc(l.id)}" ${l.id===line?'selected':''} ${ok?'':'disabled'}>${esc(l.id)}${l.active===false?' — out of service':u.trained.includes(l.id)?'':' — not trained'}</option>`}).join('')}</select></label>
      <label class="f">Scan MO<input type="text" id="ssMO" placeholder="Scan or type MO number" autofocus value="${q[0]?q[0].mo:''}"></label>
    </div>
    ${q.length?`<div class="note">Next on ${esc(line)}: ${q.slice(0,3).map(m=>`<button class="btn ghost" style="min-height:32px;padding:4px 10px" data-action="pickMO" data-mo="${m.mo}">${m.mo} · ${esc(m.item)}</button>`).join(' ')}</div>`:''}
    ${!u.trained.length?'<p style="color:var(--bad)">You are not trained on any machine. Ask your shift lead.</p>':''}
    <div><h3 class="h2">QC and safety checklist</h3><div class="grid2" style="margin-top:8px">${C().safety.map((c,i)=>`<label class="chk"><input type="checkbox" id="saf${i}" class="saf"> ${c}</label>`).join('')}</div></div>
    <div><h3 class="h2">Shift PM checklist</h3><div class="grid2" style="margin-top:8px">${C().pm.map((c,i)=>`<label class="chk"><input type="checkbox" id="pm${i}" class="pmc"> ${c}</label>`).join('')}</div></div>
    <label class="f">Issues to report<textarea id="ssIssue" placeholder="Anything found during the walk-around"></textarea></label>
    <p class="note" id="ssErr" style="color:var(--bad)"></p>`,
    `<button class="btn go" data-action="doStartShift">Start running</button>`);
}
async function openChangeover(fromPush){
  if(!needLogin(()=>openChangeover())) return;
  if(!shiftOn()){toast('Start a shift first.');return}
  if(!S.shift.co){if(fromPush===true){closeModal();return} if(!await send('coOpen',{},{quiet:true}))return}
  const sh=S.shift, co=sh.co, mo=moById(co.oldMo), nmo=co.newMo?moById(co.newMo):null;
  const steps=`<div class="steps"><span class="${co.step===1?'on':co.step>1?'done':''}">1 · End MO</span><span class="${co.step===2?'on':co.step>2?'done':''}">2 · Next MO</span><span class="${co.step===3?'on':''}">3 · Set up</span></div>`;
  if(co.step===1){
    modal('Change over',`${steps}
      ${mo?`<div class="panel"><dl class="kv"><dt>Ending MO</dt><dd>${mo.mo} · ${esc(mo.item)}</dd><dt>MO timer</dt><dd class="num">${hms(now()-sh.moStart)}</dd><dt>Made this run</dt><dd class="num">${sh.moCount}</dd><dt>Total produced</dt><dd class="num">${mo.produced} of ${mo.qty}</dd></dl></div>
      <div class="grid2"><label class="f">Final count for this run<input type="number" id="coQty" value="${sh.moCount}" autofocus></label><label class="f">Scrap<input type="number" id="coScrap" value="0"></label></div>
      <label class="chk"><input type="checkbox" id="coClose" ${mo.produced>=mo.qty?'checked':''}> Close this MO (order complete)</label>`:'<p>No MO is running. Go on to pick the next one.</p>'}`,
      `<button class="btn ghost" data-action="cancelCO">Cancel</button><button class="btn go" data-action="coEnd">End MO and start changeover</button>`);
  }else if(co.step===2){
    const q=S.mos.filter(m=>m.line===sh.line&&m.status==='OPEN'&&String(m.id)!==String(co.oldMo)).sort((a,b)=>a.seq-b.seq);
    modal('Change over',`${steps}<div class="bigtimer num" data-live="coTimer">${LIVE.coTimer()}</div>
      <label class="f">Scan next MO<input type="text" id="coMO" autofocus placeholder="Scan or type MO number" value="${q[0]?q[0].mo:''}"></label>
      ${q.length?`<div class="note">From the ${esc(sh.line)} schedule: ${q.slice(0,4).map(m=>`<button class="btn ghost" style="min-height:32px;padding:4px 10px" data-action="pickCO" data-mo="${m.mo}">${m.mo} · ${esc(m.item)}</button>`).join(' ')}</div>`:'<p class="note">Nothing else scheduled on this line. Scan an MO or ask your shift lead.</p>'}
      <p class="note" id="coErr" style="color:var(--bad)"></p>`,
      `<button class="btn go" data-action="coPick">Use this MO</button>`);
  }else{
    const p=part(nmo.item);
    modal('Change over',`${steps}
      <div class="row"><div class="grow"><div class="h2">${esc(nmo.item)} · MO ${nmo.mo}</div><div class="sub">${nmo.qty.toLocaleString()} units · ${p.pleats} pleats/unit · drawing rev ${p.rev}</div></div><div class="bigtimer num" data-live="coTimer">${LIVE.coTimer()}</div></div>
      ${co.loading?'<div class="panel empty">Loading steps for this part from the database…</div>':`
      ${{db:`<p class="note">Steps and settings from the database for ${esc(p.key)} on ${esc(sh.line)}.</p>`,cache:`<p class="note" style="color:var(--warn)">Database not reachable. Showing the steps saved for this part at ${fmtT(co.cacheT)}.</p>`,'admin-offline':`<p class="note" style="color:var(--warn)">Database not reachable and nothing saved for this part. Showing the standard checklist.</p>`,'admin-empty':`<p class="note" style="color:var(--warn)">No database steps for ${esc(p.key)} on ${esc(sh.line)}. Showing the standard checklist.</p>`}[co.src]||''}
      ${co.settings&&co.settings.length?`<div><h3 class="h2">Settings for ${esc(p.key)}</h3>${coSettingsHTML(co.settings)}</div>`:''}
      <div style="display:flex;flex-direction:column;gap:6px">${coList(co).map((st,i)=>coStepHTML(st,i,co.done.includes(i))).join('')}</div>
      ${S.settings.useDb&&SRV.assets?'<div><button class="btn ghost sm" data-action="coReload">Reload steps from database</button></div>':''}`}
      <div class="row"><button class="btn" data-action="drawing">Get updated drawing</button><button class="btn" data-action="adminSetting">Change setting - Admin</button><button class="btn" data-action="rollTags">Scan roll tag</button><button class="btn" data-action="qcCheck">First-article QC</button></div>
      ${sh.override?`<p class="note">Temporary settings for this MO: ${Object.entries(sh.override).map(([k,v])=>`${k} ${v}`).join(' · ')}</p>`:''}
      <p class="note" id="coErr" style="color:var(--bad)"></p>`,
      `<button class="btn go" data-action="coFinish">Start running</button>`);
  }
  ui.modal='changeover'; ui.modalSig=JSON.stringify([S.shift.co,shiftOn()]);
}
const coList=co=>co.steps||C().coSteps.map(t=>({text:t}));
function setMoFromScan(val){const m=S.mos.find(x=>String(x.mo)===String(val).trim()&&x.status==='OPEN');return m||null}

function openQC(){
  if(!needLogin(openQC)) return;
  if(!shiftOn()){toast('Start a shift before logging QC.');return}
  const mo=curMO(), p=part(mo?mo.item:(S.shift.co&&S.shift.co.newMo?moById(S.shift.co.newMo).item:''));
  const f=(id,label,target)=>`<label class="f">${label} <span class="num">${target.toFixed(2)} ±${p.tol}</span><input type="number" step="0.01" id="${id}" data-spec="${target}" data-tol="${p.tol}" data-change="qcIn" placeholder="${target.toFixed(2)}"></label>`;
  modal('QC check',`<p class="sub">${esc(p.key||'No MO')} · drawing rev ${p.rev} · inches</p>
    <div class="spec">${f('qLen','Length',p.len)}${f('qWid','Width',p.wid)}${f('qPh','Pleat height',p.ph)}${f('qSbl','Side band height L',p.sb)}${f('qSbr','Side band height R',p.sb)}</div>
    <div class="grid2">
      <div class="f"><span class="note">Glue QC</span><div class="seg" id="qGlue"><button data-action="seg" data-v="Pass" class="on">Pass</button><button data-action="seg" data-v="Fail">Fail</button></div></div>
      <div class="f"><span class="note">Cut QC</span><div class="seg" id="qCut"><button data-action="seg" data-v="Pass" class="on">Pass</button><button data-action="seg" data-v="Fail">Fail</button></div></div>
      <label class="f">Scrap count, past 30 min<input type="number" id="qScrap" value="0" min="0"></label>
    </div><p class="note" id="qErr" style="color:var(--bad)"></p>`,
    `<button class="btn go" data-action="saveQC">Save QC check</button>`);
}
function openRolls(){
  if(!needLogin(openRolls)) return;
  if(!shiftOn()){toast('Start a shift before scanning rolls.');return}
  const mo=curMO(), list=S.rolls.filter(r=>r.shift===sid()).reverse();
  modal('Roll tags',`<div class="grid2"><label class="f">Scan roll tag<input type="text" id="rId" autofocus placeholder="Roll ID"></label><label class="f">Material<input type="text" id="rMat" value="${esc(part(mo?mo.item:'').media)}"></label><label class="f">Lot<input type="text" id="rLot"></label><label class="f">Footage on roll<input type="number" id="rFt" placeholder="ft"></label></div>
    <div><h3 class="h2">Roll production report</h3><div class="tblwrap" style="margin-top:8px"><table class="xl"><thead><tr><th>Roll</th><th>Material</th><th>Lot</th><th>MO</th><th>Loaded</th><th>Units on roll</th></tr></thead><tbody>
    ${list.map((r,i)=>{const nxt=i===0?S.shift.count:list[i-1].countAt;return `<tr><td>${esc(r.roll)}</td><td>${esc(r.material)}</td><td>${esc(r.lot)}</td><td>${r.mo||''}</td><td>${fmtT(r.t)}</td><td>${nxt-r.countAt}</td></tr>`}).join('')||'<tr><td colspan="6">No rolls scanned this shift.</td></tr>'}</tbody></table></div></div>`,
    `<button class="btn go" data-action="saveRoll">Add roll</button>`);
}
function openTicket(){
  if(!needLogin(openTicket)) return;
  modal('Maintenance ticket',`<div class="grid2">
    <label class="f">Machine<select id="tMach">${[...S.lines.map(l=>l.id),...C().equipment].map(m=>`<option ${S.shift&&S.shift.line===m?'selected':''}>${esc(m)}</option>`).join('')}</select></label>
    <label class="f">Issue<select id="tType">${C().issueTypes.map(x=>`<option>${x}</option>`).join('')}</select></label>
    <label class="f">Priority<select id="tPri"><option>Low</option><option selected>Medium</option><option value="High">High · line down</option></select></label></div>
    <label class="f">Notes<textarea id="tNotes" placeholder="What is happening, when it started, what you tried" autofocus></textarea></label>
    <div style="margin-top:10px"><div class="row"><b class="grow">Pictures of what is wrong</b>${photoButtons('tPhotos')}</div>
      <div class="thumbs" id="tPics" style="margin-top:8px"></div><p class="note" id="tPicMsg">Optional. Up to ${PHOTO_LIMIT} pictures.</p></div>`,
    `<button class="btn go" data-action="saveTicket">Submit ticket</button>`);
  ui.tPhotos=[];
}
/* ---------- maintenance pictures: taken with the tablet's camera, shrunk to 1600 px JPEG before sending ---------- */
const PHOTO_MAX=1600, PHOTO_LIMIT=6;
const photoButtons=(change,id)=>`<label class="btn sm">${IC.camera||''}Take a picture<input type="file" accept="image/*" capture="environment" hidden data-change="${change}"${id!=null?` data-id="${id}"`:''}></label>
  <label class="btn sm ghost">Choose a picture<input type="file" accept="image/*" multiple hidden data-change="${change}"${id!=null?` data-id="${id}"`:''}></label>`;
function shrinkPhoto(file){
  return new Promise((res,rej)=>{const u=URL.createObjectURL(file),img=new Image();
    img.onload=()=>{const k=Math.min(1,PHOTO_MAX/Math.max(img.naturalWidth,img.naturalHeight)),c=document.createElement('canvas');
      c.width=Math.max(1,Math.round(img.naturalWidth*k));c.height=Math.max(1,Math.round(img.naturalHeight*k));
      c.getContext('2d').drawImage(img,0,0,c.width,c.height);URL.revokeObjectURL(u);c.toBlob(b=>b?res(b):rej(new Error('That picture couldn\'t be read.')),'image/jpeg',0.82)};
    img.onerror=()=>{URL.revokeObjectURL(u);rej(new Error('That file isn\'t a picture.'))};img.src=u});
}
async function uploadPhoto(ticket,blob){
  const fd=new FormData();fd.append('ticket',ticket);fd.append('photo',blob,'photo.jpg');
  try{const r=await fetch('/api/hmi/ticket-photo',{method:'POST',credentials:'same-origin',headers:{'X-CSRFToken':csrf()},body:fd});return await r.json()}
  catch(e){return {ok:false,error:'Couldn\'t reach the server.'}}
}
function drawTicketPics(){
  const el=$('#tPics');if(!el)return;
  el.innerHTML=ui.tPhotos.map((p,i)=>`<div class="thumb"><img src="${p.url}" alt="Picture ${i+1}"><button class="btn sm ghost" data-action="tPhotoDel" data-i="${i}" aria-label="Remove picture ${i+1}">Remove</button></div>`).join('');
}
function openPhoto(id){
  const t=S.tickets.find(x=>(x.photos||[]).some(p=>p.id===id)),p=t&&t.photos.find(x=>x.id===id);
  modal(t?`${esc(t.machine)} · ${esc(t.type)}`:'Picture',`<img class="photo-big" src="/api/hmi/ticket-photo/${id}" alt="Picture on the maintenance ticket">${p?`<p class="note" style="margin-top:6px">Taken by ${esc(p.by)} · ${fmtD(p.t)} ${fmtT(p.t)}</p>`:''}`,
    `<a class="btn ghost" href="/api/hmi/ticket-photo/${id}" target="_blank" rel="noopener">Open full size</a>`);
}
function drawingSVG(p){
  const pl=Array.from({length:16},(_,i)=>`<line x1="${70+i*14}" y1="60" x2="${70+i*14}" y2="230" stroke="#7a8895" stroke-width="1"/>`).join('');
  return `<svg viewBox="0 0 620 330" role="img" aria-label="Drawing of ${esc(p.key)}"><g font-family="IBM Plex Mono,monospace" font-size="12" fill="#1c2b3a">
  <rect x="60" y="50" width="240" height="190" fill="#fff" stroke="#1c2b3a" stroke-width="2"/>${pl}
  <line x1="60" y1="262" x2="300" y2="262" stroke="#1c2b3a"/><path d="M60 258v8M300 258v8" stroke="#1c2b3a"/><text x="180" y="280" text-anchor="middle">L ${p.len.toFixed(2)} ±${p.tol}</text>
  <line x1="36" y1="50" x2="36" y2="240" stroke="#1c2b3a"/><path d="M32 50h8M32 240h8" stroke="#1c2b3a"/><text x="28" y="150" text-anchor="middle" transform="rotate(-90 28 150)">W ${p.wid.toFixed(2)} ±${p.tol}</text>
  <text x="60" y="36" font-size="13" font-weight="600">TOP VIEW</text>
  <text x="360" y="36" font-size="13" font-weight="600">SECTION A–A</text>
  <polyline points="360,170 375,110 390,170 405,110 420,170 435,110 450,170 465,110 480,170" fill="none" stroke="#1c2b3a" stroke-width="2"/>
  <rect x="352" y="104" width="8" height="70" fill="#c9d5e1" stroke="#1c2b3a"/><rect x="480" y="104" width="8" height="70" fill="#c9d5e1" stroke="#1c2b3a"/>
  <line x1="510" y1="110" x2="510" y2="170" stroke="#1c2b3a"/><path d="M506 110h8M506 170h8" stroke="#1c2b3a"/><text x="520" y="144">PH ${p.ph.toFixed(2)}</text>
  <line x1="340" y1="104" x2="340" y2="174" stroke="#1c2b3a"/><path d="M336 104h8M336 174h8" stroke="#1c2b3a"/><text x="332" y="198" text-anchor="end">SB ${p.sb.toFixed(2)}</text>
  <rect x="340" y="230" width="270" height="90" fill="#fff" stroke="#1c2b3a"/><line x1="340" y1="255" x2="610" y2="255" stroke="#1c2b3a"/>
  <text x="350" y="248" font-weight="600">${esc(p.key)}</text><text x="560" y="248">REV ${p.rev}</text>
  <text x="350" y="274">${p.pleats} PLEATS · ${esc(p.media)}</text><text x="350" y="294">INCHES · TOL ±${p.tol} UNLESS NOTED</text><text x="350" y="312">CHECK GLUE BEAD CONTINUOUS ALL SIDES</text>
  </g></svg>`;
}
function openDrawing(){
  const sh=S.shift, id=curMO()?curMO().item:(sh&&sh.co&&sh.co.newMo?moById(sh.co.newMo).item:'MG4062HP');
  const p=part(id);
  modal(`Drawing · ${esc(p.key)}`,`<div class="drawing">${drawingSVG(p)}</div><p class="note" id="drawNote">${sh&&sh.drawChecked?`Rev ${p.rev} confirmed current at ${fmtT(sh.drawChecked)}.`:'Showing the drawing on file at shift start.'}</p>`,
    `<button class="btn" data-action="adminSetting">Change setting - Admin</button><button class="btn primary" data-action="getDrawing" data-k="${esc(p.key)}">Get updated drawing</button>`);
}
function openAdminSetting(){
  if(!isAdmin()){openLogin(openAdminSetting,true);return}
  const sh=S.shift; if(!shiftOn()){toast('Start a shift first.');return}
  const o=sh.override||{}, it=curMO()?curMO().item:(sh.co&&sh.co.newMo?moById(sh.co.newMo).item:''), p=part(it);
  modal('Change setting - Admin',`<p class="sub">Temporary for the current MO only. Clears at the next changeover.</p><div class="grid2">
    <label class="f">Target speed (P/m)<input type="number" id="oPpm" value="${o.ppm||''}" placeholder="${lineOf(sh.line).rated}"></label>
    <label class="f">Pleats per unit<input type="number" id="oPl" value="${o.pleats||''}" placeholder="${p.pleats}"></label>
    <label class="f">Pleat height (in)<input type="number" step="0.01" id="oPh" value="${o.ph||''}" placeholder="${p.ph}"></label>
    <label class="f">Glue temp (°F)<input type="number" id="oGlue" value="${o.glue||''}" placeholder="350"></label>
    <label class="f">Reason<input type="text" id="oWhy" value="${esc(o.why||'')}" placeholder="Why this run differs"></label></div>`,
    `${sh.override?'<button class="btn ghost" data-action="clearOverride">Clear</button>':''}<button class="btn go" data-action="saveOverride">Apply to this MO</button>`);
}
function openNotes(){
  if(!needLogin(openNotes)) return;
  const list=S.notes.filter(n=>(sid()&&n.shift===sid())||(!n.shift&&n.line===station)).reverse();
  modal('Shift notes',`<label class="f">New note<textarea id="nText" autofocus placeholder="For the next shift or the lead"></textarea></label>
    <div style="display:flex;flex-direction:column;gap:8px">${list.map(n=>`<div class="panel"><b class="num">${fmtT(n.t)}</b> · ${esc(n.by)}<div>${esc(n.text)}</div></div>`).join('')||'<p class="note">No notes yet this shift.</p>'}</div>`,
    `<button class="btn go" data-action="saveNote">Save note</button>`);
}
function openEndShift(){
  if(!shiftOn()){go('end-of-shift');return}
  if(!needLogin(openEndShift)) return;
  const st=shiftStats(), mo=curMO();
  modal('End shift',`<dl class="kv"><dt>Line</dt><dd>${esc(S.shift.line)}</dd><dt>Units</dt><dd class="num">${S.shift.count}</dd><dt>Uptime</dt><dd class="num">${st.uptime}%</dd><dt>Running MO</dt><dd>${mo?`${mo.mo} · ${esc(mo.item)} (${S.shift.moCount} this run)`:'—'}</dd></dl>
    ${needReason().length?`<p style="color:var(--warn)">${needReason().length} downtime entries still need a reason. You can end the shift, but the report will show them as not entered.</p>`:''}
    <label class="f">Issues for the report<textarea id="esIssue" placeholder="Optional">${esc(S.shift.endIssues||'')}</textarea></label>`,
    `${needReason().length?'<button class="btn" data-action="go" data-page="downtime">Enter downtime first</button>':''}<button class="btn danger" data-action="doEndShift">End shift and see report</button>`);
}
function openAlarms(){
  const a=alarms();
  modal('Alarms',a.length?`<div style="display:flex;flex-direction:column;gap:8px">${a.map(x=>`<div class="panel row" style="border-color:var(--${x.sev})"><span class="grow">${esc(x.text)}</span>${x.notice?`<button class="btn ghost" data-action="ackOne" data-id="${x.notice}">Acknowledge</button>`:''}<button class="btn" data-action="go" data-page="${x.go}">Open</button></div>`).join('')}</div>`:'<div class="empty">No active alarms.</div>');
}
/* ===================== actions (every change goes to the server) ===================== */
const close=()=>{if(ui.modal==='downPrompt')ui.downSnooze=Date.now()+5*MIN;closeModal()};
const confirmBox=(title,body,yes,action,attrs='')=>modal(title,body,`<button class="btn ghost" data-action="close">Cancel</button><button class="btn danger" data-action="${action}" ${attrs}>${yes}</button>`);
function rowsTable(rows,cols){rows=rows||[];cols=cols&&cols.length?cols:(rows[0]?Object.keys(rows[0]):[]);
  return `<div class="tblwrap" style="margin-top:8px;max-height:320px;overflow:auto"><table class="xl"><thead><tr>${cols.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${cols.map(c=>`<td class="l">${esc(r[c]==null?'':typeof r[c]==='object'?JSON.stringify(r[c]):r[c])}</td>`).join('')}</tr>`).join('')||`<tr><td colspan="${Math.max(1,cols.length)}">No rows</td></tr>`}</tbody></table></div>`}
const A={
  toggleNav:()=>$('#nav').classList.toggle('open'),
  go:el=>{$('#modal').hidden=true;ui.modal=null;go(el.dataset.page)},
  close,
  alarms:()=>openAlarms(),
  login:async()=>{if(user()){await API.logout();toast('Logged out')}else openLogin(null)},
  adminLogin:()=>openLogin(()=>render(true),true),
  pickUser:el=>{pending.pick=el.dataset.id;document.querySelectorAll('.users button').forEach(b=>b.classList.toggle('on',b===el));$('#pin').focus()},
  doLogin:async()=>{
    const r=await API.login(pending.pick,$('#pin').value.trim(),pending.adminOnly);
    if(!r.ok){$('#loginErr').textContent=r.error;$('#pin').select();return}
    const n=pending.next; $('#modal').hidden=true; ui.modal=null; render(true); toast('Logged in as '+user().name); if(n) n();
  },
  startShift:()=>{pending=null;openStartShift()}, changeover:()=>openChangeover(), qcCheck:()=>openQC(), rollTags:()=>openRolls(), maintTicket:()=>openTicket(), drawing:()=>openDrawing(), shiftNotes:()=>openNotes(), endShift:()=>openEndShift(), adminSetting:()=>openAdminSetting(),
  pickMO:el=>{$('#ssMO').value=el.dataset.mo},
  pickCO:el=>{$('#coMO').value=el.dataset.mo},
  doStartShift:async()=>{
    const line=$('#ssLine').value, safety=[...document.querySelectorAll('.saf')].map(c=>c.checked), pm=[...document.querySelectorAll('.pmc')].map(c=>c.checked);
    const r=await API.cmd('startShift',{line,mo:$('#ssMO').value.trim(),issues:$('#ssIssue').value,safety,pm});
    if(!r.ok){$('#ssErr').textContent=r.error;return}
    station=line; LS.set('hmi-station',line); derive(); $('#modal').hidden=true; ui.modal=null; go('controls'); toast(r.msg);
  },
  cancelCO:async()=>{if(await send('coCancel',{},{quiet:true}))closeModal()},
  coEnd:async()=>{const r=await send('coEnd',{qty:$('#coQty')?+$('#coQty').value:0,scrap:$('#coScrap')?+$('#coScrap').value:0,close:$('#coClose')?$('#coClose').checked:false},{quiet:true});if(r)openChangeover()},
  coPick:async()=>{const r=await API.cmd('coPick',{mo:$('#coMO').value});if(!r.ok){$('#coErr').textContent=r.error;return}openChangeover()},
  coReload:async()=>{await send('coReload',{},{quiet:true});openChangeover()},
  coFinish:async()=>{const r=await API.cmd('coFinish',{});if(!r.ok){$('#coErr').textContent=r.error;return}closeModal();toast(r.msg)},
  seg:el=>{el.parentElement.querySelectorAll('button').forEach(b=>b.classList.toggle('on',b===el))},
  saveQC:async()=>{
    const g=id=>$(id).value;
    const r=await API.cmd('qcSave',{len:g('#qLen'),wid:g('#qWid'),ph:g('#qPh'),sbl:g('#qSbl'),sbr:g('#qSbr'),glue:$('#qGlue .on').dataset.v,cut:$('#qCut .on').dataset.v,scrap:+$('#qScrap').value||0});
    if(!r.ok){$('#qErr').textContent=r.error;return} closeModal(); toast(r.msg);
  },
  saveRoll:async()=>{const r=await send('rollAdd',{roll:$('#rId').value,material:$('#rMat').value,lot:$('#rLot').value,feet:$('#rFt').value});if(r)openRolls()},
  saveTicket:async()=>{const pics=ui.tPhotos||[];const r=await send('ticketAdd',{machine:$('#tMach').value,type:$('#tType').value,priority:$('#tPri').value,notes:$('#tNotes').value},{quiet:pics.length>0});if(!r)return;
    let bad=0;for(const [i,p] of pics.entries()){const m=$('#tPicMsg');if(m)m.textContent=`Sending picture ${i+1} of ${pics.length}…`;const u=await uploadPhoto(r.data.id,p.blob);if(!u.ok)bad++}
    pics.forEach(p=>URL.revokeObjectURL(p.url));ui.tPhotos=[];closeModal();
    toast(bad?`Ticket sent, but ${bad} picture${bad>1?'s':''} didn't upload. Add ${bad>1?'them':'it'} from the Maintenance page.`:pics.length?`Maintenance ticket sent with ${pics.length} picture${pics.length>1?'s':''}`:r.msg||'Maintenance ticket sent');Remote.get(true)},
  tPhotoDel:el=>{const p=ui.tPhotos.splice(+el.dataset.i,1)[0];if(p)URL.revokeObjectURL(p.url);drawTicketPics()},
  photoView:el=>openPhoto(+el.dataset.id),
  errLog:el=>{const d=S.downtime.find(x=>x.id===+el.dataset.id);if(d)errLog(d)},
  ticketStatus:el=>send('ticketStatus',{id:el.dataset.id},{quiet:true}),
  getDrawing:async el=>{const r=await send('getDrawing',{part:el.dataset.k},{quiet:true});if(r){openDrawing();const n=$('#drawNote');if(n)n.textContent=r.msg}},
  saveOverride:async()=>{const v=id=>$(id).value.trim();const r=await send('overrideSet',{ppm:v('#oPpm'),pleats:v('#oPl'),ph:v('#oPh'),glue:v('#oGlue'),why:v('#oWhy')});if(r)closeModal()},
  clearOverride:async()=>{if(await send('overrideClear',{},{quiet:true}))closeModal()},
  saveNote:async()=>{const r=await send('noteAdd',{text:$('#nText').value},{quiet:true});if(r)openNotes()},
  doEndShift:async()=>{const r=await send('endShift',{issues:$('#esIssue').value});if(r){$('#modal').hidden=true;ui.modal=null;go('end-of-shift')}},
  copyReport:()=>{const t=reportText();copyText(t).then(ok=>{if(ok)toast('Report copied');else modal('Report',`<pre style="white-space:pre-wrap;user-select:text">${esc(t)}</pre><p class="note">Select the text and copy it.</p>`)})},
  ackSched:()=>send('ackNotices',{},{quiet:true}),
  ackOne:async el=>{await send('ackNotices',{id:el.dataset.id},{quiet:true});openAlarms()},
  schedView:el=>{ui.schedView=el.dataset.v;ui.ganttX=null;render(true)},
  schedFilter:el=>{ui.schedFilter=el.dataset.v;render(true)},
  sort:el=>{const k=el.dataset.k;ui.sort=ui.sort&&ui.sort[0]===k?[k,-ui.sort[1]]:[k,1];render(true)},
  autoQC:el=>send('settingSet',{k:'autoQC',v:el.dataset.v==='1'},{quiet:true}),
  pullS2K:()=>{toast('Pulling MOs from S2K…');send('s2kPull')},
  pullPeople:()=>send('peoplePull'),
  pasteCSV:()=>modal('Paste MOs from S2K',`<p class="sub">One MO per line: MO, Item, Qty, Due (YYYY-MM-DD), Process, MFG/OUT</p><textarea id="csv" style="min-height:180px" placeholder="309300,MG4062HP,2400,2026-04-08,BARB,MFGOUT"></textarea>`,`<button class="btn go" data-action="doCSV">Import</button>`),
  doCSV:async()=>{const rows=$('#csv').value.split(/\n/).map(l=>l.split(',').map(s=>s.trim())).filter(r=>r[0]&&!isNaN(+r[0]));const r=await send('importMOs',{list:rows.map(r=>({mo:+r[0],item:r[1],qty:+r[2],due:r[3],process:r[4]||'BARB',mfg:r[5]||'MFGOUT'}))});if(r)closeModal()},
  resetData:el=>{const m=el.dataset.mode;confirmBox(m==='demo'?'Load demo data?':'Clear demo data and go live?',m==='demo'
      ?'<p>Replaces everything on the server with the example plant: MOs, shifts, records and people. Use this only for training.</p>'
      :'<p>Removes all MOs, shifts, downtime, QC, rolls, tickets and notes, and turns off the demo machine feeds. Machines, people, training, settings and connections stay.</p><p>If the server was running on demo data, its demo history is deleted too.</p>',
      m==='demo'?'Load demo data':'Clear and go live','doResetData',`data-mode="${m}"`)},
  doResetData:async el=>{const r=await send('dataReset',{mode:el.dataset.mode});if(r){closeModal();go('controls')}},
  addUser:()=>modal('Add person',`<div class="grid2"><label class="f">Name<input type="text" id="uName" autofocus></label><label class="f">PIN<input type="text" id="uPin"></label><label class="f">Role<select id="uRole"><option value="operator">operator</option><option value="maintenance">maintenance</option><option value="admin">shift lead</option></select></label></div><p class="note" id="uErr" style="color:var(--bad)"></p>`,`<button class="btn go" data-action="saveUser">Add</button>`),
  saveUser:async()=>{const r=await send('userAdd',{name:$('#uName').value,pin:$('#uPin').value,role:$('#uRole').value},{err:'#uErr'});if(r)closeModal()},
  testConn:el=>{toast('Testing…');send('testConn',{k:el.dataset.k},{quiet:true}).then(r=>{if(r&&r.data)toast((r.data.ok?'OK · ':'Failed · ')+r.data.msg)})},
  testAll:()=>{toast('Testing all connections…');send('testAll')},
  paycomNow:()=>{toast('Importing from Paycom…');send('paycomImport')},
  paycomFields:async()=>{const r=await send('paycomFields',{},{quiet:true});if(r){ui.pcFields=r.data;render(true)}},
  dbConnect:async()=>{
    ui.dbMsg='Connecting…';render(true);
    const a=await API.cmd('dbSchema',{});
    if(!a.ok){ui.dbSchema=null;ui.dbMsg='Could not connect: '+a.error;render(true);return}
    const q=await API.cmd('dbQueries',{}); ui.dbSchema=a.data; ui.dbSaved=q.ok?q.data:{};
    Object.keys(E.QDEF).forEach(n=>{if(!ui.dbSql[n]&&ui.dbSaved[n])ui.dbSql[n]=ui.dbSaved[n].sql});
    ui.dbMsg=`Connected · ${a.data.tables.length} tables`; render(true);
  },
  dbBuild:el=>{const n=el.dataset.q;try{ui.dbSql[n]=buildSQL(n);ui.dbRes[n]=null;render(true)}catch(e){toast(e.message)}},
  dbTest:async el=>{const n=el.dataset.q,sql=($('#sql-'+n)||{}).value||'';ui.dbSql[n]=sql;const tp=ui.dbTP[n];
    const r=await API.cmd('dbTest',{sql,params:{...tp,mo:'',line_code:(lineOf(tp.machine)||{}).code||''}});ui.dbRes[n]=r.ok?r.data:{error:r.error};render(true)},
  dbSave:async el=>{const n=el.dataset.q,sql=($('#sql-'+n)||{}).value||'';ui.dbSql[n]=sql;
    const r=await API.cmd('dbSave',{name:n,sql,params:ui.dbTP[n]});
    if(!r.ok){ui.dbRes[n]={error:'Not saved: '+r.error};render(true);return}
    const q=await API.cmd('dbQueries',{});ui.dbSaved=q.ok?q.data:{};toast('Saved on the server');render(true)},
  dbDrop:async el=>{const n=el.dataset.q;await send('dbSave',{name:n,sql:null});const q=await API.cmd('dbQueries',{});ui.dbSaved=q.ok?q.data:{};render(true)},
  adminTab:el=>{ui.alerts=null;ui.alertEdit=null;ui.mailForm=null;ui.smsForm=null;ui.alertMsg=null;ui.adminTab=el.dataset.v;ui.netDraft=null;ui.connInfo=null;ui.srcs=null;ui.srcEdit=null;ui.srcKey=null;ui.srcTest=null;ui.connForm=null;ui.connMsg=null;ui.newFeedKey=null;render(true)},
  updUpload:async()=>{const f=ui.updFile;if(!f){toast('Choose the update file first.');return}
    const F=ui.updForm||{};if(F.when==='at'&&!F.at){toast('Pick the date and time.');return}
    if(F.when==='now'&&!confirm('Install this update now? The suite stops for a minute or two while it installs.'))return;
    ui.updMsg='Uploading and checking…';render(true);
    const fd=new FormData();fd.append('file',f);fd.append('when',F.when||'now');fd.append('at',F.at||'');
    let r;try{const res=await fetch('/api/updates/upload',{method:'POST',credentials:'same-origin',headers:{'X-CSRFToken':csrf()},body:fd});r=await res.json()}catch(e){r={ok:false,error:"Couldn't reach the server."}}
    ui.updMsg=r.ok?r.msg:r.error;if(r.ok){ui.upd=r.data;ui.updFile=null}render(true)},
  updWhen:async el=>{const w=el.dataset.w,F=ui.updForm||{};if(w==='at'&&!F.at){toast('Pick the date and time.');return}
    if(w==='now'&&!confirm('Install the update now? The suite stops for a minute or two while it installs.'))return;
    const r=await API.cmd('updWhen',{when:w,at:F.at||''});if(r.ok){ui.upd=r.data}else toast(r.error);render(true)},
  updCancel:async()=>{if(!confirm('Remove the waiting update?'))return;const r=await API.cmd('updCancel',{});if(r.ok)ui.upd=r.data;else toast(r.error);render(true)},
  updGoBack:async()=>{if(!confirm('Go back to the previous version? The suite stops for a minute or two.'))return;const r=await API.cmd('updGoBack',{});if(r.ok){ui.upd=r.data;toast(r.msg)}else toast(r.error);render(true)},
  connTest:async el=>{const g=el.dataset.g;ui.connMsg=ui.connMsg||{};ui.connMsg[g]={ok:true,text:'Testing…'};render(true);
    const r=await API.cmd('connTest',{group:g,fields:ui.connForm[g]});ui.connMsg[g]=r.ok?{ok:r.data.ok,text:r.data.msg+(r.data.writable?' Note: this login is allowed to change data; a read-only login is safer.':'')}:{ok:false,text:r.error};render(true)},
  connSave:async el=>{const g=el.dataset.g;if(g==='machines'||g==='assets')ui.mtables={};ui.connMsg=ui.connMsg||{};ui.connMsg[g]={ok:true,text:'Saving…'};render(true);
    const r=await send('connSave',{group:g,fields:ui.connForm[g]},{quiet:true,retry:true});
    if(!r){ui.connMsg[g]={ok:false,text:'Not saved. '+(($('#toast')||{}).textContent||'')};render(true);return}
    ui.connInfo=r.data;if(ui.connForm[g]){ui.connForm[g].password='';ui.connForm[g].token=''}ui.connMsg[g]={ok:true,text:r.msg};render(true)},
  connRemove:async el=>{const g=el.dataset.g;if(!confirm('Remove this connection? Anything that uses it stops until it is set up again.'))return;
    const r=await send('connRemove',{group:g},{quiet:true});if(r){ui.connInfo=r.data;ui.connForm=null;ui.connMsg={[g]:{ok:true,text:r.msg}};render(true)}},
  feedKeyNew:async()=>{if(ui.connInfo&&ui.connInfo.feed.on&&!confirm('Make a new feed key? Gateways using the old key stop being accepted until they get the new one.'))return;
    const r=await send('connSave',{group:'feed',fields:{}},{quiet:true});if(r){ui.connInfo=r.data;ui.newFeedKey=r.key;ui.connMsg={feed:{ok:true,text:r.msg}};render(true)}},
  srcNew:()=>{ui.srcEdit={name:'',kind:'qc',line:'',mode:'push',url:'',every:60,enabled:true,mapping:{units:'in'},fields:[]};render(true)},
  srcEdit:el=>{const x=ui.srcs.find(s=>s.id===+el.dataset.id);if(!x)return;ui.srcEdit=JSON.parse(JSON.stringify(x));render(true)},
  srcCancel:()=>{ui.srcEdit=null;render(true)},
  srcSave:async()=>{const r=await send('srcSave',{src:ui.srcEdit},{err:'#srcErr'});if(!r)return;ui.srcEdit=null;ui.srcs=null;if(r.key)ui.srcKey={name:r.data.name,key:r.key};toast(r.msg);render(true)},
  srcKey:async el=>{if(!confirm('Make a new key? The system sending data must be given the new key; the old one stops working now.'))return;
    const r=await send('srcNewKey',{id:+el.dataset.id},{quiet:true});if(r){ui.srcKey={name:r.data.name,key:r.key};render(true)}},
  srcKeyDone:()=>{ui.srcKey=null;render(true)},
  srcCopyKey:()=>copyText(ui.srcKey?ui.srcKey.key:'').then(ok=>toast(ok?'Key copied':'Select the key and copy it')),
  srcDel:async el=>{if(!confirm('Remove this data source? The data it already sent is kept.'))return;const r=await send('srcDelete',{id:+el.dataset.id});if(r){ui.srcs=null;render(true)}},
  srcTest:async el=>{toast('Testing…');const r=await send('srcTest',{id:+el.dataset.id},{quiet:true});if(r){ui.srcTest={name:r.data.src.name,...r.data};ui.srcs=null;render(true)}},
  srcTestDone:()=>{ui.srcTest=null;render(true)},
  downPrompt:()=>openDownPrompt(),
  downLogin:()=>openLogin(()=>openDownPrompt()),
  alertNew:()=>{ui.alertEdit={name:'',email:'',phone:'',lines:[],downAfter:15,notifyBack:true,active:true};render(true)},
  alertEdit:el=>{const c=ui.alerts.contacts.find(x=>x.id===+el.dataset.id);if(c){ui.alertEdit=JSON.parse(JSON.stringify(c));render(true)}},
  alertCancel:()=>{ui.alertEdit=null;render(true)},
  alertSave:async()=>{const r=await send('alertContactSave',{contact:ui.alertEdit},{err:'#alertErr'});if(r){ui.alertEdit=null;ui.alerts=null;render(true)}},
  alertDel:async el=>{if(!confirm('Remove this person from downtime alerts?'))return;const r=await send('alertContactDelete',{id:+el.dataset.id});if(r){ui.alerts=null;render(true)}},
  alertTest:async el=>{toast('Sending a test…');const r=await API.cmd('alertTest',{id:+el.dataset.id});ui.alertMsg={people:{ok:r.ok,text:r.ok?r.msg:r.error}};ui.alerts=null;render(true)},
  mailSave:async()=>{const r=await API.cmd('alertConfigSave',{group:'smtp',fields:ui.mailForm});ui.alertMsg={smtp:{ok:r.ok,text:r.ok?r.msg:r.error}};if(r.ok){ui.alerts=null;ui.mailForm=null}render(true)},
  mailRemove:async()=>{if(!confirm('Remove the mail server? Email alerts stop.'))return;const r=await API.cmd('alertConfigSave',{group:'smtp-remove'});ui.alertMsg={smtp:{ok:r.ok,text:r.ok?r.msg:r.error}};ui.alerts=null;ui.mailForm=null;render(true)},
  smsSave:async()=>{const r=await API.cmd('alertConfigSave',{group:'sms',fields:ui.smsForm});ui.alertMsg={sms:{ok:r.ok,text:r.ok?r.msg:r.error}};if(r.ok){ui.alerts=null;ui.smsForm=null}render(true)},
  smsRemove:async()=>{if(!confirm('Remove Twilio? Text alerts stop.'))return;const r=await API.cmd('alertConfigSave',{group:'sms-remove'});ui.alertMsg={sms:{ok:r.ok,text:r.ok?r.msg:r.error}};ui.alerts=null;ui.smsForm=null;render(true)},
  copySql:el=>{const pre=el.closest('div').parentElement.querySelector('pre');copyText(pre?pre.textContent:'').then(ok=>toast(ok?'Query copied':'Select the query and copy it'))},
  copyStation:el=>copyText(stationURL(el.dataset.id)).then(ok=>toast(ok?'Address copied':'Select the address and copy it')),
  copyFeedKey:()=>copyText(ui.newFeedKey||'').then(ok=>toast(ok?'Key copied':'Select the key and copy it')),
  s2kBuild:()=>{const K=ui.s2k;try{K.sql=s2kBuildSQL(K.bld||{});K.res=null;K.msg='SQL written. Run test to check it.';K.err=false}catch(e){toast(e.message)}render(true)},
  s2kTables:async()=>{const K=ui.s2k;if(K.schema){K.schema=null;render(true);return}const r=await API.cmd('dbSchema',{db:'s2k'});if(r.ok){K.schema=r.data;K.msg=`Connected (read-only) · ${r.data.tables.length} tables in ${r.data.database}`;K.err=false}else{K.msg=r.error;K.err=true}render(true)},
  s2kTest:async()=>{const K=ui.s2k;K.sql=$('#s2kSql').value;const r=await API.cmd('dbTest',{db:'s2k',sql:K.sql});if(r.ok){K.res=r.data;K.msg=`${r.data.rows.length} row${r.data.rows.length===1?'':'s'} in ${r.data.ms} ms${r.data.truncated?' (first 500)':''}. Showing the first 20.`;K.err=false}else{K.res=null;K.msg=r.error;K.err=true}render(true)},
  s2kSave:async()=>{const K=ui.s2k;K.sql=$('#s2kSql').value;const r=await API.cmd('dbSave',{name:'s2k_mos',sql:K.sql});if(!r.ok){K.msg='Not saved: '+r.error;K.err=true;render(true);return}
    const q=await API.cmd('dbQueries',{});K.saved=q.ok?q.data.s2k_mos||null:null;K.msg='Saved. Pulls now use this query.';K.err=false;render(true)},
  netAdd:()=>{const el=$('#netAdd'),v=(el&&el.value||'').trim();if(!v)return;const d=(ui.netDraft||S.network.allowed.map(a=>a.range)).slice();if(!d.includes(v))d.push(v);ui.netDraft=d;render(true)},
  netDel:el=>{const d=(ui.netDraft||S.network.allowed.map(a=>a.range)).slice();d.splice(+el.dataset.i,1);if(d.length)ui.netDraft=d;render(true)},
  netUndo:()=>{ui.netDraft=null;render(true)},
  netDefault:()=>{ui.netDraft=[S.network.default];render(true)},
  netSave:async()=>{const d=(ui.netDraft||[]).map(x=>String(x).trim()).filter(Boolean);if(!d.length){$('#netErr').textContent='Keep at least one range.';return}
    const r=await send('networkSet',{allowed:d},{err:'#netErr'});if(r){ui.netDraft=null;render(true)}},
  netFw:()=>send('networkFirewallSync'),
  lstAdd:async el=>{const k=el.dataset.k,inp=$('#la-'+k),v=inp.value.trim();if(!v){inp.focus();return}const r=await send('listAdd',{k,v},{quiet:true});if(r)setTimeout(()=>{const x=$('#la-'+k);if(x)x.focus()},0)},
  lstDel:el=>send('listDel',{k:el.dataset.k,i:+el.dataset.i}),
  lstMove:el=>send('listMove',{k:el.dataset.k,i:+el.dataset.i,d:+el.dataset.d},{quiet:true}),
  addLine:async()=>{const r=await send('lineAdd',{},{quiet:true});if(r&&r.data)setTimeout(()=>{const x=[...document.querySelectorAll('[data-f="id"]')].find(i=>i.dataset.id===r.data.id);if(x){x.focus();x.select()}},50)},
  delLine:el=>{const id=el.dataset.id,q=S.mos.filter(m=>m.line===id&&m.status==='OPEN').length;
    confirmBox(`Remove ${esc(id)}?`,`<p>${q?`${q} queued MO${q>1?'s':''} will go back to Unscheduled. `:''}Training and feed rates for this machine are deleted. Past downtime, QC and reports keep the name.</p><p class="note">To stop it temporarily, untick In service instead.</p>`,'Remove machine','doDelLine',`data-id="${esc(id)}"`)},
  doDelLine:async el=>{if(await send('lineDel',{id:el.dataset.id},{quiet:true}))closeModal()},
  addShift:()=>send('shiftAdd',{},{quiet:true}),
  delShift:el=>{const sh=C().shifts[+el.dataset.s];confirmBox(`Remove ${esc(sh.name)}?`,`<p>${esc(sh.name)} (${esc(sh.start)}–${esc(sh.end)}) and its breaks come off the schedule timeline. Past reports keep the name.</p>`,'Remove shift','doDelShift',`data-s="${el.dataset.s}"`)},
  doDelShift:async el=>{if(await send('shiftDel',{s:+el.dataset.s},{quiet:true}))closeModal()},
  addBreak:el=>send('breakAdd',{s:+el.dataset.s},{quiet:true}),
  delBreak:el=>send('breakDel',{s:+el.dataset.s,b:+el.dataset.b},{quiet:true}),
  addPart:()=>{const v=($('#newPart')||{}).value;if(v&&v.trim())send('partAdd',{k:v.trim()},{quiet:true})},
  delPart:el=>send('partDel',{k:el.dataset.k}),
  copyCfg:()=>{const t=$('#expJson');copyText(t.value).then(ok=>{if(ok)toast('Settings copied');else{t.focus();t.select();toast('Text selected: press Ctrl+C, or long-press and choose Copy')}})},
  importCfg:()=>{let o;try{o=JSON.parse($('#impJson').value)}catch(e){$('#impErr').textContent='That is not valid settings text. Copy it again from the other station.';return}
    if(!o||!o.config||!Array.isArray(o.lines)){$('#impErr').textContent='Settings text is missing the machines or config section.';return}
    ui.imp=o; confirmBox('Load these settings?',`<p>This replaces machines, shifts, reasons, checklists, parts, rules and feed rates with ${o.lines.length} machines and ${o.config.shifts?o.config.shifts.length:0} shifts from the pasted settings. People are added if they are new (without PINs); existing PINs are kept.</p>`,'Load settings','doImport')},
  doImport:async()=>{if(await send('configImport',{data:ui.imp})){ui.imp=null;closeModal()}},
  resetCfg:()=>confirmBox('Restore default settings?','<p>Reasons, checklists, shifts, parts and rules go back to the defaults. Machines, people, MOs and records are not changed.</p>','Restore defaults','doResetCfg'),
  doResetCfg:async()=>{if(await send('configReset',{},{quiet:true}))closeModal()},
};
const sendCH=(n,p)=>send(n,p,{restore:true,quiet:true});
const CH={
  alertF:el=>{const E=ui.alertEdit;if(E)E[el.dataset.f]=el.type==='checkbox'?el.checked:el.value},
  alertLine:el=>{const E=ui.alertEdit;if(!E)return;const l=el.dataset.line;E.lines=E.lines.filter(x=>x!==l);if(el.checked)E.lines.push(l)},
  mailF:el=>{ui.mailForm[el.dataset.f]=el.type==='checkbox'?el.checked:el.value},
  smsF:el=>{ui.smsForm[el.dataset.f]=el.value},
  srcF:el=>{const E=ui.srcEdit;if(!E)return;const f=el.dataset.f;E[f]=el.type==='checkbox'?el.checked:el.value;if(f==='kind'||f==='mode'||f==='line')render(true)},
  srcMap:el=>{const E=ui.srcEdit;if(!E)return;const k=el.dataset.k;E.mapping[k]=k==='fields'?el.value.split(',').map(x=>x.trim()).filter(Boolean):el.value},
  updFile:el=>{ui.updFile=el.files&&el.files[0]||null},
  tPhotos:async el=>{const files=[...(el.files||[])];el.value='';ui.tPhotos=ui.tPhotos||[];const m=$('#tPicMsg');
    for(const f of files){if(ui.tPhotos.length>=PHOTO_LIMIT){if(m)m.textContent=`Up to ${PHOTO_LIMIT} pictures.`;break}
      try{const b=await shrinkPhoto(f);ui.tPhotos.push({blob:b,url:URL.createObjectURL(b)})}catch(e){if(m)m.textContent=e.message}}
    drawTicketPics()},
  ticketPhotoAdd:async el=>{const files=[...(el.files||[])];el.value='';if(!files.length)return;if(!user()){openLogin();return}
    let n=0,err='';for(const f of files){try{const r=await uploadPhoto(el.dataset.id,await shrinkPhoto(f));if(r.ok)n++;else err=r.error}catch(e){err=e.message}}
    toast(n?`${n} picture${n>1?'s':''} added${err?' · '+err:''}`:err||'No picture added');Remote.get(true)},
  updF:el=>{(ui.updForm=ui.updForm||{})[el.dataset.f]=el.value},
  updFR:el=>{(ui.updForm=ui.updForm||{})[el.dataset.f]=el.value;render(true)},
  updFolder:async el=>{const r=await API.cmd('updFolder',{when:el.value});if(r.ok){ui.upd=r.data;toast('Saved')}else toast(r.error);render(true)},
  connFR:el=>{const g=el.dataset.g;ui.connForm=ui.connForm||{};ui.connForm[g]=ui.connForm[g]||{};ui.connForm[g][el.dataset.f]=el.value;render(true)},
  connF:el=>{const g=el.dataset.g;ui.connForm=ui.connForm||{};ui.connForm[g]=ui.connForm[g]||{};ui.connForm[g][el.dataset.f]=el.value},
  s2kBld:el=>{const K=ui.s2k=ui.s2k||{};const B=K.bld=K.bld||{};B[el.dataset.f]=el.value;if(el.dataset.f==='table'){for(const [k] of S2K_ROLES)B[k]='';B.fcol=''}if(el.dataset.f==='table'||el.tagName==='SELECT')render(true)},
  s2kSql:el=>{(ui.s2k=ui.s2k||{}).sql=el.value},
  netEd:el=>{const d=(ui.netDraft||S.network.allowed.map(a=>a.range)).slice();d[+el.dataset.i]=el.value.trim();ui.netDraft=d;render(true)},
  station:el=>{station=el.value;LS.set('hmi-station',station);derive();ui.ganttX=null;render(true)},
  ssLine:el=>{pending={...(pending||{}),line:el.value};openStartShift()},
  coStep:el=>send('coStep',{i:+el.dataset.i,checked:el.checked},{quiet:true}),
  qcIn:el=>{const v=parseFloat(el.value);el.classList.remove('in','out');if(!isNaN(v))el.classList.add(Math.abs(v-+el.dataset.spec)<=+el.dataset.tol+1e-9?'in':'out')},
  dReason:el=>{
    if(el.value==='__other'){const v=(prompt('Type the reason for this stop:')||'').trim();if(!v){render(true);return}
      send('downtimeSet',{id:el.dataset.id,reason:v.slice(0,120)},{restore:true}).then(()=>{if(ui.modal==='downPrompt')openDownPrompt()});return}
    send('downtimeSet',{id:el.dataset.id,reason:el.value||null},{restore:true}).then(()=>{if(ui.modal==='downPrompt')setTimeout(openDownPrompt,50)})},
  dNotes:el=>sendCH('downtimeSet',{id:el.dataset.id,notes:el.value}),
  qcEvery:el=>sendCH('settingSet',{k:'qcEvery',v:+el.value}),
  set:el=>sendCH('settingSet',{k:el.dataset.k,v:el.type==='checkbox'?el.checked:el.value}),
  role:el=>sendCH('userSet',{id:el.dataset.id,f:'role',v:el.value}),
  train:el=>sendCH('userSet',{id:el.dataset.id,f:'train',v:{line:el.dataset.line,on:el.checked}}),
  pin:el=>sendCH('userSet',{id:el.dataset.id,f:'pin',v:el.value.trim()}),
  uactive:el=>sendCH('userSet',{id:el.dataset.id,f:'active',v:el.checked}),
  showInactive:el=>{ui.showInactive=el.checked;render(true)},
  lst:el=>sendCH('listSet',{k:el.dataset.k,i:+el.dataset.i,v:el.value}),
  lineF:el=>sendCH('lineSet',{id:el.dataset.id,f:el.dataset.f,v:el.type==='checkbox'?el.checked:el.value}),
  mdTest:async(el)=>{const id=el.dataset.id,w=el.dataset.w||'feed',k=w==='feed'?id:id+'|'+w;ui.feedTest=ui.feedTest||{};ui.feedTest[k]={ok:true,text:'Reading…'};render(true);
    const r=await API.cmd('lineFeedTest',{id,which:w});
    if(!r.ok){ui.feedTest[k]={ok:false,text:r.error||'Failed'};render(true);return}
    const d=r.data;ui.feedTest[k]={ok:true,text:w==='err'?(d.rows.length?'Newest alarm rows:\n'+d.rows.map(x=>`${x.at||'row '+x.row}  ${x.code}${x.code&&x.text?' · ':''}${x.text}`).join('\n'):'The alarm log table is empty.'):w==='uptime'?`Newest row ${d.newest}: ${d.run?'running':'stopped'}.`
      :`Newest row ${d.newest}: ${d.ppm} P/m${d.count!=null?` · filters cut counter ${d.count}`:''}${d.recipe?` · recipe ${d.recipe}`:''}${typeof d.run==='boolean'?` · ${d.run?'running':'stopped'}`:''}${d.runErr?` · ${d.runErr}`:''}.`};render(true)},
  shiftF:el=>sendCH('shiftSet',{s:+el.dataset.s,f:el.dataset.f,v:el.value}),
  brkF:el=>sendCH('breakSet',{s:+el.dataset.s,b:+el.dataset.b,f:el.dataset.f,v:el.value}),
  partF:el=>sendCH('partSet',{k:el.dataset.k,f:el.dataset.f,v:el.value}),
  rule:el=>sendCH('ruleSet',{k:el.dataset.k,v:el.value}),
  kioskPanels:el=>{const list=el.value.split(/\n/).map(l=>l.trim()).filter(Boolean).map(l=>{const m=l.match(/^(.*?)[=:]\s*(\d+)\s*$/);return m?{title:m[1].trim()||'Panel '+m[2],panel:+m[2]}:null}).filter(Boolean);sendCH('settingSet',{k:'kiosk.panels',v:list})},
  dbTable:async el=>{ui.dbTable=el.value;ui.dbPrev=null;render(true);if(!el.value)return;const r=await API.cmd('dbPreview',{table:el.value});ui.dbPrev=r.ok?r.data:{rows:[{error:r.error}],columns:['error']};render(true)},
  bldF:el=>{const B=ui.bld[el.dataset.q];B[el.dataset.f]=el.value;
    if(el.dataset.f==='table'){for(const k in B)if(k!=='table')delete B[k];
      const real=((ui.dbSchema.tables.find(t=>t.name===el.value)||{}).columns||[]).map(c=>c.name), cols=real.map(c=>c.toLowerCase());
      const guess={step:['step','instruction','step_text','description','text'],seq:['seq','step_no','step_number','sort','sort_order','sequence'],detail:['detail','details','note','notes'],setting:['setting','setting_name','parameter'],value:['value','setting_value','target'],unit:['unit','units','uom'],asset:['asset','asset_id','tool','tool_id'],image:['image','image_url','photo','photo_url'],name:['name','setting','setting_name','parameter'],
        rev:['rev','revision','drawing_rev'],pleats:['pleats','pleat_count','pleats_per_unit'],len:['length','len','length_in'],wid:['width','wid','width_in'],ph:['pleat_height','ph'],sb:['side_band','side_band_height','sb'],tol:['tol','tolerance'],media:['media','media_type','material'],
        f_part:['part','part_no','part_number','item','sku'],f_machine:['machine','machine_name','line','asset_line'],f_from:['from_part','from_part_no','previous_part']};
      const roles=E.QDEF[el.dataset.q].roles.map(r=>r[0]);
      for(const [k,names] of Object.entries(guess)){if(k==='f_from'&&el.dataset.q!=='changeover_steps')continue;if(k==='f_machine'&&el.dataset.q==='part_spec')continue;if(!k.startsWith('f_')&&!roles.includes(k))continue;const hit=names.find(n=>cols.includes(n));if(hit)B[k]=real[cols.indexOf(hit)]}}
    render(true)},
  dbSql:el=>{ui.dbSql[el.dataset.q]=el.value},
  dbTP:el=>{ui.dbTP[el.dataset.q][el.dataset.f]=el.value.trim()}
};

document.addEventListener('click',e=>{
  const a=e.target.closest('[data-action]');
  if(a){e.preventDefault();A[a.dataset.action]?.(a,e);return}
  if(e.target===$('#modal')){closeModal();return}
});
document.addEventListener('change',e=>{const el=e.target.closest('[data-change]');if(el)CH[el.dataset.change]?.(el)});
document.addEventListener('input',e=>{const el=e.target;if(el.dataset&&el.dataset.change==='qcIn')CH.qcIn(el)});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&!$('#modal').hidden)closeModal();
  if(e.key==='Enter'&&e.target.id==='pin')A.doLogin();
  if(e.key==='Enter'&&e.target.dataset&&e.target.dataset.enter){e.preventDefault();A[e.target.dataset.enter](e.target)}
  if(e.key==='Enter'&&e.target.id==='coMO')A.coPick();
  if(e.key==='Enter'&&e.target.id==='rId'){e.preventDefault();$('#rLot').focus()}
});
// keep a shift lead's session alive while they are using the screen
let lastPing=0;
['pointerdown','keydown'].forEach(ev=>document.addEventListener(ev,()=>{if(API&&user()&&Date.now()-lastPing>30000){lastPing=Date.now();API.ping()}},true));
window.addEventListener('resize',()=>{if(S)afterRender()});
window.addEventListener('hashchange',()=>{const h=location.hash.slice(1);if(VIEWS[h]&&h!==ui.page)go(h)});

/* ===================== boot ===================== */
async function boot(){
  API=Remote;
  const h=location.hash.slice(1); if(VIEWS[h]&&!(STATION_LOCK&&LOCKED_OUT.includes(h))) ui.page=h;
  while(!(await Remote.get(true))){$('#view').innerHTML='<div class="panel empty">Cannot reach the server. Retrying every 5 seconds…</div>';await new Promise(r=>setTimeout(r,5000))}
  booted=true; render(true); Remote.poll();
  setInterval(()=>{if(S){const a=document.activeElement;if(!(a&&a.id==='stationSel'))renderBanner();liveBits()}},1000);
}
boot();
