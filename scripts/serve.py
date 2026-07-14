#!/usr/bin/env python3
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import os, json, secrets, time, mimetypes, urllib.parse, urllib.request, subprocess, re

BASE=Path(__file__).resolve().parents[1]
os.chdir(BASE)

def load_env_file(path):
    try:
        for line in Path(path).read_text().splitlines():
            line=line.strip()
            if not line or line.startswith('#') or '=' not in line: continue
            k,v=line.split('=',1)
            os.environ.setdefault(k.strip(), v.strip().strip('\"').strip("'"))
    except FileNotFoundError:
        pass

load_env_file(BASE/'.env')
CONFIG_PATH=Path(os.environ.get('CONFIG_PATH', BASE/'config.json'))
MEDIA_EXTS={'.jpg','.jpeg','.png','.webp'}
ADMIN_PASSWORD=os.environ.get('DASHBOARD_ADMIN_PASSWORD')
SESSIONS={}
SESSION_TTL=3600

# OpenClaw 兼容 OpenAI 的聊天 API（token 仅后端持有，不暴露到前端）
OPENCLAW_API_URL=os.environ.get('OPENCLAW_API_URL', 'http://127.0.0.1:18789/v1/chat/completions')
OPENCLAW_API_TOKEN=os.environ.get('OPENCLAW_API_TOKEN', '')

SAFE_TOP_LEVEL={'refreshSeconds'}

# ========== 系统监控（真实数据，macOS 原生命令，零依赖） ==========
_sys_cache={'data':None,'time':0}
SYS_CACHE_TTL=3

def _sh(cmd, timeout=4):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ''

def get_system_stats():
    now=time.time()
    if _sys_cache['data'] and now-_sys_cache['time']<SYS_CACHE_TTL:
        return _sys_cache['data']
    out={'ok':True}
    # CPU 使用率
    try:
        t=_sh("top -l 1 -n 0")
        m=re.search(r'CPU usage:\s*([\d.]+)%\s*user,\s*([\d.]+)%\s*sys,\s*([\d.]+)%\s*idle', t)
        if m:
            idle=float(m.group(3))
            out['cpu']=round(100-idle,1)
        else:
            out['cpu']=None
    except Exception:
        out['cpu']=None
    # 内存使用率
    try:
        total=int(_sh("sysctl -n hw.memsize").strip() or 0)
        vm=_sh("vm_stat")
        psize=16384
        pm=re.search(r'page size of (\d+)', vm)
        if pm: psize=int(pm.group(1))
        def pg(name):
            mm=re.search(name+r':\s*(\d+)', vm)
            return int(mm.group(1))*psize if mm else 0
        # 已用 = active + wired + compressed
        wired=pg('Pages wired down')
        active=pg('Pages active')
        compressed=pg('Pages occupied by compressor')
        used=wired+active+compressed
        if total>0:
            out['memPercent']=round(used/total*100,1)
            out['memUsedGb']=round(used/1024/1024/1024,1)
            out['memTotalGb']=round(total/1024/1024/1024,1)
        else:
            out['memPercent']=None
    except Exception:
        out['memPercent']=None
    # 磁盘（优先看数据卷，macOS 根卷只读意义不大）
    try:
        target='/System/Volumes/Data'
        d=_sh("df -k '"+target+"' 2>/dev/null").strip().splitlines()
        if len(d)<2:
            d=_sh("df -k /").strip().splitlines()
        if len(d)>=2:
            parts=d[-1].split()
            total_kb=int(parts[1]); used_kb=int(parts[2])
            out['diskPercent']=round(used_kb/total_kb*100,1)
            out['diskUsedGb']=round(used_kb/1024/1024,1)
            out['diskTotalGb']=round(total_kb/1024/1024,1)
        else:
            out['diskPercent']=None
    except Exception:
        out['diskPercent']=None
    # 负载
    try:
        la=_sh("sysctl -n vm.loadavg").strip()
        lm=re.findall(r'[\d.]+', la)
        out['load']=[float(x) for x in lm[:3]] if lm else None
    except Exception:
        out['load']=None
    # 温度（可选，无工具则 None）
    try:
        tmp=_sh("which osx-cpu-temp").strip()
        if tmp:
            tv=_sh("osx-cpu-temp").strip()
            tm=re.search(r'([\d.]+)', tv)
            out['tempC']=float(tm.group(1)) if tm else None
        else:
            out['tempC']=None
    except Exception:
        out['tempC']=None
    # 运行时间
    try:
        bt=_sh("sysctl -n kern.boottime")
        bm=re.search(r'sec = (\d+)', bt)
        if bm:
            up=int(now)-int(bm.group(1))
            days=up//86400; hours=(up%86400)//3600
            out['uptime']=(str(days)+'天' if days else '')+str(hours)+'小时'
        else:
            out['uptime']=None
    except Exception:
        out['uptime']=None
    _sys_cache['data']=out
    _sys_cache['time']=now
    return out

# ========== 可清理项扫描（只读不删） ==========
_cleanup_cache={'data':None,'time':0}
CLEANUP_TTL=60

def _du_bytes(path):
    """返回目录字节数，失败返回 0。"""
    try:
        expanded=os.path.expanduser(path)
        if not os.path.exists(expanded):
            return 0
        r=_sh("du -sk '"+expanded+"' 2>/dev/null", timeout=8).strip()
        if not r:
            return 0
        kb=int(r.split()[0])
        return kb*1024
    except Exception:
        return 0

def _fmt_size(b):
    if b>=1024**3: return round(b/1024**3,1), 'GB'
    if b>=1024**2: return round(b/1024**2,0), 'MB'
    if b>=1024: return round(b/1024,0), 'KB'
    return b, 'B'

def get_cleanup_stats():
    now=time.time()
    if _cleanup_cache['data'] and now-_cleanup_cache['time']<CLEANUP_TTL:
        return _cleanup_cache['data']
    targets=[
        {'key':'cache','name':'用户缓存','icon':'🧹','path':'~/Library/Caches'},
        {'key':'logs','name':'日志文件','icon':'📝','path':'~/Library/Logs'},
        {'key':'trash','name':'回收站','icon':'🗑️','path':'~/.Trash'},
        {'key':'temp','name':'临时文件','icon':'📂','path':'~/Library/Application Support/CrashReporter'},
    ]
    items=[]
    total=0
    for t in targets:
        b=_du_bytes(t['path'])
        total+=b
        val,unit=_fmt_size(b)
        items.append({'key':t['key'],'name':t['name'],'icon':t['icon'],
                      'bytes':b,'size':(str(val)+' '+unit)})
    tv,tu=_fmt_size(total)
    out={'ok':True,'items':items,'totalBytes':total,'total':(str(tv)+' '+tu)}
    _cleanup_cache['data']=out
    _cleanup_cache['time']=now
    return out

# ========== Agent 记忆（解析 MEMORY.md 真实主题块） ==========
_mem_cache={'data':None,'time':0}
MEM_CACHE_TTL=120
WORKSPACE_DIR=os.environ.get('WORKSPACE_DIR', os.path.expanduser('~/.openclaw/workspace'))

def get_agent_memory():
    now=time.time()
    if _mem_cache['data'] and now-_mem_cache['time']<MEM_CACHE_TTL:
        return _mem_cache['data']
    path=os.path.join(WORKSPACE_DIR, 'MEMORY.md')
    nodes=[]
    # 跳过的通用章节（非具体记忆）
    skip={'about','core principles','skills and capabilities','future goals'}
    try:
        with open(path,'r',encoding='utf-8') as f:
            text=f.read()
        blocks=re.split(r'\n## ', text)
        idx=0
        for blk in blocks:
            blk=blk.strip()
            if not blk: continue
            lines=blk.splitlines()
            title=lines[0].lstrip('# ').strip()
            if not title or title.lower() in skip: continue
            body=' '.join(l.strip() for l in lines[1:] if l.strip() and not l.strip().startswith('#'))
            body=re.sub(r'[*`>\-]+',' ',body)
            desc=body[:80].strip()
            if not desc: continue
            # 提取日期作为时间信号
            dm=re.search(r'(20\d\d[-\.\/]\d{1,2}[-\.\/]\d{1,2}|20\d\d)', title+' '+body[:200])
            date_hint=dm.group(1) if dm else None
            # 标签：从标题拆关键词（中文按词，英文整个短语保留）
            clean_title=re.sub(r'\(20\d\d\)|20\d\d[-\.\/]?\d*[-\.\/]?\d*','',title).strip()
            # 若含中文，按分隔符拆；否则整个标题当一个标签
            if re.search(r'[\u4e00-\u9fff]', clean_title):
                raw_tags=re.split(r'[\s/（）()、,，\-]+', clean_title)
                tags=[t for t in raw_tags if len(t)>=2][:3]
            else:
                tags=[clean_title[:24]] if clean_title else []
            nodes.append({'title':title[:20],'desc':desc,'tags':tags or ['记忆'],
                          'date':date_hint,'order':idx})
            idx+=1
    except Exception as e:
        _mem_cache['data']={'ok':False,'error':str(e),'nodes':[]}
        _mem_cache['time']=now
        return _mem_cache['data']
    # 温度：按顺序递减（靠前=更活跃），95 到 20
    n=len(nodes) or 1
    for i,nd in enumerate(nodes):
        nd['temp']=max(20, round(95-(i/n)*75))
        nd['hoursAgo']=round((i/n)*200)+1
    out={'ok':True,'nodes':nodes,'count':len(nodes)}
    _mem_cache['data']=out
    _mem_cache['time']=now
    return out

# ========== 网络测速（真实 ping + 下载） ==========
def get_speedtest():
    out={'ok':True}
    # Ping（阿里 DNS）
    try:
        p=_sh("ping -c 3 -t 4 223.5.5.5", timeout=8)
        m=re.search(r'=\s*[\d.]+/([\d.]+)/', p)
        out['ping']=round(float(m.group(1)),1) if m else None
    except Exception:
        out['ping']=None
    # 下载测速（5MB 测试文件）
    try:
        r=_sh("curl -s -o /dev/null -w '%{speed_download} %{time_total}' --max-time 10 'https://speed.cloudflare.com/__down?bytes=5000000'", timeout=12).strip()
        parts=r.split()
        if len(parts)>=1 and float(parts[0])>0:
            bps=float(parts[0])
            out['downloadMbps']=round(bps*8/1024/1024,1)
        else:
            out['downloadMbps']=None
    except Exception:
        out['downloadMbps']=None
    # 上传测速（上传 2MB）
    try:
        r=_sh("head -c 2000000 /dev/zero | curl -s -o /dev/null -w '%{speed_upload}' --max-time 10 -X POST --data-binary @- 'https://speed.cloudflare.com/__up'", timeout=12).strip()
        if r and float(r)>0:
            out['uploadMbps']=round(float(r)*8/1024/1024,1)
        else:
            out['uploadMbps']=None
    except Exception:
        out['uploadMbps']=None
    return out

SAFE_NESTED={
    'surge': {'enabled','baseUrl','maxEvents'},
    'ubnt': {'enabled','baseUrl','site'},
    'thresholds': {'diskFreeGbWarn','diskFreeGbCritical','httpTimeoutSec'},
}

def read_json(path):
    with open(path,'r',encoding='utf-8') as f:
        return json.load(f)
def cfg_get():
    return read_json(CONFIG_PATH)

def emby_cfg():
    cfg=cfg_get()
    e=(cfg.get('emby') or {})
    svc=(cfg.get('services') or {}).get('emby') or {}
    return {
        'enabled': e.get('enabled', True),
        'internalUrl': (e.get('internalUrl') or svc.get('url') or 'http://127.0.0.1:8096').rstrip('/'),
        'publicUrl': (e.get('publicUrl') or e.get('internalUrl') or svc.get('url') or 'http://127.0.0.1:8096').rstrip('/'),
        'apiKey': os.environ.get('EMBY_API_KEY') or os.environ.get('EMBY_TOKEN') or e.get('apiKey') or e.get('token'),
        'userId': os.environ.get('EMBY_USER_ID') or e.get('userId'),
    }

def emby_api(path, timeout=10):
    e=emby_cfg()
    if not e.get('enabled') or not e.get('apiKey'):
        raise RuntimeError('emby api not configured')
    url=e['internalUrl'] + path
    req=urllib.request.Request(url, headers={'X-Emby-Token': e['apiKey']})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))

def emby_user_id():
    e=emby_cfg()
    if e.get('userId'):
        return e['userId']
    users=emby_api('/Users', timeout=10)
    if not users:
        raise RuntimeError('no emby users')
    return users[0]['Id']

_emby_stats_cache={'data':None,'time':0}
EMBY_STATS_TTL=60

def emby_stats():
    now=time.time()
    if _emby_stats_cache['data'] and now-_emby_stats_cache['time']<EMBY_STATS_TTL:
        return _emby_stats_cache['data']
    out={'ok':True}
    try:
        counts=emby_api('/Items/Counts', timeout=10)
        out['movies']=counts.get('MovieCount', 0)
        out['series']=counts.get('SeriesCount', 0)
        out['episodes']=counts.get('EpisodeCount', 0)
        out['albums']=counts.get('AlbumCount', 0)
    except Exception as e:
        out['ok']=False
        out['error']=str(e)
    # 在线会话（正在播放的用户）
    try:
        sessions=emby_api('/Sessions', timeout=10)
        active=[s for s in sessions if s.get('NowPlayingItem')] if isinstance(sessions,list) else []
        out['activeUsers']=len(active)
        out['totalSessions']=len(sessions) if isinstance(sessions,list) else 0
    except Exception:
        out['activeUsers']=None
        out['totalSessions']=None
    _emby_stats_cache['data']=out
    _emby_stats_cache['time']=now
    return out


ADULT_KEYWORDS={
    '成人','三级','福利姬','写真','无码','有码','女优','巨乳','人妻','熟女','素人','痴女','出轨','不伦','调教','凌辱','痴汉','偷拍','流出','约炮','口交','性交','高潮','中出','颜射','潮吹','乳交','肛交','强奸','乱伦','ntr','av','jav','fc2','heyzo','一本道','caribbeancom','tokyo-hot','1pondo','pacopacomama','carib','麻豆','swag','onlyfans','porn','xxx','hentai','adult','sex','erotic','r18','r-18','uncensored','censored','nude','naked',
    'midv','kwbd','juy','juq','snos','ssis','ipzz','ipx','mide','pred','abp','abw','miaa','stars','ssni','dass','mukd','meyd','rbd','vec','dvaj','jul','mdyd','iptd','pppd','dvdms','fss','cjod','atid','venu','nsfs','mimk','roe','hunt','hmn','adn','sdde','juvr','waaa','mird','mifd','dasd','ebod','apns','shkd','snis','soe','jux','miae','ipz','meyd'
}
ADULT_RATING_MARKERS={'xxx','nc-17','r18','r-18','adult','x'}

def is_adult_item(it):
    parts=[]
    for k in ('Name','OriginalTitle','SortName','Path','Overview','OfficialRating','CustomRating','Type'):
        v=it.get(k)
        if v: parts.append(str(v))
    for tag in (it.get('Tags') or []): parts.append(str(tag))
    for g in (it.get('Genres') or []): parts.append(str(g))
    text=' '.join(parts).lower()
    rating=str(it.get('OfficialRating') or it.get('CustomRating') or '').lower()
    if any(m in rating for m in ADULT_RATING_MARKERS): return True
    if any(k in text for k in ADULT_KEYWORDS): return True
    # 常见番号模式：ABC-123 / ABCD-123 等，避免误伤纯中文影视标题。
    import re
    if re.search(r'\b[a-z]{2,6}[-_ ]?\d{2,5}\b', text): return True
    return False

def is_adult_path_title(path):
    text=str(path).lower()
    if any(k in text for k in ADULT_KEYWORDS): return True
    import re
    return bool(re.search(r'\b[a-z]{2,6}[-_ ]?\d{2,5}\b', text))

def emby_recent(limit=10):
    uid=emby_user_id()
    qs=urllib.parse.urlencode({
        'Limit': max(30,min(200,int(limit)*8)),
        'Fields': 'Path,DateCreated,PrimaryImageAspectRatio,ProductionYear,Genres,Tags,OfficialRating,CustomRating,OriginalTitle,SortName',
        'ImageTypeLimit': 1,
        'EnableImageTypes': 'Primary',
    })
    items=emby_api(f'/Users/{urllib.parse.quote(uid)}/Items/Latest?{qs}', timeout=15)
    e=emby_cfg()
    out=[]
    for it in items:
        if is_adult_item(it):
            continue
        iid=str(it.get('Id') or '')
        if not iid: continue
        img_tag=(it.get('ImageTags') or {}).get('Primary')
        # 跳过没有主海报的 Emby 项目，避免首页裂图/空图。
        if not img_tag:
            continue
        name=it.get('Name') or '未命名'
        year=it.get('ProductionYear')
        title=f'{name} ({year})' if year and str(year) not in str(name) else name
        poster=f'/api/emby/image/{urllib.parse.quote(iid)}'
        poster += '?tag=' + urllib.parse.quote(str(img_tag))
        server_id=str(it.get('ServerId') or '')
        item_url=f"{e['publicUrl']}/web/index.html#!/item?id={urllib.parse.quote(iid)}"
        if server_id:
            item_url += '&serverId=' + urllib.parse.quote(server_id)
        out.append({
            'id': iid,
            'title': title,
            'category': it.get('Type') or 'Emby',
            'mtime': it.get('DateCreated'),
            'poster': poster,
            'href': item_url,
            'source': 'emby-api',
        })
        if len(out)>=limit: break
    return out

def write_json(path,obj):
    tmp=path.with_suffix(path.suffix+'.tmp')
    with open(tmp,'w',encoding='utf-8') as f:
        json.dump(obj,f,ensure_ascii=False,indent=2)
        f.write('\n')
    tmp.replace(path)

def public_config(cfg):
    return {
        'refreshSeconds': cfg.get('refreshSeconds'),
        'surge': {k: cfg.get('surge',{}).get(k) for k in ['enabled','baseUrl','maxEvents']},
        'ubnt': {k: cfg.get('ubnt',{}).get(k) for k in ['enabled','baseUrl','site']},
        'thresholds': {k: cfg.get('thresholds',{}).get(k) for k in ['diskFreeGbWarn','diskFreeGbCritical','httpTimeoutSec']},
    }

def merge_safe(cfg, patch):
    out=json.loads(json.dumps(cfg))
    for k in SAFE_TOP_LEVEL:
        if k in patch:
            out[k]=patch[k]
    for section, keys in SAFE_NESTED.items():
        if section in patch and isinstance(patch[section], dict):
            out.setdefault(section,{})
            for k in keys:
                if k in patch[section]:
                    out[section][k]=patch[section][k]
    return out

def valid_token(token):
    if not token: return False
    now=time.time()
    expired=[t for t,ts in SESSIONS.items() if now-ts>SESSION_TTL]
    for t in expired: SESSIONS.pop(t,None)
    return token in SESSIONS

def media_roots():
    try:
        cfg=read_json(CONFIG_PATH)
        roots=[]
        for v in (cfg.get('paths') or {}).values():
            if v:
                p=Path(v).expanduser().resolve()
                if p.exists(): roots.append(p)
        return roots
    except Exception:
        return []

def is_under(path, root):
    try:
        path=Path(path).resolve(); root=Path(root).resolve()
        return path == root or root in path.parents
    except Exception:
        return False

def poster_title(path):
    p=Path(path)
    parent=p.parent.name
    if parent.lower().startswith('season ') and p.parent.parent.name:
        return p.parent.parent.name
    return parent or p.stem

def poster_category(path, roots):
    p=Path(path).resolve()
    for root in roots:
        if is_under(p, root):
            try:
                rel=p.relative_to(root)
                if len(rel.parts)>2 and rel.parts[0].lower() in ('mp','media'):
                    return rel.parts[1]
                return rel.parts[0] if len(rel.parts)>2 else '媒体库'
            except Exception:
                pass
    return '媒体库'

def recent_posters(limit=10):
    roots=media_roots()
    items=[]
    names=('poster.jpg','poster.png','folder.jpg','cover.jpg')
    for root in roots:
        if not root.exists(): continue
        for name in names:
            try:
                for f in root.rglob(name):
                    if f.is_file() and f.suffix.lower() in MEDIA_EXTS:
                        try:
                            if f.parent.name.lower().startswith('season ') and (f.parent.parent/'poster.jpg').exists():
                                continue
                            if is_adult_path_title(f):
                                continue
                            st=f.stat()
                            if st.st_size <= 0:
                                continue
                            items.append({'path':str(f.resolve()),'title':poster_title(f),'category':poster_category(f, roots),'mtime':st.st_mtime,'size':st.st_size})
                        except Exception:
                            pass
            except Exception:
                pass
    items.sort(key=lambda x:x.get('mtime') or 0, reverse=True)
    out=[]; seen=set()
    for it in items:
        key=str(Path(it['path']).parent)
        if key in seen: continue
        seen.add(key)
        token=urllib.parse.quote(it['path'], safe='')
        out.append({'title':it['title'],'category':it['category'],'mtime':it['mtime'],'poster':'/api/media/poster?path='+token})
        if len(out)>=limit: break
    return out

class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        body=json.dumps(obj,ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _hotspot_api(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == '/api/hotspot/weibo':
                data = fetch_weibo_hot()
                return self._json(200, {'ok':True, 'data': data})
            elif path == '/api/hotspot/douyin':
                data = fetch_douyin_hot()
                return self._json(200, {'ok':True, 'data': data})
            elif path == '/api/hotspot/zhihu':
                data = fetch_zhihu_hot()
                return self._json(200, {'ok':True, 'data': data})
            elif path == '/api/hotspot/bilibili':
                data = fetch_bilibili_hot()
                return self._json(200, {'ok':True, 'data': data})
            elif path == '/api/hotspot/toutiao':
                data = fetch_toutiao_hot()
                return self._json(200, {'ok':True, 'data': data})
            elif path == '/api/hotspot/github':
                data = fetch_github_trending()
                return self._json(200, {'ok':True, 'data': data})
            elif path == '/api/hotspot/all':
                data = fetch_all_hotspot()
                return self._json(200, {'ok':True, **data})
            else:
                return self._json(404, {'ok':False, 'error': 'not found'})
        except Exception as e:
            return self._json(500, {'ok':False, 'error':str(e)})

    def _body_json(self):
        n=int(self.headers.get('Content-Length','0') or 0)
        if n<=0: return {}
        return json.loads(self.rfile.read(n).decode('utf-8'))

    def _chat_proxy(self):
        """代理到 OpenClaw 兼容 OpenAI 的聊天 API，token 仅后端注入，支持流式转发。"""
        if not OPENCLAW_API_TOKEN:
            return self._json(503, {'ok': False, 'error': 'OPENCLAW_API_TOKEN 未配置，请在 .env 中设置'})
        try:
            body = self.rfile.read(int(self.headers.get('Content-Length', '0') or 0))
            req = urllib.request.Request(OPENCLAW_API_URL, data=body, method='POST', headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + OPENCLAW_API_TOKEN,
            })
            with urllib.request.urlopen(req, timeout=120) as r:
                ctype = r.headers.get('Content-Type', 'text/event-stream')
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                # 流式逐块转发
                while True:
                    chunk = r.read(1024)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
        except urllib.error.HTTPError as e:
            return self._json(e.code, {'ok': False, 'error': 'upstream: ' + str(e)})
        except Exception as e:
            return self._json(502, {'ok': False, 'error': str(e)})

    def do_GET(self):
        if self.path.startswith('/api/emby/recent'):
            try:
                q=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                limit=max(1,min(30,int((q.get('limit') or ['10'])[0])))
                try:
                    items=emby_recent(limit)
                except Exception:
                    items=recent_posters(limit)
                return self._json(200, {'ok':True,'items':items})
            except Exception as e:
                return self._json(500, {'ok':False,'error':str(e)})
        if self.path.startswith('/api/emby/stats'):
            return self._json(200, emby_stats())
        if self.path.startswith('/api/emby/image/'):
            try:
                item_id=urllib.parse.unquote(urllib.parse.urlparse(self.path).path.rsplit('/',1)[-1])
                if not item_id:
                    return self._json(400, {'ok':False,'error':'missing item id'})
                e=emby_cfg()
                if not e.get('apiKey'):
                    return self._json(503, {'ok':False,'error':'emby api not configured'})
                q=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                params={'quality':'88','maxWidth':'480'}
                if (q.get('tag') or [''])[0]:
                    params['tag']=(q.get('tag') or [''])[0]
                url=e['internalUrl'] + f"/Items/{urllib.parse.quote(item_id)}/Images/Primary?" + urllib.parse.urlencode(params)
                req=urllib.request.Request(url, headers={'X-Emby-Token': e['apiKey']})
                with urllib.request.urlopen(req, timeout=12) as r:
                    data=r.read()
                    ctype=r.headers.get('Content-Type') or 'image/jpeg'
                if not data:
                    return self._json(404, {'ok':False,'error':'empty image'})
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Cache-Control','public, max-age=600')
                self.send_header('Content-Length',str(len(data)))
                self.end_headers(); self.wfile.write(data); return
            except Exception as e:
                return self._json(502, {'ok':False,'error':str(e)})
        if self.path.startswith('/api/media/poster'):
            try:
                q=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                raw=(q.get('path') or [''])[0]
                path=Path(raw).expanduser().resolve()
                roots=media_roots()
                if not any(is_under(path, root) for root in roots):
                    return self._json(403, {'ok':False,'error':'forbidden'})
                if not path.exists() or path.suffix.lower() not in MEDIA_EXTS:
                    return self._json(404, {'ok':False,'error':'not found'})
                data=path.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', mimetypes.guess_type(str(path))[0] or 'image/jpeg')
                self.send_header('Cache-Control','public, max-age=300')
                self.send_header('Content-Length',str(len(data)))
                self.end_headers(); self.wfile.write(data); return
            except Exception as e:
                return self._json(500, {'ok':False,'error':str(e)})
        if self.path.startswith('/api/hotspot/'):
            return self._hotspot_api()
        if self.path.startswith('/api/system/stats'):
            return self._json(200, get_system_stats())
        if self.path.startswith('/api/system/cleanup'):
            return self._json(200, get_cleanup_stats())
        if self.path.startswith('/api/agent/memory'):
            return self._json(200, get_agent_memory())
        if self.path.startswith('/api/system/speedtest'):
            return self._json(200, get_speedtest())
        if self.path.startswith('/api/admin/config'):
            token=self.headers.get('X-Dashboard-Token')
            if not valid_token(token):
                return self._json(401, {'ok':False,'error':'unauthorized'})
            try:
                cfg=read_json(CONFIG_PATH)
                return self._json(200, {'ok':True,'config':public_config(cfg)})
            except Exception as e:
                return self._json(500, {'ok':False,'error':str(e)})
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith('/api/chat'):
            return self._chat_proxy()
        if self.path.startswith('/api/admin/login'):
            try:
                data=self._body_json()
                if not ADMIN_PASSWORD:
                    return self._json(403, {'ok':False,'error':'admin password not configured'})
                if secrets.compare_digest(str(data.get('password','')), str(ADMIN_PASSWORD)):
                    token=secrets.token_urlsafe(32)
                    SESSIONS[token]=time.time()
                    return self._json(200, {'ok':True,'token':token,'ttl':SESSION_TTL})
                return self._json(403, {'ok':False,'error':'bad password'})
            except Exception as e:
                return self._json(400, {'ok':False,'error':str(e)})
        if self.path.startswith('/api/admin/config'):
            token=self.headers.get('X-Dashboard-Token')
            if not valid_token(token):
                return self._json(401, {'ok':False,'error':'unauthorized'})
            try:
                data=self._body_json()
                patch=data.get('config',{})
                cfg=read_json(CONFIG_PATH)
                new_cfg=merge_safe(cfg, patch)
                write_json(CONFIG_PATH,new_cfg)
                return self._json(200, {'ok':True,'config':public_config(new_cfg)})
            except Exception as e:
                return self._json(400, {'ok':False,'error':str(e)})
        return self._json(404, {'ok':False,'error':'not found'})

# ========== 热点数据爬取 ==========
_hotspot_cache = {'weibo': None, 'douyin': None, 'weibo_time': 0, 'douyin_time': 0}
HOTSPOT_CACHE_TTL = 120  # 2分钟缓存

def _fetch_json(url, headers=None, timeout=8):
    req = urllib.request.Request(url, headers=headers or {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', errors='ignore'))

def fetch_weibo_hot():
    global _hotspot_cache
    now = time.time()
    if _hotspot_cache['weibo'] and now - _hotspot_cache['weibo_time'] < HOTSPOT_CACHE_TTL:
        return _hotspot_cache['weibo']
    
    try:
        data = _fetch_json('https://weibo.com/ajax/side/hotSearch', headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://weibo.com/'
        })
        result = []
        for i, item in enumerate(data.get('data', {}).get('realtime', [])):
            if not item.get('word'):
                continue
            word = item.get('word', '')
            cat = _classify(word)
            
            # 热度等级
            num = item.get('num', 0)
            if num >= 1000000:
                heat = f'{num//10000}万'
            elif num > 0:
                heat = str(num)
            else:
                heat = '热'
            # 特殊标签
            label = item.get('label_name', '')
            if label == '爆':
                heat = '爆'
            elif label == '热':
                heat = '热'
            elif label == '新':
                heat = '新'
            
            trend = 'up'
            if item.get('category') == 'ad':
                continue
            
            result.append({
                'rank': len(result) + 1,
                'text': word,
                'heat': heat,
                'trend': trend,
                'isNew': label == '新',
                'category': cat,
                'raw': item.get('num', 0),
                'url': item.get('url') or ('https://s.weibo.com/weibo?q=' + urllib.parse.quote('#' + word + '#'))
            })
            if len(result) >= 20:
                break
        
        _hotspot_cache['weibo'] = result
        _hotspot_cache['weibo_time'] = now
        return result
    except Exception as e:
        # 失败返回缓存（如果有）
        if _hotspot_cache['weibo']:
            return _hotspot_cache['weibo']
        raise RuntimeError(f'微博热榜获取失败: {e}')

def fetch_douyin_hot():
    global _hotspot_cache
    now = time.time()
    if _hotspot_cache['douyin'] and now - _hotspot_cache['douyin_time'] < HOTSPOT_CACHE_TTL:
        return _hotspot_cache['douyin']
    
    try:
        data = _fetch_json('https://www.douyin.com/aweme/v1/web/hot/search/list/', headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://www.douyin.com/'
        })
        result = []
        word_list = data.get('data', {}).get('word_list', [])
        for i, item in enumerate(word_list):
            word = item.get('word', '')
            if not word:
                continue
            cat = _classify(word)
            
            hot_value = item.get('hot_value', 0)
            if hot_value >= 10000000:
                heat = f'{hot_value//10000}万'
            elif hot_value > 0:
                heat = f'{hot_value//10000}万'
            else:
                heat = '热'
            
            trend = 'up' if item.get('event_time', 0) > time.time() - 3600 else 'same'
            
            result.append({
                'rank': len(result) + 1,
                'text': word,
                'heat': heat,
                'trend': trend,
                'isNew': i < 3,
                'category': cat,
                'raw': hot_value,
                'url': 'https://www.douyin.com/search/' + urllib.parse.quote(word)
            })
            if len(result) >= 20:
                break
        
        _hotspot_cache['douyin'] = result
        _hotspot_cache['douyin_time'] = now
        return result
    except Exception as e:
        if _hotspot_cache['douyin']:
            return _hotspot_cache['douyin']
        raise RuntimeError(f'抖音热榜获取失败: {e}')

def fetch_zhihu_hot():
    global _hotspot_cache
    now = time.time()
    if _hotspot_cache.get('zhihu') and now - _hotspot_cache.get('zhihu_time', 0) < HOTSPOT_CACHE_TTL:
        return _hotspot_cache['zhihu']
    
    try:
        # 用知乎热榜的另一个公开接口
        data = _fetch_json('https://www.zhihu.com/api/v4/search/top_search', headers={
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1',
            'Referer': 'https://www.zhihu.com/'
        })
        result = []
        items = data.get('top_search', {}).get('words', [])
        for i, item in enumerate(items):
            query = item.get('query', '') or item.get('word', '')
            if not query:
                continue
            heat = item.get('hot_value', 0) or item.get('heat_score', 0)
            if heat >= 10000000:
                heat_text = f'{heat//10000}万'
            elif heat > 0:
                heat_text = f'{heat//10000}万'
            else:
                heat_text = '热'
            
            cat = _classify(query)
            
            result.append({
                'rank': len(result) + 1,
                'text': query,
                'heat': heat_text,
                'trend': 'up',
                'isNew': item.get('is_new', False) or i < 3,
                'category': cat,
                'raw': heat,
                'url': 'https://www.zhihu.com/search?type=content&q=' + urllib.parse.quote(query)
            })
            if len(result) >= 20:
                break
        
        _hotspot_cache['zhihu'] = result
        _hotspot_cache['zhihu_time'] = now
        return result
    except Exception as e:
        if _hotspot_cache.get('zhihu'):
            return _hotspot_cache['zhihu']
        raise RuntimeError(f'知乎热榜获取失败: {e}')

def fetch_bilibili_hot():
    global _hotspot_cache
    now = time.time()
    if _hotspot_cache.get('bilibili') and now - _hotspot_cache.get('bilibili_time', 0) < HOTSPOT_CACHE_TTL:
        return _hotspot_cache['bilibili']
    
    try:
        data = _fetch_json('https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1', headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://www.bilibili.com/'
        })
        result = []
        for i, item in enumerate(data.get('data', {}).get('list', [])):
            title = item.get('title', '')
            if not title:
                continue
            stat = item.get('stat', {})
            heat = stat.get('view', 0)
            if heat >= 10000000:
                heat_text = f'{heat//10000}万'
            elif heat >= 10000:
                heat_text = f'{heat//10000}万'
            else:
                heat_text = str(heat)
            
            cat = _classify(title)
            
            result.append({
                'rank': len(result) + 1,
                'text': title,
                'heat': heat_text,
                'trend': 'up',
                'isNew': i < 2,
                'category': cat,
                'raw': heat,
                'url': ('https://www.bilibili.com/video/' + item.get('bvid')) if item.get('bvid') else (item.get('short_link_v2') or 'https://www.bilibili.com/v/popular/all')
            })
            if len(result) >= 20:
                break
        
        _hotspot_cache['bilibili'] = result
        _hotspot_cache['bilibili_time'] = now
        return result
    except Exception as e:
        if _hotspot_cache.get('bilibili'):
            return _hotspot_cache['bilibili']
        raise RuntimeError(f'B站热榜获取失败: {e}')

def fetch_toutiao_hot():
    global _hotspot_cache
    now = time.time()
    if _hotspot_cache.get('toutiao') and now - _hotspot_cache.get('toutiao_time', 0) < HOTSPOT_CACHE_TTL:
        return _hotspot_cache['toutiao']
    
    try:
        data = _fetch_json('https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc', headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://www.toutiao.com/'
        })
        result = []
        for i, item in enumerate(data.get('data', [])):
            title = item.get('Title', '')
            if not title:
                continue
            hot = item.get('HotValue', 0)
            try:
                hot = int(hot)
            except (ValueError, TypeError):
                hot = (20 - i) * 100000
            if hot >= 10000000:
                heat_text = f'{hot//10000}万'
            elif hot >= 10000:
                heat_text = f'{hot//10000}万'
            else:
                heat_text = str(hot)
            
            cat = _classify(title)
            
            result.append({
                'rank': len(result) + 1,
                'text': title,
                'heat': heat_text,
                'trend': 'up',
                'isNew': item.get('IsNew', False) or i < 2,
                'category': cat,
                'raw': hot,
                'url': item.get('Url') or ('https://www.toutiao.com/search/?keyword=' + urllib.parse.quote(title))
            })
            if len(result) >= 20:
                break
        
        _hotspot_cache['toutiao'] = result
        _hotspot_cache['toutiao_time'] = now
        return result
    except Exception as e:
        if _hotspot_cache.get('toutiao'):
            return _hotspot_cache['toutiao']
        raise RuntimeError(f'头条热榜获取失败: {e}')

def fetch_github_trending():
    global _hotspot_cache
    now = time.time()
    if _hotspot_cache.get('github') and now - _hotspot_cache.get('github_time', 0) < HOTSPOT_CACHE_TTL * 3:
        return _hotspot_cache['github']
    
    try:
        # GitHub trending 需要解析 HTML
        import urllib.request
        req = urllib.request.Request('https://github.com/trending', headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode('utf-8', errors='ignore')
        
        result = []
        import re
        # 简单正则解析 trending 项目
        articles = re.findall(r'<article class="Box-row">.*?</article>', html, re.DOTALL)
        for i, art in enumerate(articles[:20]):
            # repo 名
            name_match = re.search(r'h2[^>]*>.*?<a[^>]*href="/([^"]+)"', art, re.DOTALL)
            if not name_match:
                continue
            repo = name_match.group(1).strip()
            # description
            desc_match = re.search(r'<p[^>]*class="col-9[^"]*"[^>]*>(.*?)</p>', art, re.DOTALL)
            desc = desc_match.group(1).strip() if desc_match else ''
            desc = re.sub(r'<[^>]+>', '', desc).strip()
            # stars 今日
            star_match = re.search(r'<span[^>]*>.*?(\d[\d,]*).*?stars? today', art, re.DOTALL | re.IGNORECASE)
            stars_today = star_match.group(1) if star_match else '?'
            
            result.append({
                'rank': len(result) + 1,
                'text': repo + (f' — {desc[:40]}' if desc else ''),
                'heat': f'{stars_today}★/day',
                'trend': 'up',
                'isNew': i < 3,
                'category': 'tech',
                'raw': i,
                'url': 'https://github.com/' + repo
            })
            if len(result) >= 15:
                break
        
        _hotspot_cache['github'] = result
        _hotspot_cache['github_time'] = now
        return result
    except Exception as e:
        if _hotspot_cache.get('github'):
            return _hotspot_cache['github']
        raise RuntimeError(f'GitHub Trending获取失败: {e}')

def _classify(text):
    """关键词分类引擎：按 科技/财经/娱乐/国际/社会 优先级判定"""
    kw_map = {
        'tech': ['芯片', 'AI', '人工智能', '大模型', '手机', '华为', '苹果', '特斯拉', '科技', '互联网', '算法', '算力', '机器人', '量子', '元宇宙', '代码', '程序', '开源', 'GitHub', '软件', '5G', '6G', '半导体', 'GPU', 'CPU', '小米', 'OpenAI', 'ChatGPT', '英伟达', '马斯克', '自动驾驶', '新能源', '电动车', '字节', '腾讯', '阿里', '百度', '鸿蒙'],
        'finance': ['A股', '美股', '港股', '股市', '基金', '经济', 'GDP', '降息', '加息', '比特币', '美元', '汇率', '楼市', '房价', '银行', '投资', '股票', '证券', '金融', '货币', '通胀', '美联储', '央行', '理财', '黄金', '原油', '期货', '上市', 'IPO', '消费'],
        'ent': ['明星', '电影', '综艺', '演唱会', '歌手', '演员', '导演', '剧', '娱乐', '游戏', '动漫', '网红', '选秀', '偶像', '粉丝', '票房', '音乐', '电竞', '主播', '直播', '娱乐圈', '上映', '首播', '凡人修仙', '番剧'],
        'world': ['美国', '俄罗斯', '乌克兰', '欧盟', '联合国', '国际', '全球', '日本', '韩国', '中东', '欧洲', '外交', '军事', '战争', '制裁', '北约', 'G7', 'G20', '海外', '以色列', '巴勒斯坦', '伊朗', '朝鲜', '印度', '总统', '首相', '白宫', '普京', '特朗普', '秘鲁'],
        'society': ['高考', '考试', '教育', '医疗', '天气', '地震', '台风', '暴雨', '高温', '交通', '安全', '事故', '政策', '民生', '就业', '养老', '食品', '春节', '端午', '中秋', '国庆', '疫情', '病毒', '警方', '法院', '女足', '男足']
    }
    for cat_name in ('tech', 'finance', 'ent', 'world', 'society'):
        if any(kw in text for kw in kw_map[cat_name]):
            return cat_name
    return 'society'

def fetch_all_hotspot():
    """批量获取所有源，返回汇总数据"""
    sources = {}
    errors = {}
    for name, func in [('weibo', fetch_weibo_hot), ('douyin', fetch_douyin_hot), 
                        ('zhihu', fetch_zhihu_hot), ('bilibili', fetch_bilibili_hot),
                        ('toutiao', fetch_toutiao_hot), ('github', fetch_github_trending)]:
        try:
            sources[name] = func()
        except Exception as e:
            errors[name] = str(e)
            sources[name] = []
    
    # 计算预警数（所有源前10名中，标记为new或热度>100万的）
    alert_count = 0
    hot_count = 0
    total_events = 0
    for name, lst in sources.items():
        total_events += len(lst)
        for item in lst[:10]:
            if item.get('isNew'):
                alert_count += 1
            heat_str = str(item.get('heat', ''))
            if '万' in heat_str and int(heat_str.replace('万', '')) > 500:
                hot_count += 1
    
    return {
        'sources': sources,
        'stats': {
            'alert': max(3, alert_count),
            'hot': max(5, hot_count),
            'total': total_events,
            'sources_available': len([k for k, v in sources.items() if len(v) > 0]),
            'errors': errors
        }
    }

ThreadingHTTPServer((os.environ.get('DASHBOARD_HOST','127.0.0.1'), int(os.environ.get('DASHBOARD_PORT','8765'))), Handler).serve_forever()
