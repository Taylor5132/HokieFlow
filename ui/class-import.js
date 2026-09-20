import {occurrences} from './schedule.js';
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function importMerge(existing,incoming){
 const ids=new Set(existing.map(e=>e.id));const added=incoming.filter(e=>!ids.has(e.id));
 const conflicts=[];
 if(added.length){
  const from=new Date(Math.min(...added.map(e=>+new Date(e.start))));
  const to=new Date(Math.max(...added.map(e=>+new Date(e.end))));
  const old=occurrences(existing,from,to);
  added.forEach((a,i)=>{[...old,...added.slice(0,i)].forEach(b=>{const bs=b.occurrenceStart||new Date(b.start),be=b.occurrenceEnd||new Date(b.end);if(new Date(a.start)<be&&new Date(a.end)>bs)conflicts.push(`${a.title} overlaps ${b.title}`);});});
 }
 return {added,events:[...existing,...added],conflicts:[...new Set(conflicts)]};
}
export function openClassImport({openSheet,closeSheet,request,getEvents,save,done}){
 openSheet('Add classes',`<div class="class-import"><p class="helper">Search the available VT timetable or import your calendar.</p><form id="class-search" class="class-form"><label>Term<select name="term"><option value="202609">Fall 2026</option></select></label><div class="class-search-fields"><label>Subject<input name="subject" placeholder="CS" maxlength="10"></label><label>Course number<input name="course_number" placeholder="2506" maxlength="10"></label><label>CRN<input name="crn" placeholder="Section number" pattern="[0-9]{3,5}"></label></div><button class="primary">Search classes</button></form><div id="class-results" aria-live="polite"></div><hr><label class="class-file">Import calendar (.ics)<input id="class-file" type="file" accept=".ics,text/calendar"></label><p class="helper">Your file is processed for this import and is not kept on the server.</p><div class="class-search-fields"><label>From<input id="import-start" type="date" value="${new Intl.DateTimeFormat('en-CA',{timeZone:'America/New_York',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date())}" required></label><label>Through<input id="import-end" type="date" value="2026-12-09" required></label></div><label class="term-consent"><input id="term-consent" type="checkbox">For timetable classes, use the weekly pattern through the published term, excluding known holidays.</label><p id="class-message" role="status"></p><div id="import-review"></div></div>`);
 const root=document.querySelector('.class-import'),message=root.querySelector('#class-message'),results=root.querySelector('#class-results'),review=root.querySelector('#import-review');
 let busy=false,preview=null;
 async function run(action){if(busy)return;busy=true;message.textContent='Loading…';root.querySelectorAll('button').forEach(b=>b.disabled=true);try{await action();}catch(e){message.textContent=e.message;}finally{busy=false;root.querySelectorAll('button').forEach(b=>b.disabled=false);}}
 function displayPreview(data){
  preview=data;const merged=importMerge(getEvents(),data.events||[]);const warnings=[...(data.errors||[]),...(data.warnings||[])];
  if(merged.conflicts.length)warnings.push(...merged.conflicts);
  message.textContent=warnings.join(' ');
  const dates=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  review.innerHTML=`<h3>${merged.added.length} meetings to add</h3><p class="helper">Exact dates in Eastern time. Existing imported meetings are skipped.</p>${merged.added.slice(0,8).map(e=>`<p><strong>${esc(e.title)}</strong><br>${esc(dates.format(new Date(e.start)))} · ${esc(e.location)}</p>`).join('')}${merged.added.length>8?`<p>And ${merged.added.length-8} more meetings.</p>`:''}${merged.added.length&&!(data.errors||[]).length?`<button type="button" class="primary" id="save-import">${merged.conflicts.length?'Save despite overlaps':'Add to my schedule'}</button>`:''}`;
  review.querySelector('#save-import')?.addEventListener('click',()=>run(async()=>{
   const latest=importMerge(getEvents(),preview.events||[]);
   if(latest.events.length>200)throw Error('Your schedule can hold 200 entries. Choose a shorter import range.');
   await save(latest.events);closeSheet();done();
  }));
 }
 async function prepare(extra){review.innerHTML='';preview=null;const data=await request('/api/classes/preview',{start:root.querySelector('#import-start').value,end:root.querySelector('#import-end').value,allow_term_assumption:root.querySelector('#term-consent').checked,...extra});displayPreview(data);}
 root.querySelector('#class-search').addEventListener('submit',e=>{e.preventDefault();void run(async()=>{
  review.innerHTML='';results.innerHTML='';const fields=Object.fromEntries(new FormData(e.target));fields.subject=fields.subject.trim().toUpperCase();
  const data=await request('/api/classes?'+new URLSearchParams(fields),undefined,'GET');
  message.textContent=(data.notices||[]).join(' ');
  results.innerHTML=`<p class="helper">${data.sections.length} results · available snapshots only</p>${(data.snapshots||[]).map(s=>`<p class="helper">${s.is_stale?'Older timetable':'Timetable'} · ${esc(new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}).format(new Date(s.fetched_at)))} ET · ${esc(s.query.subject||s.query.subj_code||'Selected classes')}</p>`).join('')}${data.sections.map(s=>`<article class="class-search-result"><strong>${esc(s.course)} · ${esc(s.title)}</strong><small>CRN ${esc(s.crn)}</small>${s.meetings.map(m=>`<p>${esc(m.days.map(d=>({M:'Mon',T:'Tue',W:'Wed',R:'Thu',F:'Fri',S:'Sat',U:'Sun'}[d]||d)).join(' / '))} ${esc(m.begin||'TBA')}–${esc(m.end||'TBA')}<br>${esc(s.building_names[m.building]||m.location_raw||'Location unavailable')} ${esc(m.room||'')}</p>`).join('')}${s.flags.has_unknown_building?'<p class="helper">Building name lookup unavailable.</p>':''}<button class="text-button" type="button" data-crn="${esc(s.crn)}" data-term="${esc(s.term)}">Review class +</button></article>`).join('')}`;
 });});
 results.addEventListener('click',e=>{const b=e.target.closest('[data-crn]');if(b)void run(()=>prepare({term:b.dataset.term,crn:b.dataset.crn}));});
 root.querySelector('#class-file').addEventListener('change',e=>void run(async()=>{const file=e.target.files[0];if(!file)return;if(file.size>512*1024)throw Error('Choose an ICS file smaller than 512 KB.');await prepare({ics:await file.text()});}));
 ['import-start','import-end','term-consent'].forEach(id=>root.querySelector('#'+id).addEventListener('change',()=>{review.innerHTML='';preview=null;message.textContent='Review the class or select your calendar file again to apply these changes.';root.querySelector('#class-file').value='';}));
}
