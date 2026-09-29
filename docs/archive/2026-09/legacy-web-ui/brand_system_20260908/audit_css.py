"""Read-only metrics with explicit, reproducible counting definitions."""
from pathlib import Path
from collections import Counter
import re,json
BASE=Path(__file__).resolve().parent
ROOT=BASE.parents[1]
def blocks(text):
    text=re.sub(r'/\*.*?\*/','',text,flags=re.S);i=0
    while i<len(text):
        a=text.find('{',i)
        if a<0:break
        header=text[i:a].strip();depth=1;j=a+1;quote=None
        while j<len(text) and depth:
            ch=text[j]
            if quote:
                if ch==quote and text[j-1]!='\\':quote=None
            elif ch in ['"',"'"]:quote=ch
            elif ch=='{':depth+=1
            elif ch=='}':depth-=1
            j+=1
        yield header,text[a+1:j-1];i=j
def audit(path):
    raw=path.read_text(encoding='utf-8');css=re.sub(r'/\*.*?\*/','',raw,flags=re.S)
    selectors=Counter();hardcodes=[]
    def walk(text,context=()):
        for sel,body in blocks(text):
            if sel.startswith('@'):walk(body,context+(re.sub(r'\s+','',sel),));continue
            selectors[(context,re.sub(r'\s+',' ',sel))]+=1
            if sel not in [':root','[data-theme="light"]']:
                hardcodes.extend(re.findall(r'#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)',body))
    walk(css)
    durations=set(re.findall(r'(?<![\w.-])(?:\.\d+|\d+(?:\.\d+)?)(?:ms|s)\b',css))
    transitions=[]
    for prop in re.findall(r'(?:^|[;{])\s*(?:animation|transition)\s*:\s*([^;}]+)',css):
        transitions.extend(re.findall(r'(?<![\w.-])(?:\.\d+|\d+(?:\.\d+)?)(?:ms|s)\b',prop))
    accent=re.findall(r'--(?:color-)?accent(?:-hover|-strong)?\s*:\s*(#[0-9a-fA-F]{3,8})\b',css)
    core=re.findall(r'--(?:color-)?accent\s*:\s*(#[0-9a-fA-F]{3,8})\b',css)
    result={
        'gradient_declarations':len(re.findall(r'(?:linear|radial|conic)-gradient\(',css)),
        'box_shadow_declarations':len(re.findall(r'(?<![\w-])box-shadow\s*:',css)),
        'literal_shadow_recipes':len(set(re.findall(r'(?<![\w-])box-shadow\s*:\s*(?!var\(|none)([^;}]+)',css))),
        'core_accent_hues':sorted(set(c.lower() for c in core)),
        'accent_with_hover_and_strong_hues':sorted(set(c.lower() for c in accent)),
        'duration_literals':sorted(durations),
        'component_duration_literals':sorted(set(transitions)),
        'motion_duration_tokens':dict(re.findall(r'(--motion-[\w-]+)\s*:\s*([^;]+)',css)),
        'shadow_tokens':dict(re.findall(r'(--shadow-[\w-]+)\s*:\s*([^;]+)',css)),
        'duplicate_selector_definitions':sum(n-1 for n in selectors.values() if n>1),
        'duplicate_selectors':[{'context':list(ctx),'selector':sel,'count':n} for (ctx,sel),n in selectors.items() if n>1],
        'component_color_literals':sorted(set(hardcodes)),
        'reduced_motion':'prefers-reduced-motion' in css,
        'css_bytes':len(raw.encode('utf-8')),
    }
    return result
result={'definitions':{'scope':'design.css only; SVG gradients are listed separately as one shared brand geometry.','duplicates':'same normalized selector in the same normalized at-rule context','shadows':'box-shadow property declarations, including none/focus and inset selection marker; raw recipes exclude var()/none','accent':'unique literal core accent values; hover/strong counted separately','durations':'unique duration literals and component animation/transition literals; cadence tokens include spinner/cursor/skeleton'},'before':audit(BASE/'before/design.css'),'after':audit(ROOT/'web/runtime/design.css'),'logo_variants':{'before':'CSS square mark + manually repeated text; no shared composition API','after':['SVG N mark','DOM horizontal lockup','DOM wordmark'],'svg_geometry_count':1,'svg_asset_files':['brand-mark.svg','brand-favicon.svg']}}
(BASE/'metrics.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))
