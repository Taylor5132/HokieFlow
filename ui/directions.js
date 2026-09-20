import {buildings,buildingGroups,matchBuilding} from './buildings.js';

export function appleMapsURL({origin='current',destination,mode='walk',position=null}){
 if(!['walk','bus'].includes(mode))throw new Error('Choose Walk or Bus.');
 const to=buildings.find(b=>b.id===destination);
 if(!to)throw new Error('Choose your destination.');
 if(origin===destination)throw new Error('Choose two different buildings.');
 const url=new URL('https://maps.apple.com/');
 url.searchParams.set('daddr',`${to.name}, Virginia Tech, Blacksburg, VA`);
 url.searchParams.set('dirflg',mode==='bus'?'r':'w');
 if(origin==='current'){
  if(position){
   if(!Number.isFinite(position.lat)||!Number.isFinite(position.lon)||Math.abs(position.lat)>90||Math.abs(position.lon)>180)throw new Error('Location unavailable. Please try Find me again.');
   url.searchParams.set('saddr',`${position.lat},${position.lon}`);
  }
 }else{
  const from=buildings.find(b=>b.id===origin);if(!from)throw new Error('Choose a starting point.');
  url.searchParams.set('saddr',`${from.name}, Virginia Tech, Blacksburg, VA`);
 }
 return url.href;
}
// Dining and schedule links may refer to a named place outside the dropdown list.
// These links go straight to Apple Maps without expanding the curated list.
export function placeDirectionsURL(place){
 if(!String(place||'').trim())throw new Error('Choose a destination.');
 const url=new URL('https://maps.apple.com/');url.searchParams.set('daddr',`${String(place).trim()}, Virginia Tech, Blacksburg, VA`);url.searchParams.set('dirflg','w');return url.href;
}
const pin='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="12" cy="12" r="6"/><path d="M12 2v4m0 12v4M2 12h4m12 0h4"/></svg>';
const walkIcon='<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="14" cy="4" r="2"/><path d="m7 21 4-8 3 3v5m-8-9 3-4 4 1 3 4h3M11 9l-1 5"/></svg>';
const busIcon='<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><rect x="4" y="3" width="16" height="15" rx="3"/><path d="M4 10h16M7 14h1m8 0h1M7 18v3m10-3v3"/></svg>';
export function mountDirections(root,{selection={origin:'current',destination:'',mode:'walk'},destinationHint=''}={}){
 let alive=true,requestVersion=0,position=null;
 root.innerHTML=`<div class="route-fields"><div class="route-field"><span class="route-point" aria-hidden="true">A</span><div><label for="route-origin">From</label><select id="route-origin"></select></div><button type="button" class="find-me" aria-label="Find my location">${pin}<span>Find me</span></button></div><div class="route-field"><span class="route-point destination-point" aria-hidden="true">B</span><div><label for="route-destination">To</label><select id="route-destination"></select></div></div></div><p class="route-location helper" role="status"></p><div class="route-modes" role="group" aria-label="Travel mode"><button type="button" data-route-mode="walk">${walkIcon} Walk</button><button type="button" data-route-mode="bus">${busIcon} Bus</button></div><a class="primary campus-directions" target="_blank" rel="noopener noreferrer">Find campus route</a><p class="route-provider">Opens in Apple Maps</p><p class="route-error helper" role="alert"></p>`;
 const from=root.querySelector('#route-origin'),to=root.querySelector('#route-destination'),link=root.querySelector('.campus-directions'),error=root.querySelector('.route-error'),note=root.querySelector('.route-location'),locate=root.querySelector('.find-me');
 from.add(new Option('My location','current'));to.add(new Option('Choose a building',''));
 for(const select of [from,to])for(const [group] of buildingGroups){const node=document.createElement('optgroup');node.label=group;for(const b of buildings.filter(b=>b.group===group))node.append(new Option(b.name,b.id));select.append(node);}
 from.value=selection.origin;to.value=selection.destination;
 function update(){selection.origin=from.value;selection.destination=to.value;error.textContent='';root.querySelectorAll('[data-route-mode]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.routeMode===selection.mode)));try{link.href=appleMapsURL({...selection,position});link.removeAttribute('aria-disabled');}catch{link.removeAttribute('href');link.setAttribute('aria-disabled','true');}}
 function hint(value){const found=matchBuilding(value);if(found){to.value=found.id;selection.destination=found.id;}update();}
 from.onchange=()=>{requestVersion++;position=null;locate.disabled=false;locate.querySelector('span').textContent='Find me';note.textContent='';update();};to.onchange=update;
 root.querySelectorAll('[data-route-mode]').forEach(button=>button.onclick=()=>{selection.mode=button.dataset.routeMode;update();});
 link.onclick=event=>{try{link.href=appleMapsURL({...selection,position});}catch(e){event.preventDefault();error.textContent=e.message;to.focus();}};
 locate.onclick=()=>{
  from.value='current';position=null;update();const version=++requestVersion;
  if(!navigator.geolocation||!window.isSecureContext){note.textContent='Apple Maps will use your location when you open directions.';return;}
  locate.disabled=true;locate.querySelector('span').textContent='Finding…';note.textContent='Allow location to set your starting point.';
  navigator.geolocation.getCurrentPosition(p=>{if(!alive||version!==requestVersion)return;position={lat:p.coords.latitude,lon:p.coords.longitude};locate.disabled=false;locate.querySelector('span').textContent='Find me';note.textContent='Location ready. It will be shared with Apple Maps when you open directions.';update();},()=>{if(!alive||version!==requestVersion)return;locate.disabled=false;locate.querySelector('span').textContent='Find me';note.textContent='Apple Maps can use your location when you open directions, or choose a building.';update();},{enableHighAccuracy:true,timeout:10000,maximumAge:30000});
 };
 if(destinationHint)hint(destinationHint);else update();
 return {destroy(){alive=false;requestVersion++;},setDestinationHint(value){hint(value);to.focus();}};
}
