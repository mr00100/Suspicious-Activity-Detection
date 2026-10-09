"""SAD Local Hardware / Device Agent.

This agent is intentionally separate from the web application's restricted CLI.
It exposes only localhost APIs for real hardware/device integration:
- /radar             receive authorized ESP32/Arduino radar data
- /bluetooth/scan    real BLE discovery through Bleak (if supported)
- /wifi/scan         real nearby Wi-Fi network discovery
- /wifi/connect      connect to a Wi-Fi network using the OS network manager
- /lan/scan          inspect the local machine's neighbor/ARP table

No fake devices or radar targets are generated. Only localhost is bound.
"""
import asyncio, json, os, platform, re, subprocess, tempfile, time
from flask import Flask, request, jsonify
from flask_sock import Sock
from flask_cors import CORS

app=Flask(__name__); CORS(app, resources={r"/*":{"origins":"*"}}); sock=Sock(app)
clients=set(); latest={"type":"radar","targets":[]}

def run_cmd(args, timeout=15):
    p=subprocess.run(args, capture_output=True, text=True, timeout=timeout, shell=False)
    return p.returncode, p.stdout, p.stderr

@app.post('/radar')
def radar():
    global latest
    data=request.get_json(silent=True) or {}; targets=data.get('targets',[])
    if not isinstance(targets,list): return jsonify(error='targets must be an array'),400
    clean=[]
    for t in targets[:100]:
        try: clean.append({'id':t.get('id'),'angle':float(t.get('angle',0)),'distance':float(t.get('distance',0)),'strength':float(t.get('strength',0))})
        except (TypeError,ValueError): continue
    latest={'type':'radar','targets':clean}; payload=json.dumps(latest)
    for ws in list(clients):
        try: ws.send(payload)
        except Exception: clients.discard(ws)
    return jsonify(ok=True,target_count=len(clean))

@app.get('/health')
def health(): return jsonify(ok=True,targets=len(latest['targets']),clients=len(clients))

@app.get('/radio/bluetooth/status')
def bt_status():
    try:
        from bleak import BleakScanner
        # There is no portable Bleak API for toggling Windows Bluetooth power.
        # Report adapter availability without pretending the radio is on.
        try:
            adapters = asyncio.run(BleakScanner.discover(timeout=0.5))
            return jsonify(available=True,powered=True,devices_seen=len(adapters),limitation='Bluetooth must be enabled by the user in Windows.')
        except Exception as scan_error:
            return jsonify(available=True,powered=False,error=str(scan_error),limitation='Turn Bluetooth ON in Windows, then scan again.')
    except Exception as e:
        return jsonify(available=False,powered=False,error=str(e))

@app.get('/bluetooth/scan')
def bt_scan():
    try:
        from bleak import BleakScanner
        devices=asyncio.run(BleakScanner.discover(timeout=8.0))
        unique={}
        for d in devices:
            addr=str(getattr(d,'address','') or '').strip()
            if not addr: continue
            if addr.lower() in unique: continue
            unique[addr.lower()]={'name':d.name or 'Unknown Bluetooth Device','address':addr,'rssi':getattr(d,'rssi',None),'id':addr,'real':True}
        out=list(unique.values())
        return jsonify(ok=True,devices=out,count=len(out),note='Only devices actually returned by the local Bluetooth adapter are shown; no synthetic devices are added.')
    except ImportError: return jsonify(error='Bluetooth scanning requires the Bleak package. Install requirements.txt.'),503
    except Exception as e: return jsonify(error=f'Bluetooth adapter unavailable: {e}'),503

@app.get('/radio/wifi/status')
def wifi_status():
    try:
        if platform.system()=='Windows':
            code,out,err=run_cmd(['netsh','wlan','show','interfaces'])
            return jsonify(available=code==0,powered=code==0,raw=out[:1000])
        code,out,err=run_cmd(['nmcli','radio','wifi'])
        return jsonify(available=code==0,powered=out.strip().lower()=='enabled')
    except Exception as e: return jsonify(available=False,powered=False,error=str(e))

def scan_windows_wifi():
    code,out,err=run_cmd(['netsh','wlan','show','networks','mode=bssid'])
    if code!=0: raise RuntimeError(err.strip() or 'netsh Wi-Fi scan failed')
    networks=[]; current=None
    for raw in out.splitlines():
        line=raw.strip()
        m=re.match(r'^SSID\s+\d+\s*:\s*(.*)$',line,re.I)
        if m:
            if current: networks.append(current)
            current={'ssid':m.group(1).strip(),'signal':None,'auth':None,'bssid':None}; continue
        if current:
            m=re.match(r'^Signal\s*:\s*(\d+)%$',line,re.I)
            if m: current['signal']=int(m.group(1)); continue
            m=re.match(r'^Authentication\s*:\s*(.*)$',line,re.I)
            if m: current['auth']=m.group(1).strip(); continue
            m=re.match(r'^BSSID\s+\d+\s*:\s*(.*)$',line,re.I)
            if m and not current.get('bssid'): current['bssid']=m.group(1).strip()
    if current: networks.append(current)
    return networks

def scan_linux_wifi():
    code,out,err=run_cmd(['nmcli','-t','-f','SSID,SIGNAL,SECURITY,BSSID','device','wifi','list'])
    if code!=0: raise RuntimeError(err.strip() or 'nmcli Wi-Fi scan failed')
    outn=[]
    for line in out.splitlines():
        parts=line.split(':')
        if len(parts)>=4: outn.append({'ssid':parts[0],'signal':int(parts[1] or 0),'auth':parts[2],'bssid':parts[3]})
    return outn

@app.get('/wifi/scan')
def wifi_scan():
    try:
        networks=scan_windows_wifi() if platform.system()=='Windows' else scan_linux_wifi()
        # Keep each observed access point/BSSID. Multiple BSSIDs can legitimately
        # advertise the same SSID, and hiding them made scans look like only one device.
        unique={}
        for n in networks:
            key=(n.get('bssid') or '').lower() or f"{n.get('ssid','')}|{n.get('signal','')}"
            if key not in unique: unique[key]=n
        return jsonify(ok=True,networks=list(unique.values()),count=len(unique))
    except Exception as e: return jsonify(error=f'Wi-Fi scan unavailable: {e}'),503

@app.post('/wifi/connect')
def wifi_connect():
    d=request.get_json(silent=True) or {}; ssid=str(d.get('ssid','')).strip(); password=str(d.get('password',''))
    if not ssid or len(ssid)>128: return jsonify(error='Invalid Wi-Fi network name.'),400
    if len(password)>256: return jsonify(error='Password is too long.'),400
    try:
        if platform.system()=='Windows':
            # Create a temporary WPA2/WPA3 profile locally, import it, connect, then delete the file.
            import xml.sax.saxutils as sax
            profile=f'''<?xml version="1.0"?>\n<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">\n<name>{sax.escape(ssid)}</name>\n<SSIDConfig><SSID><name>{sax.escape(ssid)}</name></SSID></SSIDConfig>\n<connectionType>ESS</connectionType><connectionMode>auto</connectionMode>\n<MSM><security><authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption><useOneX>false</useOneX></authEncryption><sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>{sax.escape(password)}</keyMaterial></sharedKey></security></MSM>\n</WLANProfile>'''
            fd,path=tempfile.mkstemp(suffix='.xml');os.close(fd)
            try:
                open(path,'w',encoding='utf-8').write(profile)
                code,out,err=run_cmd(['netsh','wlan','add','profile',f'filename={path}','user=current'])
                if code!=0: raise RuntimeError(err.strip() or out.strip() or 'Could not add Wi-Fi profile')
                code,out,err=run_cmd(['netsh','wlan','connect',f'name={ssid}'])
                if code!=0: raise RuntimeError(err.strip() or out.strip() or 'Could not connect to Wi-Fi')
            finally:
                try: os.remove(path)
                except OSError: pass
        else:
            code,out,err=run_cmd(['nmcli','device','wifi','connect',ssid,'password',password])
            if code!=0: raise RuntimeError(err.strip() or out.strip() or 'Could not connect to Wi-Fi')
        return jsonify(ok=True,ssid=ssid)
    except Exception as e: return jsonify(error=f'Wi-Fi connection failed: {e}'),503

@app.get('/lan/scan')
def lan_scan():
    try:
        if platform.system()=='Windows': code,out,err=run_cmd(['arp','-a'])
        else: code,out,err=run_cmd(['ip','neigh'])
        if code!=0: raise RuntimeError(err.strip() or 'LAN neighbor scan failed')
        return jsonify(ok=True,raw=out)
    except Exception as e: return jsonify(error=f'LAN scan unavailable: {e}'),503

@sock.route('/ws')
def ws_route(ws):
    clients.add(ws)
    try:
        ws.send(json.dumps(latest))
        while True:
            msg=ws.receive()
            if msg is None: break
    finally: clients.discard(ws)

if __name__=='__main__': app.run(host='127.0.0.1',port=8765,debug=False)
