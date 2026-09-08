"""Opt-in local actual model -> TEST HTTP/SQLite -> WebGL draft test.

Replaying one prior household frame is a TEST fixture, NOT a new live-camera
observation, surveyed geometry, or identity/movement/people accuracy evidence.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import cv2
import numpy as np
import pytest

from apps.api.tests.test_photo_registration_model_integration import running_backend

ROOT=Path(__file__).resolve().parents[3]
SOURCE=ROOT/'data/verification/core-vision/probe-before/actual-source.jpg'


@pytest.mark.skipif(os.environ.get('OM_SCENE_BROWSER')!='1' or not SOURCE.is_file(),reason='Opt-in previously captured local frame WebGL test')
def test_actual_furniture_model_generates_unvalidated_3d_draft_in_real_browser(tmp_path):
    image=cv2.imdecode(np.frombuffer(SOURCE.read_bytes(),np.uint8),1)
    assert image is not None
    file=tmp_path/'explicit-static-frame-test-replay.avi'
    height,width=image.shape[:2]
    writer=cv2.VideoWriter(str(file),cv2.VideoWriter_fourcc(*'MJPG'),10,(width,height))
    assert writer.isOpened()
    try:
        for _ in range(300):writer.write(image)
    finally:writer.release()
    lifecycle=[]
    output=Path(os.environ.get('OM_CORE_VERIFICATION_DIR') or ROOT/'data/verification/core-vision')/'scene-webgl'
    output.mkdir(parents=True,exist_ok=True)
    receipt={'physical_camera':False,'source_image_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
             'input':'TEST repeated static local frame; actual NanoDet, no mock boxes', 'lifecycle':lifecycle}
    try:
        with running_backend(tmp_path,99,lifecycle) as (client,database):
            assert client.patch('/api/settings',json={'detection_mode':'aruco','hand_detection_enabled':False}).status_code==200
            upload=client.post('/api/cameras/upload-video',files={'file':('static-test-replay.avi',file.read_bytes(),'video/x-msvideo')})
            assert upload.status_code==200,upload.text
            camera=client.post('/api/cameras',json={'name':'TEST static replay, not live home','source_type':'video','source':upload.json()['source'],'enabled':True,'config':{'loop':False}})
            assert camera.status_code==200,camera.text
            camera=camera.json()['id']
            assert client.post(f'/api/cameras/{camera}/start').status_code==200
            endpoint=f'/api/cameras/{camera}/scene'
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                preview=client.get(f'/api/cameras/{camera}/vision-snapshot')
                if preview.status_code==200:break
                time.sleep(.05)
            response=client.post(endpoint+'/propose')
            assert response.status_code==200,response.text
            scene=response.json()['scene']
            receipt['scene']=scene
            assert scene['world_geometry']['objects'],'Actual source did not yield furniture; do not substitute fabricated geometry'
            assert scene['world_geometry']['status']=='draft'
            node=subprocess.run(['node',str(ROOT/'scripts/verify-scene-browser.mjs'),str(client.base_url).rstrip('/'),camera,str(output)],
                cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=45,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            receipt['browser_exit_code']=node.returncode
            assert node.returncode==0,node.stdout+node.stderr
            assert client.get('/api/events').json()==[]
            assert client.post(f'/api/cameras/{camera}/stop').status_code==200
            assert client.post('/api/storage/clear-test').status_code==200
    finally:
        (output/'pipeline.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
