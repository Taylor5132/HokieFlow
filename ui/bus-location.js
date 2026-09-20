/** Opt-in location tracking. No persistence or transmission of coordinates. */
export function trackBusLocation(geolocation,{onPosition,onError}){
 let active=true;
 const id=geolocation.watchPosition(position=>{
  if(active)onPosition({lat:position.coords.latitude,lon:position.coords.longitude,accuracy:position.coords.accuracy,observedAt:position.timestamp});
 },error=>{if(active)onError(error);},{enableHighAccuracy:true,maximumAge:10000,timeout:15000});
 return ()=>{active=false;geolocation.clearWatch(id);};
}
export function locationError(error){return error?.code===1?'Location permission is blocked. Allow location for this site in your browser, then tap Near me again.':error?.code===3?'Location timed out. Try Near me again outdoors or with Wi-Fi enabled.':'Your device couldn’t determine its location. Try Near me again.';}
