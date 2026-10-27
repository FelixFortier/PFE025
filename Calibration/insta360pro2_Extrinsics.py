from calib_functions import *
import cv2
import matlab.engine
import os 

from pathlib import Path
current_dir = Path(__file__).parent.resolve()
print(f"Working Directory: {current_dir}")

# Define paths
path1 = current_dir / "/Pictures/cam1/location/"
path2 = current_dir / "/Pictures/cam2/location/"
path3 = current_dir / "/Pictures/cam3/location/"

# Define array of paths
taken = [path1, path2, path3]
print(taken)

# Define pattern parameters
squareSizeInMM = 148.0

with open('cameras.txt', 'r') as f:
    cameraIPs = [line.rstrip() for line in f]

# Define url names
url1 = ['rtmp://'+cameraIPs[0]+'/live/origin1','rtmp://'+cameraIPs[0]+'/live/origin2','rtmp://'+cameraIPs[0]+'/live/origin3','rtmp://'+cameraIPs[0]+'/live/origin4','rtmp://'+cameraIPs[0]+'/live/origin5','rtmp://'+cameraIPs[0]+'/live/origin6']
url2 = ['rtmp://'+cameraIPs[1]+'/live/origin1','rtmp://'+cameraIPs[1]+'/live/origin2','rtmp://'+cameraIPs[1]+'/live/origin3','rtmp://'+cameraIPs[1]+'/live/origin4','rtmp://'+cameraIPs[1]+'/live/origin5','rtmp://'+cameraIPs[1]+'/live/origin6']
url3 = ['rtmp://'+cameraIPs[2]+'/live/origin1','rtmp://'+cameraIPs[2]+'/live/origin2','rtmp://'+cameraIPs[2]+'/live/origin3','rtmp://'+cameraIPs[2]+'/live/origin4','rtmp://'+cameraIPs[2]+'/live/origin5','rtmp://'+cameraIPs[2]+'/live/origin6']

urlAll = url1+url2+url3

# Remove images
for i in range(len(taken)):
    files = glob.glob("{}*.jpg".format(taken[i]))
    for f in files:
        os.remove(f)

'''
---------------------------------
CAPTURE IMAGES WITH PATTERN ON IT
---------------------------------
'''

# Initialize indexes
idx = 0
cam = 0

# Initialize cam view & file names
camView = [1,1,1]
filenames = [[],[],[]]

# Take new photos to localize the cameras
while idx<len(urlAll):
    print('Cam#{}'.format(cam+1))

    # Define url array for current camera
    url = urlAll[idx:idx+6]

    # Capture video of each view
    vcap = [cv2.VideoCapture(i) for i in url]
    iframe=1

    # Display a view, Capture images (C) and go to Next view (N)
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
                    filenames[cam] = "{}{}.jpg".format(taken[cam],iframe)
                    cv2.imwrite(filenames[cam], frame)
                    print('Photo saved')
                    iframe+=1
                    camView[cam] = i+1

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

print('\n---------------------------------------------------------------------------------------------------------\n')

# Camera localization

# Start MATLAB session
eng = matlab.engine.start_matlab()

# Camera calibration (modifier la fonction pour l'adapter au return de 3 matrices et non d'1 seul objet de type CameraParameters)
tvec1,tvec2,tvec3,rmtx1,rmtx2,rmtx3 = eng.LocateCameras(filenames[0],filenames[1],filenames[2],squareSizeInMM, nargout = 6)

# End MATLAB session
eng.quit()