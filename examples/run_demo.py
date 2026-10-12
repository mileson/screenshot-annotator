#!/usr/bin/env python3
"""Build a fully synthetic v2 example. No accounts or user screenshots needed."""
from pathlib import Path
import argparse
import json
import sys
from PIL import Image, ImageDraw, ImageFont
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from screenshot_annotator_v2.engine import run_batch

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'demo-output')
    args = parser.parse_args()
    out = args.output.resolve()
    raw = out / 'raw'
    raw.mkdir(parents=True, exist_ok=True)
    image = Image.new('RGB', (1200, 900), '#F1F5F9')
    draw = ImageDraw.Draw(image)
    from screenshot_annotator_v2.common import load_font
    draw.rounded_rectangle((70, 90, 1130, 780), radius=30, fill='white', outline='#CBD5E1', width=3)
    draw.text((120, 130), 'Synthetic settings / 演示设置', fill='#153047', font=load_font(34))
    draw.text((120, 190), 'This image contains no real user data.', fill='#607080', font=load_font(24))
    draw.rounded_rectangle((120, 310, 650, 420), radius=20, fill='#EAF1FF')
    draw.text((160, 345), 'Private mode / 隐私模式', fill='#153047', font=load_font(30))
    draw.rounded_rectangle((780, 590, 1060, 680), radius=20, fill='#2563EB')
    draw.text((850, 615), 'Save / 保存', fill='white', font=load_font(30))
    image.save(raw / 'settings.png')
    manifest = {'schema_version':'2.0','job_id':'synthetic-demo','style':{'preset':'action-orange-v2'},'privacy':{'source_handling':'read_only_copy','sensitive_data':'not_present_synthetic_fixture','watermark':'preserve_unknown','authorized_watermark_removal':False},'flow':{'type':'linear','sort':'declared_order','order':['settings'],'edges':[]},'overview':{'mode':'vertical','max_pixels':40000000},'assets':[{'id':'settings','source':'raw/settings.png','output_name':'settings-annotated.png','kind':'sequential_action','protected_regions':[{'id':'title','rect':[115,120,1100,245],'reason':'Keep the heading readable'}],'annotations':[{'id':'private','rect':[120,310,650,420],'semantic_role':'sequence','callout':{'text':'选择隐私模式','marker':'auto','anchor':'auto'}},{'id':'save','rect':[780,590,1060,680],'semantic_role':'sequence','callout':{'text':'点击保存','marker':'auto','anchor':'auto'}}]}]}
    path = out / 'demo-plan.json'
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    result = run_batch(path, out / 'output')
    print('Generated:', Path(result).relative_to(out))
    print('Automatic validation is not manual visual approval. Inspect annotated, overview and qa outputs.')
if __name__ == '__main__':
    main()
