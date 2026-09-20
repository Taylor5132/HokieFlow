export const dateKey=d=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
export function occurrences(events,from,to){
 const result=[];
 for(const event of events||[]){
  const initial=new Date(event.start),end=new Date(event.end);if(!Number.isFinite(+initial)||!Number.isFinite(+end)||end<=initial)continue;
  let start=new Date(initial);const duration=end-initial;
  if(event.repeat==='weekly'&&start<from){const weeks=Math.max(0,Math.floor((from-start)/604800000)-1);start.setDate(start.getDate()+weeks*7);}
  for(let n=0;n<600&&start<=to;n++){
   if(event.repeat==='weekly'&&event.repeatUntil&&dateKey(start)>event.repeatUntil)break;
   const finish=new Date(+start+duration);
   if(finish>from)result.push({...event,occurrenceStart:new Date(start),occurrenceEnd:finish});
   if(event.repeat!=='weekly')break;start.setDate(start.getDate()+7);
  }
 }
 return result.sort((a,b)=>a.occurrenceStart-b.occurrenceStart);
}
export function upcoming(events,now=new Date(),limit=3){const until=new Date(now);until.setFullYear(until.getFullYear()+5);return occurrences(events,now,until).slice(0,limit);}
export function localInput(iso){const d=new Date(iso);return `${dateKey(d)}T${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;}
