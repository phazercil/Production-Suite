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
  gb:{name:"Glue Bead",short:"Glue Bead",tag:"GB"},
  pk:{name:"Packaging",short:"Packaging",tag:"PK"}};
const STAGE_ORDER=["s1","eb","ac","gb","pk"];
/* An S2K order (plant/mofamily.py) has one MO per stage: the stage's own S2K number, and what that stage makes. */
const stageMo=(mo,o)=>(mo.stageMos||{})[o];
const stageQty=(mo,o)=>((mo.stageQty||{})[o])||mo.qty;
const noS1=mo=>!(mo.ops&&mo.ops.s1);

const $=(s,r=document)=>r.querySelector(s);
const $$=(s,r=document)=>Array.from(r.querySelectorAll(s));
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const clone=o=>JSON.parse(JSON.stringify(o));
const uid=p=>p+Math.random().toString(36).slice(2,9);
const fmtN=n=>Math.round(n).toLocaleString("en-US");
const fmtS=n=>n>=1000?(n/1000).toFixed(n>=10000?0:1).replace(/\.0$/,"")+"k":String(Math.round(n));
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
  S.cfg=j.cfg;S.cfgSig=j.cfgSig;S.modes=j.modes;S.mos={};j.mos.forEach(m=>S.mos[m.id]=m);S.jobs=j.jobs;S.res=j.res;S.byM=j.byM;S.work=j.work;S.hist=j.hist||[];S.labor=j.labor||{on:false,lines:{},days:{}};S.catalog=j.catalog;S.v=j.v;S.loaded=true;
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
let resizeT;window.addEventListener("resize",()=>{clearTimeout(resizeT);resizeT=setTimeout(()=>{if($(".gwrap")&&!$("#layer").firstChild)render()},250)});
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
/* Hours no shift covers (e.g. midnight to 6 am) are left off the board: every day shows only the hours from the
   earliest shift start to the latest shift end of all the plant's shifts (also a shift no line is crewed for yet,
   so a planner sees the whole working day). A shift past midnight shows whole days. */
/* A shift as worked on the day starting at d: its seasonal variant (Admin › Machine schedule) when one covers the day. */
function shiftOn(sh,d){const iso=isoDay(d);for(const v of sh.variants||[])if(v.from<=iso&&iso<=v.to)return {...sh,start:v.start,end:v.end,days:v.days,breaks:v.breaks,variant:v.name||"Seasonal variant"};return sh}
/* Each shift's stretch of every day in view, shaded on the board: 1st shift lightest, 2nd darker, 3rd darker still. */
function shiftBands(a,b){
  const sh=S.cfg.shifts.filter(s=>(s.days||[]).length).slice().sort((x,y)=>hm(x.start)-hm(y.start));
  const out=[];
  for(let d=addDays(a,-1);d<b;d=addDays(d,1)){const dow=new Date(d).getDay();
    sh.forEach((s0_,i)=>{const s=shiftOn(s0_,d);if(!(s.days||[]).includes(dow))return;const s0=hm(s.start),L=(hm(s.end)-s0+1440)%1440||1440;
      const st=d+s0*MIN,en=st+L*MIN;if(en>a&&st<b)out.push({s:Math.max(st,a),e:Math.min(en,b),n:i+1,name:s.name})})}
  return out;
}
function bandHtml(bands,a,px){return bands.map(x=>`<div class="sband n${Math.min(x.n,3)}" style="left:${X(x.s,a,px)}px;width:${Math.max(0,X(x.e,a,px)-X(x.s,a,px))}px" title="${esc(x.name)}"></div>`).join("")}
let TMAP=null;   // time <-> board position for the view on screen
/* A day no line works (a weekend or holiday with no shift crewed, and nothing scheduled) shrinks to a thin strip,
   so the board is mostly production time. Its hours still map onto the strip, so nothing on it is lost. */
const CLOSED_W={week:30,month:8};
/* The stretches of a day some shift covers (an overnight shift's end included), plus any crewed or scheduled time
   outside them: the board shows only these, so a night with no shift doesn't take up room. */
function daySegs(d){
  const out=[],e0=addDays(d,1),clip=(x,y)=>{x=Math.max(x,d);y=Math.min(y,e0);if(y>x)out.push([x,y])};
  for(const sh0 of S.cfg.shifts||[])for(const k of [-1,0]){const day=addDays(d,k),sh=shiftOn(sh0,day),days=sh.days||[];if(!days.includes(new Date(day).getDay()))continue;
    const s0=hm(sh.start),L=(hm(sh.end)-s0+1440)%1440||1440;clip(day+s0*MIN,day+(s0+L)*MIN)}
  for(const segs of Object.values(S.work||{}))for(const [x,y] of segs)clip(x,y);
  for(const r of Object.values(S.res||{}))if(r&&r.end>d&&r.start<e0)for(const [x,y] of (r.runSegs&&r.runSegs.length?r.runSegs:[[r.start,r.end]]))clip(x,y);
  out.sort((x,y)=>x[0]-y[0]);const m=[];
  for(const g of out){const l=m[m.length-1];if(l&&g[0]<=l[1]+MIN)l[1]=Math.max(l[1],g[1]);else m.push([g[0],g[1]])}
  return m;
}
function makeMap(a,b,px){
  const days=[];let off=0;
  const cw=CLOSED_W[S.view.scale];const all=[];for(let d=a;d<b;d=addDays(d,1))all.push([d,daySegs(d)]);
  const any=all.some(([,g])=>g.length);
  for(const [d,g] of all){
    const closed=!!(cw&&any&&!g.length),segs=[];let w=0;
    if(closed){segs.push({s:d,e:addDays(d,1),off,w:cw});w=cw}
    else for(const [x,y] of (g.length?g:[[d,addDays(d,1)]])){const sw=(y-x)/HOUR*px;segs.push({s:x,e:y,off:off+w,w:sw});w+=sw}
    days.push({d,s:segs[0].s,e:segs[segs.length-1].e,off,w,closed,segs});off+=w}
  return {a,b,px,days,W:off,
    x(t){if(t<=a)return 0;if(t>=b)return off;let k=days.length-1;while(k>0&&t<days[k].d)k--;const D=days[k];
      for(const g of D.segs){if(t<=g.e)return g.off+(Math.max(t,g.s)-g.s)/(g.e-g.s)*g.w}return D.off+D.w},
    t(x){let k=days.length-1;while(k>0&&x<days[k].off)k--;const D=days[k];let g=D.segs[0];
      for(const q of D.segs)if(x>=q.off)g=q;return Math.min(g.e,g.s+Math.max(0,x-g.off)/g.w*(g.e-g.s))}};
}
/* the hours shown on a day, as text ("07:00 – 24:00", two stretches joined by ·) */
const segLabel=D=>D.segs.map(g=>{const f=t=>{const x=new Date(t),h=t===addDays(D.d,1)?24:x.getHours();return String(h).padStart(2,"0")+":"+String(x.getMinutes()).padStart(2,"0")};return f(g.s)+"–"+f(g.e)}).join(" · ");
const dayClosed=t=>!!(TMAP&&TMAP.days.find(D=>D.d===t)?.closed);
const X=(t,a,px)=>TMAP&&TMAP.a===a&&TMAP.px===px?TMAP.x(t):(t-a)/HOUR*px;
function dayHours(D){const out=[];for(const g of D.segs)for(let t=Math.floor(g.s/HOUR)*HOUR;t<g.e;t+=HOUR)out.push([Math.max(t,g.s),Math.min(t+HOUR,g.e)]);return out}
function buckets(a,b){const out=[];if(S.view.scale==="day"){if(TMAP)out.push(...dayHours(TMAP.days[0]));else for(let t=a;t<b;t+=HOUR)out.push([t,t+HOUR])}else{for(let t=a;t<b;t=addDays(t,1))out.push([t,addDays(t,1)])}return out}
function rangeLabel(a,b){
  const d=new Date(a),mon=d.toLocaleString("en-US",{month:"long"});
  if(S.view.scale==="day")return d.toLocaleDateString("en-US",{weekday:"long",month:"short",day:"numeric",year:"numeric"});
  if(S.view.scale==="month")return `${mon} ${d.getFullYear()}`;
  const e=new Date(addDays(b,-1));return `${d.toLocaleDateString("en-US",{month:"short",day:"numeric"})} – ${e.toLocaleDateString("en-US",{month:"short",day:"numeric",year:"numeric"})}`;
}
const machineById=id=>S.cfg.machines.find(m=>m.id===id);
function qtyIn(r,a,b){const tot=r.runSegs.reduce((s,[x,y])=>s+(y-x),0);if(!tot)return 0;let ov=0;for(const [x,y] of r.runSegs){const o=Math.min(y,b)-Math.max(x,a);if(o>0)ov+=o}return r.qty*ov/tot}
/* time saved (+) or lost (−) against the plan's speed */
function paceTxt(min){min=Math.round(min||0);return min>=0?`${fmtDur(min)} saved`:`${fmtDur(-min)} lost`}
function paceBadge(min){min=Math.round(min||0);if(Math.abs(min)<1)return "";return `<span class="pace ${min>=0?"ahead":"behind"}" title="${esc(paceTxt(min))} against the planned speed">${min>=0?"+":"−"}${fmtDur(Math.abs(min))}</span>`}
function paceLines(p){if(!p)return "";const pct=Math.round((p.eff/p.planEff-1)*100);
  return `\nRunning ${pct>=0?pct+"% faster":(-pct)+"% slower"} than planned (${fmtN(Math.round(p.eff))}/hr against ${fmtN(Math.round(p.planEff))}/hr over ${fmtDur(p.workMin)} of run time)\n${paceTxt(p.savedMin)} so far · the rest is planned at the real speed, so it finishes ${p.finishMin>=0?fmtDur(p.finishMin)+" sooner":fmtDur(-p.finishMin)+" later"}`}
function resultsFor(mid){return (S.byM[mid]||[]).map(k=>S.res[k]).filter(Boolean)}
function renderSchedule(){
  const main=$("#main");
  const keepScroll=$(".gwrap")?{l:$(".gwrap").scrollLeft,t:$(".gwrap").scrollTop,k:S.view.scale+S.view.anchor}:null;
  const railScroll=$(".rail-list")?$(".rail-list").scrollTop:0;
  const {sc,a,b}=viewWindow();let px=sc.px;TMAP=makeMap(a,b,px);
  /* week and month fill the screen: working days stretch, the thin strips for closed days keep their width */
  if(S.view.scale!=="day"){const labw=parseFloat(getComputedStyle($(".planner")||document.body).getPropertyValue("--labw"))||200;
    const avail=(main.clientWidth||1200)-(($(".rail")?.offsetWidth)||300)-labw-80,fixed=TMAP.days.filter(D=>D.closed).reduce((s,D)=>s+D.w,0);
    if(TMAP.W-fixed>0&&avail>TMAP.W){px=px*(avail-fixed)/(TMAP.W-fixed);TMAP=makeMap(a,b,px)}}
  const W=Math.round(X(b,a,px));
  const mos=Object.values(S.mos);
  const late=new Set(),waiting=new Set();let scheduled=0;
  for(const mo of mos){const s1Done=mo.status!=="OPEN"||(mo.net??mo.produced??0)>=mo.qty;const ops=STAGE_ORDER.filter(o=>mo.ops&&mo.ops[o]&&!(o==="s1"&&s1Done));const rs=ops.map(o=>S.res[jobKey(mo.id,o)]).filter(Boolean);
    if(rs.length===ops.length&&ops.length)scheduled++;if(rs.some(r=>r.late))late.add(mo.id);if(rs.some(r=>r.waiting))waiting.add(mo.id)}
  const unsched=mos.length-scheduled;
  let axis="";const now=Date.now();
  if(S.view.scale==="day"){for(const [t,e] of dayHours(TMAP.days[0])){const h=new Date(t).getHours();axis+=`<div class="tick ${h%6===0?"major":""}" style="left:${X(t,a,px)}px;width:${X(e,a,px)-X(t,a,px)}px"><span class="big">${String(h).padStart(2,"0")}:${String(new Date(t).getMinutes()).padStart(2,"0")}</span></div>`}}
  else{for(let t=a;t<b;t=addDays(t,1)){const d=new Date(t),w=X(addDays(t,1),a,px)-X(t,a,px),we=d.getDay()===0||d.getDay()===6,td=sod(now)===t;
    if(dayClosed(t)){axis+=`<div class="tick closed ${td?"today":""}" style="left:${X(t,a,px)}px;width:${w}px" title="${DOW[d.getDay()]} ${d.getMonth()+1}/${d.getDate()}: no shift on any line"><span class="big">${S.view.scale==="week"?DOW[d.getDay()].slice(0,2):""}</span>${S.view.scale==="week"?d.getDate():""}</div>`;continue}
    const sp=(S.labor.days||{})[isoDay(t)],spT=sp?` title="${sp==="WORK"?"Extra work day":sp==="HOLIDAY"?"Holiday":"Day off"} in the Labor Budget plan"`:"",spL=sp==="HOLIDAY"?"Holiday":sp==="OFF"?"Off":sp==="WORK"?"Work day":"";
    axis+=S.view.scale==="week"?`<div class="tick major ${we?"wkend":""} ${td?"today":""} ${sp&&sp!=="WORK"?"hol":""}" style="left:${X(t,a,px)}px;width:${w}px"${spT}><span class="big">${DOW[d.getDay()]} ${d.getMonth()+1}/${d.getDate()}</span>${spL||segLabel(TMAP.days.find(D=>D.d===t))}</div>`
      :`<div class="tick ${d.getDay()===1?"major":""} ${we?"wkend":""} ${td?"today":""} ${sp&&sp!=="WORK"?"hol":""}" style="left:${X(t,a,px)}px;width:${w}px"${spT}><span class="big">${d.getDate()}</span>${sp==="HOLIDAY"?"H":DOW[d.getDay()][0]}</div>`}}
  const bk=buckets(a,b);
  const closedHtml=TMAP.days.filter(D=>D.closed).map(D=>`<div class="closedday" style="left:${D.off}px;width:${D.w}px"></div>`).join("")
    +TMAP.days.filter(D=>!D.closed).flatMap(D=>D.segs.slice(1).map(g=>`<div class="segcut" style="left:${g.off}px" title="Hours with no shift left out"></div>`)).join("");
  const bands=bandHtml(shiftBands(a,b),a,px)+closedHtml;
  const nowX=(now>=a&&now<b)?`<div class="nowline" style="left:${X(now,a,px)}px"></div>`:"";
  let rows="";
  for(const st of STAGE_ORDER){
    const ms=S.cfg.machines.filter(m=>m.stage===st);if(!ms.length)continue;
    const all=ms.flatMap(m=>resultsFor(m.id));
    const gTot=all.reduce((s,r)=>s+qtyIn(r,a,b),0);
    const gcells=bk.map(([x,y])=>{const q=all.reduce((s,r)=>s+qtyIn(r,x,y),0),cw=X(y,a,px)-X(x,a,px);if(!q||cw<20)return "";
      return `<div class="qcell has" style="left:${X(x,a,px)}px;width:${cw}px" title="${fmtN(Math.round(q))} pcs ${S.view.scale==="day"?"this hour":"on "+fmtD(x)}">${cw<60?fmtS(q):fmtK(q)}</div>`}).join("");
    rows+=`<div class="g-row group"><div class="g-lab"><span class="nm"><span class="swatch" style="background:var(--${st==="s1"?"st1":st})"></span>${esc(STAGES[st].name)}</span><span class="tot">${fmtN(Math.round(gTot))} pcs</span></div><div class="g-track" style="width:${W}px">${bands}${gcells}</div></div>`;
    for(const m of ms){
      const rs=resultsFor(m.id);
      const work=S.work[m.id]||[];let off="",cur=a;
      /* week and month leave out breaks too short to see (lunch, start-up); the day view shows them all */
      const minGap={day:0,week:45*MIN,month:3*HOUR}[S.view.scale]||0;
      for(const [s,e] of work){if(s>cur&&(s-cur>=minGap||cur===a))off+=`<div class="off" style="left:${X(cur,a,px)}px;width:${X(s,a,px)-X(cur,a,px)}px"></div>`;cur=e}
      if(cur<b)off+=`<div class="off" style="left:${X(cur,a,px)}px;width:${X(b,a,px)-X(cur,a,px)}px"></div>`;
      const grid=S.view.scale==="day"?"":bk.map(([x])=>`<div class="gridline" style="left:${X(x,a,px)}px"></div>`).join("");
      const bars=rs.filter(r=>r.end>a&&r.start<b).map(r=>{
        const mo=S.mos[r.moId];if(!mo)return "";
        const l=X(r.start,a,px),w=r.rest?Math.max(96,X(r.end,a,px)-l):Math.max(4,X(r.end,a,px)-l),cw=r.coMin?Math.min(w,X(r.runStart,a,px)-l):0;
        const pl=r.parts>1?` · ${r.part}/${r.parts}`:"",sp=r.parts>1&&r.part===1?r.split:null;
        const spTxt=sp?(sp.mode==="qty"?`Part 1 stops after ${fmtN(sp.qty)}`:sp.mode==="time"?`Part 1 stops at ${fmtDT(sp.at)}`:"Part 1 runs until the operator ends the MO"):"";
        const tip=`${mo.mo}${pl} · ${mo.part} · ${r.rest?"the rest once the operator ends part 1":fmtN(r.qty)+" pcs"}${r.running?" · RUNNING at the kiosk now":""}${paceLines(r.pace)}${spTxt?"\n"+spTxt:""}\n${fmtDT(r.start)} → ${fmtDT(r.end)}${r.coMin?`\n${r.inkjet?"Inkjet-only changeover":"Changeover"} ${fmtDur(r.coMin)}: ${fmtDT(r.start)} → ${fmtDT(r.runStart)}`:r.inkjet?"\nInkjet-only changeover (same base part): no changeover time":""}\nRunning ${fmtDT(r.runStart)} → ${fmtDT(r.end)}\n${r.basis}${r.laborCost!=null?`\nCrew wages ${fmtMoney(r.laborCost)}${r.qty?` (${fmtMoney(r.laborCost/r.qty,3)} / pc)`:""}`:""}${r.warn.length?"\n⚠ "+r.warn.join("\n⚠ "):""}${r.late?"\n⚠ Finishes after due date":""}`;
        const canSplit=!S.editLocked&&!r.unplanned&&(r.parts<2||r.part===1);
        return `<div class="bar ${r.op} ${r.late?"late":""} ${r.waiting?"waiting":""} ${r.running?"running":""} ${r.rest?"rest":""} ${r.parts>1?"split":""}" draggable="true" data-job="${esc(r.id)}" style="left:${l}px;width:${w}px" title="${esc(tip)}">${cw>0?`<div class="co" style="width:${cw}px">${cw>=34?`<span><i>CO ${Math.round(r.coMin)}m</i></span>`:""}</div>`:""}<div class="run"><b>${esc(mo.mo)}${pl}</b><span>${r.rest?"rest · when part 1 ends":`${esc(mo.part)} · ${fmtK(r.qty)}`}</span></div>${r.pace&&w>=170?paceBadge(r.pace.savedMin):""}${(r.late||r.waiting||r.unplanned||r.warn.length)?`<span class="flag">!</span>`:""}${canSplit&&w>=40?`<button type="button" class="splitbtn" data-split="${esc(jobKey(r.moId,r.op))}" title="${r.parts>1?"Change or undo the split":"Split this run: stop at a time, when the operator ends it, or after a quantity"}" aria-label="Split ${esc(mo.mo)}">${r.parts>1?"⇄":"✂"}</button>`:""}</div>`}).join("");
      /* what already ran on this line (from the kiosks), behind the plan */
      const hbars=(S.hist||[]).filter(h=>h.machineId===m.id&&h.end>a&&h.start<b).map(h=>{
        const l=X(Math.max(h.start,a),a,px),w=Math.max(3,X(Math.min(h.end,b),a,px)-l);
        const nums=h.net!=null?`Gross ${fmtN(h.gross)} · Net ${fmtN(h.net)} · Scrap ${fmtN(h.scrap)}`:`${fmtN(h.gross)} counted so far`;
        const tip=`${h.now?"Running now":"Ran"}: ${h.mo} · ${h.part}\n${fmtDT(h.start)} → ${h.now?"now":fmtDT(h.end)}\n${nums}${h.planMin!=null?`\nPlanned speed ${fmtDur(h.planMin)} for that many · took ${fmtDur(h.workMin)} of shift time · ${paceTxt(h.savedMin)}`:""}`;
        return `<div class="hbar ${m.stage} ${h.now?"now":""}" style="left:${l}px;width:${w}px" title="${esc(tip)}"><b>${esc(h.mo)}</b><span>${fmtK(h.net!=null?h.net:h.gross)}${h.savedMin!=null&&w>=70?" "+paceBadge(h.savedMin):""}</span></div>`}).join("");
      const tot=rs.reduce((s,r)=>s+qtyIn(r,a,b),0);
      const cells="";
      const U={ppm:"PPM",mms:"mm/s",pcs:"parts/min"}[m.unit]||"PPM";
      const sp=m.stage==="s1"?`${m.mode==="fixed"||m.unit&&m.unit!=="ppm"?m.ppm+" "+U:m.mode==="new"?"New-machine PPM":"Sheet PPM"}`:`${(S.modes[m.stage]||[]).find(x=>x[0]===m.mode)?.[1].split(" (")[0]||"Fixed"}`;
      const lab=(S.labor.lines||{})[m.id];
      const crewTxt=lab?Object.values(lab.shifts).sort((x,y)=>x.n-y.n).map(c=>c.staffed?`${ordinal(c.n)} ${c.crew}`:`${ordinal(c.n)} no crew`).join(" · "):"";
      const labTip=`${m.name}\n${sp} · ${m.scrap}% scrap · ${m.downtime}% down${lab?"\nCrew: "+crewTxt:""}\n${fmtN(Math.round(tot))} pcs in view`;
      rows+=`<div class="g-row ${rs.some(r=>r.end>a&&r.start<b)?"":"idle"}"><div class="g-lab" title="${esc(labTip)}"><span class="nm">${esc(m.name)}</span><span class="ds">${tot?`<b>${fmtK(Math.round(tot))}</b> pcs · `:""}${esc(sp)}</span></div><div class="g-track" data-m="${esc(m.id)}" style="width:${W}px">${bands}${off}${grid}${hbars}${bars}${cells}</div></div>`;
    }
  }
  /* Orders by stage: an MO with several stages is listed once in each stage it still needs, so each section is the
     work waiting for that kind of line. A card drags (or opens) its own stage. */
  const q=S.view.q.trim().toLowerCase();
  const moList=mos.filter(mo=>!q||`${mo.mo} ${mo.part}`.toLowerCase().includes(q))
    .sort((x,y)=>(x.due||9e15)-(y.due||9e15)||String(x.mo).localeCompare(String(y.mo)));
  const card=(mo,o)=>{
    const item=mo.item,k=jobKey(mo.id,o),r=S.res[k],j=S.jobs[k];
    const s1Done=noS1(mo)||mo.status!=="OPEN"||(mo.net??mo.produced??0)>=mo.qty;
    const blocked=o!=="s1"&&!s1Done&&!S.jobs[jobKey(mo.id,"s1")];
    const smo=stageMo(mo,o);
    const others=STAGE_ORDER.filter(x=>x!==o&&mo.ops&&mo.ops[x]).map(x=>{const rx=S.res[jobKey(mo.id,x)];const dn=x==="s1"&&s1Done;
      return `<span class="chip mini ${x} ${rx||dn?"done":""}" title="${esc(STAGES[x].name)}: ${dn?"finished at the pleater":rx?`${esc(machineById(rx.machineId)?.name||"")} ${fmtDT(rx.start)} → ${fmtDT(rx.end)}`:"not on the board yet"}">${STAGES[x].tag}</span>`}).join("");
    const where=r?`<span class="on">${esc(machineById(r.machineId)?.name||"")} · ${fmtDT(r.start)} → ${fmtDT(r.end).slice(4)}</span>`:blocked?`<span class="muted">Schedule Stage 1 first</span>`:`<span class="muted">Not on the board yet</span>`;
    return `<div class="mo ${o} ${blocked?"blocked":"grab"} ${j?"placed":""}" ${blocked?"":`draggable="true"`} data-mo="${esc(mo.id)}" data-op="${o}" tabindex="0" role="button" title="${blocked?"Schedule Stage 1 for this order first":`Drag onto a ${esc(STAGES[o].short)} line, or click to set a start time`}">
      <div class="mo-top"><span class="mo-id">${esc(mo.mo)}${mo.example?' <span class="pill">example</span>':""}${o!=="s1"&&mo.status!=="OPEN"?' <span class="pill" title="Closed at the pleater">S1 closed</span>':""}</span><span class="mo-qty">${fmtN(stageQty(mo,o))}</span></div>
      ${smo&&smo.mo!==mo.mo?`<div class="mo-meta"><span title="The S2K MO for this stage">${STAGES[o].tag} MO ${esc(smo.mo)} · ${esc(smo.part)}</span></div>`:""}
      <div class="mo-meta"><span>${esc(mo.part)}${item?` · ${esc(item.cat)} · ${item.pleats} pl`:mo.pleats?` · ${mo.pleats} pl`:` · <span class="late">not in cost sheet</span>`}</span>${mo.due?`<span class="${late.has(mo.id)?"late":""}">Due ${fmtD(mo.due)}</span>`:""}</div>
      ${mo.produced||mo.scrapped?`<div class="mo-nums" title="Counted at the pleater: gross is every filter made, scrap what was scrapped, net the good ones"><span>Gross <b>${fmtN(mo.produced||0)}</b></span><span>Net <b>${fmtN(mo.net||0)}</b></span><span class="${mo.scrapped?"sc":""}">Scrap <b>${fmtN(mo.scrapped||0)}</b></span></div>`:""}
      ${r&&r.pace?`<div class="mo-pace ${r.pace.savedMin>=0?"ahead":"behind"}">${r.pace.eff>=r.pace.planEff?"Faster":"Slower"} than planned · ${paceTxt(r.pace.savedMin)} · finish moved ${r.pace.finishMin>=0?fmtDur(r.pace.finishMin)+" earlier":fmtDur(-r.pace.finishMin)+" later"}</div>`:""}
      <div class="mo-where">${where}${others?`<span class="others">${others}</span>`:""}</div></div>`};
  const sections=STAGE_ORDER.map(o=>{
    const list=moList.filter(mo=>{if(!(mo.ops&&mo.ops[o]))return false;const s1Done=noS1(mo)||mo.status!=="OPEN"||(mo.net??mo.produced??0)>=mo.qty;
      if(o==="s1"&&s1Done)return false;
      return S.view.showDone||!S.jobs[jobKey(mo.id,o)]});
    if(!list.length&&!S.cfg.machines.some(m=>m.stage===o))return "";
    const open=!(S.view.closed||{})[o];
    return `<section class="rail-sec ${o}"><button type="button" class="rail-sec-h" data-sec="${o}" aria-expanded="${open}"><span class="swatch" style="background:var(--${o==="s1"?"st1":o})"></span>${esc(STAGES[o].name)}<span class="n">${list.length}</span></button>
      ${open?(list.map(mo=>card(mo,o)).join("")||`<p class="muted small" style="padding:6px 12px">${S.view.showDone?"No orders for this stage.":"Everything for this stage is on the board."}</p>`):""}</section>`}).join("");
  const rail=moList.length?sections:"";
  main.innerHTML=`
  <div class="toolbar">
    <div class="seg" role="group" aria-label="Scale">${["day","week","month"].map(s=>`<button data-scale="${s}" aria-pressed="${S.view.scale===s}">${s}</button>`).join("")}</div>
    <button class="btn" id="prev" aria-label="Previous">‹</button><button class="btn" id="today">Today</button><button class="btn" id="next" aria-label="Next">›</button>
    <input type="date" id="jump" aria-label="Jump to date" value="${isoDay(S.view.anchor)}">
    <span class="range">${esc(rangeLabel(a,b))}</span>
    <span class="spacer"></span>
    <div class="legend"><span><i style="background:var(--st1)"></i>Stage 1</span><span><i style="background:var(--eb)"></i>End Band</span><span><i style="background:var(--ac)"></i>Arts &amp; Crafts</span><span><i style="background:var(--gb)"></i>Glue Bead</span><span><i class="hatch"></i>Changeover</span>${S.cfg.shifts.filter(s=>(s.days||[]).length).slice().sort((x,y)=>hm(x.start)-hm(y.start)).slice(0,3).map((s,i)=>`<span><i class="sband-key n${i+1}"></i>${esc(s.name)}</span>`).join("")}<span><i class="offkey"></i>Off shift / break</span></div>
  </div>
  <div class="kpis">
    <span class="kpi"><b>${mos.length}</b> orders</span><span class="kpi"><b>${scheduled}</b> fully scheduled</span>
    <span class="kpi ${unsched?"warn":""}"><b>${unsched}</b> need scheduling</span>
    <span class="kpi ${late.size?"bad":""}"><b>${late.size}</b> finish after due date</span>
    ${waiting.size?`<span class="kpi bad"><b>${waiting.size}</b> waiting on Stage 1</span>`:""}
    ${(()=>{const ps=Object.values(S.res).filter(r=>r.pace);if(!ps.length)return "";const t=ps.reduce((s,r)=>s+r.pace.savedMin,0);
      return `<span class="kpi ${t>=0?"ok":"bad"}" title="The MOs running now against their planned speed; their runs are re-planned at the real speed"><b>${t>=0?"+":"−"}${fmtDur(Math.abs(t))}</b> ${t>=0?"ahead":"behind"} on running MOs</span>`})()}
  </div>
  <div class="board">
    <aside class="rail" aria-label="Manufacturing orders">
      <div class="rail-head">
        <div class="row"><h3>Orders</h3><span class="muted small">by stage · drag onto a line</span></div>
        <div class="row"><input type="search" id="q" placeholder="Find MO or part" value="${esc(S.view.q)}"></div>
        <label class="check small"><input type="checkbox" id="showdone" ${S.view.showDone?"checked":""}> Show stages already on the board</label>
      </div>
      <div class="rail-list">${rail||`<div class="empty"><b>No orders yet</b>Import your MO list on the <a href="#orders">Orders</a> page.</div>`}</div>
    </aside>
    <div class="gwrap"><div class="g" style="width:calc(var(--labw) + ${W}px)">
      <div class="g-head"><div class="g-corner">Line</div><div class="g-axis" style="width:${W}px">${bands}${axis}</div></div>
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
  $$(".rail-sec-h").forEach(h=>h.onclick=()=>{S.view.closed=Object.assign({},S.view.closed,{[h.dataset.sec]:h.getAttribute("aria-expanded")==="true"});render()});
  $$(".rail .mo[data-op]").forEach(c=>{
    const blocked=c.classList.contains("blocked");
    c.addEventListener("dragstart",e=>{if(blocked){e.preventDefault();toast("Schedule Stage 1 for this order first.");return}
      DRAG={moId:+c.dataset.mo,op:c.dataset.op,grabPx:0};e.dataTransfer.setData("text/plain",jobKey(c.dataset.mo,c.dataset.op));e.dataTransfer.effectAllowed="move";c.classList.add("dragging")});
    c.addEventListener("dragend",()=>c.classList.remove("dragging"));
    const open=()=>{if(blocked){toast("Schedule Stage 1 for this order first.");return}openJobDialog(+c.dataset.mo,c.dataset.op)};
    c.addEventListener("click",open);c.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();open()}});
  });
  $$(".bar").forEach(bEl=>{
    bEl.addEventListener("dragstart",e=>{const j=S.jobs[bEl.dataset.job];if(!j)return;const rect=bEl.getBoundingClientRect();
      DRAG={moId:j.moId,op:j.op,part:j.part||1,grabPx:e.clientX-rect.left,w:rect.width,from:j.id};e.dataTransfer.setData("text/plain",j.id);e.dataTransfer.effectAllowed="move";
      bEl.classList.add("dragging")});
    bEl.addEventListener("dragend",()=>bEl.classList.remove("dragging"));
    bEl.addEventListener("click",e=>{const j=S.jobs[bEl.dataset.job];if(!j)return;
      if(e.target.closest(".splitbtn")||(j.part||1)>1){openSplitDialog(j.moId,j.op);return}
      openJobDialog(j.moId,j.op)});
  });
  /* where the MO would start: the dragged bar's left edge (a bar keeps the spot it was grabbed by), snapped */
  const tAt=(tr,e,d=DRAG)=>{const rect=tr.getBoundingClientRect();const x=Math.max(0,e.clientX-rect.left-(d&&d.grabPx||0));
    const t=TMAP?TMAP.t(x):a+x/px*HOUR;const sn=SCALES[S.view.scale].snap;return Math.round(t/sn)*sn};
  $$(".g-track[data-m]").forEach(tr=>{
    const m=machineById(+tr.dataset.m);
    tr.addEventListener("dragover",e=>{if(!DRAG)return;const ok=m.stage===DRAG.op;tr.classList.toggle("drop-ok",ok);tr.classList.toggle("drop-no",!ok);
      if(ok){e.preventDefault();e.dataTransfer.dropEffect="move";let g=tr.querySelector(".ghost-line");if(!g){g=document.createElement("div");g.className="ghost-line";g.innerHTML="<span></span>";tr.appendChild(g)}
        const t=tAt(tr,e);g.style.left=X(t,a,px)+"px";g.style.width=(DRAG.w?Math.max(2,DRAG.w):2)+"px";g.classList.toggle("box",!!DRAG.w);g.firstChild.textContent="Starts "+fmtDT(t)}});
    tr.addEventListener("dragleave",e=>{if(!tr.contains(e.relatedTarget)){tr.classList.remove("drop-ok","drop-no");tr.querySelector(".ghost-line")?.remove()}});
    tr.addEventListener("drop",async e=>{e.preventDefault();tr.classList.remove("drop-ok","drop-no");tr.querySelector(".ghost-line")?.remove();
      if(!DRAG)return;const d=DRAG;DRAG=null;
      if(m.stage!==d.op){toast(`${STAGES[d.op].short} work can only go on a ${STAGES[d.op].short} line.`);return}
      if(d.op!=="s1"&&!S.jobs[jobKey(d.moId,"s1")]){toast("Schedule Stage 1 for this order first.");return}
      const k=d.from||jobKey(d.moId,d.op),want=tAt(tr,e,d);
      const was=S.res[k];
      if(was&&was.running&&was.machineId===m.id){toast(`${S.mos[d.moId]?.mo||"This MO"} is running on ${m.name} now, so it stays at now until the operator ends it at the kiosk.`);return}
      const r=await putJob({moId:d.moId,op:d.op,part:d.part||1,machineId:m.id,reqStart:want});
      const x=S.res[k];
      if(r.ok&&x){
        if(x.paced)toast(`Held back to ${fmtDT(x.start)} so it doesn't run out of Stage 1 parts.`);
        else if(x.start-want>SCALES[S.view.scale].snap)toast(`Starts ${fmtDT(x.start)}: the first time ${m.name} is crewed and free from ${fmtDT(want)}.`)}
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

/* ---------- split a run in two ---------- */
function openSplitDialog(moId,op){
  const mo=S.mos[moId];if(!mo)return;const k=jobKey(moId,op),r1=S.res[k],j1=S.jobs[k];if(!j1)return;
  const k2=`${k}__2`,r2=S.res[k2],sp=r1&&r1.parts>1?r1.split:null;
  const total=op==="s1"?Math.max(0,mo.qty-(mo.net??mo.produced??0)):mo.qty;
  const mid=r1?Math.round(((r1.runStart+r1.end)/2)/(15*MIN))*15*MIN:Date.now();
  const mode=sp?sp.mode:"time";
  $("#layer").innerHTML=`<div class="scrim" id="scrim"><div class="dlg" role="dialog" aria-modal="true" aria-labelledby="dlgt">
    <div class="hd"><div><h2 id="dlgt">Split ${esc(mo.mo)} · ${esc(STAGES[op].name)}</h2><div class="muted small">${esc(mo.part)} · ${fmtN(total)} to make${r1?` · on ${esc(machineById(r1.machineId)?.name||"")}, ${fmtDT(r1.runStart)} → ${fmtDT(r1.end)}`:""}</div></div><button class="btn ghost" id="dx" aria-label="Close">✕</button></div>
    <p class="muted small" style="margin:0 0 10px">Part 1 stays where it is and stops as set here. Part 2 makes the rest: it starts right after part 1 on the same line, and you can drag it to any time or line.</p>
    <fieldset class="splitopts">
      <label class="opt"><input type="radio" name="sm" value="time" ${mode==="time"?"checked":""}><span><b>At a time of day</b><small>Part 1 stops then; part 2 makes what's left.</small><input type="datetime-local" id="sat" value="${toLocalInput(sp&&sp.at||mid)}"></span></label>
      ${op==="s1"?`<label class="opt"><input type="radio" name="sm" value="operator" ${mode==="operator"?"checked":""}><span><b>When the operator ends the MO</b><small>Part 1 runs until it's ended at the kiosk; part 2 makes whatever is left then.</small></span></label>`:""}
      <label class="opt"><input type="radio" name="sm" value="qty" ${mode==="qty"?"checked":""}><span><b>By quantity</b><small>Part 1 makes this many; part 2 makes the rest.</small><input type="number" id="sqty" min="1" max="${Math.max(1,Math.floor(total)-1)}" step="1" value="${sp&&sp.qty?Math.round(sp.qty):Math.round(total/2)}"></span></label>
    </fieldset>
    ${r2?`<div class="note">Now: part 1 ${r1.rest?"":fmtN(r1.qty)+" pcs"} ${fmtDT(r1.start)} → ${fmtDT(r1.end)}; part 2 ${r2.rest?"the rest once part 1 ends":fmtN(r2.qty)+" pcs"} on ${esc(machineById(r2.machineId)?.name||"")} ${fmtDT(r2.start)}${r2.rest?"":" → "+fmtDT(r2.end)}.</div>`:""}
    <div class="foot">${r2?`<button class="btn danger" id="smerge">Undo the split</button>`:""}<span style="flex:1"></span><button class="btn" id="dc">Cancel</button><button class="btn primary" id="sok">${r2?"Save":"Split"}</button></div>
  </div></div>`;
  const close=()=>closeDialog();$("#dx").onclick=close;$("#dc").onclick=close;$("#scrim").onclick=e=>{if(e.target.id==="scrim")close()};
  $$(".splitopts input[type=datetime-local],.splitopts input[type=number]").forEach(x=>x.addEventListener("focus",()=>{x.closest(".opt").querySelector("input[type=radio]").checked=true}));
  $("#sok").onclick=async()=>{const m=$(".splitopts input[name=sm]:checked").value;
    const body={key:k,mode:m};if(m==="time"){const v=$("#sat").value;if(!v){toast("Pick the time part 1 stops.");return}body.at=new Date(v).getTime()}
    if(m==="qty")body.qty=+$("#sqty").value;
    const r=await guard(api("/api/scheduler/job/split",body));if(!r.ok){toast(r.error||"Couldn't split.");return}
    close();await load();toast(r2?"Split changed.":"Split: part 2 is right after part 1. Drag it anywhere.")};
  if(r2)$("#smerge").onclick=async()=>{close();await guard(api("/api/scheduler/job/merge",{key:k}));await load();toast("Back to one run.")};
}

/* ---------- Labor Budget plan ---------- */
function ordinal(n){return n+(n===1?"st":n===2?"nd":n===3?"rd":"th")}
function fmtMoney(v,d=2){return "$"+Number(v||0).toLocaleString("en-US",{minimumFractionDigits:d,maximumFractionDigits:d})}
function laborSection(c){
  const L=S.labor||{lines:{}},lead=!!(S.me&&S.me.lead);
  const head=`<label class="check"><input type="checkbox" id="uselabor" ${c.useLabor!==false?"checked":""}> Use the Labor Budget plan: no runs on its holidays and days off, and runs on its extra work days. Each line runs the shifts ticked for it above; the plan's crews are shown for cost and staffing only</label>`;
  let body;
  if(c.useLabor===false)body=`<p class="small muted">Turned off: every line runs all the shifts ticked above, on the shifts' own days.</p>`;
  else if(!L.on)body=`<p class="small muted">No Labor Budget plan is saved yet. A shift lead can build one in <a href="/labor/">Labor Budget</a>; lines with the same name there are linked automatically.</p>`;
  else{
    const rows=c.machines.map(m=>{const lab=L.lines[m.id];
      if(!lab)return `<tr><td>${esc(m.name)}</td><td colspan="${c.shifts.length+1}" class="muted">Not in the labor plan (no line named "${esc(m.name)}"), so it runs all its shifts.</td></tr>`;
      return `<tr><td>${esc(m.name)}</td><td>${esc(lab.line)}</td>${c.shifts.map(sh=>{const x=lab.shifts[String(sh.id)];
        if(!x)return `<td class="muted">not this line's shift</td>`;
        return x.staffed?`<td>${ordinal(x.n)}: crew ${x.crew}${x.filled<x.crew?` <span class="muted">(${x.filled} named)</span>`:""}${lead&&x.costPerHour!=null?`<br><span class="muted small">${fmtMoney(x.costPerHour)}/h base wages</span>`:""}</td>`
          :`<td><span class="late">${ordinal(x.n)}: no crew</span><br><span class="muted small">still runs: add a crew in the Labor Budget</span></td>`}).join("")}</tr>`}).join("");
    body=`<div class="tablewrap"><table><thead><tr><th>Line</th><th>Labor plan line</th>${c.shifts.map((sh,i)=>`<th>${esc(sh.name)} <span class="muted small">(plan shift ${i+1})</span></th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>
      <p class="small muted" style="margin-top:8px">Shifts are matched to the plan's shift numbers by their order below (first shift = plan shift 1). The work calendar and crews update as soon as the Labor Budget is saved.${lead?" Crew wages on each run are straight-time base wages: no overtime, raises or benefits.":""}</p>`}
  return `<div class="page-head"><div><h3>Labor Budget plan</h3></div></div><div class="stack" style="gap:8px;margin-bottom:22px">${head}${body}</div>`;
}

/* ---------- lines & shifts page ---------- */
function weeklyMinutes(m){let t=0;for(const sh of S.cfg.shifts){if(!(m.shifts||[]).includes(sh.id))continue;t+=shiftWorkMin(sh,m)*(sh.days||[]).length}return t}
const thruLunch=(m,sh)=>!!(m&&(m.lunchShifts||[]).includes(sh.id));
const hasLunch=sh=>(sh.breaks||[]).some(b=>/lunch/i.test(b.label||""));
function shiftWorkMin(sh,m){const s0=hm(sh.start);let L=(hm(sh.end)-s0+1440)%1440||1440;let br=0;for(const b of sh.breaks||[])if(!(thruLunch(m,sh)&&/lunch/i.test(b.label||"")))br+=(hm(b.end)-hm(b.start)+1440)%1440;return Math.max(0,L-br)}
function renderLines(){
  const c=S.cfg;
  const rows=c.machines.map((m,i)=>{
    const wk=weeklyMinutes(m),diff=(S.labor&&S.labor.diff||{})[m.id]||[];
    return `<tr data-i="${i}">
      <td><input type="text" id="mn-${m.id}" data-f="name" value="${esc(m.name)}" aria-label="Line name">${diff.length?`<div class="budget-diff" title="The Labor Budget plans this line differently. This page is what the suite runs; change the budget to match if it should.">⚠ ${diff.map(esc).join("<br>")}</div>`:""}</td>
      <td><select data-f="stage" id="ms-${m.id}" aria-label="Stage">${STAGE_ORDER.map(s=>`<option value="${s}" ${m.stage===s?"selected":""}>${STAGES[s].name}</option>`).join("")}</select></td>
      <td><select data-f="mode" id="mm-${m.id}" aria-label="Speed basis">${(S.modes[m.stage]||[]).map(([v,l])=>`<option value="${v}" ${m.mode===v?"selected":""}>${l}</option>`).join("")}</select></td>
      <td class="n">${m.stage==="s1"?`<input type="number" min="1" step="1" data-f="ppm" id="mp-${m.id}" value="${m.ppm}" aria-label="Rated speed"> <span class="muted small">${({ppm:"PPM",mms:"mm/s",pcs:"parts/min"})[m.unit]||"PPM"}</span>`:`<input type="number" min="1" step="1" data-f="rate" id="mr-${m.id}" value="${m.rate}" aria-label="Filters per hour"> <span class="muted small">/hr</span>`}</td>
      <td class="n"><input type="number" min="0" max="90" step="0.1" data-f="scrap" id="msc-${m.id}" value="${m.scrap}" aria-label="Scrap percent"></td>
      <td class="n"><input type="number" min="0" max="90" step="0.1" data-f="downtime" id="mdt-${m.id}" value="${m.downtime}" aria-label="Downtime percent"></td>
      <td class="n"><input type="number" min="0" step="1" data-f="changeover" id="mco-${m.id}" value="${m.changeover}" aria-label="Changeover minutes"></td>
      <td><div class="shifts-cell">${c.shifts.map(sh=>{const on=(m.shifts||[]).includes(sh.id);return `<div class="shiftpick"><label><input type="checkbox" data-shift="${esc(sh.id)}" ${on?"checked":""}>${esc(sh.name)}</label>${on&&hasLunch(sh)?`<label class="thru" title="Relief cover keeps this line running during ${esc(sh.name)}'s lunch, so runs don't pause for it"><input type="checkbox" data-lunch="${esc(sh.id)}" ${thruLunch(m,sh)?"checked":""}>through lunch</label>`:""}</div>`}).join("")||`<span class="muted small">No shifts</span>`}</div></td>
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
  <div class="tablewrap"><table><thead><tr><th>Line</th><th>Stage</th><th>Speed basis</th><th class="n">Speed / fallback</th><th class="n">Scrap %</th><th class="n">Downtime %</th><th class="n">Changeover min</th><th title="Tick the shifts a line runs; on each, tick through lunch if relief cover keeps it running during that shift's lunch">Shifts · through lunch</th><th class="n">Hours / week</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
  <p class="small muted" style="margin:8px 0 22px">Sheet-based lines fall back to the speed shown when a part has no rate on the cost sheet. Scrap adds extra pieces to every run; downtime slows the effective speed.</p>
  <div class="stack" style="gap:6px;margin-bottom:22px"><label class="check"><input type="checkbox" id="cofirst" ${c.coFirst!==false?"checked":""}> Include a changeover before the first run on each line</label><label class="check"><input type="checkbox" id="cosame" ${c.coSamePart?"checked":""}> Apply changeover time even when the next run is the same part</label>
    <div class="row small" style="gap:8px;align-items:center;flex-wrap:wrap"><span>Inkjet-only changeover between filters with the same base part, for part numbers starting</span><input type="text" id="inkbases" value="${esc((c.inkjetBases||[]).join(", "))}" style="width:120px" aria-label="Base part prefixes" placeholder="AQ"><span>(e.g. AQ1278 → AQ1278C):</span><input type="number" id="inkmin" min="0" max="240" value="${c.inkjetMin||0}" style="width:70px" aria-label="Inkjet changeover minutes"><span>min</span></div></div>
  ${laborSection(c)}
  <div class="page-head"><div><h3>Shift schedule</h3><p class="sub">These shifts are shared with the Operator Kiosk. End Band, Arts &amp; Crafts and Glue Bead runs start as soon as Stage 1 is producing and their changeover is done. When a later stage runs faster than Stage 1, its start is held back just enough that it never runs out of parts. Breaks and lunches are removed from available run time; a run pauses through them and resumes after, except lunch on a shift a line runs <b>through lunch</b>.</p></div><button class="btn" id="adds">+ Add shift</button></div>
  <div class="shift-grid">${shifts}</div>`;
  if(S.labor&&S.labor.on)$("#main").querySelector(".page-head").insertAdjacentHTML("afterend",`<div class="card" style="border-left:4px solid var(--accent);margin-bottom:14px"><b>These are the lines the suite runs.</b> A line added in the <a href="/labor/">Labor Budget</a> starts here with the budget's shifts and lunch; after that this page is its own, and <span class="budget-diff" style="display:inline">⚠</span> marks where the budget plans a line differently.</div>`);
  const tb=$("tbody");
  tb.oninput=tb.onchange=e=>{const c=S.cfg;const tr=e.target.closest("tr");if(!tr)return;const m=c.machines[+tr.dataset.i];if(!m)return;
    if(e.target.dataset.f){const f=e.target.dataset.f;let v=e.target.type==="checkbox"?e.target.checked:e.target.value;if(["ppm","rate","scrap","downtime","changeover"].includes(f))v=Number(v)||0;m[f]=v;
      if(f==="stage"){m.mode=(S.modes[v]||[["fixed"]])[0][0];if(v!=="s1"&&!m.rate)m.rate=300;if(v==="s1"&&!m.ppm)m.ppm=180}
      saveCfg();if(e.type==="change"&&(f==="stage"||f==="mode"||f==="downtime"))renderLines()}
    if(e.target.dataset.lunch&&e.type==="change"){const id=isNaN(+e.target.dataset.lunch)?e.target.dataset.lunch:+e.target.dataset.lunch;m.lunchShifts=(m.lunchShifts||[]).filter(x=>x!==id);if(e.target.checked)m.lunchShifts.push(id);saveCfg();renderLines()}
    if(e.target.dataset.shift&&e.type==="change"){const id=isNaN(+e.target.dataset.shift)?e.target.dataset.shift:+e.target.dataset.shift;m.shifts=m.shifts||[];if(e.target.checked){if(!m.shifts.includes(id))m.shifts.push(id)}else{m.shifts=m.shifts.filter(x=>x!==id);m.lunchShifts=(m.lunchShifts||[]).filter(x=>x!==id)}saveCfg();renderLines()}};
  tb.onclick=e=>{const c=S.cfg;const b=e.target.closest("[data-del]");if(!b)return;const i=+b.dataset.del,m=c.machines[i];
    const n=Object.values(S.jobs).filter(j=>j.machineId===m.id).length;
    if(!b.dataset.sure){b.dataset.sure="1";b.textContent=n?`Remove + unschedule ${n}?`:"Click again to remove";return}
    if(typeof m.id==="number")REMOVED.machines.add(m.id);c.machines.splice(i,1);saveCfg();renderLines()};
  $("#addm").onclick=()=>{const c=S.cfg;c.machines.push({id:uid("new-"),name:`New line ${c.machines.length+1}`,stage:"s1",mode:"fixed",ppm:180,rate:0,scrap:5,downtime:10,changeover:30,shifts:c.shifts.map(s=>s.id).slice(0,1)});saveCfg();renderLines()};
  $("#uselabor").onchange=e=>{S.cfg.useLabor=e.target.checked;saveCfg()};
  $("#cosame").onchange=e=>{S.cfg.coSamePart=e.target.checked;saveCfg()};
  $("#inkbases").onchange=e=>{S.cfg.inkjetBases=e.target.value.split(/[,\s]+/).filter(Boolean);saveCfg()};$("#inkmin").onchange=e=>{S.cfg.inkjetMin=Math.max(0,Number(e.target.value)||0);saveCfg()};$("#cofirst").onchange=e=>{S.cfg.coFirst=e.target.checked;saveCfg()};
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
    const kids=STAGE_ORDER.filter(o=>stageMo(mo,o)&&stageMo(mo,o).mo!==mo.mo).map(o=>`<div class="small muted" title="${esc(STAGES[o].name)}">${STAGES[o].tag} ${esc(stageMo(mo,o).mo)} · ${esc(stageMo(mo,o).part)} · ${fmtN(stageQty(mo,o))}</div>`).join("");
    return `<tr data-id="${esc(mo.id)}"><td class="mono">${esc(mo.mo)}${kids}</td><td>${esc(mo.part)}${ms.length>1?`<br><select data-pick id="pk-${esc(mo.id)}" aria-label="Cost sheet match">${ms.map(it=>`<option value="${esc(it.uid)}" ${item&&item.uid===it.uid?"selected":""}>${esc(it.aq)} / ${esc(it.cust||"—")} · ${esc(it.cat)} · ${it.pleats} pl</option>`).join("")}</select>`:""}</td>
      <td>${item?`${esc(item.cat)} · ${item.pleats} pl`:`<span class="pill bad">Not on cost sheet</span> <input type="number" min="1" data-pleats id="pl-${esc(mo.id)}" value="${mo.pleats||""}" placeholder="pleats" style="width:72px" aria-label="Pleats">`}</td>
      <td class="n"><input type="number" min="1" data-qty id="qt-${esc(mo.id)}" value="${mo.qty}" aria-label="Quantity"></td>
      <td><input type="date" data-due id="du-${esc(mo.id)}" value="${mo.due?isoDay(mo.due):""}" aria-label="Due date"></td>
      <td><div class="opsel">${STAGE_ORDER.map(o=>`<button type="button" class="${o}" data-op="${o}" aria-pressed="${!!(mo.ops&&mo.ops[o])}" ${o==="s1"?"disabled":""} title="${STAGES[o].name}">${STAGES[o].tag}</button>`).join("")}</div></td>
      <td>${st}</td><td>${mo.produced?`<span class="muted small">${fmtN(mo.produced)} gross · ${fmtN(mo.net||0)} net · ${fmtN(mo.scrapped||0)} scrap</span>`:`<button class="btn sm danger" data-rm>Delete</button>`}</td></tr>`}).join("");
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
