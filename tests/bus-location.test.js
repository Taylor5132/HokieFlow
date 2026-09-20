import test from 'node:test';
import assert from 'node:assert/strict';
import {trackBusLocation,locationError} from '../ui/bus-location.js';
test('tracking receives movement and stops delivering after cancellation',()=>{
 let success,options,cleared;const values=[];
 const geo={watchPosition(s,e,o){success=s;options=o;return 7;},clearWatch(id){cleared=id;}};
 const stop=trackBusLocation(geo,{onPosition:p=>values.push(p),onError:()=>{}});
 success({coords:{latitude:37,longitude:-80,accuracy:12},timestamp:123});
 success({coords:{latitude:37.01,longitude:-80,accuracy:10},timestamp:124});
 assert.equal(values.length,2);assert.equal(values[1].lat,37.01);assert.equal(options.maximumAge,10000);
 stop();success({coords:{latitude:38},timestamp:125});assert.equal(cleared,7);assert.equal(values.length,2);
});
test('permission and timeout failures have distinct recovery instructions',()=>{
 assert.match(locationError({code:1}),/Allow location/);assert.match(locationError({code:3}),/timed out/);
});
