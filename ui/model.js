// Small pure helpers are shared by UI and contract tests.
export function planHeading(answer) {
  if (answer?.clarification) return 'I need one detail.';
  if (!answer) return 'A gap in your day?';
  if (answer.feasible === false) return 'No plan fits.';
  if (!answer.itinerary?.legs?.length) return 'No usable plan yet.';
  if (answer.itinerary.arrives_in_window === false) return 'No plan fits.';
  return answer.feasible === true ? 'A little time, well spent.' : 'Check this plan.';
}
export function allergenLabel(leg) {
  if (leg.allergens?.length) return `Declared allergens: ${leg.allergens.join(', ')}`;
  if (leg.venue_allergen_free === true) return 'Documented allergen-free kitchen (Viridian)';
  return 'Allergens UNKNOWN';
}
export function numberOrDash(value) { return typeof value === 'number' && Number.isFinite(value) ? String(value) : '—'; }
export function sourceForLeg(leg) { return leg.type === 'bus' ? 'Scheduled · service-filtered' : leg.type === 'eat' ? 'VT menu API · source data' : 'Estimated · straight-line × 1.30'; }
