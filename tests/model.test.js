import test from 'node:test';
import assert from 'node:assert/strict';
import {planHeading,allergenLabel,numberOrDash,sourceForLeg} from '../ui/model.js';
import {previewStates} from '../ui/fixtures.js';

test('infeasible and contradictory plans never get a fits headline',()=>{
 assert.equal(planHeading(previewStates.late),'No plan fits.');
 assert.equal(planHeading({...previewStates.fits,itinerary:{...previewStates.fits.itinerary,arrives_in_window:false}}),'No plan fits.');
 assert.equal(planHeading({feasible:true,itinerary:{legs:[]}}),'No usable plan yet.');
});
test('clarification takes priority over a generic failure',()=>assert.equal(planHeading(previewStates.clarify),'I need one detail.'));
test('blank allergens are unknown; declarations take priority over a conflicting kitchen flag',()=>{
 assert.equal(allergenLabel({allergens:[],allergens_known:true}),'Allergens UNKNOWN');
 assert.equal(allergenLabel({allergens:['Milk'],venue_allergen_free:true}),'Declared allergens: Milk');
 assert.match(allergenLabel({allergens:[],venue_allergen_free:true}),/Viridian/);
});
test('unknown nutrition is not displayed as zero',()=>{assert.equal(numberOrDash(null),'—');assert.equal(numberOrDash(0),'0');});
test('bus legs never imply live vehicle ETAs',()=>assert.equal(sourceForLeg({type:'bus',is_realtime:true}),'Scheduled · service-filtered'));

