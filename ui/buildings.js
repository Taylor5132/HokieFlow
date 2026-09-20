// The destination list requested by the UI owner. No GIS catalog is queried.
export const buildingGroups = [
 ['Academic & Research', [
  'Agnew Hall','Architecture Annex','Bioinformatics Facility','Bishop-Favrao Hall','Burruss Hall','Cheatham Hall','Cowgill Hall','Davidson Hall','Derring Hall','Durham Hall','Fralin Hall','Goodwin Hall','Hancock Hall','Hitt Hall','Holden Hall','Litton-Reaves Hall','Major Williams Hall','McBryde Hall','Norris Hall','Pamplin Hall','Patton Hall','Price Hall','Robeson Hall','Sandy Hall','Saunders Hall','Seitz Hall','Shanks Hall','Smyth Hall','Torgersen Hall','Wallace Hall','Whittemore Hall','Williams Hall'
 ]],
 ['Residence Halls', [
  'Ambler Johnston Hall — East','Ambler Johnston Hall — West','Barringer Hall','Campbell Hall — East','Campbell Hall — West','Cochrane Hall','Creativity and Innovation District (CID) Residence Hall','Eggleston Hall — East','Eggleston Hall — West','Eggleston Hall — Main','Harper Hall','Hoge Hall','Johnson Hall','Miles Hall','New Hall West','Newman Hall',"O’Shaughnessy Hall",'Payne Hall','Pearson Hall','New Cadet Hall','Peddrew-Yates Hall','Pratt Hall','Slusher Hall — Tower','Slusher Hall — Wing','Vawter Hall'
 ]],
 ['Athletic & Student Life', [
  'Cassell Coliseum','Dietrick Hall','Graduate Life Center (GLC) at Donaldson Brown','Hannover House','Lane Stadium','McComas Hall','Newman Library','Owens Hall','Squires Student Center','Student Wellness Center','The Moss Arts Center','War Memorial Gym'
 ]]
];
export const buildings = buildingGroups.flatMap(([group,names])=>names.map(name=>({id:name.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/-$/,''),name,group})));
const normalize=s=>String(s||'').toLowerCase().replace(/[^a-z0-9]/g,'');
export function matchBuilding(hint){const query=normalize(hint);if(!query)return null;return buildings.find(b=>normalize(b.name)===query)||buildings.find(b=>query.includes(normalize(b.name))||normalize(b.name).includes(query))||null;}
