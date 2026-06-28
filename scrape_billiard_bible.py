"""
Scrape shot data from billiard-bible.com/shot-list/0 (분석샷 category).

Usage:
  python3 scrape_billiard_bible.py            # fetch known IDs + scroll to get more
  python3 scrape_billiard_bible.py --ids-only  # just print collected IDs
"""
import urllib.request, re, json, time, sys
from pathlib import Path

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,*/*',
    'Accept-Language': 'ko,en;q=0.9',
}
OUT = Path(__file__).parent / 'data' / 'billiard_bible_shots.json'
OUT.parent.mkdir(exist_ok=True)


def parse_positions(raw: str) -> list[dict]:
    """Parse 'frame|x|y|frame|x|y|...' into list of {frame, x, y}."""
    if not raw:
        return []
    parts = raw.split('|')
    result = []
    for i in range(0, len(parts) - 2, 3):
        try:
            result.append({'frame': int(parts[i]), 'x': float(parts[i+1]), 'y': float(parts[i+2])})
        except (ValueError, IndexError):
            pass
    return result


def fetch_shot(shot_id: int) -> dict | None:
    url = f'https://billiard-bible.com/shot-detail/0/{shot_id}'
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode(errors='replace')
    except Exception as e:
        print(f'  fetch error for {shot_id}: {e}')
        return None

    # Data is inside a Next.js RSC push call:
    # self.__next_f.push([1, "...\"shotDetailRawData\":{\"shot_id\":39495,...}..."])
    # Strategy: find the push call, extract the JS string, then json.loads to unescape.

    marker = f'\\"shot_id\\":{shot_id}'
    idx = html.find(marker)
    if idx < 0:
        print(f'  no shot_id found for {shot_id}')
        return None

    # Find the start of the push call string argument (opening quote after [1,)
    push_start = html.rfind('push([1,"', 0, idx)
    if push_start < 0:
        push_start = html.rfind('push([1,\n"', 0, idx)
    if push_start < 0:
        print(f'  push call not found for {shot_id}')
        return None
    # Start of the string content (after the opening quote)
    str_start = html.index('"', push_start + 8) + 1

    # Find end of the string (unescaped closing quote before ])
    # Scan for closing pattern "])" that's not escaped
    pos = str_start
    str_end = -1
    while pos < len(html) - 2:
        if html[pos] == '\\':
            pos += 2
            continue
        if html[pos] == '"' and html[pos+1:pos+3] in ('])', '])'):
            str_end = pos
            break
        pos += 1

    if str_end < 0:
        print(f'  could not find string end for {shot_id}')
        return None

    # Wrap in quotes so json.loads can unescape it properly
    raw_str = '"' + html[str_start:str_end] + '"'
    try:
        unescaped = json.loads(raw_str)
    except Exception:
        # Fallback: manual unescape
        unescaped = html[str_start:str_end].replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')

    # Now find the shot data JSON object in the unescaped string
    obj_marker = f'"shot_id":{shot_id}'
    obj_idx = unescaped.find(obj_marker)
    if obj_idx < 0:
        print(f'  shot data not found after unescape for {shot_id}')
        return None

    obj_start = unescaped.rfind('{', 0, obj_idx)
    depth = 0
    obj_end = obj_start
    for i in range(obj_start, min(obj_start + 15000, len(unescaped))):
        if unescaped[i] == '{': depth += 1
        elif unescaped[i] == '}':
            depth -= 1
            if depth == 0:
                obj_end = i + 1
                break

    raw_json = unescaped[obj_start:obj_end]

    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        # Try regex fallback
        data = {}
        for key in ['shot_id','youtube_id','rotation','ball_thickness','rail_speed',
                    'fade_out_frame','description_arrangement','description_tip',
                    'white_ball_positions','yellow_ball_positions','red_ball_positions',
                    'cue_positions','ball_contract_point','player_position','lecture_youtube_urls']:
            m = re.search(rf'"{key}"\s*:\s*("([^"\\]|\\.)*"|\d+)', raw_json)
            if m:
                val = m.group(1)
                data[key] = json.loads(val) if val.startswith('"') else int(val)

    if not data.get('shot_id'):
        return None

    # Parse position strings into arrays
    result = {
        'id': data.get('shot_id', shot_id),
        'youtube_id': data.get('youtube_id', ''),
        'shot_type': '',  # from categories if available
        'description': data.get('description_arrangement', ''),
        'tip': data.get('description_tip', ''),
        'ball_thickness': data.get('ball_thickness', 3),
        'rail_speed': data.get('rail_speed', 2),
        'lecture_url': data.get('lecture_youtube_urls', ''),
        'white': parse_positions(data.get('white_ball_positions', '')),
        'yellow': parse_positions(data.get('yellow_ball_positions', '')),
        'red': parse_positions(data.get('red_ball_positions', '')),
        'cue': parse_positions(data.get('cue_positions', '')),
        'contract_point': data.get('ball_contract_point', ''),
        'rotation': data.get('rotation', 0),
        'player_pos': data.get('player_position', ''),
        'fade_out_frame': data.get('fade_out_frame', 100),
    }

    # Extract categories from surrounding HTML
    cats_match = re.findall(r'"category_name"\s*:\s*"([^"]+)"', html)
    result['categories'] = cats_match

    return result


def get_next_shot_id(html: str, current_id: int) -> int | None:
    """Extract prev_shot_id from the page (shots are ordered newest→oldest, prev goes back further)."""
    # Field: \\"prev_shot_id\\":39486  (double-escaped in HTML)
    m = re.search(r'prev_shot_id["\\ ]*:\s*["\\ ]*(-?\d+)', html)
    if m:
        val = int(m.group(1))
        return val if val > 0 else None
    return None


def get_shot_ids_from_list() -> list[int]:
    """Fetch the list page and extract visible shot IDs."""
    url = 'https://billiard-bible.com/shot-list/0'
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode(errors='replace')

    ids = list(dict.fromkeys(int(m) for m in re.findall(r'/shot-detail/0/(\d+)', html)))
    return ids


def crawl_all(shots: list, done_ids: set, start_id: int, max_shots: int = 2000) -> list:
    """Follow prev-shot chain starting from start_id, collecting all shots."""
    current_id = start_id
    consecutive_fails = 0

    while current_id and len(shots) < max_shots:
        if current_id in done_ids:
            # Already have it - still need to find next pointer
            url = f'https://billiard-bible.com/shot-detail/0/{current_id}'
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=15) as r:
                    html = r.read().decode(errors='replace')
                next_id = get_next_shot_id(html, current_id)
                print(f'  #{current_id} already have, next → {next_id}')
                current_id = next_id
                time.sleep(0.3)
            except Exception as e:
                print(f'  #{current_id} error getting next: {e}')
                current_id = None
            continue

        print(f'[{len(shots)+1}] Fetching #{current_id}...', end=' ', flush=True)
        url = f'https://billiard-bible.com/shot-detail/0/{current_id}'
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                html = r.read().decode(errors='replace')
        except Exception as e:
            print(f'network error: {e}')
            consecutive_fails += 1
            if consecutive_fails >= 5:
                print('Too many failures, stopping.')
                break
            time.sleep(2)
            continue

        shot = fetch_shot(current_id)
        next_id = get_next_shot_id(html, current_id)

        if shot:
            shots.append(shot)
            done_ids.add(current_id)
            consecutive_fails = 0
            print(f'ok — {shot["description"][:45]} | next={next_id}')
        else:
            print(f'parse failed | next={next_id}')
            done_ids.add(current_id)  # mark as attempted

        if len(shots) % 10 == 0 and len(shots) > 0:
            shots.sort(key=lambda s: s['id'], reverse=True)
            with open(OUT, 'w', encoding='utf-8') as f:
                json.dump(shots, f, ensure_ascii=False, indent=2)
            print(f'  → Saved {len(shots)} shots so far')

        current_id = next_id
        time.sleep(0.5)

    return shots


def main():
    # Load existing data
    if OUT.exists():
        with open(OUT, encoding='utf-8') as f:
            shots = json.load(f)
        done_ids = {s['id'] for s in shots}
    else:
        shots = []
        done_ids = set()

    # Seed IDs: args or known starting point
    seed_ids = [int(x) for x in sys.argv[1:] if x.isdigit()]

    if not seed_ids:
        # Find the highest known ID to start from, or use known starting point
        if shots:
            start_id = max(s['id'] for s in shots)
        else:
            # Try to get start ID from list page
            print('Fetching shot list page for start ID...')
            list_ids = get_shot_ids_from_list()
            start_id = max(list_ids) if list_ids else 39495
        print(f'Starting chain crawl from #{start_id}')
        shots = crawl_all(shots, done_ids, start_id)
    else:
        # Manual IDs provided: fetch those first, then chain from lowest
        for sid in seed_ids:
            if sid not in done_ids:
                print(f'Fetching seed #{sid}...', end=' ', flush=True)
                shot = fetch_shot(sid)
                if shot:
                    shots.append(shot)
                    done_ids.add(sid)
                    print(f'ok')
                else:
                    print('skip')
                time.sleep(0.5)
        # Chain from lowest seed
        start_id = min(seed_ids)
        shots = crawl_all(shots, done_ids, start_id)

    shots.sort(key=lambda s: s['id'], reverse=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(shots, f, ensure_ascii=False, indent=2)
    print(f'\nDone! {len(shots)} shots saved to {OUT}')


if __name__ == '__main__':
    main()
