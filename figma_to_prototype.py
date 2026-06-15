#!/usr/bin/env python3
"""
figma_to_prototype.py
---------------------
Downloads a Figma file and converts it into a verified, self-contained HTML prototype.

Handles all three Figma interaction types automatically:
  NAVIGATE     -> clickable hotspot that navigates to another screen
  CHANGE_TO    -> visual toggle (checkbox, selected state) within same screen
  AFTER_TIMEOUT -> auto-advance timer between screens

Usage (from a Figma URL - recommended):
  python3 figma_to_prototype.py \
      --figma-url   "https://www.figma.com/design/ABC123/Name" \
      --figma-token figd_xxx \
      --output-dir  ./my_prototype

Usage (from a pre-downloaded JSON):
  python3 figma_to_prototype.py \
      --figma-file  figma_file.json \
      --figma-token figd_xxx \
      --file-key    ABC123 \
      --output-dir  ./my_prototype

  # Or with pre-downloaded images:
  python3 figma_to_prototype.py \
      --figma-file  figma_file.json \
      --images-dir  ./my_images \
      --output-dir  ./my_prototype
"""

import argparse, json, os, sys, time, io, zipfile
import urllib.request, urllib.error, urllib.parse

# --- ENV LOADER --------------------------------------------------------------

def load_env(path='.env'):
    """Parse a simple KEY=VALUE .env file into os.environ (no-op if missing)."""
    if not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, val = line.partition('=')
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)

# --- DOWNLOAD HELPERS --------------------------------------------------------

def extract_file_key(figma_url):
    parsed = urllib.parse.urlparse(figma_url)
    if parsed.netloc not in {'figma.com', 'www.figma.com'}:
        raise ValueError("URL must be a figma.com URL")
    parts = [p for p in parsed.path.split('/') if p]
    if len(parts) >= 2 and parts[0] in {'design', 'file', 'proto'}:
        return parts[1]
    raise ValueError(f"Could not extract file key from URL: {figma_url}")


def fetch_figma_json(file_key, token):
    url = f"https://api.figma.com/v1/files/{file_key}"
    req = urllib.request.Request(url, headers={'X-Figma-Token': token})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', 'replace')
        raise RuntimeError(f"Figma API error {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


def save_and_validate_json(data, path):
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, ensure_ascii=True, indent=2)
        f.write('\n')
    # validate by reading back
    with open(path, 'r', encoding='utf-8') as f:
        json.load(f)
    print(f"   JSON saved and validated: {path}")


# --- STEP 1: EXTRACT FRAMES --------------------------------------------------

def extract_frames(figma_data):
    frames = {}
    doc = figma_data.get('document', {})
    for page in doc.get('children', []):
        for node in page.get('children', []):
            _walk_frames(node, frames, depth=0)
    return frames

def _walk_frames(node, frames, depth):
    if not isinstance(node, dict): return
    if node.get('type') in ('FRAME', 'COMPONENT', 'INSTANCE') and depth <= 1:
        bb = node.get('absoluteBoundingBox') or {}
        frames[node['id']] = {
            'id':     node['id'],
            'name':   node['name'],
            'x':      bb.get('x', 0),
            'y':      bb.get('y', 0),
            'width':  bb.get('width', 393),
            'height': bb.get('height', 852),
        }
    for child in (node.get('children') or []):
        _walk_frames(child, frames, depth + 1)

# --- STEP 2: EXTRACT ALL INTERACTIONS ----------------------------------------

def extract_interactions(figma_data, frame_ids):
    """
    Returns:
      hotspots: frame_id -> [{element, x, y, w, h, destination_id}]  (NAVIGATE)
      toggles:  frame_id -> [{element, x, y, w, h}]                  (CHANGE_TO)
      timeouts: frame_id -> {destination_id, timeout_ms}              (AFTER_TIMEOUT)
    """
    hotspots = {}
    toggles  = {}
    timeouts = {}

    doc = figma_data.get('document', {})
    for page in doc.get('children', []):
        for node in page.get('children', []):
            _collect_interactions(node, frame_ids, hotspots, toggles, timeouts, depth=0)

    return hotspots, toggles, timeouts

def _collect_interactions(node, frame_ids, hotspots, toggles, timeouts, parent_frame_id=None, ox=0, oy=0, depth=0):
    if not isinstance(node, dict) or depth > 30: return

    is_frame = node.get('type') in ('FRAME', 'COMPONENT', 'INSTANCE') and node['id'] in frame_ids
    if is_frame:
        parent_frame_id = node['id']
        bb = node.get('absoluteBoundingBox') or {}
        ox, oy = bb.get('x', 0), bb.get('y', 0)

    if parent_frame_id:
        for interaction in (node.get('interactions') or []):
            if not isinstance(interaction, dict): continue
            trigger_obj = interaction.get('trigger') or {}
            trigger     = trigger_obj.get('type', '')
            raw_ms      = trigger_obj.get('timeout', 2000)

            for action in (interaction.get('actions') or []):
                if not isinstance(action, dict): continue
                nav  = action.get('navigation', '')
                dest = action.get('destinationId', '')
                bb   = node.get('absoluteBoundingBox') or {}
                x    = round((bb.get('x') or 0) - ox)
                y    = round((bb.get('y') or 0) - oy)
                w    = round(bb.get('width') or 0)
                h    = round(bb.get('height') or 0)
                if w <= 0 or h <= 0: continue

                if nav == 'NAVIGATE' and dest in frame_ids and trigger in ('ON_CLICK', 'ON_TAP', 'ON_PRESS'):
                    hotspots.setdefault(parent_frame_id, [])
                    key = (x, y, w, h, dest)
                    if not any((s['x'],s['y'],s['w'],s['h'],s['destination_id']) == key for s in hotspots[parent_frame_id]):
                        hotspots[parent_frame_id].append({
                            'element': node.get('name', ''), 'x': x, 'y': y, 'w': w, 'h': h,
                            'destination_id': dest
                        })

                elif nav == 'CHANGE_TO' and trigger in ('ON_CLICK', 'ON_TAP', 'ON_PRESS'):
                    toggles.setdefault(parent_frame_id, [])
                    key = (x, y, w, h)
                    if not any((t['x'],t['y'],t['w'],t['h']) == key for t in toggles[parent_frame_id]):
                        toggles[parent_frame_id].append({
                            'element': node.get('name', ''), 'x': x, 'y': y, 'w': w, 'h': h
                        })

                elif trigger == 'AFTER_TIMEOUT' and dest in frame_ids:
                    actual_ms = int(raw_ms * 1000) if raw_ms < 100 else int(raw_ms)
                    if parent_frame_id not in timeouts:
                        timeouts[parent_frame_id] = {'destination_id': dest, 'timeout_ms': actual_ms}

    for child in (node.get('children') or []):
        if isinstance(child, dict):
            _collect_interactions(child, frame_ids, hotspots, toggles, timeouts, parent_frame_id, ox, oy, depth + 1)

# --- STEP 3: DETECT ENTRY FRAME ----------------------------------------------

def detect_entry_frame(frames, hotspots):
    all_dests = {s['destination_id'] for spots in hotspots.values() for s in spots}
    candidates = [fid for fid in frames if fid not in all_dests]
    if candidates:
        for fid in candidates:
            name = frames[fid]['name'].lower()
            if any(x in name for x in ['splash', 'version 6', 'v6', 'start', 'launch']):
                return fid
        return candidates[0]
    return list(frames.keys())[0]

# --- STEP 4: DOWNLOAD IMAGES -------------------------------------------------

def download_images(frame_ids, file_key, token, img_dir):
    os.makedirs(img_dir, exist_ok=True)
    image_map = {}
    ids = list(frame_ids)

    for i in range(0, len(ids), 20):
        batch = ids[i:i+20]
        dash_ids = ','.join(f.replace(':', '-') for f in batch)
        url = f"https://api.figma.com/v1/images/{file_key}?ids={dash_ids}&format=png&scale=2"
        req = urllib.request.Request(url, headers={'X-Figma-Token': token})
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(f"  x API error {e.code}: {e.read()}")
            continue

        for dash_id, img_url in (data.get('images') or {}).items():
            colon_id = dash_id.replace('-', ':')
            fid = colon_id if colon_id in frame_ids else dash_id
            if not img_url: continue
            filename = f"frame_{fid.replace(':', '_')}.png"
            filepath = os.path.join(img_dir, filename)
            try:
                urllib.request.urlretrieve(img_url, filepath)
                image_map[fid] = f"images/{filename}"
                print(f"  + {filename}")
            except Exception as e:
                print(f"  x {fid}: {e}")
        time.sleep(0.5)

    return image_map

def use_local_images(frame_ids, src_dir, out_dir):
    import shutil
    os.makedirs(out_dir, exist_ok=True)
    image_map = {}
    for fid in frame_ids:
        filename = f"frame_{fid.replace(':', '_')}.png"
        src = os.path.join(src_dir, filename)
        dst = os.path.join(out_dir, filename)
        if os.path.exists(src):
            shutil.copy2(src, dst)
            image_map[fid] = f"images/{filename}"
    return image_map

# --- STEP 5: BUILD HTML -------------------------------------------------------

def build_html(frames, hotspots, toggles, timeouts, image_map, entry_id):
    screens_html = ""

    for fid, frame in frames.items():
        w = round(frame['width'])
        h = round(frame['height'])
        img_src   = image_map.get(fid, '')
        spots     = hotspots.get(fid, [])
        togs      = toggles.get(fid, [])
        timeout   = timeouts.get(fid)
        is_active = 'active' if fid == entry_id else ''
        auto_attr = f'data-auto="{timeout["destination_id"]}" data-ms="{timeout["timeout_ms"]}"' if timeout else ''

        hs_html = ""
        for s in spots:
            el        = s['element'].replace('"', '&quot;')
            dest_name = frames.get(s['destination_id'], {}).get('name', s['destination_id'])
            hs_html  += (
                f'\n    <div class="hs" onclick="go(\'{s["destination_id"]}\')"'
                f' data-dest="{s["destination_id"]}" data-element="{el}"'
                f' title="{el} -> {dest_name}"'
                f' style="left:{s["x"]}px;top:{s["y"]}px;width:{s["w"]}px;height:{s["h"]}px;"></div>'
            )
        for t in togs:
            el       = t['element'].replace('"', '&quot;')
            hs_html += (
                f'\n    <div class="hs toggle" onclick="handleToggle(this)"'
                f' data-element="{el}" data-toggled="false"'
                f' title="{el} (toggle)"'
                f' style="left:{t["x"]}px;top:{t["y"]}px;width:{t["w"]}px;height:{t["h"]}px;"></div>'
            )

        screens_html += (
            f'\n  <div id="s{fid.replace(":","_")}" class="screen {is_active}"'
            f' data-id="{fid}" data-w="{w}" data-h="{h}" {auto_attr}>'
            f'\n    <img src="{img_src}" alt="{frame["name"]}" draggable="false" loading="lazy" />'
            f'{hs_html}'
            f'\n  </div>'
        )

    frames_js = json.dumps({
        fid: {'name': f['name'], 'w': round(f['width']), 'h': round(f['height'])}
        for fid, f in frames.items()
    })

    total_nav = sum(len(v) for v in hotspots.values())
    total_tog = sum(len(v) for v in toggles.values())

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Prototype</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:#111;display:flex;flex-direction:column;align-items:center;padding:12px;font-family:-apple-system,BlinkMacSystemFont,sans-serif;min-height:100vh;}}
#toolbar{{width:100%;max-width:500px;display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;}}
#frame-label{{color:#777;font-size:11px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
.tbtn{{background:#222;color:#888;border:1px solid #333;padding:5px 12px;border-radius:6px;font-size:11px;cursor:pointer;}}
.tbtn:hover{{background:#2a2a2a;color:#fff;}}
.tbtn.on{{background:#1a3a5c;color:#7cb8ff;border-color:#2a5a8c;}}
#bc{{width:100%;max-width:500px;display:flex;flex-wrap:wrap;gap:3px;align-items:center;margin-bottom:8px;min-height:14px;font-size:10px;}}
.crumb{{color:#444;cursor:pointer;}}.crumb:hover{{color:#999;}}.crumb.cur{{color:#ccc;}}
.sep{{color:#333;}}
#device-wrap{{transform-origin:top center;position:relative;}}
#device{{position:relative;background:#000;overflow:hidden;border-radius:44px;box-shadow:0 0 0 2px #2a2a2a,0 20px 60px rgba(0,0,0,0.8);}}
.screen{{display:none;position:absolute;inset:0;width:100%;height:100%;}}
.screen.active{{display:block;}}
.screen img{{position:absolute;inset:0;width:100%;height:100%;object-fit:fill;display:block;pointer-events:none;user-select:none;-webkit-user-drag:none;}}
.hs{{position:absolute;cursor:pointer;z-index:10;border-radius:4px;-webkit-tap-highlight-color:transparent;}}
.hs:active{{background:rgba(255,255,255,0.08);}}
.hs.toggle{{z-index:11;}}
.hs.toggle[data-toggled="true"]{{background:rgba(0,180,100,0.12);outline:2px solid rgba(0,180,100,0.4);}}
body.debug .hs{{background:rgba(80,180,255,0.15);outline:1.5px solid rgba(80,180,255,0.55);}}
body.debug .hs.toggle{{background:rgba(255,180,0,0.15);outline:1.5px solid rgba(255,180,0,0.55);}}
body.debug .hs:hover{{background:rgba(80,180,255,0.3);outline:2px solid rgba(80,180,255,0.9);}}
body.debug .hs.toggle:hover{{background:rgba(255,180,0,0.3);outline:2px solid rgba(255,180,0,0.9);}}
body.debug .hs:hover::after,body.debug .hs.toggle:hover::after{{content:attr(data-element);position:absolute;bottom:100%;left:0;background:rgba(0,0,0,0.85);font-size:8px;padding:2px 4px;border-radius:3px;white-space:nowrap;pointer-events:none;z-index:99;}}
body.debug .hs:hover::after{{color:#7cb8ff;}}
body.debug .hs.toggle:hover::after{{color:#ffb300;}}
#toast{{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,0.8);color:#fff;padding:6px 16px;border-radius:16px;font-size:11px;opacity:0;transition:opacity 0.2s;pointer-events:none;z-index:9999;white-space:nowrap;}}
#toast.show{{opacity:1;}}
</style>
</head>
<body>
<div id="toolbar">
  <span id="frame-label">Loading...</span>
  <div style="display:flex;gap:5px;">
    <button class="tbtn on" id="dbgBtn" onclick="toggleDebug()">Hide Hotspots</button>
    <button class="tbtn" onclick="goBack()">Back</button>
    <button class="tbtn" onclick="restart()">Restart</button>
  </div>
</div>
<div id="bc"></div>
<div id="device-wrap"><div id="device">{screens_html}</div></div>
<div id="toast"></div>
<script>
const FRAMES={frames_js};
const ENTRY="{entry_id}";
let cur=ENTRY,hist=[ENTRY],hnames=[FRAMES[ENTRY]?.name||''],autoTimer=null,debug=true;
document.body.classList.add('debug');
function resize(w,h){{
  const mw=Math.min(window.innerWidth-24,500),mh=window.innerHeight-130,s=Math.min(mw/w,mh/h);
  const dv=document.getElementById('device'),wr=document.getElementById('device-wrap');
  dv.style.width=w+'px';dv.style.height=h+'px';dv.style.borderRadius=(w>h?20:44)+'px';
  wr.style.width=Math.round(w*s)+'px';wr.style.height=Math.round(h*s)+'px';wr.style.transform=`scale(${{s}})`;
}}
function go(fid){{
  if(!FRAMES[fid]){{toast('Unknown: '+fid);return;}}
  clearTimeout(autoTimer);
  document.querySelector('.screen.active')?.classList.remove('active');
  const el=document.querySelector(`.screen[data-id="${{fid}}"]`);
  if(!el){{toast('Missing: '+fid);return;}}
  el.classList.add('active');
  const f=FRAMES[fid];resize(f.w,f.h);
  if(fid!==cur){{hist.push(fid);hnames.push(f.name);}}
  cur=fid;document.getElementById('frame-label').textContent=f.name;renderBC();
  if(el.dataset.auto) autoTimer=setTimeout(()=>go(el.dataset.auto),parseInt(el.dataset.ms||2000));
}}
function handleToggle(el){{
  el.dataset.toggled=String(el.dataset.toggled!=='true');
}}
function renderBC(){{
  const bc=document.getElementById('bc');bc.innerHTML='';
  const trail=hist.slice(-8),names=hnames.slice(-8);
  trail.forEach((fid,i)=>{{
    if(i>0){{const s=document.createElement('span');s.className='sep';s.textContent='>';bc.appendChild(s);}}
    const c=document.createElement('span');c.className='crumb'+(i===trail.length-1?' cur':'');c.textContent=names[i];
    const idx=hist.length-trail.length+i;
    c.onclick=()=>{{hist=hist.slice(0,idx+1);hnames=hnames.slice(0,idx+1);cur=fid;
      go(fid);hist.pop();hnames.pop();hist.push(fid);hnames.push(FRAMES[fid]?.name||'');}};
    bc.appendChild(c);
  }});
}}
function goBack(){{
  if(hist.length<=1){{toast('Already at start');return;}}
  hist.pop();hnames.pop();const prev=hist[hist.length-1];
  clearTimeout(autoTimer);cur=prev;
  document.querySelector('.screen.active')?.classList.remove('active');
  const el=document.querySelector(`.screen[data-id="${{prev}}"]`);
  if(el){{el.classList.add('active');const f=FRAMES[prev];resize(f.w,f.h);document.getElementById('frame-label').textContent=f.name;renderBC();}}
}}
function restart(){{
  clearTimeout(autoTimer);
  document.querySelectorAll('.hs.toggle').forEach(t=>t.dataset.toggled='false');
  hist=[ENTRY];hnames=[FRAMES[ENTRY]?.name||''];cur=ENTRY;
  go(ENTRY);hist=[ENTRY];hnames=[FRAMES[ENTRY]?.name||''];
}}
function toggleDebug(){{
  debug=!debug;document.body.classList.toggle('debug',debug);
  const btn=document.getElementById('dbgBtn');btn.textContent=debug?'Hide Hotspots':'Show Hotspots';btn.classList.toggle('on',debug);
}}
function toast(msg){{const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2500);}}
window.addEventListener('load',()=>{{
  const missing=Object.keys(FRAMES).filter(f=>!document.querySelector(`.screen[data-id="${{f}}"]`));
  if(missing.length) console.error('MISSING SCREENS:',missing);
  else console.log('OK {len(frames)} screens | {total_nav} nav hotspots | {total_tog} toggles | {len(timeouts)} auto-advances');
}});
go(ENTRY);hist=[ENTRY];hnames=[FRAMES[ENTRY]?.name||''];
</script>
</body>
</html>"""

# --- STEP 6: VALIDATE --------------------------------------------------------

def validate(frames, hotspots, toggles, timeouts, image_map, html):
    issues = []
    for fid, frame in frames.items():
        if f'data-id="{fid}"' not in html:
            issues.append(f"MISSING SCREEN: {frame['name']} [{fid}]")
        if fid not in image_map:
            issues.append(f"MISSING IMAGE: {frame['name']} [{fid}]")
        for s in hotspots.get(fid, []):
            if f"go('{s['destination_id']}')" not in html:
                issues.append(f"MISSING LINK: {frame['name']} -> {s['element']} -> {s['destination_id']}")
        for t in toggles.get(fid, []):
            if 'handleToggle' not in html:
                issues.append("handleToggle missing from JS")
                break
        to = timeouts.get(fid)
        if to and f'data-auto="{to["destination_id"]}"' not in html:
            issues.append(f"MISSING TIMEOUT: {frame['name']}")
    return issues

# --- STEP 7: NETLIFY DEPLOY --------------------------------------------------

def _netlify_request(method, path, token, data=None, content_type='application/json'):
    url = f"https://api.netlify.com/api/v1{path}"
    body = None
    if data is not None:
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method, headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': content_type,
    })
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', 'replace')
        raise RuntimeError(f"Netlify API error {exc.code}: {body}") from exc


def _zip_directory(directory):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(directory):
            for fname in files:
                full = os.path.join(root, fname)
                arcname = os.path.relpath(full, directory)
                zf.write(full, arcname)
    return buf.getvalue()


def deploy_to_netlify(output_dir, token):
    print('\n7. Deploying to Netlify...')

    print('   Creating site...')
    site = _netlify_request('POST', '/sites', token, data={})
    site_id   = site['id']
    site_name = site['name']

    print(f'   Zipping {output_dir}...')
    zip_bytes = _zip_directory(output_dir)
    print(f'   Uploading {len(zip_bytes)//1024}KB zip...')

    deploy = _netlify_request(
        'POST', f'/sites/{site_id}/deploys', token,
        data=zip_bytes, content_type='application/zip'
    )
    deploy_id = deploy['id']

    print('   Waiting for deploy to go live', end='', flush=True)
    for _ in range(60):
        time.sleep(3)
        status = _netlify_request('GET', f'/deploys/{deploy_id}', token)
        state  = status.get('state', '')
        print('.', end='', flush=True)
        if state == 'ready':
            print(' ready!')
            url = status.get('deploy_url') or f"https://{site_name}.netlify.app"
            return url
        if state in ('error', 'failed'):
            raise RuntimeError(f"Deploy failed with state: {state}")

    raise RuntimeError("Deploy timed out after 3 minutes")


# --- MAIN --------------------------------------------------------------------

def main():
    load_env()
    ap = argparse.ArgumentParser(description='Figma -> HTML Prototype Builder')
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--figma-url',  help='Figma design/proto/file URL (downloads JSON automatically)')
    src.add_argument('--figma-file', help='Path to pre-downloaded Figma JSON file')
    ap.add_argument('--figma-token',
                    default=os.environ.get('FIGMA_TOKEN') or os.environ.get('FIGMA_PERSONAL_ACCESS_TOKEN', ''),
                    help='Figma personal access token (or set FIGMA_TOKEN / FIGMA_PERSONAL_ACCESS_TOKEN env var)')
    ap.add_argument('--netlify-token', default=os.environ.get('NETLIFY_API_TOKEN', ''),
                    help='Netlify personal access token (or set NETLIFY_API_TOKEN env var)')
    ap.add_argument('--file-key',    default='', help='Figma file key (only needed with --figma-file)')
    ap.add_argument('--images-dir',  default='', help='Path to pre-downloaded images directory')
    ap.add_argument('--output-dir',  default='./prototype')
    ap.add_argument('--entry',       default='', help='Override entry frame ID')
    args = ap.parse_args()

    print('─' * 50)
    print('Figma -> HTML Prototype Builder')
    print('─' * 50)

    # Step 0: resolve JSON source
    if args.figma_url:
        if not args.figma_token:
            ap.error('--figma-token (or FIGMA_TOKEN env var) is required when using --figma-url')
        file_key = extract_file_key(args.figma_url)
        json_path = os.path.join(args.output_dir, 'figma_file.json')
        os.makedirs(args.output_dir, exist_ok=True)
        print(f'\n0. Downloading Figma file {file_key}...')
        figma_data = fetch_figma_json(file_key, args.figma_token)
        save_and_validate_json(figma_data, json_path)
    else:
        file_key = args.file_key
        json_path = args.figma_file
        print(f'\n0. Loading {json_path}...')
        with open(json_path, encoding='utf-8') as f:
            figma_data = json.load(f)

    print(f"   File: {figma_data.get('name', '?')}")

    print('\n1. Extracting frames...')
    frames = extract_frames(figma_data)
    print(f'   {len(frames)} frames found')

    print('\n2. Extracting interactions...')
    hotspots, toggles, timeouts = extract_interactions(figma_data, set(frames.keys()))
    total_nav = sum(len(v) for v in hotspots.values())
    total_tog = sum(len(v) for v in toggles.values())
    print(f'   {total_nav} nav hotspots (NAVIGATE)')
    print(f'   {total_tog} toggle hotspots (CHANGE_TO)')
    print(f'   {len(timeouts)} auto-advances (AFTER_TIMEOUT)')

    entry_id = args.entry or detect_entry_frame(frames, hotspots)
    print(f"\n3. Entry frame: {frames[entry_id]['name']} [{entry_id}]")

    img_dir = os.path.join(args.output_dir, 'images')
    if args.images_dir:
        print(f'\n4. Using local images from {args.images_dir}...')
        image_map = use_local_images(frames.keys(), args.images_dir, img_dir)
    elif args.figma_token and file_key:
        print(f'\n4. Downloading {len(frames)} images from Figma API...')
        image_map = download_images(frames.keys(), file_key, args.figma_token, img_dir)
    else:
        print('\n4. No image source -- pass --figma-token + --file-key OR --images-dir')
        image_map = {}
    print(f'   {len(image_map)}/{len(frames)} images ready')

    print('\n5. Building HTML...')
    html = build_html(frames, hotspots, toggles, timeouts, image_map, entry_id)
    os.makedirs(args.output_dir, exist_ok=True)
    html_path = os.path.join(args.output_dir, 'index.html')
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'   index.html: {os.path.getsize(html_path)//1024}KB')

    print('\n6. Validating...')
    issues = validate(frames, hotspots, toggles, timeouts, image_map, html)
    if issues:
        print(f'   x {len(issues)} issues:')
        for i in issues:
            print(f'     - {i}')
        sys.exit(1)
    else:
        print(f'   OK {len(frames)} screens | {total_nav} nav | {total_tog} toggles | {len(timeouts)} timers -- all verified')

    if args.netlify_token:
        try:
            live_url = deploy_to_netlify(args.output_dir, args.netlify_token)
            print(f"\n{'─'*50}")
            print(f"Done -> {args.output_dir}/index.html")
            print(f"Live -> {live_url}")
            print(f"{'─'*50}")
        except RuntimeError as exc:
            print(f"   x Deploy failed: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"\n{'─'*50}")
        print(f"Done -> {args.output_dir}/index.html")
        print(f"{'─'*50}")

if __name__ == '__main__':
    main()
