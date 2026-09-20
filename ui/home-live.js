import {distanceMeters} from './dining.js';
export const CAMPUS={lat:37.2296,lon:-80.424};
export function weatherLabel(code){if(code===0)return 'Clear';if([1,2].includes(code))return 'Partly cloudy';if(code===3)return 'Overcast';if([45,48].includes(code))return 'Fog';if([51,53,55,56,57].includes(code))return 'Drizzle';if([61,63,65,66,67,80,81,82].includes(code))return 'Rain';if([71,73,75,77,85,86].includes(code))return 'Snow';if([95,96,99].includes(code))return 'Thunderstorms';return 'Weather';}
export function closestStops(stops,location){return stops.map(s=>({...s,distance:distanceMeters(location,s)})).filter(s=>Number.isFinite(s.distance)&&s.distance<=1500).sort((a,b)=>a.distance-b.distance).slice(0,3);}
export function campusNowMs(clock,{syncMoment=0,performanceNow=0,failed=false}={}){
 // The server clock is authoritative (replay is PINNED to the snapshot), so the
 // browser clock must never decide which departures are still upcoming. Live
 // mode advances the last synced instant by locally elapsed time; replay does not.
 const server=Date.parse(clock?.iso||clock?.evaluated_at||'');
 if(!Number.isFinite(server))return null;
 if(failed||clock?.is_replay||clock?.pinned||!clock?.ticking)return server;
 return server+Math.max(0,performanceNow-syncMoment);
}
export function departureRows(departures,now=Date.now()){
 const groups=new Map();for(const d of departures){const timestamp=Date.parse(d.departure_at);if(!Number.isFinite(timestamp)||timestamp<now)continue;const key=d.stop_id+'|'+d.route+'|'+(d.destination_loop||d.pattern||'');if(!groups.has(key))groups.set(key,{...d,times:[]});groups.get(key).times.push(timestamp);}
 return [...groups.values()].map(g=>({...g,times:[...new Set(g.times)].sort((a,b)=>a-b).slice(0,3)})).sort((a,b)=>a.times[0]-b.times[0]).slice(0,4);
}
export async function fetchWeather(){
 const params=new URLSearchParams({latitude:CAMPUS.lat,longitude:CAMPUS.lon,current:'temperature_2m,weather_code',hourly:'precipitation_probability',temperature_unit:'fahrenheit',timezone:'America/New_York',timeformat:'unixtime',forecast_days:'2'});
 const r=await fetch('https://api.open-meteo.com/v1/forecast?'+params,{signal:AbortSignal.timeout(12000)});if(!r.ok)throw new Error('Weather unavailable');const data=await r.json();
 if(!Number.isFinite(data.current?.temperature_2m))throw new Error('Weather unavailable');
 const index=data.hourly?.time?.findIndex((t,i)=>t>=Date.now()/1000&&t<Date.now()/1000+43200&&data.hourly.precipitation_probability[i]>=50)??-1;
 return {temperature:Math.round(data.current.temperature_2m),condition:weatherLabel(data.current.weather_code),rain:index>=0?{at:data.hourly.time[index]*1000,chance:data.hourly.precipitation_probability[index]}:null,at:data.current.time*1000};
}
