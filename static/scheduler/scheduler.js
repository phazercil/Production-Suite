"use strict";
/* Scheduler front end.
 * The scheduling engine runs in Django (scheduler/engine.py). This script draws the board from the
 * server's results, lets planners drag stages onto lines, and posts every change back. Saving a
 * change also rebuilds the Stage 1 queues the Operator Kiosk shows as "Up next".
 */
const MIN=6e4, HOUR=36e5, DAY=864e5;
const STAGES={
  s1:{name:"Stage 1 · Pleating",short:"Stage 1",tag:"S1"},
  eb:{name:"Stage 2 · End Bands",short:"End Band",tag:"EB"},
  ac:{name:"Arts & Crafts",short:"Arts & Crafts",tag:"A&C"},
  gb:{name:"Glue Bead",short:"Glue Bead",tag:"GB"}};
const STAGE_ORDER=["s1","eb","ac","gb"];

const $=(s,r=document)=>r.querySelector(s);
const $$=(s,r=document)=>Array.from(r.querySelectorAll(s));
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const clone=o=>JSON.parse(JSON.stringify(o));
const uid=p=>p+Math.random().toString(36).slice(2,9);
const fmtN=n=>Math.round(n).toLocaleString("en-US");
const fmtK=n=>n>=100000?Math.round(n/1000)+"k":n>=10000?(n/1000).toFixed(1).replace(/\.0$/,"")+"k":fmtN(n);
const sod=t=>{const d=new Date(t);d.setHours(0,0,0,0);return d.getTime()};
const addDays=(t,n)=>{const d=new Date(t);d.setDate(d.getDate()+n);return d.getTime()};
const hm=s=>{const [h,m]=String(s||"0:0").split(":").map(Number);return (h||0)*60+(m||0)};
const DOW=["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
const fmtDT=t=>{const d=new Date(t);return `${DOW[d.getDay()]} ${d.getMonth()+1}/${d.getDate()} ${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}`};
const fmtD=t=>{const d=new Date(t);return `${d.getMonth()+1}/${d.getDate()}/${String(d.getFullYear()).slice(2)}`};
const fmtDur=m=>{m=Math.round(m);const h=Math.floor(m/60),mm=m%60;return h?`${h}h ${String(mm).padStart(2,"0")}m`:`${mm}m`};
const toLocalInput=t=>{const d=new Date(t);const p=n=>String(n).padStart(2,"0");return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`};
const isoDay=t=>toLocalInput(t).slice(0,10);
const lsGet=k=>{try{return JSON.parse(localStorage.getItem(k))}catch(e){return null}};
const lsSet=(k,v)=>{try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}};
const jobKey=(moId,op)=>`${moId}__${op}`;

/* ---------- server ---------- */
function csrf(){const m=document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);return m?decodeURIComponent(m[1]):""}
async function api(path,body,isForm){
  try{
    const r=await fetch(path,body===undefined?{credentials:"same-origin",cache:"no-store"}:{method:"POST",credentials:"same-origin",
      headers:isForm?{"X-CSRFToken":csrf()}:{"Content-Type":"application/json","X-CSRFToken":csrf()},body:isForm?body:JSON.stringify(body)});
    const j=await r.json();
    if(j.login){S.me=null;S.editLocked=true;renderWho();openLogin(j.error);return j}
    if(!j.ok&&j.error)toast(j.error);
    setSync(true);return j;
  }catch(e){setSync(false);toast("The server is not answering. Check the network and try again.");return {ok:false}}
}

/* ---------- state ---------- */
const S={cfg:null,modes:{},mos:{},jobs:{},res:{},byM:{},work:{},catalog:{},v:null,loaded:false,
  view:Object.assign({scale:"week",anchor:Date.now(),q:"",showDone:true},lsGet("plp.view")||{})};
let LAST=null;
async function load(force){
  const {a,b}=viewWindow();
  const j=await api(`/api/scheduler/state?from=${a}&to=${b}`);
  if(!j.ok)return;
  S.me=j.me;S.editLocked=j.editLocked;renderWho();
  S.cfg=j.cfg;S.cfgSig=j.cfgSig;S.modes=j.modes;S.mos={};j.mos.forEach(m=>S.mos[m.id]=m);S.jobs=j.jobs;S.res=j.res;S.byM=j.byM;S.work=j.work;S.labor=j.labor||{on:false,lines:{},days:{}};S.catalog=j.catalog;S.v=j.v;S.loaded=true;
  LAST={res:S.res,byM:S.byM};
  requestRender(force);
}
let pollT=null;
function poll(){clearTimeout(pollT);pollT=setTimeout(async()=>{
  if(!document.hidden&&!$("#layer").firstChild&&!saveTimer){const j=await api(`/api/scheduler/state?from=0&to=0`);if(j.ok&&j.v!==S.v)await load()}
  poll()},8000)}
function setSync(ok){const d=$("#syncdot"),t=$("#synctext");if(!d)return;d.className="dot "+(ok?"live":"local");t.textContent=ok?(S.saving?"Saving…":"Shared plan · live"):"Server not answering"}
async function guard(p){S.saving=true;setSync(true);const r=await p;S.saving=false;setSync(r.ok!==false);return r}
let saveTimer=null;
// Lines and shifts the planner removed on this screen; the server deletes only these.
const REMOVED={machines:new Set(),shifts:new Set()};
function saveCfg(){clearTimeout(saveTimer);saveTimer=setTimeout(async()=>{saveTimer=null;
  const r=await guard(api("/api/scheduler/config",Object.assign({},S.cfg,{cfgSig:S.cfgSig,removedMachines:[...REMOVED.machines],removedShifts:[...REMOVED.shifts]})));
  if(r.ok||r.stale){REMOVED.machines.clear();REMOVED.shifts.clear()}
  await load(true)},600)}
async function putJob(j){const r=await guard(api("/api/scheduler/job",j));await load();return r}
async function delJob(key,cascade){await guard(api("/api/scheduler/job/delete",{key,cascade:!!cascade}));await load()}
async function putMO(mo){const r=await guard(api("/api/scheduler/mo",mo));await load();return r}
async function delMO(id){await guard(api("/api/scheduler/mo/delete",{id}));await load()}

/* ---------- shift lead login (changes to the plan need a shift lead PIN) ---------- */
function renderWho(){
  const box=$("#who");if(!box)return;
  box.innerHTML=S.me&&S.me.lead?`<span class="muted small">Editing as <b style="color:var(--fg)">${esc(S.me.name)}</b></span><button class="btn sm ghost" id="logoutBtn">Log out</button>`
    :S.editLocked?`<span class="muted small">View only</span><button class="btn sm primary" id="loginBtn">Log in to edit</button>`:"";
  const li=$("#loginBtn"),lo=$("#logoutBtn");
  if(li)li.onclick=()=>openLogin();
  if(lo)lo.onclick=async()=>{await api("/api/hmi/logout",{});await load(true);toast("Logged out.")};
}
function openLogin(msg){
  if($("#pinIn"))return;
  $("#layer").innerHTML=`<div class="scrim" id="scrim"><form class="dlg" role="dialog" aria-modal="true" aria-labelledby="lgt" id="lgform" style="width:min(380px,100%)">
    <div class="hd"><h2 id="lgt">Shift lead PIN</h2><button type="button" class="btn ghost" id="lgx" aria-label="Close">✕</button></div>
    <p class="muted small" style="margin:0">${esc(msg||"Changing the schedule needs a shift lead PIN. It's the same PIN used on the Operator Kiosk.")}</p>
    <label class="small muted" style="display:flex;flex-direction:column;gap:4px">PIN<input type="password" id="pinIn" autocomplete="off" inputmode="numeric"></label>
    <div class="note bad" id="lgerr" hidden></div>
    <div class="foot"><button type="button" class="btn" id="lgc">Cancel</button><button class="btn primary">Log in</button></div></form></div>`;
  const close=()=>closeDialog();$("#lgx").onclick=close;$("#lgc").onclick=close;
  $("#lgform").onsubmit=async e=>{e.preventDefault();
    const r=await fetch("/api/hmi/login",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json","X-CSRFToken":csrf()},body:JSON.stringify({pin:$("#pinIn").value.trim(),perm:"schedule.edit"})}).then(x=>x.json()).catch(()=>({ok:false,error:"The server is not answering."}));
    if(!r.ok){const er=$("#lgerr");er.textContent=r.error||"That is not a shift lead PIN.";er.hidden=false;$("#pinIn").select();return}
    close();await load(true);toast("Logged in. You can change the schedule now.")};
  $("#pinIn").focus();
}

/* ---------- render loop ---------- */
let rq=false;
function requestRender(force){
  if(rq)return;rq=true;
  requestAnimationFrame(()=>{rq=false;
    const route=currentRoute();const ae=document.activeElement;
    if(!force&&route!=="schedule"&&ae&&$("#main").contains(ae)&&/INPUT|TEXTAREA|SELECT/.test(ae.tagName))return;
    render();
  });
}
function currentRoute(){const h=location.hash.replace("#","");return ["schedule","lines","orders","parts"].includes(h)?h:"schedule"}
function render(){
  const r=currentRoute();
  $$("nav.tabs a").forEach(a=>a.setAttribute("aria-current",a.dataset.r===r?"page":"false"));
  if(!S.loaded){$("#main").innerHTML=`<div class="empty"><b>Loading the plan…</b></div>`;return}
  ({schedule:renderSchedule,lines:renderLines,orders:renderOrders,parts:renderParts})[r]();
}
window.addEventListener("hashchange",()=>{closeDialog();render()});
document.addEventListener("dragend",()=>{DRAG=null;$$(".ghost-line").forEach(g=>g.remove());$$(".drop-ok,.drop-no").forEach(t=>t.classList.remove("drop-ok","drop-no"))});
document.addEventListener("keydown",e=>{if(e.key==="Escape"&&$("#layer").firstChild)closeDialog()});
let toastT;
function toast(msg){let t=$(".toast");if(!t){t=document.createElement("div");t.className="toast";t.setAttribute("role","status");$(".planner").appendChild(t)}t.textContent=msg;clearTimeout(toastT);toastT=setTimeout(()=>t.remove(),4200)}
function saveView(){lsSet("plp.view",{scale:S.view.scale,anchor:S.view.anchor,showDone:S.view.showDone})}

/* ---------- schedule page ---------- */
const SCALES={
  day:{px:64,start:t=>sod(t),end:s=>addDays(s,1),step:1,snap:15*MIN},
  week:{px:9,start:t=>{const s=sod(t),d=new Date(s).getDay();return addDays(s,-((d+6)%7))},end:s=>addDays(s,7),step:7,snap:30*MIN},
  month:{px:2,start:t=>{const d=new Date(t);return new Date(d.getFullYear(),d.getMonth(),1).getTime()},end:s=>{const d=new Date(s);return new Date(d.getFullYear(),d.getMonth()+1,1).getTime()},step:"m",snap:2*HOUR}};
function viewWindow(){const sc=SCALES[S.view.scale];const a=sc.start(S.view.anchor);return {sc,a,b:sc.end(a)}}
const X=(t,a,px)=>(t-a)/HOUR*px;
function buckets(a,b){const out=[];if(S.view.scale==="day"){for(let t=a;t<b;t+=HOUR)out.push([t,t+HOUR])}else{for(let t=a;t<b;t=addDays(t,1))out.push([t,addDays(t,1)])}return out}
function rangeLabel(a,b){
  const d=new Date(a),mon=d.toLocaleString("en-US",{month:"long"});
  if(S.view.scale==="day")return d.toLocaleDateString("en-US",{weekday:"long",month:"short",day:"numeric",year:"numeric"});
  if(S.view.scale==="month")return `${mon} ${d.getFullYear()}`;
  const e=new Date(addDays(b,-1));return `${d.toLocaleDateString("en-US",{month:"short",day:"numeric"})} – ${e.toLocaleDateString("en-US",{month:"short",day:"numeric",year:"numeric"})}`;
}
const machineById=id=>S.cfg.machines.find(m=>m.id===id);
function qtyIn(r,a,b){const tot=r.runSegs.reduce((s,[x,y])=>s+(y-x),0);if(!tot)return 0;let ov=0;for(const [x,y] of r.runSegs){const o=Math.min(y,b)-Math.max(x,a);if(o>0)ov+=o}return r.qty*ov/tot}
function resultsFor(mid){return (S.byM[mid]||[]).map(k=>S.res[k]).filter(Boolean)}
function renderSchedule(){
  const main=$("#main");
  const keepScroll=$(".gwrap")?{l:$(".gwrap").scrollLeft,t:$(".gwrap").scrollTop,k:S.view.scale+S.view.anchor}:null;
  const railScroll=$(".rail-list")?$(".rail-list").scrollTop:0;
  const {sc,a,b}=viewWindow();const px=sc.px;const W=Math.round(X(b,a,px));
  const mos=Object.values(S.mos);
  const late=new Set(),waiting=new Set();let scheduled=0;
  for(const mo of mos){const s1Done=mo.status!=="OPEN"||(mo.produced||0)>=mo.qty;const ops=STAGE_ORDER.filter(o=>mo.ops&&mo.ops[o]&&!(o==="s1"&&s1Done));const rs=ops.map(o=>S.res[jobKey(mo.id,o)]).filter(Boolean);
    if(rs.length===ops.length&&ops.length)scheduled++;if(rs.some(r=>r.late))late.add(mo.id);if(rs.some(r=>r.waiting))waiting.add(mo.id)}
  const unsched=mos.length-scheduled;
  let axis="";const now=Date.now();
  if(S.view.scale==="day"){for(let t=a;t<b;t+=HOUR){const h=new Date(t).getHours();axis+=`<div class="tick ${h%6===0?"major":""}" style="left:${X(t,a,px)}px;width:${px}px"><span class="big">${String(h).padStart(2,"0")}:00</span></div>`}}
  else{for(let t=a;t<b;t=addDays(t,1)){const d=new Date(t),w=X(addDays(t,1),a,px)-X(t,a,px),we=d.getDay()===0||d.getDay()===6,td=sod(now)===t;
    const sp=(S.labor.days||{})[isoDay(t)],spT=sp?` title="${sp==="WORK"?"Extra work day":sp==="HOLIDAY"?"Holiday":"Day off"} in the Labor Budget plan"`:"",spL=sp==="HOLIDAY"?"Holiday":sp==="OFF"?"Off":sp==="WORK"?"Work day":"";
    axis+=S.view.scale==="week"?`<div class="tick major ${we?"wkend":""} ${td?"today":""} ${sp&&sp!=="WORK"?"hol":""}" style="left:${X(t,a,px)}px;width:${w}px"${spT}><span class="big">${DOW[d.getDay()]} ${d.getMonth()+1}/${d.getDate()}</span>${spL||"06 · 12 · 18"}</div>`
      :`<div class="tick ${d.getDay()===1?"major":""} ${we?"wkend":""} ${td?"today":""} ${sp&&sp!=="WORK"?"hol":""}" style="left:${X(t,a,px)}px;width:${w}px"${spT}><span class="big">${d.getDate()}</span>${sp==="HOLIDAY"?"H":DOW[d.getDay()][0]}</div>`}}
  const bk=buckets(a,b);
  const nowX=(now>=a&&now<b)?`<div class="nowline" style="left:${X(now,a,px)}px"></div>`:"";
  let rows="";
  for(const st of STAGE_ORDER){
    const ms=S.cfg.machines.filter(m=>m.stage===st);if(!ms.length)continue;
    const all=ms.flatMap(m=>resultsFor(m.id));
    const gTot=all.reduce((s,r)=>s+qtyIn(r,a,b),0);
    const gcells=bk.map(([x,y])=>{const q=all.reduce((s,r)=>s+qtyIn(r,x,y),0);return `<div class="qcell ${q?"has":""}" style="left:${X(x,a,px)}px;width:${X(y,a,px)-X(x,a,px)}px">${q?fmtK(q):""}</div>`}).join("");
    rows+=`<div class="g-row group"><div class="g-lab"><span class="nm"><span class="swatch" style="background:var(--${st==="s1"?"st1":st})"></span>${esc(STAGES[st].name)}</span><span class="tot">${fmtN(gTot)} pcs in view</span></div><div class="g-track" style="width:${W}px">${gcells}</div></div>`;
    for(const m of ms){
      const rs=resultsFor(m.id);
      const work=S.work[m.id]||[];let off="",cur=a;
      for(const [s,e] of work){if(s>cur)off+=`<div class="off" style="left:${X(cur,a,px)}px;width:${X(s,a,px)-X(cur,a,px)}px"></div>`;cur=e}
      if(cur<b)off+=`<div class="off" style="left:${X(cur,a,px)}px;width:${X(b,a,px)-X(cur,a,px)}px"></div>`;
      const grid=S.view.scale==="day"?"":bk.map(([x])=>`<div class="gridline" style="left:${X(x,a,px)}px"></div>`).join("");
      const bars=rs.filter(r=>r.end>a&&r.start<b).map(r=>{
        const mo=S.mos[r.moId];if(!mo)return "";
        const l=X(r.start,a,px),w=Math.max(4,X(r.end,a,px)-l),cw=r.coMin?Math.min(w,X(r.runStart,a,px)-l):0;
        const tip=`${mo.mo} · ${mo.part} · ${fmtN(r.qty)} pcs\n${fmtDT(r.start)} → ${fmtDT(r.end)}${r.coMin?`\nChangeover ${fmtDur(r.coMin)}: ${fmtDT(r.start)} → ${fmtDT(r.runStart)}`:""}\nRunning ${fmtDT(r.runStart)} → ${fmtDT(r.end)}\n${r.basis}${r.laborCost!=null?`\nCrew wages ${fmtMoney(r.laborCost)}${r.qty?` (${fmtMoney(r.laborCost/r.qty,3)} / pc)`:""}`:""}${r.warn.length?"\n⚠ "+r.warn.join("\n⚠ "):""}${r.late?"\n⚠ Finishes after due date":""}`;
        return `<div class="bar ${r.op} ${r.late?"late":""} ${r.waiting?"waiting":""}" draggable="true" data-job="${esc(r.id)}" style="left:${l}px;width:${w}px" title="${esc(tip)}">${cw>0?`<div class="co" style="width:${cw}px">${cw>=34?`<span><i>CO ${Math.round(r.coMin)}m</i></span>`:""}</div>`:""}<div class="run"><b>${esc(mo.mo)}</b><span>${esc(mo.part)} · ${fmtK(r.qty)}</span></div>${(r.late||r.waiting||r.unplanned||r.warn.length)?`<span class="flag">!</span>`:""}</div>`}).join("");
      const tot=rs.reduce((s,r)=>s+qtyIn(r,a,b),0);
      const cells=bk.map(([x,y])=>{const q=rs.reduce((s,r)=>s+qtyIn(r,x,y),0);return `<div class="qcell ${q?"has":""}" style="left:${X(x,a,px)}px;width:${X(y,a,px)-X(x,a,px)}px">${q?fmtK(q):""}</div>`}).join("");
      const sp=m.stage==="s1"?`${m.mode==="fixed"?m.ppm+" PPM":m.mode==="new"?"New-machine PPM":"Sheet PPM"}`:`${(S.modes[m.stage]||[]).find(x=>x[0]===m.mode)?.[1].split(" (")[0]||"Fixed"}`;
      const lab=(S.labor.lines||{})[m.id];
      const crewTxt=lab?Object.values(lab.shifts).sort((x,y)=>x.n-y.n).map(c=>c.staffed?`${ordinal(c.n)} ${c.crew}`:`${ordinal(c.n)} none`).join(" · "):"";
      rows+=`<div class="g-row"><div class="g-lab"><span class="nm">${esc(m.name)}${m.stage==="s1"?' <span class="pill" title="Feeds the Operator Kiosk queue">kiosk</span>':""}</span><span class="ds">${esc(sp)} · ${m.scrap}% scrap · ${m.downtime}% down</span>${lab?`<span class="ds" title="Crew per shift from the Labor Budget plan">Crew: ${esc(crewTxt)}</span>`:""}<span class="tot">${fmtN(tot)} pcs in view</span></div><div class="g-track" data-m="${esc(m.id)}" style="width:${W}px">${off}${grid}${bars}${cells}</div></div>`;
    }
  }
  const moList=mos.filter(mo=>{const q=S.view.q.trim().toLowerCase();if(q&&!(`${mo.mo} ${mo.part}`.toLowerCase().includes(q)))return false;
    if(!S.view.showDone){const s1Done=mo.status!=="OPEN"||(mo.produced||0)>=mo.qty;const ops=STAGE_ORDER.filter(o=>mo.ops&&mo.ops[o]);if(ops.every(o=>(o==="s1"&&s1Done)||S.jobs[jobKey(mo.id,o)]))return false}return true})
    .sort((x,y)=>(x.due||9e15)-(y.due||9e15)||String(x.mo).localeCompare(String(y.mo)));
  const rail=moList.map(mo=>{
    const item=mo.item;
    const s1Done=mo.status!=="OPEN"||(mo.produced||0)>=mo.qty;
    const chips=STAGE_ORDER.filter(o=>mo.ops&&mo.ops[o]).map(o=>{
      const k=jobKey(mo.id,o),r=S.res[k];
      if(o==="s1"&&s1Done)return `<span class="chip s1 done" title="Stage 1 finished at the pleater">S1 <span class="t">done</span></span>`;
      const blocked=o!=="s1"&&!s1Done&&!S.jobs[jobKey(mo.id,"s1")];
      return `<span class="chip ${o} ${r?"done":""} ${blocked&&!r?"blocked":""}" draggable="${blocked&&!r?"false":"true"}" data-mo="${esc(mo.id)}" data-op="${o}" tabindex="0" role="button" title="${blocked&&!r?"Schedule Stage 1 first":r?`${esc(machineById(r.machineId)?.name)}: ${fmtDT(r.start)} → ${fmtDT(r.end)}`:"Drag onto a "+STAGES[o].short+" line, or click to set a start time"}">${STAGES[o].tag}${r?` <span class="t">${fmtDT(r.end).slice(4)}</span>`:""}</span>`}).join("");
    return `<div class="mo"><div class="mo-top"><span class="mo-id">${esc(mo.mo)}${mo.example?' <span class="pill">example</span>':""}${mo.status!=="OPEN"?' <span class="pill" title="Closed at the pleater; later stages still to run">closed at S1</span>':""}</span><span class="mo-qty">${fmtN(mo.qty)}</span></div>
      <div class="mo-meta"><span>${esc(mo.part)}${item?` · ${esc(item.cat)} · ${item.pleats} pl`:mo.pleats?` · ${mo.pleats} pl`:` · <span class="late">not in cost sheet</span>`}</span>${mo.due?`<span class="${late.has(mo.id)?"late":""}">Due ${fmtD(mo.due)}</span>`:""}${mo.produced?`<span>${fmtN(mo.produced)} made</span>`:""}</div>
      <div class="chips">${chips}</div></div>`}).join("");
  main.innerHTML=`
  <div class="toolbar">
    <div class="seg" role="group" aria-label="Scale">${["day","week","month"].map(s=>`<button data-scale="${s}" aria-pressed="${S.view.scale===s}">${s}</button>`).join("")}</div>
    <button class="btn" id="prev" aria-label="Previous">‹</button><button class="btn" id="today">Today</button><button class="btn" id="next" aria-label="Next">›</button>
    <input type="date" id="jump" aria-label="Jump to date" value="${isoDay(S.view.anchor)}">
    <span class="range">${esc(rangeLabel(a,b))}</span>
    <span class="spacer"></span>
    <div class="legend"><span><i style="background:var(--st1)"></i>Stage 1</span><span><i style="background:var(--eb)"></i>End Band</span><span><i style="background:var(--ac)"></i>Arts &amp; Crafts</span><span><i style="background:var(--gb)"></i>Glue Bead</span><span><i class="hatch"></i>Changeover</span><span><i style="background:repeating-linear-gradient(135deg,var(--off) 0 3px,var(--off-stripe) 3px 6px);border:1px solid var(--line)"></i>Off shift / break</span></div>
  </div>
  <div class="kpis">
    <span class="kpi"><b>${mos.length}</b> orders</span><span class="kpi"><b>${scheduled}</b> fully scheduled</span>
    <span class="kpi ${unsched?"warn":""}"><b>${unsched}</b> need scheduling</span>
    <span class="kpi ${late.size?"bad":""}"><b>${late.size}</b> finish after due date</span>
    ${waiting.size?`<span class="kpi bad"><b>${waiting.size}</b> waiting on Stage 1</span>`:""}
  </div>
  <div class="board">
    <aside class="rail" aria-label="Manufacturing orders">
      <div class="rail-head">
        <div class="row"><h3>Orders</h3><span class="muted small">drag a stage onto a line</span></div>
        <div class="row"><input type="search" id="q" placeholder="Find MO or part" value="${esc(S.view.q)}"></div>
        <label class="check small"><input type="checkbox" id="showdone" ${S.view.showDone?"checked":""}> Show fully scheduled orders</label>
      </div>
      <div class="rail-list">${rail||`<div class="empty"><b>No orders yet</b>Import your MO list on the <a href="#orders">Orders</a> page.</div>`}</div>
    </aside>
    <div class="gwrap"><div class="g" style="width:calc(var(--labw) + ${W}px)">
      <div class="g-head"><div class="g-corner">Line</div><div class="g-axis" style="width:${W}px">${axis}</div></div>
      ${rows||`<div class="empty"><b>No lines set up</b>Add machines on the <a href="#lines">Lines &amp; Shifts</a> page.</div>`}
      <div style="position:absolute;top:46px;bottom:0;left:var(--labw);width:${W}px;pointer-events:none">${nowX}</div>
    </div></div>
  </div>`;
  const gw=$(".gwrap");
  if(keepScroll&&keepScroll.k===S.view.scale+S.view.anchor){gw.scrollLeft=keepScroll.l;gw.scrollTop=keepScroll.t}
  else if(now>=a&&now<b&&S.view.scale==="day"){gw.scrollLeft=Math.max(0,X(now,a,px)-200)}
  $(".rail-list").scrollTop=railScroll;
  wireSchedule(a,px);
}
function shiftView(dir){
  const s=S.view.scale;
  if(s==="month"){const d=new Date(SCALES.month.start(S.view.anchor));d.setMonth(d.getMonth()+dir);S.view.anchor=d.getTime()}
  else S.view.anchor=addDays(SCALES[s].start(S.view.anchor),dir*SCALES[s].step);
  saveView();load(true);
}
let DRAG=null;
function wireSchedule(a,px){
  $$("[data-scale]").forEach(b=>b.onclick=()=>{S.view.scale=b.dataset.scale;saveView();load(true)});
  $("#prev").onclick=()=>shiftView(-1);$("#next").onclick=()=>shiftView(1);
  $("#today").onclick=()=>{S.view.anchor=Date.now();saveView();load(true)};
  $("#jump").onchange=e=>{if(e.target.value){const [y,m,d]=e.target.value.split("-").map(Number);S.view.anchor=new Date(y,m-1,d).getTime();saveView();load(true)}};
  $("#q").oninput=e=>{S.view.q=e.target.value;const pos=e.target.selectionStart;render();const q=$("#q");q.focus();q.setSelectionRange(pos,pos)};
  $("#showdone").onchange=e=>{S.view.showDone=e.target.checked;saveView();render()};
  $$(".chip").forEach(c=>{
    c.addEventListener("dragstart",e=>{if(c.classList.contains("blocked")){e.preventDefault();toast("Schedule Stage 1 for this order first.");return}
      DRAG={moId:+c.dataset.mo,op:c.dataset.op,grab:0};e.dataTransfer.setData("text/plain",jobKey(c.dataset.mo,c.dataset.op));e.dataTransfer.effectAllowed="move"});
    const open=()=>{if(c.classList.contains("blocked")){toast("Schedule Stage 1 for this order first.");return}openJobDialog(+c.dataset.mo,c.dataset.op)};
    c.addEventListener("click",open);c.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();open()}});
  });
  $$(".bar").forEach(bEl=>{
    bEl.addEventListener("dragstart",e=>{const j=S.jobs[bEl.dataset.job];if(!j)return;const rect=bEl.getBoundingClientRect();
      DRAG={moId:j.moId,op:j.op,grab:(e.clientX-rect.left)/px*HOUR};e.dataTransfer.setData("text/plain",j.id);e.dataTransfer.effectAllowed="move"});
    bEl.addEventListener("click",()=>{const j=S.jobs[bEl.dataset.job];if(j)openJobDialog(j.moId,j.op)});
  });
  const tAt=(tr,e)=>{const rect=tr.getBoundingClientRect();let t=a+(e.clientX-rect.left)/px*HOUR-(DRAG?DRAG.grab:0);const sn=SCALES[S.view.scale].snap;return Math.round(t/sn)*sn};
  $$(".g-track[data-m]").forEach(tr=>{
    const m=machineById(+tr.dataset.m);
    tr.addEventListener("dragover",e=>{if(!DRAG)return;const ok=m.stage===DRAG.op;tr.classList.toggle("drop-ok",ok);tr.classList.toggle("drop-no",!ok);
      if(ok){e.preventDefault();e.dataTransfer.dropEffect="move";let g=tr.querySelector(".ghost-line");if(!g){g=document.createElement("div");g.className="ghost-line";g.innerHTML="<span></span>";tr.appendChild(g)}
        const t=tAt(tr,e);g.style.left=X(t,a,px)+"px";g.firstChild.textContent=fmtDT(t)}});
    tr.addEventListener("dragleave",e=>{if(!tr.contains(e.relatedTarget)){tr.classList.remove("drop-ok","drop-no");tr.querySelector(".ghost-line")?.remove()}});
    tr.addEventListener("drop",async e=>{e.preventDefault();tr.classList.remove("drop-ok","drop-no");tr.querySelector(".ghost-line")?.remove();
      if(!DRAG)return;const d=DRAG;DRAG=null;
      if(m.stage!==d.op){toast(`${STAGES[d.op].short} work can only go on a ${STAGES[d.op].short} line.`);return}
      if(d.op!=="s1"&&!S.jobs[jobKey(d.moId,"s1")]){toast("Schedule Stage 1 for this order first.");return}
      const k=jobKey(d.moId,d.op);
      const r=await putJob({moId:d.moId,op:d.op,machineId:m.id,reqStart:tAt(tr,e)});
      if(r.ok&&d.op!=="s1"){const x=S.res[k];if(x&&x.paced)toast(`Held back to ${fmtDT(x.start)} so it doesn't run out of Stage 1 parts.`)}
    });
  });
}

/* ---------- job dialog ---------- */
function closeDialog(){$("#layer").innerHTML=""}
function openJobDialog(moId,op){
  const mo=S.mos[moId];if(!mo)return;const k=jobKey(moId,op);const existing=S.jobs[k];
  const lines=S.cfg.machines.filter(m=>m.stage===op);
  if(!lines.length){toast(`There are no ${STAGES[op].short} lines. Add one on the Lines & Shifts page.`);return}
  const s1r=S.res[jobKey(moId,"s1")];
  const st={machineId:existing?existing.machineId:lines[0].id,reqStart:existing?existing.reqStart:0};
  if(!existing){const last=resultsFor(lines[0].id).reduce((x,r)=>Math.max(x,r.end),0);const nowR=Math.ceil(Date.now()/(15*MIN))*15*MIN;st.reqStart=Math.max(nowR,last,op!=="s1"&&s1r?s1r.end:0)}
  const item=mo.item;
  $("#layer").innerHTML=`<div class="scrim" id="scrim"><div class="dlg" role="dialog" aria-modal="true" aria-labelledby="dlgt">
    <div class="hd"><div><h2 id="dlgt">${esc(mo.mo)} · ${esc(STAGES[op].name)}</h2><div class="muted small">${esc(mo.part)}${item?` · ${esc(item.aq)} / ${esc(item.cust||"—")} · ${esc(item.cat)} · ${item.pleats} pleats`:" · not found in cost sheet"} · ${fmtN(mo.qty)} pcs${mo.due?` · due ${fmtD(mo.due)}`:""}</div></div><button class="btn ghost" id="dx" aria-label="Close">✕</button></div>
    <div class="fieldrow">
      <label>Line<select id="dm">${lines.map(m=>`<option value="${m.id}" ${m.id===st.machineId?"selected":""}>${esc(m.name)}</option>`).join("")}</select></label>
      <label>Requested start<input type="datetime-local" id="ds" value="${toLocalInput(st.reqStart)}"></label>
    </div>
    <div id="dest"><div class="muted small">Working out the run…</div></div>
    <div class="foot">${existing?`<button class="btn danger" id="dun">Unschedule</button>`:""}<span style="flex:1"></span><button class="btn" id="dc">Cancel</button><button class="btn primary" id="dok">${existing?"Save":"Schedule"}</button></div>
  </div></div>`;
  let seq=0,estT=null;
  const est=()=>{clearTimeout(estT);estT=setTimeout(async()=>{
    const v=$("#ds").value;if(v)st.reqStart=new Date(v).getTime();st.machineId=+$("#dm").value;
    const n=++seq;const j=await api("/api/scheduler/preview",{moId,op,machineId:st.machineId,reqStart:st.reqStart});
    if(n!==seq||!$("#dest"))return;const r=j.res,m=machineById(st.machineId);
    if(!r){$("#dest").innerHTML=`<div class="note bad">This line has no shifts assigned, so no end time can be estimated.</div>`;return}
    const deps=op==="s1"?STAGE_ORDER.filter(o=>o!=="s1"&&S.jobs[jobKey(moId,o)]).length:0;
    $("#dest").innerHTML=`<dl>
      <dt>Run speed</dt><dd>${fmtN(r.rate)}/hr <span class="muted">(${esc(r.basis)})</span></dd>
      <dt>Effective speed</dt><dd>${fmtN(r.eff)}/hr after ${m.downtime}% downtime</dd>
      <dt>Pieces to run</dt><dd>${fmtN(r.gross)} for ${fmtN(r.qty)} good at ${m.scrap}% scrap</dd>
      <dt>Changeover</dt><dd>${r.coMin?`${fmtDur(r.coMin)} · ${fmtDT(r.start)} → ${fmtDT(r.runStart)}`:"none (same part as the previous run)"}</dd>
      <dt>Run time</dt><dd>${fmtDur(r.runMin)} of shift time</dd>
      ${r.laborCost!=null?`<dt>Crew wages</dt><dd>${fmtMoney(r.laborCost)}${r.qty?` <span class="muted">(${fmtMoney(r.laborCost/r.qty,3)} per piece, base wages from the Labor Budget)</span>`:""}</dd>`:""}
      <dt>Estimated start</dt><dd>${fmtDT(r.start)}${r.coMin?` <span class="muted">(running from ${fmtDT(r.runStart)})</span>`:""}</dd>
      <dt>Estimated end</dt><dd class="big">${fmtDT(r.end)}</dd></dl>
      ${r.overlap?`<div class="note" style="background:var(--accent-soft);color:var(--accent)">Runs alongside Stage 1${r.paced?", paced so it never runs out of Stage 1 parts":""}. Stage 1 finishes ${fmtDT(j.s1End||r.start)}.</div>`:""}${r.warn.map(w=>`<div class="note">${esc(w)}</div>`).join("")}${r.late?`<div class="note bad">Finishes after the due date (${fmtD(mo.due)}).</div>`:""}${deps?`<div class="note">Downstream stages for this order will move to follow the new Stage 1 end time.</div>`:""}${m.stage==="s1"?`<div class="note" style="background:var(--accent-soft);color:var(--accent)">Saving updates the ${esc(m.name)} queue on the Operator Kiosk.</div>`:""}`;
  },150)};
  est();
  $("#dm").onchange=est;$("#ds").oninput=est;
  const close=()=>closeDialog();$("#dx").onclick=close;$("#dc").onclick=close;
  $("#scrim").onclick=e=>{if(e.target.id==="scrim")close()};
  $("#dok").onclick=async()=>{const v=$("#ds").value;if(v)st.reqStart=new Date(v).getTime();st.machineId=+$("#dm").value;close();
    const r=await putJob({moId,op,machineId:st.machineId,reqStart:st.reqStart});if(r.ok)toast(existing?"Schedule updated.":"Scheduled.")};
  if(existing)$("#dun").onclick=async()=>{
    if(op==="s1"){const dn=STAGE_ORDER.filter(o=>o!=="s1"&&S.jobs[jobKey(moId,o)]);if(dn.length){const b=$("#dun");if(!b.dataset.sure){b.dataset.sure="1";b.textContent=`Also unschedule ${dn.length} later stage${dn.length>1?"s":""}?`;return}close();await delJob(k,true);toast("Unscheduled.");return}}
    close();await delJob(k);toast("Unscheduled.")};
  $("#dm").focus();
}

/* ---------- Labor Budget plan ---------- */
function ordinal(n){return n+(n===1?"st":n===2?"nd":n===3?"rd":"th")}
function fmtMoney(v,d=2){return "$"+Number(v||0).toLocaleString("en-US",{minimumFractionDigits:d,maximumFractionDigits:d})}
function laborSection(c){
  const L=S.labor||{lines:{}},lead=!!(S.me&&S.me.lead);
  const head=`<label class="check"><input type="checkbox" id="uselabor" ${c.useLabor!==false?"checked":""}> Use the Labor Budget plan: no runs on its holidays and days off, runs on its extra work days, and each line only runs the shifts the plan has a crew for</label>`;
  let body;
  if(c.useLabor===false)body=`<p class="small muted">Turned off: every line runs all the shifts ticked above, on the shifts' own days.</p>`;
  else if(!L.on)body=`<p class="small muted">No Labor Budget plan is saved yet. A shift lead can build one in <a href="/labor/">Labor Budget</a>; lines with the same name there are linked automatically.</p>`;
  else{
    const rows=c.machines.map(m=>{const lab=L.lines[m.id];
      if(!lab)return `<tr><td>${esc(m.name)}</td><td colspan="${c.shifts.length+1}" class="muted">Not in the labor plan (no line named "${esc(m.name)}"), so it runs all its shifts.</td></tr>`;
      return `<tr><td>${esc(m.name)}</td><td>${esc(lab.line)}</td>${c.shifts.map(sh=>{const x=lab.shifts[String(sh.id)];
        if(!x)return `<td class="muted">not this line's shift</td>`;
        return x.staffed?`<td>${ordinal(x.n)}: crew ${x.crew}${x.filled<x.crew?` <span class="muted">(${x.filled} named)</span>`:""}${lead&&x.costPerHour!=null?`<br><span class="muted small">${fmtMoney(x.costPerHour)}/h base wages</span>`:""}</td>`
          :`<td><span class="late">${ordinal(x.n)}: no crew</span><br><span class="muted small">not used for runs</span></td>`}).join("")}</tr>`}).join("");
    body=`<div class="tablewrap"><table><thead><tr><th>Line</th><th>Labor plan line</th>${c.shifts.map((sh,i)=>`<th>${esc(sh.name)} <span class="muted small">(plan shift ${i+1})</span></th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>
      <p class="small muted" style="margin-top:8px">Shifts are matched to the plan's shift numbers by their order below (first shift = plan shift 1). The work calendar and crews update as soon as the Labor Budget is saved.${lead?" Crew wages on each run are straight-time base wages: no overtime, raises or benefits.":""}</p>`}
  return `<div class="page-head"><div><h3>Labor Budget plan</h3></div></div><div class="stack" style="gap:8px;margin-bottom:22px">${head}${body}</div>`;
}

/* ---------- lines & shifts page ---------- */
function weeklyMinutes(m){let t=0;for(const sh of S.cfg.shifts){if(!(m.shifts||[]).includes(sh.id))continue;t+=shiftWorkMin(sh,m)*(sh.days||[]).length}return t}
function shiftWorkMin(sh,m){const s0=hm(sh.start);let L=(hm(sh.end)-s0+1440)%1440||1440;let br=0;for(const b of sh.breaks||[])if(!(m&&m.runLunch&&/lunch/i.test(b.label||"")))br+=(hm(b.end)-hm(b.start)+1440)%1440;return Math.max(0,L-br)}
function renderLines(){
  const c=S.cfg;
  const rows=c.machines.map((m,i)=>{
    const wk=weeklyMinutes(m);
    return `<tr data-i="${i}">
      <td><input type="text" id="mn-${m.id}" data-f="name" value="${esc(m.name)}" aria-label="Line name"></td>
      <td><select data-f="stage" id="ms-${m.id}" aria-label="Stage">${STAGE_ORDER.map(s=>`<option value="${s}" ${m.stage===s?"selected":""}>${STAGES[s].name}</option>`).join("")}</select></td>
      <td><select data-f="mode" id="mm-${m.id}" aria-label="Speed basis">${(S.modes[m.stage]||[]).map(([v,l])=>`<option value="${v}" ${m.mode===v?"selected":""}>${l}</option>`).join("")}</select></td>
      <td class="n">${m.stage==="s1"?`<input type="number" min="1" step="1" data-f="ppm" id="mp-${m.id}" value="${m.ppm}" aria-label="Pleats per minute"> <span class="muted small">PPM</span>`:`<input type="number" min="1" step="1" data-f="rate" id="mr-${m.id}" value="${m.rate}" aria-label="Filters per hour"> <span class="muted small">/hr</span>`}</td>
      <td class="n"><input type="number" min="0" max="90" step="0.1" data-f="scrap" id="msc-${m.id}" value="${m.scrap}" aria-label="Scrap percent"></td>
      <td class="n"><input type="number" min="0" max="90" step="0.1" data-f="downtime" id="mdt-${m.id}" value="${m.downtime}" aria-label="Downtime percent"></td>
      <td class="n"><input type="number" min="0" step="1" data-f="changeover" id="mco-${m.id}" value="${m.changeover}" aria-label="Changeover minutes"></td>
      <td class="c"><input type="checkbox" data-f="runLunch" id="mrl-${m.id}" ${m.runLunch?"checked":""} aria-label="Runs through lunch"></td>
      <td><div class="shifts-cell">${c.shifts.map(sh=>`<label><input type="checkbox" data-shift="${esc(sh.id)}" ${(m.shifts||[]).includes(sh.id)?"checked":""}>${esc(sh.name)}</label>`).join("")||`<span class="muted small">No shifts</span>`}</div></td>
      <td class="n num">${(wk/60).toFixed(1)} h<br><span class="muted small">${(wk/60*(1-m.downtime/100)).toFixed(1)} h net</span></td>
      <td><button class="btn sm danger" data-del="${i}">Remove</button></td></tr>`}).join("");
  const shifts=c.shifts.map((sh,i)=>{
    const s0=hm(sh.start);let L=(hm(sh.end)-s0+1440)%1440||1440;
    const brs=(sh.breaks||[]).map(b=>{const o=(hm(b.start)-s0+1440)%1440,l=(hm(b.end)-hm(b.start)+1440)%1440;return `<i style="left:${o/L*100}%;width:${l/L*100}%;background:repeating-linear-gradient(135deg,var(--off-stripe) 0 3px,var(--line) 3px 6px)"></i>`}).join("");
    return `<div class="card shift" data-si="${i}">
      <div class="hd"><input type="text" id="sn-${sh.id}" data-sf="name" value="${esc(sh.name)}" aria-label="Shift name"><button class="btn sm danger" data-sdel="${i}">Remove</button></div>
      <div class="fieldrow"><label>Start<input type="time" id="sst-${sh.id}" data-sf="start" value="${esc(sh.start)}"></label><label>End<input type="time" id="sen-${sh.id}" data-sf="end" value="${esc(sh.end)}"></label>
        <label>Days<div class="days">${DOW.map((d,k)=>`<button type="button" data-day="${k}" aria-pressed="${(sh.days||[]).includes(k)}" aria-label="${d}">${d[0]}</button>`).join("")}</div></label></div>
      <div class="timeline" aria-hidden="true"><i style="left:0;right:0;background:var(--accent-soft)"></i>${brs}</div>
      <div class="small muted">${(L/60).toFixed(2).replace(/\.?0+$/,"")} h on site · <b class="num" style="color:var(--fg)">${(shiftWorkMin(sh)/60).toFixed(2).replace(/\.?0+$/,"")} h</b> running</div>
      <div class="stack" style="gap:6px">${(sh.breaks||[]).map((b,j)=>`<div class="brk" data-bi="${j}"><input type="text" id="bl-${sh.id}-${j}" data-bf="label" value="${esc(b.label)}" aria-label="Break name"><input type="time" id="bs-${sh.id}-${j}" data-bf="start" value="${esc(b.start)}" aria-label="Break start"><input type="time" id="be-${sh.id}-${j}" data-bf="end" value="${esc(b.end)}" aria-label="Break end"><button class="btn sm ghost danger" data-bdel="${j}" aria-label="Remove break">✕</button></div>`).join("")}
      <div><button class="btn sm" data-badd>+ Add break or lunch</button></div></div></div>`}).join("");
  $("#main").innerHTML=`
  <div class="page-head"><div><h2>Lines &amp; Shifts</h2><p class="sub">Machine speeds, scrap, downtime and changeovers drive every run length on the schedule. Stage 1 speed comes from the cost sheet's pleats per minute divided by the part's pleat count. Stage 1 lines are the pleaters with an Operator Kiosk; their feeds, colors and training are set in the kiosk's Admin page.</p></div>
  <button class="btn primary" id="addm">+ Add line</button></div>
  <div class="tablewrap"><table><thead><tr><th>Line</th><th>Stage</th><th>Speed basis</th><th class="n">Speed / fallback</th><th class="n">Scrap %</th><th class="n">Downtime %</th><th class="n">Changeover min</th><th title="Relief cover keeps the line running during lunch, so runs don't pause for it">Runs through lunch</th><th>Shifts</th><th class="n">Hours / week</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
  <p class="small muted" style="margin:8px 0 22px">Sheet-based lines fall back to the speed shown when a part has no rate on the cost sheet. Scrap adds extra pieces to every run; downtime slows the effective speed.</p>
  <div class="stack" style="gap:6px;margin-bottom:22px"><label class="check"><input type="checkbox" id="cofirst" ${c.coFirst!==false?"checked":""}> Include a changeover before the first run on each line</label><label class="check"><input type="checkbox" id="cosame" ${c.coSamePart?"checked":""}> Apply changeover time even when the next run is the same part</label></div>
  ${laborSection(c)}
  <div class="page-head"><div><h3>Shift schedule</h3><p class="sub">These shifts are shared with the Operator Kiosk. End Band, Arts &amp; Crafts and Glue Bead runs start as soon as Stage 1 is producing and their changeover is done. When a later stage runs faster than Stage 1, its start is held back just enough that it never runs out of parts. Breaks and lunches are removed from available run time; a run pauses through them and resumes after, except lunch on a line marked <b>Runs through lunch</b>.</p></div><button class="btn" id="adds">+ Add shift</button></div>
  <div class="shift-grid">${shifts}</div>`;
  if(S.labor&&S.labor.master){
    // the Labor Budget is the master of lines and shifts: they're shown here, changed there
    $("#main").querySelector(".page-head").insertAdjacentHTML("afterend",`<div class="card" style="border-left:4px solid var(--accent);margin-bottom:14px"><b>Lines and shifts come from the Labor Budget.</b> Add, rename or remove lines, change which shifts a line runs, whether it runs through lunch, and shift times, days and lunch in the <a href="/labor/">Labor Budget</a>; this page follows it when it's saved. Speeds, scrap, downtime and changeover times are still set here.</div>`);
    $$('#main [data-f="name"],#main [data-f="stage"],#main [data-f="runLunch"],#main [data-shift],#main [data-del],#main #addm,#main .shift input,#main .shift button,#main #adds').forEach(el=>{el.disabled=true;el.title="Set in the Labor Budget"});
  }
  const tb=$("tbody");
  tb.oninput=tb.onchange=e=>{const c=S.cfg;const tr=e.target.closest("tr");if(!tr)return;const m=c.machines[+tr.dataset.i];if(!m)return;
    if(e.target.dataset.f){const f=e.target.dataset.f;let v=e.target.type==="checkbox"?e.target.checked:e.target.value;if(["ppm","rate","scrap","downtime","changeover"].includes(f))v=Number(v)||0;m[f]=v;
      if(f==="stage"){m.mode=(S.modes[v]||[["fixed"]])[0][0];if(v!=="s1"&&!m.rate)m.rate=300;if(v==="s1"&&!m.ppm)m.ppm=180}
      saveCfg();if(e.type==="change"&&(f==="stage"||f==="mode"||f==="downtime"||f==="runLunch"))renderLines()}
    if(e.target.dataset.shift&&e.type==="change"){const id=isNaN(+e.target.dataset.shift)?e.target.dataset.shift:+e.target.dataset.shift;m.shifts=m.shifts||[];if(e.target.checked){if(!m.shifts.includes(id))m.shifts.push(id)}else m.shifts=m.shifts.filter(x=>x!==id);saveCfg();renderLines()}};
  tb.onclick=e=>{const c=S.cfg;const b=e.target.closest("[data-del]");if(!b)return;const i=+b.dataset.del,m=c.machines[i];
    const n=Object.values(S.jobs).filter(j=>j.machineId===m.id).length;
    if(!b.dataset.sure){b.dataset.sure="1";b.textContent=n?`Remove + unschedule ${n}?`:"Click again to remove";return}
    if(typeof m.id==="number")REMOVED.machines.add(m.id);c.machines.splice(i,1);saveCfg();renderLines()};
  $("#addm").onclick=()=>{const c=S.cfg;c.machines.push({id:uid("new-"),name:`New line ${c.machines.length+1}`,stage:"s1",mode:"fixed",ppm:180,rate:0,scrap:5,downtime:10,changeover:30,shifts:c.shifts.map(s=>s.id).slice(0,1)});saveCfg();renderLines()};
  $("#uselabor").onchange=e=>{S.cfg.useLabor=e.target.checked;saveCfg()};
  $("#cosame").onchange=e=>{S.cfg.coSamePart=e.target.checked;saveCfg()};$("#cofirst").onchange=e=>{S.cfg.coFirst=e.target.checked;saveCfg()};
  $("#adds").onclick=()=>{const c=S.cfg;c.shifts.push({id:uid("new-"),name:`Shift ${c.shifts.length+1}`,start:"23:00",end:"06:00",days:[1,2,3,4,5],breaks:[{label:"Lunch",start:"03:00",end:"03:30"}]});saveCfg();renderLines()};
  $$(".shift").forEach(card=>{const SH=()=>S.cfg.shifts[+card.dataset.si];
    card.oninput=card.onchange=e=>{const sh=SH();if(!sh)return;const t=e.target;
      if(t.dataset.sf){sh[t.dataset.sf]=t.value;saveCfg();if(e.type==="change"&&t.dataset.sf!=="name")renderLines()}
      if(t.dataset.bf){sh.breaks[+t.closest("[data-bi]").dataset.bi][t.dataset.bf]=t.value;saveCfg();if(e.type==="change"&&t.dataset.bf!=="label")renderLines()}};
    card.onclick=e=>{const c=S.cfg,sh=SH();const t=e.target.closest("button");if(!t||!sh)return;
      if(t.dataset.day!=null){const d=+t.dataset.day;sh.days=sh.days.includes(d)?sh.days.filter(x=>x!==d):[...sh.days,d].sort();saveCfg();renderLines()}
      if(t.dataset.bdel!=null){sh.breaks.splice(+t.dataset.bdel,1);saveCfg();renderLines()}
      if(t.hasAttribute("data-badd")){sh.breaks.push({label:"Break",start:sh.start,end:sh.start.replace(/:(\d\d)$/,(x,mm)=>":"+String(Math.min(59,+mm+15)).padStart(2,"0"))});saveCfg();renderLines()}
      if(t.dataset.sdel!=null){if(!t.dataset.sure){t.dataset.sure="1";t.textContent="Click again to remove";return}c.machines.forEach(m=>m.shifts=(m.shifts||[]).filter(x=>x!==sh.id));if(typeof sh.id==="number")REMOVED.shifts.add(sh.id);c.shifts.splice(+t.dataset.sdel,1);saveCfg();renderLines()}};
  });
}

/* ---------- orders page ---------- */
let IMP=null, IMPV=null; // IMP: {header, rows, map, labels}; IMPV: server preview
async function previewImport(){if(!IMP)return;IMPV=null;const j=await api("/api/scheduler/import/preview",{rows:IMP.rows,map:IMP.map});if(j.ok)IMPV=j;renderOrders()}
function renderOrders(){
  const mos=Object.values(S.mos).sort((a,b)=>(a.due||9e15)-(b.due||9e15));
  const rows=mos.map(mo=>{const item=mo.item;const ms=mo.matches||[];
    const st=STAGE_ORDER.filter(o=>mo.ops&&mo.ops[o]).map(o=>{const r=S.res[jobKey(mo.id,o)];return r?`<span class="pill ${r.late?"bad":o}" title="${esc(machineById(r.machineId)?.name)}">${STAGES[o].tag} ${fmtDT(r.end).slice(4)}</span>`:`<span class="pill">${STAGES[o].tag} —</span>`}).join(" ");
    return `<tr data-id="${esc(mo.id)}"><td class="mono">${esc(mo.mo)}</td><td>${esc(mo.part)}${ms.length>1?`<br><select data-pick id="pk-${esc(mo.id)}" aria-label="Cost sheet match">${ms.map(it=>`<option value="${esc(it.uid)}" ${item&&item.uid===it.uid?"selected":""}>${esc(it.aq)} / ${esc(it.cust||"—")} · ${esc(it.cat)} · ${it.pleats} pl</option>`).join("")}</select>`:""}</td>
      <td>${item?`${esc(item.cat)} · ${item.pleats} pl`:`<span class="pill bad">Not on cost sheet</span> <input type="number" min="1" data-pleats id="pl-${esc(mo.id)}" value="${mo.pleats||""}" placeholder="pleats" style="width:72px" aria-label="Pleats">`}</td>
      <td class="n"><input type="number" min="1" data-qty id="qt-${esc(mo.id)}" value="${mo.qty}" aria-label="Quantity"></td>
      <td><input type="date" data-due id="du-${esc(mo.id)}" value="${mo.due?isoDay(mo.due):""}" aria-label="Due date"></td>
      <td><div class="opsel">${STAGE_ORDER.map(o=>`<button type="button" class="${o}" data-op="${o}" aria-pressed="${!!(mo.ops&&mo.ops[o])}" ${o==="s1"?"disabled":""} title="${STAGES[o].name}">${STAGES[o].tag}</button>`).join("")}</div></td>
      <td>${st}</td><td>${mo.produced?`<span class="muted small">${fmtN(mo.produced)} made</span>`:`<button class="btn sm danger" data-rm>Delete</button>`}</td></tr>`}).join("");
  const pv=IMPV;
  $("#main").innerHTML=`
  <div class="page-head"><div><h2>Manufacturing Orders</h2><p class="sub">Export open MOs from S2K or your database as Excel or CSV and bring them in here. MOs pulled from S2K on the Operator Kiosk land here too. Parts are matched to the cost sheet by customer part number or AQ number to get pleat counts and stage rates.</p></div></div>
  <div class="import-grid">
    <div class="card stack">
      <h3>Import MO list</h3>
      <div class="drop" id="drop"><p style="margin:0 0 8px">Drop an .xlsx or .csv export here</p><input type="file" id="file" accept=".xlsx,.xlsm,.csv,.txt"></div>
      <label class="small muted">Or paste rows copied from Excel (include the header row)<textarea id="paste" rows="5" placeholder="MO	Part	Qty	Due Date&#10;MO-24811	MG3012	4800	11/02/2026"></textarea></label>
      <div><button class="btn" id="pastego">Read pasted rows</button></div>
    </div>
    <div class="card stack">
      <h3>Columns</h3>
      ${IMP?`<div class="map">${IMP.labels.map(([k,l])=>`<label>${l}<select data-map="${k}" id="map-${k}"><option value="-1">—</option>${IMP.header.map((h,i)=>`<option value="${i}" ${IMP.map[k]===i?"selected":""}>${esc(h||"Column "+(i+1))}</option>`).join("")}</select></label>`).join("")}</div>
        ${pv?`<p class="small muted" style="margin:0">${pv.count} orders ready · ${pv.unmatched} parts not on the cost sheet · existing MO numbers will be updated, keeping their schedule.</p>
        ${pv.count?`<div class="tablewrap"><table><thead><tr><th>MO</th><th>Part</th><th class="n">Qty</th><th>Due</th><th>Stages</th></tr></thead><tbody>${pv.orders.map(p=>`<tr><td class="mono">${esc(p.mo)}</td><td>${esc(p.part)} ${p.matched?"":`<span class="pill bad">no match</span>`}</td><td class="n num">${fmtN(p.qty)}</td><td>${p.due?esc(p.due):"—"}</td><td>${STAGE_ORDER.filter(o=>p.ops[o]).map(o=>STAGES[o].tag).join(" + ")}</td></tr>`).join("")}</tbody></table></div>`:`<div class="note">No rows have an MO number, part and quantity yet. Check the column choices above.</div>`}
        <div class="fieldrow"><button class="btn primary" id="impgo" ${pv.count?"":"disabled"}>Add ${pv.count} orders</button><button class="btn ghost" id="impx">Clear</button></div>`:`<p class="muted small">Reading…</p>`}`
      :`<p class="muted small" style="margin:0">After you load a file, pick which column holds the MO number, part, quantity and due date. Optional columns can force End Band, Arts &amp; Crafts or Glue Bead on or off; otherwise the cost sheet decides which stages a part needs.</p>`}
    </div>
  </div>
  <div class="page-head" style="margin-top:24px"><div><h3>Open orders (${mos.length})</h3><p class="sub small">Toggle the stages an order needs. Stage 1 is always required and must be scheduled before EB, A&amp;C or GB. Orders with parts already made can't be deleted here; close them from the Operator Kiosk.</p></div><button class="btn" id="addmo">+ Add order by hand</button></div>
  <div id="addform"></div>
  <div class="tablewrap"><table><thead><tr><th>MO</th><th>Part</th><th>Cost sheet</th><th class="n">Qty</th><th>Due</th><th>Stages</th><th>Scheduled finish</th><th></th></tr></thead><tbody id="motb">${rows||`<tr><td colspan="8"><div class="empty"><b>No orders yet</b>Import an MO export above or add one by hand.</div></td></tr>`}</tbody></table></div>`;
  const parse=async fd=>{const j=await api("/api/scheduler/import/parse",fd,true);if(j.ok){IMP=j;await previewImport()}};
  const handle=f=>{const fd=new FormData();fd.append("file",f);parse(fd)};
  $("#file").onchange=e=>{if(e.target.files[0])handle(e.target.files[0])};
  const dz=$("#drop");dz.ondragover=e=>{e.preventDefault();dz.classList.add("over")};dz.ondragleave=()=>dz.classList.remove("over");
  dz.ondrop=e=>{e.preventDefault();dz.classList.remove("over");if(e.dataTransfer.files[0])handle(e.dataTransfer.files[0])};
  $("#pastego").onclick=()=>{const t=$("#paste").value;if(!t.trim()){toast("Paste some rows first.");return}const fd=new FormData();fd.append("text",t);parse(fd)};
  if(IMP){$$("[data-map]").forEach(s=>s.onchange=()=>{IMP.map[s.dataset.map]=+s.value;previewImport()});
    if($("#impx"))$("#impx").onclick=()=>{IMP=null;IMPV=null;renderOrders()};
    if($("#impgo"))$("#impgo").onclick=async()=>{$("#impgo").disabled=true;const j=await guard(api("/api/scheduler/import/commit",{rows:IMP.rows,map:IMP.map}));
      if(j.ok){IMP=null;IMPV=null;toast(`Added ${j.added} orders${j.updated?`, updated ${j.updated}`:""}${j.skipped?`. Skipped ${j.skipped} (closed, already started on another part, or numbers too long)`:""}.`)}await load(true)}}
  const tb=$("#motb");
  tb.onchange=e=>{const tr=e.target.closest("tr[data-id]");if(!tr)return;const id=+tr.dataset.id;const t=e.target;const p={id};
    if(t.hasAttribute("data-qty"))p.qty=Number(t.value)||S.mos[id].qty;
    if(t.hasAttribute("data-due"))p.due=t.value||null;
    if(t.hasAttribute("data-pleats"))p.pleats=Number(t.value)||null;
    if(t.hasAttribute("data-pick"))p.itemUid=t.value;
    putMO(p)};
  tb.onclick=async e=>{const tr=e.target.closest("tr[data-id]");if(!tr)return;const id=+tr.dataset.id;
    const ob=e.target.closest("[data-op]");if(ob){const ops=Object.assign({s1:true},S.mos[id].ops);ops[ob.dataset.op]=!ops[ob.dataset.op];await putMO({id,ops});return}
    const rm=e.target.closest("[data-rm]");if(rm){if(!rm.dataset.sure){rm.dataset.sure="1";rm.textContent="Confirm delete";return}await delMO(id)}};
  $("#addmo").onclick=()=>{$("#addform").innerHTML=`<div class="card fieldrow" style="margin-bottom:12px">
    <label>MO number<input type="text" id="nmo"></label><label>Part<input type="text" id="npart" placeholder="MG3012 or AQ1031"></label><label>Qty<input type="number" id="nqty" min="1"></label><label>Due<input type="date" id="ndue"></label>
    <button class="btn primary" id="nsave" style="align-self:flex-end">Add order</button><button class="btn ghost" id="ncancel" style="align-self:flex-end">Cancel</button></div>`;
    $("#nmo").focus();$("#ncancel").onclick=()=>$("#addform").innerHTML="";
    $("#nsave").onclick=async()=>{const mo=$("#nmo").value.trim(),part=$("#npart").value.trim(),qty=Number($("#nqty").value);
      if(!mo||!part||!qty){toast("Enter an MO number, part and quantity.");return}
      const r=await putMO({mo,part,qty,due:$("#ndue").value||null});
      if(r.ok&&!r.matched)toast("Added. That part isn't on the cost sheet, so enter its pleat count in the table.")}};
}

/* ---------- parts page ---------- */
let PQ="",PARTS=null,pqT=null;
async function loadParts(){const j=await api("/api/scheduler/catalog?q="+encodeURIComponent(PQ));if(j.ok){PARTS=j;if(currentRoute()==="parts")renderParts()}}
function renderParts(){
  if(!PARTS){$("#main").innerHTML=`<div class="empty"><b>Loading the cost sheet…</b></div>`;loadParts();return}
  const P=PARTS,src=P.source&&P.source.source?`${P.source.builtin?"Built from":"Imported from"} ${esc(P.source.source)}${P.source.importedAt?` on ${fmtD(P.source.importedAt)}`:""}`:"Cost sheet";
  $("#main").innerHTML=`
  <div class="page-head"><div><h2>Parts &amp; Run Rates</h2><p class="sub">${src}. ${P.count} parts. Stage 1 rates below use each Stage 1 line's current speed basis.</p></div>
    <div class="fieldrow"><label class="btn" for="cfile" style="cursor:pointer">Re-import cost sheet</label><input type="file" id="cfile" accept=".xlsx,.xlsm" hidden>${P.source&&!P.source.builtin?`<button class="btn ghost" id="creset">Use built-in sheet</button>`:""}</div></div>
  <div class="fieldrow" style="margin-bottom:10px"><input type="search" id="pq" placeholder="Search AQ, customer part or category" value="${esc(PQ)}" style="width:min(360px,100%)"><span class="muted small">${P.total>P.items.length?`Showing first ${P.items.length} of ${P.total}`:`${P.total} parts`}</span></div>
  <div class="tablewrap"><table><thead><tr><th>AQ #</th><th>Customer part</th><th>Category</th><th class="n">Pieces</th><th class="n">Pleats</th><th class="n">PPM</th><th class="n">New PPM</th>${P.lines.map(n=>`<th class="n">${esc(n)} /hr</th>`).join("")}<th class="n">Stage 2 /hr</th><th class="n">Mask qty</th><th class="n">Stage 3 (A&amp;C) /hr</th></tr></thead>
  <tbody>${P.items.map(it=>`<tr><td class="mono">${esc(it.aq)}</td><td class="mono">${esc(it.cust||"—")}</td><td>${esc(it.cat)}</td><td class="n num">${it.pieces??"—"}</td><td class="n num">${it.pleats}</td><td class="n num">${it.ppm??"—"}</td><td class="n num">${it.nppm??"—"}</td>${it.rates.map(r=>`<td class="n num">${fmtN(r)}</td>`).join("")}<td class="n num">${it.s2??"—"}</td><td class="n num">${it.mask??"—"}</td><td class="n num">${it.s3??"—"}</td></tr>`).join("")}</tbody></table></div>`;
  $("#pq").oninput=e=>{PQ=e.target.value;clearTimeout(pqT);pqT=setTimeout(loadParts,250)};
  $("#cfile").onchange=async e=>{const f=e.target.files[0];if(!f)return;const fd=new FormData();fd.append("file",f);
    const j=await guard(api("/api/scheduler/catalog/import",fd,true));if(j.ok){toast(`Loaded ${j.count} parts from ${f.name}.`);PARTS=null;await load();renderParts()}};
  if($("#creset"))$("#creset").onclick=async()=>{await guard(api("/api/scheduler/catalog/reset",{}));PARTS=null;await load();renderParts()};
}

render();load(true).then(poll);
