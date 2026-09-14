import cv2
from timecode import Timecode
from threading import Thread
import numpy as np
import time
import requests
import json
from datetime import datetime
from art import *
from matplotlib import pyplot as plt
import queue

class vStream:
    def __init__(self,src,width,height):
        self.width=width
        self.height=height
        self.buffer = queue.Queue(600)
        self.prev_timestamp=-1
        self.timestamps = 0
        self.capture=cv2.VideoCapture(src)
        self.framerate = self.capture.get(cv2.CAP_PROP_FPS)
        self.thread=Thread(target=self.update,args=())
        self.thread.daemon=True
        self.started = True
        self.begin = False
        self.thread.start()
    
    def update(self):
        while self.started:
            if self.begin == True :
                self.ret,self.frame=self.capture.read()
                self.timestamps = self.capture.get(cv2.CAP_PROP_POS_MSEC)
                if self.timestamps != self.prev_timestamp:
                    self.frame2=cv2.resize(self.frame,(self.width,self.height))
                    self.buffer.put(self.frame2)
                self.prev_timestamp = self.timestamps
                time.sleep(0)

    def video(self) :
        return self.buffer.get()   

    def getFrame(self):
        return self.ret, self.frame2
    
    def getFramerate(self):
        return self.framerate

    def TMSP(self):
        return self.timestamps 

    def  start(self):
        self.begin = True

    def  end(self):
        self.begin = False
    
    def  getbegin(self):
        return self.begin 
  
    def size(self):
        return self.buffer.qsize()

    def stop(self) :
        self.started = False
        self.thread.join()

    def __exit__(self, exc_type, exc_value, traceback) :
        self.stream.release()
    
def time_comp(heure, minute, seconde, heure2, minute2, seconde2):
    #t1 > t2 -> 0
    #t1 < t2 -> 1
    #t1 = t2 -> 2
    dt = 0.033
    if(heure > heure2):
        return 0
    elif(heure < heure2):
        return 1
    elif(heure == heure2):
        if(minute > minute2):
            return 0
        elif(minute < minute2):
            return 1
        elif(minute == minute2):
            if(seconde > seconde2 and abs(seconde - seconde2) > dt ):
                    return 0
            elif(seconde < seconde2 and abs(seconde - seconde2) > dt ):
                return 1
            elif((seconde == seconde2) or abs(seconde - seconde2)<dt):
                return 2

def frames_to_tc(self, frames):
        """Converts frames back to timecode
        :returns str: the string representation of the current time code
        """
        if self.drop_frame:
            # Number of frames to drop on the minute marks is the nearest
            # integer to 6% of the framerate
            ffps = float(self.framerate)
            drop_frames = int(round(ffps * .066666))
        else:
            ffps = float(self._int_framerate)
            drop_frames = 0

        # Number of frames per ten minutes
        frames_per_10_minutes = int(round(ffps * 60 * 10))

        # Number of frames in a day - timecode rolls over after 24 hours
        frames_per_24_hours = int(round(ffps * 60 * 60 * 24))

        # Number of frames per minute is the round of the framerate * 60 minus
        # the number of dropped frames
        frames_per_minute = int(round(ffps) * 60) - drop_frames

        frame_number = frames - 1

        # If frame_number is greater than 24 hrs, next operation will rollover
        # clock
        frame_number %= frames_per_24_hours

        if self.drop_frame:
            d = frame_number // frames_per_10_minutes
            m = frame_number % frames_per_10_minutes
            if m > drop_frames:
                frame_number += (drop_frames * 9 * d) + drop_frames * ((m - drop_frames) // frames_per_minute)
            else:
                frame_number += drop_frames * 9 * d

        ifps = self._int_framerate

        frs = frame_number % ifps
        if self.fraction_frame:
            frs = round(frs / float(ifps), 3)

        secs = int((frame_number // ifps) % 60)
        mins = int(((frame_number // ifps) // 60) % 60)
        hrs = int((((frame_number // ifps) // 60) // 60))

        return hrs, mins, secs, frs

def date():

    year = datetime.now().year
    month = datetime.now().month 
    day = datetime.now().day 
    hour = datetime.now().hour 
    minute = datetime.now().minute 
    second = datetime.now().second 
    year = str(year)
    if month < 10 : 
        month = '0'+str(month)
    else :
        month = str(month)
    if day < 10 : 
        day = '0'+str(day)
    else :
        day = str(day)
    if hour < 10 : 
        hour = '0'+str(hour)
    else :
        hour = str(hour)
    if minute < 10 : 
        minute = '0'+str(minute)
    else :
        minute = str(minute)
    if second-1 < 10 : 
        second = '0'+str(second-1)
    else :
        second = str(second)

    return month+day+hour+minute+year+second

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

def concat_tile(im_list_2d):
    return cv2.vconcat([cv2.hconcat(im_list_h) for im_list_h in im_list_2d])

#===========================================================================================
#                                       variable                                           #
#===========================================================================================

#variable of task request for API Insta360
post_dic = {}
post_dic['headers'] = {}
post_dic['headers']['content-type'] = 'application/json'

#URL of Insta360
#--------------------------------------------------------------
URL1 = "http://10.180.137.156"
URL2 = "http://10.180.137.158"
URL3 = "http://10.180.137.160"

#URL of Stream and origin live (live from fisheyes cameras)
#--------------------------------------------------------------
url_rtsp= 'rtsp://10.180.137.51/live/live'
url_rtmp1 = 'rtmp://10.180.137.156/live/live'
url_rtmp2 = 'rtmp://10.180.137.158/live/live'
url_rtmp3 = 'rtmp://10.180.137.160/live/live'
url_rtmp4='rtmp://10.180.137.50:1935/live/av0'
url_hsl = "http://10.180.137.51:8000/tmp/live/live.m3u8"

url_origin1 = 'rtmp://10.180.137.156/live'
url_origin2 = 'rtmp://10.180.137.158/live'
url_origin3 = 'rtmp://10.180.137.160/live'

url_rtmp1_origin1 = 'rtmp://10.180.137.156/live/origin1'
url_rtmp1_origin2 = 'rtmp://10.180.137.156/live/origin2'
url_rtmp1_origin3 = 'rtmp://10.180.137.156/live/origin3'
url_rtmp1_origin4 = 'rtmp://10.180.137.156/live/origin4'
url_rtmp1_origin5 = 'rtmp://10.180.137.156/live/origin5'
url_rtmp1_origin6 = 'rtmp://10.180.137.156/live/origin6'

url_rtmp2_origin1 = 'rtmp://10.180.137.158/live/origin1'
url_rtmp2_origin2 = 'rtmp://10.180.137.158/live/origin2'
url_rtmp2_origin3 = 'rtmp://10.180.137.158/live/origin3'
url_rtmp2_origin4 = 'rtmp://10.180.137.158/live/origin4'
url_rtmp2_origin5 = 'rtmp://10.180.137.158/live/origin5'
url_rtmp2_origin6 = 'rtmp://10.180.137.158/live/origin6'

url_rtmp3_origin1 = 'rtmp://10.180.137.160/live/origin1'
url_rtmp3_origin2 = 'rtmp://10.180.137.160/live/origin2'
url_rtmp3_origin3 = 'rtmp://10.180.137.160/live/origin3'
url_rtmp3_origin4 = 'rtmp://10.180.137.160/live/origin4'
url_rtmp3_origin5 = 'rtmp://10.180.137.160/live/origin5'
url_rtmp3_origin6 = 'rtmp://10.180.137.160/live/origin6'
#--------------------------------------------------------------

#Creation of stream array 
nameIP = [URL1 , URL2, URL3]
nameOrigin = [url_origin1 , url_origin2, url_origin3]
names = [url_rtmp1, url_rtmp2, url_rtmp3, url_rtmp4]
window_titles = ['first', 'second','1','2','3','4','5','6','7','8','9','10','11','12','13','14','15','16','17','18','19','20','21','22','23','24','25','26','27','28','29','30','31','32','33','34','35','36','37','38','39','40','41','42','43','44','45','46']
r = [None]*len(names)
f = [None]*len(names)
post_dic = [None]*len(names)

#Variable
frames = [None] * len(names)  #Frame
ret = [None] * len(names)     #Bool      
resize = [None] * len(names)  #To resize windows
tc = [None] * len(names)      #timecode
h = [None] * len(names)       #hours
m = [None] * len(names)       #minutes
s = [None] * len(names)       #seconds
ms = [None] * len(names)      #miliseconds
flag = [1] * len(names)       #flag to block the read of a frame 1 -> frame pass 0-> block

resize_hw = (960,480) #Size of resize windows

brightflag=0                        #Flag 
Prev_frame = [None] * len(names)    #Previous Frame
Current_frame = [None] * len(names) #Actual Frame 
motion = [None] * len(names) 
flaglum= [0] * len(names)           #Flag if variation of Light
grayPrev  = [None] * len(names)
grayCurr  = [None] * len(names)
luminance = [None] * len(names)

#Array for graph
plt1 = []                              
plt2 = []
plt3 = []
plt4 = []

#Size of frame
dispW=1920
dispH=1080

#buffer mini size

buffer_size = 0

#===========================================================================================

for i in range(len(nameIP)):
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
    
    
    #Stream Parameters for Insta360 see https://github.com/Insta360Develop/ProCameraApi for more info
    post_dic[i]['json'] = {
                    "name": "camera._startLive",
                    "parameters": {
                        "origin": {
                            "mime": "h265",
                            "width": 1920,  #to Stream in 4k 1920*1440
                            "height": 1440, #2880 for max size,2160 for stitching
                            "framerate": 25,
                            "bitrate": 20480,
                            "liveUrl": nameOrigin[i],
                            "saveOrigin": False 
                        },
                        "stiching": {
                            "mode": "3d_top_left", #"pano" for mono 360 video,"3d_top_left" for stereo 360 with top/bottom layout, left eye on top. "3d_top_right" for stereo 360 with top/bottom layout, right eye on top.
                            "mime": "h264",
                            "width": 3840, # 3840*1920 for normal, 7680*3840 for max #Optimal 1920*1080
                            "height": 1920,
                            "framerate": 25, 
                            "bitrate": 10240,
                            "_liveUrl": names[i]
                        },
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
                    "mode": "normal"
                } 
    jprint(execute(post_dic[i], nameIP[i]).json()) 

tprint('SLEEP')
time.sleep(2)

print('===========================================================================================')

tprint('Live ON')
cap = [vStream]*len(names)
# Create a VideoCapture object and read from input file
# If the input is the camera, pass 0 instead of the video file name
for i in range(len(names)):
    cap[i]=vStream(names[i],dispW,dispH)

#Timecode creation loop for each stream
for i,c in enumerate(cap):
    if c is not None:
        print(names[i])
        cap[i].start()
        ips = c.getFramerate()
        print(names[i],ips)
        tc[i]=Timecode(ips,'0:0:0.0')  #Creation of timecode

tprint('sleep')

time.sleep(2)

#Sync Lumiere
#--------------------------------------------------------------

print('sync cam pls turn the light On/Off')

# Open the Video
# read the first frame  of the video as the initial background image
for i,c in enumerate(cap):
    if c is not None:
        Prev_frame[i]= c.video()
        Prev_frame[i]=cv2.resize(Prev_frame[i], resize_hw)

while True:
    #To fill buffer
    g = 0
    for i,c in enumerate(cap):
        if c.size() > buffer_size:
            g+=1
    if g == len(names):

    ##capture frame by frame
        for i,c in enumerate(cap):
            if c is not None:
                Current_frame[i]=cap[i].video()
                Current_frame[i]=cv2.resize(Current_frame[i], resize_hw) 
    
        # Calculation of the average luminance of each frame to compare it with the previous one
        for i,c in enumerate(cap):
            if c is not None:
                motion[i]=0
                grayPrev[i]= cv2.cvtColor(Prev_frame[i], cv2.COLOR_BGR2GRAY)
                grayCurr[i]= cv2.cvtColor(Current_frame[i], cv2.COLOR_BGR2GRAY)
                tmp = np.average(grayPrev[i])
                tmp2 = np.average(grayCurr[i])

                luminance[i] = tmp2-tmp

                if i == 0 :
                    plt1.append(tmp2)
                if i == 1 :
                    plt2.append(tmp2)
                if i == 2 :
                    plt3.append(tmp2)
                if i == 3: 
                    plt4.append(tmp2)

        # loop to see if the difference is greater than a certain value
        for i,c in enumerate(cap):
            if c is not None: 
                if luminance[i] > 80 : 
                        motion[i] = 1 
                        flaglum[i]=1      
                if motion[i] :
                    brightflag+=1
        for i,c in enumerate(cap):
            if c is not None: 
                Prev_frame[i]=Current_frame[i]

        #Start the timecode if he difference is true
        print(motion)
        for i,c in enumerate(cap):
            if c is not None: 
                if flaglum[i] :
                    tc[i].frames+=1
        
        #To break the loop if all of stream sqz the variation of luminance
        tmps = 0
        for i in range(len(names)):
            if flaglum[i] : 
                tmps +=1

        if tmps == len(names):
            break

    time.sleep(0)

# Read until all timecode started     
while True:
    g = 0
    for i,c in enumerate(cap):
        if c.size() > buffer_size:
            g+=1
    if g == len(names):

        # Capture frame-by-frame if flag == 1 
        for i,c in enumerate(cap):
            if c is not None:
                if flag[i] == 1 :
                    frames[i] = cap[i].video()
                    resize[i]=cv2.resize(frames[i], resize_hw) 
                    tc[i].frames += 1
        
        #Print actual timecode just for test 
        for i,fr in enumerate(frames):
            if fr is not None:
                print('video ',i,':',tc[i])

        #Loop to get the time of timecode
        for i,c in enumerate(cap):
            if c is not None:
                h[i],m[i],s[i],ms[i] = frames_to_tc(tc[i], tc[i].frames)
                s[i] = s[i]+ms[i]
        for i in range(len(names)):
            flag[i]=1

        #Timecode comparison loop and Display the resulting frame
        for i in range(len(names)):
            for j in range(len(names)):
                if not i == j :
                    timetimes = time_comp(h[i],m[i],s[i],h[j],m[j],s[j])
                    if timetimes == 1 :
                        print('<')
                        if flag[j]==1:
                            flag[j] = 0
                    elif timetimes == 0 :
                        print('>')
                        if flag[i]==1:
                            flag[i]=0
                    elif timetimes :
                        print("=")

        
        im_tile = concat_tile([[resize[0], resize[1]],
                       [resize[2], resize[3]]])
        cv2.imshow('frame_finale', im_tile)

    # Press Q on keyboard to  exit       
    if cv2.waitKey(5) & 0xFF == ord('q'):
        break

    time.sleep(0)

# Closes all the frames
cv2.destroyAllWindows()

#Same as Sync Lumiere
#=========================================================================================================
flaglum= [0] * len(names)
for i,c in enumerate(cap):
    if c is not None:
        Prev_frame[i]= c.video()
        Prev_frame[i]=cv2.resize(Prev_frame[i], resize_hw)

while True:
    for i,c in enumerate(cap):
        if c is not None:
            Current_frame[i]=cap[i].video()
            Current_frame[i]=cv2.resize(Current_frame[i], resize_hw) 
    for i,c in enumerate(cap):
        if c is not None:
            motion[i]=0
            grayPrev[i]= cv2.cvtColor(Prev_frame[i], cv2.COLOR_BGR2GRAY)
            grayCurr[i]= cv2.cvtColor(Current_frame[i], cv2.COLOR_BGR2GRAY)
            tmp = np.average(grayPrev[i])
            tmp2 = np.average(grayCurr[i])
            luminance[i] = tmp2-tmp
            if i == 0 :
                plt1.append(tmp2)
            if i == 1 :
                plt2.append(tmp2)
            if i == 2 :
                plt3.append(tmp2)
            if i == 3: 
                plt4.append(tmp2)
    for i,c in enumerate(cap):
        if c is not None: 
            if luminance[i] > 80: 
                    motion[i] = 1 
                    flaglum[i]=1      
            if motion[i] :
                brightflag+=1
    for i,c in enumerate(cap):
        if c is not None: 
            Prev_frame[i]=Current_frame[i]
    print(motion)       
    tmps = 0
    for i in range(len(names)):
        if flaglum[i] : 
            tmps +=1
    if tmps == len(names):
        break
    time.sleep(0)



# When everything done, release the video capture object
for c in cap:
    if c is not None:
        cap[i].stop()

#To close all stream of Insta360
for i in range(len(nameIP)):
    r[i],f[i]=connect(nameIP[i])
    print('connection de :', nameIP[i])
    jprint(r[i].json())


    post_dic[i] = {}
    post_dic[i]['headers'] = {}
    post_dic[i]['headers']['content-type'] = 'application/json'
    post_dic[i]['headers']['Fingerprint'] = f[i]
    res = state(f[i], nameIP[i])
    jprint(res.json())
    post_dic[i]['json'] = {"name": "camera._stopLive"}
    jprint(execute(post_dic[i], nameIP[i]).json()) 


plt.plot(plt1)
plt.plot(plt2)
plt.plot(plt3)
plt.plot(plt4)
plt.ylabel('Luminance')
plt.xlabel('Frames')
plt.show()