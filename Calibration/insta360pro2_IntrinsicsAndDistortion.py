from calib_functions import *
import cv2

from pathlib import Path
current_dir = Path(__file__).parent.resolve()
#print(f"Working Directory: {current_dir}")

# Define paths
path1 = current_dir / "Pictures" / "cam1" / "calib"
path2 = current_dir / "Pictures" / "cam2" / "calib"
path3 = current_dir / "Pictures" / "cam3" / "calib"

# Define arrays of paths
calib = [path1, path2, path3]
#print(calib)

# Define pattern parameters
nc,nr = 7,11

with open('cameras.txt', 'r') as f:
    cameraIPs = [line.rstrip() for line in f]

# Define url names
url1 = ['rtmp://'+cameraIPs[0]+'/live/origin1','rtmp://'+cameraIPs[0]+'/live/origin2','rtmp://'+cameraIPs[0]+'/live/origin3','rtmp://'+cameraIPs[0]+'/live/origin4','rtmp://'+cameraIPs[0]+'/live/origin5','rtmp://'+cameraIPs[0]+'/live/origin6']
url2 = ['rtmp://'+cameraIPs[1]+'/live/origin1','rtmp://'+cameraIPs[1]+'/live/origin2','rtmp://'+cameraIPs[1]+'/live/origin3','rtmp://'+cameraIPs[1]+'/live/origin4','rtmp://'+cameraIPs[1]+'/live/origin5','rtmp://'+cameraIPs[1]+'/live/origin6']
url3 = ['rtmp://'+cameraIPs[2]+'/live/origin1','rtmp://'+cameraIPs[2]+'/live/origin2','rtmp://'+cameraIPs[2]+'/live/origin3','rtmp://'+cameraIPs[2]+'/live/origin4','rtmp://'+cameraIPs[2]+'/live/origin5','rtmp://'+cameraIPs[2]+'/live/origin6']

urlAll = url1+url2+url3

print(url1)


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
    print('Press C to capture image or N to go to Next view')

    # Define url array for current camera
    url = urlAll[idx:idx+6]

    # Capture video of each view
    vcap = [cv2.VideoCapture(i) for i in url]

    #print(vcap)

    for j, cap in enumerate(vcap):
        print(f"View {j+1}: {cap.isOpened()}")

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