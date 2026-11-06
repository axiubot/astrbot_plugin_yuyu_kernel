window.__inAstrBot = true;
const _probe = document.getElementById('jsprobe'); if (_probe) _probe.remove();

window.__yuyuStart = 1;
window.addEventListener("error", e => {
  const b = document.getElementById("banner");
  if (b){ b.style.display = "block"; b.textContent = "脚本错误：" + (e.message || e.type); }
});
window.addEventListener("unhandledrejection", e => {
  const b = document.getElementById("banner");
  if (b){ b.style.display = "block"; b.textContent = "异步错误：" + String(e.reason && e.reason.message || e.reason); }
});
const getBridge = () => window.__localApi || window.AstrBotPluginPage || null;
const $ = s => document.querySelector(s);
const esc = s => String(s==null?"":s).replace(/[<>&]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));
const fmt = ts => { if(!ts) return ""; const d=new Date(ts*1000);
  return `${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")} ${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}`; };
const withTimeout = (p, ms, tag) => Promise.race([
  Promise.resolve(p),
  new Promise((_, rej) => setTimeout(() => rej(new Error("超时：" + tag)), ms)),
]);
const toast = m => { const el=$("#toast"); el.textContent=m; el.classList.add("on"); setTimeout(()=>el.classList.remove("on"),2300); };
const NAMES = {overview:"总览",emotion:"情绪",memory:"记忆",chat:"对话",graph:"关系网",diary:"日记",
               promise:"承诺",people:"人物",style:"风格",system:"系统"};

let DATA=null, NOTE_KIND="", THEME="dark";
try { THEME = localStorage.getItem("yuyu.theme") || "dark"; } catch(e) { THEME = "dark"; }
document.documentElement.setAttribute("data-theme", THEME);
$("#theme").addEventListener("click", () => {
  THEME = THEME === "dark" ? "light" : "dark";
  try { localStorage.setItem("yuyu.theme", THEME); } catch(e) {}
  document.documentElement.setAttribute("data-theme", THEME);
  if (DATA) render(DATA);
});

function syncNav(){
  const hash = (location.hash || "#overview").slice(1);
  const key = hash === "overview" ? "overview" : hash;
  document.querySelectorAll(".navitem").forEach(a => {
    const h = (a.getAttribute("href") || "").slice(1);
    a.classList.toggle("on", h === hash);
  });
  $("#title").textContent = NAMES[key] || "总览";
  if (DATA && key === "graph") renderGraph(DATA.edges || []);
}
window.addEventListener("hashchange", syncNav);
syncNav();
window.__m1 = 1;
$("#refresh").addEventListener("click", () => { load(); toast("已刷新"); });

function C(){ return getComputedStyle(document.documentElement).getPropertyValue("--txt").trim() || "#e9edf6"; }
function DIM(){ return getComputedStyle(document.documentElement).getPropertyValue("--dim").trim() || "#8791a6"; }

function svgWrap(inner, w, h){
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="auto" xmlns="http://www.w3.org/2000/svg">${inner}</svg>`;
}
function drawRadar(dims){
  const box = document.getElementById("radar"); if(!box) return;
  const keys = Object.keys(dims || {}); if(!keys.length){ box.innerHTML = ""; return; }
  const n = keys.length, size = 420, cx = size/2, cy = size/2, R = size/2 - 40;
  const pt = (i, r) => [cx + Math.cos(-Math.PI/2 + i*2*Math.PI/n)*r, cy + Math.sin(-Math.PI/2 + i*2*Math.PI/n)*r];
  let g = "";
  for (const ring of [0.25,0.5,0.75,1]){
    const pts = keys.map((_,i)=>pt(i,R*ring).map(v=>v.toFixed(1)).join(",")).join(" ");
    g += `<polygon points="${pts}" fill="none" stroke="rgba(128,140,170,.25)"/>`;
  }
  keys.forEach((_,i)=>{ const [x,y]=pt(i,R); g += `<line x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" stroke="rgba(128,140,170,.18)"/>`; });
  const pts = keys.map((k,i)=>{ const v=Math.max(0,Math.min(100,Number(dims[k])))/100; return pt(i,R*v).map(x=>x.toFixed(1)).join(","); }).join(" ");
  g += `<polygon points="${pts}" fill="rgba(255,95,126,.35)" stroke="#ff5f7e" stroke-width="1.6"/>`;
  keys.forEach((k,i)=>{ const [x,y]=pt(i,R+20); g += `<text x="${x.toFixed(1)}" y="${(y+4).toFixed(1)}" fill="#8791a6" font-size="12" text-anchor="middle">${k}</text>`; });
  box.innerHTML = svgWrap(g, size, size);
}
function barsSvg(arr, labels, h, color){
  if(!arr || !arr.length) return svgWrap("", 440, h);
  const W=440, H=h, x0=14, y0=8, max=Math.max(...arr,1), bw=(W-x0*2)/arr.length*0.56;
  let g="";
  arr.forEach((v,i)=>{
    const bh=(H-34)*(v/max), x=x0+(W-x0*2)*(i+0.5)/arr.length-bw/2, y=y0+(H-34)-bh;
    g += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(bh,1).toFixed(1)}" rx="3" fill="${color}" opacity=".85"/>`;
    g += `<text x="${(x+bw/2).toFixed(1)}" y="${(y-4).toFixed(1)}" fill="#8791a6" font-size="10" text-anchor="middle">${v}</text>`;
    g += `<text x="${(x+bw/2).toFixed(1)}" y="${H-10}" fill="#8791a6" font-size="10" text-anchor="middle">${labels[i]||""}</text>`;
  });
  return svgWrap(g, W, H);
}
function drawWeek(arr){
  const box = document.getElementById("week"); if(!box) return;
  const labels=["6天前","5天前","4天前","3天前","2天前","昨天","今天"].slice(-(arr||[]).length);
  box.innerHTML = barsSvg(arr||[], labels, 150, "#ff5f7e");
}
function drawGrowth(arr){
  const box = document.getElementById("growth"); if(!box) return;
  const labels = (arr||[]).map((_,i)=> i===(arr.length-1) ? "本周" : `${arr.length-1-i}周前`);
  box.innerHTML = barsSvg(arr||[], labels, 170, "#7aa2ff");
}
function drawSpark(hist){
  const box = document.getElementById("spark"); if(!box) return;
  if(!hist || hist.length<2){ box.innerHTML = `<div class="empty">数据还不够，跑一会儿就有了</div>`; return; }
  const W=440,H=180,x0=12,y0=10;
  let g="";
  for(let i=0;i<=3;i++){ const y=y0+(H-30)*i/3; g+=`<line x1="${x0}" y1="${y}" x2="${W-x0}" y2="${y}" stroke="rgba(128,140,170,.18)"/>`; }
  [["joy","#3ddc97"],["stress","#ff5f7e"],["attach","#7aa2ff"]].forEach(([key,color])=>{
    const pts = hist.map((row,i)=>{
      const v = Math.max(0,Math.min(100, Number(((row.dims||{})[key]) ?? 50)));
      const x = x0 + (W-2*x0)*i/(hist.length-1), y = y0 + (H-30)*(1-v/100);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    g += `<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.6"/>`;
  });
  [["joy","#3ddc97"],["stress","#ff5f7e"],["attach","#7aa2ff"]].forEach(([key,color],i)=>{
    g += `<rect x="${x0+i*92}" y="${H-16}" width="8" height="8" fill="${color}"/><text x="${x0+i*92+12}" y="${H-8}" fill="#8791a6" font-size="11">${key}</text>`;
  });
  box.innerHTML = svgWrap(g, W, H);
}
function drawDonut(kinds){
  const box = document.getElementById("donut"); if(!box) return;
  const entries = Object.entries(kinds||{}).sort((a,b)=>b[1]-a[1]).slice(0,6);
  if(!entries.length){ box.innerHTML = `<div class="empty">还没有记忆</div>`; return; }
  const W=320,H=240,total=entries.reduce((s,[,v])=>s+v,0)||1,cx=W/2-56,cy=H/2,R=72,r=46;
  const colors=["#ff5f7e","#ffa26b","#7aa2ff","#3ddc97","#c084fc","#f472b6"];
  let g="", a=-Math.PI/2;
  entries.forEach(([k,v],i)=>{
    const ang=v/total*Math.PI*2, a2=a+ang, large=ang>Math.PI?1:0;
    const p=(rad,angle)=>[cx+rad*Math.cos(angle), cy+rad*Math.sin(angle)];
    const [x1,y1]=p(R,a), [x2,y2]=p(R,a2), [x3,y3]=p(r,a2), [x4,y4]=p(r,a);
    g += `<path d="M${x1.toFixed(1)},${y1.toFixed(1)} A${R},${R} 0 ${large} 1 ${x2.toFixed(1)},${y2.toFixed(1)} L${x3.toFixed(1)},${y3.toFixed(1)} A${r},${r} 0 ${large} 0 ${x4.toFixed(1)},${y4.toFixed(1)} Z" fill="${colors[i%6]}"/>`;
    a=a2;
  });
  entries.forEach(([k,v],i)=>{
    const y=34+i*24;
    g += `<rect x="${cx+R+18}" y="${y-8}" width="9" height="9" fill="${colors[i%6]}"/><text x="${cx+R+33}" y="${y}" fill="#8791a6" font-size="11">${k} ${v}</text>`;
  });
  box.innerHTML = svgWrap(g, W, H);
}
function renderGraph(edges){
  const box = document.getElementById("graphCv") || document.getElementById("graph");
  if(!box) return;
  if(!edges || !edges.length){ box.innerHTML = `<div class="empty">还没有关系数据（用 link_fact 教她几次就有了）</div>`; return; }
  const W=680,H=440,cx=W/2,cy=H/2,R=Math.min(W,H)/2-50;
  const nodes=[]; edges.forEach(([a,_,b])=>{ if(!nodes.includes(a)) nodes.push(a); if(!nodes.includes(b)) nodes.push(b); });
  const use=nodes.slice(0,26), pos={};
  use.forEach((n,i)=>{ const a=-Math.PI/2+i*2*Math.PI/use.length; pos[n]=[cx+Math.cos(a)*R, cy+Math.sin(a)*R]; });
  let g="";
  edges.forEach(([a,_,b])=>{ const p=pos[a], q=pos[b]; if(!p||!q) return;
    g += `<line x1="${p[0].toFixed(1)}" y1="${p[1].toFixed(1)}" x2="${q[0].toFixed(1)}" y2="${q[1].toFixed(1)}" stroke="rgba(255,95,126,.22)"/>`; });
  use.forEach(n=>{ const [x,y]=pos[n];
    g += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="5" fill="rgba(255,95,126,.85)"/>`;
    g += `<text x="${x.toFixed(1)}" y="${(y-10).toFixed(1)}" fill="#8791a6" font-size="11" text-anchor="middle">${String(n).slice(0,10)}</text>`; });
  box.innerHTML = svgWrap(g, W, H);
}
function renderChat(rows, filter){
  const f=(filter||"").trim();
  const list=f?rows.filter(r=>r.text.includes(f)):rows;
  $("#chat").innerHTML=list.length?list.map(r=>
    `<div class="it"><div class="t">${esc(r.role)}</div><div class="x">${esc(r.text)}</div></div>`).join("")
    :'<div class="empty">没有对话</div>';
}

function renderDiary(diaries,dreams){
  const block=(title,rows,color)=>rows.length
    ?`<div class="panel" style="margin-bottom:12px"><h3 style="color:${color}">${title}</h3>`+
     rows.map(d=>`<div class="it"><div class="t">${esc(d.date||"")}</div><div class="x">${esc(d.text)}</div></div>`).join("")+`</div>`:"";
  const html=block("日记",diaries,"#ffb3c2")+block("梦",dreams,"#9db4ff");
  $("#diaryList").innerHTML=html||'<div class="panel"><div class="empty">她还没写</div></div>';
}


let REVIEW_OFFSET = 0;
async function loadReview(append){
  const bridge=getBridge(); if(!bridge) return;
  try{
    const r=await bridge.apiPost("/astrbot_plugin_yuyu_inner/act",{do:"notes_list", offset: append?REVIEW_OFFSET:0});
    const d=(r&&r.data)?(r.data.data||r.data):r;
    const pkg=d.notes||{}; const items=pkg.items||[];
    $("#reviewInfo").textContent=`共 ${pkg.total||0} 条`;
    const html=items.map(x=>`
      <div class="it"><div class="t">#${x.idx} ${fmt(x.ts)}</div>
        <div class="x" contenteditable="true" data-edit="${x.idx}">${esc(x.text)}</div>
        <button class="mini" data-save="${x.idx}">存</button>
        <button class="mini ghost" data-pin="${x.idx}">${x.status==="open"?"取消":"置顶"}</button>
        <button class="mini ghost" data-del="${x.idx}">删</button></div>`).join("");
    $("#review").innerHTML = append ? ($("#review").innerHTML + html) : html;
    REVIEW_OFFSET = append ? REVIEW_OFFSET + items.length : items.length;
    bindReview();
  }catch(e){ toast("审核台加载失败"); }
}

function bindReview(){
  const bridge=getBridge();
  document.querySelectorAll("[data-save]").forEach(b=>b.addEventListener("click", async()=>{
    const idx=b.dataset.save;
    const el=document.querySelector(`[data-edit="${idx}"]`);
    const text=(el.textContent||"").trim(); if(!text) return toast("空内容");
    await bridge.apiPost("/astrbot_plugin_yuyu_inner/act",{do:"note_update", idx:Number(idx), text});
    toast("改好了"); loadReview(false);
  }));
  document.querySelectorAll("[data-pin]").forEach(b=>b.addEventListener("click", async()=>{
    await bridge.apiPost("/astrbot_plugin_yuyu_inner/act",{do:"note_pin", idx:Number(b.dataset.pin)});
    toast("好"); loadReview(false);
  }));
  document.querySelectorAll("[data-del]").forEach(b=>b.addEventListener("click", async()=>{
    if(!confirm("删掉这条记忆？")) return;
    await bridge.apiPost("/astrbot_plugin_yuyu_inner/act",{do:"note_delete", idx:Number(b.dataset.del)});
    toast("删掉了"); loadReview(false);
  }));
}

function renderNotes(){
  const list=(DATA&&DATA.recent_notes)||[];
  const filtered=list.filter(n=>{ if(!NOTE_KIND) return true; if(NOTE_KIND==="open") return n.status==="open"; return (n.kind||"note")===NOTE_KIND; });
  $("#notes").innerHTML=filtered.length?filtered.map(n=>
    `<div class="it"><div class="t">${fmt(n.ts)}</div><div class="x">${esc(n.text)}</div>${n.status==="open"?'<span class="chip">搁着</span>':''}</div>`).join("")
    :'<div class="empty">这里还没有东西</div>';
}

function render(d){
  DATA=d; const c=d.counts||{}, e=d.emotion||{};
  $("#now").textContent=`在线 · ${d.now||""}`;
  $("#sub").textContent=`更新于 ${d.now||""}`;
  const stMap={calm:"平静",hurt:"有点委屈",afraid:"有点怕",closed:"不想说话"};
  $("#state").textContent=stMap[e.state]||e.state||"?";

  const cards=[["记忆",c.notes,"条"],["疤",c.scars,"处"],["习惯",c.habits,"个"],["承诺",c.promises,"件"],
               ["人物",c.people,"人"],["世界书",c.worldbook,"张"],["关系网",c.graph,"点"]];
  $("#counts").innerHTML=cards.map(([k,v,u])=>
    `<div class="card"><div class="k">${k}</div><div class="v">${v??0}<span class="h" style="margin-left:3px">${u}</span></div></div>`).join("");

  const dims=e.dims||{}; drawRadar(dims); drawWeek(d.daily||[]); drawDonut(d.kinds||{});

  const sw=d.switches||{};
  $("#switches").innerHTML=[["主动说话","proactive"],["情绪核心","emotion"],["自动备份","autobackup"]]
    .map(([label,key])=>`<span class="chip ${sw[key]?"g":"d"}" data-sw="${key}" data-v="${sw[key]?1:0}">${label} ${sw[key]?"开":"关"}</span>`).join("");
  document.querySelectorAll("[data-sw]").forEach(el=>el.addEventListener("click",async()=>{
    const key=el.dataset.sw, next=el.dataset.v!=="1"; const bridge=getBridge();
    try{ await bridge.apiPost("/astrbot_plugin_yuyu_inner/act",{do:"toggle",key,value:next}); toast(`${key} → ${next?"开":"关"}`); load(); }
    catch(err){ toast("切换失败"); }
  }));

  const t=d.today||{};
  $("#today").innerHTML=`<span class="chip d">记了 ${t.notes??0} 条</span><span class="chip d">说了 ${t.talks??0} 句</span>`;

  $("#dims").innerHTML=Object.entries(dims).map(([k,v])=>
    `<div class="card"><div class="k">${k}</div><div class="v" style="font-size:20px">${v}</div>
     <div style="height:5px;background:rgba(128,140,170,.18);border-radius:3px;margin-top:8px;overflow:hidden">
     <div style="height:100%;width:${Math.max(0,Math.min(100,v))}%;background:linear-gradient(90deg,#ff5f7e,#ffa26b)"></div></div></div>`).join("");

  drawSpark(d.history||[]);
  drawGrowth(d.weekly||[]);
  $("#emoTags").innerHTML=`<span class="chip d">期待 ${e.expectation??0}</span><span class="chip d">责怪 ${Math.round(e.blame??0)}</span>`
    +((e.thoughts||[]).length?e.thoughts.map(x=>`<span class="chip">${esc(x)}</span>`).join(""):"");
  $("#thoughts").innerHTML=(e.thoughts||[]).length?e.thoughts.map(x=>`<span class="chip">${esc(x)}</span>`).join("")
    :'<span class="chip d">现在什么都不想</span>';

  const _sc = document.getElementById('selfcheck');
  if (_sc) _sc.textContent = "数据：记忆 " + ((d.recent_notes||[]).length) + " · 全部 " + ((d.notes_all||[]).length) + " · 日记 " + ((d.diaries||[]).length) + " · 梦 " + ((d.dreams||[]).length) + " · 对话 " + ((d.chat||[]).length) + " · 渲染 " + new Date().toLocaleTimeString();
  renderNotes(); renderChat(d.chat||[]); renderGraph(d.edges||[]); renderDiary(d.diaries||[], d.dreams||[]);

  const ps=d.promises||[];
  $("#promises").innerHTML=ps.length?ps.map(p=>{
    const left=Math.max(0,Math.round((p.due*1000-Date.now())/60000));
    return `<div class="it"><div class="t">${fmt(p.due)}</div><div class="x">跟${esc(p.to)}说：${esc(p.text)}</div><span class="chip g">${left} 分钟</span></div>`;
  }).join(""):'<div class="empty">没有待办</div>';

  const pp=d.people||[];
  $("#people").innerHTML=pp.length?pp.map(x=>
    `<div class="it"><div class="t">${esc(x.name)}</div><div class="x">${esc(x.bits)||"还没记细节"}</div></div>`).join("")
    :'<div class="empty">还没认识谁</div>';

  const stl=d.pending_style||[];
  $("#style").innerHTML=stl.length?stl.map((x,i)=>
    `<div class="it"><div class="x">${esc(x)}</div><button class="mini" data-ok="${i}">采纳</button><button class="mini ghost" data-no="${i}">不要</button></div>`).join("")
    :'<div class="empty">没有待批的观察</div>';
  document.querySelectorAll("[data-ok]").forEach(b=>b.addEventListener("click",()=>act("approve",Number(b.dataset.ok))));
  document.querySelectorAll("[data-no]").forEach(b=>b.addEventListener("click",()=>act("reject",Number(b.dataset.no))));

  const bk=d.backups||[];
  $("#backups").innerHTML=bk.length?bk.map(b=>
    `<div class="it"><div class="t">${fmt(b.ts)}</div><div class="x">${esc(b.name)} · ${b.kb} KB</div>
     <button class="mini ghost" data-restore="${esc(b.name)}">恢复</button></div>`).join("")
    :'<div class="empty">还没有备份</div>';
  document.querySelectorAll("[data-restore]").forEach(b=>b.addEventListener("click",()=>{
    if(confirm("用这个备份覆盖现有记忆？")) act("restore", 0, b.dataset.restore);
  }));

  $("#persona").textContent=d.persona||"（读不到人格）";
  $("#sys").textContent=JSON.stringify({counts:c,today:t,kinds:d.kinds,emotion:{state:e.state,dims}},null,2);
}

async function act(do_, index=0, name=""){
  const bridge=getBridge(); if(!bridge){ toast("页面桥没加载"); return; }
  try{
    const r=await bridge.apiPost("/astrbot_plugin_yuyu_inner/act",{do:do_,index,name});
    const d=(r&&r.data)?(r.data.data||r.data):r; toast(d.msg||"完成"); load();
  }catch(e){ toast("失败："+e.message); }
}

async function load(){
  window.__ls = 1;
  const banner=$("#banner");

  // 本机面板：shim 直接打 /api/status，跳过快照
  if (!window.__localPanel) {
  // ① 先读插件写的快照文件（不依赖页面桥）
  try{
    window.__snapshot = 1;
    let snapUrl = "";
    const resp = await fetch(snapUrl || "./data.json", {cache: "no-store"});
    if (resp.ok){
      const d = await resp.json();
      if (d && d.counts){
        banner.style.display = "none";
        window.__ls = "snapshot";
        render(d);
        window.__yuyuReady = 1;
        return;
      }
    }
  }catch(e){ /* 快照还没有，走桥 */ }
  }

  let bridge=getBridge();
  for(let i=0;i<20&&!bridge;i++){ await new Promise(r=>setTimeout(r,150)); bridge=getBridge(); }
  window.__ls = 2;
  if(!bridge){ banner.style.display="block"; banner.textContent="不在 AstrBot 页面里（页面桥没加载）"; return; }
  let raw=null,err=null;
  window.__ls="loop";
  for(const ep of ["/astrbot_plugin_yuyu_inner/status","status"]){
    try{ window.__ls="await:"+ep; const r=await withTimeout(bridge.apiGet(ep), 8000, ep); window.__ls="got:"+ep; if(r&&r.status==="error"){err=new Error(r.message||"接口错误");continue;} raw=r;break; }
    catch(e){ err=e; window.__ls="catch:"+String(e&&e.message); }
  }
  if(!raw){
    // 桥不走通（比如页面被单独打开）→ 直接同源请求兜底
    try{
      window.__ls = "fetch";
      const resp = await fetch("/api/plug/astrbot_plugin_yuyu_inner/status", {credentials: "include"});
      if (resp.ok) { raw = await resp.json(); }
      else { err = new Error("直接请求返回 HTTP " + resp.status); }
    }catch(e){ err = e; }
  }
  if(!raw){
    window.__fallbackFetch = 1;
    banner.style.display="block";
    const hint = (window.parent === window) ? "（这个页面像是被单独打开了，请从插件页的按钮进入）" : "";
    banner.textContent="读取失败："+((err&&err.message)||"接口没响应")+hint;
    return;
  }
  window.__ls = 3;
  banner.style.display="none";
  render((raw&&raw.data)?(raw.data.data||raw.data):raw);
  window.__ls = 4;
}

$("#btn-say").addEventListener("click", async () => {
  const el=$("#sayText"), text=el.value.trim(); if(!text) return;
  const bridge=getBridge(); if(!bridge){toast("页面桥没加载");return;}
  try{ await bridge.apiPost("/astrbot_plugin_yuyu_inner/act",{do:"say",text}); el.value=""; toast("发出去了"); load(); }
  catch(e){ toast("发送失败："+e.message); }
});
$("#sayText").addEventListener("keydown", e => { if(e.key==="Enter") $("#btn-say").click(); });
$("#btn-reach").addEventListener("click",()=>act("reach"));
$("#btn-backup").addEventListener("click",()=>act("backup"));
$("#btn-backup2").addEventListener("click",()=>act("backup"));
$("#btn-digest").addEventListener("click",()=>act("digest"));
$("#btn-tick").addEventListener("click",()=>act("tick"));
$("#btn-reset").addEventListener("click",()=>{ if(confirm("把情绪重置成平静？")) act("reset_emotion"); });
document.querySelectorAll("#segs span").forEach(el=>el.addEventListener("click",()=>{
  document.querySelectorAll("#segs span").forEach(x=>x.classList.remove("on"));
  el.classList.add("on"); NOTE_KIND=el.dataset.k; renderNotes();
}));
$("#btn-search").addEventListener("click", async () => {
  const kw=$("#search").value.trim(); if(!kw) return;
  const bridge=getBridge(); if(!bridge){toast("页面桥没加载");return;}
  try{
    const r=await bridge.apiPost("/astrbot_plugin_yuyu_inner/act",{do:"search",q:kw});
    const d=(r&&r.data)?(r.data.data||r.data):r;
    $("#notes").innerHTML=(d.hits||[]).length?d.hits.map(h=>`<div class="it"><div class="x">${esc(h)}</div></div>`).join("")
      :'<div class="empty">没搜到</div>';
  }catch(e){ toast("搜索失败："+e.message); }
});
$("#btn-chat-search").addEventListener("click",()=>renderChat(DATA?DATA.chat:[], $("#chatSearch").value));

$("#btn-tts").addEventListener("click", async () => {
  const text = ($("#sayText").value.trim() || "唔……御坂、御坂在这儿呢。");
  const bridge = getBridge(); if(!bridge){ toast("页面桥没加载"); return; }
  try{
    toast("合成中…");
    const r = await bridge.apiPost("/astrbot_plugin_yuyu_inner/act", {do:"tts", text});
    const d = (r && r.data) ? (r.data.data || r.data) : r;
    if (d.audio){
      $("#audioBox").innerHTML = `<audio controls autoplay src="${d.audio}" style="width:100%;margin-top:8px"></audio>`;
      toast(d.msg || "好了");
    } else { toast(d.msg || "没合成出来"); }
  }catch(e){ toast("失败：" + e.message); }
});
$("#btn-forget").addEventListener("click", async () => {
  const kw = $("#search").value.trim();
  if(!kw){ toast("先在搜索框填关键词"); return; }
  if(!confirm(`删掉所有包含「${kw}」的记忆？`)) return;
  const bridge = getBridge(); if(!bridge) return;
  try{
    const r = await bridge.apiPost("/astrbot_plugin_yuyu_inner/act", {do:"forget", q:kw});
    const d = (r && r.data) ? (r.data.data || r.data) : r;
    toast(d.msg || "完成"); load();
  }catch(e){ toast("失败：" + e.message); }
});

window.__m2 = 1;
window.__boot = async function(){
  try {
    const more = document.getElementById("btn-more");
    if (more) more.addEventListener("click", ()=>loadReview(true));
    window.__m3 = 1;
    await load();
    await loadReview(false);
    window.__yuyuReady = 1;
    setInterval(()=>{ load().catch(()=>{}); }, 20000);
  } catch (e) {
    window.__yuyuErr = String((e && e.stack) || e);
    const b = document.getElementById("banner");
    if (b) { b.style.display = "block"; b.textContent = "启动失败：" + String((e && e.message) || e); }
  }
};
window.__boot();
