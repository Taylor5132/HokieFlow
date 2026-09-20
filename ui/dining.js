export function distanceMeters(a,b){const rad=x=>x*Math.PI/180;const dlat=rad(b.lat-a.lat),dlon=rad(b.lon-a.lon);const h=Math.sin(dlat/2)**2+Math.cos(rad(a.lat))*Math.cos(rad(b.lat))*Math.sin(dlon/2)**2;return 6371000*2*Math.atan2(Math.sqrt(h),Math.sqrt(1-h));}
export function nearbyDining(places,location){return (Array.isArray(places)?places:[]).filter(p=>typeof p.name==='string').map(p=>({...p,distance:location&&Number.isFinite(p.lat)&&Number.isFinite(p.lon)&&Math.abs(p.lat)<=90&&Math.abs(p.lon)<=180?distanceMeters(location,p):null})).sort((a,b)=>(a.distance??Infinity)-(b.distance??Infinity));}
export function distanceFeet(meters){return Number.isFinite(meters)&&meters>=0?`${Math.round(meters*3.280839895).toLocaleString('en-US')} ft`:'—';}

// ---- menu presentation helpers (pure, so they are unit-tested) -------------

// Meal tabs come from the payload, so a hall that publishes only breakfast or
// dinner shows only those -- never an empty Lunch tab.
export function mealTabs(payload){
  const meals=(payload?.meals||[]).filter(m=>m&&m.meal);
  return meals.map(m=>({meal:m.meal,count:Number(m.count)||0,label:`${m.meal}${m.count?` (${m.count})`:''}`}));
}

// One line per macro, with unknown kept unknown: replay/offline nutrition is
// refused upstream rather than shown as a stale number.
export function nutritionRows(item){
  const n=item?.nutrition;
  if(!n)return [{label:'Nutrition',value:'Not available for this date'}];
  const round=(v,d=1)=>Number.isFinite(Number(v))?Number(v).toFixed(d).replace(/\.0+$/,''):null;
  const rows=[
    ['Calories',round(n.kcal,0),'kcal'],
    ['Protein',round(n.protein_g),'g'],
    ['Carbs',round(n.carb_g),'g'],
    ['Fat',round(n.fat_g),'g'],
    ['Sodium',round(n.sodium_mg,0),'mg'],
  ];
  return rows.map(([label,value,unit])=>({label,value:value===null?'Unknown':`${value} ${unit}`}));
}

// SAFETY: a blank allergen field is UNKNOWN, not "contains none". The only thing
// that explains a blank is a documented allergen-free kitchen.
export function allergenLine(item){
  const listed=(item?.allergens||[]).filter(Boolean);
  if(listed.length)return `Contains: ${listed.join(', ')}`;
  if(item?.venue_allergen_free===true)return 'Documented allergen-free kitchen';
  return 'Not stated — treat as unknown, ask staff';
}
