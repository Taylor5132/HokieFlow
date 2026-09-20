import test from 'node:test';
import assert from 'node:assert/strict';
import {campusNowMs,departureRows,closestStops,weatherLabel} from '../ui/home-live.js';
test('departure chips omit past times, deduplicate and group by stop and route',()=>{
 const make=(time,stop='1')=>({route:'TEST',stop_id:stop,departure_at:time});
 const rows=departureRows([make('2026-09-20T12:00Z'),make('2026-09-20T12:00Z'),make('2026-09-20T11:00Z'),make('2026-09-20T12:30Z','2')],Date.parse('2026-09-20T11:30Z'));
 assert.equal(rows.length,2);assert.equal(rows[0].times.length,1);
});
test('nearby stops exclude stops outside 1.5 km',()=>{assert.equal(closestStops([{lat:0,lon:0},{lat:10,lon:10}],{lat:0,lon:0}).length,1);});
test('weather uses code meanings rather than guessed conditions',()=>{assert.equal(weatherLabel(2),'Partly cloudy');assert.equal(weatherLabel(95),'Thunderstorms');assert.equal(weatherLabel(null),'Weather');});
test('opposite loop destinations stay separate for the same route and stop',()=>{
 const rows=departureRows(['orange','maroon'].map(destination_loop=>({route:'CAS',stop_id:'1',destination_loop,departure_at:'2026-09-20T12:00Z'})),Date.parse('2026-09-20T11:00Z'));
 assert.equal(rows.length,2);assert.notEqual(rows[0].destination_loop,rows[1].destination_loop);
});

test('replay uses the pinned snapshot clock, not the browser clock',()=>{
 // Regression: the bus board filtered departures against Date.now(), so with a
 // frozen snapshot (pinned 2026-09-19) real departures for that snapshot were all
 // in the past and the card rendered empty in the offline demo.
 const pinned=campusNowMs({iso:'2026-09-19T11:22:29-04:00',is_replay:true,pinned:true,ticking:false});
 assert.equal(pinned,Date.parse('2026-09-19T11:22:29-04:00'));
 const rows=departureRows([{route:'SME',stop_id:'1600',departure_at:'2026-09-19T11:48:29-04:00'}],pinned);
 assert.equal(rows.length,1);
 assert.equal(Math.ceil((rows[0].times[0]-pinned)/60000),26);
});
test('live mode advances the synced instant by elapsed local time',()=>{
 const synced={iso:'2026-09-19T11:22:29-04:00',is_replay:false,pinned:false,ticking:true};
 const at=campusNowMs(synced,{syncMoment:1000,performanceNow:31000});
 assert.equal(at-Date.parse('2026-09-19T11:22:29-04:00'),30000);
});
test('a failed sync holds the last known instant instead of guessing',()=>{
 const held=campusNowMs({iso:'2026-09-19T11:22:29-04:00',is_replay:false,pinned:false,ticking:true},{failed:true,syncMoment:1000,performanceNow:99000});
 assert.equal(held,Date.parse('2026-09-19T11:22:29-04:00'));
});
test('an absent clock reports null so callers can fall back',()=>{
 assert.equal(campusNowMs(null),null);
 assert.equal(campusNowMs({}),null);
});
