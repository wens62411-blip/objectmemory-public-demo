"""Read-only hardware enumeration plus a real, short webcam frame probe."""
import argparse
import json
from datetime import datetime,timezone
from pathlib import Path
import cv2
from serial.tools import list_ports

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--camera-index',type=int,default=0)
    args=parser.parse_args()
    ports=[{'port':p.device,'description':p.description,'usb':p.vid is not None and p.pid is not None} for p in list_ports.comports()]
    source=cv2.VideoCapture(args.camera_index,cv2.CAP_DSHOW)
    opened=source.isOpened()
    ok,frame=source.read()
    result={'timestamp':datetime.now(timezone.utc).isoformat(),'webcam':{'index':args.camera_index,'opened':opened,'frame_read':ok,'width':frame.shape[1] if ok else None,'height':frame.shape[0] if ok else None},'serial_ports':ports,'esp32_detected':None if any(p['usb'] for p in ports) else False,'physical_flash_attempted':False,'note':'USB串口本身不能证明设备型号，发现USB但未确认型号时返回null；此探测不上传固件，不保存家庭画面。'}
    source.release()
    path=ROOT/'data'/'verification'/'hardware-probe.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
