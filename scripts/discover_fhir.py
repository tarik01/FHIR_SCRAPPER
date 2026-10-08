"""Read-only HAPI discovery. Persist aggregates only, never resource payloads."""
import collections
import datetime
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = 'https://hapi.fhir.org/baseR4'
results = []

def fetch(label, path, strict=False):
    url = path if path.startswith('https://') else BASE + path
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != 'hapi.fhir.org' or not (parsed.path == '/baseR4' or parsed.path.startswith('/baseR4/')):
        raise ValueError('Unexpected pagination origin: ' + str((parsed.scheme, parsed.netloc, parsed.path)))
    headers = {'Accept': 'application/fhir+json'}
    if strict:
        headers['Prefer'] = 'handling=strict'
    started = time.monotonic()
    info = {'query': label}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
            data = json.load(response)
            info.update(status=response.status, seconds=round(time.monotonic()-started, 2),
                        retry_after=response.headers.get('Retry-After'),
                        rate_headers={k:v for k,v in response.headers.items() if 'ratelimit' in k.lower()})
    except urllib.error.HTTPError as error:
        info.update(status=error.code, seconds=round(time.monotonic()-started,2))
        data = json.loads(error.read())
    except Exception as error:
        info.update(error=type(error).__name__, seconds=round(time.monotonic()-started,2))
        data = {}
    info['resource_type'] = data.get('resourceType')
    if 'total' in data:
        info['total'] = data['total']
    if data.get('resourceType') == 'OperationOutcome':
        info['issue_codes'] = [i.get('code') for i in data.get('issue', [])]
    results.append(info)
    print(json.dumps(info), flush=True)
    time.sleep(0.5)
    return data, info

def entries(bundle):
    return [e['resource'] for e in bundle.get('entry', []) if e.get('resource',{}).get('resourceType') in ('Patient','Observation')]

metadata, meta = fetch('GET /metadata', '/metadata')
meta.update(fhir_version=metadata.get('fhirVersion'), software=metadata.get('software'), formats=metadata.get('format'))
for rest in metadata.get('rest', []):
    meta['system_operations'] = [o.get('name') for o in rest.get('operation', [])]
    meta['resources'] = {r['type']: {'interactions':[i['code'] for i in r.get('interaction',[])], 'search_params':[p['name'] for p in r.get('searchParam',[])], 'operations':[o['name'] for o in r.get('operation',[])]} for r in rest.get('resource', []) if r['type'] in ('Patient','Observation')}
for kind in ('Patient','Observation'):
    fetch(f'GET /{kind}?_summary=count', f'/{kind}?_summary=count')
patients, pinfo = fetch('GET /Patient?_count=100', '/Patient?_count=100')
ps = entries(patients)
pinfo['sample'] = {'size':len(ps),'missing_name':sum(not p.get('name') for p in ps),'missing_birth_date':sum(not p.get('birthDate') for p in ps),'gender':dict(collections.Counter(p.get('gender','absent') for p in ps))}
next_url = next((l['url'] for l in patients.get('link',[]) if l.get('relation')=='next'), None)
pinfo['has_next'] = bool(next_url)
if next_url:
    page, pi = fetch('GET Patient next link (opaque cursor)', next_url)
    pi['returned'] = len(entries(page))
    pi['overlap_with_first_page'] = len({p['id'] for p in ps} & {p['id'] for p in entries(page)})
obs, oi = fetch('GET /Observation?_count=100', '/Observation?_count=100')
os = entries(obs)
oi['sample'] = {'size':len(os),'statuses':dict(collections.Counter(o.get('status','absent') for o in os)), 'value_types':dict(collections.Counter(k for o in os for k in o if k.startswith('value'))),'without_value':sum(not any(k.startswith('value') for k in o) for o in os),'components':sum(bool(o.get('component')) for o in os),'without_coding':sum(not o.get('code',{}).get('coding') for o in os),'subject_forms':dict(collections.Counter('absent_reference' if not o.get('subject',{}).get('reference') else ('absolute' if '://' in o['subject']['reference'] else o['subject']['reference'].split('/')[0]) for o in os))}
large, li = fetch('GET /Patient?_count=1000', '/Patient?_count=1000')
li['returned'] = len(entries(large))
sorted_b, si = fetch('GET /Patient?_sort=_lastUpdated&_count=10 (strict)', '/Patient?_sort=_lastUpdated&_count=10', True)
ts = [p.get('meta',{}).get('lastUpdated') for p in entries(sorted_b)]
si['returned'] = len(ts)
si['timestamps_nondecreasing'] = all(datetime.datetime.fromisoformat(a.replace('Z','+00:00')) <= datetime.datetime.fromisoformat(b.replace('Z','+00:00')) for a,b in zip(ts,ts[1:]) if a and b) if ts else None
fetch('GET /Patient?2026 lastUpdated window&_summary=count (strict)', '/Patient?_lastUpdated=ge2026-01-01&_lastUpdated=lt2027-01-01&_summary=count', True)
elements, ei = fetch('GET /Patient?_elements=id&_count=2 (strict)', '/Patient?_elements=id&_count=2', True)
ei['returned_field_sets'] = [sorted(p) for p in entries(elements)]
fetch('GET /Patient?unsupported parameter (strict)', '/Patient?discoveryUnsupportedParam=1&_count=1', True)
selected, sel = fetch('GET /Patient?_has:Observation:subject:status=final&_count=2 (strict)', '/Patient?_has:Observation:subject:status=final&_count=2', True)
ids = [p['id'] for p in entries(selected)]
if ids:
    refs = ','.join('Patient/'+i for i in ids)
    linked, linked_info = fetch('GET /Observation?subject=<2 selected patient references>&_count=5 (strict)', '/Observation?'+urllib.parse.urlencode({'subject':refs,'_count':5}), True)
    linked_info['returned'] = len(entries(linked))
    linked_info['all_subjects_match'] = all(o.get('subject',{}).get('reference') in {'Patient/'+i for i in ids} for o in entries(linked))
fetch('GET /Observation/_history?_count=1', '/Observation/_history?_count=1')
report = {'checked_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(), 'base_url':BASE, 'requests':results}
Path('discovery2-results.json').write_text(json.dumps(report, indent=2)+'\n')
print('Saved aggregate report to discovery2-results.json', flush=True)
