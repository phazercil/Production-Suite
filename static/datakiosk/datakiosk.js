/* Data Kiosk: live numbers per pleating line plus the Grafana panels set in Operator › Admin › Connections.
 * Polls /api/data/state every 5 s. Grafana frames are created once per line and panel and only
 * reloaded if their address changes, so the wall screen never flickers. */
"use strict";
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtT=ms=>{const d=new Date(ms);let h=d.getHours();const ap=h>=12?"pm":"am";h=h%12||12;return h+":"+String(d.getMinutes()).padStart(2,"0")+" "+ap};
let S=null, SRV=null, recvAt=Date.now(), online=true, rotIdx=0, rotAt=0;
const now=()=>S?S.now+(Date.now()-recvAt):Date.now();

async function load(){
  try{const r=await fetch("/api/data/state",{cache:"no-store",credentials:"same-origin"});const j=await r.json();
    if(r.status===401){location.reload();return}
    S=j.snap;SRV=j.server;recvAt=Date.now();online=true;render()}
  catch(e){online=false;renderStatus()}
}
function grafana(){const g=S.settings.grafana;return g.on&&g.url&&g.uid?g:null}
function panelSrc(g,panel,line){
  const q=new URLSearchParams({orgId:g.org||1,panelId:panel,from:S.settings.kiosk.range||"now-12h",to:"now",refresh:g.refresh||"10s",theme:"dark"});
  if(g.lineVar)q.set("var-"+g.lineVar,line);
  return `${g.url.replace(/\/$/,"")}/d-solo/${encodeURIComponent(g.uid)}/${encodeURIComponent(g.slug||"_")}?${q}`;
}
function dashSrc(g){
  const q=new URLSearchParams({orgId:g.org||1,from:S.settings.kiosk.range||"now-12h",to:"now",refresh:g.refresh||"10s",theme:"dark",kiosk:""});
  return `${g.url.replace(/\/$/,"")}/d/${encodeURIComponent(g.uid)}/${encodeURIComponent(g.slug||"_")}?${q}`;
}
function renderStatus(){
  if(!S)return;
  const g=grafana();
  $("#dkStatus").innerHTML=`<span><i class="dot ${online?"ok":"bad"}"></i>${online?"Server connected":"Server not answering"}</span>
    <span><i class="dot ${g?"ok":"warn"}"></i>${g?"Grafana panels":"Built-in charts"}</span>${S.demo?'<span><i class="dot warn"></i>Demo data</span>':""}
    ${S.planAt?`<span>Plan updated ${fmtT(S.planAt)}</span>`:""}`;
}
function tickClock(){const t=fmtT(now());$("#dkClock").innerHTML=t.replace(/ (am|pm)/,"<small> $1</small>")}

function lineLive(l){
  const R=S.runs[l.id]||{}, sh=R.shift, on=!!(sh&&sh.active), st=R.stats||{uptime:100,down:0}, h=R.hist||[];
  const mo=on?S.mos.find(m=>String(m.id)===String(sh.moId)):null;
  const next=S.mos.filter(m=>m.line===l.id&&m.status==="OPEN"&&!(mo&&m.id===mo.id)).sort((a,b)=>a.seq-b.seq)[0];
  const pill=!on?(l.active===false?'<span class="pill bad">OUT OF SERVICE</span>':'<span class="pill idle">IDLE</span>'):sh.state==="running"?'<span class="pill ok">RUNNING</span>':sh.state==="changeover"?'<span class="pill warn">CHANGEOVER</span>':'<span class="pill bad">STOPPED</span>';
  const ppm=on&&h.length?h[h.length-1].ppm:0, good=mo?Math.max(0,mo.produced-mo.scrap):0, pct=mo&&mo.qty?Math.min(100,good/mo.qty*100):0;
  const feed=R.feed||{};
  return `<div class="nm"><i style="background:${esc(l.color)}"></i>${esc(l.id)}<span style="flex:1"></span>${pill}</div>
    <div class="speed"><b>${on?ppm:"—"}</b><span>pleats / min · rated ${l.rated}</span></div>
    ${mo?`<div class="mo">MO <b>${esc(mo.mo)}</b> · ${esc(mo.item)} · ${good.toLocaleString()} of ${mo.qty.toLocaleString()} good</div><div class="track"><i style="width:${pct}%"></i></div>`
      :`<div class="mo">${on&&sh.state==="changeover"?"Changing over":"No MO running"}${next?` · next <b>${esc(next.mo)}</b> ${esc(next.item)}`:""}</div>`}
    <div class="stats"><div><span>Shift count</span><b>${on?sh.count.toLocaleString():"—"}</b></div><div class="${on&&st.uptime<80?"bad":""}"><span>Uptime</span><b>${on?st.uptime+"%":"—"}</b></div><div class="${on&&st.down>30?"bad":""}"><span>Down</span><b>${on?st.down+" min":"—"}</b></div></div>
    <div class="who">${on?`${esc(sh.userName)} · since ${fmtT(sh.start)}`:"No shift running"}${on&&feed.t&&!feed.ok?` · <span style="color:var(--bad)">No machine data</span>`:""}</div>`;
}
function drawSpark(c,l){
  const R=S.runs[l.id]||{}, h=R.hist||[]; const r=c.getBoundingClientRect(); if(!r.width)return;
  const dpr=window.devicePixelRatio||1; c.width=r.width*dpr; c.height=r.height*dpr; const x=c.getContext("2d"); x.scale(dpr,dpr);
  const W=r.width,H=r.height,L=36,Rr=10,T=14,B=20, css=v=>getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  x.clearRect(0,0,W,H);
  if(!h.length){x.fillStyle=css("--ink-2");x.font="500 14px "+css("--f-body");x.textAlign="center";x.fillText("No readings this shift",W/2,H/2);return}
  const t0=h[0].t,t1=Math.max(h[h.length-1].t,t0+60*6e4),ymax=Math.ceil(Math.max(l.rated,...h.map(p=>p.ppm))*1.1/100)*100;
  const X=t=>L+(t-t0)/(t1-t0)*(W-L-Rr),Y=v=>T+(1-v/ymax)*(H-T-B);
  x.strokeStyle=css("--rule");x.fillStyle=css("--ink-2");x.font="500 10px "+css("--f-mono");x.textAlign="right";x.textBaseline="middle";
  for(let v=0;v<=ymax;v+=ymax/4){x.beginPath();x.moveTo(L,Y(v));x.lineTo(W-Rr,Y(v));x.stroke();x.fillText(Math.round(v),L-5,Y(v))}
  x.textAlign="center";x.textBaseline="alphabetic";const d=new Date(t0);d.setMinutes(0,0,0);for(let tk=d.getTime()+36e5;tk<t1;tk+=36e5)x.fillText(fmtT(tk).replace(":00 ",""),X(tk),H-5);
  x.beginPath();h.forEach((p,i)=>i?x.lineTo(X(p.t),Y(p.ppm)):x.moveTo(X(p.t),Y(p.ppm)));x.lineTo(X(h[h.length-1].t),Y(0));x.lineTo(X(h[0].t),Y(0));x.closePath();x.fillStyle="rgba(91,155,213,.16)";x.fill();
  x.beginPath();h.forEach((p,i)=>i?x.lineTo(X(p.t),Y(p.ppm)):x.moveTo(X(p.t),Y(p.ppm)));x.strokeStyle=css("--accent");x.lineWidth=2;x.lineJoin="round";x.stroke();
  const lp=h[h.length-1];x.beginPath();x.arc(X(lp.t),Y(lp.ppm),3.5,0,7);x.fillStyle=css("--accent");x.fill();
}
function render(){
  renderStatus();
  const g=grafana(), k=S.settings.kiosk||{}, lines=S.lines.filter(l=>l.active!==false);
  $("#dkSetup").hidden=!!g;
  if(!g)$("#dkSetup").innerHTML=`<div class="setup">Grafana is not connected, so these are the built-in charts. A shift lead can connect it in <a href="/operator/#admin">Operator Kiosk › Admin › Connections</a> and pick the panels for this screen there.</div>`;
  const dash=g&&k.layout==="dashboard";
  $("#dkDash").hidden=!dash;
  if(dash){const src=dashSrc(g);const f=$("#dkDash iframe");if(!f||f.dataset.src!==src)$("#dkDash").innerHTML=`<iframe title="Grafana dashboard" data-src="${esc(src)}" src="${esc(src)}"></iframe>`}
  let show=lines;
  if(+k.rotate>0&&lines.length>1){if(Date.now()-rotAt>k.rotate*1000){rotIdx=(rotIdx+1)%lines.length;rotAt=Date.now()}show=[lines[rotIdx%lines.length]]}
  const box=$("#dkLines");
  if(!lines.length){box.innerHTML='<div class="empty">No pleating lines are set up yet.</div>';return}
  const want=show.map(l=>l.id);
  [...box.children].forEach(c=>{if(!want.includes(c.dataset.line))c.remove()});
  const panels=g&&!dash?(k.panels||[]):g?[]:[{title:"Pleater speed · this shift",builtin:true}];
  const cols=Math.max(1,Math.min(+k.cols||3,panels.length||1));
  show.forEach((l,i)=>{
    let band=box.querySelector(`[data-line="${CSS.escape(l.id)}"]`);
    if(!band){band=document.createElement("article");band.className="band";band.dataset.line=l.id;band.innerHTML='<div class="live"></div><div class="panels"></div>';box.appendChild(band)}
    if(box.children[i]!==band)box.insertBefore(band,box.children[i]);
    band.querySelector(".live").innerHTML=lineLive(l);
    const pb=band.querySelector(".panels");pb.style.gridTemplateColumns=`repeat(auto-fit,minmax(min(100%,${cols>2?260:340}px),1fr))`;
    const sig=JSON.stringify(panels.map(p=>p.builtin?"b":panelSrc(g,p.panel,l.id)));
    if(pb.dataset.sig!==sig){pb.dataset.sig=sig;
      pb.innerHTML=panels.map(p=>p.builtin?`<div class="panel"><h3>${esc(p.title)}</h3><canvas aria-label="Pleater speed for ${esc(l.id)}"></canvas></div>`
        :`<div class="panel"><h3>${esc(p.title)}</h3><iframe title="${esc(p.title)} · ${esc(l.id)}" src="${esc(panelSrc(g,p.panel,l.id))}" loading="lazy"></iframe></div>`).join("")||'<div class="empty">Add panels in Operator Kiosk › Admin › Connections.</div>'}
    const c=pb.querySelector("canvas");if(c)drawSpark(c,l);
  });
}
window.addEventListener("resize",()=>{if(S)render()});
load();setInterval(load,5000);setInterval(()=>{tickClock();if(S&&+(S.settings.kiosk||{}).rotate>0)render()},1000);tickClock();
// keep the wall screen awake where the browser allows it
if(navigator.wakeLock)navigator.wakeLock.request("screen").catch(()=>{});
