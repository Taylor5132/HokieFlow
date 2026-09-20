import test from 'node:test';
import assert from 'node:assert/strict';
import {departureRows,closestStops,weatherLabel} from '../ui/home-live.js';
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
