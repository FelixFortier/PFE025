# Launch origin live streams for 3 cameras
exec(open('LiveOriginMulti.py').read())

# Calibrate intrinsics & disortion
print('STARTING CALIBRAITON OF INTRINSIC PARAMETERS & DISTORTION')
exec(open('insta360pro2_IntrinsicsAndDistortion.py').read())

# Locate the cameras
print('STARTING CALIBRATION OF EXTRINSIC PARAMETERS')
exec(open('insta360pro2_Extrinsics.py').read())