import {openClassImport} from './class-import.js';
import {mountScrollMotion} from './motion.js';
import {loadPreferences,savePreferences} from './preferences.js';
import {trackBusLocation,locationError} from './bus-location.js';
import {CAMPUS,campusNowMs,closestStops,departureRows,fetchWeather} from './home-live.js';
import {dateKey,occurrences,upcoming,localInput} from './schedule.js';
import {nearbyDining,distanceFeet} from './dining.js';
import {mountDirections,placeDirectionsURL} from './directions.js';
import { config } from './config.js';
import { previewTime, previewStates, sampleVehicle } from './fixtures.js';
import { planHeading, allergenLabel, numberOrDash, sourceForLeg } from './model.js';

const $ = selector => document.querySelector(selector);
const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const paths = {
 user:'M20 21v-2a7 7 0 0 0-14 0v2M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z', home:'M3 10 12 3l9 7v11h-6v-8H9v8H3Z', plan:'M8 3v4m8-4v4M3 10h18M5 5h14a2 2 0 0 1 2 2v13H3V7a2 2 0 0 1 2-2Z',
 events:'M4 5h16v5a2 2 0 0 0 0 4v5H4v-5a2 2 0 0 0 0-4V5Zm11 0v2m0 4v2m0 4v2',
 dining:'M5 3v7m3-7v7m-6-7v5a3 3 0 0 0 6 0m-3 3v10m12-18c-5 5-5 10 0 10V3Zm0 10v8',
 bus:'M5 17v4m14-4v4M4 10h16M6 14h1m10 0h1M6 3h12a2 2 0 0 1 2 2v12H4V5a2 2 0 0 1 2-2Z', more:'M4 12h.01M12 12h.01M20 12h.01', arrow:'M4 12h16m-6-6 6 6-6 6',send:'M12 19V5m-6 6 6-6 6 6',close:'m6 6 12 12M6 18 18 6', pin:'M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 1 1 16 0Zm-5 0a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z',check:'m5 12 4 4L19 6', clock:'M12 7v5l3 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z'
};
const icon = name => `<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.plan}"/></svg>`;
let preferenceStorage;try{preferenceStorage=window.localStorage;}catch{}
const preferences=loadPreferences(preferenceStorage);document.documentElement.dataset.theme=preferences.theme;
let campusEvents=[],campusEventState='idle',campusEventNotices=[],campusEventError='',addingCampusEvent=null;
const state = {tab:'home',answer:null,clock:null,clockFailed:false,busy:false,error:'',query:'',review:'fits',status:null, savedClass:null, homeData:null, homeError:'',user:null,accountReady:false,accountError:'',accountStorageError:'',accountData:{savedClass:null,reduceMotion:false,plans:[],events:[]},accountVersion:0,accountSaving:false};
let googleAuthErrorMessage = (() => {
  // Supabase reports OAuth failures by sending the browser to the Site URL with
  // error / error_code / error_description in the query (or the fragment),
  // never to our callback. Read both, or the failure is invisible.
  const params = new URLSearchParams(window.location.search);
  const hash = new URLSearchParams((window.location.hash || '').replace(/^#/, ''));
  const message = [params.get('auth_error'),
                   params.get('error_description') || params.get('error'),
                   hash.get('error_description') || hash.get('error')]
                  .filter(Boolean).join(' — ');
  if (!message) return '';
  const url = new URL(window.location.href);
  for (const key of ['auth_error','error','error_code','error_description']) url.searchParams.delete(key);
  url.hash = '';
  history.replaceState({}, '', url.pathname + url.search);
  return message;
})();
let calendarMonth=new Date(new Date().getFullYear(),new Date().getMonth(),1),selectedDay=dateKey(new Date());
let diningLocationFailed=false;
let diningPlaces=[],diningLocation=null,diningLoading=false,diningError='',diningLoaded=false;
let homeWeather=null,weatherError=false,transitStops=[],transitDepartures=[],transitLocation=null,transitBusy=false,transitError='',transitUpdated=0,transitReplay=false;
let stopBusLocation=null,busLocating=false,busLocationError='',busRefreshQueued=false,busLocationVersion=0;
let directionsPanel=null;let destinationHint='';const routeSelection={origin:'current',destination:'',mode:'walk'};
let lastFocus, syncMoment=performance.now();
const preview = config.mode === 'preview';
const time = value => { if (!value || !Number.isFinite(Date.parse(value))) return '—'; return new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour:'numeric',minute:'2-digit'}).format(new Date(value)); };
const badge = (text, kind='') => `<span class="badge ${kind}">${escape(text)}</span>`;
function campusMs(){const m=campusNowMs(state.clock,{syncMoment,performanceNow:performance.now(),failed:state.clockFailed});return m===null?Date.now():m;}
function clockLabel() {
 if (!state.clock) return '';
 const c=state.clock;
 const base = c.is_replay || c.pinned ? `Ⅱ Pinned ${c.human || time(c.iso)} · replay` : `Campus ${c.human || time(c.iso)}`;
 return state.clockFailed ? `${base} · sync unavailable` : base;
}
function greeting(){const hour=Number(new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour:'numeric',hourCycle:'h23'}).format(new Date()));return hour>=5&&hour<12?'Good morning':hour>=12&&hour<17?'Good afternoon':'Good evening';}
function heading(eyebrow,title,accent) { return `<header class="page-heading">${eyebrow?`<p class="eyebrow">${eyebrow}</p>`:''}<h1>${title}${accent?`<br><span>${accent}</span>`:''}</h1></header>`; }
function sourceNote(){return '';}
function planner(){return `<form id="ask-form" class="footer-composer"><label class="sr-only" for="request">Ask HokieFlow</label><textarea id="request" rows="1" maxlength="1000" placeholder="Ask HokieFlow…" ${state.busy?'disabled':''}>${escape(state.query)}</textarea><button type="submit" aria-label="${state.busy?'Planning your request':'Send message'}" ${state.busy?'disabled':''}>${icon('send')}</button></form>`;}
function classInfo(){return state.savedClass||state.homeData?.nextClass||(preview?{code:'CS 2506',title:'Comp Org',building:'McBryde Hall',room:'100',time:'10:10 AM',sample:true}:null);}
function classCard(){const c=classInfo();return `<section class="home-card class-card" aria-labelledby="class-heading"><div class="section-title"><h2 class="eyebrow" id="class-heading">${c?.sample?'Class directions':'Your class'}</h2>${badge(c?.sample?'Sample class':state.savedClass?'Saved':c?'Today':'Make it yours')}</div>${c?`<div class="class-summary"><div><h3>${escape(c.code)}</h3><p>${escape(c.title||'')} · ${escape(c.building)} ${escape(c.room||'')}</p></div><span class="class-time">${escape(c.time||'Time not set')}</span></div><button class="primary" data-action="class-route">Get directions ${icon('arrow')}</button>`:'<h3>Add your next class.</h3><p>Save a classroom to get walking directions.</p>'}<button class="text-button" data-action="edit-class">${c?'Change class':'Add a class'} ${icon('plan')}</button></section>`;}
function busCard(){return `<section class="home-card bus-card reveal" aria-labelledby="bus-heading"><div class="section-title"><h2 class="eyebrow" id="bus-heading">Campus routes</h2></div><div id="campus-directions"></div></section>`;}
function oldHomeDining(){const food=state.answer?.itinerary?.legs?.find(leg=>leg.type==='eat');return `<section class="home-card dining-home reveal" aria-labelledby="dining-heading"><div class="section-title"><h2 class="eyebrow" id="dining-heading">Dining</h2>${badge('D2')}</div><h3>A little room for lunch.</h3><p>D2 at Dietrick Hall</p>${food?`<div class="meal-summary"><strong>${escape(food.item)}</strong><span>${numberOrDash(food.kcal)} kcal · ${numberOrDash(food.protein_g)} g protein ${preview?'· illustrative':''}</span><p class="allergen unknown">${escape(allergenLabel(food))}</p></div>`:'<div class="dining-facts"><span>Menu & nutrition <b>Choose a meal plan</b></span><span>Your preferences <b>Include them in your request</b></span></div>'}<button class="primary" data-action="meal">Find a meal for my gap ${icon('arrow')}</button><button class="text-button" data-action="dining-route">Directions to D2 ${icon('pin')}</button></section>`;}
function classEditor(){if(!state.user){authScreen('login');return;}const c=classInfo()||{};openSheet('Your next classroom.',`<p>Your next stop, saved to your account.</p><form id="class-form" class="class-form"><label>Course code<input name="code" required maxlength="30" value="${escape(c.code||'')}" placeholder="CS 2506"></label><label>Course name<input name="title" maxlength="80" value="${escape(c.title||'')}"></label><label>Building<input name="building" required maxlength="100" value="${escape(c.building||'')}" placeholder="McBryde Hall"></label><div class="field-pair"><label>Room<input name="room" maxlength="20" value="${escape(c.room||'')}"></label><label>Class time<input name="time" maxlength="30" value="${escape(c.time||'')}" placeholder="10:10 AM"></label></div><button class="primary">Save class ${icon('check')}</button></form>`);}
function planResult(){
 const a=state.answer;
 if(state.busy)return `<section class="answer loading" aria-busy="true"><span></span><span></span><p>Checking dining, transit, and the latest vehicle snapshot…</p></section>`;
 if(!a)return `<section class="empty reveal"><span class="empty-icon">${icon('plan')}</span><h2>A gap in your day?</h2><p>Start with where you are and when you need to arrive. We’ll keep the tradeoffs in view.</p></section>`;
 if(typeof a.answer==='string')return `<section class="answer reveal"><h2 class="eyebrow">HokieFlow</h2><p class="chat-answer">${escape(a.answer)}</p>${Array.isArray(a.sources)&&a.sources.length?`<details class="answer-sources"><summary>Sources</summary>${a.sources.map(source=>`<p>${escape(source.title||source.file_id||'Campus source')}${source.updated_at?' · updated '+escape(source.updated_at):''}${source.page?' · page '+escape(source.page):''}</p>`).join('')}</details>`:''}</section>`;
 const i=a.itinerary, r=a.infeasible_reason;
 const failure=r?.code==='deadline_missed'?`The closest option arrives about ${numberOrDash(r.late_by_min)} min late. Try a later deadline or skip the meal.`:r?.code==='unknown_place'?`Unknown place: ${r.value}. Choose one of: ${(r.known_places||[]).join(', ')}.`:r?.code==='invalid_window'?'Check your start and deadline; the end must be later than the start.':r?.code==='no_legs'?'No usable route could be built. Choose another origin or destination.':'';
 const valid=a.feasible===true && i?.arrives_in_window!==false && i?.legs?.length;
 return `<section class="answer reveal ${a.feasible===false?'needs-detail':''}" aria-labelledby="answer-heading"><div class="answer-top"><h2 class="eyebrow">${a.replan_trigger?'Re-planned':'Your next step'}</h2>${badge(preview?'Illustrative replay':a._time?.is_replay?'Replay':'Evaluated')}</div><h3 id="answer-heading">${escape(planHeading(a))}</h3>${a.clarification?`<p class="clarification">${escape(a.clarification.question)}</p><p>${escape(a.clarification.detail)}</p><button class="text-button" data-action="edit">Update my request ${icon('arrow')}</button>`:`<p>${escape(failure || a.rationale || 'Review the source details below.')}</p>`}
 ${a._time?.evaluated_at?`<p class="helper">Evaluated at ${escape(time(a._time.evaluated_at))} · ${a._time.is_replay?'pinned replay':'server request time'}</p>`:''}
 ${a._interpretation_notes?.map(note=>`<p class="interpretation">↳ ${escape(note)}</p>`).join('')||''}
 ${a.replan_trigger?`<div class="notice"><strong>What changed</strong><p>${escape(a.replan_trigger.detail)}</p><button class="text-button" data-action="previous">View previous plan ${icon('arrow')}</button></div>`:''}
 ${(a.notes||[]).map(note=>`<p class="notice">${escape(note)}</p>`).join('')}
 ${i?.legs?.length?`<button class="text-button" data-action="save-plan" ${state.accountSaving?'disabled':''}>Save this plan ${icon('check')}</button><div class="plan-metrics"><div><span>LEAVE</span><strong>${escape(time(i.leave_time))}</strong></div><div><span>ARRIVE · EST.</span><strong>${escape(time(i.arrive_time))}</strong></div><div><span>${valid?'SPARE · EST.':'TIMING'}</span><strong>${valid?`${numberOrDash(i.slack_min)}m`:'Check deadline'}</strong></div></div><h4 class="eyebrow itinerary-label">${valid?'Your itinerary':'Closest option · does not meet your request'}</h4>${legs(i.legs)}<button class="primary" data-action="directions">Open navigation options ${icon('arrow')}</button>`:''}</section>`;
}
function legs(items){return `<div class="itinerary">${items.map((leg,index)=>`<button class="leg-row" data-leg="${index}"><time>${escape(time(leg.start_time||leg.dep_time))}</time><span class="timeline-dot ${leg.type==='eat'?'orange':''}"></span><span class="leg-copy"><strong>${escape(leg.type==='eat'?'Eat at D2':leg.type==='bus'?`Bus · ${leg.route_id}`:`Walk to ${leg.to}`)}</strong><small>${escape(leg.type==='eat'?leg.item:leg.type==='bus'?`${leg.from_stop_name} → ${leg.to_stop_name}`:leg.from)}</small><span class="leg-source">${escape((preview?'Illustrative · ':'')+sourceForLeg(leg))}</span></span><span class="leg-duration">${escape(numberOrDash(leg.minutes))}<small>min${leg.type==='bus'?' sched.':' est.'}</small></span></button>`).join('')}</div>`;}
function busContent(){return '';}
function diningContent(){const food=state.answer?.itinerary?.legs?.find(leg=>leg.type==='eat');return food?`<section class="food-card reveal"><p class="eyebrow">D2 at Dietrick Hall</p><h2>${escape(food.item)}</h2><p>${escape(food.portion||'Portion unavailable')}</p><div class="nutrition"><div><strong>${numberOrDash(food.kcal)}</strong><span>kcal · ${preview?'illustrative':'VT menu API'}</span></div><div><strong>${numberOrDash(food.protein_g)}</strong><span>g protein · ${preview?'illustrative':'VT menu API'}</span></div></div><div class="allergen ${food.allergens?.length?'listed':'unknown'}"><strong>${escape(allergenLabel(food))}</strong><p>Blank allergen information does not mean an item is safe.</p></div>${food.avoid?.length?`<p>Hard filter excludes: ${escape(food.avoid.join(', '))}</p>`:''}<p class="helper">${escape(food.method||'Menu source')} · coverage limited to D2.</p></section>`:`<section class="empty"><span class="empty-icon">${icon('dining')}</span><h2>Food that fits your gap.</h2><p>Make a plan to see its D2 meal, nutrition, and declared allergens. </p><button class="primary" data-tab="home">Plan a meal ${icon('arrow')}</button></section>`;}
function moreContent(){return `${heading('','Settings','')}<section class="settings" aria-label="App settings"><div class="appearance-setting"><div><strong>Appearance</strong><small>Pick your favorite look.</small></div><div class="theme-options" role="group" aria-label="Color theme"><button data-theme="light" aria-pressed="${preferences.theme==='light'}">Light</button><button data-theme="dark" aria-pressed="${preferences.theme==='dark'}">Dark</button></div></div><button data-tab="about"><span><strong>About HokieFlow</strong><small>A little help getting around campus.</small></span>${icon('arrow')}</button></section><section class="settings-account"><h2 class="eyebrow">Account</h2>${accountPanel()}</section>`;}
function aboutContent(){return `${heading('','Meet HokieFlow.','')}<article class="about-copy"><p class="about-intro">A little campus buddy in your pocket.</p><p>College is a big place. HokieFlow helps you figure out what’s next and how to get there.</p><div class="about-feature"><span>01</span><div><h2>Know what’s next.</h2><p>Add your classes, lunch dates, and study plans. See your next three things right on Home.</p></div></div><div class="about-feature"><span>02</span><div><h2>Get there without the guesswork.</h2><p>Check bus times or choose a building. Open Apple Maps and let it show you the way.</p></div></div><div class="about-feature"><span>03</span><div><h2>Find your next bite.</h2><p>See dining spots nearby and how far away they are. Less searching, more snacking.</p></div></div><p>One place for the little things that make a busy day easier.</p><details class="data-credits"><summary>Data credits</summary><p>Weather by <a href="https://open-meteo.com/" target="_blank" rel="noreferrer">Open-Meteo</a>. Bus times by Blacksburg Transit. Dining locations from Virginia Tech. Directions open in Apple Maps.</p></details><section class="team-credits"><h2 class="eyebrow">Made by</h2><ul><li>Muhammad Bilal</li><li>Taehyun Yoon</li><li>Lilly Terziyska</li><li>Joshitha Nodagala</li></ul></section></article>`;}

function render(){
 const page=state.tab==='events'?campusEventsPage():state.tab==='login'?`${heading('',state.authMode==='register'?'Create account':'Log in','')}<section class="login-page">${authBody(state.authMode||'login')}</section>`:state.tab==='about'?aboutContent():state.tab==='home'?`<header class="home-header"><div><p class="home-date"><span>${escape(new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',weekday:'long'}).format(new Date()))}</span><span>${escape(new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',month:'long',day:'numeric'}).format(new Date()))}</span></p><h1>${greeting()}<br><span>Hokie<b>.</b></span></h1></div><div id="home-weather" class="home-weather">${weatherMarkup()}</div></header>${state.homeError?`<p class="notice">${escape(state.homeError)}</p>`:''}${googleAuthErrorMessage?`<p class="notice" role="alert">${escape(googleAuthErrorMessage)}</p>`:''}${upcomingCard()}<div id="home-transit">${nearbyBusCard()}</div>${homeDining()}${state.answer||state.busy?planResult():''}`:state.tab==='plan'?schedulePage():state.tab==='dining'?`${heading('','Dining','')}${diningDirectory()}`:state.tab==='bus'?`${heading('','Bus','')}${busCard()}${busContent()}<a class="text-button bt-service-link" href="https://ridebt.org/routes-schedules" target="_blank" rel="noreferrer">BT schedules & service ${icon('arrow')}</a>`:moreContent();
 directionsPanel?.destroy();directionsPanel=null;
 $('#main').innerHTML=`<div class="page-enter">${state.tab==='login'?'':accountBar()}${state.error?`<p role="alert" class="error">${escape(state.error)}</p>`:''}${page}${sourceNote()}${state.tab==='home'?'<p class="copyright">HokieFlow</p>':''}</div>`;
 $('#chat-footer').innerHTML=planner();
 updateFooterSpace();
 const routeElement=$('#campus-directions');
 if(routeElement)directionsPanel=mountDirections(routeElement,{selection:routeSelection,destinationHint:destinationHint||classInfo()?.building||''});
 $('#navigation').innerHTML=[['home','Home'],['plan','Schedule'],['events','Events'],['dining','Dining'],['bus','Bus'],['more','More']].map(([id,label])=>`<button data-tab="${id}" ${state.tab===id?'aria-current="page"':''}>${icon(id)}<span>${label}</span></button>`).join('');
}
function announce(text){$('#announcer').textContent=text;}
function navigate(tab){state.tab=tab;render();if(tab==='events'&&campusEventState==='idle')void loadCampusEvents();if(tab==='dining'&&!diningLoaded&&!diningLoading)void loadDining();window.scrollTo({top:0,behavior:'instant'});$('#main').focus({preventScroll:true});}
async function getJSON(path,options={}){const response=await fetch(`${config.apiBase}${path}`,{...options,signal:AbortSignal.timeout(path.startsWith('/api/transit/')?18000:70000)});const body=await response.json();if(!response.ok && !body.clarification && body.feasible!==false)throw new Error('The planner could not respond. Please try again.');return body;}
async function ask(){if(state.busy)return;state.query=$('#request')?.value||state.query;if(!state.query.trim()){ $('#request')?.focus();return;}state.tab='home';state.busy=true;state.error='';render();announce('Checking dining, transit, and the latest vehicle snapshot.');try{if(preview){await new Promise(resolve=>setTimeout(resolve,500));state.answer=structuredClone(previewStates[state.review]);}else state.answer=await getJSON('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:state.query,schedule:events()})});}catch(error){state.error=error.message;state.answer=null;}finally{state.busy=false;render();announce(state.error||(state.answer?.answer?'Answer ready.':planHeading(state.answer)));$('.answer')?.scrollIntoView({behavior:reduced()?'instant':'smooth',block:'start'});}}
const reduced=()=>matchMedia('(prefers-reduced-motion: reduce)').matches||document.body.classList.contains('reduce-motion');
function openSheet(title,body){lastFocus=document.activeElement;$('#detail-content').innerHTML=`<p class="eyebrow">HokieFlow · details</p><h2 id="detail-title">${escape(title)}</h2>${body}`;$('#detail').showModal();document.body.classList.add('sheet-open');}
function closeSheet(){$('#detail').close();document.body.classList.remove('sheet-open');lastFocus?.focus();}
document.addEventListener('submit',event=>{
 if(event.target.id==='ask-form'){event.preventDefault();void ask();}
 if(event.target.id==='class-form'){event.preventDefault();const fields=new FormData(event.target);const course=Object.fromEntries(['code','title','building','room','time'].map(key=>[key,String(fields.get(key)||'').trim()]));if(!course.code||!course.building)return;void saveAccount({...state.accountData,savedClass:course}).then(()=>{closeSheet();render();announce('Class saved to your account.');$('[data-action="edit-class"]')?.focus();}).catch(showAccountError);}
 if(event.target.id==='event-form'){event.preventDefault();void submitEvent(event.target);}
 if(event.target.id==='auth-form'){event.preventDefault();void submitAuth(event.target);}

});
document.addEventListener('input',event=>{if(event.target.id==='request')state.query=event.target.value;});
document.addEventListener('change',event=>{if(event.target.id==='review-state')state.review=event.target.value;});
document.addEventListener('click',event=>{
 const el=event.target.closest('button,a');if(!el)return;
 if(el.dataset.tab){navigate(el.dataset.tab);return;}
 if(el.dataset.prompt){state.query=el.dataset.prompt;$('#request').value=state.query;$('#request').focus();return;}
 if(el.dataset.leg!==undefined){const leg=state.answer?.itinerary?.legs?.[Number(el.dataset.leg)];if(leg)openSheet(leg.type==='eat'?leg.item:leg.type==='bus'?'Scheduled bus leg':`Walk to ${leg.to}`,`<p>${escape((preview?'Illustrative · ':'')+sourceForLeg(leg))}</p><p>${numberOrDash(leg.minutes)} minutes · ${escape(leg.method||'Method unavailable')}</p>${leg.type==='eat'?`<div class="allergen unknown">${escape(allergenLabel(leg))}</div><p>${numberOrDash(leg.kcal)} kcal · ${numberOrDash(leg.protein_g)} g protein</p>`:'<p>Check the route details before setting off.</p>'}`);return;}
 if(el.dataset.campusEvent){void addCampusEvent(el.dataset.campusEvent);return;}
 const action=el.dataset.action;
 if(action==='refresh-events'){if(state.accountStorageError)void loadAccount();void loadCampusEvents();return;}
 if(el.dataset.theme){preferences.theme=el.dataset.theme;document.documentElement.dataset.theme=preferences.theme;savePreferences(preferenceStorage,preferences);render();$(`.theme-options [data-theme="${preferences.theme}"]`)?.focus();return;}
 if(action==='nearby-buses')locateBuses();
 if(action==='refresh-buses')void loadBuses();
 if(action==='import-classes'){if(!state.user){authScreen('login');return;}openClassImport({openSheet,closeSheet,request:accountRequest,getEvents:events,save:items=>saveAccount({...state.accountData,events:items}),done:()=>{render();announce('Classes added to your schedule.');}});}
 if(action==='add-event')eventEditor();
 if(el.dataset.editEvent)eventEditor(el.dataset.editEvent);
 if(el.dataset.deleteEvent)void deleteEvent(el.dataset.deleteEvent);
 if(el.dataset.calendarMove){calendarMonth.setMonth(calendarMonth.getMonth()+Number(el.dataset.calendarMove));render();}
 if(el.dataset.day){selectedDay=el.dataset.day;render();}
 if(action==='calendar-today'){calendarMonth=new Date(new Date().getFullYear(),new Date().getMonth(),1);selectedDay=dateKey(new Date());render();}
 if(action==='nearby-dining')locateDining();
 if(action==='all-dining'){diningLocation=null;render();}
 if(action==='retry-dining'){if(diningLocationFailed)locateDining();else void loadDining();}
 if(el.dataset.diningPlace){window.open(placeDirectionsURL(el.dataset.diningPlace),'_blank','noopener,noreferrer');}
 
 if(action==='class-route'||action==='dining-route'){destinationHint=action==='class-route'?(classInfo()?.building||''):'Dietrick';if(state.tab!=='bus')navigate('bus');directionsPanel?.setDestinationHint(destinationHint);$('#campus-directions')?.scrollIntoView({behavior:reduced()?'instant':'smooth',block:'start'});}

 if(action==='edit-class')classEditor();
 if(action==='meal'){state.query='I want to eat at D2 and get to '+(classInfo()?.building||'McBryde')+'. My deadline is ';$('#request').value=state.query;$('#request').focus();}

 if(action==='edit'){navigate('home');$('#request').focus();}
 if(action==='review'){state.answer=structuredClone(previewStates[state.review]);navigate('plan');announce(planHeading(state.answer));}
 if(action==='google-login')startGoogleLogin();
 if(action==='login')authScreen('login');
 if(action==='signup')authScreen('register');
 if(action==='logout')void logout();
 if(action==='save-plan')void saveCurrentPlan();
 if(el.dataset.savedPlan){const plan=state.accountData.plans.find(p=>p.id===el.dataset.savedPlan);if(plan){state.answer=plan.answer;state.query=plan.query;navigate('home');}}
 if(el.dataset.removePlan){void saveAccount({...state.accountData,plans:state.accountData.plans.filter(p=>p.id!==el.dataset.removePlan)}).then(()=>{render();announce('Saved plan removed.');}).catch(showAccountError);}
 if(action==='vehicle')openSheet('Example vehicle',`<p>Single illustrative observation at ${time(sampleVehicle.observed_at)} · pinned replay.</p><div class="detail-line"><span>Observed load</span><strong>30%</strong></div><div class="detail-line"><span>Schedule deviation</span><strong>1.4 min early</strong></div><p>This is not an arrival ETA. No interpolation or movement is shown.</p>`);
 if(action==='directions'){destinationHint=state.answer?.constraints?.to_place||classInfo()?.building||'';navigate('bus');directionsPanel?.setDestinationHint(destinationHint);$('#campus-directions')?.scrollIntoView({behavior:reduced()?'instant':'smooth',block:'start'});}
 if(action==='previous'){const previous=state.answer?.alternatives?.find(item=>item.type==='previous_itinerary_a');openSheet('Previous plan',`<p>${escape(state.answer?.replan_trigger?.detail||'Previous plan details')}</p>${previous?`<p>${escape(time((previous.itinerary||previous).leave_time))} → ${escape(time((previous.itinerary||previous).arrive_time))} · previous estimate</p>${((previous.itinerary||previous).legs||[]).map(leg=>`<div class="previous-leg"><span>${escape(leg.type==='bus'?`Bus · ${leg.route_id}`:leg.type==='eat'?'Eat at D2':`Walk to ${leg.to}`)}</span><strong>${numberOrDash(leg.minutes)} min</strong></div>`).join('')}<p>${(previous.itinerary||previous).used_bus?'Scheduled bus itinerary':'Estimated walking itinerary'} · retained for comparison</p>`:'<p>Previous itinerary unavailable.</p>'}`);}
  if(action==='limits')openSheet('Sources & assumptions',`<ul class="limits"><li>D2 is the only integrated dining menu in the reference.</li><li>Blank allergen data is UNKNOWN, never safe.</li><li>VT GIS paths are used when provided. Legacy straight-line walking estimates remain explicitly labelled.</li><li>Directions open in your maps app through per-leg deep links.</li><li>Bus positions are discrete observations, not ETAs.</li><li>Replay time stays pinned. Live time comes from the server.</li><li>Weather is NWS-backed in live mode and unavailable in replay without a coherent snapshot. Events use the bounded September 2026 snapshot.</li><li>With Gemini configured, HokieFlow AI selects allowlisted deterministic tools; replay and provider failures use the bounded parser.</li></ul>`);
 if(el.classList.contains('close'))closeSheet();
});
$('#detail').addEventListener('cancel',event=>{event.preventDefault();closeSheet();});
$('#detail').addEventListener('click',event=>{if(event.target===$('#detail'))closeSheet();});
$('.close').innerHTML=icon('close');
async function syncClock(){try{state.clock=await getJSON('/api/time');state.clockFailed=false;syncMoment=performance.now();}catch{state.clockFailed=true;}const label=$('#clock-label');if(label)label.textContent=clockLabel();}
void loadAccount();
if(config.homeEndpoint){void getJSON(config.homeEndpoint).then(data=>{state.homeData=data;render();}).catch(()=>{state.homeError='We couldn’t refresh your day. Please try again shortly.';render();});}
if(preview){state.clock=previewTime;render();}else{render();void Promise.allSettled([syncClock(),getJSON('/api/status').then(status=>{state.status=status;})]).then(()=>render());setInterval(syncClock,30000);setInterval(()=>{const c=state.clock;if(!c||c.is_replay||c.pinned||!c.ticking||state.clockFailed)return;const label=$('#clock-label');if(label)label.textContent=`Campus ${new Intl.DateTimeFormat('en-US',{timeZone:c.timezone||'America/New_York',hour:'numeric',minute:'2-digit',second:'2-digit'}).format(new Date(campusMs()))} · server synced`;},1000);}

function updateFooterSpace(){requestAnimationFrame(()=>{const footer=$('#chat-footer');document.documentElement.style.setProperty('--chat-height',`${footer.offsetHeight}px`);});}
new ResizeObserver(updateFooterSpace).observe($('#chat-footer'));
function keyboardLayout(){const viewport=window.visualViewport;const gap=viewport?Math.max(0,innerHeight-viewport.height-viewport.offsetTop):0;document.documentElement.style.setProperty('--keyboard-gap',`${gap}px`);document.body.classList.toggle('keyboard-open',gap>120);}
window.visualViewport?.addEventListener('resize',keyboardLayout);
window.visualViewport?.addEventListener('scroll',keyboardLayout);

function accountBar(){return `<div class="account-bar"><span>${state.user?`Hi, ${escape(state.user.name)}`:''}</span><button class="account-icon" aria-label="${state.user?'My account':'Log in or sign up'}" data-action="${state.user?'account':'login'}" ${!state.accountReady||state.busy?'disabled':''}>${icon('user')}</button></div>${state.accountError?`<p class="error" role="alert">${escape(state.accountError)}</p>`:''}`;}
function accountPanel(){return state.user?`<section class="account-panel"><span class="account-avatar">${escape(state.user.name.slice(0,1).toUpperCase())}</span><h2>${escape(state.user.name)}</h2><p>${escape(state.user.email)}</p><p class="helper">Your schedule and saved plans belong to this account.</p><button class="text-button" data-action="logout" ${state.busy||state.accountSaving?'disabled':''}>Log out</button><h3>Saved plans</h3>${state.accountData.plans.length?state.accountData.plans.map(plan=>`<div class="saved-plan"><button data-saved-plan="${escape(plan.id)}"><strong>${escape(plan.query||'Campus plan')}</strong><small>${escape(planHeading(plan.answer))} · ${plan.answer._time?.is_replay?'replay':'saved response'}</small></button><button class="remove-plan" aria-label="Remove saved plan: ${escape(plan.query)}" data-remove-plan="${escape(plan.id)}" ${state.accountSaving?'disabled':''}>${icon('close')}</button></div>`).join(''):'<p class="helper">Choose “Save this plan” on a planning result to keep it here.</p>'}</section>`:`<section class="account-panel"><h2>Make it your campus.</h2><p>Create an account to keep your class and plans between visits.</p><button class="primary" data-action="signup" ${!state.accountReady?'disabled':''}>Create account ${icon('arrow')}</button><button class="text-button" data-action="login">Already have an account? Log in</button></section>`;}
async function accountRequest(path,body,method='POST'){
 const response=await fetch(`${config.apiBase}${path}`,{method,credentials:'same-origin',headers:{'Content-Type':'application/json','X-HokieFlow-Request':'1'},...(body===undefined?{}:{body:JSON.stringify(body)}),signal:AbortSignal.timeout(20000)});
 const data=await response.json();if(!response.ok)throw new Error(data.error||'Account request failed.');return data;
}
function applyAccount(result){
 if(Object.hasOwn(result,'user'))state.user=result.user?{...result.user,name:result.user.name||result.user.user_metadata?.name||result.user.email?.split('@')[0]||'Hokie'}:null;
 state.accountStorageError=result.storageError||'';
 if(result.storageErrorCode)console.warn('HokieFlow schedule storage:',result.storageErrorCode);
 if(result.data)state.accountData={savedClass:null,reduceMotion:false,plans:[],events:[],...result.data};
 else state.accountData={savedClass:null,reduceMotion:false,plans:[],events:[]};
 state.accountVersion=result.version||0;state.savedClass=state.accountData.savedClass;
 document.body.classList.remove('reduce-motion');state.accountError=state.accountStorageError;
}
async function loadAccount(){try{applyAccount(await accountRequest('/api/auth/me',undefined,'GET'));}catch{state.accountError='We couldn’t load your account. Please try again shortly.';}finally{state.accountReady=true;render();}}
function authScreen(mode){if(!state.accountReady)return;if($('#detail').open)closeSheet();state.authMode=mode;navigate('login');}
function googleLogo(){return `<svg class="google-mark" width="20" height="20" viewBox="0 0 18 18" aria-hidden="true" focusable="false"><path fill="#4285F4" d="M17.64 9.205c0-.638-.057-1.252-.164-1.841H9v3.481h4.844a4.14 4.14 0 0 1-1.796 2.716v2.258h2.909c1.702-1.567 2.683-3.875 2.683-6.614Z"/><path fill="#34A853" d="M9 18c2.43 0 4.468-.806 5.957-2.181l-2.909-2.258c-.806.54-1.837.859-3.048.859-2.344 0-4.328-1.583-5.036-3.71H.957v2.332A9 9 0 0 0 9 18Z"/><path fill="#FBBC05" d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A9 9 0 0 0 0 9c0 1.452.347 2.827.957 4.042l3.007-2.332Z"/><path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.441 1.346l2.582-2.582C13.464.892 11.426 0 9 0A9 9 0 0 0 .957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58Z"/></svg>`;}
function authBody(mode){return `<p>${mode==='register'?'Create an account to save your classroom and plans.':'Log in to pick up your saved class and plans.'}</p><button class="google-signin" type="button" data-action="google-login">${googleLogo()}Continue with Google</button><div class="auth-divider"><span>or use email</span></div><p id="google-auth-error" class="auth-message" role="alert">${escape(googleAuthErrorMessage)}</p><form id="auth-form" class="class-form" data-mode="${mode}">${mode==='register'?'<label>Your name<input name="name" autocomplete="name" maxlength="60" required></label>':''}<label>Email<input name="email" type="email" autocomplete="email" maxlength="254" required></label><label>Password<input name="password" type="password" autocomplete="${mode==='register'?'new-password':'current-password'}" minlength="12" maxlength="128" required></label><p class="helper">${mode==='register'?'Use 12–128 characters.':'Use your HokieFlow password.'}</p><p id="auth-error" role="alert"></p><button class="primary" type="submit">${mode==='register'?'Create account':'Log in'} ${icon('arrow')}</button></form><button class="text-button" data-action="${mode==='register'?'login':'signup'}">${mode==='register'?'Already have an account? Log in':'New here? Create an account'}</button>`;}
async function submitAuth(form){const button=form.querySelector('[type="submit"]');button.disabled=true;const fields=new FormData(form);const payload=Object.fromEntries(fields);$('#auth-error').textContent='';try{const result=await accountRequest(`/api/auth/${form.dataset.mode}`,payload);if(result.requires_confirmation){$('#auth-error').textContent='Check your email to confirm your account, then log in.';return;}applyAccount(result);state.answer=null;state.query='';if($('#detail').open)closeSheet();navigate('home');announce('Logged in. Your saved data is ready.');}catch(error){const target=$('#auth-error');if(target)target.textContent=error.message;}finally{button.disabled=false;}}
async function saveAccount(data){if(!state.user)throw new Error('Log in to save your data.');if(state.accountStorageError)throw new Error(state.accountStorageError);if(state.accountSaving)throw new Error('A save is already in progress.');state.accountSaving=true;try{applyAccount(await accountRequest('/api/account/data',{data,version:state.accountVersion},'PUT'));}finally{state.accountSaving=false;}}
function showAccountError(error){state.accountError=error.message;if($('#detail').open){let message=$('#account-save-error');if(!message){message=document.createElement('p');message.id='account-save-error';message.className='error';message.setAttribute('role','alert');$('#detail-content').append(message);}message.textContent=error.message;}else render();}
async function saveCurrentPlan(){if(!state.user){authScreen('login');return;}if(!state.answer)return;if(state.accountData.plans.length>=20){showAccountError(new Error('Remove an older saved plan first (20 maximum).'));return;}try{await saveAccount({...state.accountData,plans:[{id:crypto.randomUUID(),query:state.query,answer:structuredClone(state.answer)},...state.accountData.plans]});announce('Plan saved to your account.');openSheet('Plan saved.', '<p>You can find it under More → Saved plans.</p>');}catch(error){showAccountError(error);}}
async function logout(){try{await accountRequest('/api/auth/logout',{});applyAccount({user:null});state.answer=null;state.query='';diningLocation=null;disableBusLocation();navigate('home');void loadBuses();announce('Logged out.');}catch(error){showAccountError(error);}}
document.addEventListener('click',event=>{if(event.target.closest('[data-action="account"]'))navigate('more');});

function startGoogleLogin(){
 const message=$('#google-auth-error');
 try{
  console.log('[HokieFlow auth]', {
   googleAuthUrl: config.googleAuthUrl,
   origin: window.location.origin,
   href: config.googleAuthUrl ? new URL(config.googleAuthUrl, window.location.origin).href : null
  });
  if(!config.googleAuthUrl)throw new Error('Google sign-in is temporarily unavailable. Please use email to continue.');
  const target=new URL(config.googleAuthUrl,window.location.origin);
  if(target.origin!==window.location.origin||!['http:','https:'].includes(target.protocol))throw new Error('Google sign-in is temporarily unavailable. Please use email to continue.');
  window.location.assign(target.href);
 }catch(error){if(message)message.textContent=error.message;}
}

function events(){return state.user?state.accountData.events||[]:[];}
const eventTime=d=>new Intl.DateTimeFormat('en-US',{hour:'numeric',minute:'2-digit'}).format(d);
function eventRows(items){return items.map(e=>`<button class="schedule-event" data-edit-event="${escape(e.id)}"><span class="event-date">${escape(e.occurrenceStart.toLocaleDateString('en-US',{month:'short',day:'numeric'}))}<small>${escape(eventTime(e.occurrenceStart))}</small></span><span><strong>${escape(e.title)}</strong><small>${escape(e.location||'No location')} · ${escape(e.kind)}</small></span>${icon('arrow')}</button>`).join('');}
function upcomingCard(){const items=upcoming(events());return `<section class="home-card" id="upcoming"><div class="section-title"><h2 class="eyebrow">Up next</h2><button class="text-button" data-tab="plan">Your schedule ${icon('arrow')}</button></div>${!state.user?`<p>Sign in to save classes and events.</p><button class="primary" data-action="signup">Create your account ${icon('arrow')}</button>`:items.length?eventRows(items):`<p>No upcoming events.</p><button class="primary" data-action="add-event">Add to your schedule +</button>`}</section>`;}
function schedulePage(){
 if(!state.user)return `${heading('','Schedule','')}${upcomingCard()}`;
 const year=calendarMonth.getFullYear(),month=calendarMonth.getMonth(),days=new Date(year,month+1,0).getDate(),offset=new Date(year,month,1).getDay();
 const monthEvents=occurrences(events(),new Date(year,month,1),new Date(year,month+1,1));
 const selected=occurrences(events(),new Date(selectedDay+'T00:00'),new Date(selectedDay+'T23:59:59'));
 return `${heading('','Schedule','')}<div class="calendar-top"><button aria-label="Previous month" data-calendar-move="-1">‹</button><h2>${calendarMonth.toLocaleDateString('en-US',{month:'long',year:'numeric'})}</h2><button aria-label="Next month" data-calendar-move="1">›</button></div><div class="calendar-grid">${['Sun','Mon','Tue','Wed','Thu','Fri','Sat'].map(d=>`<span class="calendar-weekday">${d}</span>`).join('')}${'<span></span>'.repeat(offset)}${Array.from({length:days},(_,i)=>{const key=dateKey(new Date(year,month,i+1));return `<button data-day="${key}" aria-label="${key}" aria-pressed="${key===selectedDay}" ${key===dateKey(new Date())?'aria-current="date"':''}>${i+1}${monthEvents.some(e=>dateKey(e.occurrenceStart)===key)?'<i aria-hidden="true"></i>':''}</button>`;}).join('')}</div><div class="section-title"><button class="text-button" data-action="calendar-today">Today</button><span class="helper">Times in ${escape(Intl.DateTimeFormat().resolvedOptions().timeZone)}</span></div><button class="primary" data-action="add-event">Add class or event +</button><button class="text-button import-classes-button" data-action="import-classes">Find classes or import calendar</button><section class="day-agenda"><h2 class="eyebrow">${escape(new Date(selectedDay+'T12:00').toLocaleDateString('en-US',{weekday:'long',month:'long',day:'numeric'}))}</h2>${selected.length?eventRows(selected):'<p class="helper">No events.</p>'}</section>`;
}
function eventEditor(id,draft=null){if(!state.user){authScreen('login');return;}const e=draft||events().find(e=>e.id===id);openSheet(e?'Your event.':'Make it a date.',`<form id="event-form" class="class-form" data-id="${escape(e?.id||'')}"><label>Title<input name="title" required maxlength="100" value="${escape(e?.title||'')}" placeholder="CS 2506, study group, lunch…"></label><label>Type<select name="kind"><option ${e?.kind==='class'?'selected':''} value="class">Class</option><option ${e?.kind==='event'?'selected':''} value="event">Event</option></select></label><label>Location<input name="location" maxlength="150" value="${escape(e?.location||'')}" placeholder="Building, room, or meeting spot"></label><label>Starts<input name="start" type="datetime-local" required value="${e?localInput(e.start):selectedDay+'T09:00'}"></label><label>Ends<input name="end" type="datetime-local" required value="${e?(e.end?localInput(e.end):''):selectedDay+'T10:00'}"></label><label>Repeat<select name="repeat"><option value="none">Does not repeat</option><option value="weekly" ${e?.repeat==='weekly'?'selected':''}>Every week on this day</option></select></label><label>Repeat through<input type="date" name="repeatUntil" value="${escape(e?.repeatUntil||'')}"></label><p class="helper">Times use ${escape(Intl.DateTimeFormat().resolvedOptions().timeZone)}. Editing a weekly event changes the whole series.</p><p class="error" id="event-error" role="alert" hidden></p><button class="primary" type="submit">Save event ${icon('check')}</button>${e?.location?`<button class="text-button" type="button" data-dining-place="${escape(e.location)}">Get directions ${icon('pin')}</button>`:''}${e&&!draft?`<button class="text-button" type="button" data-delete-event="${escape(e.id)}">Delete ${e.repeat==='weekly'?'series':'event'}</button>`:''}</form>`);}
async function submitEvent(form){const f=Object.fromEntries(new FormData(form));const start=new Date(f.start),end=new Date(f.end);const error=$('#event-error');if(!f.title.trim()||!Number.isFinite(+start)||!Number.isFinite(+end)||end<=start||f.repeat==='weekly'&&(!f.repeatUntil||f.repeatUntil<f.start.slice(0,10))){error.hidden=false;error.textContent='Add a title, an end time after the start, and an end date for weekly events.';return;}const button=form.querySelector('[type=submit]');button.disabled=true;const item={id:form.dataset.id||crypto.randomUUID(),title:f.title.trim(),kind:f.kind,location:f.location.trim(),start:start.toISOString(),end:end.toISOString(),repeat:f.repeat,repeatUntil:f.repeat==='weekly'?f.repeatUntil:null};try{await saveAccount({...state.accountData,events:[...events().filter(e=>e.id!==item.id),item]});selectedDay=dateKey(start);calendarMonth=new Date(start.getFullYear(),start.getMonth(),1);closeSheet();render();announce('Schedule saved.');}catch(e){error.hidden=false;error.textContent=e.message;}finally{button.disabled=false;}}
async function deleteEvent(id){try{await saveAccount({...state.accountData,events:events().filter(e=>e.id!==id)});closeSheet();render();announce('Event removed.');}catch(e){showAccountError(e);}}
function homeDining(){const location=diningLocation||transitLocation;const places=nearbyDining(diningPlaces,location||CAMPUS).slice(0,4);return `<section id="home-dining" class="home-card nearby-dining"><div class="section-title"><h2 class="eyebrow">${location?'Nearby dining':'Campus dining'}</h2><button class="text-button" data-action="nearby-dining" ${diningLoading?'disabled':''}>${icon('pin')} ${diningLoading?'Loading…':'Near me'}</button></div>${diningError?`<p class="helper" role="alert">${escape(diningError)}</p><button class="text-button" data-action="retry-dining">Retry</button>`:''}${places.map(p=>`<button class="nearby-bus-row dining-nearby-row" data-dining-place="${escape(p.building||p.name)}"><span><strong>${escape(p.name)}</strong><small>${escape(p.building)}</small></span><span class="departure-chips"><b class="distance-chip">${distanceFeet(p.distance)}</b></span></button>`).join('')}${!places.length&&!diningError?'<p class="helper">Loading dining locations…</p>':''}<div class="bus-footnote"><span></span><button class="text-button" data-tab="dining">See all</button></div></section>`;}

async function loadDining(){diningLoading=true;diningError='';updateHomeData();try{const data=await getJSON('/api/dining/places');if(!Array.isArray(data.places))throw new Error('Dining locations couldn’t load. Please try again.');diningPlaces=data.places;diningLoaded=true;}catch(e){diningError='Dining locations couldn’t load. Please try again.';}finally{diningLoading=false;if(state.tab==='dining')render();else updateHomeData();}}
function locateDining(){
 if(diningLoading)return;
 diningLocationFailed=false;diningError='';
 if(!window.isSecureContext||!navigator.geolocation){diningLocationFailed=true;diningError='Location needs HTTPS or localhost. Open this page in Safari or Chrome and allow location.';render();return;}
 diningLoading=true;render();
 const success=p=>{diningLocation={lat:p.coords.latitude,lon:p.coords.longitude,accuracy:p.coords.accuracy};diningLoading=false;diningLocationFailed=false;if(!diningLoaded)void loadDining();else render();};
 const failure=e=>{diningLoading=false;diningLocationFailed=true;diningError=locationError(e)+' If you are using the in-app preview, try Safari or Chrome.';render();};
 navigator.geolocation.getCurrentPosition(success,e=>{
  if(e.code===1){failure(e);return;}
  navigator.geolocation.getCurrentPosition(success,failure,{timeout:20000,maximumAge:60000,enableHighAccuracy:false});
 },{timeout:15000,maximumAge:0,enableHighAccuracy:true});
}
function diningDirectory(){const places=nearbyDining(diningPlaces,diningLocation);return `<div class="dining-location"><button class="primary" data-action="nearby-dining" ${diningLoading?'disabled':''}>${diningLoading?'Finding your options…':diningLocation?'Refresh my location':'Find dining near me'} ${icon('pin')}</button>${diningLocation?`<button class="text-button" data-action="all-dining">Show all campus dining</button>`:''}</div>${diningError?`<p class="notice" role="alert">${escape(diningError)}</p><button class="text-button" data-action="retry-dining">Try again</button>`:''}${!places.length&&!diningLoading&&!diningError?'<div class="empty"><h2>Your next favorite spot.</h2><p>No dining locations to show right now. Check back soon.</p></div>':''}<div class="dining-list">${places.map(p=>`<article class="dining-place"><div class="section-title"><h2>${escape(p.name)}</h2>${p.distance!==null?badge(distanceFeet(p.distance)+' away'):''}</div><p>${escape(p.building||p.address||'Campus dining')}</p>${p.hours_text?`<p class="helper">${escape(p.hours_text)}</p>`:''}${p.description?`<p>${escape(p.description)}</p>`:''}${p.updated_at?`<p class="helper">Updated ${escape(p.updated_at)}</p>`:''}<button class="text-button" data-dining-place="${escape(p.building||p.name)}">Get directions ${icon('arrow')}</button></article>`).join('')}</div>`;}

setInterval(()=>{const card=$("#upcoming");if(card&&state.tab==='home'&&!$('#detail').open&&!state.busy)card.outerHTML=upcomingCard();},60000);

function weatherMarkup(){return homeWeather?`<strong>${homeWeather.temperature}°</strong><span>${escape(homeWeather.condition)}</span>${homeWeather.rain?`<b>${homeWeather.rain.chance}% rain · ${new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour:'numeric'}).format(new Date(homeWeather.rain.at))}</b>`:''}`:`<span>${weatherError?'Weather unavailable':'Weather…'}</span>`;}
function nearbyBusCard(){const busNow=transitReplay?campusMs():Date.now();const rows=departureRows(transitDepartures,busNow);const stale=!transitReplay&&(!transitUpdated||Date.now()-transitUpdated>120000);return `<section class="home-card nearby-buses"><div class="section-title"><h2 class="eyebrow">${transitLocation?'Nearby buses':'Campus buses'}</h2><button class="text-button" data-action="nearby-buses" aria-pressed="${Boolean(stopBusLocation)}">${icon('pin')} ${busLocating?'Locating…':stopBusLocation?'Location on':'Near me'}</button></div>${busLocationError?`<p class="helper" role="alert">${escape(busLocationError)}</p>`:''}${transitError?`<p class="helper" role="status">${escape(transitError)}</p>`:''}${rows.map(row=>`<a class="nearby-bus-row" href="https://ridebt.org/routes-schedules" target="_blank" rel="noreferrer"><span><strong>${escape(row.route)}</strong><small>${escape(row.stop_name)} #${escape(row.stop_id)}</small>${['maroon','orange'].includes(row.destination_loop)?`<small class="loop-label">${row.destination_loop==='maroon'?'Maroon':'Orange'} Loop</small>`:''}</span><span class="departure-chips">${row.times.map((t,i)=>`<b class="${i===0?'soon '+(['maroon','orange'].includes(row.destination_loop)?'loop-'+row.destination_loop:'loop-unknown'):''}" title="${escape(new Date(t).toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'numeric',minute:'2-digit'}))}">${stale?'—':Math.max(0,Math.ceil((t-busNow)/60000))+(stale?'':'m')}</b>`).join('')}</span></a>`).join('')}${!rows.length?`<p class="helper">${transitBusy?'Loading departures…':transitError?'':'No upcoming departures at these stops.'}</p>`:''}<div class="bus-footnote"><span>${rows.length?(transitReplay?'BT schedule · replay':stale?'Times need refreshing':'BT estimates · updated '+Math.max(0,Math.floor((Date.now()-transitUpdated)/1000))+'s ago'):(transitLocation?'Within 1.5 km':'Near central campus')}</span><button class="text-button" data-action="refresh-buses" ${transitBusy?'disabled':''}>Refresh</button></div></section>`;}
function updateHomeData(){if($('#home-dining'))$('#home-dining').outerHTML=homeDining();if($('#home-weather'))$('#home-weather').innerHTML=weatherMarkup();if($('#home-transit'))$('#home-transit').innerHTML=nearbyBusCard();}
async function loadWeather(){try{homeWeather=await fetchWeather();weatherError=false;}catch{weatherError=true;homeWeather=null;}updateHomeData();}
async function loadBuses(){
 if(transitBusy)return;
 const version=busLocationVersion,location=transitLocation||CAMPUS;
 transitBusy=true;transitError='';updateHomeData();
 try{
  if(!transitStops.length){const data=await getJSON('/api/transit/stops');if(data.status==='unavailable')throw new Error();transitStops=data.stops||[];}
  const stops=closestStops(transitStops,location);
  if(!stops.length){if(version===busLocationVersion){transitDepartures=[];transitError='No BT stops within 1.5 km of your location.';}return;}
  const responses=await Promise.allSettled(stops.map(s=>getJSON('/api/transit/departures?stop='+encodeURIComponent(s.id))));
  if(version!==busLocationVersion)return;
  const valid=responses.filter(r=>r.status==='fulfilled'&&r.value.status!=='unavailable');if(!valid.length)throw new Error();
  const fresh=valid.filter(r=>r.value.is_replay||(Number.isFinite(Date.parse(r.value.fetched_at))&&Date.now()-Date.parse(r.value.fetched_at)<=120000));if(!fresh.length)throw new Error();
  transitReplay=fresh.every(r=>r.value.is_replay);transitDepartures=fresh.flatMap(r=>r.value.departures||[]);transitUpdated=Math.min(...fresh.map(r=>Date.parse(r.value.fetched_at)));
  if(fresh.length!==responses.length)transitError='Some stop times couldn’t refresh.';
 }catch{if(version===busLocationVersion){transitDepartures=[];transitError='Bus times are temporarily unavailable. Retrying automatically.';}}
 finally{transitBusy=false;updateHomeData();if(version!==busLocationVersion)void loadBuses();}
}
function disableBusLocation(){stopBusLocation?.();stopBusLocation=null;busLocating=false;transitLocation=null;busLocationVersion++;transitDepartures=[];}
function locateBuses(){
 if(stopBusLocation){disableBusLocation();busLocationError='';void loadBuses();return;}
 if(!window.isSecureContext||!navigator.geolocation){busLocationError='Location needs HTTPS or localhost in a browser that supports location.';updateHomeData();return;}
 busLocating=true;busLocationError='';updateHomeData();
 stopBusLocation=trackBusLocation(navigator.geolocation,{
  onPosition(position){
   busLocating=false;busLocationError='';
   const oldIds=transitLocation?closestStops(transitStops,transitLocation).map(s=>s.id).join(','):null;
   transitLocation=position;
   const newIds=closestStops(transitStops,position).map(s=>s.id).join(',');
   if(oldIds===null||oldIds!==newIds){busLocationVersion++;transitDepartures=[];void loadBuses();}else updateHomeData();
  },
  onError(error){disableBusLocation();busLocationError=locationError(error);void loadBuses();}
 });
}
window.addEventListener('pagehide',()=>{stopBusLocation?.();stopBusLocation=null;});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)void loadBuses();});
window.addEventListener('online',()=>void loadBuses());
void loadDining();void loadWeather();void loadBuses();setInterval(()=>{if(!document.hidden){void loadBuses();}},15000);setInterval(()=>{if(!document.hidden)void loadWeather();},600000);setInterval(()=>{updateHomeData();const date=$('.home-date');if(date){const now=new Date();date.children[0].textContent=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',weekday:'long'}).format(now);date.children[1].textContent=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',month:'long',day:'numeric'}).format(now);}const h=$('.home-header h1');if(h)h.innerHTML=greeting()+'<br><span>Hokie<b>.</b></span>';},10000);

// Wait for deferred vendor scripts before mounting animation enhancements.
window.addEventListener("load",()=>mountScrollMotion($("#main")),{once:true});

async function loadCampusEvents(){
 if(campusEventState==='loading')return;
 campusEventState='loading';campusEventError='';render();
 try{const result=await getJSON('/api/events');campusEvents=result.events||[];campusEventNotices=result.notices||[];campusEventState=result.state||'ok';}
 catch{campusEventState='unavailable';campusEventError='Events could not load. Try again.';}
 if(state.tab==='events')render();
}
function campusEventsPage(){
 const stamp=value=>new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}).format(new Date(value));
 return `${heading('','Events','')}<div class="section-title events-toolbar"><button class="text-button" data-action="refresh-events" ${campusEventState==='loading'?'disabled':''}>Refresh</button></div><p class="helper">Virginia Tech · Times in Eastern time</p>${campusEventState==='loading'?'<p role="status" class="helper">Loading events…</p>':''}${campusEventError&&campusEventError!==state.accountError?`<p class="error" role="alert">${escape(campusEventError)}</p>`:''}${campusEventNotices.map(n=>`<p class="helper">${escape(n)}</p>`).join('')}<div class="campus-events">${campusEvents.map(e=>{
 const added=events().some(item=>item.id===e.id);
 return `<article class="campus-event"><div><p class="eyebrow">${escape(e.all_day?new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',month:'short',day:'numeric'}).format(new Date(e.start))+' · All day':stamp(e.start))}</p><h2>${escape(e.title)}</h2><p>${escape(e.location||'Location not published')}</p>${!e.end?'<p class="helper">Choose an end time when adding.</p>':''}${/^https:\/\/events\.vt\.edu\//.test(e.url||'')?`<a class="text-button" href="${escape(e.url)}" target="_blank" rel="noopener noreferrer">Event details</a>`:''}</div><button class="add-campus-event" data-campus-event="${escape(e.id)}" aria-label="${escape(added?'Added to schedule: '+e.title:'Add to schedule: '+e.title)}" ${added||addingCampusEvent?'disabled':''}>${added?icon('check'):addingCampusEvent===e.id?'…':'+'}</button></article>`;
 }).join('')}</div>${!campusEvents.length&&campusEventState!=='loading'?'<p class="helper">No upcoming events in the available feed.</p>':''}`;
}
async function addCampusEvent(id){
 if(!state.user){authScreen('login');return;}
 if(addingCampusEvent||events().some(e=>e.id===id))return;
 const e=campusEvents.find(e=>e.id===id);if(!e)return;
 const item={id:e.id,title:e.title.slice(0,100),kind:'event',location:(e.location||'').slice(0,150),start:e.start,end:e.end,repeat:'none',repeatUntil:null};
 if(!item.end){eventEditor(null,item);return;}
 addingCampusEvent=id;campusEventError='';render();
 try{await saveAccount({...state.accountData,events:[...events(),item]});announce('Added to your schedule.');}
 catch(error){campusEventError=error.message;}
 finally{addingCampusEvent=null;render();}
}
