import argparse
import csv
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

import app
import dyno_core as core

class TestResults(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root=Path(self.tmp.name)
        self.results=app.Results(self.root)
        self.identifier='20261117_183439_340263'
        self.folder=self.root/self.identifier
        self.folder.mkdir()
        self.metadata={'nominal_voltage':6,'result':'completed','torque_Nm_per_count':-.0007256921/170499}
        (self.folder/'metadata.json').write_text(json.dumps(self.metadata))
        with (self.folder/'points.csv').open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=['sweep_leg','commanded_load','mean_rpm','mean_voltage_V','mean_current_A','mean_electrical_input_W','mean_torque_Nm_direction_baseline'])
            w.writeheader();w.writerow(dict(sweep_leg='up',commanded_load=7,mean_rpm=3834,mean_voltage_V=5.839,mean_current_A=1.895,mean_electrical_input_W=11.063,mean_torque_Nm_direction_baseline=.001615))
    def tearDown(self):self.tmp.cleanup()
    def test_old_scale_corrected_once_and_original_preserved(self):
        original=(self.folder/'points.csv').read_bytes()
        d=self.results.detail(self.identifier)
        self.assertEqual(d['correction_factor'],10)
        self.assertAlmostEqual(d['points'][0]['torque_mNm_provisional'],16.15)
        self.assertAlmostEqual(d['points'][0]['efficiency_pct_apparent'],58.61,delta=.05)
        self.assertEqual(original,(self.folder/'points.csv').read_bytes())
        self.assertEqual(self.results.detail(self.identifier)['correction_factor'],10)
        new=dict(self.metadata,torque_Nm_per_count=core.TORQUE_PER_COUNT)
        self.assertEqual(app.torque_correction(new)[0],1)
    def test_archive_contains_originals_corrected_table_and_offline_charts(self):
        d=self.results.detail(self.identifier)
        with zipfile.ZipFile(io.BytesIO(self.results.archive(d))) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(z.read('original_data/points.csv'),(self.folder/'points.csv').read_bytes())
            self.assertIn(b'<svg',z.read('report.html'))
            rows=list(csv.DictReader(io.StringIO(z.read('results_corrected.csv').decode())))
            self.assertAlmostEqual(float(rows[0]['torque_mNm_provisional']),16.15)
    def test_traversal_and_unknown_scale(self):
        with self.assertRaises(ValueError):self.results.folder('../outside')
        self.assertEqual(app.torque_correction({})[0],1)
        self.assertIn('Unknown',app.torque_correction({})[1])
    def test_negative_or_stopped_has_no_efficiency(self):
        for values in [(-.01,4000,6),(.01,0,6),(.01,4000,0)]:
            self.assertEqual(app.apparent(*values),(None,None))
        self.assertGreater(app.apparent(.1,4000,1)[1],100) # flag, don't clamp

class TestControl(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        args=argparse.Namespace(output=self.tmp.name,demo=True,results_only=False)
        self.lab=app.Lab(args)
        self.lab.tick()
        self.client='test-client-owner'
    def tearDown(self):self.lab.close();self.tmp.cleanup()
    def create(self):
        self.lab.execute(dict(action='new',client=self.client,motor_off_confirmed=True,voltage=6,max_load=5))
    def test_no_automatic_sweep_and_explicit_new_test(self):
        self.assertIsNone(self.lab.run)
        with self.assertRaises(ValueError):self.lab.execute(dict(action='new',client=self.client,motor_off_confirmed=False))
        self.create()
        self.assertEqual(self.lab.run.phase,'idle')
        self.assertFalse(self.lab.hardware.on)
        self.assertEqual(max(x[0] for x in core.SEQUENCE),5)
        self.assertEqual(self.lab.run.metadata['calibration_distance_mm'],74)
    def test_restart_creates_new_folder_and_keeps_results(self):
        self.create();first=self.lab.run.folder
        self.lab.run.abort('test')
        self.lab.tick()
        self.assertTrue(self.lab.sealed)
        self.create()
        self.assertNotEqual(first,self.lab.run.folder)
        self.assertTrue((first/'metadata.json').exists())
        self.assertEqual(len(self.lab.results.listing()),2)
    def test_other_browser_cannot_arm_and_stop_remains_available(self):
        self.create()
        with self.assertRaises(ValueError):self.lab.execute(dict(action='start',client='someone-else'))
        self.lab.stop_load.set();self.lab.tick()
        self.assertEqual(self.lab.run.phase,'aborted')
        self.assertFalse(self.lab.hardware.on)
    def test_heartbeat_loss_opens_load(self):
        self.create();run=self.lab.run
        run.phase='starting';run.running_start=time.monotonic();run.deadline=time.monotonic()+20
        self.lab.heartbeat=time.monotonic()-4
        self.lab.hardware.on=True
        self.lab.tick()
        self.assertEqual(run.phase,'aborted')
        self.assertFalse(self.lab.hardware.on)
    def test_full_sweep_twice_with_no_terminal_restart(self):
        clock=[100.]
        with patch.object(core.time,'monotonic',lambda:clock[0]):
            for cycle in range(2):
                self.lab.hardware.motor=False
                self.create();r=self.lab.run;r.zero=350000;r.phase='ready'
                self.lab.execute(dict(action='start',client=self.client,motor_off_confirmed=True))
                for i in range(3000):
                    self.lab.heartbeat=clock[0]
                    self.lab.tick();clock[0]+=.05
                    if self.lab.sealed:break
                self.assertEqual(r.phase,'finished')
                self.assertFalse(self.lab.hardware.on)
            self.assertEqual(len(self.lab.results.listing()),2)
    def test_guided_baseline_then_settling_reaches_ready(self):
        clock=[100.]
        with patch.object(core.time,'monotonic',lambda:clock[0]):
            self.create()
            self.lab.execute(dict(action='baseline',client=self.client,direction='clockwise'))
            for i in range(1200):
                self.lab.tick();clock[0]+=.05
                if self.lab.run.phase=='ready':break
            self.assertEqual(self.lab.run.phase,'ready')
            self.assertGreater(len(self.lab.run.baseline_values),100)
            self.assertFalse(self.lab.hardware.motor)
            self.assertFalse(self.lab.hardware.on)
            self.assertIsNotNone(self.lab.run.metadata['baseline'])

class TestHTTP(unittest.TestCase):
    def test_views_downloads_and_command_validation(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            lab=app.Lab(argparse.Namespace(output=root,demo=True,results_only=False));lab.tick()
            server=app.ThreadingHTTPServer(('127.0.0.1',0),app.handler_for(lab))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base='http://127.0.0.1:'+str(server.server_address[1])
            try:
                self.assertIn(b'Saved results',urllib.request.urlopen(base+'/').read())
                state=json.load(urllib.request.urlopen(base+'/api/state'))
                body=json.dumps(dict(action='new',client='http-test-client',motor_off_confirmed=True,voltage=6,max_load=5)).encode()
                with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(urllib.request.Request(base+'/api/command',data=body))
                request=urllib.request.Request(base+'/api/command',data=body,headers={'Content-Type':'application/json','X-Lab-Token':state['csrf']})
                self.assertEqual(urllib.request.urlopen(request).status,200)
                lab.tick();lab.run.abort('http test');lab.tick()
                runs=json.load(urllib.request.urlopen(base+'/api/results'));identifier=runs[0]['id']
                detail=json.load(urllib.request.urlopen(base+'/api/result/'+identifier));self.assertEqual(detail['correction_factor'],1)
                with zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(base+'/download/'+identifier+'/all.zip').read())) as z:
                    self.assertIn('report.html',z.namelist())
                request=urllib.request.Request(base+'/api/state',headers={'Host':'untrusted.example'})
                with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(request)
            finally:server.shutdown();server.server_close();lab.close()

if __name__=='__main__':unittest.main()
