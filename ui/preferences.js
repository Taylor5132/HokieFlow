import {occurrences} from './schedule.js';
const KEY='hokieflow.preferences';
export function loadPreferences(storage){try{const p=JSON.parse(storage.getItem(KEY)||'{}');return {theme:p.theme==='dark'?'dark':'light',notifications:p.notifications===true};}catch{return {theme:'light',notifications:false};}}
export function savePreferences(storage,prefs){try{storage.setItem(KEY,JSON.stringify(prefs));}catch{/* Private browsing may disable persistence. */}}
export function dueReminders(events,now=new Date()){
 return occurrences(events,now,new Date(+now+600000)).filter(e=>e.occurrenceStart>now).map(e=>({key:`${e.id}:${e.occurrenceStart.toISOString()}`,title:e.title,minutes:Math.ceil((e.occurrenceStart-now)/60000)}));
}
