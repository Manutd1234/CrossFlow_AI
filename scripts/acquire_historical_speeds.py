#!/usr/bin/env python3
"""Download fixed public research data as inert files; no accounts or paid jobs."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import hashlib,json,ssl,urllib.request,zipfile,io
from pathlib import Path
import certifi
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'backend/data/traffic-imports/historical-expansion-2026-10-24'
SOURCES=(('guangzhou-metadata.json','https://zenodo.org/api/records/1205229',2_000_000),
 ('guangzhou-Data.zip','https://zenodo.org/records/1205229/files/Data.zip?download=1',25_000_000),
 ('batam-grand-mall.pdf','https://repository.unrika.ac.id/xmlui/bitstream/handle/123456789/1023/REPOSITORY%20JURNAL%20FELANY%20FRISCA%20ULANDARI%20.pdf?sequence=1&isAllowed=y',12_000_000))


def download(item):
 name,url,limit=item
 request=urllib.request.Request(url,headers={'User-Agent':'CrossFlow academic historical traffic research'})
 with urllib.request.urlopen(request,context=ssl.create_default_context(cafile=certifi.where()),timeout=40) as response:
  raw=response.read(limit+1);final=response.url
 if len(raw)>limit:raise ValueError('Source exceeds size bound')
 if name.endswith('.pdf') and not raw.startswith(b'%PDF'):raise ValueError('Not PDF')
 if name.endswith('.zip'):
  with zipfile.ZipFile(io.BytesIO(raw)) as archive:
   if archive.getinfo('Data/traffic_speed_data.csv').file_size>40_000_000:raise ValueError('Unexpected source CSV size')
 if name.endswith('.json') and json.loads(raw)['metadata']['license']['id']!='cc-by-4.0':raise ValueError('Dataset licence differs')
 (OUT/name).write_bytes(raw)
 return {'name':name,'source_url':url,'final_url':final,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
         'downloaded_at':datetime.now(timezone.utc).isoformat(),'status':'downloaded'}


def main():
 OUT.mkdir(parents=True,exist_ok=True)
 with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(download,SOURCES))
 (OUT/'acquisition.json').write_text(json.dumps(results,indent=2)+'\n')
 print(json.dumps(results,indent=2))

if __name__=='__main__':main()
