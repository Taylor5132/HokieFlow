import test from 'node:test';
import assert from 'node:assert/strict';
import {buildings,matchBuilding} from '../ui/buildings.js';
import {appleMapsURL,placeDirectionsURL} from '../ui/directions.js';
import {distanceFeet} from '../ui/dining.js';
const mc=matchBuilding('McBryde Hall').id,war=matchBuilding('War Memorial Gym').id;
test('curated list contains only requested groups and distinct wings',()=>{
 assert.equal(buildings.length,69);assert.equal(new Set(buildings.map(b=>b.id)).size,69);
 assert.equal(buildings.filter(b=>b.name.startsWith('Eggleston Hall')).length,3);
 assert.equal(buildings.some(b=>b.name==='Data and Decision Sciences'),false);
 assert.equal(buildings.some(b=>b.name==='Lavery Hall'),false);
});
test('current location defaults to Apple Maps here without sending coordinates',()=>{
 const u=new URL(appleMapsURL({destination:mc}));assert.equal(u.origin,'https://maps.apple.com');assert.equal(u.searchParams.has('saddr'),false);assert.equal(u.searchParams.get('dirflg'),'w');assert.match(u.searchParams.get('daddr'),/McBryde Hall, Virginia Tech, Blacksburg/);
});
test('building origin and bus choice encode Apple transit directions',()=>{
 const u=new URL(appleMapsURL({origin:mc,destination:war,mode:'bus'}));assert.equal(u.searchParams.get('dirflg'),'r');assert.match(u.searchParams.get('saddr'),/McBryde Hall/);
});
test('explicit location is used only with current-location origin',()=>{
 const position={lat:1,lon:2};assert.equal(new URL(appleMapsURL({destination:mc,position})).searchParams.get('saddr'),'1,2');assert.match(new URL(appleMapsURL({origin:war,destination:mc,position})).searchParams.get('saddr'),/War Memorial Gym/);
});
test('bad destinations, identical selections and nonfinite coordinates fail',()=>{
 for(const args of [{destination:'bad'},{origin:mc,destination:mc},{destination:mc,mode:'ada'},{destination:mc,position:{lat:NaN,lon:2}}])assert.throws(()=>appleMapsURL(args));
});
test('dining directions safely encode text without adding dropdown options',()=>{
 const u=new URL(placeDirectionsURL('Turner Place & dining'));assert.equal(u.hostname,'maps.apple.com');assert.match(u.searchParams.get('daddr'),/^Turner Place & dining/);assert.equal(buildings.some(b=>b.name==='Turner Place'),false);
});
test('dining distances display feet for small and large distances',()=>{
 assert.equal(distanceFeet(100),'328 ft');assert.equal(distanceFeet(1000),'3,281 ft');assert.equal(distanceFeet(0),'0 ft');assert.equal(distanceFeet(null),'—');assert.equal(distanceFeet(NaN),'—');
});
