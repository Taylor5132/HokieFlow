/** SYNTHETIC DESIGN FIXTURES. These are not the Python repository's captured data.
 * Shapes follow the supplied functionality matrix. Every preview screen labels this.
 * No weather, events, class schedule, exact bus ETA, or trained predictions exist here.
 */
const stamp = '2026-09-19T11:22:29-04:00';
export const previewTime = { mode:'cache', is_replay:true, pinned:true, ticking:false, time_source:'snapshot', evaluated_at:stamp, iso:stamp, human:'11:22 AM', weekday:'Sat 19 Sep 2026', timezone:'America/New_York' };
const legs = [
  { seq:1,type:'walk',from:'Burruss',to:'D2',start_time:stamp,minutes:8,method:'Straight-line × 1.30 estimate',coords_verified:false },
  { seq:2,type:'eat',place:'D2 at Dietrick Hall',location_num:'15',start_time:'2026-09-19T11:30:29-04:00',minutes:20,item:'Example lunch selection',portion:'1 portion',kcal:null,protein_g:null,allergens:[],allergens_known:false,venue_allergen_free:false,diet_tags:[],method:'Assumed eating time' },
  { seq:3,type:'walk',from:'D2',to:'McBryde',start_time:'2026-09-19T11:50:29-04:00',minutes:12,method:'Straight-line × 1.30 estimate',coords_verified:false },
];
const plan = { feasible:true,rationale:'This illustrative walking plan leaves time before the selected deadline.',itinerary:{ legs,leave_time:stamp,arrive_time:'2026-09-19T12:02:29-04:00',window_end:'2026-09-19T13:25:00-04:00',total_min:40,slack_min:82.5,arrives_in_window:true,used_bus:false }, _time:previewTime,_interpretation_notes:['Illustrative window: 11:22 AM to 1:25 PM. No class timetable is connected.'],constraints:{from_place:'Burruss',to_place:'McBryde',diet:'none',avoid:[]},_links:[] };
export const previewStates = {
  fits:plan,
  replan:{...plan,replan_trigger:{cause:'bus_early',detail:'Illustrative snapshot: the bus is early, leaving too little boarding time. A walking alternative is shown.'},alternatives:[{type:'previous_itinerary_a',itinerary:{...plan.itinerary,arrive_time:'2026-09-19T11:37:29-04:00',total_min:15,slack_min:107.5,used_bus:true,legs:[{seq:1,type:'bus',route_id:'EXAMPLE',from_stop_name:'Origin stop',to_stop_name:'Destination stop',start_time:stamp,minutes:15,is_realtime:false,method:'GTFS schedule (service-filtered)'}]}}]},
  late:{...plan,_interpretation_notes:['Illustrative deadline: 11:45 AM. No class timetable is connected.'],feasible:false,rationale:'The closest illustrative option misses the deadline.',infeasible_reason:{code:'deadline_missed',late_by_min:17},itinerary:{...plan.itinerary,window_end:'2026-09-19T11:45:29-04:00',slack_min:-17,arrives_in_window:false}},
  clarify:{feasible:false,clarification:{kind:'need_deadline',question:'What time do you need to arrive by?',detail:'A deadline is needed before the planner can check whether a trip fits.'},infeasible_reason:{code:'clarification_needed'},_time:previewTime},
  passed:{feasible:false,clarification:{kind:'deadline_passed',question:'That time has passed. Do you mean a later time, or tomorrow?',detail:'The planner will not silently move your deadline to tomorrow.'},_time:previewTime},
  unknown:{feasible:false,infeasible_reason:{code:'unknown_place',value:'Narnia',known_places:['Burruss','Stop 1600','McBryde','Hahn','D2','West End','Owens','Squires']},_time:previewTime},
  stale:{...plan,notes:['Transit snapshot is stale. This plan uses walking estimates; no live vehicle claim is made.'],_ui_stale:true},
};
export const sampleVehicle = {bus_id:'EXAMPLE',route_id:'EXAMPLE',load_pct:30,sched_delta_min:-1.4,observed_at:stamp,is_stale:false};
