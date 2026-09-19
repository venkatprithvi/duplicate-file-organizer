from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile
import plistlib
import re
import struct
import xml.etree.ElementTree as ET

IMAGE_SUFFIXES={'.jpg','.jpeg','.png','.tif','.tiff','.webp','.heic','.heif'}
VIDEO_SUFFIXES={'.mp4','.mov','.m4v','.3gp','.3g2','.mkv','.webm','.avi','.mts','.m2ts'}
OFFICE_SUFFIXES={'.docx','.docm','.xlsx','.xlsm','.pptx','.pptm'}
PDF_SUFFIXES={'.pdf'}
AUDIO_SUFFIXES={'.mp3','.m4a','.flac','.wav','.aiff','.aif','.ogg','.opus','.aac','.wma'}


def _year_from_text(value) -> str|None:
    if value is None: return None
    s=str(value).strip()
    m=re.search(r'(19\d{2}|20\d{2}|21\d{2})',s)
    return m.group(1) if m else None


def _parse_datetime(value) -> datetime|None:
    if value is None: return None
    if isinstance(value,datetime): return value
    s=str(value).strip().replace('Z','+00:00')
    for fmt in ('%Y:%m:%d %H:%M:%S','%Y-%m-%d %H:%M:%S','%Y-%m-%dT%H:%M:%S','%Y-%m-%dT%H:%M:%S%z','%Y/%m/%d %H:%M:%S'):
        try: return datetime.strptime(s,fmt)
        except ValueError: pass
    return None


def _dms_to_decimal(values, ref):
    def ratio(v):
        try: return float(v)
        except Exception: return float(v.numerator)/float(v.denominator)
    if len(values)!=3: raise ValueError('invalid GPS')
    d,m,s=values
    out=ratio(d)+ratio(m)/60+ratio(s)/3600
    return -out if str(ref).upper() in {'S','W'} else out


def _image_metadata(path:Path):
    try:
        from PIL import Image
        with Image.open(path) as img:
            exif=img.getexif()
            year=None; source=None
            if exif:
                for tag,src in ((36867,'EXIF_DateTimeOriginal'),(36868,'EXIF_DateTimeDigitized'),(306,'EXIF_DateTime')):
                    dt=_parse_datetime(exif.get(tag))
                    if dt: year=str(dt.year); source=src; break
                gps=exif.get_ifd(34853) if hasattr(exif,'get_ifd') else exif.get(34853)
                if gps:
                    lat,lon=gps.get(2),gps.get(4); lr,lor=gps.get(1),gps.get(3)
                    if lat and lon and lr and lor:
                        la=_dms_to_decimal(lat,lr); lo=_dms_to_decimal(lon,lor)
                        return year,source,f'GPS_{la:.5f}_{lo:.5f}','EXIF_GPS'
            return year,source,None,'NONE'
    except Exception:
        return None,None,None,'NONE'


def _office_metadata(path:Path):
    try:
        with ZipFile(path) as z:
            data=z.read('docProps/core.xml')
        root=ET.fromstring(data)
        for child in root.iter():
            tag=child.tag.rsplit('}',1)[-1]
            if tag=='created':
                dt=_parse_datetime(child.text)
                if dt: return str(dt.year),'OFFICE_CORE_CREATED',None,'NONE'
        return None,None,None,'NONE'
    except Exception:
        return None,None,None,'NONE'


def _pdf_metadata(path:Path):
    try:
        size=path.stat().st_size
        with path.open('rb') as f:
            head=f.read(min(size,8*1024*1024))
            if size>8*1024*1024:
                f.seek(max(0,size-2*1024*1024)); tail=f.read(2*1024*1024)
            else: tail=b''
        raw=head+tail
        # PDF date syntax: D:YYYYMMDDHHmmSS...
        m=re.search(rb'/CreationDate\s*\(D:(\d{4})(\d{2})?(\d{2})?',raw)
        if m: return m.group(1).decode(),'PDF_CreationDate',None,'NONE'
        # XMP often contains <CreateDate> or <xmp:CreateDate>.
        for pat in (rb'<(?:[^:>]+:)?CreateDate[^>]*>\s*([^<]+)',rb'<(?:[^:>]+:)?DateCreated[^>]*>\s*([^<]+)'):
            m=re.search(pat,raw,re.I)
            if m:
                y=_year_from_text(m.group(1).decode(errors='ignore'))
                if y:return y,'PDF_XMP_CreateDate',None,'NONE'
    except Exception: pass
    return None,None,None,'NONE'


def _mp4_creation(path:Path):
    try:
        raw=path.read_bytes()[:64*1024*1024]
        # QuickTime/MP4 mvhd stores seconds since 1904. Search plausible mvhd boxes.
        idx=0
        while True:
            idx=raw.find(b'mvhd',idx)
            if idx<0 or idx+20>=len(raw): break
            version=raw[idx+4]
            off=idx+8 if version==0 else idx+16
            if off+4<=len(raw):
                sec=struct.unpack('>I',raw[off:off+4])[0]
                if 0<sec<4000000000:
                    ts=datetime(1904,1,1,tzinfo=timezone.utc).timestamp()+sec
                    dt=datetime.fromtimestamp(ts,timezone.utc)
                    if 1970<=dt.year<=2100:return str(dt.year),'MP4_MVHD_CREATION',None,'NONE'
            idx+=4
    except Exception: pass
    return None,None,None,'NONE'


def _mkv_creation(path:Path):
    # Matroska DateUTC is an EBML signed 64-bit nanosecond offset from 2001-01-01.
    try:
        raw=path.read_bytes()[:64*1024*1024]
        marker=b'\x44\x61'
        pos=raw.find(marker)
        if pos>=0 and pos+12<len(raw):
            # Scan nearby for an 8-byte plausible signed value.
            for i in range(pos+2,min(pos+32,len(raw)-8)):
                v=struct.unpack('>q',raw[i:i+8])[0]
                if -100*365*24*3600*1_000_000_000 < v < 200*365*24*3600*1_000_000_000:
                    dt=datetime(2001,1,1,tzinfo=timezone.utc)+__import__('datetime').timedelta(microseconds=v/1000)
                    if 1970<=dt.year<=2100:return str(dt.year),'MKV_DATEUTC',None,'NONE'
    except Exception: pass
    return None,None,None,'NONE'


def _plist_metadata(path:Path):
    try:
        if path.suffix.lower() not in {'.plist','.mobileconfig'}: return None,None,None,'NONE'
        with path.open('rb') as f: obj=plistlib.load(f)
        for key in ('CreationDate','created','creationDate','DateCreated'):
            if key in obj:
                dt=_parse_datetime(obj[key]); y=str(dt.year) if dt else _year_from_text(obj[key])
                if y:return y,'PLIST_CREATED',None,'NONE'
    except Exception: pass
    return None,None,None,'NONE'


def _filesystem_fallback(path:Path):
    try:
        st=path.stat()
        birth=getattr(st,'st_birthtime',None)
        if birth:
            return str(datetime.fromtimestamp(birth).year),'FILESYSTEM_BIRTHTIME',None,'NONE','LOW'
        return str(datetime.fromtimestamp(st.st_mtime).year),'FILESYSTEM_MODIFIED',None,'NONE','LOW'
    except OSError: return 'Unknown Year','NONE',None,'NONE','NONE'


def extract_creation_metadata(path:Path):
    """Return year, location, location_source, date_source, confidence.

    Priority is embedded/original content metadata first, filesystem birth time
    only as a low-confidence fallback, then modified time as the final fallback.
    No online service is used and filenames are never used to infer a date/location.
    """
    suffix=path.suffix.casefold()
    year=source=location=loc_source=None
    confidence='HIGH'
    if suffix in IMAGE_SUFFIXES:
        year,source,location,loc_source=_image_metadata(path)
    elif suffix in OFFICE_SUFFIXES:
        year,source,location,loc_source=_office_metadata(path)
    elif suffix in PDF_SUFFIXES:
        year,source,location,loc_source=_pdf_metadata(path)
    elif suffix in {'.mp4','.mov','.m4v','.3gp','.3g2'}:
        year,source,location,loc_source=_mp4_creation(path)
    elif suffix in {'.mkv','.webm'}:
        year,source,location,loc_source=_mkv_creation(path)
    elif suffix in {'.plist','.mobileconfig'}:
        year,source,location,loc_source=_plist_metadata(path)
    if year:
        return year,location or 'Unknown Location',loc_source or 'NONE',source or 'EMBEDDED_METADATA','HIGH'
    fy,fs,_,_,fc=_filesystem_fallback(path)
    return fy,'Unknown Location','NONE',fs,fc
