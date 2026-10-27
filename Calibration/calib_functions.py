import numpy as np
import cv2
import glob


"""
CALIBRATION FUNCTIONS
"""

def CameraCalibration(nc,nr,path):
    # Prepare object points, like (0,0,0), (1,0,0), (2,0,0) ....,(nc,nr,0)
    objp = np.zeros((nr*nc,3), np.float32)
    objp[:,:2] = np.mgrid[0:nr,0:nc].T.reshape(-1,2)

    # Arrays to store object points and image points from all the images.
    objpoints = [] # 3d point in real world space
    imgpoints = [] # 2d points in image plane.


    # Lecture des images
    print("Reading images...")
    images = glob.glob(path)
    print("Done.")

    # Initialize total of good images for calibration
    total=0
    good=0

    # Recherche des coins
    print("Finding corners...")
    for fname in images:
        total+=1

        im=cv2.imread(fname)
        gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)   # Passage en gris

        # Find chess board corners
        ret, corners = cv2.findChessboardCorners(gray, (nr,nc), None)

        # If found, add object points, image points (after refining them)
        if ret == True:
            good+=1
            objpoints.append(objp)
            imgpoints.append(corners)

    print("Done.")
    print("{}{}{}{}".format("Images considered : ", good,"/",total))

    # Calibration
    print("Starting calibration...")
    ret, mtx, dist, rvecs, tvecs = cv2.calibrateCamera(objpoints, imgpoints, gray.shape[::-1], None, None)

    # Refine camera matrices using cv2.getOptimalNewCameraMatrix()
    h,  w = im.shape[:2]
    newcameramtx, roi = cv2.getOptimalNewCameraMatrix(mtx, dist, (w,h), 0, (w,h))
    print("Done.")

    return mtx,dist,newcameramtx,w,h,roi,imgpoints,objpoints,rvecs,tvecs


"""
UNDISTORTION FUNCTIONS
"""

# Classical undistortion
def UndistortClassic(images,mtx,dist,newcameramtx,w,h,roi,dest_path):
    idx=1
    for fname in images:
        img=cv2.imread(fname)
        # Create undistorted object
        dst = cv2.undistort(img, mtx, dist, None, newcameramtx)

        # Crop the image
        x, y, w, h = roi
        dst = dst[y:y+h, x:x+w]
        format='.jpg'
        filename = '{}{}{}'.format(dest_path,idx,format)
        cv2.imwrite('filename', dst)

        # Update index value
        idx+=1

    return



# Undistortion using remapping
def UndistortRemapping(images,mtx,dist,newcameramtx,w,h,roi,dest_path,cam):
    idx=1
    for fname in images:
        img = cv2.imread(fname)
        # Create undistorted object
        mapx, mapy = cv2.initUndistortRectifyMap(mtx, dist, None, newcameramtx, (w,h), 5)
        dst = cv2.remap(img, mapx, mapy, cv2.INTER_LINEAR)

        # Crop the image
        x, y, w, h = roi
        dst = dst[y:y+h, x:x+w]
        format='.jpg'
        filename = '{}cam{}_{}{}'.format(dest_path,cam,idx,format)
        cv2.imwrite(filename, dst)

        # Update index value
        idx+=1

    return