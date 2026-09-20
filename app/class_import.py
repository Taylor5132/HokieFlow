"""Read-only adapters for Taylor's class contracts and account-calendar imports."""
from datetime import date, datetime
from hashlib import sha256
from dataclasses import replace
from hokieday import classes, config


def sources():
    roots = [config.DATA_DIR / 'classes', config.FIXTURES_DIR]
    found = []
    for root in roots:
        for path in root.glob('*.json'):
            try:
                found.append(classes.load_snapshot(path))
            except (ValueError, OSError, TypeError, KeyError):
                continue
        if found:
            break
    return sorted(found, key=lambda s: s.fetched_at, reverse=True)


def crosswalk():
    path = config.FIXTURES_DIR / 'classes_buildings.html'
    return {b.code: b for b in classes.parse_building_list_html(path.read_text())} if path.exists() else {}


def search_endpoint(query):
    term = str(query.get('term', '202609'))
    snapshots = [s for s in sources() if s.term == term]
    if not snapshots:
        return {'state': 'unavailable', 'sections': [], 'notices': ['No timetable snapshot is available for this term. Import a calendar or enter your classes manually.']}
    sections, seen, notices, metadata = [], set(), [], []
    for snapshot in snapshots:
        parsed = classes.snapshot_sections(snapshot, crosswalk())
        filtered = classes.search(parsed.sections, term=term,
                                  subject=query.get('subject') or None,
                                  course_number=query.get('course_number') or None,
                                  crn=query.get('crn') or None)
        # Newest (term, CRN) wins even if older snapshots contain changed details.
        filtered = [s for s in filtered if (s.term, s.crn) not in seen]
        seen.update((s.term, s.crn) for s in parsed.sections)
        contract = classes.search_result_json(replace(parsed, sections=tuple(filtered)), snapshot, now=config.now())
        sections.extend(contract['sections'])
        metadata.append(contract['snapshot'])
        notices.extend(contract['warnings'] + contract['errors'])
    return {'state': 'results' if sections else 'no_results', 'sections': sections[:100],
            'snapshots': metadata, 'notices': list(dict.fromkeys(notices)),
            'partial': True}


def preview_endpoint(payload):
    """Normalize exact dated meetings; never persist an uploaded ICS here."""
    start, end = date.fromisoformat(payload['start']), date.fromisoformat(payload['end'])
    if end < start or (end-start).days > 180:
        raise ValueError('Choose a date range of at most 180 days.')
    warnings, sections, ics, snapshot = [], [], [], None
    consent = payload.get('allow_term_assumption') is True
    if payload.get('ics') is not None:
        text = payload['ics']
        if not isinstance(text, str):
            raise ValueError('Upload an .ics calendar file.')
        parsed = classes.parse_ics(text)
        if parsed.errors:
            return {'state': 'malformed_ics', 'events': [], 'warnings': list(parsed.warnings), 'errors': list(parsed.errors)}
        ics = list(parsed.events)
        warnings.extend(parsed.warnings)
        records = classes.add_to_schedule([], ics_events=ics)['schedule']
    else:
        term, crn = str(payload.get('term', '')), str(payload.get('crn', ''))
        for candidate in sources():
            if candidate.term != term:
                continue
            matches = classes.search(classes.snapshot_sections(candidate, crosswalk()).sections, term=term, crn=crn)
            if matches:
                sections, snapshot = matches, candidate
                break
        if not sections:
            raise ValueError('This class is no longer in the available timetable. Search again.')
        ok, reason = classes.term_expandability(term)
        if not ok:
            raise ValueError(reason)
        if not consent:
            return {'state': 'recurrence_unavailable', 'events': [], 'errors': ['Confirm the term-long weekly pattern, or import your calendar instead.']}
        if any(s.has_tba for s in sections):
            raise ValueError('This section has an arranged or unknown meeting time. Enter it manually when your time is confirmed.')
        warnings.append('Weekly dates are inferred from the published term and skip known holidays. Check any short-session dates with your instructor.')
        if classes.snapshot_is_stale(snapshot, now=config.now()):
            warnings.append('Timetable snapshot is older than six hours. Verify details before saving.')
        records = classes.add_to_schedule([], sections, [crn], term=term)['schedule']
    contract = classes.schedule_json(records, sections, start=start, end=end, ics_events=ics,
                                     snapshot=snapshot, now=config.now(), allow_term_assumption=consent)
    if contract['state'] == 'bounds_exceeded':
        return {**contract, 'events': [], 'errors': [contract['reason']]}
    occurrences = classes.combined_occurrences(sections, ics, start=start, end=end, allow_term_assumption=consent)
    if len(occurrences) > 200:
        raise ValueError('More than 200 meetings in this range. Choose a shorter range or fewer classes.')
    rows = []
    for o in occurrences:
        key = repr((o.identity, o.start.isoformat()))
        building = next((s.building_names.get(o.meeting.building) for s in sections if s.crn == o.crn), None)
        location = (f'{building} {o.meeting.room or ""}'.strip() if building else o.meeting.location_raw)
        rows.append({'id': 'class_' + sha256(key.encode()).hexdigest()[:32],
                     'title': (f'{o.course} · {o.title}' if o.course else o.title)[:100],
                     'kind': 'class', 'location': location[:150], 'start': o.start.isoformat(),
                     'end': o.end.isoformat(), 'repeat': 'none', 'repeatUntil': None})
    return {**contract, 'events': rows, 'warnings': warnings, 'errors': [],
            'next_class': classes.next_class_json(sections, config.now(), start=start, end=end,
                                                  ics_events=ics, allow_term_assumption=consent)}
