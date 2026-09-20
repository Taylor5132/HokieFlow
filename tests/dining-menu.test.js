import test from 'node:test';
import assert from 'node:assert/strict';
import {mealTabs,nutritionRows,allergenLine} from '../ui/dining.js';

// The Dining tab listed halls but never their food. These helpers turn one
// /api/dining/menu payload into the meal switcher, the nutrition detail, and the
// allergen line -- and keep the two honesty rules the backend enforces.

test('meal tabs come from the data, not a fixed list',()=>{
  const tabs=mealTabs({meals:[{meal:'Lunch',count:202},{meal:'Dinner',count:177}]});
  assert.deepEqual(tabs.map(t=>t.meal),['Lunch','Dinner']);
  assert.equal(tabs[0].label,'Lunch (202)');
  assert.deepEqual(mealTabs({meals:[]}),[]);
  assert.deepEqual(mealTabs(null),[]);
});

test('nutrition detail lists the numbers a student asked for',()=>{
  const rows=nutritionRows({nutrition:{kcal:300,protein_g:10,fat_g:1.5,carb_g:57,sodium_mg:620}});
  assert.deepEqual(rows, [
    {label:'Calories',value:'300 kcal'},
    {label:'Protein',value:'10 g'},
    {label:'Carbs',value:'57 g'},
    {label:'Fat',value:'1.5 g'},
    {label:'Sodium',value:'620 mg'},
  ]);
});

test('unknown nutrition says so instead of showing a stale number',()=>{
  const rows=nutritionRows({nutrition:null});
  assert.equal(rows.length,1);
  assert.match(rows[0].value,/Not available/);
});

test('a blank allergen field is unknown, never "contains none"',()=>{
  assert.equal(allergenLine({allergens:['Wheat','Soybeans']}),'Contains: Wheat, Soybeans');
  assert.match(allergenLine({allergens:[],venue_allergen_free:false}),/unknown/);
  assert.match(allergenLine({allergens:[]}),/unknown/);
  assert.match(allergenLine({allergens:[],venue_allergen_free:true}),/allergen-free/);
});