import requests
import json
import time
from datetime import datetime
import cv2

def jprint(obj):
    # create a formatted string of the Python JSON object
    text = json.dumps(obj, sort_keys=True, indent=4)
    print(text)

def connect(URL):
    command = URL + ":20000/osc/commands/execute"  
    post_dic = {}
    post_dic['json'] = {
                "name": "camera._connect",
                "parameters":
                    {
                        "hw_time":"MMDDhhmm[[CC]YY][.ss]",
                        "time_zone": "GMT+08:00/GMT-08:00"
                    }
            }
    response = requests.post(command,**post_dic)

    if response.status_code == 200:
                        results = response.json()['results']
                        fingerprint = results['Fingerprint']
                        results = response.json()
    return response, fingerprint

def execute(post_dic, URL): 

    command = URL + ":20000/osc/commands/execute"  
    response = requests.post(command,**post_dic)

    if response.status_code == 200:
                        connected = True
                        results = response.json()
    return response

def state(Fingerprint, URL):

    command = URL + ":20000/osc/state"
    post_dic = {}
    post_dic['headers'] = {}
    post_dic['headers']['content-type'] = 'application/json'
    post_dic['headers']['Fingerprint'] = Fingerprint
    response=requests.post(command,**post_dic)
    return response


#===========================================================================================
#                               variable                                                   #
#===========================================================================================

post_dic = {}
post_dic['headers'] = {}
post_dic['headers']['content-type'] = 'application/json'

rtmp_server='rtsp://10.180.137.158/live/live'
state_api = "http://10.180.137.158:20000/osc/state"
liveUrl = "%s/live" % (rtmp_server) 

URL1 = "http://10.180.137.156"
URL2 = "http://10.180.137.158"
URL3 = "http://10.180.137.160"

url_rtmp1 = "rtmp://10.180.137.156/live/live"
url_rtmp2 = "rtmp://10.180.137.158/live/live"
url_rtmp3 = "rtmp://10.180.137.160/live/live"

url_o1 = "rtmp://10.180.137.156/live"
url_o2 = "rtmp://10.180.137.158/live"
url_o3 = "rtmp://10.180.137.160/live"


#Creation of stream array 
nameIP = [URL1 , URL2, URL3]
names = [url_rtmp1, url_rtmp2, url_rtmp3]
nameOrigin = [url_o1,url_o2,url_o3]
window_titles = ['first', 'second','1','2','3','4','5','6','7','8','9','10','11','12','13','14','15','16','17','18','19','20','21','22','23','24','25','26','27','28','29','30','31','32','33','34','35','36','37','38','39','40','41','42','43','44','45','46']
r = [None]*len(names)
f = [None]*len(names)
post_dic = [None]*len(names)
frames = [None] * len(names)  #Frame
ret = [None] * len(names)     #Bool      

#===========================================================================================

for i in range(len(names)):
    r[i],f[i]=connect(nameIP[i])
    print('connection de :', nameIP[i])
    jprint(r[i].json())

    print('===========================================================================================')


    post_dic[i] = {}
    post_dic[i]['headers'] = {}
    post_dic[i]['headers']['content-type'] = 'application/json'
    post_dic[i]['headers']['Fingerprint'] = f[i]
    res = state(f[i], nameIP[i])
    print('connection de :', nameIP[i])
    jprint(res.json())

    print('===========================================================================================')

    post_dic[i]['json'] = {
                "name": "camera._startLive",
                "parameters": {
                    "origin": {
                        "mime": "h265",
                        "width": 1920,
                        "height": 1080, #2880 for max size,2160 for stitching
                        "framerate": 30,
                        "bitrate": 6000,
                        "liveUrl": nameOrigin[i],
                        "saveOrigin": False 
                    },
                    ''' "stiching": {
                         "mode": "pano",
                         "mime": "h264",
                         "width": 1920, # 3840*1920 for normal, 7680*3840 for max
                         "height": 1080,
                         "framerate": 30, 
                         "bitrate": 6000,
                         "_liveUrl": "rtmp://10.180.137.160/live/live"
                     },'''
                    "audio": {
                    "mime":'aac', 
                    "sampleFormat":'s16',
                    "channelLayout":'stereo',
                    "samplerate":48000,
                    "bitrate":64
                    },
                    "autoConnect":{
                    "enable": True, 
                    "interval": 5,
                    "count": 3
                    }
                },
                "stabilization": False,
                "mode": "origin live"
            }
    jprint(execute(post_dic[i], nameIP[i]).json()) 




time.sleep(10)


print('===========================================================================================')

for i in range(len(names)):
    r[i],f[i]=connect(nameIP[i])
    print('connection de :', nameIP[i])
    jprint(r[i].json())



    post_dic[i] = {}
    post_dic[i]['headers'] = {}
    post_dic[i]['headers']['content-type'] = 'application/json'
    post_dic[i]['headers']['Fingerprint'] = f[i]
    res = state(f[i], nameIP[i])
    jprint(res.json())