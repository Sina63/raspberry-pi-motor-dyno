#!/usr/bin/env python3
"""Hardware and attended-sweep engine imported by app.py.
Uses the verified swapped relay mapping, active-low coils, RPM GPIO13 (2 PPR),
INA226 0x44 and HX711 DT GPIO5 / SCK GPIO6 in a separate reader process.
Torque scale: provisional 10 g at 74 mm, 170499-count span.
K8 OPEN removes generator load; it cannot turn off the motor supply.
"""
import argparse
import csv
import json
import math
import multiprocessing
import queue
import signal
import statistics as stats
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PINS = (24, 25, 12, 16, 17, 27, 22, 23)
ROUTES = {1:(0,0,0,0,0,0,0), 2:(0,0,0,1,1,0,0),
          3:(0,0,0,1,0,1,0), 4:(1,1,0,0,1,0,0),
          5:(1,0,1,0,0,0,0), 6:(1,0,1,1,1,0,0),
          7:(1,0,1,1,0,1,1)}
OHMS = {0:None, 1:16.8, 2:11.2, 3:8.4, 4:5.6, 5:3.7333, 6:2.8, 7:1.8667}
TORQUE_PER_COUNT = -0.007256921 / 170499
SEQUENCE = [(0,5,'reference')] + [(i, 2 if i==7 else 3 if i==6 else 5,'up') for i in range(1,8)] + [(i,3 if i==6 else 5,'down') for i in range(6,0,-1)] + [(0,5,'final')]

class Abort(Exception):
    pass

def hx_read_sample(lg, handle, stopping):
    """Original standalone reader protocol; discard a stretched clock pulse."""
    deadline=time.monotonic()+2
    while lg.gpio_read(handle,5):
        if stopping.is_set():
            return None
        if time.monotonic()>deadline:
            raise Abort('HX711 not ready')
        stopping.wait(.001)
    value=0
    for pulse in range(25):
        begin=time.monotonic_ns()
        lg.gpio_write(handle,6,1)
        bit=lg.gpio_read(handle,5) if pulse<24 else 0
        lg.gpio_write(handle,6,0)
        if time.monotonic_ns()-begin>60000:
            lg.gpio_write(handle,6,1)
            time.sleep(.001)
            lg.gpio_write(handle,6,0)
            return None
        if pulse<24:
            value=(value<<1)|bit
    return value-0x1000000 if value & 0x800000 else value

def hx_process(messages, stopping):
    """Separate interpreter: dashboard/JSON threads cannot stretch HX clocks."""
    handle=None
    lg=None
    clock_claimed=False
    messages.cancel_join_thread()
    try:
        import lgpio as lg
        handle=lg.gpiochip_open(0)
        lg.gpio_claim_output(handle,6,0)
        clock_claimed=True
        lg.gpio_claim_input(handle,5)
        lg.gpio_write(handle,6,1)
        time.sleep(.001)
        lg.gpio_write(handle,6,0)
        last_valid=time.monotonic()
        while not stopping.is_set():
            value=hx_read_sample(lg,handle,stopping)
            if value is not None:
                if abs(value)>=0x7ffff0:
                    raise Abort('HX711 out of range')
                now=time.monotonic()
                messages.put(('reading',now,value),timeout=.1)
                last_valid=now
                # Match the working standalone reader's quiet interval.
                stopping.wait(.2)
            elif time.monotonic()-last_valid>2:
                raise Abort('HX711 repeated timing retries; no valid data')
    except Exception as error:
        try:
            messages.put(('error',time.monotonic(),str(error)),timeout=.1)
        except queue.Full:
            pass
    finally:
        if handle is not None and lg is not None:
            try:
                if clock_claimed:
                    lg.gpio_write(handle,6,0)
            finally:
                lg.gpiochip_close(handle)

def percentile(values, fraction):
    x = sorted(values)
    position = (len(x)-1)*fraction
    lo = int(position)
    return x[lo] + (x[min(lo+1,len(x)-1)]-x[lo])*(position-lo)

def estimate_baseline(values):
    """Hysteresis extrema; balanced peak/valley mean, not time average."""
    if len(values) < 100:
        raise Abort('Too few baseline samples. Repeat the slow-turn capture.')
    smooth = [stats.median(values[max(0,i-1):min(len(values),i+2)]) for i in range(len(values))]
    span = percentile(smooth,.95)-percentile(smooth,.05)
    if span < 3000:
        raise Abort('Too little repeating force change. Check motion and sensor.')
    threshold = .25*span
    hi = lo = smooth[0]
    trend = 0
    peaks, valleys = [], []
    for value in smooth[1:]:
        if trend == 0:
            hi, lo = max(hi,value), min(lo,value)
            if value-lo > threshold:
                trend, hi = 1, value
            elif hi-value > threshold:
                trend, lo = -1, value
        elif trend == 1:
            hi = max(hi,value)
            if hi-value > threshold:
                peaks.append(hi)
                trend, lo = -1,value
        else:
            lo = min(lo,value)
            if value-lo > threshold:
                valleys.append(lo)
                trend, hi = 1,value
    if min(len(peaks),len(valleys)) < 3:
        raise Abort('Fewer than three full push/pull cycles. Repeat slowly through more positions.')
    # Discard secondary ripples with extreme levels far inside the full envelope.
    pmed, vmed = stats.median(peaks), stats.median(valleys)
    peaks = [x for x in peaks if abs(x-pmed) < .35*span]
    valleys = [x for x in valleys if abs(x-vmed) < .35*span]
    if min(len(peaks),len(valleys)) < 3:
        raise Abort('Baseline cycles were too irregular. Repeat the capture.')
    p, v = stats.mean(peaks), stats.mean(valleys)
    if max(stats.pstdev(peaks),stats.pstdev(valleys)) > .25*(p-v):
        raise Abort('Baseline extrema varied too much. Repeat without touching the arm.')
    return {'zero_counts':(p+v)/2, 'peak_mean':p, 'valley_mean':v,
            'peak_count':len(peaks), 'valley_count':len(valleys),
            'peak_sd':stats.pstdev(peaks), 'valley_sd':stats.pstdev(valleys),
            'method':'same-direction balanced mean of detected peaks and valleys',
            'absolute_zero_verified':False}

def fault_reason(sample, nominal, reference, running_age, loaded_age, current_limit):
    if sample.get('error'):
        return sample['error']
    if sample['sensor_age'] > .9 or sample['raw_age'] > .9:
        return 'Sensor data stopped updating'
    if abs(sample['current']) >= current_limit:
        return 'Measured current limit reached'
    if sample['voltage'] > min(12.6, nominal*1.12):
        return 'Supply voltage above selected test voltage'
    if sample['rpm'] > 12000:
        return 'RPM above software limit'
    if running_age > 2:
        if sample['voltage'] < nominal*.75:
            return 'Voltage fell: supply OFF, current limiting, or wiring fault'
        if sample['rpm'] < 300:
            return 'Very low RPM / possible stall'
        if reference and loaded_age > .4 and sample['rpm'] < .4*reference:
            return 'RPM below 40% of unloaded reference'
    return None

class Hardware:
    def __init__(self):
        self.h = self.lg = self.factory = self.rpm_input = self.bus = None
        self.outputs = []
        self.hx_child = self.hx_stop = self.hx_messages = None
        self.stop = threading.Event()
        self.workers = []
        self.lock = threading.Lock()
        self.pulses = deque()
        self.raw_samples = deque(maxlen=2000)
        self.data = {'voltage':0., 'current':0., 'raw':0, 'raw_time':0., 'sensor_time':0., 'error':None}
        try:
            import lgpio
            from smbus import SMBus
            from gpiozero import Button
            from gpiozero.pins.lgpio import LGPIOFactory
            self.lg = lgpio
            self.h = lgpio.gpiochip_open(0)
            # Claim physical master first, initially OFF (active-low).
            for pin in (PINS[7],)+PINS[:7]:
                lgpio.gpio_claim_output(self.h,pin,1)
                self.outputs.append(pin)
            self.factory = LGPIOFactory()
            self.rpm_input = Button(13,pull_up=True,bounce_time=None,pin_factory=self.factory)
            self.rpm_input.when_pressed = self.pulse
            self.bus = SMBus(1)
            self.bus.write_i2c_block_data(0x44,0,[0x47,0xB7])
            context=multiprocessing.get_context('spawn')
            self.hx_stop=context.Event()
            self.hx_messages=context.Queue(maxsize=64)
            self.hx_child=context.Process(target=hx_process,args=(self.hx_messages,self.hx_stop),daemon=True)
            self.hx_child.start()
            for target in (self.hx_worker,self.ina_worker):
                worker=threading.Thread(target=target,daemon=True)
                self.workers.append(worker)
                worker.start()
        except BaseException:
            self.close()
            raise

    def pulse(self):
        with self.lock:
            self.pulses.append(time.monotonic())

    def master(self,on):
        self.lg.gpio_write(self.h,PINS[7],0 if on else 1)

    def route(self,index):
        for pin,on in zip(PINS[:7],ROUTES[index]):
            self.lg.gpio_write(self.h,pin,0 if on else 1)

    def hx_worker(self):
        try:
            while not self.stop.is_set():
                try:
                    kind,stamp,value=self.hx_messages.get(timeout=.15)
                except queue.Empty:
                    if not self.hx_child.is_alive():
                        raise Abort('HX711 reader process stopped')
                    continue
                if kind=='error':
                    raise Abort(value)
                with self.lock:
                    self.data.update(raw=value,raw_time=stamp)
                    self.raw_samples.append((stamp,value))
        except Exception as error:
            with self.lock:
                self.data['error']='Load-cell fault: '+str(error)

    def read_register(self,register):
        d=self.bus.read_i2c_block_data(0x44,register,2)
        return (d[0]<<8)|d[1]

    def ina_worker(self):
        try:
            self.stop.wait(1.2)
            while not self.stop.is_set():
                raw=self.read_register(1)
                if raw>=32768:
                    raw-=65536
                if abs(raw)>=32700:
                    raise Abort('INA226 current measurement out of range')
                voltage=self.read_register(2)*.00125*.95115
                with self.lock:
                    self.data.update(voltage=voltage,current=raw*.0000025/.02663,sensor_time=time.monotonic())
                self.stop.wait(.15)
        except Exception as error:
            with self.lock:
                self.data['error']='Voltage/current sensor fault: '+str(error)

    def sample(self):
        now=time.monotonic()
        with self.lock:
            d=dict(self.data)
            while self.pulses and self.pulses[0]<now-1:
                self.pulses.popleft()
            d['rpm']=len(self.pulses)*30. # 2 pulses / revolution, one-second window
            recent=[value for stamp,value in self.raw_samples if stamp>now-.3]
        d['raw']=stats.mean(recent) if recent else d['raw']
        d['raw_age']=now-d['raw_time']
        d['sensor_age']=now-d['sensor_time']
        return d

    def raw_since(self,stamp):
        with self.lock:
            return [(t,v) for t,v in self.raw_samples if t>stamp]

    def close(self):
        if self.h is not None and self.lg:
            try:
                if PINS[7] in self.outputs:
                    self.master(False)
            except Exception:
                pass
        self.stop.set()
        if self.hx_stop is not None:
            self.hx_stop.set()
        if self.hx_child is not None and self.hx_child.pid is not None:
            self.hx_child.join(3)
            if self.hx_child.is_alive():
                self.hx_child.terminate()
                self.hx_child.join(1)
        for worker in self.workers:
            worker.join(2.5)
        if any(worker.is_alive() for worker in self.workers):
            # Keep handles valid for any blocked driver thread until process exit.
            return
        if self.hx_messages is not None:
            self.hx_messages.close()
        if self.rpm_input:
            self.rpm_input.close()
        if self.factory:
            self.factory.close()
        if self.bus:
            self.bus.close()
        if self.h is not None and self.lg:
            for pin in self.outputs:
                try:
                    self.lg.gpio_write(self.h,pin,1)
                except Exception:
                    pass
            self.lg.gpiochip_close(self.h)
            self.h=None

class Demo:
    def __init__(self):
        self.start=time.monotonic()
        self.index=0
        self.on=False
        self.motor=False
        self.cursor=0
        self.nominal=6.
    def master(self,on): self.on=on
    def route(self,index): self.index=index
    def sample(self):
        t=time.monotonic()-self.start
        load=self.index if self.on else 0
        rpm=(5100-260*load) if self.motor else 0
        raw=350000+100000*math.sin(t*1.5) if not self.motor else 350000-60000*load
        return dict(rpm=rpm,voltage=self.nominal if self.motor else 0.,current=.5+.15*load if self.motor else 0.,raw=raw,raw_age=0.,sensor_age=0.,error=None)
    def raw_since(self,stamp):
        now=time.monotonic()
        return [(now,self.sample()['raw'])] if now-stamp>=.1 else []
    def close(self): pass

class Run:
    def __init__(self,hardware,args):
        self.hw,self.args=hardware,args
        self.commands=queue.Queue(maxsize=8)
        self.guard=threading.Lock()
        self.history=deque(maxlen=1800)
        self.phase='idle'
        self.message='Motor supply OFF. Choose its intended direction, then start slow-turn capture.'
        self.load=0
        self.deadline=0.
        self.zero=None
        self.reference=None
        self.baseline_values=[]
        self.baseline_cursor=0.
        self.direction=''
        self.running_start=None
        self.connected_start=0.
        self.segment_start=0.
        self.step=-1
        self.segment=[]
        self.reference_values=[]
        self.result='not_started'
        self.closing=False
        self.stop_requested=threading.Event()
        self.reset_guard=True
        self.folder=Path(args.output).expanduser()/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        self.folder.mkdir(parents=True,exist_ok=False)
        self.sample_file=(self.folder/'samples.csv').open('x',newline='')
        self.writer=csv.writer(self.sample_file)
        self.writer.writerow(['timestamp','elapsed_s','phase','commanded_load','nominal_load_ohm','rpm','voltage_V','current_A','electrical_input_W','torque_Nm_direction_baseline','raw_counts','sensor_age_s','hx_age_s'])
        self.baseline_file=(self.folder/'baseline.csv').open('x',newline='')
        self.bwriter=csv.writer(self.baseline_file)
        self.bwriter.writerow(['elapsed_s','raw_counts'])
        self.summary_file=(self.folder/'points.csv').open('x',newline='')
        self.swriter=csv.writer(self.summary_file)
        self.swriter.writerow(['sweep_leg','commanded_load','nominal_load_ohm','hold_s','averaging_window_s','samples','mean_rpm','sd_rpm','mean_voltage_V','mean_current_A','mean_electrical_input_W','mean_torque_Nm_direction_baseline','sd_torque_Nm','quality'])
        self.metadata={'started':datetime.now().isoformat(),'demo':args.demo,'nominal_voltage':args.voltage,'measured_current_abort_A':args.current_limit,'gpio_mapping_K1_to_K8':PINS,'rpm_ppr':2,'voltage_factor':.95115,'current_divisor':.02663,'torque_Nm_per_count':TORQUE_PER_COUNT,'torque_calibration':'provisional: 10 g at 74 mm, span 170499 counts','calibration_mass_g':10.,'calibration_distance_mm':74.,'calibration_span_counts':170499,'application_version':'2.0','motor_power_control':False,'temperature_measurement':False,'measurement_windows':'asynchronous: RPM 1 s; HX711 mean of last 0.3 s; INA226 averaging approx 0.532 s; 10 Hz log rows are not independent sensor conversions','sequence':SEQUENCE,'baseline':None}
        self.start=time.monotonic()
        self.state={}
        self.last_log=0.
        self.write_metadata()

    def write_metadata(self):
        self.metadata['result']=self.result
        temporary=self.folder/'metadata.tmp'
        temporary.write_text(json.dumps(self.metadata,indent=2))
        temporary.replace(self.folder/'metadata.json')

    def begin_baseline(self,direction,s):
        if self.phase not in ('idle','baseline_failed'):
            return
        if direction not in ('clockwise','counterclockwise'):
            raise Abort('Choose motor direction, viewed from the shaft end.')
        if s['voltage']>.2 or abs(s['current'])>.03 or s['rpm']>0:
            raise Abort('Turn motor supply OFF and let shaft stop before baseline capture.')
        self.direction=direction
        self.metadata['direction_viewed_from_shaft_end']=direction
        self.baseline_values=[]
        self.baseline_cursor=time.monotonic()
        self.hw.master(False)
        self.phase='baseline'
        self.deadline=time.monotonic()+45
        self.message='Turn shaft slowly in the SELECTED direction for 45 seconds. Do not touch the arm. Aim for 3–5 revolutions.'

    def abort(self,reason):
        # Request physical master OFF before any logging/UI work.
        try:
            self.hw.master(False)
        finally:
            self.load=0
            self.phase='aborted'
            self.message='TEST ABORTED: '+reason+'. TURN MOTOR SUPPLY OFF NOW. K8 commanded open; motor power is NOT controlled.'
            self.result='aborted: '+reason
            print('\a\n'+self.message,flush=True)
            self.write_metadata()

    def advance(self):
        self.hw.master(False)
        self.load=0
        self.step+=1
        if self.step>=len(SEQUENCE):
            self.phase='finished'
            self.result='completed'
            self.message='Sweep complete. TURN MOTOR SUPPLY OFF NOW. Data saved. Torque remains provisional.'
            print('\a\n'+self.message,flush=True)
            self.write_metadata()
            return
        target,duration,leg=SEQUENCE[self.step]
        self.pending=target
        self.phase='release'
        self.deadline=time.monotonic()+.2
        self.message=f'Changing load to {target}; K8 open.'

    def finish_segment(self):
        index,hold,leg=SEQUENCE[self.step]
        # Exclude first 0.8 s: relay transient, INA averaging and RPM window.
        rows=[row for stamp,row in self.segment if stamp-self.segment_start>=.8]
        if not rows:
            raise Abort('No valid samples for test point')
        columns=list(zip(*rows)) # rpm, voltage, current, power, torque
        means=[stats.mean(x) for x in columns]
        quality='short_transient_point' if hold<5 else 'unverified_steady_state'
        self.swriter.writerow([leg,index,OHMS[index],hold,round(hold-.8,2),len(rows),means[0],stats.pstdev(columns[0]),*means[1:],stats.pstdev(columns[4]),quality])
        self.summary_file.flush()
        self.advance()

    def tick(self):
        now=time.monotonic()
        tick_elapsed = max(0., now - getattr(self, 'previous_tick', now))
        self.previous_tick = now
        s=self.hw.sample()
        ready=s['sensor_age']<.9 and s['raw_age']<.9 and not s['error']
        if self.stop_requested.is_set() and self.phase not in ('aborted','finished'):
            self.abort('STOP button pressed')
        if ready and hasattr(self, 'offline_gap_start'):
            self.message = self.sensor_resume_message
            del self.offline_gap_start
        active=self.phase in ('starting','release','route_wait','hold')
        if active:
            # Give the operator up to 20 s to turn power on. Once voltage
            # appears, the ordinary running/stall guard becomes active.
            running_age=now-self.running_start
            if self.phase=='starting':
                running_age=now-self.voltage_seen if hasattr(self,'voltage_seen') else 0
            reason=fault_reason(s,self.args.voltage,self.reference,running_age,
                                now-self.connected_start,self.args.current_limit)
            if reason:
                self.abort(reason)
        elif self.phase in ('baseline','settling','ready'):
            if not ready:
                motor_off = (s['sensor_age'] < .9 and
                             abs(s['current']) < .03 and s['voltage'] < .2)
                if motor_off and not s['error']:
                    if not hasattr(self, 'offline_gap_start'):
                        self.offline_gap_start = now
                        self.sensor_resume_message = self.message
                    if now - self.offline_gap_start > 3:
                        self.abort('Load-cell delay lasted over 3 seconds')
                    else:
                        self.deadline += tick_elapsed
                        self.message = 'PAUSED: waiting for load-cell readings. Keep motor OFF; stop turning until readings return.'
                else:
                    self.abort(s['error'] or 'Sensor fault or stale data')
            elif abs(s['current'])>=self.args.current_limit or s['voltage']>self.args.voltage*1.12:
                self.abort('Unexpected supply/current reading before test')
            elif self.phase in ('baseline','settling') and s['voltage']>.3:
                self.abort('Motor supply must stay OFF during baseline/settling')
        try:
            command=self.commands.get_nowait()
        except queue.Empty:
            command=None
        if command and self.phase not in ('aborted','finished'):
            if command.get('action')=='baseline':
                if ready:
                    self.begin_baseline(command.get('direction',''),s)
            elif command.get('action')=='start' and self.phase=='ready':
                if not ready:
                    raise Abort('Sensors not ready')
                if self.args.demo:
                    self.hw.motor=True
                self.phase='starting'
                self.running_start=now
                self.deadline=now+20
                self.message='Turn motor supply ON now. Waiting for speed; generator load remains open.'
        if self.phase=='baseline' and ready:
            for stamp,value in self.hw.raw_since(self.baseline_cursor):
                self.baseline_cursor=max(stamp,self.baseline_cursor)
                self.baseline_values.append(value)
                self.bwriter.writerow([stamp-self.start,value])
            self.baseline_file.flush()
            if now>=self.deadline:
                try:
                    result=estimate_baseline(self.baseline_values)
                except Abort as error:
                    self.phase='baseline_failed'
                    self.message=str(error)+' Motor OFF; press capture to retry.'
                else:
                    self.zero=result['zero_counts']
                    self.metadata['baseline']=result
                    self.write_metadata()
                    self.phase='settling'
                    self.deadline=now+10
                    self.message='STOP turning and release the shaft. Motor stays OFF. Settling for 10 seconds; baseline stays fixed.'
        elif self.phase=='settling' and ready and now>=self.deadline:
            self.phase='ready'
            self.message='Ready. Set supply to selected voltage with 2 A current limit, output OFF. Clear hands, click ARM SWEEP, then turn motor supply ON.'
        elif self.phase=='starting':
            if now>=self.deadline:
                raise Abort('Motor did not reach running speed within 20 seconds')
            if s['voltage']>self.args.voltage*.75:
                if not hasattr(self,'voltage_seen'):
                    self.voltage_seen=now
                if now-self.voltage_seen>2 and s['rpm']<300:
                    raise Abort('Motor powered but RPM missing / possible stall')
                if s['rpm']>300:
                    self.advance()
        elif self.phase=='release' and now>=self.deadline:
            if self.pending:
                self.hw.route(self.pending)
            self.phase='route_wait'
            self.deadline=now+.2
        elif self.phase=='route_wait' and now>=self.deadline:
            if self.pending:
                self.hw.master(True)
            self.load=self.pending
            self.connected_start=now
            self.segment_start=now
            self.segment=[]
            self.phase='hold'
            self.deadline=now+SEQUENCE[self.step][1]
            self.message=f'Automatic sweep: load {self.load}, {SEQUENCE[self.step][2]}. Keep clear; stay at the supply.'
        elif self.phase=='hold':
            if self.step==0 and now-self.segment_start>2:
                self.reference_values.append(s['rpm'])
                self.reference=stats.median(self.reference_values)
            if now>=self.deadline:
                if self.step==0 and (not self.reference or self.reference<300):
                    raise Abort('No valid unloaded RPM reference')
                self.finish_segment()
        torque=None if self.zero is None or s['raw_age']>.9 else (s['raw']-self.zero)*TORQUE_PER_COUNT
        power=s['voltage']*s['current']
        if self.phase=='hold' and now-self.last_log>=.1:
            self.segment.append((now,[s['rpm'],s['voltage'],s['current'],power,torque]))
        if now-self.last_log>=.1:
            self.last_log=now
            self.writer.writerow([datetime.now().isoformat(timespec='milliseconds'),round(now-self.start,4),self.phase,self.load,OHMS[self.load],s['rpm'],s['voltage'],s['current'],power,torque,s['raw'],s['sensor_age'],s['raw_age']])
            self.sample_file.flush()
            point={'t':round(now-self.start,2),'rpm':s['rpm'],'voltage':s['voltage'],'current':s['current'],'power':power,'torque':None if torque is None else torque*1000,'raw':s['raw'] if s['raw_age']<=.9 else None,'load':self.load}
            with self.guard:
                self.history.append(point)
        ui_message=self.message
        if not ready and self.phase in ('idle','baseline_failed'):
            ui_message='Capture blocked: '+(s['error'] or 'Waiting for fresh voltage/current and load-cell readings.')+' Keep motor supply OFF.'
        with self.guard:
            self.state={'phase':self.phase,'message':ui_message,'load':self.load,'remaining':max(0,round(self.deadline-now,1)) if self.phase in ('baseline','settling','hold','starting') else 0,'ready':ready,'direction':self.direction,'zero':self.zero,'demo':self.args.demo,'nominal':self.args.voltage,'limit':self.args.current_limit,'folder':str(self.folder),'reference_rpm':self.reference,'sensor_error':s['error'],'history':list(self.history)}

    def close(self):
        try:
            self.hw.master(False)
        finally:
            for f in (self.sample_file,self.baseline_file,self.summary_file):
                f.close()
            if self.result=='not_started':
                self.result='stopped before completion'
            self.write_metadata()

