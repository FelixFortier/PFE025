from calib_functions import *
import cv2 

'''
------------------------
Cam #1 is 10.180.137.156
Cam #2 is 10.180.137.158
Cam #3 is 10.180.137.160
------------------------
'''

# ATTENTION : BIEN ADAPTER LES CHEMINS POUR VOTRE UTILISATION PERSONNELLE !!!

# Define paths
path1 = "C:/Users/AT10820/Desktop/Insta360Pro2Calibration/Pictures/cam1/calib/"
path2 = "C:/Users/AT10820/Desktop/Insta360Pro2Calibration/Pictures/cam2/calib/"
path3 = "C:/Users/AT10820/Desktop/Insta360Pro2Calibration/Pictures/cam3/calib/"

# Define arrays of paths
calib = [path1, path2, path3]

# Define pattern parameters
nc,nr = 7,11

# Define url names
url156 = ['rtmp://10.180.137.156/live/origin1','rtmp://10.180.137.156/live/origin2','rtmp://10.180.137.156/live/origin3','rtmp://10.180.137.156/live/origin4','rtmp://10.180.137.156/live/origin5','rtmp://10.180.137.156/live/origin6']
url158 = ['rtmp://10.180.137.158/live/origin1','rtmp://10.180.137.158/live/origin2','rtmp://10.180.137.158/live/origin3','rtmp://10.180.137.158/live/origin4','rtmp://10.180.137.158/live/origin5','rtmp://10.180.137.158/live/origin6']
url160 = ['rtmp://10.180.137.160/live/origin1','rtmp://10.180.137.160/live/origin2','rtmp://10.180.137.160/live/origin3','rtmp://10.180.137.160/live/origin4','rtmp://10.180.137.160/live/origin5','rtmp://10.180.137.160/live/origin6']

urlAll = url156+url158+url160

'''
--------------------------
CAPTURE CALIBRATION IMAGES
--------------------------
'''

# Initialize indexes
idx = 0
cam = 0

# While all streams have not been seen
while idx<len(urlAll):
    # Print camera name
    print('Cam#{}'.format(cam+1))

    # Define url array for current camera
    url = urlAll[idx:idx+6]

    # Capture video of each view
    vcap = [cv2.VideoCapture(i) for i in url]
    iframe=1

    # For each view : display it, Capture images (C) or go to Next view (N)
    for i in range(len(url)):
        # Define window name
        window = 'Cam{} View{}'.format(cam+1,i+1)

        # Cam main loop
        while True:
            # Capture frame-by-frame
            ret, frame = vcap[i].read()

            # Rotate & resize image
            frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
            resize = cv2.resize(frame, (1080, 1920)) 

            if frame is not None:
                # Display the resulting frame
                image=cv2.imshow(window, resize)    

                # Press C to capture images
                if cv2.waitKey(1) & 0xFF == ord('c'):
                    print('Photo taken')
                    filename = "{}{}.jpg".format(calib[cam],iframe)
                    cv2.imwrite(filename, frame)
                    print('Photo saved')
                    iframe+=1

                # Press N to go to next view
                if cv2.waitKey(1) & 0xFF == ord('n'):
                    break

        # When everything done, release the capture
        if i != 5:
            print('Next lens')
        else:
            print('Next cam')
        vcap[i].release()
        cv2.destroyAllWindows()

    # Update index values
    cam+=1
    idx+=6

'''
------------------------------------------------
Now, we have our images : calibrate the camera !
------------------------------------------------
'''

print('\n---------------------------------------------------------------------------------------------------------\n')

# Calibrate cameras
for cam in range(len(calib)):
    print('Calibrating Cam{}'.format(cam+1))

    # Calibration
    files = "{}*.jpg".format(calib[cam])
    images = glob.glob(files)
    mtx,dist,newcameramtx,w,h,roi,imgpoints,objpoints,rvecs,tvecs = CameraCalibration(nc,nr,files)

    # Save intrinsic matrix & distortion vector
    if cam+1==1:
        Intrinsic1 = newcameramtx
        Dist1 = dist
    elif cam+1==2:
        Intrinsic2 = newcameramtx
        Dist2 = dist
    elif cam+1==3:
        Intrinsic3 = newcameramtx
        Dist3 = dist
    else:
        print('Error: camera index out of range')

print('\n---------------------------------------------------------------------------------------------------------\n')

# Show matrices
print('Intrinsic matrices -------------------------------')
print('Camera 1 Intrinsic Matrix :\n{}\n'.format(Intrinsic1))
print('Camera 2 Intrinsic Matrix :\n{}\n'.format(Intrinsic2))
print('Camera 3 Intrinsic Matrix :\n{}\n'.format(Intrinsic3))
print('\n')
print('Distortion vectors -------------------------------')
print('Camera 1 Distortion :\n{}\n'.format(Dist1))
print('Camera 2 Distortion :\n{}\n'.format(Dist2))   
print('Camera 3 Distortion :\n{}\n'.format(Dist3))