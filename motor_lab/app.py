#!/usr/bin/env python3
"""Motor Lab 2.0 — persistent local dashboard and attended dyno controller.

Hardware: python3 app.py --hardware
Preview:  python3 app.py --demo --host 127.0.0.1 --port 8877
Results:  python3 app.py --results-only
No mode automatically starts a motor or sweep. No motor supply cutoff exists.
"""
import argparse
import csv
import html
import io
import ipaddress
import json
import math
import queue
import re
import secrets
import signal
import socket
import threading
import time
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, unquote

import dyno_core as core

VERSION = '2.0'
RUN_ID = re.compile(r'^\d{8}_\d{6}_\d{6}$')
ACTIVE = {'starting','release','route_wait','hold'}
TERMINAL = {'finished','aborted'}
FILES = ('samples.csv','points.csv','baseline.csv','metadata.json')
FIELDS = ('leg','load','resistance_ohm','hold_s','rpm','voltage','current','input_W',
          'torque_mNm_provisional','mechanical_W_apparent','efficiency_pct_apparent',
          'quality','torque_correction_factor')

def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError,ValueError):
        return None

def clean(value):
    if isinstance(value,float) and not math.isfinite(value): return None
    if isinstance(value,dict): return {k:clean(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [clean(v) for v in value]
    return value

def read_rows(path):
    if not path.exists(): return []
    if path.is_symlink(): raise ValueError('Linked recording files are not supported')
    if path.stat().st_size > 80*1024*1024:
        raise ValueError('Recording exceeds the browser processing limit; download its original CSV.')
    with path.open(newline='') as stream:
        return [r for r in csv.DictReader(stream) if None not in r and all(v is not None for v in r.values())]

def torque_correction(metadata):
    """Only correct the explicitly known old 7.4 mm scale; never guess."""
    recorded = number(metadata.get('torque_Nm_per_count'))
    if recorded is not None and math.isclose(recorded,-.0007256921/170499,rel_tol=1e-6):
        return 10., 'Historical 7.4 mm scale corrected to 74 mm for this view and derived downloads. Originals are unchanged.'
    if recorded is not None and math.isclose(recorded,core.TORQUE_PER_COUNT,rel_tol=1e-6):
        return 1., '74 mm calibration recorded by this program. No additional scaling applied.'
    return 1., 'Unknown torque scale: displayed as recorded. Verify calibration before interpreting torque or efficiency.'

def apparent(torque_nm,rpm,input_power):
    if None in (torque_nm,rpm,input_power) or rpm < 300 or torque_nm < 0 or input_power <= .1:
        return None,None
    mechanical = torque_nm*rpm*2*math.pi/60
    return mechanical,mechanical/input_power*100

def derive_point(row,factor):
    torque = number(row.get('mean_torque_Nm_direction_baseline'))
    torque = None if torque is None else torque*factor
    rpm = number(row.get('mean_rpm'))
    power = number(row.get('mean_electrical_input_W'))
    mechanical,efficiency = apparent(torque,rpm,power)
    return dict(leg=row.get('sweep_leg',''),load=number(row.get('commanded_load')),
                resistance_ohm=number(row.get('nominal_load_ohm')),hold_s=number(row.get('hold_s')),
                rpm=rpm,voltage=number(row.get('mean_voltage_V')),current=number(row.get('mean_current_A')),
                input_W=power,torque_mNm_provisional=None if torque is None else torque*1000,
                mechanical_W_apparent=mechanical,efficiency_pct_apparent=efficiency,
                quality=row.get('quality','unverified'),torque_correction_factor=factor)

class Results:
    def __init__(self,root):
        self.root=Path(root).expanduser().resolve()
        self.root.mkdir(parents=True,exist_ok=True)

    def folder(self,identifier):
        if not RUN_ID.fullmatch(identifier): raise ValueError('Invalid recording identifier')
        p=self.root/identifier
        if p.is_symlink() or p.resolve().parent!=self.root or not p.is_dir():
            raise FileNotFoundError('Recording not found')
        return p

    def metadata(self,folder):
        try:
            value=json.loads((folder/'metadata.json').read_text())
            return value if isinstance(value,dict) else {}
        except (OSError,ValueError):
            return {}

    def listing(self):
        items=[]
        for p in sorted(self.root.iterdir(),reverse=True):
            if not RUN_ID.fullmatch(p.name) or p.is_symlink() or not p.is_dir(): continue
            m=self.metadata(p)
            point_count=0
            points_file=p/'points.csv'
            if points_file.exists() and not points_file.is_symlink():
                with points_file.open() as stream:
                    point_count=max(0,sum(1 for _ in stream)-1)
            items.append(dict(id=p.name,started=m.get('started',p.name),voltage=m.get('nominal_voltage'),
                              result=m.get('result','incomplete / interrupted'),demo=bool(m.get('demo')),
                              point_count=point_count))
        return items

    def detail(self,identifier):
        folder=self.folder(identifier)
        metadata=self.metadata(folder)
        factor,note=torque_correction(metadata)
        points=[derive_point(r,factor) for r in read_rows(folder/'points.csv')]
        sample_rows=read_rows(folder/'samples.csv')
        history=[]
        for row in sample_rows:
            t=number(row.get('elapsed_s'))
            if t is None: continue
            torque=number(row.get('torque_Nm_direction_baseline'))
            history.append(dict(t=t,rpm=number(row.get('rpm')),current=number(row.get('current_A')),
                                voltage=number(row.get('voltage_V')),power=number(row.get('electrical_input_W')),
                                torque=None if torque is None else torque*factor*1000,
                                load=number(row.get('commanded_load')),phase=row.get('phase','')))
        # Separate every point in long files, without averaging away extremes.
        if len(history)>6000:
            stride=math.ceil(len(history)/2000)
            selected=set([0,len(history)-1])
            for i in range(0,len(history),stride):
                block=history[i:i+stride]
                for key in ('rpm','current','torque'):
                    valid=[j for j,r in enumerate(block) if r[key] is not None]
                    if valid:
                        selected.add(i+min(valid,key=lambda j:block[j][key]))
                        selected.add(i+max(valid,key=lambda j:block[j][key]))
            history=[history[i] for i in sorted(selected)]
        baseline=[]
        for row in read_rows(folder/'baseline.csv'):
            t,raw=number(row.get('elapsed_s')),number(row.get('raw_counts'))
            if t is not None and raw is not None: baseline.append(dict(t=t,raw=raw))
        warnings=[note,'Torque uses a direction-specific baseline and provisional force calibration. Apparent efficiency is not a validated motor-efficiency measurement.',
                  'Mechanical power in the table uses mean torque × mean RPM. Short holds and asynchronous sensors can affect this estimate.',
                  'Commanded relay settings do not verify physical contact positions. No motor cutoff or temperature measurement exists.']
        if metadata.get('result')!='completed':
            warnings.append('This is not a completed sweep. It may be ongoing, aborted or interrupted; tables can contain only part of the planned sequence.')
        if any((p['efficiency_pct_apparent'] or 0)>100 for p in points):
            warnings.append('Some apparent efficiencies exceed 100%; those values indicate a measurement/calibration problem, not real conversion efficiency.')
        currents=[p['current'] for p in points if p['current'] is not None]
        if currents and max(currents)>1.8:
            warnings.append('This recording approached the 1.9 A software limit. Do not increase load or voltage on the strength of this result.')
        if any(p['load'] in (6,7) for p in points):
            warnings.append('Settings 6 and 7 are short transient observations, not verified steady-state performance.')
        if metadata.get('application_version')!='2.0':
            warnings.append('Historical recording: a completed flag does not by itself verify mechanical calibration or power stability.')
        return clean(dict(id=identifier,metadata=metadata,points=points,history=history,baseline=baseline,
                          correction_factor=factor,warnings=warnings,files=[f for f in FILES if (folder/f).exists()]))

    def corrected_csv(self,detail):
        stream=io.StringIO(newline='')
        writer=csv.DictWriter(stream,fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(detail['points'])
        return stream.getvalue().encode()

    def report(self,detail):
        def display(value):
            return '—' if value is None else f'{value:.3f}' if isinstance(value,(float,int)) else html.escape(str(value))
        headers=['Leg','Load','RPM','V','A','Input W','Torque mN·m*','Mechanical W*','Efficiency %*']
        keys=['leg','load','rpm','voltage','current','input_W','torque_mNm_provisional','mechanical_W_apparent','efficiency_pct_apparent']
        table='<table><tr>'+''.join('<th>'+x+'</th>' for x in headers)+'</tr>'
        for p in detail['points']:
            table+='<tr>'+''.join('<td>'+display(p[k])+'</td>' for k in keys)+'</tr>'
        table+='</table>'
        plots=''
        for key,title,color in [('rpm','RPM','#e65570'),('current','Current (A)','#bb851e'),('input_W','Electrical input (W)','#8262cc'),('torque_mNm_provisional','Provisional torque (mN·m)','#21826c')]:
            valid=[p for p in detail['points'] if p[key] is not None and p['load'] is not None]
            if not valid: continue
            lo=min(0,min(p[key] for p in valid));hi=max(p[key] for p in valid)
            if hi==lo:hi=lo+1
            hi+=(hi-lo)*.1
            svg=f'<svg viewBox="0 0 600 240" role="img" aria-label="{title}"><rect width="600" height="240" fill="#fff"/>'
            for j in range(5):
                y=20+j*45
                svg+=f'<path d="M65 {y}H575" stroke="#e0e7ef"/><text x="58" y="{y+4}" text-anchor="end" font-size="11">{hi-(hi-lo)*j/4:.2f}</text>'
            for j in range(8):svg+=f'<text x="{65+j*510/7}" y="220" text-anchor="middle" font-size="12">{j}</text>'
            for legs,dash in [({'reference','up'},''),({'down','final'},'5 4')]:
                group=[p for p in valid if p['leg'] in legs]
                points=' '.join(f"{65+p['load']*510/7},{20+(hi-p[key])/(hi-lo)*180}" for p in group)
                svg+=f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2" stroke-dasharray="{dash}"/>'
                for p in group:svg+=f'<circle cx="{65+p["load"]*510/7}" cy="{20+(hi-p[key])/(hi-lo)*180}" r="4" fill="{color}"/>'
            plots+='<section><h3>'+title+'</h3>'+svg+'</svg><small>Load setting · solid: increasing · dashed: decreasing</small></section>'
        warnings=''.join('<li>'+html.escape(x)+'</li>' for x in detail['warnings'])
        metadata=html.escape(json.dumps(detail['metadata'],indent=2))
        return ('<!doctype html><meta charset="utf-8"><title>Motor Lab results</title><style>body{font:16px/1.6 system-ui;max-width:1200px;margin:auto;padding:25px;color:#20334b;background:#f4f7fb}table{border-collapse:collapse;font-size:13px;width:100%}th,td{padding:9px;border-bottom:1px solid #cad4e2;text-align:right}section{background:white;padding:15px;border-radius:12px}svg{width:100%}.plots{display:grid;grid-template-columns:1fr 1fr;gap:15px}pre{white-space:pre-wrap;overflow-wrap:anywhere}small{color:#5b6a82}@media(max-width:700px){.plots{grid-template-columns:1fr}}</style>'
                '<h1>Motor Lab · saved results</h1><p>'+html.escape(detail['id'])+'</p><ul>'+warnings+'</ul><div class="plots">'+plots+'</div><h2>All points</h2><div style="overflow:auto">'+table+'</div><p>* Provisional torque and apparent mechanical power/efficiency. Not verified motor performance.</p><h2>Original metadata</h2><pre>'+metadata+'</pre>').encode()

    def archive(self,detail):
        folder=self.folder(detail['id'])
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
            for filename in FILES:
                path=folder/filename
                if path.is_file() and not path.is_symlink():z.write(path,'original_data/'+filename)
            z.writestr('results_corrected.csv',self.corrected_csv(detail))
            z.writestr('report.html',self.report(detail))
            z.writestr('display_notes.json',json.dumps({'correction_factor':detail['correction_factor'],'warnings':detail['warnings']},indent=2))
        return output.getvalue()

class DashboardRun(core.Run):
    def __init__(self,hardware,args):
        super().__init__(hardware,args)
        self.metadata.update(max_load=args.max_load,application_version=VERSION,
                             torque_measurement='provisional direction-baseline reaction measurement; force path unverified')
        self.write_metadata()

class Lab:
    def __init__(self,args):
        self.args=args
        self.results=Results(args.output)
        self.commands=queue.Queue(maxsize=8)
        self.lock=threading.Lock()
        self.stop_load=threading.Event()
        self.secret=secrets.token_urlsafe(24)
        self.owner=None
        self.heartbeat=0.
        self.run=None
        self.sealed=False
        self.hardware=None
        self.hardware_error=None
        self.notice='Motor supply OFF. Start a new test when ready.'
        self.state={}
        self.hardware_sample=None
        self.connect()

    def connect(self):
        if self.args.results_only:return
        try:
            self.hardware=core.Demo() if self.args.demo else core.Hardware()
            self.hardware_error=None
        except Exception as error:
            self.hardware_error=str(error)
            self.notice='Hardware unavailable. Results remain accessible. Stop other GPIO programs before retrying.'

    def motor_off(self,confirmation=False):
        if not confirmation: raise ValueError('Confirm motor power is OFF first.')
        if self.hardware is None: raise ValueError('Hardware is unavailable.')
        s=self.hardware.sample()
        if s['sensor_age']>.9:
            raise ValueError('Cannot confirm motor voltage: electrical sensor data is stale.')
        if s['voltage']>.2 or abs(s['current'])>.03 or s['rpm']>0:
            raise ValueError('Turn motor supply OFF, release shaft and wait for it to stop.')

    def execute(self,command):
        action=command['action']
        if action=='stop':
            self.stop_load.set();return
        if action=='new':
            if self.args.results_only:raise ValueError('Results-only mode cannot run tests.')
            if self.run and self.run.phase not in TERMINAL|{'idle','baseline_failed'}:
                raise ValueError('Stop or complete the current session before starting a new one.')
            self.motor_off(command.get('motor_off_confirmed') is True)
            voltage=number(command.get('voltage'))
            max_load=number(command.get('max_load'))
            if voltage not in (6.,9.,12.) or max_load is None or max_load!=int(max_load) or not 1<=max_load<=7:
                raise ValueError('Choose 6, 9 or 12 V and maximum load 1–7.')
            if self.run and not self.sealed:self.run.close()
            core.SEQUENCE=[(0,5,'reference')]+[(i,2 if i==7 else 3 if i==6 else 5,'up') for i in range(1,int(max_load)+1)]+[(i,3 if i==6 else 5,'down') for i in range(int(max_load)-1,0,-1)]+[(0,5,'final')]
            settings=argparse.Namespace(output=str(self.results.root),demo=self.args.demo,voltage=voltage,current_limit=1.9,max_load=int(max_load))
            if self.args.demo:self.hardware.nominal=voltage
            self.hardware.master(False)
            self.run=DashboardRun(self.hardware,settings)
            self.owner=command['client']
            self.heartbeat=time.monotonic()
            self.stop_load.clear()
            self.sealed=False
            self.notice='New test ready. Capture the same-direction slow-turn baseline, with motor power OFF.'
            return
        if action=='recover':
            if self.run and self.run.phase not in TERMINAL|{'idle','baseline_failed'}:
                raise ValueError('Stop the test before reconnecting sensors.')
            if command.get('motor_off_confirmed') is not True:
                raise ValueError('Confirm motor power is OFF before reconnecting sensors.')
            if self.hardware:
                s=self.hardware.sample()
                if (s['sensor_age']<.9 and s['voltage']>.2) or s['rpm']>0:
                    raise ValueError('Motor is not stopped/powered off.')
            if self.run and not self.sealed:self.run.close()
            if self.hardware:self.hardware.close()
            self.hardware=None;self.run=None;self.sealed=False;self.owner=None
            self.connect()
            self.notice='Sensors reconnected. Wait for live readings, then start a new test.'
            return
        if action=='demo_off':
            if not self.args.demo:raise ValueError('Simulation control is unavailable on hardware.')
            self.hardware.motor=False
            return
        if self.run is None:raise ValueError('Start a new test first.')
        if command.get('client')!=self.owner:
            raise ValueError('This test belongs to another browser tab. STOP remains available.')
        if action=='baseline':
            self.run.commands.put_nowait({'action':'baseline','direction':command.get('direction')})
        elif action=='start':
            self.motor_off(command.get('motor_off_confirmed') is True)
            if self.run.phase!='ready':raise ValueError('Complete baseline capture first.')
            self.run.commands.put_nowait({'action':'start'})
        else:raise ValueError('Unknown action')

    def tick(self):
        now=time.monotonic()
        if self.stop_load.is_set():
            if self.hardware:self.hardware.master(False)
            if self.run and self.run.phase not in TERMINAL:
                self.run.abort('STOP button pressed')
            self.stop_load.clear()
        # At most one potentially slow command per tick. Never allow download
        # or historical file parsing on this control thread.
        try:command=self.commands.get_nowait()
        except queue.Empty:command=None
        if command:
            try:self.execute(command)
            except Exception as error:self.notice=str(error)
        if self.run and not self.sealed:
            try:
                if self.run.phase in ACTIVE and now-self.heartbeat>3:
                    self.run.abort('Operator dashboard heartbeat lost for 3 seconds')
                self.run.tick()
            except Exception as error:
                self.run.abort(str(error))
            if self.run.phase in TERMINAL:
                self.run.close()
                self.sealed=True
        sample=self.hardware.sample() if self.hardware else None
        self.hardware_sample=sample
        live=None
        if sample:
            torque=None if self.run is None or self.run.zero is None or sample['raw_age']>.9 else (sample['raw']-self.run.zero)*core.TORQUE_PER_COUNT*1000
            electrical_fresh=sample['sensor_age']<.9
            live=dict(rpm=sample['rpm'],voltage=sample['voltage'] if electrical_fresh else None,
                      current=sample['current'] if electrical_fresh else None,
                      power=sample['voltage']*sample['current'] if electrical_fresh else None,
                      torque=torque,raw=sample['raw'] if sample['raw_age']<.9 else None,
                      sensor_age=sample['sensor_age'],hx_age=sample['raw_age'],error=sample['error'])
        run_state={}
        if self.run:
            with self.run.guard:run_state=dict(self.run.state)
        phase=self.run.phase if self.run else 'offline' if self.hardware is None else 'idle'
        message=run_state.get('message',self.notice)
        ready=bool(sample and not sample['error'] and sample['sensor_age']<.9 and sample['raw_age']<.9)
        if not ready and phase in ('idle','baseline_failed'):
            message='Sensors not ready: '+((sample or {}).get('error') or self.hardware_error or 'waiting for fresh readings')
        with self.lock:
            self.state=clean(dict(version=VERSION,phase=phase,message=message,notice=self.notice,live=live,ready=ready,
                                 run=run_state,run_id=self.run.folder.name if self.run else None,owner=self.owner,
                                 csrf=self.secret,demo=self.args.demo,results_only=self.args.results_only,
                                 max_load=self.run.args.max_load if self.run else 5,
                                 nominal=self.run.args.voltage if self.run else 6,baseline_distance_mm=74,
                                 baseline_mass_g=10,baseline_provisional=True))

    def close(self):
        if self.hardware:
            try:self.hardware.master(False)
            except Exception:pass
        if self.run and not self.sealed:
            self.run.result='interrupted: application stopped'
            self.run.close()
        if self.hardware:self.hardware.close()

def handler_for(lab):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def valid_host(self):
            host=self.headers.get('Host','').split(':')[0].lower().rstrip('.')
            hostname=socket.gethostname().lower()
            if host in {'localhost','raspberrypi','raspberrypi.local',hostname,hostname+'.local'}:return True
            try:
                address=ipaddress.ip_address(host)
                return address.is_private or address.is_loopback
            except ValueError:return False
        def send_bytes(self,status,payload,kind,filename=None):
            if isinstance(payload,str):payload=payload.encode()
            self.send_response(status)
            self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(payload)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('X-Frame-Options','DENY')
            self.send_header('Referrer-Policy','same-origin')
            if filename:self.send_header('Content-Disposition','attachment; filename="'+filename+'"')
            self.end_headers()
            try:self.wfile.write(payload)
            except (BrokenPipeError,ConnectionResetError):pass
        def json(self,value,status=200):
            self.send_bytes(status,json.dumps(clean(value),allow_nan=False),'application/json')
        def do_GET(self):
            if not self.valid_host():
                self.json({'error':'Use the Pi hostname or its local IP address.'},403);return
            path=unquote(urlsplit(self.path).path)
            try:
                if path in ('/','/results'):
                    self.send_bytes(200,Path(__file__).with_name('dashboard.html').read_bytes(),'text/html; charset=utf-8')
                elif path=='/api/state':
                    with lab.lock:value=dict(lab.state)
                    self.json(value)
                elif path=='/api/results':self.json(lab.results.listing())
                elif path.startswith('/api/result/'):
                    self.json(lab.results.detail(path.removeprefix('/api/result/')))
                elif path.startswith('/download/'):
                    pieces=path.split('/')
                    if len(pieces)!=4:raise ValueError('Invalid download')
                    identifier,filename=pieces[2:]
                    folder=lab.results.folder(identifier)
                    if filename in FILES:
                        file=folder/filename
                        if file.is_symlink():raise ValueError('Invalid file')
                        self.send_bytes(200,file.read_bytes(),'application/json' if filename.endswith('.json') else 'text/csv',filename)
                    elif filename=='corrected.csv':
                        detail=lab.results.detail(identifier)
                        self.send_bytes(200,lab.results.corrected_csv(detail),'text/csv',identifier+'_corrected.csv')
                    elif filename=='all.zip':
                        detail=lab.results.detail(identifier)
                        self.send_bytes(200,lab.results.archive(detail),'application/zip',identifier+'_results.zip')
                    elif filename=='report.html':
                        self.send_bytes(200,lab.results.report(lab.results.detail(identifier)),'text/html; charset=utf-8',identifier+'_report.html')
                    else:raise FileNotFoundError()
                else:raise FileNotFoundError()
            except (ValueError,FileNotFoundError,OSError) as error:self.json({'error':str(error) or 'Not found'},404)
            except Exception:self.json({'error':'Could not read that recording. Original files remain on the Pi.'},500)
        def do_POST(self):
            if not self.valid_host():
                self.json({'error':'Invalid host'},403);return
            origin=self.headers.get('Origin')
            if origin and origin!='http://'+self.headers.get('Host',''):
                self.json({'error':'Wrong origin'},403);return
            if not secrets.compare_digest(self.headers.get('X-Lab-Token',''),lab.secret):
                self.json({'error':'Refresh this page before sending a command.'},403);return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<2048:raise ValueError('Invalid request')
                data=json.loads(self.rfile.read(length))
                if not isinstance(data,dict):raise ValueError('Invalid request')
                client=data.get('client')
                if not isinstance(client,str) or not 8<=len(client)<=100:raise ValueError('Invalid browser session')
                if self.path=='/api/heartbeat':
                    if client==lab.owner:lab.heartbeat=time.monotonic()
                    self.json({'ok':True});return
                if self.path!='/api/command':raise ValueError('Unknown request')
                if data.get('action') not in ('new','baseline','start','stop','recover','demo_off'):raise ValueError('Unknown command')
                if data['action']=='stop':lab.stop_load.set()
                else:lab.commands.put_nowait(data)
                self.json({'accepted':True})
            except (ValueError,queue.Full) as error:self.json({'error':str(error) or 'Command queue full; wait a moment.'},400)
    return Handler

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    modes=parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--hardware',action='store_true')
    modes.add_argument('--demo',action='store_true')
    modes.add_argument('--results-only',action='store_true')
    parser.add_argument('--host',default='0.0.0.0')
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--output',default='~/dyno/auto_runs')
    args=parser.parse_args()
    lab=server=None
    server_started=False
    def terminate(signum,frame):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,terminate)
    signal.signal(signal.SIGHUP,terminate)
    try:
        # Reserve web port before claiming GPIO, so a duplicate launch cannot
        # momentarily disrupt an already-running controller.
        server=ThreadingHTTPServer((args.host,args.port),BaseHTTPRequestHandler)
        server.daemon_threads=True
        lab=Lab(args)
        server.RequestHandlerClass=handler_for(lab)
        lab.tick()
        threading.Thread(target=server.serve_forever,daemon=True).start()
        server_started=True
        print(f'Motor Lab {VERSION}: http://raspberrypi.local:{args.port} — motor supply OFF. '+('SIMULATION' if args.demo else ''),flush=True)
        print('Results:',lab.results.root,flush=True)
        while True:
            try:lab.tick()
            except Exception as error:
                if lab.hardware:
                    try:lab.hardware.master(False)
                    except Exception:pass
                if lab.run and not lab.sealed:
                    try:lab.run.abort(str(error))
                    except Exception:pass
                lab.notice='Controller error: '+str(error)+'. Turn motor supply OFF.'
                with lab.lock:
                    lab.state.update(phase='aborted',message=lab.notice,notice=lab.notice,ready=False)
            time.sleep(.05)
    except KeyboardInterrupt:print('Stopping; TURN MOTOR SUPPLY OFF.',flush=True)
    finally:
        if lab:lab.close()
        if server:
            if server_started:server.shutdown()
            server.server_close()

if __name__=='__main__':main()
